"""Publish pinned engine contracts and managed execution availability.

Every production proposer runs inside Vercel independently of whether a
candidate is scored directly or executed by a task harness.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ....exceptions import ServiceError
from ....models.blackbox import (
    BLACKBOX_ENGINE_AUTORESEARCH,
    BLACKBOX_ENGINE_AUTOSADDLER,
    BLACKBOX_ENGINE_BEST_OF_N,
    BLACKBOX_ENGINE_GEPA,
    BLACKBOX_ENGINE_META_HARNESS,
    BLACKBOX_ENGINE_SHINKA_EVOLVE,
    BLACKBOX_MULTI_PART_ENGINES,
)
from .autoresearch import AutoResearchEngine
from .autosaddler import AutoSaddlerEngine
from .best_of_n import BestOfNEngine
from .gepa_engine import GepaEngine
from .meta_harness import MetaHarnessEngine
from .protocol import Engine
from .shinka_evolve import ShinkaEvolveEngine

_AGENT_TARGET_REASON = "Meta-Harness optimizes a coding agent's harness; the job's target must be an agent."
_NO_SANDBOX_REASON = "Agent sandboxes are not configured on this deployment."


@dataclass(frozen=True)
class EngineCapabilities:
    """What the deployment and the job offer the engines.

    ``sandbox`` says whether agent sandboxes can be created here (with
    ``sandbox_reason`` explaining why not); ``agent_target`` says whether
    the job's versions drive a coding agent.
    """

    sandbox: bool = False
    agent_target: bool = False
    sandbox_reason: str | None = None
    proposer_available: bool = False
    proposer_reason: str | None = None


NO_CAPABILITIES = EngineCapabilities()


@dataclass(frozen=True)
class EngineSpec:
    """Catalog entry for one engine."""

    id: str
    label: str
    description: str
    factory: Callable[[], Engine] | None = None
    unavailable_reason: str | None = None
    requires_sandbox: bool = False
    requires_agent_target: bool = False
    requires_proposer: bool = False
    checkpoint_recovery_supported: bool = False

    @property
    def supports_parts(self) -> bool:
        """Return whether the engine accepts a multi-part starting point.

        Derived from ``BLACKBOX_MULTI_PART_ENGINES`` so the catalog the UI reads
        can never advertise parts the submit validator rejects.

        Returns:
            ``True`` when the engine is in the validator's multi-part set.
        """
        return self.id in BLACKBOX_MULTI_PART_ENGINES

    def unavailable_reason_for(self, caps: EngineCapabilities) -> str | None:
        """Explain why the engine cannot run for ``caps``, or return ``None`` when it can.

        Args:
            caps: What the deployment and the job offer.

        Returns:
            A user-facing reason, or ``None`` when the engine is runnable.
        """
        if self.factory is None:
            return self.unavailable_reason
        if self.requires_proposer and not caps.proposer_available:
            return caps.proposer_reason or "The upstream proposer runtime is not configured on this deployment."
        if self.requires_sandbox and not caps.sandbox:
            return caps.sandbox_reason or _NO_SANDBOX_REASON
        if self.requires_agent_target and not caps.agent_target:
            return _AGENT_TARGET_REASON
        return None

    def available_for(self, caps: EngineCapabilities) -> bool:
        """Return True when the engine can run for ``caps``.

        Args:
            caps: What the deployment and the job offer.

        Returns:
            Whether :meth:`unavailable_reason_for` is ``None``.
        """
        return self.unavailable_reason_for(caps) is None


ENGINES: dict[str, EngineSpec] = {
    BLACKBOX_ENGINE_GEPA: EngineSpec(
        id=BLACKBOX_ENGINE_GEPA,
        label="GEPA",
        description="Reflective evolution with a Pareto front of versions.",
        factory=GepaEngine,
        checkpoint_recovery_supported=True,
    ),
    BLACKBOX_ENGINE_BEST_OF_N: EngineSpec(
        id=BLACKBOX_ENGINE_BEST_OF_N,
        label="Best-of-N",
        description="Independent proposals from the reflection model; keep the best.",
        factory=BestOfNEngine,
    ),
    BLACKBOX_ENGINE_AUTORESEARCH: EngineSpec(
        id=BLACKBOX_ENGINE_AUTORESEARCH,
        label="AutoResearch",
        description="A coding agent runs research rounds over a Pareto frontier of versions.",
        factory=AutoResearchEngine,
        requires_proposer=True,
    ),
    BLACKBOX_ENGINE_META_HARNESS: EngineSpec(
        id=BLACKBOX_ENGINE_META_HARNESS,
        label="Meta-Harness",
        description="A coding-agent proposer searches harness code using candidate and evaluation history.",
        factory=MetaHarnessEngine,
        requires_proposer=True,
    ),
    BLACKBOX_ENGINE_AUTOSADDLER: EngineSpec(
        id=BLACKBOX_ENGINE_AUTOSADDLER,
        label="AutoSaddler",
        description=(
            "A coding agent diagnoses the scorer's feedback and patches the version; "
            "only patches a fresh scorer run confirms are kept."
        ),
        factory=AutoSaddlerEngine,
        requires_proposer=True,
    ),
    BLACKBOX_ENGINE_SHINKA_EVOLVE: EngineSpec(
        id=BLACKBOX_ENGINE_SHINKA_EVOLVE,
        label="ShinkaEvolve",
        description="Evolves many versions in parallel islands and learns which models improve them most.",
        factory=ShinkaEvolveEngine,
        requires_proposer=True,
    ),
}


def available_engine_ids(caps: EngineCapabilities = NO_CAPABILITIES, *, parts: bool = False) -> list[str]:
    """Return the ids of engines that can run for ``caps``, in catalog order.

    Args:
        caps: What the deployment and the job offer.
        parts: When True, keep only engines that take a multi-part starting point.

    Returns:
        Runnable engine ids.
    """
    return [spec.id for spec in ENGINES.values() if spec.available_for(caps) and (spec.supports_parts or not parts)]


def get_engine(engine_id: str, caps: EngineCapabilities = NO_CAPABILITIES) -> Engine:
    """Instantiate the engine registered under ``engine_id``.

    Args:
        engine_id: Catalog id such as ``"gepa"``.
        caps: What the deployment and the job offer.

    Returns:
        A fresh engine instance.

    Raises:
        ServiceError: When the id is unknown or the engine cannot run for ``caps``.
    """
    spec = ENGINES.get(engine_id)
    if spec is None:
        raise ServiceError(f"Unknown engine '{engine_id}'. Available engines: {', '.join(available_engine_ids(caps))}.")
    reason = spec.unavailable_reason_for(caps)
    if reason is not None or spec.factory is None:
        raise ServiceError(f"Engine '{engine_id}' is not available: {reason}")
    return spec.factory()
