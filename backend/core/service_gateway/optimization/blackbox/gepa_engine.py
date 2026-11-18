"""GEPA engine: runs ``gepa.optimize_anything`` against the eval server.

Overlays only what Skynet owns — the scorer budget, the per-lane workspace,
the job-log logger and the stop-at-score stopper — on GEPA's defaults; the
eval server is the single scoring path, exactly like upstream ``gepa.oa``.

A repository run on the worker engine loop (SKY-720) runs GEPA in the worker.
A repository version is a tree id. The proposer asks the loop's model for each
new version with no box open. It reads and writes files with no box: a tree id
is the hash of its content, so a resume that writes the same files gets the
same tree. Only the model's shell commands and the scoring open a box. Each
model reply and each command is one step, so a resumed run replays GEPA to the
same population.
"""

from __future__ import annotations

import functools
import logging
import math
import re
import shutil
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

from gepa.core.result import GEPAResult
from gepa.core.state import GEPAState
from gepa.gepa_launcher import EngineConfig, GEPAConfig, ReflectionConfig, TrackingConfig, optimize_anything

from ....billing.usage_tags import usage_scope
from ....constants import OPTIMIZER_NAME_GEPA, PROGRESS_CANDIDATE
from ....exceptions import ServiceError
from ....sandbox.timeouts import Phase
from ....sandbox.trees import BoxFailureError, GrantDeniedError, TreeRejectedError
from ..budget_stop import BudgetReached
from ..trajectory import capture_proposal_prompts, current_proposal_iteration, trajectory_watch
from .feedback import STR_CANDIDATE_KEY, emit_scorer_feedback, gepa_side_info
from .harness_bridge import hashed_kind
from .native_engines import ScoredThresholdStopper, record_text
from .native_runtime import run_native_engine
from .protocol import (
    BudgetExhaustedError,
    Candidate,
    EngineContext,
    EvalServer,
    RepoSeed,
    Result,
    ScorerAbortError,
    Task,
    engine_score,
    fill_unscored,
    score_batch,
)
from .repo_tree import EXPLOITS_DIR, inside
from .upstream import GEPA_SOURCE
from .workspace_scorer import run_on_pool

logger = logging.getLogger(__name__)
# One proposal's model replies; the proposer keeps the parent when they run out.
_MAX_TURNS = 6
# A command's stdout and stderr, a read file and a folder listing are each cut to this many characters.
_OUTPUT_CHARS = 4000
_FEEDBACK_RECORDS = 5
# The error names the path, and a hidden path must not reach the proposer (SKY-353), so the note is fixed.
_REJECTED_NOTE = "The command wrote a path that it may not write. Its output was discarded."
# A block closes at a line with its own fence, so a file that holds a shorter fence can use a longer one.
_BLOCK = re.compile(r"^(`{3,})(sh|read|ls|file)[ \t]*([^\n]*)\n(.*?)^\1[ \t]*$", re.DOTALL | re.MULTILINE)
# These errors end the run. The loop wraps its own stops in ``ScorerAbortError``.
# The loop commits a grant list with no network policy as the command's own failure, which it calls no stop.
# The proposer sends the same empty grant list with each command, so every later command would fail the same way.
# A replay raises the committed error again at the same step, so the live run and its resume stop at the same place.
_STOP_ERRORS = (ScorerAbortError, GrantDeniedError, BudgetExhaustedError)


class _JobLogger:
    """GEPA ``LoggerProtocol`` adapter that writes into the job log."""

    def log(self, message: str, *args: Any, **kwargs: Any) -> None:
        """Forward one GEPA log line to the job logger.

        Args:
            message: The line GEPA wants logged.
            *args: Ignored; kept for protocol tolerance.
            **kwargs: Ignored; kept for protocol tolerance.
        """
        logger.info("%s", message)


def _version(candidate: Any, str_mode: bool) -> Any:
    """Return the version the server keyed for a GEPA program candidate.

    Args:
        candidate: A GEPA program candidate.
        str_mode: Whether the task optimizes one text.

    Returns:
        The text in text mode, else the components mapping.
    """
    return candidate[STR_CANDIDATE_KEY] if str_mode and isinstance(candidate, dict) else candidate


