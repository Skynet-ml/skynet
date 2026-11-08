"""The real metric-hardening runner: model calls, scorer boxes and the job's one budget hold (SKY-369).

The SKY-355 core asks a runner to attack, judge, draft fixes and score. This
module answers with the job's attacker and judge models through a metered
model gateway, and scores in parent-owned boxes on the trusted tree. The
attacker sees only the stripped tree (SKY-353).

A cloud job holds rounds × cap once, as one setup-phase operation keyed
``hardening:<job_id>``. Every model call settles its cost into that
operation as a cumulative, non-final settle, so the hold is never counted
twice. Sandbox boxes meter themselves under their own operations. When the
job ends, :meth:`HardeningBudget.finish` settles the actual spend for good, or
releases the hold when nothing was spent.
"""

from __future__ import annotations

import contextlib
import copy
import json
import logging
import re
import shutil
import tarfile
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar

from sqlalchemy import Engine, or_, select, update
from sqlalchemy.orm import Session

from ..api.errors import DomainError
from ..api.model_catalog import agent_model_id
from ..billing.budgets import BudgetService
from ..billing.byok_bridge import inject_byok_connections, payload_uses_token_source
from ..billing.byok_vault import ProviderKeyVault
from ..billing.model_gateway import ModelGateway
from ..billing.operation_pricing import ChargePolicy, OperationQuote, json_fingerprint
from ..billing.protected_execution import bind_protected_sandbox
from ..billing.runtime import BudgetRuntime, PaidResult, ProviderFailedError, generation_operation_key
from ..billing.vercel_usage import PACKAGE_REGISTRY_HOSTS
from ..config import Settings
from ..constants import TOKEN_SOURCE_BYOK, TOKEN_SOURCE_MANAGED
from ..models.hardening import HardeningExploit, HardeningJob, HardeningJobModel, HardeningView
from ..service_gateway.optimization.blackbox.classification import JUDGE_LABELS, answer_format
from ..service_gateway.optimization.blackbox.hardening import Candidate, HardeningRunner
from ..service_gateway.optimization.blackbox.repo_scorer import RepoScorer
from ..service_gateway.optimization.blackbox.repo_tree import apply_patch
from ..service_gateway.optimization.blackbox.repo_workspace import RepoWorkspace
from ..service_gateway.optimization.blackbox.sandbox_contract import SandboxSpec
from .hardening_job import HardeningWorkspace, run_hardening_job
from .repo_staging import _judge_limits, _reply_text, _repo_model_gateway

T = TypeVar("T")

logger = logging.getLogger(__name__)

HARDENING_PRICE_VERSION = "skynet-hardening-hold-v1"
OPEN_STATES = frozenset({"reserved", "dispatched", "pending"})
QUEUED_STATUSES = ("preflight", "running")
WAITING_OR_QUEUED = frozenset({"preflight", "running", "waiting_review", "waiting_fix"})
CLAIM_SCAN_LIMIT = 20
# A failed close leaves the hold on an ended row; an idle worker retries it at
# most this often, so a broken ledger does not turn every poll into writes.
HOLD_SWEEP_SECONDS = 60.0
HOLD_SWEEP_LIMIT = 20
_sweep_lock = threading.Lock()
_last_sweep = 0.0
# The attacker reads the stripped tree as text; past this the prompt would
# cost more than a round may spend, so the rest of the tree is cut.
MAX_TREE_CHARS = 120_000
MAX_FILE_CHARS = 20_000
ATTACK_ANSWER_TOKENS = 8_000
FIX_ANSWER_TOKENS = 8_000
SCORE_TIMEOUT_SECONDS = 600.0


class HardeningBudgetExceededError(RuntimeError):
    """A model call would spend past the job's hold."""


class HardeningClaimLostError(RuntimeError):
    """Another pod took the job, so this pass must stop paying for calls."""


def operation_key(job_id: str) -> str:
    """Return the one budget operation key a hardening job holds under.

    Args:
        job_id: The job's id.

    Returns:
        ``hardening:<job_id>``.
    """
    return f"hardening:{job_id}"


