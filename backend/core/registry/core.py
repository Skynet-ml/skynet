"""Service registry holding user-defined DSPy modules, metrics, and optimizers.

Provides ``ServiceRegistry`` and the typed errors it raises so that
service code and tests can register factories by name.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import dspy

ModuleFactory = Callable[..., "dspy.Module"]
MetricFn = Callable[..., float]
OptimizerFactory = Callable[..., Any]


class RegistryError(RuntimeError):
    """Base exception raised for registry-related issues."""


class DuplicateRegistrationError(RegistryError):
    """Raised when a user attempts to register the same key twice."""


@dataclass
class ServiceRegistry:
    """Holds user-defined factories for modules, metrics, and optimizers."""

    modules: dict[str, ModuleFactory] = field(default_factory=dict)
    metrics: dict[str, MetricFn] = field(default_factory=dict)
    optimizers: dict[str, OptimizerFactory] = field(default_factory=dict)

    def register_module(self, name: str, factory: ModuleFactory) -> None:
        """Register a DSPy module factory under a short name.

        Args:
            name: Lookup key for the module factory.
            factory: Callable returning a ``dspy.Module`` instance.

        Raises:
            DuplicateRegistrationError: When ``name`` is already registered.
        """
        self._register(self.modules, name, factory)

    def register_optimizer(self, name: str, factory: OptimizerFactory) -> None:
        """Register an optimizer factory under a short name.

        Args:
            name: Lookup key for the optimizer factory.
            factory: Callable building an optimizer instance.

        Raises:
            DuplicateRegistrationError: When ``name`` is already registered.
        """
        self._register(self.optimizers, name, factory)

    def snapshot(self) -> dict[str, list[str]]:
        """Return sorted registered names keyed by asset type.

        Returns:
            A mapping with sorted name lists under ``modules``, ``metrics``,
            and ``optimizers`` keys.
        """
        return {
            "modules": sorted(self.modules.keys()),
            "metrics": sorted(self.metrics.keys()),
            "optimizers": sorted(self.optimizers.keys()),
        }

    @staticmethod
    def _register(store: dict[str, Any], name: str, value: Any) -> None:
        """Insert ``value`` into ``store`` under ``name``, raising on duplicates.

        Registration is expected at process startup (single-threaded). The
        check-then-set is intentionally lock-free; callers must not
        register concurrently from request-serving threads.

        Args:
            store: Mapping to mutate in place.
            name: Key under which to register ``value``.
            value: Object to store.

        Raises:
            DuplicateRegistrationError: When ``name`` is already in ``store``.
        """
        if name in store:
            raise DuplicateRegistrationError(f"Entry '{name}' already registered.")
        store[name] = value
