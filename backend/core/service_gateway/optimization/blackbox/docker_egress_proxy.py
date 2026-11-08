"""Egress proxy for a local setup box: HTTPS ``CONNECT`` to allowlisted hosts only.

A local setup box sits on its own internal Docker network with no route to the
internet (the network's gateway, the Docker VM itself, stays reachable). This
proxy runs in a sidecar container on that network and on ``bridge``, and is the
box's only way to the internet. It mirrors the Vercel allowlist: a tunnel opens only
to an exact allowlisted host on port 443, and only when every address the host
resolves to is a public one. That last check keeps the Mac itself out of reach
(Docker Desktop routes ``host.docker.internal`` and its 192.168.65.0/24 range to
services that listen only on the Mac's loopback), along with every private,
loopback and link-local range. The proxy connects to the address it checked,
so a second DNS answer cannot swap in a private one.

Each request prints one JSON line to stdout (``event`` is ``CALL_EVENT``) with
the host, port, outcome, byte counts, start time and duration, so the parent can
write the call into the run record (E28). The proxy sees only TLS bytes, so a
line never holds a body, a header or a path.

The sidecar runs this file with the image's own Python, so it must use the
standard library only. It is also importable for unit tests.

Usage: ``python3 -I -c <this file> <port> <lifetime seconds> <host> [<host> ...]``
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import socket
import sys
import threading
import time

READY_MARKER = "skynet-egress-proxy-ready"
CALL_EVENT = "skynet-egress-call"
_PRINT_LOCK = threading.Lock()
_ALLOWED_PORT = 443
_HEAD_LIMIT = 8192
_HANDSHAKE_SECONDS = 30.0
_CONNECT_SECONDS = 10.0
_IDLE_SECONDS = 300.0
# Each tunnel takes two threads; the sidecar runs under --pids-limit 256.
MAX_CLIENTS = 64
_FORBIDDEN = b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
_BAD_GATEWAY = b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
_ESTABLISHED = b"HTTP/1.1 200 Connection Established\r\n\r\n"


def parse_connect(head: bytes) -> tuple[str, int] | None:
    """Read the target of a ``CONNECT host:port`` request head.

    Args:
        head: The request bytes up to and including the blank line.

    Returns:
        The lower-case host without a trailing dot and the port, or ``None``
        for any other method or a malformed line.
    """
    line = head.split(b"\r\n", 1)[0].decode("latin-1")
    parts = line.split()
    if len(parts) != 3 or parts[0] != "CONNECT" or not parts[2].startswith("HTTP/1."):
        return None
    host, sep, port = parts[1].rpartition(":")
    if not sep or not port.isdigit() or host.startswith("["):
        return None
    return host.rstrip(".").lower(), int(port)


def public_addresses(host: str, port: int) -> list[tuple[int, tuple]] | None:
    """Resolve ``host`` and keep the answer only when every address is public.

    Args:
        host: The allowlisted host name.
        port: The destination port.

    Returns:
        ``(family, sockaddr)`` pairs to connect to, or ``None`` when the name
        does not resolve or any answer is not a global address.
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return None
    addresses = []
    for family, _, _, _, sockaddr in infos:
        if not ipaddress.ip_address(sockaddr[0].split("%")[0]).is_global:
            return None
        addresses.append((family, sockaddr))
    return addresses or None


def target_allowed(target: tuple[str, int] | None, allowed: frozenset[str]) -> bool:
    """Decide whether a parsed ``CONNECT`` target may be tunnelled.

    Args:
        target: The parsed host and port, or ``None``.
        allowed: Exact allowlisted host names.

    Returns:
        Whether the host is allowlisted and the port is 443.
    """
    return target is not None and target[1] == _ALLOWED_PORT and target[0] in allowed


def _read_head(client: socket.socket) -> tuple[bytes, bytes] | None:
    """Read the request head and any bytes the client sent after it.

    Args:
        client: The accepted connection.

    Returns:
        The head and the remainder, or ``None`` when no full head arrives.
    """
    data = b""
    while b"\r\n\r\n" not in data:
        if len(data) > _HEAD_LIMIT:
            return None
        chunk = client.recv(4096)
        if not chunk:
            return None
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    return head + b"\r\n\r\n", rest


def _pipe(source: socket.socket, sink: socket.socket, copied: list[int] | None = None) -> None:
    """Copy bytes one way until either side closes or goes idle.

    Args:
        source: Socket to read from.
        sink: Socket to write to.
        copied: Gets the count of bytes copied, when given.
    """
    total = 0
    try:
        while chunk := source.recv(65536):
            sink.sendall(chunk)
            total += len(chunk)
    except OSError:
        pass
    finally:
        if copied is not None:
            copied.append(total)
        for side in (sink, source):
            with contextlib.suppress(OSError):
                side.shutdown(socket.SHUT_RDWR)


