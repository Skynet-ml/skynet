"""Sum the local CLI subscription usage that Skynet's own runs caused.

Skynet runs ``claude`` with ``--no-session-persistence`` and ``codex`` with
``--ephemeral`` (see :mod:`.cli_isolation`), so the CLIs' own session logs
never hold Skynet's calls. The reliable record is Skynet's usage evidence:
each CLI call inside a run settles a zero-cent row in
``execution_usage_evidence`` with the CLI, its status and its token counts.

This module only reads. It never changes credits, balances or wallet
numbers. No CLI reports a used-percent for Skynet's calls, so the only
window fact is the cap reset time that :func:`.cli_probe.read_cli_limits`
saved when a CLI reported a limit. A 429 row marked ``dispatched: false``
never started a CLI (a saved cap or no CLI for the model), so it is skipped.
One operation keeps one row, so a row's ``capped_runs`` lists the fallback
CLIs that also ran and hit a cap; each counts as one failed call.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from ..config import Settings
from ..storage.models import ExecutionBudgetModel, ExecutionOperationModel, ExecutionUsageEvidenceModel
from .cli_probe import CLI_IDS, read_cli_limits

WINDOWS: tuple[tuple[str, timedelta], ...] = (("5h", timedelta(hours=5)), ("7d", timedelta(days=7)))
"""Report windows. They match the lengths of the Claude and Codex subscription windows."""

_OK_STATUSES = (200, "ok")
"""Success statuses: a dispatcher call stores HTTP 200, a proposer turn stores ``"ok"``."""


def _utc(moment: datetime) -> datetime:
    """Return ``moment`` as an aware UTC time.

    Args:
        moment: A stored time. SQLite gives it back naive, in UTC.

    Returns:
        The same instant with a UTC zone.
    """
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def _tokens(evidence: dict[str, Any], key: str) -> int:
    """Read one token count from a stored evidence document.

    Args:
        evidence: The evidence JSON of one CLI call.
        key: ``prompt_tokens`` or ``completion_tokens``.

    Returns:
        The count, or 0 when the call has no usage (a failed call).
    """
    usage = evidence.get("usage")
    value = usage.get(key) if isinstance(usage, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _calls(evidence: Any) -> list[tuple[str, bool, dict[str, Any]]]:
    """List the CLI calls that one evidence row stands for.

    Args:
        evidence: The stored evidence JSON of one operation.

    Returns:
        ``(cli, failed, evidence)`` for each call: the named CLI first, then
        each CLI in ``capped_runs`` as a failed call with no tokens. Empty for
        a row that started no CLI.
    """
    if not isinstance(evidence, dict) or evidence.get("dispatched") is False:
        return []
    calls = [(str(evidence.get("cli")), evidence.get("status") not in _OK_STATUSES, evidence)]
    capped_runs = evidence.get("capped_runs")
    if isinstance(capped_runs, list):
        calls.extend((str(cli), True, {}) for cli in capped_runs)
    return calls


def _empty_window() -> dict[str, int]:
    """Return the zero totals of one window.

    Returns:
        ``{calls, failed_calls, runs, input_tokens, output_tokens}`` at 0.
    """
    return {"calls": 0, "failed_calls": 0, "runs": 0, "input_tokens": 0, "output_tokens": 0}


def _cap(limits: dict[str, dict[str, str]], cli: str, now: datetime) -> dict[str, str] | None:
    """Return a CLI's active cap, if any.

    Args:
        limits: The saved caps from :func:`read_cli_limits`.
        cli: The CLI id.
        now: The current time.

    Returns:
        ``{reset_at, message}`` when the cap ends after ``now``, else ``None``.
    """
    entry = limits.get(cli)
    if entry is None:
        return None
    try:
        reset_at = _utc(datetime.fromisoformat(entry["reset_at"]))
    except ValueError:
        return None
    if reset_at <= now:
        return None
    return {"reset_at": reset_at.isoformat(), "message": entry["message"]}


def local_cli_usage(
    engine: Engine, settings: Settings, username: str, *, now: datetime | None = None
) -> dict[str, Any]:
    """Sum the CLI subscription calls that the user's Skynet runs made.

    Args:
        engine: The database that holds the usage evidence.
        settings: Settings with ``local_state_dir`` for the saved caps.
        username: The owner of the runs.
        now: The current time; the clock by default.

    Returns:
        ``{generated_at, windows, clis: [{cli, last_used_at, cap, windows:
        {"5h": totals, "7d": totals}}]}``. Totals hold ``calls``,
        ``failed_calls``, ``runs``, ``input_tokens`` and ``output_tokens``.
    """
    now = _utc(now or datetime.now(UTC))
    oldest = now - max(span for _, span in WINDOWS)
    with Session(engine) as session:
        rows = session.execute(
            select(
                ExecutionUsageEvidenceModel.evidence,
                ExecutionUsageEvidenceModel.created_at,
                ExecutionBudgetModel.id,
            )
            .join(ExecutionOperationModel, ExecutionOperationModel.id == ExecutionUsageEvidenceModel.operation_id)
            .join(ExecutionBudgetModel, ExecutionBudgetModel.id == ExecutionOperationModel.budget_id)
            .where(ExecutionBudgetModel.username == username, ExecutionUsageEvidenceModel.created_at >= oldest)
        ).all()
    totals = {cli: {name: _empty_window() for name, _ in WINDOWS} for cli in CLI_IDS}
    runs: dict[tuple[str, str], set[str]] = {}
    last_used: dict[str, datetime] = {}
    for evidence, created_at, budget_id in rows:
        created_at = _utc(created_at)
        for cli, failed, call in _calls(evidence):
            if cli not in totals:
                continue
            last_used[cli] = max(last_used.get(cli, created_at), created_at)
            for name, span in WINDOWS:
                if created_at < now - span:
                    continue
                window = totals[cli][name]
                window["calls"] += 1
                window["failed_calls"] += int(failed)
                window["input_tokens"] += _tokens(call, "prompt_tokens")
                window["output_tokens"] += _tokens(call, "completion_tokens")
                runs.setdefault((cli, name), set()).add(budget_id)
    for (cli, name), budgets in runs.items():
        totals[cli][name]["runs"] = len(budgets)
    limits = read_cli_limits(settings)
    return {
        "generated_at": now.isoformat(),
        "windows": [name for name, _ in WINDOWS],
        "clis": [
            {
                "cli": cli,
                "last_used_at": last_used[cli].isoformat() if cli in last_used else None,
                "cap": _cap(limits, cli, now),
                "windows": totals[cli],
            }
            for cli in CLI_IDS
        ],
    }
