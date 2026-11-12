"""Loaders and introspection for user-authored workflow code.

Helpers that load a user-authored :class:`dspy.Signature` or
transform callable from source and read the signature's fields. Two
callers use them: the ``safe_exec`` validators, which run them in a
spawned child process, and the workflow preview
(``optimization/workflow.py``), which runs inside the preview sandbox.
"""

from __future__ import annotations

import typing
from collections.abc import Callable
from typing import Any

import dspy

from ...exceptions import ServiceError


def extract_signature_fields(signature_cls: type[dspy.Signature]) -> tuple[list[str], list[str]]:
    """Inspect a DSPy signature class and return its input and output field names.

    Args:
        signature_cls: The signature class to introspect.

    Returns:
        A tuple ``(input_field_names, output_field_names)``.

    Raises:
        ServiceError: When the signature has no input or no output fields.
    """

    inputs: list[str] = []
    outputs: list[str] = []

    fields_mapping = getattr(signature_cls, "model_fields", None) or getattr(signature_cls, "__pydantic_fields__", None)
    if fields_mapping:
        for field_name, field_info in fields_mapping.items():
            if _is_signature_field(field_info, field_type="input"):
                inputs.append(field_name)
            elif _is_signature_field(field_info, field_type="output"):
                outputs.append(field_name)
    else:
        for attr_name, attr_value in signature_cls.__dict__.items():
            if _is_signature_field(attr_value, field_type="input"):
                inputs.append(attr_name)
            elif _is_signature_field(attr_value, field_type="output"):
                outputs.append(attr_name)
    if not inputs or not outputs:
        raise ServiceError("Signature must declare at least one input and one output field.")
    return inputs, outputs


def image_input_field_names(signature_cls: type[dspy.Signature]) -> set[str]:
    """Return the names of input fields annotated as ``dspy.Image``.

    Inspects the signature's Pydantic field annotations directly. Only
    matches the bare ``dspy.Image`` annotation — not ``Optional[dspy.Image]``
    or ``list[dspy.Image]`` — because the LLM is instructed to emit the
    bare form for image columns.

    Args:
        signature_cls: The signature class to introspect.

    Returns:
        Names of input fields whose annotation is ``dspy.Image``.
    """
    image_type = getattr(dspy, "Image", None)
    if image_type is None:
        return set()
    fields_mapping = getattr(signature_cls, "model_fields", None) or getattr(signature_cls, "__pydantic_fields__", None)
    if not fields_mapping:
        return set()
    out: set[str] = set()
    for field_name, field_info in fields_mapping.items():
        if not _is_signature_field(field_info, field_type="input"):
            continue
        if _annotation_is_image(getattr(field_info, "annotation", None), image_type):
            out.add(field_name)
    return out


def _annotation_is_image(annotation: Any, image_type: type) -> bool:
    """Return True when ``annotation`` denotes the bare ``dspy.Image`` type.

    Handles both eagerly-evaluated annotations (``dspy.Image`` resolved to
    the actual class) and lazy ``ForwardRef``s — the latter occurs when a
    signature is loaded via ``exec`` from inside a module that uses
    ``from __future__ import annotations``, which leaves Pydantic with
    ``ForwardRef('dspy.Image')`` instead of the resolved class.

    Args:
        annotation: The Pydantic field annotation to check.
        image_type: The resolved ``dspy.Image`` class.

    Returns:
        True when ``annotation`` denotes ``dspy.Image``.
    """
    if annotation is image_type:
        return True
    forward_arg = getattr(annotation, "__forward_arg__", None)
    if isinstance(forward_arg, str):
        return forward_arg in {"dspy.Image", "Image"}
    if isinstance(annotation, str):
        return annotation in {"dspy.Image", "Image"}
    return False


def load_signature_from_code(code: str) -> type[dspy.Signature]:
    """Execute user-provided code and return the single DSPy signature class it defines.

    Trust boundary: the body of ``code`` runs in the calling process via
    ``exec``. Callers must isolate it: the ``safe_exec`` validators call this in
    a spawned child process, and the workflow preview calls it inside the
    preview sandbox. Never call it directly in the API process.

    Args:
        code: User-authored signature source code.

    Returns:
        The single :class:`dspy.Signature` subclass defined by ``code``.

    Raises:
        ServiceError: When the code has a syntax error, defines no Signature
            subclass, or defines more than one.
    """

    namespace: dict[str, Any] = {"dspy": dspy}
    try:
        # dont_inherit=True: this module's ``from __future__ import annotations``
        # (PEP 563) would otherwise leak into the exec'd code and stringize the
        # signature's field annotations. DSPy resolves bare builtins (``str``,
        # ``int``) but leaves dotted generics like ``typing.List[str]`` as an
        # unresolved ForwardRef, which later crashes GEPA's signature rebuild
        # ("Field types must be types, but received: ForwardRef(...)").
        exec(compile(code, "<signature_code>", "exec", dont_inherit=True), namespace)
    except SyntaxError as exc:
        raise ServiceError(f"signature_code has a syntax error: {exc}") from exc
    signature_classes = [
        obj
        for obj in namespace.values()
        if isinstance(obj, type) and issubclass(obj, dspy.Signature) and obj is not dspy.Signature
    ]
    if not signature_classes:
        raise ServiceError("signature_code must define a dspy.Signature subclass.")
    if len(signature_classes) > 1:
        raise ServiceError("signature_code must define exactly one dspy.Signature subclass.")
    signature_cls = signature_classes[0]
    _resolve_signature_annotations(signature_cls, namespace)
    return signature_cls


