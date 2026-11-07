"""Execute pinned upstream agent engines with a filesystem evaluator mailbox.

This standalone module is copied into the selected runtime. It deliberately
imports no Skynet application code; the parent owns the scorer and its cases.
"""

from __future__ import annotations

import base64
import contextlib
import importlib.util
import io
import json
import logging
import math
import os
import signal
import subprocess
import sys
import tarfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from gepa.oa.budget import BudgetTracker
from gepa.oa.config import OptimizeAnythingConfig
from gepa.oa.eval_server import EvalServer
from gepa.oa.task import Task

try:
    from . import harness_bridge, native_engines, repo_tree
except ImportError:  # In the sandbox this file runs as a script beside its sibling modules.
    _sibling_modules = {}
    for _sibling in ("harness_bridge", "repo_tree", "native_engines"):
        _spec = importlib.util.spec_from_file_location(_sibling, Path(__file__).with_name(f"{_sibling}.py"))
        assert _spec is not None
        assert _spec.loader is not None
        _sibling_modules[_sibling] = importlib.util.module_from_spec(_spec)
        sys.modules[_sibling] = _sibling_modules[_sibling]
        _spec.loader.exec_module(_sibling_modules[_sibling])
    harness_bridge = _sibling_modules["harness_bridge"]
    native_engines = _sibling_modules["native_engines"]
    repo_tree = _sibling_modules["repo_tree"]

