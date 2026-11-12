"""Subprocess isolation for validation-time exec of user-authored code.

User-authored signature, transform and scorer code has to be
exec'd at some point to validate it. Running exec() directly in the API process leaks arbitrary
user code into the web server — a simple ``while True: pass`` would hang a
request worker, and ``import os; os.kill(1, 9)`` would be much worse.

This module wraps validation in a subprocess boundary: each call spawns a
fresh child via ``multiprocessing.spawn``, exec-s the code there, extracts
only the metadata the parent actually needs (field names, callable param
names), and returns a pickleable result. If the
child hangs, we kill it after a timeout; if it raises, the parent
re-raises a ``ServiceError``.

Invariant: the child NEVER returns the compiled class or function back.
Dynamically-exec'd classes can't be pickled across processes anyway, and
letting the compiled object cross the boundary would defeat the point.
Callers that need the actual object — the workflow preview — already run
inside the preview sandbox and exec directly.
"""

from __future__ import annotations

import inspect
import multiprocessing as mp
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from queue import Empty as QueueEmpty
from typing import Any

from ..exceptions import ServiceError
from .optimization.blackbox.runner import IMAGE_HELPER_NAME, LLM_HELPER_NAME, LOG_HELPER_NAME, Image, log, missing_llm
from .optimization.blackbox.scorer import load_scorer_from_code
from .optimization.data import (
    extract_signature_fields,
    image_input_field_names,
    load_signature_from_code,
    load_transform_from_code,
)

_DEFAULT_PARSE_TIMEOUT_SECONDS = 30.0
_TERMINATE_GRACE_SECONDS = 2.0
_QUEUE_READ_SECONDS = 5.0
_QUEUE_POLL_SECONDS = 0.25
_NO_RESULT = object()

# Dogpile-safe per-process caches: identical user code is validated once per
# replica, and concurrent submissions of the same code share that one
# subprocess-spawn cost instead of racing N fresh interpreters that all pay
# the multi-second ``import dspy`` price. Keyed on the raw source string —
# the cost of holding source in memory is trivial next to the spawn cost it
# avoids. Per-key locks live in ``_dogpile_locks``; ``_locks_mutex`` guards
# only the locks dict, not the heavy validation itself.
_signature_cache: dict[str, SignatureIntrospection] = {}
_transform_cache: dict[str, TransformIntrospection] = {}
_scorer_cache: dict[str, bool] = {}
_dogpile_locks: dict[str, threading.Lock] = {}
_locks_mutex = threading.Lock()

_VALIDATION_CACHE_MAX_ENTRIES = 256


def _bounded_cache_put(cache: dict[str, Any], key: str, value: Any) -> None:
    """Insert ``key`` → ``value``, evicting the oldest entry once at the cap.

    The memo dicts above are keyed by full user source strings, so without a
    bound they grow with every distinct submission for the life of the
    process — and the API process is long-lived and fork-parent to every job.
    FIFO suffices: the caches absorb same-code bursts, not long-tail reuse.
    Evicting a dogpile lock someone still holds merely lets two concurrent
    submissions of the same code validate twice.
    """
    if key not in cache and len(cache) >= _VALIDATION_CACHE_MAX_ENTRIES:
        cache.pop(next(iter(cache)))
    cache[key] = value


def _dogpile_lock(key: str) -> threading.Lock:
    """Return the lock for ``key``, creating it under ``_locks_mutex`` if absent.

    Args:
        key: Stable key (cache namespace plus code string) the caller will use
            to gate its cache miss.

    Returns:
        A ``threading.Lock`` unique to ``key`` across the process.
    """
    with _locks_mutex:
        lock = _dogpile_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _bounded_cache_put(_dogpile_locks, key, lock)
        return lock


@dataclass(frozen=True)
class SignatureIntrospection:
    """Metadata extracted from a user-authored signature class."""

    class_name: str
    input_fields: list[str]
    output_fields: list[str]
    image_input_fields: list[str]


@dataclass(frozen=True)
class TransformIntrospection:
    """Metadata extracted from a user-authored workflow transform callable."""

    callable_name: str
    param_names: list[str]


