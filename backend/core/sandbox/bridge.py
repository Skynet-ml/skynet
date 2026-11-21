"""Protected sandbox runtimes for the trusted parent.

``core.billing.protected_execution`` builds its sandbox here, so it imports no
runtime itself. This module returns the run's chosen provider, or the
deployment default (Docker in local mode, Vercel everywhere else), through the
SBX-11 sync bridge. It also lists the providers a run may choose (SKY-606).
The Vercel provider reserves and settles each usage slice against the run's
budget, and the copy of a user image into its registry is reserved before the
copy and settled on the run too (SKY-891).

``core.sandbox.__init__`` never imports this module, because this module
imports the blackbox runtimes, which import ``core.sandbox``.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from ..billing.operation_pricing import ChargePolicy, image_copy_usd, operation_quote
from ..billing.runtime import PaidResult
from ..config import Settings
from ..service_gateway.optimization.blackbox.sandbox import docker_sandbox_unavailable_reason
from ..service_gateway.optimization.blackbox.sandbox_contract import SandboxRuntime
from ..service_gateway.optimization.blackbox.sandbox_runtime import AsyncBridgeRuntime, current_sandbox_runtime
from .errors import SandboxFatalError
from .factory import PROVIDERS, preflight
from .metering import USAGE_ADAPTERS

# A fallback English label; the UI shows its own translated name when it has one.
_LABELS = {"vercel": "Vercel Sandbox", "docker": "Local Docker"}


# These providers run an egress proxy whose request log each box writes into
# the run record. Vercel enforces the allowlist at its edge; its box runs the
# proxy in record-only mode, so its record is cooperative (SKY-634).
_EGRESS_RECORDING_PROVIDERS = frozenset({"docker", "podman", "vercel"})
# The longest copy the reservation covers: ``COPY_TIMEOUT`` of the Vercel
# provider, which ends a longer copy. This module must not import that
# provider, because it loads the Vercel SDK.
COPY_RESERVE_SECONDS = 3600.0
ImageCopier = Callable[[str, tuple[str, str] | None], Awaitable[str]]


def metered_copier(copy_image: ImageCopier, budget: Any) -> ImageCopier:
    """Wrap an image copier so each copy is reserved first and settled on the run (SKY-891).

    The provider calls the copier once for each new copy, never for a copy
    it reuses, so only a real copy is charged. A failed copy is charged for
    its started minutes too, and its error goes on after the settlement.

    Args:
        copy_image: Copies a user image into the provider's registry.
        budget: The run's budget runtime.

    Returns:
        A copier with the same contract that runs the copy under the budget.
    """
    policy = ChargePolicy("sandbox")

    async def copy(source_image: str, pull_credentials: tuple[str, str] | None) -> str:
        """Reserve the copy's time limit, copy once, then settle the started minutes.

        Args:
            source_image: The user image, pinned by digest.
            pull_credentials: The (username, password) that read the source image.

        Returns:
            The copy's reference.

        Raises:
            BudgetError: The run cannot fund the copy, so no copy starts.
        """
        loop = asyncio.get_running_loop()
        stop = threading.Event()
        # The stop check and the store of the future happen under one lock, so a
        # cancel between them cannot miss the copy.
        guard = threading.Lock()
        running: dict[str, Any] = {}
        quote = operation_quote(
            {"image_copy": source_image},
            image_copy_usd(COPY_RESERVE_SECONDS),
            policy,
            {"provider": "image-copy", "timeout_seconds": COPY_RESERVE_SECONDS},
        )

        def dispatch() -> PaidResult[str | BaseException]:
            """Run the copy on the provider's event loop and charge its started minutes."""
            started = time.monotonic()
            value: str | BaseException
            with guard:
                future = None
                if not stop.is_set():
                    future = asyncio.run_coroutine_threadsafe(copy_image(source_image, pull_credentials), loop)
                    running["future"] = future
            if future is None:
                value = asyncio.CancelledError()
            else:
                try:
                    value = future.result()
                except BaseException as error:
                    value = error
            seconds = time.monotonic() - started
            return PaidResult(
                value=value,
                provider_usd=image_copy_usd(seconds),
                evidence={"provider": "image-copy", "seconds": round(seconds, 3)},
            )

        # Each copy is a new cost, so each one has its own operation key.
        settling = asyncio.ensure_future(
            asyncio.to_thread(
                budget.execute,
                quote,
                policy,
                dispatch,
                operation_key=f"image-copy:{uuid.uuid4().hex}",
                cost_kind="sandbox",
            )
        )
        try:
            outcome = await asyncio.shield(settling)
        except asyncio.CancelledError:
            # The thread cannot be cancelled: stop the copy and wait for the
            # settlement, so the reservation never stays open.
            with guard:
                stop.set()
                if "future" in running:
                    running["future"].cancel()
            while not settling.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.shield(settling)
            raise
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return copy


