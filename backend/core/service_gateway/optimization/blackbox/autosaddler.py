"""AutoSaddler engine: run the pinned upstream loop in the worker engine loop (SKY-723).

Skynet does not reimplement AutoSaddler. The upstream ``microsoft/AutoSaddler``
v2 engine, run store and policies run unchanged, in a child process on the
worker. The child uses its own interpreter (the image's AutoSaddler venv), so
upstream and Skynet never share dependencies. Skynet contributes only a
scenario plugin (``autosaddler_runner.py`` and ``autosaddler_plugin/``).

The child holds no capability of its own. It asks this host for each model
call, command, tree read and tree write, and for each evaluation, one JSON line
at a time over its stdin and stdout. This follows Cloudflare's capability RPC
pattern (Workers service bindings and Cap'n Web): the isolated code gets only
the calls that the host binds for it. Each model call and command is one step
of the engine loop. A resume starts the child again from its start, and the
loop answers each committed step from its log. This is the replay pattern of
Cloudflare Workflows, where ``step.do`` results are durable and the workflow
code runs again from the top. So the child must ask the same calls in the
same order; the runner keeps its prompts free of clocks, random ids and set
order.

A context with no worker host still runs the native box until the SKY-728
cutover.
"""

from __future__ import annotations

import base64
import contextlib
import json
import math
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import IO, Any

from ....exceptions import ServiceError
from ....sandbox.trees import BoxFailureError, GrantDeniedError, MetricFailedError, TreeRejectedError
from ....worker.engine_loop import ModelCallFailedError
from . import sandbox_log
from .feedback import emit_candidate, emit_case_scored, emit_scorer_feedback
from .native_runtime import (
    AUTOSADDLER_IMAGE_PYTHON,
    _candidate_shape_ok,
    assert_metric_absent,
    native_task_payload,
    run_native_engine,
    sanitize_side_info,
)
from .protocol import BudgetExhaustedError, EngineContext, EvalServer, Result, Task
from .runner import side_info_json_default
from .upstream import AUTOSADDLER_REVISION

RUNNER = Path(__file__).with_name("autosaddler_runner.py")
# The runner's argument for worker mode; it must match ``autosaddler_runner.WORKER_FLAG``.
WORKER_FLAG = "--worker"
# A step that fails with one of these commits a record, and a replay fails the same way, so the child may
# see it and go on.
_STEP_ERRORS = (ModelCallFailedError, BoxFailureError, GrantDeniedError, MetricFailedError, TreeRejectedError)
# The child's own work between two requests is local file and git work. No lease or heartbeat ends a child
# that hangs there, so the host stops it after this long with no request.
_IDLE_SECONDS = 600.0
# How long the child may take to exit after it sent its result.
_EXIT_SECONDS = 30.0
_NO_RESULT = "The AutoSaddler runner exited with no result; see runner.log in the run folder."


