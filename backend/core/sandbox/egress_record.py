"""Write egress proxy requests into the run record, within fixed caps (SKY-634).

An egress proxy prints one JSON line for each request (see
``docker_egress_proxy.py``). A box reads the end of that log before the proxy
goes away and writes each line as one ``egress`` call.

Candidate code decides how many requests it makes, so every step is capped:
a box reads at most ``LOG_LINES`` lines and ``LOG_BYTES`` bytes of one log,
writes at most ``BOX_ROWS`` rows, and a run gets at most ``RUN_ROWS`` rows.
Past a cap, one summary row gives the count of the requests that it left out.
Summary rows count against the run cap too. The last row of the run cap is
kept for a summary, so a full run ends with one; after it, the run gets no
more egress rows.

Docker and Podman run the proxy in a sidecar, which is the box's only way
out, so their record is complete up to the caps. A Vercel box runs it inside
the box in record-only mode (``VERCEL_SOURCE``): the Vercel edge enforces the
allowlist, and the in-box proxy only records. That record is cooperative:
code that ignores ``HTTPS_PROXY`` goes out unrecorded, and code in the box can
write to the log.
"""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # The worker relay imports the blackbox runtimes, which import this package.
    from ..worker.scoped_relay import CallRecorder

PROXY_SOURCE = (
    Path(__file__).resolve().parents[1] / "service_gateway/optimization/blackbox/docker_egress_proxy.py"
).read_text(encoding="utf-8")
READY_MARKER = "skynet-egress-proxy-ready"
CALL_EVENT = "skynet-egress-call"
LOG_LINES = 200
# A proxy line is at most about 9 KB: the proxy reads at most 8 KB of a request head.
LOG_BYTES = 2 * 1024 * 1024
BOX_ROWS = 200
RUN_ROWS = 2000
TRUNCATED_STATUS = "truncated"
VERCEL_SOURCE = "vercel-in-box-proxy (cooperative)"
# Shorter env values (ports, flags, "1") are not secrets, and masking them would
# mangle the byte counts; the job log scrubber uses the same floor.
SECRET_MIN_CHARS = 8
_HOST_CHARS = 253
# A longer host is no DNS name. The row drops it whole: a cut could keep part of a secret.
LONG_HOST = "(host name too long)"
# A worker serves many runs; it keeps the row count of the newest runs only.
_TRACKED_RUNS = 4096
_run_rows: OrderedDict[str, int] = OrderedDict()
_run_rows_lock = threading.Lock()


def parse_calls(log: bytes) -> list[dict]:
    """Parse the request lines of an egress proxy log, in start order.

    Args:
        log: The proxy's output.

    Returns:
        One dict for each request line; the ready marker and other lines are skipped.
    """
    calls: list[dict] = []
    for line in log.decode("utf-8", errors="replace").splitlines():
        try:
            call = json.loads(line)
            if not isinstance(call, dict) or call.get("event") != CALL_EVENT:
                continue
            call["started_at"] = datetime.fromisoformat(call["started_at"])
            call["duration_ms"] = int(call["duration_ms"])
        except (ValueError, TypeError, KeyError):
            continue
        calls.append(call)
    # The proxy prints a line when a tunnel ends, so a long tunnel comes after
    # shorter ones that started later.
    return sorted(calls, key=lambda call: call["started_at"])


def box_secrets(env: Mapping[str, str]) -> list[str]:
    """Return the box env values that the store must mask.

    Args:
        env: The box env.

    Returns:
        The values of at least ``SECRET_MIN_CHARS`` characters.
    """
    return [value for value in env.values() if len(value) >= SECRET_MIN_CHARS]


def _take_run_rows(run_id: str, wanted: int, limit: int) -> int:
    """Reserve rows from the run's cap.

    Args:
        run_id: The run id.
        wanted: The rows the box wants to write.
        limit: The run's row count that the reservation may reach.

    Returns:
        The rows the box may write.
    """
    with _run_rows_lock:
        used = _run_rows.pop(run_id, 0)
        granted = max(0, min(wanted, limit - used))
        _run_rows[run_id] = used + granted
        while len(_run_rows) > _TRACKED_RUNS:
            _run_rows.popitem(last=False)
    return granted


def _target(host: object) -> str:
    """Return the row target for a logged host.

    Args:
        host: The host from the proxy line.

    Returns:
        The host, or ``LONG_HOST`` when it is longer than a DNS name.
    """
    text = str(host or "")
    return text if len(text) <= _HOST_CHARS else LONG_HOST


def _requests_seen(calls: Sequence[dict]) -> int:
    """Count the requests the proxy printed, also the ones before the read window.

    Args:
        calls: The parsed lines.

    Returns:
        The highest ``seq`` of the lines, but never fewer than the lines.
    """
    seqs = [call["seq"] for call in calls if isinstance(call.get("seq"), int)]
    return max([len(calls), *seqs])


def record_calls(
    recorder: CallRecorder,
    log: bytes,
    secrets: Sequence[str],
    *,
    box_rows: int,
    source: str | None = None,
) -> int:
    """Write the requests of one proxy log into the run record, within the caps.

    The proxy sees only the host and port inside the TLS tunnel, so a record
    keeps the byte counts and the proxy's answer code, never a body.

    Args:
        recorder: The run's call recorder.
        log: The end of the proxy's output, at most ``LOG_LINES`` lines.
        secrets: Values the box can see; the store masks them in each record.
        box_rows: The rows the box already wrote.
        source: Marks each row with the proxy that saw it, or ``None``.

    Returns:
        The request rows written, for the box's own count.
    """
    calls = parse_calls(log[-LOG_BYTES:])
    seen = _requests_seen(calls)
    room = max(0, min(len(calls), BOX_ROWS - box_rows))
    kept = calls[: _take_run_rows(recorder.optimization_id, room, RUN_ROWS - 1)]
    marker = {"source": source} if source else {}
    for call in kept:
        recorder.record(
            replace(recorder.start(), at=call["started_at"]),
            kind="egress",
            target=_target(call.get("host")),
            status=str(call.get("status") or "failed"),
            request=json.dumps({"port": call.get("port"), "bytes_sent": call.get("bytes_sent"), **marker}),
            response=json.dumps({"code": call.get("code"), "bytes_received": call.get("bytes_received")}),
            failure_reason=call.get("reason") or None,
            secrets=secrets,
            duration_ms=call["duration_ms"],
        )
    if seen > len(kept) and _take_run_rows(recorder.optimization_id, 1, RUN_ROWS):
        recorder.record(
            recorder.start(),
            kind="egress",
            target="egress",
            status=TRUNCATED_STATUS,
            request=json.dumps(marker) if marker else b"",
            failure_reason=f"{seen - len(kept)} further requests not recorded",
            secrets=secrets,
            duration_ms=0,
        )
    return len(kept)
