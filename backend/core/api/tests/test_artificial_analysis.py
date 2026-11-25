"""Tests for the verified Artificial Analysis model links."""

from __future__ import annotations

import time
import urllib.error
from unittest.mock import patch

import pytest

from .. import artificial_analysis as aa
from ..model_catalog import CatalogModel, ModelCatalogResponse

# conftest stubs ``_fetch_head`` offline per test; keep the real one for error handling.
_REAL_FETCH_HEAD = aa._fetch_head

_PAGE = (
    "<html><head><title>{title} - Intelligence, Performance &amp; Price Analysis | Artificial Analysis</title>"
    '<link rel="canonical" href="https://artificialanalysis.ai/models/{slug}"/></head>'
)


def _page(slug: str, title: str) -> tuple[int, str, str]:
    """Build a fake fetch result for a real model page.

    Args:
        slug: The page slug.
        title: The model name in the page title.

    Returns:
        A ``_fetch_head`` result.
    """
    url = aa._BASE_URL + slug
    return 200, url, _PAGE.format(title=title, slug=slug)


def _model(value: str, name: str | None = "Claude Opus 4.5", index: float | None = 50.0) -> CatalogModel:
    """Build a catalog model.

    Args:
        value: Catalog id.
        name: Display name.
        index: Intelligence index.

    Returns:
        The model.
    """
    return CatalogModel(value=value, label=value, provider="openrouter", display_name=name, intelligence_index=index)


@pytest.fixture(autouse=True)
def _reset_cache():
    """Start every test with an empty link cache."""
    aa._links.clear()
    aa._refresh_in_flight = False
    yield
    aa._links.clear()
    aa._refresh_in_flight = False


def test_slug_candidates_cover_id_name_and_claude_order() -> None:
    """Candidates come from the id, the Claude family/version swap and the display name."""
    assert aa.slug_candidates("openrouter/anthropic/claude-haiku-4.5", "Claude Haiku 4.5") == [
        "claude-haiku-4-5",
        "claude-4-5-haiku",
    ]
    assert aa.slug_candidates("openrouter/google/gemma-3-27b-it", "Gemma 3 27B") == ["gemma-3-27b-it", "gemma-3-27b"]
    assert aa.slug_candidates("openrouter/qwen/qwen3.8-max-0902", None) == ["qwen3-8-max-0902", "qwen3-8-max"]
    assert aa.slug_candidates("openrouter/openai/gpt-5-2025-08-07", "GPT-5") == ["gpt-5"]


@pytest.mark.parametrize(
    ("name", "title", "expected"),
    [
        ("Claude Haiku 4.5", "Claude 4.5 Haiku (non-reasoning) - Intelligence", True),
        ("MiniMax M2.7", "MiniMax-M2.7 - Intelligence", True),
        ("Gemini 3.1 Flash Lite Preview", "Gemini 3.1 Flash-Lite - Intelligence", True),
        ("Qwen3.8 Max (0902)", "Qwen3.8 Max (0902) - Intelligence", True),
        ("Claude Fable 5.2", "Claude Fable 5.2 Models - Intelligence", True),
        ("gpt-oss-120b", "gpt-oss-120b Models - Intelligence", True),
        ("DeepSeek V4 Pro 0423", "DeepSeek V4 Pro 0813 Models - Intelligence", False),
        ("DeepSeek V4 Pro 0423", "DeepSeek V4 Pro 0813 (max) - Intelligence", False),
        ("Mistral Small 4", "Mistral Small (Sep) - Intelligence", False),
        ("DeepSeek V3.2 Exp", "DeepSeek V3.2 (non-reasoning) - Intelligence", False),
        ("GPT-4o (2024-08-06)", "GPT-4o (Nov) - Intelligence", False),
    ],
)
def test_names_match(name: str, title: str, expected: bool) -> None:
    """Only a title naming the same release counts, in any word order."""
    assert aa.names_match(name, title) is expected


