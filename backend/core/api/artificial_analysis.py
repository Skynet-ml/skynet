"""Verified links from catalog models to their Artificial Analysis pages.

OpenRouter attaches each model's Artificial Analysis intelligence index but
not the page it came from, so the link is derived: a few candidate slugs from
the OpenRouter id and display name (``anthropic/claude-opus-4.5`` ->
``claude-opus-4-5``, ``claude-4-5-opus``), each fetched to confirm it is a
real model page whose title names the same model. Only a confirmed page is
ever linked.

Verification runs in a background thread and its results are cached for a
day, so the catalog response never waits on it; a model shows its link from
the first response after its check lands.
"""

from __future__ import annotations

import html
import logging
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from threading import Lock, Thread

from .model_catalog import CatalogModel, ModelCatalogResponse

logger = logging.getLogger(__name__)

_BASE_URL = "https://artificialanalysis.ai/models/"
_OPENROUTER_PREFIX = "openrouter/"
_TTL_SECONDS = 24 * 3600
# A network failure is not proof the page is missing; retry sooner than a day.
_ERROR_TTL_SECONDS = 3600
_TIMEOUT_SECONDS = 5
_WORKERS = 4
# The <title> and canonical link sit in the first few KB of a multi-MB page.
_HEAD_BYTES = 32 * 1024
_USER_AGENT = "Mozilla/5.0 (compatible; SkynetModelCatalog/1.0)"

_DATE_SUFFIX_RE = re.compile(r"-(\d{8}|\d{4}-\d{2}-\d{2})$")
_CLAUDE_RE = re.compile(r"^claude-(haiku|sonnet|opus)-([\d-]+)$")
_DROPPABLE_SUFFIX_RE = re.compile(r"-(it|instruct|preview|v1|exp|\d{4})$")
_TITLE_RE = re.compile(r"<title>([^<]*)</title>")
_CANONICAL_RE = re.compile(r'<link rel="canonical" href="([^"]*)"')
_PARENS_RE = re.compile(r"\([^)]*\)")
# Model-family pages title themselves "<name> Models - ..."; the word is not part of the name.
_MODELS_SUFFIX_RE = re.compile(r"\s+Models$")
_TOKEN_RE = re.compile(r"[a-z0-9]+")
# OpenRouter and Artificial Analysis disagree on whether these are part of a name.
_IGNORED_TOKENS = frozenset({"instruct", "preview"})

_lock = Lock()
_links: dict[str, tuple[str | None, float]] = {}
_refresh_in_flight = False


def _slugify(text: str) -> str:
    """Turn text into an Artificial Analysis style slug.

    Args:
        text: A model name or id.

    Returns:
        Lowercase alphanumeric runs joined by hyphens (``GPT-5.4 Mini`` -> ``gpt-5-4-mini``).
    """
    return "-".join(_TOKEN_RE.findall(text.lower()))


def slug_candidates(model_value: str, display_name: str | None) -> list[str]:
    """Derive the Artificial Analysis slugs a catalog model's page may live at.

    Args:
        model_value: Catalog id, e.g. ``openrouter/anthropic/claude-opus-4.5``.
        display_name: OpenRouter's name without the lab prefix, if any.

    Returns:
        Distinct candidate slugs, the most likely first.
    """
    model_name = model_value.removeprefix(_OPENROUTER_PREFIX).split("/", 1)[-1].split(":", 1)[0]
    bases = [_slugify(_DATE_SUFFIX_RE.sub("", model_name))]
    if display_name:
        bases.append(_slugify(display_name))
    candidates: list[str] = []
    for base in bases:
        claude = _CLAUDE_RE.match(base)
        for slug in (
            base,
            f"claude-{claude.group(2)}-{claude.group(1)}" if claude else "",
            _DROPPABLE_SUFFIX_RE.sub("", base),
        ):
            if slug and slug not in candidates:
                candidates.append(slug)
    return candidates


def _name_tokens(name: str) -> list[str]:
    """Split a model name into comparable tokens.

    Args:
        name: A human model name.

    Returns:
        Sorted lowercase alphanumeric tokens, without :data:`_IGNORED_TOKENS`,
        so word order and punctuation don't matter (``Claude 4.5 Haiku`` ==
        ``Claude Haiku 4.5``).
    """
    return sorted(t for t in _TOKEN_RE.findall(name.lower()) if t not in _IGNORED_TOKENS)


def names_match(display_name: str, page_title: str) -> bool:
    """Report whether an Artificial Analysis page title names the given model.

    The page title reads ``"Claude Opus 4.5 (non-reasoning) - Intelligence, ..."``;
    the parenthetical names a reasoning setting, so it is compared both with
    and without it (``Qwen3.8 Max (0902)`` keeps its date in parentheses).
    A model-family page reads ``"Claude Fable 5.2 Models - ..."``; the trailing
    ``Models`` is dropped before the compare.

    Args:
        display_name: OpenRouter's name for the model.
        page_title: The page's ``<title>`` text.

    Returns:
        ``True`` when the names carry the same tokens.
    """
    model = _MODELS_SUFFIX_RE.sub("", html.unescape(page_title).split(" - ", 1)[0])
    wanted = _name_tokens(display_name)
    return bool(wanted) and wanted in (_name_tokens(model), _name_tokens(_PARENS_RE.sub(" ", model)))


