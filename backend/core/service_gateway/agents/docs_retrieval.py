"""Lexical retrieval over the public Skynet docs for the docs-only agent.

The docs are the Markdown pages under ``frontend/src/features/docs/content``.
Each page splits into passages at its ``##`` headings, and a BM25 index ranks
the passages for a question. There are no embeddings on purpose: the index is
small, builds in milliseconds, and costs nothing per question.

The backend image is built from ``backend/`` alone, so the docs folder is not
inside it. When the folder is missing, the corpus loads the English pages from
the web app's ``/llms-full.txt`` instead (``APP_PUBLIC_URL``), and every
locale then answers from the English pages. The Mac app (local mode) serves
no public web app, so it reads the hosted app of its build target instead
(``hosted_app_url``: staging for a staging build, production for a release).
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from ...config import settings

logger = logging.getLogger("app.agents.docs_retrieval")

BM25_K1 = 1.2
BM25_B = 0.75
DEFAULT_LOCALE = "en"
# A heading counts like this many body words, so a section about the asked
# topic outranks one that only mentions it.
_HEADING_WEIGHT = 3
_MAX_PASSAGE_CHARS = 1800
_REMOTE_TTL_SECONDS = 3600.0
_REMOTE_TIMEOUT_SECONDS = 10.0
_REPO_DOCS_DIR = Path(__file__).resolve().parents[4] / "frontend" / "src" / "features" / "docs" / "content"

_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿]")
# Escapes keep raw Hebrew out of backend modules (test_i18n_catalog_boundary).
_HEBREW_WORD = re.compile("^[\u05d0-\u05ea]+$")
# One-letter Hebrew prefixes: and, the, in, as, to, from, that.
_HEBREW_PREFIXES = "\u05d5\u05d4\u05d1\u05db\u05dc\u05de\u05e9"
_WORD_SPLIT = re.compile(r"[\W_]+", re.UNICODE)
_SOURCE_LINE = re.compile(r"^Source: (?P<url>\S+)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class Passage:
    """One docs section that retrieval can return."""

    locale: str
    slug: str
    heading: str
    text: str

    @property
    def href(self) -> str:
        """Return the relative docs link for the passage's page.

        Returns:
            ``/docs/<slug>`` for English, ``/docs/<locale>/<slug>`` otherwise.
        """
        base = "/docs" if self.locale == DEFAULT_LOCALE else f"/docs/{self.locale}"
        return f"{base}/{self.slug}" if self.slug else base


@dataclass
class _Bm25Index:
    """BM25 statistics for one locale's passages."""

    passages: list[Passage]
    term_freqs: list[Counter[str]] = field(default_factory=list)
    lengths: list[float] = field(default_factory=list)
    doc_freq: Counter[str] = field(default_factory=Counter)
    avg_length: float = 0.0


def tokenize(text: str) -> list[str]:
    """Split text into lowercase search tokens.

    Accents and Hebrew vowel points are removed. Han and kana runs, which have
    no spaces, become overlapping character pairs.

    Args:
        text: Any text.

    Returns:
        The tokens in order.
    """
    plain = "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch))
    tokens: list[str] = []
    for word in _WORD_SPLIT.split(plain.lower()):
        if not word:
            continue
        if len(word) < 2 or not _CJK.search(word):
            tokens.append(word)
            continue
        tokens.extend(word[i : i + 2] for i in range(len(word) - 1))
    return tokens


def _variants(token: str) -> list[str]:
    """Return the token, plus the token without a one-letter Hebrew prefix.

    Args:
        token: One token from :func:`tokenize`.

    Returns:
        One or two index terms.
    """
    if len(token) >= 4 and _HEBREW_WORD.match(token) and token[0] in _HEBREW_PREFIXES:
        return [token, token[1:]]
    return [token]


def _terms(text: str) -> list[str]:
    """Return every index term of ``text``, with Hebrew prefix variants.

    Args:
        text: Any text.

    Returns:
        The terms in order.
    """
    return [term for token in tokenize(text) for term in _variants(token)]


