"""Tests for the warm box lease, its start budget and its placement (SKY-732).

The tests use fake boxes, so no test calls a paid provider or the network.
The tree sandbox's own lease tests are in the blackbox tests folder.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import threading
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest

from core.billing.signals import BudgetReached
from core.sandbox.box_lease import BoxLease, StartBudget
from core.sandbox.errors import SandboxInfraError
from core.sandbox.grants import Grant, GrantError
from core.sandbox.placement import MODAL, VERCEL, Placement

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
    """A check error reaches the caller over the close error of the dropped box, except under a budget stop.

    Args:
        check_error: The error that the health check raises.
        close_error: The error that the close of the dropped box raises.
        raised: Names the error that must reach the caller.
    """
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


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ([Grant("pypi.org")], [Grant("api.example.com")]),
        ([Grant("pypi.org")], [Grant("pypi.org", credential=True)]),
        ([Grant("pypi.org", credential=True)], [Grant("pypi.org")]),
    ],
    ids=["host", "adds-credential", "drops-credential"],
)
def test_new_grants_replace_the_warm_box(first: list[Grant], second: list[Grant]) -> None:
    """A box keeps the network policy it opened with, so another host or credential flag needs a new box.

    Args:
        first: The grant list of the first acquire.
        second: Another grant list for the second acquire.
    """
    provider = _Provider()
    lease = provider.lease()
    box = lease.acquire("agent-1", first)
    lease.release(box, healthy=True)

    again = lease.acquire("agent-1", second)

    assert box.closed
    assert again.grants == tuple(second)


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
    """Grant lists that resolve to the same policy share the warm box: host case and repeats do not matter.

    Args:
        first: The grant list of the first acquire.
        second: An equal grant list in another form, for the second acquire.
    """
    provider = _Provider()
    lease = provider.lease()
    box = lease.acquire("agent-1", first)
    lease.release(box, healthy=True)

    assert lease.acquire("agent-1", second) is box
    assert provider.opened == [box]


@pytest.mark.parametrize("host", [7, None])
def test_a_host_that_is_not_text_is_a_grant_error_and_keeps_the_warm_box(host: object) -> None:
    """A bad host is a refused grant list, not an AttributeError from the lease.

    Args:
        host: A host that is not text.
    """
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
    """A Vercel spill would count past the cap, so the cap would have no effect.

    Args:
        spill: A spill provider that is not valid.
    """
    with pytest.raises(ValueError, match="spill provider"):
        _Provider().lease(spill=spill)


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
    """A bad option fails when the lease is built, not at its first acquire.

    Args:
        option: The name of the lease option.
        value: A value that the option does not accept.
        message: A part of the expected error message.
    """
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
    """The log names only the error type, because the error text can hold a secret; close() raises the error.

    Args:
        caplog: Captures the log lines.
    """
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
    """No release can come between the kept error and the cleared reaper, so a later release starts a new reaper.

    Args:
        monkeypatch: Replaces the parts that the test makes fail or records.
    """
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
    """The reaper waits on its own event, so a release, a drop or a failed open of another agent costs it no pass.

    Args:
        monkeypatch: Replaces the parts that the test makes fail or records.
    """
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


@pytest.mark.parametrize("broken", ["clock", "wait"])
def test_a_release_right_after_a_reaper_failure_still_gets_a_reaper(
    broken: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A release in the reaper's failure log, the old gap before the reaper cleared itself, starts a new reaper.

    Args:
        broken: The reaper call that fails once, the clock or the idle wait.
        monkeypatch: Hooks the reaper's failure log.
    """
    provider = _Provider()
    ticker = _Ticker()
    failed: list[bool] = []

    def clock() -> float:
        """Return the fake time, and fail the reaper's first read when the clock is the broken call.

        Returns:
            The fake time.

        Raises:
            RuntimeError: At the reaper's first read.
        """
        if broken == "clock" and not failed and threading.current_thread().name == _REAPER:
            failed.append(True)
            raise RuntimeError("The clock broke.")
        return provider.clock.now

    def wait(seconds: float) -> None:
        """Fail the first wait when the idle wait is the broken call, else wait on the ticker.

        Args:
            seconds: The reaper's wait.

        Raises:
            RuntimeError: At the first wait.
        """
        if broken == "wait" and not failed:
            failed.append(True)
            raise RuntimeError("The wait broke.")
        ticker(seconds)

    lease = provider.lease(clock=clock, sleep_after_seconds=600, idle_wait=wait)
    first = lease.acquire("agent-1", [])
    second = lease.acquire("agent-2", [])
    reapers: list[threading.Thread | None] = []
    logged = threading.Event()
    log = logging.getLogger(BoxLease.__module__)
    warning = log.warning

    def release_in_log(message: str, *args: Any) -> None:
        """Release the second box from another thread at the failure log, then log.

        Args:
            message: The log message.
            *args: The message arguments.
        """
        if "reaper stopped" in message and not logged.is_set():
            # Joined here, so the release runs where the old reaper had not yet cleared itself.
            other = threading.Thread(target=lease.release, args=(second, True), daemon=True)
            other.start()
            other.join(timeout=5)
            reapers.append(lease._reaper)
            logged.set()
        warning(message, *args)

    monkeypatch.setattr(log, "warning", release_in_log)
    lease.release(first, healthy=True)
    failing = lease._reaper
    assert failing is not None
    try:
        assert logged.wait(timeout=5), "The reaper did not fail."
        failing.join(timeout=5)
        assert not failing.is_alive()
        new = reapers[0]
        assert new is not None, "The release in the gap started no reaper."
        assert new is not failing, "The release in the gap started no reaper."
        assert lease._reaper is new
        # Taken here, so the kept error does not reach the close in the teardown.
        with pytest.raises(RuntimeError, match="broke"):
            lease.acquire("agent-3", [])
        ticker.passes(1)
        provider.clock.now = 600.0
        ticker.tick_to_end()
        assert provider.closed == [first, second]
    finally:
        _stop(lease, ticker)


