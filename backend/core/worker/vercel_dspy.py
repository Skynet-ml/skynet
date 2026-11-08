"""Supervise the unchanged DSPy service through a parent-owned Vercel sandbox."""

from __future__ import annotations

import base64
import json
import logging
import re
import secrets
import shlex
from pathlib import Path
from typing import Any

from ..billing.runtime import UsagePendingError
from ..billing.signals import BudgetReached
from ..brand import APP_NAME
from ..exceptions import InfrastructureInterruptionError
from ..models.blackbox import BLACKBOX_HARNESS_CODEX
from ..models.unified import DSPY_OPTIMIZERS
from ..service_gateway.optimization.blackbox.remote_sandbox import RemoteSandboxRuntime
from ..service_gateway.optimization.blackbox.repo_tree import REPO_SNAPSHOT_KEY, archive_chunks
from ..service_gateway.optimization.blackbox.sandbox import CommandResult, SandboxSpec
from .constants import EVENT_ERROR, EVENT_RESULT, EVENT_TERMINAL
from .failure_events import failure_event
from .isolated_runner import EVENT_PREFIX
from .sandbox_protocol import SANDBOX_PROTOCOL

logger = logging.getLogger(__name__)

CHECKPOINT_EVENT = "checkpoint_file"
_CHECKPOINT_PATH = re.compile(r"(?:(?:pair_\d+|gepa)/)?gepa_state\.bin\Z")
BROKEN_IMAGE_MESSAGE = "The sandbox image cannot start its optimizer; rebuild the sandbox image."
OUTDATED_IMAGE_MESSAGE = (
    f"The sandbox image is older than this version of {APP_NAME}; rebuild and re-pin the sandbox image."
)
PROTOCOL_PROBE = (
    'PYTHONPATH=/app python3 -c "from core.worker.sandbox_protocol import SANDBOX_PROTOCOL; print(SANDBOX_PROTOCOL)"'
)
_PROBE_TIMEOUT_SECONDS = 120.0
_MISSING_CORE_MODULE = re.compile(r"(?:ModuleNotFoundError|ImportError): .*\bcore\.")
_DIAGNOSTIC_LINES = 40


def _save_checkpoint(directory: Path, event: dict[str, Any]) -> None:
    """Mirror only recognized state files into the generation-owned checkpoint directory.

    Args:
        directory: Parent-owned checkpoint staging root.
        event: Guest frame containing a relative state path and base64 bytes.
    """
    path = event.get("path")
    if not isinstance(path, str) or not _CHECKPOINT_PATH.fullmatch(path):
        raise ValueError("The sandbox returned an invalid checkpoint path.")
    data = base64.b64decode(event["data"], validate=True)
    target = directory / path
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".incoming")
    temporary.write_bytes(data)
    temporary.replace(target)


def _starts_codex(payload: dict[str, Any]) -> bool:
    """Tell whether the optimizer in this box can start a contained Codex proposer.

    Args:
        payload: The run payload (black-box ``proposer`` or unified
            ``optimizer.proposer``).

    Returns:
        True for a sandbox proposer on the Codex harness, outside a setup check
        and a DSPy optimizer.
    """
    if "_preflight" in payload:
        return False
    optimizer = payload.get("optimizer")
    if isinstance(optimizer, dict) and optimizer.get("choice") in DSPY_OPTIMIZERS:
        return False
    proposer = payload.get("proposer")
    if proposer is None and isinstance(optimizer, dict):
        proposer = optimizer.get("proposer")
    if not isinstance(proposer, dict) or proposer.get("host_cli") is not None:
        return False
    return proposer.get("harness", BLACKBOX_HARNESS_CODEX) == BLACKBOX_HARNESS_CODEX


