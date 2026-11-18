"""The worker's actor tree: User, Workspace, Run and Agent, with chunked budget leases (SKY-717).

* :class:`User` owns the wallet (through :class:`BudgetService`) and its one workspace.
* :class:`Workspace` holds the policy and the budget. Waking it runs the lazy
  data migration (:func:`storage_version.ensure_current`) before anything else.
* :class:`Run` is the coordinator. It holds the population and the champion,
  commits them once per generation, and hands budget chunks down to its agents.
* :class:`Agent` is the single writer of its own step log.

The budget goes down the tree in chunks, so the budget row is not locked for
each admission. The run reserves a pool of equal slices with one
:meth:`BudgetService.reserve_lease` call. An agent takes one slice as its
lease and spends from it locally. It asks for the next slice only when the
lease cannot cover its next step, and it stops before paid work when no slice
is left. Each step settles its cumulative lease spend with the step id as the
evidence key. The ledger keeps one evidence row per (lease operation, key), so
a step id settles at most once on its lease. A replay on another lease is a
new key, and the ledger does not stop it.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any

from sqlalchemy.engine import Engine

from ..billing.budget_amounts import CENT_SCALE, MAX_CENTS, cent_units
from ..billing.budgets import (
    MAX_LEASE_SLICES,
    BudgetBoundExceededError,
    BudgetError,
    BudgetFundingLostError,
    BudgetInFlightError,
    BudgetInsufficientError,
    BudgetService,
    BudgetUnreconciledError,
    OperationSnapshot,
)
from ..storage import storage_version
from ..storage.step_log_store import (
    AGENT_ID_MAX,
    GRANT_LIST_ID_MAX,
    RUN_ID_MAX,
    SEQ_MAX,
    STEP_KINDS,
    StepLogStore,
)
from ..storage.workspace_store import WorkspaceRecord, WorkspaceStore

# The run commits its generations to its own step log under this agent id, so no agent may use it.
RUN_AGENT_ID = "run"
LEASE_COST_KIND = "agent_lease"
LEASE_PRICE_SNAPSHOT = {"version": "agent-lease-v1"}
# The evidence key of the final settle that closes a lease. A step id always holds a ``:``, so it never collides.
LEASE_CLOSE_KEY = "close"
# The ledger keeps nine fractional cent digits (``cent_units``); a finer spend makes every settle fail.
LEDGER_CENT_STEP = Decimal(1) / CENT_SCALE
# A settle admits work only while the budget is in one of these states. Any other state stops new paid work.
ADMITTING_BUDGET_STATES = frozenset({"open", "attached"})


class LeaseExhaustedError(RuntimeError):
    """The agent stops before paid work: no lease covers its next step, or the budget is blocked, closed or fenced.

    Blocked: a settle found bound_exceeded or funding_lost. Closed: a cancel or a reached limit (stop_admission).
    Fenced: a recovery moved the budget generation or the job's ``execution_generation`` past the run's own.
    """


def _check_id(name: str, value: Any, maximum: int) -> None:
    """Refuse an id that the step log refuses at commit, which comes only after the paid work.

    Args:
        name: The id's name, for the error.
        value: The id.
        maximum: The step log's length limit for the id.

    Raises:
        ValueError: The id is not a string, is empty or too long, or is not printable ASCII.
    """
    if not isinstance(value, str) or not 0 < len(value) <= maximum or not (value.isascii() and value.isprintable()):
        raise ValueError(f"{name} is not 1 to {maximum} printable ASCII characters: {value!r}")


def _check_count(name: str, value: Any, maximum: int) -> None:
    """Refuse a count that is not an int from 0 to the maximum, as the step log and the ledger do.

    Args:
        name: The count's name, for the error.
        value: The count.
        maximum: The largest count allowed.

    Raises:
        ValueError: The count is a bool, not an int, or outside 0 to ``maximum``.
    """
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"{name} is not an int from 0 to {maximum}: {value!r}")


def _ledger_cents(cost: Any) -> Decimal | None:
    """Turn a reported step cost into an amount the ledger can hold, rounded up to nine fractional digits.

    Args:
        cost: The cost that the work reported, as a Decimal, int, float or str.

    Returns:
        The amount, from ``-MAX_CENTS`` to ``MAX_CENTS``, or ``None`` when the cost is not a finite number.
    """
    try:
        amount = Decimal(str(cost))
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite():
        return None
    # The caps come before the rounding: rounding a very large amount to nine digits passes Decimal's precision.
    return max(min(amount, Decimal(MAX_CENTS)), -Decimal(MAX_CENTS)).quantize(LEDGER_CENT_STEP, rounding=ROUND_CEILING)


def _cost_text(cost: Decimal) -> str:
    """Write a ledger amount in plain notation, with no exponent, no trailing zeros and no negative zero.

    The text goes into the step body and the settle evidence, so the same cost always gives the same text.

    Args:
        cost: A ledger amount from :func:`_ledger_cents`.

    Returns:
        The text, for example ``0``, ``0.0000001`` or ``1.5``.
    """
    text = format(cost, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


@dataclass(frozen=True)
class StepResult:
    """What one step's paid work returns.

    Attributes:
        body: The JSON step body: the command and its output, or the model response.
        cost_cents: The actual cost of the work, at most the step's bound. The ledger rounds it up to nine
            fractional digits.
        tree_id: The new tree id after a command; ``None`` for a model step.
    """

    body: dict[str, Any]
    cost_cents: Decimal | int | float | str
    tree_id: str | None = None


@dataclass
class _Lease:
    """One budget slice that an agent holds, and what the agent spent from it."""

    operation_id: str
    max_cents: Decimal
    spent_cents: Decimal = Decimal(0)
    # Set before the first final settle. The settle can commit and lose its reply, so the lease takes no more
    # spend: a retry replays the same final settle with the same spend.
    closing: bool = False


class User:
    """The top actor: one account, its wallet and its one workspace."""

    def __init__(self, username: str, *, engine: Engine, budgets: BudgetService) -> None:
        """Bind the user to the database and the ledger that holds the wallet.

        Args:
            username: The authenticated account.
            engine: Engine whose schema carries ``workspaces``.
            budgets: The ledger that owns the account wallet and its budgets.
        """
        self.username = username
        self.budgets = budgets
        self.engine = engine
        self._store = WorkspaceStore(engine)

    def workspace(self) -> Workspace:
        """Create the user's workspace when it does not exist, then wake it.

        Returns:
            The awake workspace.
        """
        record = self._store.get_or_create(self.username)
        return Workspace.wake(self, record.id)


class Workspace:
    """One user's workspace: its policy and the budget that its runs spend from."""

    def __init__(self, user: User, record: WorkspaceRecord) -> None:
        """Bind the workspace to its owner and its row.

        Use :meth:`wake`; it runs the lazy migration first.

        Args:
            user: The owner.
            record: The workspace row after the migration.
        """
        self.user = user
        self.id = record.id
        self.policy = record.policy
        self.budget_id = record.execution_budget_id

    @classmethod
    def wake(cls, user: User, workspace_id: str) -> Workspace:
        """Check the owner, upgrade the workspace's data to the current storage version, then load it.

        Args:
            user: The owner.
            workspace_id: The workspace.

        Returns:
            The awake workspace.

        Raises:
            PermissionError: The workspace belongs to another user; its data is not touched.
        """
        store = WorkspaceStore(user.engine)
        # Only the owner may start the upgrade, because the upgrade writes. The owner column has no old format.
        if store.get(workspace_id).username != user.username:
            raise PermissionError(f"workspace {workspace_id} belongs to another user")
        # The migration comes before any other read: no read or write may see data in an old format (SKY-713).
        storage_version.ensure_current(user.engine, workspace_id)
        return cls(user, store.get(workspace_id))

    def start_run(
        self,
        run_id: str,
        *,
        step_log: StepLogStore,
        execution_generation: int,
        budget_generation: int,
        grant_list_id: str,
        chunk_cents: Decimal | int | str,
        lease_slices: int,
        next_generation: int = 0,
    ) -> Run:
        """Start the coordinator of one run that spends from this workspace's budget.

        Args:
            run_id: The job id.
            step_log: This worker's step log.
            execution_generation: The job's current ``execution_generation``, the step log fence.
            budget_generation: The budget's current generation, the ledger fence.
            grant_list_id: The grant list that the run's steps run with.
            chunk_cents: The size of one agent lease.
            lease_slices: How many leases the run reserves with one ledger call.
            next_generation: The number of the run's next generation; above 0 when it resumes.

        Returns:
            The run.

        Raises:
            ValueError: :class:`Run` refuses an id, a generation, the chunk size or the slice count.
            LookupError: The workspace has no budget.
        """
        return Run(
            self,
            run_id,
            step_log=step_log,
            execution_generation=execution_generation,
            budget_generation=budget_generation,
            grant_list_id=grant_list_id,
            chunk_cents=Decimal(str(chunk_cents)),
            lease_slices=lease_slices,
            next_generation=next_generation,
        )


