"""Tests for the setup shell's egress proxy: secret placeholders go out as real values only to approved hosts."""

from __future__ import annotations

import base64
import contextlib
import datetime
import gzip
import http.client
import io
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import requests
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
    subject: str, issuer_key, issuer: x509.Name | None, key, *, ca: bool, sans: Sequence[str] = ()
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


def _answer_for(request: bytes) -> bytes:
    """Build the fake host's response to one request.

    ``/echo`` returns the request, ``/gzip-echo`` returns it marked as gzip, and
    ``/chunked-echo`` returns it in chunks that split the real value. ``/dup-gzip-echo``
    and ``/dup-te-echo`` return it gzipped, behind a repeated encoding header that ends in a plain one.
    The ``/lf-*`` paths hide the framing behind bare line feeds, which many clients accept.

    Args:
        request: The request the host got.

    Returns:
        The response bytes.
    """
    path = request.split(b" ")[1:2]
    if path == [b"/lf-gzip-echo"]:
        body = gzip.compress(request)
        return b"HTTP/1.1 200 OK\r\nX-A: a\nContent-Encoding: gzip\r\nContent-Length: %d\r\n\r\n" % len(body) + body
    if path == [b"/lf-te-echo"]:
        cut = request.find(VALUE.encode()) + 5
        chunks = b"".join(b"%x\r\n%s\r\n" % (len(part), part) for part in (request[:cut], request[cut:]))
        return b"HTTP/1.1 200 OK\r\nX-A: a\nTransfer-Encoding: chunked\r\n\r\n" + chunks + b"0\r\n\r\n"
    if path == [b"/lf-end-echo"]:
        body = gzip.compress(request)
        return b"HTTP/1.1 200 OK\nContent-Encoding: gzip\nContent-Length: %d\n\n" % len(body) + body + b"\r\n\r\n"
    if path == [b"/dup-gzip-echo"]:
        body = gzip.compress(request)
        head = b"Content-Encoding: gzip\r\nContent-Encoding: identity\r\nContent-Length: %d\r\n" % len(body)
        return b"HTTP/1.1 200 OK\r\n" + head + b"\r\n" + body
    if path == [b"/dup-te-echo"]:
        body = gzip.compress(request)
        head = b"Transfer-Encoding: gzip\r\nTransfer-Encoding: chunked\r\n"
        return b"HTTP/1.1 200 OK\r\n" + head + b"\r\n%x\r\n%s\r\n0\r\n\r\n" % (len(body), body)
    if path == [b"/gzip-echo"]:
        return b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: %d\r\n\r\n" % len(request) + request
    if path == [b"/chunked-echo"]:
        cut = request.find(VALUE.encode()) + 5 if VALUE.encode() in request else len(request) // 2
        chunks = b"".join(b"%x\r\n%s\r\n" % (len(part), part) for part in (request[:cut], request[cut:]) if part)
        return b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + chunks + b"0\r\n\r\n"
    body = request if path == [b"/echo"] else b"fine"
    return b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body) + body


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
                    conn.sendall(_answer_for(request))
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
    with _open_tls(egress, host, trust=trust) as tls:
        if isinstance(tls, bytes):
            return tls
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


def _auth(egress: LocalEgressProxy) -> str:
    """Return the session's ``Proxy-Authorization`` header line.

    Args:
        egress: The running proxy.

    Returns:
        The header line with its line break.
    """
    return f"Proxy-Authorization: Basic {egress._proxy_auth.decode()}\r\n"