class HardeningMeter(BudgetRuntime):
    """A budget runtime that charges every model call to the job's one hold.

    Sandbox and connector costs, and every call of a local job, go through
    the normal per-call operations of :class:`BudgetRuntime`.
    """

    def __init__(
        self,
        service: BudgetService,
        *,
        operation_id: str | None,
        lost: threading.Event | None = None,
        exhausted: threading.Event | None = None,
        **kwargs: Any,
    ) -> None:
        """Create the meter.

        Args:
            service: The budget service.
            operation_id: The job's hold, or None for a local job.
            lost: Set when this pod loses its claim on the job.
            exhausted: Set when the hold cannot cover another model call.
            **kwargs: The :class:`BudgetRuntime` arguments.
        """
        super().__init__(service, **kwargs)
        self.operation_id = operation_id
        self.spent_usd = 0.0
        self._lost = lost if lost is not None else threading.Event()
        self._exhausted = exhausted if exhausted is not None else threading.Event()
        self._calls = 0
        # Gateway threads share one meter. The lock keeps each cumulative
        # settle on top of the latest one, and ``_inflight`` counts the
        # maximum of every call that is out, so two calls cannot both fit
        # into the last of the hold.
        self._lock = threading.Lock()
        self._inflight = Decimal(0)

    def execute(
        self,
        quote: OperationQuote,
        policy: ChargePolicy,
        dispatch: Callable[[], PaidResult[T]],
        *,
        operation_key: str,
        cost_kind: str,
        role: str | None = None,
        attempt: int = 0,
    ) -> T:
        """Dispatch one call and settle its cost into the job's hold.

        Args:
            quote: Bound tied to the exact request.
            policy: The conversion policy recorded in the quote.
            dispatch: Single-attempt transport.
            operation_key: The call's own key; unused for model calls of a cloud job.
            cost_kind: Model or sandbox category.
            role: Optional model role attribution.
            attempt: Physical retry number.

        Returns:
            The provider result.

        Raises:
            HardeningClaimLostError: When another pod took the job.
            HardeningBudgetExceededError: When the call's maximum would pass the hold.
            ProviderFailedError: When the provider failed the response.
        """
        if self._lost.is_set():
            raise HardeningClaimLostError("Another pod took the hardening job.")
        if self.operation_id is None or cost_kind != "model":
            return super().execute(
                quote, policy, dispatch, operation_key=operation_key, cost_kind=cost_kind, role=role, attempt=attempt
            )
        if quote.price_snapshot.get("policy") != policy.snapshot():
            raise ValueError("Settlement policy must match the admitted quote.")
        with self._lock:
            operation = self.service.get_operation(self.operation_id, self.username)
            if operation.actual_cents + self._inflight + quote.maximum.total > operation.max_cents:
                self._exhausted.set()
                raise HardeningBudgetExceededError("The job's hold cannot cover another model call.")
            if operation.state == "reserved":
                self.service.mark_dispatched(self.operation_id, self.username)
            self._inflight += quote.maximum.total
        try:
            try:
                result = dispatch()
            except BaseException as exc:
                # The provider may have run the call before the transport
                # failed, so the hold keeps the call's maximum.
                self._settle(
                    quote.maximum.total,
                    quote.maximum.wallet,
                    {"usage": "unconfirmed", "failure": type(exc).__name__},
                    role,
                    policy,
                )
                raise
            evidence = dict(result.evidence)
            if result.refused:
                charge_total = charge_wallet = Decimal(0)
            elif result.provider_usd is None:
                # No confirmed usage: charge the call's maximum so the user is
                # never under-billed for a call that may have run.
                charge_total, charge_wallet = quote.maximum.total, quote.maximum.wallet
                evidence["usage"] = "unconfirmed"
            else:
                charge = policy.convert(result.provider_usd)
                charge_total, charge_wallet = charge.total, charge.wallet
            self._settle(charge_total, charge_wallet, evidence, role, policy)
        finally:
            with self._lock:
                self._inflight -= quote.maximum.total
        if result.failure is not None:
            raise ProviderFailedError(result.failure)
        return result.value

    def _settle(
        self,
        charge_total: Decimal,
        charge_wallet: Decimal,
        evidence: dict[str, Any],
        role: str | None,
        policy: ChargePolicy,
    ) -> None:
        """Add one call's charge to the hold as a cumulative, non-final settle.

        Args:
            charge_total: The call's charge in cents.
            charge_wallet: The wallet part of the charge in cents.
            evidence: The call's usage evidence.
            role: Optional model role attribution.
            policy: The conversion policy, for the dollars spent.
        """
        assert self.operation_id is not None
        with self._lock:
            operation = self.service.get_operation(self.operation_id, self.username)
            total = operation.actual_cents + charge_total
            wallet = operation.actual_wallet_cents + charge_wallet
            self._calls += 1
            self.service.settle(
                self.operation_id,
                self.username,
                evidence_key=json_fingerprint({"call": self._calls, "total": str(total), "evidence": evidence}),
                actual_cents=total,
                actual_wallet_cents=wallet,
                evidence={**evidence, "role": role},
                final=False,
            )
            self.spent_usd += float(charge_total * policy.cent_usd)


