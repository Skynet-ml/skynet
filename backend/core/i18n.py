"""Backend facade over the shared i18n catalog.

The canonical catalog lives at ``i18n/locales/he.json`` in the repo root, and
``scripts/generate_i18n.py`` copies it into ``backend/core/i18n_locales/he.json``
so wheel installs keep the catalog inside the package. Backend modules keep
using ``t(key, **params)`` and the ``TERM_*`` constants below, but the Hebrew
strings and domain terms are no longer mirrored in Python code.

The formatter is intentionally small: it supports ``{name}`` params plus
``{term.someKey}`` placeholders resolved from the shared terms section. If we
need richer plural/gender formatting later, this facade is the single place to
swap in Fluent, Babel/gettext, or an ICU-compatible formatter.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from core.brand import APP_NAME

# Lookup the in-package copy first (works for wheel installs),
# and fall back to the repo-root canonical when running from a source tree
# without the bundled copy. Module-level constant so import never blocks on
# disk I/O — the catalog itself is read lazily by ``_catalog()``.
_PACKAGE_CATALOG = Path(__file__).resolve().parent / "i18n_locales" / "he.json"
_REPO_CATALOG = Path(__file__).resolve().parents[2] / "i18n" / "locales" / "he.json"
CATALOG_PATH = _PACKAGE_CATALOG if _PACKAGE_CATALOG.is_file() else _REPO_CATALOG
TERM_PATTERN = re.compile(r"{term\.([A-Za-z0-9_]+)}")
# The app's name comes from i18n/brand.json rather than the catalogs, so every
# locale resolves {term.appName} to the same untranslated brand.
_BRAND_TERMS: dict[str, str] = {"appName": APP_NAME}

_TERM_KEYS: dict[str, str] = {
    "TERM_OPTIMIZATION": "optimization",
    "TERM_OPTIMIZATION_PLURAL": "optimizationPlural",
    "TERM_DATASET": "dataset",
    "TERM_EXAMPLE": "example",
    "TERM_EXAMPLE_PLURAL": "examplePlural",
    "TERM_ROW": "row",
    "TERM_ROW_PLURAL": "rowPlural",
    "TERM_COLUMN": "column",
    "TERM_INPUT_COLUMNS": "inputColumns",
    "TERM_OUTPUT_COLUMN": "outputColumnTarget",
    "TERM_SPLIT_TRAIN": "splitTrain",
    "TERM_SPLIT_VAL": "splitVal",
    "TERM_SPLIT_TEST": "splitTest",
    "TERM_PROGRAM": "program",
    "TERM_MODULE": "module",
    "TERM_OPTIMIZER": "optimizer",
    "TERM_METRIC": "metric",
    "TERM_SCORE": "score",
    "TERM_FINAL_SCORE": "finalScore",
    "TERM_MODEL": "model",
    "TERM_PAIR_PLURAL": "pairPlural",
    "CANCELLATION_REASON": "cancellationReason",
    "PAUSE_REASON": "pauseReason",
}


@lru_cache(maxsize=1)
def _catalog() -> dict[str, Any]:
    """Load and memoise the JSON i18n catalog from disk.

    Returns:
        Parsed catalog mapping. Cached for the process lifetime since the
        catalog is shipped with the wheel and never mutated at runtime.
    """
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def _terms() -> dict[str, str]:
    """Return the ``terms`` section of the catalog (or an empty dict).

    Returns:
        Mapping of term key → Hebrew string, or ``{}`` when the section is
        missing or wrongly typed (caller falls back to the key itself).
    """
    terms = _catalog().get("terms", {})
    if not isinstance(terms, dict):
        return {}
    return terms


def _messages() -> dict[str, str]:
    """Return the ``messages`` section of the catalog (or an empty dict).

    Returns:
        Mapping of message key → Hebrew template, or ``{}`` when the section
        is missing or wrongly typed.
    """
    messages = _catalog().get("messages", {})
    if not isinstance(messages, dict):
        return {}
    return messages


def _sample_datasets() -> dict[str, dict[str, Any]]:
    """Return the ``backend.sampleDatasets`` section of the catalog.

    Returns:
        Mapping of dataset id → sample-dataset payload, or ``{}`` when missing
        or wrongly typed. Defensive ``.get()`` chain so a stripped or partially
        populated catalog cannot break import.
    """
    backend = _catalog().get("backend", {})
    if not isinstance(backend, dict):
        return {}
    samples = backend.get("sampleDatasets", {})
    if not isinstance(samples, dict):
        return {}
    return samples


def term(key: str) -> str:
    """Return a canonical shared term by key, or the key when missing.

    Args:
        key: Term identifier (e.g. ``"optimization"``).

    Returns:
        The Hebrew term string from the catalog, or ``key`` itself when the
        catalog has no entry.
    """
    if key in _BRAND_TERMS:
        return _BRAND_TERMS[key]
    value = _terms().get(key)
    return value if isinstance(value, str) else key


def __getattr__(name: str) -> Any:
    """Resolve TERM_* and SAMPLE_DATASETS attributes on first access.

    Lazy module-level lookup (PEP 562) keeps imports cheap when callers only
    need ``t()`` — without this every ``import core.i18n`` would parse the
    25-KB catalog file at module-load time.

    Args:
        name: Attribute being accessed on the ``core.i18n`` module.

    Returns:
        The resolved Hebrew literal / dataset mapping.

    Raises:
        AttributeError: When ``name`` is not a recognised public symbol.
    """
    if name == "SAMPLE_DATASETS":
        return _sample_datasets()
    if name in _TERM_KEYS:
        return term(_TERM_KEYS[name])
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def t(key: str, /, **params: Any) -> str:
    """Look up and format a Hebrew user-facing string by stable semantic key.

    Returns the key itself when missing, surfacing catalog drift as an obvious
    development artifact rather than a silent blank. ``key`` is positional-only
    so callers can pass an unrelated ``key=...`` template parameter (e.g. from
    :class:`DomainError` kwargs) without colliding with the lookup argument.

    Args:
        key: Catalog identifier for the message template.
        **params: Optional placeholder values substituted via ``str.format``.

    Returns:
        The localized, formatted string (or ``key`` when no template exists).
    """
    template = _messages().get(key, key)
    template = TERM_PATTERN.sub(lambda match: term(match.group(1)), template)
    if params:
        try:
            return template.format(**params)
        except (KeyError, IndexError, ValueError):
            return template
    return template


DEFAULT_LOCALE = "en"
_LOCALE_TAG = re.compile(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*")


def request_locale(explicit: str | None, accept_language: str | None = None) -> str:
    """Pick the locale to answer a request in.

    The UI's own locale wins over ``Accept-Language``: the browser sends its
    language, which can differ from the language the user chose in the app.

    Args:
        explicit: Locale tag the client named in the request body, if any.
        accept_language: Raw ``Accept-Language`` header, if any.

    Returns:
        A locale tag; English when neither source names a usable one.
    """
    if explicit and _LOCALE_TAG.fullmatch(explicit.strip()):
        return explicit.strip()
    ranked: list[tuple[float, int, str]] = []
    for position, part in enumerate((accept_language or "").split(",")):
        tag, _, quality = part.strip().partition(";")
        tag = tag.strip()
        if not _LOCALE_TAG.fullmatch(tag):
            continue
        weight = 1.0
        if quality.strip().startswith("q="):
            try:
                weight = float(quality.strip()[2:])
            except ValueError:
                continue
        if weight > 0:
            ranked.append((-weight, position, tag))
    return min(ranked)[2] if ranked else DEFAULT_LOCALE


@lru_cache(maxsize=64)
def _overlay(locale: str) -> dict[str, Any]:
    """Load one per-locale overlay catalog, or an empty mapping when absent.

    Args:
        locale: Locale tag matching an ``i18n/locales/<locale>.json`` file.

    Returns:
        The parsed overlay, or ``{}`` when no file exists for ``locale``.
    """
    if not re.fullmatch(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*", locale):
        return {}
    for directory in (_PACKAGE_CATALOG.parent, _REPO_CATALOG.parent):
        path = directory / f"{locale}.json"
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    return {}


def locale_chain(locale: str | None) -> list[str]:
    """Return the lookup order for ``locale``, ending at the Hebrew base.

    Mirrors the frontend registry: a regional tag falls back to its language,
    every non-Hebrew locale then falls back to English, and Hebrew is last
    because it is the only complete catalog.

    Args:
        locale: Requested locale tag (``"pt-BR"``, ``"en"``); ``None`` means
            the Hebrew base.

    Returns:
        Distinct locale tags to try in order.
    """
    if not locale:
        return ["he"]
    language = locale.split("-", 1)[0]
    chain: list[str] = []
    for tag in (locale, language, "en", "he"):
        if tag and tag not in chain:
            chain.append(tag)
        if tag == "he":
            break
    return chain


def t_locale(locale: str | None, key: str, /, **params: Any) -> str:
    """Look up and format a user-facing string in ``locale``.

    Walks :func:`locale_chain` so a key a locale has not translated degrades to
    English, then Hebrew, never to a blank. ``{term.x}`` placeholders resolve
    through the same chain.

    Args:
        locale: Recipient locale tag; ``None`` renders the Hebrew base.
        key: Catalog identifier for the message template.
        **params: Optional placeholder values substituted via ``str.format``.

    Returns:
        The localized, formatted string (or ``key`` when no locale has it).
    """
    chain = locale_chain(locale)

    def lookup(section: str, name: str) -> str | None:
        """Return the first ``section``/``name`` value found along the chain."""
        for tag in chain:
            values = (_catalog() if tag == "he" else _overlay(tag)).get(section, {})
            value = values.get(name) if isinstance(values, dict) else None
            if isinstance(value, str):
                return value
        return None

    template = lookup("messages", key) or key
    template = TERM_PATTERN.sub(
        lambda match: _BRAND_TERMS.get(match.group(1)) or lookup("terms", match.group(1)) or match.group(1),
        template,
    )
    if params:
        try:
            return template.format(**params)
        except (KeyError, IndexError, ValueError):
            return template
    return template