@contextlib.contextmanager
def _open_tls(egress: LocalEgressProxy, host: str, *, trust: bytes) -> Iterator[ssl.SSLSocket | bytes]:
    """Open a ``CONNECT`` tunnel with the session credentials and start TLS in it.

    Args:
        egress: The running proxy.
        host: The target host.
        trust: The CA the client trusts.

    Yields:
        The client TLS socket, or the proxy's answer when it refused the ``CONNECT``.
    """
    sock = socket.create_connection(("127.0.0.1", egress.port), timeout=10)
    sock.sendall(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n{_auth(egress)}\r\n".encode())
    answer = sock.recv(4096)
    if not answer.startswith(b"HTTP/1.1 200"):
        sock.close()
        yield answer
        return
    context = ssl.create_default_context(cadata=trust.decode())
    with context.wrap_socket(sock, server_hostname=host) as tls:
        yield tls


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
    """Check that a placeholder in a plain or chunked body fails the request.

    The host may get the body bytes before the placeholder, but never the placeholder or the value.
    """
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
    sock.sendall(f"CONNECT {target}:443 HTTP/1.1\r\n{_auth(proxy)}\r\n".encode())
    answer = sock.recv(4096)
    sock.close()
    assert answer.startswith(b"HTTP/1.1 403")
    assert _calls(proxy, 1)[-1]["status"] == "refused"


def test_plain_http_to_loopback_is_refused(proxy: LocalEgressProxy) -> None:
    """Check that a plain http:// request to a private host is refused too."""
    sock = socket.create_connection(("127.0.0.1", proxy.port), timeout=10)
    sock.sendall(f"GET http://127.0.0.1:8000/ HTTP/1.1\r\nHost: 127.0.0.1\r\n{_auth(proxy)}\r\n".encode())
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
    assert env["HTTPS_PROXY"] == proxy.proxy_url
    assert env["NODE_USE_ENV_PROXY"] == "1"
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
    assert egress._ca_key is not None
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
    assert [name.value for name in constraints.value.permitted_subtrees or []] == [APPROVED]
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


def _with_key(head: str) -> bytes:
    """Add the API_KEY placeholder header and the blank line to a request head.

    Args:
        head: The request line and headers, each line with its line break.

    Returns:
        The request bytes.
    """
    return f"{head}Authorization: Bearer {placeholder('API_KEY')}\r\n\r\n".encode()


@pytest.mark.parametrize(
    "request_head",
    [
        f"GET / HTTP/1.1\r\nHost: {OTHER}\r\n",
        f"GET https://{OTHER}/ HTTP/1.1\r\nHost: {APPROVED}\r\n",
        f"GET {OTHER}:443 HTTP/1.1\r\nHost: {APPROVED}\r\n",
        f"GET / HTTP/1.1\r\nHost: {APPROVED}\r\nHost: {OTHER}\r\n",
        f"GET / HTTP/1.1\r\nHost: {APPROVED}:8443\r\n",
        "GET / HTTP/1.1\r\n",
    ],
)
def test_request_for_another_host_in_the_tunnel_is_refused(
    proxy: LocalEgressProxy, upstream: Upstream, request_head: str
) -> None:
    """Check that a Host header or target that does not name the CONNECT host gets 400 before any swap."""
    response = _via_proxy(proxy, APPROVED, _with_key(request_head), trust=proxy.ca_pem)
    assert response.startswith(b"HTTP/1.1 400")
    assert upstream.requests == []
    assert _calls(proxy, 1)[-1]["status"] == "refused"


@pytest.mark.parametrize("host_header", [f"{APPROVED}:443", "API.Example.com."])
def test_host_header_with_the_connect_port_or_case_is_accepted(
    proxy: LocalEgressProxy, upstream: Upstream, host_header: str
) -> None:
    """Check that the Host header may name the CONNECT port, and may differ in case or a trailing dot."""
    response = _via_proxy(proxy, APPROVED, _with_key(f"GET / HTTP/1.1\r\nHost: {host_header}\r\n"), trust=proxy.ca_pem)
    assert response.startswith(b"HTTP/1.1 200")
    assert f"Authorization: Bearer {VALUE}".encode() in upstream.requests[-1]


def test_encoding_and_range_headers_are_not_forwarded(proxy: LocalEgressProxy, upstream: Upstream) -> None:
    """Check that the host gets ``Accept-Encoding: identity`` and no range, so the echo scan sees plain bytes."""
    head = f"GET / HTTP/1.1\r\nHost: {APPROVED}\r\nAccept-Encoding: gzip, br\r\nRange: bytes=3-\r\nIf-Range: x\r\n"
    _via_proxy(proxy, APPROVED, _with_key(head), trust=proxy.ca_pem)
    sent = upstream.requests[-1].lower()
    assert b"accept-encoding: identity\r\n" in sent
    assert b"gzip" not in sent
    assert b"range:" not in sent


@pytest.mark.parametrize("path", ["/gzip-echo", "/chunked-echo"])
def test_encoded_or_chunked_echo_of_the_real_value_is_cut(proxy: LocalEgressProxy, path: str) -> None:
    """Check that an encoded echo is cut, and a chunked echo is scanned after the chunks are joined."""
    response = _via_proxy(proxy, APPROVED, _get(path, header=placeholder("API_KEY")), trust=proxy.ca_pem)
    assert VALUE.encode() not in response
    assert VALUE[5:].encode() not in response
    assert _calls(proxy, 1)[-1]["status"] == "failed"


def _gunzip_body(response: bytes) -> bytes:
    """Return what a client could gunzip from a response body, chunked or not.

    Args:
        response: The response bytes the client got.

    Returns:
        The decompressed data, or empty bytes when there is none.
    """
    head, _, body = response.partition(b"\r\n\r\n")
    if b"transfer-encoding: chunked" in head.lower():
        data = b""
        with contextlib.suppress(ValueError):
            while body:
                size, _, body = body.partition(b"\r\n")
                data, body = data + body[: int(size, 16)], body[int(size, 16) + 2 :]
        body = data
    with contextlib.suppress(zlib.error):
        return zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(body)
    return b""


@pytest.mark.parametrize("path", ["/dup-gzip-echo", "/dup-te-echo"])
def test_repeated_encoding_headers_cannot_hide_an_echo(proxy: LocalEgressProxy, path: str) -> None:
    """Check that a repeated encoding header that ends in a plain one cannot pass a gzipped echo to the client."""
    response = _via_proxy(proxy, APPROVED, _get(path, header=placeholder("API_KEY")), trust=proxy.ca_pem)
    assert VALUE.encode() not in _gunzip_body(response)
    record = _calls(proxy, 1)[-1]
    assert record["status"] == "failed"
    assert VALUE not in record["reason"]


class _BytesSocket:
    """A socket stand-in that lets ``http.client`` parse bytes the client already got."""

    def __init__(self, data: bytes) -> None:
        """Keep the bytes.

        Args:
            data: The response bytes.
        """
        self._file = io.BytesIO(data)

    def makefile(self, *_args: object, **_kwargs: object) -> io.BytesIO:
        """Return the bytes as a file.

        Args:
            _args: Ignored.
            _kwargs: Ignored.

        Returns:
            The file.
        """
        return self._file


def _lenient_body(response: bytes) -> bytes:
    """Decode a response as ``http.client`` does, which accepts bare line feeds, then gunzip it.

    Args:
        response: The response bytes the client got.

    Returns:
        The decoded body, or empty bytes when the client could not read one.
    """
    parsed = http.client.HTTPResponse(_BytesSocket(response))  # type: ignore[arg-type]
    try:
        parsed.begin()
        body = parsed.read()
    except (http.client.HTTPException, ValueError, OSError):
        return b""
    if parsed.getheader("content-encoding") == "gzip":
        with contextlib.suppress(zlib.error):
            return zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(body)
    return body


@pytest.mark.parametrize("path", ["/lf-gzip-echo", "/lf-te-echo", "/lf-end-echo"])
def test_bare_line_feeds_in_the_response_head_cannot_hide_an_echo(proxy: LocalEgressProxy, path: str) -> None:
    """Check that framing hidden behind a bare line feed cannot pass an echo to a lenient client."""
    response = _via_proxy(proxy, APPROVED, _get(path, header=placeholder("API_KEY")), trust=proxy.ca_pem)
    assert VALUE.encode() not in _lenient_body(response)
    assert VALUE.encode() not in response
    assert _calls(proxy, 1)[-1]["status"] == "failed"


def test_stop_keeps_tls_on_a_socket_that_sends_during_shutdown(
    proxy: LocalEgressProxy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Check that a send that races stop() goes out encrypted or fails, never in plain text."""
    raw_client, raw_server = socket.socketpair()
    server = proxy._leaf_context(APPROVED).wrap_socket(raw_server, server_side=True, do_handshake_on_connect=False)
    handshake = threading.Thread(target=server.do_handshake)
    handshake.start()
    client = ssl.create_default_context(cadata=proxy.ca_pem.decode()).wrap_socket(raw_client, server_hostname=APPROVED)
    handshake.join()
    proxy._track(client)
    real_shutdown = socket.socket.shutdown

    def racing_shutdown(sock: socket.socket, how: int) -> None:
        """Send from the TLS socket in the middle of its shutdown, as a request thread could.

        Args:
            sock: The socket to shut down.
            how: The shutdown mode.
        """
        if sock is client:
            with contextlib.suppress(OSError):
                client.sendall(b"Authorization: Bearer RACE-SECRET")
        real_shutdown(sock, how)

    monkeypatch.setattr(socket.socket, "shutdown", racing_shutdown)
    proxy.stop()
    monkeypatch.setattr(socket.socket, "shutdown", real_shutdown)
    server.settimeout(5)
    assert b"RACE-SECRET" not in socket.socket.recv(server, 65_536)
    client.close()
    server.close()


def test_chunked_response_without_a_value_is_relayed(proxy: LocalEgressProxy) -> None:
    """Check that a chunked response with no swapped value reaches the client whole, in chunks."""
    request = f"GET /chunked-echo HTTP/1.1\r\nHost: {APPROVED}\r\nX: {placeholder('WILD')}\r\n\r\n".encode()
    response = _via_proxy(proxy, APPROVED, request, trust=proxy.ca_pem)
    head, _, body = response.partition(b"\r\n\r\n")
    assert b"Transfer-Encoding: chunked" in head
    data = b""
    while body:
        size, _, body = body.partition(b"\r\n")
        data, body = data + body[: int(size, 16)], body[int(size, 16) + 2 :]
    assert placeholder("WILD").encode() in data
    assert data.endswith(b"Connection: close\r\n\r\n")


@pytest.mark.parametrize(
    ("framing", "reason"),
    [
        ("Transfer-Encoding: chunked\r\n\r\n5;ext=1\r\nhello\r\n0\r\n\r\n", b"chunk extensions are not allowed"),
        ("Transfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\nX-Trailer: 1\r\n\r\n", b"trailers are not allowed"),
        ("Content-Length: 5\r\nContent-Length: 5\r\n\r\nhello", b"more than one content-length header"),
        ("Transfer-Encoding: chunked\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n", b"more than one transfer"),
    ],
)
def test_ambiguous_body_framing_is_refused(
    proxy: LocalEgressProxy, upstream: Upstream, framing: str, reason: bytes
) -> None:
    """Check that chunk extensions, trailers and repeated framing headers get 400."""
    request = f"POST / HTTP/1.1\r\nHost: {APPROVED}\r\n{framing}".encode()
    response = _via_proxy(proxy, APPROVED, request, trust=proxy.ca_pem)
    assert response.startswith(b"HTTP/1.1 400")
    assert reason in response
    assert all(b"ext=1" not in sent and b"X-Trailer" not in sent for sent in upstream.requests)


@pytest.mark.parametrize("auth", ["", "Proxy-Authorization: Basic d3Jvbmc6d3Jvbmc=\r\n"])
def test_connect_without_the_session_credentials_is_refused(proxy: LocalEgressProxy, auth: str) -> None:
    """Check that a ``CONNECT`` with no or wrong credentials gets 407 and opens no tunnel."""
    sock = socket.create_connection(("127.0.0.1", proxy.port), timeout=10)
    sock.sendall(f"CONNECT {APPROVED}:443 HTTP/1.1\r\nHost: {APPROVED}:443\r\n{auth}\r\n".encode())
    answer = sock.recv(4096)
    sock.close()
    assert answer.startswith(b"HTTP/1.1 407")
    assert b'Proxy-Authenticate: Basic realm="skynet"' in answer
    assert _calls(proxy, 1)[-1]["status"] == "refused"


def test_plain_http_without_the_session_credentials_is_refused(proxy: LocalEgressProxy) -> None:
    """Check that a plain ``http://`` request with no credentials gets 407."""
    sock = socket.create_connection(("127.0.0.1", proxy.port), timeout=10)
    sock.sendall(f"GET http://{APPROVED}/ HTTP/1.1\r\nHost: {APPROVED}\r\n\r\n".encode())
    answer = sock.recv(4096)
    sock.close()
    assert answer.startswith(b"HTTP/1.1 407")


def test_proxy_url_holds_the_session_credentials(proxy: LocalEgressProxy) -> None:
    """Check that the env URL carries the credentials that the proxy checks, and each session has its own."""
    url = urllib.parse.urlsplit(proxy.proxy_url)
    assert (url.hostname, url.port) == ("127.0.0.1", proxy.port)
    assert base64.b64encode(f"{url.username}:{url.password}".encode()) == proxy._proxy_auth
    assert len(url.password or "") >= 32
    other = LocalEgressProxy([SECRET], proxy.state_dir).start()
    try:
        assert other.proxy_url != proxy.proxy_url
    finally:
        other.stop()


@pytest.mark.parametrize("client", ["urllib", "requests"])
def test_python_clients_send_the_credentials_from_the_env_url(proxy: LocalEgressProxy, client: str) -> None:
    """Check that urllib (pip's base) and requests send the env URL's credentials, so they get 403, not 407."""
    target = "http://127.0.0.1:9/"
    if client == "urllib":
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy.proxy_url}))
        with pytest.raises(urllib.error.HTTPError) as error:
            opener.open(target, timeout=10)
        code = error.value.code
    else:
        code = requests.get(target, proxies={"http": proxy.proxy_url}, timeout=10).status_code
    assert code == 403