def test_a_reaper_start_failure_drops_the_box_and_the_next_acquire_opens_a_new_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the reaper thread cannot start, the release closes the box, so the step's re-run does not block.

    Args:
        monkeypatch: Replaces the parts that the test makes fail or records.
    """
    provider = _Provider()
    ticker = _Ticker()
    lease = provider.lease(sleep_after_seconds=600, idle_wait=ticker)
    box = lease.acquire("agent-1", [])
    start = threading.Thread.start
    refused: list[bool] = []

    def refuse_once(self: threading.Thread) -> None:
        """Refuse the first reaper start, as when the process has no thread left.

        Args:
            self: The thread.

        Raises:
            RuntimeError: At the first reaper start.
        """
        if self.name == _REAPER and not refused:
            refused.append(True)
            raise RuntimeError("can't start new thread")
        start(self)

    monkeypatch.setattr(threading.Thread, "start", refuse_once)
    with pytest.raises(RuntimeError, match="can't start new thread"):
        lease.release(box, healthy=True)
    try:
        assert box.closed, "A box that the failed release keeps stays open and held forever."
        assert provider.closed == [box]
        assert lease._reaper is None, "A thread that never started blocks every new reaper."
        assert not lease._leases
        assert not lease._idle

        new = _acquire_within(lease, "agent-1")
        assert new is not box
        assert provider.opened == [box, new]
        lease.release(new, healthy=True)
        ticker.passes(1)
        assert lease._reaper is not None
        assert lease._reaper.is_alive()
    finally:
        _stop(lease, ticker)
    assert new.closed


def test_a_clock_failure_in_a_release_drops_the_box_and_the_next_acquire_opens_a_new_one() -> None:
    """A release whose clock read fails closes the box, so the step's re-run opens a new box and does not block."""
    provider = _Provider()
    broken: list[bool] = []

    def clock() -> float:
        """Return the fake time, or fail while the test breaks the clock.

        Returns:
            The fake time.

        Raises:
            RuntimeError: While the clock is broken.
        """
        if broken:
            raise RuntimeError("The clock broke.")
        return provider.clock.now

    ticker = _Ticker()
    lease = provider.lease(clock=clock, sleep_after_seconds=600, idle_wait=ticker)
    box = lease.acquire("agent-1", [])
    broken.append(True)
    with pytest.raises(RuntimeError, match="The clock broke"):
        lease.release(box, healthy=True)
    broken.clear()
    try:
        assert box.closed, "A box that the failed release keeps stays open and held forever."
        assert provider.closed == [box]
        assert not lease._leases
        assert not lease._idle
        assert lease._reaper is None

        new = _acquire_within(lease, "agent-1")
        assert new is not box
        assert provider.opened == [box, new]
        lease.release(new, healthy=True)
        ticker.passes(1)
        provider.clock.now = 600.0
        ticker.tick_to_end()
        assert new.closed
    finally:
        _stop(lease, ticker)


