"""Catalog of the coding CLIs local mode can run model calls through.

A subscription model is named on the wire as ``subscription/<cli>/<slug>``,
where ``<slug>`` is the OpenRouter catalog slug the wizard already shows (for
example ``subscription/claude/anthropic/claude-sonnet-4.6``). ``CLI_MODELS``
maps each slug to the ``--model`` argument that CLI accepts.
"""

from __future__ import annotations

from typing import Literal, get_args

CliProvider = Literal["codex", "claude"]

SUBSCRIPTION_MODEL_PREFIX = "subscription/"

# The desktop shell (SKY-116) keeps a copy of this table for its first-run
# check. Change both together.
MIN_VERSIONS: dict[CliProvider, str] = {
    "claude": "2.1.226",
    "codex": "0.160.0",
}

# Model ids come from Codex's own model list (``codex debug models``) and the
# Claude API model ids that ``claude --model`` accepts. Every key is a slug in the public OpenRouter catalog.
CLI_MODELS: dict[CliProvider, dict[str, str]] = {
    "codex": {
        "openai/gpt-6.1-sol": "gpt-6.1-sol",
        "openai/gpt-6-sol": "gpt-6-sol",
        "openai/gpt-6-astra": "gpt-6-astra",
        "openai/gpt-6-luna": "gpt-6-luna",
        "openai/gpt-5.6-sol": "gpt-5.6-sol",
        "openai/gpt-5.6-terra": "gpt-5.6-terra",
        "openai/gpt-5.6-luna": "gpt-5.6-luna",
    },
    "claude": {
        "anthropic/claude-fable-5.1": "claude-fable-5-1",
        "anthropic/claude-fable-5": "claude-fable-5",
        "anthropic/claude-opus-5.5": "claude-opus-5-5",
        "anthropic/claude-opus-5": "claude-opus-5",
        "anthropic/claude-opus-4.8": "claude-opus-4-8",
        "anthropic/claude-opus-4.7": "claude-opus-4-7",
        "anthropic/claude-opus-4.6": "claude-opus-4-6",
        "anthropic/claude-opus-4.5": "claude-opus-4-5-20251101",
        "anthropic/claude-sonnet-5.5": "claude-sonnet-5-5",
        "anthropic/claude-sonnet-5": "claude-sonnet-5",
        "anthropic/claude-sonnet-4.6": "claude-sonnet-4-6",
        "anthropic/claude-sonnet-4.5": "claude-sonnet-4-5-20250929",
        "anthropic/claude-haiku-5.5": "claude-haiku-5-5",
        "anthropic/claude-haiku-4.5": "claude-haiku-4-5",
    },
}

_FULL_EFFORTS = ("low", "medium", "high", "xhigh", "max")
# The top level of each CLI, offered only on the models that list it. Claude
# Code's ``--effort ultracode`` runs many parallel agents each turn; Codex's
# ``ultra`` is one more reasoning level above max.
CliTopEffort = Literal["ultra", "ultracode"]
_CODEX_ULTRA = (*_FULL_EFFORTS, "ultra")
_CLAUDE_ULTRACODE = (*_FULL_EFFORTS, "ultracode")
# The effort levels each CLI takes for each slug in CLI_MODELS. Checked with
# no model call against Claude Code 2.1.316 (its shipped model catalog and
# effort gates) and codex-cli 0.163.0 (``codex debug models``). Claude Code
# ignores ``--effort`` for a model with no levels, and runs an unsupported
# xhigh or max at high. Ultracode needs a model that takes xhigh. Recheck
# this table when a CLI updates.
CLI_EFFORTS: dict[CliProvider, dict[str, tuple[str, ...]]] = {
    "codex": {
        **dict.fromkeys(CLI_MODELS["codex"], _CODEX_ULTRA),
        "openai/gpt-6-luna": _FULL_EFFORTS,
        "openai/gpt-5.6-luna": _FULL_EFFORTS,
    },
    "claude": {
        **dict.fromkeys(CLI_MODELS["claude"], _CLAUDE_ULTRACODE),
        "anthropic/claude-opus-4.6": ("low", "medium", "high", "max"),
        "anthropic/claude-sonnet-4.6": ("low", "medium", "high", "max"),
        "anthropic/claude-opus-4.5": ("low", "medium", "high"),
        "anthropic/claude-sonnet-4.5": (),
        "anthropic/claude-haiku-4.5": (),
    },
}
# The slugs that take fast mode, from the same check. Every Codex model
# lists the ``priority`` ("Fast") service tier. Claude Code offers
# ``--fast-mode`` only on Opus 4.8 and Opus 5 models, and only to accounts
# with usage credits; Claude Code checks the account itself.
CLI_FAST: dict[CliProvider, frozenset[str]] = {
    "codex": frozenset(CLI_MODELS["codex"]),
    "claude": frozenset({"anthropic/claude-opus-5.5", "anthropic/claude-opus-5", "anthropic/claude-opus-4.8"}),
}

_CLI_PROVIDERS: tuple[str, ...] = get_args(CliProvider)


def cli_efforts(cli: str, slug: str) -> tuple[str, ...]:
    """Return the effort levels ``cli`` takes for a catalog slug.

    Args:
        cli: The CLI that runs the call.
        slug: OpenRouter catalog slug, such as ``anthropic/claude-sonnet-4.6``.

    Returns:
        The levels, lowest first; empty for an unknown CLI or slug.
    """
    return CLI_EFFORTS.get(cli, {}).get(slug, ())  # type: ignore[call-overload]


def cli_fast(cli: str, slug: str) -> bool:
    """Return whether ``cli`` takes fast mode for a catalog slug.

    Args:
        cli: The CLI that runs the call.
        slug: OpenRouter catalog slug, such as ``anthropic/claude-opus-5.5``.

    Returns:
        ``True`` when the model takes fast mode on ``cli``.
    """
    return slug in CLI_FAST.get(cli, frozenset())  # type: ignore[call-overload]


def parse_subscription_model_id(model_id: str) -> tuple[CliProvider, str] | None:
    """Split a ``subscription/<cli>/<slug>`` model id into its CLI and slug.

    Args:
        model_id: A model id from a run payload, optionally ``openrouter/``-free.

    Returns:
        ``(cli, slug)`` when the id names a known CLI and a non-empty slug,
        else ``None``. The slug is not checked against ``CLI_MODELS``.
    """
    if not model_id.startswith(SUBSCRIPTION_MODEL_PREFIX):
        return None
    cli, _, slug = model_id.removeprefix(SUBSCRIPTION_MODEL_PREFIX).partition("/")
    slug = slug.strip("/")
    if cli not in _CLI_PROVIDERS or not slug:
        return None
    return cli, slug  # type: ignore[return-value]


def cli_model_arg(cli: CliProvider, slug: str) -> str:
    """Return the ``--model`` argument ``cli`` takes for a catalog slug.

    Args:
        cli: The CLI that runs the call.
        slug: OpenRouter catalog slug, such as ``anthropic/claude-sonnet-4.6``.

    Returns:
        The CLI's own model name for that slug.

    Raises:
        ValueError: When ``cli`` is unknown or has no model for ``slug``.
    """
    models = CLI_MODELS.get(cli)
    if models is None:
        raise ValueError(f"Unknown CLI {cli!r}")
    try:
        return models[slug]
    except KeyError:
        raise ValueError(f"{cli} has no model for {slug!r}") from None
