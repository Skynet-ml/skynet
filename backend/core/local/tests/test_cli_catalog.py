"""Tests for the subscription model catalog and its model-id parser."""

from __future__ import annotations

import pytest

from core.local.cli_catalog import (
    CLI_EFFORTS,
    CLI_FAST,
    CLI_MODELS,
    MIN_VERSIONS,
    cli_efforts,
    cli_fast,
    cli_model_arg,
    current_cli_slug,
    parse_subscription_model_id,
)


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("subscription/claude/anthropic/claude-sonnet-4.6", ("claude", "anthropic/claude-sonnet-4.6")),
        ("subscription/codex/openai/gpt-6-sol", ("codex", "openai/gpt-6-sol")),
        ("subscription/codex/openai/gpt-6-sol/", ("codex", "openai/gpt-6-sol")),
    ],
)
def test_parse_subscription_model_id_splits_cli_and_slug(model_id: str, expected: tuple[str, str]) -> None:
    """A well-formed id gives its CLI and catalog slug.

    Args:
        model_id: The wire model id.
        expected: The ``(cli, slug)`` pair it must give.
    """
    assert parse_subscription_model_id(model_id) == expected


@pytest.mark.parametrize(
    "model_id",
    [
        "anthropic/claude-sonnet-4.6",
        "openrouter/subscription/claude/anthropic/claude-sonnet-4.6",
        "subscription/gemini/google/gemini-3.5-flash",
        "subscription/droid/z-ai/glm-5.3",
        "subscription/claude",
        "subscription/claude/",
        "",
    ],
)
def test_parse_subscription_model_id_rejects_other_ids(model_id: str) -> None:
    """An id without the prefix, a known CLI and a slug gives ``None``.

    Args:
        model_id: The wire model id.
    """
    assert parse_subscription_model_id(model_id) is None


def test_cli_model_arg_maps_slug_to_cli_name() -> None:
    """Each CLI gets its own ``--model`` name for a catalog slug."""
    assert cli_model_arg("claude", "anthropic/claude-sonnet-4.6") == "claude-sonnet-4-6"
    assert cli_model_arg("claude", "anthropic/claude-haiku-5.5") == "claude-haiku-5-5"
    assert cli_model_arg("codex", "openai/gpt-6-sol") == "gpt-6-sol"


def test_cli_model_arg_rejects_unmapped_slug() -> None:
    """A slug the CLI does not serve raises instead of guessing a name."""
    with pytest.raises(ValueError, match="no model"):
        cli_model_arg("codex", "anthropic/claude-sonnet-4.6")
    with pytest.raises(ValueError, match="Unknown CLI"):
        cli_model_arg("gemini", "google/gemini-3.5-flash")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Unknown CLI"):
        cli_model_arg("droid", "anthropic/claude-haiku-4.5")  # type: ignore[arg-type]


def test_every_cli_has_models_and_a_minimum_version() -> None:
    """The catalog and the version table cover the same two CLIs, and Droid is not one."""
    assert set(CLI_MODELS) == set(MIN_VERSIONS) == {"codex", "claude"}
    assert all(CLI_MODELS[cli] for cli in CLI_MODELS)


def test_every_cli_model_has_an_effort_row() -> None:
    """Every slug a CLI runs has its own effort levels, in ladder order, from the known ladder."""
    ladder = ["low", "medium", "high", "xhigh", "max"]
    for cli, models in CLI_MODELS.items():
        assert set(CLI_EFFORTS[cli]) == set(models)
        for levels in CLI_EFFORTS[cli].values():
            assert levels
            assert list(levels) == [level for level in ladder if level in levels]


@pytest.mark.parametrize(
    ("cli", "slug", "levels"),
    [
        ("claude", "anthropic/claude-opus-5.5", ("low", "medium", "high", "xhigh", "max")),
        ("claude", "anthropic/claude-haiku-5.5", ("low", "medium", "high", "xhigh", "max")),
        ("claude", "anthropic/claude-opus-4.6", ("low", "medium", "high", "max")),
        ("claude", "anthropic/claude-sonnet-4.6", ("low", "medium", "high", "max")),
        ("claude", "anthropic/claude-opus-4.5", ("low", "medium", "high")),
        ("claude", "anthropic/claude-sonnet-4.5", ()),
        ("claude", "anthropic/claude-haiku-4.5", ()),
        ("codex", "openai/gpt-6.1-sol", ("low", "medium", "high", "xhigh", "max")),
        ("codex", "openai/gpt-5.6-luna", ("low", "medium", "high", "xhigh", "max")),
        ("codex", "openai/gpt-6-luna", ("low", "medium", "high", "xhigh", "max")),
        ("claude", "anthropic/claude-opus-4.7", ("low", "medium", "high", "xhigh", "max")),
        ("claude", "anthropic/claude-unknown", ()),
        ("droid", "anthropic/claude-opus-5.5", ()),
    ],
)
def test_cli_efforts_gives_the_levels_the_cli_takes(cli: str, slug: str, levels: tuple[str, ...]) -> None:
    """Older Claude models take fewer levels, and an unknown CLI or slug takes none.

    Args:
        cli: The CLI.
        slug: The catalog slug.
        levels: The levels the CLI takes for it.
    """
    assert cli_efforts(cli, slug) == levels


def test_no_cli_model_offers_a_retired_level() -> None:
    """Ultra and ultracode only add subagents, so no CLI model lists them."""
    for cli in CLI_EFFORTS:
        for slug, levels in CLI_EFFORTS[cli].items():
            assert not {"ultra", "ultracode"} & set(levels), slug


@pytest.mark.parametrize(
    ("cli", "slug", "fast"),
    [
        ("claude", "anthropic/claude-opus-5.5", True),
        ("claude", "anthropic/claude-opus-4.8", True),
        ("claude", "anthropic/claude-opus-4.7", False),
        ("claude", "anthropic/claude-sonnet-5.5", False),
        ("codex", "openai/gpt-6.1-sol", True),
        ("codex", "openai/gpt-5.6-luna", True),
        ("codex", "openai/gpt-unknown", False),
        ("droid", "anthropic/claude-opus-5.5", False),
    ],
)
def test_cli_fast_names_the_models_with_fast_mode(cli: str, slug: str, fast: bool) -> None:
    """Fast mode is on every Codex model and on Claude's Opus 4.8 and Opus 5 models only.

    Args:
        cli: The CLI.
        slug: The catalog slug.
        fast: Whether the model takes fast mode on ``cli``.
    """
    assert cli_fast(cli, slug) is fast


def test_every_fast_model_is_a_cli_model() -> None:
    """The fast table names only slugs the CLI runs."""
    for cli, slugs in CLI_FAST.items():
        assert slugs <= set(CLI_MODELS[cli])


@pytest.mark.parametrize("slug", ["anthropic/claude-sonnet-4.5", "anthropic/claude-haiku-4.5"])
def test_a_removed_cli_model_runs_the_default(slug: str) -> None:
    """A stored pick of a removed Claude slug runs the CLI's default model."""
    default = next(iter(CLI_MODELS["claude"]))
    assert current_cli_slug("claude", slug) == default
    assert cli_model_arg("claude", slug) == CLI_MODELS["claude"][default]
    assert current_cli_slug("claude", "anthropic/claude-unknown-1") is None
    with pytest.raises(ValueError):
        cli_model_arg("claude", "anthropic/claude-unknown-1")