class AutoSaddlerEngine:
    """Drive the upstream AutoSaddler engine in a worker child process, one step at a time."""

    name = "autosaddler"

    def __init__(self, python: str = AUTOSADDLER_IMAGE_PYTHON, runner: Path = RUNNER) -> None:
        """Choose the child's interpreter and entry point.

        Args:
            python: The interpreter of the isolated AutoSaddler venv.
            runner: The script that the child runs.
        """
        self.python = python
        self.runner = runner

    def run(self, task: Task, server: EvalServer, ctx: EngineContext) -> Result:
        """Run the pinned upstream engine with Skynet's evaluator and plugin.

        Args:
            task: Seed candidate, objective and visible examples.
            server: Skynet evaluator and shared evaluation budget.
            ctx: The engine loop's context; with no model, command or tree call it is a native-box context.

        Returns:
            The best development-confirmed candidate upstream selected.

        Raises:
            BudgetExhaustedError: When no evaluation is left before the child starts.
            ServiceError: When the child fails or stops with no result.
        """
        if (
            ctx.call_model is None
            or ctx.run_command is None
            or ctx.put_files is None
            or ctx.read_file is None
            or ctx.list_files is None
        ):
            return run_native_engine(self.name, task, server, ctx)
        # Upstream refuses an evaluation budget of 0, so a spent budget would fail as a crash.
        if server.remaining <= 0:
            raise BudgetExhaustedError("The native optimizer evaluation budget is exhausted.")
        seed = ctx.repo_seed
        payload = {
            "engine_id": self.name,
            "task": native_task_payload(self.name, task),
            "max_evals": server.remaining,
            "max_iterations": ctx.max_iterations,
            "stop_at_score": ctx.stop_at_score,
            "repo": None
            if seed is None
            else {
                "tree_id": seed.tree_id,
                "editable_paths": list(seed.editable_paths),
                "readonly_paths": list(seed.readonly_paths),
                "constrained": bool(seed.constrained_paths),
            },
        }
        document_text = json.dumps(payload, default=side_info_json_default)
        assert_metric_absent(document_text, task)
        workdir = Path(ctx.run_dir) / "autosaddler"
        # A resume replays the run from its start, so the child must not see the files of an earlier try.
        # No ``ignore_errors``: a folder that is not removed must fail here, not run the child on stale files.
        if workdir.exists():
            shutil.rmtree(workdir)
        workdir.mkdir(parents=True)
        with (workdir / "runner.log").open("w", encoding="utf-8") as log:
            child = subprocess.Popen(
                # ``-I`` would also ignore ``PYTHONHASHSEED``. ``-s -P`` keep the user site folder and the
                # runner's own folder off ``sys.path``, so this module can not shadow upstream's ``autosaddler``.
                [self.python, "-s", "-P", str(self.runner), WORKER_FLAG],
                cwd=workdir,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=log,
                text=True,
                encoding="utf-8",
                # The child gets no worker secret. A fixed hash seed keeps str hashes, and so set order, the same
                # in a replay, so a prompt hash matches its committed step.
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "HOME": str(workdir),
                    "LANG": "C.UTF-8",
                    "PYTHONHASHSEED": "0",
                },
            )
            try:
                _send(child.stdin, document_text)
                document = self._serve(child, task, server, ctx)
                try:
                    child.wait(timeout=_EXIT_SECONDS)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            except BaseException:
                # A loop stop or a worker death gives the engine no result, so the child must not go on.
                child.kill()
                child.wait()
                raise
            finally:
                for stream in (child.stdin, child.stdout):
                    # A close flushes the rest of a reply that a stopped child no longer reads.
                    if stream is not None:
                        with contextlib.suppress(BrokenPipeError):
                            stream.close()
        return _result(document, task, server)

    def _serve(
        self, child: subprocess.Popen[str], task: Task, server: EvalServer, ctx: EngineContext
    ) -> dict[str, Any]:
        """Answer the child's requests one at a time until it sends its result.

        Args:
            child: The runner process.
            task: The engine's task, whose full cases the evaluator restores.
            server: The eval server.
            ctx: The engine loop's context.

        Returns:
            The child's result document.

        Raises:
            ServiceError: When the child sends a bad or unknown request, goes silent, or exits with no result.
        """
        assert child.stdout is not None
        while True:
            silent = threading.Event()
            # A kill closes the child's stdout, so the read below ends with an empty line.
            watchdog = threading.Timer(_IDLE_SECONDS, _stop_silent, (child, silent))
            watchdog.start()
            try:
                line = child.stdout.readline()
            finally:
                watchdog.cancel()
                # A watchdog that already started must finish its kill before the check below.
                watchdog.join()
            # The watchdog can fire just after a full line arrives. The child is then dead, so serving the line
            # could commit a paid step whose reply nobody reads.
            if silent.is_set():
                raise ServiceError(
                    f"The AutoSaddler runner sent no request for {_IDLE_SECONDS:g} seconds; see runner.log in "
                    "the run folder."
                )
            if not line:
                raise ServiceError(_NO_RESULT)
            try:
                request = json.loads(line)
            except ValueError:
                request = None
            if not isinstance(request, dict):
                raise ServiceError(
                    "The AutoSaddler runner sent a request that is not a JSON object; see runner.log in the run folder."
                )
            op = request.get("op")
            try:
                if op == "result":
                    return dict(request.get("result") or {})
                if op == "model":
                    reply = _model(ctx, request)
                elif op == "command":
                    reply = _command(ctx, request)
                elif op == "put_files":
                    reply = _put_files(ctx, request)
                elif op == "read_file":
                    assert ctx.read_file is not None
                    data = ctx.read_file(str(request["tree_id"]), str(request["path"]))
                    reply = {"data": None if data is None else base64.b64encode(data).decode("ascii")}
                elif op == "list_files":
                    assert ctx.list_files is not None
                    reply = {"paths": ctx.list_files(str(request["tree_id"]), str(request.get("path") or ""))}
                elif op == "evaluate":
                    reply = _evaluate(task, server, ctx, request)
                elif op == "progress":
                    event = request.get("event", {})
                    if not isinstance(event, dict):
                        raise TypeError("event")
                    _progress(task, server, ctx, event)
                    reply = {}
                elif op == "log":
                    sandbox_log.forward(request.get("record"), owner=ctx.log_owner, scrub=sandbox_log.Scrubber(()))
                    reply = {}
                else:
                    raise ServiceError(f"The AutoSaddler runner sent an unknown request: {op!r}.")
            except (TypeError, ValueError, LookupError, ArithmeticError) as error:
                # A missing, mistyped or malformed field, or a tree the run can not see, is the child's fault, not
                # the host's. ArithmeticError covers a number the host can not convert, such as int(inf) from 1e999.
                raise ServiceError(
                    f"The AutoSaddler runner sent a bad request: {op!r} ({type(error).__name__}); see runner.log in "
                    "the run folder."
                ) from error
            try:
                text = json.dumps(_json_finite(reply), default=side_info_json_default, allow_nan=False)
            except (TypeError, ValueError):
                # As the native mailbox does: the child gets a refusal, and the run goes on.
                text = json.dumps({"error": "The worker could not send its reply as JSON."})
            _send(child.stdin, text)


