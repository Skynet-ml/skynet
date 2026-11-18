"""Tests for the warm box lease, its start budget and the metric box per score (SKY-732).

The lease tests use fake boxes. The tree sandbox tests use local subprocess
boxes, so no test calls a paid provider or the network.
"""

from __future__ import annotations

import contextlib
import contextvars
import tempfile
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from core.billing.budgets import BudgetError
from core.billing.signals import BudgetReached
from core.sandbox.box_lease import BoxLease, StartBudget
from core.sandbox.errors import SandboxInfraError
from core.sandbox.grants import Grant, GrantError
from core.sandbox.placement import MODAL, VERCEL, Placement
from core.sandbox.trees import BoxFailureError, GrantDeniedError
from core.service_gateway.optimization.blackbox.sandbox import LocalSubprocessRuntime, SandboxSpec
from core.service_gateway.optimization.blackbox.sandbox_contract import CommandResult, SandboxSession
from core.service_gateway.optimization.blackbox.tree_sandbox import RuntimeBoxLease, StepRef, TreeSandbox
from core.storage.models import Base, JobModel
from core.storage.object_store import LocalObjectStore
from core.storage.step_log_store import StepLogStore
from core.storage.tree_store import TreeEntry, TreeStore

RUN = "job-ada"
_SPEC = SandboxSpec(lifetime_seconds=60, operation_key="op")
# Test limits only: no test reaches them unless it sets its own.
_LIMITS: dict[str, Any] = {"vercel_cap": 100, "starts_per_minute": 1000}
_REAPER = "box-lease-reaper"
# Each lease and ticker of the running test, for the autouse teardown to close.
_LEASES: list[BoxLease[Any]] = []
_TICKERS: list[_Ticker] = []
# Stands for the settlement's context scopes, such as settled_charges and the usage scope.
_RUN_SCOPE: contextvars.ContextVar[str] = contextvars.ContextVar("run_scope")


@pytest.fixture(autouse=True)
def _close_every_lease(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Close every lease that the test built, and fail when a reaper thread outlives the test.

    A healthy release starts a reaper thread that runs while an idle box is
    left, and many tests never close their lease or let the box sleep.
    Without this, each such test leaves a reaper thread alive for up to the
    idle time.

    Args:
        monkeypatch: Records each lease, also a RuntimeBoxLease or one built
            without the provider helper.

    Yields:
        Nothing; the teardown closes the leases and checks the threads.
    """
    before = {thread for thread in threading.enumerate() if thread.name == _REAPER}
    build = BoxLease.__init__

    def record(self: BoxLease[Any], *args: Any, **kwargs: Any) -> None:
        """Build the lease, then record it.

        Args:
            self: The new lease.
            *args: The lease's arguments.
            **kwargs: The lease's options.
        """
        build(self, *args, **kwargs)
        _LEASES.append(self)

    monkeypatch.setattr(BoxLease, "__init__", record)
    try:
        yield
    finally:
        # Freed first, because close() joins a reaper that blocks in its ticker.
        for ticker in _TICKERS:
            ticker.free()
        for lease in _LEASES:
            # The test already checked the errors it cares about; the teardown only stops the reaper.
            with contextlib.suppress(BaseException):
                lease.close()
        _LEASES.clear()
        _TICKERS.clear()
    left = [thread for thread in threading.enumerate() if thread.name == _REAPER and thread not in before]
    for thread in left:
        thread.join(timeout=5)
    assert not [thread for thread in left if thread.is_alive()], "A box lease reaper thread outlived the test."


@dataclass(eq=False)
class _Box:
    """Stand for one provider box."""

    placement: Placement
    grants: tuple[Grant, ...]
    closed: bool = False


@dataclass
class _Clock:
    """Give a fake monotonic time that only the fake sleep moves."""

    now: float = 0.0
    sleeps: list[float] = field(default_factory=list)

    def __call__(self) -> float:
        """Return the fake time."""
        return self.now

    def sleep(self, seconds: float) -> None:
        """Move the fake time forward.

        Args:
            seconds: The wait.
        """
        self.sleeps.append(seconds)
        self.now += seconds


@dataclass
class _Provider:
    """Open fake boxes and record each open, its time and each close."""

    clock: _Clock = field(default_factory=_Clock)
    opened: list[_Box] = field(default_factory=list)
    start_times: list[float] = field(default_factory=list)
    closed: list[_Box] = field(default_factory=list)
    fail_opens: int = 0

    def open(self, placement: Placement, grants: Sequence[Grant]) -> _Box:
        """Open a fake box.

        Args:
            placement: The box's provider.
            grants: The box's grants.

        Returns:
            The box.

        Raises:
            SandboxInfraError: For the first ``fail_opens`` opens.
        """
        if self.fail_opens:
            self.fail_opens -= 1
            raise SandboxInfraError("The provider is down.")
        box = _Box(placement, tuple(grants))
        self.opened.append(box)
        self.start_times.append(self.clock.now)
        return box

    def close(self, box: _Box) -> None:
        """Close a fake box.

        Args:
            box: The box.
        """
        box.closed = True
        self.closed.append(box)

    def lease(self, **options: Any) -> BoxLease[_Box]:
        """Return a lease over this provider with the fake clock.

        Args:
            **options: Extra lease options.

        Returns:
            The lease.
        """
        return BoxLease(self.open, self.close, **{"clock": self.clock, "sleep": self.clock.sleep, **_LIMITS, **options})


def _acquire_within(lease: BoxLease[_Box], agent_id: str, timeout: float = 5.0) -> _Box:
    """Acquire in a daemon thread, and fail the test when the acquire hangs.

    A reservation that a failure leaves behind makes the next acquire for its
    key wait forever, so this turns that hang into a test failure.

    Args:
        lease: The lease.
        agent_id: The agent.
        timeout: The longest wait in seconds.

    Returns:
        The box.

    Raises:
        BaseException: The acquire's own error.
    """
    outcome: list[_Box | BaseException] = []

    def run() -> None:
        """Record the acquire's box or error."""
        try:
            outcome.append(lease.acquire(agent_id, []))
        except BaseException as error:
            outcome.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout=timeout)
    assert not thread.is_alive(), f"The acquire for {agent_id} hung."
    if isinstance(outcome[0], BaseException):
        raise outcome[0]
    return outcome[0]


def test_an_agent_gets_its_warm_box_back() -> None:
    """A healthy release keeps the box, so the agent's next step opens no box."""
    provider = _Provider()
    lease = provider.lease()

    box = lease.acquire("agent-1", [Grant("pypi.org")])
    lease.release(box, healthy=True)

    assert lease.acquire("agent-1", [Grant("pypi.org")]) is box
    assert provider.opened == [box]
    assert box.placement == Placement(VERCEL)


def test_each_agent_has_its_own_box() -> None:
    """Two agents never share a box."""
    provider = _Provider()
    lease = provider.lease()

    first = lease.acquire("agent-1", [])
    second = lease.acquire("agent-2", [])

    assert first is not second
    assert len(provider.opened) == 2


def test_an_unhealthy_release_drops_the_box() -> None:
    """After a failed step the box closes, and the next acquire opens a new box."""
    provider = _Provider()
    lease = provider.lease()
    box = lease.acquire("agent-1", [])

    lease.release(box, healthy=False)
    again = lease.acquire("agent-1", [])

    assert box.closed
    assert again is not box


def test_a_warm_box_that_fails_its_check_is_replaced() -> None:
    """A warm box that fails health closes, and the agent gets a new box."""
    provider = _Provider()
    dead: set[int] = set()
    lease = provider.lease(check=lambda box: id(box) not in dead)
    box = lease.acquire("agent-1", [])
    lease.release(box, healthy=True)
    dead.add(id(box))

    again = lease.acquire("agent-1", [])

    assert again is not box
    assert provider.closed == [box]


