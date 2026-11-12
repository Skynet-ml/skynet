"""The notification event types, the channels, and the default routing grid.

A leaf module (no imports from the rest of the package) so preferences,
routing, channels and the API can all share one definition.
"""

from __future__ import annotations

from typing import Final

INBOX: Final = "inbox"
EMAIL: Final = "email"
CHROME: Final = "chrome"
SLACK: Final = "slack"
MACOS: Final = "macos"

CHANNELS: Final[tuple[str, ...]] = (INBOX, EMAIL, CHROME, SLACK)
# The channels that send outside the app; the inbox row is written by ``notify`` itself.
DELIVERY_CHANNELS: Final[tuple[str, ...]] = (EMAIL, CHROME, SLACK)
# Channels only the Mac app (local mode) has. Hosted grids, routing and API
# answers never contain them, so hosted behavior stays exactly as it was.
LOCAL_CHANNELS: Final[tuple[str, ...]] = (MACOS,)

NEEDS_INPUT: Final = "needs_input"
BUDGET_NEAR: Final = "budget_near"
FAILED: Final = "failed"
FINISHED: Final = "finished"
NEW_BEST: Final = "new_best"
STUCK: Final = "stuck"
SHARED: Final = "shared"
PRODUCT_NEWS: Final = "product_news"

EVENT_TYPES: Final[tuple[str, ...]] = (
    NEEDS_INPUT,
    BUDGET_NEAR,
    FAILED,
    FINISHED,
    NEW_BEST,
    STUCK,
    SHARED,
    PRODUCT_NEWS,
)

# The inbox's Focused tab: what the user has to act on or wants to know at once.
FOCUSED_EVENTS: Final[frozenset[str]] = frozenset({NEEDS_INPUT, BUDGET_NEAR, FAILED, FINISHED})
# Urgent events skip the hold and go out at once on every enabled channel.
URGENT_EVENTS: Final[frozenset[str]] = frozenset({NEEDS_INPUT, BUDGET_NEAR})
# Events that only ever land in the inbox, whatever the grid says.
INBOX_ONLY_EVENTS: Final[frozenset[str]] = frozenset({PRODUCT_NEWS})


def _row(inbox: bool, email: bool, chrome: bool, slack: bool) -> dict[str, bool]:
    """Build one event's channel switches.

    Args:
        inbox: Whether the event shows in the inbox.
        email: Whether it is emailed.
        chrome: Whether it is pushed to the browser.
        slack: Whether it is posted to Slack.

    Returns:
        The channel -> enabled mapping.
    """
    return {INBOX: inbox, EMAIL: email, CHROME: chrome, SLACK: slack}


# Finished and failed push to Chrome by default: there is no run-start opt-in,
# and a push only reaches browsers the user subscribed in Settings. A stored
# grid always overrides these cells, so a saved opt-out stays off.
DEFAULT_GRID: Final[dict[str, dict[str, bool]]] = {
    NEEDS_INPUT: _row(True, True, True, True),
    BUDGET_NEAR: _row(True, True, True, False),
    FAILED: _row(True, True, True, False),
    FINISHED: _row(True, True, True, False),
    NEW_BEST: _row(True, False, False, False),
    STUCK: _row(True, False, False, False),
    SHARED: _row(True, False, False, False),
    PRODUCT_NEWS: _row(True, False, False, False),
}

# A local-mode channel's default cells: the same events Chrome push gets by default.
LOCAL_DEFAULT_CELLS: Final[dict[str, dict[str, bool]]] = {
    event: {MACOS: row[CHROME]} for event, row in DEFAULT_GRID.items()
}

DEFAULT_HOLD_MINUTES: Final = 10
MIN_HOLD_MINUTES: Final = 0
MAX_HOLD_MINUTES: Final = 1440
DEFAULT_HELD_CHANNELS: Final[tuple[str, ...]] = (EMAIL,)
