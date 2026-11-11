"""Attribute each billed model call to the run stage, pair, candidate and case it served.

Tags travel with the request as one header, so they cross every boundary a
call can take: a worker child's HTTP call to the gateway, a sandbox command's
mailbox document, and the guest proxy. They are display attribution only,
except ``box``. The money a call costs and the role it is billed under come
from the trusted route, never from a tag, so a forged tag can only mislabel
its own run's rows. A forged ``box`` tag can at most stop its own run early
(see ``BudgetRuntime.pin``).
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import re
import threading
from collections.abc import Iterator, Mapping
from typing import Any

USAGE_TAGS_HEADER = "x-app-usage-tags"
# Still sent by the pinned sandbox image and older clients; read, never written.
LEGACY_USAGE_TAGS_HEADER = "x-skynet-usage-tags"
USAGE_TAGS_HEADERS = frozenset({USAGE_TAGS_HEADER, LEGACY_USAGE_TAGS_HEADER})
TAG_KEYS = ("stage", "pair", "candidate", "case", "caller", "kind", "lane", "box")
# ``caller`` values: which optimizer drove an optimization-route call.
CALLER_PROPOSER = "proposer"
CALLER_REFLECTION = "reflection"
# ``kind`` values: what an engine that makes several sorts of model calls used one for.
KIND_MUTATION = "mutation"
KIND_META_NOTES = "meta_notes"
KIND_NOVELTY_JUDGE = "novelty_judge"
KIND_EMBEDDING = "embedding"
# ``lane`` names the Auto lane (``<phase>-<engine>``) a call ran in. Unlike the
# other keys it follows the call into threads started inside the lane, because
# engines score and reflect on their own pools where the lane's context is gone.
LANE_KEY = "lane"
# ``box`` names the pinned sandbox whose work made a call. The parent sets it
# over the guest's tags for every call that a box's mailbox relays, so the
# budget runtime can tell when every pinned box waits on its own model calls.
BOX_KEY = "box"
_MAX_VALUE_CHARS = 64
_MAX_HEADER_CHARS = 512
_VALUE = re.compile(r"[A-Za-z0-9_.:@/+ -]+")

_scope: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar("usage_tags", default=None)


def box_tag(operation_key: str) -> str:
    """Name a sandbox in the ``box`` tag by its admitted operation key.

    A digest keeps the tag short and printable for any key.

    Args:
        operation_key: The logical operation key the box was reserved under.

    Returns:
        A 32-character hex tag.
    """
    return hashlib.sha256(operation_key.encode()).hexdigest()[:32]


def clean_tags(tags: Mapping[str, Any] | None) -> dict[str, str]:
    """Keep only known keys with short, printable values.

    Args:
        tags: Candidate tags from any source, trusted or not.

    Returns:
        The tags safe to store and display; unknown keys and odd values are dropped.
    """
    cleaned: dict[str, str] = {}
    for key in TAG_KEYS:
        value = (tags or {}).get(key)
        if value is None or isinstance(value, bool):
            continue
        text = str(value).strip()[:_MAX_VALUE_CHARS]
        if text and _VALUE.fullmatch(text):
            cleaned[key] = text
    return cleaned


def encode_tags(tags: Mapping[str, Any]) -> str:
    """Serialize tags for the request header.

    Args:
        tags: Tags to send.

    Returns:
        Compact JSON, empty when nothing is worth sending.
    """
    cleaned = clean_tags(tags)
    return json.dumps(cleaned, separators=(",", ":"), sort_keys=True) if cleaned else ""


def parse_tags(header: str | None) -> dict[str, str]:
    """Read tags from a request header, ignoring anything malformed.

    Args:
        header: The raw header value, or ``None``.

    Returns:
        The cleaned tags; empty for a missing, oversized or invalid header.
    """
    if not header or len(header) > _MAX_HEADER_CHARS:
        return {}
    try:
        value = json.loads(header)
    except ValueError:
        return {}
    return clean_tags(value) if isinstance(value, dict) else {}


def header_tags(headers: Mapping[str, str] | None) -> dict[str, str]:
    """Read the tags header from a header mapping of any case.

    The current header wins over the legacy one when a request carries both.

    Args:
        headers: Request headers.

    Returns:
        The cleaned tags.
    """
    found = {name.lower(): value for name, value in (headers or {}).items() if name.lower() in USAGE_TAGS_HEADERS}
    if USAGE_TAGS_HEADER in found:
        return parse_tags(found[USAGE_TAGS_HEADER])
    return parse_tags(found.get(LEGACY_USAGE_TAGS_HEADER))


def current_tags() -> dict[str, str]:
    """Return the tags of the innermost open :func:`usage_scope`.

    Returns:
        A copy of the active tags, empty outside any scope.
    """
    return dict(_scope.get() or {})


@contextlib.contextmanager
def usage_scope(**tags: Any) -> Iterator[None]:
    """Tag every model call made while the block runs on this context.

    Scopes nest: an inner one keeps the outer's tags and overrides those it sets.

    Args:
        **tags: ``stage``, ``pair``, ``candidate``, ``case``, ``caller``, ``kind``, ``lane`` or ``box``;
            ``None`` keeps the outer value.

    Yields:
        Nothing; the scope ends when the block does.
    """
    token = _scope.set({**current_tags(), **clean_tags(tags)})
    try:
        yield
    finally:
        _scope.reset(token)


class TagsToken:
    """Undo one :func:`usage_tags_token` from the context that opened it."""

    def __init__(self, token: contextvars.Token[dict[str, str] | None]) -> None:
        """Hold the context variable token to restore.

        Args:
            token: Token returned when the tags were set.
        """
        self._token = token

    def reset(self) -> None:
        """Restore the outer tags; a no-op when called from another context."""
        with contextlib.suppress(ValueError):
            _scope.reset(self._token)


def usage_tags_token(**tags: Any) -> TagsToken:
    """Open a tag scope that a callback closes later, unlike :func:`usage_scope`.

    Args:
        **tags: Tags to add; ``None`` keeps the outer value.

    Returns:
        A token whose ``reset`` restores the outer tags.
    """
    return TagsToken(_scope.set({**current_tags(), **clean_tags(tags)}))


def with_tags_header(headers: Mapping[str, str] | None, tags: Mapping[str, Any]) -> dict[str, str]:
    """Return headers carrying ``tags`` merged over any tags already present.

    Args:
        headers: Existing request headers.
        tags: Tags that take precedence over the existing header's.

    Returns:
        A new header mapping; unchanged apart from the tags header, which is
        written under both its current and its pre-rename name.
    """
    result = {name: value for name, value in (headers or {}).items() if name.lower() not in USAGE_TAGS_HEADERS}
    encoded = encode_tags({**header_tags(headers), **clean_tags(tags)})
    if encoded:
        # The DSPy guest runs the pinned image's backend copy while the parent
        # ships the model proxy, so either side may be the older one; an older
        # proxy forwards and an older gateway reads only the legacy name.
        result[USAGE_TAGS_HEADER] = encoded
        result[LEGACY_USAGE_TAGS_HEADER] = encoded
    return result


_thread_start = threading.Thread.start


def _start_in_lane(self: threading.Thread) -> None:
    """Start a thread that keeps the starting context's ``lane`` tag.

    Python 3.11 threads begin with an empty context, so without this every
    call an engine makes from its own worker threads would lose its lane. Only
    ``lane`` crosses over: the other keys keep their per-context behavior.

    Args:
        self: The thread being started.
    """
    lane = (_scope.get() or {}).get(LANE_KEY)
    if lane is not None:
        target_run = self.run

        def run_in_lane() -> None:
            """Run the thread's body with the starting context's lane."""
            _scope.set({LANE_KEY: lane})
            target_run()

        self.run = run_in_lane  # type: ignore[method-assign]
    _thread_start(self)


threading.Thread.start = _start_in_lane  # type: ignore[method-assign]