def build_index(passages: list[Passage]) -> _Bm25Index:
    """Build a BM25 index over passages, with headings weighted up.

    Args:
        passages: The passages of one locale.

    Returns:
        The index.
    """
    index = _Bm25Index(passages=passages)
    for passage in passages:
        tf: Counter[str] = Counter()
        heading_terms = _terms(passage.heading)
        for term in heading_terms:
            tf[term] += _HEADING_WEIGHT
        body_terms = _terms(passage.text)
        tf.update(body_terms)
        index.term_freqs.append(tf)
        index.lengths.append(len(heading_terms) * _HEADING_WEIGHT + len(body_terms))
        index.doc_freq.update(tf.keys())
    index.avg_length = sum(index.lengths) / len(index.lengths) if index.lengths else 0.0
    return index


def search(index: _Bm25Index, query: str, *, limit: int) -> list[tuple[Passage, float]]:
    """Rank the index's passages for ``query`` by BM25.

    Args:
        index: A locale index from :func:`build_index`.
        query: The user's question.
        limit: The most passages to return.

    Returns:
        ``(passage, score)`` pairs with a positive score, best first.
    """
    terms = set(_terms(query))
    total = len(index.passages)
    if not terms or not total:
        return []
    scored: list[tuple[Passage, float]] = []
    for i, passage in enumerate(index.passages):
        tf_map = index.term_freqs[i]
        norm = BM25_K1 * (1 - BM25_B + BM25_B * index.lengths[i] / (index.avg_length or 1))
        score = 0.0
        for term in terms:
            tf = tf_map.get(term)
            if not tf:
                continue
            n = index.doc_freq[term]
            idf = math.log(1 + (total - n + 0.5) / (n + 0.5))
            score += idf * tf * (BM25_K1 + 1) / (tf + norm)
        if score > 0:
            scored.append((passage, score))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:limit]


def split_page(locale: str, slug: str, markdown: str) -> list[Passage]:
    """Split one docs page into passages at its ``##`` headings.

    A long section splits again at paragraph breaks, so one passage stays
    small enough that several fit in the prompt.

    Args:
        locale: The page's locale.
        slug: The page slug (``""`` for the docs index).
        markdown: The page body.

    Returns:
        The page's non-empty passages.
    """
    sections: list[tuple[str, list[str]]] = [("", [])]
    for line in markdown.splitlines():
        if line.startswith("## "):
            sections.append((line[3:].strip(), []))
        else:
            sections[-1][1].append(line)
    passages: list[Passage] = []
    for heading, lines in sections:
        body = "\n".join(lines).strip()
        if not body:
            continue
        chunk = ""
        for paragraph in body.split("\n\n"):
            if chunk and len(chunk) + len(paragraph) > _MAX_PASSAGE_CHARS:
                passages.append(Passage(locale, slug, heading, chunk.strip()))
                chunk = ""
            chunk += paragraph + "\n\n"
        if chunk.strip():
            passages.append(Passage(locale, slug, heading, chunk.strip()[: _MAX_PASSAGE_CHARS * 2]))
    return passages


def load_folder(root: Path) -> dict[str, list[Passage]]:
    """Read every locale folder of Markdown pages under ``root``.

    Args:
        root: The docs content folder (one sub-folder per locale).

    Returns:
        The passages by locale.
    """
    corpus: dict[str, list[Passage]] = {}
    for locale_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        passages: list[Passage] = []
        for path in sorted(locale_dir.rglob("*.md")):
            slug = path.relative_to(locale_dir).with_suffix("").as_posix()
            slug = "" if slug == "index" else slug
            passages.extend(split_page(locale_dir.name, slug, path.read_text(encoding="utf-8")))
        if passages:
            corpus[locale_dir.name] = passages
    return corpus


