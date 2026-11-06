"""Handlers for account-level reads: wallet, model catalog, discovery, registry.

Also hosts ``update_user_preferences``, which mirrors the real client-facing
settings write and is registered non-mutating so it never trips a task's
server-mutation guard.
"""

from __future__ import annotations

from typing import Any

from bench.world import ToolError, World, tool

PREFERENCE_KEYS = (
    "advanced_mode",
    "expand_advanced",
    "lite_mode",
    "wizard_code_assist",
    "wizard_split_mode",
    "tagger_assist",
)


@tool("get_wallet_for_agent")
def get_wallet(w: World, args: dict[str, Any]) -> dict[str, Any]:
    """Return the caller's purchased balance, free grant, and recent ledger.

    Returns:
        The wallet with a computed ``spendable_cents`` total and the ledger
        trimmed to the 15 most recent entries, newest first.
    """
    wallet = w.s["wallet"]
    ledger = list(reversed(wallet["ledger"]))[:15]
    return {
        "paid_balance_cents": wallet["paid_balance_cents"],
        "free_grant": dict(wallet["free_grant"]),
        "spendable_cents": wallet["paid_balance_cents"]
        + wallet["free_grant"]["cents_remaining"],
        "ledger": ledger,
    }


@tool("list_models_for_agent")
def list_models(w: World, args: dict[str, Any]) -> dict[str, Any]:
    """List models; copy each entry's ``name`` verbatim into submissions.

    Args:
        w: The world.
        args: Optional ``query`` substring (case-insensitive) to filter by name.

    Returns:
        ``{"models": [...], "total": n}``.
    """
    query = (args.get("query") or "").lower()
    models = [m for m in w.s["models"] if query in m["name"].lower()]
    return {"models": [dict(m) for m in models], "total": len(models)}


@tool("discover_models_models_discover_post")
def discover_models(w: World, args: dict[str, Any]) -> dict[str, Any]:
    """Probe an OpenAI-compatible endpoint for its model list.

    Args:
        w: The world.
        args: ``base_url`` and optional ``api_key``.

    Returns:
        ``{"base_url", "models", "error"}``. ``error`` is set (and ``models``
        empty) when the endpoint is unreachable or an API key is required but
        was not supplied.
    """
    base_url = (args.get("base_url") or "").rstrip("/")
    endpoint = w.s["endpoints"].get(base_url)
    if endpoint is None:
        return {
            "base_url": base_url,
            "models": [],
            "error": "Could not reach endpoint or it is not OpenAI-compatible.",
        }
    if endpoint.get("error"):
        return {"base_url": base_url, "models": [], "error": endpoint["error"]}
    if endpoint.get("needs_key") and not args.get("api_key"):
        return {
            "base_url": base_url,
            "models": [],
            "error": "This endpoint requires an API key.",
        }
    return {"base_url": base_url, "models": list(endpoint["models"]), "error": None}


@tool("get_registry_snapshot_registry_get")
def get_registry_snapshot(w: World, args: dict[str, Any]) -> dict[str, Any]:
    """Return sorted names of every registered module, metric, and optimizer."""
    reg = w.s["registry"]
    return {
        "modules": sorted(reg["modules"]),
        "metrics": sorted(reg["metrics"]),
        "optimizers": sorted(reg["optimizers"]),
    }


@tool("update_user_preferences")
def update_user_preferences(w: World, args: dict[str, Any]) -> dict[str, Any]:
    """Merge the supplied preference fields and return the full preference set.

    Raises:
        ToolError: 422 when an unknown preference key is supplied.
    """
    prefs = w.s["preferences"]
    unknown = [k for k in args if k not in PREFERENCE_KEYS]
    if unknown:
        raise ToolError(422, f"unknown preference(s): {', '.join(unknown)}")
    for key in PREFERENCE_KEYS:
        if key in args:
            prefs[key] = args[key]
    return dict(prefs)
