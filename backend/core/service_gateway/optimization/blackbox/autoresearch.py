"""Run Skynet's AutoResearch loop in the worker engine loop (SKY-725).

The round loop of :class:`native_engines.AutoResearchEngine` runs here unchanged:
the directives, the leaderboard, the Pareto front and the failure dossier come
from the same code. Only each round's agent session changes: the ``claude`` CLI
in a long-lived box becomes a tool loop in the worker, as in the Meta-Harness
port (SKY-722). Each model reply is one ``model`` step and each shell command is
one ``command`` step, so a killed run resumes from its last step.

The agent scores a file with an ``evaluate`` tool in place of ``eval.sh``: the
tool reads the file from the workspace tree and scores it in the worker, as the
upstream HTTP handler does. The lab is a tree that the engine builds again from
its local files before each round, so the frontier can lose a file. After a
round, only the agent's ``work/`` and ``notebook.md`` come back from the tree.

The session watchdogs count model turns and tool calls, not seconds, so a
resume stops a session at the same place. A run outside the worker engine loop
still uses the native box runtime until the SKY-728 cutover; the loop path
never calls it.
"""

from __future__ import annotations

import contextlib
import json
import math
import posixpath
import shutil
from collections.abc import Iterator
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from gepa.oa.budget import BudgetExhausted
from gepa.oa.config import OptimizeAnythingConfig
from gepa.oa.task import Task as UpstreamTask

from ....exceptions import ServiceError
from ....models.blackbox import BlackboxProposer
from ....worker.engine_loop import ModelCallFailedError
from . import loop_tools, native_engines, sandbox_log
from .feedback import emit_candidate, emit_case_scored, emit_scorer_feedback
from .native_runtime import assert_metric_absent, native_task_payload, run_native_engine, sanitize_side_info
from .protocol import (
    BudgetExhaustedError,
    EngineContext,
    EvalServer,
    FileTooLargeError,
    Result,
    Task,
    candidate_key,
)
from .runner import side_info_json_default

BASH_TOOL = {
    "name": "bash",
    "description": (
        "Run one shell command in the research lab, with no network. Each command starts in the lab root, in a "
        "fresh box: files in the lab persist between commands, but the shell state and /tmp do not. Only `work/` "
        "and `notebook.md` carry over to the next round. Returns the exit code, stdout, stderr and whether the "
        "command timed out."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"command": {"type": "string", "description": "The shell command."}},
        "required": ["command"],
    },
}

EVALUATE_TOOL = {
    "name": "evaluate",
    "description": (
        "Score a candidate file of the lab with the evaluator. With only `file`, it is a full evaluation; with "
        "`example_ids`, it is a probe on just those examples. Each scored example costs one evaluation unit. "
        "Returns the scores and the evaluator's feedback as JSON."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "file": {"type": "string", "description": "The candidate file, relative to the lab root."},
            "example_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "The examples of a probe; leave out for a full evaluation.",
            },
        },
        "required": ["file"],
    },
}

# The native session gets 90 seconds to write up after it spends its round's allowance. A clock would stop
# a resumed session at another place, so the worker counts model turns.
_WRITE_UP_TURNS = 3
# ``max_no_eval_seconds`` becomes model turns with no budget change, at a nominal 30 seconds for one model reply
# and its tool calls. The native watchdog reads a clock; a turn count stops a resumed session at the same place.
_SECONDS_PER_TURN = 30
# The worker holds the carried lab in memory at each round's tree write, so the files that come back from the tree
# are bounded: room for a few candidates of the largest size that ``evaluate`` scores, and the notebook. A file
# past the per-file cap could never be scored, so it stays behind.
_MAX_CARRY_BYTES = 4 * native_engines.MAX_CANDIDATE_BYTES


