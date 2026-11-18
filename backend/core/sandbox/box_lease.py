"""Keep one warm box for each run's agent, placed by grants and quota, under a start budget (SKY-732).

:class:`BoxLease` is a :class:`~core.sandbox.trees.BoxSource`. ``acquire``
gives the agent its warm box, and a healthy ``release`` keeps the box for the
agent's next step. A box stays disposable: an unhealthy release, a failed
health check, a new grant list, or a healthy release whose clock read or
reaper start fails drops the box, and the next acquire opens a new one. The
step log stays the truth, so the step re-runs from its last committed tree.

A warm box sleeps when it is idle, as a Cloudflare sandbox does after its
``sleepAfter`` time (default 10 minutes). An acquire holds the box, so a busy
box never sleeps, and each healthy release starts its idle time again. When no
caller holds a box for ``sleep_after_seconds``, a reaper thread closes the box
and drops it from the lease, also when no other call comes to the lease. The
box state is lost by design: the next acquire for the agent opens a new box
through placement and the start budget, as after an unhealthy release.
The reaper and :meth:`BoxLease.close` close an idle box in the context of
the box's last healthy release, so the settlement sees that caller's usage
and log scopes. The reaper looks only at the oldest idle box, wakes only for
its deadline or a close, and ends when no idle box is left; the next healthy
release starts a new one.
:meth:`BoxLease.close` stops the reaper. The reaper keeps its first error,
and a budget stop from settlement replaces an error of another type, so it
stops the run. The next ``acquire`` or ``close`` raises the kept error once.
An error outside a box close ends the reaper, and the next warm box starts
a new one.

:mod:`core.sandbox.placement` picks the provider of each new box, and
:class:`StartBudget` keeps box starts under the configured starts per minute,
so recovery after a mass failure ramps up within that budget. The Vercel cap
and the start budget have no defaults: the real Vercel team limits are not
confirmed, so each caller passes them. Both count per lease, in one process;
they do not see the boxes and starts of other leases or workers.
"""

from __future__ import annotations

import contextvars
import logging
import math
import threading
import time
from collections import Counter, OrderedDict, deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import ClassVar, Generic, TypeVar, cast

from ..billing.signals import BudgetReached
from .grants import Grant, merge_grants
from .placement import MODAL, VERCEL, Placement, Quota, choose, gpu_type, network_grants

logger = logging.getLogger(__name__)

BoxT = TypeVar("BoxT")

_WINDOW_SECONDS = 60.0
# The Cloudflare Sandbox SDK's sleepAfter default.
SLEEP_AFTER_SECONDS = 600.0


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
    return gpu_type(grants), frozenset(merge_grants(network_grants(grants)).items())


@dataclass
class _Lease(Generic[BoxT]):
    """Hold one key's box, the grants it opened with, its provider, whether a caller holds it, and its last release."""

    grants: _GrantKey
    placement: Placement
    box: BoxT | None = None
    busy: bool = True
    released_at: float = 0.0
    # The context of the last healthy release, for the reaper's close.
    context: contextvars.Context = field(default_factory=contextvars.Context)