@pytest.mark.parametrize(
    ("release_error", "close_error", "raised"),
    [
        (RuntimeError("release"), BudgetReached("close"), "close"),
        (RuntimeError("release"), RuntimeError("close"), "release"),
        (BudgetReached("release"), RuntimeError("close"), "release"),
        (BudgetReached("release"), BudgetReached("close"), "release"),
    ],
)
def test_a_close_error_after_a_failed_healthy_release_hides_it_only_when_it_is_a_stop(
    release_error: BaseException, close_error: BaseException, raised: str
) -> None:
    """A failed healthy release raises its own error over the box's close error, except under a budget stop.

    Args:
        release_error: The error that the healthy release raises.
        close_error: The error that the close of the dropped box raises.
        raised: Names the error that must reach the caller.
    """
    provider = _Provider()
    broken: list[bool] = []

    def clock() -> float:
        """Return the fake time, or raise the release error while the test breaks the clock.

        Returns:
            The fake time.

        Raises:
            BaseException: The release error, while the clock is broken.
        """
        if broken:
            raise release_error
        return provider.clock.now

    def close(box: _Box) -> None:
        """Raise the close error.

        Args:
            box: The box.

        Raises:
            BaseException: The close error.
        """
        raise close_error

    lease = BoxLease(provider.open, close, clock=clock, sleep=provider.clock.sleep, **_LIMITS)
    box = lease.acquire("agent-1", [])
    broken.append(True)
    with pytest.raises(BaseException) as caught:
        lease.release(box, healthy=True)
    broken.clear()

    assert caught.value is {"release": release_error, "close": close_error}[raised]
    assert not lease._leases
    assert not lease._idle
    assert lease._reaper is None
    assert lease._open[VERCEL] == 0


def test_close_closes_an_idle_box_in_the_context_of_its_last_release() -> None:
    """close() runs each idle box close in its release's context, as the reaper does, not in the closer's."""
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
    token = _RUN_SCOPE.set("step-1")
    try:
        lease.release(box, healthy=True)
    finally:
        _RUN_SCOPE.reset(token)
    ticker.passes(1)
    token = _RUN_SCOPE.set("closer")
    try:
        _stop(lease, ticker)
    finally:
        _RUN_SCOPE.reset(token)

    assert box.closed
    assert seen == ["step-1"]


@pytest.mark.parametrize("seconds", [0, -1, True, None, "600", float("inf"), float("nan"), 1e10])
def test_the_lease_refuses_a_bad_sleep_after_time(seconds: Any) -> None:
    """A bad idle time fails when the lease is built, like a bad start budget.

    Args:
        seconds: A sleep-after time that is not valid.
    """
    with pytest.raises(ValueError, match="sleep-after"):
        _Provider().lease(sleep_after_seconds=seconds)