def _run_in_subprocess(
    target: Callable[..., None],
    args: tuple[Any, ...],
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Spawn ``target(*args, queue)`` in a child process and return its dict result.

    Uses the ``spawn`` start method unconditionally — a fresh Python
    interpreter with no inherited file descriptors, sockets, or memory
    from the parent. This is the whole point of the isolation.

    Args:
        target: Worker function executed in the child process.
        args: Positional arguments for ``target`` (queue is appended).
        timeout_seconds: Wall-clock budget before the child is terminated.

    Returns:
        Dict result the child placed on the queue.

    Raises:
        ServiceError: When the child times out, exits without a result,
            or returns a non-dict payload.
    """

    ctx = mp.get_context("spawn")
    queue: Any = ctx.Queue()
    proc = ctx.Process(target=target, args=(*args, queue))
    proc.start()

    # Read while waiting rather than join-then-read: a child cannot exit until
    # its result has left the pipe, so a large payload (rendered images in
    # side info) would otherwise hang the join until the timeout.
    deadline = time.monotonic() + timeout_seconds
    result: Any = _NO_RESULT
    while result is _NO_RESULT:
        try:
            result = queue.get(timeout=_QUEUE_POLL_SECONDS)
        except QueueEmpty:
            if not proc.is_alive():
                break
            if time.monotonic() >= deadline:
                proc.terminate()
                proc.join(_TERMINATE_GRACE_SECONDS)
                if proc.is_alive():
                    proc.kill()
                    proc.join(_TERMINATE_GRACE_SECONDS)
                raise ServiceError(
                    f"user code exceeded the {timeout_seconds:.0f}s validation timeout and was terminated."
                ) from None
    proc.join(_TERMINATE_GRACE_SECONDS)

    if result is _NO_RESULT:
        try:
            result = queue.get(timeout=_QUEUE_READ_SECONDS)
        except Exception as exc:  # queue.Empty or manager teardown: child died before emitting
            raise ServiceError("validation subprocess exited without returning a result.") from exc
    if not isinstance(result, dict):
        raise ServiceError("validation subprocess returned an unexpected value.")
    return result


def _raise_child_error(result: dict[str, Any]) -> None:
    """Translate a ``{"ok": False, ...}`` child payload into a ``ServiceError``.

    Args:
        result: The error payload emitted by ``_error_payload`` in the child.

    Raises:
        ServiceError: Always; the message is built from the payload.
    """

    error_type = result.get("error_type", "")
    error_msg = result.get("error", "user code failed")
    if error_type == "ServiceError":
        raise ServiceError(error_msg)
    if error_type:
        raise ServiceError(f"{error_type}: {error_msg}")
    raise ServiceError(error_msg)


def _error_payload(exc: BaseException) -> dict[str, Any]:
    """Build the ``{"ok": False, ...}`` dict that workers put on the queue.

    Args:
        exc: The exception caught inside the child process.

    Returns:
        A pickleable error payload with class name and traceback.
    """

    return {
        "ok": False,
        "error": str(exc),
        "error_type": type(exc).__name__,
        "traceback": traceback.format_exc(),
    }


def _signature_worker(code: str, queue: Any) -> None:
    """Child-side entry point for ``validate_signature_code``.

    Args:
        code: User-authored signature class source.
        queue: Multiprocessing queue used to return a result dict.
    """
    try:
        cls = load_signature_from_code(code)
        inputs, outputs = extract_signature_fields(cls)
        queue.put(
            {
                "ok": True,
                "class_name": cls.__name__,
                "input_fields": inputs,
                "output_fields": outputs,
                "image_input_fields": sorted(image_input_field_names(cls)),
            }
        )
    except BaseException as exc:  # user code is arbitrary — any failure is reported, not raised
        queue.put(_error_payload(exc))


def validate_signature_code(
    code: str,
    *,
    timeout_seconds: float = _DEFAULT_PARSE_TIMEOUT_SECONDS,
) -> SignatureIntrospection:
    """Parse user-authored signature code in a subprocess and return its shape.

    Memoized per-process: concurrent submissions of the same signature share
    one subprocess spawn instead of dogpiling N spawn+import-dspy cycles.

    Args:
        code: User-authored signature class source.
        timeout_seconds: Maximum time to wait for the child to finish.

    Returns:
        Field metadata extracted from the compiled signature class.

    Raises:
        ServiceError: When the user code fails to load or the child errors.
    """
    cached = _signature_cache.get(code)
    if cached is not None:
        return cached
    with _dogpile_lock(f"sig:{code}"):
        cached = _signature_cache.get(code)
        if cached is not None:
            return cached
        result = _run_in_subprocess(
            _signature_worker,
            (code,),
            timeout_seconds=timeout_seconds,
        )
        if not result.get("ok"):
            _raise_child_error(result)
        introspection = SignatureIntrospection(
            class_name=result["class_name"],
            input_fields=list(result["input_fields"]),
            output_fields=list(result["output_fields"]),
            image_input_fields=list(result.get("image_input_fields") or []),
        )
        _bounded_cache_put(_signature_cache, code, introspection)
        return introspection


def _transform_worker(code: str, queue: Any) -> None:
    """Child-side entry point for ``validate_transform_code``.

    Args:
        code: User-authored transform callable source.
        queue: Multiprocessing queue used to return a result dict.
    """
    try:
        transform = load_transform_from_code(code)
        sig = inspect.signature(transform)
        param_names = [p.name for p in sig.parameters.values()]
        queue.put(
            {
                "ok": True,
                "callable_name": getattr(transform, "__name__", "transform"),
                "param_names": param_names,
            }
        )
    except BaseException as exc:  # user code is arbitrary — any failure is reported, not raised
        queue.put(_error_payload(exc))


def validate_transform_code(
    code: str,
    *,
    timeout_seconds: float = _DEFAULT_PARSE_TIMEOUT_SECONDS,
) -> TransformIntrospection:
    """Parse user-authored transform code in a subprocess and return its shape.

    Memoized per-process like the signature validator: concurrent
    submissions of the same transform share one subprocess spawn.

    Args:
        code: User-authored transform callable source.
        timeout_seconds: Maximum time to wait for the child to finish.

    Returns:
        Callable name plus parameter names extracted via ``inspect``.

    Raises:
        ServiceError: When the user code fails to load or the child errors.
    """
    cached = _transform_cache.get(code)
    if cached is not None:
        return cached
    with _dogpile_lock(f"tra:{code}"):
        cached = _transform_cache.get(code)
        if cached is not None:
            return cached
        result = _run_in_subprocess(
            _transform_worker,
            (code,),
            timeout_seconds=timeout_seconds,
        )
        if not result.get("ok"):
            _raise_child_error(result)
        introspection = TransformIntrospection(
            callable_name=result["callable_name"],
            param_names=list(result["param_names"]),
        )
        _bounded_cache_put(_transform_cache, code, introspection)
        return introspection


def _scorer_worker(scorer_code: str, queue: Any) -> None:
    """Child-side entry point for ``validate_scorer_code``: load the code, report whether it defines a scorer.

    Args:
        scorer_code: User-authored scorer source.
        queue: Multiprocessing queue used to return a result dict.
    """
    try:
        load_scorer_from_code(
            scorer_code, helpers={LLM_HELPER_NAME: missing_llm, IMAGE_HELPER_NAME: Image, LOG_HELPER_NAME: log}
        )
        queue.put({"ok": True})
    except BaseException as exc:  # user code is arbitrary — any failure is reported, not raised
        queue.put(_error_payload(exc))


def validate_scorer_code(code: str, *, timeout_seconds: float = _DEFAULT_PARSE_TIMEOUT_SECONDS) -> None:
    """Load user-authored scorer code in a subprocess to prove it defines a scorer.

    Memoized per-process like the signature validator, for the same dogpile reason.

    Args:
        code: User-authored scorer source.
        timeout_seconds: Maximum time to wait for the child to finish.

    Raises:
        ServiceError: When the code fails to load, defines no scorer, or the
            child errors out.
    """
    if _scorer_cache.get(code):
        return
    with _dogpile_lock(f"scr:{code}"):
        if _scorer_cache.get(code):
            return
        result = _run_in_subprocess(_scorer_worker, (code,), timeout_seconds=timeout_seconds)
        if not result.get("ok"):
            _raise_child_error(result)
        _bounded_cache_put(_scorer_cache, code, True)