class ProtectedBridgeRuntime(AsyncBridgeRuntime):
    """Open protected boxes of one async provider through the sync bridge."""

    def __init__(
        self,
        provider: str,
        *,
        budget: Any,
        image: str | None = None,
        docker_bin: str | None = None,
        recorder: Any = None,
        copy_image: ImageCopier | None = None,
    ) -> None:
        """Bind the runtime to a provider and the run's budget.

        Args:
            provider: A provider name from ``core.sandbox.factory.PROVIDERS``.
            budget: The run's budget runtime. A billable provider reserves and
                settles each box against it.
            image: The default image of a provider that takes one (Docker).
            docker_bin: The Docker CLI path of the Docker provider.
            recorder: The run's call recorder. A provider with an egress proxy
                writes each proxy request into it; other providers ignore it.
            copy_image: Copies a user image into the registry of a provider
                that starts only from its own (Vercel). A billable provider
                runs each copy under ``budget``. ``None`` makes that provider
                refuse user images.

        Raises:
            SandboxFatalError: The provider is unknown, missing from this
                build, or not allowed in production.
        """
        super().__init__(provider, image=image, docker_bin=docker_bin, copy_image=copy_image)
        self.protected = True
        # Credentials stay in the control plane, so no box gets injected headers.
        self.injects_headers = False
        # The broker prices a billable box before it opens one; a local box is free.
        self.billable = self._capabilities.billable
        if self.billable:
            self._options["budget"] = budget
            if copy_image is not None:
                self._copy_image = metered_copier(copy_image, budget)
        if recorder is not None and provider in _EGRESS_RECORDING_PROVIDERS:
            self._options["recorder"] = recorder


def default_provider(settings: Settings) -> str:
    """Return the async provider a run uses when it names none.

    Args:
        settings: Trusted deployment configuration.

    Returns:
        ``"docker"`` in local mode, otherwise ``"vercel"``.
    """
    return "docker" if settings.local_mode else "vercel"


def offered_providers(settings: Settings) -> list[dict[str, str]]:
    """List the providers a user may pick for a run on this deployment.

    Local mode offers only its Docker box. A hosted deployment offers every
    provider that production allows, that protected execution meters through
    a usage adapter, and whose own preflight passes here. A new provider
    joins the list once it meets all three, with no other change.

    Args:
        settings: Trusted deployment configuration.

    Returns:
        One ``{"id", "label"}`` entry per offered provider, in registry order.
    """
    if settings.local_mode:
        names = ["docker"]
    else:
        names = []
        for name, entry in PROVIDERS.items():
            if not entry.production_allowed or name not in USAGE_ADAPTERS:
                continue
            try:
                preflight(name)
            except SandboxFatalError:
                continue
            names.append(name)
    return [{"id": name, "label": _LABELS.get(name, name)} for name in names]


def provider_offered(settings: Settings, provider: object) -> bool:
    """Check a run's requested provider against the offered list.

    The default provider always passes: a stored run names it, and the
    deployment's own availability check reports a missing default instead.

    Args:
        settings: Trusted deployment configuration.
        provider: The requested provider id, or ``None`` for the default.

    Returns:
        True when the run names no provider, the default or an offered one.
        False for any other value, including a non-string one.
    """
    if provider is None or provider == default_provider(settings):
        return True
    # The setup check passes the raw payload, so an unhashable value must refuse, not raise.
    if not isinstance(provider, str):
        return False
    return provider in {entry["id"] for entry in offered_providers(settings)}


def protected_unavailable_reason(settings: Settings) -> str | None:
    """Explain why this deployment cannot open protected boxes, or return ``None``.

    Args:
        settings: Trusted deployment configuration.

    Returns:
        A user-facing reason, or ``None`` when protected boxes can open.
    """
    if current_sandbox_runtime() is not None:
        return None
    try:
        preflight(default_provider(settings))
    except SandboxFatalError as error:
        return str(error)
    # The async Docker provider has no preflight; the same probe checks the
    # Docker CLI, the daemon and the image.
    return docker_sandbox_unavailable_reason(settings) if settings.local_mode else None


def protected_runtime(
    settings: Settings, *, image: str, budget: Any, recorder: Any = None, provider: str | None = None
) -> SandboxRuntime:
    """Build the protected runtime of this deployment.

    Args:
        settings: Trusted deployment configuration.
        image: The immutable image of every box.
        budget: The run's budget runtime.
        recorder: The run's call recorder. Docker, Podman and Vercel boxes
            write each egress proxy request into it (SKY-634).
        provider: The run's chosen provider; the deployment default when None.

    Returns:
        The chosen async provider through the sync bridge.

    Raises:
        SandboxFatalError: The provider is not offered here, or cannot load.
    """
    if not provider_offered(settings, provider):
        raise SandboxFatalError(f"Sandbox provider {provider!r} is not offered on this deployment.")
    name = provider or default_provider(settings)
    if name == "docker":
        return ProtectedBridgeRuntime(
            "docker", budget=budget, image=image, docker_bin=settings.docker_bin, recorder=recorder
        )
    return ProtectedBridgeRuntime(name, budget=budget, recorder=recorder)
