"""ShinkaEvolve engine: drive the pinned upstream loop from the worker's engine loop.

Skynet does not reimplement ShinkaEvolve. The upstream ``SakanaAI/ShinkaEvolve``
package (islands, parent selection, diff/full/crossover mutations, meta notes
and the model bandit) runs unchanged in ``shinka_runner.py``, in its own pinned
interpreter, isolated from Skynet's dependencies.

With the engine loop's context, the runner is a child process of the worker.
It sends each evaluation and each model call back to this engine as one request
line, and this engine answers them with the run's eval server and the loop's
``call_model``. Each model reply and each scorer command is one committed step,
so a resumed run replays the upstream run from its step log (SKY-724). The
child runs no candidate or repository code: the loop's boxes score every version.

When the contract file names a ``[proposer] entry`` (SKY-815), ShinkaEvolve keeps
its search, but the child asks this engine for each new version instead of its
model. The engine runs the command once in a proposer box over the parent's tree,
with ShinkaEvolve's mutation prompt in the ``ASCENT_TASK`` file, and the tree the
command leaves is the new version.

With the agent editor (SKY-830), the child asks this engine for each new version,
and the engine runs one tool session: each model reply is one ``model`` step and
each ``run`` tool call one ``command`` step. The ``[proposer] mcp`` servers come
only with the agent editor; their grants let the provider edge add their headers,
so no secret enters a box. Novelty rejection embeds programs in the child with a
fixed local embedder, and its judge's calls are model steps.

Without the loop's context, the engine runs the runner in the managed sandbox, as
before, until the cutover (SKY-728). On the loop, a worker image without the
ShinkaEvolve interpreter fails the run.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import IO, Any
from urllib.parse import urlsplit

from ....billing.model_gateway import MCP_RELAY_ROUTE_KEY
from ....exceptions import ServiceError
from ....i18n import t
from ....sandbox.grants import Grant, GrantError
from ....sandbox.trees import TreeRejectedError
from ....worker.engine_loop import ModelCallFailedError
from ..budget_stop import BudgetReached
from . import sandbox_log, shinka_bundle
from .feedback import emit_candidate, emit_case_scored, emit_scorer_feedback
from .native_runtime import (
    SHINKA_IMAGE_PYTHON,
    SHINKA_KEY_ENV,
    SHINKA_META_KEY_ENV,
    _candidate_shape_ok,
    _finite,
    assert_metric_absent,
    native_task_payload,
    run_native_engine,
    sanitize_side_info,
)
from .protocol import BudgetExhaustedError, EngineContext, EvalServer, Result, Task, ToolDefinition, candidate_key
from .runner import side_info_json_default

logger = logging.getLogger(__name__)

_RPC_PREFIX = "APP_NATIVE_RPC "
_PROGRESS_PREFIX = "APP_NATIVE_PROGRESS "
_LOG_PREFIX = "APP_NATIVE_LOG "
_RUNNER_FILE = "shinka_runner.py"
# The runner loads these as siblings when it runs as a script.
_RUNNER_FILES = (_RUNNER_FILE, "harness_bridge.py", "repo_tree.py", "shinka_bundle.py")
_INPUT_FILE = "native_input.json"
_RESULT_FILE = "native_result.json"
_REPO_CHUNK_DIR = "repo-tree"
_WORKDIR = "shinka_evolve"
# The proposer box reads the task from this file of the parent's tree. It is never part of a version.
_TASK_PATH = ".ascent/task.md"
# The most of a tool's output that the agent editor's model sees, so one call cannot fill its context.
_TOOL_OUTPUT_CHARS = 16000
# Room that a cut ``run`` output keeps for its note.
_RUN_NOTE_CHARS = 200
_AGENT_SYSTEM = (
    "You edit a program to improve its score. Use the tools to read and change its files. The run tool runs a "
    "shell command in a sandbox over your current files, and keeps the files the command writes. When you are "
    "done, reply with no tool call."
)
_PATH_SCHEMA = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
_AGENT_TOOLS: list[ToolDefinition] = [
    {
        "name": "list_files",
        "description": "List the files below a folder; an empty path is the root.",
        "input_schema": _PATH_SCHEMA,
    },
    {
        "name": "read_file",
        "description": (
            f"Read a text file. A file over {_TOOL_OUTPUT_CHARS} characters is cut, with a note at the end; "
            "read the rest with the run tool, for example with sed."
        ),
        "input_schema": _PATH_SCHEMA,
    },
    {
        "name": "write_file",
        "description": "Write a text file, in full. It replaces the whole file.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
    {
        "name": "run",
        "description": "Run a shell command over the files, and return its exit code and output.",
        "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
    },
]
# Only the end of a failed command's stderr goes back to upstream as the mutation's error.
_COMMAND_ERROR_CHARS = 2000
# The runner never calls this URL: its model calls come back to the worker. Upstream
# still needs a well-formed ``local/<model>@<url>`` name for its bandit.
_ROUTE_URL = "http://engine-loop.invalid/v1"
_ROUTE_LABEL = "engine-loop"
# The budget and the evaluation limit end a run; this only bounds a lost reply.
_TIMEOUT_SECONDS = 7 * 24 * 3600.0
# The child parses untrusted model replies, so a reply that makes a parser loop
# or grow must not take the worker host with it. Its own work is prompt building,
# parsing and a lineage poll that reads only new programs: a few hundred MiB and
# well under a second a generation, while it waits on the worker for nearly all
# of a run. The address-space limit leaves room for the thread stacks and
# malloc arenas that inflate the virtual size.
_CHILD_MEMORY_BYTES = 8 * 1024**3
_CHILD_CPU_SECONDS = 6 * 3600
# macOS refuses to lower the address-space limit; the workers run Linux.
_MEMORY_LIMITED = sys.platform == "linux"
# Between two lines the child only starts, builds prompts and parses replies;
# the worker scores versions and calls the model with no timer running.
_IDLE_SECONDS = 30 * 60.0
# A stopped child only reports its scored versions and writes its result.
_DRAIN_SECONDS = 60.0
# A real line holds one request or one scored version; a repository version is
# bounded well below this.
_LINE_CHARS = 64 * 1024**2
# Each file the child writes, its stderr log included, stops at this size, so a
# runaway writer cannot fill the worker's disk. Upstream's program database is
# the largest file of a real run and stays far below it.
_CHILD_FILE_BYTES = 4 * 1024**3
# Only the end of the stderr log goes into a failure message.
_STDERR_TAIL_BYTES = 64 * 1024
# ``preexec_fn`` is not safe in a parent with threads, such as the worker, so
# the child's first program sets its limits and then becomes the runner. The
# limits stay through ``exec``.
_LIMIT_THEN_RUN = (
    "import json, os, resource, sys\n"
    "for name, limit in json.loads(sys.argv[1]).items():\n"
    "    resource.setrlimit(getattr(resource, name), (limit, limit))\n"
    "os.execv(sys.executable, [sys.executable, *sys.argv[2:]])\n"
)


class ShinkaEvolveEngine:
    """Run upstream ShinkaEvolve on the engine loop when its context is present, else in the sandbox."""

    name = "shinka_evolve"

    def __init__(self, python: str = SHINKA_IMAGE_PYTHON) -> None:
        """Bind the engine to the interpreter that holds the pinned upstream package.

        Args:
            python: The ``shinka-evolve`` interpreter.
        """
        self.python = python

    def run(self, task: Task, server: EvalServer, ctx: EngineContext) -> Result:
        """Run the pinned upstream engine with Skynet's evaluator and model.

        On the engine loop, a resume replays the run from its start: upstream
        runs again, and the loop answers each request it already committed
        from its step log.

        Args:
            task: Seed candidate, objective and visible examples.
            server: Skynet evaluator and shared evaluation budget.
            ctx: Run context: the loop's model, run folder, seed and native options.

        Returns:
            The best version upstream found, or the best version scored before
            upstream failed or the scorer cap stopped it. The seed with no score
            when the child ends cleanly on the seed and no version has a full score.

        Raises:
            ServiceError: When the worker has no ShinkaEvolve interpreter, when the settings need a feature
                the worker does not run yet, when the child sends a malformed request or candidate, or when
                upstream fails, goes quiet or writes an overlong line before it scores a version.
            BudgetExhaustedError: When no scorer call remains, or the cap stops the run before a version is scored.
            BudgetReached: When the budget stops the run; it carries the best version scored so far.
        """
        options = ctx.native_options
        if ctx.call_model is None:
            if options is not None and (options.repo or {}).get("file_proposer"):
                # The sandbox runner has no command editor; it would mutate with the model and drop the command.
                raise ServiceError("ShinkaEvolve runs the [proposer] entry command only on the worker's engine loop.")
            return run_native_engine(self.name, task, server, ctx)
        # Only images built with INCLUDE_SHINKA_EVOLVE=1 hold the interpreter. On the loop a worker without it
        # fails the run: the sandbox path would run the features with other steps and no replay (SKY-830).
        if not Path(self.python).exists():
            raise ServiceError(t("blackbox.shinka_interpreter_missing"))
        settings = dict((options.shinka if options is not None else None) or {})
        route = ((options.budget_route if options is not None else None) or {}).get(MCP_RELAY_ROUTE_KEY) or {}
        agent = settings.get("editor") == "agent"
        if route.get("servers") and not agent:
            # Upstream's own mutation calls have no tools, so the servers would go unused without a word.
            raise ServiceError(
                "ShinkaEvolve loads the [proposer] mcp servers only with its agent editor. "
                "Set the agent editor, choose another engine, or remove [proposer] mcp from optimize.toml."
            )
        if route.get("services") and agent:
            # The agent's box gets only the proposer's hosts and the mcp servers, so the services would go unused.
            raise ServiceError(
                "The ShinkaEvolve agent editor cannot give the [proposer] services to its agent yet. "
                "Choose another engine, or remove [proposer] services from optimize.toml."
            )
        repo = options.repo if options is not None else None
        # The loop checks the seed's hidden paths against its own (SKY-719), so
        # the bundle must refuse the same paths, or a model could read what a box hides.
        if repo is not None:
            if ctx.repo_seed is None:
                raise ServiceError(
                    "The repository options have no repository seed to check their hidden paths against."
                )
            if dict(repo.get("hidden_hashes") or {}) != dict(ctx.repo_seed.hidden_hashes):
                raise ServiceError("The repository options hide other paths than the run's repository seed.")
        if repo is not None and repo.get("file_proposer"):
            if route.get("services"):
                raise ServiceError(
                    "ShinkaEvolve cannot give the [proposer] services to the [proposer] entry command yet. "
                    "Choose another engine, or remove [proposer] services from optimize.toml."
                )
            # The box may reach only a model route on the user's own key (BYOK), never the deployment's key. A
            # BYOK budget route listens on the worker's loopback gateway, which no box reaches, and the route
            # does not name its charge policy. So every run refuses; SKY-828 lifts this after SKY-728 (SKY-815).
            raise ServiceError(
                "ShinkaEvolve cannot give the [proposer] entry command a model route on your own key yet. "
                "Choose another engine, or remove [proposer] entry from optimize.toml."
            )
        if repo is None and task.seed_candidate is None:
            # A blank run evolves an empty starting text rather than refusing.
            task = replace(task, seed_candidate="")
        if server.remaining <= 0:
            raise BudgetExhaustedError("The ShinkaEvolve evaluation budget is exhausted.")
        workdir = Path(ctx.run_dir) / _WORKDIR
        # A resume starts upstream from scratch; its old files would change what it reads.
        shutil.rmtree(workdir, ignore_errors=True)
        workdir.mkdir(parents=True)
        here = Path(__file__).parent
        for name in _RUNNER_FILES:
            shutil.copyfile(here / name, workdir / name)
        nonce = uuid.uuid4().hex
        models = (options.shinka_models if options is not None else ()) or ()
        label = str((models[0] if models else {}).get("model") or (options.model if options else "") or _ROUTE_LABEL)
        payload: dict[str, Any] = {
            "nonce": nonce,
            "engine_id": self.name,
            "max_evals": server.remaining,
            "max_iterations": ctx.max_iterations,
            "stop_at_score": ctx.stop_at_score,
            "timeout_seconds": _TIMEOUT_SECONDS,
            "seed": ctx.seed,
            # The runner answers on stdin, one job at a time, with seeded draws.
            "transport": "stdio",
            "task": native_task_payload(self.name, task),
            "shinka": settings,
            # One route: the loop, not upstream's bandit, picks the model of each call.
            "shinka_models": [{"model": label, "url": _ROUTE_URL, "key_env": SHINKA_KEY_ENV.format(index=0)}],
            "shinka_meta_key_env": SHINKA_META_KEY_ENV,
        }
        if repo is not None:
            (workdir / _REPO_CHUNK_DIR).mkdir()
            chunks = []
            for index, chunk in enumerate(repo["chunks"]):
                chunks.append(f"{_REPO_CHUNK_DIR}/tree.{index:04d}.b64")
                shutil.copyfile(chunk, workdir / chunks[-1])
            payload["repo"] = {
                "chunks": chunks,
                "editable_paths": list(repo["editable_paths"]),
                "readonly_paths": list(repo["readonly_paths"]),
                "constrained": bool(repo.get("constrained")),
                # The Shinka bundle refuses the model's opens and edits of these (E62).
                "hidden_hashes": dict(repo.get("hidden_hashes") or {}),
            }
        document_text = json.dumps(payload, default=side_info_json_default)
        assert_metric_absent(document_text, task)
        (workdir / _INPUT_FILE).write_text(document_text, encoding="utf-8")
        agent_session = _AgentSession(ctx, options, list(route.get("servers") or ())) if agent else None
        requests = _Requests(task, server, ctx, nonce, agent=agent_session)
        try:
            outcome = self._drive(requests, workdir)
        except BudgetReached as stop:
            # The child drains, writes an interrupted incumbent and exits 1; its
            # progress lines report every scored version, so, as in ``_result``'s
            # fallback, the best version comes from them.
            if requests.best is not None:
                stop.result = requests.best_so_far(workdir)
                stop.evidence.update(
                    selection_scope="validation",
                    final_evaluation_completed=False,
                    final_evaluation_reason="budget_reached",
                )
            raise
        except BudgetExhaustedError:
            # As in GEPA, the scorer cap ends the run with the best version scored so far.
            if requests.best is None:
                raise
            return requests.best_so_far(workdir)
        if isinstance(outcome, Result):
            return outcome
        result = self._result(workdir, outcome, requests)
        result.metadata["model_cost_cents"] = str(requests.cost_cents)
        return result

    def _drive(self, requests: _Requests, workdir: Path) -> int | Result:
        """Run the child process and answer each of its requests in the order it sends them.

        Args:
            requests: Answers the child's request lines.
            workdir: The child's folder.

        Returns:
            The child's exit code. When the worker stopped the child because it
            went quiet or wrote an overlong line, the best version it reported.

        Raises:
            ServiceError: When the child sends a malformed request, or the worker
                stops it before it reports a scored version.
            BudgetReached: When the budget stops the run, after the child drains.
            BudgetExhaustedError: When the scorer cap stops the run, after the child drains.
        """
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(workdir),
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            # Upstream prices calls from a bundled catalog unless told not to;
            # the loop, not the child, prices and bills every call.
            "SHINKA_PRICING_MODE": "offline",
            # Per-thread buffers and malloc arenas inflate the address space
            # that ``_child_limits`` bounds; the child needs no parallel math.
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "MALLOC_ARENA_MAX": "2",
        }
        with (workdir / "stderr.log").open("w", encoding="utf-8") as stderr:
            child = subprocess.Popen(
                [self.python, "-c", _LIMIT_THEN_RUN, json.dumps(_child_limits()), _RUNNER_FILE, _INPUT_FILE],
                cwd=workdir,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr,
                text=True,
                encoding="utf-8",
                # The child's output is untrusted: an invalid byte, as upstream prints of model
                # text can hold, must not raise in ``readline`` and stop the run or replace a
                # budget stop. Worker replies are ASCII JSON, so only plain output changes.
                errors="replace",
                bufsize=1,
                # Its own process group, so a stop also kills upstream's
                # ``evaluate.py`` jobs, which would otherwise poll for days and
                # write into the folder a resume starts again.
                start_new_session=True,
            )
            assert child.stdin is not None
            assert child.stdout is not None
            killer = _GroupKiller(child.pid)
            # The same limit bounds the child's start (tree unpack, git, upstream
            # import), which writes no line until its first request.
            killer.arm(_IDLE_SECONDS)
            overlong = False
            try:
                try:
                    while (line := _read_line(child.stdout)) is not None:
                        # The limit pauses only while the worker handles the line: a
                        # progress or log line must not leave a hung child unbounded.
                        killer.cancel()
                        requests.line(line, child.stdin)
                        killer.arm(_IDLE_SECONDS)
                except _OverlongLineError:
                    overlong = True
                except (BudgetReached, BudgetExhaustedError):
                    # A group member that holds the pipe and ignores the stop
                    # would block the worker, so the drain has a time limit.
                    killer.arm(_DRAIN_SECONDS)
                    # A closed reply stream stops the child, which then reports
                    # every scored version, so the stop's best version never
                    # depends on how far its progress lines got.
                    with contextlib.suppress(OSError):
                        child.stdin.close()
                    # An overlong line ends the drain; the stop keeps the versions before it.
                    with contextlib.suppress(_OverlongLineError):
                        while (line := _read_line(child.stdout)) is not None:
                            # The drain reads only untrusted progress and logs: no
                            # error of one line may replace the stop.
                            try:
                                requests.line(line, None)
                            except Exception:
                                logger.warning("ShinkaEvolve wrote a line the drain could not read.", exc_info=True)
                    raise
            finally:
                killer.cancel()
                # Kill the group before the leader is reaped: until then its id
                # cannot go to an unrelated process. At the end of its output the
                # leader is already exiting, and a kernel ignores a signal to an
                # exiting process, so the kill keeps its exit code. macOS refuses
                # to signal a group whose only member is the unreaped leader.
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(child.pid, signal.SIGKILL)
                returncode = child.wait()
                with contextlib.suppress(OSError):
                    child.stdin.close()
                child.stdout.close()
        if killer.fired or overlong:
            reason = (
                f"ShinkaEvolve wrote a line longer than {_LINE_CHARS} characters, so the worker stopped it."
                if overlong
                else f"ShinkaEvolve wrote nothing for {_IDLE_SECONDS:g} seconds, so the worker stopped it."
            )
            if requests.best is None:
                raise ServiceError(reason)
            # As for a failed child, the run keeps the best version scored before the stop.
            logger.warning("%s It keeps its best reported version.", reason)
            return requests.best_so_far(workdir)
        return returncode

    @staticmethod
    def _result(workdir: Path, returncode: int, requests: _Requests) -> Result:
        """Read the child's result document.

        Args:
            workdir: The child's folder.
            returncode: The child's exit code.
            requests: The child's answers: its eval server holds the score of
                each version, and its progress holds the best scored version.

        Returns:
            The best version and its score. When upstream failed after it scored
            a version, the best version scored before the failure. The seed with
            no score when the child ends cleanly on the seed and no version has a
            full score.

        Raises:
            ServiceError: When no version has a full score, unless the child ended cleanly on the seed.
        """
        path = workdir / _RESULT_FILE
        try:
            document = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except ValueError:
            # A child killed while it writes leaves a partial document.
            document = {}
        if not isinstance(document, dict):
            document = {}
        metadata = document.get("metadata")
        metadata = {**(metadata if isinstance(metadata, dict) else {}), "native_artifacts_dir": str(workdir)}
        incumbent = document.get("interrupted_incumbent")
        # A failure keeps the last good version, as the sandbox path does; a
        # version with no score, or with a crashed case (SKY-350), is never one.
        # The score comes from the eval server, never from the child's claim.
        if isinstance(incumbent, dict):
            score = requests.recorded_score(incumbent.get("best_candidate"))
            if score is not None:
                logger.warning("ShinkaEvolve stopped early and keeps its best version: %s", document.get("error"))
                return Result(
                    best_candidate=incumbent["best_candidate"],
                    best_score=score,
                    total_evals=requests.evals,
                    metadata=metadata,
                )
        score = requests.recorded_score(document.get("best_candidate"))
        if returncode != 0 or document.get("error") or score is None:
            # A killed child writes no result, and a rejected incumbent is no
            # version, but the progress can still hold a scored version.
            if requests.best is not None:
                logger.warning(
                    "ShinkaEvolve stopped early and keeps its best reported version: %s", document.get("error")
                )
                return requests.best_so_far(workdir)
            # SKY-751: when every version fails, the child ends cleanly on the
            # seed with no score. The run keeps the seed, as the sandbox path does.
            if (
                returncode == 0
                and not document.get("error")
                and document.get("best_score") is None
                and requests.task.seed_candidate is not None
                and document.get("best_candidate") == requests.task.seed_candidate
            ):
                return Result(
                    best_candidate=requests.task.seed_candidate,
                    best_score=None,
                    total_evals=requests.evals,
                    metadata=metadata,
                )
            detail = str(document.get("error") or _stderr_tail(workdir) or "No result was produced.")
            raise ServiceError(f"ShinkaEvolve failed: {detail}")
        return Result(
            best_candidate=document["best_candidate"],
            best_score=score,
            total_evals=requests.evals,
            metadata=metadata,
        )


class _OverlongLineError(Exception):
    """The child wrote a line longer than ``_LINE_CHARS``."""


def _read_line(stream: IO[str]) -> str | None:
    """Read one line of the child's output, and never more than ``_LINE_CHARS`` of it.

    Args:
        stream: The child's stdout.

    Returns:
        The line without its newline, or ``None`` at the end of the output.

    Raises:
        _OverlongLineError: When the line has no end within ``_LINE_CHARS`` characters.
    """
    line = stream.readline(_LINE_CHARS)
    if not line:
        return None
    if len(line) >= _LINE_CHARS and not line.endswith("\n"):
        raise _OverlongLineError
    return line.rstrip("\n")


def _stderr_tail(workdir: Path) -> str:
    """Read the end of the child's stderr log, for a failure message.

    Args:
        workdir: The child's folder.

    Returns:
        The last 2000 characters of the log's final ``_STDERR_TAIL_BYTES`` bytes.
    """
    with (workdir / "stderr.log").open("rb") as log:
        log.seek(max(0, log.seek(0, os.SEEK_END) - _STDERR_TAIL_BYTES))
        return log.read().decode("utf-8", errors="replace")[-2000:]


def _child_limits() -> dict[str, int]:
    """Return the limits of the child's memory, CPU time and file sizes, which ``_LIMIT_THEN_RUN`` sets.

    Returns:
        Each limit's ``resource`` name and its value, used as both the soft and the hard limit.
    """
    limits = {"RLIMIT_CPU": _CHILD_CPU_SECONDS, "RLIMIT_FSIZE": _CHILD_FILE_BYTES}
    if _MEMORY_LIMITED:
        limits = {"RLIMIT_AS": _CHILD_MEMORY_BYTES, **limits}
    return limits


class _GroupKiller:
    """Kill a child's process group when a deadline passes, unless the deadline is cancelled first."""

    def __init__(self, pid: int) -> None:
        """Bind the killer to the child.

        Args:
            pid: The child's process id, which is also its group id.
        """
        self.pid = pid
        self.fired = False
        self._timer: threading.Timer | None = None

    def arm(self, seconds: float) -> None:
        """Start a new deadline in place of the current one.

        Args:
            seconds: The time from now to the kill.
        """
        self.cancel()
        self._timer = threading.Timer(seconds, self._kill)
        self._timer.daemon = True
        self._timer.start()

    def cancel(self) -> None:
        """Cancel the deadline, and wait for a kill in progress, so the leader is reaped only after it."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer.join()
            self._timer = None

    def _kill(self) -> None:
        """Kill the group."""
        self.fired = True
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.pid, signal.SIGKILL)


class _Proposer:
    """Run the contract file's ``[proposer] entry`` in a proposer box for each new version (SKY-815).

    Each run is one committed ``command`` step of the loop, so a resume replays it.
    """

    def __init__(self, ctx: EngineContext, options: Any) -> None:
        """Bind the proposer to the run's boxes and the contract file's commands.

        Args:
            ctx: The run context; its ``repo_seed`` names the visible seed tree.
            options: The run's native options, with the file's commands and the proposer's hosts.

        Raises:
            ServiceError: When the context has no boxes or the options name no command.
        """
        repo = options.repo
        entry = (getattr(options.proposer, "run_command", None) or "").strip()
        if None in (ctx.run_command, ctx.put_files, ctx.read_file, ctx.file_ids) or not entry:
            raise ServiceError("ShinkaEvolve needs the engine loop's boxes and a command to run [proposer] entry.")
        assert ctx.repo_seed is not None
        self.ctx = ctx
        self.entry = entry
        self.setup = [str(part) for part in repo.get("proposer_setup") or ()]
        gateway = getattr(getattr(options, "gateway", None), "url", None) or ""
        model_host = urlsplit(gateway).hostname
        # The run's gateway holds the deployment's managed key, so the box gets no grant or address for it. A
        # model route on the user's own key needs a metered relay; the engine refuses the command until SKY-828.
        self.grants = tuple(Grant(host) for host in sorted(set(repo.get("proposer_hosts") or ())) if host != model_host)
        # The same name the managed proposer box sets (docs/repository-constraints.md).
        self.exports = [f"export APP_MODEL={shlex.quote(str(options.model or ''))}"]
        self.seed = ctx.repo_seed.tree_id
        self.base: str | None = None
        self.seed_ids: dict[str, str] = {}
        self.base_ids: dict[str, str] = {}

    def propose(self, files: dict[str, str], task: str) -> dict[str, Any]:
        """Run the command once over the parent's tree, and return the version it leaves.

        Args:
            files: The parent's files that differ from the seed, by path.
            task: ShinkaEvolve's mutation prompt, which the command reads from ``ASCENT_TASK``.

        Returns:
            ``{"files": ...}``, each text file of the command's tree that differs from the seed, or
            ``{"error": ...}`` when the command failed or wrote a path it may not write.

        Raises:
            ServiceError: When the ``[proposer] setup`` command fails, or changes or removes a file of the seed.
            GrantDeniedError: When the engine loop cannot resolve the grants. A ``run_command`` that resolves the
                grants itself raises ``GrantError`` instead, a ``ValueError``; the method passes it on too, so it
                stops the run and does not become a failed mutation.
        """
        assert self.ctx.run_command is not None
        assert self.ctx.put_files is not None
        if self.base is None:
            self.base = self._setup()
        try:
            parent = self.ctx.put_files(
                {**{path: text.encode() for path, text in files.items()}, _TASK_PATH: task.encode()}, self.base
            )
            result, after = self.ctx.run_command(
                parent,
                "\n".join(
                    [
                        *self.exports,
                        f'export ASCENT_TASK="$PWD/{_TASK_PATH}" APP_PROMPT_FILE="$PWD/{_TASK_PATH}"',
                        self.entry,
                    ]
                ),
                self.grants,
            )
        except GrantError:
            # A grant the registry cannot resolve fails every mutation the same way, so it stops the run.
            raise
        except (ValueError, TreeRejectedError) as exc:
            # The version would hold a path that a box may not write; upstream counts a failed mutation.
            return {"error": f"The [proposer] entry command left no usable version: {exc}"}
        if result.timed_out or result.exit_code != 0:
            return {"error": _command_failure("[proposer] entry", result)}
        return {"files": self._version(after, parent, files)}

    def _setup(self) -> str:
        """Run ``[proposer] setup`` once over the seed tree, before the first session, as SKY-753 does.

        Returns:
            The tree every session starts from: the seed, with the setup's output when the file names a setup.

        Raises:
            ServiceError: When the setup command fails, or changes or removes a file of the seed.
        """
        assert self.ctx.run_command is not None
        assert self.ctx.file_ids is not None
        self.seed_ids = self.ctx.file_ids(self.seed)
        if not self.setup:
            self.base_ids = self.seed_ids
            return self.seed
        name = f"[proposer] setup command in optimize.toml ({shlex.join(self.setup)})"
        result, tree = self.ctx.run_command(self.seed, "\n".join([*self.exports, shlex.join(self.setup)]), self.grants)
        if result.timed_out or result.exit_code != 0:
            raise ServiceError(_command_failure(name, result))
        self.base_ids = self.ctx.file_ids(tree)
        # Every session starts from the setup's tree, so a seed file it changed would enter every version.
        changed = sorted(path for path, oid in self.seed_ids.items() if self.base_ids.get(path) != oid)
        if changed:
            raise ServiceError(
                f"The {name} changed or removed files of the repository: {', '.join(changed)}."
                " Those changes would enter every version. Make the setup write only new files."
            )
        return tree

    def _version(self, after: str, parent_tree: str, parent: dict[str, str]) -> dict[str, str]:
        """Read the files of the command's tree that make up the new version.

        Args:
            after: The tree the command left.
            parent_tree: The tree the command started from.
            parent: The parent's files that differ from the seed.

        Returns:
            The version's files by path.
        """
        return _tree_version(self.ctx, after, parent_tree, parent, self.seed_ids, self.base_ids)


def _tree_version(
    ctx: EngineContext,
    after: str,
    parent_tree: str,
    parent: dict[str, str],
    seed_ids: dict[str, str],
    base_ids: dict[str, str],
) -> dict[str, str]:
    """Read the files of a tree that make up a new version.

    A version holds the text files that differ from the seed, as an agent session's workspace does. The
    setup's own output and the task file are not part of it. The content ids tell which files changed, so only
    those are read.

    Args:
        ctx: The run context, with the loop's tree reads.
        after: The tree the work left.
        parent_tree: The tree the work started from.
        parent: The parent's files that differ from the seed.
        seed_ids: The content id of each file of the seed.
        base_ids: The content id of each file of the tree every session starts from.

    Returns:
        The version's files by path.
    """
    assert ctx.file_ids is not None
    assert ctx.read_file is not None
    parent_ids = ctx.file_ids(parent_tree)
    version: dict[str, str] = {}
    for path, oid in ctx.file_ids(after).items():
        setup_output = path in base_ids and path not in seed_ids
        # A ``.git`` part in any letter case is Git metadata, never a version file (SKY-810).
        git_part = any(part.lower() == ".git" for part in path.split("/"))
        if git_part or path == _TASK_PATH or (setup_output and path not in parent):
            continue
        if path in parent and parent_ids.get(path) == oid:
            version[path] = parent[path]
            continue
        if path not in parent and base_ids.get(path) == oid:
            continue
        # The tree entry has no size, so the limit is checked on the bytes.
        data = ctx.read_file(after, path)
        if data is None or len(data) > shinka_bundle.MAX_FILE_BYTES or b"\0" in data:
            continue
        try:
            version[path] = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
    return version


def _command_failure(name: str, result: Any) -> str:
    """Describe a failed box command for the run's error.

    Args:
        name: The command's name in the message.
        result: The command's result.

    Returns:
        The message, with the end of the command's stderr.
    """
    how = "ran out of time" if result.timed_out else f"failed with exit code {result.exit_code}"
    detail = (result.stderr or result.stdout or "").strip()[-_COMMAND_ERROR_CHARS:]
    return f"The {name} {how}." + (f" {detail}" if detail else "")


def _listing_output(listed: list[str]) -> str:
    """Join a folder's file list for the agent editor, cut at whole lines.

    Args:
        listed: The paths below the folder.

    Returns:
        The list, one path a line, with a note when it is cut.
    """
    if not listed:
        return "No files."
    kept: list[str] = []
    size = 0
    for line in listed:
        size += len(line) + 1
        if size > _TOOL_OUTPUT_CHARS + 1:
            break
        kept.append(line)
    text = "\n".join(kept)
    if len(kept) < len(listed):
        text += f"\n[cut: {len(kept)} of {len(listed)} files; use the run tool, for example find]"
    return text


def _run_output(result: Any) -> str:
    """Describe a ``run`` tool call's result for the agent editor, within ``_TOOL_OUTPUT_CHARS``.

    Args:
        result: The command's result.

    Returns:
        The exit status line, then the ends of stdout and stderr, with a note when they are cut.
    """
    how = "timed out" if result.timed_out else f"exit code {result.exit_code}"
    out, err = result.stdout or "", result.stderr or ""
    output = f"{how}\nstdout:\n{out}\nstderr:\n{err}"
    if len(output) <= _TOOL_OUTPUT_CHARS:
        return output
    # The note's numbers vary, so the room keeps a fixed reserve for it instead of its exact length.
    room = max(_TOOL_OUTPUT_CHARS - len(how) - len("\nstdout:\n\nstderr:\n\n") - _RUN_NOTE_CHARS, 0)
    # Errors are usually at the end of each stream, so each keeps its end; stderr gets at least half the room.
    err_kept = err[len(err) - min(len(err), max(room - len(out), room // 2)) :]
    out_kept = out[len(out) - min(len(out), room - len(err_kept)) :]
    note = (
        f"[cut: kept the last {len(out_kept)} of {len(out)} stdout characters "
        f"and the last {len(err_kept)} of {len(err)} stderr characters]"
    )
    return f"{how}\nstdout:\n{out_kept}\nstderr:\n{err_kept}\n{note}"


class _AgentSession:
    """Run ShinkaEvolve's agent editor on the engine loop: one tool session for each new version (SKY-830).

    Each model reply is one committed ``model`` step and each ``run`` tool call one committed ``command`` step.
    File reads and writes go to the loop's content-addressed trees, which are no steps, so a resume that
    replays the steps rebuilds the same trees and the same messages.
    """

    def __init__(self, ctx: EngineContext, options: Any, servers: list[str]) -> None:
        """Bind the session to the run's boxes, the proposer's hosts and the ``[proposer] mcp`` servers.

        Args:
            ctx: The run context; its ``repo_seed`` names the seed tree of a repository run.
            options: The run's native options, with the proposer's tool-call limit and hosts.
            servers: The names of the ``[proposer] mcp`` servers on the run's relay route.

        Raises:
            ServiceError: When the context has no boxes, or the options name no address for a server.
        """
        if None in (ctx.run_command, ctx.put_files, ctx.read_file, ctx.list_files, ctx.file_ids):
            raise ServiceError("The ShinkaEvolve agent editor needs the engine loop's boxes.")
        repo = (options.repo if options is not None else None) or {}
        self.ctx = ctx
        limit = getattr(getattr(options, "proposer", None), "max_tool_calls", None)
        self.max_tool_calls = int(limit) if limit else None
        known = {str(server["name"]): server for server in repo.get("proposer_mcp") or ()}
        missing = sorted(set(servers) - set(known))
        if missing:
            raise ServiceError(f"The [proposer] mcp servers have no address in the run's options: {missing}.")
        self.servers = {name: str(known[name]["url"]) for name in sorted(servers)}
        mcp_hosts = {urlsplit(url).hostname or "" for url in self.servers.values()}
        # A credential grant fails to resolve when the registry holds no headers for its host, so a server
        # with no headers gets a plain grant.
        credential_hosts = {
            urlsplit(str(known[name]["url"])).hostname or "" for name in servers if known[name].get("has_headers")
        }
        gateway = getattr(getattr(options, "gateway", None), "url", None) or ""
        model_host = urlsplit(gateway).hostname
        plain_hosts = {host for host in set(repo.get("proposer_hosts") or ()) - mcp_hosts if host != model_host}
        # The box never gets the gateway: the worker makes every model call. An MCP server's grant asks the
        # provider edge to add the server's headers, so its secrets never enter the box (SKY-714).
        self.grants = (
            *(Grant(host) for host in sorted(plain_hosts | (mcp_hosts - credential_hosts))),
            *(Grant(host, credential=True) for host in sorted(credential_hosts)),
        )
        self.seed = ctx.repo_seed.tree_id if ctx.repo_seed is not None else None
        self.seed_ids = ctx.file_ids(self.seed) if self.seed is not None and ctx.file_ids is not None else {}

    def edit(self, files: dict[str, str], task: str, charge: Callable[[Decimal], None]) -> dict[str, Any]:
        """Run one session over the parent's files, and return the version it leaves.

        Args:
            files: The parent's files that differ from the seed, by path; a text run sends ``program.md``.
            task: The agent's task, which ShinkaEvolve built from its mutation prompt.
            charge: Adds the cost of each model call to the run's model cost.

        Returns:
            ``{"files", "text", "tool_calls", "tool_cap_reached"}``: the version's files, the agent's last
            reply and the tool calls it spent; or ``{"error": ...}`` when a model call failed or the parent
            could not be written.

        Raises:
            GrantError: When the loop cannot resolve the box's grants; every session would fail the same way.
            Exception: Any stop of the loop, such as a budget stop.
        """
        assert self.ctx.call_model is not None
        assert self.ctx.put_files is not None
        try:
            parent = self.ctx.put_files({path: text.encode() for path, text in files.items()}, self.seed)
        except (ValueError, TreeRejectedError) as exc:
            return {"error": f"The agent editor cannot write the parent's files: {exc}"}
        tree = parent
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._system()},
            {"role": "user", "content": task},
        ]
        calls = 0
        while True:
            if self.ctx.check_budget is not None:
                self.ctx.check_budget()
            try:
                reply = self.ctx.call_model(messages, _AGENT_TOOLS)
            except ModelCallFailedError as exc:
                # The provider can bill a failed call, and its committed step keeps that cost on a replay.
                charge(exc.cost_cents)
                return {"error": f"The agent editor's model call failed: {exc}"}
            charge(reply.cost_cents)
            if not reply.tool_calls:
                break
            messages.append(
                {
                    "role": "assistant",
                    "content": reply.text or None,
                    "tool_calls": [
                        {
                            "id": call["id"],
                            "type": "function",
                            "function": {
                                "name": call["name"],
                                "arguments": (
                                    call["arguments"]
                                    if isinstance(call["arguments"], str)
                                    else json.dumps(call["arguments"], sort_keys=True)
                                ),
                            },
                        }
                        for call in reply.tool_calls
                    ],
                }
            )
            for call in reply.tool_calls:
                if self.max_tool_calls is not None and calls >= self.max_tool_calls:
                    # The provider needs an answer to each call of the reply, but the session ends here.
                    output = "The session reached its tool-call limit."
                else:
                    calls += 1
                    output, tree = self._tool(call, tree)
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": output})
            if self.max_tool_calls is not None and calls >= self.max_tool_calls:
                return {
                    "files": _tree_version(self.ctx, tree, parent, files, self.seed_ids, self.seed_ids),
                    "text": None,
                    "tool_calls": calls,
                    "tool_cap_reached": True,
                }
        return {
            "files": _tree_version(self.ctx, tree, parent, files, self.seed_ids, self.seed_ids),
            "text": reply.text,
            "tool_calls": calls,
            "tool_cap_reached": False,
        }

    def _system(self) -> str:
        """Describe the tools, and the ``[proposer] mcp`` servers when the run has some.

        Returns:
            The session's system message.
        """
        text = _AGENT_SYSTEM
        if self.servers:
            listed = "\n".join(f"- {name}: {url}" for name, url in self.servers.items())
            text += (
                "\n\nThe run tool's box can call these MCP servers over HTTP (JSON-RPC). The network adds their "
                f"credentials, so send no credentials yourself:\n{listed}"
            )
        return text

    def _tool(self, call: Mapping[str, Any], tree: str) -> tuple[str, str]:
        """Answer one tool call over the session's tree.

        Args:
            call: The tool call, in the loop's flat form.
            tree: The session's current tree.

        Returns:
            The tool's output for the model, and the session's tree after the call.

        Raises:
            GrantError: When the loop cannot resolve the box's grants.
        """
        assert self.ctx.read_file is not None
        assert self.ctx.list_files is not None
        assert self.ctx.put_files is not None
        assert self.ctx.run_command is not None
        arguments = call["arguments"]
        if not isinstance(arguments, dict):
            return "The tool call's arguments are not a JSON object.", tree
        name = call["name"]
        value = {key: arguments.get(key) for key in ("path", "content", "command")}
        try:
            if name == "list_files":
                listed = self.ctx.list_files(tree, str(value["path"] or ""))
                return ("No such folder." if listed is None else _listing_output(listed)), tree
            if name == "read_file" and isinstance(value["path"], str):
                data = self.ctx.read_file(tree, value["path"])
                if data is None:
                    return "No such file.", tree
                text = data.decode("utf-8", errors="replace")
                if len(text) <= _TOOL_OUTPUT_CHARS:
                    return text, tree
                # Without the note, the model could write back only the part it saw and drop the rest.
                return f"{text[:_TOOL_OUTPUT_CHARS]}\n[cut at {_TOOL_OUTPUT_CHARS} of {len(text)} characters]", tree
            if name == "write_file" and isinstance(value["path"], str) and isinstance(value["content"], str):
                return "Written.", self.ctx.put_files({value["path"]: value["content"].encode()}, tree)
            if name == "run" and isinstance(value["command"], str):
                if self.ctx.check_budget is not None:
                    self.ctx.check_budget()
                result, after = self.ctx.run_command(tree, value["command"], self.grants)
                return _run_output(result), after
        except GrantError:
            # A grant the registry cannot resolve fails every session the same way, so it stops the run.
            raise
        except (ValueError, LookupError, TreeRejectedError) as exc:
            # Such as a path a box may not write: the model gets the error and can correct its call.
            return f"The tool call failed: {exc}", tree
        return f"The tool call {name!r} is unknown or has malformed arguments.", tree


class _Requests:
    """Answer the child's framed lines: evaluations and model calls, progress and logs."""

    def __init__(
        self,
        task: Task,
        server: EvalServer,
        ctx: EngineContext,
        nonce: str,
        proposer: _Proposer | None = None,
        agent: _AgentSession | None = None,
    ) -> None:
        """Bind the answers to the run.

        Args:
            task: The task, whose cases the child names by position.
            server: The eval server.
            ctx: The run context.
            nonce: The framing token of the child's lines.
            proposer: Runs the contract file's ``[proposer] entry`` for each new version, or ``None``.
            agent: Runs the agent editor's session for each new version, or ``None``.
        """
        self.task = task
        self.proposer = proposer
        self.agent = agent
        self.server = server
        self.ctx = ctx
        self.nonce = nonce
        self.scrub = sandbox_log.Scrubber(())
        self.cost_cents = Decimal(0)
        self.best: dict[str, Any] | None = None
        # The cases ``_evaluate`` scored for each version: the server's mean
        # covers only these, so a version is a result only once it covers all.
        self.covered: dict[str, set[int]] = {}
        # Every evaluation the child asked for, so a stop reports the run's
        # total, not the total when the best version was found.
        self.evals = 0

    def best_so_far(self, workdir: Path) -> Result:
        """Return the best version the child reported, for a run that stopped before its result.

        Args:
            workdir: The child's folder.

        Returns:
            The best version, its score and the run's evaluations so far.
        """
        assert self.best is not None
        return Result(
            best_candidate=self.best["candidate"],
            best_score=self.best["score"],
            total_evals=self.evals,
            metadata={"native_artifacts_dir": str(workdir), "model_cost_cents": str(self.cost_cents)},
        )

    def recorded_score(self, candidate: Any) -> float | None:
        """Return the eval server's own mean score of a version the child names.

        Args:
            candidate: The version, as the child sent it.

        Returns:
            The finite mean score, or ``None`` for a malformed version, one the
            server never scored on every case, or one with a metric error.
        """
        if not _candidate_shape_ok(candidate, self.task.str_mode):
            return None
        if self.task.cases and len(self.covered.get(candidate_key(candidate), ())) < len(self.task.cases):
            return None
        score = self.server.mean_score(candidate)
        return score if score is not None and math.isfinite(score) else None

    def line(self, line: str, replies: IO[str] | None) -> bool:
        """Handle one line the child wrote.

        Args:
            line: The line, without its newline.
            replies: The child's stdin, where each reply goes as one line, or
                ``None`` after a stop, when only progress and logs are read.

        Returns:
            Whether a reply went to the child.

        Raises:
            ServiceError: When a request, its candidate or its case is malformed.
            BudgetReached: When the budget stops the run during an evaluation.
            BudgetExhaustedError: When no scorer call remains for an evaluation.
        """
        for prefix, handle in (
            (_RPC_PREFIX, self._request),
            (_PROGRESS_PREFIX, self._progress),
            (_LOG_PREFIX, self._log),
        ):
            framed = f"{prefix}{self.nonce} "
            if line.startswith(framed):
                body = line[len(framed) :]
                if prefix == _RPC_PREFIX:
                    if replies is None:
                        return False
                    try:
                        request = json.loads(body)
                    except (ValueError, RecursionError):
                        request = None
                    if not isinstance(request, dict):
                        raise ServiceError("ShinkaEvolve sent a malformed request.")
                    reply = self._request(request)
                else:
                    # A broken log or progress line must not stop the run the
                    # way a broken request does, so it stays as plain output. Deep
                    # nesting makes the parser raise ``RecursionError``.
                    try:
                        data = json.loads(body)
                    except (ValueError, RecursionError):
                        data = None
                    if not isinstance(data, dict):
                        sandbox_log.stream_line(body, owner=None, scrub=self.scrub, label="stdout")
                        return False
                    reply = handle(data)
                if reply is not None and replies is not None:
                    try:
                        text = json.dumps(reply, default=side_info_json_default, allow_nan=False)
                    except (TypeError, ValueError):
                        # Such as a NaN score: the child stops on an evaluation
                        # error and retries a failed model call.
                        text = json.dumps({"id": reply.get("id"), "error": "The worker returned an invalid reply."})
                    # A child that already exited reports its own failure in its result.
                    try:
                        replies.write(text + "\n")
                        replies.flush()
                    except BrokenPipeError:
                        return False
                    return True
                return False
        if line.strip():
            logger.debug("ShinkaEvolve output: %s", line)
        return False

    def _request(self, request: dict[str, Any]) -> dict[str, Any]:
        """Answer one evaluation or model request.

        Args:
            request: The request.

        Returns:
            The reply.

        Raises:
            ServiceError: When the request is malformed.
        """
        if request.get("kind") == "model":
            return {"id": request.get("id"), **self._model(request.get("messages"))}
        if request.get("kind") == "evaluate":
            return {"id": request.get("id"), **self._evaluate(request)}
        kind = request.get("kind")
        if (kind == "propose" and self.proposer is not None) or (kind == "agent" and self.agent is not None):
            files = request.get("files")
            task = request.get("task")
            if (
                not isinstance(files, dict)
                or not isinstance(task, str)
                or not all(isinstance(path, str) and isinstance(text, str) for path, text in files.items())
            ):
                raise ServiceError("ShinkaEvolve sent a malformed proposal.")
            if self.ctx.check_budget is not None:
                self.ctx.check_budget()
            if kind == "agent":
                assert self.agent is not None
                return {"id": request.get("id"), **self.agent.edit(files, task, self._charge)}
            assert self.proposer is not None
            return {"id": request.get("id"), **self.proposer.propose(files, task)}
        raise ServiceError("ShinkaEvolve sent an unknown request.")

    def _charge(self, cents: Decimal) -> None:
        """Add one model call's cost to the run's model cost.

        Args:
            cents: The call's cost in cents.
        """
        self.cost_cents += cents

    def _model(self, messages: Any) -> dict[str, Any]:
        """Call the loop's model: the reply is one committed step, and its cost adds to the run's.

        Args:
            messages: The chat messages.

        Returns:
            ``{"text": ...}``, or ``{"error": <type>}`` when the model call failed.

        Raises:
            ServiceError: When the messages are malformed.
            Exception: Any error of the model call other than a failed call, such as a stop.
        """
        if not isinstance(messages, list) or not all(isinstance(message, dict) for message in messages):
            raise ServiceError("ShinkaEvolve sent malformed model messages.")
        # ``run`` starts the child only with the loop's context, which sets ``call_model``.
        assert self.ctx.call_model is not None
        # A failed call goes back to upstream, which retries it as it would retry
        # a provider error; any other error, such as a stop, ends the run.
        try:
            reply = self.ctx.call_model(messages, None)
        except ModelCallFailedError as exc:
            # The provider can bill a failed call, and its committed step keeps that cost on a replay.
            self.cost_cents += exc.cost_cents
            return {"error": type(exc).__name__}
        self.cost_cents += reply.cost_cents
        return {"text": reply.text}

    def _evaluate(self, request: dict[str, Any]) -> dict[str, Any]:
        """Score one case through the eval server: on the engine loop, its commands are committed steps.

        Args:
            request: The candidate, the case the child names by position, and the version number.

        Returns:
            The score and the side information a proposer may see, or an error
            that stops the child when it names no case of a task with cases.

        Raises:
            ServiceError: When the candidate or the case is malformed.
            BudgetReached: When the budget stops the run before or after the scorer call.
            BudgetExhaustedError: When no scorer call remains.
        """
        candidate = request.get("candidate")
        if not _candidate_shape_ok(candidate, self.task.str_mode):
            raise ServiceError("ShinkaEvolve sent an invalid candidate shape.")
        example = request.get("example")
        if example is None and self.task.cases:
            # The server would score the version with no case. The child stops,
            # and the run keeps its best version scored on every case.
            return {"error": "The evaluation names no case."}
        case = None
        if example is not None:
            index = example.get("id") if isinstance(example, dict) else None
            if type(index) is not int or not 0 <= index < len(self.task.cases):
                raise ServiceError("ShinkaEvolve named an unknown case.")
            case = self.task.cases[index]
        if self.ctx.check_budget is not None:
            self.ctx.check_budget()
        candidate_id = request.get("candidate_id")
        with sandbox_log.event_scope(
            candidate=str(candidate_id) if type(candidate_id) is int else None,
            case=None if example is None else str(example["id"]),
        ):
            score, info = self.server.evaluate(candidate, case)
        self.evals += 1
        if example is not None:
            self.covered.setdefault(candidate_key(candidate), set()).add(example["id"])
        if self.ctx.check_budget is not None:
            self.ctx.check_budget()
        if score is not None and isinstance(info, dict) and type(candidate_id) is int:
            emit_scorer_feedback(
                self.ctx.progress_callback,
                example_id="?" if example is None else str(example["id"]),
                score=float(score),
                side_info=info,
                candidate_id=str(candidate_id),
            )
        return {"score": score, "info": sanitize_side_info(info)}

    def _progress(self, event: dict[str, Any]) -> None:
        """Relay a scored case, or a scored version, to the run's progress.

        The event is untrusted child output: one with a malformed field is ignored.

        Args:
            event: The child's progress event.
        """
        score = event.get("score")
        candidate_id = event.get("candidate_id")
        if type(candidate_id) is not int or not _finite(score):
            return
        if event.get("event") == "case_scored":
            if type(event.get("total")) is int:
                emit_case_scored(
                    self.ctx.progress_callback,
                    trial=candidate_id,
                    example_id=str(event.get("example_id", "?")),
                    score=float(score),
                    total=event["total"],
                )
            return
        candidate = event.get("candidate")
        per_example = event.get("per_example") or []
        total_evals = event.get("total_evals", 0)
        if (
            not _candidate_shape_ok(candidate, self.task.str_mode)
            or type(total_evals) is not int
            or not isinstance(per_example, list)
            or not all(isinstance(pair, list) and len(pair) == 2 and _finite(pair[1]) for pair in per_example)
        ):
            return
        generation = event.get("generation")
        parent_id = event.get("parent_id")
        # A version with a crashed case has no score (SKY-350).
        shown = None if self.server.metric_error(candidate) is not None else float(score)
        recorded = self.recorded_score(candidate)
        if recorded is not None and (self.best is None or recorded > self.best["score"]):
            self.best = {"candidate": candidate, "score": recorded}
        emit_candidate(
            self.ctx.progress_callback,
            candidate_id=str(candidate_id),
            parent_id=None if parent_id is None else str(parent_id),
            generation=generation if type(generation) is int else 0,
            score=shown,
            per_example=[] if shown is None else [(str(case_id), float(value)) for case_id, value in per_example],
            candidate=candidate,
            discovered_at_evals=total_evals,
            iteration=None,
        )

    def _log(self, data: Any) -> None:
        """Relay one structured record from the child into the run log.

        Args:
            data: The decoded record.
        """
        sandbox_log.forward(data, owner=None, scrub=self.scrub)
