"""Tests for the registry alias wiring that the API app uses."""

from core.registry import ServiceRegistry
from core.registry.resolvers import MODULE_ALIASES, OPTIMIZER_ALIASES
from core.service_gateway.service_builder import wire_registry_aliases


def test_wire_registry_aliases_registers_everything():
    """Every optimizer/module alias resolves on a fresh registry."""
    registry = wire_registry_aliases(ServiceRegistry())
    for alias in OPTIMIZER_ALIASES:
        assert alias in registry.optimizers
    for alias in MODULE_ALIASES:
        assert alias in registry.modules


def test_wire_registry_aliases_keeps_prebuilt_entries():
    """A pre-registered factory wins over the alias default."""
    registry = ServiceRegistry()
    sentinel = object()
    alias = next(iter(OPTIMIZER_ALIASES))
    registry.register_optimizer(alias, sentinel)
    wire_registry_aliases(registry)
    assert registry.optimizers[alias] is sentinel