class HardeningBudget:
    """Holds and closes one hardening job's budget operation."""

    def __init__(self, service: BudgetService, *, username: str, budget_id: str) -> None:
        """Create the budget handle.

        Args:
            service: The budget service.
            username: The job's owner.
            budget_id: The setup's execution budget.
        """
        self.service = service
        self.username = username
        self.budget_id = budget_id

    def reserve(self, job: HardeningJob, hold_usd: float, *, current: str | None = None) -> str:
        """Hold the job's dollars once; a repeat call returns the same open operation.

        A retried job whose earlier hold already closed takes the next attempt
        of the same key, so the ledger keeps one key for each job.

        Args:
            job: The job.
            hold_usd: Rounds × the per-round cap.
            current: The job's stored operation, if any.

        Returns:
            The open operation id.
        """
        if current is not None and self.service.get_operation(current, self.username).state in OPEN_STATES:
            return current
        policy = ChargePolicy("managed_model")
        attempt = 0
        while True:
            snapshot = self.service.get(self.budget_id, self.username)
            operation = self.service.reserve(
                self.budget_id,
                self.username,
                operation_key=generation_operation_key(operation_key(job.id), snapshot.generation),
                generation=snapshot.generation,
                phase="setup",
                cost_kind="model",
                request_fingerprint=json_fingerprint({"job": job.id, "hold_usd": str(hold_usd)}),
                price_snapshot={"version": HARDENING_PRICE_VERSION, "policy": policy.snapshot()},
                max_cents=Decimal(str(hold_usd)) / policy.cent_usd,
                attempt=attempt,
            )
            if operation.state in OPEN_STATES:
                return operation.id
            attempt += 1

    def finish(self, operation_id: str) -> None:
        """Settle the actual spend for good, or release a hold that spent nothing.

        Args:
            operation_id: The job's operation.
        """
        operation = self.service.get_operation(operation_id, self.username)
        if operation.state == "reserved":
            self.service.release(operation_id, self.username)
        elif operation.state in {"dispatched", "pending"}:
            self.service.settle(
                operation_id,
                self.username,
                evidence_key=json_fingerprint({"final": operation_id, "total": str(operation.actual_cents)}),
                actual_cents=operation.actual_cents,
                actual_wallet_cents=operation.actual_wallet_cents,
                evidence={"hardening": "end"},
                final=True,
            )


