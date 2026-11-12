"""Validate pinned GEPA recovery against immutable task and checkpoint evidence."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import pickletools
import platform
from pathlib import Path
from typing import Any

from ..constants import PROGRESS_CANDIDATE

GEPA_REVISION = "0632cdb5dcc052e690eab439e1b4a7e3e9cfe407"
GEPA_SCHEMA = 7
_MUTABLE_FIELDS = frozenset(
    {
        "name",
        "description",
        "username",
        "is_private",
        "estimated_cents_low",
        "estimated_cents_high",
        "execution_budget_id",
        "execution_budget_revision",
        "execution_budget_generation",
        "max_cost_cents",
        "preflight_id",
        "preflight_fingerprint",
    }
)


class CheckpointCompatibilityError(ValueError):
    """Reject restoration when exact checkpoint compatibility is unproven."""


def supports_checkpoint(payload: dict[str, Any]) -> bool:
    """Identify the pinned engine paths with a genuine external restore contract.

    Args:
        payload: Stored optimizer request, before private worker fields are injected.

    Returns:
        Whether this request runs GEPA directly. The other engines have no
        restore contract yet.
    """
    strategy = payload.get("strategy")
    return (
        isinstance(strategy, dict) and strategy.get("mode", "single") == "single" and strategy.get("engine") == "gepa"
    )


def _configuration_hash(payload: dict[str, Any]) -> str:
    """Hash execution inputs while permitting funding and presentation updates.

    Args:
        payload: Immutable request and mutable submission metadata.

    Returns:
        SHA-256 of the canonical execution configuration, including all dataset rows.
    """
    execution = {key: value for key, value in payload.items() if not key.startswith("_") and key not in _MUTABLE_FIELDS}
    return hashlib.sha256(
        json.dumps(execution, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _dependencies() -> dict[str, str]:
    """Return exact installed engine dependencies used to build a checkpoint."""
    return {name: importlib.metadata.version(name) for name in ("gepa", "dspy", "litellm")}


def source_files(root: Path | None = None) -> list[tuple[str, Path]]:
    """List the backend modules the runtime identity covers, sorted by path.

    Args:
        root: Package directory to scan; the running package by default.

    Returns:
        Package-relative POSIX paths paired with their files, without tests or caches.
    """
    root = root if root is not None else Path(__file__).resolve().parents[1]
    return [
        (path.relative_to(root).as_posix(), path)
        for path in sorted(root.rglob("*.py"))
        if "__pycache__" not in path.parts and "tests" not in path.parts
    ]


def source_digest(root: Path | None = None) -> str:
    """Hash the backend modules of one package tree.

    Args:
        root: Package directory to hash; the running package by default.

    Returns:
        SHA-256 over the relative path and bytes of every covered module.
    """
    digest = hashlib.sha256()
    for relative, path in source_files(root):
        digest.update(relative.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def runtime_identity() -> dict[str, Any]:
    """Identify the exact Python, dependencies, and backend source mounted in a guest.

    Returns:
        Immutable compatibility evidence checked before authored code executes.
    """
    direct_url = importlib.metadata.distribution("gepa").read_text("direct_url.json")
    return {
        "python": platform.python_version(),
        "dependencies": _dependencies(),
        "source_sha256": source_digest(),
        "gepa_revision": json.loads(direct_url or "{}").get("vcs_info", {}).get("commit_id"),
    }


def _state_metadata(data: bytes) -> dict[str, Any]:
    """Read primitive metadata from pickle opcodes without importing or executing payload classes.

    Args:
        data: State bytes authored by the isolated optimizer process.

    Returns:
        Top-level state dictionary with opaque values for constructed Python objects.

    Raises:
        CheckpointCompatibilityError: When the pickle uses unsupported or malformed structure.
    """
    stack: list[Any] = []
    memo: dict[int, Any] = {}
    mark = object()
    opaque = object()

    def pop_items() -> list[Any]:
        """Remove values above the most recent pickle mark."""
        position = len(stack) - 1 - stack[::-1].index(mark)
        items = stack[position + 1 :]
        del stack[position:]
        return items

    try:
        for opcode, arg, _position in pickletools.genops(data):
            name = opcode.name
            if name in {"PROTO", "FRAME"}:
                continue
            if name == "MARK":
                stack.append(mark)
            elif name in {
                "SHORT_BINUNICODE",
                "BINUNICODE",
                "BINUNICODE8",
                "UNICODE",
                "BININT",
                "BININT1",
                "BININT2",
                "INT",
                "LONG",
                "LONG1",
                "LONG4",
                "BINFLOAT",
                "FLOAT",
            }:
                stack.append(arg)
            elif name in {
                "BINBYTES",
                "SHORT_BINBYTES",
                "BINBYTES8",
                "BYTEARRAY8",
                "BINSTRING",
                "SHORT_BINSTRING",
                "STRING",
            }:
                stack.append(opaque)
            elif name == "NONE":
                stack.append(None)
            elif name in {"NEWTRUE", "NEWFALSE"}:
                stack.append(name == "NEWTRUE")
            elif name in {"EMPTY_DICT", "EMPTY_LIST", "EMPTY_TUPLE", "EMPTY_SET"}:
                stack.append({} if name == "EMPTY_DICT" else opaque)
            elif name in {"BINPUT", "LONG_BINPUT", "PUT"}:
                memo[int(arg)] = stack[-1]
            elif name == "MEMOIZE":
                memo[len(memo)] = stack[-1]
            elif name in {"BINGET", "LONG_BINGET", "GET"}:
                stack.append(memo[int(arg)])
            elif name in {"SETITEMS", "DICT"}:
                items = pop_items()
                mapping = dict(zip(items[::2], items[1::2], strict=True))
                if name == "DICT":
                    stack.append(mapping)
                elif isinstance(stack[-1], dict):
                    stack[-1].update(mapping)
            elif name == "SETITEM":
                value, key = stack.pop(), stack.pop()
                if isinstance(stack[-1], dict):
                    stack[-1][key] = value
            elif name in {"APPENDS", "ADDITEMS"}:
                pop_items()
            elif name == "APPEND":
                stack.pop()
            elif name in {"LIST", "TUPLE", "FROZENSET"}:
                pop_items()
                stack.append(opaque)
            elif name in {"TUPLE1", "TUPLE2", "TUPLE3"}:
                del stack[-int(name[-1]) :]
                stack.append(opaque)
            elif name in {"GLOBAL", "EXT1", "EXT2", "EXT4"}:
                stack.append(opaque)
            elif name in {"STACK_GLOBAL", "REDUCE", "NEWOBJ"}:
                del stack[-2:]
                stack.append(opaque)
            elif name == "NEWOBJ_EX":
                del stack[-3:]
                stack.append(opaque)
            elif name in {"BUILD", "POP"}:
                stack.pop()
            elif name == "POP_MARK":
                pop_items()
            elif name == "DUP":
                stack.append(stack[-1])
            elif name == "STOP":
                if len(stack) != 1 or not isinstance(stack[0], dict):
                    raise ValueError("Invalid root state")
                return stack[0]
            else:
                raise ValueError(f"Unsupported pickle opcode {name}")
    except (ValueError, IndexError, KeyError, TypeError) as exc:
        raise CheckpointCompatibilityError("The checkpoint metadata could not be inspected safely.") from exc
    raise CheckpointCompatibilityError("The checkpoint is incomplete.")


def _worker_runtime(code_version: str | None) -> dict[str, Any]:
    """Describe the worker process that ran GEPA itself, outside any sandbox image.

    Args:
        code_version: Deployed application version owning the adapter.

    Returns:
        The GEPA revision, application version, source digest, dependencies, and Python.
    """
    return {
        "upstream_revision": GEPA_REVISION,
        "code_version": code_version,
        "source_sha256": runtime_identity()["source_sha256"],
        "dependencies": _dependencies(),
        "python": platform.python_version(),
    }


def checkpoint_manifest(
    data: bytes, payload: dict[str, Any], code_version: str | None, *, sandbox_image: str | None = None
) -> dict[str, Any]:
    """Record compatibility for GEPA state just published by its optimizer.

    State written inside a sandbox belongs to that image, which carries the
    optimizer's code and dependencies; only that image can load it again. State
    written by the worker's own process belongs to the worker's runtime instead.

    Args:
        data: Complete atomically published GEPA state bytes.
        payload: Resolved immutable request, including dataset content.
        code_version: Deployed application version owning the adapter.
        sandbox_image: The image the run is pinned to, or None when GEPA ran in the worker.

    Returns:
        Recovery evidence stored atomically alongside the exact bytes.

    Raises:
        CheckpointCompatibilityError: When this source or state schema is unsupported.
    """
    if not supports_checkpoint(payload):
        raise CheckpointCompatibilityError("This optimizer has no supported checkpoint recovery contract.")
    if sandbox_image is None:
        direct_url = importlib.metadata.distribution("gepa").read_text("direct_url.json")
        source = json.loads(direct_url or "{}")
        if source.get("vcs_info", {}).get("commit_id") != GEPA_REVISION:
            raise CheckpointCompatibilityError("The installed GEPA source does not match the approved revision.")
    state = _state_metadata(data)
    if not isinstance(state, dict) or state.get("validation_schema_version") != GEPA_SCHEMA:
        raise CheckpointCompatibilityError("The checkpoint is not a supported GEPA state schema 7 snapshot.")
    return {
        "manifest_version": 1 if sandbox_image is None else 2,
        "state_schema": GEPA_SCHEMA,
        "checkpoint_sha256": hashlib.sha256(data).hexdigest(),
        "configuration_sha256": _configuration_hash(payload),
        **(_worker_runtime(code_version) if sandbox_image is None else {"sandbox_image": sandbox_image}),
        "iteration": int(state.get("i", 0)),
        "metric_calls": int(state.get("total_num_evals", 0)),
    }


def evaluated_incumbent_from_progress(
    event_name: Any,
    metrics: Any,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    """Copy a completed candidate event into a JSON-safe recovery envelope.

    Args:
        event_name: Progress event discriminator emitted by the optimizer.
        metrics: Candidate metrics supplied through the worker event queue.
        payload: Immutable request used to identify the evaluated split.

    Returns:
        A validated incumbent envelope, or ``None`` for incomplete or unsafe input.
    """
    if event_name != PROGRESS_CANDIDATE or not isinstance(metrics, dict):
        return None
    score = metrics.get("score")
    if isinstance(score, bool) or not isinstance(score, int | float) or not math.isfinite(float(score)):
        return None
    prompt = metrics.get("prompt")
    if not isinstance(prompt, dict) or not prompt:
        return None
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in prompt.items()):
        return None
    per_example = metrics.get("per_example")
    if not isinstance(per_example, list) or not per_example:
        return None
    for row in per_example:
        row_score = row.get("score") if isinstance(row, dict) else None
        if isinstance(row_score, bool) or not isinstance(row_score, int | float) or not math.isfinite(float(row_score)):
            return None
    candidate_id = metrics.get("candidate_id")
    if not isinstance(candidate_id, str | int) or isinstance(candidate_id, bool):
        return None
    discovered_at_evals = metrics.get("discovered_at_evals")
    if not isinstance(discovered_at_evals, int) or isinstance(discovered_at_evals, bool):
        discovered_at_evals = 0
    iteration = metrics.get("iteration")
    if not isinstance(iteration, int) or isinstance(iteration, bool):
        iteration = None
    split = payload.get("split_fractions")
    data = payload.get("data")
    if split is None and isinstance(data, dict):
        split = data.get("split")
    selection_scope = "validation"
    if isinstance(split, dict):
        val_fraction = split.get("val")
        if isinstance(val_fraction, int | float) and not isinstance(val_fraction, bool) and float(val_fraction) <= 0:
            selection_scope = "training"
    incumbent = {
        "candidate_id": str(candidate_id),
        "candidate_origin": "seed" if str(candidate_id) == "0" else "optimized",
        "candidate": dict(prompt),
        "selection_score": float(score),
        "selection_scope": selection_scope,
        "evaluated_examples": len(per_example),
        "discovered_at_evals": discovered_at_evals,
        "iteration": iteration,
    }
    try:
        return json.loads(json.dumps(incumbent, allow_nan=False))
    except (TypeError, ValueError):
        return None


def validate_checkpoint(
    data: bytes,
    manifest: dict[str, Any] | None,
    payload: dict[str, Any],
    code_version: str | None,
    *,
    sandbox_image: str | None = None,
) -> None:
    """Reject mismatched state before upstream deserializes or spends on resumed seed evaluation.

    Args:
        data: Persisted state bytes.
        manifest: Compatibility evidence captured when those bytes were written.
        payload: Request to resume without changing the task or optimizer.
        code_version: Adapter version available on the replacement worker.
        sandbox_image: The image the run is pinned to, which the resume boots from.

    Raises:
        CheckpointCompatibilityError: When integrity, source, task, or runtime does not match.
    """
    if not supports_checkpoint(payload) or not manifest:
        raise CheckpointCompatibilityError(
            "No verified compatible checkpoint is available; a fresh restart is required."
        )
    in_image = manifest.get("manifest_version") == 2
    if in_image and sandbox_image is None:
        raise CheckpointCompatibilityError("Checkpoint recovery is incompatible: sandbox_image changed.")
    expected = {
        "manifest_version": 2 if in_image else 1,
        "state_schema": GEPA_SCHEMA,
        "checkpoint_sha256": hashlib.sha256(data).hexdigest(),
        "configuration_sha256": _configuration_hash(payload),
        **({"sandbox_image": sandbox_image} if in_image else _worker_runtime(code_version)),
    }
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise CheckpointCompatibilityError(f"Checkpoint recovery is incompatible: {key} changed.")
