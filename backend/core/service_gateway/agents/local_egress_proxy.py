"""Open egress for the Mac app's setup commands, with secret placeholders that only approved hosts get.

A setup command runs under a ``sandbox-exec`` profile that lets it connect
only to this proxy, on a loopback port (:mod:`.local_command_runner`). The
proxy is the command's only way out, and it reaches any public host. It
refuses private, loopback and link-local addresses with the public-address
check of :mod:`~core.service_gateway.optimization.blackbox.docker_egress_proxy`,
so the Mac's own services stay out of reach, and it connects to the address it
checked.

A saved secret with hosts never enters the command environment. The command
sees ``skynet-secret-<NAME>`` instead. For a ``CONNECT`` to a host that some
secret approves, the proxy ends TLS with a leaf certificate from a CA that it
makes for this session only, reads the HTTP/1.1 request, and puts the real
value in place of the placeholder only in a request header, and only for a
secret whose ``allowed_hosts`` has the host. This matches the run relay, which
fills ``${NAME}`` only in declared headers. The proxy never changes the URL or
the body: a placeholder of an approved secret there fails the request with
"secret NAME can go only in a request header". A ``CONNECT`` to any other
host is a plain tunnel, so its bytes, and any placeholder in them, go out
unchanged. A plain ``http://`` request is forwarded without any swap.

The CA's key stays in memory. The leaf key and certificates are files in a
folder of the app's state folder, which the command cannot read, and
:meth:`LocalEgressProxy.stop` deletes that folder. Only the command
environment trusts the CA (``SSL_CERT_FILE`` and the like), and the CA can
sign only for the approved hosts (a name constraint).

Each request gives one record with the host, port, outcome and byte counts,
in the JSON line shape of the Docker egress proxy. A record never holds a
path, a header or a value.
"""

from __future__ import annotations

import base64
import contextlib
import datetime
import errno
import io
import ipaddress
import itertools
import json
import logging
import re
import shutil
import socket
import ssl
import tempfile
import threading
import time
from collections import deque
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from hmac import compare_digest
from pathlib import Path
from secrets import token_urlsafe
from typing import BinaryIO
from urllib.parse import urlsplit

import certifi
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from ...billing.env_connectors import host_allowed
from ...models.blackbox import REPO_SECRET_NAME_PATTERN
from ..optimization.blackbox.docker_egress_proxy import (
    CALL_EVENT,
    _open_upstream,
    _pipe,
    _read_head,
    _split_connect,
    parse_connect,
    public_addresses,
)

logger = logging.getLogger(__name__)

PLACEHOLDER_PREFIX = "skynet-secret-"
# The lookahead keeps ``skynet-secret-AB`` from matching the secret ``A``.
_PLACEHOLDER = re.compile(rb"skynet-secret-([A-Za-z_][A-Za-z0-9_]{0,127})(?![A-Za-z0-9_])")
# The longest placeholder; a body scan keeps this many bytes from one piece for the next.
_PLACEHOLDER_MAX = len(PLACEHOLDER_PREFIX) + 128
_NAME = re.compile(REPO_SECRET_NAME_PATTERN)
_CHUNK_SIZE = re.compile(rb"[0-9A-Fa-f]{1,16}")
_HEAD_LIMIT = 65_536
_HANDSHAKE_SECONDS = 30.0
_IO_SECONDS = 300.0
_IDLE_SECONDS = 300.0
MAX_CLIENTS = 128
_CALL_HISTORY = 1000
# A shorter value would cut ordinary responses that happen to hold it.
_ECHO_GUARD_MIN = 8
_UNICODE_ESCAPE = re.compile(rb"\\u([0-9a-fA-F]{4})")
_CA_DAYS = 2
_HOP_HEADERS = frozenset(
    {b"connection", b"keep-alive", b"proxy-connection", b"proxy-authorization", b"expect", b"upgrade", b"te"}
)
# An encoded or partial response could carry a swapped value past the echo guard.
_INSPECT_DROP = _HOP_HEADERS | {b"accept-encoding", b"range", b"if-range"}
# A second copy of these could make the upstream read a request the proxy did not check.
_SINGLE_HEADERS = (b"host", b"content-length", b"transfer-encoding")
_RESPONSE_SINGLE_HEADERS = (b"content-encoding", b"content-length", b"transfer-encoding")
_HOST_HEADER = re.compile(rb"(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._-]+)(?::([0-9]{1,5}))?")
_PROXY_USER = "skynet"
_ESTABLISHED = b"HTTP/1.1 200 Connection Established\r\n\r\n"
_CONTINUE = b"HTTP/1.1 100 Continue\r\n\r\n"
_PHRASES = {400: "Bad Request", 403: "Forbidden", 407: "Proxy Authentication Required", 502: "Bad Gateway"}
_PROXY_CHALLENGE = 'Proxy-Authenticate: Basic realm="skynet"\r\n'


@dataclass(frozen=True)
class SetupSecret:
    """A saved secret that a setup session may use."""

    name: str
    value: str
    # Exact hosts or ``*.suffix``; empty means the command gets the plain value (SKY-994).
    allowed_hosts: tuple[str, ...] = ()


