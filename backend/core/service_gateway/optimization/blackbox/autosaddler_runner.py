"""Runner that drives the pinned upstream AutoSaddler v2 engine, in the worker or in a native box.

The file has two modes. It imports no backend application code in either.

- Worker mode (``--worker``, SKY-723). The worker starts this file with the
  isolated AutoSaddler interpreter, as ``python -s -P``, so Skynet's own
  ``autosaddler`` module can not shadow upstream's. The process holds no
  capability of its own: each model call, box command, tree read and tree
  write, and each evaluation is one request to the worker over this process's
  stdin and stdout (see ``autosaddler.py``). The worker runs each model call
  and command as one engine-loop step.
- Native mode (a payload file argument). The native runtime copies this file
  into an isolated sandbox next to the app plugin assets. The parent owns the
  scorer and its cases and answers evaluations over the filesystem mailbox
  the other native engines use; an app agent harness runs each session. This
  mode stays until the SKY-728 cutover.

Everything the app contributes is expressed through the upstream ports: a
scenario whose evaluator scores candidates through the worker's evaluator
budget, an evidence builder that surfaces the scorer's per-case feedback, a
prompt pack composed from the upstream methodology plus the app plugin, and a
transport that runs each upstream session as a model tool loop. The engine
loop, policies and run store are upstream's own.

A resume runs this process again from its start, and the worker answers each
committed step from its log. So the requests must be the same in each run: the
ids are seeded, the run folder paths and clock times never reach a prompt, and
the requests go one at a time.

The prompt-pack wiring and plugin prompts are adapted from microsoft/AutoSaddler
under the MIT License; see ``autosaddler_plugin/NOTICE.md`` and
``autosaddler_plugin/LICENSE``.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import dataclasses
import importlib.util
import io
import itertools
import json
import math
import os
import random
import re
import shlex
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import traceback
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, tzinfo
from decimal import Decimal
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import IO, Any

try:
    from autosaddler.v2.core import domain as as_domain
    from autosaddler.v2.core import policies as as_policies
    from autosaddler.v2.core import ports as as_ports
    from autosaddler.v2.core.engine import AutoSaddlerEngine
    from autosaddler.v2.core.run_state import RunState
    from autosaddler.v2.harness.component_map import ComponentMapHarnessSpace
    from autosaddler.v2.harness.git import GitHarnessSpace, GitVerificationVerdict
    from autosaddler.v2.prompting import assets as as_assets
    from autosaddler.v2.prompting.history import HISTORY_ROOT, build_history_bundle
    from autosaddler.v2.prompting.models import SessionSpec
    from autosaddler.v2.prompting.models import Usage as SessionUsage
    from autosaddler.v2.providers.base import BaseAgentProvider, TransportOutcome
    from autosaddler.v2.providers.workspace_renderer import WorkspaceRenderer
    from autosaddler.v2.storage.local import LocalRunStore
except ImportError:  # Upstream needs Python 3.12; worker-side tests still import the helpers.
    as_domain = as_policies = as_ports = as_assets = None
    AutoSaddlerEngine = RunState = ComponentMapHarnessSpace = GitHarnessSpace = GitVerificationVerdict = None
    HISTORY_ROOT = build_history_bundle = SessionSpec = LocalRunStore = None
    SessionUsage = BaseAgentProvider = TransportOutcome = WorkspaceRenderer = None

try:
    from . import harness_bridge, repo_tree
except ImportError:  # This file runs as a script beside its sibling modules.
    _sibling_modules = {}
    for _sibling in ("harness_bridge", "repo_tree"):
        _spec = importlib.util.spec_from_file_location(_sibling, Path(__file__).with_name(f"{_sibling}.py"))
        assert _spec is not None
        assert _spec.loader is not None
        _sibling_modules[_sibling] = importlib.util.module_from_spec(_spec)
        sys.modules[_sibling] = _sibling_modules[_sibling]
        _spec.loader.exec_module(_sibling_modules[_sibling])
    harness_bridge = _sibling_modules["harness_bridge"]
    repo_tree = _sibling_modules["repo_tree"]

_RPC_PREFIX = "APP_NATIVE_RPC "
_PROGRESS_PREFIX = "APP_NATIVE_PROGRESS "
_LOG_PREFIX = "APP_NATIVE_LOG "
_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
_PROC_ROOT = Path("/proc")
_TOKEN_NAMES = ("prompt_tokens", "completion_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
# The worker passes this flag; the native box passes its payload file instead.
WORKER_FLAG = "--worker"
_PLUGIN_NAME = "app"
_PLUGIN_ROOT = Path(__file__).resolve().parent / "autosaddler_plugin"
_SINGLE_COMPONENT = "candidate"
_EVIDENCE_SCHEMA = "app-autosaddler-evidence/v1"
_DIAGNOSIS_SCHEMA = "app-autosaddler-diagnosis/v1"
_EVOLUTION_SCHEMA = "app-autosaddler-evolution/v1"
_REFLECTION_SCHEMA = "app-autosaddler-reflection/v1"
_SESSION_CONTEXT_PATH = ".autosaddler/session_context.json"
_TRAINING_EVIDENCE_PATH = ".autosaddler/training_evidence.json"
_PROMPT_ASSETS_PATH = ".autosaddler/prompt_assets.json"
_CAPABILITIES = frozenset({"read_workspace", "edit_workspace", "load_skills"})
# Every harness brings its own tools; the renderer only needs to know each
# capability is available. ``.agents/skills`` is the shared
# skill location pi, codex and opencode all discover.
_HARNESS_CAPABILITY_TOOLS = dict.fromkeys(
    ("read_workspace", "edit_workspace", "run_commands", "load_skills", "network"), ()
)
# The worker-mode renderer names these tools in the instruction file; the transport below serves them.
_CAPABILITY_TOOLS = {
    "read_workspace": ("list_files", "read_file"),
    "edit_workspace": ("write_file",),
    "run_commands": ("run_command",),
    "load_skills": ("read_file",),
}
_SKILL_DIRECTORY = ".agents/skills"
# Under ``.autosaddler`` so it never collides with a repository's own instruction file.
_INSTRUCTION_FILE = ".autosaddler/INSTRUCTIONS.md"
_GIT_IDENTITY = ("-c", "user.name=app", "-c", "user.email=app@localhost", "-c", "commit.gpgsign=false")
# Repository runs read their prompts and skills from this plugin subfolder.
_REPO_ASSETS = "repo/"
# Mirrors protocol.RUN_FAILED_STATUS: this file imports no app code. The status
# marks a case whose [run] entry failed (SKY-751).
_RUN_FAILED_STATUS = "run_failed"
# Upstream AutoSaddler 0.2.0 ends the run when a training case has no valid
# score, and a valid score must be finite. So a case with no score gets this
# floor, in upstream's view only: it always loses, and the app never reports
# it. It is not -sys.float_info.max because upstream sums the case scores
# before it divides, and that sum must stay finite (up to 1.7e8 cases).
# SKY-750 (metric direction): the floor assumes that a higher score is
# better. A minimize run must flip it to +1e300.
_NO_SCORE_FLOOR = -1e300
# Upstream records means and per-case means that can hold the floor, and the
# agent reads them. Such a mean is near _NO_SCORE_FLOOR / n with n below 1.7e8
# cases, so any number at or below this bound comes from the floor.
_NO_SCORE_BOUND = _NO_SCORE_FLOOR / 1e9
_MODEL_LABEL = "engine-loop"
_MAX_TURNS = 40
# Upstream bounds each session with ``asyncio.wait_for`` on the real clock (30 s by default). In worker mode a
# timeout leaves the old session thread on the channel while upstream retries, and a replay would not time out
# where the live run did. The engine loop bounds each step instead, so this fixed bound is never reached.
_STEP_SESSION_TIMEOUT_SECONDS = 365 * 24 * 3600.0
_TOOL_OUTPUT_CHARS = 16_000
# The model sees this in place of the process's own folder, which differs between a run and its resume.
_WORK_ROOT = "/work"
_CLOCK_START = datetime(2000, 1, 1, tzinfo=UTC)
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?")
# The engine loop's flat tool form; its model client converts the tools to the provider's form.
_TOOLS = [
    {
        "name": "list_files",
        "description": "List the files below a workspace folder.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Workspace-relative folder."}},
            "required": [],
        },
    },
    {
        "name": "read_file",
        "description": "Read one workspace file as text.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Workspace-relative file."}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write one workspace file, and make its folders.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
    {
        "name": "run_command",
        "description": (
            "Run a shell command in a fresh box over a copy of the workspace, with no network. "
            "Its file changes do not come back to the workspace."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
]


def _openai_call(call: Mapping[str, Any]) -> dict[str, Any]:
    """Give one flat tool call of the engine loop in the OpenAI form that the next prompt's history holds.

    Args:
        call: The flat ``{"id", "name", "arguments"}`` call.

    Returns:
        ``{"id", "type": "function", "function": {"name", "arguments"}}``, with the arguments as JSON object text;
        arguments that are no JSON object read as ``{}``.
    """
    arguments = call.get("arguments")
    return {
        "id": call.get("id"),
        "type": "function",
        "function": {
            "name": call.get("name"),
            # Sorted keys give the same text for a live reply and for its replay from the step log. A provider
            # adapter can parse the arguments of each earlier call, so raw text that is no JSON object would fail
            # every later call; only the tool's error result tells the model about it.
            "arguments": json.dumps(arguments, sort_keys=True) if isinstance(arguments, dict) else "{}",
        },
    }


class EvaluationStopped(BaseException):
    """Abort upstream immediately; upstream retry handling must not see this."""


class BudgetStopped(EvaluationStopped):
    """Stop admission without converting an unperformed evaluation into feedback."""


class TargetReached(BaseException):
    """Stop the loop once a development score meets the configured target."""


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
        self.total_evals = 0
        self._write_lock = threading.Lock()

    def evaluate(self, candidate: str | dict[str, str], example: Any = None) -> tuple[float, dict[str, Any]]:
        """Evaluate only through the parent-owned budget and scorer.

        Args:
            candidate: Text candidate or named component mapping.
            example: Visible training or development case.

        Returns:
            Parent score and feedback.

        Raises:
            EvaluationStopped: When the parent rejects evaluation or does not reply.
        """
        if self.stopped.is_set():
            raise EvaluationStopped("The parent evaluator has stopped this run.")
        request_id = uuid.uuid4().hex
        self.emit(_RPC_PREFIX, {"id": request_id, "candidate": candidate, "example": example})
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
                with self._write_lock:
                    self.total_evals += 1
                # The parent sends null for a case with no score (a metric
                # error or a failed [run] entry); the evaluator gives any
                # non-finite score the no-score floor (SKY-751).
                score = response.get("score")
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
        """Write one framed event atomically across evaluator threads.

        Args:
            prefix: Request or progress event family.
            payload: Event fields to serialize.
        """
        with self._write_lock:
            print(f"{prefix}{self.nonce} {json.dumps(payload, default=str, allow_nan=False)}", flush=True)


class WorkerChannel:
    """Call the worker: one JSON request line out, one JSON reply line back, one call at a time."""

    def __init__(self, reader: IO[str], writer: IO[str]) -> None:
        """Bind the channel to the worker's pipes.

        Args:
            reader: The replies from the worker.
            writer: The requests to the worker.
        """
        self.reader = reader
        self.writer = writer
        self.error: EvaluationStopped | None = None
        self.total_evals = 0
        # The lock keeps each request and its reply together on the pipes when a call comes from a thread. It
        # does not order the steps: upstream runs one session at a time, and the evaluator admits one case at a
        # time (``max_concurrency=1``), so the calls come in one order.
        self._lock = threading.Lock()

    def call(self, op: str, **fields: Any) -> dict[str, Any]:
        """Send one request and wait for its reply.

        Args:
            op: The request kind.
            **fields: The request fields.

        Returns:
            The worker's reply.

        Raises:
            EvaluationStopped: When the worker closed the channel.
        """
        with self._lock:
            self.writer.write(json.dumps({"op": op, **fields}, sort_keys=True, allow_nan=False) + "\n")
            self.writer.flush()
            line = self.reader.readline()
        if not line:
            raise EvaluationStopped("The worker closed the channel.")
        return json.loads(line)

    def emit(self, prefix: str, payload: dict[str, Any]) -> None:
        """Send one progress event or run-log record, as :meth:`EvaluatorMailbox.emit` does in a box.

        Args:
            prefix: ``_PROGRESS_PREFIX`` or ``_LOG_PREFIX``.
            payload: The event or the record.
        """
        if prefix == _PROGRESS_PREFIX:
            self.call("progress", event=_json_finite(payload))
        else:
            self.call("log", record=_json_finite(payload))

    def evaluate(
        self, candidate: str | dict[str, str], example: Any = None, candidate_id: int | None = None
    ) -> tuple[float, dict[str, Any]]:
        """Evaluate only through the worker's budget and scorer.

        Args:
            candidate: Text candidate, named parts, or a repository patch.
            example: Visible training or development case.
            candidate_id: The version number the run view shows, if it has one.

        Returns:
            The score, NaN for a case with no score (a metric error or a failed ``[run]`` entry), and the scorer's
            feedback.

        Raises:
            EvaluationStopped: When the worker refuses the evaluation; ``BudgetStopped`` for a spent budget.
        """
        if self.error is not None:
            raise self.error
        reply = self.call("evaluate", candidate=candidate, example=example, candidate_id=candidate_id)
        if "error" in reply:
            kind = BudgetStopped if reply.get("stop_reason") == "budget_reached" else EvaluationStopped
            self.error = kind(str(reply["error"]))
            raise self.error
        self.total_evals += 1
        score = reply.get("score")
        return (math.nan if score is None else float(score)), dict(reply.get("info") or {})

    def put_files(self, files: Mapping[str, bytes], base_tree_id: str | None) -> str:
        """Write files over a tree in the worker's tree store.

        Args:
            files: The file bytes by ``/``-separated path.
            base_tree_id: The tree to start from, or ``None``.

        Returns:
            The new tree id.

        Raises:
            ValueError: When the tree store refuses the files.
        """
        reply = self.call(
            "put_files",
            files={path: base64.b64encode(data).decode("ascii") for path, data in sorted(files.items())},
            base_tree_id=base_tree_id,
        )
        if "error" in reply:
            raise ValueError(str(reply["error"]))
        return str(reply["tree_id"])

    def list_files(self, tree_id: str, path: str) -> list[str] | None:
        """List the files below a folder of a tree in the worker's tree store.

        Args:
            tree_id: The tree.
            path: The ``/``-separated folder; ``""`` is the tree's root.

        Returns:
            The sorted file paths from the tree's root, without the run's hidden paths; ``None`` when the
            path is not a folder.
        """
        paths = self.call("list_files", tree_id=tree_id, path=path).get("paths")
        return None if paths is None else [str(name) for name in paths]

    def read_file(self, tree_id: str, path: str) -> bytes | None:
        """Read one file of a tree in the worker's tree store.

        Args:
            tree_id: The tree.
            path: The ``/``-separated file path.

        Returns:
            The bytes, or ``None`` when the path is no file in the tree.
        """
        data = self.call("read_file", tree_id=tree_id, path=path).get("data")
        return None if data is None else base64.b64decode(data)


def open_channel() -> WorkerChannel:
    """Take this process's stdout for the channel, and send every other print to stderr.

    Upstream and git may print; a stray line on the channel would break it.

    Returns:
        The channel over stdin and the original stdout.
    """
    writer = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(2, 1)
    return WorkerChannel(sys.stdin, writer)


def seed_ids(seed: int = 0) -> None:
    """Make random values and ``uuid4`` ids the same in a run and in its resume.

    Upstream's other random sources need no seed: its policies seed their own ``random.Random``, and its
    ``tempfile`` names are for files that it renames at once, so they never reach a request. Upstream and this
    runner do not use ``secrets``, ``os.urandom`` or ``uuid1``.

    Args:
        seed: The seed.
    """
    random.seed(seed)
    ids = random.Random(seed)
    original = uuid.uuid4

    def fixed() -> uuid.UUID:
        """Return the next seeded id.

        Returns:
            A version 4 UUID from the seeded generator.
        """
        return uuid.UUID(int=ids.getrandbits(128), version=4)

    # Upstream's engine module binds ``uuid4`` by name when it is imported (``from uuid import uuid4``), which
    # is before this runs, so each module that holds the original gets the seeded one too.
    for module in list(sys.modules.values()):
        if getattr(module, "__dict__", {}).get("uuid4") is original:
            module.uuid4 = fixed  # type: ignore[union-attr]


def fix_clocks() -> None:
    """Give upstream's modules a counted clock, so a run and its resume write the same event records.

    Upstream puts clock times and wall seconds into its events, and its history manifest hashes the events into
    a file that the box and the model see, where a scrub can not reach. Upstream reads the clock only for these
    records; session time limits are in this runner's transport. A git commit id also holds the commit time, so
    every commit, the runner's and upstream's, gets the fixed start as its time.

    The patch replaces names in upstream's modules only, not in ``time`` itself: asyncio and the runner's own
    waits read ``time.monotonic`` and need the real clock. Upstream imports all its clock users before this runs.
    """
    os.environ["GIT_AUTHOR_DATE"] = os.environ["GIT_COMMITTER_DATE"] = _CLOCK_START.isoformat()
    ticks = itertools.count()

    def seconds() -> float:
        """Return the next counted second.

        Returns:
            The number of earlier clock reads.
        """
        return float(next(ticks))

    def epoch() -> float:
        """Return the next counted wall time.

        Returns:
            The fixed start as epoch seconds, plus one second for each earlier clock read.
        """
        return _CLOCK_START.timestamp() + next(ticks)

    class CountedDatetime(datetime):
        """A ``datetime`` whose clock reads count one second for each call from a fixed start."""

        @classmethod
        def now(cls, tz: tzinfo | None = None) -> CountedDatetime:
            """Return the next counted time.

            Args:
                tz: The time zone of the result; ``None`` gives a naive time.

            Returns:
                The fixed start plus one second for each earlier call.
            """
            moment = cls.fromtimestamp(epoch(), tz or UTC)
            return moment if tz is not None else moment.replace(tzinfo=None)

        @classmethod
        def utcnow(cls) -> CountedDatetime:
            """Return the next counted time as a naive UTC time.

            Returns:
                The fixed start plus one second for each earlier call.
            """
            return cls.now()

        @classmethod
        def today(cls) -> CountedDatetime:
            """Return the next counted time as a naive time.

            Returns:
                The fixed start plus one second for each earlier call.
            """
            return cls.now()

    counted = {
        time.time: epoch,
        time.time_ns: lambda: int(epoch() * 1e9),
        time.monotonic: seconds,
        time.monotonic_ns: lambda: int(seconds() * 1e9),
        time.perf_counter: seconds,
        time.perf_counter_ns: lambda: int(seconds() * 1e9),
    }
    counted_time = SimpleNamespace(**{**vars(time), **{clock.__name__: tick for clock, tick in counted.items()}})
    datetime_module = sys.modules[datetime.__module__]
    counted_datetime_module = SimpleNamespace(**{**vars(datetime_module), "datetime": CountedDatetime})
    for name, module in list(sys.modules.items()):
        if name.split(".")[0] != "autosaddler":
            continue
        names = getattr(module, "__dict__", {})
        for key, value in list(names.items()):
            if value is time:
                setattr(module, key, counted_time)
            elif value is datetime_module:
                setattr(module, key, counted_datetime_module)
            elif value is datetime:
                setattr(module, key, CountedDatetime)
            else:
                # A ``from time import monotonic`` binds the clock itself. Identity, since a module value may not
                # hash.
                for clock, tick in counted.items():
                    if value is clock:
                        setattr(module, key, tick)


def scrub(text: str, root: Path, *, times: bool = True) -> str:
    """Remove the parts of a text that differ between a run and its resume.

    Args:
        text: Text for the model.
        root: This process's folder, which the worker makes new for each run.
        times: Whether to also hide clock times. Only the session prompts need it: a replay matches the prompt
            hash, and upstream builds those prompts live. A tool output needs no time scrub, because a replay gives
            the committed command output and the same workspace files; a ``<time>`` there would corrupt a file
            that the model reads and writes back, and would hide log times.

    Returns:
        The text with ``root`` as ``/work`` and, when ``times``, each clock time as ``<time>``.
    """
    text = text.replace(str(root), _WORK_ROOT)
    return _TIMESTAMP.sub("<time>", text) if times else text


def candidate_value(components: Mapping[str, str]) -> str | dict[str, str]:
    """Return the app candidate shape encoded by a component map.

    Args:
        components: Upstream frozen-key component mapping.

    Returns:
        The bare text for single-component candidates, else the named parts.
    """
    if set(components) == {_SINGLE_COMPONENT}:
        return components[_SINGLE_COMPONENT]
    return dict(components)


def baseline_components(seed: Any) -> dict[str, str]:
    """Encode the app seed as an upstream component map.

    Args:
        seed: Seed text or named parts from the task.

    Returns:
        Component mapping upstream can mutate.

    Raises:
        ValueError: When the seed is neither text nor named parts.
    """
    if isinstance(seed, str):
        return {_SINGLE_COMPONENT: seed}
    if isinstance(seed, dict) and seed:
        return {str(name): str(text) for name, text in seed.items()}
    raise ValueError("AutoSaddler requires a seed candidate: a version text or named parts.")


def visible_examples(cases: Sequence[Any] | None) -> list[Any]:
    """Return the examples both upstream splits score, one ``None`` when there are no cases.

    Upstream diagnoses on a training split and confirms each patch on a
    development split. The app confirms a patch by re-running the same scorer,
    so both splits hold the same examples; without cases the scorer runs once
    per candidate with ``case=None``.

    Args:
        cases: The run's cases, if any.

    Returns:
        The examples to score, never empty.
    """
    return list(cases or []) or [None]


def _case_id(split: str, index: int, example: Any) -> str:
    """Derive a stable case identity from the example when it carries one.

    Args:
        split: Upstream split name.
        index: Position within the split.
        example: Visible example.

    Returns:
        A split-prefixed identifier unique within the run.
    """
    label = example.get("id") if isinstance(example, dict) else None
    suffix = str(label) if isinstance(label, str | int) and str(label) else str(index)
    return f"{split}-{index}-{suffix}" if isinstance(label, str | int) else f"{split}-{suffix}"


def build_cases(split: str, examples: Sequence[Any]) -> tuple[Any, ...]:
    """Wrap app examples as upstream cases that carry the raw example payload.

    Args:
        split: Upstream split name.
        examples: Visible examples for that split.

    Returns:
        Upstream cases whose payload holds the example under ``example``.
    """
    return tuple(
        as_domain.Case(
            case_id=_case_id(split, index, example),
            split=split,
            payload={"example": as_domain.to_json_value(example)},
        )
        for index, example in enumerate(examples)
    )


def _git_output(cwd: Path, *arguments: str, env: Mapping[str, str] | None = None) -> str:
    """Run git in a checkout and return its output as UTF-8 text.

    Args:
        cwd: Checkout or worktree to run in.
        *arguments: Arguments after ``git``.
        env: Process environment, when it differs from this process's.

    Returns:
        Standard output.

    Raises:
        RuntimeError: When git fails.
        UnicodeDecodeError: When the output is not UTF-8.
    """
    result = subprocess.run(
        ["git", "--literal-pathspecs", *repo_tree.SAFE_GIT, *_GIT_IDENTITY, *arguments],
        cwd=cwd,
        env=None if env is None else dict(env),
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace") or result.stdout.decode("utf-8", "replace")
        raise RuntimeError(f"git {arguments[0]} failed: {detail.strip()[-2000:]}")
    return result.stdout.decode("utf-8")


def workspace_patch(workspace: Path, base: str, changed_paths: Sequence[Any]) -> str:
    """Diff a mutation workspace against the starting commit without touching its index.

    Args:
        workspace: Upstream mutation worktree, checked out at the parent version.
        base: Starting commit of the run.
        changed_paths: Paths the agent changed relative to the parent.

    Returns:
        The patch the parent would score for this version.
    """
    with tempfile.TemporaryDirectory() as scratch:
        # Upstream stages and commits the changed paths itself after verification,
        # so the worktree's own index must stay untouched; a private one stages them here.
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(scratch) / "index")}
        _git_output(workspace, "read-tree", "HEAD", env=env)
        if changed_paths:
            _git_output(workspace, "add", "--all", "--", *(str(path) for path in changed_paths), env=env)
        return _git_output(workspace, "diff", "--cached", "--binary", "--no-color", "--no-ext-diff", base, env=env)


def rule_blocker(mailbox: EvaluatorMailbox | WorkerChannel) -> Callable[[str], None]:
    """Build the callback that sends a path-rule block to the parent's or the worker's run log.

    Args:
        mailbox: Parent connection in a box, or the worker connection.

    Returns:
        A function taking the block's reason.
    """

    def block(reason: str) -> None:
        """Send one ``rule.block`` run event.

        Args:
            reason: Why the path rules refused the version.
        """
        mailbox.emit(_LOG_PREFIX, repo_tree.rule_block_record(reason))

    return block


def refusal_logger(mailbox: EvaluatorMailbox) -> Callable[[dict[str, str]], None]:
    """Build the callback that sends a hidden-path refusal to the parent's run log.

    Args:
        mailbox: Parent connection.

    Returns:
        A function taking one ``{"tool", "kind"}`` refusal.
    """

    def refuse(refusal: dict[str, str]) -> None:
        """Send one ``proposer.refusal`` run event.

        Args:
            refusal: The refused tool and the hidden path kind.
        """
        mailbox.emit(_LOG_PREFIX, harness_bridge.refusal_record(refusal))

    return refuse


def checkout_seed(channel: WorkerChannel, tree_id: str, destination: Path) -> str:
    """Copy the seed tree into a new Git checkout, and commit it as the base of every version.

    The worker lists and reads the tree with no box and no step, and leaves out the run's hidden paths. The
    listing gives no file mode, so the checkout has no executable bits; a version's patch is against this
    checkout, so it carries no mode change.

    Args:
        channel: Worker connection.
        tree_id: The seed tree.
        destination: The new checkout folder.

    Returns:
        The base commit.

    Raises:
        RuntimeError: When the worker can not list the seed tree.
    """
    paths = channel.list_files(tree_id, "")
    if paths is None:
        raise RuntimeError("Could not list the seed tree.")
    destination.mkdir(parents=True)
    for path in paths:
        # A seeded ``.git/config`` or hook would run its commands at the ``git`` calls below; a case-insensitive
        # disk also takes ``.GIT`` as ``.git``.
        if ".git" in (part.casefold() for part in PurePosixPath(path).parts):
            continue
        data = channel.read_file(tree_id, path)
        if data is None:
            continue
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    _git_output(destination, "init", "--quiet")
    _git_output(destination, "add", "--all", "--force")
    _git_output(destination, "commit", "--quiet", "--allow-empty", "--no-verify", "-m", "base")
    return _git_output(destination, "rev-parse", "HEAD").strip()


def repo_verifier(
    base: str,
    editable_paths: Sequence[str],
    readonly_paths: Sequence[str],
    on_block: Callable[[str], None] | None = None,
) -> Any:
    """Build the upstream verifier that refuses versions the trusted scorer would refuse.

    Args:
        base: Starting commit of the run.
        editable_paths: Paths a version may change.
        readonly_paths: Submodules and Git LFS files that stay as fetched.
        on_block: Under a constraints file, receives each path-rule refusal's
            reason, and the agent reads only the illegal-action message.

    Returns:
        A ``GitVerifier`` for upstream's ``GitHarnessSpace``.
    """

    def verify(context: Any) -> Any:
        """Check one finished mutation before upstream commits it.

        Args:
            context: Upstream verification context.

        Returns:
            Upstream verdict; a rejection becomes a rejected iteration, never a score.
        """
        try:
            patch = workspace_patch(context.workspace, base, context.changed_paths)
        except UnicodeDecodeError:
            return GitVerificationVerdict(False, "utf8", "A changed text file is not UTF-8.", ())
        problems = repo_tree.patch_violations(patch, editable_paths, readonly_paths)
        if problems and on_block is not None:
            on_block(" ".join(problems))
            return GitVerificationVerdict(False, "repository-rules", repo_tree.ILLEGAL_ACTION_MESSAGE, ())
        if len(patch.encode("utf-8")) > repo_tree.MAX_PATCH_BYTES:
            problems.append("The change is larger than a version may be.")
        if problems:
            return GitVerificationVerdict(False, "repository-rules", "; ".join(problems), ())
        return GitVerificationVerdict(True, "repository-rules", "The change stays inside the editable paths.", ())

    return verify


def repo_space(
    repo: Mapping[str, Any],
    seed_patch: Any,
    run_dir: Path,
    proposer: dict[str, Any] | None,
    mailbox: EvaluatorMailbox | WorkerChannel,
) -> tuple[Any, str]:
    """Check out the repository and open upstream's Git candidate space on it.

    Args:
        repo: Payload's ``repo`` entry: tree chunks in a box or the seed tree in the worker, path rules and
            whether a constraints file governs the run.
        seed_patch: Starting version as a patch against the fetched commit, or empty.
        run_dir: Upstream run directory.
        proposer: Serialized harness launch in a box, whose workspace files never join a version; ``None`` in
            the worker.
        mailbox: Parent connection in a box, or the worker connection, which also reads the seed tree; both
            receive path-rule blocks.

    Returns:
        The Git harness space and the commit every version is diffed against.
    """
    if isinstance(mailbox, WorkerChannel):
        checkout = Path("repo-checkout").resolve()
        base = checkout_seed(mailbox, str(repo["tree_id"]), checkout)
        workspace_files = [_INSTRUCTION_FILE]
    else:
        checkout = repo_tree.unpack_tree((Path(chunk) for chunk in repo["chunks"]), Path("repo-checkout").resolve())
        base = _git_output(checkout, "rev-parse", repo_tree.BASE_REF).strip()
        workspace_files = harness_bridge.workspace_files(proposer)
    # Harnesses read skills rendered into the mutation worktree, a folder
    # upstream does not treat as its own, and the worker renderer writes its
    # instruction file there; left unexcluded, they would count as the
    # agent's change. Every worktree shares this exclude file.
    with (checkout / ".git/info/exclude").open("a", encoding="utf-8") as exclude:
        exclude.write(f"\n/{_SKILL_DIRECTORY}/\n")
    repo_tree.exclude_paths(checkout, workspace_files)
    start = base
    if isinstance(seed_patch, str) and seed_patch.strip():
        repo_tree.apply_patch(checkout, seed_patch)
        _git_output(checkout, "add", "--all")
        _git_output(checkout, "commit", "--quiet", "--no-verify", "-m", "seed")
        start = _git_output(checkout, "rev-parse", "HEAD").strip()
    editable = [str(path) for path in repo["editable_paths"]]
    readonly = [str(path) for path in repo["readonly_paths"]]
    space = GitHarnessSpace(
        source_repo=checkout,
        base_revision=start,
        store_root=run_dir / "candidates",
        worktree_root=run_dir / "worktrees",
        writable_paths=[PurePosixPath(path) for path in editable],
        forbidden_paths=[PurePosixPath(path) for path in readonly],
        verifier=repo_verifier(
            base, editable, readonly, on_block=rule_blocker(mailbox) if repo.get("constrained") else None
        ),
    )
    return space, base


class AppEvaluator:
    """Score upstream candidates through the parent scorer, case by case."""

    def __init__(
        self,
        *,
        mailbox: EvaluatorMailbox | WorkerChannel,
        harness_space: Any,
        store_run_dir: Path,
        max_concurrency: int,
        stop_at_score: float | None,
        fingerprint: str,
        development_case_ids: Sequence[str] = (),
        repo_base: str | None = None,
    ) -> None:
        """Bind the evaluator to the parent transport and run layout.

        Args:
            mailbox: Parent evaluation transport in a box, or the worker connection.
            harness_space: Upstream space holding candidate text or repository versions.
            store_run_dir: Upstream run directory used for relative artifact references.
            max_concurrency: Parallel scorer requests the parent admits.
            stop_at_score: Development aggregate that ends the search early.
            fingerprint: Stable identity of the scorer for upstream observations.
            development_case_ids: Development cases in split order, which names them by position.
            repo_base: Starting commit of a repository run, whose versions go to the parent as patches against it.
        """
        self.mailbox = mailbox
        self.harness_space = harness_space
        self.repo_base = repo_base
        self.patches: dict[str, str] = {}
        self.store_run_dir = store_run_dir
        self.stop_at_score = stop_at_score
        self.fingerprint = fingerprint
        self.components: dict[str, dict[str, str]] = {}
        self.best_development: dict[str, Any] | None = None
        # Development candidates with a case that has no score. Their
        # upstream aggregate holds the floor, so the app reports no score.
        self.no_score_candidates: set[str] = set()
        # The subset with a failed [run] case. A metric error fails for every
        # version, so only these never become a parent (SKY-751).
        self.run_failed_candidates: set[str] = set()
        # The run view orders versions numerically, so each candidate that
        # reaches development gets the next version number in arrival order.
        self.versions: dict[str, int] = {}
        self.case_ids = {case_id: str(index) for index, case_id in enumerate(development_case_ids)}
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))

    def value_of(self, candidate: Any) -> str | dict[str, str]:
        """Return the candidate in the shape the parent scorer takes.

        Args:
            candidate: Upstream candidate reference.

        Returns:
            The text or named parts, or for a repository version its patch against the starting commit.
        """
        if self.repo_base is None:
            return candidate_value(self.components_of(candidate))
        if candidate.candidate_id not in self.patches:
            materialized = self.harness_space.materialize(candidate, "inspect")
            try:
                # The parent rebuilds every version from this patch alone, exactly
                # as it does for every other engine's repository versions.
                self.patches[candidate.candidate_id] = _git_output(
                    materialized.root, "diff", "--binary", "--no-color", "--no-ext-diff", self.repo_base, "HEAD"
                )
            finally:
                materialized.release()
        return self.patches[candidate.candidate_id]

    def components_of(self, candidate: Any) -> dict[str, str]:
        """Read and remember a candidate's component text.

        Args:
            candidate: Upstream candidate reference.

        Returns:
            Component mapping the candidate encodes.
        """
        if candidate.candidate_id not in self.components:
            materialized = self.harness_space.materialize(candidate, "inspect")
            try:
                value = json.loads((materialized.root / "candidate.json").read_text(encoding="utf-8"))
            finally:
                materialized.release()
            self.components[candidate.candidate_id] = {str(name): str(text) for name, text in value.items()}
        return self.components[candidate.candidate_id]

    async def evaluate(self, candidate: Any, cases: Sequence[Any], context: Any) -> Any:
        """Produce one upstream evaluation from parent scores.

        Args:
            candidate: Upstream candidate to score.
            cases: Cases requested for this evaluation.
            context: Upstream evaluation context and attempt sink.

        Returns:
            Upstream evaluation covering exactly the requested cases.

        Raises:
            TargetReached: When a development aggregate meets the stop target.
        """
        value = self.value_of(candidate)
        context.artifact_dir.mkdir(parents=True, exist_ok=True)
        if context.purpose == "development":
            self.versions.setdefault(candidate.candidate_id, len(self.versions))
        observations = await asyncio.gather(
            *(
                self._observe(candidate.candidate_id, value, case, repetition, context, total=len(cases))
                for case in cases
                for repetition in range(context.repetitions)
            )
        )
        evaluation = as_domain.Evaluation(
            evaluation_id=as_domain.sha256_digest(context.operation_id),
            candidate_id=candidate.candidate_id,
            split=context.split,
            purpose=context.purpose,
            iteration=context.iteration,
            requested_case_ids=tuple(case.case_id for case in cases),
            observations=tuple(sorted(observations, key=lambda item: (item.case_id, item.repetition))),
            artifact_dir=as_domain.ArtifactRef(
                uri=context.artifact_dir.relative_to(self.store_run_dir).as_posix(),
                kind="app-evaluation-directory",
            ),
        )
        if context.purpose == "development":
            self._report_development(candidate.candidate_id, value, evaluation.aggregate_score, observations)
        return evaluation

    async def _observe(
        self, candidate_id: str, value: Any, case: Any, repetition: int, context: Any, *, total: int
    ) -> Any:
        """Score one case once, reusing an observation upstream already recorded.

        Args:
            candidate_id: Upstream candidate identity.
            value: App candidate shape sent to the scorer.
            case: Case to score.
            repetition: Repetition index.
            context: Upstream evaluation context.
            total: How many cases the evaluation covers.

        Returns:
            The recorded upstream observation.
        """
        sink = context.attempt_sink
        existing = sink.completed(candidate_id=candidate_id, case_id=case.case_id, repetition=repetition)
        if existing is not None:
            return existing
        async with self._semaphore:
            attempt_id, _attempt_number = sink.start(
                candidate_id=candidate_id, case_id=case.case_id, repetition=repetition
            )
            worker = isinstance(self.mailbox, WorkerChannel)
            started = time.monotonic()
            try:
                if worker:
                    score, info = await asyncio.to_thread(
                        self.mailbox.evaluate, value, case.payload.get("example"), self.versions.get(candidate_id)
                    )
                else:
                    score, info = await asyncio.to_thread(self.mailbox.evaluate, value, case.payload.get("example"))
            except EvaluationStopped:
                sink.fail(attempt_id, "evaluation_stopped", as_domain.Cost(rollouts=1))
                raise
            # No wall time in the worker: it reaches the history the model reads, and a resume must send the
            # same prompts.
            cost = (
                as_domain.Cost(rollouts=1)
                if worker
                else as_domain.Cost(rollouts=1, wall_seconds=time.monotonic() - started)
            )
            # Upstream only diagnoses an iteration that has failing training
            # cases, so anything short of a full score must read as a task
            # failure. A case with no score is a valid task failure at the
            # floor: an invalid case ends the upstream run (SKY-751).
            disposition: Any = "success" if math.isfinite(score) and score >= 1.0 else "task_failure"
            # Repetitions re-score the same case, so only the first fills the
            # version's case in the run view; the aggregate averages them.
            if context.purpose == "development" and repetition == 0 and math.isfinite(score):
                self.mailbox.emit(
                    _PROGRESS_PREFIX,
                    {
                        "event": "case_scored",
                        "candidate_id": self.versions[candidate_id],
                        "example_id": self.case_ids.get(case.case_id, "?"),
                        "score": score,
                        "total": total,
                    },
                )
            observation = as_domain.Observation.create(
                candidate_id=candidate_id,
                case_id=case.case_id,
                split=context.split,
                repetition=repetition,
                disposition=disposition,
                score=score if math.isfinite(score) else _NO_SCORE_FLOOR,
                evaluator_fingerprint=self.fingerprint,
                cost=cost,
                metadata={"scorer_feedback": as_domain.to_json_value(_json_finite(info))},
            )
            sink.complete(attempt_id, observation, cost)
            return observation

    def _report_development(
        self, candidate_id: str, value: Any, score: float | None, observations: Sequence[Any]
    ) -> None:
        """Publish a completed development aggregate, with its case scores, to the parent.

        Args:
            candidate_id: Upstream candidate identity.
            value: App candidate shape.
            score: Mean development score, if every case produced one.
            observations: The evaluation's per-case observations.

        Raises:
            TargetReached: When the aggregate meets the stop target.
        """
        # A version with a case that has no score has the floor in its mean
        # (SKY-751). Final selection drops it, so it must not become best,
        # show a score or stop the run.
        if score is None or not math.isfinite(score) or any(_no_score(item) for item in observations):
            self.no_score_candidates.add(candidate_id)
            if any(_failed_run_case(item) for item in observations):
                self.run_failed_candidates.add(candidate_id)
            return
        if self.best_development is None or score > self.best_development["best_score"]:
            self.best_development = {"best_candidate": value, "best_score": score, "candidate_id": candidate_id}
        per_case: dict[str, list[float]] = {}
        for observation in observations:
            if observation.score is not None:
                per_case.setdefault(observation.case_id, []).append(float(observation.score))
        self.mailbox.emit(
            _PROGRESS_PREFIX,
            {
                "candidate_id": self.versions[candidate_id],
                "candidate": value,
                "score": score,
                "total_evals": self.mailbox.total_evals,
                "per_example": [
                    (self.case_ids.get(case_id, "?"), sum(values) / len(values)) for case_id, values in per_case.items()
                ],
            },
        )
        if self.stop_at_score is not None and score >= self.stop_at_score:
            raise TargetReached(f"Development score {score} reached the target {self.stop_at_score}.")


def _no_score(observation: Any) -> bool:
    """Tell whether an upstream observation has no score from the parent scorer.

    Args:
        observation: Upstream observation.

    Returns:
        Whether its score is missing, not finite or the no-score floor.
    """
    score = observation.score
    return score is None or not math.isfinite(score) or score == _NO_SCORE_FLOOR


def _without_floor(value: Any) -> Any:
    """Replace every number that comes from the no-score floor with ``None``.

    Args:
        value: A JSON value from an upstream session context or history file.

    Returns:
        The value, with each floor-derived number as ``None``.
    """
    if isinstance(value, float) and value <= _NO_SCORE_BOUND:
        return None
    if isinstance(value, Mapping):
        return {key: _without_floor(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_without_floor(item) for item in value]
    return value


def _history_files(store: Any, context: Mapping[str, Any]) -> dict[str, str]:
    """Build upstream's history files with no floor-derived number in them.

    Upstream writes raw aggregates and case scores into the history, so a
    version with a case that has no score shows the floor. The app never
    reports that floor (SKY-751), so it reads as ``None``. The manifest
    digests then describe the files the agent gets.

    Args:
        store: Upstream run store.
        context: Upstream session context.

    Returns:
        Workspace paths mapped to file text.
    """
    files = dict(build_history_bundle(store, context).workspace_files)
    manifest_path = f"{HISTORY_ROOT}/manifest.json"
    for path, text in files.items():
        if path.endswith(".json") and path != manifest_path:
            files[path] = as_domain.canonical_json(_without_floor(json.loads(text))) + "\n"
    manifest = json.loads(files[manifest_path])
    manifest["files"] = {
        path: {"sha256": as_domain.sha256_digest(text), "bytes": len(text.encode("utf-8"))}
        for path, text in sorted(files.items())
        if path != manifest_path
    }
    files[manifest_path] = as_domain.canonical_json(manifest) + "\n"
    return files


def _scored_view(evaluation: Any) -> Any:
    """Return the evaluation with each floor observation made invalid again.

    Upstream acceptance then compares only the cases both versions scored,
    and its recorded means never hold the floor.

    Args:
        evaluation: Upstream evaluation.

    Returns:
        The evaluation itself when it holds no floor, else a copy.
    """
    if not any(observation.score == _NO_SCORE_FLOOR for observation in evaluation.observations):
        return evaluation
    observations = tuple(
        dataclasses.replace(observation, disposition="execution_error", score=None)
        if observation.score == _NO_SCORE_FLOOR
        else observation
        for observation in evaluation.observations
    )
    return dataclasses.replace(evaluation, observations=observations)


def _failed_run_case(observation: Any) -> bool:
    """Tell whether an upstream observation is a case whose ``[run]`` entry failed.

    Args:
        observation: Upstream observation that carries the scorer feedback.

    Returns:
        Whether it has no score and the run-failed status. A scorer may
        report that status with a real score, which it keeps, as in
        ``protocol.py``.
    """
    feedback = observation.metadata.get("scorer_feedback")
    return isinstance(feedback, Mapping) and feedback.get("status") == _RUN_FAILED_STATUS and _no_score(observation)


def _run_failed(evaluation: Any) -> bool:
    """Tell whether any case of an upstream evaluation had a failed ``[run]`` entry.

    Args:
        evaluation: Upstream evaluation whose observations carry the scorer feedback.

    Returns:
        Whether any observation is a failed ``[run]`` case.
    """
    return any(_failed_run_case(observation) for observation in evaluation.observations)


def _compared_keys(parent: Any, child: Any) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """Check two training evaluations as upstream acceptance does and list their cases.

    Args:
        parent: Parent's training evaluation.
        child: Child's training evaluation.

    Returns:
        The sorted ``(case, repetition)`` keys both sides observed, and the
        subset where both sides have a valid observation.

    Raises:
        ValueError: The evaluations are not training evaluations of the same
            cases, as upstream ``MatchedValidStrictImprovement`` requires.
    """
    if parent.split != "train" or child.split != "train":
        raise ValueError("Acceptance may compare training evaluations only")
    if parent.requested_case_ids != child.requested_case_ids:
        raise ValueError("Parent and child must be evaluated on the identical ordered case set")
    parent_by_key = {(item.case_id, item.repetition): item for item in parent.observations}
    child_by_key = {(item.case_id, item.repetition): item for item in child.observations}
    if parent_by_key.keys() != child_by_key.keys():
        raise ValueError("Parent and child observations must have identical case/repetition keys")
    keys = sorted(parent_by_key)
    return keys, [key for key in keys if parent_by_key[key].is_valid and child_by_key[key].is_valid]


class NoScoreRejectedAcceptance:
    """Acceptance that never prefers a version with a failed ``[run]`` entry (SKY-751).

    Upstream compares only the cases both versions scored, so a child that
    crashed on one case and scored high on the rest would replace a fully
    scored parent. A child with a failed entry is rejected; a child that
    fixes its parent's failed entry is accepted. Acceptance sees only the
    training batch; ``AppPromptPack`` keeps a version with a failed ``[run]``
    case in development from becoming a parent.
    """

    def __init__(self, inner: Any) -> None:
        """Wrap upstream's acceptance policy.

        Args:
            inner: Upstream acceptance, used when both or neither side failed.
        """
        self.inner = inner

    def compare(self, parent: Any, child: Any) -> Any:
        """Decide whether the child replaces the parent.

        Args:
            parent: Parent's training evaluation.
            child: Child's training evaluation on the same cases.

        Returns:
            Upstream's patch verdict.
        """
        parent_failed, child_failed = _run_failed(parent), _run_failed(child)
        parent, child = _scored_view(parent), _scored_view(child)
        keys, matched = _compared_keys(parent, child)
        if parent_failed == child_failed and matched:
            return self.inner.compare(parent, child)
        # Upstream raises when no case is valid on both sides, which would end
        # the run; with nothing to compare, the child cannot be better.
        if parent_failed == child_failed:
            accepted, reason = False, "no case has a valid score on both versions"
        elif parent_failed:
            accepted, reason = True, "the child fixes a failed [run] entry"
        else:
            accepted, reason = False, "the child has a failed [run] entry"
        # The verdict's scores are only a record. Upstream records reject
        # non-finite numbers, so a side with no valid case records 0.0.
        return as_domain.PatchVerdict(
            before_score=parent.aggregate_score if parent.aggregate_score is not None else 0.0,
            after_score=child.aggregate_score if child.aggregate_score is not None else 0.0,
            compared_case_ids=tuple(dict.fromkeys(case_id for case_id, _ in keys)),
            accepted=accepted,
            reason=reason,
        )


class NoScoreLastRanking:
    """Ranking that never selects a version with a failed ``[run]`` case (SKY-751).

    Such a case holds the floor in upstream's mean. The app reports no score
    for that version, so it must not win the final selection. A metric error
    fails for every version, so that version ranks by its mean over its
    scored cases, as the SKY-367 fill ranks it in the other engines.
    """

    def __init__(self, inner: Any) -> None:
        """Wrap upstream's ranking policy.

        Args:
            inner: Upstream ranking, used on the versions with no failed ``[run]`` case.
        """
        self.inner = inner

    def select(self, candidates: Sequence[tuple[Any, Any]]) -> tuple[Any, Any]:
        """Select the best version with a score, or the seed when no version has one.

        Args:
            candidates: ``(candidate, development evaluation)`` pairs, the seed first.

        Returns:
            The selected pair.
        """
        views = {id(item[1]): _scored_view(item[1]) for item in candidates}
        scored = [
            (candidate, views[id(evaluation)])
            for candidate, evaluation in candidates
            if not _run_failed(evaluation) and views[id(evaluation)].aggregate_score is not None
        ]
        if not scored:
            # The app reports no score for the seed then, which finishes the
            # run with no score; any other unscored version would fail the run.
            return candidates[0]
        selected, _ = self.inner.select(scored)
        return next(item for item in candidates if item[0] is selected)


class AppEvidenceBuilder:
    """Turn a training evaluation into the scorer feedback the agent diagnoses from."""

    def __init__(self, store: Any, cases: Mapping[str, Any]) -> None:
        """Bind the builder to the run store and visible cases.

        Args:
            store: Upstream run store.
            cases: Training cases keyed by identity.
        """
        self.store = store
        self.cases = cases

    def build(self, evaluation: Any) -> Any:
        """Write per-case scores, feedback and inputs as a digest-verified artifact.

        Args:
            evaluation: Completed training evaluation.

        Returns:
            Upstream artifact reference for the evidence document.

        Raises:
            ValueError: When evidence is requested for a non-training split.
        """
        if evaluation.split != "train":
            raise ValueError("App evidence is built from the training split only")
        case_records = []
        for case_id in evaluation.requested_case_ids:
            observations = [item for item in evaluation.observations if item.case_id == case_id]
            case = self.cases.get(case_id)
            case_records.append(
                {
                    "case_id": case_id,
                    "input": case.payload.get("example") if case is not None else None,
                    "per_repetition": [
                        {
                            "repetition": item.repetition,
                            "disposition": item.disposition,
                            "score": None if _no_score(item) else item.score,
                            "scorer_feedback": item.metadata.get("scorer_feedback"),
                        }
                        for item in observations
                    ],
                }
            )
        evidence_id = as_domain.sha256_digest(evaluation.evaluation_id)
        return self.store.write_json(
            f"evidence/{evidence_id.removeprefix('sha256:')}/evidence.json",
            {
                "schema_version": _EVIDENCE_SCHEMA,
                "evaluation_id": evaluation.evaluation_id,
                "candidate_id": evaluation.candidate_id,
                "split": "train",
                "purpose": evaluation.purpose,
                "case_records": case_records,
            },
            kind="app-training-evidence",
        )


def _diagnosis_schema(component_names: Sequence[str]) -> dict[str, Any]:
    """Describe the patch output the component-map harness applies.

    Args:
        component_names: Frozen component schema of the candidate.

    Returns:
        JSON schema for diagnose-patch session output.
    """
    return {
        "type": "object",
        "required": ["schema_version", "intent", "diagnosis", "expected_effect", "updates"],
        "properties": {
            "schema_version": {"const": _DIAGNOSIS_SCHEMA},
            "intent": {"type": "string", "minLength": 1},
            "diagnosis": {"type": "string", "minLength": 1},
            "expected_effect": {"type": "string", "minLength": 1},
            "updates": {
                "type": "object",
                "properties": {name: {"type": "string", "minLength": 1} for name in component_names},
                "additionalProperties": False,
                "minProperties": 1,
            },
        },
        "additionalProperties": False,
    }


def _evolve_schema(candidate_ids: Sequence[str], source_options: Mapping[str, Sequence[str]]) -> dict[str, Any]:
    """Describe the composition output upstream turns into a selection plan.

    Args:
        candidate_ids: Accepted candidates available as parents.
        source_options: Component names mapped to candidates that changed them.

    Returns:
        JSON schema for evolve session output.
    """
    return {
        "type": "object",
        "required": ["schema_version", "parent_ids", "component_sources", "rationale"],
        "properties": {
            "schema_version": {"const": _EVOLUTION_SCHEMA},
            "parent_ids": {
                "type": "array",
                "items": {"type": "string", "enum": list(candidate_ids)},
                "minItems": 1,
                "uniqueItems": True,
            },
            "component_sources": {
                "type": "object",
                "properties": {
                    name: {"type": "string", "enum": list(sources)} for name, sources in source_options.items()
                },
                "additionalProperties": False,
            },
            "rationale": {"type": "string", "minLength": 1},
        },
        "additionalProperties": False,
    }


def _repo_diagnosis_schema() -> dict[str, Any]:
    """Describe the diagnosis output for a repository run, whose change is the edited workspace itself.

    Returns:
        JSON schema for diagnose-patch session output.
    """
    return {
        "type": "object",
        "required": ["schema_version", "intent", "diagnosis", "expected_effect", "changed_paths"],
        "properties": {
            "schema_version": {"const": _DIAGNOSIS_SCHEMA},
            "intent": {"type": "string", "minLength": 1},
            "diagnosis": {"type": "string", "minLength": 1},
            "expected_effect": {"type": "string", "minLength": 1},
            "changed_paths": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "uniqueItems": True,
                "minItems": 1,
            },
        },
        "additionalProperties": False,
    }


def _reflection_schema() -> dict[str, Any]:
    """Describe the lesson output upstream records after each iteration.

    Returns:
        JSON schema for reflect session output.
    """
    return {
        "type": "object",
        "required": ["schema_version", "lessons"],
        "properties": {
            "schema_version": {"const": _REFLECTION_SCHEMA},
            "lessons": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["scope", "statement", "evidence_case_ids"],
                    "properties": {
                        "scope": {"type": "string", "minLength": 1},
                        "statement": {"type": "string", "minLength": 1},
                        "evidence_case_ids": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
                    },
                    "additionalProperties": False,
                },
            },
        },
        "additionalProperties": False,
    }


def _strings(value: Any, label: str) -> tuple[str, ...]:
    """Require a non-empty list of strings from the upstream session context.

    Args:
        value: Raw context value.
        label: Context key named in the error.

    Returns:
        The validated strings.

    Raises:
        TypeError: When the value is not a non-empty list of strings.
    """
    if not isinstance(value, list) or not value or any(not isinstance(item, str) for item in value):
        raise TypeError(f"App prompt context {label} must be a non-empty list of strings")
    return tuple(value)


def _source_options(value: Any, candidate_ids: Sequence[str]) -> dict[str, tuple[str, ...]]:
    """Validate the component source options upstream offers for composition.

    Args:
        value: Raw ``component_source_options`` context value.
        candidate_ids: Accepted candidate identities.

    Returns:
        Component names mapped to eligible source candidates.

    Raises:
        ValueError: When a source names a candidate outside the accepted set.
    """
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError("component_source_options must be an object")
    options: dict[str, tuple[str, ...]] = {}
    for name, raw_sources in value.items():
        sources = _strings(raw_sources, f"component_source_options[{name!r}]")
        if any(source not in candidate_ids for source in sources):
            raise ValueError(f"component_source_options[{name!r}] contains an unknown candidate")
        options[str(name)] = sources
    return options


def _shared_asset(relative_path: str, asset_id: str) -> Any:
    """Load one upstream methodology asset.

    Args:
        relative_path: Path below the upstream prompting package.
        asset_id: Stable identity recorded in provenance.

    Returns:
        Upstream prompt asset.
    """
    return as_assets.load_prompt_asset(
        root=Path(as_assets.__file__).resolve().parent,
        relative_path=relative_path,
        asset_id=asset_id,
        source=f"shared/{relative_path.removeprefix('methodology/')}",
    )


def _plugin_asset(relative_path: str, asset_id: str) -> Any:
    """Load one app plugin asset.

    Args:
        relative_path: Path below the plugin directory.
        asset_id: Stable identity recorded in provenance.

    Returns:
        Upstream prompt asset.
    """
    return as_assets.load_prompt_asset(
        root=_PLUGIN_ROOT,
        relative_path=relative_path,
        asset_id=asset_id,
        source=f"plugins/{_PLUGIN_NAME}/{relative_path}",
    )


def resolved_assets(kind: str, skill_paths: Mapping[str, str | None], prefix: str = "") -> Any:
    """Compose upstream methodology with the app plugin for one session kind.

    Args:
        kind: Upstream session kind.
        skill_paths: Skill names mapped to optional plugin skill documents.
        prefix: Plugin subfolder holding this run's prompts, empty for text candidates.

    Returns:
        Resolved system context, task prompt and skills with provenance.
    """
    shared_skills = {
        "history-analysis": "methodology/skills/history-analysis/SKILL.md",
        "diagnose": "methodology/skills/causal-diagnosis/SKILL.md",
        "patch-verification": "methodology/skills/verification-baseline/SKILL.md",
    }
    method = "diagnose" if kind == "diagnose_patch" else kind
    return as_assets.resolve_prompt_composition(
        as_assets.PromptComposition(
            system_assets=(
                _shared_asset("methodology/system/optimizer-invariants.md", "methodology.system.invariants"),
                _plugin_asset(f"{prefix}SYSTEM.md", f"{_PLUGIN_NAME}.system"),
            ),
            task_assets=(
                _shared_asset(f"methodology/prompts/{method}-method.md", f"methodology.prompt.{kind}"),
                _plugin_asset(f"{prefix}prompts/{kind}.md", f"{_PLUGIN_NAME}.prompt.{kind}"),
            ),
            skill_assets={
                name: (
                    *(
                        (_shared_asset(shared_skills[name], f"methodology.skill.{name}"),)
                        if name in shared_skills
                        else ()
                    ),
                    *((_plugin_asset(f"{prefix}{path}", f"{_PLUGIN_NAME}.skill.{name}"),) if path is not None else ()),
                )
                for name, path in skill_paths.items()
            },
        )
    )


_DIAGNOSE_SKILLS: dict[str, str | None] = {
    "history-analysis": None,
    "diagnose": None,
    "candidate-patch": "skills/candidate-patch/SKILL.md",
    "patch-verification": "skills/patch-verification/SKILL.md",
}
_ANALYSIS_SKILLS: dict[str, str | None] = {"history-analysis": None}


def composition_record(prefix: str = "") -> Any:
    """Record every prompt composition the app plugin can produce.

    Args:
        prefix: Plugin subfolder holding this run's prompts, empty for text candidates.

    Returns:
        Upstream composition entity for the run store.
    """
    return as_assets.prompt_composition_record(
        plugin_name=_PLUGIN_NAME,
        compositions={
            "evolve": resolved_assets("evolve", _ANALYSIS_SKILLS, prefix),
            "diagnose_patch": resolved_assets("diagnose_patch", _DIAGNOSE_SKILLS, prefix),
            "reflect": resolved_assets("reflect", _ANALYSIS_SKILLS, prefix),
        },
    )


class AppPromptPack:
    """Render upstream sessions with the app objective and candidate schema."""

    def __init__(
        self,
        *,
        store: Any,
        component_names: Sequence[str],
        objective: str | None,
        background: str | None,
        readonly_paths: Sequence[str] | None = None,
        run_failed_candidates: set[str] | None = None,
    ) -> None:
        """Bind the prompt pack to the run store and task framing.

        Args:
            store: Upstream run store used for history bundles and evidence.
            component_names: Frozen candidate component schema, or a repository's editable paths.
            objective: User objective text.
            background: User background notes.
            readonly_paths: Paths that stay as fetched; set only for a repository run.
            run_failed_candidates: The evaluator's live set of versions with a
                failed ``[run]`` case in development.
        """
        self.store = store
        self.run_failed_candidates = set() if run_failed_candidates is None else run_failed_candidates
        self.component_names = tuple(component_names)
        self.objective = objective
        self.background = background
        self.readonly_paths = None if readonly_paths is None else tuple(readonly_paths)
        self.prefix = "" if readonly_paths is None else _REPO_ASSETS

    def session(self, kind: str, context: Mapping[str, Any]) -> Any:
        """Build the session specification for one upstream session kind.

        Args:
            kind: ``evolve``, ``diagnose_patch`` or ``reflect``.
            context: Upstream session context.

        Returns:
            Upstream session specification.

        Raises:
            ValueError: For an unknown session kind.
        """
        rendered = {
            **context,
            "objective": self.objective,
            "background": self.background,
            "mutation_scope": list(self.component_names),
            **({} if self.readonly_paths is None else {"readonly_paths": list(self.readonly_paths)}),
        }
        workspace_files = _history_files(self.store, context)
        mutation_label = None
        if kind == "diagnose_patch":
            workspace_files[_TRAINING_EVIDENCE_PATH] = self._evidence(context.get("evidence"))
            schema = (
                _diagnosis_schema(self.component_names) if self.readonly_paths is None else _repo_diagnosis_schema()
            )
            skill_paths = _DIAGNOSE_SKILLS
            mutation_label = "candidate-patch"
        elif kind == "evolve":
            candidate_ids = _strings(context.get("candidate_ids"), "candidate_ids")
            options = _source_options(context.get("component_source_options"), candidate_ids)
            # Upstream accepts on the training batch only, so a version with a
            # failed [run] case in development stays in its accepted list. Only
            # the parents the agent may name exclude it (SKY-751); a metric
            # error keeps its version. When every accepted version failed a
            # [run] case, the search keeps them all.
            parents = [item for item in candidate_ids if item not in self.run_failed_candidates] or list(candidate_ids)
            options = {
                name: kept
                for name, sources in options.items()
                if (kept := [item for item in sources if item in parents])
            }
            rendered["candidate_ids"], rendered["component_source_options"] = parents, options
            schema = _evolve_schema(parents, options)
            skill_paths = _ANALYSIS_SKILLS
        elif kind == "reflect":
            schema = _reflection_schema()
            skill_paths = _ANALYSIS_SKILLS
        else:
            raise ValueError(f"Unknown app session kind: {kind}")
        # The context holds upstream's raw aggregates, which can hold the floor.
        workspace_files[_SESSION_CONTEXT_PATH] = as_domain.canonical_json(_without_floor(rendered)) + "\n"
        resolved = resolved_assets(kind, skill_paths, self.prefix)
        workspace_files[_PROMPT_ASSETS_PATH] = (
            as_domain.canonical_json(
                {
                    "schema_version": "autosaddler-prompt-assets/v1",
                    "assets": [
                        {
                            "asset_id": asset.asset_id,
                            "version": asset.version,
                            "source": asset.source,
                            "sha256": asset.sha256,
                            "bytes": asset.bytes,
                        }
                        for asset in resolved.provenance
                    ],
                }
            )
            + "\n"
        )
        return SessionSpec(
            kind=kind,
            system_context=resolved.system_context,
            task_prompt=resolved.task_prompt,
            skills=resolved.skills,
            output_schema=schema,
            workspace_files=workspace_files,
            capabilities=_CAPABILITIES,
            mutation_label=mutation_label,
        )

    def _evidence(self, artifact: Any) -> str:
        """Load digest-verified training evidence for a diagnosis session.

        Args:
            artifact: Evidence artifact record from the upstream context.

        Returns:
            Evidence document text.

        Raises:
            ValueError: When the evidence drifted or carries the wrong schema.
        """
        if not isinstance(artifact, Mapping) or not isinstance(artifact.get("uri"), str):
            raise TypeError("App diagnosis context requires an evidence artifact")
        payload = (self.store.run_dir / artifact["uri"]).read_bytes()
        if as_domain.sha256_digest(payload) != artifact.get("sha256"):
            raise ValueError("App training evidence digest drift")
        value = json.loads(payload)
        if not isinstance(value, dict) or value.get("schema_version") != _EVIDENCE_SCHEMA:
            raise ValueError("App training evidence schema is invalid")
        return payload.decode("utf-8")


class StepTransport:
    """Run one rendered AutoSaddler session as a model tool loop: each model reply and command is one step.

    This is the tool loop of Cloudflare's Agents SDK: the model asks for a
    tool, the host runs it and sends the result back, until the model answers
    with text. File tools act on the local session workspace and are no steps.
    A command runs in a box over a copy of the workspace.
    """

    def __init__(self, channel: WorkerChannel, root: Path, base_tree_id: str | None, repo_base: str | None) -> None:
        """Bind the transport to the worker and to the run's trees.

        Args:
            channel: Worker connection.
            root: This process's folder, which the model sees as ``/work``.
            base_tree_id: The seed tree a command workspace starts from, ``None`` for text runs.
            repo_base: The base commit of a repository run, whose tree is ``base_tree_id``.
        """
        self.channel = channel
        self.root = root
        self.base_tree_id = base_tree_id
        self.repo_base = repo_base

    async def run(self, session: Any, timeout_seconds: float) -> Any:
        """Run the session's tool loop and report its cost.

        The instruction file the renderer wrote carries the system context.
        The engine loop bounds each step, so this transport sets no clock
        deadline: a deadline would make a resume differ from its run. Upstream
        still wraps this call in ``asyncio.wait_for``, and a timeout there
        would leave the thread on the channel, so ``execute_worker`` gives
        upstream a session timeout that no run reaches.

        Args:
            session: Rendered session with workspace, prompt and identifiers.
            timeout_seconds: Not used; see above.

        Returns:
            Transport outcome whose raw response is the model's final text.
        """
        return await asyncio.to_thread(self.converse, Path(session.workspace), str(session.task_prompt))

    def converse(self, workspace: Path, task_prompt: str) -> Any:
        """Call the model until it answers with no tool call, or until the turn limit.

        Args:
            workspace: The session workspace.
            task_prompt: The session's task prompt.

        Returns:
            Transport outcome with the final text and the session's cost.
        """
        instructions = workspace / _INSTRUCTION_FILE
        system = instructions.read_text(encoding="utf-8") if instructions.is_file() else ""
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": scrub(system, self.root)},
            {"role": "user", "content": scrub(task_prompt, self.root)},
        ]
        cost_cents = Decimal(0)
        for _ in range(_MAX_TURNS):
            reply = self.channel.call("model", prompt=messages, tools=_TOOLS)
            if "error" in reply:
                # A failed call can still be billed, and its step committed that cost.
                cost_cents += Decimal(str(reply.get("cost_cents", "0")))
                return self._outcome(f"The model call failed: {reply['error']}", cost_cents, failed=True)
            cost_cents += Decimal(str(reply["cost_cents"]))
            calls = list(reply.get("tool_calls") or [])
            if not calls:
                return self._outcome(str(reply["text"]), cost_cents, failed=False)
            messages.append(
                {"role": "assistant", "content": reply["text"], "tool_calls": [_openai_call(call) for call in calls]}
            )
            messages.extend(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "content": clip(
                        call.get("name"),
                        scrub(self.tool(workspace, call), self.root, times=False),
                    ),
                }
                for call in calls
            )
        return self._outcome(f"The session reached its limit of {_MAX_TURNS} model turns.", cost_cents, failed=True)

    def tool(self, workspace: Path, call: Mapping[str, Any]) -> str:
        """Run one tool call; a bad call becomes an error text for the model, not a failure.

        Args:
            workspace: The session workspace.
            call: The engine loop's flat ``{"id", "name", "arguments"}``; the arguments are an object, or the raw
                text of arguments that are no JSON object.

        Returns:
            The tool's output.
        """
        name = call.get("name")
        try:
            arguments = call.get("arguments")
            if not isinstance(arguments, dict):
                raise TypeError("the tool arguments must be a JSON object")
            if name == "list_files":
                folder = _inside(workspace, str(arguments.get("path") or "."))
                return "\n".join(
                    path.relative_to(workspace).as_posix()
                    for path in sorted(folder.rglob("*"))
                    if path.is_file() and not _in_git(path.relative_to(workspace))
                )
            if name == "read_file":
                return _inside(workspace, str(arguments["path"])).read_text(encoding="utf-8", errors="replace")
            if name == "write_file":
                target = _inside(workspace, str(arguments["path"]))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(str(arguments["content"]), encoding="utf-8")
                return f"Wrote {target.relative_to(workspace).as_posix()}."
            if name == "run_command":
                tree_id = self.channel.put_files(self.workspace_files(workspace), self.base_tree_id)
                command = str(arguments["command"])
                deleted = self.deleted_paths(workspace)
                if deleted:
                    # The box starts from the seed tree, and a tree write can not delete a file.
                    command = f"rm -f -- {' '.join(shlex.quote(path) for path in deleted)}\n{command}"
                result = self.channel.call("command", tree_id=tree_id, command=command)
                if "error" in result:
                    return f"Error: the box failed: {result['error']}"
                return (
                    f"exit code {result['exit_code']}{' (timed out)' if result['timed_out'] else ''}\n"
                    f"stdout:\n{result['stdout']}\nstderr:\n{result['stderr']}"
                )
        except (KeyError, TypeError, ValueError, OSError) as error:
            return f"Error: {type(error).__name__}: {error}"
        return f"Error: unknown tool {name!r}."

    def workspace_files(self, workspace: Path) -> dict[str, bytes]:
        """Collect the workspace files a command box needs over the base tree.

        In a repository run the box starts from the seed tree, so only the
        files that differ from the base commit go up.

        Args:
            workspace: The session workspace.

        Returns:
            The file bytes by workspace-relative path.
        """
        if self.repo_base is not None and (workspace / ".git").exists():
            changed = _git_output(workspace, "diff", "--name-only", "--no-renames", "-z", self.repo_base).split("\0")
            changed += _git_output(workspace, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
            paths = sorted({workspace / name for name in changed if name})
        else:
            paths = sorted(path for path in workspace.rglob("*") if not _in_git(path.relative_to(workspace)))
        return {
            path.relative_to(workspace).as_posix(): path.read_bytes()
            for path in paths
            if path.is_file() and not path.is_symlink()
        }

    def deleted_paths(self, workspace: Path) -> list[str]:
        """List the files that the workspace deleted from the seed of a repository run.

        Args:
            workspace: The session workspace.

        Returns:
            The workspace-relative paths, sorted; empty for a text run.
        """
        if self.repo_base is None or not (workspace / ".git").exists():
            return []
        output = _git_output(workspace, "diff", "--name-only", "--no-renames", "--diff-filter=D", "-z", self.repo_base)
        return sorted(name for name in output.split("\0") if name)

    def _outcome(self, text: str, cost_cents: Decimal, *, failed: bool) -> Any:
        """Build the upstream outcome of one session.

        Args:
            text: The final reply, or why the session failed.
            cost_cents: What the session's model steps cost.
            failed: Whether the session failed.

        Returns:
            The upstream transport outcome.
        """
        cost = float(cost_cents / 100) or None
        # Upstream's usage record refuses a cost with no unit; model step costs are in US dollars.
        usage = SessionUsage(
            input_tokens=0,
            output_tokens=0,
            model=_MODEL_LABEL,
            provider_cost=cost,
            provider_cost_unit="usd" if cost is not None else None,
            duration_seconds=0.0,
            status="failed" if failed else "success",
            error_type="ModelSession" if failed else None,
            usage_incomplete=False,
        )
        return TransportOutcome(raw_response=text, usage=(usage,))


def clip(name: Any, text: str) -> str:
    """Cut a long tool output to the model's limit, and say what was cut.

    A file read keeps its start, so the model knows that it does not have the whole file. A command keeps its
    end, where the result and the errors are.

    Args:
        name: The tool name.
        text: The tool output.

    Returns:
        The text, with a notice line when it was cut.
    """
    cut = len(text) - _TOOL_OUTPUT_CHARS
    if cut <= 0:
        return text
    if name == "read_file":
        return (
            f"{text[:_TOOL_OUTPUT_CHARS]}\n[The file goes on: {cut} more characters were cut. Do not write this "
            "file back from this text.]"
        )
    return f"[The first {cut} characters were cut.]\n{text[-_TOOL_OUTPUT_CHARS:]}"


def _in_git(path: Path) -> bool:
    """Tell whether a workspace-relative path is in, or is, a ``.git`` entry.

    A case-insensitive disk opens ``.GIT`` as ``.git``, so a write there could point git at a model-made config.

    Args:
        path: The workspace-relative path.

    Returns:
        Whether any part of the path is ``.git`` in any case.
    """
    return any(part.casefold() == ".git" for part in path.parts)


def _inside(workspace: Path, path: str) -> Path:
    """Resolve a model-given path, and refuse one outside the workspace or in ``.git``.

    Args:
        workspace: The session workspace.
        path: The workspace-relative path; ``/work/...`` is read as relative too.

    Returns:
        The absolute path.

    Raises:
        ValueError: When the path leaves the workspace or enters ``.git``.
    """
    base = workspace.resolve()
    target = (base / path.removeprefix(_WORK_ROOT + "/").lstrip("/")).resolve()
    if not target.is_relative_to(base) or _in_git(target.relative_to(base)):
        raise ValueError(f"The path {path!r} is outside the workspace.")
    return target


def step_provider(channel: WorkerChannel, root: Path, base_tree_id: str | None, repo_base: str | None) -> Any:
    """Build the upstream provider whose sessions run as engine-loop steps.

    Args:
        channel: Worker connection.
        root: This process's folder.
        base_tree_id: The seed tree of a repository run, else ``None``.
        repo_base: The base commit of a repository run, else ``None``.

    Returns:
        A ``BaseAgentProvider`` rendering the instruction file the transport reads.
    """
    renderer = WorkspaceRenderer(
        provider=_PLUGIN_NAME,
        instruction_file=_INSTRUCTION_FILE,
        skill_directory=_SKILL_DIRECTORY,
        capability_tools=_CAPABILITY_TOOLS,
    )
    return BaseAgentProvider(renderer, StepTransport(channel, root, base_tree_id, repo_base))


class HarnessTransport:
    """Run one rendered AutoSaddler session through an app agent harness."""

    def __init__(
        self, proposer: dict[str, Any], model: str, on_refusal: Callable[[dict[str, str]], None] | None = None
    ) -> None:
        """Remember the launch to replay for every session.

        Args:
            proposer: Serialized harness launch from the parent payload.
            model: Model identifier the harness routes to.
            on_refusal: Receives each hidden-path refusal of a session.
        """
        self.proposer = proposer
        self.model = model
        self.on_refusal = on_refusal

    async def run(self, session: Any, timeout_seconds: float) -> Any:
        """Drive the harness in the rendered workspace and report what it used.

        The instruction file the renderer wrote already carries the system
        context, so the harness only receives the task prompt.

        Args:
            session: Rendered session with workspace, prompt and identifiers.
            timeout_seconds: Session deadline enforced on the harness process.

        Returns:
            Transport outcome whose raw response is the harness's final message.

        Raises:
            TimeoutError: When the harness did not finish within the deadline.
        """
        outcome = await asyncio.to_thread(
            harness_bridge.run_session,
            self.proposer,
            workspace=Path(session.workspace),
            prompt=session.task_prompt,
            model=self.model,
            session_dir=Path.home() / harness_bridge.SESSIONS_DIR / str(session.session_id),
            timeout_seconds=timeout_seconds,
        )
        if self.on_refusal is not None:
            for refusal in outcome.refusals:
                self.on_refusal(refusal)
        if outcome.timed_out:
            raise TimeoutError(f"Harness session {session.session_id} exceeded {timeout_seconds:.0f}s")
        failed = outcome.failed
        cost = outcome.cost_usd or None
        # Upstream's usage record refuses a cost with no unit; harness session costs are in US dollars.
        usage = SessionUsage(
            input_tokens=outcome.usage.get("input_tokens", 0),
            output_tokens=outcome.usage.get("output_tokens", 0),
            model=self.model,
            provider_cost=cost,
            provider_cost_unit="usd" if cost is not None else None,
            duration_seconds=outcome.duration_seconds,
            status="failed" if failed else "success",
            error_type="HarnessExit" if failed else None,
            usage_incomplete=not outcome.usage,
        )
        detail = harness_bridge.failure_detail(outcome)
        raw = outcome.text if outcome.text is not None else f"Harness exited with {outcome.returncode}: {detail}"
        return TransportOutcome(raw_response=raw, usage=(usage,))


def harness_provider(
    proposer: dict[str, Any], model: str, on_refusal: Callable[[dict[str, str]], None] | None = None
) -> Any:
    """Build the upstream provider for the proposer harness.

    Args:
        proposer: Serialized harness launch from the parent payload.
        model: Model identifier the harness routes to.
        on_refusal: Receives each hidden-path refusal of a session.

    Returns:
        A ``BaseAgentProvider`` rendering the harness's instruction file.
    """
    renderer = WorkspaceRenderer(
        provider=str(proposer["harness"]),
        instruction_file=str(proposer.get("instructions_file") or "AGENTS.md"),
        skill_directory=_SKILL_DIRECTORY,
        capability_tools=_HARNESS_CAPABILITY_TOOLS,
    )
    return BaseAgentProvider(renderer, HarnessTransport(proposer, model, on_refusal))


class UsageTrackingProvider:
    """Accumulate per-model token usage from every upstream agent session."""

    def __init__(self, inner: Any, model: str) -> None:
        """Wrap the upstream provider.

        Args:
            inner: Upstream agent provider.
            model: Configured model, used when a session omits its model id.
        """
        self.inner = inner
        self.model = model
        self.usage_by_model: dict[str, dict[str, int]] = {}
        self.sessions = 0
        self.incomplete = False

    async def run(self, request: Any) -> Any:
        """Run one session and record what it consumed.

        Args:
            request: Upstream session request.

        Returns:
            Upstream session result.
        """
        self.sessions += 1
        try:
            result = await self.inner.run(request)
        except BaseException:
            self.incomplete = True
            raise
        if not result.usage and result.status != "completed":
            self.incomplete = True
        for usage in result.usage:
            if getattr(usage, "usage_incomplete", False):
                self.incomplete = True
            record_usage(self.usage_by_model, usage, self.model)
        return result


def record_usage(destination: dict[str, dict[str, int]], usage: Any, model: str) -> None:
    """Fold one upstream usage record into the app's per-model token counters.

    Args:
        destination: Per-model cumulative usage.
        usage: Upstream usage record.
        model: Fallback model identifier.
    """
    metadata = getattr(usage, "provider_metadata", None) or {}
    current = destination.setdefault(str(getattr(usage, "model", None) or model), dict.fromkeys(_TOKEN_NAMES, 0))
    current["prompt_tokens"] += int(getattr(usage, "input_tokens", 0) or 0)
    current["completion_tokens"] += int(getattr(usage, "output_tokens", 0) or 0)
    current["cache_read_input_tokens"] += int(getattr(usage, "cached_input_tokens", 0) or 0)
    current["cache_creation_input_tokens"] += int(metadata.get("cache_creation_input_tokens", 0) or 0)
    current["total_tokens"] = sum(current[name] for name in _TOKEN_NAMES)


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
    """Stop agent sessions after a parent evaluator failure or deadline."""
    for pid in _descendants():
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


def _archive_artifacts(paths: list[tuple[str, Path]]) -> None:
    """Preserve regular run files within a bounded artifact archive.

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
    """Replace unsupported nonfinite scores while preserving metadata.

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


def _incumbent(store: Any, evaluator: AppEvaluator, seed: Any) -> dict[str, Any]:
    """Recover the candidate to report when the run stops before selecting.

    Args:
        store: Upstream run store holding the event log.
        evaluator: Evaluator that saw every development aggregate.
        seed: Upstream seed candidate.

    Returns:
        Best development-scored candidate, else the unscored seed.
    """
    if evaluator.best_development is not None:
        return {
            "best_candidate": evaluator.best_development["best_candidate"],
            "best_score": evaluator.best_development["best_score"],
        }
    with contextlib.suppress(Exception):
        state = RunState.replay(store.events())
        if state.accepted_candidate_ids:
            candidate = state.candidates[state.accepted_candidate_ids[-1]]
            return {"best_candidate": evaluator.value_of(candidate), "best_score": None}
    return {"best_candidate": evaluator.value_of(seed), "best_score": None}


def execute(payload: dict[str, Any]) -> dict[str, Any]:
    """Run the pinned upstream engine once and describe the outcome.

    Args:
        payload: Parent configuration, task and evaluator framing token.

    Returns:
        Result envelope, or a failure envelope retaining available usage.

    Raises:
        ValueError: When the payload targets another engine or leaks test examples.
    """
    if payload.get("engine_id") != "autosaddler":
        raise ValueError("Unsupported native optimizer.")
    task = payload.get("task", {})
    if task.get("test_set") is not None:
        raise ValueError("Held-out examples must not enter the native optimizer.")
    if AutoSaddlerEngine is None:
        raise RuntimeError("The pinned upstream autosaddler package is not importable in this runtime.")
    model = str(payload["model"])
    timeout = float(payload["timeout_seconds"])
    repo = payload.get("repo")
    proposer = payload["proposer"]
    examples = visible_examples(task.get("train_set"))
    train_cases = build_cases("train", examples)
    development_cases = build_cases("development", examples)
    run_dir = Path("autosaddler-run").resolve()
    store = LocalRunStore(run_dir=run_dir, run_id=f"app-{payload['nonce'][:12]}")
    mailbox = EvaluatorMailbox(payload["nonce"], timeout)
    if repo is None:
        components = baseline_components(task.get("seed_candidate"))
        harness_space: Any = ComponentMapHarnessSpace(
            baseline=components,
            store_root=run_dir / "candidates",
            materialization_root=run_dir / "materialized",
        )
        repo_base = None
        component_names = tuple(components)
        readonly_paths = None
    else:
        harness_space, repo_base = repo_space(repo, task.get("seed_candidate"), run_dir, proposer, mailbox)
        component_names = tuple(str(path) for path in repo["editable_paths"])
        readonly_paths = tuple(str(path) for path in repo["readonly_paths"])
    prefix = "" if repo is None else _REPO_ASSETS
    evaluator = AppEvaluator(
        mailbox=mailbox,
        harness_space=harness_space,
        store_run_dir=run_dir,
        max_concurrency=int(payload.get("max_concurrency", 1)),
        stop_at_score=payload.get("stop_at_score"),
        fingerprint=as_domain.sha256_digest(as_domain.canonical_json({"evaluator": "app-parent-scorer"})),
        development_case_ids=[case.case_id for case in development_cases],
        repo_base=repo_base,
    )
    scenario = as_ports.ScenarioComponents(
        name=_PLUGIN_NAME,
        version="1",
        harness_space=harness_space,
        evaluator=evaluator,
        evidence_builder=AppEvidenceBuilder(store, {case.case_id: case for case in train_cases}),
        prompt_pack=AppPromptPack(
            store=store,
            component_names=component_names,
            objective=task.get("objective"),
            background=task.get("background"),
            readonly_paths=readonly_paths,
            run_failed_candidates=evaluator.run_failed_candidates,
        ),
        train_cases=train_cases,
        development_cases=development_cases,
        required_capabilities=_CAPABILITIES,
        resolved_entities={
            **as_assets.prompt_source_entities(plugin_root=_PLUGIN_ROOT, plugin_name=_PLUGIN_NAME),
            "resolved/prompts/compositions.json": composition_record(prefix),
            "resolved/scenario_runtime.json": {
                "schema_version": "app-autosaddler-scenario/v1",
                "components": list(component_names),
                "train_case_ids": [case.case_id for case in train_cases],
                "development_case_ids": [case.case_id for case in development_cases],
            },
        },
    )
    provider = UsageTrackingProvider(harness_provider(proposer, model, refusal_logger(mailbox)), model)
    max_evals = int(payload["max_evals"])
    max_iterations = payload.get("max_iterations") or max_evals
    policies = as_policies.PolicyBundle(
        task_selection=as_policies.FixedTaskSelectionPolicy(batch_size=min(2, len(train_cases))),
        acceptance=NoScoreRejectedAcceptance(as_policies.MatchedValidStrictImprovement()),
        development=as_policies.FullOnAcceptDevelopment(),
        ranking=NoScoreLastRanking(as_policies.MeanDevelopmentRanking()),
        budget=as_policies.BudgetPolicy(max_rollouts=max_evals, max_iterations=int(max_iterations)),
    )
    store.initialize(
        resolved_config={
            "schema_version": "app-autosaddler-run/v1",
            "engine": "autosaddler",
            "source": payload.get("source"),
            "provider": {"type": proposer["harness"], "model": model},
            "optimization": {
                "task_selection": {"type": "fixed", "batch_size": policies.task_selection.batch_size},
                "acceptance": {"type": "no_score_rejected", "inner": "matched_valid_strict_improvement"},
                "development": {"type": "full_on_accept"},
                "ranking": {"type": "no_score_last", "inner": "mean_development"},
                "budget": {"max_rollouts": max_evals, "max_iterations": int(max_iterations)},
                "stop_at_score": payload.get("stop_at_score"),
            },
        },
        resolved_entities=scenario.resolved_entities,
    )
    engine = AutoSaddlerEngine(store=store, scenario=scenario, provider=provider, policies=policies)
    seed = harness_space.seed()
    document: dict[str, Any] = {}
    finished = threading.Event()

    def optimize() -> None:
        """Run upstream on a supervised thread so evaluator failures cannot spawn retries."""
        try:
            result = engine.run()
            selected = store.read_json("result.json")
            no_score = result.selected_candidate_id in evaluator.no_score_candidates
            if no_score and isinstance(selected, dict):
                selected = {**selected, "development_score": None}
            document.update(
                {
                    "best_candidate": evaluator.value_of(_candidate(store, result.selected_candidate_id)),
                    # Upstream's score for that version holds the no-score floor (SKY-751).
                    "best_score": None if no_score else result.development_score,
                    "total_evals": mailbox.total_evals,
                    "metadata": {
                        "autosaddler_run_id": result.run_id,
                        "selected_candidate_id": result.selected_candidate_id,
                        "iterations": result.iterations,
                        "selection": selected if isinstance(selected, dict) else None,
                    },
                }
            )
        except TargetReached as exc:
            document.update(evaluator.best_development or {})
            document["total_evals"] = mailbox.total_evals
            document["metadata"] = {"stop_reason": "target_reached", "detail": str(exc)}
        except (Exception, EvaluationStopped) as exc:
            # The parent keeps stderr, so the traceback survives with the envelope.
            traceback.print_exc()
            document["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            finished.set()

    worker = threading.Thread(target=optimize, daemon=True)
    worker.start()
    deadline = time.monotonic() + max(0.1, timeout - min(10.0, timeout * 0.1))
    while not finished.wait(0.05):
        if mailbox.stopped.is_set() or time.monotonic() >= deadline:
            document["error"] = str(mailbox.error or "Native optimizer exceeded its runtime limit.")
            mailbox.stopped.set()
            _stop_children()
            worker.join(timeout=1.0)
            break
    if mailbox.error is not None:
        document["error"] = str(mailbox.error)
    if isinstance(mailbox.error, BudgetStopped):
        document.pop("error", None)
        document.pop("best_candidate", None)
        document.pop("best_score", None)
        document["stop_reason"] = "budget_reached"
        document.update(_incumbent(store, evaluator, seed))
    if document.get("error"):
        document["interrupted_incumbent"] = _incumbent(store, evaluator, seed)
    document["total_evals"] = mailbox.total_evals
    document["usage_by_model"] = provider.usage_by_model
    document["usage_complete"] = not provider.incomplete and (bool(provider.usage_by_model) or provider.sessions == 0)
    try:
        _archive_artifacts([("upstream", run_dir), ("sessions", Path.home() / ".claude/projects")])
    except Exception as exc:
        document["error"] = f"Could not preserve native artifacts: {exc}"
    return _json_finite(document)


def execute_worker(payload: dict[str, Any], channel: WorkerChannel) -> dict[str, Any]:
    """Run the pinned upstream engine once in the worker, one step at a time, and describe the outcome.

    Args:
        payload: Worker configuration and task.
        channel: Worker connection.

    Returns:
        Result envelope, or a failure envelope.

    Raises:
        ValueError: When the payload targets another engine or leaks test examples.
        RuntimeError: When upstream is not importable in this interpreter.
    """
    if payload.get("engine_id") != "autosaddler":
        raise ValueError("Unsupported native optimizer.")
    task = payload.get("task", {})
    if task.get("test_set") is not None:
        raise ValueError("Held-out examples must not enter the native optimizer.")
    if AutoSaddlerEngine is None:
        raise RuntimeError("The pinned upstream autosaddler package is not importable in this runtime.")
    root = Path.cwd().resolve()
    repo = payload.get("repo")
    examples = visible_examples(task.get("train_set"))
    train_cases = build_cases("train", examples)
    development_cases = build_cases("development", examples)
    run_dir = root / "autosaddler-run"
    # A fixed run id: it reaches the prompts, and a resume must send the same prompts.
    store = LocalRunStore(run_dir=run_dir, run_id="app-run")
    if repo is None:
        components = baseline_components(task.get("seed_candidate"))
        harness_space: Any = ComponentMapHarnessSpace(
            baseline=components,
            store_root=run_dir / "candidates",
            materialization_root=run_dir / "materialized",
        )
        repo_base = None
        component_names = tuple(components)
        readonly_paths = None
    else:
        harness_space, repo_base = repo_space(repo, task.get("seed_candidate"), run_dir, None, channel)
        component_names = tuple(str(path) for path in repo["editable_paths"])
        readonly_paths = tuple(str(path) for path in repo["readonly_paths"])
    prefix = "" if repo is None else _REPO_ASSETS
    evaluator = AppEvaluator(
        mailbox=channel,
        harness_space=harness_space,
        store_run_dir=run_dir,
        # One evaluation at a time: a step's place in the log must not depend on thread timing.
        max_concurrency=1,
        stop_at_score=payload.get("stop_at_score"),
        fingerprint=as_domain.sha256_digest(as_domain.canonical_json({"evaluator": "app-parent-scorer"})),
        development_case_ids=[case.case_id for case in development_cases],
        repo_base=repo_base,
    )
    scenario = as_ports.ScenarioComponents(
        name=_PLUGIN_NAME,
        version="1",
        harness_space=harness_space,
        evaluator=evaluator,
        evidence_builder=AppEvidenceBuilder(store, {case.case_id: case for case in train_cases}),
        prompt_pack=AppPromptPack(
            store=store,
            component_names=component_names,
            objective=task.get("objective"),
            background=task.get("background"),
            readonly_paths=readonly_paths,
            run_failed_candidates=evaluator.run_failed_candidates,
        ),
        train_cases=train_cases,
        development_cases=development_cases,
        required_capabilities=_CAPABILITIES,
        resolved_entities={
            **as_assets.prompt_source_entities(plugin_root=_PLUGIN_ROOT, plugin_name=_PLUGIN_NAME),
            "resolved/prompts/compositions.json": composition_record(prefix),
            "resolved/scenario_runtime.json": {
                "schema_version": "app-autosaddler-scenario/v1",
                "components": list(component_names),
                "train_case_ids": [case.case_id for case in train_cases],
                "development_case_ids": [case.case_id for case in development_cases],
            },
        },
    )
    provider = step_provider(channel, root, None if repo is None else str(repo["tree_id"]), repo_base)
    max_evals = int(payload["max_evals"])
    max_iterations = payload.get("max_iterations") or max_evals
    policies = as_policies.PolicyBundle(
        task_selection=as_policies.FixedTaskSelectionPolicy(batch_size=min(2, len(train_cases))),
        acceptance=NoScoreRejectedAcceptance(as_policies.MatchedValidStrictImprovement()),
        development=as_policies.FullOnAcceptDevelopment(),
        ranking=NoScoreLastRanking(as_policies.MeanDevelopmentRanking()),
        budget=as_policies.BudgetPolicy(max_rollouts=max_evals, max_iterations=int(max_iterations)),
    )
    store.initialize(
        resolved_config={
            "schema_version": "app-autosaddler-run/v1",
            "engine": "autosaddler",
            "provider": {"type": _PLUGIN_NAME, "model": _MODEL_LABEL},
            "optimization": {
                "task_selection": {"type": "fixed", "batch_size": policies.task_selection.batch_size},
                "acceptance": {"type": "no_score_rejected", "inner": "matched_valid_strict_improvement"},
                "development": {"type": "full_on_accept"},
                "ranking": {"type": "no_score_last", "inner": "mean_development"},
                "budget": {"max_rollouts": max_evals, "max_iterations": int(max_iterations)},
                "stop_at_score": payload.get("stop_at_score"),
            },
        },
        resolved_entities=scenario.resolved_entities,
    )
    engine = AutoSaddlerEngine(
        store=store,
        scenario=scenario,
        provider=provider,
        policies=policies,
        diagnosis_patch_timeout_seconds=_STEP_SESSION_TIMEOUT_SECONDS,
        selection_timeout_seconds=_STEP_SESSION_TIMEOUT_SECONDS,
        reflection_timeout_seconds=_STEP_SESSION_TIMEOUT_SECONDS,
    )
    seed = harness_space.seed()
    document: dict[str, Any] = {}
    try:
        result = engine.run()
        selected = store.read_json("result.json")
        selected_id, score = result.selected_candidate_id, result.development_score
        if selected_id in evaluator.no_score_candidates:
            # Upstream's score for that version holds the no-score floor (SKY-751).
            score = None
            # Upstream ranks a version with a metric error by its scored cases,
            # but a version with no score is never the final pick (SKY-751).
            # With no fully scored version, upstream's pick stays: the seed
            # finishes with no score, and any other version fails the run.
            if evaluator.best_development is not None:
                selected_id = evaluator.best_development["candidate_id"]
                score = evaluator.best_development["best_score"]
            if isinstance(selected, dict):
                selected = {**selected, "selected_candidate_id": selected_id, "development_score": score}
        document.update(
            {
                "best_candidate": evaluator.value_of(_candidate(store, selected_id)),
                "best_score": score,
                "metadata": {
                    "autosaddler_run_id": result.run_id,
                    "selected_candidate_id": selected_id,
                    "iterations": result.iterations,
                    "selection": selected if isinstance(selected, dict) else None,
                },
            }
        )
    except TargetReached as exc:
        document.update(evaluator.best_development or {})
        document["metadata"] = {"stop_reason": "target_reached", "detail": str(exc)}
    except BudgetStopped:
        document["stop_reason"] = "budget_reached"
        document.update(_incumbent(store, evaluator, seed))
    except (Exception, EvaluationStopped) as exc:
        # The worker keeps stderr in the run folder, so the traceback survives with the envelope.
        traceback.print_exc()
        document["error"] = f"{type(exc).__name__}: {exc}"
    document["total_evals"] = channel.total_evals
    return _json_finite(document)


def _candidate(store: Any, candidate_id: str) -> Any:
    """Look up a finalized candidate in the upstream event log.

    Args:
        store: Upstream run store.
        candidate_id: Selected candidate identity.

    Returns:
        The upstream candidate record.
    """
    return RunState.replay(store.events()).candidates[candidate_id]


def main() -> int:
    """Run one engine invocation: in the worker with ``--worker``, else in a box from its JSON input file.

    Returns:
        Zero for a completed run, one for a failure envelope.
    """
    if sys.argv[1:] == [WORKER_FLAG]:
        return worker_main()
    try:
        payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        result = execute(payload)
    except Exception as exc:
        result = {"error": f"{type(exc).__name__}: {exc}", "usage_by_model": {}, "usage_complete": False}
    Path("native_result.json").write_text(json.dumps(result, default=str, allow_nan=False), encoding="utf-8")
    return 1 if result.get("error") else 0


def worker_main() -> int:
    """Read the worker's payload line, run the engine, and send the result line.

    Returns:
        Zero for a completed run, one for a failure envelope.
    """
    channel = open_channel()
    seed_ids()
    fix_clocks()
    try:
        payload = json.loads(sys.stdin.readline())
        result = execute_worker(payload, channel)
    except Exception as exc:
        traceback.print_exc()
        result = {"error": f"{type(exc).__name__}: {exc}"}
    channel.writer.write(json.dumps({"op": "result", "result": result}, default=str, allow_nan=False) + "\n")
    channel.writer.flush()
    return 1 if result.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