def test_a_check_that_raises_drops_the_box() -> None:
    """A check error closes the warm box and reaches the caller; the next acquire opens a new box."""
    provider = _Provider()
    calls: list[_Box] = []

    def check(box: _Box) -> bool:
        """Raise on the first check.

        Args:
            box: The warm box.

        Returns:
            True after the first check.

        Raises:
            RuntimeError: On the first check.
        """
        calls.append(box)
        if len(calls) == 1:
            raise RuntimeError("check failed")
        return True

    lease = provider.lease(check=check)
    box = lease.acquire("agent-1", [])
    lease.release(box, healthy=True)

    with pytest.raises(RuntimeError, match="check failed"):
        lease.acquire("agent-1", [])

    assert box.closed
    assert lease.acquire("agent-1", []) is not box


@pytest.mark.parametrize(
    ("check_error", "close_error", "raised"),
    [
        (BudgetReached("check"), RuntimeError("close"), "check"),
        (RuntimeError("check"), RuntimeError("close"), "check"),
        (RuntimeError("check"), BudgetReached("close"), "close"),
    ],
)
def test_a_close_error_after_a_check_error_hides_it_only_when_it_is_a_stop(
    check_error: BaseException, close_error: BaseException, raised: str
) -> None:
    """A check error reaches the caller over the close error of the dropped box, except under a budget stop."""
    provider = _Provider()

    def check(box: _Box) -> bool:
        """Raise the check error.

        Args:
            box: The warm box.

        Raises:
            BaseException: The check error.
        """
        raise check_error

    def close(box: _Box) -> None:
        """Raise the close error.

        Args:
            box: The box.

        Raises:
            BaseException: The close error.
        """
        raise close_error

    lease = BoxLease(provider.open, close, check=check, **_LIMITS)
    box = lease.acquire("agent-1", [])
    lease.release(box, healthy=True)

    with pytest.raises(BaseException) as caught:
        lease.acquire("agent-1", [])

    assert caught.value is {"check": check_error, "close": close_error}[raised]


def test_new_grants_replace_the_warm_box() -> None:
    """A box keeps the network policy it opened with, so other grants need a new box."""
    provider = _Provider()
    lease = provider.lease()
    box = lease.acquire("agent-1", [Grant("pypi.org")])
    lease.release(box, healthy=True)

    again = lease.acquire("agent-1", [Grant("api.example.com")])

    assert box.closed
    assert again.grants == (Grant("api.example.com"),)


def test_a_failed_close_of_a_replaced_box_frees_the_agent() -> None:
    """A close error reaches the caller, and the agent's next acquire still opens a box."""
    provider = _Provider()
    failing = [True]

    def close(box: _Box) -> None:
        """Fail the first close.

        Args:
            box: The box.

        Raises:
            RuntimeError: On the first close.
        """
        if failing:
            failing.clear()
            raise RuntimeError("close failed")
        provider.close(box)

    lease = BoxLease(provider.open, close, **_LIMITS)
    box = lease.acquire("agent-1", [Grant("pypi.org")])
    lease.release(box, healthy=True)

    with pytest.raises(RuntimeError, match="close failed"):
        lease.acquire("agent-1", [])

    assert _acquire_within(lease, "agent-1") is not box


def test_a_bad_gpu_grant_keeps_the_warm_box() -> None:
    """A refused grant list changes nothing in the lease."""
    provider = _Provider()
    lease = provider.lease()
    box = lease.acquire("agent-1", [])
    lease.release(box, healthy=True)

    with pytest.raises(GrantError):
        lease.acquire("agent-1", [Grant("gpu:a100"), Grant("gpu:h100")])

    assert not box.closed
    assert lease.acquire("agent-1", []) is box


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ([Grant("PyPI.org")], [Grant("pypi.org")]),
        ([Grant("pypi.org."), Grant("pypi.org", credential=True)], [Grant("pypi.org", credential=True)]),
        ([Grant("gpu:A100")], [Grant("gpu:a100")]),
    ],
)
def test_an_equal_grant_list_keeps_the_warm_box(first: list[Grant], second: list[Grant]) -> None:
    """Grant lists that resolve to the same policy share the warm box: host case and repeats do not matter."""
    provider = _Provider()
    lease = provider.lease()
    box = lease.acquire("agent-1", first)
    lease.release(box, healthy=True)

    assert lease.acquire("agent-1", second) is box
    assert provider.opened == [box]


@pytest.mark.parametrize("host", [7, None])
def test_a_host_that_is_not_text_is_a_grant_error_and_keeps_the_warm_box(host: object) -> None:
    """A bad host is a refused grant list, not an AttributeError from the lease."""
    provider = _Provider()
    lease = provider.lease()
    box = lease.acquire("agent-1", [])
    lease.release(box, healthy=True)

    with pytest.raises(GrantError):
        lease.acquire("agent-1", [Grant(host)])  # type: ignore[arg-type]

    assert lease.acquire("agent-1", []) is box
    assert provider.opened == [box]


def test_a_gpu_grant_opens_on_modal() -> None:
    """The opener gets the Modal placement with the GPU type."""
    provider = _Provider()

    box = provider.lease().acquire("agent-1", [Grant("gpu:a100")])

    assert box.placement == Placement(MODAL, "a100")


def test_boxes_over_the_vercel_cap_spill_and_come_back_when_room_frees() -> None:
    """At the cap new boxes go to the spill provider; a dropped Vercel box frees room."""
    provider = _Provider()
    lease = provider.lease(vercel_cap=1, spill="daytona")

    first = lease.acquire("agent-1", [])
    second = lease.acquire("agent-2", [])
    lease.release(first, healthy=False)
    third = lease.acquire("agent-3", [])

    assert [box.placement.provider for box in (first, second, third)] == [VERCEL, "daytona", VERCEL]


def test_a_vercel_box_in_a_slow_close_still_counts_against_the_cap() -> None:
    """A dropped Vercel box keeps its place until its close ends, so a concurrent acquire spills."""
    provider = _Provider()
    closing = threading.Event()
    gate = threading.Event()

    def close(box: _Box) -> None:
        """Hold the close until the gate opens.

        Args:
            box: The box.
        """
        closing.set()
        gate.wait(timeout=5)
        provider.close(box)

    lease = BoxLease(provider.open, close, vercel_cap=1, spill="daytona", starts_per_minute=1000)
    first = lease.acquire("agent-1", [])
    releaser = threading.Thread(target=lease.release, args=(first, False), daemon=True)
    releaser.start()
    try:
        assert closing.wait(timeout=5)
        second = _acquire_within(lease, "agent-2")
    finally:
        gate.set()
        releaser.join(timeout=5)
    third = _acquire_within(lease, "agent-3")

    assert not releaser.is_alive()
    assert first.closed
    assert [box.placement.provider for box in (first, second, third)] == [VERCEL, "daytona", VERCEL]


def test_a_failed_open_frees_its_reservation() -> None:
    """An open error reaches the caller and does not hold a Vercel place or the agent's key."""
    provider = _Provider(fail_opens=1)
    lease = provider.lease(vercel_cap=1)

    with pytest.raises(SandboxInfraError):
        lease.acquire("agent-1", [])
    box = _acquire_within(lease, "agent-1")

    assert box.placement == Placement(VERCEL)


def test_box_starts_stay_under_the_budget_after_a_mass_failure() -> None:
    """When every box dies at once, the new starts spread out within the starts per minute."""
    provider = _Provider()
    lease = provider.lease(starts_per_minute=3)
    agents = [f"agent-{index}" for index in range(6)]
    boxes = [lease.acquire(agent, []) for agent in agents]
    for box in boxes:
        lease.release(box, healthy=False)

    for agent in agents:
        lease.acquire(agent, [])

    times = provider.start_times
    assert len(times) == 12
    for start in times:
        assert sum(1 for other in times if start <= other < start + 60) <= 3
    # The first three starts use the budget at once; each later start waits for the window.
    assert times[:4] == [0.0, 0.0, 0.0, 60.0]
    assert times[-1] == 180.0


def test_the_start_budget_refuses_a_bad_limit() -> None:
    """The budget must be a positive whole number."""
    for limit in (0, -1, True, 1.5):
        with pytest.raises(ValueError, match="start budget"):
            StartBudget(limit)  # type: ignore[arg-type]


