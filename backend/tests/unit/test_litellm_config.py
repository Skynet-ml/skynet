"""Regression tests for production LiteLLM cost, concurrency and request-parameter settings."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from litellm.utils import get_optional_params

_CONFIG_PATH = Path(__file__).resolve().parents[3] / "deploy" / "litellm" / "config.yaml"
# Not in LiteLLM's model catalog, as a model newer than the pinned LiteLLM is not.
_UNCATALOGED_MODEL = "example-lab/model-from-next-month"


def test_proxy_has_global_spend_and_parallel_request_backstops() -> None:
    """Keep the proxy's independent launch circuit breakers enabled."""
    config = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8"))

    assert config["litellm_settings"]["max_budget"] == 50
    assert config["litellm_settings"]["budget_duration"] == "1d"
    assert config["general_settings"]["global_max_parallel_requests"] == 64


@pytest.mark.parametrize("effort", ["low", "high", "max"])
def test_proxy_forwards_reasoning_effort_for_models_litellm_does_not_know(effort: str) -> None:
    """A harness's chosen effort survives ``drop_params`` on the OpenRouter wildcard.

    Args:
        effort: The effort a coding harness sends.
    """
    config = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8"))
    (wildcard,) = [entry["litellm_params"] for entry in config["model_list"] if entry["model_name"] == "*"]
    request = {"model": _UNCATALOGED_MODEL, "custom_llm_provider": "openrouter", "reasoning_effort": effort}
    drop_params = config["litellm_settings"]["drop_params"]

    dropped = get_optional_params(**request, drop_params=drop_params)
    kept = get_optional_params(
        **request, drop_params=drop_params, allowed_openai_params=wildcard["allowed_openai_params"]
    )

    assert "reasoning_effort" not in dropped
    assert kept["reasoning_effort"] == effort
