"""Tests for the Qwen Code harness: registry entry, pinned install, gateway-only launch, protected env and output parsing."""

from __future__ import annotations

from core.models.blackbox import BLACKBOX_HARNESS_QWEN_CODE

from ..harnesses import REGISTRY
from ..harnesses.qwen_code import (
    QWEN_CODE_EFFORTS,
)


def test_spec_declares_exactly_the_levels_it_passes() -> None:
    """Declare the Qwen Code settings schema levels, so submit refuses any other level before paid work."""
    assert REGISTRY[BLACKBOX_HARNESS_QWEN_CODE].supported_efforts == QWEN_CODE_EFFORTS
    assert QWEN_CODE_EFFORTS == ("none", "low", "medium", "high", "xhigh", "max")


def _merge(target: dict, source: dict) -> dict:
    """Merge ``source`` into ``target`` like Qwen Code's ``mergeRecursively`` for these keys.

    Args:
        target: The merged settings so far.
        source: The next scope, which wins.

    Returns:
        ``target``.
    """
    for key, value in source.items():
        if isinstance(value, dict):
            target[key] = _merge(target[key] if isinstance(target.get(key), dict) else {}, value)
        else:
            target[key] = value
    return target


def _fill_env(value: object, env: dict[str, str]) -> object:
    """Fill ``${NAME}`` strings from ``env``, like Qwen Code's ``resolveEnvVarsInObject``.

    Args:
        value: A parsed settings value.
        env: The process env.

    Returns:
        The value with each ``${NAME}`` string filled.
    """
    if isinstance(value, dict):
        return {key: _fill_env(item, env) for key, item in value.items()}
    if isinstance(value, list):
        return [_fill_env(item, env) for item in value]
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        return env.get(value[2:-1], "")
    return value