class Run:
    """The coordinator of one run: its population, its champion, its agents and its lease pool."""

    def __init__(
        self,
        workspace: Workspace,
        run_id: str,
        *,
        step_log: StepLogStore,
        execution_generation: int,
        budget_generation: int,
        grant_list_id: str,
        chunk_cents: Decimal,
        lease_slices: int,
        next_generation: int = 0,
    ) -> None:
        """Bind the run to its workspace, its step log and its fences.

        Args:
            workspace: The workspace whose budget the run spends.
            run_id: The job id.
            step_log: This worker's step log.
            execution_generation: The job's current ``execution_generation``.
            budget_generation: The budget's current generation.
            grant_list_id: The grant list that the run's steps run with.
            chunk_cents: The size of one agent lease; above 0.
            lease_slices: How many leases one ledger call reserves; at least 1.
            next_generation: The number of the run's next generation; above 0 when it resumes.

        Raises:
            ValueError: The chunk size is not above 0 or is not an amount the ledger can hold exactly (more than
                nine fractional digits, or above ``MAX_CENTS``), the slice count is not an int from 1 to
                ``MAX_LEASE_SLICES``, or an id, a generation or the next generation is one that the step log or
                the ledger refuses.
            LookupError: The workspace has no budget.
        """
        # The ledger and the step log refuse these with a plain ValueError, the step log only after the paid
        # work, so a bad run fails here and not at a step. With a run id of at most RUN_ID_MAX characters,
        # every lease, slice and step key fits the ledger's 128-character key limit.
        _check_id("run_id", run_id, RUN_ID_MAX)
        if ":" in run_id:
            raise ValueError(f"run_id must not hold a ':': {run_id!r}")
        _check_id("grant_list_id", grant_list_id, GRANT_LIST_ID_MAX)
        # The job's execution_generation is an Integer column, so a larger value never matches it, and the
        # step log fences every step after its paid work.
        _check_count("execution_generation", execution_generation, 2**31 - 1)
        _check_count("budget_generation", budget_generation, 2**63 - 1)
        _check_count("next_generation", next_generation, SEQ_MAX)
        cent_units(chunk_cents)
        if type(lease_slices) is not int or chunk_cents <= 0 or not 1 <= lease_slices <= MAX_LEASE_SLICES:
            raise ValueError(f"bad chunk size or slice count: {chunk_cents!r}, {lease_slices!r}")
        budget_id = workspace.budget_id
        if budget_id is None:
            raise LookupError(f"workspace {workspace.id} has no budget")
        self.budget_id: str = budget_id
        self.workspace = workspace
        self.run_id = run_id
        self.step_log = step_log
        self.execution_generation = execution_generation
        self.budget_generation = budget_generation
        self.grant_list_id = grant_list_id
        self.chunk_cents = chunk_cents
        self.population: list[Any] = []
        self.champion: Any = None
        self.generation = next_generation
        self._lease_slices = lease_slices
        self._lock = threading.Lock()
        self._pool: list[OperationSnapshot] = []
        # Slices whose dispatch failed and whose state is unknown. They stay out of the pool, so no agent
        # takes them again, and the run close resolves each one.
        self._returned: list[OperationSnapshot] = []
        self._leases = 0
        # The key and slice count of a reservation with an unknown result. The next try replays them, so a
        # lease that committed but lost its reply comes back to this run and is not left reserved.
        self._retry: tuple[str, int] | None = None
        # The lease counter starts at 0 in each instance. Without the nonce, a restarted run replays the old
        # lease keys, and reserve_lease gives back slices that the old instance's agents already hold.
        self._nonce = uuid.uuid4().hex
        self._closed = False
        # Set when a settle or the pre-step read finds the budget blocked, closed or fenced. The ledger then refuses
        # new dispatches, but an agent that holds a lease with room does not dispatch again, so this flag stops its
        # paid work.
        self._blocked = False
        self._agents: dict[str, Agent] = {}

    @property
    def closed(self) -> bool:
        """Whether :meth:`close` ran; a closed run starts no new step.

        Returns:
            ``True`` once :meth:`close` started, also when it raised.
        """
        with self._lock:
            return self._closed

    def spawn(self, agent_id: str, *, next_seq: int = 0) -> Agent:
        """Start one agent, the only writer of its step log.

        Args:
            agent_id: The agent's id, unique in the run.
            next_seq: The ``seq`` of the agent's next step; above 0 when it resumes.

        Returns:
            The agent.

        Raises:
            ValueError: Another agent of the run has the id, the id is the run's own, or the step log refuses the
                id or ``next_seq`` (it does so only at commit, after the paid work).
            RuntimeError: The run is closed.
        """
        _check_id("agent_id", agent_id, AGENT_ID_MAX)
        _check_count("next_seq", next_seq, SEQ_MAX)
        with self._lock:
            if self._closed:
                raise RuntimeError(f"run {self.run_id} is closed")
            if agent_id == RUN_AGENT_ID or agent_id in self._agents:
                raise ValueError(f"agent id {agent_id!r} is taken in run {self.run_id}")
            agent = Agent(self, agent_id, next_seq=next_seq)
            self._agents[agent_id] = agent
        return agent

    def take_chunk(self) -> OperationSnapshot | None:
        """Hand one budget slice to an agent, and reserve a new pool when the pool is empty.

        When the full pool does not fit the budget, a pool of half the size is
        tried, down to one slice. When a reservation fails with an error that is
        not a ledger refusal or a ``ValueError``, its result is unknown: the next call replays the
        same lease key and slice count, and the ledger gives back the same slices
        if the reservation committed.

        Returns:
            A reserved slice, or ``None`` when the run is closed, the budget is stopped or the budget cannot give
            one now.
        """
        with self._lock:
            if self._closed or self._blocked:
                return None
            slices = self._lease_slices
            while not self._pool and slices >= 1:
                if self._retry is None:
                    # A new key for each refused try: a lease key replayed with another slice count is a conflict.
                    self._retry = (
                        f"{self.run_id}:g{self.budget_generation}:{self._nonce}:lease-{self._leases}",
                        slices,
                    )
                    self._leases += 1
                lease_key, slices = self._retry
                try:
                    self._pool = self._reserve(lease_key, slices)
                except ValueError as error:
                    # A ledger refusal (BudgetError is a ValueError) commits nothing, so the next try may use a new
                    # key. A plain ValueError is bad input that the run's own checks missed: reserve_lease
                    # checks the input before its transaction, and any error in the transaction rolls it back. So
                    # nothing committed either, and a replay would only raise again on every close.
                    self._retry = None
                    # BudgetUnreconciledError is a BudgetInFlightError, but a smaller pool cannot fix it.
                    if isinstance(error, BudgetUnreconciledError) or not isinstance(
                        error, (BudgetInsufficientError, BudgetInFlightError)
                    ):
                        raise
                    slices //= 2
                else:
                    self._retry = None
            return self._pool.pop() if self._pool else None

    def _reserve(self, lease_key: str, slices: int) -> list[OperationSnapshot]:
        """Reserve a pool of slices, or replay the reservation with the same key and slice count.

        Args:
            lease_key: The lease key.
            slices: The number of slices.

        Returns:
            The slices, newly reserved or replayed.
        """
        return self.workspace.user.budgets.reserve_lease(
            self.budget_id,
            self.workspace.user.username,
            lease_key=lease_key,
            slices=slices,
            generation=self.budget_generation,
            phase="run",
            cost_kind=LEASE_COST_KIND,
            request_fingerprint=lease_key,
            price_snapshot=LEASE_PRICE_SNAPSHOT,
            max_cents=self.chunk_cents,
        )

    def end_generation(self, population: Sequence[Any], champion: Any) -> str:
        """Commit the generation's population and champion: the run's one commit for each generation.

        Args:
            population: The generation's candidates, as JSON values.
            champion: The best candidate so far, as a JSON value.

        Returns:
            The step id of the commit.
        """
        self.population = list(population)
        self.champion = champion
        step_id = self.step_log.commit(
            run_id=self.run_id,
            agent_id=RUN_AGENT_ID,
            seq=self.generation,
            kind="model",
            execution_generation=self.execution_generation,
            grant_list_id=self.grant_list_id,
            body={"generation": self.generation, "population": self.population, "champion": self.champion},
        )
        self.generation += 1
        return step_id

    def close(self) -> None:
        """Stop the run, close every agent's lease and return the pool's untaken slices to the budget.

        Every agent gets its close, and the pool is released, even when one close
        fails. A failed agent keeps its lease, so a later call can close it again.
        An agent whose lease spend passed the lease's bound keeps its lease too:
        the ledger blocks the budget with ``bound_exceeded``, and every close
        raises :class:`BudgetBoundExceededError` until a manual reconciliation.
        A reservation with an unknown result is replayed, so its slices are released
        too. A slice whose dispatch failed is released, or final-settled at 0 when
        it dispatched. When one of these ledger calls fails, the run keeps what it
        could not return, so a later close can return it.

        Raises:
            Exception: The first error of an agent close, after all closes ran. With
                no agent error, the first error of returning the unused slices.
        """
        with self._lock:
            self._closed = True
            agents = list(self._agents.values())
        first_error: Exception | None = None
        try:
            for agent in agents:
                try:
                    agent.close()
                except Exception as error:
                    first_error = first_error or error
        finally:
            # The unused slices are taken only after every agent close: a close waits for that agent's step,
            # and a failed step can return its slice. A new step sees ``_closed`` and takes nothing.
            return_error = self._return_unused()
            first_error = first_error or return_error
        if first_error is not None:
            raise first_error

    def _return_unused(self) -> Exception | None:
        """Give every slice that no agent holds back to the budget, and keep the ones that fail.

        Returns:
            The first error, or ``None`` when all slices went back.
        """
        budgets = self.workspace.user.budgets
        username = self.workspace.user.username
        first_error: Exception | None = None
        with self._lock:
            retry, pool, returned = self._retry, self._pool, self._returned
            self._pool, self._returned = [], []
        if retry is not None:
            try:
                pool = [*pool, *self._reserve(*retry)]
            except BudgetError:
                # The replay checks only the fence before it finds the slices, and the fence released them.
                with self._lock:
                    self._retry = None
            except Exception as error:
                first_error = error
            else:
                with self._lock:
                    self._retry = None
        if pool:
            try:
                budgets.release_slices(self.budget_id, username, [chunk.id for chunk in pool])
            except Exception as error:
                with self._lock:
                    self._pool.extend(pool)
                first_error = first_error or error
        for chunk in returned:
            try:
                try:
                    budgets.release(chunk.id, username)
                except BudgetUnreconciledError:
                    # The dispatch committed, but no agent did work on the slice, so its real spend is 0.
                    budgets.settle(
                        chunk.id,
                        username,
                        evidence_key=LEASE_CLOSE_KEY,
                        actual_cents=0,
                        evidence={"closed": True, "unused": True},
                        final=True,
                    )
            except Exception as error:
                with self._lock:
                    self._returned.append(chunk)
                first_error = first_error or error
        return first_error

    def _return_chunk(self, chunk: OperationSnapshot) -> None:
        """Keep a slice whose dispatch failed with an unknown state, so the close resolves it.

        Args:
            chunk: The slice.
        """
        with self._lock:
            self._returned.append(chunk)

    def _block(self) -> None:
        """Stop all new paid work of the run, because a settle or the pre-step read found the budget stopped."""
        with self._lock:
            self._blocked = True

    def _budget_admits_work(self) -> bool:
        """Read the budget and the job, and stop the run's paid work when they no longer admit it.

        A recovery fences this run (``fence_generation``) and moves the job to a new
        ``execution_generation``, but keeps the budget ``attached``. So the state
        alone does not stop a fenced run: an agent that still holds a lease would
        do paid work that the ledger bills and the step log drops at flush. A
        generation only goes up, so a mismatch stops the run for good.

        Returns:
            ``True`` when the budget admits work and both generations are still the run's own.
        """
        admission = WorkspaceStore(self.workspace.user.engine).budget_admission(
            self.budget_id, self.workspace.user.username, self.run_id
        )
        if (
            admission.state in ADMITTING_BUDGET_STATES
            and admission.generation == self.budget_generation
            and admission.execution_generation == self.execution_generation
        ):
            return True
        self._block()
        return False

    def _is_blocked(self) -> bool:
        """Tell whether a settle or the pre-step read found the budget stopped, closed or fenced.

        Returns:
            ``True`` when the run's agents may start no more paid work.
        """
        with self._lock:
            return self._blocked


