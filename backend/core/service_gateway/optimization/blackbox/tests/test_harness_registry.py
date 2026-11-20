"""Tests for the harness registry: discovery, order, and launch output that matches the pre-registry catalog."""

from __future__ import annotations

import pytest

from core.models.blackbox import BLACKBOX_HARNESSES

from ..harnesses import REGISTRY, HarnessSpec, register


def test_model_harness_ids_match_the_registry() -> None:
    """Fail when a registered harness is missing from the request model's allowed ids."""
    assert (*REGISTRY, "custom") == BLACKBOX_HARNESSES


def test_register_rejects_a_duplicate_id() -> None:
    """Refuse a second spec with an id that is already registered."""
    existing = REGISTRY["pi"]
    with pytest.raises(ValueError, match="already registered"):
        register(HarnessSpec(id="pi", launch=existing.launch, binary="pi", version="0", output_format="pi"))
    assert REGISTRY["pi"] is existing