def _stop_silent(child: subprocess.Popen[str], silent: threading.Event) -> None:
    """Kill a child that sent no request in time, and mark why.

    Args:
        child: The runner process.
        silent: Set before the kill, so the reader knows the cause of the empty line.
    """
    silent.set()
    child.kill()


def _send(stream: IO[str] | None, line: str) -> None:
    """Write one JSON line to the child.

    Args:
        stream: The child's stdin.
        line: The JSON text, with no newline in it.

    Raises:
        ServiceError: When the child closed its stdin, which it does only when it stops.
    """
    assert stream is not None
    try:
        stream.write(line + "\n")
        stream.flush()
    except BrokenPipeError as error:
        raise ServiceError(_NO_RESULT) from error


def _model(ctx: EngineContext, request: dict[str, Any]) -> dict[str, Any]:
    """Call the model as one step.

    Args:
        ctx: The engine loop's context.
        request: The child's prompt and tool definitions.

    Returns:
        The reply text, tool calls and cost in cents, or the failure and the cost that the step committed.
    """
    assert ctx.call_model is not None
    try:
        reply = ctx.call_model(request["prompt"], request.get("tools"))
    except ModelCallFailedError as error:
        return {"error": f"{type(error).__name__}: {error}", "cost_cents": str(error.cost_cents)}
    return {"text": reply.text, "tool_calls": reply.tool_calls, "cost_cents": str(reply.cost_cents)}


def _command(ctx: EngineContext, request: dict[str, Any]) -> dict[str, Any]:
    """Run one command in a box, with no network, as one step.

    Args:
        ctx: The engine loop's context.
        request: The child's tree id and command.

    Returns:
        The command's result and the tree after it, or the box error that the step committed.
    """
    assert ctx.run_command is not None
    try:
        result, tree_id = ctx.run_command(str(request["tree_id"]), str(request["command"]), ())
    except _STEP_ERRORS as error:
        # The type alone: the live message and the replayed message differ in their step prefix.
        return {"error": type(error).__name__}
    return {
        "exit_code": result.exit_code,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "timed_out": result.timed_out,
        "tree_id": tree_id,
    }


def _put_files(ctx: EngineContext, request: dict[str, Any]) -> dict[str, Any]:
    """Write the child's files over a tree, with no step.

    Args:
        ctx: The engine loop's context.
        request: The files as base64 by path, and the base tree id or ``None``.

    Returns:
        The new tree id, or why the tree store refused the files.
    """
    assert ctx.put_files is not None
    files = {str(path): base64.b64decode(data) for path, data in dict(request["files"]).items()}
    try:
        return {"tree_id": ctx.put_files(files, request.get("base_tree_id"))}
    except (ValueError, LookupError) as error:
        return {"error": f"{type(error).__name__}: {error}"}


