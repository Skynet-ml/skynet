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

Without that context, the engine runs the runner in the managed sandbox, as
before, until the cutover (SKY-728).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import resource
import shutil
import signal
import subprocess
import sys
import threading
import uuid
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import IO, Any

from ....billing.model_gateway import MCP_RELAY_ROUTE_KEY
from ....exceptions import ServiceError
from ....worker.engine_loop import ModelCallFailedError
from ..budget_stop import BudgetReached
from . import sandbox_log
from .feedback import emit_candidate, emit_case_scored, emit_scorer_feedback
from .native_runtime import (
    SHINKA_IMAGE_PYTHON,
    SHINKA_KEY_ENV,
    SHINKA_META_KEY_ENV,
    assert_metric_absent,
    native_task_payload,
    run_native_engine,
    sanitize_side_info,
)
from .protocol import BudgetExhaustedError, EngineContext, EvalServer, Result, Task
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
# The runner never calls this URL: its model calls come back to the worker. Upstream
# still needs a well-formed ``local/<model>@<url>`` name for its bandit.
_ROUTE_URL = "http://engine-loop.invalid/v1"
_ROUTE_LABEL = "engine-loop"
# The budget and the evaluation limit end a run; this only bounds a lost reply.
_TIMEOUT_SECONDS = 7 * 24 * 3600.0
# The child parses untrusted model replies, so a reply that makes a parser loop
# or grow must not take the worker host with it. Its own work is prompt building
# and parsing: a few hundred MiB and well under a second a generation, while it
# waits on the worker for nearly all of a run. The address-space limit leaves
# room for the thread stacks and malloc arenas that inflate the virtual size.
_CHILD_MEMORY_BYTES = 8 * 1024**3
_CHILD_CPU_SECONDS = 6 * 3600
# macOS refuses to lower the address-space limit; the workers run Linux.
_MEMORY_LIMITED = sys.platform == "linux"
# Between a reply and its next line the child only builds prompts and parses the
# reply; the worker scores versions and calls the model with no timer running.
_IDLE_SECONDS = 30 * 60.0
# A stopped child only reports its scored versions and writes its result.
_DRAIN_SECONDS = 60.0


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
            upstream failed or the scorer cap stopped it.

        Raises:
            ServiceError: When the settings need a feature the worker does not run yet, or upstream fails
                before it scores a version.
            BudgetExhaustedError: When no scorer call remains, or the cap stops the run before a version is scored.
            BudgetReached: When the budget stops the run; it carries the best version scored so far.
        """
        # Only images built with INCLUDE_SHINKA_EVOLVE=1 hold the interpreter; a
        # worker without it keeps the sandbox path instead of failing the run.
        if ctx.call_model is None or not Path(self.python).exists():
            return run_native_engine(self.name, task, server, ctx)
        options = ctx.native_options
        settings = dict((options.shinka if options is not None else None) or {})
        route = ((options.budget_route if options is not None else None) or {}).get(MCP_RELAY_ROUTE_KEY) or {}
        if settings.get("editor") == "agent":
            raise ServiceError("The ShinkaEvolve agent editor does not run in the worker yet.")
        if settings.get("novelty"):
            raise ServiceError("ShinkaEvolve novelty rejection does not run in the worker yet.")
        if route.get("servers"):
            raise ServiceError(
                "ShinkaEvolve cannot load the [proposer] mcp servers yet. "
                "Choose another engine, or remove [proposer] mcp from optimize.toml."
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
        requests = _Requests(task, server, ctx, nonce)
        try:
            returncode = self._drive(requests, workdir)
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
        result = self._result(workdir, returncode, requests)
        result.metadata["model_cost_cents"] = str(requests.cost_cents)
        return result

    def _drive(self, requests: _Requests, workdir: Path) -> int:
        """Run the child process and answer each of its requests in the order it sends them.

        Args:
            requests: Answers the child's request lines.
            workdir: The child's folder.

        Returns:
            The child's exit code.
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
        }
        with (workdir / "stderr.log").open("w", encoding="utf-8") as stderr:
            # ``preexec_fn`` is unsafe in a threaded parent only when it takes a
            # lock; ``_limit_resources`` makes only system calls.
            child = subprocess.Popen(
                [self.python, _RUNNER_FILE, _INPUT_FILE],
                cwd=workdir,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr,
                text=True,
                encoding="utf-8",
                bufsize=1,
                # Its own process group, so a stop also kills upstream's
                # ``evaluate.py`` jobs, which would otherwise poll for days and
                # write into the folder a resume starts again.
                start_new_session=True,
                preexec_fn=_limit_resources,  # noqa: PLW1509
            )
            assert child.stdin is not None
            assert child.stdout is not None
            killer = _GroupKiller(child.pid)
            try:
                try:
                    for line in child.stdout:
                        killer.cancel()
                        if requests.line(line.rstrip("\n"), child.stdin):
                            killer.arm(_IDLE_SECONDS)
                except (BudgetReached, BudgetExhaustedError):
                    # A group member that holds the pipe and ignores the stop
                    # would block the worker, so the drain has a time limit.
                    killer.arm(_DRAIN_SECONDS)
                    # A closed reply stream stops the child, which then reports
                    # every scored version, so the stop's best version never
                    # depends on how far its progress lines got.
                    with contextlib.suppress(OSError):
                        child.stdin.close()
                    for line in child.stdout:
                        requests.line(line.rstrip("\n"), None)
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
        if killer.fired:
            raise ServiceError(
                f"ShinkaEvolve wrote nothing for {_IDLE_SECONDS:g} seconds after a reply, so the worker stopped it."
            )
        return returncode

    @staticmethod
    def _result(workdir: Path, returncode: int, requests: _Requests) -> Result:
        """Read the child's result document.

        Args:
            workdir: The child's folder.
            returncode: The child's exit code.
            requests: The child's answers: its eval server knows the versions that
                hit a metric error, and its progress holds the best scored version.

        Returns:
            The best version and its score. When upstream failed after it scored
            a version, the best version scored before the failure.

        Raises:
            ServiceError: When the child failed before it scored a version without a metric error.
        """
        path = workdir / _RESULT_FILE
        document = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        incumbent = document.get("interrupted_incumbent")
        # A failure keeps the last good version, as the sandbox path does; a
        # version with no score, or with a crashed case (SKY-350), is never one.
        if (
            isinstance(incumbent, dict)
            and incumbent.get("best_score") is not None
            and requests.server.metric_error(incumbent.get("best_candidate")) is None
        ):
            logger.warning("ShinkaEvolve stopped early and keeps its best version: %s", document.get("error"))
            return Result(
                best_candidate=incumbent["best_candidate"],
                best_score=incumbent["best_score"],
                total_evals=incumbent.get("total_evals", 0),
                metadata={**document.get("metadata", {}), "native_artifacts_dir": str(workdir)},
            )
        if returncode != 0 or not document or document.get("error") or document.get("best_score") is None:
            # A killed child writes no result, and a rejected incumbent is no
            # version, but the progress can still hold a scored version.
            if requests.best is not None:
                logger.warning(
                    "ShinkaEvolve stopped early and keeps its best reported version: %s", document.get("error")
                )
                return requests.best_so_far(workdir)
            stderr = (workdir / "stderr.log").read_text(encoding="utf-8", errors="replace")
            detail = str(document.get("error") or stderr[-2000:] or "No result was produced.")
            raise ServiceError(f"ShinkaEvolve failed: {detail}")
        return Result(
            best_candidate=document["best_candidate"],
            best_score=document["best_score"],
            total_evals=document.get("total_evals", 0),
            metadata={**document.get("metadata", {}), "native_artifacts_dir": str(workdir)},
        )


