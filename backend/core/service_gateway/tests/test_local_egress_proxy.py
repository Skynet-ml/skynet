"""Tests for the setup shell's egress proxy: secret placeholders go out as real values only to approved hosts."""

from __future__ import annotations

import datetime
import socket
import ssl
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from core.service_gateway.agents import local_egress_proxy as proxy_module
from core.service_gateway.agents.local_egress_proxy import LocalEgressProxy, SetupSecret, placeholder

APPROVED = "api.example.com"
OTHER = "other.example.com"
VALUE = "sk-test-real-value-0123456789"
SECRET = SetupSecret("API_KEY", VALUE, (APPROVED,))


def _cert(
    subject: str, issuer_key, issuer: x509.Name | None, key, *, ca: bool, sans: list[str] = ()
) -> x509.Certificate:
    """Make a test certificate.

    Args:
        subject: The common name.
        issuer_key: The key that signs it.
        issuer: The issuer name, or None for a self-signed one.
        key: The certificate's key.
        ca: Whether it is a CA.
        sans: The DNS names.

    Returns:
        The certificate.
    """
    now = datetime.datetime.now(datetime.UTC)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)])
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(issuer or name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(hours=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    )
    if sans:
        builder = builder.add_extension(x509.SubjectAlternativeName([x509.DNSName(s) for s in sans]), critical=False)
    return builder.sign(issuer_key, hashes.SHA256())


@dataclass
class Upstream:
    """A fake TLS host on loopback that keeps the decrypted requests it gets."""

    port: int
    ca_pem: bytes
    requests: list[bytes] = field(default_factory=list)


def _read_request(conn: ssl.SSLSocket) -> bytes:
    """Read one HTTP/1.1 request with its body.

    Args:
        conn: The server side of the connection.

    Returns:
        The request bytes.
    """
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(65_536)
        if not chunk:
            return data
        data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    lower = head.lower()
    if b"transfer-encoding: chunked" in lower:
        while not body.endswith(b"0\r\n\r\n"):
            chunk = conn.recv(65_536)
            if not chunk:
                break
            body += chunk
    elif b"content-length:" in lower:
        length = int(lower.split(b"content-length:")[1].split(b"\r\n")[0])
        while len(body) < length:
            chunk = conn.recv(65_536)
            if not chunk:
                break
            body += chunk
    return head + b"\r\n\r\n" + body


@pytest.fixture
def upstream(tmp_path: Path) -> Iterator[Upstream]:
    """Run a fake TLS host for the approved and the other host; ``/echo`` returns the request."""
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = _cert("test upstream CA", ca_key, None, ca_key, ca=True)
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = _cert(APPROVED, ca_key, ca.subject, key, ca=False, sans=[APPROVED, OTHER])
    (tmp_path / "up.pem").write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "up-key.pem").write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(tmp_path / "up.pem", tmp_path / "up-key.pem")
    listener = socket.create_server(("127.0.0.1", 0))
    state = Upstream(listener.getsockname()[1], ca.public_bytes(serialization.Encoding.PEM))

    def serve() -> None:
        """Answer each connection once."""
        while True:
            try:
                raw, _ = listener.accept()
            except OSError:
                return
            try:
                with context.wrap_socket(raw, server_side=True) as conn:
                    conn.settimeout(5)
                    request = _read_request(conn)
                    state.requests.append(request)
                    body = request if request.split(b" ")[1:2] == [b"/echo"] else b"fine"
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
            except (OSError, IndexError):
                continue

    threading.Thread(target=serve, daemon=True).start()
    yield state
    listener.close()