class RepoHardeningRunner(HardeningRunner):
    """Attacks, judges and scores one repository's metric for one pass."""

    def __init__(
        self,
        workspace: HardeningWorkspace,
        job: HardeningJob,
        *,
        gateway: ModelGateway,
        meter: HardeningMeter,
        open_scorer: Callable[[Path, tuple[str, ...]], RepoScorer],
        managed_key: str,
    ) -> None:
        """Create the runner.

        Args:
            workspace: The trees and metric paths of this pass.
            job: The job.
            gateway: The metered model gateway.
            meter: The gateway's runtime, which counts the dollars spent.
            open_scorer: Opens and prepares a scorer box on one archive and
                the fixes packed into it.
            managed_key: The platform's OpenRouter key.
        """
        self.workspace = workspace
        self.job = job
        self.gateway = gateway
        self.meter = meter
        self.open_scorer = open_scorer
        self.fixes: list[str] = list(workspace.applied_fixes)
        self._scorers: dict[tuple[str, ...], RepoScorer] = {}
        self._routes: dict[str, dict[str, str]] = {}
        self._managed_key = managed_key
        self._scratch = Path(tempfile.mkdtemp(prefix="hardening-"))

    def preflight(self, *, view: HardeningView) -> list[str]:
        """Report nothing: the worker's preflight gate already ran.

        Args:
            view: What the attacker may see.

        Returns:
            An empty list.
        """
        return []

    def attack(
        self, *, round_index: int, view: HardeningView, model: str, exploits_path: Path, budget_usd: float | None
    ) -> tuple[list[Candidate], float]:
        """Ask the attacker model for exploits of the metric.

        Args:
            round_index: The 1-based round.
            view: What the attacker may see.
            model: The attacker model.
            exploits_path: The suite folder.
            budget_usd: Unused; the hold bounds the spend.

        Returns:
            The candidates and the dollars spent.
        """
        known = sorted(p.name for p in exploits_path.iterdir() if p.is_dir()) if exploits_path.is_dir() else []
        metric = ""
        if view == "white_box":
            metric = f"\nThe metric code:\n{self.job.scorer.get('metric_code', '') if self.job.scorer else ''}\n"
        prompt = (
            f"Round {round_index}. Find ways to raise this repository's metric score without a real improvement.\n"
            f"Known exploits, which you must not repeat: {', '.join(known) or 'none'}.{metric}\n"
            "The repository files:\n"
            f"{_tree_text(self.workspace.agent_archive)}\n"
            'Answer with JSON only: {"candidates": [{"kind": "case" or "method", '
            '"patch_diff": "<git diff against the repository>", "why_md": "<why it games the metric>"}]}'
        )
        reply, cost = self._ask(model, "attacker", prompt, {"max_tokens": ATTACK_ANSWER_TOKENS})
        return _candidates(reply), cost

    def judge(self, *, candidate: Candidate, model: str, budget_usd: float | None) -> tuple[Any, float]:
        """Ask the judge model whether one candidate games the metric.

        Args:
            candidate: The attacker's claim.
            model: The judge model.
            budget_usd: Unused; the hold bounds the spend.

        Returns:
            The raw judge reply and the dollars spent.
        """
        prompt = (
            "Does this change raise the metric score without a real improvement (an exploit), "
            "or is it a valid improvement?\n"
            f"Claim:\n{candidate.why_md}\n\nChange:\n{candidate.patch_diff}\n\n{answer_format(JUDGE_LABELS)}"
        )
        reply, cost = self._ask(model, "judge", prompt, None)
        return reply or "", cost

    def draft_fix(
        self, *, exploits: list[HardeningExploit], model: str, exploits_path: Path, budget_usd: float | None
    ) -> tuple[str, float]:
        """Ask the attacker model for a metric fix that defeats the confirmed exploits.

        Args:
            exploits: The confirmed exploits.
            model: The model that drafts the fix.
            exploits_path: The suite folder.
            budget_usd: Unused; the hold bounds the spend.

        Returns:
            The fix as a unified diff, and the dollars spent.
        """
        listed = "\n\n".join(f"Exploit {e.id}:\n{e.why_md}\n{e.patch_diff}" for e in exploits)
        prompt = (
            "These changes raise the metric score without a real improvement:\n"
            f"{listed}\n\nThe metric code:\n{self.job.scorer.get('metric_code', '') if self.job.scorer else ''}\n\n"
            "Write a git diff to the repository's metric files that stops these exploits and "
            "keeps the score of honest solutions. Answer with the diff only."
        )
        reply, cost = self._ask(model, "fixer", prompt, {"max_tokens": FIX_ANSWER_TOKENS})
        return _diff_only(reply or ""), cost

    def score(self, *, fix_diff: str | None, exploit: HardeningExploit | None) -> float:
        """Score the base solution, or an exploit, on the trusted metric.

        Args:
            fix_diff: A metric fix to apply first, or None.
            exploit: The exploit to replay, or None for the base solution.

        Returns:
            The metric score.
        """
        return self._score(fix_diff, exploit.patch_diff if exploit is not None else "")

    def reference_score(self, *, fix_diff: str | None, reference: Path) -> float:
        """Score the reference solution on the trusted metric.

        Args:
            fix_diff: A metric fix to apply first, or None.
            reference: A patch file, or a folder that holds ``patch.diff``.

        Returns:
            The metric score.
        """
        patch = reference / "patch.diff" if reference.is_dir() else reference
        return self._score(fix_diff, patch.read_text(encoding="utf-8"))

    def apply_fix(self, *, fix_diff: str) -> None:
        """Score later checks of this pass with an approved fix.

        Args:
            fix_diff: The approved fix.
        """
        self.fixes.append(fix_diff)

    def close(self) -> None:
        """Close every scorer box and delete the scratch trees."""
        scorers, self._scorers = list(self._scorers.values()), {}
        for scorer in scorers:
            # The gateway still stops a box that fails to close.
            with contextlib.suppress(Exception):
                scorer.close()
        shutil.rmtree(self._scratch, ignore_errors=True)

    def _ask(self, model: str, role: str, prompt: str, limits: dict[str, Any] | None) -> tuple[str | None, float]:
        """Send one prompt through a metered route and count its cost.

        Args:
            model: The model.
            role: The route's role name.
            prompt: The question.
            limits: Request limits; the judge limits when None.

        Returns:
            The reply text (None when refused or cut off) and the dollars spent.
        """
        route = self._route(model, role)
        before = self.meter.spent_usd
        body = {
            "model": route["model"],
            "messages": [{"role": "user", "content": prompt}],
            **(limits if limits is not None else _judge_limits(route["model"])),
        }
        response = self.gateway.dispatch_guest(route["token"], "/v1/chat/completions", body, {})
        text = _reply_text(response.body) if 200 <= response.status < 300 else None
        return text, self.meter.spent_usd - before

    def _route(self, model: str, role: str) -> dict[str, str]:
        """Register a parent-only route for one model, once.

        Args:
            model: The catalog model id.
            role: The role name.

        Returns:
            The route.
        """
        key = f"{role}:{model}"
        if key not in self._routes:
            self._routes[key] = self.gateway.register(
                model=agent_model_id(model),
                api_key=self._managed_key,
                role="judge" if role == "judge" else "optimization",
                policy=ChargePolicy("managed_model"),
                parent_only=True,
            )
        return self._routes[key]

    def _score(self, fix_diff: str | None, patch: str) -> float:
        """Score one solution patch on the trusted tree with the fixes so far.

        Args:
            fix_diff: A fix to try on top of the approved ones, or None.
            patch: The solution as a git patch.

        Returns:
            The metric score, or NaN when ``fix_diff`` does not apply.
        """
        fixes = tuple(self.fixes) + ((fix_diff,) if fix_diff else ())
        scorer = self._scorers.get(fixes)
        if scorer is None:
            try:
                archive = self._fixed_archive(fixes)
            except RuntimeError:
                if not fix_diff:
                    raise
                # A drafted fix that does not apply must fail every check, so
                # the job waits for the user instead of failing (and paying
                # again). NaN compares false both ways; -inf <= -inf would pass
                # the suite replay.
                logger.info("Hardening job %s: the drafted fix does not apply.", self.job.id)
                return float("nan")
            scorer = self.open_scorer(archive, fixes)
            self._scorers[fixes] = scorer
        return float(scorer.score(patch)["score"])

    def _fixed_archive(self, fixes: tuple[str, ...]) -> Path:
        """Pack the trusted tree with the given metric fixes applied in order.

        Args:
            fixes: The fixes.

        Returns:
            The packed tree.
        """
        folder = Path(tempfile.mkdtemp(dir=self._scratch))
        tree = folder / "tree"
        shutil.copytree(self.workspace.trusted_root, tree, symlinks=True)
        for fix in fixes:
            apply_patch(tree, fix)
        archive = folder / "tree.tgz"
        with tarfile.open(archive, "w:gz") as packed:
            for entry in sorted(tree.iterdir()):
                packed.add(entry, arcname=entry.name)
        return archive