def _fetch_head(url: str) -> tuple[int, str, str]:
    """Fetch the start of a page.

    Args:
        url: Page URL.

    Returns:
        ``(status, final url after redirects, first bytes as text)``; a 404
        returns its status with empty text.

    Raises:
        OSError: On network failures, timeouts and any HTTP error but a 404.
    """
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            return response.status, response.geturl(), response.read(_HEAD_BYTES).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        # Only a 404 proves the page is missing; a rate limit, bot challenge or
        # 5xx must take the short error TTL instead of hiding the link a day.
        if exc.code != 404:
            raise
        return exc.code, url, ""


def verify_page(slug: str, display_name: str) -> bool:
    """Check that ``/models/<slug>`` is this model's own page.

    The site answers an unknown slug with a 404, but a known slug can belong
    to another release of the line (``deepseek-v4-pro`` shows the newest V4
    Pro), so the title must name the same model too.

    Args:
        slug: Candidate slug.
        display_name: OpenRouter's name for the model.

    Returns:
        ``True`` for a 200 page, not redirected, whose canonical link is the
        URL itself and whose title names the model.

    Raises:
        OSError: On network failures and timeouts.
    """
    url = _BASE_URL + slug
    status, final_url, head = _fetch_head(url)
    if status != 200 or final_url.rstrip("/") != url:
        return False
    canonical = _CANONICAL_RE.search(head)
    title = _TITLE_RE.search(head)
    return bool(
        canonical and canonical.group(1).rstrip("/") == url and title and names_match(display_name, title.group(1))
    )


def find_page(model: CatalogModel) -> str | None:
    """Return the first candidate page verified for a model.

    Args:
        model: A rated OpenRouter catalog model with a display name.

    Returns:
        The page URL, or ``None`` when no candidate checks out.

    Raises:
        OSError: When a candidate could not be checked; the caller retries later.
    """
    for slug in slug_candidates(model.value, model.display_name):
        if verify_page(slug, model.display_name or ""):
            return _BASE_URL + slug
    return None


def _checkable(model: CatalogModel) -> bool:
    """Report whether a model can have a page: an OpenRouter model with a score and a name.

    Args:
        model: A catalog model.

    Returns:
        ``True`` when worth checking.
    """
    return (
        model.value.startswith(_OPENROUTER_PREFIX) and model.intelligence_index is not None and bool(model.display_name)
    )


def _check(model: CatalogModel) -> tuple[str | None, float]:
    """Verify one model and say how long the answer holds.

    Args:
        model: A checkable catalog model.

    Returns:
        ``(page url or None, expiry as a monotonic time)``.
    """
    try:
        return find_page(model), time.monotonic() + _TTL_SECONDS
    except Exception:
        logger.debug("Artificial Analysis check failed for %s", model.value, exc_info=True)
        return None, time.monotonic() + _ERROR_TTL_SECONDS


def _refresh(models: list[CatalogModel]) -> None:
    """Verify the given models and store the results; never raises.

    Args:
        models: Checkable models whose cached answer is missing or expired.
    """
    global _refresh_in_flight
    try:
        with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
            for model, result in zip(models, pool.map(_check, models), strict=True):
                with _lock:
                    _links[model.value] = result
        found = sum(1 for m in models if _links.get(m.value, (None, 0))[0])
        logger.info("Artificial Analysis links verified: %d of %d models", found, len(models))
    except Exception:
        logger.exception("Artificial Analysis link refresh failed")
    finally:
        with _lock:
            _refresh_in_flight = False


def with_artificial_analysis_links(catalog: ModelCatalogResponse) -> ModelCatalogResponse:
    """Attach each model's verified Artificial Analysis page.

    Never blocks: models without a fresh cached answer start a background
    check and go out without a link this time.

    Args:
        catalog: A catalog, left unmodified.

    Returns:
        A copy whose ``artificial_analysis_url`` carries the verified pages.
    """
    global _refresh_in_flight
    now = time.monotonic()
    with _lock:
        links = dict(_links)
        stale = [m for m in catalog.models if _checkable(m) and links.get(m.value, (None, 0.0))[1] <= now]
        start = bool(stale) and not _refresh_in_flight
        if start:
            _refresh_in_flight = True
    if start:
        Thread(target=_refresh, args=(stale,), name="artificial-analysis-links", daemon=True).start()
    return catalog.model_copy(
        update={
            "models": [
                m.model_copy(update={"artificial_analysis_url": links.get(m.value, (None, 0.0))[0]})
                for m in catalog.models
            ]
        }
    )