def _guest_failure(result: CommandResult) -> Exception:
    """Explain a guest that exited without a terminal event from its own stderr.

    The guest frames every failure inside its entrypoint as an event, so a bare
    non-zero exit means Python died before reaching it and the traceback on
    stderr is the only evidence. A ``core`` module failing to import there
    means the image's own copy of the backend predates what this run needs.

    Args:
        result: Outcome of the guest command.

    Returns:
        The exception to raise, ending with the guest's last stderr line.
    """
    lines = [line.rstrip() for line in result.stderr.splitlines() if line.strip()]
    if lines:
        logger.error(
            "Vercel optimizer guest exited %s without a result:\n%s",
            result.exit_code,
            "\n".join(lines[-_DIAGNOSTIC_LINES:]),
        )
    last = lines[-1].strip() if lines and not result.timed_out else ""
    if _MISSING_CORE_MODULE.match(last):
        return RuntimeError(f"{BROKEN_IMAGE_MESSAGE} {last}")
    detail = f" {last}" if last else ""
    return InfrastructureInterruptionError(
        f"The optimizer in the cloud sandbox exited without a complete result (exit {result.exit_code}).{detail}"
    )


def _require_image_protocol(session: Any, lifetime: float) -> None:
    """Refuse an image whose optimizer cannot read this worker's request.

    The image validates the request with its own copy of the backend models,
    so an image that predates a request field rejects it with a bare
    validation error. Images built before the protocol existed never check
    it, so the worker reads the image's protocol itself before sending
    anything.

    Args:
        session: The freshly opened sandbox session.
        lifetime: The sandbox lifetime, bounding the probe.

    Raises:
        RuntimeError: When the image lacks the protocol or speaks another one.
    """
    result = session.run(PROTOCOL_PROBE, timeout_seconds=min(lifetime, _PROBE_TIMEOUT_SECONDS))
    if result.ok:
        found = result.stdout.strip()
        if found == str(SANDBOX_PROTOCOL):
            return
        raise RuntimeError(
            f"The sandbox image speaks protocol {found}, but this worker speaks {SANDBOX_PROTOCOL};"
            " rebuild and re-pin the sandbox image."
        )
    if "core.worker.sandbox_protocol" in result.stderr:
        raise RuntimeError(OUTDATED_IMAGE_MESSAGE)
    raise _guest_failure(result)


