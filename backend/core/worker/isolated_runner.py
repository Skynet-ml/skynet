"""Run the unchanged optimizer service inside a credential-free managed sandbox."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import sys
import threading
from pathlib import Path
from typing import Any

from core.billing.signals import BudgetReached
from core.config import app_env
from core.constants import OPTIMIZATION_TYPE_BLACKBOX
from core.models.unified import UNIFIED_PAYLOAD_KIND
from core.service_gateway.optimization.blackbox import preflight as blackbox_preflight
from core.service_gateway.optimization.blackbox.preflight import verify_anything_in_sandbox
from core.service_gateway.optimization.blackbox.sandbox_runtime import (
    ContainedSubprocessRuntime,
    current_sandbox_runtime,
    sandbox_runtime_context,
)
from core.worker.interaction import run_interaction
from core.worker.preflight import run_dspy_preflight
from core.worker.sandbox_protocol import SANDBOX_PROTOCOL
from core.worker.subprocess_runner import run_service_in_subprocess

# Shared with the pinned sandbox image's own backend copy; renaming breaks older images.
EVENT_PREFIX = "SKYNET_JOB_EVENT "
INCOMPATIBLE_IMAGE_MESSAGE = "The sandbox image speaks an older protocol than this worker; rebuild the sandbox image."
_CHECKPOINT_PATH = re.compile(r"(?:(?:pair_\d+|gepa)/)?gepa_state\.bin\Z")


class EventQueue:
    """Forward subprocess events through a framed stdout transport."""

    def __init__(self, nonce: str) -> None:
        """Bind events to the invocation nonce supplied by the trusted supervisor.

        Args:
            nonce: Random delimiter distinguishing events from ordinary program logs.
        """
        self.nonce = nonce
        self._lock = threading.Lock()

    def put(self, event: dict[str, Any]) -> None:
        """Publish one JSON-safe optimizer event without altering its meaning.

        Args:
            event: Original worker event emitted by the service runner.
        """
        with self._lock:
            sys.__stdout__.write(f"{EVENT_PREFIX}{self.nonce} {json.dumps(event)}\n")
            sys.__stdout__.flush()


def _export_checkpoints(directory: Path, events: EventQueue, seen: dict[str, str]) -> None:
    """Publish atomically completed upstream checkpoint files without deserializing them.

    Args:
        directory: Guest checkpoint directory.
        events: Parent-owned event transport.
        seen: Content digests already mirrored to the parent.
    """
    for path in directory.rglob("gepa_state.bin"):
        relative = path.relative_to(directory).as_posix()
        if not _CHECKPOINT_PATH.fullmatch(relative):
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        digest = hashlib.sha256(data).hexdigest()
        if seen.get(relative) != digest:
            events.put({"type": "checkpoint_file", "path": relative, "data": base64.b64encode(data).decode()})
            seen[relative] = digest


def _forget_request(path: Path) -> None:
    """Remove the request file once it is in memory, so no proposer can read the metric from disk.

    The document carries ``workspace.metric_code`` (unified runs) or
    ``scorer.metric_code`` (legacy black-box runs). Proposers run as
    contained commands in this same box and could otherwise find it.

    Args:
        path: The request file the supervisor wrote.
    """
    with contextlib.suppress(OSError):
        path.unlink()


def _is_blackbox_family(payload: dict[str, Any]) -> bool:
    """Tell whether a payload runs on the black-box engine layer.

    Args:
        payload: The guest request payload.

    Returns:
        True for legacy black-box runs and for unified runs.
    """
    return (
        payload.get("_optimization_type") == OPTIMIZATION_TYPE_BLACKBOX
        or payload.get("payload_kind") == UNIFIED_PAYLOAD_KIND
    )


def _run_preflight(payload: dict[str, Any], preflight: dict[str, Any], events: EventQueue) -> dict[str, Any]:
    """Dispatch a setup check to the verifier that matches the payload kind.

    Args:
        payload: The guest request payload.
        preflight: Scope and identity of the requested check.
        events: Parent-owned event transport, for DSPy preview tokens.

    Returns:
        The verifier's result document.

    Raises:
        RuntimeError: When a black-box check did not enter its sandbox.
    """
    if payload.get("payload_kind") == UNIFIED_PAYLOAD_KIND:
        return blackbox_preflight.verify_unified_in_sandbox(payload, preflight["scope"])
    if payload.get("_optimization_type") == OPTIMIZATION_TYPE_BLACKBOX:
        runtime = current_sandbox_runtime()
        if runtime is None:
            raise RuntimeError("Anything setup did not enter its selected sandbox.")
        return verify_anything_in_sandbox(
            payload,
            scope=preflight["scope"],
            identity=preflight["identity"],
            runtime=runtime,
        )
    return run_dspy_preflight(payload, preflight, lambda token: events.put({"type": "preview_token", **token}))


def main() -> None:
    """Load the scoped request and execute the normal service entry point."""
    request_path = Path(sys.argv[1])
    document = json.loads(request_path.read_text())
    _forget_request(request_path)
    events = EventQueue(document["nonce"])
    if document.get("protocol") != SANDBOX_PROTOCOL:
        events.put({"type": "error", "error": INCOMPATIBLE_IMAGE_MESSAGE, "traceback": ""})
        return
    tools_route = document["payload"].pop("_skynet_tools_route", None)
    if tools_route is not None:
        if not app_env("APP_BUDGET_RELAY_URL"):
            raise RuntimeError("The protected tool roster requires its isolated mailbox relay.")
        os.environ["APP_TOOL_RELAY_TOKEN"] = str(tools_route["token"])
    packages_route = document["payload"].pop("_skynet_packages_route", None)
    if packages_route is not None:
        os.environ["APP_PACKAGE_RELAY_TOKEN"] = str(packages_route["token"])
    preflight = document["payload"].pop("_preflight", None)
    interaction = document["payload"].pop("_interaction", None)
    directory = Path(document["payload"].get("_gepa_log_dir") or "checkpoints")
    for relative, encoded in document.get("checkpoints", {}).items():
        if not _CHECKPOINT_PATH.fullmatch(relative):
            raise ValueError("Invalid recovery checkpoint path.")
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(encoded, validate=True))
    stopped = threading.Event()
    seen: dict[str, str] = {}

    def monitor() -> None:
        """Return completed optimizer state while the managed command remains active."""
        while not stopped.wait(1):
            _export_checkpoints(directory, events, seen)

    thread = threading.Thread(target=monitor, daemon=True)
    export = document.get("export_checkpoints", False)
    if export:
        thread.start()
    runtime_scope = (
        sandbox_runtime_context(ContainedSubprocessRuntime())
        if _is_blackbox_family(document["payload"])
        else contextlib.nullcontext()
    )
    try:
        with runtime_scope:
            if interaction is not None:
                result = run_interaction(document["payload"], interaction, events.put)
                events.put({"type": "interaction_result", "result": result})
            elif preflight is not None:
                result = _run_preflight(document["payload"], preflight, events)
                events.put({"type": "preflight_result", "result": result})
            else:
                run_service_in_subprocess(document["payload"], document["artifact_id"], events, "spawn")
    except BudgetReached as error:
        events.put({"type": "error", "error": str(error), "code": "budget_reached", "traceback": ""})
    except Exception as error:
        # The parent rebuilds a budget stop from ``error_type`` so a setup
        # check stopped by the spending limit is not reported as broken code.
        events.put({"type": "error", "error": str(error), "error_type": type(error).__name__, "traceback": ""})
    finally:
        if export:
            stopped.set()
            thread.join()
            _export_checkpoints(directory, events, seen)


if __name__ == "__main__":
    main()