class Agent:
    """One agent: the only writer of its step log, spending from its own lease."""

    def __init__(self, run: Run, agent_id: str, *, next_seq: int = 0) -> None:
        """Bind the agent to its run.

        Use :meth:`Run.spawn`; it keeps agent ids unique in the run.

        Args:
            run: The coordinator.
            agent_id: The agent's id.
            next_seq: The ``seq`` of the agent's next step.
        """
        self.run = run
        self.agent_id = agent_id
        self.next_seq = next_seq
        self._lease: _Lease | None = None
        # Held by ``step`` and ``close``, so a close waits for an in-flight step to settle.
        self._lock = threading.Lock()

    def step(self, kind: str, max_cents: Decimal | int | str, work: Callable[[], StepResult]) -> str:
        """Run one paid step: check the lease, do the work, commit the step, then settle it.

        A crash between the commit and the settle leaves the step unsettled and
        its slice dispatched. Only usage reconciliation of that slice recovers it.

        Args:
            kind: ``command`` or ``model``.
            max_cents: The most that the work can cost; the lease must cover it before the work starts.
            work: The paid work. It must catch its own failures and return a :class:`StepResult` with the
                real cost. An exception from ``work`` reaches the caller, and that call is billed nothing.

        Returns:
            The step id.

        Raises:
            ValueError: The kind is not a step kind, the agent's next seq is past ``SEQ_MAX``, or the bound is
                negative, above one lease, or not an amount the ledger can hold exactly (more than nine fractional
                digits, not finite, or above ``MAX_CENTS``); the work did not start. Or the work's cost is outside
                the bound, and a bad cost is refused before the commit. Or ``reserve_lease`` refused the run's
                lease input when a new lease was needed; the work did not start. That refusal comes from the
                run's configuration, not from this step, so it repeats on every later step of the run.
            RuntimeError: The run is closed; the work did not start.
            LeaseExhaustedError: No lease covers the bound, or the budget is blocked (bound_exceeded or
                funding_lost) or closed (a cancel or a reached limit), or a recovery moved the budget generation or
                the job's ``execution_generation`` past the run's own; the work did not start.
            BudgetBoundExceededError: A settle found a spend above its lease bound, and the ledger blocked the
                budget. From the final settle of the old lease, before a new lease, the work did not start.
                From the settle after the commit, the work ran and its step is committed.
            BudgetFundingLostError: A settle found that the wallet cannot pay, and the ledger blocked the
                budget. The two cases are the same as for :class:`BudgetBoundExceededError`.
            BudgetConflictError: The ledger refused to reserve or dispatch a new lease slice, for example a
                :class:`BudgetFencedError` for an obsolete budget generation; the work did not start.
            BudgetUnreconciledError: ``reserve_lease`` found previous-generation paid work that is not
                reconciled; the work did not start.
            LookupError: The budget admission read found no budget for the run's budget id and owner; the work did
                not start.
            BudgetNotFoundError: The budget went away after the state read and before ``reserve_lease`` or a
                settle; the work did not start.
        """
        # The step log refuses these only at commit, after the paid work, so they are refused here first.
        if kind not in STEP_KINDS:
            raise ValueError(f"unknown step kind: {kind!r}")
        # The bound is not rounded like a cost: a bound the ledger cannot hold exactly is refused here, so a cost
        # equal to the bound always fits it.
        cent_units(max_cents)
        bound = Decimal(str(max_cents))
        if bound < 0 or bound > self.run.chunk_cents:
            raise ValueError(f"step bound {bound} is negative or above one lease of {self.run.chunk_cents}")
        with self._lock:
            # A step that waited for the lock sees the seq that the step before it used up, so the check is here.
            _check_count("next_seq", self.next_seq, SEQ_MAX)
            # An agent whose final settle failed still holds its lease, so the lease alone cannot stop it.
            if self.run._closed:
                raise RuntimeError(f"run {self.run.run_id} is closed")
            # A cancel or a reached limit closes the budget (stop_admission), and a recovery fences the run's
            # generations, all with no settle, so only a read sees them before the work. The flag stays set
            # after a resume_admission, so a run that was stopped once never starts paid work again.
            self._check_admission()
            lease = self._covering_lease(bound)
            # Getting the lease can settle, reserve and dispatch, and a stop or a recovery can come in between.
            # The agent keeps the lease, so its close settles what the lease spent.
            self._check_admission()
            result = work()
            # The work is paid once it returns, so its cost counts before any check or commit can fail, and
            # the next settle bills it. A cost above the bound counts in full: when it passes the lease, the
            # ledger records bound_exceeded and blocks the budget, instead of hiding the overrun. A negative
            # cost counts 0, and a cost that is not a finite number counts the full bound. The spend stays an
            # amount the ledger can hold, so the lease can always settle and close.
            cost = _ledger_cents(result.cost_cents)
            counted = bound if cost is None else max(cost, Decimal(0))
            lease.spent_cents = min(lease.spent_cents + counted, Decimal(MAX_CENTS))
            if cost is None or not 0 <= cost <= bound:
                raise ValueError(f"step cost {result.cost_cents} is outside its bound of {bound}")
            step_id = self.run.step_log.commit(
                run_id=self.run.run_id,
                agent_id=self.agent_id,
                seq=self.next_seq,
                kind=kind,
                execution_generation=self.run.execution_generation,
                grant_list_id=self.run.grant_list_id,
                body={**result.body, "cost_cents": _cost_text(cost)},
                tree_id=result.tree_id,
            )
            self.next_seq += 1
            # The settle does not wait for ``durable``. Evidence is unique per (lease operation, step id): a
            # replay on this lease with the same spend is a no-op, and with another spend it is a conflict.
            # A replay on another lease is a new key, so the ledger does not stop it.
            self._settle(
                lease,
                evidence_key=step_id,
                evidence={"step_id": step_id, "cost_cents": _cost_text(cost)},
                final=False,
            )
            return step_id

    def commit_unpaid(
        self, kind: str, grant_list_id: str, work: Callable[[], tuple[dict[str, Any], str | None]]
    ) -> str:
        """Commit one step that no lease pays for, such as a read or the record of a failed box step.

        Args:
            kind: ``command`` or ``model``.
            grant_list_id: The grant list that the step names.
            work: Gives the step body and its tree id; it runs only after the seq check.

        Returns:
            The step id.

        Raises:
            ValueError: The agent's next seq is past ``SEQ_MAX``; the work did not start.
        """
        # The same lock as ``step``, so the two paths cannot take one seq, and the seq check comes before the work.
        with self._lock:
            _check_count("next_seq", self.next_seq, SEQ_MAX)
            body, tree_id = work()
            step_id = self.run.step_log.commit(
                run_id=self.run.run_id,
                agent_id=self.agent_id,
                seq=self.next_seq,
                kind=kind,
                execution_generation=self.run.execution_generation,
                grant_list_id=grant_list_id,
                body=body,
                tree_id=tree_id,
            )
            self.next_seq += 1
            return step_id

    def _check_admission(self) -> None:
        """Refuse paid work when the run is stopped, or when the budget or the job no longer admits it.

        Raises:
            LeaseExhaustedError: The run is blocked, or the budget is closed, blocked or fenced.
        """
        if self.run._is_blocked() or not self.run._budget_admits_work():
            raise LeaseExhaustedError(f"the budget of run {self.run.run_id} is blocked, closed or fenced")

    def _settle(self, lease: _Lease, *, evidence_key: str, evidence: dict[str, Any], final: bool) -> None:
        """Settle the lease's cumulative spend, and stop the run's paid work when the budget stops admitting work.

        Args:
            lease: The lease to settle.
            evidence_key: The evidence identity on the lease.
            evidence: The usage evidence.
            final: Whether the lease is done, so its unspent coverage goes back to the budget.

        Raises:
            BudgetBoundExceededError: The spend passed the lease bound; the ledger blocked the budget.
            BudgetFundingLostError: The wallet cannot pay the spend; the ledger blocked the budget.
        """
        try:
            snapshot = self.run.workspace.user.budgets.settle(
                lease.operation_id,
                self.run.workspace.user.username,
                evidence_key=evidence_key,
                actual_cents=lease.spent_cents,
                evidence=evidence,
                final=final,
            )
        except (BudgetBoundExceededError, BudgetFundingLostError):
            self.run._block()
            raise
        # Another agent's settle can block the budget, and a cancel or a reached limit closes it (stop_admission).
        # This snapshot shows both, so this agent stops too, even with coverage left on its lease.
        if snapshot.budget.state not in ADMITTING_BUDGET_STATES:
            self.run._block()

    def _covering_lease(self, bound: Decimal) -> _Lease:
        """Return a lease with room for the bound; close a low lease and take a new chunk first.

        Args:
            bound: The most that the next step can cost.

        Returns:
            The lease to spend from.

        Raises:
            LeaseExhaustedError: The run cannot give a new chunk.
        """
        lease = self._lease
        if lease is not None and not lease.closing and lease.max_cents - lease.spent_cents >= bound:
            return lease
        self._settle_lease()
        budgets = self.run.workspace.user.budgets
        username = self.run.workspace.user.username
        chunk = self.run.take_chunk()
        if chunk is None:
            raise LeaseExhaustedError(f"agent {self.agent_id} has no budget lease left")
        try:
            budgets.mark_dispatched(chunk.id, username)
        except Exception:
            try:
                # When the slice never dispatched, its coverage can go back to the budget at once.
                budgets.release(chunk.id, username)
            except BudgetUnreconciledError:
                # The dispatch committed and only its reply was lost. The agent holds the slice, so its
                # close final-settles it at its real spend.
                self._lease = _Lease(chunk.id, chunk.max_cents)
            except Exception:
                # The slice state is unknown. The run close releases it, or final-settles it at 0 if it
                # dispatched.
                self.run._return_chunk(chunk)
            raise
        # The run hands each slice to one taker only, and never hands out a returned slice. So a slice that
        # is already dispatched (``dispatch_claimed`` false) can only be this agent's own: it is adopted, and
        # its close final-settles it at its real spend.
        self._lease = _Lease(chunk.id, chunk.max_cents)
        return self._lease

    def close(self) -> None:
        """Settle the lease for what it spent and return the rest to the budget; wait for an in-flight step."""
        with self._lock:
            self._settle_lease()

    def _settle_lease(self) -> None:
        """Final-settle the held lease, and drop it only when the settle succeeds, so a failed close can retry."""
        lease = self._lease
        if lease is None:
            return
        lease.closing = True
        self._settle(lease, evidence_key=LEASE_CLOSE_KEY, evidence={"closed": True}, final=True)
        self._lease = None