def test_the_lease_refuses_a_bad_vercel_cap() -> None:
    """The cap must be a whole number of 0 or more."""
    provider = _Provider()
    for cap in (None, True, 1.5, -1):
        with pytest.raises(ValueError, match="Vercel cap"):
            provider.lease(vercel_cap=cap)


@pytest.mark.parametrize("spill", [VERCEL, "", None])
def test_the_lease_refuses_a_bad_spill_provider(spill: Any) -> None:
    """A Vercel spill would count past the cap, so the cap would have no effect."""
    with pytest.raises(ValueError, match="spill provider"):
        _Provider().lease(spill=spill)
    with pytest.raises(ValueError, match="spill provider"):
        RuntimeBoxLease({VERCEL: _Runtime()}, _SPEC, spill=spill, **_LIMITS)


@pytest.mark.parametrize(
    ("option", "value", "message"),
    [
        ("keep_warm", "yes", "keep_warm"),
        ("check", 1, "check"),
        ("clock", None, "callable"),
        ("sleep", 1, "callable"),
        ("idle_wait", 1, "callable"),
    ],
)
def test_the_lease_refuses_a_bad_option(option: str, value: Any, message: str) -> None:
    """A bad option fails when the lease is built, not at its first acquire."""
    with pytest.raises(TypeError, match=message):
        _Provider().lease(**{option: value})


def test_the_lease_refuses_an_opener_or_closer_that_is_not_callable() -> None:
    """A bad opener or closer fails when the lease is built."""
    provider = _Provider()

    with pytest.raises(TypeError, match="callable"):
        BoxLease(None, provider.close, **_LIMITS)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="callable"):
        BoxLease(provider.open, None, **_LIMITS)  # type: ignore[arg-type]


def test_the_lease_needs_a_cap_and_a_start_budget() -> None:
    """The real Vercel team limits are not confirmed, so the lease has no default for them."""
    provider = _Provider()

    with pytest.raises(TypeError):
        BoxLease(provider.open, provider.close)  # type: ignore[call-arg]


def test_a_lease_without_keep_warm_closes_every_box_at_release() -> None:
    """With keep_warm=False a healthy release closes the box, so no box serves two acquires."""
    provider = _Provider()
    lease = provider.lease(keep_warm=False)
    box = lease.acquire("agent-1", [])

    lease.release(box, healthy=True)

    assert box.closed
    assert lease.acquire("agent-1", []) is not box
    assert not lease.keeps_warm


def test_close_closes_idle_boxes_and_a_held_box_at_its_release() -> None:
    """At the run's end every box closes, and no new box opens."""
    provider = _Provider()
    lease = provider.lease()
    idle = lease.acquire("agent-1", [])
    lease.release(idle, healthy=True)
    held = lease.acquire("agent-2", [])

    lease.close()
    assert idle.closed
    assert not held.closed
    lease.release(held, healthy=True)

    assert held.closed
    with pytest.raises(RuntimeError, match="closed"):
        lease.acquire("agent-1", [])


def test_concurrent_opens_never_pass_the_vercel_cap() -> None:
    """Callers that open at the same time each count against the cap before their open ends."""
    provider = _Provider()
    entered = threading.Semaphore(0)
    gate = threading.Event()
    placements: list[Placement] = []

    def open_box(placement: Placement, grants: Sequence[Grant]) -> _Box:
        """Record the placement, then hold the open until the gate opens.

        Args:
            placement: The box's provider.
            grants: The box's grants.

        Returns:
            The box.
        """
        placements.append(placement)
        entered.release()
        gate.wait(timeout=5)
        return provider.open(placement, grants)

    lease = BoxLease(open_box, provider.close, vercel_cap=2, starts_per_minute=1000)
    threads = [threading.Thread(target=lease.acquire, args=(f"agent-{index}", []), daemon=True) for index in range(5)]
    for thread in threads:
        thread.start()
    # Every open is in flight at once before any of them ends.
    assert all(entered.acquire(timeout=5) for _ in threads)
    gate.set()
    for thread in threads:
        thread.join(timeout=5)

    assert not any(thread.is_alive() for thread in threads)
    assert sorted(placement.provider for placement in placements) == [MODAL, MODAL, MODAL, VERCEL, VERCEL]


def test_a_second_acquire_for_a_busy_agent_waits_and_gets_the_same_box() -> None:
    """One agent's box serves one caller at a time; the next caller gets it at the release."""
    provider = _Provider()
    lease = provider.lease()
    box = lease.acquire("agent-1", [])
    got: list[_Box] = []
    done = threading.Event()

    def second() -> None:
        """Acquire the agent's box from another thread."""
        got.append(lease.acquire("agent-1", []))
        done.set()

    waiter = threading.Thread(target=second, daemon=True)
    waiter.start()
    assert not done.wait(timeout=0.2)
    lease.release(box, healthy=True)
    waiter.join(timeout=5)

    assert not waiter.is_alive()
    assert got == [box]
    assert provider.opened == [box]


def test_close_makes_a_waiter_on_a_busy_agent_raise_at_the_release() -> None:
    """A caller that waits for a held box gets RuntimeError after close, and no new box opens."""
    provider = _Provider()
    lease = provider.lease()
    box = lease.acquire("agent-1", [])
    errors: list[RuntimeError] = []
    done = threading.Event()

    def second() -> None:
        """Acquire the agent's box from another thread and record the error."""
        try:
            lease.acquire("agent-1", [])
        except RuntimeError as error:
            errors.append(error)
        done.set()

    waiter = threading.Thread(target=second, daemon=True)
    waiter.start()
    assert not done.wait(timeout=0.2)
    lease.close()
    assert not done.is_set()
    lease.release(box, healthy=True)
    waiter.join(timeout=5)

    assert not waiter.is_alive()
    assert [str(error) for error in errors] == ["The box lease is closed."]
    assert box.closed
    assert provider.opened == [box]


def test_a_close_during_the_budget_wait_stops_the_open() -> None:
    """An acquire that waits for a start opens no box when the lease closes in the wait."""
    provider = _Provider()

    def close_in_the_wait(seconds: float) -> None:
        """Close the lease while the acquire waits for the budget, then move the fake time.

        Args:
            seconds: The wait.
        """
        lease.close()
        provider.clock.sleep(seconds)

    lease = provider.lease(starts_per_minute=1, sleep=close_in_the_wait)
    first = lease.acquire("agent-1", [])
    lease.release(first, healthy=False)

    with pytest.raises(RuntimeError, match="closed"):
        lease.acquire("agent-2", [])

    assert provider.opened == [first]
    assert provider.clock.sleeps == [60.0]
    # A leaked reservation would make this acquire wait forever instead of raising.
    with pytest.raises(RuntimeError, match="closed"):
        _acquire_within(lease, "agent-2")


def test_close_wakes_every_acquire_that_waits_for_a_start() -> None:
    """A close ends each real budget wait at once with no start counted, so no thread waits out the window."""
    provider = _Provider()
    waiting = threading.Semaphore(0)

    def sleep(seconds: float) -> None:
        """Signal that a waiter reached the budget wait, then wait on the lease.

        Args:
            seconds: The budget wait.
        """
        waiting.release()
        lease._wait_on_lease(seconds)

    lease = provider.lease(starts_per_minute=1, clock=time.monotonic, sleep=sleep)
    first = lease.acquire("agent-0", [])
    errors: list[RuntimeError] = []

    def wait(agent_id: str) -> None:
        """Acquire a new box, which waits for the next start, and record the error.

        Args:
            agent_id: The agent.
        """
        try:
            lease.acquire(agent_id, [])
        except RuntimeError as error:
            errors.append(error)

    waiters = [threading.Thread(target=wait, args=(f"agent-{index}",), daemon=True) for index in range(1, 6)]
    for waiter in waiters:
        waiter.start()
    # Each waiter reserves its agent before the budget wait; the window frees its start only after 60 s.
    assert all(waiting.acquire(timeout=5) for _ in waiters)
    lease.close()
    for waiter in waiters:
        waiter.join(timeout=5)

    assert not any(waiter.is_alive() for waiter in waiters), "A waiter still waits for a start after close."
    assert [str(error) for error in errors] == ["The box lease is closed."] * 5
    assert len(lease._budget._starts) == 1
    assert provider.opened == [first]
    lease.release(first, healthy=True)
    assert first.closed


