"""Judge sessions: an MCP client scores its own repository versions (SKY-696).

A session is one run row whose payload has a ``judge`` block. Its budget is
attached to that row like a normal run's, but no worker and no guest runs it.
Each ``repo_judge_score`` call stages the repository at the pinned base commit
in the parent, refuses a version that breaks the editing rules, and scores it
on the search cases. The repository scorer runs the version in its own run
box and the metric in a separate metric box. A call is one setup evidence row
(scope ``judge``) under the session's budget, so a retry with the same
arguments replays the stored result and pays once.

Only the budget or ``stop_run`` ends a session. The end scores the best
version once on the held-out cases (on every case when the split holds none
out, and once with no cases) and stores the result with its run record.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import tempfile
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..billing.budgets import (
    BudgetConflictError,
    BudgetError,
    BudgetInFlightError,
    BudgetInsufficientError,
    BudgetService,
    BudgetSnapshot,
)
from ..billing.protected_execution import protected_image
from ..billing.runtime import UsagePendingError
from ..billing.signals import BudgetReached
from ..config import settings
from ..connectors import repo_hosts
from ..connectors.github_repo import RepoFetchError, commit_diff
from ..constants import (
    COMPOSITION_SINGLE,
    OPTIMIZATION_TYPE_BLACKBOX,
    PAYLOAD_OVERVIEW_COMPOSITION,
    PAYLOAD_OVERVIEW_DATASET_ROWS,
    PAYLOAD_OVERVIEW_DESCRIPTION,
    PAYLOAD_OVERVIEW_MAX_COST_CENTS,
    PAYLOAD_OVERVIEW_MODULE_NAME,
    PAYLOAD_OVERVIEW_NAME,
    PAYLOAD_OVERVIEW_OPTIMIZATION_TYPE,
    PAYLOAD_OVERVIEW_OPTIMIZER_NAME,
    PAYLOAD_OVERVIEW_SEED,
    PAYLOAD_OVERVIEW_TOKEN_SOURCE,
    PAYLOAD_OVERVIEW_TOKEN_SOURCES_BY_MODEL,
    PAYLOAD_OVERVIEW_USERNAME,
)
from ..exceptions import InfrastructureInterruptionError, ServiceError
from ..models import BlackboxRunRequest, JobCancelResponse
from ..models.blackbox import BLACKBOX_MODULE_NAME, BlackboxNamedScore, BlackboxRunResponse
from ..sandbox.bridge import default_provider
from ..service_gateway.optimization.blackbox.agent_runs import PHASE_FINAL
from ..service_gateway.optimization.blackbox.preflight import judge_lifetime_seconds
from ..service_gateway.optimization.blackbox.repo_constraints import CONSTRAINTS_FILE
from ..service_gateway.optimization.blackbox.repo_tree import REPO_SNAPSHOT_KEY, patch_violations
from ..service_gateway.optimization.blackbox.scorer import normalize_score
from ..service_gateway.optimization.blackbox.service import (
    CASE_SPLIT_KEY,
    FinalRun,
    _case_results,
    _final_run,
    _outcome_verdict,
    case_split_positions,
    legacy_case_splits,
)
from ..storage.models import JobModel
from ..storage.preflights import PreflightStore, WizardPreflightModel
from ..storage.usage import json_byte_size
from ..worker.repo_staging import StagedRepository, add_run_record
from .agent_engine import fill_proposer_host_cli
from .auth import AuthenticatedUser
from .errors import DomainError
from .model_billing import enforce_local_mode_sources, normalize_model_token_sources
from .preflight_execution import judge_scorer
from .routers._helpers import enforce_job_quota, enforce_storage_quota, stable_seed
from .routers.execution_budgets import budget_http_error
from .routers.submissions import (
    _BLACKBOX_WORKFLOW,
    _admit_byok_models,
    _enforce_balance,
    _enforce_submission_admission,
    _execution_overview,
    _materialize_repo_cases,
    _persistable_payload,
)

logger = logging.getLogger(__name__)

JUDGE_KEY_PREFIX = "mcp-judge-"
JUDGE_SCOPE = "judge"
FINAL_SCOPE = "judge-final"
JUDGE_OPTIMIZER = "judge"
# A patch is stored with its call's evidence until the end scores the best one.
MAX_PATCH_CHARS = 2_000_000
_OPEN_STATUSES = ("pending", "running")
_LEASE_MARGIN_SECONDS = 60
_MESSAGE_CHARS = 2000
ENDED_DETAIL = "This judge session ended. Call get_run with session_id for its final score."
_END_MESSAGE = "The judge session ended and scored its best version once."
_NO_FINAL_MESSAGE = "The budget ran out before the final score, so the session has no held-out score."
# A retry can succeed after these failures, so a call does not store them as its result.
_TRANSIENT_ERRORS = (RepoFetchError, InfrastructureInterruptionError)


class JudgeError(Exception):
    """Refuse one judge call with a stable code the tool returns."""

    def __init__(self, code: str, detail: str) -> None:
        """Keep the code and the reason.

        Args:
            code: Stable error code, such as ``judge.candidate_refused``.
            detail: Plain reason for the client.
        """
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.session_id: str | None = None


class JudgePendingError(Exception):
    """Tell the client to call again: another request holds the session or its usage settles."""


class _RefusedError(Exception):
    """Stop a call before any paid box opens: the version breaks the editing rules."""


def judge_key(draft_id: str, fingerprint: str) -> str:
    """Return the session key of a draft and its arguments.

    Args:
        draft_id: The draft.
        fingerprint: Fingerprint of the draft and its spending limit.

    Returns:
        The key stored on the session row.
    """
    return f"{JUDGE_KEY_PREFIX}{draft_id}-{fingerprint[:16]}"


def open_session_ids(engine: Any, username: str, draft_id: str) -> list[str]:
    """List the draft's judge sessions that did not end.

    Args:
        engine: Database that holds the run rows.
        username: Account that owns the draft.
        draft_id: The draft.

    Returns:
        The open session ids.
    """
    with Session(engine) as session:
        return list(
            session.scalars(
                select(JobModel.optimization_id).where(
                    JobModel.username == username,
                    JobModel.idempotency_key.startswith(f"{JUDGE_KEY_PREFIX}{draft_id}-", autoescape=True),
                    JobModel.status.in_(_OPEN_STATUSES),
                )
            )
        )


def is_opening(job: dict[str, Any]) -> bool:
    """Tell whether a session row is from an open that did not finish.

    Args:
        job: The session's job record.

    Returns:
        True while the row has no attached budget yet.
    """
    return bool(((job.get("payload") or {}).get("judge") or {}).get("opening"))


def _utc_now() -> str:
    """Return the current UTC time as ISO text, as the job columns take it."""
    return datetime.now(UTC).isoformat()


def _sha256(text: str) -> str:
    """Return the hex SHA-256 of a patch's UTF-8 bytes.

    Args:
        text: The patch.

    Returns:
        The hex digest.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def open_session(
    request: BlackboxRunRequest,
    key: str,
    budget_key: str | None,
    user: AuthenticatedUser,
    *,
    job_store: Any,
    token: str | None,
    floor_cents: int,
    draft_id: str,
    resume_id: str | None = None,
) -> str:
    """Admit, fund and attach a judge session, as a submission does, with no worker.

    The gates are those of :func:`core.api.routers.submissions.submit_repo_payload`:
    admission, the balance, the model sources, the quotas and the protected
    budget. The session has no setup check, because each call stages and
    checks the repository itself.

    Args:
        request: The draft's run request with its spending limit.
        key: Session key, stored on the row.
        budget_key: Budget creation key of the session key.
        user: Account that pays.
        job_store: Store that persists the row, the budget and the credentials.
        token: Repository host token, or ``None`` for a public repository.
        floor_cents: Smallest limit of one scored call.
        draft_id: The draft.
        resume_id: Row an interrupted open left behind, to finish opening.

    Returns:
        The session id.

    Raises:
        DomainError: 402 without balance, 409 for a quota or budget conflict.
        JudgePendingError: When a concurrent call opens the same session.
    """
    payload = request
    username = user.username
    payload.username = username
    _materialize_repo_cases(payload, job_store=job_store, username=username)
    _enforce_submission_admission(job_store, username)
    requested_source = payload.token_source
    configs, by_model = normalize_model_token_sources(payload)
    fill_proposer_host_cli(job_store, username, payload)
    enforce_local_mode_sources(payload, requested_source, configs)
    if not settings.local_mode:
        _enforce_balance(job_store, username, payload.token_source)
    _admit_byok_models(
        job_store,
        username,
        configs,
        payload.reflection_model_settings,
        bool(payload.shinka and payload.shinka.novelty),
    )
    repo = payload.target.repo
    if repo is None:
        raise DomainError("submission.validation_failed", status=400)
    if not repo.commit:
        # Every call diffs and stages against this one tree, so a branch is pinned once.
        repo.commit = repo.draft_base or repo_hosts.resolve_commit(
            repo_hosts.provider_of(repo.model_dump()), token, repo.repository, repo.branch
        )
    budgets = BudgetService(engine=job_store.engine)
    try:
        budget = budgets.find_by_creation_key(username, budget_key) if budget_key else None
        if budget is None:
            budget = budgets.create(
                username,
                int(payload.max_cost_cents or 0),
                idempotency_key=budget_key or key,
                uncapped=settings.local_mode,
            )
    except BudgetError as error:
        raise budget_http_error(error) from error
    session_id = resume_id or str(uuid4())
    payload.execution_budget_id = budget.id
    payload.execution_budget_revision = budget.revision
    payload.max_cost_cents = budget.total_cents
    payload.seed = stable_seed(session_id)
    payload.sandbox_provider = payload.sandbox_provider or default_provider(settings)
    dump = _persistable_payload(payload, job_store)
    if resume_id is None:
        enforce_job_quota(job_store, username)
        enforce_storage_quota(job_store, username, incoming_bytes=json_byte_size(dump))
        try:
            job_store.create_job(session_id, username=username, idempotency_key=key)
        except IntegrityError as error:
            raise JudgePendingError("Another call opens this judge session.") from error
        # Running, not pending: no worker may claim the row, and the sweeper skips judge rows.
        job_store.update_job(session_id, status="running", started_at=_utc_now(), payload={"judge": {"opening": True}})
    try:
        attached = budgets.attach_to_job(budget.id, username, session_id, expected_revision=budget.revision)
    except BudgetError as error:
        current = budgets.get(budget.id, username)
        if current.job_id != session_id:
            raise budget_http_error(error) from error
        attached = current
    job_store.set_payload_overview(
        session_id,
        {
            PAYLOAD_OVERVIEW_OPTIMIZATION_TYPE: OPTIMIZATION_TYPE_BLACKBOX,
            **_execution_overview(payload),
            PAYLOAD_OVERVIEW_COMPOSITION: COMPOSITION_SINGLE,
            PAYLOAD_OVERVIEW_NAME: payload.name,
            PAYLOAD_OVERVIEW_DESCRIPTION: payload.description,
            PAYLOAD_OVERVIEW_USERNAME: username,
            PAYLOAD_OVERVIEW_MODULE_NAME: BLACKBOX_MODULE_NAME,
            PAYLOAD_OVERVIEW_OPTIMIZER_NAME: JUDGE_OPTIMIZER,
            PAYLOAD_OVERVIEW_DATASET_ROWS: len(payload.cases or []),
            PAYLOAD_OVERVIEW_SEED: payload.seed,
            PAYLOAD_OVERVIEW_TOKEN_SOURCE: payload.token_source,
            PAYLOAD_OVERVIEW_TOKEN_SOURCES_BY_MODEL: by_model,
            PAYLOAD_OVERVIEW_MAX_COST_CENTS: payload.max_cost_cents,
        },
    )
    dump["judge"] = {
        "version": 1,
        "draft_id": draft_id,
        "base_commit": repo.commit,
        "floor_cents": floor_cents,
    }
    dump["execution_budget_generation"] = attached.generation
    job_store.update_job(
        session_id,
        payload=dump,
        status="running",
        code_version=settings.code_version,
        execution_budget_id=attached.id,
        execution_budget_generation=attached.generation,
    )
    image = protected_image(settings)
    if image is not None:
        job_store.pin_sandbox_image(session_id, image)
    logger.info("Opened judge session %s for draft %s", session_id, draft_id)
    return session_id