class _UpstreamServer:
    """Answer the gepa.oa server calls that AutoResearch makes with the host eval server.

    It names cases by position and versions by the order of their first full
    sweep or checkpoint, as the native runtime does. It sends the same
    ``case_scored``, ``candidate`` and feedback events, and it fills a null case
    with its sweep's mean. It scores one case at a time in the caller's thread,
    so each step has a fixed place in the agent's log. It keeps no clock values.
    """

    url = ""

    def __init__(self, server: EvalServer, task: Task, ctx: EngineContext) -> None:
        """Bind the adapter to the host eval server.

        Args:
            server: The host eval server.
            task: The task; its cases go to the scorer, never to the proposer.
            ctx: The engine context, for its progress sink.
        """
        self._server = server
        self._cases = {str(index): case for index, case in enumerate(task.cases)}
        self._progress = ctx.progress_callback
        self._sweeps: dict[str, list[tuple[str, float]]] = {}
        self._progress_log: list[dict[str, Any]] = []
        self._best_val_score = float("-inf")
        self.task = SimpleNamespace(has_dataset=bool(self._cases))
        self.budget = loop_tools.Budget(server)
        self.eval_log: list[dict[str, Any]] = []
        self._candidate_registry: dict[str, int] = {}
        # The versions with a NaN or infinite case score: a crash, as a metric error is (SKY-751).
        self._non_finite: set[str] = set()
        # ``None`` until a finite score, so a run that scored nothing has no incumbent and fails loudly.
        self.best_score: float | None = None
        self.best_candidate: Any = None

    @property
    def progress_log(self) -> list[dict[str, Any]]:
        """Return the logged checkpoints."""
        return list(self._progress_log)

    def iter_split(self, split: str) -> Iterator[tuple[str, Any]]:
        """Yield the cases of a split: all cases are the train split.

        Args:
            split: ``train`` or ``val``.

        Returns:
            ``(case id, case)`` pairs.
        """
        return iter(self._cases.items() if split == "train" else ())

    def evaluate(self, candidate: str, example: Any = None, **kwargs: Any) -> tuple[float, dict[str, Any]]:
        """Score a candidate on one case, or as a whole when the task has no cases.

        Args:
            candidate: The candidate text.
            example: The case id, or ``None`` for the whole candidate.
            **kwargs: Not used.

        Returns:
            The score, negative infinity after a metric error, and the side information a proposer may see.

        Raises:
            BudgetExhausted: When no scorer call is left.
        """
        del kwargs
        candidate_id = self._candidate_registry.get(candidate_key(candidate))
        case = None if example is None else self._cases[example]
        try:
            # The scorer's events and model calls carry the version and the case, as in the native runtime.
            with sandbox_log.event_scope(candidate=None if candidate_id is None else str(candidate_id), case=example):
                score, info = self._server.evaluate(candidate, case)
        except BudgetExhaustedError as error:
            raise BudgetExhausted(str(error)) from error
        if score is not None and not math.isfinite(score):
            self._non_finite.add(candidate_key(candidate))
        if score is not None and math.isfinite(score) and isinstance(info, dict) and candidate_id is not None:
            emit_scorer_feedback(
                self._progress,
                example_id="?" if example is None else example,
                score=float(score),
                side_info=info,
                candidate_id=str(candidate_id),
            )
        value = float("-inf") if score is None else float(score)
        self.eval_log.append({"eval": self._server.used, "score": value, "candidate_len": len(candidate)})
        if math.isfinite(value) and (self.best_score is None or value > self.best_score):
            self.best_score = value
            self.best_candidate = candidate
        # The native child gets the side information as JSON, so the proposer sees the same values here.
        shown = json.loads(json.dumps(sanitize_side_info(info), default=side_info_json_default))
        return value, shown if isinstance(shown, dict) else {"info": shown}

    def evaluate_examples(
        self, candidate: str, example_ids: list[str] | None = None, split: str | None = None
    ) -> tuple[float, dict[str, Any]]:
        """Score a candidate on cases, and fill each null case with the sweep's scored mean (SKY-367).

        Only a sweep of every case streams its scores, as in the native runtime: a probe would leave a version
        half scored in the run view.

        Args:
            candidate: The candidate text.
            example_ids: The case ids, or ``None`` for every case.
            split: Not used; AutoResearch names its cases.

        Returns:
            The mean and the per-case ``scores`` and ``infos``, with the null cases under ``UNSCORED_CASES_KEY``.

        Raises:
            BudgetExhausted: When the budget cannot score every case.
        """
        del split
        # A repeated case is scored once, so the mean is over distinct cases, as the ranking and the log read it.
        ids = (
            list(self._cases)
            if example_ids is None
            else list(dict.fromkeys(e for e in example_ids if e in self._cases))
        )
        if self.budget.remaining < len(ids):
            raise BudgetExhausted(f"Not enough budget to evaluate all examples: {self.budget.remaining} remaining")
        full = set(ids) == set(self._cases)
        trial = self._register(candidate) if full else None
        if trial is not None:
            native_engines.log_event(
                "candidate.eval.start",
                f"Evaluating candidate {trial} on {len(ids)} cases",
                candidate=trial,
                cases=len(ids),
            )
        raw: list[float] = []
        infos: dict[str, Any] = {}
        for eid in ids:
            score, info = self.evaluate(candidate, eid)
            raw.append(score)
            infos[eid] = info
            if trial is not None and math.isfinite(score):
                emit_case_scored(self._progress, trial=trial, example_id=eid, score=score, total=len(ids))
        if trial is not None:
            self._sweeps[candidate_key(candidate)] = [
                (eid, score) for eid, score in zip(ids, raw, strict=True) if math.isfinite(score)
            ]
        filled = native_engines.fill_unscored([""] * len(raw), raw)
        if not all(math.isfinite(score) for score in filled):
            # A sweep that scored no case keeps its mean and lists every case, so no engine ranks it.
            filled, unscored = raw, list(ids)
        else:
            unscored = [eid for eid, score in zip(ids, raw, strict=True) if not math.isfinite(score)]
        return sum(filled) / len(filled), {
            "scores": dict(zip(ids, filled, strict=True)),
            "infos": infos,
            "num_evaluated": len(ids),
            native_engines.UNSCORED_CASES_KEY: unscored,
        }

    def log_progress(
        self, val_score: float, candidate: str | None = None, reflection_cost: float = 0.0
    ) -> dict[str, Any]:
        """Log a checkpoint and send the version to the climb view, as the native runtime does.

        Args:
            val_score: The candidate's mean.
            candidate: The candidate text.
            reflection_cost: Not used.

        Returns:
            The score and the best score, as the gepa.oa server returns them.
        """
        del reflection_cost
        self._best_val_score = max(self._best_val_score, val_score)
        entry: dict[str, Any] = {
            "val_score": val_score,
            "best_val_score": self._best_val_score,
            "total_evals": self._server.used,
        }
        if candidate is not None:
            entry["candidate_id"] = self._register(candidate)
        self._progress_log.append(entry)
        if isinstance(candidate, str) and math.isfinite(val_score):
            sweep = self._sweeps.pop(candidate_key(candidate), [])
            # A version with a crashed case shows no score (SKY-350, SKY-367).
            shown = None if self.crashed(candidate) else val_score
            emit_candidate(
                self._progress,
                candidate_id=str(entry["candidate_id"]),
                parent_id=None,
                generation=0,
                score=shown,
                per_example=[] if shown is None else sweep,
                candidate=candidate,
                discovered_at_evals=self._server.used,
                iteration=None,
            )
        return {"val_score": val_score, "best_val_score": self._best_val_score}

    def crashed(self, candidate: str) -> bool:
        """Tell whether a version crashed on a case: a metric error, or a NaN or infinite score.

        Args:
            candidate: The version.

        Returns:
            ``True`` when the version must get no score.
        """
        return self._server.metric_error(candidate) is not None or candidate_key(candidate) in self._non_finite

    def _register(self, candidate: str) -> int:
        """Give a candidate its id: its place in the order of first registrations.

        Args:
            candidate: The candidate.

        Returns:
            The id.
        """
        return self._candidate_registry.setdefault(candidate_key(candidate), len(self._candidate_registry))


