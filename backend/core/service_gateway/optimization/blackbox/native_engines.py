"""Meta-Harness and AutoResearch loops behind the gepa.oa ``Engine`` contract.

This standalone module is copied into the selected runtime beside
``native_runner.py``. The upstream Meta-Harness repository is a research script
locked to its own domain (memory systems for text classification), so it cannot
be imported as a library. This module reimplements its loop structure, state
files and proposer prompt, swapping the domain-specific evaluation for the
gepa.oa ``EvalServer`` the run scores with. That prompt is derived at run time
from the verbatim upstream file under ``upstream_prompts/`` through
exact-snippet substitutions, so a pin bump that changes the upstream wording
fails loudly instead of drifting silently.

AutoResearch is Skynet's own engine: round-based, directive-driven research
over a per-example Pareto frontier, with the evidence recorded by the engine
rather than by the agent.

Attribution, all MIT-licensed: Meta-Harness, Copyright (c) 2026 Yoonho Lee
(stanford-iris-lab/meta-harness); gepa.oa, Copyright (c) 2025 Lakshya A Agrawal
(gepa-ai/gepa). License texts ship in ``upstream_prompts/``
and ``backend/THIRD_PARTY_NOTICES.md``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gepa.gepa_launcher import EngineConfig, GEPAConfig, ReflectionConfig, optimize_anything
from gepa.oa.budget import BudgetExhausted
from gepa.oa.config import OptimizeAnythingConfig
from gepa.oa.engine import Result
from gepa.oa.eval_server import EvalServer
from gepa.oa.task import Task, seed_as_text

try:
    from . import repo_tree
except ImportError:  # In the sandbox the runner loads ``repo_tree`` as a top-level sibling first.
    import repo_tree

META_HARNESS_REVISION = "0cbc31e97c9e6d24232d1dc754827c02e1ec415c"
AUTORESEARCH_VERSION = "1"
PROMPTS_DIR = Path(__file__).with_name("upstream_prompts")
# Mirrors core.run_log.RECORD_ATTR: this module runs in the sandbox without Skynet's code.
RUN_LOG_ATTR = "run_log"
run_log = logging.getLogger("app.engine")


def log_event(
    event: str,
    message: str,
    *,
    level: int = logging.INFO,
    source: str = "engine",
    candidate: Any = None,
    case: Any = None,
    **fields: Any,
) -> None:
    """Log one typed run event, which the runner streams to the job's run log.

    Args:
        event: Event name, such as ``proposer.done``.
        message: Human-readable line.
        level: Logging level.
        source: ``engine`` or ``proposer``.
        candidate: Candidate the event belongs to.
        case: Case the event belongs to.
        **fields: JSON-serializable event payload.
    """
    run_log.log(
        level,
        "%s",
        message,
        extra={
            RUN_LOG_ATTR: {
                "source": source,
                "event": event,
                "fields": fields or None,
                "candidate": None if candidate is None else str(candidate),
                "case": None if case is None else str(case),
            }
        },
    )


def blocked_patch(repo: dict[str, Any], patch: str) -> str | None:
    """Refuse a version that breaks the path rules of a run with a constraints file.

    The agent reads only the fixed illegal-action message; the run log keeps the
    reason. A run without a constraints file is not checked here: its guest
    gates keep naming the refused paths to the agent.

    Args:
        repo: Repository settings holding ``editable_paths``, ``readonly_paths``
            and ``constrained``.
        patch: The version's diff against the starting commit.

    Returns:
        The illegal-action message for a refused version, else ``None``.
    """
    if not repo.get("constrained"):
        return None
    problems = repo_tree.patch_violations(patch, repo["editable_paths"], repo["readonly_paths"])
    if not problems:
        return None
    reason = " ".join(problems)
    log_event(
        repo_tree.RULE_BLOCK_EVENT,
        f"Illegal action blocked: {repo_tree.PATH_RULE}. {reason}",
        level=logging.WARNING,
        rule=repo_tree.PATH_RULE,
        reason=reason,
    )
    return repo_tree.ILLEGAL_ACTION_MESSAGE


ASSET_CHECKSUMS = {
    "meta_harness/SKILL.md": "fce9a51d2e95d8a2d59c60b91106adc0309232a8d6b2fe0fa0785395dcb78d1c",
}
BUDGET_EXHAUSTED_MARKER = "BUDGET_EXHAUSTED"
# Upstream meta_harness.py: PROPOSER_ALLOWED_TOOLS.
META_HARNESS_TOOLS = "Read,Glob,Grep,Agent,Write,Edit,Bash"
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,80}$")
_SESSION_POLL_SECONDS = 0.2
# The CLI stderr tail carried into a failed run's error and its run log.
_FAILURE_TAIL_CHARS = 2000

# AutoResearch tuning: frontier and dossier sizes keep STATE.md readable; the
# stale-round thresholds decide when to change tack and when to stop paying for
# rounds that no longer move the leader.
_FRONTIER_SIZE = 4
_LEADERBOARD_SIZE = 10
_DOSSIER_EXAMPLES = 5
_FEEDBACK_CHARS = 600
_PIVOT_AFTER = 2
_STOP_AFTER = 4
_PLANNED_ROUNDS = 6
# A named score's Pareto column is keyed apart from example ids.
NAMED_PREFIX = "score:"
# Once a round spends its allowance the agent still gets time to write up the notebook.
_WRITE_UP_GRACE_SECONDS = 90

# With no example ids the call is a full evaluation over the whole visible pool,
# which is what the server logs as progress; with ids it is a probe. HTTP 429
# surfaces the BUDGET_EXHAUSTED marker the brief tells the agent to obey. The
# JSON body is built with the runtime's own interpreter because the sandbox
# image is not guaranteed to ship jq.
EVAL_SCRIPT = """\
#!/usr/bin/env bash
# Usage: ./eval.sh <candidate_file> [example_id ...]
set -euo pipefail
CANDIDATE_FILE="$1"
shift
SERVER_URL="{server_url}"
PYTHON="{python}"
BODY=$(CANDIDATE_FILE="$CANDIDATE_FILE" "$PYTHON" -c 'import json, os, sys
body = {{"candidate": open(os.environ["CANDIDATE_FILE"], encoding="utf-8").read()}}
if sys.argv[1:]:
    body["example_ids"] = sys.argv[1:]
print(json.dumps(body))' "$@")
RESPONSE=$(curl -s -w "\\n%{{http_code}}" -X POST "$SERVER_URL/{route}" \\
    -H "Content-Type: application/json" -d "$BODY")
HTTP_CODE=$(echo "$RESPONSE" | tail -1)
BODY=$(echo "$RESPONSE" | sed '$d')
echo "$BODY"
if [ "$HTTP_CODE" = "429" ]; then echo "{marker}" >&2; exit 1; fi
if [ "$HTTP_CODE" != "200" ]; then echo "evaluator returned HTTP $HTTP_CODE" >&2; exit 1; fi
"""

# Repository mode: the candidate is the diff between the lab's ``repo/`` checkout
# and the commit it started from. The rules file and the shared ``repo_tree``
# module reject a diff the trusted scorer would refuse anyway, without spending
# budget; the scorer still enforces the same rules on its own. Under a
# constraints file the engine's evaluator wrapper refuses it instead, so the
# block reaches the run log. Example ids are probes, as in text mode.
REPO_EVAL_SCRIPT = """\
#!/usr/bin/env bash
# Usage: ./eval.sh [example_id ...]
set -euo pipefail
cd "$(dirname "$0")"
PATCH_FILE=$(mktemp)
trap 'rm -f "$PATCH_FILE"' EXIT
git -C repo add --all
git -C repo diff --cached --binary --no-color --no-ext-diff {base} > "$PATCH_FILE"
SERVER_URL="{server_url}"
PYTHON="{python}"
BODY=$(PATCH_FILE="$PATCH_FILE" PYTHONPATH="{tools}" "$PYTHON" -c 'import json, os, sys
import repo_tree
try:
    patch = open(os.environ["PATCH_FILE"], encoding="utf-8").read()
except UnicodeDecodeError:
    sys.exit("Not evaluated: a changed text file is not UTF-8.")
rules = json.load(open(".repo-rules.json", encoding="utf-8"))
problems = [] if rules["constrained"] else repo_tree.patch_violations(
    patch, rules["editable_paths"], rules["readonly_paths"]
)
if problems:
    sys.exit("Not evaluated:\\n" + "\\n".join(problems))
body = {{"candidate": patch}}
if sys.argv[1:]:
    body["example_ids"] = sys.argv[1:]
print(json.dumps(body))' "$@")
RESPONSE=$(curl -s -w "\\n%{{http_code}}" -X POST "$SERVER_URL/{route}" \\
    -H "Content-Type: application/json" --data-binary @- <<< "$BODY")
HTTP_CODE=$(echo "$RESPONSE" | tail -1)
BODY=$(echo "$RESPONSE" | sed '$d')
echo "$BODY"
if [ "$HTTP_CODE" = "429" ]; then echo "{marker}" >&2; exit 1; fi
if [ "$HTTP_CODE" != "200" ]; then echo "evaluator returned HTTP $HTTP_CODE" >&2; exit 1; fi
"""

CHECKOUT_SCRIPT = """\
#!/usr/bin/env bash
# Usage: ./checkout.sh <id>   (an archive id such as c003, or "base" for the starting commit)
set -euo pipefail
cd "$(dirname "$0")"
VERSION="${{1:?usage: ./checkout.sh <id|base>}}"
case "$VERSION" in *[!A-Za-z0-9]*) echo "unknown version: $VERSION" >&2; exit 2;; esac
if [ "$VERSION" != "base" ] && [ ! -f "archive/$VERSION.patch" ]; then
    echo "unknown version: $VERSION" >&2; exit 2
fi
git -C repo reset --quiet --hard {base}
git -C repo clean --quiet -fd
if [ "$VERSION" != "base" ] && [ -s "archive/$VERSION.patch" ]; then
    git -C repo apply --binary --whitespace=nowarn "$PWD/archive/$VERSION.patch"
fi
echo "repo/ is now $VERSION"
"""

# Meta-Harness repository mode: a candidate is ``agents/<name>.patch``, the diff
# of ``repo/`` against the starting commit when the proposer saved it. The same
# rules check as ``eval.sh`` refuses a diff the trusted scorer would refuse, so
# a bad candidate fails here instead of costing a benchmark. Under a
# constraints file a refused diff moves to the blocks folder, which the engine
# reads after each proposer turn to log the block.
MH_SAVE_SCRIPT = """\
#!/usr/bin/env bash
# Usage: ./save.sh <name>   (writes repo/'s changes against the starting commit to agents/<name>.patch)
set -euo pipefail
cd "$(dirname "$0")"
NAME="${{1:?usage: ./save.sh <name>}}"
case "$NAME" in .*|*[!A-Za-z0-9_.-]*) echo "invalid candidate name: $NAME" >&2; exit 2;; esac
PATCH_FILE="agents/$NAME.patch"
git -C repo add --all
git -C repo diff --cached --binary --no-color --no-ext-diff {base} > "$PATCH_FILE"
PATCH_FILE="$PATCH_FILE" PYTHONPATH="{tools}" "{python}" -c 'import json, os, sys, time
import repo_tree
path = os.environ["PATCH_FILE"]
try:
    patch = open(path, encoding="utf-8").read()
except UnicodeDecodeError:
    os.remove(path)
    sys.exit("Not saved: a changed text file is not UTF-8.")
rules = json.load(open(".repo-rules.json", encoding="utf-8"))
problems = repo_tree.patch_violations(patch, rules["editable_paths"], rules["readonly_paths"])
if problems and rules["constrained"]:
    os.makedirs("{blocks}", exist_ok=True)
    os.replace(path, os.path.join("{blocks}", f"{{time.time_ns()}}.patch"))
    sys.exit(repo_tree.ILLEGAL_ACTION_MESSAGE)
if not patch.strip():
    problems.append("repo/ has no changes against the starting commit.")
if len(patch.encode("utf-8")) > repo_tree.MAX_PATCH_BYTES:
    problems.append("The change is larger than a version may be.")
if problems:
    os.remove(path)
    sys.exit("Not saved:\\n" + "\\n".join(problems))'
echo "saved $PATCH_FILE"
"""

MH_RULE_BLOCKS_DIR = ".rule-blocks"

MH_CHECKOUT_SCRIPT = """\
#!/usr/bin/env bash
# Usage: ./checkout.sh <name>   (a saved candidate such as seed, or "base" for the starting commit)
set -euo pipefail
cd "$(dirname "$0")"
NAME="${{1:?usage: ./checkout.sh <name|base>}}"
case "$NAME" in .*|*[!A-Za-z0-9_.-]*) echo "unknown candidate: $NAME" >&2; exit 2;; esac
if [ "$NAME" != "base" ] && [ ! -f "agents/$NAME.patch" ]; then
    echo "unknown candidate: $NAME" >&2; exit 2
fi
git -C repo reset --quiet --hard {base}
git -C repo clean --quiet -fd
if [ "$NAME" != "base" ] && [ -s "agents/$NAME.patch" ]; then
    git -C repo apply --binary --whitespace=nowarn "$PWD/agents/$NAME.patch"
