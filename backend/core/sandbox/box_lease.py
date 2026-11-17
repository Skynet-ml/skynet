"""Keep one warm box for each agent, placed by grants and quota, under a start budget (SKY-732).

:class:`BoxLease` is a :class:`~core.sandbox.trees.BoxSource`. ``acquire``
gives the agent its warm box, and a healthy ``release`` keeps the box for the
agent's next step. A box stays disposable: an unhealthy release, a failed
health check or a new grant list drops the box, and the next acquire opens a
new one. The step log stays the truth, so the step re-runs from its last
committed tree.

:mod:`core.sandbox.placement` picks the provider of each new box, and
:class:`StartBudget` keeps box starts under the configured starts per minute,
so recovery after a mass failure ramps up within that budget. The Vercel cap
and the start budget have no defaults: the real Vercel team limits are not
confirmed, so each caller passes them. Both count per lease, in one process;
they do not see the boxes and starts of other leases or workers.
"""

from __future__ import annotations

import threading
import time
from collections import Counter, deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar, cast

from .grants import Grant, _merge
from .placement import MODAL, VERCEL, Placement, Quota, choose, gpu_type, network_grants

BoxT = TypeVar("BoxT")

_WINDOW_SECONDS = 60.0


class StartBudget:
    """Allow at most a number of box starts in any 60-second window, and wait for the next free start."""

    def __init__(
        self,
        starts_per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Set the budget.

        Args:
            starts_per_minute: The most starts in any 60-second window.
            clock: Gives the monotonic time in seconds.
            sleep: Waits a number of seconds.

        Raises:
            ValueError: When the budget is not a positive whole number.
        """
        if isinstance(starts_per_minute, bool) or not isinstance(starts_per_minute, int) or starts_per_minute < 1:
            raise ValueError("The start budget must be a positive whole number of starts per minute.")
        self._limit = starts_per_minute
        self._clock = clock
        self._sleep = sleep
        self._starts: deque[float] = deque()
        self._lock = threading.Lock()

    def take(self, stop: Callable[[], bool] = lambda: False) -> bool:
        """Wait until a start fits in the window, then count it.

        Args:
            stop: Returns True when the caller no longer wants the start; checked before each count.

        Returns:
            True when a start is counted, False when ``stop`` ends the wait with no start counted.
        """
        while True:
            with self._lock:
                if stop():
                    return False
                now = self._clock()
                while self._starts and self._starts[0] <= now - _WINDOW_SECONDS:
                    self._starts.popleft()
                if len(self._starts) < self._limit:
                    self._starts.append(now)
                    return True
                wait = self._starts[0] + _WINDOW_SECONDS - now
            # The sleep is outside the lock, so other callers can count the window while this one waits.
            self._sleep(wait)


_GrantKey = tuple[str | None, frozenset[tuple[str, bool]]]


def _grant_key(grants: Sequence[Grant]) -> _GrantKey:
    """Return the GPU type and the normalized network grants, so equal grant lists share a warm box.

    Args:
        grants: The box's grants.

    Returns:
        The GPU type, and each normalized host with its merged credential flag.

    Raises:
        GrantError: When a GPU grant or a host is not valid.
    """
    # The same host normalization and credential merge as resolve_grants, so its grant-list id agrees.
    return gpu_type(grants), frozenset(_merge(network_grants(grants)).items())


@dataclass
class _Lease(Generic[BoxT]):
    """Hold one key's box, the grants it opened with, its provider, and whether a caller holds it."""

    grants: _GrantKey
    placement: Placement
    box: BoxT | None = None
    busy: bool = True


class BoxLease(Generic[BoxT]):
    """Give each agent its warm box, and replace the box when it fails."""

    def __init__(
        self,
        open_box: Callable[[Placement, Sequence[Grant]], BoxT],
        close_box: Callable[[BoxT], None],
        *,
        vercel_cap: int,
        starts_per_minute: int,
        check: Callable[[BoxT], bool] | None = None,
        keep_warm: bool = True,
        spill: str = MODAL,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        """Bind the lease to a box opener and closer.

        Args:
            open_box: Opens a new box on a placement with the grants.
            close_box: Closes a box for good.
            vercel_cap: The most Vercel boxes this lease keeps open; more spill.
                It counts only this lease's boxes.
            starts_per_minute: The most box starts of this lease in any 60-second window.
            check: Returns False when a warm box is no longer good; ``None`` skips the check.
            keep_warm: False closes every box at its release, so no box serves two acquires.
            spill: The provider that takes the boxes over the Vercel cap.
            clock: Gives the monotonic time in seconds.
            sleep: Waits a number of seconds for a start; ``None`` waits on the
                lease, so :meth:`close` ends the wait at once.

        Raises:
            ValueError: When the cap or the spill provider is not valid, so a bad lease fails here.
            TypeError: When keep_warm is not a bool, or a function option is not callable.
        """
        if isinstance(vercel_cap, bool) or not isinstance(vercel_cap, int) or vercel_cap < 0:
            raise ValueError("The Vercel cap must be a whole number of 0 or more.")
        if not isinstance(spill, str) or not spill or spill == VERCEL:
            # A Vercel spill would count past the cap, so the cap would have no effect.
            raise ValueError("The spill provider must be a provider name other than Vercel.")
        if not isinstance(keep_warm, bool):
            raise TypeError("keep_warm must be True or False.")
        if not all(callable(function) for function in (open_box, close_box, clock)):
            raise TypeError("The opener, the closer and the clock must be callable.")
        if check is not None and not callable(check):
            raise TypeError("The check must be callable or None.")
        if sleep is not None and not callable(sleep):
            raise TypeError("The sleep must be callable or None.")
        self._open_box = open_box
        self._close_box = close_box
        self._check = check
        self._keep_warm = keep_warm
        self._vercel_cap = vercel_cap
        self._spill = spill
        self._leases: dict[str, _Lease[BoxT]] = {}
        # Box identity to its key: a box need not be hashable, and the lease holds it while the entry exists.
        self._holders: dict[int, str] = {}
        self._open: Counter[str] = Counter()
        self._closed = False
        self._cond = threading.Condition()
        self._budget = StartBudget(starts_per_minute, clock=clock, sleep=sleep or self._wait_for_start)

    @property
    def keeps_warm(self) -> bool:
        """Return True when a healthy release keeps the box for the next acquire."""
        return self._keep_warm

    def acquire(self, agent_id: str, grants: Sequence[Grant]) -> BoxT:
        """Return the agent's warm box, or open a new one.

        Args:
            agent_id: The agent.
            grants: The box's grants; a warm box with other grants is replaced.

        Returns:
            The box. The caller holds it until :meth:`release`.

        Raises:
            RuntimeError: When the lease is closed.
            GrantError: When a GPU grant or a host is not valid, or the open
                egress grant asks for a credential.
            Exception: The opener's, the check's or the closer's error, raw.
        """
        # Checked before any lease changes, so a bad grant list leaves the agent's warm box in place.
        wanted = _grant_key(grants)
        key = agent_id
        while True:
            lease, stale = self._claim(key, grants, wanted)
            if lease.box is None:
                break
            try:
                if self._check is None or self._check(lease.box):
                    return lease.box
            except BaseException:
                self.release(lease.box, healthy=False)
                raise
            self.release(lease.box, healthy=False)
        try:
            # Inside the try, so a close error still frees the reserved key for the agent's next acquire.
            if stale is not None:
                self._close_box(stale)
            # A close ends the budget wait with no start counted, so queued acquires do not hold threads.
            started = self._budget.take(stop=lambda: self._closed)
            # The close and the budget wait run outside the lock, so the lease can close before the open.
            with self._cond:
                if not started or self._closed:
                    raise RuntimeError("The box lease is closed.")
            box = self._open_box(lease.placement, grants)
        except BaseException:
            with self._cond:
                del self._leases[key]
                self._open[lease.placement.provider] -= 1
                self._cond.notify_all()
            raise
        with self._cond:
            lease.box = box
            self._holders[id(box)] = key
        return box

    def release(self, box: BoxT, healthy: bool) -> None:
        """Keep a healthy box warm for its key, or close and drop it.

        Args:
            box: A box that :meth:`acquire` gave.
            healthy: False when the box failed, so the next acquire opens a new box.

        Raises:
            ValueError: When this lease does not hold the box, or no caller holds it.
            Exception: The closer's error, raw.
        """
        with self._cond:
            key = self._holders.get(id(box))
            if key is None:
                raise ValueError("This lease does not hold the box.")
            lease = self._leases[key]
            if not lease.busy:
                # A second release would mark the box idle while another caller holds it.
                raise ValueError("No caller holds the box.")
            if healthy and self._keep_warm and not self._closed:
                lease.busy = False
                self._cond.notify_all()
                return
            self._drop(key)
        self._close_box(box)

    def close(self) -> None:
        """Close every idle box and stop new acquires; a held box closes at its release.

        Raises:
            Exception: The first closer error, after every idle box had its close.
        """
        with self._cond:
            self._closed = True
            # Wakes the acquires that wait for a start, also when no idle box drops below.
            self._cond.notify_all()
            idle = [key for key, lease in self._leases.items() if not lease.busy]
            boxes = [self._drop(key) for key in idle]
        error: BaseException | None = None
        for box in boxes:
            try:
                self._close_box(box)
            except BaseException as exc:
                error = error or exc
        if error is not None:
            raise error

    def _wait_for_start(self, seconds: float) -> None:
        """Wait for a free start on the lease, and wake at once when the lease closes.

        Args:
            seconds: The longest wait.
        """
        with self._cond:
            # Checked under the lock, so a close between the budget's check and this wait still wakes it.
            if not self._closed:
                self._cond.wait(seconds)

    def _claim(self, key: str, grants: Sequence[Grant], wanted: _GrantKey) -> tuple[_Lease[BoxT], BoxT | None]:
        """Take the key's warm box, or reserve a new box and its provider.

        Args:
            key: The agent id.
            grants: The box's grants.
            wanted: The normalized grants; a warm box with other grants is replaced.

        Returns:
            The claimed lease, whose box is ``None`` when the caller must open
            it, and a warm box with other grants that the caller must close.

        Raises:
            RuntimeError: When the lease is closed.
        """
        with self._cond:
            while key in self._leases and self._leases[key].busy:
                self._cond.wait()
            if self._closed:
                raise RuntimeError("The box lease is closed.")
            lease = self._leases.get(key)
            if lease is not None and lease.grants == wanted:
                lease.busy = True
                return lease, None
            stale = self._drop(key) if lease is not None else None
            placement = choose(grants, Quota(self._vercel_cap, self._open[VERCEL], self._spill))
            # Counted now, not after the open, so callers that open at the same time cannot pass the cap.
            self._open[placement.provider] += 1
            lease = _Lease(wanted, placement)
            self._leases[key] = lease
            return lease, stale

    def _drop(self, key: str) -> BoxT:
        """Remove an open box from the table; the caller holds the condition lock.

        Args:
            key: The key of a lease whose box is open.

        Returns:
            The box, for the caller to close.
        """
        lease = self._leases.pop(key)
        box = cast(BoxT, lease.box)
        del self._holders[id(box)]
        self._open[lease.placement.provider] -= 1
        self._cond.notify_all()
        return box