def placeholder(name: str) -> str:
    """Return the text that stands for a hosted secret in the command environment.

    Args:
        name: The secret's environment variable name.

    Returns:
        ``skynet-secret-<name>``.
    """
    return f"{PLACEHOLDER_PREFIX}{name}"


class _RefusalError(Exception):
    """A request that the proxy answers with an error and does not send on."""

    def __init__(self, code: int, reason: str) -> None:
        """Keep the answer code and the reason.

        Args:
            code: The HTTP status of the answer.
            reason: The reason; it names a secret at most, never a value.
        """
        super().__init__(reason)
        self.code = code
        self.reason = reason


class _CutError(Exception):
    """A response that could give a real secret value to the command, so the proxy stopped it."""


def _answer(code: int, reason: str) -> bytes:
    """Write a short plain-text HTTP answer that closes the connection.

    Args:
        code: The HTTP status.
        reason: The text of the body.

    Returns:
        The answer bytes.
    """
    body = f"Skynet egress proxy: {reason}\n".encode()
    head = (
        f"HTTP/1.1 {code} {_PHRASES.get(code, 'Error')}\r\nContent-Type: text/plain; charset=utf-8\r\n"
        f"{_PROXY_CHALLENGE if code == 407 else ''}Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    )
    return head.encode() + body


def _parse_head(head: bytes) -> tuple[list[bytes], list[tuple[bytes, bytes]]]:
    """Split a request head into its request line parts and its headers.

    Args:
        head: The head up to and including the blank line.

    Returns:
        The method, target and version, and the ``(name, value)`` headers.

    Raises:
        _RefusalError: For a malformed line, so a bare line break cannot start a
            header that the upstream would read and the proxy would not.
    """
    lines = head.removesuffix(b"\r\n\r\n").split(b"\r\n")
    parts = lines[0].split(b" ")
    if len(parts) != 3 or parts[2] not in (b"HTTP/1.1", b"HTTP/1.0") or any(c in lines[0] for c in b"\n\0"):
        raise _RefusalError(400, "bad request line")
    headers = []
    for line in lines[1:]:
        name, sep, value = line.partition(b":")
        if not sep or not name or name != name.strip() or any(c in line for c in b"\r\n\0"):
            raise _RefusalError(400, "bad request header")
        headers.append((name, value.strip(b" \t")))
    return parts, headers


def _strict_response_head(head: bytes) -> bool:
    """Tell whether a response head has only CRLF line breaks and plain ``name: value`` headers.

    A client that accepts a bare line feed, a folded line or a padded name can
    read headers or a head end that the proxy's checks did not see.

    Args:
        head: The response head up to and including the blank line.

    Returns:
        Whether the head is strict.
    """
    lines = head.removesuffix(b"\r\n\r\n").split(b"\r\n")
    if any(c in line for line in lines for c in b"\r\n\0"):
        return False
    for line in lines[1:]:
        name, sep, _ = line.partition(b":")
        if not sep or not name or name != name.strip(b" \t"):
            return False
    return True


def _read_tls_head(reader: BinaryIO) -> bytes | None:
    """Read one request head from the client's TLS stream.

    Args:
        reader: The buffered client stream.

    Returns:
        The head with its blank line, or None when the client closed first.

    Raises:
        _RefusalError: When the head is too long.
    """
    head = b""
    while True:
        line = reader.readline(_HEAD_LIMIT + 1)
        if not line:
            return None
        head += line
        if len(head) > _HEAD_LIMIT:
            raise _RefusalError(400, "the request head is too long")
        if line == b"\r\n" and len(head) > 2:
            return head


def _body_pieces(reader: BinaryIO, *, chunked: bool, length: int) -> Iterator[tuple[bytes, bytes]]:
    """Read a request body as the client sent it, and its decoded data.

    Args:
        reader: The buffered client stream after the head.
        chunked: Whether the body uses chunked transfer coding.
        length: The ``Content-Length`` when it is not chunked.

    Yields:
        ``(raw, data)``: the bytes to forward, and the body data in them.

    Raises:
        _RefusalError: For a malformed or short body.
    """
    if not chunked:
        left = length
        while left:
            data = reader.read(min(left, 65_536))
            if not data:
                raise _RefusalError(400, "the body ended early")
            left -= len(data)
            yield data, data
        return
    while True:
        line = reader.readline(1024)
        # Chunk extensions and trailers are not scanned, so they are refused.
        if not line.endswith(b"\r\n") or not _CHUNK_SIZE.fullmatch(line[:-2]):
            raise _RefusalError(400, "bad chunked body; chunk extensions are not allowed")
        size = int(line[:-2], 16)
        if size == 0:
            if reader.readline(3) != b"\r\n":
                raise _RefusalError(400, "bad chunked body; trailers are not allowed")
            yield line + b"\r\n", b""
            return
        yield line, b""
        while size:
            data = reader.read(min(size, 65_536))
            if not data:
                raise _RefusalError(400, "the body ended early")
            size -= len(data)
            yield data, data
        end = reader.read(2)
        if end != b"\r\n":
            raise _RefusalError(400, "bad chunked body")
        yield end, b""


def _response_pieces(reader: io.BufferedReader, *, chunked: bool, length: int | None) -> Iterator[bytes]:
    """Read a response body and yield its decoded data.

    Args:
        reader: The buffered upstream stream after the head.
        chunked: Whether the body uses chunked transfer coding.
        length: The ``Content-Length``, or None to read until the upstream closes.

    Yields:
        The body data; chunk framing and trailers are dropped.

    Raises:
        _CutError: For a malformed chunked body.
    """
    if not chunked:
        left = length
        while left is None or left > 0:
            try:
                data = reader.read1(65_536 if left is None else min(left, 65_536))
            except ssl.SSLEOFError:
                return
            if not data:
                return
            if left is not None:
                left -= len(data)
            yield data
        return
    while True:
        line = reader.readline(1024)
        size_text = line[:-2].split(b";", 1)[0].strip()
        if not line.endswith(b"\r\n") or not _CHUNK_SIZE.fullmatch(size_text):
            raise _CutError("the chunked response was malformed")
        size = int(size_text, 16)
        if size == 0:
            seen = 0
            while (trailer := reader.readline(_HEAD_LIMIT)) != b"\r\n":
                seen += len(trailer)
                if not trailer.endswith(b"\r\n") or seen > _HEAD_LIMIT:
                    raise _CutError("the chunked response was malformed")
            return
        while size:
            data = reader.read(min(size, 65_536))
            if not data:
                raise _CutError("the chunked response ended early")
            size -= len(data)
            yield data
        if reader.read(2) != b"\r\n":
            raise _CutError("the chunked response was malformed")


class _PlaceholderScan:
    """Find the placeholder of an approved secret in data that comes in pieces."""

    def __init__(self, names: frozenset[str]) -> None:
        """Start a scan.

        Args:
            names: The secrets that the request host approves.
        """
        # No end boundary here: ``skynet-secret-KEYabc`` in a body still holds KEY's placeholder.
        alternatives = b"|".join(re.escape(name.encode()) for name in sorted(names, key=len, reverse=True))
        self._pattern = re.compile(re.escape(PLACEHOLDER_PREFIX.encode()) + b"(" + alternatives + b")")
        self._tail = b""

    def feed(self, data: bytes) -> str | None:
        """Scan the next piece.

        Args:
            data: The next piece of the data.

        Returns:
            The name of an approved secret whose placeholder is in the data so far, or None.
        """
        text = self._tail + data
        match = self._pattern.search(text)
        if match is not None:
            return match.group(1).decode()
        self._tail = text[-_PLACEHOLDER_MAX:]
        return None


def _echo_needles(value: bytes) -> frozenset[bytes]:
    """Return a swapped value and the forms a JSON echo endpoint can write it in.

    JSON encoders escape quotes, backslashes and control characters, may write
    non-ASCII as ``\\uXXXX`` in either hex case, may escape ``/`` (PHP) and
    may escape ``<``, ``>`` and ``&`` (Go). A URL- or base64-encoded echo is
    not caught: the guard is best-effort for encodings other than these.

    Args:
        value: The real value, as sent.

    Returns:
        At most 17 byte strings, the raw value among them.
    """
    text = value.decode("utf-8", "replace")
    needles = {value}
    for ascii_only in (True, False):
        escaped = json.dumps(text, ensure_ascii=ascii_only)[1:-1]
        for slash in (escaped, escaped.replace("/", "\\/")):
            for html in (slash, slash.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")):
                needles.add(html.encode())
                needles.add(_UNICODE_ESCAPE.sub(lambda match: b"\\u" + match.group(1).upper(), html.encode()))
    return frozenset(needles)


def _status_code(first: bytes) -> int | None:
    """Read the status code of a response from its first bytes.

    Args:
        first: The first bytes of the response.

    Returns:
        The status code, or None when the bytes do not start a status line.
    """
    parts = first.split(b"\r\n", 1)[0].split(b" ")
    return int(parts[1]) if len(parts) > 1 and parts[0].startswith(b"HTTP/") and parts[1].isdigit() else None


class LocalEgressProxy:
    """Run the egress proxy of one setup session on a loopback port."""

    def __init__(self, secrets: Sequence[SetupSecret], state_dir: Path) -> None:
        """Check the session's secrets.

        Args:
            secrets: The secrets the session may use.
            state_dir: The app's state folder; the command must not be able to read it.

        Raises:
            ValueError: For a bad name, a repeated name, or a hosted secret
                that is empty or has a line break, which would break the header
                it goes in.
        """
        names = [secret.name for secret in secrets]
        for secret in secrets:
            if not _NAME.match(secret.name):
                raise ValueError(f"Secret name '{secret.name}' is not an environment variable name.")
            if secret.allowed_hosts and (not secret.value or any(c in secret.value for c in "\r\n\0")):
                raise ValueError(f"Secret {secret.name} must be one line of text to go in a request header.")
        if len(set(names)) != len(names):
            raise ValueError("Each secret name must be unique.")
        self._hosted = {secret.name: secret for secret in secrets if secret.allowed_hosts}
        self._plain = {secret.name: secret.value for secret in secrets if not secret.allowed_hosts}
        self.state_dir = state_dir
        self._state: Path | None = None
        self._ca_key: ec.EllipticCurvePrivateKey | None = None
        self._listeners: list[socket.socket] = []
        self._clients: set[socket.socket] = set()
        self._stopped = False
        self._token = ""
        self._proxy_auth = b""
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(MAX_CLIENTS)
        self._seq = itertools.count(1)
        self._leaf_contexts: dict[str, ssl.SSLContext] = {}
        self._upstream_context = ssl.create_default_context(cafile=certifi.where())
        self._upstream_context.set_alpn_protocols(["http/1.1"])
        self.calls: deque[dict] = deque(maxlen=_CALL_HISTORY)
        self.port: int | None = None
        self.ca_pem = b""

    def __enter__(self) -> LocalEgressProxy:
        """Start the proxy.

        Returns:
            The started proxy.
        """
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        """Stop the proxy and delete its keys.

        Args:
            _exc: The exception details, which it ignores.
        """
        self.stop()

    def start(self) -> LocalEgressProxy:
        """Make the session CA and listen on a free loopback port.

        Returns:
            The proxy; ``port`` and ``ca_pem`` are set.
        """
        self.state_dir.mkdir(parents=True, exist_ok=True)
        # mkdtemp makes the folder readable by this user only.
        self._state = Path(tempfile.mkdtemp(prefix="egress-", dir=self.state_dir))
        self._make_ca()
        self._token = token_urlsafe(32)
        self._proxy_auth = base64.b64encode(f"{_PROXY_USER}:{self._token}".encode())
        self._listeners = self._bind()
        self.port = self._listeners[0].getsockname()[1]
        for listener in self._listeners:
            threading.Thread(target=self._serve, args=(listener,), daemon=True).start()
        return self

    @property
    def proxy_url(self) -> str:
        """Return the proxy URL with its session credentials, for the command environment."""
        return f"http://{_PROXY_USER}:{self._token}@127.0.0.1:{self.port}"

    def _bind(self) -> list[socket.socket]:
        """Listen on one free port on both 127.0.0.1 and ``[::1]``.

        The sandbox rule ``localhost:PORT`` lets the command reach both
        addresses, so the proxy holds both and no other process can take ``[::1]``.

        Returns:
            The listeners; the first is the IPv4 one.

        Raises:
            OSError: When no port is free on both addresses.
        """
        for _ in range(20):
            ipv4 = socket.create_server(("127.0.0.1", 0), backlog=128)
            try:
                ipv6 = socket.create_server(("::1", ipv4.getsockname()[1]), family=socket.AF_INET6, backlog=128)
            except OSError as error:
                if error.errno == errno.EADDRINUSE:
                    ipv4.close()
                    continue
                # A Mac without IPv6 loopback has no [::1] for any process to bind.
                if error.errno in (errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL):
                    logger.warning("The egress proxy listens on IPv4 only: [::1] is not available (%s).", error)
                    return [ipv4]
                ipv4.close()
                raise
            return [ipv4, ipv6]
        raise OSError(errno.EADDRINUSE, "no loopback port was free on both IPv4 and IPv6")

    def stop(self) -> None:
        """Stop listening, end the open connections and delete the session's keys."""
        with self._lock:
            self._stopped = True
            listeners, self._listeners = self._listeners, []
            clients = list(self._clients)
            self._leaf_contexts.clear()
        for listener in listeners:
            with contextlib.suppress(OSError):
                listener.close()
        for client in clients:
            # SSLSocket.shutdown drops its TLS object first, so a racing sendall
            # would go out in plain text; the OS-level call keeps TLS and fails it.
            with contextlib.suppress(OSError):
                socket.socket.shutdown(client, socket.SHUT_RDWR)
        if self._state is not None:
            shutil.rmtree(self._state, ignore_errors=True)
            self._state = None
        self._ca_key = None

    def write_ca_bundle(self, path: Path) -> None:
        """Write the CA file that the command trusts: the public roots and the session CA.

        Tunnelled hosts show their own certificates, so the file must keep the public roots.

        Args:
            path: Where to write it; the command must be able to read it.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(Path(certifi.where()).read_bytes().rstrip(b"\n") + b"\n" + self.ca_pem)

    def command_env(self, ca_bundle: Path) -> dict[str, str]:
        """Return the variables a setup command needs: secrets, placeholders, proxy and CA.

        Args:
            ca_bundle: The file from :meth:`write_ca_bundle`.

        Returns:
            The variables; a hosted secret has its placeholder, never its value.
        """
        url = self.proxy_url
        env = dict(self._plain)
        env.update({name: placeholder(name) for name in self._hosted})
        for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
            env[key] = url
        env["NO_PROXY"] = env["no_proxy"] = ""
        # Node's fetch ignores the proxy variables without this (Node 22.21 and later read it).
        env["NODE_USE_ENV_PROXY"] = "1"
        for key in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "PIP_CERT", "NODE_EXTRA_CA_CERTS"):
            env[key] = str(ca_bundle)
        env["GIT_SSL_CAINFO"] = str(ca_bundle)
        return env

    def _approved(self, host: str) -> dict[str, SetupSecret]:
        """Return the secrets that may go to a host.

        Args:
            host: The lower-case request host.

        Returns:
            The secrets by name.
        """
        return {name: secret for name, secret in self._hosted.items() if host_allowed(host, secret.allowed_hosts)}

    def _make_ca(self) -> None:
        """Make the session CA in memory and the leaf key file in the state folder.

        Raises:
            OSError: When the proxy has no state folder.
        """
        if self._state is None:
            raise OSError(errno.ENOENT, "the egress proxy has no state folder")
        now = datetime.datetime.now(datetime.UTC)
        self._ca_key = ec.generate_private_key(ec.SECP256R1())
        self._ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Skynet setup session CA")])
        self._not_after = now + datetime.timedelta(days=_CA_DAYS)
        public_key = self._ca_key.public_key()
        builder = (
            x509.CertificateBuilder()
            .subject_name(self._ca_name)
            .issuer_name(self._ca_name)
            .public_key(public_key)
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(hours=1))
            .not_valid_after(self._not_after)
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=False,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
        )
        domains = sorted({host.removeprefix("*.") for secret in self._hosted.values() for host in secret.allowed_hosts})
        if domains:
            # A leaked CA then still cannot sign for a host that no secret approves.
            builder = builder.add_extension(
                x509.NameConstraints(permitted_subtrees=[x509.DNSName(d) for d in domains], excluded_subtrees=None),
                critical=True,
            )
        self.ca_pem = builder.sign(self._ca_key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM)
        self._leaf_key = ec.generate_private_key(ec.SECP256R1())
        key_path = self._state / "leaf-key.pem"
        key_path.touch(mode=0o600)
        key_path.write_bytes(
            self._leaf_key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
            )
        )

    def _leaf_context(self, host: str) -> ssl.SSLContext:
        """Return the TLS server context that shows the client a certificate for ``host``.

        Args:
            host: An approved host.

        Returns:
            The context, made once for each host.

        Raises:
            OSError: When the proxy stopped and its keys are gone.
        """
        with self._lock:
            if self._stopped or self._state is None or self._ca_key is None:
                raise OSError(errno.ECONNABORTED, "the proxy stopped")
            context = self._leaf_contexts.get(host)
            if context is not None:
                return context
            try:
                san: x509.GeneralName = x509.IPAddress(ipaddress.ip_address(host))
            except ValueError:
                san = x509.DNSName(host)
            now = datetime.datetime.now(datetime.UTC)
            cert = (
                x509.CertificateBuilder()
                .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
                .issuer_name(self._ca_name)
                .public_key(self._leaf_key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(hours=1))
                .not_valid_after(self._not_after)
                .add_extension(x509.SubjectAlternativeName([san]), critical=False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(
                    x509.KeyUsage(
                        digital_signature=True,
                        content_commitment=False,
                        key_encipherment=False,
                        data_encipherment=False,
                        key_agreement=False,
                        key_cert_sign=False,
                        crl_sign=False,
                        encipher_only=False,
                        decipher_only=False,
                    ),
                    critical=True,
                )
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                .add_extension(
                    x509.AuthorityKeyIdentifier.from_issuer_public_key(self._ca_key.public_key()), critical=False
                )
                .sign(self._ca_key, hashes.SHA256())
            )
            cert_path = self._state / f"leaf-{len(self._leaf_contexts)}.pem"
            cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM) + self.ca_pem)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.set_alpn_protocols(["http/1.1"])
            context.load_cert_chain(cert_path, self._state / "leaf-key.pem")
            self._leaf_contexts[host] = context
            return context

    def _serve(self, listener: socket.socket) -> None:
        """Accept connections, one thread each, until the listener closes.

        Args:
            listener: The bound loopback listener.
        """
        slots = self._slots
        while True:
            try:
                client, _ = listener.accept()
            except OSError:
                return
            if not slots.acquire(blocking=False):
                client.close()
                continue
            try:
                threading.Thread(target=self._handle_slot, args=(client, slots), daemon=True).start()
            except RuntimeError:
                slots.release()
                client.close()

    def _handle_slot(self, client: socket.socket, slots: threading.BoundedSemaphore) -> None:
        """Serve one client, then free its slot.

        Args:
            client: The accepted connection.
            slots: The semaphore that caps concurrent clients.
        """
        try:
            self._track(client)
            self._handle(client)
        except OSError:
            client.close()
        finally:
            self._untrack(client)
            slots.release()

    def _track(self, sock: socket.socket) -> None:
        """Keep an open socket so that :meth:`stop` can shut it down.

        Args:
            sock: A client, client TLS or upstream socket.

        Raises:
            OSError: When the proxy stopped.
        """
        with self._lock:
            if self._stopped:
                raise OSError(errno.ECONNABORTED, "the proxy stopped")
            self._clients.add(sock)

    def _untrack(self, sock: socket.socket) -> None:
        """Forget a socket that closes.

        Args:
            sock: A socket from :meth:`_track`.
        """
        with self._lock:
            self._clients.discard(sock)

    def _check_auth(self, headers: list[tuple[bytes, bytes]]) -> None:
        """Check the session credentials, so only this session's commands can use the proxy.

        Args:
            headers: The request headers.

        Raises:
            _RefusalError: With 407 when the credentials are missing or wrong.
        """
        given = [value for name, value in headers if name.lower() == b"proxy-authorization"]
        scheme, _, credentials = given[0].partition(b" ") if len(given) == 1 else (b"", b"", b"")
        if scheme.lower() != b"basic" or not compare_digest(credentials.strip(), self._proxy_auth):
            raise _RefusalError(407, "the proxy needs the session credentials")

    def _record(self, call: dict) -> None:
        """Keep and log one request's record.

        Args:
            call: The host, port, outcome, byte counts and timing; never a value.
        """
        entry = {"event": CALL_EVENT, "seq": next(self._seq), **call}
        self.calls.append(entry)
        logger.info(json.dumps(entry, sort_keys=True))

    def _handle(self, client: socket.socket) -> None:
        """Serve one client connection: a ``CONNECT`` or a plain ``http://`` request.

        Args:
            client: The accepted connection.
        """
        wall = time.time()
        clock = time.monotonic()
        call: dict = {"host": None, "port": None, "mode": None, "bytes_sent": 0, "bytes_received": 0}
        call.update(status="failed", code=None, reason="the client sent no request")
        with client:
            client.settimeout(_HANDSHAKE_SECONDS)
            try:
                request = _read_head(client)
                if request is None:
                    return
                head, rest = request
                if head.startswith(b"CONNECT "):
                    self._connect(client, head, rest, call)
                else:
                    self._plain_http(client, head, rest, call)
            except _RefusalError as refusal:
                call.update(status="refused", code=refusal.code, reason=refusal.reason)
                with contextlib.suppress(OSError):
                    client.sendall(_answer(refusal.code, refusal.reason))
            except OSError as error:
                if call["status"] != "ok":
                    call.update(status="failed", reason=type(error).__name__)
            finally:
                if call["host"] is not None or call["status"] != "failed":
                    started_at = datetime.datetime.fromtimestamp(wall, datetime.UTC).isoformat(timespec="milliseconds")
                    duration_ms = max(0, round((time.monotonic() - clock) * 1000))
                    self._record({**call, "started_at": started_at, "duration_ms": duration_ms})

    def _checked_upstream(self, host: str, port: int) -> socket.socket:
        """Connect to a public address of a host.

        Args:
            host: The lower-case host.
            port: The port.

        Returns:
            The connected socket.

        Raises:
            _RefusalError: When the host is not public or no address answers.
        """
        addresses = public_addresses(host, port) if 0 < port < 65_536 else None
        if addresses is None:
            raise _RefusalError(403, "the host does not resolve to public addresses only")
        upstream = _open_upstream(addresses, _IDLE_SECONDS)
        if upstream is None:
            raise _RefusalError(502, "no checked address answered")
        return upstream

    def _connect(self, client: socket.socket, head: bytes, rest: bytes, call: dict) -> None:
        """Serve a ``CONNECT``: inspect it for an approved host, or tunnel it.

        Args:
            client: The client connection.
            head: The ``CONNECT`` head.
            rest: Bytes the client sent after the head.
            call: The request's record, updated in place.

        Raises:
            _RefusalError: For a bad or private target.
        """
        target = parse_connect(head)
        sent_target = _split_connect(head)
        call.update(host=sent_target[0] if sent_target else None, port=target[1] if target else None)
        self._check_auth(_parse_head(head)[1])
        if target is None:
            raise _RefusalError(400, "bad CONNECT target")
        host, port = target
        approved = self._approved(host)
        call["mode"] = "inspect" if approved else "tunnel"
        if approved and rest:
            raise _RefusalError(400, "the client sent data before the tunnel opened")
        upstream = self._checked_upstream(host, port)
        with upstream:
            self._track(upstream)
            try:
                client.sendall(_ESTABLISHED)
                call.update(status="ok", code=200, reason=None)
                if approved:
                    self._inspect(client, upstream, target, approved, call)
                else:
                    self._tunnel(client, upstream, rest, call)
            finally:
                self._untrack(upstream)

    def _plain_http(self, client: socket.socket, head: bytes, rest: bytes, call: dict) -> None:
        """Forward a plain ``http://`` request without any swap.

        Args:
            client: The client connection.
            head: The request head with an absolute ``http://`` target.
            rest: Bytes the client sent after the head.
            call: The request's record, updated in place.

        Raises:
            _RefusalError: For a bad request or a private host.
        """
        (method, target, _), headers = _parse_head(head)
        self._check_auth(headers)
        try:
            url = urlsplit(target.decode("ascii"))
            host, port = (url.hostname or "").rstrip(".").lower(), url.port or 80
        except (UnicodeDecodeError, ValueError):
            host, port, url = "", 0, None
        if url is None or url.scheme != "http" or not host:
            raise _RefusalError(400, "a request through the proxy must be CONNECT or an absolute http:// URL")
        call.update(host=host, port=port, mode="http", method=method.decode("latin-1"))
        upstream = self._checked_upstream(host, port)
        path = (url.path or "/") + (f"?{url.query}" if url.query else "")
        lines = [b" ".join([method, path.encode(), b"HTTP/1.1"])]
        lines += [name + b": " + value for name, value in headers if name.lower() not in _HOP_HEADERS]
        with upstream:
            self._track(upstream)
            try:
                call.update(status="ok", code=None, reason=None)
                first = b"\r\n".join([*lines, b"Connection: close"]) + b"\r\n\r\n" + rest
                self._tunnel(client, upstream, first, call)
            finally:
                self._untrack(upstream)

    def _tunnel(self, client: socket.socket, upstream: socket.socket, first: bytes, call: dict) -> None:
        """Copy bytes both ways, unchanged, until either side closes or both go idle.

        Args:
            client: The client connection.
            upstream: The upstream connection.
            first: Bytes to send upstream before the copy starts.
            call: The request's record, updated in place.
        """
        sent: list[int] = []
        received: list[int] = []
        last_byte = [time.monotonic()]
        back = threading.Thread(target=_pipe, args=(upstream, client, received, last_byte), daemon=True)
        back.start()
        if first:
            upstream.sendall(first)
        client.settimeout(_IDLE_SECONDS)
        _pipe(client, upstream, sent, last_byte)
        back.join()
        call.update(bytes_sent=len(first) + sum(sent), bytes_received=sum(received))

    def _inspect(
        self,
        client: socket.socket,
        upstream: socket.socket,
        target: tuple[str, int],
        approved: dict[str, SetupSecret],
        call: dict,
    ) -> None:
        """End TLS for an approved host, and send one request on with its header placeholders swapped.

        Args:
            client: The client connection after the ``200`` answer.
            upstream: The TCP connection to the host's checked address.
            target: The ``CONNECT`` host and port.
            approved: The secrets that may go to the host.
            call: The request's record, updated in place.
        """
        call.update(status="failed", code=None, reason="the client TLS handshake failed")
        with self._leaf_context(target[0]).wrap_socket(client, server_side=True) as tls:
            self._track(tls)
            try:
                tls.settimeout(_IO_SECONDS)
                reader = tls.makefile("rb")
                try:
                    self._forward(reader, tls, upstream, target, approved, call)
                except _RefusalError as refusal:
                    call.update(status="refused", code=refusal.code, reason=refusal.reason)
                    with contextlib.suppress(OSError):
                        tls.sendall(_answer(refusal.code, refusal.reason))
                except _CutError as cut:
                    call.update(status="failed", reason=str(cut))
            finally:
                self._untrack(tls)

    def _forward(
        self,
        reader: BinaryIO,
        tls: ssl.SSLSocket,
        upstream: socket.socket,
        target: tuple[str, int],
        approved: dict[str, SetupSecret],
        call: dict,
    ) -> None:
        """Check and rewrite one request, send it upstream, and relay the response.

        Args:
            reader: The buffered client stream.
            tls: The client TLS connection.
            upstream: The TCP connection to the host's checked address.
            target: The ``CONNECT`` host and port.
            approved: The secrets that may go to the host.
            call: The request's record, updated in place.

        Raises:
            _RefusalError: For a malformed request, a request for another host,
                or a placeholder of an approved secret outside the headers.
            _CutError: When the response could hold a swapped value.
            OSError: When the proxy stopped.
        """
        host, port = target
        call.update(reason="the client sent no request")
        head = _read_tls_head(reader)
        if head is None:
            return
        (method, path, _), headers = _parse_head(head)
        call["method"] = method.decode("latin-1")
        # An absolute or authority target could send the request, and the value, to another host.
        if not (path.startswith(b"/") or (path == b"*" and method == b"OPTIONS")):
            raise _RefusalError(400, "the request target must be a path")
        lower = [(name.lower(), value) for name, value in headers]
        for single in _SINGLE_HEADERS:
            if sum(name == single for name, _ in lower) > 1:
                raise _RefusalError(400, f"the request has more than one {single.decode()} header")
        values = dict(lower)
        host_match = _HOST_HEADER.fullmatch(values.get(b"host", b""))
        if (
            host_match is None
            or host_match.group(1).decode().strip("[]").rstrip(".").lower() != host
            or (host_match.group(2) is not None and int(host_match.group(2)) != port)
        ):
            raise _RefusalError(400, "the Host header must name the CONNECT host")
        names = frozenset(approved)
        name = _PlaceholderScan(names).feed(path)
        if name is not None:
            raise _RefusalError(403, f"secret {name} can go only in a request header")
        chunked = values.get(b"transfer-encoding", b"").strip().lower()
        length_text = values.get(b"content-length")
        if chunked not in (b"", b"chunked") or (chunked and length_text is not None):
            raise _RefusalError(400, "unsupported body framing")
        if length_text is not None and not length_text.isdigit():
            raise _RefusalError(400, "bad Content-Length")
        length = int(length_text or 0)
        used: dict[str, bytes] = {}

        def swap(match: re.Match[bytes]) -> bytes:
            """Return the real value for an approved secret's placeholder.

            Args:
                match: A placeholder match in a header value.

            Returns:
                The real value, or the match unchanged for a secret the host does not approve.
            """
            secret = approved.get(match.group(1).decode())
            if secret is None:
                return match.group(0)
            used[secret.name] = secret.value.encode()
            return used[secret.name]

        if b"100-continue" in values.get(b"expect", b"").lower() and (chunked or length):
            tls.sendall(_CONTINUE)
        call.update(reason="the upstream TLS handshake failed")
        with self._upstream_context.wrap_socket(upstream, server_hostname=host) as upstream_tls:
            self._track(upstream_tls)
            try:
                # stop() shuts down every tracked socket, so no value goes out after it.
                if self._stopped:
                    raise OSError(errno.ECONNABORTED, "the proxy stopped")
                lines = [b" ".join([method, path, b"HTTP/1.1"])]
                lines += [
                    name + b": " + _PLACEHOLDER.sub(swap, value)
                    for name, value in headers
                    if name.lower() not in _INSPECT_DROP
                ]
                out_head = b"\r\n".join([*lines, b"Accept-Encoding: identity", b"Connection: close"]) + b"\r\n\r\n"
                call.update(reason=None)
                upstream_tls.sendall(out_head)
                call["bytes_sent"] = len(out_head)
                scan = _PlaceholderScan(names)
                for raw, data in _body_pieces(reader, chunked=bool(chunked), length=length):
                    name = scan.feed(data)
                    if name is not None:
                        raise _RefusalError(403, f"secret {name} can go only in a request header")
                    upstream_tls.sendall(raw)
                    call["bytes_sent"] += len(raw)
                call.update(status="ok")
                self._relay_response(upstream_tls.makefile("rb"), tls, used, method, call)
            finally:
                self._untrack(upstream_tls)

    def _relay_response(
        self, upstream: io.BufferedReader, tls: ssl.SSLSocket, used: dict[str, bytes], method: bytes, call: dict
    ) -> None:
        """Copy the response to the client, and stop it if it could hold a swapped value.

        An echo endpoint would otherwise hand the real value to the command. The
        proxy scans the decoded body, so a chunk boundary cannot split a value
        past the scan, and sends a chunked body on in chunks of its own.

        Args:
            upstream: The buffered upstream TLS stream.
            tls: The client TLS connection.
            used: The swapped values by secret name.
            method: The request method.
            call: The request's record, updated in place.

        Raises:
            _CutError: When the response holds a swapped value, or is encoded
                after a swap so the proxy cannot check it.
        """
        guard = {name: _echo_needles(value) for name, value in used.items() if len(value) >= _ECHO_GUARD_MIN}

        def check(data: bytes) -> None:
            """Stop the response when it holds a swapped value.

            Args:
                data: Response bytes the client has not got yet.

            Raises:
                _CutError: When a value is in the bytes.
            """
            for name, needles in guard.items():
                if any(needle in data for needle in needles):
                    raise _CutError(f"the response held the value of secret {name}, so it was cut")

        while True:
            try:
                head = _read_tls_head(upstream)
            except _RefusalError as error:
                raise _CutError("the response head was too long") from error
            if head is None:
                return
            call["bytes_received"] += len(head)
            call["code"] = code = _status_code(head)
            check(head)
            if guard and not _strict_response_head(head):
                raise _CutError("the response head had a bare line break or a malformed header, so it was cut")
            if code is None or not 100 <= code < 200 or code == 101:
                break
            tls.sendall(head)
        fields = [line.partition(b":") for line in head.removesuffix(b"\r\n\r\n").split(b"\r\n")[1:]]
        values = {name.strip().lower(): value.strip().lower() for name, _, value in fields}
        names = [name.strip().lower() for name, _, _ in fields]
        if guard and any(names.count(single) > 1 for single in _RESPONSE_SINGLE_HEADERS):
            raise _CutError("the response repeated a framing header, so the proxy could not check it")
        if guard and values.get(b"content-encoding", b"identity") not in (b"", b"identity"):
            raise _CutError("the response was encoded, so the proxy could not check it for secret values")
        framing = values.get(b"transfer-encoding", b"")
        chunked = framing == b"chunked"
        if guard and framing and not chunked:
            raise _CutError("the response framing was not chunked, so the proxy could not check it")
        length_text = values.get(b"content-length", b"")
        length = int(length_text) if length_text.isdigit() and not chunked else None
        tls.sendall(head)
        if method == b"HEAD" or code in (204, 304):
            return
        keep = max((len(needle) for needles in guard.values() for needle in needles), default=1) - 1
        pending = b""

        def emit(data: bytes) -> None:
            """Send body data to the client in the response's framing.

            Args:
                data: Checked body data.
            """
            if data:
                tls.sendall(b"%x\r\n%s\r\n" % (len(data), data) if chunked else data)

        for data in _response_pieces(upstream, chunked=chunked, length=length):
            call["bytes_received"] += len(data)
            pending += data
            check(pending)
            cut = len(pending) - keep
            if cut > 0:
                emit(pending[:cut])
                pending = pending[cut:]
        emit(pending)
        if chunked:
            tls.sendall(b"0\r\n\r\n")