def log_call(call: dict) -> None:
    """Print one request's JSON line for the parent's run record.

    Args:
        call: The request's host, port, outcome, byte counts and timing.
    """
    line = json.dumps({"event": CALL_EVENT, **call}, sort_keys=True)
    with _PRINT_LOCK, contextlib.suppress(OSError, ValueError):
        print(line, flush=True)


def _open_upstream(addresses: list[tuple[int, tuple]]) -> socket.socket | None:
    """Connect to the first checked address that answers.

    Args:
        addresses: Public ``(family, sockaddr)`` pairs.

    Returns:
        The connected socket, or ``None`` when none answers.
    """
    for family, sockaddr in addresses:
        upstream = socket.socket(family, socket.SOCK_STREAM)
        upstream.settimeout(_CONNECT_SECONDS)
        try:
            upstream.connect(sockaddr)
        except OSError:
            upstream.close()
            continue
        upstream.settimeout(_IDLE_SECONDS)
        return upstream
    return None


def handle(client: socket.socket, allowed: frozenset[str]) -> None:
    """Serve one client connection: refuse it, or tunnel it to its checked target.

    Args:
        client: The accepted connection.
        allowed: Exact allowlisted host names.
    """
    # The box image's own python3 runs this file, so no datetime.UTC (Python 3.11+).
    wall = time.time()
    started_at = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(wall)) + f".{int(wall % 1 * 1000):03d}+00:00"
    clock = time.monotonic()
    call: dict = {}
    sent: list[int] = []
    received: list[int] = []
    with client:
        client.settimeout(_HANDSHAKE_SECONDS)
        try:
            request = _read_head(client)
            if request is None:
                return
            head, rest = request
            target = parse_connect(head)
            # Only the host and port go in the log: a refused plain-HTTP line could carry a path or query.
            call = {"host": target[0] if target else None, "port": target[1] if target else None}
            call.update(status="refused", code=403, reason="host or port is not allowed")
            addresses = public_addresses(*target) if target_allowed(target, allowed) else None
            if addresses is None:
                client.sendall(_FORBIDDEN)
                return
            call.update(status="failed", code=502, reason="no checked address answered")
            upstream = _open_upstream(addresses)
            if upstream is None:
                client.sendall(_BAD_GATEWAY)
                return
            call.update(reason="the tunnel could not start")
            with upstream:
                back = threading.Thread(target=_pipe, args=(upstream, client, received), daemon=True)
                try:
                    back.start()
                except RuntimeError:
                    return
                client.sendall(_ESTABLISHED)
                call.update(status="ok", code=200, reason=None)
                if rest:
                    upstream.sendall(rest)
                client.settimeout(_IDLE_SECONDS)
                _pipe(client, upstream, sent)
                back.join()
        except OSError as error:
            if call.get("status") == "failed":
                call.update(reason=type(error).__name__)
            return
        finally:
            if call:
                log_call(
                    {
                        **call,
                        "bytes_sent": len(rest) + sum(sent) if call["status"] == "ok" else 0,
                        "bytes_received": sum(received),
                        "started_at": started_at,
                        "duration_ms": max(0, round((time.monotonic() - clock) * 1000)),
                    }
                )


def _handle_slot(client: socket.socket, allowed: frozenset[str], slots: threading.BoundedSemaphore) -> None:
    """Serve one client, then free its slot.

    Args:
        client: The accepted connection.
        allowed: Exact allowlisted host names.
        slots: The semaphore that caps concurrent clients.
    """
    try:
        handle(client, allowed)
    finally:
        slots.release()


def serve(listener: socket.socket, allowed: frozenset[str], max_clients: int = MAX_CLIENTS) -> None:
    """Accept connections, one thread each, until the listener closes.

    A listener that fails stops the proxy, which leaves the box with no way
    out rather than an unchecked one. A client beyond ``max_clients``, or one
    whose thread cannot start, is closed, and the proxy keeps serving.

    Args:
        listener: A bound, listening socket.
        allowed: Exact allowlisted host names.
        max_clients: Most clients served at the same time.
    """
    slots = threading.BoundedSemaphore(max_clients)
    while True:
        try:
            client, _ = listener.accept()
        except OSError:
            return
        if not slots.acquire(blocking=False):
            client.close()
            continue
        try:
            threading.Thread(target=_handle_slot, args=(client, allowed, slots), daemon=True).start()
        except RuntimeError:
            slots.release()
            client.close()


def main(argv: list[str]) -> None:
    """Listen on every interface of the sidecar and exit when the box's lifetime ends.

    Args:
        argv: ``<port> <lifetime seconds> <host> [<host> ...]``.
    """
    port, lifetime, *hosts = argv
    allowed = frozenset(host.rstrip(".").lower() for host in hosts)
    listener = socket.create_server(("0.0.0.0", int(port)), backlog=128)
    timer = threading.Timer(float(lifetime), os._exit, args=(0,))
    timer.daemon = True
    timer.start()
    print(READY_MARKER, flush=True)
    serve(listener, allowed)


if __name__ == "__main__":
    main(sys.argv[1:])