def parse_llms_full(text: str) -> list[Passage]:
    """Split the web app's ``/llms-full.txt`` into English passages.

    Each page there ends with a ``Source: <url>`` line that names its
    ``/docs/...`` path; the slug comes from that path.

    Args:
        text: The ``/llms-full.txt`` body.

    Returns:
        The English passages.
    """
    passages: list[Passage] = []
    start = 0
    for match in _SOURCE_LINE.finditer(text):
        page = text[start : match.start()]
        start = match.end()
        path = httpx.URL(match.group("url")).path
        if not path.startswith("/docs"):
            continue
        slug = path.removeprefix("/docs").strip("/")
        body = re.sub(r"\n-{3,}\s*$", "", page.strip())
        body = re.sub(r"^# .*\n", "", body, count=1)
        passages.extend(split_page(DEFAULT_LOCALE, slug, body))
    return passages


class DocsCorpus:
    """Lazily loaded, per-locale BM25 indexes over the docs."""

    def __init__(self, root: Path | None = None, *, remote_url: str | None = None) -> None:
        """Remember where the docs come from; nothing loads until the first search.

        Args:
            root: The docs content folder; ``None`` uses ``DOCS_AGENT_CONTENT_DIR``
                or the repository's frontend folder.
            remote_url: The ``/llms-full.txt`` URL used when the folder is
                missing; ``None`` builds it from ``APP_PUBLIC_URL``, or from
                the hosted app URL in local mode.
        """
        configured = settings.docs_agent_content_dir.strip()
        self._root = root or (Path(configured) if configured else _REPO_DOCS_DIR)
        base_url = settings.hosted_app_url if settings.local_mode else settings.app_public_url
        self._remote_url = remote_url or f"{base_url.rstrip('/')}/llms-full.txt"
        self._lock = threading.Lock()
        self._indexes: dict[str, _Bm25Index] = {}
        self._loaded_at: float | None = None
        self._remote = False

    def _load(self) -> None:
        """Build the indexes from the folder, or from ``/llms-full.txt`` without it."""
        if self._root.is_dir():
            corpus = load_folder(self._root)
            self._remote = False
        else:
            response = httpx.get(self._remote_url, timeout=_REMOTE_TIMEOUT_SECONDS, follow_redirects=True)
            response.raise_for_status()
            corpus = {DEFAULT_LOCALE: parse_llms_full(response.text)}
            self._remote = True
        self._indexes = {locale: build_index(passages) for locale, passages in corpus.items() if passages}
        self._loaded_at = time.monotonic()

    def _ensure_loaded(self) -> None:
        """Load once, and reload the remote copy after its TTL.

        Raises:
            httpx.HTTPError: When the folder is missing and the remote fetch fails.
            OSError: When the folder cannot be read.
        """
        with self._lock:
            stale = self._remote and self._loaded_at is not None
            stale = stale and time.monotonic() - self._loaded_at > _REMOTE_TTL_SECONDS
            if self._loaded_at is None or stale or not self._indexes:
                self._load()

    def search(self, query: str, locale: str, *, limit: int = 5) -> list[tuple[Passage, float]]:
        """Return the best passages for ``query`` in ``locale``, else in English.

        Args:
            query: The user's question.
            locale: The UI locale; a locale without docs uses English.
            limit: The most passages to return.

        Returns:
            ``(passage, score)`` pairs, best first.

        Raises:
            httpx.HTTPError: When no docs source can be loaded.
            OSError: When the folder cannot be read.
        """
        self._ensure_loaded()
        index = self._indexes.get(locale) or self._indexes.get(DEFAULT_LOCALE)
        if index is None:
            return []
        hits = search(index, query, limit=limit)
        if not hits and index is not self._indexes.get(DEFAULT_LOCALE) and DEFAULT_LOCALE in self._indexes:
            # A question typed in English on a translated page still deserves an answer.
            hits = search(self._indexes[DEFAULT_LOCALE], query, limit=limit)
        return hits