def _evaluate(task: Task, server: EvalServer, ctx: EngineContext, request: dict[str, Any]) -> dict[str, Any]:
    """Score a version on one case through the eval server, whose scorer runs on the loop.

    Args:
        task: The engine's task, whose full cases the child never holds.
        server: The eval server.
        ctx: The engine loop's context, for the feedback events.
        request: The child's candidate, its guest case and the version's id.

    Returns:
        The score and the sanitized side information, or a budget stop.

    Raises:
        ServiceError: When the guest case names no known case, or the candidate does not have the task's shape.
    """
    candidate = request.get("candidate")
    if not _candidate_shape_ok(candidate, task.str_mode):
        raise ServiceError("The AutoSaddler runner sent a candidate with the wrong shape.")
    example = request.get("example")
    case = None
    if example is not None:
        index = example.get("id") if isinstance(example, dict) else None
        if type(index) is not int or not 0 <= index < len(task.cases):
            raise ServiceError("The AutoSaddler runner named an unknown case.")
        case = task.cases[index]
    try:
        score, info = server.evaluate(candidate, case)
    except BudgetExhaustedError as error:
        return {"error": str(error) or "The scorer budget is spent.", "stop_reason": "budget_reached"}
    candidate_id = request.get("candidate_id")
    if (
        _finite(score)
        and isinstance(info, dict)
        and isinstance(candidate_id, int)
        and not isinstance(candidate_id, bool)
    ):
        emit_scorer_feedback(
            ctx.progress_callback,
            example_id="?" if case is None else str(example["id"]),
            score=float(score),
            side_info=info,
            candidate_id=str(candidate_id),
        )
    return {"score": score, "info": sanitize_side_info(info)}


def _progress(task: Task, server: EvalServer, ctx: EngineContext, event: dict[str, Any]) -> None:
    """Relay one child checkpoint to the run view: a scored case, or a scored version.

    A version event whose candidate does not have the task's shape is dropped.

    Args:
        task: The engine's task, which gives the candidate shape.
        server: The eval server, which knows the versions with a crashed case.
        ctx: The engine loop's context.
        event: The child's event.
    """
    score = event.get("score")
    candidate_id = event.get("candidate_id")
    if not _finite(score) or isinstance(candidate_id, bool) or not isinstance(candidate_id, int):
        return
    if event.get("event") == "case_scored":
        total = event.get("total")
        if isinstance(total, int) and not isinstance(total, bool):
            emit_case_scored(
                ctx.progress_callback,
                trial=candidate_id,
                example_id=str(event.get("example_id", "?")),
                score=float(score),
                total=total,
            )
        return
    candidate = event.get("candidate")
    if not _candidate_shape_ok(candidate, task.str_mode):
        return
    # The child scores a case with no score at the floor and sends no version event for that version
    # (SKY-751), so this check is only a guard.
    shown = None if server.metric_error(candidate) is not None else float(score)
    per_example = event.get("per_example")
    emit_candidate(
        ctx.progress_callback,
        candidate_id=str(candidate_id),
        parent_id=None,
        generation=0,
        score=shown,
        per_example=[]
        if shown is None
        else [
            (str(example_id), float(value))
            for example_id, value in (per_example if isinstance(per_example, list) else [])
            if _finite(value)
        ],
        candidate=candidate,
        discovered_at_evals=int(event.get("total_evals") or 0),
        iteration=None,
    )


def _finite(value: Any) -> bool:
    """Tell whether a child-sent score is a finite number.

    Args:
        value: The score.

    Returns:
        ``True`` for a finite int or float.
    """
    return isinstance(value, float | int) and not isinstance(value, bool) and math.isfinite(value)


def _json_finite(value: Any) -> Any:
    """Make a reply strict-JSON safe: each NaN or infinity becomes ``None``.

    A NaN score already reads as ``None`` in the child; side information and tool arguments keep their other values.

    Args:
        value: The reply, or a value in it.

    Returns:
        The same structure with each non-finite float as ``None``.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _json_finite(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_finite(item) for item in value]
    return value


def _result(document: dict[str, Any], task: Task, server: EvalServer) -> Result:
    """Turn the child's result document into the engine result.

    Args:
        document: The child's result.
        task: The engine's task.
        server: The eval server, which counts the evaluations.

    Returns:
        The selected version and its development score.

    Raises:
        ServiceError: When the child failed, stopped before it scored any version but the seed, or sent a best
            candidate that does not have the task's shape.
    """
    if document.get("error"):
        raise ServiceError(f"AutoSaddler failed: {document['error']}")
    if document.get("best_score") is None and (
        task.seed_candidate is None or document.get("best_candidate") != task.seed_candidate
    ):
        raise ServiceError("The upstream optimizer stopped before producing a fully evaluated candidate.")
    if not _candidate_shape_ok(document.get("best_candidate"), task.str_mode):
        raise ServiceError("The AutoSaddler runner sent a best candidate with the wrong shape.")
    metadata = {**dict(document.get("metadata") or {}), "upstream_revision": AUTOSADDLER_REVISION}
    if document.get("stop_reason"):
        metadata["stop_reason"] = document["stop_reason"]
    return Result(
        best_candidate=document["best_candidate"],
        best_score=document.get("best_score"),
        total_evals=server.used,
        metadata=metadata,
    )