def _resolve_signature_annotations(
    signature_cls: type[dspy.Signature],
    namespace: dict[str, Any],
) -> None:
    """Force-resolve a signature's field annotations into concrete types in place.

    When the authored code carries ``from __future__ import annotations`` (PEP 563)
    Pydantic stores each field annotation as a ``ForwardRef`` string instead of the
    resolved type. DSPy's ``with_instructions`` / ``make_signature`` rebuild the
    signature from ``cls.fields`` and reject those refs ("Field types must be types,
    but received: ForwardRef(...)"). Resolving names against the exec namespace as
    ``globalns`` (so ``typing``/``List`` etc. bind) and writing the concrete types
    back onto the Pydantic ``FieldInfo`` objects makes DSPy's rebuild — which reads
    ``cls.fields`` (those FieldInfos), not the raw class ``__annotations__`` — accept
    them, regardless of future-import state. We deliberately do not call
    ``model_rebuild``: Pydantic re-evaluates the stringized ``__annotations__``
    against the class's own module globals (where ``List`` etc. are absent) and
    would crash. Resolution failures are left untouched so a signature that merely
    references an unresolvable name still loads as before.

    Args:
        signature_cls: The loaded ``dspy.Signature`` subclass to mutate in place.
        namespace: The exec namespace the signature was defined in, used as the
            global scope for name resolution.
    """
    try:
        hints = typing.get_type_hints(signature_cls, globalns=namespace)
    except Exception:
        return
    fields_mapping = getattr(signature_cls, "model_fields", None) or getattr(signature_cls, "__pydantic_fields__", None)
    if not fields_mapping:
        return
    for field_name, field_info in fields_mapping.items():
        resolved = hints.get(field_name)
        if resolved is not None and getattr(field_info, "annotation", None) is not resolved:
            field_info.annotation = resolved


def load_transform_from_code(code: str) -> Callable[..., Any]:
    """Execute user-provided code and return the transform callable it defines.

    Transforms are the non-LLM steps of a workflow graph: plain functions
    taking the node's input ports as keyword arguments and returning a dict
    keyed by the node's declared output ports. Same trust boundary as
    :func:`load_signature_from_code`: callers must isolate it.

    Args:
        code: User-authored transform source code.

    Returns:
        The transform callable extracted from the namespace.

    Raises:
        ServiceError: When the code has a syntax error or defines no callable.
    """

    namespace: dict[str, Any] = {"dspy": dspy}
    try:
        # dont_inherit=True for the same reason as load_signature_from_code:
        # keep this module's PEP 563 future-import from stringizing the
        # user function's annotations.
        exec(compile(code, "<transform_code>", "exec", dont_inherit=True), namespace)
    except SyntaxError as exc:
        raise ServiceError(f"transform_code has a syntax error: {exc}") from exc
    transform = namespace.get("transform")
    if not callable(transform):
        callables = [
            obj for obj in namespace.values() if callable(obj) and not isinstance(obj, type) and obj is not dspy
        ]
        if len(callables) == 1:
            transform = callables[0]
    if not callable(transform):
        raise ServiceError("transform_code must define a callable named 'transform'.")
    return transform


def _is_signature_field(value: Any, *, field_type: str) -> bool:
    """Return True when a signature attribute represents the requested field type.

    Args:
        value: The signature attribute to inspect.
        field_type: Either ``"input"`` or ``"output"``.

    Returns:
        True when ``value`` is a DSPy field of the requested type.
    """

    type_attr = getattr(dspy, "InputField" if field_type == "input" else "OutputField", None)
    if type_attr is not None:
        try:
            if isinstance(value, type_attr):
                return True
        except TypeError:
            pass

    marker = getattr(value, "__dspy_field_type", None)
    if marker == field_type:
        return True

    extra = getattr(value, "json_schema_extra", None)
    return bool(isinstance(extra, dict) and extra.get("__dspy_field_type") == field_type)
