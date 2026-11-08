"""Tests for the egress proxy that is a local setup box's only way out."""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterator

import pytest

from .. import docker_egress_proxy as proxy


@pytest.mark.parametrize(
    ("head", "target"),
    [
        (b"CONNECT pypi.org:443 HTTP/1.1\r\nHost: pypi.org:443\r\n\r\n", ("pypi.org", 443)),
        (b"CONNECT PyPI.org.:443 HTTP/1.0\r\n\r\n", ("pypi.org", 443)),
        (b"GET http://pypi.org/ HTTP/1.1\r\n\r\n", None),
        (b"CONNECT pypi.org HTTP/1.1\r\n\r\n", None),
        (b"CONNECT [::1]:443 HTTP/1.1\r\n\r\n", None),
        (b"CONNECT pypi.org:443\r\n\r\n", None),
    ],
)
def test_parse_connect_reads_only_well_formed_connect_lines(head: bytes, target: tuple[str, int] | None) -> None:
    """Only a ``CONNECT host:port HTTP/1.x`` line names a target."""
    assert proxy.parse_connect(head) == target


@pytest.mark.parametrize(
    ("target", "allowed"),
    [
        (("pypi.org", 443), True),
        (("pypi.org", 80), False),
        (("evil.pypi.org", 443), False),
        (("host.docker.internal", 443), False),
        (None, False),
    ],
)
def test_target_allowed_needs_an_exact_host_on_443(target: tuple[str, int] | None, allowed: bool) -> None:
    """Match hosts exactly, as the Vercel allowlist does, and only on port 443."""
    assert proxy.target_allowed(target, frozenset({"pypi.org"})) is allowed


def _answers(*ips: str) -> list[tuple]:
    """Build ``getaddrinfo`` answers for ``ips``.

    Args:
        *ips: IPv4 or IPv6 addresses.

    Returns:
        Answers shaped like ``socket.getaddrinfo``'s.
    """
    return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in ips]


@pytest.mark.parametrize(
    ("ips", "public"),
    [
        (("151.101.0.223",), True),
        (("151.101.0.223", "2a04:4e42::223"), True),
        (("192.168.65.254",), False),
        (("151.101.0.223", "127.0.0.1"), False),
        (("10.0.0.5",), False),
        (("172.17.0.1",), False),
        (("169.254.169.254",), False),
        (("::1",), False),
        (("fe80::1%eth0",), False),
        (("100.64.0.1",), False),
    ],
)
def test_public_addresses_refuse_any_private_answer(
    monkeypatch: pytest.MonkeyPatch, ips: tuple[str, ...], public: bool
) -> None:
    """One private answer, the Docker host range included, refuses the whole host."""
    monkeypatch.setattr(proxy.socket, "getaddrinfo", lambda *args, **kwargs: _answers(*ips))
    result = proxy.public_addresses("pypi.org", 443)
    assert (result is not None) is public


def test_public_addresses_refuse_a_name_that_does_not_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    """A lookup failure refuses the tunnel instead of raising."""

    def fail(*args: object, **kwargs: object) -> list[tuple]:
        """Fail like a missing name.

        Raises:
            socket.gaierror: Always.
        """
        raise socket.gaierror("no such name")

    monkeypatch.setattr(proxy.socket, "getaddrinfo", fail)
    assert proxy.public_addresses("pypi.org", 443) is None


@pytest.fixture
def running_proxy() -> Iterator[int]:
    """Serve the proxy on a loopback port for one test.

    Yields:
        The proxy's port.
    """
    listener = socket.create_server(("127.0.0.1", 0))
    thread = threading.Thread(target=proxy.serve, args=(listener, frozenset({"pypi.org", "localhost"})), daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1]
    finally:
        listener.close()


def _request(port: int, head: bytes, extra: bytes = b"") -> tuple[socket.socket, bytes]:
    """Send one request head to the proxy and read its status line.

    Args:
        port: The proxy's port.
        head: The request head.
        extra: Bytes sent right after the head, before any answer.

    Returns:
        The open client socket and the proxy's first answer; empty when the
        proxy closed the connection without one.
    """
    client = socket.create_connection(("127.0.0.1", port), timeout=5)
    answer = b""
    try:
        client.sendall(head + extra)
        while b"\r\n\r\n" not in answer:
            chunk = client.recv(1024)
            if not chunk:
                break
            answer += chunk
    except ConnectionResetError:
        pass
    return client, answer