def _candidate_patch(
    repo: dict[str, Any], base: str, candidate_commit: str | None, candidate_patch: str | None, token: str | None
) -> str:
    """Return the candidate as a patch against the pinned base commit.

    Args:
        repo: The session's repository target.
        base: The pinned base commit.
        candidate_commit: A commit that descends from ``base``, or ``None``.
        candidate_patch: A ``git diff`` against ``base``, or ``None``.
        token: Repository host token, or ``None`` for a public repository.

    Returns:
        The patch.

    Raises:
        JudgeError: When the commit cannot be fetched or the patch is too large.
    """
    if candidate_patch is not None:
        patch = candidate_patch
    else:
        assert candidate_commit is not None
        provider = repo_hosts.provider_of(repo)
        with tempfile.TemporaryDirectory(prefix="skynet-judge-") as workdir:
            try:
                patch = commit_diff(
                    repo["repository"],
                    base,
                    candidate_commit,
                    token,
                    Path(workdir),
                    **repo_hosts.git_kwargs(provider),
                )
            except RepoFetchError as error:
                raise JudgeError("judge.candidate_refused", str(error)[:_MESSAGE_CHARS]) from error
    if len(patch) > MAX_PATCH_CHARS:
        raise JudgeError("judge.candidate_refused", f"The candidate patch is longer than {MAX_PATCH_CHARS} characters.")
    return patch