def test_release_refuses_a_box_it_does_not_hold() -> None:
    """A box from elsewhere is a caller bug."""
    with pytest.raises(ValueError, match="does not hold"):
        _Provider().lease().release(_Box(Placement(VERCEL), ()), healthy=True)


def test_release_refuses_a_box_that_no_caller_holds() -> None:
    """A second healthy release of a warm box is a caller bug; the box stays the agent's."""
    provider = _Provider()
    lease = provider.lease()
    box = lease.acquire("agent-1", [])
    lease.release(box, healthy=True)

    with pytest.raises(ValueError, match="No caller holds"):
        lease.release(box, healthy=True)

    assert lease.acquire("agent-1", []) is box


class _Ticker:
    """Stand for the reaper's idle wait: each wait blocks until the test ticks."""

    def __init__(self) -> None:
        """Start with no wait, no tick and blocking waits."""
        self.waits: list[float] = []
        self.thread: threading.Thread | None = None
        self._ticks = 0
        self._free = False
        self._cond = threading.Condition()
        _TICKERS.append(self)

    def __call__(self, seconds: float) -> None:
        """Record the wait, then block until the next tick.

        Args:
            seconds: The reaper's wait.
        """
        with self._cond:
            if self._free:
                # A short real pause, so a reaper that ignores close does not take the CPU.
                self._cond.wait(0.001)
                return
            self.waits.append(seconds)
            self.thread = threading.current_thread()
            turn = len(self.waits)
            self._cond.notify_all()
            # No timeout: a timeout would let a stalled test get an extra reaper pass. free() always ends this
            # wait, because _stop and the autouse teardown free every ticker before the close.
            self._cond.wait_for(lambda: self._free or self._ticks >= turn)

    def passes(self, count: int) -> None:
        """Wait until the reaper made ``count`` passes over the lease.

        Args:
            count: The number of reaper waits.
        """
        with self._cond:
            assert self._cond.wait_for(lambda: len(self.waits) >= count, timeout=5), "The reaper made no pass."

    def tick(self) -> None:
        """End the reaper's wait, and wait until its next pass ends."""
        with self._cond:
            self._ticks += 1
            self._cond.notify_all()
        self.passes(self._ticks + 1)

    def tick_to_end(self) -> None:
        """End the reaper's wait, and check that the reaper thread then ends with no new wait."""
        with self._cond:
            self._ticks += 1
            waits = len(self.waits)
            thread = self.thread
            self._cond.notify_all()
        assert thread is not None, "The reaper made no pass."
        thread.join(timeout=5)
        assert not thread.is_alive(), "The reaper still runs with no idle box."
        assert len(self.waits) == waits

    def free(self) -> None:
        """Make every wait end at once, so the reaper can see a close."""
        with self._cond:
            self._free = True
            self._cond.notify_all()


def _sleepy(provider: _Provider) -> tuple[BoxLease[_Box], _Ticker, _Box]:
    """Return a lease with a 600 s idle time, its ticker, and one idle box released at time 0.

    Args:
        provider: The fake provider.

    Returns:
        The lease, the ticker after the reaper's first pass, and the idle box.
    """
    ticker = _Ticker()
    lease = provider.lease(sleep_after_seconds=600, idle_wait=ticker)
    box = lease.acquire("agent-1", [])
    lease.release(box, healthy=True)
    ticker.passes(1)
    return lease, ticker, box


def _stop(lease: BoxLease[_Box], ticker: _Ticker) -> None:
    """Close the lease, and free the ticker first so the reaper sees the close.

    Args:
        lease: The lease.
        ticker: The lease's idle wait.
    """
    ticker.free()
    lease.close()


def test_an_idle_box_sleeps_after_the_idle_time_with_no_other_call() -> None:
    """Like a Cloudflare sleepAfter, the reaper closes an idle box on its own when its idle time ends."""
    provider = _Provider()
    lease, ticker, box = _sleepy(provider)
    try:
        assert ticker.waits == [600.0]
        provider.clock.now = 599.0
        ticker.tick()
        assert not box.closed
        assert ticker.waits[-1] == 1.0

        provider.clock.now = 600.0
        ticker.tick_to_end()

        assert box.closed
        assert provider.closed == [box]
        assert "agent-1" not in lease._leases
        # With no idle box left, the reaper thread ends, so a lease that is never closed holds no thread.
        assert lease._reaper is None
    finally:
        _stop(lease, ticker)


def test_activity_restarts_the_idle_time() -> None:
    """An acquire and its healthy release restart the idle time, so a used box stays warm."""
    provider = _Provider()
    lease, ticker, box = _sleepy(provider)
    try:
        provider.clock.now = 500.0
        assert lease.acquire("agent-1", []) is box
        lease.release(box, healthy=True)

        provider.clock.now = 600.0
        ticker.tick()
        assert not box.closed
        assert ticker.waits[-1] == 500.0

        provider.clock.now = 1100.0
        ticker.tick_to_end()
        assert box.closed
    finally:
        _stop(lease, ticker)


def test_a_busy_box_never_sleeps() -> None:
    """A held box stays open past the idle time, and its idle time starts at its release."""
    provider = _Provider()
    lease, ticker, box = _sleepy(provider)
    try:
        assert lease.acquire("agent-1", []) is box
        provider.clock.now = 10_000.0
        # The held box is not idle, so the reaper has nothing to wait for and ends.
        ticker.tick_to_end()
        assert not box.closed
        assert lease._reaper is None

        lease.release(box, healthy=True)
        ticker.passes(2)
        assert ticker.waits[-1] == 600.0
        provider.clock.now = 10_599.0
        ticker.tick()
        assert not box.closed

        provider.clock.now = 10_600.0
        ticker.tick_to_end()
        assert box.closed
    finally:
        _stop(lease, ticker)


def test_the_next_acquire_after_a_sleep_opens_a_new_box_and_counts_a_start() -> None:
    """The box state is lost by design: the agent gets a new box through placement and the start budget."""
    provider = _Provider()
    lease, ticker, box = _sleepy(provider)
    try:
        provider.clock.now = 600.0
        ticker.tick_to_end()

        fresh = lease.acquire("agent-1", [])

        assert fresh is not box
        assert not fresh.closed
        assert provider.opened == [box, fresh]
        assert provider.start_times == [0.0, 600.0]
        # The start at time 0 left the 60 s window, so the budget holds only the new start.
        assert list(lease._budget._starts) == [600.0]
        assert lease._open[VERCEL] == 1
    finally:
        _stop(lease, ticker)


def test_an_idle_box_close_error_is_logged_by_type_only(caplog: pytest.LogCaptureFixture) -> None:
    """The log names only the error type, because the error text can hold a secret; close() raises the error."""
    provider = _Provider()

    def fail_close(box: _Box) -> None:
        """Fail the close with a secret in the text.

        Args:
            box: The box.

        Raises:
            SandboxInfraError: Always.
        """
        raise SandboxInfraError("token=secret-value")

    ticker = _Ticker()
    lease = BoxLease(
        provider.open,
        fail_close,
        clock=provider.clock,
        sleep_after_seconds=600,
        idle_wait=ticker,
        **_LIMITS,
    )
    box = lease.acquire("agent-1", [])
    with caplog.at_level("WARNING", logger="core.sandbox.box_lease"):
        lease.release(box, healthy=True)
        ticker.passes(1)
        try:
            provider.clock.now = 600.0
            ticker.tick_to_end()
        finally:
            with pytest.raises(SandboxInfraError):
                _stop(lease, ticker)

    assert "agent-1" not in lease._leases
    assert [record.getMessage() for record in caplog.records] == ["The idle box close failed: SandboxInfraError"]


