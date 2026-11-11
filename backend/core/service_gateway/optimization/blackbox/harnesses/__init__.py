"""Harness registry: one module per built-in coding-agent CLI.

Each sibling module builds a :class:`HarnessSpec` and passes it to
:func:`register` when it is imported. Importing this package imports every
sibling module, so a new CLI needs only its own file here. ``base`` holds the
shared types and is not a harness.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from .base import HarnessLaunch

_SKIPPED_MODULES = frozenset({"base"})


@dataclass(frozen=True)
class HarnessSpec:
    """Everything the catalog knows about one built-in harness."""

    id: str
    launch: Callable[..., HarnessLaunch]
    binary: str
    version: str
    # A key of ``harness_bridge.PARSERS``; the bridge ships alone into the
    # sandbox, so it keeps the parser table and the spec only names an entry.
    output_format: str
    # Applied on top of the launch env when the run is protected.
    protected_env: Mapping[str, str] = field(default_factory=dict)
    # Catalog order; it is the order of the "Known harnesses" error text.
    position: int = 1000
    # The Skynet effort levels the CLI accepts; ``None`` means all of them.
    # Submit refuses any other level, before the run reserves budget (SKY-585).
    supported_efforts: tuple[str, ...] | None = None


REGISTRY: dict[str, HarnessSpec] = {}


def register(spec: HarnessSpec) -> HarnessSpec:
    """Add ``spec`` to :data:`REGISTRY` and keep the registry in position order.

    Args:
        spec: The harness to add.

    Returns:
        The same spec, so a module can keep it as a constant.

    Raises:
        ValueError: When another spec already uses the same id.
    """
    if spec.id in REGISTRY:
        raise ValueError(f"Harness '{spec.id}' is already registered.")
    ordered = sorted([*REGISTRY.values(), spec], key=lambda item: (item.position, item.id))
    REGISTRY.clear()
    REGISTRY.update((item.id, item) for item in ordered)
    return spec


def discover() -> None:
    """Import every harness module in this package so each one registers itself."""
    for module in pkgutil.iter_modules(__path__):
        if module.name not in _SKIPPED_MODULES and not module.name.startswith("_"):
            importlib.import_module(f"{__name__}.{module.name}")


discover()