def _search_cases(cases: list[Any], seed: int | None) -> list[tuple[int, Any]]:
    """Choose the cases a call scores: the search and validation cases, never the held-out ones.

    The split is the one a normal run makes with the same cases and seed. With
    fewer than five cases it holds nothing out, so a call scores every case.

    Args:
        cases: The staged cases, in their order.
        seed: The session's seed.

    Returns:
        Each chosen case with its position in ``cases``.
    """
    if not cases:
        return []
    splits = legacy_case_splits(cases, seed)
    position = {id(case): index for index, case in reversed(list(enumerate(cases)))}
    chosen: dict[int, tuple[int, Any]] = {}
    for case in (*splits.train, *splits.val):
        chosen.setdefault(id(case), (position[id(case)], case))
    return sorted(chosen.values(), key=lambda item: item[0])


def _final_cases(cases: list[Any], seed: int | None) -> tuple[list[Any], bool]:
    """Choose the cases the end scores: the held-out ones, or every case when the split holds none out.

    Args:
        cases: The staged cases, in their order.
        seed: The session's seed.

    Returns:
        The cases, and whether they are held-out cases.
    """
    splits = legacy_case_splits(cases, seed) if cases else None
    if splits is not None and splits.test:
        return list(splits.test), True
    return cases, False


def needed_cents(floor_cents: int, calls: list[dict[str, Any]], final_count: int) -> int:
    """Return the headroom a call needs: its own hold and the reserve of the final score.

    The reserve is one floor until a call scored. Then it is 1.5 times the
    mean settled spend per search case times the final case count, and never
    below one floor. Without cases, one score of the version counts as a case.

    Args:
        floor_cents: Smallest limit of one scored call.
        calls: The stored results of the session's scored calls.
        final_count: How many cases the final score runs (1 without cases).

    Returns:
        The cents that must remain unspent before a call starts.
    """
    reserve = floor_cents
    cases = sum(max(len(call.get("cases") or []), 1) for call in calls)
    if cases:
        spent = sum(Decimal(str(call.get("spent_cents") or 0)) for call in calls)
        reserve = max(floor_cents, math.ceil(Decimal(3) * spent * final_count / (2 * cases)))
    return floor_cents + reserve