def run_vercel_dspy(payload: dict[str, Any], artifact_id: str, event_queue: Any, _start_method: str) -> None:
    """Execute the full optimizer in the pinned backend image without provider credentials.

    Args:
        payload: Resolved request carrying a scoped parent sandbox descriptor.
        artifact_id: Generation-specific result artifact namespace.
        event_queue: Worker event queue receiving original optimizer events.
        _start_method: Existing process target contract; the guest initializes fresh.
    """
    session = None
    try:
        guest_payload = dict(payload)
        if "_preflight" in guest_payload:
            guest_payload.pop("_gepa_log_dir", None)
        descriptor = guest_payload.pop("_budget_gateway_descriptor", None)
        if not isinstance(descriptor, dict):
            raise TypeError("The Vercel optimizer requires a trusted sandbox descriptor.")
        runtime = RemoteSandboxRuntime(descriptor["url"], descriptor["control_token"])
        lifetime = float(descriptor["lifetime_seconds"])
        nonce = secrets.token_hex(24)
        checkpoint_root = Path(guest_payload["_gepa_log_dir"]) if guest_payload.get("_gepa_log_dir") else None
        checkpoints = {}
        if checkpoint_root is not None:
            for path in checkpoint_root.rglob("gepa_state.bin"):
                relative = path.relative_to(checkpoint_root).as_posix()
                if _CHECKPOINT_PATH.fullmatch(relative):
                    checkpoints[relative] = base64.b64encode(path.read_bytes()).decode()
            guest_payload["_gepa_log_dir"] = "checkpoints"
        # The proposer runs as a contained subprocess of this box, so the box
        # itself carries the proposer's granted hosts (E57). A repository's
        # setup, targets and metric run in parent-owned boxes, never here. A
        # setup check never runs the proposer and gets no host grant, so its
        # box stays offline.
        staged = guest_payload.get(REPO_SNAPSHOT_KEY)
        hosts = (
            tuple(staged.get("proposer_hosts") or ())
            if isinstance(staged, dict) and "_preflight" not in guest_payload
            else ()
        )
        session = runtime.open(
            SandboxSpec(
                lifetime_seconds=lifetime,
                image=descriptor["image"],
                network_disabled=not hosts,
                allowed_hosts=hosts,
                operation_key=f"dspy:{artifact_id}",
            )
        )
        _require_image_protocol(session, lifetime)
        # A Codex proposer runs as a contained subprocess of this box and
        # cannot run as root, so the parent writes and checks the root-owned
        # file here, before the optimizer starts (SKY-389). Other runs skip
        # the write; the guest check still fails closed if Codex starts.
        if _starts_codex(guest_payload):
            session.write_codex_requirements()
        session_root = f".app-dspy-{nonce}"
        snapshot = guest_payload.pop(REPO_SNAPSHOT_KEY, None)
        if isinstance(snapshot, dict):
            chunks = []
            for index, chunk in enumerate(archive_chunks(Path(snapshot["archive"]))):
                chunks.append(f"{session_root}/repo/tree.{index:04d}.b64")
                session.write_files({chunks[-1]: chunk})
            guest_payload[REPO_SNAPSHOT_KEY] = {**snapshot, "archive": None, "chunks": chunks}
        document = {
            "payload": guest_payload,
            "artifact_id": artifact_id,
            "nonce": nonce,
            "checkpoints": checkpoints,
            "export_checkpoints": checkpoint_root is not None,
            "protocol": SANDBOX_PROTOCOL,
        }
        request_path = f"{session_root}/request.json"
        session.write_files({request_path: json.dumps(document)})
        pending = {"stdout": "", "stderr": ""}
        terminal = False
        prefix = f"{EVENT_PREFIX}{nonce} "

        def output(stream: str, text: str) -> None:
            """Forward optimizer events and atomically stage recovery checkpoints.

            Args:
                stream: Sandbox stdout or stderr channel.
                text: The next command output chunk.
            """
            nonlocal terminal
            pending[stream] = pending.get(stream, "") + text
            while "\n" in pending[stream]:
                line, pending[stream] = pending[stream].split("\n", 1)
                if not line.startswith(prefix):
                    continue
                event = json.loads(line[len(prefix) :])
                if not isinstance(event, dict):
                    raise TypeError("The sandbox returned an invalid optimizer event.")
                if event.get("type") == CHECKPOINT_EVENT:
                    if checkpoint_root is None:
                        raise ValueError("The sandbox returned an unrequested checkpoint.")
                    _save_checkpoint(checkpoint_root, event)
                else:
                    terminal = terminal or event.get("type") in {
                        EVENT_RESULT,
                        EVENT_TERMINAL,
                        EVENT_ERROR,
                        "preflight_result",
                        "interaction_result",
                    }
                    event_queue.put(event)

        # The image is the whole optimizer: its own /app copy of the backend
        # runs, so a run's code never depends on which worker supervises it.
        command = f"PYTHONPATH=/app python3 -m core.worker.isolated_runner {shlex.quote(request_path)}"
        if "_preflight" in guest_payload:
            event_queue.put({"type": "preflight_phase", "phase": "evaluator"})
        result = session.run(
            command,
            env={"PYTHONUNBUFFERED": "1", "LITELLM_LOCAL_MODEL_COST_MAP": "True"},
            timeout_seconds=lifetime,
            on_output=output,
        )
        if not terminal or not result.ok:
            raise _guest_failure(result)
    except BudgetReached as error:
        event_queue.put(
            {
                "type": EVENT_TERMINAL,
                "outcome": {
                    "status": "stopped",
                    "stop_reason": "budget_reached",
                    "result_availability": "none",
                    "result": None,
                    "message": str(error),
                    "evidence": {
                        "candidate_origin": None,
                        "final_evaluation_completed": False,
                        "final_evaluation_reason": "budget_reached",
                    },
                },
            }
        )
    except Exception as error:
        event_queue.put(failure_event(error))
    finally:
        if session is not None:
            try:
                session.close()
            except UsagePendingError:
                pass
            except Exception as error:
                event_queue.put(failure_event(error))
