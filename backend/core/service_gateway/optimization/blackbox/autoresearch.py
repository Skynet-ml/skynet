"""Run Skynet's AutoResearch loop in the worker engine loop (SKY-725).

The round loop of :class:`native_engines.AutoResearchEngine` runs here unchanged:
the directives, the leaderboard, the Pareto front and the failure dossier come
from the same code. Only each round's agent session changes: the ``claude`` CLI
in a long-lived box becomes a tool loop in the worker, as in the Meta-Harness
port (SKY-722). Each model reply is one ``model`` step and each shell command is
one ``command`` step, so a killed run resumes from its last step.

The agent scores a file with an ``evaluate`` tool in place of ``eval.sh``: the
tool reads the file from the workspace tree and scores it in the worker, as the
upstream HTTP handler does. A round with no full sweep of a new version ends with
a sweep of the last probed version, so the run view gets a version score for the
round. The engine skips that sweep when the version already had a full sweep or
the budget cannot pay for one. The lab is a tree that the engine builds again
from its local files before each round, so the frontier can lose a file. After a
round, only the agent's ``work/`` and ``notebook.md`` come back from the tree.

In a repository run (SKY-825), a version is a tree. Each round's session works
on the leader's tree, or the seed's in the first round, with the lab under
``.autoresearch/``. The tree that the session leaves, without the lab, is the
round's version, and the engine gives it a full evaluation. The agent has only
the bash tool, since the engine scores the version. When the contract file names
``[proposer] entry`` (SKY-816), that command makes each round's version in place
of the tool loop, with the round's prompt and lab files in ``ASCENT_TASK``.

The session watchdogs count model turns and tool calls, not seconds, so a
resume stops a session at the same place. The engine runs only on the worker
engine loop (SKY-728).
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import posixpath
import shutil
from collections.abc import Callable, Iterator
from decimal import Decimal
from types import SimpleNamespace
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
from .native_runtime import assert_metric_absent, native_task_payload, sanitize_side_info
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
from .upstream import AUTORESEARCH_SOURCE

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

REPO_BASH_TOOL = {
    "name": "bash",
    "description": (
        "Run one shell command in the repository, with no network. Each command starts in the repository root, in a "
        "fresh box: files persist between commands, but the shell state and /tmp do not. The repository files you "
        "leave are this round's version; only `.autoresearch/notebook.md` of the lab carries over to the next "
        "round. Returns the exit code, stdout, stderr and whether the command timed out."
    ),
    "input_schema": BASH_TOOL["input_schema"],
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
# The worker holds the carried lab in memory at each round's tree write. This cap bounds only the agent's files
# that come back from the tree, ``work/`` and ``notebook.md``: room for a few candidates of the largest size that
# ``evaluate`` scores, and the notebook. A file past the per-file cap could never be scored, so it stays behind.
_MAX_CARRY_BYTES = 4 * native_engines.MAX_CANDIDATE_BYTES
# The archive gets one file for each new fully scored candidate, so a long run would hold GBs in worker memory at
# the tree write and could pass the sandbox's ``MAX_TREE_BYTES`` (1 GiB). Eight candidates of the largest size, or
# hundreds of typical ones, keep the lab tree with ``work/`` well under that limit. The worker's own archive, the
# frontier and the ids stay complete; only the copy in the lab tree is cut.
_MAX_ARCHIVE_BYTES = 8 * native_engines.MAX_CANDIDATE_BYTES
# A tree path that comes back to the worker must fit the worker's file system whatever the run_dir prefix is, so a
# fixed rule on the UTF-8 relative path decides each skip, not the local file system. macOS has PATH_MAX 1024 and
# NAME_MAX 255 bytes (Linux: 4096 and 255): 512 bytes for the relative path leave 511 for the lab prefix, and 255
# bytes for one name is NAME_MAX itself.
_MAX_CARRY_PATH_BYTES = 512
_MAX_CARRY_NAME_BYTES = 255
# An ``evaluate`` error echoes the agent's path or ids, which may be huge: a short head keeps the error readable.
_ECHO_CHARS = 200
# The marker file that stands for an empty lab folder in the tree.
_KEEP = ".keep"
# The lab folder of a repository run, relative to the repository root.
_REPO_LAB = ".autoresearch"
# The lab files that a repository run's session gets: the archive and the frontier hold tree ids, not code.
_REPO_LAB_FILES = ("TASK.md", "BRIEF.md", "STATE.md", "notebook.md")

logger = logging.getLogger(__name__)


def _echo(text: str) -> str:
    """Cut untrusted text that an error result repeats to its first ``_ECHO_CHARS`` characters.

    Args:
        text: The text to repeat.

    Returns:
        The text unchanged when it fits, else its head and a note with its full length.
    """
    if len(text) <= _ECHO_CHARS:
        return text
    return f"{text[:_ECHO_CHARS]}[... {len(text)} characters in all]"


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
        # A non-finite score would lead the native ranking and its stop check, so it records as a crash (SKY-725).
        value = float(score) if score is not None and math.isfinite(score) else float("-inf")
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
                # The run log gets the same events as from the native runner and the Meta-Harness port.
                logger.info(
                    "Candidate %s scored %.4f on case %s",
                    trial,
                    score,
                    eid,
                    extra=run_log.event_extra(
                        source="engine",
                        event="case.scored",
                        fields={"score": score, "total": len(ids)},
                        candidate=trial,
                        case=eid,
                    ),
                )
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
        # A version with a metric error on every case has no finite mean, but it still shows, with no score.
        if isinstance(candidate, str) and (math.isfinite(val_score) or self.crashed(candidate)):
            sweep = self._sweeps.pop(candidate_key(candidate), [])
            # A version with a crashed case shows no score (SKY-350, SKY-367).
            shown = None if self.crashed(candidate) else val_score
            logger.info(
                "Candidate %s scored %s after %s evaluations",
                entry["candidate_id"],
                "no score (a case crashed)" if shown is None else f"{shown:.4f}",
                self._server.used,
                extra=run_log.event_extra(
                    source="engine",
                    event="candidate.scored",
                    fields={"score": shown, "total_evals": self._server.used},
                    candidate=entry["candidate_id"],
                ),
            )
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

    def __init__(
        self,
        config: OptimizeAnythingConfig,
        ctx: EngineContext,
        *,
        max_tool_calls: int,
        proposer: loop_tools.FileProposer | None = None,
    ) -> None:
        """Build the native engine and bind the context.

        Args:
            config: The native engine's config.
            ctx: The engine loop's context, with its model, command and tree calls.
            max_tool_calls: The tool calls one round's session may start.
            proposer: The contract file's command, which makes each repository round's version in place of the
                tool loop; ``None`` runs the tool loop.
        """
        super().__init__(config)
        self._ctx = ctx
        self._proposer = proposer
        self._max_tool_calls = max_tool_calls
        self._no_eval_turns = (
            None
            if self.max_no_eval_seconds is None
            else max(1, math.ceil(self.max_no_eval_seconds / _SECONDS_PER_TURN))
        )
        # The native ``repo`` mode needs a git checkout and ``eval.sh``, so a repository run keeps it off and works
        # on trees. Its versions are tree ids: the seed's, then the tree that each session leaves.
        self._repo = ctx.repo_seed
        self._swept: set[str] = set()
        # The last version that a text round's ``evaluate`` tool probed, for the sweep at the round's end.
        self._probed: str | None = None

    def _layout(self, task: UpstreamTask, server: Any) -> None:
        """Create the native lab, without ``eval.sh``: the ``evaluate`` tool or the engine scores each version.

        Args:
            task: The upstream task.
            server: The upstream server adapter.
        """
        super()._layout(task, server)
        if self._repo is not None:
            rules = native_engines.repo_rules(
                {"editable_paths": self._repo.editable_paths or (".",), "readonly_paths": self._repo.readonly_paths}
            )
            with (self.work_dir / "TASK.md").open("a", encoding="utf-8") as brief:
                brief.write(rules)

    def _brief(self) -> str:
        """Return the native brief, with the ``evaluate`` tool in place of ``eval.sh``, or the repository brief.

        Returns:
            The ``BRIEF.md`` text.
        """
        if self._repo is not None:
            return self._tree_brief()
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
            (
                "`archive/<id>.txt` holds every candidate that ever received a full evaluation, with scores in "
                "`archive/index.tsv`.",
                "`archive/<id>.txt` holds the best candidates that ever received a full evaluation before this "
                "round, up to a size cap; `archive/index.tsv` has the scores of all of them. `archive/` and "
                "`frontier/` refresh only between rounds: the `evaluate` result is the current record for this "
                "round.",
            ),
            ("- If `eval.sh` prints", "- If the `evaluate` tool answers"),
            (
                "- `notebook.md` is yours and is the only memory that survives between rounds.",
                "- `notebook.md` is yours. It and `work/` are the only files that survive between rounds, up to "
                f"size caps: keep each file at most {native_engines.MAX_CANDIDATE_BYTES // 2**20} MiB and all of "
                f"them together at most {_MAX_CARRY_BYTES // 2**20} MiB, each path inside the lab at most "
                f"{_MAX_CARRY_PATH_BYTES} bytes and each file or folder name at most {_MAX_CARRY_NAME_BYTES} bytes "
                "as UTF-8. A file past a cap is dropped.",
            ),
        ]
        if self.example_ids:
            edits.append(
                (
                    "- `./eval.sh <file> <example_id> [<example_id> ...]` is a **probe**",
                    "- The `evaluate` tool with a `file` and `example_ids` is a **probe**",
                )
            )
            edits.append(
                (
                    "they never enter the leaderboard.",
                    "they never enter the leaderboard. When a round gives no new candidate a full evaluation, the "
                    "engine gives one to the text of the round's last probe, after the session ends. It skips this "
                    "when that text already had a full evaluation or the budget cannot pay for one.",
                )
            )
        return native_engines.adapt(super()._brief(), edits)

    def _tree_brief(self) -> str:
        """Write the standing research method of a repository run, where the engine scores each round's version.

        Returns:
            The ``BRIEF.md`` text.
        """
        full = (
            "one unit per visible example (see `TASK.md`)"
            if self.example_ids
            else "one unit, since the evaluator scores the repository as a whole"
        )
        return (
            "# Research brief\n\n"
            "You are running one round of a multi-round research effort to improve a code repository against a "
            "fixed evaluator. `TASK.md` says what the change is for, how it is judged and which paths you may "
            "edit. Higher scores are better.\n\n"
            "## Files\n\n"
            "- The working directory is the repository at the version this round starts from: the leader of the "
            "leaderboard, or the starting version in the first round. Edit its files in place.\n"
            f"- `{_REPO_LAB}/` is the lab. `TASK.md`, `BRIEF.md` and `STATE.md` there are maintained by the engine. "
            "Read them; never edit them.\n"
            "- `STATE.md` is rewritten before every round: the leaderboard, the failure dossier, this round's "
            "**directive** and the evaluations left.\n"
            f"- `{_REPO_LAB}/notebook.md` is yours and is the only memory that survives between rounds. Later "
            "rounds, possibly a different session of you, depend on it.\n\n"
            "## Evaluating\n\n"
            f"- You cannot run the evaluator. When the session ends, the engine removes `{_REPO_LAB}/` and gives "
            f"the repository files you leave a full evaluation, which costs {full}. They are this round's "
            "version.\n"
            "- A round that leaves the repository as it started gets no evaluation, and the run stops.\n"
            "- The evaluator runs the repository's setup and then the scorer in another sandbox. This sandbox is "
            "offline, so local builds and tests may lack dependencies.\n"
            "- Change only the editable paths in `TASK.md`.\n\n"
            "## Method for a round\n\n"
            "1. **Orient.** Read `STATE.md`, then `notebook.md`. Do not rerun a hypothesis the notebook already "
            "refuted unless you have a new reason, and say what the reason is.\n"
            "2. **Diagnose.** Study the failure dossier and the evaluator feedback, and read the code it points "
            "to. Name concrete failure modes, each tied to the examples that show it.\n"
            "3. **Hypothesize.** Under a `## Round N` heading in `notebook.md`, write two to four falsifiable "
            "hypotheses. For each: the change, why it should help, and which examples it should move.\n"
            "4. **Change.** Make the change of the most promising hypothesis in the repository, and check it as "
            "far as this sandbox allows.\n"
            "5. **Record.** Write in `notebook.md` what you changed and what you expect it to move. The next "
            "round's `STATE.md` has the result.\n\n"
            "## Standards\n\n"
            "- Follow the round's directive in `STATE.md`; it is chosen from the evidence of earlier rounds.\n"
            "- Keep the repository working. The final result is measured afresh, so a change that only helps "
            "because it special-cases an example, or weakens the checks that judge it, is not an improvement.\n"
            "- Between two versions with the same score, the smaller diff is better. A change that deletes code "
            "and keeps the score is a win.\n"
            "- There is no human to ask. End the session when the change is ready; the engine starts the next "
            "round with fresh evidence.\n"
        )

    def _partner(self) -> tuple[str, list[str]] | None:
        """Find the frontier version that most complements the leader, in a text run only.

        A repository session sees only the leader's tree, so it cannot graft another version into it: a repository
        run never gets the ``combine`` directive.

        Returns:
            ``(candidate, examples it beats the leader on)``, or ``None``.
        """
        return None if self._repo is not None else super()._partner()

    def _directive_text(self, directive: str, table: dict[str, dict[str, float]], front: list[str]) -> str:
        """Spell out what this round should do, for a session that changes the leader's tree once.

        Args:
            directive: The chosen directive.
            table: Per-example scores of fully evaluated versions.
            front: Current frontier, leader first.

        Returns:
            Markdown for the directive section.
        """
        if self._repo is None:
            return super()._directive_text(directive, table, front)
        leader = f"`{self._ids[front[0]]}`" if front else "the starting version"
        if directive == "survey":
            return (
                "**Survey.** The engine gave the starting version a full evaluation as the baseline. Map the failure "
                "modes across examples and make the most promising change."
            )
        if directive == "exploit":
            return (
                f"**Exploit.** The last round raised the leader to {leader}, which this round starts from. Push "
                "further along the direction that worked."
            )
        if directive == "pivot":
            return (
                f"**Pivot.** The last rounds did not beat {leader}: small edits have stalled. Take a different "
                "approach in the code. Keep what the notebook confirmed and drop what it refuted."
            )
        return (
            f"**Explore.** The last round did not beat {leader}, which this round starts from again. Aim a new "
            "hypothesis at the hard examples in the failure dossier, which no version handles well yet."
        )

    def _write_state(self, server: Any, directive: str) -> None:
        """Refresh ``STATE.md`` and ``frontier/``, with the budget line of a repository run where the engine scores.

        A repository session cannot run the evaluator, so a round quota of evaluation units would mislead it.

        Args:
            server: Evaluation server with the budget state.
            directive: This round's directive.
        """
        super()._write_state(server, directive)
        remaining = server.budget.remaining
        if self._repo is None or remaining is None:
            return
        state = self.work_dir / "STATE.md"
        line = f"- The engine gives this round's version one full evaluation, of the {remaining} evaluation units left."
        lines = [
            line if text.startswith("- This round may spend ") else text
            for text in state.read_text(encoding="utf-8").split("\n")
        ]
        state.write_text("\n".join(lines), encoding="utf-8")

    def _round_prompt(self, directive: str) -> str:
        """Write the prompt that starts one round's session, with the lab paths of a repository run.

        Args:
            directive: This round's directive.

        Returns:
            The prompt text.
        """
        if self._repo is None:
            return super()._round_prompt(directive)
        return (
            f"This is round {self.round} ({directive}). The working directory is the repository. Read "
            f"`{_REPO_LAB}/BRIEF.md`, then `{_REPO_LAB}/STATE.md`, then `{_REPO_LAB}/notebook.md`, and carry out "
            f"the round as the brief describes. Record everything in `{_REPO_LAB}/notebook.md`."
        )

    def _session(self, server: Any, directive: str, *, max_budget_usd: float | None) -> native_engines.ProposerOutcome:
        """Run one round's session as a tool loop: one step per model reply and per command.

        The cost cap counts only committed costs, and the write-up grace, the no-evaluation watchdog, the
        tool-call cap and the prompt cap count model turns, tool calls and committed text, so a resume makes the
        same choices. As in the native harness, the session stops before the tool call past the cap; the run then
        goes on to the native stop rules. Billed failed model calls that reach the cost cap end the session as out
        of budget. A model call that fails on every attempt after the session made a tool call ends only the
        session, as the prompt cap does; one before any tool call cannot come from tool output, so it is an error.

        A repository session has only the bash tool and makes no evaluation, so the write-up grace and the
        no-evaluation watchdog do not apply. The engine scores the version that the session leaves.

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
        swept = len(self._swept)
        self._probed = None
        if self._repo is None:
            quota = self._round_quota(server)
            tools = [BASH_TOOL, EVALUATE_TOOL]
            misuse = "Call the bash tool with a command string, or the evaluate tool."
            files = self._lab_files()
            # The local mode carries an agent's executable in ``work/`` to the next round, as the native lab does.
            executables = [name for name in files if os.access(self.work_dir / name, os.X_OK)]
            # A tree write cannot delete a file, so each round starts from a new tree: the frontier can shrink.
            tree = ctx.put_files(files, None, executables=executables)
        else:
            parent = self._repo_parent(server)
            if parent is None:
                return native_engines.ProposerOutcome(
                    session_id=f"round{self.round}",
                    cost_usd=0.0,
                    is_error=False,
                    text="",
                    budget_exhausted=True,
                    killed=False,
                )
            quota = None
            tools = [REPO_BASH_TOOL]
            misuse = "Call the bash tool with a command string."
            files = {
                name: (self.work_dir / name).read_bytes()
                for name in _REPO_LAB_FILES
                if (self.work_dir / name).is_file()
            }
            if self._proposer is not None:
                return self._command_session(server, directive, parent, files)
            tree = start = loop_tools.put_lab(ctx, parent, _REPO_LAB, files)
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
            if self._repo is None and self._no_eval_turns is not None and idle_turns >= self._no_eval_turns:
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
            reply, failed_cents = loop_tools.call_model(ctx, messages, tools, spent, max_budget_usd)
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
                if self._repo is None and call.get("name") == EVALUATE_TOOL["name"]:
                    output = loop_tools.clip(self._evaluate(server, tree, call))
                else:
                    output, tree = loop_tools.run_bash(ctx, tree, call, misuse=misuse)
                messages.append({"role": "tool", "tool_call_id": call.get("id"), "content": output})
            if killed:
                break
        if self._repo is None:
            self._pull(tree)
            # A probe scores some cases, so its mean is not a version score. A round with no full sweep of a new
            # version would leave no point in the run view's score over time, so the engine sweeps the last probed
            # version, as it sweeps each repository round's version. A budget too small for the sweep skips it.
            probed = self._probed
            if (
                len(self._swept) == swept
                and probed is not None
                and probed not in self._swept
                and server.budget.remaining >= len(self.example_ids)
            ):
                exhausted = not self._sweep(server, probed) or exhausted
        else:
            self._pull_notebook(tree)
            version = loop_tools.drop_lab(ctx, start, tree, parent, _REPO_LAB)
            # A version that was scored before gets no second sweep, so the round spends nothing and the run stops.
            if version is not None and version not in self._swept:
                exhausted = not self._sweep(server, version) or exhausted
        return native_engines.ProposerOutcome(
            session_id=f"round{self.round}",
            cost_usd=float(spent) / 100,
            is_error=killed or failed,
            text=text,
            budget_exhausted=exhausted,
            killed=killed,
        )

    def _command_session(
        self, server: Any, directive: str, parent: str, files: dict[str, bytes]
    ) -> native_engines.ProposerOutcome:
        """Run one repository round with the contract file's command, and give its version a full evaluation.

        Args:
            server: The upstream server adapter, with the engine's recording wrappers.
            directive: This round's directive.
            parent: The tree of the version that the round starts from.
            files: The round's lab files.

        Returns:
            The session's outcome. A failed command leaves no version, so the round spends nothing and the native
            stop rule ends the run without an error.
        """
        assert self._proposer is not None
        task = loop_tools.task_text(self._round_prompt(directive), _REPO_LAB, files, _REPO_LAB_FILES)
        start = loop_tools.put_lab(
            self._ctx, self._proposer.base(parent), _REPO_LAB, {**files, loop_tools.TASK_FILE: task}
        )
        tree, failure = self._proposer.run(start)
        name = f"round{self.round}"
        if failure:
            # The outcome is no error, so the native loop logs nothing; this line says why no version came.
            native_engines.log_event(
                "proposer.done",
                f"Proposer session {name} failed: {failure}",
                level=logging.WARNING,
                source="proposer",
                session=name,
            )
        self._pull_notebook(tree)
        version = self._proposer.version(start, tree, parent)
        exhausted = version is not None and version not in self._swept and not self._sweep(server, version)
        return native_engines.ProposerOutcome(
            session_id=name,
            cost_usd=0.0,
            is_error=False,
            text=failure or "",
            budget_exhausted=exhausted,
            killed=False,
        )

    def _lab_files(self) -> dict[str, bytes]:
        """Read the local lab for the round's tree, with the archived candidates cut to the archive cap.

        The archived candidates come best score first, and on a tie the newest first. One that would pass the cap
        stays out, and the scan goes on. Its bytes are never read: the cap checks each file's size first.

        Returns:
            The file bytes, keyed by path inside the lab, with a ``.keep`` marker in each empty folder.
        """
        ranking = self._ranking()
        # The id order breaks a tie.
        order = {
            f"archive/{cid}{self.suffix}": (ranking[candidate], place)
            for place, (candidate, cid) in enumerate(self._ids.items())
        }
        paths = {
            path.relative_to(self.work_dir).as_posix(): path
            for path in sorted(self.work_dir.rglob("*"))
            if path.is_file()
        }
        total = 0
        for name in sorted(order.keys() & paths.keys(), key=order.__getitem__, reverse=True):
            size = paths[name].stat().st_size
            if total + size > _MAX_ARCHIVE_BYTES:
                del paths[name]
                continue
            total += size
        files = {name: path.read_bytes() for name, path in paths.items()}
        # A tree holds only files, so an empty folder of the lab, such as ``frontier/`` in round 1 or a ``work/``
        # with every file dropped, gets a marker file; without it, the folder is missing from the box.
        files.update(
            {
                f"{path.relative_to(self.work_dir).as_posix()}/{_KEEP}": b""
                for path in sorted(self.work_dir.rglob("*"))
                if path.is_dir() and not any(path.iterdir())
            }
        )
        return files

    def _evaluate(self, server: Any, tree: str, call: dict[str, Any]) -> str:
        """Score a lab file as the upstream HTTP handler that ``eval.sh`` calls does.

        A full sweep of every visible example logs a checkpoint; a probe does not.

        Args:
            server: The upstream server adapter, with the engine's recording wrappers.
            tree: The lab tree to read the file from.
            call: The tool call.

        Returns:
            The tool result as JSON text. An error echoes at most ``_ECHO_CHARS`` of the untrusted path or ids;
            the session cuts the whole result to ``OUTPUT_BYTES``, as it cuts a command output.
        """
        assert self._ctx.read_file is not None
        refused = loop_tools.refused_arguments(call, EVALUATE_TOOL["name"])
        if refused is not None:
            return refused
        arguments = loop_tools.arguments(call) or {}
        file = arguments.get("file")
        # Only a missing key, null or an empty list asks for a full sweep: any other value that is not a list of
        # strings, such as "" or 0, gets the error and spends nothing.
        example_ids = arguments.get("example_ids")
        path = posixpath.normpath(file) if isinstance(file, str) and file else ""
        if not path or path == ".." or path.startswith(("../", "/")):
            return json.dumps({"error": "Give `file` as a path inside the lab."}, ensure_ascii=False)
        if example_ids is not None and not (
            isinstance(example_ids, list) and all(isinstance(eid, str) for eid in example_ids)
        ):
            return json.dumps({"error": "Give `example_ids` as a list of example id strings."}, ensure_ascii=False)
        # The candidate is copied into the observations, the archive and the frontier, so its size is bounded. A lab
        # file may hold up to ``MAX_TREE_BYTES``, so the cap refuses a larger file before it loads.
        try:
            data = self._ctx.read_file(tree, path, native_engines.MAX_CANDIDATE_BYTES)
        except FileTooLargeError:
            error = f"{_echo(path)} is larger than {native_engines.MAX_CANDIDATE_BYTES} bytes, the candidate cap."
            return json.dumps({"error": error}, ensure_ascii=False)
        if data is None:
            return json.dumps({"error": f"No file {_echo(path)} in the lab."}, ensure_ascii=False)
        try:
            candidate = data.decode("utf-8")
        except UnicodeDecodeError:
            return json.dumps({"error": f"{_echo(path)} is not UTF-8 text."}, ensure_ascii=False)
        marker = json.dumps({"error": native_engines.BUDGET_EXHAUSTED_MARKER}, ensure_ascii=False)
        budget = server.budget
        try:
            if not server.task.has_dataset:
                score, info = server.evaluate(candidate)
                # The engine's wrapper logs a checkpoint only for a finite score, so a metric-error candidate still
                # shows here, with no score, as a repository version does in ``_sweep``.
                if not math.isfinite(score):
                    server.log_progress(score, candidate=candidate)
                body: dict[str, Any] = {"score": score, "info": info}
            else:
                visible = set(self.example_ids)
                unknown = [eid for eid in example_ids or () if eid not in visible]
                if unknown:
                    error = f"Unknown or sealed example_ids: {_echo(str(unknown[:5]))}"
                    return json.dumps({"error": error}, ensure_ascii=False)
                targets = list(example_ids or self.example_ids)
                average, info = server.evaluate_examples(candidate, example_ids=targets)
                if set(targets) == visible:
                    # A sweep with a crashed case is not a full observation, so ``_ids`` alone would miss it.
                    self._swept.add(candidate)
                    server.log_progress(average, candidate=candidate)
                else:
                    self._probed = candidate
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
        return json.dumps(body, ensure_ascii=False)

    def _pull(self, tree: str) -> None:
        """Copy the agent's ``work/`` and ``notebook.md``, with each file's executable mode, into the local lab.

        The files come in sorted order, the notebook first. A file whose relative path passes the path or name
        byte bound, or past the per-file cap, or past the total cap stays behind. The tree decides each skip, so a
        resume skips the same files: the bounds apply to the relative path before any local file access, and a
        file that the local file system still refuses counts toward the total cap and never fails the run.

        Args:
            tree: The tree after the session.
        """
        assert self._ctx.read_file is not None
        assert self._ctx.list_files is not None
        work = self.work_dir / "work"
        shutil.rmtree(work)
        work.mkdir()
        executables = set(self._ctx.list_files(tree, executable=True) or ())
        total = 0
        for path in ["notebook.md", *(self._ctx.list_files(tree, "work") or [])]:
            # The marker that ``_lab_files`` puts in an empty ``work/`` is not the agent's file.
            if path == f"work/{_KEEP}":
                continue
            # ``work/`` starts empty and the notebook path is short, so a path past the bounds has no local file.
            if len(path.encode("utf-8", "surrogateescape")) > _MAX_CARRY_PATH_BYTES or any(
                len(name.encode("utf-8", "surrogateescape")) > _MAX_CARRY_NAME_BYTES for name in path.split("/")
            ):
                continue
            # The cap is the room left under the total cap, so a file that cannot fit never loads.
            try:
                content = self._ctx.read_file(
                    tree, path, min(native_engines.MAX_CANDIDATE_BYTES, _MAX_CARRY_BYTES - total)
                )
            except FileTooLargeError:
                content = None
            local = self.work_dir / path
            # The output is untrusted, so a local refusal, such as a path the bounds let through on a deep run_dir,
            # only leaves the file behind.
            with contextlib.suppress(OSError):
                if content is None:
                    # A deleted or skipped notebook must not come back from an earlier round.
                    local.unlink(missing_ok=True)
                    continue
                total += len(content)
                local.parent.mkdir(parents=True, exist_ok=True)
                local.write_bytes(content)
                local.chmod(0o755 if path in executables else 0o644)

    def _record(self, server: Any) -> Callable[[], None]:
        """Wrap the scoring entry points as the native engine does, then sweep the seed of a repository run.

        The seed's score must be in the evidence before round one reads the leader's score: else a round one whose
        version fails would count as a raise of the leader, and round two would get the ``exploit`` directive.

        Args:
            server: The upstream server adapter.

        Returns:
            A function that removes the wrappers.
        """
        restore = super()._record(server)
        if self._repo is not None:
            # A seed sweep that the budget refused leaves less budget than a sweep, so ``_repo_parent`` stops round one.
            self._sweep(server, self._repo.tree_id)
        return restore

    def _repo_parent(self, server: Any) -> str | None:
        """Pick the tree that a repository round starts from: the best frontier version, or the seed.

        Args:
            server: The upstream server adapter, with the engine's recording wrappers.

        Returns:
            The parent's tree id, or ``None`` when the budget cannot score the round's version.
        """
        assert self._repo is not None
        # A session that the budget cannot score would pay for a version that never counts.
        if server.budget.remaining < max(1, len(self.example_ids)):
            return None
        front = native_engines.pareto_front(self._table(), self._ranking())
        return front[0] if front else self._repo.tree_id

    def _sweep(self, server: Any, tree: str) -> bool:
        """Give a version a full evaluation, as a full ``evaluate`` call does in a text run.

        A version is swept once, also when its sweep fails, so a round that leaves it again spends nothing.

        Args:
            server: The upstream server adapter, with the engine's recording wrappers.
            tree: The version: a repository version's tree id, or a text round's candidate.

        Returns:
            ``False`` when the budget could not score the version.
        """
        self._swept.add(tree)
        try:
            if not server.task.has_dataset:
                # The engine's wrapper logs a checkpoint only for a finite score, so a metric-error version still
                # shows here, with no score.
                score, _ = server.evaluate(tree)
                if not math.isfinite(score):
                    server.log_progress(score, candidate=tree)
            else:
                average, _ = server.evaluate_examples(tree, example_ids=list(self.example_ids))
                server.log_progress(average, candidate=tree)
        except BudgetExhausted:
            return False
        return True

    def _pull_notebook(self, tree: str) -> None:
        """Copy the agent's notebook of a repository session into the local lab.

        Args:
            tree: The tree after the session.
        """
        assert self._ctx.read_file is not None
        try:
            content = self._ctx.read_file(tree, f"{_REPO_LAB}/notebook.md", native_engines.MAX_CANDIDATE_BYTES)
        except FileTooLargeError:
            content = None
        notebook = self.work_dir / "notebook.md"
        if content is None:
            # A deleted or oversize notebook must not come back from an earlier round.
            notebook.unlink(missing_ok=True)
        else:
            notebook.write_bytes(content)