def session_minimum_cents(floor_cents: int) -> int:
    """Return the smallest limit of a judge session: one call and the final score.

    Args:
        floor_cents: Smallest limit of one scored call.

    Returns:
        The minimum ``max_cost_cents``.
    """
    return needed_cents(floor_cents, [], 1)


def _score(staged: StagedRepository, patch: str, cases: list[Any], *, label: str) -> FinalRun:
    """Score a patch on cases with the staged repository scorer.

    The repository scorer applies the patch in its own run box and runs the
    metric in a separate metric box, as in a run.

    Args:
        staged: The staged repository with its bound scorer.
        patch: The version.
        cases: The cases; empty scores the version once.
        label: What the version is, for logs and errors.

    Returns:
        The version's scores.
    """
    scorer = staged.scorers[0]

    def score(candidate: str, case: Any) -> tuple[float, dict[str, Any]]:
        """Score one case through the scorer's guest endpoint, as a run's guest does.

        Args:
            candidate: The patch.
            case: One case, or ``None``.

        Returns:
            The score and the side information the proposer may see.

        Raises:
            ServiceError: When the scorer cannot score the version.
        """
        reply = scorer.dispatch({"candidate": candidate, "case": case})
        if reply.status != 200:
            raise ServiceError(reply.body.decode(errors="replace"))
        return normalize_score(json.loads(reply.body))

    # A repository target scores one case at a time, as in a run.
    return _final_run(score, patch, cases, label=label, phase=PHASE_FINAL, concurrency=1, repo_scorer=True)


def _named(final: FinalRun) -> dict[str, dict[str, Any]]:
    """Return the named scores as plain JSON.

    Args:
        final: A scored version.

    Returns:
        Each name's score and feedback.
    """
    return {name: entry.model_dump(mode="json") for name, entry in final.named.items()}


def _budget(job_store: Any, job: dict[str, Any]) -> BudgetSnapshot | None:
    """Read the session's budget, or ``None`` before the open attached one.

    Args:
        job_store: Store whose engine holds the budgets.
        job: The session's job record.

    Returns:
        The budget snapshot.
    """
    budget_id = job.get("execution_budget_id")
    if budget_id is None:
        return None
    return BudgetService(engine=job_store.engine).get(budget_id, job["username"])


def _store(engine: Any, payload: dict[str, Any]) -> PreflightStore:
    """Return an evidence store whose lease covers one judge attempt.

    Args:
        engine: Database that holds the evidence.
        payload: The session's payload.

    Returns:
        The store.
    """
    lease = judge_lifetime_seconds(payload, len(payload.get("cases") or [])) + _LEASE_MARGIN_SECONDS
    return PreflightStore(engine, lease_seconds=lease)


def _scored_answer(session_id: str, result: dict[str, Any], budget: BudgetSnapshot | None) -> dict[str, Any]:
    """Build the tool answer of a scored call.

    Args:
        session_id: The session.
        result: The call's stored ``judge_result``.
        budget: The session's budget after the call, if known.

    Returns:
        The answer.
    """
    return {
        "status": "scored",
        "session_id": session_id,
        **{key: result.get(key) for key in ("score", "named_scores", "feedback", "cases", "failed_cases")},
        "patch_sha256": result.get("patch_sha256"),
        **({"available_cents": float(budget.available_cents)} if budget is not None else {}),
        "next_tool": "repo_judge_score",
        "note": "Call repo_judge_score with another candidate, or stop_run with session_id to end the session. "
        "The end scores the best candidate once on the held-out cases.",
    }


def _ended_answer(session_id: str, status: str) -> dict[str, Any]:
    """Build the tool answer of a session that ended.

    Args:
        session_id: The session.
        status: The session's status.

    Returns:
        The answer.
    """
    return {
        "status": "ended",
        "session_id": session_id,
        "run_status": status,
        "next_tool": "get_run",
        "detail": ENDED_DETAIL,
    }


def _failure(code: str, message: str) -> dict[str, Any]:
    """Build the evidence of a failed call.

    Args:
        code: Stable error code.
        message: Plain reason, capped.

    Returns:
        The evidence result.
    """
    message = message[:_MESSAGE_CHARS]
    # The public evidence exposes only the checks, so a replay reads the code there.
    return {
        "checks": [{"key": "judge", "status": "failed", "code": code, "message": message}],
        "judge_error": {"code": code, "message": message},
    }


def _replay(document: dict[str, Any], session_id: str, budget: BudgetSnapshot | None) -> dict[str, Any]:
    """Answer a call whose evidence already exists or another request holds.

    Args:
        document: The call's public evidence.
        session_id: The session.
        budget: The session's budget.

    Returns:
        The scored answer.

    Raises:
        JudgeError: When the stored call failed.
        JudgePendingError: While the call runs or its usage settles.
    """
    if document.get("status") == "succeeded" and isinstance(document.get("judge_result"), dict):
        return _scored_answer(session_id, document["judge_result"], budget)
    if document.get("status") == "failed":
        check = next((item for item in document.get("checks") or [] if item.get("key") == "judge"), {})
        code = str(check.get("code") or "judge.score_failed")
        raise JudgeError(code, str(check.get("message") or "The candidate could not be scored."))
    raise JudgePendingError("The candidate is still being scored, or its usage settles.")


