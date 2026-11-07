"""Engine-facing contract for black-box optimization.

Mirrors the shape of GEPA's ``optimize_anything`` engine protocol — a task,
an eval server that is the single scoring choke point, and a result — so a
future upstream ``gepa.oa`` release can be adopted without changing the
Skynet-side contract (design brief TODO-4).
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from gepa.oa.budget import BudgetExhausted

from ....billing.usage_tags import usage_scope
from ..cost_ceiling import CostCeilingExceededError
from ..timing import STAGE_TRAINING
from .runner import FEEDBACK_REQUIRED

if TYPE_CHECKING:
    from .native_runtime import NativeOptions

logger = logging.getLogger(__name__)

Candidate = str | dict[str, str]
SideInfo = dict[str, Any]
ScorerFn = Callable[[Candidate, Any], tuple[float | None, SideInfo]]
EvalListener = Callable[["EvalServer", float | None], None]

# Side info ``status`` of a case the metric or scorer crashed on. That case has
# no score (``None``), never 0: a crash is not a measurement (SKY-350).
METRIC_ERROR = "metric_error"
STATUS_KEY = "status"
# SKY-367: the pinned upstream engines need a number for every case, and they
# use negative infinity as their own "no score" value. A single case scored
# alone gets it; a batch fills each null case with its version's mean over the
# scored cases (see :func:`fill_unscored`), so the null cases do not change the
# mean upstream ranks by. Only a version with no scored case is UNSCORED.
UNSCORED = float("-inf")


def engine_score(score: float | None) -> float:
    """Give a pinned upstream engine its own "no score" value for a metric error case.

    Args:
        score: A score from :meth:`EvalServer.evaluate`, ``None`` after a metric error.

    Returns:
        The score, or :data:`UNSCORED` when there is none.
    """
    return UNSCORED if score is None else score


def fill_unscored(candidates: list[Candidate], scores: list[float | None]) -> list[float]:
    """Give each null case its version's mean over the scored cases in the same batch.

    Args:
        candidates: The version scored on each case, aligned with ``scores``.
        scores: Each case's score, ``None`` after a metric error.

    Returns:
        A number for every case: the score, the mean of the same version's
        scored cases for a null case, or :data:`UNSCORED` when the version
        has no scored case in the batch.
    """
    scored: dict[str, list[float]] = {}
    for candidate, score in zip(candidates, scores, strict=True):
        if score is not None:
            scored.setdefault(candidate_key(candidate), []).append(score)
    filled: list[float] = []
    for candidate, score in zip(candidates, scores, strict=True):
        if score is None:
            values = scored.get(candidate_key(candidate))
            score = sum(values) / len(values) if values else UNSCORED
        filled.append(score)
    return filled


def score_batch(
    score_one: Callable[[Candidate, Any], tuple[float | None, SideInfo]],
    pairs: list[tuple[Candidate, Any]],
    max_workers: int | None,
) -> list[tuple[float, SideInfo]]:
    """Score a batch of pairs at the engine's concurrency and fill the null cases.

    Upstream sends a whole batch in one call and does not fan it out, so the
    batch keeps the per-pair concurrency the engines had before (SKY-367).

    Args:
        score_one: Scores one pair; ``None`` after a metric error.
        pairs: The ``(candidate, case)`` pairs of one upstream batch.
        max_workers: The engine's scorer concurrency.

    Returns:
        One ``(score, side_info)`` per pair, in order, with null cases filled
        by :func:`fill_unscored`; side info still marks each metric error.
    """
    workers = max(1, min(max_workers or 1, len(pairs)))
    if workers == 1:
        results = [score_one(candidate, example) for candidate, example in pairs]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(lambda pair: score_one(*pair), pairs))
    filled = fill_unscored([candidate for candidate, _ in pairs], [score for score, _ in results])
    return [(score, info) for score, (_, info) in zip(filled, results, strict=True)]


def is_metric_error(side_info: SideInfo) -> bool:
    """Return True when the side info marks a case the metric or scorer crashed on.

    Args:
        side_info: What the scorer returned for one case.

    Returns:
        Whether its ``status`` is :data:`METRIC_ERROR`.
    """
    return side_info.get(STATUS_KEY) == METRIC_ERROR


class BudgetExhaustedError(RuntimeError):
    """Raised by :class:`EvalServer` once its scorer-run cap is reached."""


class ScorerAbortError(RuntimeError):
    """Raised by a scorer to stop the whole run.

    :class:`EvalServer` records every other scorer failure as a metric error
    to keep one bad version from killing a run; this one passes through
    untouched because the scorer has established that no version can be scored.
    """


def candidate_key(candidate: Candidate) -> str:
    """Return a stable identity string for a candidate.

    Args:
        candidate: A text candidate or a named-parts mapping.

    Returns:
        The text itself, or the sorted-key JSON form of a mapping.
    """
    if isinstance(candidate, dict):
        return json.dumps(candidate, sort_keys=True)
    return candidate


@dataclass
class Task:
    """What an engine optimizes: the starting point, the goal and its cases.

    ``cases`` is the train split: the cases an engine scores versions on
    while searching (each case is also a Pareto axis); with none it scores
    a version on its own. ``valset`` is the validation split an engine
    selects its incumbent on when it can; when empty, engines select on
    ``cases``. The test split never reaches a task: the service's final run
    re-scores the starting point and the winner on it afterwards.

    ``context`` is private run context (artifact kind, workspace code,
    models, DSPy optimizer settings) for in-process adapters such as the
    DSPy engine. It holds the metric source, so it must never be exported to
    a proposer, a native engine payload, a prompt or an agent workspace.
    """

    seed_candidate: Candidate | None
    objective: str | None = None
    background: str | None = None
    cases: list[Any] = field(default_factory=list)
    valset: list[Any] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def has_dataset(self) -> bool:
        """Return True when the task carries cases (multi-task mode)."""
        return bool(self.cases)

    @property
    def str_mode(self) -> bool:
        """Return True when candidates are plain text (including seedless)."""
        return not isinstance(self.seed_candidate, dict)


@dataclass
class Result:
    """What an engine hands back: its best version and what it cost."""

    best_candidate: Candidate
    best_score: float | None
    total_evals: int
    metadata: dict[str, Any] = field(default_factory=dict)
    # A DSPy engine's compiled program (``ProgramArtifact.model_dump()``).
    artifact: dict[str, Any] | None = None


@dataclass
class CandidateRecord:
    """One distinct version an eval server scored: its running mean, latest side info and crash flags."""

    candidate: Candidate
    total: float
    count: int
    # Root-server call number that first scored this version, so the run's
    # versions can be laid out in the order they appeared.
    first_eval: int
    side_info: SideInfo
    # Kept apart from ``side_info``, which holds only the latest case, so a
    # crash on an earlier case still marks the version.
    crashed: bool = False
    # Set once the metric or scorer crashed on any case: the version then has
    # no score at all, so it can never be the best or be published.
    metric_error: str | None = None

    @property
    def mean_score(self) -> float | None:
        """Return the mean score across every case this version was scored on, or ``None`` after a metric error."""
        if self.metric_error is not None:
            return None
        return self.total / self.count


@dataclass
class EngineContext:
    """Run-scoped resources every engine receives alongside the task."""

    reflection_lm: Callable[[str | list[dict[str, Any]]], str]
    run_dir: str
    native_options: NativeOptions | None = None
    proposer_token_budget_usd: float | None = None
    check_budget: Callable[[], None] | None = None
    remaining_cost_usd: Callable[[], float] | None = None
    seed: int = 0
    stop_at_score: float | None = None
    # Cap on proposer rounds for engines that iterate (Meta-Harness,
    # AutoSaddler, ShinkaEvolve generations); ``None``
    # runs until the scorer budget is spent.
    max_iterations: int | None = None
    # How many cases an engine may score at once. Only pays off when the
    # scorer itself runs elsewhere (one sandbox per case); 1 for text targets.
    concurrency: int = 1
    # "harness · model" the proposer is told it writes for, on agent targets.
    target_label: str | None = None
    # Job-level progress sink. Engines that can narrate their run (GEPA via
    # the trajectory watcher) stream candidate events through it live.
    progress_callback: Callable[[str, dict[str, Any]], None] | None = None


class Engine(Protocol):
    """One optimization engine (GEPA, Best-of-N, ...)."""

    name: str

    def run(self, task: Task, server: EvalServer, ctx: EngineContext) -> Result:
        """Optimize ``task`` scoring only through ``server``.

        Args:
            task: The starting point, goal and cases.
            server: The budgeted scorer every evaluation must go through.
            ctx: Reflection LM, workspace and stop settings for this run.

        Returns:
            The engine's best version and its score.
        """
        ...


class EvalServer:
    """Budgeted scoring choke point shared by every engine in a run.

    Enforces ``max_evals``, keeps a per-candidate mean so a best version is
    always recoverable even when an engine dies mid-run, and — through
    :meth:`lane` — hands out sub-budgets that count against the parent.

    Safe to call from several threads at once: a call is reserved against
    every budget in the chain before the scorer runs, so concurrent
    evaluations can never overspend, and the bookkeeping is locked.
    """

    def __init__(
        self,
        scorer: ScorerFn,
        *,
        max_evals: int,
        parent: EvalServer | None = None,
        on_eval: EvalListener | None = None,
    ) -> None:
        """Create a server over ``scorer`` with a cap of ``max_evals`` calls.

        Args:
            scorer: Callable scoring ``(candidate, case)`` → ``(score, side_info)``.
            max_evals: Maximum scorer calls this server will make.
            parent: Server whose budget this one is a slice of, if any.
            on_eval: Listener called with ``(server, score)`` after each root eval.
        """
        self._scorer = scorer
        self.max_evals = max_evals
        self._parent = parent
        self._on_eval = on_eval
        self.used = 0
        self._records: dict[str, CandidateRecord] = {}
        # Locks are only ever taken child → parent, so lanes cannot deadlock.
        self._lock = threading.Lock()

    @property
    def _root(self) -> EvalServer:
        """Return the top of the lane chain: the server that owns the scorer and listener."""
        server = self
        while server._parent is not None:
            server = server._parent
        return server

    @property
    def remaining(self) -> int:
        """Return how many scorer calls this server may still make."""
        own = max(0, self.max_evals - self.used)
        if self._parent is None:
            return own
        return min(own, self._parent.remaining)

    def lane(self, max_evals: int) -> EvalServer:
        """Open a sub-budget of at most ``max_evals`` calls against this server.

        Args:
            max_evals: Cap for the lane; clamped to what remains here.

        Returns:
            A child server whose evaluations count against both budgets.
        """
        return EvalServer(self._scorer, max_evals=min(max_evals, self.remaining), parent=self)

    def evaluate(self, candidate: Candidate, example: Any = None) -> tuple[float | None, SideInfo]:
        """Score one candidate on one case (or on its own in single-task mode).

        Args:
            candidate: The version to score.
            example: The case to score it on, or ``None`` when there are no cases.

        Returns:
            The score and the scorer's side information. A metric error case
            returns ``None`` with ``status: metric_error``.

        Raises:
            BudgetExhaustedError: When no scorer calls remain.
        """
        root = self._root
        self._reserve()
        with usage_scope(stage=STAGE_TRAINING):
            score, side_info = root._score(candidate, example)
        # Per-call heartbeat at DEBUG so it surfaces only in the Logs tab's
        # verbose view — the black-box counterpart of the DSPy per-example eval
        # heartbeat, so every run type gets the same normal=aggregates /
        # verbose=per-call split. Counted against the run-wide budget.
        if score is None:
            logger.debug("scorer eval %d/%d %s: %s", root.used, root.max_evals, METRIC_ERROR, side_info.get("error"))
        else:
            logger.debug("scorer eval %d/%d score=%.3f", root.used, root.max_evals, score)
        self._account(candidate, score, side_info)
        return score, side_info

    def charge(self, candidate: Candidate | None, score: float | None, side_info: SideInfo | None = None) -> None:
        """Count one evaluation scored outside the scorer against every budget in the chain.

        In-process adapters (the DSPy engine) run the metric themselves; this
        keeps their metric calls inside the run's scorer-run budget and
        progress, so budgets hold for every optimizer.

        Args:
            candidate: The version that was scored, or ``None`` when the
                caller cannot name it; an unnamed evaluation is counted but
                not recorded in the version history.
            score: The score it got, or ``None`` when the metric crashed.
            side_info: What the metric said about it.

        Raises:
            BudgetExhaustedError: When no scorer calls remain.
        """
        self._reserve()
        self._account(candidate, score, side_info or {})

    def _account(self, candidate: Candidate | None, score: float | None, side_info: SideInfo) -> None:
        """Record a reserved evaluation in every server of the chain and notify the listener.

        Args:
            candidate: The scored version, or ``None`` to count it without recording.
            score: Its score, or ``None`` after a metric error.
            side_info: What the scorer said about it.
        """
        root = self._root
        server: EvalServer | None = self
        while server is not None and candidate is not None:
            with server._lock:
                server._record(candidate, score, side_info)
            server = server._parent
        if root._on_eval is not None:
            with root._lock:
                root._on_eval(root, score)

    def _reserve(self) -> None:
        """Claim one scorer call here and in every ancestor, or claim nothing.

        Raises:
            BudgetExhaustedError: When this server or an ancestor has no
                calls left; no budget in the chain is touched in that case.
        """
        with self._lock:
            if self.used >= self.max_evals:
                raise BudgetExhaustedError(f"scorer-run budget of {self.max_evals} exhausted")
            if self._parent is not None:
                self._parent._reserve()
            self.used += 1

    def _score(self, candidate: Candidate, example: Any) -> tuple[float | None, SideInfo]:
        """Call the scorer, turning a crash into a metric error with no score.

        A scorer that raises on one version marks that version as bad — it
        must not kill the run (GEPA's ``raise_on_exception=False`` semantics,
        applied here so every engine gets them).

        Args:
            candidate: The version to score.
            example: The case to score it on.

        Returns:
            The score and side information, or ``(None, {"error": ..., "status": "metric_error"})``.

        Raises:
            ScorerAbortError: When the scorer asks to stop the run, or returns no feedback.
        """
        try:
            score, side_info = self._scorer(candidate, example)
        except (ScorerAbortError, BudgetExhaustedError, BudgetExhausted, CostCeilingExceededError):
            raise
        except Exception as exc:
            # A scorer without feedback lacks it on every version, so flooring
            # would spend the whole budget teaching the proposer nothing.
            if FEEDBACK_REQUIRED in str(exc):
                raise ScorerAbortError(str(exc)) from exc
            error = f"{type(exc).__name__}: {exc}"
            logger.warning("scorer raised on a candidate (%s): %s", METRIC_ERROR, error)
            return None, {"error": error, STATUS_KEY: METRIC_ERROR}
        # A nested server (an Auto lane) scores through the upstream server,
        # which hands back UNSCORED or a batch-filled number; the status marks
        # it as no score.
        if score is None or is_metric_error(side_info):
            return None, {**side_info, STATUS_KEY: METRIC_ERROR}
        return score, side_info

    def _record(self, candidate: Candidate, score: float | None, side_info: SideInfo) -> None:
        """Fold ``score`` into the candidate's running mean and keep its latest side info.

        Args:
            candidate: The scored version.
            score: Its score on the case just evaluated, or ``None`` after a metric error.
            side_info: What the scorer said about it on that case.
        """
        key = candidate_key(candidate)
        record = self._records.get(key)
        failed = score is None or is_metric_error(side_info)
        error = str(side_info.get("error") or METRIC_ERROR) if failed else None
        crashed = failed or "error" in side_info
        # A failed case adds nothing to the total: the version's mean is None.
        value = 0.0 if failed or score is None else float(score)
        if record is None:
            self._records[key] = CandidateRecord(candidate, value, 1, self._root.used, side_info, crashed, error)
            return
        record.total += value
        record.count += 1
        record.side_info = side_info
        record.crashed = record.crashed or crashed
        record.metric_error = record.metric_error or error

    @property
    def history(self) -> list[CandidateRecord]:
        """Return every distinct version this server scored, in first-seen order."""
        return list(self._records.values())

    def mean_score(self, candidate: Candidate) -> float | None:
        """Return the mean score recorded for ``candidate``, if any.

        Args:
            candidate: The version to look up.

        Returns:
            Its mean score across the cases scored so far, or ``None`` when it
            was not scored or hit a metric error.
        """
        record = self._records.get(candidate_key(candidate))
        return None if record is None else record.mean_score

    def metric_error(self, candidate: Candidate) -> str | None:
        """Return the error text of the metric error ``candidate`` hit, if any.

        Args:
            candidate: The version to look up.

        Returns:
            The first metric error's text, or ``None`` when the version was
            not scored or never hit one.
        """
        record = self._records.get(candidate_key(candidate))
        return None if record is None else record.metric_error

    @property
    def best_candidate(self) -> Candidate | None:
        """Return the candidate with the highest mean score, if any was scored without a metric error."""
        scored = [record for record in self._records.values() if record.mean_score is not None]
        if not scored:
            return None
        return max(scored, key=lambda record: record.mean_score).candidate

    @property
    def best_score(self) -> float | None:
        """Return the highest mean score recorded, if any."""
        best = self.best_candidate
        return None if best is None else self.mean_score(best)