def test_proxy_listens_on_ipv4_and_ipv6_loopback(proxy: LocalEgressProxy) -> None:
    """Check that the proxy holds the port on both loopback addresses the sandbox rule allows."""
    families = {listener.family for listener in proxy._listeners}
    assert socket.AF_INET in families
    if socket.has_ipv6:
        assert socket.AF_INET6 in families
        with socket.create_connection(("::1", proxy.port), timeout=10) as sock:
            sock.sendall(f"CONNECT {APPROVED}:443 HTTP/1.1\r\n\r\n".encode())
            assert sock.recv(4096).startswith(b"HTTP/1.1 407")


def test_stop_closes_an_open_inspected_connection(proxy: LocalEgressProxy) -> None:
    """Check that stop() ends a client TLS connection that waits for its request."""
    with _open_tls(proxy, APPROVED, trust=proxy.ca_pem) as tls:
        assert isinstance(tls, ssl.SSLSocket)
        started = time.monotonic()
        proxy.stop()
        try:
            data = tls.recv(4096)
        except (ssl.SSLError, OSError):
            data = b""
        assert data == b""
        assert time.monotonic() - started < 5


def test_no_swap_after_stop(proxy: LocalEgressProxy, upstream: Upstream) -> None:
    """Check that a request that ends after stop() never sends the real value."""
    with _open_tls(proxy, APPROVED, trust=proxy.ca_pem) as tls:
        assert isinstance(tls, ssl.SSLSocket)
        request = _get("/echo", header=placeholder("API_KEY"))
        tls.sendall(request[:-2])
        time.sleep(0.2)
        proxy.stop()
        time.sleep(1)
        with contextlib.suppress(ssl.SSLError, OSError):
            tls.sendall(request[-2:])
            tls.recv(4096)
    time.sleep(0.5)
    assert all(VALUE.encode() not in sent for sent in upstream.requests)
    with pytest.raises(OSError):
        proxy._leaf_context(OTHER)