def judge_call(
    job_store: Any,
    session_id: str,
    *,
    username: str,
    candidate_commit: str | None,
    candidate_patch: str | None,
    token: str | None,
) -> dict[str, Any]:
    """Score one candidate in an open judge session, once for the same arguments.

    Args:
        job_store: Store that holds the session, its budget and its evidence.
        session_id: The session.
        username: Account that owns the session.
        candidate_commit: A commit that descends from the pinned base, or ``None``.
        candidate_patch: A ``git diff`` against the pinned base, or ``None``.
        token: Repository host token, or ``None`` for a public repository.

    Returns:
        The score, the named scores, the feedback and each search case's
        result; or the ended answer when the budget ends the session.

    Raises:
        JudgeError: When the candidate is refused or cannot be scored.
        JudgePendingError: While another request scores it or usage settles.
        DomainError: 409 ``budget.pending`` when the end must wait for usage.
    """
    job = job_store.get_job(session_id)
    if job.get("status") not in _OPEN_STATUSES:
        return _ended_answer(session_id, str(job.get("status")))
    if is_opening(job):
        raise JudgePendingError("The judge session is still opening.")
    payload = job["payload"]
    judge = payload["judge"]
    repo = payload["target"]["repo"]
    patch = _candidate_patch(repo, judge["base_commit"], candidate_commit, candidate_patch, token)
    sha = _sha256(patch)
    budget = _budget(job_store, job)
    assert budget is not None
    final_row = _final_row(job_store.engine, budget.id)
    if final_row is not None:
        return _ending_answer(job_store, job, final_row)
    if budget.state != "attached":
        ended = end_session(job_store, job, ended_by="budget")
        return _ended_answer(session_id, ended.status)
    if not budget.uncapped:
        final_cases, _held_out = _final_cases(list(payload.get("cases") or []), payload.get("seed"))
        needed = needed_cents(
            int(judge.get("floor_cents") or 0), _scored_results(job_store.engine, budget.id), max(len(final_cases), 1)
        )
        # The session ends while the budget still pays for the final score.
        if budget.total_cents - budget.setup_spent_cents - budget.run_spent_cents < needed:
            ended = end_session(job_store, job, ended_by="budget")
            return _ended_answer(session_id, ended.status)
        if budget.available_cents < needed:
            # Only the holds of calls in flight are in the way, and they release when those calls settle.
            raise JudgePendingError("Other calls of this session hold its budget. Call again with the same arguments.")
    store = _store(job_store.engine, payload)
    try:
        claim = store.claim(
            username=username,
            budget_id=budget.id,
            revision=budget.revision,
            workflow=_BLACKBOX_WORKFLOW,
            scope=JUDGE_SCOPE,
            payload={"session": session_id, "patch_sha256": sha},
            session_id=session_id,
            reuse_failed=True,
        )
    except BudgetConflictError as error:
        raise JudgePendingError("The session's budget changed during the call.") from error
    identity = claim.document["id"]
    if claim.token is None:
        return _replay(claim.document, session_id, _budget(job_store, job))
    token_ = claim.token
    # The end's claim takes the same budget lock, so an end that claimed first is visible here.
    if _final_row(job_store.engine, budget.id) is not None:
        store.finish(
            identity,
            claim_token=token_,
            status="failed",
            result=_failure("judge.session_ended", "The judge session ended before this call scored."),
        )
        return _ended_answer(session_id, str(job_store.get_job(session_id)["status"]))

    def refuse(protected: dict[str, Any], staged: StagedRepository) -> None:
        """Refuse a candidate that changes the contract or a protected path, before any paid box.

        Args:
            protected: The staged payload.
            staged: The staged repository.

        Raises:
            _RefusedError: With every broken rule.
        """
        problems = patch_violations(
            patch,
            protected["target"]["repo"]["editable_paths"],
            staged.snapshot.readonly_paths,
            (*staged.protected_paths, CONSTRAINTS_FILE, *staged.hidden_paths),
            exploit_paths=(staged.exploits_path,),
        )
        if problems:
            raise _RefusedError(" ".join(problems))

    budget_ended = False
    transient = False
    error: JudgeError | None = None
    try:
        with store.heartbeat(identity, claim_token=token_):
            try:
                with judge_scorer(
                    payload,
                    username=username,
                    budget_id=budget.id,
                    generation=claim.generation,
                    owner_id=f"judge-{identity}-{claim.attempt}",
                    image=job.get("sandbox_image"),
                    engine=job_store.engine,
                    case_count=len(payload.get("cases") or []),
                    before_scoring=refuse,
                ) as (protected, staged, _gateway):
                    chosen = _search_cases(list(protected.get("cases") or []), payload.get("seed"))
                    final = _score(staged, patch, [case for _, case in chosen], label="candidate")
                after = _budget(job_store, job)
                assert after is not None
                # Concurrent calls that settle in this window count too, so their overlap makes the spend err high.
                spent = (
                    after.setup_spent_cents + after.run_spent_cents - budget.setup_spent_cents - budget.run_spent_cents
                )
                result = {
                    "score": final.score,
                    "named_scores": _named(final),
                    "feedback": final.feedback,
                    "cases": [
                        {"index": index, "score": score, "feedback": feedback}
                        for (index, _case), (score, feedback) in zip(chosen, final.cases, strict=True)
                    ],
                    "failed_cases": len(final.failures),
                    "patch_sha256": sha,
                    "spent_cents": float(max(spent, Decimal(0))),
                }
                document = store.finish(
                    identity,
                    claim_token=token_,
                    status="succeeded",
                    result={
                        "checks": [{"key": "judge", "status": "succeeded"}],
                        "judge_result": result,
                        "_patch": patch,
                    },
                )
            except UsagePendingError:
                document = store.finish(
                    identity,
                    claim_token=token_,
                    status="pending",
                    result={"checks": [{"key": "judge", "status": "pending"}], "_retry_after_usage": True},
                )
            except (BudgetReached, BudgetInsufficientError) as exc:
                budget_ended = True
                document = store.finish(
                    identity, claim_token=token_, status="failed", result=_failure("judge.session_ended", str(exc))
                )
            except BudgetConflictError:
                raise
            except _RefusedError as exc:
                error = JudgeError("judge.candidate_refused", f"The candidate breaks the editing rules: {exc}")
                document = store.finish(
                    identity, claim_token=token_, status="failed", result=_failure(error.code, error.detail)
                )
            except (ServiceError, RuntimeError, ValueError) as exc:
                if isinstance(exc, _TRANSIENT_ERRORS) or isinstance(exc.__cause__, _TRANSIENT_ERRORS):
                    transient = True
                    # No judge_error, so the next claim runs the call again instead of replaying it.
                    document = store.finish(
                        identity,
                        claim_token=token_,
                        status="pending",
                        result={"checks": [{"key": "judge", "status": "pending"}], "_retry_after_usage": True},
                    )
                else:
                    # ValueError covers the other budget errors; the message never holds metric code.
                    error = JudgeError("judge.score_failed", str(exc))
                    document = store.finish(
                        identity, claim_token=token_, status="failed", result=_failure(error.code, error.detail)
                    )
    except BudgetConflictError as exc:
        raise JudgePendingError("The session's budget changed during the call.") from exc
    if transient:
        raise JudgePendingError(
            "A temporary repository or sandbox failure stopped the call. Call again with the same arguments."
        )
    if budget_ended:
        ended = end_session(job_store, job_store.get_job(session_id), ended_by="budget")
        return _ended_answer(session_id, ended.status)
    if error is not None:
        raise JudgeError(error.code, error.detail[:_MESSAGE_CHARS])
    if document.get("status") != "succeeded":
        raise JudgePendingError("The call's usage settles. Call again with the same arguments.")
    return _scored_answer(session_id, document["judge_result"], _budget(job_store, job))