fi
echo "repo/ is now $NAME"
"""


def load_asset(relative: str) -> str:
    """Read a vendored upstream prompt and verify it is the pinned revision's text.

    Args:
        relative: Path under ``upstream_prompts/``.

    Returns:
        The verbatim upstream file.

    Raises:
        RuntimeError: When the file is missing or differs from the pinned checksum.
    """
    path = PROMPTS_DIR / relative
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Missing pinned upstream prompt {relative}: {exc}") from exc
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if digest != ASSET_CHECKSUMS[relative]:
        raise RuntimeError(f"Pinned upstream prompt {relative} drifted (sha256 {digest}).")
    return text


def adapt(text: str, edits: list[tuple[str, ...]]) -> str:
    """Apply exact-snippet substitutions, each of which must match exactly once.

    Args:
        text: Verbatim upstream text.
        edits: ``(old, new)`` replacements, or ``(start, end, new)`` spans replaced
            from the start marker through the end marker inclusive.

    Returns:
        The adapted text.

    Raises:
        RuntimeError: When a snippet is absent or ambiguous, which means the
            upstream wording changed under the pin.
    """
    for edit in edits:
        if len(edit) == 2:
            old, new = edit
            if text.count(old) != 1:
                raise RuntimeError(f"Upstream prompt snippet matched {text.count(old)} times: {old[:60]!r}")
            text = text.replace(old, new)
            continue
        start, end, new = edit
        if text.count(start) != 1 or text.count(end) != 1:
            raise RuntimeError(f"Upstream prompt span is not unique: {start[:60]!r} .. {end[:60]!r}")
        head = text.index(start)
        tail = text.index(end) + len(end)
        if tail <= head:
            raise RuntimeError(f"Upstream prompt span is inverted: {start[:60]!r} .. {end[:60]!r}")
        text = text[:head] + new + text[tail:]
    return text


def _plural(count: int, noun: str) -> str:
    """Render ``count`` with ``noun`` pluralized by an ``s``.

    Args:
        count: Number of items.
        noun: Singular noun.

    Returns:
        A phrase such as ``3 candidates`` or ``1 candidate``.
    """
    return f"{count} {noun}{'' if count == 1 else 's'}"


def visible_example_ids(server: EvalServer) -> list[str]:
    """List the training and validation example ids the server exposes.

    Args:
        server: Evaluation server for the current task.

    Returns:
        Example ids in split order; empty for single-candidate tasks.
    """
    return [eid for split in ("train", "val") for eid, _ in server.iter_split(split)]


# Sweep details list the case ids that ``native_runner`` filled, so an engine
# that must not rank a partly crashed version can tell it apart (SKY-367).
UNSCORED_CASES_KEY = "unscored_cases"
TEXT_CANDIDATE_KEY = "current_candidate"


def fill_unscored(keys: list[str], scores: list[float]) -> list[float]:
    """Give each null case its version's mean over the scored cases in the same batch.

    In the sandbox a null score arrives as negative infinity. Upstream engines
    average a version's cases, so one case that always crashes would make every
    version negative infinity and hide which one is better (SKY-367).

    Args:
        keys: The version scored on each case, as a stable key.
        scores: Each case's score, negative infinity for a null case.

    Returns:
        A number for every case: the score, or the same version's scored mean
        for a null case, or negative infinity when the version has no scored case.
    """
    scored: dict[str, list[float]] = {}
    for key, score in zip(keys, scores, strict=True):
        if math.isfinite(score):
            scored.setdefault(key, []).append(score)
    filled: list[float] = []
    for key, score in zip(keys, scores, strict=True):
        values = scored.get(key)
        if not math.isfinite(score):
            score = sum(values) / len(values) if values else float("-inf")
        filled.append(score)
    return filled


def _logged(server: EvalServer, candidate: str, score: float) -> bool:
    """Check that the server logged this aggregate score for this candidate.

    Args:
        server: Evaluation server whose ``progress_log`` holds the checkpoints.
        candidate: Candidate text.
        score: Aggregate score to find.

    Returns:
        Whether a progress entry has the candidate's id and this ``val_score``.
    """
    candidate_id = server._candidate_registry.get(candidate)
    return candidate_id is not None and any(
        entry.get("candidate_id") == candidate_id and entry.get("val_score") == score for entry in server.progress_log
    )


def best_aggregate_candidate(server: EvalServer, only: Collection[str] | None = None) -> tuple[str, float] | None:
    """Return the best candidate among the server's logged aggregate checkpoints.

    Dataset ``EvalServer.best_score`` is the best per-example score, not a
    candidate-level aggregate; ``/evaluate_examples`` and the engines log the
    aggregates with candidate ids instead.

    Args:
        server: Evaluation server whose ``progress_log`` holds the checkpoints.
        only: Consider only these candidates, or every candidate when ``None``.

    Returns:
        ``(candidate, score)`` for the highest logged aggregate, or ``None``.
    """
    by_id = {cid: candidate for candidate, cid in server._candidate_registry.items()}
    best: tuple[str, float] | None = None
    for entry in server.progress_log:
        candidate = by_id.get(entry.get("candidate_id"))
        score = entry.get("val_score")
        if candidate is None or not isinstance(score, int | float) or not math.isfinite(score):
            continue
        if only is not None and candidate not in only:
            continue
        if best is None or score > best[1]:
            best = (candidate, float(score))
    return best


class ProposerFailedError(RuntimeError):
    """The proposer CLI failed on its own, so the run cannot continue."""


@dataclass
class ProposerOutcome:
    """One ``claude --print`` invocation's parsed result."""

    session_id: str
    cost_usd: float
    is_error: bool
    text: str
    budget_exhausted: bool
    killed: bool
    returncode: int | None = None
    stderr_tail: str = ""

    def raise_if_failed(self, name: str) -> None:
        """Fail the run when the CLI errored without the engine stopping it or the budget running out.

        Before this, an errored session was treated as a session that found
        nothing, so a CLI that could not start ended the run as a success built
        from the seed alone.

        Args:
            name: Session label used in the message, such as ``round1``.

        Raises:
            ProposerFailedError: When the session failed on its own.
        """
        if not self.is_error or self.killed or self.budget_exhausted:
            return
        detail = self.stderr_tail.strip() or self.text.strip() or "no output"
        raise ProposerFailedError(
            f"The proposer CLI session {name} failed (exit {self.returncode}): {detail[-_FAILURE_TAIL_CHARS:]}"
        )


# Variables a proposer never inherits. The raw provider key and nested-session
# flag follow upstream; the scorer/metric names keep any path or key the
# parent's scorer uses out of the agent's reach.
_PROPOSER_ENV_DROP = frozenset({"CLAUDECODE", "ANTHROPIC_API_KEY"})
# The legacy spellings stay listed: an older control plane may still set them.
_PROPOSER_ENV_DROP_PREFIXES = ("APP_SCORER", "APP_METRIC", "SKYNET_SCORER", "SKYNET_METRIC")


def proposer_environment(environ: dict[str, str]) -> dict[str, str]:
    """Copy the box environment for a proposer, without what it must not see.

    Args:
        environ: The runner's environment.

    Returns:
        The environment the proposer CLI runs with.
    """
    return {
        name: value
        for name, value in environ.items()
        if name not in _PROPOSER_ENV_DROP and not name.startswith(_PROPOSER_ENV_DROP_PREFIXES)
    }


def run_proposer(
    prompt: str,
    *,
    work_dir: Path,
    log_dir: Path,
    name: str,
    model: str,
    session_id: str,
    resume: bool = False,
    max_budget_usd: float | None = None,
    tools: str | None = None,
    append_system_prompt: str | None = None,
    should_kill: Callable[[], bool] | None = None,
) -> ProposerOutcome:
    """Run the proposer through the ``claude`` command, exactly as the upstream drivers do.

    The harness bridge answers to ``claude`` for the configured harness, so this
    is the single invocation shape every proposer must answer.

    Args:
        prompt: Task prompt for this session.
        work_dir: Workspace the agent edits.
        log_dir: Where the CLI stdout, stderr and metadata land.
        name: Log file stem, such as ``iter3`` or ``session2``.
        model: Model identifier passed to the CLI.
        session_id: Session to create or, with ``resume``, to continue.
        resume: Whether to continue ``session_id`` instead of starting it.
        max_budget_usd: Remaining proposer spend passed as ``--max-budget-usd``.
        tools: Comma-separated tool whitelist, or ``None`` for the CLI default.
        append_system_prompt: Extra system prompt (the Meta-Harness skill).
        should_kill: Polled while the CLI runs; returning ``True`` terminates it.

    Returns:
        The parsed outcome; a CLI failure is reported, not raised.
    """
    cmd = ["claude", "--print", "--output-format", "json", "--model", model, "--dangerously-skip-permissions"]
    cmd.extend(["--resume", session_id] if resume else ["--session-id", session_id])
    if tools:
        cmd.extend(["--tools", tools, "--allowedTools", tools])
    if append_system_prompt:
        cmd.extend(["--append-system-prompt", append_system_prompt])
    if max_budget_usd is not None:
        cmd.extend(["--max-budget-usd", f"{max(0.0, max_budget_usd):.6f}"])
    cmd.append(prompt)
    env = proposer_environment(dict(os.environ))
    log_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    killed = False
    log_event(
        "proposer.start",
        f"Proposer session {name} started" + (" (resumed)" if resume else ""),
        source="proposer",
        session=name,
        model=model,
        resume=resume,
        max_budget_usd=max_budget_usd,
    )
    with (
        (log_dir / f"{name}_stdout.json").open("w", encoding="utf-8") as stdout_file,
        (log_dir / f"{name}_stderr.txt").open("w", encoding="utf-8") as stderr_file,
    ):
        process = subprocess.Popen(
            cmd, cwd=str(work_dir), env=env, stdout=stdout_file, stderr=stderr_file, text=True, start_new_session=True
        )
        while process.poll() is None:
            if should_kill is not None and should_kill():
                killed = True
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                break
            time.sleep(_SESSION_POLL_SECONDS)
    stdout = (log_dir / f"{name}_stdout.json").read_text(encoding="utf-8")
    stderr = (log_dir / f"{name}_stderr.txt").read_text(encoding="utf-8")
    document = _result_document(stdout)
    outcome = ProposerOutcome(
        session_id=str(document.get("session_id") or session_id),
        cost_usd=float(document.get("total_cost_usd") or 0.0),
        is_error=killed or process.returncode != 0 or bool(document.get("is_error")),
        text=str(document.get("result") or ""),
        budget_exhausted=BUDGET_EXHAUSTED_MARKER in stdout or BUDGET_EXHAUSTED_MARKER in stderr,
        killed=killed,
        returncode=process.returncode,
        stderr_tail=stderr[-_FAILURE_TAIL_CHARS:],
    )
    duration = round(time.monotonic() - started, 1)
    log_event(
        "proposer.done",
        f"Proposer session {name} {'failed' if outcome.is_error else 'finished'} in {duration}s, "
        f"spent {outcome.cost_usd:.4f} dollars",
        level=logging.WARNING if outcome.is_error and not killed else logging.INFO,
        source="proposer",
        session=name,
        duration_s=duration,
        cost_usd=outcome.cost_usd,
        returncode=process.returncode,
        killed=killed,
        budget_exhausted=outcome.budget_exhausted,
    )
    if outcome.is_error:
        # The runner's stderr reaches the job's run log; the CLI's own files die with the sandbox.
        print(
            f"[{name}] proposer CLI exited {process.returncode}"
            f"{' after being stopped' if killed else ''}: {(stderr.strip() or outcome.text.strip())[-_FAILURE_TAIL_CHARS:]}",
            file=sys.stderr,
            flush=True,
        )
    (log_dir / f"{name}_meta.json").write_text(
        json.dumps(
            {
                "session_id": outcome.session_id,
                "returncode": process.returncode,
                "cost_usd": outcome.cost_usd,
                "is_error": outcome.is_error,
                "killed": killed,
                "duration_s": round(time.monotonic() - started, 1),
            }
        ),
        encoding="utf-8",
    )
    return outcome


def _result_document(stdout: str) -> dict[str, Any]:
    """Find the ``claude --output-format json`` result object in captured stdout.

    Args:
        stdout: Captured CLI output.

    Returns:
        The result object, or an empty mapping when none parses.
    """
    for raw in (stdout.strip(), *reversed(stdout.strip().splitlines())):
        try:
            payload = json.loads(raw)
        except ValueError:
            continue
        if isinstance(payload, dict) and ("result" in payload or "session_id" in payload):
            return payload
    return {}


def _remaining_cost(max_token_cost: float | None, spent: float) -> float | None:
    """Return the proposer spend still allowed, or ``None`` when uncapped.

    Args:
        max_token_cost: Configured proposer cap in USD.
        spent: Summed CLI-reported cost so far.

    Returns:
        Remaining USD, never negative, or ``None``.
    """
    return None if max_token_cost is None else max(0.0, float(max_token_cost) - spent)


def _reached(stop_at_score: float | None, score: float | None) -> bool:
    """Tell whether ``score`` satisfies the configured early-stop threshold.

    Args:
        stop_at_score: Threshold from the run config, or ``None``.
        score: Best score so far, or ``None``.

    Returns:
        ``True`` only when both are set and the threshold is met.
    """
    return stop_at_score is not None and score is not None and score >= stop_at_score


class ScoredThresholdStopper:
    """GEPA stopper that ignores versions with a crashed case.

    GEPA ranks a version by the mean with each null case filled (SKY-367).
    Final selection drops such a version, so its filled mean must not stop
    the run either.
    """

    def __init__(self, threshold: float, crashed: Callable[[Any], bool]) -> None:
        """Bind the threshold and the crash check.

        Args:
            threshold: The run's ``stop_at_score``.
            crashed: Tells whether a GEPA program candidate hit a metric error.
        """
        self.threshold = threshold
        self.crashed = crashed

    def __call__(self, gepa_state: Any) -> bool:
        """Tell whether a version with no crashed case reached the threshold.

        Args:
            gepa_state: GEPA's run state.

        Returns:
            ``True`` once such a version's validation mean meets the threshold.
        """
        pairs = zip(gepa_state.program_candidates, gepa_state.program_full_scores_val_set, strict=False)
        return any(score >= self.threshold for candidate, score in pairs if not self.crashed(candidate))


def unwrap_text_candidate(candidate: Any) -> Any:
    """Return the text of a GEPA text-mode program candidate.

    Args:
        candidate: A GEPA program candidate.

    Returns:
        The text inside a one-key ``current_candidate`` mapping, else ``candidate``.
    """
    if isinstance(candidate, dict) and list(candidate) == [TEXT_CANDIDATE_KEY]:
        return candidate[TEXT_CANDIDATE_KEY]
    return candidate


