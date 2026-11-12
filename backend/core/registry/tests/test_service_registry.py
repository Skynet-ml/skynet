"""Unit tests for ``ServiceRegistry``."""

from __future__ import annotations

from typing import Any

import pytest

from core.registry.core import (
    DuplicateRegistrationError,
    ServiceRegistry,
)


def _module_factory(**_kwargs: Any) -> object:
    """Return a placeholder module instance for registry tests."""
    return object()


def _optimizer_factory(**_kwargs: Any) -> object:
    """Return a placeholder optimizer instance for registry tests."""
    return object()


def _noop() -> None:
    """No-op factory used when the test only inspects identity."""


@pytest.fixture
def registry() -> ServiceRegistry:
    """Yield a fresh empty ``ServiceRegistry`` for each test.

    Returns:
        A new ``ServiceRegistry`` instance with no entries.
    """
    return ServiceRegistry()


def test_register_module_then_get_returns_factory(registry: ServiceRegistry) -> None:
    """Register module then get returns factory."""
    registry.register_module("my_module", _module_factory)

    assert registry.modules["my_module"] is _module_factory


def test_register_optimizer_then_get_returns_factory(registry: ServiceRegistry) -> None:
    """Register optimizer then get returns factory."""
    registry.register_optimizer("bootstrap", _optimizer_factory)

    assert registry.optimizers["bootstrap"] is _optimizer_factory


@pytest.mark.parametrize(
    "register",
    ["register_module", "register_optimizer"],
    ids=["module", "optimizer"],
)
def test_duplicate_registration_raises(registry: ServiceRegistry, register: str) -> None:
    """Duplicate registration raises."""
    getattr(registry, register)("name", _noop)

    with pytest.raises(DuplicateRegistrationError, match="already registered"):
        getattr(registry, register)("name", _noop)


def test_snapshot_empty_registry_returns_empty_lists(registry: ServiceRegistry) -> None:
    """Snapshot empty registry returns empty lists."""
    snap = registry.snapshot()

    assert snap == {"modules": [], "metrics": [], "optimizers": []}


def test_snapshot_reflects_registered_names(registry: ServiceRegistry) -> None:
    """Snapshot reflects registered names."""
    registry.register_module("mod_b", _noop)
    registry.register_module("mod_a", _noop)
    registry.register_optimizer("fake_opt", _noop)

    snap = registry.snapshot()

    assert snap["modules"] == ["mod_a", "mod_b"]
    assert snap["metrics"] == []
    assert snap["optimizers"] == ["fake_opt"]


def test_snapshot_updates_after_new_registration(registry: ServiceRegistry) -> None:
    """Snapshot updates after new registration."""
    registry.register_module("first", _noop)
    snap_before = registry.snapshot()

    registry.register_module("second", _noop)
    snap_after = registry.snapshot()

    assert "second" not in snap_before["modules"]
    assert "second" in snap_after["modules"]


def test_two_registries_do_not_share_state() -> None:
    """Two registries do not share state."""
    reg_a = ServiceRegistry()
    reg_b = ServiceRegistry()

    reg_a.register_module("shared_name", _noop)

    assert "shared_name" not in reg_b.modules


def test_duplicate_check_is_per_instance_not_global() -> None:
    """Duplicate check is per instance not global."""
    reg_a = ServiceRegistry()
    reg_b = ServiceRegistry()

    reg_a.register_module("name", _noop)
    reg_b.register_module("name", _noop)

    assert reg_b.modules["name"] is _noop