def _rows(engine: Any, budget_id: str, scope: str, statuses: tuple[str, ...] | None = None) -> list[Any]:
    """Read the session's evidence rows of one scope.

    Args:
        engine: Database that holds the evidence.
        budget_id: The session's budget.
        scope: ``judge`` or ``judge-final``.
        statuses: Only rows with one of these statuses, or every row.

    Returns:
        The rows.
    """
    query = select(WizardPreflightModel).where(
        WizardPreflightModel.budget_id == budget_id, WizardPreflightModel.scope == scope
    )
    if statuses is not None:
        query = query.where(WizardPreflightModel.status.in_(statuses))
    with Session(engine) as session:
        return list(session.scalars(query))


def _scored_results(engine: Any, budget_id: str) -> list[dict[str, Any]]:
    """Return the stored results of the session's scored calls.

    Args:
        engine: Database that holds the evidence.
        budget_id: The session's budget.

    Returns:
        Each scored call's ``judge_result``.
    """
    rows = _rows(engine, budget_id, JUDGE_SCOPE, ("succeeded", "pending"))
    return [row.result["judge_result"] for row in rows if isinstance((row.result or {}).get("judge_result"), dict)]


def _final_row(engine: Any, budget_id: str) -> Any | None:
    """Return the end's evidence row: once it exists, the session is ending.

    Args:
        engine: Database that holds the evidence.
        budget_id: The session's budget.

    Returns:
        The row, or ``None`` before an end claimed.
    """
    return next(iter(_rows(engine, budget_id, FINAL_SCOPE)), None)


def _live_calls(engine: Any, budget_id: str) -> bool:
    """Tell whether a call of the session still runs under a lease its heartbeat renews.

    A crashed call's lease expires, and the next claim marks its row pending,
    so a dead call never holds the end.

    Args:
        engine: Database that holds the evidence.
        budget_id: The session's budget.

    Returns:
        True while a live call runs.
    """
    now = datetime.now(UTC)
    for row in _rows(engine, budget_id, JUDGE_SCOPE, ("running",)):
        expires = row.lease_expires_at
        if expires is not None and (expires if expires.tzinfo else expires.replace(tzinfo=UTC)) > now:
            return True
    return False