def _scored_progress(
    callback: Callable[[str, dict[str, Any]], None] | None, server: EvalServer, str_mode: bool
) -> Callable[[str, dict[str, Any]], None] | None:
    """Wrap a progress callback so a crashed version's live event shows no score.

    GEPA ranks by filled means, so its state holds a filled mean for a
    version that crashed on a case. Final selection drops that version
    (SKY-350), so the live tree must not show the filled mean (SKY-367).

    Args:
        callback: The job's progress callback, or ``None``.
        server: The server that records each version's metric error.
        str_mode: Whether the task optimizes one text.

    Returns:
        The wrapped callback, or ``None`` when there is no callback.
    """
    if callback is None:
        return None

    def forward(event: str, metrics: dict[str, Any]) -> None:
        """Forward one event, with no score for a candidate that crashed on a case.

        Args:
            event: The progress event name.
            metrics: The event payload.
        """
        if event == PROGRESS_CANDIDATE and server.metric_error(_version(metrics.get("prompt"), str_mode)) is not None:
            metrics = {**metrics, "score": None, "per_example": []}
        callback(event, metrics)

    return forward


class GepaEngine:
    """Reflective prompt-evolution engine backed by ``optimize_anything``."""

    name = "gepa"

    def run(self, task: Task, server: EvalServer, ctx: EngineContext) -> Result:
        """Optimize ``task`` with GEPA, scoring only through ``server``.

        Args:
            task: The starting point, goal and cases.
            server: The budgeted scorer for this lane.
            ctx: Reflection LM, workspace and stop settings.

        Returns:
            GEPA's validation-best version, or the server's best when the
            budget ran out before GEPA could report.
        """
        proposer = _LoopRepoProposer.bind(ctx, task)
        # On the loop a version is a tree id, so GEPA starts from a tree.
        start = proposer.start_tree() if proposer is not None else task.seed_candidate
        if server.remaining <= 0:
            return Result(best_candidate=start or "", best_score=None, total_evals=0)
        if proposer is None and ctx.native_options is not None and ctx.native_options.repo is not None:
            # Until the cutover (SKY-728) binds every run to a loop, a coding agent writes each
            # version inside the native runtime's checkout, where GEPA runs with it as its proposer.
            return run_native_engine("gepa_repo", task, server, ctx)
        run_dir = str(Path(ctx.run_dir) / self.name)
        if proposer is not None:
            # GEPA resumes from a state file in its run folder. A loop resume replays every committed step
            # from the start instead, so an old state would skip steps and the replay would diverge.
            shutil.rmtree(run_dir, ignore_errors=True)
        Path(run_dir).mkdir(parents=True, exist_ok=True)
        stop_callbacks: list[Any] = []
        if proposer is not None:
            stop_callbacks.append(lambda _state: proposer.failure is not None)
        if ctx.stop_at_score is not None:
            stop_callbacks.append(
                ScoredThresholdStopper(
                    ctx.stop_at_score,
                    lambda candidate: server.metric_error(_version(candidate, task.str_mode)) is not None,
                )
            )
        config = GEPAConfig(
            engine=EngineConfig(
                run_dir=run_dir,
                seed=ctx.seed,
                max_metric_calls=server.remaining,
                # On the loop, GEPA reads the proposer's ``total_cost``: the committed cost of its model steps.
                max_reflection_cost=ctx.proposer_token_budget_usd,
                # ``batch_evaluator`` is GEPA's only scoring path here, and it
                # fans a batch out itself up to ``ctx.concurrency``. GEPA's own
                # pool would only nest a second pool over it.
                parallel=False,
                display_progress_bar=False,
            ),
            reflection=(
                ReflectionConfig(reflection_lm=None, custom_candidate_proposer=proposer)
                if proposer is not None
                else ReflectionConfig(reflection_lm=_LaneReflection(ctx.reflection_lm, ctx.check_budget))
            ),
            tracking=TrackingConfig(logger=_JobLogger()),
            stop_callbacks=stop_callbacks or None,
        )

        # GEPA hands the dataset rows back by identity and keys each candidate's
        # per-case scores by the case's index. Feedback carries that same index
        # so the tree drawer can pair the scorer's notes with a case's score cell.
        example_ids = {id(example): str(index) for index, example in enumerate([*task.cases, *task.valset])}

        def score_one(candidate: Candidate, example: Any = None) -> tuple[float | None, dict[str, Any]]:
            """Route one GEPA evaluation through the budgeted server and report its feedback.

            Args:
                candidate: The version GEPA wants scored.
                example: The case, or ``None`` in single-task mode.

            Returns:
                The score (``None`` after a metric error) and side information,
                named scores as plain numbers for GEPA's objective frontier and
                their feedback beside them.
            """
            if ctx.check_budget is not None:
                ctx.check_budget()
            # The run record keys a GEPA version's calls by the iteration that proposed it,
            # as its candidate event does; seed and valset sweeps have none and stay run-wide.
            with usage_scope(candidate=current_proposal_iteration()):
                score, side_info = server.evaluate(candidate, example)
            if ctx.check_budget is not None:
                ctx.check_budget()
            emit_scorer_feedback(
                ctx.progress_callback,
                example_id=example_ids.get(id(example), "?"),
                score=score,
                side_info=side_info,
            )
            return score, gepa_side_info(side_info)

        def evaluator(candidate: Candidate, example: Any = None) -> tuple[float, dict[str, Any]]:
            """Score one case alone, as GEPA's single-pair calls do.

            Args:
                candidate: The version GEPA wants scored.
                example: The case, or ``None`` in single-task mode.

            Returns:
                The score, :data:`UNSCORED` after a metric error, and side information.
            """
            score, side_info = score_one(candidate, example)
            return engine_score(score), side_info

        def batch_evaluator(pairs: list[tuple[Candidate, Any]]) -> list[tuple[float, dict[str, Any]]]:
            """Score one GEPA batch and fill each null case with its version's scored mean.

            GEPA averages a version's batch itself. A filled case leaves that
            mean at the scored cases' mean, so a case that always crashes does
            not hide which version is better (SKY-367).

            Args:
                pairs: The ``(candidate, case)`` pairs of one GEPA batch.

            Returns:
                One ``(score, side_info)`` per pair, in order.
            """
            # Text targets keep the serial loop. Agent targets run each case in
            # its own sandbox, so their batch runs ``ctx.concurrency`` wide on the
            # sandbox pool.
            if ctx.concurrency <= 1:
                return score_batch(score_one, pairs, 1)
            calls = [functools.partial(score_one, candidate, example) for candidate, example in pairs]
            results = run_on_pool(Phase.SCORE, calls, ctx.concurrency)
            filled = fill_unscored([candidate for candidate, _ in pairs], [score for score, _ in results])
            return [(score, info) for score, (_, info) in zip(filled, results, strict=True)]

        kwargs: dict[str, Any] = {
            "seed_candidate": start,
            "evaluator": evaluator,
            "batch_evaluator": batch_evaluator,
            "config": config,
        }
        # Train cases drive reflection and the validation cases rank versions.
        # Without a validation split GEPA ranks on the train cases; the test
        # split never reaches GEPA, the service's final run scores it.
        if task.cases:
            kwargs["dataset"] = task.cases
            kwargs["valset"] = task.valset or task.cases
        if task.objective:
            kwargs["objective"] = task.objective
        if task.background:
            kwargs["background"] = task.background

        # The watcher tails run_dir/gepa_state.bin and forwards each accepted
        # candidate as a live progress event, the channel the trajectory tree
        # streams through. The proposal capture pins scorer feedback to the
        # iteration that asked for it and snapshots rejected proposals' text.
        # A loop resume sends the events again until SKY-808 makes their write idempotent.
        with (
            capture_proposal_prompts(OPTIMIZER_NAME_GEPA),
            trajectory_watch(run_dir, _scored_progress(ctx.progress_callback, server, task.str_mode)),
        ):
            try:
                gepa_result: GEPAResult[Any, Any] | None = optimize_anything(**kwargs)
            except BudgetReached as exc:
                recovered = _load_result_from_state(run_dir, seed=ctx.seed, str_mode=task.str_mode)
                if recovered is not None:
                    candidate = recovered.best_candidate
                    score = _val_score(recovered.val_aggregate_scores[recovered.best_idx])
                    exc.result = Result(
                        best_candidate=candidate,
                        best_score=score,
                        total_evals=server.used,
                        metadata={"upstream_source": GEPA_SOURCE, "selection_source": "gepa_checkpoint"},
                    )
                    exc.evidence.update(
                        selection_scope="validation",
                        final_evaluation_completed=False,
                        final_evaluation_reason="budget_reached",
                    )
                raise
            except BudgetExhaustedError:
                gepa_result = _load_result_from_state(run_dir, seed=ctx.seed, str_mode=task.str_mode)
        if proposer is not None and proposer.failure is not None:
            # GEPA logs a proposer's error and goes on, so the stop comes back here.
            raise proposer.failure

        if gepa_result is None:
            if start is None:
                raise ServiceError("GEPA stopped before producing a fully evaluated candidate.")
            return Result(best_candidate=start, best_score=None, total_evals=server.used)

        best: Any = gepa_result.best_candidate
        if isinstance(best, dict) and task.str_mode:
            best = next(iter(best.values()), "")
        return Result(
            best_candidate=best,
            best_score=_val_score(gepa_result.val_aggregate_scores[gepa_result.best_idx]),
            total_evals=server.used,
            metadata={
                "upstream_source": GEPA_SOURCE,
                "candidates": len(gepa_result.candidates),
                "gepa_metric_calls": gepa_result.total_metric_calls,
                "candidate_tree": _candidate_tree(gepa_result, str_mode=task.str_mode),
            },
        )