def _failing_lease(provider: _Provider, error: BaseException) -> tuple[BoxLease[_Box], _Ticker]:
    """Return a lease whose closer raises ``error``, after its one idle box reached its idle time.

    Args:
        provider: The fake provider.
        error: The closer's error.

    Returns:
        The lease and its ticker, after the reaper's failed close.
    """

    def fail_close(box: _Box) -> None:
        """Fail the close.

        Args:
            box: The box.

        Raises:
            BaseException: ``error``, always.
        """
        raise error

    ticker = _Ticker()
    lease = BoxLease(
        provider.open, fail_close, clock=provider.clock, sleep_after_seconds=600, idle_wait=ticker, **_LIMITS
    )
    lease.release(lease.acquire("agent-1", []), healthy=True)
    ticker.passes(1)
    provider.clock.now = 600.0
    ticker.tick_to_end()
    return lease, ticker


def test_a_budget_stop_from_an_idle_box_close_reaches_the_next_acquire_once() -> None:
    """A settlement budget stop must stop the run, so the next acquire raises it and opens no box."""
    provider = _Provider()
    stop = BudgetReached("budget stopped")
    lease, ticker = _failing_lease(provider, stop)
    try:
        with pytest.raises(BudgetReached) as raised:
            lease.acquire("agent-2", [])
        assert raised.value is stop
        assert len(provider.opened) == 1

        # Raised once: the run's own handling decides what comes next.
        assert lease.acquire("agent-2", []) is provider.opened[1]
    finally:
        _stop(lease, ticker)


def test_a_budget_stop_from_an_idle_box_close_reaches_close() -> None:
    """With no acquire after the failed close, close() raises the budget stop."""
    provider = _Provider()
    stop = BudgetReached("budget stopped")
    lease, ticker = _failing_lease(provider, stop)

    with pytest.raises(BudgetReached) as raised:
        _stop(lease, ticker)

    assert raised.value is stop


def test_a_budget_stop_replaces_an_earlier_box_error_of_the_reaper() -> None:
    """A budget stop must stop the run, so a box error from the same pass does not hide it."""
    provider = _Provider()
    stop = BudgetReached("budget stopped")
    errors: dict[str, BaseException] = {}

    def fail_close(box: _Box) -> None:
        """Fail each close with the error set for the box's agent.

        Args:
            box: The box.

        Raises:
            BaseException: The agent's error, always.
        """
        raise errors[next(agent for agent, opened in boxes.items() if opened is box)]

    ticker = _Ticker()
    lease = BoxLease(
        provider.open, fail_close, clock=provider.clock, sleep_after_seconds=600, idle_wait=ticker, **_LIMITS
    )
    boxes = {agent: lease.acquire(agent, []) for agent in ("agent-1", "agent-2")}
    errors.update({"agent-1": SandboxInfraError("The box is gone."), "agent-2": stop})
    for box in boxes.values():
        lease.release(box, healthy=True)
    ticker.passes(1)
    try:
        provider.clock.now = 600.0
        ticker.tick_to_end()

        with pytest.raises(BudgetReached) as raised:
            lease.acquire("agent-3", [])
        assert raised.value is stop
    finally:
        _stop(lease, ticker)


def test_a_budget_stop_in_close_replaces_a_box_error_that_the_reaper_kept() -> None:
    """close() uses the reaper's rule, so a budget stop from its own close wins over a kept box error."""
    provider = _Provider()
    stop = BudgetReached("budget stopped")
    errors: dict[str, BaseException] = {}

    def fail_close(box: _Box) -> None:
        """Fail each close with the error set for the box's agent.

        Args:
            box: The box.

        Raises:
            BaseException: The agent's error, always.
        """
        raise errors[next(agent for agent, opened in boxes.items() if opened is box)]

    ticker = _Ticker()
    lease = BoxLease(
        provider.open, fail_close, clock=provider.clock, sleep_after_seconds=600, idle_wait=ticker, **_LIMITS
    )
    boxes = {agent: lease.acquire(agent, []) for agent in ("agent-1", "agent-2")}
    errors.update({"agent-1": SandboxInfraError("The box is gone."), "agent-2": stop})
    lease.release(boxes["agent-1"], healthy=True)
    ticker.passes(1)
    provider.clock.now = 300.0
    lease.release(boxes["agent-2"], healthy=True)
    provider.clock.now = 600.0
    ticker.tick()
    assert "agent-1" not in lease._leases
    assert "agent-2" in lease._leases

    with pytest.raises(BudgetReached) as raised:
        _stop(lease, ticker)

    assert raised.value is stop


def test_a_reaper_that_fails_outside_a_close_keeps_its_error_and_the_next_release_starts_a_new_one() -> None:
    """A broken idle wait ends the reaper; the error reaches the next acquire, and a release starts a new reaper."""
    provider = _Provider()
    ticker = _Ticker()
    broken: list[float] = []
    # Holds the first failure until the test has the reaper thread, so the thread cannot end before the test sees it.
    fail = threading.Event()

    def wait_once_broken(seconds: float) -> None:
        """Fail the first wait when the test allows it, then wait on the ticker.

        Args:
            seconds: The reaper's wait.

        Raises:
            RuntimeError: At the first wait.
        """
        if not broken:
            broken.append(seconds)
            fail.wait(timeout=5)
            raise RuntimeError("The wait broke.")
        ticker(seconds)

    lease = provider.lease(sleep_after_seconds=600, idle_wait=wait_once_broken)
    box = lease.acquire("agent-1", [])
    lease.release(box, healthy=True)
    reaper = lease._reaper
    assert reaper is not None
    fail.set()
    reaper.join(timeout=5)
    try:
        assert not reaper.is_alive()
        assert broken, "The reaper stopped before its first wait."
        assert lease._reaper is None, "The failed reaper still blocks a new one."
        with pytest.raises(RuntimeError, match="The wait broke"):
            lease.acquire("agent-1", [])

        assert lease.acquire("agent-1", []) is box
        lease.release(box, healthy=True)
        ticker.passes(1)
        provider.clock.now = 600.0
        ticker.tick_to_end()
        assert box.closed
    finally:
        _stop(lease, ticker)


def test_a_slow_idle_close_does_not_delay_the_next_deadline() -> None:
    """The reaper reads the clock again after a close, so a box whose time passed in the close closes at once."""
    provider = _Provider()

    def slow_close(box: _Box) -> None:
        """Close the box, and move the fake time 30 s as a slow provider call does.

        Args:
            box: The box.
        """
        provider.close(box)
        provider.clock.now += 30.0

    ticker = _Ticker()
    lease = BoxLease(
        provider.open, slow_close, clock=provider.clock, sleep_after_seconds=600, idle_wait=ticker, **_LIMITS
    )
    first = lease.acquire("agent-1", [])
    lease.release(first, healthy=True)
    ticker.passes(1)
    provider.clock.now = 10.0
    second = lease.acquire("agent-2", [])
    lease.release(second, healthy=True)
    try:
        provider.clock.now = 600.0
        ticker.tick_to_end()

        assert provider.closed == [first, second]
    finally:
        _stop(lease, ticker)


def test_close_waits_for_the_reaper_to_stop() -> None:
    """close() returns only after the reaper thread ends, so no reaper outlives the lease."""
    provider = _Provider()
    lease, ticker, _ = _sleepy(provider)
    reaper = lease._reaper
    assert reaper is not None
    closer = threading.Thread(target=lease.close, daemon=True)
    closer.start()
    try:
        # The reaper still blocks in the ticker, so close must still wait for it.
        closer.join(timeout=0.2)
        assert closer.is_alive(), "close() returned while the reaper still ran."
    finally:
        ticker.free()
        closer.join(timeout=5)

    assert not closer.is_alive(), "close() did not stop the reaper."
    assert not reaper.is_alive()


def test_close_wakes_the_default_reaper_wait_at_once() -> None:
    """The default idle wait is on the lease, so close does not wait out the 600 s idle time."""
    provider = _Provider()
    lease = provider.lease(clock=time.monotonic, sleep=None)
    box = lease.acquire("agent-1", [])
    lease.release(box, healthy=True)
    reaper = lease._reaper
    assert reaper is not None
    closer = threading.Thread(target=lease.close, daemon=True)

    closer.start()
    closer.join(timeout=5)

    assert not closer.is_alive(), "close() waited out the idle time."
    assert not reaper.is_alive()
    assert box.closed