@pytest.mark.parametrize(
    "head",
    [
        b"CONNECT example.com:443 HTTP/1.1\r\n\r\n",
        b"CONNECT pypi.org:80 HTTP/1.1\r\n\r\n",
        b"CONNECT localhost:443 HTTP/1.1\r\n\r\n",
        b"GET http://pypi.org/ HTTP/1.1\r\nHost: pypi.org\r\n\r\n",
    ],
)
def test_proxy_refuses_other_hosts_ports_methods_and_loopback(running_proxy: int, head: bytes) -> None:
    """An allowlisted name that resolves to loopback is refused like any other host."""
    client, answer = _request(running_proxy, head)
    with client:
        assert answer.startswith(b"HTTP/1.1 403 ")


def test_proxy_tunnels_an_allowed_host_to_its_checked_address(
    running_proxy: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bytes flow both ways, including bytes the client sent before the answer."""
    upstream = socket.create_server(("127.0.0.1", 0))
    seen: list[bytes] = []

    def echo() -> None:
        """Echo one connection back in upper case until it closes."""
        connection, _ = upstream.accept()
        with connection:
            while chunk := connection.recv(1024):
                seen.append(chunk)
                connection.sendall(chunk.upper())

    threading.Thread(target=echo, daemon=True).start()
    checked = [(socket.AF_INET, upstream.getsockname())]
    monkeypatch.setattr(proxy, "public_addresses", lambda host, port: checked if host == "pypi.org" else None)

    client, answer = _request(running_proxy, b"CONNECT pypi.org:443 HTTP/1.1\r\n\r\n", b"early")
    with client, upstream:
        assert answer.startswith(b"HTTP/1.1 200 ")
        client.sendall(b"-late")
        # The echo of the early bytes can arrive in the same read as the head.
        echoed = answer.partition(b"\r\n\r\n")[2]
        while len(echoed) < len(b"early-late"):
            echoed += client.recv(1024)
        assert echoed == b"EARLY-LATE"


def _start_proxy(max_clients: int = proxy.MAX_CLIENTS) -> socket.socket:
    """Serve the proxy on a loopback port in a background thread.

    Args:
        max_clients: Most clients served at the same time.

    Returns:
        The listener; closing it stops the proxy.
    """
    listener = socket.create_server(("127.0.0.1", 0))
    threading.Thread(target=proxy.serve, args=(listener, frozenset({"pypi.org"}), max_clients), daemon=True).start()
    return listener


def _fail_thread_starts(monkeypatch: pytest.MonkeyPatch, times: int) -> list[int]:
    """Make the next ``times`` thread starts raise like a full pids limit.

    Args:
        monkeypatch: Pytest fixture.
        times: How many starts fail.

    Returns:
        A one-item counter of the failures raised so far.
    """
    real_start = threading.Thread.start
    failed = [0]

    def start(thread: threading.Thread) -> None:
        """Raise for the first ``times`` calls, then start normally.

        Args:
            thread: The thread to start.

        Raises:
            RuntimeError: For the first ``times`` calls.
        """
        if failed[0] < times:
            failed[0] += 1
            raise RuntimeError("can't start new thread")
        real_start(thread)

    monkeypatch.setattr(proxy.threading.Thread, "start", start)
    return failed


def test_serve_closes_a_client_whose_thread_cannot_start_and_keeps_serving(monkeypatch: pytest.MonkeyPatch) -> None:
    """A full pids limit costs one client, not the whole proxy."""
    listener = _start_proxy()
    with listener:
        failed = _fail_thread_starts(monkeypatch, 1)
        port = listener.getsockname()[1]
        first, answer = _request(port, b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
        with first:
            assert answer == b""
        second, answer = _request(port, b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
        with second:
            assert answer.startswith(b"HTTP/1.1 403 ")
        assert failed == [1]


def test_serve_closes_clients_beyond_the_cap_and_frees_slots(monkeypatch: pytest.MonkeyPatch) -> None:
    """Over the cap a client is closed at once; a finished client frees its slot."""
    listener = _start_proxy(max_clients=1)
    with listener:
        port = listener.getsockname()[1]
        held = socket.create_connection(("127.0.0.1", port), timeout=5)
        extra, answer = _request(port, b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
        with extra:
            assert answer == b""
        held.close()
        for _ in range(50):
            client, answer = _request(port, b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
            with client:
                if answer:
                    break
            threading.Event().wait(0.05)
        assert answer.startswith(b"HTTP/1.1 403 ")


def test_handle_closes_the_tunnel_when_its_return_pipe_cannot_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``200`` goes out when the second pipe thread cannot start."""
    upstream = socket.create_server(("127.0.0.1", 0))
    monkeypatch.setattr(proxy, "public_addresses", lambda host, port: [(socket.AF_INET, upstream.getsockname())])
    _fail_thread_starts(monkeypatch, 1)
    ours, theirs = socket.socketpair()
    with upstream, ours:
        ours.sendall(b"CONNECT pypi.org:443 HTTP/1.1\r\n\r\n")
        proxy.handle(theirs, frozenset({"pypi.org"}))
        ours.settimeout(5)
        assert ours.recv(1024) == b""


def test_handle_logs_a_refused_request_with_its_host_and_no_path(capsys: pytest.CaptureFixture[str]) -> None:
    """Print one JSON line for a refused request: the host and port, never the request line."""
    ours, theirs = socket.socketpair()
    with ours:
        ours.sendall(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
        proxy.handle(theirs, frozenset({"pypi.org"}))
        plain, other = socket.socketpair()
        with plain:
            plain.sendall(b"GET http://pypi.org/secret-path?key=abc HTTP/1.1\r\n\r\n")
            proxy.handle(other, frozenset({"pypi.org"}))
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(line["event"], line["host"], line["port"], line["status"], line["code"]) for line in lines] == [
        (proxy.CALL_EVENT, "example.com", 443, "refused", 403),
        (proxy.CALL_EVENT, None, None, "refused", 403),
    ]
    assert lines[0]["bytes_sent"] == 0
    assert lines[0]["started_at"].endswith("+00:00")
    assert "secret-path" not in json.dumps(lines)


def test_handle_logs_a_tunnel_with_its_byte_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Print one ok line for a tunnel, with the bytes each way and its duration."""
    upstream = socket.create_server(("127.0.0.1", 0))
    monkeypatch.setattr(proxy, "public_addresses", lambda host, port: [(socket.AF_INET, upstream.getsockname())])
    logged: list[dict] = []
    monkeypatch.setattr(proxy, "log_call", logged.append)

    def echo() -> None:
        """Answer the tunnel with ten bytes, then close it."""
        connection, _ = upstream.accept()
        with connection:
            connection.recv(1024)
            connection.sendall(b"0123456789")

    threading.Thread(target=echo, daemon=True).start()
    ours, theirs = socket.socketpair()
    with upstream, ours:
        ours.sendall(b"CONNECT pypi.org:443 HTTP/1.1\r\n\r\nhello")
        proxy.handle(theirs, frozenset({"pypi.org"}))
    [line] = logged
    assert (line["host"], line["status"], line["code"], line["reason"]) == ("pypi.org", "ok", 200, None)
    assert (line["bytes_sent"], line["bytes_received"]) == (5, 10)
    assert line["duration_ms"] >= 0


def test_handle_logs_a_bad_gateway_as_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Print a failed line with a reason when no checked address answers."""
    monkeypatch.setattr(proxy, "public_addresses", lambda host, port: [(socket.AF_INET, ("127.0.0.1", 1))])
    logged: list[dict] = []
    monkeypatch.setattr(proxy, "log_call", logged.append)
    ours, theirs = socket.socketpair()
    with ours:
        ours.sendall(b"CONNECT pypi.org:443 HTTP/1.1\r\n\r\n")
        proxy.handle(theirs, frozenset({"pypi.org"}))
    [line] = logged
    assert (line["status"], line["code"], line["reason"]) == ("failed", 502, "no checked address answered")