def _limit_resources() -> None:
    """Bound the child's memory and CPU time; it runs in the forked child before ``exec``.

    It makes only system calls, so it takes no lock that another worker thread
    could hold at the fork.
    """
    if _MEMORY_LIMITED:
        resource.setrlimit(resource.RLIMIT_AS, (_CHILD_MEMORY_BYTES, _CHILD_MEMORY_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (_CHILD_CPU_SECONDS, _CHILD_CPU_SECONDS))


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


class _Requests:
    """Answer the child's framed lines: evaluations and model calls, progress and logs."""

    def __init__(self, task: Task, server: EvalServer, ctx: EngineContext, nonce: str) -> None:
        """Bind the answers to the run.

        Args:
            task: The task, whose cases the child names by position.
            server: The eval server.
            ctx: The run context.
            nonce: The framing token of the child's lines.
        """
        self.task = task
        self.server = server
        self.ctx = ctx
        self.nonce = nonce
        self.scrub = sandbox_log.Scrubber(())
        self.cost_cents = Decimal(0)
        self.best: dict[str, Any] | None = None
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

    def line(self, line: str, replies: IO[str] | None) -> bool:
        """Handle one line the child wrote.

        Args:
            line: The line, without its newline.
            replies: The child's stdin, where each reply goes as one line, or
                ``None`` after a stop, when only progress and logs are read.

        Returns:
            Whether a reply went to the child.
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
                    reply = self._request(json.loads(body))
                else:
                    # A broken log or progress line must not stop the run the
                    # way a broken request does, so it stays as plain output.
                    try:
                        data = json.loads(body)
                    except ValueError:
                        data = None
                    if not isinstance(data, dict):
                        sandbox_log.stream_line(body, owner=None, scrub=self.scrub, label="stdout")
                        return False
                    reply = handle(data)
                if reply is not None and replies is not None:
                    text = json.dumps(reply, default=side_info_json_default, allow_nan=False)
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
        raise ServiceError("ShinkaEvolve sent an unknown request.")

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
            The score and the side information a proposer may see.

        Raises:
            ServiceError: When the candidate or the case is malformed.
        """
        candidate = request.get("candidate")
        if self.task.str_mode:
            valid = isinstance(candidate, str)
        else:
            valid = (
                isinstance(candidate, dict)
                and bool(candidate)
                and all(isinstance(name, str) and isinstance(text, str) for name, text in candidate.items())
            )
        if not valid:
            raise ServiceError("ShinkaEvolve sent an invalid candidate shape.")
        example = request.get("example")
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

        Args:
            event: The child's progress event.
        """
        score = event.get("score")
        candidate_id = event.get("candidate_id")
        if type(candidate_id) is not int or not isinstance(score, int | float) or isinstance(score, bool):
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
        per_example = event.get("per_example")
        generation = event.get("generation")
        parent_id = event.get("parent_id")
        # A version with a crashed case has no score (SKY-350).
        shown = None if self.server.metric_error(event.get("candidate")) is not None else float(score)
        if shown is not None and (self.best is None or shown > self.best["score"]):
            self.best = {"candidate": event.get("candidate"), "score": shown}
        emit_candidate(
            self.ctx.progress_callback,
            candidate_id=str(candidate_id),
            parent_id=None if parent_id is None else str(parent_id),
            generation=generation if type(generation) is int else 0,
            score=shown,
            per_example=[]
            if shown is None
            else [(str(case_id), float(value)) for case_id, value in (per_example or []) if value is not None],
            candidate=event.get("candidate"),
            discovered_at_evals=int(event.get("total_evals") or 0),
            iteration=None,
        )

    def _log(self, data: Any) -> None:
        """Relay one structured record from the child into the run log.

        Args:
            data: The decoded record.
        """
        sandbox_log.forward(data, owner=None, scrub=self.scrub)