_RPC_PREFIX = "APP_NATIVE_RPC "
_PROGRESS_PREFIX = "APP_NATIVE_PROGRESS "
_LOG_PREFIX = "APP_NATIVE_LOG "
# Loggers that narrate every HTTP call at INFO; their warnings still stream.
_QUIET_LOGGERS = ("httpx", "httpcore", "urllib3", "LiteLLM", "litellm", "openai", "anthropic")
_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
_TOKEN_NAMES = ("prompt_tokens", "completion_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
_PROC_ROOT = Path("/proc")


class EvaluationStopped(BaseException):
    """Unwind infrastructure failures past upstream's ordinary bad-candidate handling."""


class BudgetStopped(EvaluationStopped):
    """Stop admission without converting an unperformed evaluation into feedback."""


class EvaluatorMailbox:
    """Send evaluation requests to the parent and await matching response files."""

    def __init__(self, nonce: str, timeout_seconds: float) -> None:
        """Create a process-scoped evaluator transport.

        Args:
            nonce: Parent-generated request framing token.
            timeout_seconds: Maximum time any evaluation can wait for a response.
        """
        self.nonce = nonce
        self.timeout_seconds = timeout_seconds
        self.error: EvaluationStopped | None = None
        self.stopped = threading.Event()
        # Candidate and case of the evaluation this thread is making, so the
        # parent can tag what the scorer logs while scoring it.
        self.labels = threading.local()
        self._write_lock = threading.Lock()

    def evaluate(self, candidate: str, example: Any = None, **kwargs: Any) -> tuple[float, dict[str, Any]]:
        """Evaluate only through the parent-owned budget and scorer.

        Args:
            candidate: Upstream text candidate.
            example: Visible training or validation case.
            **kwargs: Additional upstream evaluation context.

        Returns:
            Parent score (negative infinity for a metric error) and feedback.

        Raises:
            EvaluationStopped: When the parent rejects evaluation or does not reply.
        """
        del kwargs
        if self.stopped.is_set():
            raise EvaluationStopped("The parent evaluator has stopped this run.")
        request_id = uuid.uuid4().hex
        request = {
            "id": request_id,
            "candidate": candidate,
            "example": example,
            "candidate_id": getattr(self.labels, "candidate_id", None),
            "case": getattr(self.labels, "case", None),
        }
        self.emit(_RPC_PREFIX, request)
        response_path = Path("rpc") / f"{request_id}.json"
        deadline = time.monotonic() + self.timeout_seconds
        while not self.stopped.is_set() and time.monotonic() < deadline:
            if response_path.exists():
                try:
                    response = json.loads(response_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    # Both runtime filesystems can expose a new file before its
                    # contents have finished landing.
                    time.sleep(0.05)
                    continue
                if "error" in response:
                    kind = BudgetStopped if response.get("stop_reason") == "budget_reached" else EvaluationStopped
                    self.error = kind(str(response["error"]))
                    self.stopped.set()
                    raise self.error
                score = response.get("score")
                # SKY-367: the parent sends null for a metric error. The pinned
                # upstream engines need a number, and negative infinity is
                # their own "no score" value; a sweep fills it with its
                # scored mean in ``ProgressEvalServer.evaluate_examples``.
                return (float("-inf") if score is None else float(score)), dict(response.get("info") or {})
            time.sleep(0.05)
        if self.stopped.is_set():
            # The supervisor stopped the run and records why; claiming a reply
            # timeout here would mask that reason.
            raise self.error or EvaluationStopped("The parent evaluator has stopped this run.")
        self.error = self.error or EvaluationStopped("Native evaluator response timed out.")
        self.stopped.set()
        raise self.error

    def emit(self, prefix: str, payload: dict[str, Any]) -> None:
        """Write one framed event atomically across upstream evaluator threads.

        Args:
            prefix: Request or progress event family.
            payload: Event fields to serialize.
        """
        with self._write_lock:
            print(f"{prefix}{self.nonce} {json.dumps(payload, default=str, allow_nan=False)}", flush=True)


class RunLogHandler(logging.Handler):
    """Stream every log record to the parent the moment it is emitted."""

    def __init__(self, mailbox: EvaluatorMailbox) -> None:
        """Bind the handler to the parent connection.

        Args:
            mailbox: Parent evaluation and progress connection.
        """
        super().__init__(logging.INFO)
        self.mailbox = mailbox

    def emit(self, record: logging.LogRecord) -> None:
        """Send one record, with its run-log fields, as a framed log line.

        Args:
            record: Any record at INFO or above in this process.
        """
        try:
            data = getattr(record, native_engines.RUN_LOG_ATTR, None)
            line = {
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
                **(data if isinstance(data, dict) else {}),
            }
            try:
                self.mailbox.emit(_LOG_PREFIX, line)
            except ValueError:
                # A non-finite number in the fields; the line itself still matters.
                self.mailbox.emit(_LOG_PREFIX, {**line, "fields": None})
        except Exception:
            self.handleError(record)


def install_run_log(mailbox: EvaluatorMailbox) -> None:
    """Route this process's log records to the parent's run log.

    Args:
        mailbox: Parent evaluation and progress connection.
    """
    root = logging.getLogger()
    root.addHandler(RunLogHandler(mailbox))
    root.setLevel(logging.INFO)
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


class ProgressEvalServer(EvalServer):
    """Forward aggregate checkpoints, and the case scores behind them, as upstream records them."""

    def __init__(self, task: Task, mailbox: EvaluatorMailbox, config: OptimizeAnythingConfig, output_dir: Path) -> None:
        """Bind the upstream evaluator and its additive progress transport.

        Args:
            task: Upstream task carrying the job's cases, if any.
            mailbox: Parent evaluation and progress connection.
            config: Native engine evaluation budget and concurrency.
            output_dir: Persisted upstream evaluation artifacts.
        """
        self.mailbox = mailbox
        self._sweeps: dict[str, dict[str, Any]] = {}
        self._sweep_lock = threading.Lock()
        super().__init__(
            task,
            mailbox.evaluate,
            BudgetTracker(max_evals=config.max_evals),
            max_concurrency=config.max_concurrency,
            output_dir=output_dir,
        )
        # Cases are named by position, as the other engines name them, so the
        # run view lines a native version's cases up with the rest of the run.
        self._case_ids = {id(example): str(index) for index, example in enumerate(self._examples.values())}

    def _sweep_targets(self, example_ids: list[str] | None, split: str | None) -> list[str]:
        """Mirror the case selection upstream ``evaluate_examples`` makes.

        Args:
            example_ids: Explicit case ids, if the caller named them.
            split: Split name otherwise, ``"all"`` for train and val.

        Returns:
            The ids of the cases upstream will score.
        """
        if example_ids is not None:
            return [eid for eid in example_ids if eid in self._examples]
        if split is not None:
            splits = ("train", "val") if split == "all" else (split,)
            return [eid for name in splits for eid in self._split_ids.get(name, [])]
        return list(self._split_ids["train"])

    def evaluate_examples(
        self, candidate: str, example_ids: list[str] | None = None, split: str | None = None
    ) -> tuple[float, dict[str, Any]]:
        """Score a candidate on cases, streaming the sweep when it covers every visible case.

        Upstream logs a checkpoint only for a sweep of every visible case, so
        only such a sweep streams case by case; a partial probe would otherwise
        leave a version half-scored in the run view for good.

        Args:
            candidate: Candidate to score.
            example_ids: Explicit case ids, if the caller named them.
            split: Split name otherwise.

        Returns:
            The upstream aggregate and per-case details, with each null case
            filled by :func:`_fill_unscored`.
        """
        targets = self._sweep_targets(example_ids, split)
        if set(targets) == set(self._agent_visible_ids()):
            candidate_id = self._register_candidate(candidate)
            native_engines.log_event(
                "candidate.eval.start",
                f"Evaluating candidate {candidate_id} on {len(targets)} cases",
                candidate=candidate_id,
                cases=len(targets),
            )
            with self._sweep_lock:
                self._sweeps[_candidate_key(candidate)] = {
                    "candidate_id": candidate_id,
                    "total": len(targets),
                    "scores": [],
                }
        return _fill_unscored(*super().evaluate_examples(candidate, example_ids, split))

    def evaluate(self, candidate: str, example: Any | None = None, **kwargs: Any) -> tuple[float, dict[str, Any]]:
        """Score one case, announcing it when it belongs to a streaming sweep.

        Args:
            candidate: Candidate to score.
            example: Case to score it on, or ``None`` in single-task mode.
            **kwargs: Extra evaluator arguments upstream forwards.

        Returns:
            The unchanged upstream score and side information.
        """
        with self._lock:
            self.mailbox.labels.candidate_id = self._candidate_registry.get(_candidate_key(candidate))
        self.mailbox.labels.case = None if example is None else self._case_ids.get(id(example))
        score, info = super().evaluate(candidate, example, **kwargs)
        with self._sweep_lock:
            sweep = self._sweeps.get(_candidate_key(candidate))
        if sweep is None or example is None or not isinstance(score, float | int) or not math.isfinite(score):
            return score, info
        example_id = self._case_ids.get(id(example), "?")
        with self._sweep_lock:
            sweep["scores"].append((example_id, float(score)))
        self.mailbox.emit(
            _PROGRESS_PREFIX,
            {
                "event": "case_scored",
                "candidate_id": sweep["candidate_id"],
                "example_id": example_id,
                "score": float(score),
                "total": sweep["total"],
            },
        )
        return score, info

    def log_progress(
        self, val_score: float, candidate: str | None = None, reflection_cost: float = 0.0
    ) -> dict[str, Any]:
        """Forward an upstream aggregate, with its sweep's case scores, without constructing ancestry.

        Args:
            val_score: Aggregate score computed by the unchanged upstream engine.
            candidate: Candidate measured at this checkpoint.
            reflection_cost: Upstream-reported cumulative proposer dollars.

        Returns:
            The unchanged upstream checkpoint result.
        """
        result = super().log_progress(val_score, candidate, reflection_cost)
        if isinstance(candidate, str) and math.isfinite(val_score):
            with self._sweep_lock:
                sweep = self._sweeps.pop(_candidate_key(candidate), None)
            self.mailbox.emit(
                _PROGRESS_PREFIX,
                {
                    "candidate_id": self._register_candidate(candidate),
                    "candidate": candidate,
                    "score": val_score,
                    "total_evals": self.budget.used,
                    "per_example": [] if sweep is None else list(sweep["scores"]),
                },
            )
        return result


def _fill_unscored(average: float, info: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    """Give each null case of one sweep the sweep's mean over its scored cases.

    The mailbox turns a parent null into negative infinity, and the parent's
    JSON never carries a non-finite score, so a non-finite case is a metric
    error. The engines rank versions by this aggregate, so a case that always
    crashes must not pull every version to negative infinity (SKY-367).

    Args:
        average: Upstream's mean over the sweep's cases.
        info: Upstream's per-case details, with ``scores`` keyed by case id.

    Returns:
        The filled cases' mean and the details with each null case filled,
        or both unchanged when the sweep scored no case.
    """
    scores = info.get("scores")
    if not isinstance(scores, dict) or not scores:
        return average, info
    filled = native_engines.fill_unscored([""] * len(scores), list(scores.values()))
    if not all(math.isfinite(score) for score in filled):
        return average, info
    return sum(filled) / len(filled), {**info, "scores": dict(zip(scores, filled, strict=True))}


def _candidate_key(candidate: Any) -> str:
    """Key a candidate the way upstream's registry does.

    Args:
        candidate: Candidate text, or a legacy mapping.

    Returns:
        The text itself, or its sorted JSON form.
    """
    return candidate if isinstance(candidate, str) else json.dumps(candidate, sort_keys=True)


def _number(value: Any) -> int:
    """Return a nonnegative integral token count.

    Args:
        value: Native CLI usage value.

    Returns:
        Its count or zero when the field is absent or invalid.
    """
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _add_usage(destination: dict[str, dict[str, int]], model: str, counts: dict[str, Any], *, camel: bool) -> None:
    """Accumulate one CLI summary or one completed assistant message.

    Args:
        destination: Per-model cumulative usage.
        model: Model identifier reported by the proposer session.
        counts: Native token usage fields.
        camel: Whether fields use the CLI final-summary camelCase spelling.
    """
    mapping = (
        ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens")
        if camel
        else ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    )
    current = destination.setdefault(model, dict.fromkeys(_TOKEN_NAMES, 0))
    for name, native_name in zip(_TOKEN_NAMES, mapping, strict=True):
        current[name] += _number(counts.get(native_name))
    current["total_tokens"] = sum(current[name] for name in _TOKEN_NAMES)


def collect_usage(paths: list[Path], model: str) -> dict[str, dict[str, int]]:
    """Recover usage without counting mirrored session transcripts twice.

    Args:
        paths: Upstream artifact and Claude transcript directories.
        model: Configured model, used when a native record omits its model id.

    Returns:
        Per-model usage from final CLI summaries or deduplicated message ids,
        with ``calls`` counting one model request per distinct assistant message.
    """
    totals: dict[str, dict[str, int]] = {}
    complete_sessions: set[str] = set()
    summaries: dict[str, dict[str, Any]] = {}
    summary_turns: dict[str, int] = {}
    messages: dict[tuple[str, str], dict[str, Any]] = {}
    for root in paths:
        if not root.exists():
            continue
        for path in root.rglob("*.json"):
            if not path.name.endswith("_stdout.json") or path.is_symlink():
                continue
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            if isinstance(document, dict) and isinstance(document.get("modelUsage"), dict):
                session = str(document.get("session_id") or path.name)
                summaries[session] = document["modelUsage"]
                summary_turns[session] = _number(document.get("num_turns"))
        for path in root.rglob("*.jsonl"):
            if path.is_symlink():
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for index, line in enumerate(lines):
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict) or entry.get("type") != "assistant":
                    continue
                message = entry.get("message")
                if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
                    continue
                session = str(entry.get("sessionId") or path.stem)
                message_id = str(message.get("id") or entry.get("uuid") or index)
                messages[(session, message_id)] = message
    for session, usage in summaries.items():
        if not usage:
            continue
        complete_sessions.add(session)
        for native_model, counts in usage.items():
            if isinstance(counts, dict):
                _add_usage(totals, str(native_model), counts, camel=True)
    for (session, _message_id), message in messages.items():
        message_model = str(message.get("model") or model)
        if session not in complete_sessions:
            _add_usage(totals, message_model, message["usage"], camel=False)
        current = totals.setdefault(message_model, {**dict.fromkeys(_TOKEN_NAMES, 0), "total_tokens": 0})
        current["calls"] = current.get("calls", 0) + 1
    sessions_with_messages = {session for session, _message_id in messages}
    # A summary without its transcript still reports how many turns it took.
    for session in complete_sessions - sessions_with_messages:
        first_model = next((str(name) for name, counts in summaries[session].items() if isinstance(counts, dict)), None)
        if first_model is not None and summary_turns.get(session):
            current = totals[first_model]
            current["calls"] = current.get("calls", 0) + summary_turns[session]
    return totals


def _descendants() -> list[int]:
    """Find only processes descended from this isolated runner.

    Returns:
        Child process ids, including separately grouped proposer sessions.
    """
    parents: dict[int, int] = {}
    if _PROC_ROOT.is_dir():
        for process in _PROC_ROOT.iterdir():
            if not process.name.isdigit():
                continue
            try:
                lines = (process / "status").read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError):
                continue
            for line in lines:
                if line.startswith("PPid:") and line.split()[-1].isdigit():
                    parents[int(process.name)] = int(line.split()[-1])
                    break
    else:
        completed = subprocess.run(["ps", "-eo", "pid=,ppid="], capture_output=True, text=True, check=False)
        for line in completed.stdout.splitlines():
            columns = line.split()
            if len(columns) == 2 and all(value.isdigit() for value in columns):
                parents[int(columns[0])] = int(columns[1])
    descendants = {os.getpid()}
    while True:
        expanded = descendants | {pid for pid, parent in parents.items() if parent in descendants}
        if expanded == descendants:
            return sorted(descendants - {os.getpid()}, reverse=True)
        descendants = expanded


def _stop_children() -> None:
    """Stop native proposer processes after a parent evaluator failure."""
    for pid in _descendants():
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


def _archive_artifacts(paths: list[tuple[str, Path]]) -> None:
    """Preserve regular upstream files within a bounded artifact archive.

    Args:
        paths: Artifact prefixes and corresponding process-local directories.

    Raises:
        RuntimeError: When raw artifacts exceed the transfer limit.
    """
    buffer = io.BytesIO()
    total = 0
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for prefix, root in paths:
            if not root.exists():
                continue
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.is_symlink():
                    continue
                total += path.stat().st_size
                if total > _MAX_ARTIFACT_BYTES:
                    raise RuntimeError("Native artifacts exceed the 64 MiB transfer limit.")
                archive.add(path, arcname=str(Path(prefix) / path.relative_to(root)), recursive=False)
    Path("native_artifacts.tar.gz.b64").write_text(base64.b64encode(buffer.getvalue()).decode("ascii"))


def _json_finite(value: Any) -> Any:
    """Replace unsupported nonfinite scores while preserving upstream metadata.

    Args:
        value: Result field or nested metadata value.

    Returns:
        A JSON-safe value with nonfinite floats represented by ``None``.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(key): _json_finite(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_finite(item) for item in value]
    return value


def _has_evaluated_single_result(server: EvalServer, output_dir: Path, candidate: Any, score: float) -> bool:
    """Verify that one completed upstream evaluation measured the returned pair.

    Args:
        server: Completed upstream evaluation history.
        output_dir: Upstream artifacts containing full evaluated candidates.
        candidate: Candidate selected by the unchanged engine.
        score: Finite single-task score selected by that engine.

    Returns:
        Whether any completed evaluation supports both the candidate and score.
    """
    for index, entry in enumerate(server.eval_log):
        if entry.get("score") != score:
            continue
        try:
            record = json.loads((output_dir / "evals" / f"{index}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and record.get("candidate") == candidate and record.get("score") == score:
            return True
    return False


def _budget_incumbent(engine: Any, task: Task, server: EvalServer, output_dir: Path) -> dict[str, Any]:
    """Retain an incumbent published by the upstream engine before interruption.

    Args:
        engine: Interrupted upstream instance.
        task: Unchanged task and visible evaluation scope.
        server: Completed evaluation evidence.
        output_dir: Full upstream evaluator artifacts.

    Returns:
        An evaluated candidate envelope, or an empty mapping when none was published.
    """
    selected = engine.incumbent(server)
    if selected is None:
        return {}
    candidate, score = selected
    if not isinstance(score, int | float) or not math.isfinite(score):
        return {}
    if task.has_dataset:
        candidate_id = server._candidate_registry.get(candidate)
        supported = candidate_id is not None and any(
            entry.get("candidate_id") == candidate_id and entry.get("val_score") == score
            for entry in server.progress_log
        )
    else:
        supported = _has_evaluated_single_result(server, output_dir, candidate, score)
    if not supported:
        return {}
    return {
        "best_candidate": candidate,
        "best_score": score,
        "total_evals": server.budget.used,
        "metadata": {"selection_source": "upstream_published_incumbent"},
    }


def _engine_knobs(engine_id: str, proposer: dict[str, Any]) -> dict[str, Any]:
    """Translate the request's proposer settings into the upstream engine config.

    Args:
        engine_id: Upstream engine being configured.
        proposer: Proposer block of the parent payload.

    Returns:
        Only the keys the named engine's config dataclass accepts, minus unset ones.
    """
    if engine_id == "meta_harness":
        names: tuple[str, ...] = ("max_candidates_per_iter",)
    elif engine_id in ("gepa_repo", "best_of_n_repo"):
        names = ()
    else:
        names = ("ralph", "max_no_eval_seconds")
    return {name: proposer[name] for name in names if proposer.get(name) is not None}


def _install_proposer(proposer: dict[str, Any]) -> None:
    """Put the configured harness behind the ``claude`` command the upstream engines run.

    A block without a ``run_command`` (only engine knobs) leaves ``claude`` as
    whatever is on ``PATH``, which only tests provide.

    Args:
        proposer: Proposer block of the parent payload.
    """
    if not proposer.get("run_command"):
        return
    config_file = Path("proposer.json").resolve()
    config_file.write_text(json.dumps(proposer), encoding="utf-8")
    os.environ[harness_bridge.CONFIG_ENV] = str(config_file)
    shim_dir = harness_bridge.install_shim(Path.home(), Path(harness_bridge.__file__).resolve(), sys.executable)
    os.environ["PATH"] = f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}"


def execute(payload: dict[str, Any]) -> dict[str, Any]:
    """Run the exact upstream engine and capture its result and histories.

    Args:
        payload: Parent configuration, task and evaluator framing token.

    Returns:
        Result envelope, or a failure envelope retaining available usage.
    """
    if payload.get("engine_id") not in native_engines.ENGINES:
        raise ValueError("Unsupported native optimizer.")
    if payload.get("task", {}).get("test_set") is not None:
        raise ValueError("Held-out examples must not enter the native optimizer.")
    task = Task(**payload["task"])
    proposer = payload.get("proposer") or {}
    _install_proposer(proposer)
    config_values: dict[str, Any] = {"model": payload["model"], **_engine_knobs(payload["engine_id"], proposer)}
    repo = payload.get("repo")
    if repo is not None:
        checkout = repo_tree.unpack_tree((Path(chunk) for chunk in repo["chunks"]), Path("repo-checkout").resolve())
        repo_tree.exclude_paths(checkout, harness_bridge.workspace_files(proposer))
        config_values["repo"] = {
            "checkout": str(checkout),
            "base": repo_tree.BASE_REF,
            "editable_paths": list(repo["editable_paths"]),
            "readonly_paths": list(repo["readonly_paths"]),
            "constrained": bool(repo.get("constrained")),
            "tools": str(Path(__file__).resolve().parent),
        }
    if payload["engine_id"] == "meta_harness" and payload.get("max_iterations") is not None:
        config_values["max_iterations"] = payload["max_iterations"]
    output_dir = Path("upstream-artifacts").resolve()
    config = OptimizeAnythingConfig(
        engine=payload["engine_id"],
        max_token_cost=payload["max_token_cost"],
        max_evals=payload.get("max_evals"),
        max_concurrency=payload.get("max_concurrency", 1),
        run_dir=str(Path("engine-work").resolve()),
        output_dir=output_dir,
        stop_at_score=payload.get("stop_at_score"),
        sandbox=bool(payload["sandbox"]),
        engine_config=config_values,
    )
    mailbox = EvaluatorMailbox(payload["nonce"], float(payload["timeout_seconds"]))
    install_run_log(mailbox)
    server = ProgressEvalServer(task, mailbox, config, output_dir)
    engine = native_engines.ENGINES[payload["engine_id"]](config)
    document: dict[str, Any] = {}
    finished = threading.Event()

    def optimize() -> None:
        """Run upstream on a supervised thread so evaluator failures cannot spawn retries."""
        native_engines.log_event(
            "phase.start",
            f"Starting {payload['engine_id']} with up to {config.max_evals} evaluations",
            phase="optimize",
            engine=payload["engine_id"],
            max_evals=config.max_evals,
        )
        try:
            result = engine.run(task, server)
            engine.process_result(result, output_dir)
            document.update(
                {
                    "best_candidate": result.best_candidate,
                    "best_score": result.best_score,
                    "total_evals": server.budget.used,
                    "metadata": result.metadata,
                }
            )
            native_engines.log_event(
                "phase.end",
                f"{payload['engine_id']} finished, best score {result.best_score}",
                phase="optimize",
                best_score=result.best_score,
                total_evals=server.budget.used,
            )
        except (Exception, EvaluationStopped) as exc:
            document["error"] = f"{type(exc).__name__}: {exc}"
            native_engines.log_event(
                "phase.end",
                f"{payload['engine_id']} stopped: {document['error']}",
                level=logging.WARNING,
                phase="optimize",
                error=document["error"],
            )
        finally:
            finished.set()

    server.start()
    worker = threading.Thread(target=optimize, daemon=True)
    worker.start()
    timeout = float(payload["timeout_seconds"])
    deadline = time.monotonic() + max(0.1, timeout - min(10.0, timeout * 0.1))
    try:
        while not finished.wait(0.05):
            if mailbox.stopped.is_set() or time.monotonic() >= deadline:
                document["error"] = str(mailbox.error or "Native optimizer exceeded its runtime limit.")
                mailbox.stopped.set()
                _stop_children()
                worker.join(timeout=1.0)
                break
        if mailbox.error is not None:
            document["error"] = str(mailbox.error)
    finally:
        server.stop()
    if isinstance(mailbox.error, BudgetStopped):
        document.pop("error", None)
        document.pop("best_candidate", None)
        document.pop("best_score", None)
        document["stop_reason"] = "budget_reached"
        document.update(_budget_incumbent(engine, task, server, output_dir))
    if document.get("error"):
        document["interrupted_incumbent"] = _budget_incumbent(engine, task, server, output_dir)
    best_score = document.get("best_score")
    if (
        not document.get("error")
        and not task.has_dataset
        and isinstance(best_score, int | float)
        and math.isfinite(best_score)
        and not _has_evaluated_single_result(server, output_dir, document.get("best_candidate"), best_score)
    ):
        document["error"] = (
            "Upstream result fidelity check failed: the selected candidate and score were not evaluated together."
        )
    paths = [("upstream", output_dir), ("work", Path(config.run_dir)), ("sessions", Path.home() / ".claude/projects")]
    document["usage_by_model"] = collect_usage([path for _, path in paths], payload["model"])
    metadata = document.get("metadata", {})
    had_session = bool(metadata.get("session_id") or metadata.get("session_ids"))
    document["usage_complete"] = bool(document["usage_by_model"]) or (not had_session and not document.get("error"))
    try:
        _archive_artifacts(paths)
    except Exception as exc:
        document["error"] = f"Could not preserve native artifacts: {exc}"
    return _json_finite(document)


def main() -> int:
    """Run one native engine invocation from its JSON input file.

    Returns:
        Zero for a completed run, one for a persisted failure envelope.
    """
    try:
        payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        result = execute(payload)
    except Exception as exc:
        result = {"error": f"{type(exc).__name__}: {exc}", "usage_by_model": {}, "usage_complete": False}
    Path("native_result.json").write_text(json.dumps(result, default=str, allow_nan=False), encoding="utf-8")
    return 1 if result.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