class _WorkerAutoResearch(native_engines.AutoResearchEngine):
    """The native AutoResearch loop, with each round's session as a tool loop on the engine context."""

    scorer = "the evaluate tool"

    def __init__(self, config: OptimizeAnythingConfig, ctx: EngineContext, *, max_tool_calls: int) -> None:
        """Build the native engine and bind the context.

        Args:
            config: The native engine's config.
            ctx: The engine loop's context, with its model, command and tree calls.
            max_tool_calls: The tool calls one round's session may start.
        """
        super().__init__(config)
        self._ctx = ctx
        self._max_tool_calls = max_tool_calls
        self._no_eval_turns = (
            None
            if self.max_no_eval_seconds is None
            else max(1, math.ceil(self.max_no_eval_seconds / _SECONDS_PER_TURN))
        )

    def _layout(self, task: UpstreamTask, server: Any) -> None:
        """Create the native lab, without ``eval.sh``: the ``evaluate`` tool takes its place.

        Args:
            task: The upstream task.
            server: The upstream server adapter.
        """
        super()._layout(task, server)
        (self.work_dir / "eval.sh").unlink()

    def _brief(self) -> str:
        """Return the native brief, with the ``evaluate`` tool in place of ``eval.sh``.

        Returns:
            The ``BRIEF.md`` text.
        """
        edits: list[tuple[str, ...]] = [
            ("`STATE.md`, `eval.sh`, `frontier/`", "`STATE.md`, `frontier/`"),
            (
                "- `work/` is yours: write every new candidate there.",
                "- `work/` is yours: write every new candidate there. Only `work/` and `notebook.md` carry over to "
                "the next round.",
            ),
            (
                "- `./eval.sh <file>` is a **full evaluation**",
                "- The `evaluate` tool with only a `file` is a **full evaluation**",
            ),
            ("- If `eval.sh` prints", "- If the `evaluate` tool answers"),
        ]
        if self.example_ids:
            edits.append(
                (
                    "- `./eval.sh <file> <example_id> [<example_id> ...]` is a **probe**",
                    "- The `evaluate` tool with a `file` and `example_ids` is a **probe**",
                )
            )
        return native_engines.adapt(super()._brief(), edits)

    def _session(self, server: Any, directive: str, *, max_budget_usd: float | None) -> native_engines.ProposerOutcome:
        """Run one round's session as a tool loop: one step per model reply and per command.

        The cost cap counts only committed costs, and the write-up grace, the no-evaluation watchdog, the
        tool-call cap and the prompt cap count model turns, tool calls and committed text, so a resume makes the
        same choices. As in the native harness, the session stops before the tool call past the cap; the run then
        goes on to the native stop rules. Billed failed model calls that reach the cost cap end the session as out
        of budget. A model call that fails on every attempt after the session made a tool call ends only the
        session, as the prompt cap does; one before any tool call cannot come from tool output, so it is an error.

        Args:
            server: The upstream server adapter, with the engine's recording wrappers.
            directive: This round's directive.
            max_budget_usd: Proposer spend still allowed, or ``None``.

        Returns:
            The session's outcome.
        """
        ctx = self._ctx
        assert ctx.put_files is not None
        assert ctx.call_model is not None
        start_used = server.budget.used
        quota = self._round_quota(server)
        files = {
            path.relative_to(self.work_dir).as_posix(): path.read_bytes()
            for path in sorted(self.work_dir.rglob("*"))
            if path.is_file()
        }
        # A tree write cannot delete a file, so each round starts from a new tree: the frontier can shrink.
        tree = ctx.put_files(files, None)
        messages: list[dict[str, Any]] = [{"role": "user", "content": self._round_prompt(directive)}]
        spent = Decimal(0)
        text = ""
        exhausted = False
        killed = False
        failed = False
        write_up = 0
        tool_calls = 0
        idle_turns = 0
        last_used = start_used
        while True:
            # A session at the cap would pay for a reply whose tool calls it could not run.
            if tool_calls >= self._max_tool_calls:
                killed = True
                break
            if max_budget_usd is not None and float(spent) / 100 >= max_budget_usd:
                exhausted = True
                break
            if server.budget.used != last_used:
                last_used = server.budget.used
                idle_turns = 0
            if self._no_eval_turns is not None and idle_turns >= self._no_eval_turns:
                killed = True
                break
            if quota is not None and server.budget.used - start_used >= quota:
                if write_up >= _WRITE_UP_TURNS:
                    killed = True
                    break
                write_up += 1
            # The prompt cap keeps the transcript inside the model's context. A reply with tool calls makes at least
            # one call, so the tool-call cap bounds the turns.
            if loop_tools.messages_bytes(messages) > loop_tools.MAX_PROMPT_BYTES:
                killed = True
                break
            reply, failed_cents = loop_tools.call_model(
                ctx, messages, [BASH_TOOL, EVALUATE_TOOL], spent, max_budget_usd
            )
            spent += failed_cents
            if reply is None:
                exhausted = True
                break
            if isinstance(reply, ModelCallFailedError):
                # Untrusted tool output can still push the prompt past the model's context, so after a tool call
                # the failure ends only this round; the next round starts from a new transcript.
                killed = tool_calls > 0
                failed = not killed
                text = str(reply)
                break
            idle_turns += 1
            spent += reply.cost_cents
            text = reply.text
            messages.append(
                {
                    "role": "assistant",
                    "content": reply.text,
                    **(
                        {"tool_calls": [loop_tools.openai_call(call) for call in reply.tool_calls]}
                        if reply.tool_calls
                        else {}
                    ),
                }
            )
            if not reply.tool_calls:
                break
            for call in reply.tool_calls:
                tool_calls += 1
                if tool_calls > self._max_tool_calls:
                    killed = True
                    break
                if call.get("name") == EVALUATE_TOOL["name"]:
                    output = self._evaluate(server, tree, call)
                else:
                    output, tree = loop_tools.run_bash(
                        ctx, tree, call, misuse="Call the bash tool with a command string, or the evaluate tool."
                    )
                messages.append({"role": "tool", "tool_call_id": call.get("id"), "content": output})
            if killed:
                break
        self._pull(tree)
        return native_engines.ProposerOutcome(
            session_id=f"round{self.round}",
            cost_usd=float(spent) / 100,
            is_error=killed or failed,
            text=text,
            budget_exhausted=exhausted,
            killed=killed,
        )

    def _evaluate(self, server: Any, tree: str, call: dict[str, Any]) -> str:
        """Score a lab file as the upstream HTTP handler that ``eval.sh`` calls does.

        A full sweep of every visible example logs a checkpoint; a probe does not.

        Args:
            server: The upstream server adapter, with the engine's recording wrappers.
            tree: The lab tree to read the file from.
            call: The tool call.

        Returns:
            The tool result as JSON text, cut to ``OUTPUT_BYTES`` as a command output is.
        """
        assert self._ctx.read_file is not None
        refused = loop_tools.refused_arguments(call, EVALUATE_TOOL["name"])
        if refused is not None:
            return refused
        arguments = loop_tools.arguments(call) or {}
        file = arguments.get("file")
        example_ids = arguments.get("example_ids") or None
        path = posixpath.normpath(file) if isinstance(file, str) and file else ""
        if not path or path == ".." or path.startswith(("../", "/")):
            return json.dumps({"error": "Give `file` as a path inside the lab."})
        if example_ids is not None and not (
            isinstance(example_ids, list) and all(isinstance(eid, str) for eid in example_ids)
        ):
            return json.dumps({"error": "Give `example_ids` as a list of example id strings."})
        # The candidate is copied into the observations, the archive and the frontier, so its size is bounded. A lab
        # file may hold up to ``MAX_TREE_BYTES``, so the cap refuses a larger file before it loads.
        try:
            data = self._ctx.read_file(tree, path, native_engines.MAX_CANDIDATE_BYTES)
        except FileTooLargeError:
            return json.dumps(
                {"error": f"{path} is larger than {native_engines.MAX_CANDIDATE_BYTES} bytes, the candidate cap."}
            )
        if data is None:
            return json.dumps({"error": f"No file {path} in the lab."})
        try:
            candidate = data.decode("utf-8")
        except UnicodeDecodeError:
            return json.dumps({"error": f"{path} is not UTF-8 text."})
        marker = json.dumps({"error": native_engines.BUDGET_EXHAUSTED_MARKER})
        if server.budget.remaining <= 0:
            return marker
        budget = server.budget
        try:
            if not server.task.has_dataset:
                score, info = server.evaluate(candidate)
                body: dict[str, Any] = {"score": score, "info": info}
            else:
                visible = set(self.example_ids)
                unknown = [eid for eid in example_ids or () if eid not in visible]
                if unknown:
                    return json.dumps({"error": f"Unknown or sealed example_ids: {unknown[:5]}"})
                targets = list(example_ids or self.example_ids)
                average, info = server.evaluate_examples(candidate, example_ids=targets)
                if set(targets) == visible:
                    server.log_progress(average, candidate=candidate)
                body = {
                    "average_score": average,
                    "scores": info.get("scores", {}),
                    "infos": info.get("infos", {}),
                    "num_evaluated": info.get("num_evaluated", 1),
                }
        except BudgetExhausted:
            # A sweep larger than the units left is refused, but the run ends only when none is left: the next
            # round can spend the rest on probes, as in the native run.
            return marker
        body["budget"] = {"used": budget.used, "remaining": budget.remaining}
        return loop_tools.clip(json.dumps(body))

    def _pull(self, tree: str) -> None:
        """Copy the agent's ``work/`` and ``notebook.md`` from the tree into the local lab.

        The files come in sorted order, the notebook first, and a file past the per-file cap, past the total cap,
        or that the local file system refuses stays behind. The tree decides each skip, so a resume skips the same
        files.

        Args:
            tree: The tree after the session.
        """
        assert self._ctx.read_file is not None
        assert self._ctx.list_files is not None
        work = self.work_dir / "work"
        shutil.rmtree(work)
        work.mkdir()
        total = 0
        for path in ["notebook.md", *(self._ctx.list_files(tree, "work") or [])]:
            try:
                content = self._ctx.read_file(tree, path, native_engines.MAX_CANDIDATE_BYTES)
            except FileTooLargeError:
                content = None
            local = self.work_dir / path
            if content is None or total + len(content) > _MAX_CARRY_BYTES:
                # A deleted or skipped notebook must not come back from an earlier round.
                local.unlink(missing_ok=True)
                continue
            # A path that the local file system refuses, such as one past its name length, stays behind.
            with contextlib.suppress(OSError):
                local.parent.mkdir(parents=True, exist_ok=True)
                local.write_bytes(content)
                total += len(content)