def _ending_answer(job_store: Any, job: dict[str, Any], final_row: Any) -> dict[str, Any]:
    """Answer a call in a session whose end claimed: the call runs no paid work.

    An end that waits (its row is pending) runs again here, so a client that
    keeps calling still drives the end to its final score.

    Args:
        job_store: Store that holds the session, its budget and its evidence.
        job: The session's job record.
        final_row: The end's evidence row.

    Returns:
        The ended answer.

    Raises:
        DomainError: 409 ``budget.pending`` while the end must still wait.
    """
    session_id = job["optimization_id"]
    if final_row.status == "pending":
        ended = end_session(job_store, job, ended_by=str((final_row.result or {}).get("ended_by") or "budget"))
        return _ended_answer(session_id, ended.status)
    return _ended_answer(session_id, str(job_store.get_job(session_id)["status"]))


def _best_call(engine: Any, budget_id: str) -> tuple[dict[str, Any] | None, int]:
    """Find the session's best scored call: the highest score, the earliest on a tie.

    Args:
        engine: Database that holds the evidence.
        budget_id: The session's budget.

    Returns:
        The best call's stored result (``None`` without a scored call), and
        how many calls the session scored.
    """
    rows = _rows(engine, budget_id, JUDGE_SCOPE, ("succeeded", "pending"))
    done = [row for row in rows if isinstance((row.result or {}).get("judge_result"), dict)]
    scored = [row for row in done if isinstance(row.result["judge_result"].get("score"), int | float)]
    if not scored:
        return None, len(done)
    best = min(scored, key=lambda row: (-float(row.result["judge_result"]["score"]), row.started_at))
    return best.result, len(done)


def _stop(job_store: Any, job: dict[str, Any], status: str, message: str) -> None:
    """Write a terminal status, then close the budget to new work.

    Args:
        job_store: Store that holds the session.
        job: The session's job record.
        status: The terminal status.
        message: The run message.
    """
    job_store.update_job_if_status(
        job["optimization_id"], _OPEN_STATUSES, status=status, message=message, completed_at=_utc_now()
    )
    if job.get("execution_budget_id") is not None:
        try:
            BudgetService(engine=job_store.engine).stop_admission(
                job["execution_budget_id"], job["username"], reason="judge_ended"
            )
        except BudgetError:
            logger.warning("judge session %s: the budget did not close", job["optimization_id"])


def _final_result(
    final: FinalRun,
    best: dict[str, Any],
    protected: dict[str, Any],
    staged: StagedRepository,
    *,
    cases: list[Any],
    seed: int | None,
    held_out: bool,
    ended_by: str,
    calls: int,
    started: float,
    image: str | None,
) -> dict[str, Any]:
    """Build the session's stored result, with its run record.

    Args:
        final: The best version's final score.
        best: The best call's stored evidence.
        protected: The staged payload.
        staged: The staged repository.
        cases: The staged cases.
        seed: The session's seed.
        held_out: Whether ``final`` used only held-out cases.
        ended_by: ``budget`` or ``stop_run``.
        calls: How many calls the session scored.
        started: ``time.perf_counter()`` when the end started.
        image: The pinned sandbox image, or ``None``.

    Returns:
        The result as JSON.
    """
    search = best["judge_result"]
    verdict = _outcome_verdict((protected.get(REPO_SNAPSHOT_KEY) or {}).get("outcome") or {}, final)
    case_results = _case_results(None, final)
    named = final.named
    feedback = final.feedback
    if held_out:
        # Held-out feedback can quote the held-out cases; the client sees only their scores.
        for row in case_results:
            row.best_feedback = None
        named = {name: BlackboxNamedScore(score=entry.score) for name, entry in named.items()}
        feedback = None
    details: dict[str, Any] = {
        "judge": {
            "ended_by": ended_by,
            "calls": calls,
            "best_patch_sha256": search.get("patch_sha256"),
            "search_score": search.get("score"),
            "held_out_score": final.score if held_out else None,
            "held_out": held_out,
        },
        "failed_cases": len(final.failures),
        **({"outcome": verdict} if verdict else {}),
        **({CASE_SPLIT_KEY: case_split_positions(cases, legacy_case_splits(cases, seed), seed)} if cases else {}),
    }
    response = BlackboxRunResponse(
        optimizer_name=JUDGE_OPTIMIZER,
        strategy_mode="single",
        engine_used=JUDGE_OPTIMIZER,
        best_score=final.score,
        best_feedback=feedback,
        best_named_scores=named,
        case_results=case_results,
        case_count=len(final.cases),
        final_scorer_runs=final.runs,
        best_candidate=best["_patch"],
        runtime_seconds=time.perf_counter() - started,
        details=details,
    )
    result = response.model_dump(mode="json")
    add_run_record(result, protected, staged, image=image)
    record = result["details"].get("record")
    if isinstance(record, dict):
        record["score"] = final.score
        record["held_out"] = held_out
    return result


def _reopen(job_store: Any, session_id: str, budget: BudgetSnapshot) -> BudgetSnapshot:
    """Reopen a budget that a call closed at its limit, so the end can try the final score.

    Only the ``budget_reached`` close reopens, and only for an open session
    with a scored call. The budget total never changes, so the final score
    still stops at the limit.

    Args:
        job_store: Store that holds the session and its budget.
        session_id: The session.
        budget: The session's closed budget.

    Returns:
        The reopened budget, or ``budget`` unchanged when it stays closed.

    Raises:
        DomainError: 409 ``budget.pending`` while paid work of the session settles.
    """
    if (
        budget.blocked_reason != "budget_reached"
        or job_store.get_job(session_id).get("status") not in _OPEN_STATUSES
        or _best_call(job_store.engine, budget.id)[0] is None
    ):
        return budget
    try:
        return BudgetService(engine=job_store.engine).resume_admission(
            budget.id, budget.username, expected_generation=budget.generation
        )
    except BudgetInFlightError as error:
        raise DomainError("budget.pending", status=409) from error
    except (BudgetInsufficientError, BudgetConflictError):
        return budget