def test_find_page_skips_a_page_for_another_release() -> None:
    """A live slug whose title names a different release is not linked."""
    pages = {
        aa._BASE_URL + "deepseek-v4-pro": _page("deepseek-v4-pro", "DeepSeek V4 Pro 0813 (max)"),
    }
    with patch.object(aa, "_fetch_head", side_effect=lambda url: pages.get(url, (404, url, ""))):
        assert aa.find_page(_model("openrouter/deepseek/deepseek-v4-pro", "DeepSeek V4 Pro 0423")) is None
        assert (
            aa.find_page(_model("openrouter/deepseek/deepseek-v4-pro-0813", "DeepSeek V4 Pro 0813"))
            == aa._BASE_URL + "deepseek-v4-pro"
        )


def test_verify_page_rejects_redirects_and_missing_canonical() -> None:
    """A redirected or generic page never verifies."""
    url = aa._BASE_URL + "gpt-5"
    with patch.object(aa, "_fetch_head", return_value=(200, aa._BASE_URL + "other", "")):
        assert not aa.verify_page("gpt-5", "GPT-5")
    with patch.object(aa, "_fetch_head", return_value=(200, url, "<title>Artificial Analysis</title>")):
        assert not aa.verify_page("gpt-5", "GPT-5")
    with patch.object(aa, "_fetch_head", return_value=_page("gpt-5", "GPT-5 (high)")):
        assert aa.verify_page("gpt-5", "GPT-5")


def test_with_links_never_blocks_and_attaches_cached_pages() -> None:
    """The first call starts a background check; cached links are attached."""
    rated = _model("openrouter/anthropic/claude-opus-4.5")
    unrated = _model("openrouter/x/unrated", index=None)
    catalog = ModelCatalogResponse(providers=[], models=[rated, unrated])
    with patch.object(aa, "Thread") as thread:
        out = aa.with_artificial_analysis_links(catalog)
    assert [m.artificial_analysis_url for m in out.models] == [None, None]
    assert thread.call_args.kwargs["args"] == ([rated],)

    aa._refresh_in_flight = False
    aa._links[rated.value] = (aa._BASE_URL + "claude-opus-4-5", time.monotonic() + 60)
    with patch.object(aa, "Thread") as thread:
        out = aa.with_artificial_analysis_links(catalog)
    thread.assert_not_called()
    assert out.models[0].artificial_analysis_url == aa._BASE_URL + "claude-opus-4-5"
    assert out.models[1].artificial_analysis_url is None


def test_refresh_caches_failures_briefly() -> None:
    """A network failure caches no link and expires within the hour."""
    model = _model("openrouter/anthropic/claude-opus-4.5")
    with patch.object(aa, "_fetch_head", side_effect=TimeoutError("slow")):
        aa._refresh([model])
    url, expires = aa._links[model.value]
    assert url is None
    assert expires - time.monotonic() <= aa._ERROR_TTL_SECONDS
    assert aa._refresh_in_flight is False


@pytest.mark.parametrize("code", [403, 429, 503])
def test_refresh_caches_http_errors_other_than_404_briefly(code: int) -> None:
    """A rate limit, bot challenge or server error is retried within the hour, not a day.

    Args:
        code: HTTP status the page answers with.
    """
    model = _model("openrouter/anthropic/claude-opus-4.5")

    def _raise(request: object, timeout: float) -> None:
        """Fail the request like an HTTP error response would."""
        raise urllib.error.HTTPError(aa._BASE_URL, code, "error", {}, None)

    with (
        patch.object(aa, "_fetch_head", _REAL_FETCH_HEAD),
        patch.object(aa.urllib.request, "urlopen", side_effect=_raise),
    ):
        aa._refresh([model])
    url, expires = aa._links[model.value]
    assert url is None
    assert expires - time.monotonic() <= aa._ERROR_TTL_SECONDS