@pytest.fixture
def proxy(tmp_path: Path, upstream: Upstream, monkeypatch: pytest.MonkeyPatch) -> Iterator[LocalEgressProxy]:
    """Run a proxy whose example hosts lead to the fake upstream."""
    real_check = proxy_module.public_addresses

    def fake_public(host: str, port: int, *, public_only: bool = True):
        """Send the example hosts to the fake upstream; check all others for real.

        Args:
            host: The target host.
            port: The target port.
            public_only: Passed on to the real check.

        Returns:
            The addresses to connect to, or None when the host is not public.
        """
        if host in (APPROVED, OTHER):
            return [(socket.AF_INET, ("127.0.0.1", upstream.port))]
        return real_check(host, port, public_only=public_only)

    monkeypatch.setattr(proxy_module, "public_addresses", fake_public)
    secrets = [
        SECRET,
        SetupSecret("PLAIN_TOKEN", "plain-value"),
        SetupSecret("WILD", "wild-value-123", ("*.wild.test",)),
    ]
    egress = LocalEgressProxy(secrets, tmp_path / "state")
    egress._upstream_context = ssl.create_default_context(cadata=upstream.ca_pem.decode())
    with egress:
        yield egress


def _via_proxy(egress: LocalEgressProxy, host: str, request: bytes, *, trust: bytes) -> bytes:
    """Send one request through the proxy with ``CONNECT`` and TLS.

    Args:
        egress: The running proxy.
        host: The target host.
        request: The HTTP request.
        trust: The CA the client trusts.

    Returns:
        All response bytes, or the proxy's answer to the ``CONNECT``.
    """
    sock = socket.create_connection(("127.0.0.1", egress.port), timeout=10)
    sock.sendall(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n".encode())
    answer = sock.recv(4096)
    if not answer.startswith(b"HTTP/1.1 200"):
        sock.close()
        return answer
    context = ssl.create_default_context(cadata=trust.decode())
    with context.wrap_socket(sock, server_hostname=host) as tls:
        tls.sendall(request)
        data = b""
        while True:
            try:
                chunk = tls.recv(65_536)
            except (ssl.SSLError, OSError):
                break
            if not chunk:
                break
            data += chunk
    return data


def _calls(egress: LocalEgressProxy, count: int) -> list[dict]:
    """Wait for the proxy to record a number of calls, as it records one after the client closes.

    Args:
        egress: The running proxy.
        count: The number of calls to wait for.

    Returns:
        The records.
    """
    deadline = time.monotonic() + 10
    while len(egress.calls) < count and time.monotonic() < deadline:
        time.sleep(0.02)
    return list(egress.calls)


def _get(path: str, *, header: str) -> bytes:
    """Build a GET request with one ``Authorization`` header.

    Args:
        path: The target path.
        header: The header value.

    Returns:
        The request bytes.
    """
    return f"GET {path} HTTP/1.1\r\nHost: {APPROVED}\r\nAuthorization: Bearer {header}\r\n\r\n".encode()


def test_swaps_the_placeholder_in_a_header_for_the_approved_host(proxy: LocalEgressProxy, upstream: Upstream) -> None:
    """Check that the approved host gets the real value in the header, and the client gets the response."""
    response = _via_proxy(proxy, APPROVED, _get("/v1/models", header=placeholder("API_KEY")), trust=proxy.ca_pem)
    assert response.startswith(b"HTTP/1.1 200")
    assert response.endswith(b"fine")
    assert f"Authorization: Bearer {VALUE}".encode() in upstream.requests[-1]
    assert placeholder("API_KEY").encode() not in upstream.requests[-1]
    assert _calls(proxy, 1)[-1]["mode"] == "inspect"
    assert _calls(proxy, 1)[-1]["status"] == "ok"


def test_other_host_gets_the_placeholder_unchanged(proxy: LocalEgressProxy, upstream: Upstream) -> None:
    """Check that a host the secret does not approve gets a plain tunnel, without the real value."""
    request = f"GET /echo HTTP/1.1\r\nHost: {OTHER}\r\nX: {placeholder('API_KEY')}\r\n\r\n".encode()
    response = _via_proxy(proxy, OTHER, request, trust=upstream.ca_pem)
    assert placeholder("API_KEY").encode() in upstream.requests[-1]
    assert VALUE.encode() not in upstream.requests[-1]
    assert VALUE.encode() not in response
    assert _calls(proxy, 1)[-1]["mode"] == "tunnel"


def test_placeholder_in_the_url_is_refused(proxy: LocalEgressProxy, upstream: Upstream) -> None:
    """Check that a placeholder in the URL fails the request, and the host gets nothing."""
    request = f"GET /v1?key={placeholder('API_KEY')} HTTP/1.1\r\nHost: {APPROVED}\r\n\r\n".encode()
    response = _via_proxy(proxy, APPROVED, request, trust=proxy.ca_pem)
    assert response.startswith(b"HTTP/1.1 403")
    assert b"secret API_KEY can go only in a request header" in response
    assert upstream.requests == []
    assert _calls(proxy, 1)[-1]["reason"] == "secret API_KEY can go only in a request header"


@pytest.mark.parametrize("chunked", [False, True])
def test_placeholder_in_the_body_is_refused(proxy: LocalEgressProxy, upstream: Upstream, chunked: bool) -> None:
    """Check that a placeholder in a plain or chunked body fails the request before the body goes out."""
    body = ("x" * 70_000 + placeholder("API_KEY") + "y" * 10).encode()
    if chunked:
        # The placeholder spans two chunks, so the scan must join them.
        cut = 70_005
        framed = b"%x\r\n%s\r\n%x\r\n%s\r\n0\r\n\r\n" % (cut, body[:cut], len(body) - cut, body[cut:])
        head = f"POST /v1 HTTP/1.1\r\nHost: {APPROVED}\r\nTransfer-Encoding: chunked\r\n\r\n".encode()
    else:
        framed = body
        head = f"POST /v1 HTTP/1.1\r\nHost: {APPROVED}\r\nContent-Length: {len(body)}\r\n\r\n".encode()
    response = _via_proxy(proxy, APPROVED, head + framed, trust=proxy.ca_pem)
    assert response.startswith(b"HTTP/1.1 403")
    assert b"secret API_KEY can go only in a request header" in response
    assert all(VALUE.encode() not in sent and placeholder("API_KEY").encode() not in sent for sent in upstream.requests)


def test_echo_of_the_real_value_is_cut(proxy: LocalEgressProxy) -> None:
    """Check that a response that holds the swapped value never reaches the command."""
    response = _via_proxy(proxy, APPROVED, _get("/echo", header=placeholder("API_KEY")), trust=proxy.ca_pem)
    assert VALUE.encode() not in response
    assert _calls(proxy, 1)[-1]["status"] == "failed"
    assert "API_KEY" in _calls(proxy, 1)[-1]["reason"]


def test_unapproved_placeholder_on_an_approved_host_stays(proxy: LocalEgressProxy, upstream: Upstream) -> None:
    """Check that a secret approved only for other hosts is not swapped on this host."""
    _via_proxy(proxy, APPROVED, _get("/v1", header=placeholder("WILD")), trust=proxy.ca_pem)
    assert placeholder("WILD").encode() in upstream.requests[-1]
    assert b"wild-value-123" not in upstream.requests[-1]


@pytest.mark.parametrize("target", ["127.0.0.1", "localhost", "169.254.169.254", "10.0.0.1", "[::1]"])
def test_private_targets_are_refused(proxy: LocalEgressProxy, target: str) -> None:
    """Check that loopback, link-local and private targets get 403 and no tunnel."""
    sock = socket.create_connection(("127.0.0.1", proxy.port), timeout=10)
    sock.sendall(f"CONNECT {target}:443 HTTP/1.1\r\n\r\n".encode())
    answer = sock.recv(4096)
    sock.close()
    assert answer.startswith(b"HTTP/1.1 403")
    assert _calls(proxy, 1)[-1]["status"] == "refused"


def test_plain_http_to_loopback_is_refused(proxy: LocalEgressProxy) -> None:
    """Check that a plain http:// request to a private host is refused too."""
    sock = socket.create_connection(("127.0.0.1", proxy.port), timeout=10)
    sock.sendall(b"GET http://127.0.0.1:8000/ HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
    answer = sock.recv(4096)
    sock.close()
    assert answer.startswith(b"HTTP/1.1 403")


def test_records_never_hold_a_value(proxy: LocalEgressProxy, caplog: pytest.LogCaptureFixture) -> None:
    """Check that the call records and the log name hosts and counts, never a value or a path."""
    caplog.set_level("INFO", logger=proxy_module.__name__)
    _via_proxy(proxy, APPROVED, _get("/secret-path", header=placeholder("API_KEY")), trust=proxy.ca_pem)
    _via_proxy(proxy, APPROVED, _get("/echo", header=placeholder("API_KEY")), trust=proxy.ca_pem)
    record = _calls(proxy, 2)[0]
    assert record["host"] == APPROVED
    assert record["bytes_sent"] > 0
    assert record["bytes_received"] > 0
    text = caplog.text + repr(list(proxy.calls))
    assert VALUE not in text
    assert "secret-path" not in text
    assert "skynet-egress-call" in caplog.text


def test_command_env_has_placeholders_and_plain_values(proxy: LocalEgressProxy, tmp_path: Path) -> None:
    """Check that hosted secrets are placeholders, secrets without hosts are values, and the CA is set."""
    env = proxy.command_env(tmp_path / "ca.pem")
    assert env["API_KEY"] == "skynet-secret-API_KEY"
    assert env["PLAIN_TOKEN"] == "plain-value"
    assert VALUE not in "".join(env.values())
    assert env["HTTPS_PROXY"] == f"http://127.0.0.1:{proxy.port}"
    for key in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS", "PIP_CERT"):
        assert env[key] == str(tmp_path / "ca.pem")


def test_ca_bundle_keeps_public_roots_and_adds_the_session_ca(proxy: LocalEgressProxy, tmp_path: Path) -> None:
    """Check that the command's CA file trusts both public hosts and the session CA."""
    proxy.write_ca_bundle(tmp_path / "ca.pem")
    bundle = (tmp_path / "ca.pem").read_bytes()
    assert proxy.ca_pem in bundle
    assert bundle.count(b"BEGIN CERTIFICATE") > 10


def test_ca_key_never_on_disk_and_state_deleted_on_stop(tmp_path: Path) -> None:
    """Check that the CA key is in no file, the CA is name-constrained, and stop deletes the keys."""
    egress = LocalEgressProxy([SECRET], tmp_path / "state").start()
    state = egress._state
    assert state is not None
    assert state.is_relative_to(tmp_path / "state")
    egress._leaf_context(APPROVED)
    ca_key = egress._ca_key.private_bytes(
        serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    raw_scalar = egress._ca_key.private_numbers().private_value.to_bytes(32, "big")
    for path in state.iterdir():
        data = path.read_bytes()
        assert ca_key not in data
        assert raw_scalar not in data
        assert b"PRIVATE KEY" not in data or path.name == "leaf-key.pem"
    constraints = x509.load_pem_x509_certificate(egress.ca_pem).extensions.get_extension_for_class(x509.NameConstraints)
    assert constraints.critical
    assert [name.value for name in constraints.value.permitted_subtrees] == [APPROVED]
    egress.stop()
    assert not state.exists()
    assert egress._ca_key is None


@pytest.mark.parametrize(
    "secrets",
    [
        [SetupSecret("BAD-NAME", "v", ("a.com",))],
        [SetupSecret("A", "line\nbreak", ("a.com",))],
        [SetupSecret("A", "", ("a.com",))],
        [SetupSecret("A", "x"), SetupSecret("A", "y")],
    ],
)
def test_bad_secrets_are_refused(secrets: list[SetupSecret], tmp_path: Path) -> None:
    """Check that bad names, repeated names and values that cannot go in a header are refused."""
    with pytest.raises(ValueError):
        LocalEgressProxy(secrets, tmp_path)