def end_session(job_store: Any, job: dict[str, Any], *, ended_by: str) -> JobCancelResponse:
    """End a judge session: score its best version once on the held-out cases.

    A retry after a crash finds the end's evidence row: a finished end
    returns the session status, and a running one answers ``budget.pending``.
    The end also waits while a call of the session still runs, so that call
    counts toward the best version. When a call closed the budget at its
    limit, the end reopens it once to try the final score.

    Args:
        job_store: Store that holds the session, its budget and its evidence.
        job: The session's job record, with its payload.
        ended_by: ``budget`` or ``stop_run``.

    Returns:
        The session id and its terminal status.

    Raises:
        DomainError: 409 ``budget.pending`` while the end runs or usage settles.
    """
    session_id = job["optimization_id"]
    username = job["username"]
    payload = job.get("payload") or {}
    budget = _budget(job_store, job)
    if budget is not None and budget.state != "attached":
        budget = _reopen(job_store, session_id, budget)
    if budget is None or budget.state != "attached":
        # Nothing can pay for a final score any more.
        reached = budget is not None and budget.blocked_reason == "budget_reached"
        message = _NO_FINAL_MESSAGE if reached else "The judge session ended before its final score."
        _stop(job_store, job, "cancelled" if budget is None else "stopped", message)
        return JobCancelResponse(optimization_id=session_id, status=str(job_store.get_job(session_id)["status"]))
    store = _store(job_store.engine, payload)
    try:
        claim = store.claim(
            username=username,
            budget_id=budget.id,
            revision=budget.revision,
            workflow=_BLACKBOX_WORKFLOW,
            scope=FINAL_SCOPE,
            payload={"session": session_id},
            session_id=session_id,
        )
    except BudgetConflictError as error:
        raise DomainError("budget.pending", status=409) from error
    if claim.token is None:
        if claim.document.get("status") == "succeeded":
            return JobCancelResponse(optimization_id=session_id, status=str(job_store.get_job(session_id)["status"]))
        raise DomainError("budget.pending", status=409)
    identity, token = claim.document["id"], claim.token
    waiting = {"checks": [{"key": FINAL_SCOPE, "status": "pending"}], "_retry_after_usage": True, "ended_by": ended_by}
    # After this claim no new call starts, and a claim marks a crashed call's row pending.
    if _live_calls(job_store.engine, budget.id):
        store.finish(identity, claim_token=token, status="pending", result=waiting)
        raise DomainError("budget.pending", status=409)
    done = {"checks": [{"key": FINAL_SCOPE, "status": "succeeded"}]}
    best, calls = _best_call(job_store.engine, budget.id)
    started = time.perf_counter()
    try:
        with store.heartbeat(identity, claim_token=token):
            if best is None:
                status = "cancelled"
                _stop(job_store, job, status, "The judge session ended without a scored candidate.")
                store.finish(identity, claim_token=token, status="succeeded", result=done)
                return JobCancelResponse(optimization_id=session_id, status=status)
            try:
                with judge_scorer(
                    payload,
                    username=username,
                    budget_id=budget.id,
                    generation=claim.generation,
                    owner_id=f"judge-final-{identity}-{claim.attempt}",
                    image=job.get("sandbox_image"),
                    engine=job_store.engine,
                    case_count=len(payload.get("cases") or []),
                ) as (protected, staged, _gateway):
                    cases = list(protected.get("cases") or [])
                    seed = payload.get("seed")
                    final_cases, held_out = _final_cases(cases, seed)
                    final = _score(staged, best["_patch"], final_cases, label="best version")
                    result = _final_result(
                        final,
                        best,
                        protected,
                        staged,
                        cases=cases,
                        seed=seed,
                        held_out=held_out,
                        ended_by=ended_by,
                        calls=calls,
                        started=started,
                        image=job.get("sandbox_image"),
                    )
            except (UsagePendingError, BudgetInFlightError) as error:
                store.finish(identity, claim_token=token, status="pending", result=waiting)
                raise DomainError("budget.pending", status=409) from error
            except (BudgetReached, BudgetInsufficientError):
                status = "stopped"
                _stop(job_store, job, status, _NO_FINAL_MESSAGE)
            except (ServiceError, RuntimeError, ValueError) as error:
                status = "failed"
                _stop(job_store, job, status, f"The final score failed: {str(error)[:_MESSAGE_CHARS]}")
            else:
                status = "success"
                job_store.update_job_if_status(
                    session_id,
                    ("running",),
                    status=status,
                    result=result,
                    message=_END_MESSAGE,
                    completed_at=_utc_now(),
                )
                _stop(job_store, job, status, _END_MESSAGE)
            store.finish(identity, claim_token=token, status="succeeded", result=done)
    except BudgetConflictError as error:
        raise DomainError("budget.pending", status=409) from error
    logger.info("Ended judge session %s (%s): %s", session_id, ended_by, status)
    return JobCancelResponse(optimization_id=session_id, status=status)