def test_a_failing_reaper_clears_itself_in_the_lock_that_keeps_its_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """No release can come between the kept error and the cleared reaper, so a later release starts a new reaper."""
    provider = _Provider()
    ticker = _Ticker()
    broken: list[float] = []

    def wait_once_broken(seconds: float) -> None:
        """Fail the first wait, then wait on the ticker.

        Args:
            seconds: The reaper's wait.

        Raises:
            RuntimeError: At the first wait.
        """
        if not broken:
            broken.append(seconds)
            raise RuntimeError("The wait broke.")
        ticker(seconds)

    lease = provider.lease(sleep_after_seconds=600, idle_wait=wait_once_broken)
    first = lease.acquire("agent-1", [])
    second = lease.acquire("agent-2", [])
    keep = lease._keep_reap_error
    released_in_gap: list[bool] = []
    failed: list[threading.Thread] = []
    kept = threading.Event()

    def keep_then_release(error: BaseException) -> None:
        """Keep the error, then try a healthy release from another thread before the reaper clears its field.

        Args:
            error: The reaper's error.
        """
        keep(error)
        if failed:
            return
        failed.append(threading.current_thread())

        def release_if_free() -> None:
            """Release the second box only when the reaper does not hold the lease lock."""
            if not lease._cond.acquire(blocking=False):
                released_in_gap.append(False)
                return
            lease._cond.release()
            released_in_gap.append(True)
            lease.release(second, healthy=True)

        # Joined here, so the release, if the lock lets it, runs before the reaper clears its field.
        other = threading.Thread(target=release_if_free, daemon=True)
        other.start()
        other.join(timeout=5)
        kept.set()

    monkeypatch.setattr(lease, "_keep_reap_error", keep_then_release)
    lease.release(first, healthy=True)
    assert kept.wait(timeout=5), "The reaper did not fail."
    failed[0].join(timeout=5)
    try:
        assert not failed[0].is_alive()
        assert released_in_gap == [False], "A release ran between the kept error and the cleared reaper."
        assert lease._reaper is None

        lease.release(second, healthy=True)
        ticker.passes(1)
        assert lease._reaper is not None
        assert lease._reaper.is_alive()
        with pytest.raises(RuntimeError, match="The wait broke"):
            lease.acquire("agent-3", [])
    finally:
        _stop(lease, ticker)


def test_a_reaper_ends_with_no_idle_box_and_the_next_release_starts_a_new_one() -> None:
    """A lease that is never closed keeps no reaper thread after its last idle box sleeps."""
    provider = _Provider()
    lease, ticker, _ = _sleepy(provider)
    first = lease._reaper
    assert first is not None
    try:
        provider.clock.now = 600.0
        ticker.tick_to_end()
        assert lease._reaper is None
        assert not first.is_alive()

        lease.release(lease.acquire("agent-1", []), healthy=True)
        ticker.passes(2)
        assert lease._reaper is not None
        assert lease._reaper is not first
    finally:
        _stop(lease, ticker)