@contextmanager
def open_hardening_runner(
    workspace: HardeningWorkspace,
    job: HardeningJob,
    *,
    service: BudgetService,
    username: str,
    budget_id: str,
    operation_id: str | None,
    settings: Settings,
    claim: str,
    vault: ProviderKeyVault | None = None,
    lost: threading.Event | None = None,
    exhausted: threading.Event | None = None,
    on_commit: Callable[[str | None], None] = lambda _commit: None,
) -> Iterator[RepoHardeningRunner]:
    """Open the gateway, sandbox broker and runner for one pass, and close them on exit.

    Args:
        workspace: The trees and metric paths of this pass.
        job: The job.
        service: The budget service.
        username: The job's owner.
        budget_id: The setup's execution budget.
        operation_id: The job's hold, or None for a local job.
        settings: The deployment settings.
        claim: This pass's claim token; it keys the scorer boxes of the pass.
        vault: Resolves a BYOK scorer model's saved key; needed only for one.
        lost: Set when this pod loses its claim, so the meter stops paying.
        exhausted: Set when the hold cannot cover another model call.
        on_commit: Receives the commit the pass staged, so later passes pin it.

    Yields:
        The runner.

    Raises:
        ValueError: When the job has no Python metric, managed models are not
            configured, or a BYOK scorer model has no vault.
    """
    scorer_spec = copy.deepcopy(job.scorer or {})
    if scorer_spec.get("kind", "python") != "python" or not scorer_spec.get("metric_code"):
        raise ValueError("A hardening job needs a Python metric.")
    if settings.openrouter_api_key is None:
        raise ValueError("Managed model routing is not configured.")
    managed_key = settings.openrouter_api_key.get_secret_value()
    if payload_uses_token_source({"scorer": scorer_spec}, TOKEN_SOURCE_BYOK, default_token_source=TOKEN_SOURCE_MANAGED):
        if vault is None:
            raise ValueError("A BYOK scorer model needs the provider-key vault.")
        # The job row never keeps a key (the router strips it), so the saved
        # key is resolved here, as a run submit resolves it.
        inject_byok_connections(
            {"scorer": scorer_spec}, username=username, vault=vault, default_token_source=TOKEN_SOURCE_MANAGED
        )
    on_commit(workspace.commit)
    snapshot = service.get(budget_id, username)
    meter = HardeningMeter(
        service,
        operation_id=operation_id,
        lost=lost,
        exhausted=exhausted,
        username=username,
        budget_id=budget_id,
        generation=snapshot.generation,
        phase="setup",
    )
    gateway = ModelGateway(meter)
    runner: RepoHardeningRunner | None = None
    try:
        target = job.target or {}
        setup_command = (target.get("setup_command") or "").strip() or None
        hosts = PACKAGE_REGISTRY_HOSTS if setup_command else ()
        descriptor = bind_protected_sandbox(
            gateway, settings, workflow="anything", owner_id=operation_key(job.id), parent_hosts=hosts
        )
        protected = gateway.protect_payload({"scorer": dict(scorer_spec)}, managed_key=managed_key, allow_batch=False)
        model_gateway = _repo_model_gateway(protected["scorer"])
        editable = tuple(target.get("repo", {}).get("editable_paths") or ())

        def open_scorer(archive: Path, fixes: tuple[str, ...]) -> RepoScorer:
            """Open and prepare one scorer box on a packed tree.

            Each box has its own operation key: a pass opens one box for each
            set of fixes, and the base box is still open when a fix box opens.

            Args:
                archive: The packed trusted tree.
                fixes: The metric fixes packed into the tree.

            Returns:
                The prepared scorer.
            """
            box = json_fingerprint({"claim": claim, "fixes": list(fixes)})[:24]
            scorer = RepoScorer(
                RepoWorkspace(
                    runtime=gateway.parent_sandbox_runtime(),
                    spec=SandboxSpec(
                        lifetime_seconds=descriptor["lifetime_seconds"],
                        image=descriptor["image"],
                        network_disabled=not hosts,
                        allowed_hosts=hosts,
                        operation_key=f"repo-scorer:{operation_key(job.id)}:{box}",
                    ),
                    archive=archive,
                    editable_paths=editable,
                    readonly_paths=(),
                    setup_command=setup_command,
                    secrets=workspace.secrets,
                ),
                str(scorer_spec["metric_code"]),
                timeout_seconds=float(scorer_spec.get("timeout_seconds") or SCORE_TIMEOUT_SECONDS),
                gateway=model_gateway,
            )
            scorer.prepare()
            return scorer

        runner = RepoHardeningRunner(
            workspace, job, gateway=gateway, meter=meter, open_scorer=open_scorer, managed_key=managed_key
        )
        yield runner
    finally:
        if runner is not None:
            runner.close()
        gateway.close()