def task_brief(task: Task, example_ids: list[str]) -> str:
    """Describe the task the way the upstream repositories' READMEs describe theirs.

    Args:
        task: Task being optimized.
        example_ids: Visible example ids, empty for single-candidate tasks.

    Returns:
        Markdown with the objective, background and evaluation scope.
    """
    lines = [f"# {task.name}", ""]
    if task.objective:
        lines += ["## Objective", "", str(task.objective).strip(), ""]
    if task.background:
        lines += ["## Background", "", str(task.background).strip(), ""]
    lines += ["## Evaluation", ""]
    if example_ids:
        lines += [
            f"The evaluator scores a candidate on {_plural(len(example_ids), 'example')} and reports the "
            "average score (higher is better) together with per-example scores and feedback.",
            "",
            "Example ids: " + ", ".join(f"`{eid}`" for eid in example_ids),
            "",
            "The final result is measured afresh on these examples; a change that only helps by special-casing "
            "one of them is not an improvement.",
        ]
    else:
        lines += [
            "The evaluator scores the candidate as a whole and reports a score (higher is better) with feedback "
            "explaining it."
        ]
    lines += [
        "",
        "The evaluator may also report named scores, each with its own feedback. Each named score is a column "
        f"(`{NAMED_PREFIX}<name>`) of the per-example scores and an axis of the Pareto front, so a candidate that "
        "wins on one of them is worth keeping even when its average is lower.",
    ]
    return "\n".join(lines) + "\n"


@dataclass
class Observation:
    """One evaluator call the research agent made, as the server answered it."""

    candidate: str
    # The candidate's score, the mean over the examples it was scored on.
    score: float
    # Pareto columns: per-example scores, then named scores as ``score:<name>``.
    scores: dict[str, float]
    feedback: dict[str, str]
    full: bool
    round: int


def _feedback_text(info: Any) -> str:
    """Condense one example's evaluator info into a short readable note.

    Args:
        info: The per-example info the scorer returned.

    Returns:
        The feedback string, or a compact JSON rendering, clipped to a readable size.
    """
    if isinstance(info, dict) and isinstance(info.get("feedback"), str):
        text = info["feedback"]
    elif info in (None, {}):
        text = ""
    else:
        text = json.dumps(info, default=str, sort_keys=True)
    return text if len(text) <= _FEEDBACK_CHARS else text[:_FEEDBACK_CHARS] + " …"


def named_score_columns(infos: list[Any]) -> tuple[dict[str, float], dict[str, str]]:
    """Average a scorer's named scores over the examples into Pareto columns.

    Args:
        infos: The scorer's side info for each example scored (one item without examples).

    Returns:
        ``(scores, feedback)`` keyed ``score:<name>``: each name's mean score,
        and its feedback from every example joined into one note.
    """
    collected: dict[str, list[tuple[float, str]]] = {}
    for info in infos:
        raw = info.get("scores") if isinstance(info, dict) else None
        if not isinstance(raw, dict):
            continue
        for name, entry in raw.items():
            value = entry.get("score") if isinstance(entry, dict) else entry
            if isinstance(value, bool) or not isinstance(value, int | float):
                continue
            note = str(entry.get("feedback") or "") if isinstance(entry, dict) else ""
            collected.setdefault(f"{NAMED_PREFIX}{name}", []).append((float(value), note))
    scores = {column: sum(value for value, _ in entries) / len(entries) for column, entries in collected.items()}
    feedback = {
        column: _feedback_text({"feedback": " | ".join(note for _, note in entries if note)})
        for column, entries in collected.items()
    }
    return scores, feedback


def gepa_named_scores(side_info: Any) -> Any:
    """Shape side info for GEPA: numeric named scores for its frontier, their feedback beside them.

    Args:
        side_info: What the scorer returned next to the score.

    Returns:
        A copy whose ``scores`` maps each name to its number, with each named
        score's feedback under ``Feedback per score``; anything else unchanged.
    """
    raw = side_info.get("scores") if isinstance(side_info, dict) else None
    if not isinstance(raw, dict) or not any(isinstance(entry, dict) for entry in raw.values()):
        return side_info
    numbers = {name: entry.get("score") if isinstance(entry, dict) else entry for name, entry in raw.items()}
    notes = {name: entry.get("feedback") for name, entry in raw.items() if isinstance(entry, dict)}
    return {**side_info, "scores": numbers, "Feedback per score": {k: v for k, v in notes.items() if v}}


def record_text(record: Any) -> str:
    """Render one GEPA reflective record: the feedback, then each named score with its own feedback.

    Args:
        record: A record from GEPA's reflective dataset.

    Returns:
        A short readable note.
    """
    if not isinstance(record, dict):
        return _feedback_text(record)
    numbers = record.get("Scores (Higher is Better)")
    notes = record.get("Feedback per score")
    lines = [_feedback_text({"feedback": record["feedback"]}) if isinstance(record.get("feedback"), str) else ""]
    if isinstance(numbers, dict):
        notes = notes if isinstance(notes, dict) else {}
        for name, value in numbers.items():
            note = notes.get(name)
            lines.append(f"   - {name}: {value}" + (f" — {_feedback_text({'feedback': note})}" if note else ""))
    text = "\n".join(line for line in lines if line)
    return text or _feedback_text(record)


def pareto_front(table: dict[str, dict[str, float]], ranking: dict[str, float]) -> list[str]:
    """Return the candidates no other candidate beats on every column.

    Args:
        table: Per-example and named-score columns of each fully evaluated candidate.
        ranking: Each candidate's overall score, to order the front.

    Returns:
        The non-dominated candidates, highest overall score first.
    """

    def dominates(a: dict[str, float], b: dict[str, float]) -> bool:
        """Tell whether ``a`` is at least as good as ``b`` everywhere and better somewhere."""
        return all(a.get(k, 0.0) >= b[k] for k in b) and any(a.get(k, 0.0) > b[k] for k in b)

    front = [c for c, row in table.items() if not any(dominates(other, row) for d, other in table.items() if d != c)]
    return sorted(front, key=lambda c: -ranking.get(c, _mean(table[c])))


def _mean(row: dict[str, float]) -> float:
    """Average a per-example score row.

    Args:
        row: Scores keyed by example id.

    Returns:
        The mean, or 0.0 for an empty row.
    """
    return sum(row.values()) / len(row) if row else 0.0


def repo_rules(repo: dict[str, Any]) -> str:
    """Describe which parts of the repository a version may change.

    Args:
        repo: Repository settings holding ``editable_paths`` and ``readonly_paths``.

    Returns:
        Markdown appended to the task brief.
    """
    editable = ", ".join("the whole repository" if p == "." else f"`{p}`" for p in repo["editable_paths"])
    lines = ["", "## Repository", "", f"You may change: {editable}. Everything else is read-only context."]
    if repo["readonly_paths"]:
        lines += ["", "Submodules and Git LFS files stay as fetched:"]
        lines += [f"- `{path}`" for path in repo["readonly_paths"]]
    return "\n".join(lines) + "\n"


