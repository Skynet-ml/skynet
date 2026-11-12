"""In-run notification triggers (new best, stuck, near budget, needs input) for one job.

The worker feeds every persisted progress event and periodic spend reading of
a run to one :class:`RunNotificationWatcher`. The watcher emits each event
kind through :func:`core.notifications.dispatch.notify` at most once per run: the once-only guard is a durable claim on the job row
(``JobStore.claim_job_notification``), not process memory, so a resumed or
orphan-recovered run never re-sends what an earlier attempt already sent.
Only the trigger bookkeeping (best score, evaluations since improvement) is
in memory; after a restart it re-baselines from the next events, which can
delay a stuck event but never duplicate one.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from typing import Any

from ..constants import PROGRESS_CANDIDATE, TQDM_N_KEY, TQDM_TOTAL_KEY
from .catalog import BUDGET_NEAR, NEEDS_INPUT, NEW_BEST, STUCK
from .notifier import notify_run_budget, notify_run_new_best, notify_run_stuck
from .preferences import NotificationSettings, notification_settings

logger = logging.getLogger(__name__)

# Spend is read from the billing ledger, so the worker polls it on this cadence
# instead of on every poll tick.
SPEND_CHECK_INTERVAL_SECONDS = 30.0


def _dispatch_in_background(send: Callable[..., None], *args: Any, **kwargs: Any) -> None:
    """Emit one event on a daemon thread so SMTP latency never stalls the worker poll loop.

    Args:
        send: Notifier function to run.
        *args: Positional arguments for ``send``.
        **kwargs: Keyword arguments for ``send``.
    """
    threading.Thread(target=send, args=args, kwargs=kwargs, name="run-notify", daemon=True).start()


def _finite_number(value: Any) -> float | None:
    """Return ``value`` as a float when it is a finite non-bool number.

    Args:
        value: Candidate metric value.

    Returns:
        The float value, or ``None`` for anything else.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


