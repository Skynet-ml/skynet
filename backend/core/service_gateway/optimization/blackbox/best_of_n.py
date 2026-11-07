"""Run the pinned upstream Best-of-N engine through Skynet's model transport."""

from __future__ import annotations

import math
from typing import Any

from gepa.oa.config import OptimizeAnythingConfig
from gepa.oa.engines.best_of_n import BestOfNEngine as UpstreamBestOfN

from ....exceptions import ServiceError
from ..budget_stop import BudgetReached
from .feedback import emit_candidate
from .native_runtime import run_native_engine
from .protocol import EngineContext, EvalServer, Result, Task
from .upstream import local_result, reflection_endpoint, upstream_server

# Top train samples re-scored on the validation split; each costs one scorer
# run per validation case, so the cap keeps sampling the bulk of the budget.
_MAX_FINALISTS = 3


class _SampleStream:
    """Announce each fully scored sample as a root of the run's candidate tree.

    Upstream scores the samples one after another, one scorer run per case,
    so consecutive runs group into samples by count alone; a sample cut short
    by the budget never completes its group and stays out of the tree, and a
    sample with a metric error on any case stays out too.
    """

    def __init__(self, task: Task, server: EvalServer, ctx: EngineContext) -> None:
        """Bind the stream to the cases upstream scores samples on.

        Args:
            task: Optimization inputs; each sample is scored on every case.
            server: Run accounting, for the scorer runs spent per sample.
            ctx: The job's progress sink.
        """
        cases = task.cases
        self._case_ids = {id(example): str(index) for index, example in enumerate(cases)}
        self._per_sample = max(1, len(cases))
        self._server = server
        self._sink = ctx.progress_callback
        self._samples = 0
        self._seen = 0
        self._scores: list[tuple[str, float]] = []
        self._metric_error = False

    def __call__(self, candidate: Any, example: Any, score: float | None) -> None:
        """Record one scored case, announcing the sample once its last case is in.

        Args:
            candidate: The sample being scored.
            example: The case scored, ``None`` in single-task mode.
            score: The case's score, ``None`` after a metric error.
        """
        self._seen += 1
        if score is None:
            self._metric_error = True
        else:
            self._scores.append((self._case_ids.get(id(example), "?"), float(score)))
        if self._seen < self._per_sample:
            return
        scores, self._scores, self._seen = self._scores, [], 0
        metric_error, self._metric_error = self._metric_error, False
        sample, self._samples = self._samples, self._samples + 1
        # A sample with a metric error on any case has no score (SKY-350), the
        # same as the run's final selection treats it, and the candidate event
        # carries a number, so the sample stays out of the tree.
        if metric_error:
            return
        emit_candidate(
            self._sink,
            candidate_id=str(sample),
            parent_id=None,
            generation=0,
            score=sum(value for _, value in scores) / len(scores),
            per_example=[] if example is None else scores,
            candidate=candidate,
            discovered_at_evals=self._server.used,
            iteration=sample,
        )


class BestOfNEngine:
    """Adapt the upstream independent-sampling baseline without rewriting its loop."""

    name = "best_of_n"

    def run(self, task: Task, server: EvalServer, ctx: EngineContext) -> Result:
        """Run upstream sampling with the configured metered optimization model.

        Args:
            task: Starting point and optimization examples.
            server: Skynet scorer and budget.
            ctx: Configured model and artifact directory.

        Returns:
            The fully evaluated winner, or an unscored seed if no candidate completes.

        Raises:
            ServiceError: If given named components or a seedless run has no completed candidate.
        """
        if not task.str_mode:
            raise ServiceError("Best-of-N supports text starting points only.")
        if ctx.native_options is not None and ctx.native_options.repo is not None:
            # A repository sample is a patch a coding agent writes in the native
            # runtime's checkout; upstream's text sampler cannot edit a checkout.
            return run_native_engine("best_of_n_repo", task, server, ctx)
        finalists = _validation_finalists(task, server)
        sampling = server.lane(server.remaining - finalists * len(task.valset))
        upstream = upstream_server(task, sampling, ctx, on_eval=_SampleStream(task, sampling, ctx), prefetch_cases=True)
        result = None
        budget_stop = None
        try:
            with reflection_endpoint(ctx) as connection:
                engine = UpstreamBestOfN(
                    OptimizeAnythingConfig(
                        engine=self.name,
                        max_evals=sampling.remaining,
                        stop_at_score=ctx.stop_at_score,
                        engine_config={"model": "openai/app-reflection", "lm_kwargs": connection},
                    )
                )
                result = engine.run(upstream.task, upstream)
        except BudgetReached as exc:
            budget_stop = exc
        if budget_stop is None:
            budget_stop = next(iter(getattr(upstream, "platform_budget_stops", [])), None)
        if result is None:
            if budget_stop is not None:
                raise budget_stop
            raise ServiceError("Best-of-N did not produce a result.")
        engine.process_result(result, upstream.output_dir)
        adapted = local_result(result, server)
        # Upstream logs a sample with no scored case at -inf; it is not evaluated.
        evaluated = any(math.isfinite(row.get("score", -math.inf)) for row in result.metadata.get("bon_cost_log", []))
        if budget_stop is not None:
            if evaluated:
                budget_stop.result = adapted
                budget_stop.evidence.update(
                    selection_scope="training",
                    final_evaluation_completed=False,
                    final_evaluation_reason="budget_reached",
                )
            raise budget_stop
        if not evaluated:
            if task.seed_candidate is None:
                raise ServiceError("Best-of-N stopped before producing a fully evaluated candidate.")
            adapted.best_score = None
            return adapted
        if finalists:
            return _select_on_validation(task, server, sampling, adapted, finalists)
        return adapted


def _validation_finalists(task: Task, server: EvalServer) -> int:
    """Return how many top samples to re-score on the validation cases.

    Args:
        task: Train cases and the validation split.
        server: Run accounting.

    Returns:
        The number of finalists whose validation scoring the budget can
        reserve while leaving room for at least two full train samples, or 0
        when the task has no validation split.
    """
    if not task.valset:
        return 0
    per_sample = max(1, len(task.cases))
    spare = server.remaining - 2 * per_sample
    return max(0, min(_MAX_FINALISTS, spare // len(task.valset)))


def _select_on_validation(
    task: Task, server: EvalServer, sampling: EvalServer, adapted: Result, finalists: int
) -> Result:
    """Pick the winner among the best train samples by their validation mean.

    Args:
        task: Train cases and the validation split.
        server: Run accounting that still holds the reserved validation runs.
        sampling: The lane upstream sampled in; its records are train-only.
        adapted: Upstream's train-selected incumbent.
        finalists: How many top train samples to validate.

    Returns:
        The incumbent with the highest validation mean, scored as that mean.
        A sample with a metric error on any case has no mean, so it is never
        a finalist and never the winner.
    """
    per_sample = max(1, len(task.cases))
    complete = [record for record in sampling.history if record.count >= per_sample and record.mean_score is not None]
    ranked = sorted(complete, key=lambda record: record.mean_score, reverse=True)[:finalists]
    best: tuple[float, Any] | None = None
    for record in ranked:
        scores = [server.evaluate(record.candidate, case)[0] for case in task.valset]
        if any(score is None for score in scores):
            continue
        mean = sum(scores) / len(scores)
        if best is None or mean > best[0]:
            best = (mean, record.candidate)
    if best is None:
        return adapted
    adapted.best_candidate, adapted.best_score = best[1], best[0]
    adapted.total_evals = server.used
    adapted.metadata["selection_scope"] = "validation"
    return adapted