class AutoResearchEngine:
    """Round-based research over a candidate frontier, driven against the evaluation server.

    Each round is a fresh proposer session with an explicit directive the engine
    picks from the evidence so far: survey the seed, exploit a gain, combine two
    frontier candidates that win on different examples, explore the hard
    examples, or pivot after a stall. The engine records every evaluator answer
    itself, so the leaderboard, the per-example Pareto front and the failure
    dossier it hands the agent cannot be edited by the agent. What the agent
    learns carries between rounds in its own ``notebook.md``.
    """

    name = "autoresearch"

    def __init__(self, config: OptimizeAnythingConfig) -> None:
        """Pop the engine knobs from the shared config.

        Args:
            config: Cross-engine run configuration.
        """
        engine_config = dict(config.engine_config)
        self.model = str(engine_config.pop("model"))
        self.multi_round = bool(engine_config.pop("ralph", True))
        no_eval = engine_config.pop("max_no_eval_seconds", None)
        self.max_no_eval_seconds = None if no_eval is None else float(no_eval)
        # ``checkout``, ``base``, ``editable_paths``, ``readonly_paths``,
        # ``constrained`` (a constraints file governs the run) and ``tools``
        # (the folder holding ``repo_tree.py``) for a repository run.
        self.repo: dict[str, Any] | None = engine_config.pop("repo", None)
        self.suffix = ".patch" if self.repo else ".txt"
        self.max_token_cost = config.max_token_cost
        self.stop_at_score = config.stop_at_score
        self.run_dir = Path(config.run_dir or "autoresearch-run").resolve()
        self.work_dir = self.run_dir / "lab"
        self.session_ids: list[str] = []
        self.cost_usd = 0.0
        self.round = 0
        self.directives: list[str] = []
        self.observations: list[Observation] = []
        self.example_ids: list[str] = []
        self._ids: dict[str, str] = {}
        self._lock = threading.Lock()
        # Set by ``_layout`` when a repository run starts from a non-empty
        # starting patch rather than from the fetched commit itself.
        self.repo_seed = False
        self.seed = ""

    def run(self, task: Task, server: EvalServer) -> Result:
        """Run research rounds until the budget, the target score or the evidence says stop.

        Args:
            task: Task with a text seed candidate.
            server: Evaluation server the agent's ``eval.sh`` calls.

        Returns:
            The best candidate the server actually scored.

        Raises:
            RuntimeError: When the agent finished without scoring any candidate.
            ProposerFailedError: When a round's CLI session failed on its own.
        """
        self.example_ids = visible_example_ids(server)
        self.seed = seed_as_text(task.seed_candidate) if task.seed_candidate is not None else ""
        self._layout(task, server)
        restore = self._record(server)
        try:
            stale = 0
            improved = False
            while True:
                remaining = _remaining_cost(self.max_token_cost, self.cost_usd)
                if remaining is not None and remaining <= 0 and self.round:
                    break
                self.round += 1
                directive = self._directive(stale, improved)
                self.directives.append(directive)
                before = self._leader_score()
                evals_before = server.budget.used
                self._write_state(server, directive)
                outcome = self._session(server, directive, max_budget_usd=remaining)
                self.cost_usd += outcome.cost_usd
                self.session_ids.append(outcome.session_id)
                outcome.raise_if_failed(f"round{self.round}")
                after = self._leader_score()
                improved = after is not None and (before is None or after > before)
                stale = 0 if improved else stale + 1
                if not self.multi_round or self._done(server, outcome, evals_before, stale):
                    break
        finally:
            restore()
        return self._result(task, server)

    def incumbent(self, server: EvalServer) -> tuple[str, float] | None:
        """Return the best server-verified candidate so far.

        Args:
            server: Evaluation server with the run's evidence.

        Returns:
            ``(candidate, score)`` or ``None`` before any evaluation.
        """
        if server.task.has_dataset:
            # A version with a crashed case logs a filled mean (SKY-367), so the
            # fully scored sweeps rank first, then the seed, then the rest. A
            # crashed re-sweep of a ranked version can log a higher filled mean,
            # so the ranked score itself must be the logged one.
            ranking = sorted(self._ranking().items(), key=lambda item: -item[1])
            for candidate, score in ranking:
                if math.isfinite(score) and _logged(server, candidate, score):
                    return candidate, score
            return best_aggregate_candidate(server, only={self.seed}) or best_aggregate_candidate(server)
        if server.best_candidate is None or server.best_score is None:
            return None
        return str(server.best_candidate), float(server.best_score)

    def process_result(self, result: Result, output_dir: str | Path) -> None:
        """Write the final candidate, the notebook and run metadata beside the evaluator artifacts.

        Args:
            result: Result returned by ``run``.
            output_dir: Evaluator output directory.
        """
        target = Path(output_dir) / self.name
        target.mkdir(parents=True, exist_ok=True)
        (target / f"best_candidate{self.suffix}").write_text(result.best_candidate, encoding="utf-8")
        (target / "metadata.json").write_text(json.dumps(result.metadata, indent=2, default=str), encoding="utf-8")
        notebook = self.work_dir / "notebook.md"
        if notebook.is_file():
            shutil.copyfile(notebook, target / "notebook.md")

    def _refuse_blocked(self, candidate: str) -> None:
        """Stop a version the constraints file refuses before it uses any budget.

        Args:
            candidate: The version's diff.

        Raises:
            RuntimeError: With the illegal-action message, which the evaluation
                server returns to ``eval.sh``.
        """
        refusal = blocked_patch(self.repo, candidate) if self.repo else None
        if refusal:
            raise RuntimeError(refusal)

    def _record(self, server: EvalServer) -> Callable[[], None]:
        """Capture every evaluator answer the agent receives, per example.

        The server's own logs keep only aggregates, and a file the agent writes
        could be edited, so the engine wraps the scoring entry point the HTTP
        handler calls.

        Args:
            server: Evaluation server to observe.

        Returns:
            A function that removes the wrapper.
        """
        visible = set(self.example_ids)
        if server.task.has_dataset:
            original = server.evaluate_examples

            def evaluate_examples(candidate: str, example_ids: Any = None, split: Any = None) -> Any:
                """Score through the server, then log the per-example answer."""
                self._refuse_blocked(candidate)
                score, info = original(candidate, example_ids=example_ids, split=split)
                scores = {str(k): float(v) for k, v in (info.get("scores") or {}).items()}
                feedback = {str(k): _feedback_text(v) for k, v in (info.get("infos") or {}).items()}
                for eid, error in (info.get("errors") or {}).items():
                    feedback[str(eid)] = _feedback_text(f"error: {error}")
                # A crashed case gets the version's mean as a filled score (SKY-367). That mean must not
                # rank or stop the run, so the observation does not count as a full-split score.
                full = set(scores) == visible and not info.get(UNSCORED_CASES_KEY)
                mean = _mean(scores)
                named, named_feedback = named_score_columns(list((info.get("infos") or {}).values()))
                scores.update(named)
                feedback.update(named_feedback)
                self._observe(Observation(candidate, mean, scores, feedback, full, self.round))
                return score, info

            server.evaluate_examples = evaluate_examples  # type: ignore[method-assign]
            return lambda: delattr(server, "evaluate_examples")
        original_single = server.evaluate

        def evaluate(candidate: str, example: Any = None, **kwargs: Any) -> Any:
            """Score through the server, then log the whole-candidate answer."""
            self._refuse_blocked(candidate)
            score, info = original_single(candidate, example, **kwargs)
            # Upstream checkpoints only dataset sweeps; without one the run
            # view would never see a version, so each answer is its checkpoint.
            if math.isfinite(float(score)):
                server.log_progress(float(score), candidate=candidate)
            named, named_feedback = named_score_columns([info])
            # Without examples the named scores are the Pareto axes; the score
            # alone is one when the scorer names none.
            columns = named or {"_single": float(score)}
            feedback = {"_single": _feedback_text(info), **named_feedback}
            self._observe(Observation(candidate, float(score), columns, feedback, True, self.round))
            return score, info

        server.evaluate = evaluate  # type: ignore[method-assign]
        return lambda: delattr(server, "evaluate")

    def _observe(self, observation: Observation) -> None:
        """Store one observation and archive a newly fully evaluated candidate.

        Args:
            observation: The evaluator answer.
        """
        with self._lock:
            self.observations.append(observation)
            if not observation.full or observation.candidate in self._ids:
                return
            cid = f"c{len(self._ids) + 1:03d}"
            self._ids[observation.candidate] = cid
            (self.work_dir / "archive" / f"{cid}{self.suffix}").write_text(observation.candidate, encoding="utf-8")
            with (self.work_dir / "archive" / "index.tsv").open("a", encoding="utf-8") as index:
                index.write(f"{cid}\t{observation.score:.6f}\t{observation.round}\n")

    def _table(self) -> dict[str, dict[str, float]]:
        """Per-example and named-score columns of every fully evaluated candidate, latest answer winning.

        Returns:
            Rows keyed by candidate text.
        """
        with self._lock:
            return {o.candidate: o.scores for o in self.observations if o.full}

    def _ranking(self) -> dict[str, float]:
        """Overall score of every fully evaluated candidate, latest answer winning.

        Returns:
            Scores keyed by candidate text.
        """
        with self._lock:
            return {o.candidate: o.score for o in self.observations if o.full}

    def _columns(self, table: dict[str, dict[str, float]]) -> list[str]:
        """List the Pareto columns worth showing: examples, then named scores.

        Args:
            table: Per-example and named-score columns of fully evaluated candidates.

        Returns:
            Column ids in first-seen order, without the single-score placeholder.
        """
        columns = list(self.example_ids)
        for row in table.values():
            columns += [column for column in row if column not in columns and column != "_single"]
        return columns

    def _leader_score(self) -> float | None:
        """Return the best overall score among fully evaluated candidates.

        Returns:
            The score, or ``None`` before any full evaluation.
        """
        return max(self._ranking().values(), default=None)

    def _directive(self, stale: int, improved: bool) -> str:
        """Pick this round's research directive from the evidence so far.

        Args:
            stale: Consecutive rounds that did not raise the leader's score.
            improved: Whether the previous round raised it.

        Returns:
            One of ``survey``, ``pivot``, ``combine``, ``exploit`` or ``explore``.
        """
        if self.round == 1 or not self._table():
            return "survey"
        if stale >= _PIVOT_AFTER:
            return "pivot"
        if self._partner() is not None and self.directives[-1] != "combine":
            return "combine"
        return "exploit" if improved else "explore"

    def _partner(self) -> tuple[str, list[str]] | None:
        """Find the frontier candidate that most complements the leader.

        Returns:
            ``(candidate, examples it beats the leader on)``, or ``None`` when no
            frontier member wins anywhere the leader loses.
        """
        table = self._table()
        front = pareto_front(table, self._ranking())
        if len(front) < 2:
            return None
        leader = table[front[0]]
        best: tuple[str, list[str]] | None = None
        for candidate in front[1:]:
            wins = [eid for eid, score in table[candidate].items() if score > leader.get(eid, 0.0)]
            if wins and (best is None or len(wins) > len(best[1])):
                best = (candidate, wins)
        return best

    def _round_quota(self, server: EvalServer) -> int | None:
        """Spread the remaining evaluation budget over the planned rounds.

        Args:
            server: Evaluation server with the budget state.

        Returns:
            Evaluation units this round may spend, or ``None`` when uncapped.
        """
        remaining = server.budget.remaining
        if remaining is None:
            return None
        if not self.multi_round:
            return remaining
        pool = max(1, len(self.example_ids))
        rounds_left = max(1, _PLANNED_ROUNDS - self.round + 1)
        return min(remaining, max(pool, -(-remaining // rounds_left)))

    def _layout(self, task: Task, server: EvalServer) -> None:
        """Create the lab directory the brief describes.

        Args:
            task: Task providing the seed and description.
            server: Evaluation server whose URL ``eval.sh`` targets.
        """
        if self.work_dir.exists():
            shutil.rmtree(self.work_dir)
        for sub in ("archive", "frontier") if self.repo else ("archive", "frontier", "work"):
            (self.work_dir / sub).mkdir(parents=True)
        route = "evaluate_examples" if self.example_ids else "evaluate"
        brief = task_brief(task, self.example_ids)
        if self.repo:
            brief += repo_rules(self.repo)
            (self.work_dir / "repo").symlink_to(Path(self.repo["checkout"]), target_is_directory=True)
            rules = {
                "editable_paths": self.repo["editable_paths"],
                "readonly_paths": self.repo["readonly_paths"],
                "constrained": bool(self.repo.get("constrained")),
            }
            (self.work_dir / ".repo-rules.json").write_text(json.dumps(rules), encoding="utf-8")
            script = REPO_EVAL_SCRIPT.format(
                server_url=server.url,
                route=route,
                marker=BUDGET_EXHAUSTED_MARKER,
                python=sys.executable,
                tools=self.repo["tools"],
                base=self.repo["base"],
            )
            checkout_script = self.work_dir / "checkout.sh"
            checkout_script.write_text(CHECKOUT_SCRIPT.format(base=self.repo["base"]), encoding="utf-8")
            checkout_script.chmod(0o755)
            seed = seed_as_text(task.seed_candidate) if task.seed_candidate is not None else ""
            self.repo_seed = bool(seed.strip())
            if self.repo_seed:
                # The seed stays a patch against the fetched commit, so every
                # version the agent saves still diffs against that commit.
                (self.work_dir / "archive" / f"seed{self.suffix}").write_text(seed, encoding="utf-8")
                subprocess.run(["./checkout.sh", "seed"], cwd=self.work_dir, check=True, capture_output=True)
        else:
            (self.work_dir / "work" / "seed.txt").write_text(seed_as_text(task.seed_candidate), encoding="utf-8")
            script = EVAL_SCRIPT.format(
                server_url=server.url, route=route, marker=BUDGET_EXHAUSTED_MARKER, python=sys.executable
            )
        (self.work_dir / "TASK.md").write_text(brief, encoding="utf-8")
        eval_script = self.work_dir / "eval.sh"
        eval_script.write_text(script, encoding="utf-8")
        eval_script.chmod(0o755)
        (self.work_dir / "BRIEF.md").write_text(self._repo_brief() if self.repo else self._brief(), encoding="utf-8")
        (self.work_dir / "notebook.md").write_text(
            "# Research notebook\n\nOne section per round: hypotheses, experiments, outcomes, conclusions.\n",
            encoding="utf-8",
        )
        (self.work_dir / "archive" / "index.tsv").write_text("id\tscore\tround\n", encoding="utf-8")

    def _repo_brief(self) -> str:
        """Write the standing research method for a repository run.

        Returns:
            The ``BRIEF.md`` text.
        """
        probe = (
            "- `./eval.sh <example_id> [<example_id> ...]` is a **probe**: it scores `repo/` on just those examples "
            "and costs one unit per example. Probes check whether a change moves the examples it targets; they "
            "never enter the leaderboard.\n"
            if self.example_ids
            else ""
        )
        full = (
            "one unit per visible example (see `TASK.md`)"
            if self.example_ids
            else "one unit, since the evaluator scores the repository as a whole"
        )
        seed = (
            "- The run starts from a starting version, `archive/seed.patch`, not from the bare starting commit. "
            "`repo/` holds it when the run begins, and `./checkout.sh seed` restores it.\n"
            if self.repo_seed
            else ""
        )
        return (
            "# Research brief\n\n"
            "You are running one round of a multi-round research effort to improve a code repository against a "
            "fixed evaluator. `TASK.md` says what the change is for, how it is judged and which paths you may "
            "edit. Higher scores are better.\n\n"
            "## Files\n\n"
            "- `repo/` is a git checkout of the repository. Edit it in place: its files are the version you are "
            "working on. Only the editable paths in `TASK.md` may change.\n"
            "- `TASK.md`, `BRIEF.md`, `STATE.md`, `eval.sh`, `checkout.sh`, `frontier/` and `archive/` are "
            "maintained by the engine. Read them; never edit them.\n"
            "- `STATE.md` is rewritten before every round: the leaderboard, the Pareto frontier of versions that "
            "win on different examples, the failure dossier, this round's **directive** and your evaluation "
            "allowance.\n"
            "- Versions are patches against the starting commit. `frontier/<id>.patch` holds the current frontier; "
            "`archive/<id>.patch` holds every version that ever received a full evaluation, with scores in "
            "`archive/index.tsv`.\n"
            "- `./checkout.sh <id>` resets `repo/` to that version, and `./checkout.sh base` to the starting "
            "commit. It throws away unsaved work in `repo/`.\n"
            f"{seed}"
            "- `notebook.md` is yours and is the only memory that survives between rounds. Later rounds, "
            "possibly a different session of you, depend on it.\n\n"
            "## Evaluating\n\n"
            f"- `./eval.sh` is a **full evaluation** of `repo/` as it stands: it costs {full} and is the only way "
            "a version enters the leaderboard.\n"
            f"{probe}"
            "- The evaluator scores the difference between `repo/` and the starting commit. Files the repository "
            "ignores are left out, and commits you make do not matter, only the files. It applies that difference "
            "to a fresh checkout, runs the repository's setup and then the scorer, which you cannot run here. "
            "This sandbox is offline, so local builds and tests may lack dependencies.\n"
            "- A change outside the editable paths is refused before it is scored and costs nothing.\n"
            f"- If `eval.sh` prints `{BUDGET_EXHAUSTED_MARKER}`, the budget is spent: write up the notebook and end "
            "the session.\n"
            "- Never score the same state twice; every call spends budget.\n\n"
            "## Method for a round\n\n"
            "1. **Orient.** Read `STATE.md`, then `notebook.md`. Do not rerun a hypothesis the notebook already "
            "refuted unless you have a new reason, and say what the reason is.\n"
            "2. **Diagnose.** Study the failure dossier and the evaluator feedback, and read the code it points "
            "to. Name concrete failure modes, each tied to the examples that show it.\n"
            "3. **Hypothesize.** Under a `## Round N` heading in `notebook.md`, write two to four falsifiable "
            "hypotheses. For each: the change, why it should help, and which examples it should move.\n"
            "4. **Experiment.** Test one hypothesis per version: `./checkout.sh` a frontier version, make one "
            "focused change in `repo/`, and evaluate it. Write the hypothesis down before you evaluate, and the "
            "result right after.\n"
            "5. **Conclude.** Mark each hypothesis confirmed, refuted or inconclusive, with the evidence. Record "
            "what you learned about the code, not just the number.\n"
            "6. **Consolidate.** Stack the confirmed changes into one version of `repo/` and give it a full "
            "evaluation before the round's allowance runs out.\n\n"
            "## Standards\n\n"
            "- Follow the round's directive in `STATE.md`; it is chosen from the evidence of earlier rounds.\n"
            "- Keep the repository working. The final result is measured afresh, so a change that only helps "
            "because it special-cases an example, or weakens the checks that judge it, is not an improvement.\n"
            "- Between two versions with the same score, the smaller diff is better. A change that deletes code "
            "and keeps the score is a win.\n"
            "- There is no human to ask. End the session once the allowance is spent or the directive is done; "
            "the engine starts the next round with fresh evidence.\n"
        )

    def _brief(self) -> str:
        """Write the standing research method every round follows.

        Returns:
            The ``BRIEF.md`` text.
        """
        probe = (
            "- `./eval.sh <file> <example_id> [<example_id> ...]` is a **probe**: it scores the file on just those "
            "examples and costs one unit per example. Probes are for checking whether a change moves the examples "
            "it targets; they never enter the leaderboard.\n"
            if self.example_ids
            else ""
        )
        full = (
            "one unit per visible example (see `TASK.md`)"
            if self.example_ids
            else "one unit, since the evaluator scores the candidate as a whole"
        )
        return (
            "# Research brief\n\n"
            "You are running one round of a multi-round research effort to improve a text candidate against a "
            "fixed evaluator. `TASK.md` says what the candidate is for and how it is judged. Higher scores are "
            "better.\n\n"
            "## Files\n\n"
            "- `TASK.md`, `BRIEF.md`, `STATE.md`, `eval.sh`, `frontier/` and `archive/` are maintained by the "
            "engine. Read them; never edit them.\n"
            "- `STATE.md` is rewritten before every round: the leaderboard, the Pareto frontier of candidates "
            "that win on different examples, the failure dossier, this round's **directive** and your "
            "evaluation allowance.\n"
            "- `frontier/<id>.txt` holds the current frontier candidates; `archive/<id>.txt` holds every candidate "
            "that ever received a full evaluation, with scores in `archive/index.tsv`.\n"
            "- `work/` is yours: write every new candidate there. `work/seed.txt` is the starting candidate.\n"
            "- `notebook.md` is yours and is the only memory that survives between rounds. Later rounds, "
            "possibly a different session of you, depend on it.\n\n"
            "## Evaluating\n\n"
            f"- `./eval.sh <file>` is a **full evaluation**: it costs {full} and is the only way a candidate "
            "enters the leaderboard.\n"
            f"{probe}"
            f"- If `eval.sh` prints `{BUDGET_EXHAUSTED_MARKER}`, the budget is spent: write up the notebook and end "
            "the session.\n"
            "- Never score the same text twice; every call spends budget.\n\n"
            "## Method for a round\n\n"
            "1. **Orient.** Read `STATE.md`, then `notebook.md`. Do not rerun a hypothesis the notebook already "
            "refuted unless you have a new reason, and say what the reason is.\n"
            "2. **Diagnose.** Study the failure dossier and the evaluator feedback. Name concrete failure modes, "
            "each tied to the examples that show it.\n"
            "3. **Hypothesize.** Under a `## Round N` heading in `notebook.md`, write two to four falsifiable "
            "hypotheses. For each: the change, why it should help, and which examples it should move.\n"
            "4. **Experiment.** Test one hypothesis per candidate: copy a frontier candidate into `work/`, make "
            "one focused change, and evaluate it. Write the hypothesis down before you evaluate, and the result "
            "right after.\n"
            "5. **Conclude.** Mark each hypothesis confirmed, refuted or inconclusive, with the evidence. Record "
            "what you learned about the task, not just the number.\n"
            "6. **Consolidate.** Stack the confirmed changes into one candidate and give it a full evaluation "
            "before the round's allowance runs out.\n\n"
            "## Standards\n\n"
            "- Follow the round's directive in `STATE.md`; it is chosen from the evidence of earlier rounds.\n"
            "- Keep every candidate valid for `TASK.md`. The final result is measured afresh, so a change that only helps "
            "because it names or special-cases a visible example is not an improvement.\n"
            "- Between two candidates with the same score, the shorter and simpler one is better. A change that "
            "deletes text and keeps the score is a win.\n"
            "- There is no human to ask. End the session once the allowance is spent or the directive is done; "
            "the engine starts the next round with fresh evidence.\n"
        )

    def _write_state(self, server: EvalServer, directive: str) -> None:
        """Refresh ``STATE.md`` and ``frontier/`` for the coming round.

        Args:
            server: Evaluation server with the budget state.
            directive: This round's directive.
        """
        table = self._table()
        ranking = self._ranking()
        front = pareto_front(table, ranking)[:_FRONTIER_SIZE]
        frontier_dir = self.work_dir / "frontier"
        for stale_file in frontier_dir.iterdir():
            stale_file.unlink()
        for candidate in front:
            (frontier_dir / f"{self._ids[candidate]}{self.suffix}").write_text(candidate, encoding="utf-8")
        quota = self._round_quota(server)
        remaining = server.budget.remaining
        lines = [f"# Round {self.round}", "", "## Directive", "", self._directive_text(directive, table, front), ""]
        lines += ["## Budget", ""]
        lines.append(
            "- Evaluations: unlimited by count; stop when the directive is done."
            if remaining is None
            else f"- This round may spend {_plural(quota or 0, 'evaluation unit')} of the {remaining} left."
        )
        if self.max_token_cost is not None:
            left = max(0.0, float(self.max_token_cost) - self.cost_usd)
            lines.append(f"- Your own model spend left for the run: ${left:.2f}.")
        lines += ["", "## Leaderboard", ""]
        if table:
            ranked = sorted(table, key=lambda c: -ranking[c])[:_LEADERBOARD_SIZE]
            lines += ["| id | score | on frontier |", "| --- | --- | --- |"]
            lines += [f"| {self._ids[c]} | {ranking[c]:.4f} | {'yes' if c in front else ''} |" for c in ranked]
        else:
            lines.append("Nothing has been fully evaluated yet.")
        columns = self._columns(table)
        if table and columns:
            lines += ["", "## Per-example and named scores on the frontier", ""]
            lines += ["| column | " + " | ".join(self._ids[c] for c in front) + " |"]
            lines += ["| --- " * (len(front) + 1) + "|"]
            for column in columns:
                cells = " | ".join(f"{table[c].get(column, 0.0):.3f}" for c in front)
                lines.append(f"| `{column}` | {cells} |")
        lines += ["", "## Failure dossier", ""]
        lines += self._dossier(table, front)
        (self.work_dir / "STATE.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _directive_text(self, directive: str, table: dict[str, dict[str, float]], front: list[str]) -> str:
        """Spell out what this round should do.

        Args:
            directive: The chosen directive.
            table: Per-example scores of fully evaluated candidates.
            front: Current frontier, leader first.

        Returns:
            Markdown for the directive section.
        """
        leader = self._ids[front[0]] if front else ("the starting commit" if self.repo else "the seed")
        if directive == "survey" and self.repo_seed:
            return (
                "**Survey.** First give the starting version a full evaluation (`./checkout.sh seed`, then "
                "`./eval.sh`) to anchor the baseline. Then map the failure modes across examples and test your most "
                "promising hypotheses."
            )
        if directive == "survey" and self.repo:
            return (
                "**Survey.** First give the untouched checkout a full evaluation (`./checkout.sh base`, then "
                "`./eval.sh`) to anchor the baseline. Then map the failure modes across examples and test your most "
                "promising hypotheses."
            )
        if directive == "survey":
            return (
                "**Survey.** First give `work/seed.txt` a full evaluation as-is to anchor the baseline. Then map the "
                "failure modes across examples and test your most promising hypotheses."
            )
        if directive == "exploit":
            return (
                f"**Exploit.** The last round raised the leader to `{leader}`. Push further along the direction that "
                "worked, and run at least one ablation that removes a recent addition to check it earns its place."
            )
        if directive == "combine":
            partner = self._partner()
            if partner is not None:
                wins = ", ".join(f"`{eid}`" for eid in partner[1][:_DOSSIER_EXAMPLES])
                return (
                    f"**Combine.** `{self._ids[partner[0]]}` beats the leader `{leader}` on {wins}. Find which parts "
                    "of it cause those wins and graft them into the leader without losing the leader's own wins."
                )
        if directive == "pivot":
            angle = (
                "take a different approach in the code" if self.repo else "rewrite the candidate from a different angle"
            )
            return (
                f"**Pivot.** {_PIVOT_AFTER} rounds in a row did not beat `{leader}`: small edits have stalled. Make a "
                f"structural change: {angle}, or start from a frontier member other than the leader. Keep what the "
                "notebook confirmed and drop what it refuted."
            )
        return (
            f"**Explore.** The last round did not beat `{leader}`. Aim new hypotheses at the hard examples in the "
            "failure dossier, which no candidate handles well yet."
        )

    def _dossier(self, table: dict[str, dict[str, float]], front: list[str]) -> list[str]:
        """Describe where the leader fails, with the evaluator's own words.

        Args:
            table: Per-example scores of fully evaluated candidates.
            front: Current frontier, leader first.

        Returns:
            Markdown lines.
        """
        if not front:
            return ["No evidence yet."]
        leader = front[0]
        with self._lock:
            feedback = next((o.feedback for o in reversed(self.observations) if o.full and o.candidate == leader), {})
        columns = self._columns(table)
        lines: list[str] = []
        if not self.example_ids:
            note = feedback.get("_single") or "(no feedback)"
            lines += [f"Leader `{self._ids[leader]}` feedback:", "", "```", note, "```"]
            if not columns:
                return lines
            lines.append("")
        best_anywhere = {column: max(row.get(column, 0.0) for row in table.values()) for column in columns}
        hard = sorted(columns, key=lambda column: best_anywhere[column])[:_DOSSIER_EXAMPLES]
        weakest = sorted(columns, key=lambda column: table[leader].get(column, 0.0))[:_DOSSIER_EXAMPLES]
        lines += [
            "Hardest examples and named scores (best score any candidate reached): "
            + ", ".join(f"`{column}` {best_anywhere[column]:.3f}" for column in hard),
            "",
            f"Leader `{self._ids[leader]}` where it is weakest:",
            "",
        ]
        for eid in weakest:
            note = feedback.get(eid) or "(no feedback)"
            lines += [f"- `{eid}` scored {table[leader].get(eid, 0.0):.3f}: {note}"]
        return lines

    def _session(self, server: EvalServer, directive: str, *, max_budget_usd: float | None) -> ProposerOutcome:
        """Run one round's agent session under the stall and allowance watchdogs.

        Args:
            server: Evaluation server whose usage counter proves progress.
            directive: This round's directive.
            max_budget_usd: Remaining proposer spend.

        Returns:
            The session outcome.
        """
        start_used = server.budget.used
        quota = self._round_quota(server)
        last_used = start_used
        last_progress = time.monotonic()
        spent_at: float | None = None

        def should_kill() -> bool:
            """End a session that stalled or overran its allowance by more than the write-up grace."""
            nonlocal last_used, last_progress, spent_at
            now = time.monotonic()
            if server.budget.used != last_used:
                last_used = server.budget.used
                last_progress = now
            if quota is not None and server.budget.used - start_used >= quota:
                spent_at = spent_at if spent_at is not None else now
                if now - spent_at >= _WRITE_UP_GRACE_SECONDS:
                    return True
            return self.max_no_eval_seconds is not None and now - last_progress >= self.max_no_eval_seconds

        prompt = (
            f"This is round {self.round} ({directive}). Read `BRIEF.md`, then `STATE.md`, then `notebook.md` in this "
            "directory, and carry out the round as the brief describes. Record everything in `notebook.md`."
        )
        return run_proposer(
            prompt,
            work_dir=self.work_dir,
            log_dir=self.run_dir / "sessions",
            name=f"round{self.round}",
            model=self.model,
            session_id=str(uuid.uuid4()),
            max_budget_usd=max_budget_usd,
            should_kill=should_kill,
        )

    def _done(self, server: EvalServer, outcome: ProposerOutcome, evals_before: int, stale: int) -> bool:
        """Decide whether another round is worth starting.

        Args:
            server: Evaluation server with the budget state.
            outcome: The round that just ended.
            evals_before: Evaluations used before that round started.
            stale: Consecutive rounds without a better leader.

        Returns:
            ``True`` when budget, threshold, a wedged agent or a spent pivot ends the run.
        """
        remaining = server.budget.remaining
        return (
            outcome.budget_exhausted
            or (remaining is not None and remaining <= 0)
            or _reached(self.stop_at_score, self._leader_score())
            or server.budget.used == evals_before
            or stale >= _STOP_AFTER
        )

    def _result(self, task: Task, server: EvalServer) -> Result:
        """Build the result from server evidence only.

        Args:
            task: Task being optimized.
            server: Evaluation server with the run's evidence.

        Returns:
            The verified best candidate.

        Raises:
            RuntimeError: When nothing was ever scored.
        """
        best = self.incumbent(server)
        if best is None:
            raise RuntimeError("The research agent finished without scoring any candidate through eval.sh.")
        candidate, score = best
        table = self._table()
        ranking = self._ranking()
        return Result(
            best_candidate=candidate,
            best_score=score,
            total_evals=server.budget.used,
            eval_log=list(server.eval_log),
            metadata={
                "engine": self.name,
                "engine_version": AUTORESEARCH_VERSION,
                "rounds": self.round,
                "directives": list(self.directives),
                "session_ids": list(self.session_ids),
                "proposer_cost_usd": round(self.cost_usd, 6),
                "candidates_evaluated": len(table),
                "frontier": [{"id": self._ids[c], "score": round(ranking[c], 6)} for c in pareto_front(table, ranking)],
                "work_dir": str(self.work_dir),
                "seed_len": len(seed_as_text(task.seed_candidate)),
                **({"repository": True} if self.repo else {}),
            },
        )


class MetaHarnessEngine:
    """stanford-iris-lab/meta-harness ``run_evolve`` at the pinned revision, driven against the evaluation server.

    Phase 0 benchmarks the seed; every iteration then shows the frontier, asks the
    proposer for a fixed number of candidates through the upstream skill, validates
    and benchmarks each one on every visible example, recomputes the frontier and
    appends the upstream ``evolution_summary.jsonl`` rows.
    """

    name = "meta_harness"

    def __init__(self, config: OptimizeAnythingConfig) -> None:
        """Pop the engine knobs from the shared config.

        Args:
            config: Cross-engine run configuration.
        """
        engine_config = dict(config.engine_config)
        self.model = str(engine_config.pop("model"))
        # Upstream CLI default: ``--iterations 20``.
        self.max_iterations = int(engine_config.pop("max_iterations", 20))
        self.candidates_per_iter = max(1, int(engine_config.pop("max_candidates_per_iter", 3)))
        self.max_token_cost = config.max_token_cost
        self.stop_at_score = config.stop_at_score
        # ``checkout``, ``base``, ``editable_paths``, ``readonly_paths``,
        # ``constrained`` (a constraints file governs the run) and ``tools``
        # (the folder holding ``repo_tree.py``) for a repository run.
        self.repo: dict[str, Any] | None = engine_config.pop("repo", None)
        self.suffix = ".patch" if self.repo else ".txt"
        self.run_dir = Path(config.run_dir or "meta-harness-run").resolve()
        self.work_dir = self.run_dir / "meta_harness"
        self.logs_dir = self.work_dir / "logs" / "run"
        self.session_ids: list[str] = []
        self.cost_usd = 0.0
        self.example_ids: list[str] = []
        self.results: dict[str, dict[str, Any]] = {}

    def run(self, task: Task, server: EvalServer) -> Result:
        """Run the baseline and the evolution iterations.

        Args:
            task: Task with a text seed candidate.
            server: Evaluation server that benchmarks candidates.

        Returns:
            The frontier's best candidate, verified by the server.
        """
        self.example_ids = visible_example_ids(server)
        self._layout(task)
        iterations = 0
        stop_reason = "max_iterations"
        try:
            self._benchmark(server, "seed", seed_as_text(task.seed_candidate))
            self._write_frontier()
            for iteration in range(1, self.max_iterations + 1):
                best = self._best(scored=True)
                if _reached(self.stop_at_score, None if best is None else best[1]):
                    stop_reason = "stop_at_score"
                    break
                remaining = _remaining_cost(self.max_token_cost, self.cost_usd)
                if remaining is not None and remaining <= 0:
                    stop_reason = "proposer_budget"
                    break
                if server.budget.remaining is not None and server.budget.remaining < max(1, len(self.example_ids)):
                    stop_reason = "eval_budget"
                    break
                iterations = iteration
                self._iteration(server, iteration, remaining)
        except BudgetExhausted:
            stop_reason = "eval_budget"
        return self._result(server, iterations, stop_reason)

    def incumbent(self, server: EvalServer) -> tuple[str, float] | None:
        """Return the frontier's best candidate text and score.

        Args:
            server: Unused; the frontier is the engine's own verified record.

        Returns:
            ``(candidate, score)`` or ``None`` before the baseline finished.
        """
        del server
        return self._leader()

    def process_result(self, result: Result, output_dir: str | Path) -> None:
        """Write the final candidate and run metadata beside the evaluator artifacts.

        Args:
            result: Result returned by ``run``.
            output_dir: Evaluator output directory.
        """
        target = Path(output_dir) / self.name
        target.mkdir(parents=True, exist_ok=True)
        (target / f"best_candidate{self.suffix}").write_text(result.best_candidate, encoding="utf-8")
        (target / "metadata.json").write_text(json.dumps(result.metadata, indent=2, default=str), encoding="utf-8")

    def _layout(self, task: Task) -> None:
        """Create the upstream workspace: ``agents/``, the run logs and the skill.

        Args:
            task: Task providing the seed and description.
        """
        if self.work_dir.exists():
            shutil.rmtree(self.work_dir)
        for sub in ("agents", "logs/run/reports", "logs/run/claude_sessions", "logs/run/results"):
            (self.work_dir / sub).mkdir(parents=True)
        seed = seed_as_text(task.seed_candidate) if task.seed_candidate is not None else ""
        (self.work_dir / "agents" / f"seed{self.suffix}").write_text(seed, encoding="utf-8")
        brief = task_brief(task, self.example_ids)
        if self.repo:
            brief += repo_rules(self.repo)
            (self.work_dir / "repo").symlink_to(Path(self.repo["checkout"]), target_is_directory=True)
            rules = {
                "editable_paths": self.repo["editable_paths"],
                "readonly_paths": self.repo["readonly_paths"],
                "constrained": bool(self.repo.get("constrained")),
            }
            (self.work_dir / ".repo-rules.json").write_text(json.dumps(rules), encoding="utf-8")
            scripts = {
                "save.sh": MH_SAVE_SCRIPT.format(
                    base=self.repo["base"], tools=self.repo["tools"], python=sys.executable, blocks=MH_RULE_BLOCKS_DIR
                ),
                "checkout.sh": MH_CHECKOUT_SCRIPT.format(base=self.repo["base"]),
            }
            for name, text in scripts.items():
                (self.work_dir / name).write_text(text, encoding="utf-8")
                (self.work_dir / name).chmod(0o755)
            subprocess.run(["./checkout.sh", "seed"], cwd=self.work_dir, check=True, capture_output=True)
        (self.work_dir / "task.md").write_text(brief, encoding="utf-8")
        skill_dir = self.work_dir / ".claude" / "skills" / "meta-harness"
        skill_dir.mkdir(parents=True)
        skill_dir.joinpath("SKILL.md").write_text(self.skill(), encoding="utf-8")
        (self.logs_dir / "evolution_summary.jsonl").write_text("", encoding="utf-8")

    def skill(self) -> str:
        """Derive the sandbox skill from the pinned upstream ``SKILL.md``.

        Returns:
            The adapted skill text.
        """
        n = self.candidates_per_iter
        names = ", ".join(f"<name{i}>" for i in range(1, n + 1))
        edits: list[tuple[str, ...]] = [
            (
                "description: Run one iteration of memory system evolution. Called by meta_harness.py or "
                "interactively via /meta-harness.",
                "description: Run one iteration of candidate evolution. Called by the Meta-Harness driver.",
            ),
            ("# Meta-Harness (Memory System Evolution)", "# Meta-Harness (Candidate Evolution)"),
            ("Run ONE iteration of memory system evolution.", "Run ONE iteration of candidate evolution."),
            (
                "You analyze results + prediction traces, prototype changes, and implement new systems. The outer "
                "loop (`meta_harness.py`) handles benchmarking separately.",
                "You analyze results + evaluation traces, prototype changes, and write new candidates. The outer "
                "loop (the Meta-Harness driver) handles benchmarking separately.",
            ),
            (
                "- You MUST implement 3 new memory systems every iteration.",
                f"- You MUST write {_plural(n, 'new candidate')} every iteration.",
            ),
            (
                "- Design exactly 3 candidates per iteration: mix of exploitation and exploration.",
                f"- Design exactly {_plural(n, 'candidate')} per iteration: "
                + ("mix of exploitation and exploration." if n > 1 else "exploitation or exploration."),
            ),
            (
                "The most common failure mode is creating systems that are just parameter variants of existing "
                "ones. Check `evolution_summary.jsonl` for what's been tried — parameter sweeps (pool sizes, "
                "retrieval counts, context budgets, similarity metrics) almost always regress or tie.",
                "The most common failure mode is creating candidates that are just cosmetic variants of existing "
                "ones. Check `evolution_summary.jsonl` for what's been tried — cosmetic sweeps (synonyms, "
                "reordering, small number tweaks) almost always regress or tie.",
            ),
            (
                "- A new retrieval algorithm",
                "- A new memory structure (e.g. separate fast/slow pools, hierarchical organization, compressed "
                "representations)",
                "- A new structure (e.g. organize around the task's decision points instead of a linear list)\n"
                "- A new strategy (e.g. an explicit procedure, worked examples, self-checks, a different framing)\n"
                "- A new failure defense (e.g. address the error classes the traces show, not the ones you assume)\n"
                "- A new representation (e.g. compressed rules instead of prose, or the reverse)",
            ),
            (
                "**Bad candidates just tune numbers.** If the logic in `predict()` and `learn_from_batch()` is "
                "identical to the base except for constants, it's a parameter variant.",
                "**Bad candidates just tweak wording.** If the candidate is identical to the base except for a few "
                "constants or synonyms, it's a cosmetic variant.",
            ),
            (
                "**Combining systems is valid.** Take the retrieval strategy from system A and the memory format "
                "from system B,",
                "**Combining candidates is valid.** Take the structure from candidate A and the strategy from "
                "candidate B,",
            ),
            (
                "Exploitation axes: A=Prompt template, B=Memory content, C=Selection algorithm, D=Memory sizing, "
                "E=Learning trigger, F=LLM usage in learning.",
                "Exploitation axes: A=Structure, B=Content, C=Strategy, D=Length, E=Framing, F=Failure handling.",
            ),
            (
                "- **No dataset-specific hints.** Do not hardcode knowledge about specific datasets. Memory systems "
                "must be general-purpose.\n- **Never mention dataset names** in system code, prompts, or comments.",
                "- **No example-specific hints.** Do not hardcode answers to specific evaluation examples. "
                "Candidates must generalize to unseen cases.\n- **Never copy example ids or expected outputs** "
                "into a candidate.",
            ),
            ("which datasets improved/regressed and why", "which examples improved/regressed and why"),
            (
                "   - `frontier_val.json` — current best per dataset (val accuracy)\n"
                "   - `config.yaml` for current datasets and baselines\n"
                "   - recent `logs/<dataset>/<agent>/<model>/log.jsonl` traces if they exist",
                "   - `frontier_val.json` — current best per example (score)\n"
                "   - `task.md` for the objective, background and the evaluation examples\n"
                "   - recent `results/<candidate>/val.json` traces if they exist",
            ),
            (
                "2. Formulate 3 hypotheses",
                f"2. Formulate {_plural(n, 'hypothesis').replace('hypothesiss', 'hypotheses')}",
            ),
            (
                "1. Write a test script in `/tmp/` that exercises the core retrieval/learning logic in isolation.\n"
                "2. Pull real examples from `logs/<dataset>/<memory>/<model>/log.jsonl` to test against.",
                "1. Draft the change in a scratch file under `/tmp/` and reason it through against concrete cases.\n"
                "2. Pull real cases and evaluator feedback from `results/<candidate>/val.json` to test against.",
            ),
            ("4. Delete scripts when done.", "4. Delete scratch files when done."),
            ("For each of the 3 candidates:", f"For each of the {_plural(n, 'candidate')}:"),
            (
                "1. Copy a top-performing base system to `agents/<name>.py`, then make targeted modifications. This "
                "copy-then-edit approach ensures correct imports and proven patterns.",
                "1. Run `./checkout.sh <base>` to reset `repo/` to a top-performing candidate, then make targeted "
                "changes to the code in `repo/`. This checkout-then-edit approach keeps proven patterns."
                if self.repo
                else "1. Copy a top-performing base candidate to `agents/<name>.txt`, then make targeted modifications. "
                "This copy-then-edit approach keeps proven patterns.",
            ),
            (
                "After implementing, re-read the file and check: does this system introduce a genuinely NEW "
                "mechanism, or is it just a parameter variant? If the logic in `predict()` and "
                "`learn_from_batch()` is identical to the base except for numbers, REWRITE with a truly novel "
                "mechanism.\n"
                "4. Validate: `uv run python -c \"from text_classification.agents.<name> import *; print('OK')\"`\n\n"
                "Do not edit `config.yaml` just to register candidates. The benchmark auto-discovers files in "
                "`agents/`.",
                (
                    "After implementing, re-read your diff and check: does this candidate introduce a genuinely NEW "
                    "mechanism, or is it just a parameter variant? If the code is identical to the base except for "
                    "constants, REWRITE with a truly novel mechanism.\n"
                    "4. Save: `./save.sh <name>` writes `repo/`'s whole change against the starting commit to "
                    "`agents/<name>.patch` and checks it only touches files you may change; it must print `saved`. "
                    "Then move on to the next candidate with `./checkout.sh`.\n\n"
                    "Do not edit `task.md` or write patch files by hand. The benchmark applies exactly the patches "
                    "listed in `pending_eval.json` to a fresh checkout of the starting commit."
                )
                if self.repo
                else "After writing, re-read the file and check: does this candidate introduce a genuinely NEW "
                "mechanism, or is it just a cosmetic variant? If it is identical to the base except for a few "
                "words or numbers, REWRITE with a truly novel mechanism.\n"
                "4. Validate: `test -s agents/<name>.txt && echo OK` — the file must exist and be non-empty; its "
                "whole content is what gets evaluated.\n\n"
                "Do not edit `task.md`. The benchmark reads exactly the files listed in `pending_eval.json`.",
            ),
            ('"file": "agents/<name>.py",', f'"file": "agents/<name>{self.suffix}",'),
            ("Output: `CANDIDATES: <name1>, <name2>, <name3>`", f"Output: `CANDIDATES: {names}`"),
            (
                "## MemorySystem Interface",
                "- Test results: `results/<dataset>/<memory>/<model>/test.json` (separate dir, never exposed during "
                "evolution)",
                (
                    "## Candidate Format\n\n"
                    "- `repo/` is a git checkout of the repository under optimization; edit its code in place.\n"
                    "- Each candidate is a patch `agents/<name>.patch` made by `./save.sh <name>`: `repo/`'s change "
                    "against the starting commit. The evaluator applies it to a fresh checkout, runs the "
                    "repository's setup and scorer, which you cannot run here.\n"
                    "- `./checkout.sh <name>` resets `repo/` to a saved candidate, and `./checkout.sh base` to the "
                    "starting commit. It throws away unsaved work in `repo/`.\n"
                    "- `task.md` says what the repository is for, how it is judged and which paths you may change.\n"
                    "- `agents/seed.patch` is the starting candidate the run began from.\n\n"
                    if self.repo
                    else "## Candidate Format\n\n"
                    "- Each candidate is a plain text file under `agents/`; its whole content is submitted to the "
                    "evaluator.\n"
                    "- `task.md` says what the candidate is for and how it is judged.\n"
                    "- `agents/seed.txt` is the starting candidate the run began from.\n\n"
                )
                + "## Directory Structure\n\n"
                "- Val results: `results/<candidate>/val.json` (`avg_val` plus per-example `scores` and evaluator "
                "`infos`)\n"
                "- Frontier: `frontier_val.json` (`_pareto` ranks candidates by average score; every other key "
                "is an example or a named score `score:<name>` with the candidate that leads it)\n"
                "- Named scores: `results/<candidate>/val.json` `named_scores` maps each to its score and "
                "feedback; a candidate that leads one is worth building on",
            ),
            (
                '{"iteration": 1, "system": "example_system", "avg_val": 45.0, "axis": "exploitation", '
                '"hypothesis": "...", "delta": +2.1, "outcome": "45.0% (+2.1)", "components": ["tag1", "tag2", '
                '"tag3"]}',
                '{"iteration": 1, "system": "example_candidate", "avg_val": 0.45, "axis": "exploitation", '
                '"hypothesis": "...", "delta": 0.021, "outcome": "0.4500 (+0.0210)", "components": ["tag1", '
                '"tag2", "tag3"]}',
            ),
            (
                "Treat `evolution_summary.jsonl`, `frontier_val.json`, and recent training traces as the only "
                "shipped history sources in this trimmed repo.",
                "Treat `evolution_summary.jsonl`, `frontier_val.json`, and `results/<candidate>/val.json` as the "
                "only history sources in this workspace.",
            ),
        ]
        return adapt(load_asset("meta_harness/SKILL.md"), edits)

    def _task_prompt(self, iteration: int) -> str:
        """Render the per-iteration prompt with upstream's wording and this run's paths.

        Args:
            iteration: One-based iteration number.

        Returns:
            The prompt text.
        """
        logs = self.logs_dir.relative_to(self.work_dir)
        count = len(self.example_ids) or 1
        return (
            f"Run iteration {iteration} of the evolution loop. There are {_plural(count, 'evaluation example')}.\n\n"
            "## Run directories\n"
            f"All logs and results for this run are under `{logs}/`.\n"
            f"- `{logs / 'evolution_summary.jsonl'}` — past results\n"
            f"- `{logs / 'frontier_val.json'}` — frontier\n"
            f"- `{logs / 'reports'}/` — post-eval reports\n"
            f"- `{logs / 'results'}/<candidate>/val.json` — per-example scores and evaluator feedback\n"
            f"- Write pending_eval.json to: `{logs / 'pending_eval.json'}`\n\n"
            + (
                "Each candidate is a change to the repository checked out in `repo/`: edit it in place and save "
                "each candidate with `./save.sh <name>`.\n\n"
                if self.repo
                else ""
            )
            + "Follow the meta-harness skill in `.claude/skills/meta-harness/SKILL.md`."
        )

    def _iteration(self, server: EvalServer, iteration: int, remaining_cost: float | None) -> None:
        """Run one propose-validate-benchmark iteration.

        Args:
            server: Evaluation server that benchmarks candidates.
            iteration: One-based iteration number.
            remaining_cost: Proposer spend still allowed, or ``None``.

        Raises:
            ProposerFailedError: When the iteration's CLI session failed on its own.
        """
        best = self._leader()
        log_event(
            "iteration.start",
            f"Iteration {iteration} started, frontier {best[1]:.4f}"
            if best
            else f"Iteration {iteration} started, no frontier yet",
            iteration=iteration,
            frontier=best[1] if best else None,
        )
        pending = self.logs_dir / "pending_eval.json"
        pending.unlink(missing_ok=True)
        started = time.monotonic()
        outcome = run_proposer(
            self._task_prompt(iteration),
            work_dir=self.work_dir,
            log_dir=self.logs_dir / "claude_sessions",
            name=f"iter{iteration}",
            model=self.model,
            session_id=str(uuid.uuid4()),
            max_budget_usd=remaining_cost,
            tools=META_HARNESS_TOOLS,
            append_system_prompt=self.skill(),
        )
        propose_seconds = time.monotonic() - started
        self.cost_usd += outcome.cost_usd
        self.session_ids.append(outcome.session_id)
        self._log_save_blocks()
        outcome.raise_if_failed(f"iter{iteration}")
        candidates = self._read_pending(pending)
        if not candidates:
            log_event(
                "iteration.end", f"Iteration {iteration} produced no candidates", iteration=iteration, candidates=0
            )
            return
        previous_best = best[1] if best else None
        rows: list[dict[str, Any]] = []
        bench_started = time.monotonic()
        for spec in candidates:
            name = str(spec.get("name") or "")
            failure = self._validate(spec)
            row: dict[str, Any] = {
                "iteration": iteration,
                "system": name,
                "axis": spec.get("axis"),
                "hypothesis": spec.get("hypothesis"),
            }
            if failure:
                row.update({"avg_val": None, "delta": None, "outcome": f"failed: {failure}"})
                rows.append(row)
                continue
            text = (self.work_dir / str(spec["file"])).read_text(encoding="utf-8")
            avg = self._benchmark(server, name, text)
            delta = None if previous_best is None else round(avg - previous_best, 4)
            row.update(
                {
                    "avg_val": avg,
                    "delta": delta,
                    "outcome": f"{avg:.4f}" + ("" if delta is None else f" ({delta:+.4f})"),
                }
            )
            if "components" in spec:
                row["components"] = spec["components"]
            rows.append(row)
        rows[0]["timing_s"] = {
            "propose": round(propose_seconds, 1),
            "bench": round(time.monotonic() - bench_started, 1),
            "wall": round(time.monotonic() - started, 1),
        }
        with (self.logs_dir / "evolution_summary.jsonl").open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")
        self._write_frontier()
        new_best = self._leader()
        improved = bool(new_best and (previous_best is None or new_best[1] > previous_best))
        log_event(
            "iteration.end",
            f"Iteration {iteration} new best {new_best[1]:.4f}"
            if improved and new_best
            else f"Iteration {iteration} brought no improvement",
            iteration=iteration,
            candidates=len(rows),
            improved=improved,
            best=new_best[1] if new_best else None,
        )

    def _log_save_blocks(self) -> None:
        """Log each diff ``save.sh`` refused under the constraints file, then drop it.

        The diffs are checked again here, so a file the agent wrote into the
        folder becomes an event only when it truly breaks the path rules.
        """
        blocks = self.work_dir / MH_RULE_BLOCKS_DIR
        if not self.repo or not blocks.is_dir():
            return
        for path in sorted(blocks.glob("*.patch")):
            with contextlib.suppress(OSError, UnicodeDecodeError):
                blocked_patch(self.repo, path.read_text(encoding="utf-8"))
            path.unlink(missing_ok=True)

    def _read_pending(self, pending: Path) -> list[dict[str, Any]]:
        """Read the proposer's ``pending_eval.json`` candidate list.

        Args:
            pending: Path the prompt told the proposer to write.

        Returns:
            At most ``candidates_per_iter`` candidate specs; empty when absent or malformed.
        """
        try:
            document = json.loads(pending.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        candidates = document.get("candidates") if isinstance(document, dict) else None
        if not isinstance(candidates, list):
            return []
        return [spec for spec in candidates if isinstance(spec, dict)][: self.candidates_per_iter]

    def _validate(self, spec: dict[str, Any]) -> str | None:
        """Check a candidate spec the way upstream import-checks a new system.

        Args:
            spec: One ``pending_eval.json`` candidate entry.

        Returns:
            A failure reason, or ``None`` when the candidate can be benchmarked.
        """
        name = spec.get("name")
        if not isinstance(name, str) or not _SAFE_NAME.match(name):
            return "invalid candidate name"
        if name in self.results:
            return "duplicate candidate name"
        file = spec.get("file")
        if not isinstance(file, str):
            return "missing file"
        path = (self.work_dir / file).resolve()
        agents = (self.work_dir / "agents").resolve()
        if not path.is_relative_to(agents) or not path.is_file():
            return "candidate file must exist under agents/"
        if self.repo and path.suffix != self.suffix:
            return "candidate file must be a patch saved by save.sh"
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return "candidate file is not UTF-8 text"
        if not text.strip():
            return "candidate file is empty"
        if self.repo:
            if len(text.encode("utf-8")) > repo_tree.MAX_PATCH_BYTES:
                return "patch is larger than a version may be"
            refusal = blocked_patch(self.repo, text)
            if refusal:
                return refusal
            problems = repo_tree.patch_violations(text, self.repo["editable_paths"], self.repo["readonly_paths"])
            if problems:
                return "; ".join(problems)
        return None

    def _benchmark(self, server: EvalServer, name: str, candidate: str) -> float:
        """Score one candidate on every visible example and record the trace.

        Args:
            server: Evaluation server.
            name: Candidate name used for the results directory.
            candidate: Candidate text.

        Returns:
            The average score.
        """
        ids = self.example_ids or None
        avg, info = server.evaluate_examples(candidate, example_ids=ids)
        avg = float(avg)
        server.log_progress(avg, candidate=candidate)
        infos = info.get("infos", {})
        named, named_feedback = named_score_columns(list(infos.values()) if isinstance(infos, dict) else [info])
        record = {
            "system": name,
            "avg_val": avg,
            "scores": info.get("scores", {}),
            "infos": infos,
            "named_scores": {
                column.removeprefix(NAMED_PREFIX): {"score": value, "feedback": named_feedback[column]}
                for column, value in named.items()
            },
        }
        self.results[name] = {
            "avg_val": avg,
            "scores": {**record["scores"], **named},
            "candidate": candidate,
            "crashed": bool(info.get(UNSCORED_CASES_KEY)),
        }
        target = self.logs_dir / "results" / name
        target.mkdir(parents=True, exist_ok=True)
        (target / "val.json").write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
        return avg

    def _write_frontier(self) -> None:
        """Write ``frontier_val.json``: per-example leaders plus the ``_pareto`` ranking.

        A version with a crashed case holds filled scores (SKY-367), so it neither
        ranks nor leads a case. Its own ``val.json`` still records it.
        """
        ranked = sorted(
            ((name, entry) for name, entry in self.results.items() if not entry.get("crashed")),
            key=lambda item: -item[1]["avg_val"],
        )
        frontier: dict[str, Any] = {
            "_pareto": [{"system": name, "val_accuracy": entry["avg_val"]} for name, entry in ranked]
        }
        named = [
            column for entry in self.results.values() for column in entry["scores"] if column.startswith(NAMED_PREFIX)
        ]
        for eid in [*(self.example_ids or ["_single"]), *dict.fromkeys(named)]:
            leader = max(
                ((name, entry["scores"].get(eid)) for name, entry in ranked if entry["scores"].get(eid) is not None),
                key=lambda item: item[1],
                default=None,
            )
            if leader is not None:
                frontier[eid] = {"system": leader[0], "val_accuracy": leader[1]}
        (self.logs_dir / "frontier_val.json").write_text(json.dumps(frontier, indent=2), encoding="utf-8")

    def _best(self, scored: bool = False) -> tuple[str, float] | None:
        """Return the frontier leader's candidate text and average score.

        Args:
            scored: Skip versions with a crashed case. Their average holds filled scores (SKY-367),
                so it must not stop the run.

        Returns:
            ``(candidate, score)`` or ``None`` when nothing has been benchmarked.
        """
        names = [key for key, entry in self.results.items() if not (scored and entry.get("crashed"))]
        if not names:
            return None
        name = max(names, key=lambda key: self.results[key]["avg_val"])
        return self.results[name]["candidate"], float(self.results[name]["avg_val"])

    def _leader(self) -> tuple[str, float] | None:
        """Return the best fully scored version, else the seed, else the best filled average.

        A version with a crashed case has a filled average (SKY-367), so it leads
        only when no fully scored version and no seed exist.

        Returns:
            ``(candidate, score)`` or ``None`` when nothing has been benchmarked.
        """
        if (best := self._best(scored=True)) is not None:
            return best
        if "seed" in self.results:
            return self.results["seed"]["candidate"], float(self.results["seed"]["avg_val"])
        return self._best()

    def _result(self, server: EvalServer, iterations: int, stop_reason: str) -> Result:
        """Build the result from the frontier.

        Args:
            server: Evaluation server with the run's evidence.
            iterations: Iterations that ran.
            stop_reason: Why the loop ended.

        Returns:
            The frontier's best candidate.

        Raises:
            RuntimeError: When even the baseline could not be scored.
        """
        best = self._leader()
        if best is None:
            raise RuntimeError("Meta-Harness could not benchmark the seed candidate.")
        candidate, score = best
        return Result(
            best_candidate=candidate,
            best_score=score,
            total_evals=server.budget.used,
            eval_log=list(server.eval_log),
            metadata={
                "engine": self.name,
                "upstream_revision": META_HARNESS_REVISION,
                "session_ids": list(self.session_ids),
                "iterations": iterations,
                "stop_reason": stop_reason,
                "proposer_cost_usd": round(self.cost_usd, 6),
                "candidates_evaluated": len(self.results),
                "work_dir": str(self.work_dir),
            },
        )


class BestValsetVersion:
    """GEPA callback that keeps the best version scored on every validation case.

    When the evaluation budget runs out mid-round, ``optimize_anything`` raises
    instead of returning its state, and plain per-case calls never reach the
    server's aggregate log, so this is the only record of what GEPA had found.
    """

    def __init__(self) -> None:
        """Start with nothing recorded."""
        self.best: tuple[str, float] | None = None

    def on_valset_evaluated(self, event: dict[str, Any]) -> None:
        """Keep ``event``'s version when it covers the whole validation set and beats the best so far.

        Args:
            event: GEPA's ``ValsetEvaluatedEvent``.
        """
        if event["num_examples_evaluated"] < event["total_valset_size"]:
            return
        score = float(event["average_score"])
        # SKY-367: a version with no scored case averages to negative infinity; it is never best.
        if not math.isfinite(score):
            return
        if self.best is None or score > self.best[1]:
            self.best = (next(iter(event["candidate"].values()), ""), score)


class GepaRepoEngine:
    """GEPA's reflective search over repository versions, with a coding agent as its proposer.

    GEPA keeps the Pareto front and picks which version to improve and on which
    cases. Each proposal is one agent session in a checkout of that version,
    briefed with the cases' scores and scorer feedback; the new version is the
    checkout's diff against the starting commit.
    """

    name = "gepa_repo"

    def __init__(self, config: OptimizeAnythingConfig) -> None:
        """Pop the engine knobs from the shared config.

        Args:
            config: Cross-engine run configuration; ``engine_config`` must hold ``repo``.

        Raises:
            ValueError: When no repository checkout was configured.
        """
        engine_config = dict(config.engine_config)
        self.repo: dict[str, Any] | None = engine_config.pop("repo", None)
        if self.repo is None:
            raise ValueError("GEPA's agent proposer needs a repository checkout.")
        self.proposer = AgentProposer(
            model=str(engine_config.pop("model")),
            checkout=Path(self.repo["checkout"]),
            editable_paths=list(self.repo["editable_paths"]),
            readonly_paths=list(self.repo["readonly_paths"]),
            log_dir=Path(config.run_dir or "gepa-repo-run").resolve() / "sessions",
            max_token_cost=config.max_token_cost,
        )
        self.max_token_cost = config.max_token_cost
        self.stop_at_score = config.stop_at_score
        self.run_dir = Path(config.run_dir or "gepa-repo-run").resolve()

    def run(self, task: Task, server: EvalServer) -> Result:
        """Run GEPA until its evaluation or proposer budget runs out.

        Args:
            task: Task whose seed is the starting patch (empty for the commit as fetched).
            server: Evaluation server that scores each version through the parent.

        Returns:
            GEPA's validation-best version, or the best fully scored version
            when the budget stopped GEPA mid-round.

        Raises:
            RuntimeError: When nothing was scored at all.
            ProposerFailedError: When the proposer CLI failed on its own.
        """
        self.proposer.objective = task.objective
        self.proposer.background = task.background
        gepa_dir = self.run_dir / "gepa"
        gepa_dir.mkdir(parents=True, exist_ok=True)
        tracker = BestValsetVersion()
        crashed: set[str] = set()

        def _key(candidate: Any) -> str:
            """Key a version the same way for the batch and the stopper.

            Args:
                candidate: The text GEPA scores, or its program candidate mapping.

            Returns:
                The version's sorted JSON form.
            """
            return json.dumps(unwrap_text_candidate(candidate), sort_keys=True, default=str)

        config = GEPAConfig(
            engine=EngineConfig(
                run_dir=str(gepa_dir),
                max_metric_calls=server.budget.remaining,
                max_reflection_cost=self.max_token_cost,
                # Every version is applied to the one shared checkout.
                parallel=False,
                display_progress_bar=False,
            ),
            reflection=ReflectionConfig(reflection_lm=None, custom_candidate_proposer=self.proposer),
            stop_callbacks=[
                lambda _state: self.proposer.failure is not None,
                *(
                    [ScoredThresholdStopper(self.stop_at_score, lambda candidate: _key(candidate) in crashed)]
                    if self.stop_at_score is not None
                    else []
                ),
            ],
            callbacks=[tracker],
        )

        def evaluate(candidate: Any, example: Any = None) -> tuple[float, Any]:
            """Score through the server, named scores reshaped for GEPA's objective frontier."""
            score, info = server.evaluate(candidate, example)
            return score, gepa_named_scores(info)

        def batch_evaluate(pairs: list[tuple[Any, Any]]) -> list[tuple[float, Any]]:
            """Score one GEPA batch in order and fill each null case with its version's scored mean.

            Args:
                pairs: The ``(candidate, case)`` pairs of one GEPA batch.

            Returns:
                One ``(score, side_info)`` per pair, in order.
            """
            results = [evaluate(candidate, example) for candidate, example in pairs]
            keys = [_key(candidate) for candidate, _ in pairs]
            crashed.update(key for key, (score, _) in zip(keys, results, strict=True) if not math.isfinite(score))
            filled = fill_unscored(keys, [score for score, _ in results])
            return [(score, info) for score, (_, info) in zip(filled, results, strict=True)]

        kwargs: dict[str, Any] = {
            "seed_candidate": seed_as_text(task.seed_candidate) if task.seed_candidate is not None else "",
            "evaluator": evaluate,
            "batch_evaluator": batch_evaluate,
            "config": config,
        }
        # Every case both drives reflection and ranks versions.
        if task.train_set:
            kwargs["dataset"] = task.train_set
            kwargs["valset"] = task.train_set
        if task.objective:
            kwargs["objective"] = task.objective
        if task.background:
            kwargs["background"] = task.background
        try:
            gepa_result = optimize_anything(**kwargs)
        except BudgetExhausted:
            gepa_result = None
        if self.proposer.failure is not None:
            raise self.proposer.failure
        metadata: dict[str, Any] = {
            "proposals": self.proposer.proposals,
            "proposer_cost_usd": self.proposer.total_cost,
            "session_ids": list(self.proposer.session_ids),
        }
        if gepa_result is not None:
            best: Any = gepa_result.best_candidate
            if isinstance(best, dict):
                best = next(iter(best.values()), "")
            metadata["candidates"] = len(gepa_result.candidates)
            return Result(
                best_candidate=best,
                best_score=float(gepa_result.val_aggregate_scores[gepa_result.best_idx]),
                total_evals=server.budget.used,
                metadata=metadata,
            )
        fallback = tracker.best or best_aggregate_candidate(server)
        if fallback is None:
            raise RuntimeError("GEPA stopped before any repository version was fully scored.")
        return Result(
            best_candidate=fallback[0], best_score=fallback[1], total_evals=server.budget.used, metadata=metadata
        )

    def process_result(self, result: Result, output_dir: str | Path) -> None:
        """Write the best patch and run metadata beside the evaluator artifacts.

        Args:
            result: Result returned by ``run``.
            output_dir: Evaluator output directory.
        """
        target = Path(output_dir) / self.name
        target.mkdir(parents=True, exist_ok=True)
        (target / "best_candidate.patch").write_text(result.best_candidate, encoding="utf-8")
        (target / "metadata.json").write_text(json.dumps(result.metadata, indent=2, default=str), encoding="utf-8")


class AgentProposer:
    """GEPA ``custom_candidate_proposer`` that writes each new version with a coding agent.

    GEPA's reflection-cost stopper reads ``total_cost`` off this object, so the
    agent sessions' reported spend caps the run exactly as a reflection model's would.
    """

    def __init__(
        self,
        *,
        model: str,
        checkout: Path,
        editable_paths: list[str],
        readonly_paths: list[str],
        log_dir: Path,
        max_token_cost: float | None,
    ) -> None:
        """Record where the agent works and what it may spend.

        Args:
            model: Model identifier for the ``claude`` command.
            checkout: Git checkout the agent edits, made by ``repo_tree.unpack_tree``.
            editable_paths: Repository paths a version may change.
            readonly_paths: Submodule and Git LFS paths that stay as fetched.
            log_dir: Where each session's output lands.
            max_token_cost: Proposer spend cap in dollars, or ``None``.
        """
        self.model = model
        self.checkout = checkout
        self.editable_paths = editable_paths
        self.readonly_paths = readonly_paths
        self.log_dir = log_dir
        self.max_token_cost = max_token_cost
        self.objective = ""
        self.background = ""
        self.total_cost = 0.0
        self.proposals = 0
        self.session_ids: list[str] = []
        self.failure: ProposerFailedError | None = None

    def __call__(
        self,
        candidate: dict[str, str],
        reflective_dataset: dict[str, list[dict[str, Any]]],
        components_to_update: list[str],
    ) -> dict[str, str]:
        """Check out ``candidate``, let the agent improve it, and return the new patch.

        Args:
            candidate: The parent version, keyed by GEPA's single text component.
            reflective_dataset: Per-component records of the cases GEPA reflects on.
            components_to_update: The component GEPA asks to change.

        Returns:
            The new version under the same component key; the parent itself when
            the agent's diff is too large to ship.

        Raises:
            ProposerFailedError: When this or an earlier CLI session failed on its own.
        """
        # GEPA can ask again within the same iteration before its stop check runs.
        if self.failure is not None:
            raise self.failure
        key = components_to_update[0] if components_to_update else next(iter(candidate))
        parent = candidate[key]
        repo_tree.reset_tree(self.checkout)
        repo_tree.apply_patch(self.checkout, parent)
        self.proposals += 1
        outcome = run_proposer(
            self.prompt(reflective_dataset.get(key) or []),
            work_dir=self.checkout,
            log_dir=self.log_dir,
            name=f"proposal{self.proposals}",
            model=self.model,
            session_id=str(uuid.uuid4()),
            max_budget_usd=_remaining_cost(self.max_token_cost, self.total_cost),
        )
        self.total_cost += outcome.cost_usd
        self.session_ids.append(outcome.session_id)
        try:
            outcome.raise_if_failed(f"proposal{self.proposals}")
        except ProposerFailedError as error:
            # GEPA logs a proposer exception and keeps iterating, so the engine
            # reads this back to stop the run and fail it.
            self.failure = error
            raise
        patch = repo_tree.version_patch(self.checkout)
        repo_tree.reset_tree(self.checkout)
        if len(patch.encode("utf-8")) > repo_tree.MAX_PATCH_BYTES:
            return {key: parent}
        return {key: patch}

    def prompt(self, records: list[dict[str, Any]]) -> str:
        """Brief one proposal session: the goal, the rules and how this version scored.

        Args:
            records: GEPA's reflective records for the cases this proposal targets.

        Returns:
            The session prompt.
        """
        editable = ", ".join("the whole repository" if p == "." else f"`{p}`" for p in self.editable_paths)
        lines = [
            "You are improving a code repository. The current directory is a checkout of the version to improve.",
            "",
            f"Goal: {self.objective or 'raise the score the evaluator gives this repository.'}",
        ]
        if self.background:
            lines += ["", "Background:", self.background]
        lines += ["", f"You may change: {editable}. Leave everything else as it is."]
        if self.readonly_paths:
            lines.append("Submodules and Git LFS files stay as fetched: " + ", ".join(self.readonly_paths) + ".")
        lines += ["", "How this version scored, with the scorer's feedback and each named score's own feedback:"]
        for index, record in enumerate(records[:_DOSSIER_EXAMPLES], start=1):
            lines.append(f"{index}. " + record_text(record))
        if not records:
            lines.append("No feedback was recorded.")
        lines += [
            "",
            "Make one focused change that should raise the score, and leave it in the working tree. You cannot run "
            "the evaluator: Skynet scores whatever you leave in this checkout once you finish. Do not commit, and "
            "reply with one short paragraph describing the change.",
        ]
        return "\n".join(lines)


class BestOfNRepoEngine:
    """Upstream Best-of-N's sample-and-keep-best loop over repository versions.

    Each sample is one coding-agent session in a fresh checkout of the starting
    version, briefed the same way every time and never shown earlier samples,
    exactly as upstream draws independent samples from one fixed prompt. Each
    sample is scored on the cases upstream scores (training, else validation)
    through the parent, and the best full score wins.
    """

    name = "best_of_n_repo"

    def __init__(self, config: OptimizeAnythingConfig) -> None:
        """Pop the engine knobs from the shared config.

        Args:
            config: Cross-engine run configuration; ``engine_config`` must hold ``repo``.

        Raises:
            ValueError: When no repository checkout was configured.
        """
        engine_config = dict(config.engine_config)
        self.repo: dict[str, Any] | None = engine_config.pop("repo", None)
        if self.repo is None:
            raise ValueError("Best-of-N's agent sampler needs a repository checkout.")
        self.run_dir = Path(config.run_dir or "best-of-n-repo-run").resolve()
        self.proposer = AgentProposer(
            model=str(engine_config.pop("model")),
            checkout=Path(self.repo["checkout"]),
            editable_paths=list(self.repo["editable_paths"]),
            readonly_paths=list(self.repo["readonly_paths"]),
            log_dir=self.run_dir / "sessions",
            max_token_cost=config.max_token_cost,
        )
        self.max_token_cost = config.max_token_cost
        self.stop_at_score = config.stop_at_score
        self.samples: list[dict[str, Any]] = []
        self.best: tuple[str, float] | None = None

    def run(self, task: Task, server: EvalServer) -> Result:
        """Draw and score samples until a budget or the target score stops the loop.

        Args:
            task: Task whose seed is the starting patch (empty for the commit as fetched).
            server: Evaluation server that scores each version through the parent.

        Returns:
            The best fully scored sample.

        Raises:
            RuntimeError: When no sample was fully scored.
        """
        self.proposer.objective = task.objective
        self.proposer.background = task.background
        seed = seed_as_text(task.seed_candidate) if task.seed_candidate is not None else ""
        split = "train" if task.train_set else ("val" if task.val_set else None)
        cases = len(task.train_set or task.val_set or []) or 1
        while True:
            if self.max_token_cost is not None and self.proposer.total_cost >= self.max_token_cost:
                break
            # A sample the budget cannot fully score would waste an agent session.
            if server.budget.remaining is not None and server.budget.remaining < cases:
                break
            patch = self.proposer({"candidate": seed}, {}, ["candidate"])
            sample = {"sample": len(self.samples) + 1, "proposer_cost_usd": round(self.proposer.total_cost, 6)}
            self.samples.append(sample)
            try:
                if split is None:
                    score, info = server.evaluate(patch["candidate"])
                else:
                    score, info = server.evaluate_examples(patch["candidate"], split=split)
            except BudgetExhausted:
                # Upstream discards a sample the budget cut short and keeps the best complete one.
                sample["outcome"] = "budget_exhausted"
                break
            score = float(score)
            if not math.isfinite(score) or info.get(UNSCORED_CASES_KEY):
                # SKY-367: a sample with a crashed case has no score, so it is
                # never best and cannot reach stop_at_score on its filled mean.
                sample["outcome"] = "metric_error"
                continue
            server.log_progress(score, candidate=patch["candidate"])
            sample["score"] = score
            if self.best is None or score > self.best[1]:
                self.best = (patch["candidate"], score)
            if _reached(self.stop_at_score, self.best[1]):
                break
        if self.best is None:
            raise RuntimeError("Best-of-N stopped before any repository version was fully scored.")
        return Result(
            best_candidate=self.best[0],
            best_score=self.best[1],
            total_evals=server.budget.used,
            eval_log=list(server.eval_log),
            metadata={
                "engine": self.name,
                "n_samples": len(self.samples),
                "samples": list(self.samples),
                "proposer_cost_usd": round(self.proposer.total_cost, 6),
                "session_ids": list(self.proposer.session_ids),
                "repository": True,
            },
        )

    def incumbent(self, server: EvalServer) -> tuple[str, float] | None:
        """Return the best fully scored sample so far.

        Args:
            server: Unused; samples are the engine's own verified record.

        Returns:
            ``(patch, score)`` or ``None`` before any sample was scored.
        """
        del server
        return self.best

    def process_result(self, result: Result, output_dir: str | Path) -> None:
        """Write the best patch and run metadata beside the evaluator artifacts.

        Args:
            result: Result returned by ``run``.
            output_dir: Evaluator output directory.
        """
        target = Path(output_dir) / self.name
        target.mkdir(parents=True, exist_ok=True)
        (target / "best_candidate.patch").write_text(result.best_candidate, encoding="utf-8")
        (target / "metadata.json").write_text(json.dumps(result.metadata, indent=2, default=str), encoding="utf-8")


ENGINES: dict[str, type] = {
    AutoResearchEngine.name: AutoResearchEngine,
    MetaHarnessEngine.name: MetaHarnessEngine,
    GepaRepoEngine.name: GepaRepoEngine,
    BestOfNRepoEngine.name: BestOfNRepoEngine,
}


def check_assets() -> dict[str, str]:
    """Verify the vendored Meta-Harness prompt adapts cleanly at the pinned revision.

    Returns:
        The engine name mapped to its pinned upstream revision.
    """
    probe = OptimizeAnythingConfig(engine="meta_harness", engine_config={"model": "probe"})
    MetaHarnessEngine(probe).skill()
    return {"meta_harness": META_HARNESS_REVISION}
