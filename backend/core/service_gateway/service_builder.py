"""Alias wiring for the optimizer and module registry that the API app uses.

The wiring lives outside ``create_app`` so it stays importable without the API.
"""

from __future__ import annotations

from ..registry import ServiceRegistry
from ..registry.resolvers import (
    MODULE_ALIASES,
    OPTIMIZER_ALIASES,
    resolve_module_factory,
    resolve_optimizer_factory,
)


def wire_registry_aliases(registry: ServiceRegistry) -> ServiceRegistry:
    """Register the alias-backed optimizer/module factories onto ``registry``.

    Skips names already registered so a pre-built registry (tests, custom
    deployments) wins.

    Args:
        registry: The registry to populate.

    Returns:
        The same registry, for chaining.
    """
    for alias in OPTIMIZER_ALIASES:
        if alias not in registry.optimizers:
            registry.register_optimizer(alias, resolve_optimizer_factory(alias))
    for alias in MODULE_ALIASES:
        if alias not in registry.modules:
            factory, _ = resolve_module_factory(alias)
            registry.register_module(alias, factory)
    return registry