class _LoopRepoProposer:
    """GEPA ``custom_candidate_proposer`` that writes repository versions with the loop's model.

    A version is a tree id. The model reads files and writes whole files with no box, and each of its shell
    commands opens one box. GEPA's cost stopper reads :attr:`total_cost`.
    """

    def __init__(self, ctx: EngineContext, seed: RepoSeed, task: Task) -> None:
        """Record the loop's calls, the starting tree with its path rules, and the task.

        Args:
            ctx: The loop's engine context, with ``call_model``, ``run_command``, ``put_files``,
                ``read_file`` and ``list_files``.
            seed: The starting tree and its path rules.
            task: The task, for its goal and background.
        """
        self.ctx = ctx
        self.seed = seed
        self.task = task
        self.failure: Exception | None = None
        self.cost_cents = Decimal(0)

    @classmethod
    def bind(cls, ctx: EngineContext, task: Task) -> _LoopRepoProposer | None:
        """Return a proposer when the context comes from an engine loop that holds a repository seed.

        Args:
            ctx: The engine context.
            task: The task.

        Returns:
            The proposer, or ``None`` when the context has no seed or no loop calls.
        """
        calls = (ctx.call_model, ctx.run_command, ctx.put_files, ctx.read_file, ctx.list_files)
        if ctx.repo_seed is None or any(call is None for call in calls):
            return None
        return cls(ctx, ctx.repo_seed, task)

    def start_tree(self) -> str:
        """Return the tree GEPA starts from: the task's seed when it has one, else the run's seed tree.

        Auto's continuation gives the winner's tree id as the task's seed. The check reads the tree store with no
        box and no step, and a tree id is the hash of its content, so a resume makes the same choice.

        Returns:
            The task's seed tree id, or the run's seed tree when the task's seed is empty.

        Raises:
            ServiceError: When the task's seed is not empty and is not a tree that the run can see.
        """
        seed = self.task.seed_candidate
        if seed is None or seed == "":
            return self.seed.tree_id
        try:
            visible = isinstance(seed, str) and self.ctx.list_files(seed, "") is not None
        except LookupError:
            visible = False
        if not visible:
            # A silent fallback would optimize the run's seed in place of the version that the caller gave.
            raise ServiceError("The GEPA seed is not a version of this run.")
        return seed

    @property
    def total_cost(self) -> float:
        """Return the cost of this proposer's successful model replies in US dollars, as the step log holds it."""
        # A failed call that the provider billed commits its cost on the step log, but the proposer gets
        # ``ModelCallFailedError`` with no cost, so this total and GEPA's ``max_reflection_cost`` cap miss it.
        # The fix belongs in the engine loop (SKY-719): the error must carry the committed cost.
        return float(self.cost_cents / 100)

    def __call__(
        self,
        candidate: dict[str, str],
        reflective_dataset: dict[str, list[dict[str, Any]]],
        components_to_update: list[str],
        **_: Any,
    ) -> dict[str, str]:
        """Let the model read the parent version and write a new one.

        Args:
            candidate: The parent version's tree id, keyed by GEPA's single text component.
            reflective_dataset: Per-component records of the cases GEPA reflects on.
            components_to_update: The component GEPA asks to change.
            **_: GEPA's other proposer arguments, not used.

        Returns:
            The new version's tree id under the same component key; the parent when no reply wrote valid files
            or a loop call failed with an error that does not stop the run.

        Raises:
            ScorerAbortError: When the loop stopped; the engine reads it back after GEPA returns.
            GrantDeniedError: When a command's grant list does not resolve to a network policy; the engine reads it
                back too.
        """
        if self.failure is not None:
            raise self.failure
        key = components_to_update[0] if components_to_update else next(iter(candidate))
        parent = candidate[key]
        messages: list[dict[str, Any]] = [{"role": "user", "content": self.brief(reflective_dataset.get(key) or [])}]
        try:
            for _turn in range(_MAX_TURNS):
                reply = self.ctx.call_model(messages, None)
                self.cost_cents += reply.cost_cents
                messages.append({"role": "assistant", "content": reply.text})
                blocks = list(_BLOCK.finditer(reply.text))
                files = {block[3].strip(): block[4] for block in blocks if block[2] == "file"}
                if files:
                    note, tree_id = self.write(parent, files)
                    if tree_id is not None:
                        return {key: tree_id}
                elif not blocks:
                    note = "Reply with one ```ls block, one ```read block, one ```sh block, or ```file blocks."
                elif blocks[0][2] == "ls":
                    note = self.listing(parent, blocks[0][4].strip())
                elif blocks[0][2] == "read":
                    note = self.read(parent, blocks[0][4].strip())
                else:
                    note = self.command(parent, blocks[0][4])
                messages.append({"role": "user", "content": note})
        except _STOP_ERRORS as error:
            self.failure = error
            raise
        except Exception as error:
            # GEPA retries a failed batch from its first model call, which pays for each reply again. A loop
            # error replays from its committed step, so keeping the parent gives the same population on a resume.
            logger.info("GEPA proposal kept its parent after %s", type(error).__name__)
        return {key: parent}

    def brief(self, records: list[dict[str, Any]]) -> str:
        """Brief one proposal: the goal, the path rules, how the current version scored and the reply rules.

        Args:
            records: GEPA's reflective records for the cases this proposal targets.

        Returns:
            The first message of the proposal.
        """
        editable = ", ".join("the whole repository" if p == "." else f"`{p}`" for p in self.seed.editable_paths)
        lines = [
            "You are improving a code repository.",
            "",
            f"Goal: {self.task.objective or 'raise the score the evaluator gives this repository.'}",
        ]
        if self.task.background:
            lines += ["", "Background:", self.task.background]
        lines += ["", f"You may change: {editable or 'nothing'}. Leave everything else as it is."]
        # A hidden path must not reach the proposer, also when the repository rules name it (SKY-353).
        constrained = [path for path in self.seed.constrained_paths if not self.hidden(path)]
        readonly = [path for path in self.seed.readonly_paths if not self.hidden(path)]
        if constrained:
            lines.append("The repository rules protect: " + ", ".join(constrained) + ".")
        if readonly:
            lines.append("Submodules and Git LFS files stay as fetched: " + ", ".join(readonly) + ".")
        lines += ["", "How the current version scored, with the scorer's feedback and each named score's feedback:"]
        lines += [f"{index}. {record_text(record)}" for index, record in enumerate(records[:_FEEDBACK_RECORDS], 1)]
        if not records:
            lines.append("No feedback was recorded.")
        lines += [
            "",
            "Each reply holds fenced blocks of one kind:",
            "- An ```ls block holds one folder path, or nothing for the root. You get the files under it.",
            "- A ```read block holds one file path. You get the file of the current version.",
            "- A ```sh block runs one shell command in a checkout of the current version and shows you its "
            "output. Use it to run tests. The command's changes are not kept. When the command writes a path "
            "that it may not write, you get no output.",
            "- ```file <path> blocks are your answer: each one holds the whole new text of one file. The new "
            "version is the current version with these files. Use a longer fence when a file holds ```.",
            f"You have {_MAX_TURNS} replies. You cannot run the evaluator.",
        ]
        return "\n".join(lines)

    def read(self, tree_id: str, path: str) -> str:
        """Read one file of the parent version with no box.

        Args:
            tree_id: The parent version's tree.
            path: The model's path.

        Returns:
            The note that holds the file, or why it cannot be read.
        """
        try:
            # The loop's ``read_file`` gives ``None`` for a hidden path, so a hidden file reads as missing (SKY-353).
            data = self.ctx.read_file(tree_id, path)
        except LookupError:
            data = None
        if data is None:
            return f"'{path}' is not a file of the current version."
        return f"{path}:\n{_cut(data.decode('utf-8', errors='replace'))}"

    def listing(self, tree_id: str, folder: str) -> str:
        """List the files under one folder of the parent version with no box.

        Args:
            tree_id: The parent version's tree.
            folder: The model's folder path; empty or ``.`` means the root.

        Returns:
            The note that holds the file paths, or why the folder cannot be listed.
        """
        try:
            # The loop's ``list_files`` leaves out the hidden paths and the exploits folder.
            paths = self.ctx.list_files(tree_id, "" if folder in ("", ".") else folder)
        except LookupError:
            paths = None
        # A folder whose files are all hidden gets the answer of a missing folder, so it does not show them (SKY-353).
        if not paths:
            return f"'{folder}' is not a folder of the current version."
        return f"Files ({len(paths)}):\n" + _cut("\n".join(paths))

    def command(self, tree_id: str, command: str) -> str:
        """Run one of the model's commands over the parent version.

        Args:
            tree_id: The parent version's tree.
            command: The model's shell command.

        Returns:
            The note that tells the model the command's result.
        """
        try:
            result, _tree = self.ctx.run_command(tree_id, command, ())
        except BoxFailureError as error:
            return f"The command did not run: {error}"
        except TreeRejectedError:
            return _REJECTED_NOTE
        return (
            f"Exit code {result.exit_code}.\nstdout:\n{result.stdout[-_OUTPUT_CHARS:]}\n"
            f"stderr:\n{result.stderr[-_OUTPUT_CHARS:]}"
        )

    def write(self, tree_id: str, files: dict[str, str]) -> tuple[str, str | None]:
        """Write the model's files over the parent version, when each path keeps the run's path rules.

        Args:
            tree_id: The parent version's tree.
            files: The new file text by path.

        Returns:
            The note for the model and ``None``, or an empty note and the new version's tree id.
        """
        problems = [
            problem for path in files if (problem := self.path_problem(path) or self.folder_problem(tree_id, path))
        ]
        if problems:
            return "The files break the editing rules: " + " ".join(problems), None
        try:
            new_tree = self.ctx.put_files({path: text.encode() for path, text in files.items()}, tree_id)
        except ValueError as error:
            return f"The files could not be written: {error}", None
        return "", new_tree

    def folder_problem(self, tree_id: str, path: str) -> str | None:
        """Refuse a file at the path of a folder of the parent version.

        Args:
            tree_id: The parent version's tree.
            path: The model's repository-relative path.

        Returns:
            The reason, or ``None`` when ``path`` is no folder.
        """
        try:
            paths = self.ctx.list_files(tree_id, path)
        except LookupError:
            paths = None
        if paths is None:
            return None
        # A file at a folder's path replaces the whole folder, with the hidden or protected files in it.
        # A folder whose files are all hidden lists as missing, so its refusal is worded as for any path
        # outside the editable paths and does not show that it holds hidden files (SKY-353).
        if not paths:
            return f"'{path}' is outside the editable paths."
        return f"'{path}' may not be changed."

    def path_problem(self, path: str) -> str | None:
        """Explain how writing ``path`` breaks the run's path rules.

        Args:
            path: The model's repository-relative path.

        Returns:
            The reason, or ``None`` when the path is editable.
        """
        parts = path.split("/")
        if path.startswith("/") or any(part in ("", ".", "..") for part in parts) or ".git" in parts:
            return f"'{path}' is not a path inside the repository."
        # This check comes before the hidden check, so a hidden path outside the editable paths gets the same
        # answer as any other path there, and the answer does not show that it is hidden (SKY-353).
        if not inside(path, self.seed.editable_paths):
            return f"'{path}' is outside the editable paths."
        folded, exploits = path.casefold(), EXPLOITS_DIR.casefold()
        # A file at a parent of the exploits folder, such as ``.optimize``, would block the hidden suite too.
        if self.hidden(path) or inside(folded, [exploits]) or exploits.startswith(folded + "/"):
            # Generic text: the proposer must not learn where the hidden files live (SKY-353).
            return f"'{path}' may not be changed."
        if any(root.startswith(path + "/") for root in (*self.seed.constrained_paths, *self.seed.readonly_paths)):
            # A file at a parent of a protected or read-only path replaces that path.
            return f"'{path}' may not be changed."
        if inside(path, self.seed.constrained_paths):
            return f"'{path}' is protected by the repository rules ([paths] protected)."
        if inside(path, self.seed.readonly_paths):
            return f"'{path}' is a submodule or Git LFS file, which are read-only."
        return None

    def hidden(self, path: str) -> bool:
        """Report whether a hidden path is ``path`` or holds it.

        Args:
            path: The model's repository-relative path.

        Returns:
            Whether the path is hidden.
        """
        # ``put_files`` does not check hidden paths, so the proposer refuses them before it writes.
        return hashed_kind(self.seed.hidden_hashes, path) is not None