class BoxLease(Generic[BoxT]):
    """Give each box key (a run and its agent) its warm box, and replace the box when it fails."""

    # The reaper errors that stop the run; one of them replaces a kept error of another type.
    stop_errors: ClassVar[tuple[type[BaseException], ...]] = (BudgetReached,)

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
        sleep_after_seconds: float = SLEEP_AFTER_SECONDS,
        idle_wait: Callable[[float], None] | None = None,
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
            sleep_after_seconds: The idle time after which a warm box closes.
            idle_wait: The reaper's wait of up to a number of seconds; ``None``
                waits on an event that only :meth:`close` sets, so a release
                does not wake the reaper and close ends the wait at once. A
                custom wait must end soon after :meth:`close`, because close
                waits for the reaper.

        Raises:
            ValueError: When the cap, the spill provider or the idle time is not valid, so a bad lease fails here.
            TypeError: When keep_warm is not a bool, or a function option is not callable.
        """
        if (
            isinstance(sleep_after_seconds, bool)
            or not isinstance(sleep_after_seconds, int | float)
            or not math.isfinite(sleep_after_seconds)
            or sleep_after_seconds <= 0
            # A longer wait makes Condition.wait raise OverflowError in the reaper.
            or sleep_after_seconds > threading.TIMEOUT_MAX
        ):
            raise ValueError(
                "The sleep-after time must be a positive number of seconds of at most threading.TIMEOUT_MAX."
            )
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
        if idle_wait is not None and not callable(idle_wait):
            raise TypeError("The idle wait must be callable or None.")
        self._open_box = open_box
        self._close_box = close_box
        self._check = check
        self._keep_warm = keep_warm
        self._vercel_cap = vercel_cap
        self._spill = spill
        self._leases: dict[str, _Lease[BoxT]] = {}
        # The idle keys in release order. Each release sets the latest idle time, so the first key sleeps first.
        self._idle: OrderedDict[str, None] = OrderedDict()
        # Box identity to its key: a box need not be hashable, and the lease holds it while the entry exists.
        self._holders: dict[int, str] = {}
        self._open: Counter[str] = Counter()
        self._closed = False
        self._cond = threading.Condition()
        self._budget = StartBudget(starts_per_minute, clock=clock, sleep=sleep or self._wait_on_lease)
        self._clock = clock
        self._sleep_after = float(sleep_after_seconds)
        # Its own event, not the lease condition, so a release or drop does not wake the reaper for a scan.
        self._reaper_wake = threading.Event()
        self._idle_wait = idle_wait or self._reaper_wake.wait
        # Started at the first warm box and ended when no box is idle, so a lease with no idle box runs no thread.
        self._reaper: threading.Thread | None = None
        # The reaper's first close error, for the next acquire or close to raise.
        self._reap_error: BaseException | None = None

    @property
    def keeps_warm(self) -> bool:
        """Return True when a healthy release keeps the box for the next acquire."""
        return self._keep_warm

    def acquire(self, agent_id: str, grants: Sequence[Grant]) -> BoxT:
        """Return the key's warm box, or open a new one.

        Args:
            agent_id: The box key. The caller builds it from the run and the
                agent (``run_id:agent_id``), so two runs never share a box.
            grants: The box's grants; a warm box with other grants is replaced.

        Returns:
            The box. The caller holds it until :meth:`release`.

        Raises:
            RuntimeError: When the lease is closed.
            GrantError: When a GPU grant or a host is not valid, or the open
                egress grant asks for a credential.
            Exception: The opener's, the check's or the closer's error, raw;
                also the reaper's first close error, once.
        """
        # A budget stop from an idle box's settlement must stop the run, not open a new box.
        reaped = self._take_reap_error()
        if reaped is not None:
            raise reaped
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
            except BaseException as error:
                try:
                    self.release(lease.box, healthy=False)
                except BaseException as release_error:
                    # A close error must not hide a budget stop from the check; only a stop error wins.
                    if isinstance(release_error, self.stop_errors) and not isinstance(error, self.stop_errors):
                        raise
                raise
            self.release(lease.box, healthy=False)
        try:
            # Inside the try, so a close error still frees the reserved key for the agent's next acquire.
            if stale is not None:
                self._close_and_free(*stale)
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
            Exception: The closer's error, raw. In a healthy release, also the
                clock's error or the reaper thread's start error, such as
                ``RuntimeError("can't start new thread")``; the release then
                closes and drops the box, as an unhealthy release does.
        """
        error: BaseException | None = None
        with self._cond:
            key = self._holders.get(id(box))
            if key is None:
                raise ValueError("This lease does not hold the box.")
            lease = self._leases[key]
            if not lease.busy:
                # A second release would mark the box idle while another caller holds it.
                raise ValueError("No caller holds the box.")
            if healthy and self._keep_warm and not self._closed:
                # Only an idle box sleeps, so the release starts its idle time; an acquire holds the box.
                try:
                    released_at = self._clock()
                    if self._reaper is None:
                        reaper = threading.Thread(target=self._reap, name="box-lease-reaper", daemon=True)
                        # The reaper waits for this lock, so it sees the idle box set below.
                        reaper.start()
                        self._reaper = reaper
                except BaseException as exc:
                    # The caller does not retry a failed release, so a box left held would block the agent's
                    # next acquire forever and stay open. A box is disposable, so the release drops it.
                    error = exc
                else:
                    lease.busy = False
                    lease.released_at = released_at
                    lease.context = contextvars.copy_context()
                    self._idle[key] = None
                    self._cond.notify_all()
                    return
            _, provider = self._drop(key)
        if error is None:
            self._close_and_free(box, provider)
            return
        try:
            self._close_and_free(box, provider)
        except BaseException as close_error:
            # The same rule as in acquire: a close error replaces the release error only when it stops the run.
            if isinstance(close_error, self.stop_errors) and not isinstance(error, self.stop_errors):
                raise
        raise error

    def close(self) -> None:
        """Close every idle box, stop the reaper and stop new acquires; a held box closes at its release.

        Raises:
            Exception: After every idle box had its close, the first error of
                the reaper (that no acquire raised) and of these closes; a stop
                error, such as a budget stop, wins over an error of another type.
        """
        with self._cond:
            self._closed = True
            # Wakes the acquires that wait for a start, also when no idle box drops below.
            self._cond.notify_all()
            self._reaper_wake.set()
            boxes: list[tuple[BoxT, str, contextvars.Context]] = []
            for key in list(self._idle):
                # Read before the drop, which removes the lease.
                context = self._leases[key].context
                boxes.append((*self._drop(key), context))
            reaper = self._reaper
        # Joined outside the lock, because the reaper takes the lock before it sees the close.
        if reaper is not None and reaper is not threading.current_thread():
            reaper.join()
        for box, provider, context in boxes:
            try:
                # The release's context, as in the reaper, so the settlement records in that caller's scopes.
                context.run(self._close_and_free, box, provider)
            except BaseException as exc:
                # The same rule as the reaper's, so a budget stop here wins over a box error that the reaper kept.
                self._keep_reap_error(exc)
        # Taken after the join, so a close error from the reaper's last pass still reaches the caller.
        error = self._take_reap_error()
        if error is not None:
            raise error

    def _reap(self) -> None:
        """Run the reaper thread, keep the error that ends it, and let the next warm box start a new reaper.

        This is the reaper thread, like the alarm that puts a Cloudflare
        container to sleep after its ``sleepAfter`` time with no request.
        """
        error = self._reap_while_idle()
        if error is not None:
            # Logged only after the reaper cleared itself, so the log holds no lock and leaves no gap.
            logger.warning("The box lease reaper stopped: %s", type(error).__name__)

    def _reap_while_idle(self) -> BaseException | None:
        """Close each idle box whose idle time passed sleep_after, until the lease closes or no box is idle.

        Returns:
            The clock or idle wait error that ended the reaper, or ``None``.
        """
        while True:
            with self._cond:
                if self._closed or not self._idle:
                    # Cleared in the same lock block as the check, so the next healthy release starts a new reaper.
                    self._reaper = None
                    return None
                try:
                    now = self._clock()
                except BaseException as exc:
                    return self._end_reaper(exc)
                boxes: list[tuple[BoxT, str, contextvars.Context]] = []
                # Release order is idle-time order, so the scan stops at the first box that is still in its time.
                wait = 0.0
                while self._idle:
                    key = next(iter(self._idle))
                    lease = self._leases[key]
                    wait = lease.released_at + self._sleep_after - now
                    if wait > 0:
                        break
                    boxes.append((*self._drop(key), lease.context))
            for box, provider, context in boxes:
                try:
                    # The release's context, so the settlement records its charges, usage and log in that scope.
                    context.run(self._close_and_free, box, provider)
                except BaseException as exc:
                    # The error text can hold a secret.
                    logger.warning("The idle box close failed: %s", type(exc).__name__)
                    self._keep_reap_error(exc)
            # A slow close moves the clock, so the next deadline comes from a new pass, not this wait.
            # With no idle box left, the next pass ends the reaper.
            if boxes:
                continue
            try:
                self._idle_wait(wait)
            except BaseException as exc:
                with self._cond:
                    return self._end_reaper(exc)

    def _end_reaper(self, error: BaseException) -> BaseException:
        """Keep the error and clear the reaper field; the caller holds the condition lock.

        A broken clock or idle wait would fail again at once, so the thread
        ends and the next healthy release starts a new reaper. The error and
        the cleared field share the caller's lock block: a release between
        them would see a live reaper, start none, and leave its box with no
        reaper.

        Args:
            error: The error that ends the reaper.

        Returns:
            The error, for the reaper to log after it gives up the lock.
        """
        self._keep_reap_error(error)
        self._reaper = None
        return error

    def _keep_reap_error(self, error: BaseException) -> None:
        """Keep the reaper's first error, or a stop error in place of a kept error of another type.

        Args:
            error: The reaper's new error.
        """
        with self._cond:
            kept = self._reap_error
            if kept is None or (isinstance(error, self.stop_errors) and not isinstance(kept, self.stop_errors)):
                self._reap_error = error

    def _take_reap_error(self) -> BaseException | None:
        """Return the reaper's kept close error and clear it, so it is raised once.

        Returns:
            The error, or ``None`` when the reaper kept none.
        """
        with self._cond:
            error, self._reap_error = self._reap_error, None
        return error

    def _wait_on_lease(self, seconds: float) -> None:
        """Wait on the lease for a release, and wake at once when the lease closes.

        Args:
            seconds: The longest wait.
        """
        with self._cond:
            # Checked under the lock, so a close between the budget's check and this wait still wakes it.
            if not self._closed:
                self._cond.wait(seconds)

    def _claim(
        self, key: str, grants: Sequence[Grant], wanted: _GrantKey
    ) -> tuple[_Lease[BoxT], tuple[BoxT, str] | None]:
        """Take the key's warm box, or reserve a new box and its provider.

        Args:
            key: The box key, built from the run and the agent (``run_id:agent_id``).
            grants: The box's grants.
            wanted: The normalized grants; a warm box with other grants is replaced.

        Returns:
            The claimed lease, whose box is ``None`` when the caller must open
            it, and a warm box with other grants and its provider, for the
            caller to give to :meth:`_close_and_free`.

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
                self._idle.pop(key, None)
                return lease, None
            stale = self._drop(key) if lease is not None else None
            placement = choose(grants, Quota(self._vercel_cap, self._open[VERCEL], self._spill))
            # Counted now, not after the open, so callers that open at the same time cannot pass the cap.
            self._open[placement.provider] += 1
            lease = _Lease(wanted, placement)
            self._leases[key] = lease
            return lease, stale

    def _drop(self, key: str) -> tuple[BoxT, str]:
        """Remove an open box from the table; the caller holds the condition lock.

        Args:
            key: The key of a lease whose box is open.

        Returns:
            The box and its provider, for the caller to give to :meth:`_close_and_free`.
        """
        lease = self._leases.pop(key)
        self._idle.pop(key, None)
        box = cast(BoxT, lease.box)
        del self._holders[id(box)]
        self._cond.notify_all()
        return box, lease.placement.provider

    def _close_and_free(self, box: BoxT, provider: str) -> None:
        """Close a dropped box, then free its place in the provider's open count.

        Args:
            box: A box that :meth:`_drop` removed.
            provider: The box's provider.

        Raises:
            Exception: The closer's error, raw.
        """
        try:
            self._close_box(box)
        finally:
            # Freed only after the close, so a slow close still counts against the Vercel cap.
            with self._cond:
                self._open[provider] -= 1
