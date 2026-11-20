"""Hold the engine loop's proposer options, the guest task view and the worker's engine readiness check.

Since the cutover (SKY-728) every engine runs on the worker engine loop, so no
engine runs in a native box any more. This module keeps what the loop engines
share: the proposer options, the task document that a child process or a box
may hold, and the metric guards.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import threading
from dataclasses import dataclass, field
from importlib.metadata import distribution
from typing import Any, Literal

from ....config import Settings
from ....exceptions import ServiceError
from ....models.blackbox import BlackboxProposer
from .agent_eval import gateway_from_settings
from .harness import GatewayConfig
from .protocol import Task
from .sandbox import SandboxRuntime, current_sandbox_runtime, sandbox_unavailable_reason
from .upstream import (
    AUTORESEARCH_VERSION,
    AUTOSADDLER_REVISION,
    META_HARNESS_REVISION,
    SHINKA_VERSION,
)

GEPA_SOURCE = "0632cdb5dcc052e690eab439e1b4a7e3e9cfe407"
# The only task fields a child process or box may receive; Task.context, valset and the
# test split stay with the parent so no proposer can read the metric.
NATIVE_TASK_FIELDS = frozenset({"name", "seed_candidate", "objective", "background", "train_set"})
# Error side info is cut to one line of this size before a proposer sees it.
_ERROR_CHARS = 500
# Each ShinkaEvolve model route's token reaches the child in its own variable,
# named by position; the meta-notes model reads its own so its calls are told apart.
SHINKA_KEY_ENV = "APP_SHINKA_KEY_{index}"
SHINKA_META_KEY_ENV = "APP_SHINKA_META_KEY"
# The image bakes ShinkaEvolve into its own venv (backend/Dockerfile),
# because its pinned closure conflicts with the backend's own dependencies.
SHINKA_IMAGE_PYTHON = "/opt/shinka/venv/bin/python"
# Upstream AutoSaddler v2 declares Python 3.12+ (its usage dataclasses rely on
# 3.12 default semantics); the ShinkaEvolve pins need the same floor.
AUTOSADDLER_PYTHON_FLOOR = (3, 12)
# The image bakes the AutoSaddler pins into their own venv (backend/Dockerfile, SKY-645).
AUTOSADDLER_IMAGE_PYTHON = "/opt/autosaddler/venv/bin/python"
# A cold interpreter start on a busy worker can take seconds; the probe itself imports nothing.
_PROBE_TIMEOUT_SECONDS = 20


@dataclass(frozen=True)
class NativeOptions:
    """Bind an upstream proposer to the managed runtime and model gateway."""

    runtime: Literal["vercel", "docker"]
    model: str
    gateway: GatewayConfig = field(repr=False)
    max_token_cost: float | None
    proposer: BlackboxProposer = field(default_factory=BlackboxProposer)
    budget_route: dict[str, str] | None = field(default=None, repr=False)
    sandbox_runtime: SandboxRuntime | None = None
    # A repository run's packed tree: ``chunks`` (files on this machine),
    # ``editable_paths``, ``readonly_paths``, ``constrained`` (a
    # constraints file governs the run), ``proposer_hosts`` (what the
    # proposer box may reach besides the relay), ``file_proposer`` (the
    # proposer runs the file's ``[proposer] entry``) and ``proposer_setup``
    # (the file's ``[proposer] setup`` argument list).
    repo: dict[str, Any] | None = None
    # ShinkaEvolve's evolution settings, and its optimization models in bandit
    # order: each ``{"model", "url", "token"}`` names one scoped gateway route.
    shinka: dict[str, Any] | None = None
    shinka_models: tuple[dict[str, str], ...] = field(default=(), repr=False)
    # The embeddings route ShinkaEvolve's duplicate rejection uses, when it is on.
    shinka_embedding: dict[str, str] | None = field(default=None, repr=False)
    # The optimization model's effort level, which the proposer CLI applies.
    reasoning_effort: str | None = None
    usage_by_model: dict[str, dict[str, int]] = field(default_factory=dict)
    usage_lock: Any = field(default_factory=threading.Lock, repr=False, compare=False)

    @property
    def history(self) -> list[dict[str, Any]]:
        """Expose proposer tokens to the existing language-model usage reader.

        Returns:
            Usage entries with separate cache counters retained for settlement.
        """
        with self.usage_lock:
            return [{"model": model, "usage": dict(usage)} for model, usage in self.usage_by_model.items()]


def _runtime_allowed(runtime: str, settings: Settings) -> bool:
    """Check that ``runtime`` names an execution environment this deployment offers.

    Args:
        runtime: Requested execution environment.
        settings: Deployment configuration.

    Returns:
        True for Vercel, and for local Docker in local mode. Local mode opens
        its boxes in Docker whichever of the two the request names.
    """
    return runtime == "vercel" or (runtime == "docker" and settings.local_mode)


def native_runtime_unavailable_reason(runtime: str, settings: Settings) -> str | None:
    """Explain whether the selected native execution environment can launch.

    Args:
        runtime: Requested execution environment.
        settings: Deployment gateway and managed-sandbox configuration.

    Returns:
        An actionable unavailability reason, or ``None`` when checks pass.
    """
    if not _runtime_allowed(runtime, settings):
        return "Production optimizations run in the managed cloud sandbox."
    current = current_sandbox_runtime()
    if current is not None:
        return None
    # Local mode routes model calls through the run's own parent gateway
    # (the user's CLI subscriptions), never through a deployment proxy.
    if not settings.local_mode and gateway_from_settings(settings) is None:
        return "Native optimizers require a configured model gateway."
    return sandbox_unavailable_reason(settings)


def _own_venv_ready(python: str, package: str) -> bool:
    """Check that an engine's own interpreter exists on this machine and can import its package.

    Args:
        python: The engine's image interpreter.
        package: The top-level package that the engine's child imports.

    Returns:
        True when the interpreter runs Python 3.12 or newer and finds ``package``.
    """
    if not os.access(python, os.X_OK):
        return False
    probe = (
        "import importlib.util,sys; "
        f"sys.exit(0 if sys.version_info >= {AUTOSADDLER_PYTHON_FLOOR!r} and importlib.util.find_spec(sys.argv[1]) else 1)"
    )
    try:
        checked = subprocess.run(
            [python, "-c", probe, package], capture_output=True, timeout=_PROBE_TIMEOUT_SECONDS, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return checked.returncode == 0


def check_native_runtime() -> dict[str, Any]:
    """Check the engine dependencies of this worker image, with no box and no model call.

    The engines run in the worker (SKY-728): AutoSaddler and ShinkaEvolve as
    child processes of their own image interpreters. The API and the worker run
    one backend image (``infra/terraform/scripts/build-push.sh``), so the API's
    Continue check reads the worker's dependencies here.

    Returns:
        The pinned engine sources, and whether the AutoSaddler and ShinkaEvolve interpreters are ready.

    Raises:
        ServiceError: When the installed GEPA is not the approved commit.
    """
    provenance = json.loads(distribution("gepa").read_text("direct_url.json") or "{}")
    if provenance.get("vcs_info", {}).get("commit_id") != GEPA_SOURCE:
        raise ServiceError(f"Native optimizers require GEPA commit {GEPA_SOURCE}.")
    return {
        "gepa_source": GEPA_SOURCE,
        "meta_harness_source": META_HARNESS_REVISION,
        "autoresearch_version": AUTORESEARCH_VERSION,
        "autosaddler_source": AUTOSADDLER_REVISION,
        "autosaddler_ready": _own_venv_ready(AUTOSADDLER_IMAGE_PYTHON, "autosaddler"),
        "shinka_version": SHINKA_VERSION,
        "shinka_ready": _own_venv_ready(SHINKA_IMAGE_PYTHON, "shinka"),
    }


def _plain_case(case: Any) -> Any:
    """Turn one train case into plain JSON data for the native box.

    Args:
        case: A task case: a dict, or a model such as ``UnifiedCase``.

    Returns:
        The case as JSON-plain data.
    """
    dump = getattr(case, "model_dump", None)
    return dump(mode="json") if callable(dump) else case


def _guest_case(index: int, case: Any) -> dict[str, Any]:
    """Build the view of one train case that a guest may hold.

    The guest runs proposer and candidate code, so it gets only the case's
    position and its input. Only the parent's metric side gets ``expected``
    (E42, E61, E62). A dict with no ``input`` key is its own input without its
    ``expected`` key. An empty ``input``, such as ``None``, becomes ``{}``.

    Args:
        index: The case's position in the task's cases.
        case: A task case: a dict, or a model such as ``UnifiedCase``.

    Returns:
        ``{"id": index, "input": ...}``, or ``{"id": index}`` for a case that is not a dict.
    """
    plain = _plain_case(case)
    if not isinstance(plain, dict):
        return {"id": index}
    if "input" in plain:
        return {"id": index, "input": plain["input"] or {}}
    return {"id": index, "input": {key: value for key, value in plain.items() if key != "expected"}}


def native_task_payload(engine_id: str, task: Task) -> dict[str, Any]:
    """Build the task document a native box receives, from the train cases only.

    Args:
        engine_id: The native engine, used as the task name.
        task: The engine's task.

    Returns:
        Exactly the fields in ``NATIVE_TASK_FIELDS``.

    Raises:
        ServiceError: When the document would hold any other field.
    """
    document = {
        "name": engine_id,
        "seed_candidate": task.seed_candidate,
        "objective": task.objective or "",
        "background": task.background or "",
        # The guest builds an upstream task, whose train and val sets
        # form one pool; the cases go in once, as its train set.
        "train_set": [_guest_case(index, case) for index, case in enumerate(task.cases)] or None,
    }
    if set(document) != NATIVE_TASK_FIELDS:
        raise ServiceError("The native optimizer task carries fields it must not.")
    return document


def assert_metric_absent(serialized: str, task: Task) -> None:
    """Refuse to ship a native document that carries the task's metric source.

    Args:
        serialized: The document as it would be written into the box.
        task: The engine's task, whose private context holds the metric.

    Raises:
        ServiceError: When the metric source appears in the document.
    """
    metric = str((getattr(task, "context", None) or {}).get("metric_code") or "").strip()
    # Documents are JSON, so the source appears with its newlines escaped.
    if metric and (metric in serialized or json.dumps(metric)[1:-1] in serialized):
        raise ServiceError("The native optimizer payload would expose the metric source.")


def sanitize_side_info(info: Any) -> Any:
    """Reduce an error result to one ``Type: message`` line before a proposer sees it.

    Scorer errors can carry tracebacks or source lines from the metric; the
    proposer must learn only that a version failed and why, in one line.

    Args:
        info: Side information the parent scorer returned.

    Returns:
        ``{"error": "<line>"}`` for an error result, else ``info`` unchanged.
    """
    if not isinstance(info, dict) or "error" not in info:
        return info
    lines = [line.strip() for line in str(info.get("error") or "").splitlines() if line.strip()]
    kept = next((line for line in reversed(lines) if not line.startswith(("File ", "Traceback"))), "")
    return {"error": kept[:_ERROR_CHARS] or "Evaluation failed."}


def _finite(value: Any) -> bool:
    """Whether a child-reported score is a finite number.

    Args:
        value: Score field of a progress line.

    Returns:
        ``True`` for finite ints and floats, ``False`` for anything else.
    """
    return isinstance(value, float | int) and not isinstance(value, bool) and math.isfinite(value)


def _candidate_shape_ok(candidate: Any, str_mode: bool) -> bool:
    """Check that a child candidate matches the task's text or named-parts shape.

    Args:
        candidate: Candidate value received from the child.
        str_mode: Whether the task admits only text candidates.

    Returns:
        ``True`` when the parent evaluator may score the candidate.
    """
    if str_mode:
        return isinstance(candidate, str)
    return (
        isinstance(candidate, dict)
        and bool(candidate)
        and all(isinstance(name, str) and isinstance(text, str) for name, text in candidate.items())
    )
