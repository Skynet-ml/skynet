"""Run the pinned Meta-Harness loop in the worker engine loop (SKY-722).

The loop of :class:`native_engines.MetaHarnessEngine` runs here unchanged. Only
its proposer session changes: the ``claude`` CLI in a long-lived box becomes a
tool loop in the worker. The tool loop is modeled on the Cloudflare Agents SDK
tool loop, with no dependency on it: call the model with the tools, run each
tool call, append the results, and call again until a reply asks for no tool.
Its one tool is modeled on the Cloudflare Sandbox SDK ``exec``: one shell
command, with the exit code, stdout and stderr as the result. Each model reply
is one ``model`` step and each command is one ``command`` step, so a killed run
resumes from its last step.

The workspace is a tree. Each command starts from the tree that the last
command left, and the engine writes its own files (task, skill, frontier,
results) over that tree before each session. After a session, the engine reads
back only ``pending_eval.json`` and the candidate files that it lists.

A run outside the worker engine loop still uses the native box runtime until
the SKY-728 cutover; the loop path never calls it.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import posixpath
from collections.abc import Iterator, Mapping
from decimal import Decimal
from typing import Any

from gepa.oa.budget import BudgetExhausted
from gepa.oa.config import OptimizeAnythingConfig
from gepa.oa.task import Task as UpstreamTask

from .... import run_log
from ....exceptions import ServiceError
from ....models.blackbox import BlackboxProposer
from ....worker.engine_loop import ModelCallFailedError
from . import loop_tools, native_engines, sandbox_log
from .feedback import emit_candidate, emit_case_scored, emit_scorer_feedback
from .native_runtime import (
    assert_metric_absent,
    native_task_payload,
    run_native_engine,
    sanitize_side_info,
)
from .protocol import (
    BudgetExhaustedError,
    Candidate,
    EngineContext,
    EvalServer,
    FileTooLargeError,
    Result,
    Task,
    candidate_key,
    fill_unscored,
)
from .runner import side_info_json_default

BASH_TOOL = {
    "name": "bash",
    "description": (
        "Run one shell command in the Meta-Harness workspace, with no network. Each command starts in the "
        "workspace root, in a fresh box: files in the workspace persist between commands, but the shell state "
        "and /tmp do not. Keep scratch files under `scratch/` in the workspace, and delete them when done. "
        "Returns the exit code, stdout, stderr and whether the command timed out."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"command": {"type": "string", "description": "The shell command."}},
        "required": ["command"],
    },
}


_TMP_SCRATCH = "Draft the change in a scratch file under `/tmp/`"
_WORKSPACE_SCRATCH = "Draft the change in a scratch file under `scratch/` in the workspace"

# Model output is untrusted, and a committed reply replays on every resume, so a crash on its arguments could
# never recover. These bounds make an over-long or deeply nested argument an error result: deep JSON makes the
# parser, ``json.dumps`` and the next prompt hash raise RecursionError.
MAX_ARGUMENT_CHARS = 200_000
MAX_ARGUMENT_DEPTH = 32
_REFUSED_ARGUMENTS = "{}"
CANDIDATE_OUTSIDE_AGENTS = "candidate file must be a relative path under agents/"

logger = logging.getLogger(__name__)


def _openai_call(call: Mapping[str, Any]) -> dict[str, Any]:
    """Give one flat tool call of the engine loop in the OpenAI form that the next prompt's history holds.

    The AutoSaddler port (SKY-723) sends the same form, so both ports send one transcript shape.

    Args:
        call: The flat ``{"id", "name", "arguments"}`` call.

    Returns:
        ``{"id", "type": "function", "function": {"name", "arguments"}}``, with the arguments as JSON text.
    """
    arguments = call.get("arguments")
    # ``_exec`` refuses these arguments, so the transcript holds an empty object in their place: an over-long text,
    # or a mapping past the depth or length bound. A shorter text stays as it is, also when its JSON is too deep.
    # A lone surrogate cannot get here: the step commit refuses it first (SKY-821).
    if isinstance(arguments, str):
        if len(arguments) > MAX_ARGUMENT_CHARS:
            arguments = _REFUSED_ARGUMENTS
    elif _too_deep(arguments):
        arguments = _REFUSED_ARGUMENTS
    else:
        # Sorted keys give the same text for a live reply and for its replay from the step log. ``_exec`` measures
        # the arguments with no escapes, so Hebrew text does not count six characters a letter.
        arguments = json.dumps(arguments, sort_keys=True, ensure_ascii=False)
        if len(arguments) > MAX_ARGUMENT_CHARS:
            arguments = _REFUSED_ARGUMENTS
    return {
        "id": call.get("id"),
        "type": "function",
        "function": {"name": call.get("name"), "arguments": arguments},
    }


def _too_deep(value: Any) -> bool:
    """Tell whether a JSON value nests deeper than ``MAX_ARGUMENT_DEPTH``, with no recursion.

    Args:
        value: The parsed JSON value.

    Returns:
        ``True`` when a list or object sits deeper than the bound.
    """
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, dict):
            item = list(item.values())
        if not isinstance(item, list):
            continue
        if depth > MAX_ARGUMENT_DEPTH:
            return True
        stack.extend((child, depth + 1) for child in item)
    return False


class _UpstreamServer:
    """Answer the gepa.oa server calls that Meta-Harness makes with the host eval server.

    It names cases by position and versions by registration order, and it sends
    the same ``case_scored`` and ``candidate`` progress events as the native
    runtime, so the climb view does not change. It scores one case at a time in
    the caller's thread, so each step has a fixed place in the agent's log.
    """

    def __init__(self, server: EvalServer, cases: list[Any], ctx: EngineContext) -> None:
        """Bind the adapter to the host eval server.

        Args:
            server: The host eval server.
            cases: The task's cases; the scorer gets these, never the proposer.
            ctx: The engine context, for its progress sink.
        """
        self._server = server
        self._cases = {str(index): case for index, case in enumerate(cases)}
        self._progress = ctx.progress_callback
        self._ids: dict[str, int] = {}
        self._sweeps: dict[str, list[tuple[str, float]]] = {}
        # The server records a metric error only for a scorer crash, so the adapter keeps the versions that
        # scored NaN or infinite on a case: those crashed too, and must get no score (SKY-751).
        self._non_finite: set[str] = set()
        self.budget = loop_tools.Budget(server)
        self.eval_log: list[dict[str, Any]] = []

    def iter_split(self, split: str) -> Iterator[tuple[str, Any]]:
        """Yield the cases of a split: all cases are the train split.

        Args:
            split: ``train`` or ``val``.

        Returns:
            ``(case id, case)`` pairs.
        """
        return iter(self._cases.items() if split == "train" else ())

    def evaluate_examples(
        self, candidate: str, example_ids: list[str] | None = None, split: str | None = None
    ) -> tuple[float, dict[str, Any]]:
        """Score a candidate on cases, and fill each null case with the sweep's scored mean (SKY-367).

        Args:
            candidate: The candidate text.
            example_ids: The case ids, or ``None`` for every case.
            split: Not used; Meta-Harness names its cases.

        Returns:
            The mean and the per-case ``scores`` and ``infos``, with the null cases under
            ``UNSCORED_CASES_KEY``.

        Raises:
            BudgetExhausted: When the budget cannot score every case.
        """
        del split
        ids = list(self._cases) if example_ids is None else [eid for eid in example_ids if eid in self._cases]
        pairs: list[tuple[str, Any]] = [(eid, self._cases[eid]) for eid in ids] or [("_single", None)]
        if self.budget.remaining < len(pairs):
            raise BudgetExhausted(f"Not enough budget to evaluate all examples: {self.budget.remaining} remaining")
        trial = self._register(candidate)
        if set(ids) == set(self._cases):
            # The native runner logs this before each full sweep, so the run log reads the same on both paths.
            native_engines.log_event(
                "candidate.eval.start",
                f"Evaluating candidate {trial} on {len(ids)} cases",
                candidate=trial,
                cases=len(ids),
            )
        scores: list[float | None] = []
        infos: dict[str, Any] = {}
        for eid, case in pairs:
            try:
                # As in the native mailbox, the scope tags the scorer's events and its model calls.
                with sandbox_log.event_scope(candidate=str(trial), case=eid):
                    score, info = self._server.evaluate(candidate, case)
            except BudgetExhaustedError as error:
                raise BudgetExhausted(str(error)) from error
            scores.append(score)
            # The engine writes these into the proposer's workspace, and a scorer error can hold a traceback
            # with metric source, so the proposer gets one line (as from the native mailbox).
            infos[eid] = sanitize_side_info(info)
            if score is not None and math.isfinite(score) and isinstance(info, dict):
                emit_scorer_feedback(
                    self._progress,
                    example_id="?" if case is None else eid,
                    score=float(score),
                    side_info=info,
                    candidate_id=str(trial),
                )
            # As in the native runner, a NaN or infinite score is no score for the climb view.
            if score is not None and math.isfinite(score) and case is not None:
                logger.info(
                    "Candidate %s scored %.4f on case %s",
                    trial,
                    float(score),
                    eid,
                    extra=run_log.event_extra(
                        source="engine",
                        event="case.scored",
                        fields={"score": float(score), "total": len(pairs)},
                        candidate=trial,
                        case=eid,
                    ),
                )
                emit_case_scored(self._progress, trial=trial, example_id=eid, score=score, total=len(pairs))
        # As in the native runner, a NaN or infinite case is a metric error: it must not make the mean
        # non-finite, and it marks the version as crashed.
        scored = [score if score is not None and math.isfinite(score) else None for score in scores]
        filled = fill_unscored([candidate] * len(scored), scored)
        names = [eid for eid, _ in pairs]
        if any(score is not None and not math.isfinite(score) for score in scores):
            self._non_finite.add(candidate_key(candidate))
        self._sweeps[candidate_key(candidate)] = [
            (eid, score) for eid, score in zip(names, scored, strict=True) if score is not None
        ]
        # A sweep that scored no case lists every case, as in the native runner, so no engine ranks it as scored.
        return sum(filled) / len(filled), {
            "scores": dict(zip(names, filled, strict=True)),
            "infos": infos,
            "num_evaluated": len(pairs),
            native_engines.UNSCORED_CASES_KEY: [eid for eid, score in zip(names, scored, strict=True) if score is None],
        }

    def log_progress(
        self, val_score: float, candidate: str | None = None, reflection_cost: float = 0.0
    ) -> dict[str, Any]:
        """Send a benchmarked candidate to the climb view, as the native runtime does.

        Args:
            val_score: The candidate's mean.
            candidate: The candidate text.
            reflection_cost: Not used.

        Returns:
            The score, as the gepa.oa server returns it.
        """
        del reflection_cost
        if isinstance(candidate, str) and math.isfinite(val_score):
            # A version with a crashed case shows no score (SKY-350, SKY-367).
            shown = None if self.crashed(candidate) else val_score
            trial = self._register(candidate)
            logger.info(
                "Candidate %s scored %s after %s evaluations",
                trial,
                "no score (a case crashed)" if shown is None else f"{shown:.4f}",
                self._server.used,
                extra=run_log.event_extra(
                    source="engine",
                    event="candidate.scored",
                    fields={"score": shown, "total_evals": self._server.used},
                    candidate=trial,
                ),
            )
            emit_candidate(
                self._progress,
                candidate_id=str(trial),
                parent_id=None,
                generation=0,
                score=shown,
                per_example=[] if shown is None else self._sweeps.get(candidate_key(candidate), []),
                candidate=candidate,
                discovered_at_evals=self._server.used,
                iteration=None,
            )
        return {"val_score": val_score}

    def crashed(self, candidate: Candidate) -> bool:
        """Tell whether a version crashed on a case: a metric error, or a NaN or infinite score.

        Args:
            candidate: The version.

        Returns:
            ``True`` when the version must get no score.
        """
        return self._server.metric_error(candidate) is not None or candidate_key(candidate) in self._non_finite

    def _register(self, candidate: Candidate) -> int:
        """Give a candidate its id: its place in the order of first sweeps.

        Args:
            candidate: The candidate.

        Returns:
            The id.
        """
        return self._ids.setdefault(candidate_key(candidate), len(self._ids))


class _WorkerMetaHarness(native_engines.MetaHarnessEngine):
    """The native Meta-Harness loop, with the proposer session as a tool loop on the engine context."""

    record_timing = False

    def __init__(self, config: OptimizeAnythingConfig, ctx: EngineContext, max_tool_calls: int | None = None) -> None:
        """Build the native engine and bind the context.

        Args:
            config: The native engine's config.
            ctx: The engine loop's context, with its model, command and tree calls.
            max_tool_calls: The most tool calls in one session; ``None`` takes the proposer default.
        """
        super().__init__(config)
        self._ctx = ctx
        self._tree: str | None = None
        # The tree holds the agent's candidate files: the local copies serve only ``_validate`` and the benchmark,
        # so they never go back up over the tree, where the agent may have changed or deleted them since.
        self._pulled: set[str] = set()
        self._oversize: set[str] = set()
        self._max_tool_calls = BlackboxProposer().max_tool_calls if max_tool_calls is None else max_tool_calls

    def skill(self) -> str:
        """Return the native skill, with scratch files in the workspace: ``/tmp`` does not persist between commands.

        Returns:
            The skill text for the tool loop.
        """
        return super().skill().replace(_TMP_SCRATCH, _WORKSPACE_SCRATCH)

    def _propose(self, iteration: int, remaining_cost: float | None) -> native_engines.ProposerOutcome:
        """Run one proposer session as a tool loop: one step per model reply and per command.

        The proposer cap counts only committed costs, so a resume makes the same choices. The session also ends
        as out of budget after the proposer's ``max_tool_calls`` commands, at a prompt over
        ``loop_tools.MAX_PROMPT_BYTES``, or at a model call that fails after the session ran a command; all depend
        only on the committed replies, results and failures, so a resume ends it at the same place.

        Args:
            iteration: One-based iteration number.
            remaining_cost: Proposer spend still allowed, or ``None``.

        Returns:
            The session's outcome.
        """
        ctx = self._ctx
        assert ctx.put_files is not None
        assert ctx.call_model is not None
        pending = self.logs_dir / "pending_eval.json"
        # A tree write cannot delete a file, so an empty file stands for no candidates.
        pending.write_text("", encoding="utf-8")
        files = {
            path.relative_to(self.work_dir).as_posix(): path.read_bytes()
            for path in sorted(self.work_dir.rglob("*"))
            if path.is_file() and path.relative_to(self.work_dir).as_posix() not in self._pulled
        }
        # A tree holds only files, so an empty folder of the layout (such as ``logs/run/reports``) gets a marker
        # file; without it, the folder is missing and the agent's first write into it fails.
        files.update(
            {
                f"{path.relative_to(self.work_dir).as_posix()}/.keep": b""
                for path in sorted(self.work_dir.rglob("*"))
                if path.is_dir() and not any(path.iterdir())
            }
        )
        tree = ctx.put_files(files, self._tree)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.skill()},
            {"role": "user", "content": self._task_prompt(iteration)},
        ]
        spent = Decimal(0)
        text = ""
        exhausted = False
        failed = False
        commands = 0
        while not exhausted:
            if remaining_cost is not None and float(spent) / 100 >= remaining_cost:
                exhausted = True
                break
            # A reply with tool calls runs at least one command, so the command cap also bounds the turns.
            # A session at a cap ends as out of budget, so its iteration still reads ``pending_eval.json``, and the
            # run does not stop.
            if commands >= self._max_tool_calls or loop_tools.messages_bytes(messages) > loop_tools.MAX_PROMPT_BYTES:
                exhausted = True
                break
            reply, failed_cents = loop_tools.call_model(ctx, messages, [BASH_TOOL], spent, remaining_cost)
            spent += failed_cents
            if reply is None:
                exhausted = True
                break
            if isinstance(reply, ModelCallFailedError):
                # Untrusted output can still push the prompt past the model's context. After a command, the
                # session may have written candidates, so it ends as out of budget and the run goes on. A failure
                # before any command cannot come from command output, so it stops the run.
                exhausted = commands > 0
                failed = not exhausted
                text = str(reply)
                break
            spent += reply.cost_cents
            text = reply.text
            messages.append(
                {
                    "role": "assistant",
                    "content": reply.text,
                    **({"tool_calls": [_openai_call(call) for call in reply.tool_calls]} if reply.tool_calls else {}),
                }
            )
            if not reply.tool_calls:
                break
            for call in reply.tool_calls:
                if commands >= self._max_tool_calls:
                    exhausted = True
                    break
                commands += 1
                output, tree = self._exec(tree, call)
                messages.append({"role": "tool", "tool_call_id": call.get("id"), "content": output})
        self._tree = tree
        self._pull(tree)
        return native_engines.ProposerOutcome(
            session_id=f"iter{iteration}",
            cost_usd=float(spent) / 100,
            is_error=failed,
            text=text,
            budget_exhausted=exhausted,
            killed=False,
        )

    def _exec(self, tree: str, call: dict[str, Any]) -> tuple[str, str]:
        """Run one tool call as one command over the tree, modeled on the Sandbox SDK ``exec``.

        Args:
            tree: The workspace tree.
            call: The tool call: its ``name`` and its ``arguments``, as a mapping or JSON text.

        Returns:
            The tool result as JSON text, and the tree after the command.
        """
        assert self._ctx.run_command is not None
        arguments = call.get("arguments")
        if isinstance(arguments, str):
            if len(arguments) > MAX_ARGUMENT_CHARS:
                return self._refuse_arguments(tree)
            try:
                arguments = json.loads(arguments)
            # JSON nested past the parser's recursion limit raises RecursionError.
            except (ValueError, RecursionError):
                arguments = None
        if _too_deep(arguments):
            return self._refuse_arguments(tree)
        # The protocol gives valid JSON object arguments as a mapping, so the text bound above never sees them.
        if isinstance(arguments, dict) and len(json.dumps(arguments, ensure_ascii=False)) > MAX_ARGUMENT_CHARS:
            return self._refuse_arguments(tree)
        command = arguments.get("command") if isinstance(arguments, dict) else None
        if call.get("name") != BASH_TOOL["name"] or not isinstance(command, str):
            # A bad tool call goes back to the model as an error result, as in the Agents SDK tool loop.
            return json.dumps(
                {"error": f"Call the {BASH_TOOL['name']} tool with a command string."}, ensure_ascii=False
            ), tree
        return loop_tools.run_command(self._ctx, tree, command)

    @staticmethod
    def _refuse_arguments(tree: str) -> tuple[str, str]:
        """Give the error result for tool call arguments past the length or depth bound.

        Args:
            tree: The workspace tree, which does not change.

        Returns:
            The error result as JSON text, and the tree.
        """
        error = (
            f"Send the {BASH_TOOL['name']} tool arguments as JSON of at most {MAX_ARGUMENT_CHARS} characters, "
            f"nested at most {MAX_ARGUMENT_DEPTH} levels."
        )
        return json.dumps({"error": error}, ensure_ascii=False), tree

    def _pull(self, tree: str) -> None:
        """Copy ``pending_eval.json`` and the candidate files that it lists from the tree into the work folder.

        A candidate file past ``MAX_CANDIDATE_BYTES`` gets no local copy: ``_validate`` refuses it by its size.

        Args:
            tree: The tree after the session.
        """
        assert self._ctx.read_file is not None
        logs = self.logs_dir.relative_to(self.work_dir).as_posix()
        pending = self.logs_dir / "pending_eval.json"
        self._oversize = set()
        # The engine runs in the worker process, and parsing a large file costs many times its size in memory.
        # An empty file is a malformed one: the iteration gets no candidate.
        try:
            data = self._ctx.read_file(tree, f"{logs}/pending_eval.json", native_engines.MAX_PENDING_BYTES)
        except FileTooLargeError:
            data = b""
        if data is None:
            pending.unlink(missing_ok=True)
            return
        pending.write_bytes(data)
        for spec in self._read_pending(pending):
            file = spec.get("file")
            if not isinstance(file, str):
                continue
            path = posixpath.normpath(file)
            # ``_validate`` refuses a file outside ``agents/``, or a path with a null byte (no file system path
            # holds one), so only the others come back.
            if not path.startswith("agents/") or "\0" in path:
                continue
            local = self.work_dir / path
            # A tree file may hold up to ``MAX_TREE_BYTES``, so the cap refuses a larger file before it loads.
            try:
                content = self._ctx.read_file(tree, path, native_engines.MAX_CANDIDATE_BYTES)
            except FileTooLargeError:
                content = None
                self._oversize.add(path)
            # An entry that names a local folder, or a path under a local file, cannot be a candidate
            # file; ``_validate`` refuses it.
            with contextlib.suppress(OSError):
                if content is None:
                    local.unlink(missing_ok=True)
                else:
                    local.parent.mkdir(parents=True, exist_ok=True)
                    local.write_bytes(content)
                    self._pulled.add(path)

    def _validate(self, spec: dict[str, Any]) -> str | None:
        """Check a candidate spec as the native engine does, and refuse a file that ``_pull`` does not copy.

        An over-cap file left in the tree gets the size reason.

        Args:
            spec: One ``pending_eval.json`` candidate entry.

        Returns:
            A failure reason, or ``None`` when the candidate can be benchmarked.
        """
        failure = super()._validate(spec)
        file = spec.get("file")
        if failure not in (None, native_engines.CANDIDATE_MISSING) or not isinstance(file, str):
            return failure
        path = posixpath.normpath(file)
        # ``_pull`` copies only these paths from the tree, so another path (an absolute one into ``agents/``
        # included) could reach a stale local copy and not the tree's file.
        if not path.startswith("agents/"):
            return CANDIDATE_OUTSIDE_AGENTS
        # ``_pull`` writes no local copy of an over-cap file, so the native check finds no file.
        if failure == native_engines.CANDIDATE_MISSING and path in self._oversize:
            return native_engines.CANDIDATE_TOO_LARGE
        return failure


class MetaHarnessEngine:
    """Run the pinned Meta-Harness loop in the worker engine loop."""

    name = "meta_harness"

    def run(self, task: Task, server: EvalServer, ctx: EngineContext) -> Result:
        """Run the unchanged loop, with its proposer sessions on the engine context, or in the native runtime.

        Args:
            task: Optimization inputs.
            server: Skynet scorer and budget.
            ctx: The engine context; ``native_options`` gives the optional proposer knobs. With no worker
                host, the native box runtime runs the engine.

        Returns:
            The frontier's best candidate and the loop's evidence.

        Raises:
            ServiceError: When a worker-loop run is on a repository, has a proposer setting that the tool loop does
                not run, would expose the metric source, or the candidate is not one text.
            BudgetExhaustedError: When no scorer call is left.
        """
        if None in (ctx.call_model, ctx.run_command, ctx.put_files, ctx.read_file):
            # Until the SKY-728 cutover, a run outside the worker engine loop keeps the native box runtime.
            return run_native_engine(self.name, task, server, ctx)
        if ctx.repo_seed is not None:
            raise ServiceError("Meta-Harness cannot run on a repository in the worker engine loop yet.")
        options = ctx.native_options
        if options is not None:
            # Until SKY-816, the tool loop is the only proposer, so a setting that it would drop fails the run.
            loop_tools.refuse_unsupported_proposer(options, "Meta-Harness")
        if not task.str_mode:
            raise ServiceError("Native agent engines require a single text candidate.")
        # The native runtime checks the same document before it writes it into the box.
        assert_metric_absent(json.dumps(native_task_payload(self.name, task), default=side_info_json_default), task)
        if server.remaining <= 0:
            raise BudgetExhaustedError("The native optimizer evaluation budget is exhausted.")
        engine_config: dict[str, Any] = {"model": options.model if options else ""}
        if ctx.max_iterations is not None:
            engine_config["max_iterations"] = ctx.max_iterations
        if options is not None and options.proposer.max_candidates_per_iter is not None:
            engine_config["max_candidates_per_iter"] = options.proposer.max_candidates_per_iter
        config = OptimizeAnythingConfig(
            engine=self.name,
            max_token_cost=options.max_token_cost if options else None,
            max_evals=server.remaining,
            max_concurrency=1,
            run_dir=ctx.run_dir,
            stop_at_score=ctx.stop_at_score,
            sandbox=False,
            engine_config=engine_config,
        )
        worker = _WorkerMetaHarness(config, ctx, options.proposer.max_tool_calls if options else None)
        adapter = _UpstreamServer(server, list(task.cases), ctx)
        upstream = worker.run(UpstreamTask(**native_task_payload(self.name, task)), adapter)
        # A best version with no finite mean, or with a crashed case, has no score (SKY-350, SKY-367, SKY-751).
        scored = math.isfinite(upstream.best_score) and not adapter.crashed(upstream.best_candidate)
        return Result(
            best_candidate=upstream.best_candidate,
            best_score=upstream.best_score if scored else None,
            total_evals=server.used,
            metadata={**upstream.metadata, "upstream_revision": native_engines.META_HARNESS_REVISION},
        )