class AutoResearchEngine:
    """Run the round-based AutoResearch loop in the worker engine loop."""

    name = "autoresearch"

    def run(self, task: Task, server: EvalServer, ctx: EngineContext) -> Result:
        """Run the unchanged loop, with its round sessions on the engine context, or in the native runtime.

        Args:
            task: Optimization inputs.
            server: Skynet scorer and budget.
            ctx: The engine context; ``native_options`` gives the optional proposer knobs. With no worker
                host, the native box runtime runs the engine.

        Returns:
            The best server-verified candidate and the loop's evidence.

        Raises:
            ServiceError: When a worker-loop run is on a repository, has a proposer setting that the tool loop does
                not run, would expose the metric source, or the candidate is not one text.
            BudgetExhaustedError: When no scorer call is left.
        """
        if None in (ctx.call_model, ctx.run_command, ctx.put_files, ctx.read_file, ctx.list_files):
            # Until the SKY-728 cutover, a run outside the worker engine loop keeps the native box runtime.
            return run_native_engine(self.name, task, server, ctx)
        if ctx.repo_seed is not None:
            raise ServiceError("AutoResearch cannot run on a repository in the worker engine loop yet.")
        options = ctx.native_options
        if options is not None:
            # Until SKY-816, the tool loop is the only proposer, so a setting that it would drop fails the run.
            loop_tools.refuse_unsupported_proposer(options, "AutoResearch")
        if not task.str_mode:
            raise ServiceError("Native agent engines require a single text candidate.")
        # The native runtime checks the same document before it writes it into the box.
        assert_metric_absent(json.dumps(native_task_payload(self.name, task), default=side_info_json_default), task)
        if server.remaining <= 0:
            raise BudgetExhaustedError("The native optimizer evaluation budget is exhausted.")
        proposer = options.proposer if options is not None else BlackboxProposer()
        engine_config: dict[str, Any] = {
            "model": options.model if options else "",
            "ralph": proposer.ralph,
            "max_no_eval_seconds": proposer.max_no_eval_seconds,
        }
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
        adapter = _UpstreamServer(server, task, ctx)
        upstream = _WorkerAutoResearch(config, ctx, max_tool_calls=proposer.max_tool_calls).run(
            UpstreamTask(**native_task_payload(self.name, task)), adapter
        )
        # The incumbent always has a finite mean: the adapter keeps no non-finite best, and the ranking skips one.
        # A best version with a crashed case still has no score (SKY-350, SKY-367).
        crashed = adapter.crashed(upstream.best_candidate)
        return Result(
            best_candidate=upstream.best_candidate,
            best_score=None if crashed else upstream.best_score,
            total_evals=server.used,
            metadata=dict(upstream.metadata),
        )