def claim_hardening_job(
    engine: Engine, *, pod: str, lease_seconds: float, place: str
) -> tuple[str, str, HardeningJob, str | None, str] | None:
    """Claim one queued hardening job for this pod, or take one whose lease lapsed.

    The claim is one conditional UPDATE: of two pods that race for a row, only
    the one whose UPDATE matches the unclaimed or lapsed row wins it. Each
    claim stores its own token, so a later claim by another thread of the same
    pod still fences the earlier one.

    Args:
        engine: SQLAlchemy engine of the job store.
        pod: This worker pod's name.
        lease_seconds: How long the claim lasts without a renewal.
        place: ``local`` or ``cloud``; the worker takes only jobs of its place.

    Returns:
        The job id, its owner, the job, its stored budget operation and the
        claim token, or None when no job is free.
    """
    now = datetime.now(UTC)
    claim = f"{pod}:{uuid.uuid4().hex}"
    free = or_(HardeningJobModel.claimed_by.is_(None), HardeningJobModel.lease_expires_at < now)
    queued = HardeningJobModel.status.in_(QUEUED_STATUSES)
    with Session(engine) as session:
        rows = session.execute(
            select(HardeningJobModel.id, HardeningJobModel.job)
            .where(queued, free)
            .order_by(HardeningJobModel.updated_at)
            .limit(CLAIM_SCAN_LIMIT)
        ).all()
        for job_id, document in rows:
            if (document or {}).get("place") != place:
                continue
            claimed = session.execute(
                update(HardeningJobModel)
                .where(HardeningJobModel.id == job_id, queued, free)
                .values(claimed_by=claim, lease_expires_at=now + timedelta(seconds=lease_seconds))
            )
            if claimed.rowcount != 1:
                continue
            session.commit()
            row = session.get(HardeningJobModel, job_id)
            assert row is not None
            session.refresh(row)
            return row.id, row.username, HardeningJob.model_validate(row.job), row.budget_operation_id, claim
        return None


def renew_hardening_lease(engine: Engine, job_id: str, *, claim: str, lease_seconds: float) -> bool:
    """Extend this claim's lease on a job.

    Args:
        engine: SQLAlchemy engine of the job store.
        job_id: The job.
        claim: The claim token :func:`claim_hardening_job` returned.
        lease_seconds: The new lease window from now.

    Returns:
        Whether this claim still held the job.
    """
    with Session(engine) as session:
        renewed = session.execute(
            update(HardeningJobModel)
            .where(HardeningJobModel.id == job_id, HardeningJobModel.claimed_by == claim)
            .values(lease_expires_at=datetime.now(UTC) + timedelta(seconds=lease_seconds))
        )
        session.commit()
        return renewed.rowcount == 1


def finish_hardening_budget(service: BudgetService, job: HardeningJob, username: str, operation_id: str | None) -> bool:
    """Close an ended job's budget hold.

    Args:
        service: The budget service.
        job: The job in its end state.
        username: The job's owner.
        operation_id: The stored hold, if any.

    Returns:
        Whether nothing stays open: True when there was no hold or it closed.
    """
    if operation_id is None or job.binding_id is None:
        return True
    try:
        HardeningBudget(service, username=username, budget_id=job.binding_id).finish(operation_id)
    except Exception:
        # The row keeps the id, so a later step can close the hold again.
        logger.exception("Hardening job %s: could not close its budget hold.", job.id)
        return False
    return True


def store_hardening_result(
    engine: Engine, job_id: str, result: HardeningJob, *, claim: str, operation_id: str | None
) -> bool:
    """Write a pass's result and drop the claim, only while this claim still holds it.

    An ended job closes its budget hold first. A job that waits for the user
    keeps its hold, but no lease.

    Args:
        engine: SQLAlchemy engine of the job store.
        job_id: The job.
        result: The job after the pass.
        claim: The claim token :func:`claim_hardening_job` returned.
        operation_id: The job's budget hold, if any.

    Returns:
        Whether the result was written.
    """
    with Session(engine) as session:
        row = session.scalars(select(HardeningJobModel).where(HardeningJobModel.id == job_id).with_for_update()).first()
        if row is None or row.claimed_by != claim:
            logger.warning("Hardening job %s: this pod lost its claim; the result is dropped.", job_id)
            return False
        if result.status not in WAITING_OR_QUEUED and finish_hardening_budget(
            BudgetService(engine=engine), result, row.username, operation_id
        ):
            operation_id = None
        row.job = result.model_dump(mode="json")
        row.status = result.status
        row.updated_at = datetime.now(UTC)
        row.claimed_by = None
        row.lease_expires_at = None
        row.budget_operation_id = operation_id
        session.commit()
        return True


