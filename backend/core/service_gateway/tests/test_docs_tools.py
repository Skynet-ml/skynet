"""Tests for the generalist agent's docs search tool."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core.service_gateway.agents import docs_tools
from core.service_gateway.agents.docs_retrieval import DocsCorpus
from core.service_gateway.agents.generalist import GENERALIST_SYSTEM_PROMPT


@pytest.fixture
def corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DocsCorpus:
    """Point the tool at a two-locale docs folder.

    Args:
        tmp_path: Pytest temporary folder.
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        The corpus the tool now searches.
    """
    (tmp_path / "en" / "concepts").mkdir(parents=True)
    (tmp_path / "en" / "concepts" / "billing.md").write_text(
        "Intro.\n\n## Spending limit\n\nEvery run has a spending limit in USD.\n", encoding="utf-8"
    )
    (tmp_path / "en" / "quickstart.md").write_text("## First run\n\nUpload a dataset to start.\n", encoding="utf-8")
    (tmp_path / "he").mkdir()
    (tmp_path / "he" / "quickstart.md").write_text("## ריצה ראשונה\n\nהעלו מאגר נתונים.\n", encoding="utf-8")
    docs = DocsCorpus(tmp_path, remote_url="http://unused.invalid/llms-full.txt")
    monkeypatch.setattr(docs_tools, "_corpus", docs)
    return docs


def test_search_returns_passages_with_docs_links(corpus: DocsCorpus) -> None:
    """A matching question returns the passage text, its heading and its relative docs link."""
    result = asyncio.run(docs_tools.search_docs("spending limit"))

    assert result["query"] == "spending limit"
    assert result["result_count"] == len(result["results"]) >= 1
    top = result["results"][0]
    assert top == {
        "title": "Spending limit",
        "href": "/docs/concepts/billing",
        "text": "Every run has a spending limit in USD.",
    }


def test_search_uses_the_locale_pages(corpus: DocsCorpus) -> None:
    """A Hebrew question on the Hebrew docs links to the Hebrew page."""
    result = asyncio.run(docs_tools.search_docs("ריצה ראשונה", locale="he"))

    assert result["results"][0]["href"] == "/docs/he/quickstart"


def test_search_falls_back_to_english_for_unknown_locale(corpus: DocsCorpus) -> None:
    """A locale without docs answers from the English pages."""
    result = asyncio.run(docs_tools.search_docs("dataset", locale="xx"))

    assert result["results"][0]["href"] == "/docs/quickstart"


def test_search_with_no_match_returns_empty_results(corpus: DocsCorpus) -> None:
    """A question the docs do not cover returns no results, not an error."""
    result = asyncio.run(docs_tools.search_docs("zzqx unrelated"))

    assert result == {"query": "zzqx unrelated", "results": [], "result_count": 0}


def test_search_clamps_the_limit(corpus: DocsCorpus) -> None:
    """The limit stays between one passage and the cap."""
    assert asyncio.run(docs_tools.search_docs("run", limit=0))["result_count"] == 1
    assert asyncio.run(docs_tools.search_docs("run", limit=99))["result_count"] <= docs_tools._MAX_RESULTS


def test_build_names_the_docs_tool() -> None:
    """The builder offers exactly ``search_docs``."""
    assert [tool.name for tool in docs_tools.build_docs_tools()] == [docs_tools.SEARCH_DOCS_TOOL]


def test_prompt_tells_the_agent_to_answer_from_the_docs() -> None:
    """The system prompt names the tool and asks for relative docs links."""
    assert "search_docs" in GENERALIST_SYSTEM_PROMPT
    assert "/docs/" in GENERALIST_SYSTEM_PROMPT