class RunNotificationWatcher:
    """Decide and emit the in-run notifications of one job."""

    def __init__(
        self,
        *,
        optimization_id: str,
        username: str,
        job_store: Any,
        settings: NotificationSettings,
        dispatch: Callable[..., None] = _dispatch_in_background,
    ) -> None:
        """Bind the watcher to a run.

        Args:
            optimization_id: The job the user watches (a grid parent for pairs).
            username: Owner and recipient.
            job_store: Store providing ``claim_job_notification``.
            settings: The owner's notification routing.
            dispatch: ``(send, *args, **kwargs)`` runner for one event.
        """
        self._optimization_id = optimization_id
        self._username = username
        self._job_store = job_store
        self._settings = settings
        self._dispatch = dispatch
        self._best: float | None = None
        self._start: float | None = None
        self._last_improvement_calls = 0
        self._done_calls = 0
        self._planned_calls: int | None = None
        self._next_spend_check = 0.0

    @classmethod
    def for_run(cls, optimization_id: str, username: str | None, job_store: Any) -> RunNotificationWatcher | None:
        """Build a watcher when the owner routes any in-run event anywhere.

        Args:
            optimization_id: The job the user watches.
            username: Owner and recipient; ``None`` disables the watcher.
            job_store: Store providing ``claim_job_notification``.

        Returns:
            A watcher, or ``None`` when no in-run event could ever reach the owner.
        """
        if not username:
            return None
        settings = notification_settings(username)
        if not any(settings.any_enabled(event) for event in (NEW_BEST, STUCK, BUDGET_NEAR, NEEDS_INPUT)):
            return None
        return cls(optimization_id=optimization_id, username=username, job_store=job_store, settings=settings)

    def _wants(self, event_type: str) -> bool:
        """Whether the owner routes ``event_type`` anywhere.

        Args:
            event_type: Catalog event type.

        Returns:
            ``True`` when at least one grid cell for the event is on.
        """
        return self._settings.any_enabled(event_type)

    def _claim(self, claim: str, *, cap_prefix: str | None = None, cap: int | None = None) -> bool:
        """Win the durable once-per-run claim for one event kind.

        Args:
            claim: Event kind identifier stored on the job row.
            cap_prefix: Prefix counted against ``cap``.
            cap: Maximum claims with ``cap_prefix`` for this run.

        Returns:
            Whether this caller should emit the event. A store without claim
            support, or a failing one, sends nothing rather than risk repeats.
        """
        claim_fn = getattr(self._job_store, "claim_job_notification", None)
        if not callable(claim_fn):
            return False
        try:
            return bool(claim_fn(self._optimization_id, claim, cap_prefix=cap_prefix, cap=cap))
        except Exception:
            logger.warning("Run notification claim %s failed for %s", claim, self._optimization_id, exc_info=True)
            return False

    def observe_progress(self, event_name: Any, metrics: Any) -> None:
        """Feed one persisted progress event; never raises.

        Args:
            event_name: Progress event discriminator.
            metrics: The event's metrics mapping.
        """
        if not isinstance(metrics, dict):
            return
        try:
            self._observe(event_name, metrics)
        except Exception:
            logger.warning("Run notification check failed for %s", self._optimization_id, exc_info=True)

    def _observe(self, event_name: Any, metrics: dict[str, Any]) -> None:
        """Update trigger state from one event and send whatever it unlocks.

        Args:
            event_name: Progress event discriminator.
            metrics: The event's metrics mapping.
        """
        settings = self._settings
        total = metrics.get(TQDM_TOTAL_KEY)
        if isinstance(total, int) and not isinstance(total, bool) and total > 0:
            self._planned_calls = total
            done = metrics.get(TQDM_N_KEY)
            if isinstance(done, int) and not isinstance(done, bool) and done > self._done_calls:
                self._done_calls = done

        if event_name == PROGRESS_CANDIDATE:
            self._observe_candidate(metrics)

        planned = self._planned_calls
        if (
            planned
            and self._best is not None
            and self._done_calls - self._last_improvement_calls >= settings.stuck_fraction * planned
            and self._wants(STUCK)
            and self._claim("stuck")
        ):
            self._dispatch(notify_run_stuck, self._optimization_id, self._username, self._best, settings.stuck_fraction)

    def _observe_candidate(self, metrics: dict[str, Any]) -> None:
        """Track the best score and emit the first improvement over the starting one.

        Args:
            metrics: A ``candidate`` event's metrics.
        """
        score = _finite_number(metrics.get("score"))
        if score is None:
            return
        discovered = metrics.get("discovered_at_evals")
        if isinstance(discovered, int) and not isinstance(discovered, bool) and discovered > self._done_calls:
            self._done_calls = discovered
        if self._best is None:
            # The first scored version (the seed, or the incumbent after a
            # resume) is the reference point, not an improvement.
            self._best = score
            self._start = score
            self._last_improvement_calls = self._done_calls
            return
        if score <= self._best:
            return
        self._best = score
        self._last_improvement_calls = self._done_calls
        if self._wants(NEW_BEST) and self._claim("new_best"):
            self._dispatch(notify_run_new_best, self._optimization_id, self._username, score, self._start)

    def spend_check_due(self) -> bool:
        """Return whether the worker should read the run's spend now (rate-limited).

        Returns:
            ``True`` at most once per :data:`SPEND_CHECK_INTERVAL_SECONDS`.
        """
        if not self._wants(BUDGET_NEAR):
            return False
        now = time.monotonic()
        if now < self._next_spend_check:
            return False
        self._next_spend_check = now + SPEND_CHECK_INTERVAL_SECONDS
        return True

    def observe_spend(self, spent_cents: float, limit_cents: int | None) -> None:
        """Emit once when spend reaches the alert fraction of the spending limit.

        Args:
            spent_cents: Cents settled by the run so far.
            limit_cents: The run's spending limit, or ``None`` when uncapped.
        """
        settings = self._settings
        if not self._wants(BUDGET_NEAR) or not limit_cents or limit_cents <= 0:
            return
        if spent_cents >= settings.budget_alert_fraction * limit_cents and self._claim("budget"):
            self._dispatch(notify_run_budget, self._optimization_id, self._username, spent_cents, limit_cents)