def process_next_hardening_job(
    engine: Engine,
    *,
    pod: str,
    lease_seconds: float,
    local_mode: bool,
    settings: Settings,
    run: Callable[..., HardeningJob] = run_hardening_job,
    on_renew: Callable[[], None] = lambda: None,
) -> bool:
    """Claim one hardening job, run it until it waits or ends, and store the result.

    With no job to claim, the call closes the holds that earlier closes left
    open on ended jobs, at most once each :data:`HOLD_SWEEP_SECONDS`.

    Args:
        engine: SQLAlchemy engine of the job store.
        pod: This worker pod's name.
        lease_seconds: The claim's lease window.
        local_mode: Whether this worker runs jobs on the user's machine.
        settings: The deployment settings.
        run: The job driver; :func:`run_hardening_job` by default.
        on_renew: Called after each lease renewal, so the worker's liveness
            check sees a long pass as alive.

    Returns:
        Whether a job was claimed.
    """
    claimed = claim_hardening_job(
        engine, pod=pod, lease_seconds=lease_seconds, place="local" if local_mode else "cloud"
    )
    if claimed is None:
        _sweep_ended_holds(engine)
        return False
    job_id, username, job, operation_id, claim = claimed
    state: dict[str, Any] = {"operation": operation_id, "commit": None}
    vault = ProviderKeyVault(engine=engine)
    failure = _start_failure(job, settings, username=username, vault=vault)
    if failure is not None:
        result = _ended(job, failure)
    else:
        assert job.binding_id is not None
        budget = HardeningBudget(BudgetService(engine=engine), username=username, budget_id=job.binding_id)
        binding_id = job.binding_id
        lost = threading.Event()
        exhausted = threading.Event()

        def reserve(reserved_job: HardeningJob, hold: float) -> None:
            """Hold the job's dollars, reusing its open hold.

            Args:
                reserved_job: The job.
                hold: The dollars to hold.
            """
            state["operation"] = budget.reserve(reserved_job, hold, current=state["operation"])

        def runner_factory(workspace: HardeningWorkspace, factory_job: HardeningJob) -> Any:
            """Open the runner for one pass on the job's hold.

            Args:
                workspace: The pass's trees.
                factory_job: The job.

            Returns:
                The runner's context manager.
            """
            return open_hardening_runner(
                workspace,
                factory_job,
                service=budget.service,
                username=username,
                budget_id=binding_id,
                operation_id=state["operation"],
                settings=settings,
                claim=claim,
                vault=vault,
                lost=lost,
                exhausted=exhausted,
                on_commit=lambda commit: state.__setitem__("commit", commit),
            )

        stop = threading.Event()
        renewer = threading.Thread(
            target=_renew_until,
            args=(engine, job_id, claim, lease_seconds, stop, lost, on_renew),
            name=f"hardening-lease-{job_id}",
            daemon=True,
        )
        renewer.start()
        try:
            result = run(
                job,
                payload={"target": copy.deepcopy(job.target)},
                binding_id=binding_id,
                username=username,
                engine=engine,
                local_mode=local_mode,
                runner_factory=runner_factory,
                reserve=reserve,
            )
        except Exception as exc:
            # One bad job must not stop the worker loop.
            logger.warning("Hardening job %s: the pass raised %s.", job_id, type(exc).__name__)
            result = _ended(job, "hardening.runner_failed")
        finally:
            stop.set()
            renewer.join()
        if result.status == "failed" and exhausted.is_set():
            # The core round budget sees only ``job.spent_usd``, which misses
            # the metric's own model calls, so a spent hold surfaces as a raised
            # error. Running out of money is a clean end, not a failure.
            result = result.model_copy(update={"status": "budget_stop", "error": None})
        result = _pin_commit(result, state["commit"])
    store_hardening_result(engine, job_id, result, claim=claim, operation_id=state["operation"])
    return True


def _start_failure(job: HardeningJob, settings: Settings, *, username: str, vault: ProviderKeyVault) -> str | None:
    """Return the stable error key of a job that cannot start, before any paid work.

    The page shows these keys, so a known failure never surfaces as a Python
    type name.

    Args:
        job: The claimed job.
        settings: The deployment settings.
        username: The job's owner, whose saved keys a BYOK scorer uses.
        vault: The provider-key vault.

    Returns:
        The error key, or None when the job can start.
    """
    if job.scorer is None:
        return "hardening.no_scorer"
    if job.binding_id is None or job.target is None:
        return "hardening.no_target"
    if job.scorer.get("kind", "python") != "python" or not job.scorer.get("metric_code"):
        return "hardening.no_python_metric"
    if settings.openrouter_api_key is None:
        return "hardening.no_managed_models"
    scorer = {"scorer": copy.deepcopy(job.scorer)}
    if payload_uses_token_source(scorer, TOKEN_SOURCE_BYOK, default_token_source=TOKEN_SOURCE_MANAGED):
        try:
            inject_byok_connections(scorer, username=username, vault=vault, default_token_source=TOKEN_SOURCE_MANAGED)
        except ValueError:
            return "hardening.byok_missing"
        except DomainError as error:
            # No vault key, or a saved key the vault cannot decrypt: either
            # way the user must save the key again, so it is missing.
            if error.code in {"billing.byok_not_configured", "billing.byok_key_undecryptable"}:
                return "hardening.byok_missing"
            return "hardening.byok_not_served"
    return None


def close_ended_hardening_holds(engine: Engine, *, limit: int = HOLD_SWEEP_LIMIT) -> int:
    """Close the budget holds that stayed open on ended jobs.

    A hold stays on an ended row when its close failed. Nothing claims an
    ended job again, so without this sweep the hold would never close.

    Args:
        engine: SQLAlchemy engine of the job store.
        limit: The most rows to close in one call.

    Returns:
        How many holds closed.
    """
    with Session(engine) as session:
        rows = session.execute(
            select(
                HardeningJobModel.id,
                HardeningJobModel.username,
                HardeningJobModel.job,
                HardeningJobModel.budget_operation_id,
            )
            .where(
                HardeningJobModel.status.not_in(WAITING_OR_QUEUED),
                HardeningJobModel.budget_operation_id.is_not(None),
            )
            .order_by(HardeningJobModel.updated_at)
            .limit(limit)
        ).all()
    service = BudgetService(engine=engine)
    closed = 0
    for job_id, username, document, operation_id in rows:
        if not finish_hardening_budget(service, HardeningJob.model_validate(document), username, operation_id):
            continue
        with Session(engine) as session:
            cleared = session.execute(
                update(HardeningJobModel)
                .where(HardeningJobModel.id == job_id, HardeningJobModel.budget_operation_id == operation_id)
                .values(budget_operation_id=None)
            )
            session.commit()
        closed += cleared.rowcount
    return closed