class AutoResearchEngine:
    """Run the round-based AutoResearch loop in the worker engine loop."""

    name = "autoresearch"

    def run(self, task: Task, server: EvalServer, ctx: EngineContext) -> Result:
        """Run the unchanged loop, with its round sessions on the engine context, or in the native runtime.

        Args:
            task: Optimization inputs.
            server: Skynet scorer and budget.
            ctx: The engine loop's context; ``native_options`` gives the optional proposer knobs.

        Returns:
            The best server-verified candidate and the loop's evidence.

        Raises:
            ServiceError: When the context is not the worker engine loop's; when the run has a proposer setting
                that the tool loop does not run, would expose the metric source, or the candidate is not one text;
                or when a repository has a file at the lab path.
            BudgetExhaustedError: When no scorer call is left.
            RuntimeError: When a repository run cannot score its seed.
        """
        if None in (ctx.call_model, ctx.run_command, ctx.put_files, ctx.read_file, ctx.list_files):
            raise ServiceError("AutoResearch runs only on the worker engine loop.")
        options = ctx.native_options
        # The contract file's command makes a repository version; with no repository, it would edit nothing.
        file_proposer = bool(
            options is not None and (options.repo or {}).get("file_proposer") and ctx.repo_seed is not None
        )
        if options is not None:
            # A setting that the run would drop fails the run.
            loop_tools.refuse_unsupported_proposer(options, "AutoResearch", file_proposer=file_proposer)
        # A repository run's candidate is a tree id, so its seed is the visible seed tree.
        seed_tree = None if ctx.repo_seed is None else loop_tools.repo_seed_tree(ctx, _REPO_LAB, "AutoResearch")
        if seed_tree is None and not task.str_mode:
            raise ServiceError("Native agent engines require a single text candidate.")
        payload = native_task_payload(self.name, task)
        # The native runtime checks the same document before it writes it into the box.
        assert_metric_absent(json.dumps(payload, default=side_info_json_default), task)
        if seed_tree is not None:
            payload["seed_candidate"] = seed_tree
        if server.remaining <= 0:
            raise BudgetExhaustedError("The native optimizer evaluation budget is exhausted.")
        # A repository session has no ``evaluate`` tool, so a seed that the budget cannot sweep leaves no score.
        # Not BudgetExhaustedError: an Auto lane has its own small allowance, and that error fails the whole Auto run.
        sweep = max(1, len(task.cases))
        if seed_tree is not None and server.remaining < sweep:
            raise RuntimeError(
                f"A repository run needs {sweep} evaluation units to score the seed, "
                f"but only {server.remaining} are left."
            )
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
        command = loop_tools.FileProposer(ctx, options, _REPO_LAB) if file_proposer and options is not None else None
        upstream = _WorkerAutoResearch(config, ctx, max_tool_calls=proposer.max_tool_calls, proposer=command).run(
            UpstreamTask(**payload), adapter
        )
        # The incumbent always has a finite mean: the adapter keeps no non-finite best, and the ranking skips one.
        # A best version with a crashed case still has no score (SKY-350, SKY-367).
        crashed = adapter.crashed(upstream.best_candidate)
        return Result(
            best_candidate=upstream.best_candidate,
            best_score=None if crashed else upstream.best_score,
            total_evals=server.used,
            metadata={
                **upstream.metadata,
                "upstream_source": AUTORESEARCH_SOURCE,
                "upstream_revision": native_engines.AUTORESEARCH_VERSION,
            },
        )
