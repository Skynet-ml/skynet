"""Tests for the capped egress record that Docker, Podman and Vercel boxes write (SKY-634)."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from core.sandbox import egress_record
from core.worker.scoped_relay import CallRecorder


def _line(host: str, second: int, **fields: Any) -> str:
    """Return one request line as the egress proxy prints it.

    Args:
        host: The requested host.
        second: The start second of the request.
        **fields: The other fields of the line.

    Returns:
        The JSON line.
    """
    return json.dumps(
        {
            "event": egress_record.CALL_EVENT,
            "host": host,
            "port": 443,
            "status": "ok",
            "code": 200,
            "reason": None,
            "bytes_sent": 1,
            "bytes_received": 2,
            "started_at": f"2026-11-11T10:{second // 60:02d}:{second % 60:02d}.000+00:00",
            "duration_ms": 3,
            **fields,
        }
    )


def _log(count: int, *, first_seq: int = 1) -> bytes:
    """Return a proxy log with numbered request lines.

    Args:
        count: The number of request lines.
        first_seq: The ``seq`` of the first line.

    Returns:
        The log bytes.
    """
    lines = [_line(f"h{index}.example.com", index, seq=first_seq + index) for index in range(count)]
    return "\n".join([egress_record.READY_MARKER, *lines]).encode()


def _recorder() -> tuple[CallRecorder, list[dict[str, Any]]]:
    """Return a recorder of a fresh run and the calls it writes.

    Returns:
        The recorder and its written calls.
    """
    calls: list[dict[str, Any]] = []
    sink = SimpleNamespace(record_call=lambda _run, call, _secrets: calls.append(call))
    return CallRecorder(sink, f"run-{uuid.uuid4().hex}", "", ""), calls


def test_a_box_writes_at_most_its_cap_and_one_summary_row() -> None:
    """Write ``BOX_ROWS`` request rows, then one row that counts the rest."""
    recorder, calls = _recorder()
    log = _log(egress_record.BOX_ROWS)
    written = egress_record.record_calls(recorder, log, [], box_rows=5)
    assert written == egress_record.BOX_ROWS - 5
    *rows, summary = calls
    assert len(rows) == egress_record.BOX_ROWS - 5
    assert (summary["kind"], summary["status"], summary["failure_reason"]) == (
        "egress",
        egress_record.TRUNCATED_STATUS,
        "5 further requests not recorded",
    )


def test_a_full_box_writes_only_the_summary_row() -> None:
    """Write no request row once the box cap is used up."""
    recorder, calls = _recorder()
    assert egress_record.record_calls(recorder, _log(2), [], box_rows=egress_record.BOX_ROWS) == 0
    assert [call["failure_reason"] for call in calls] == ["2 further requests not recorded"]


def test_the_run_cap_spans_boxes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Share one row cap between all boxes of a run, summary rows included."""
    monkeypatch.setattr(egress_record, "RUN_ROWS", 4)
    recorder, calls = _recorder()
    assert egress_record.record_calls(recorder, _log(2), [], box_rows=0) == 2
    assert egress_record.record_calls(recorder, _log(2), [], box_rows=0) == 1
    assert [call["status"] for call in calls] == ["ok", "ok", "ok", egress_record.TRUNCATED_STATUS]
    assert calls[-1]["failure_reason"] == "1 further requests not recorded"
    other, other_calls = _recorder()
    assert egress_record.record_calls(other, _log(2), [], box_rows=0) == 2
    assert len(other_calls) == 2


def test_a_full_run_writes_no_more_rows_from_later_boxes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop all rows, summary rows too, once the run cap is used up."""
    monkeypatch.setattr(egress_record, "RUN_ROWS", 3)
    recorder, calls = _recorder()
    for _ in range(5):
        egress_record.record_calls(recorder, _log(2), [], box_rows=0)
    assert [call["status"] for call in calls] == ["ok", "ok", egress_record.TRUNCATED_STATUS]


def test_box_summaries_count_against_the_run_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Count each box's summary row against the run cap."""
    monkeypatch.setattr(egress_record, "RUN_ROWS", 3)
    recorder, calls = _recorder()
    for _ in range(5):
        egress_record.record_calls(recorder, _log(2), [], box_rows=egress_record.BOX_ROWS)
    assert len(calls) == 3


def test_the_summary_counts_lines_before_the_read_window() -> None:
    """Count the requests that the log end left out, from the highest line number."""
    recorder, calls = _recorder()
    egress_record.record_calls(recorder, _log(2, first_seq=499), [], box_rows=0)
    assert calls[-1]["failure_reason"] == "498 further requests not recorded"


def test_rows_under_the_caps_write_no_summary() -> None:
    """Write no summary row when every request fits."""
    recorder, calls = _recorder()
    egress_record.record_calls(recorder, _log(3), [], box_rows=0)
    assert [call["status"] for call in calls] == ["ok", "ok", "ok"]


def test_a_long_host_is_replaced_and_the_source_marks_each_row() -> None:
    """Drop an overlong host whole, so no cut leaves part of a secret, and mark cooperative rows."""
    recorder, calls = _recorder()
    log = _line("a" * 5000, 0).encode()
    egress_record.record_calls(recorder, log, [], box_rows=0, source=egress_record.VERCEL_SOURCE)
    [call] = calls
    assert call["target"] == egress_record.LONG_HOST
    assert json.loads(call["request"])["source"] == egress_record.VERCEL_SOURCE


def test_only_the_last_log_bytes_are_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Parse only the end of an oversized log."""
    monkeypatch.setattr(egress_record, "LOG_BYTES", 400)
    recorder, calls = _recorder()
    egress_record.record_calls(recorder, _log(20), [], box_rows=0)
    hosts = [call["target"] for call in calls if call["status"] == "ok"]
    assert 0 < len(hosts) < 20
    assert hosts[-1] == "h19.example.com"


def test_box_secrets_skip_short_values() -> None:
    """Mask only env values long enough to be secrets."""
    assert egress_record.box_secrets({"KEY": "route-value-for-tests", "FLAG": "1"}) == ["route-value-for-tests"]