def _sweep_ended_holds(engine: Engine) -> None:
    """Run :func:`close_ended_hardening_holds` at most once each :data:`HOLD_SWEEP_SECONDS`.

    Args:
        engine: SQLAlchemy engine of the job store.
    """
    global _last_sweep
    with _sweep_lock:
        now = time.monotonic()
        if _last_sweep and now - _last_sweep < HOLD_SWEEP_SECONDS:
            return
        _last_sweep = now
    try:
        close_ended_hardening_holds(engine)
    except Exception:
        # The next sweep tries again; one bad sweep must not stop the worker loop.
        logger.exception("Could not close the open holds of ended hardening jobs.")


def _renew_until(
    engine: Engine,
    job_id: str,
    claim: str,
    lease_seconds: float,
    stop: threading.Event,
    lost: threading.Event,
    on_renew: Callable[[], None],
) -> None:
    """Renew the lease every third of its window until the pass ends or the claim is lost.

    Args:
        engine: SQLAlchemy engine of the job store.
        job_id: The job.
        claim: The claim token.
        lease_seconds: The lease window.
        stop: Set when the pass ends.
        lost: Set here when another claim took the job.
        on_renew: Called after each successful renewal.
    """
    while not stop.wait(lease_seconds / 3):
        try:
            if not renew_hardening_lease(engine, job_id, claim=claim, lease_seconds=lease_seconds):
                logger.warning("Hardening job %s: another claim took the lease.", job_id)
                lost.set()
                return
            on_renew()
        except Exception:
            # A missed renewal only shortens the lease.
            logger.exception("Hardening job %s: the lease renewal failed.", job_id)


def _ended(job: HardeningJob, error: str) -> HardeningJob:
    """End a job as failed before any paid work.

    Args:
        job: The job.
        error: The error code.

    Returns:
        A changed copy of the job.
    """
    ended = job.model_copy(deep=True)
    ended.status = "failed"
    ended.error = error
    ended.reserved_usd = ended.spent_usd
    return ended


def _pin_commit(job: HardeningJob, commit: str | None) -> HardeningJob:
    """Pin later passes of a job to the commit its first pass staged.

    Args:
        job: The job after the pass.
        commit: The staged commit, if a pass reached its runner.

    Returns:
        The job; a copy with ``target.repo.commit`` set when it was unset.
    """
    repo = (job.target or {}).get("repo")
    if not commit or not isinstance(repo, dict) or repo.get("commit"):
        return job
    pinned = job.model_copy(deep=True)
    assert pinned.target is not None
    pinned.target["repo"]["commit"] = commit
    return pinned


def _tree_text(archive: Path) -> str:
    """Return the text files of a packed tree, cut at a size limit.

    Args:
        archive: The stripped tree.

    Returns:
        Each text file with its path, until the limit.
    """
    parts: list[str] = []
    size = 0
    with tarfile.open(archive) as tree:
        for member in tree:
            if not member.isreg():
                continue
            handle = tree.extractfile(member)
            if handle is None:
                continue
            raw = handle.read(MAX_FILE_CHARS + 1)
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            block = f"--- {member.name}\n{text[:MAX_FILE_CHARS]}\n"
            if size + len(block) > MAX_TREE_CHARS:
                parts.append("--- (the rest of the tree is cut)\n")
                break
            parts.append(block)
            size += len(block)
    return "".join(parts)


def _candidates(reply: str | None) -> list[Candidate]:
    """Read the attacker's candidates from its reply, skipping malformed ones.

    Args:
        reply: The reply text.

    Returns:
        The candidates; empty when the reply has no JSON object.
    """
    if not reply:
        return []
    match = re.search(r"\{.*\}", reply, re.DOTALL)
    if match is None:
        return []
    try:
        document = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    found: list[Candidate] = []
    for item in document.get("candidates") or [] if isinstance(document, dict) else []:
        if not isinstance(item, dict):
            continue
        kind, diff, why = item.get("kind"), item.get("patch_diff"), item.get("why_md")
        if kind in ("case", "method") and isinstance(diff, str) and isinstance(why, str):
            found.append(Candidate(kind=kind, patch_diff=diff, why_md=why))
    return found


def _diff_only(reply: str) -> str:
    """Return the git diff in a reply, without a Markdown fence around it.

    Args:
        reply: The reply text.

    Returns:
        The diff from its first ``diff --git`` line; the stripped reply when there is none.
    """
    start = reply.find("diff --git")
    text = reply[start:] if start >= 0 else reply.strip()
    return text.split("\n```", 1)[0].rstrip() + "\n" if text else ""