def _cut(text: str) -> str:
    """Cut a note's text to ``_OUTPUT_CHARS`` and say so, so the model does not write back a part as the whole file.

    Args:
        text: The text.

    Returns:
        The text, with a notice when it was cut.
    """
    if len(text) <= _OUTPUT_CHARS:
        return text
    return f"{text[:_OUTPUT_CHARS]}\n[cut at {_OUTPUT_CHARS} of {len(text)} characters; read the rest with sh]"


class _LaneReflection:
    """Expose only this invocation's measured spend to upstream's cost stopper."""

    def __init__(self, reflection_lm: Any, check_budget: Any = None) -> None:
        """Record the shared model's starting cost.

        Args:
            reflection_lm: Metered model callable.
            check_budget: Run-wide budget guard, checked before and after each model call.
        """
        self.reflection_lm = reflection_lm
        self.check_budget = check_budget
        self.starting_cost = float(getattr(reflection_lm, "total_cost", 0.0))

    @property
    def total_cost(self) -> float:
        """Return measured cost since this lane started."""
        return float(getattr(self.reflection_lm, "total_cost", 0.0)) - self.starting_cost

    def __call__(self, prompt: Any) -> str:
        """Forward an upstream prompt to the selected metered model.

        Args:
            prompt: Text or multimodal messages.

        Returns:
            Model completion text.
        """
        if self.check_budget is not None:
            self.check_budget()
        with usage_scope(candidate=current_proposal_iteration()):
            result = self.reflection_lm(prompt)
        if self.check_budget is not None:
            self.check_budget()
        return result