def test_a_release_does_not_wake_the_reaper(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reaper waits on its own event, so a release, a drop or a failed open of another agent costs it no pass."""
    provider = _Provider()
    reaper_reads: list[float] = []
    waiting = threading.Event()
    wait = threading.Condition.wait

    def clock() -> float:
        """Return the fake time, and count each read of the reaper thread.

        Returns:
            The fake time.
        """
        if threading.current_thread().name == _REAPER:
            reaper_reads.append(provider.clock.now)
        return provider.clock.now

    def wait_and_tell(self: threading.Condition, timeout: float | None = None) -> bool:
        """Tell the test that the reaper starts its idle wait, then wait.

        Args:
            self: The condition.
            timeout: The longest wait.

        Returns:
            The wait's own result.
        """
        if threading.current_thread().name == _REAPER:
            waiting.set()
        return wait(self, timeout)

    # Every idle wait, on an event or on a condition, ends in Condition.wait.
    monkeypatch.setattr(threading.Condition, "wait", wait_and_tell)
    lease = provider.lease(clock=clock)
    lease.release(lease.acquire("agent-0", []), healthy=True)
    assert waiting.wait(timeout=5), "The reaper made no idle wait."
    with lease._cond:
        # Taken after the reaper's wait gives up its lock, so a wait on the lease condition shows here.
        assert not lease._cond._waiters, "The reaper waits on the lease condition, so each release wakes it."
    for number in range(1, 21):
        box = lease.acquire(f"agent-{number}", [])
        lease.release(box, healthy=True)
        lease.release(lease.acquire(f"agent-{number}", []), healthy=False)
    provider.fail_opens = 1
    with pytest.raises(SandboxInfraError):
        lease.acquire("agent-x", [])
    lease.close()

    assert reaper_reads == [0.0]


def test_the_reaper_sleeps_idle_boxes_in_release_order() -> None:
    """Each release goes to the end of the idle order, so the reaper looks only at the first idle box."""
    provider = _Provider()
    ticker = _Ticker()
    lease = provider.lease(sleep_after_seconds=600, idle_wait=ticker)
    boxes = {agent: lease.acquire(agent, []) for agent in ("a", "b", "c")}
    lease.release(boxes["a"], healthy=True)
    ticker.passes(1)
    try:
        provider.clock.now = 100.0
        lease.release(boxes["b"], healthy=True)
        provider.clock.now = 200.0
        lease.release(boxes["c"], healthy=True)
        provider.clock.now = 250.0
        assert lease.acquire("a", []) is boxes["a"]
        assert list(lease._idle) == ["b", "c"]
        provider.clock.now = 300.0
        lease.release(boxes["a"], healthy=True)
        assert list(lease._idle) == ["b", "c", "a"]

        provider.clock.now = 700.0
        ticker.tick()

        assert provider.closed == [boxes["b"]]
        assert list(lease._idle) == ["c", "a"]
        # The next deadline is c's, at 800.
        assert ticker.waits[-1] == 100.0
    finally:
        _stop(lease, ticker)


def test_the_reaper_closes_an_idle_box_in_the_context_of_its_last_release() -> None:
    """The settlement of a reaper close sees the context vars of the box's last healthy release."""
    provider = _Provider()
    seen: list[str | None] = []

    def close_in_scope(box: _Box) -> None:
        """Record the run scope that the close sees, then close the box.

        Args:
            box: The box.
        """
        seen.append(_RUN_SCOPE.get(None))
        provider.close(box)

    ticker = _Ticker()
    lease = BoxLease(
        provider.open, close_in_scope, clock=provider.clock, sleep_after_seconds=600, idle_wait=ticker, **_LIMITS
    )
    box = lease.acquire("agent-1", [])
    for scope in ("step-1", "step-2"):
        token = _RUN_SCOPE.set(scope)
        try:
            lease.release(box, healthy=True)
        finally:
            _RUN_SCOPE.reset(token)
        if scope == "step-1":
            assert lease.acquire("agent-1", []) is box
    ticker.passes(1)
    try:
        provider.clock.now = 600.0
        ticker.tick_to_end()
    finally:
        _stop(lease, ticker)

    assert seen == ["step-2"]


@pytest.mark.parametrize("seconds", [0, -1, True, None, "600", float("inf"), float("nan"), 1e10])
def test_the_lease_refuses_a_bad_sleep_after_time(seconds: Any) -> None:
    """A bad idle time fails when the lease is built, like a bad start budget."""
    with pytest.raises(ValueError, match="sleep-after"):
        _Provider().lease(sleep_after_seconds=seconds)


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    """Return a SQLite engine with the schema and one job at generation 1.

    Args:
        tmp_path: Scratch folder.

    Returns:
        The engine.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(JobModel(optimization_id=RUN, username="ada", execution_generation=1))
        session.commit()
    return engine


@pytest.fixture
def step_log(engine: Engine, tmp_path: Path) -> Iterator[StepLogStore]:
    """Yield a step log with a short flush window.

    Args:
        engine: The test engine.
        tmp_path: Scratch folder.

    Yields:
        The step log; it closes after the test.
    """
    store = StepLogStore(engine, LocalObjectStore(tmp_path / "objects"), "w1", flush_window_seconds=0.02)
    yield store
    store.close()


@pytest.fixture
def trees(step_log: StepLogStore) -> TreeStore:
    """Return a tree store over the step log.

    Args:
        step_log: The test step log.

    Returns:
        The tree store.
    """
    return TreeStore(step_log)


class _DyingBox:
    """Wrap a local box whose agent commands raise once ``dead`` is set, as a box that dies mid-step."""

    def __init__(self, inner: SandboxSession) -> None:
        """Wrap the box.

        Args:
            inner: The local box.
        """
        self._inner = inner
        self.dead = False
        self.closed = False
        # The health check's outcome: None runs it, an exit code fakes it, an error is raised from it.
        self.check: int | BaseException | None = None

    def write_files(self, files: Mapping[str, str]) -> None:
        """Write files into the inner box.

        Args:
            files: Relative path to text.
        """
        self._inner.write_files(files)

    def run(self, command: str, **options: Any) -> CommandResult:
        """Raise on an agent command of a dead box, fake the health check when set, and run the others.

        Args:
            command: The shell command.
            **options: The run options.

        Returns:
            The inner box's result, or the faked health check result.

        Raises:
            SandboxInfraError: For an agent command once the box is dead.
            BaseException: The set health check error, for the ``true`` command.
        """
        if command == "true" and isinstance(self.check, BaseException):
            raise self.check
        if command == "true" and self.check is not None:
            return CommandResult(exit_code=self.check)
        if self.dead and command.startswith("cd tree"):
            raise SandboxInfraError("The box stopped.")
        return self._inner.run(command, **options)

    def read_file(self, path: str) -> str | None:
        """Read a file from the inner box.

        Args:
            path: Relative path.

        Returns:
            The text, or ``None``.
        """
        return self._inner.read_file(path)

    def close(self) -> None:
        """Close the inner box."""
        self.closed = True
        self._inner.close()


@dataclass
class _Runtime:
    """Open local boxes that can die, and record each one."""

    injects_headers: bool = False
    boxes: list[_DyingBox] = field(default_factory=list)
    # When set, each box gets a new TMPDIR in this folder, outside its working directory.
    tmp_root: Path | None = None

    def open(self, spec: SandboxSpec) -> SandboxSession:
        """Open one local box.

        Args:
            spec: The box spec.

        Returns:
            The box.
        """
        if self.tmp_root is not None:
            spec = replace(spec, env={**spec.env, "TMPDIR": tempfile.mkdtemp(dir=self.tmp_root)})
        box = _DyingBox(LocalSubprocessRuntime().open(spec))
        self.boxes.append(box)
        return box  # type: ignore[return-value]


def _on_both(runtime: _Runtime) -> dict[str, _Runtime]:
    """Map Vercel and the Modal spill to one local runtime.

    Args:
        runtime: The runtime.

    Returns:
        The provider map.
    """
    return {VERCEL: runtime, MODAL: runtime}


def _tree(trees: TreeStore, files: Mapping[str, str]) -> str:
    """Store flat files as one tree.

    Args:
        trees: The tree store.
        files: Name to text.

    Returns:
        The tree id.
    """
    return trees.put_tree(
        RUN, [TreeEntry(name, "file", trees.put_blob(RUN, text.encode())) for name, text in files.items()]
    )


def _step(seq: int, agent_id: str = "agent-1") -> StepRef:
    """Return one step of an agent at generation 1.

    Args:
        seq: The step's place.
        agent_id: The agent.

    Returns:
        The step.
    """
    return StepRef(run_id=RUN, agent_id=agent_id, seq=seq, execution_generation=1)


def _sandbox(
    trees: TreeStore, step_log: StepLogStore, metric_runtime: _Runtime | None = None
) -> tuple[TreeSandbox, _Runtime, _Runtime, RuntimeBoxLease, RuntimeBoxLease]:
    """Build the layer over a warm candidate lease and a metric lease that keeps no box warm.

    Args:
        trees: The tree store.
        step_log: The step log.
        metric_runtime: The runtime of the metric boxes; ``None`` uses a plain local runtime.

    Returns:
        The layer, the candidate and metric runtimes, and the two leases.
    """
    candidate_runtime, metric_runtime = _Runtime(), metric_runtime or _Runtime()
    candidates = RuntimeBoxLease(_on_both(candidate_runtime), _SPEC, **_LIMITS)
    metrics = RuntimeBoxLease(_on_both(metric_runtime), _SPEC, keep_warm=False, **_LIMITS)
    return TreeSandbox(trees, step_log, candidates, metrics), candidate_runtime, metric_runtime, candidates, metrics


def test_an_agent_runs_its_steps_in_one_warm_box(trees: TreeStore, step_log: StepLogStore) -> None:
    """Two steps of one agent use one box, and each step starts from its own tree."""
    sandbox, runtime, _, candidates, metrics = _sandbox(trees, step_log)
    try:
        _, first = sandbox.run(_step(1), _tree(trees, {"a.txt": "one\n"}), "echo two > b.txt", [])
        result, _ = sandbox.run(_step(2), _tree(trees, {"a.txt": "other\n"}), "ls; cat a.txt", [])
    finally:
        candidates.close()
        metrics.close()

    assert len(runtime.boxes) == 1
    assert trees.get_tree(RUN, first) is not None
    assert result.stdout == "a.txt\nother\n"


def test_a_box_that_dies_mid_step_is_replaced_and_the_step_reruns_from_its_tree(
    trees: TreeStore, step_log: StepLogStore
) -> None:
    """The dead box drops; the retry opens a new box and starts from the last committed tree."""
    sandbox, runtime, _, candidates, metrics = _sandbox(trees, step_log)
    try:
        _, committed = sandbox.run(_step(1), _tree(trees, {"a.txt": "one\n"}), "echo two > b.txt", [])
        runtime.boxes[0].dead = True
        with pytest.raises(BoxFailureError):
            sandbox.run(_step(2), committed, "echo three > c.txt", [])
        result, _ = sandbox.run(_step(2), committed, "ls", [])
    finally:
        candidates.close()
        metrics.close()

    assert len(runtime.boxes) == 2
    assert result.stdout == "a.txt\nb.txt\n"


def test_a_score_never_sees_the_files_or_processes_of_an_earlier_score(
    trees: TreeStore, step_log: StepLogStore, tmp_path: Path
) -> None:
    """Each score gets a new box, so a stray TMPDIR file or background process of the last score stays out."""
    tmp_root = tmp_path / "box-tmp"
    tmp_root.mkdir()
    sandbox, _, metric_runtime, candidates, metrics = _sandbox(trees, step_log, _Runtime(tmp_root=tmp_root))
    metric = _tree(trees, {"score.sh": "echo 1.0\n"})
    # The metric fails when TMPDIR or the working directory holds a file from an earlier score. Then it
    # leaves a file in each and a bounded background loop that keeps writing to TMPDIR after it ends.
    command = (
        'sleep 0.3; test -z "$(ls -A "$TMPDIR")" || exit 1;'
        ' test -z "$(find . -type f ! -path "./outputs/*" ! -path "./metric/*")" || exit 1;'
        ' echo seen > left.txt; echo seen > "$TMPDIR/left.txt";'
        ' (for i in 1 2 3 4 5 6 7 8 9 10; do echo seen > "$TMPDIR/loop.txt"; sleep 0.2; done) >/dev/null 2>&1 &'
        " sh metric/score.sh"
    )
    try:
        first = sandbox.score(_step(1), _tree(trees, {"answer.txt": "1\n"}), metric, command)
        second = sandbox.score(_step(2), _tree(trees, {"answer.txt": "2\n"}), metric, command)
    finally:
        candidates.close()
        metrics.close()

    assert (first, second) == (1.0, 1.0)
    assert len(metric_runtime.boxes) == 2


def test_the_metric_box_never_gets_candidate_code(trees: TreeStore, step_log: StepLogStore) -> None:
    """The metric box holds only the outputs tree and the metric tree, never the candidate tree."""
    sandbox, _, _, candidates, metrics = _sandbox(trees, step_log)
    candidate = _tree(trees, {"solve.py": "print('secret')\n"})
    metric = _tree(trees, {"score.sh": "echo 1.0\n"})
    # The metric fails unless the box holds exactly the files of the two trees.
    command = (
        'test "$(find . -type f | LC_ALL=C sort | paste -s -d " " -)" = "./metric/score.sh ./outputs/answer.txt"'
        " || exit 1; sh metric/score.sh"
    )
    try:
        _, outputs = sandbox.run(_step(1), candidate, "rm solve.py; echo 1 > answer.txt", [])
        score = sandbox.score(_step(2), outputs, metric, command)
    finally:
        candidates.close()
        metrics.close()

    assert score == 1.0


def test_a_warm_lease_cannot_give_the_metric_boxes(trees: TreeStore, step_log: StepLogStore) -> None:
    """A warm metric box would keep the files and processes of an earlier score or candidate."""
    candidates = RuntimeBoxLease(_on_both(_Runtime()), _SPEC, **_LIMITS)
    warm = RuntimeBoxLease(_on_both(_Runtime()), _SPEC, **_LIMITS)

    with pytest.raises(ValueError, match="keep_warm=False"):
        TreeSandbox(trees, step_log, candidates, warm)
    with pytest.raises(ValueError, match="keep_warm=False"):
        TreeSandbox(trees, step_log, candidates, candidates)


def test_a_lease_must_use_the_layer_registry(trees: TreeStore, step_log: StepLogStore) -> None:
    """The step records the grant list that the layer resolves, so the lease must resolve the same way."""
    registry = {"api.example.com": {"Authorization": "x"}}

    with pytest.raises(ValueError, match="registry"):
        TreeSandbox(
            trees,
            step_log,
            RuntimeBoxLease(_on_both(_Runtime()), _SPEC, registry, **_LIMITS),
            _Provider().lease(keep_warm=False),
        )


def test_a_gpu_step_is_refused_before_any_box_opens(trees: TreeStore, step_log: StepLogStore) -> None:
    """No runtime here opens a GPU box, so a GPU grant is a denied grant, not a box without its GPU."""
    sandbox, runtime, _, candidates, metrics = _sandbox(trees, step_log)
    try:
        with pytest.raises(GrantDeniedError, match="GPU"):
            sandbox.run(_step(1), _tree(trees, {}), "true", [Grant("gpu:a100")])
    finally:
        candidates.close()
        metrics.close()

    assert runtime.boxes == []


def test_a_step_with_a_host_that_is_not_text_is_a_denied_grant(trees: TreeStore, step_log: StepLogStore) -> None:
    """The step records a denied grant, not an AttributeError, and no box opens."""
    sandbox, runtime, _, candidates, metrics = _sandbox(trees, step_log)
    try:
        with pytest.raises(GrantDeniedError):
            sandbox.run(_step(1), _tree(trees, {}), "true", [Grant(7)])  # type: ignore[arg-type]
    finally:
        candidates.close()
        metrics.close()

    assert runtime.boxes == []


def test_a_refused_gpu_step_keeps_the_warm_box_and_uses_no_start() -> None:
    """The GPU refusal comes before the claim, so the agent's warm box stays and no start is counted."""
    runtime, clock = _Runtime(), _Clock()
    lease = RuntimeBoxLease(
        _on_both(runtime), _SPEC, vercel_cap=100, starts_per_minute=2, clock=clock, sleep=clock.sleep
    )
    try:
        box = lease.acquire("agent-1", [])
        lease.release(box, healthy=True)
        with pytest.raises(GrantError, match="GPU"):
            lease.acquire("agent-1", [Grant("gpu:a100")])
        again = lease.acquire("agent-1", [])
        lease.release(again, healthy=True)
        # The budget holds two starts: the first box and this one, with no wait.
        lease.release(lease.acquire("agent-2", []), healthy=True)
    finally:
        lease.close()

    assert again is box
    assert len(runtime.boxes) == 2
    assert clock.sleeps == []


def test_a_denied_grant_list_keeps_the_warm_box_and_uses_no_start() -> None:
    """A network grant with no registered credential is refused before the claim, like a GPU grant."""
    runtime, clock = _Runtime(), _Clock()
    lease = RuntimeBoxLease(
        _on_both(runtime), _SPEC, vercel_cap=100, starts_per_minute=2, clock=clock, sleep=clock.sleep
    )
    try:
        box = lease.acquire("agent-1", [])
        lease.release(box, healthy=True)
        with pytest.raises(GrantError):
            lease.acquire("agent-1", [Grant("api.example.com", credential=True)])
        again = lease.acquire("agent-1", [])
        lease.release(again, healthy=True)
        # The budget holds two starts: the first box and this one, with no wait.
        lease.release(lease.acquire("agent-2", []), healthy=True)
    finally:
        lease.close()

    assert again is box
    assert len(runtime.boxes) == 2
    assert clock.sleeps == []


@pytest.mark.parametrize("check", [1, SandboxInfraError("The box stopped.")])
def test_a_warm_box_that_fails_its_health_check_is_replaced(check: int | Exception) -> None:
    """The runtime check runs ``true`` in the warm box; a failed or broken run opens a new box."""
    runtime = _Runtime()
    lease = RuntimeBoxLease(_on_both(runtime), _SPEC, **_LIMITS)
    try:
        lease.release(lease.acquire("agent-1", []), healthy=True)
        runtime.boxes[0].check = check
        lease.release(lease.acquire("agent-1", []), healthy=True)
    finally:
        lease.close()

    assert len(runtime.boxes) == 2
    assert runtime.boxes[0].closed


def test_a_budget_stop_in_the_health_check_reaches_the_caller() -> None:
    """A budget signal from the check stops the run instead of counting as a dead box, and the box closes."""
    runtime = _Runtime()
    lease = RuntimeBoxLease(_on_both(runtime), _SPEC, **_LIMITS)
    try:
        lease.release(lease.acquire("agent-1", []), healthy=True)
        runtime.boxes[0].check = BudgetReached()
        with pytest.raises(BudgetReached):
            lease.acquire("agent-1", [])
    finally:
        lease.close()

    assert len(runtime.boxes) == 1
    assert runtime.boxes[0].closed


def test_a_lease_needs_a_runtime_for_vercel_and_the_spill() -> None:
    """A missing runtime fails at once, not later as a box failure when Vercel reaches the cap."""
    runtime = _Runtime()

    with pytest.raises(ValueError, match="modal"):
        RuntimeBoxLease({VERCEL: runtime}, _SPEC, **_LIMITS)
    with pytest.raises(ValueError, match="vercel"):
        RuntimeBoxLease({MODAL: runtime}, _SPEC, **_LIMITS)
    with pytest.raises(ValueError, match="daytona"):
        RuntimeBoxLease(_on_both(runtime), _SPEC, spill="daytona", **_LIMITS)

    lease = RuntimeBoxLease({VERCEL: runtime, "daytona": runtime}, _SPEC, spill="daytona", **_LIMITS)
    assert lease.registry == {}
    lease.close()
    assert runtime.boxes == []


def test_a_runtime_lease_keeps_its_first_budget_error_over_a_later_budget_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every budget error stops the run, so a later BudgetReached does not replace a kept BudgetError."""
    runtime = _Runtime()
    lease = RuntimeBoxLease(_on_both(runtime), _SPEC, **_LIMITS)
    boxes = [lease.acquire(agent, []) for agent in ("agent-1", "agent-2")]
    errors: list[BaseException] = [BudgetError("over budget"), BudgetReached("budget stopped")]
    for box, error in zip(runtime.boxes, errors, strict=True):

        def fail_close(close: Any = box.close, error: BaseException = error) -> None:
            """Close the local box, then fail as a settlement does.

            Args:
                close: The box's own close.
                error: The settlement's error.

            Raises:
                BaseException: ``error``, always.
            """
            close()
            raise error

        monkeypatch.setattr(box, "close", fail_close)
    for box in boxes:
        lease.release(box, healthy=True)

    with pytest.raises(BudgetError) as raised:
        lease.close()

    assert raised.value is errors[0]
    assert all(box.closed for box in runtime.boxes)