def _candidate_tree(gepa_result: GEPAResult[Any, Any], *, str_mode: bool) -> list[dict[str, Any]]:
    """Flatten GEPA's lineage into JSON-safe nodes for the run result.

    Args:
        gepa_result: The finished (or state-recovered) result.
        str_mode: Whether candidates unwrap to plain text.

    Returns:
        One node per candidate, in discovery order: the candidate, the
        indices of the parents it was mutated from (``None`` for the seed),
        its mean validation score and the metric-call count at discovery.
    """
    nodes: list[dict[str, Any]] = []
    for index, candidate in enumerate(gepa_result.candidates):
        unwrapped: Any = candidate
        if str_mode and isinstance(unwrapped, dict):
            unwrapped = next(iter(unwrapped.values()), "")
        score = gepa_result.val_aggregate_scores[index]
        nodes.append(
            {
                "candidate": unwrapped,
                "parents": [None if parent is None else int(parent) for parent in gepa_result.parents[index]],
                "val_score": _val_score(score),
                "discovery_evals": int(gepa_result.discovery_eval_counts[index]),
            }
        )
    return nodes


def _val_score(score: float | None) -> float | None:
    """Turn GEPA's validation mean into a reported score.

    Args:
        score: GEPA's mean, ``-inf`` when a case of it had a metric error.

    Returns:
        The mean, or ``None`` when it is missing or not finite.
    """
    if score is None or not math.isfinite(score):
        return None
    return float(score)


def _load_result_from_state(run_dir: str, *, seed: int, str_mode: bool) -> GEPAResult[Any, Any] | None:
    """Rebuild GEPA's result from the state it checkpointed before the budget ran out.

    Args:
        run_dir: Workspace holding ``gepa_state.bin``.
        seed: RNG seed the run used.
        str_mode: Whether candidates were plain text.

    Returns:
        The reconstructed result, or ``None`` when no usable state exists.
    """
    try:
        state = GEPAState.load(run_dir)
        return GEPAResult.from_state(
            state,
            run_dir=run_dir,
            seed=seed,
            str_candidate_key=STR_CANDIDATE_KEY if str_mode else None,
        )
    except Exception as exc:
        logger.info("no GEPA state to recover after budget exhaustion: %s", exc)
        return None
