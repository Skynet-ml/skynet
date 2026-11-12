"""Tests for ``core.service_gateway.optimization.data``.

Covers signature loading, metric loading and image-field detection.
"""

from __future__ import annotations

import typing

import dspy
import pytest

from core.exceptions import ServiceError
from core.service_gateway.optimization.data import (
    _is_signature_field,
    extract_signature_fields,
    image_input_field_names,
    load_signature_from_code,
)

_VALID_SIG = """
import dspy
class QA(dspy.Signature):
    question: str = dspy.InputField()
    answer: str = dspy.OutputField()
"""


def test_load_signature_from_code_returns_signature_class() -> None:
    """Valid source returns a ``dspy.Signature`` subclass."""
    sig = load_signature_from_code(_VALID_SIG)

    assert issubclass(sig, dspy.Signature)


def test_load_signature_from_code_syntax_error_raises_service_error() -> None:
    """A syntax error in user source raises a ``ServiceError``."""
    with pytest.raises(ServiceError, match="syntax error"):
        load_signature_from_code("def bad syntax !!!")


def test_load_signature_from_code_no_signature_raises_service_error() -> None:
    """Source without a ``dspy.Signature`` subclass raises ``ServiceError``."""
    with pytest.raises(ServiceError, match=r"must define a dspy\.Signature"):
        load_signature_from_code("x = 1")


def test_load_signature_from_code_multiple_signatures_raises_service_error() -> None:
    """Source with more than one signature raises ``ServiceError``."""
    code = """
import dspy
class A(dspy.Signature):
    q: str = dspy.InputField()
    a: str = dspy.OutputField()
class B(dspy.Signature):
    q: str = dspy.InputField()
    a: str = dspy.OutputField()
"""
    with pytest.raises(ServiceError, match="exactly one"):
        load_signature_from_code(code)


def test_load_signature_from_code_resolves_generic_annotations() -> None:
    """Dotted generic annotations resolve to real types, not ``ForwardRef``.

    Regression: ``data.py`` carries ``from __future__ import annotations``, and
    ``exec`` inherits that PEP 563 flag unless compiled with ``dont_inherit``.
    Without the fix a field typed ``typing.List[str]`` survived as an
    unresolved ``ForwardRef``, crashing GEPA's ``with_instructions`` rebuild
    with "Field types must be types, but received: ForwardRef(...)".
    """
    code = """
import dspy
import typing
class PredictDecade(dspy.Signature):
    title: str = dspy.InputField()
    subcategories: typing.List[str] = dspy.InputField()
    inventor_count: int = dspy.InputField()
    decade: str = dspy.OutputField()
"""
    sig = load_signature_from_code(code)

    assert not any(isinstance(f.annotation, typing.ForwardRef) for f in sig.model_fields.values())
    # The loader must preserve the user's typing.List form verbatim; list[str]
    # is a distinct object that compares unequal, so UP006 must not rewrite this.
    assert sig.model_fields["subcategories"].annotation == typing.List[str]  # noqa: UP006
    sig.with_instructions("rebuilt")  # GEPA rebuild path must not raise


_SIG_CODE = """\
import dspy
class QA(dspy.Signature):
    question: str = dspy.InputField()
    context: str = dspy.InputField()
    answer: str = dspy.OutputField()
"""


def test_extract_signature_fields_returns_correct_inputs() -> None:
    """All input fields are returned in the inputs slot."""
    sig_cls = load_signature_from_code(_SIG_CODE)

    inputs, _ = extract_signature_fields(sig_cls)

    assert "question" in inputs
    assert "context" in inputs


def test_extract_signature_fields_returns_correct_outputs() -> None:
    """All output fields are returned in the outputs slot."""
    sig_cls = load_signature_from_code(_SIG_CODE)

    _, outputs = extract_signature_fields(sig_cls)

    assert "answer" in outputs


def test_extract_signature_fields_returns_two_element_tuple() -> None:
    """The function returns a 2-tuple of ``(inputs, outputs)``."""
    sig_cls = load_signature_from_code(_SIG_CODE)

    result = extract_signature_fields(sig_cls)

    assert len(result) == 2


def test_extract_signature_fields_raises_when_no_inputs() -> None:
    """A signature with no inputs raises ``ServiceError``."""
    code = """\
import dspy
class NoIn(dspy.Signature):
    answer: str = dspy.OutputField()
"""
    sig_cls = load_signature_from_code(code)

    with pytest.raises(ServiceError, match="at least one input"):
        extract_signature_fields(sig_cls)


def test_extract_signature_fields_raises_when_no_outputs() -> None:
    """A signature with no outputs raises ``ServiceError``."""
    code = """\
import dspy
class NoOut(dspy.Signature):
    question: str = dspy.InputField()
"""
    sig_cls = load_signature_from_code(code)

    with pytest.raises(ServiceError, match="at least one"):
        extract_signature_fields(sig_cls)


def test_is_signature_field_returns_false_for_plain_string() -> None:
    """A plain string is not a DSPy field."""
    assert _is_signature_field("not-a-field", field_type="input") is False


def test_is_signature_field_returns_false_for_none() -> None:
    """``None`` is not a DSPy field."""
    assert _is_signature_field(None, field_type="output") is False


def test_is_signature_field_returns_false_for_plain_int() -> None:
    """An ``int`` is not a DSPy field."""
    assert _is_signature_field(42, field_type="input") is False


def test_is_signature_field_returns_true_for_dspy_input_field() -> None:
    """``dspy.InputField()`` is detected as an input field."""
    field = dspy.InputField()

    assert _is_signature_field(field, field_type="input") is True


def test_is_signature_field_returns_false_for_dspy_input_field_when_querying_output() -> None:
    """An input field is not classified as output."""
    field = dspy.InputField()

    assert _is_signature_field(field, field_type="output") is False


def test_is_signature_field_returns_true_for_dspy_output_field() -> None:
    """``dspy.OutputField()`` is detected as an output field."""
    field = dspy.OutputField()

    assert _is_signature_field(field, field_type="output") is True


def test_is_signature_field_detects_via_dspy_field_type_marker() -> None:
    """Detection via the ``__dspy_field_type`` instance attribute."""
    # Must use setattr to avoid Python's name-mangling of double-underscore names
    obj: object = object.__new__(type("_FakeField", (), {}))
    setattr(obj, "__dspy_field_type", "input")

    assert _is_signature_field(obj, field_type="input") is True


def test_is_signature_field_detects_via_json_schema_extra() -> None:
    """Detection via the ``json_schema_extra`` schema dict."""

    class _SchemaField:
        json_schema_extra = {"__dspy_field_type": "output"}

    assert _is_signature_field(_SchemaField(), field_type="output") is True


_IMAGE_SIG_CODE = """\
import dspy
class VisionQA(dspy.Signature):
    picture: dspy.Image = dspy.InputField()
    question: str = dspy.InputField()
    answer: str = dspy.OutputField()
"""


def test_image_input_field_names_returns_image_typed_inputs() -> None:
    """Inputs typed as ``dspy.Image`` are returned by ``image_input_field_names``."""
    sig = load_signature_from_code(_IMAGE_SIG_CODE)

    fields = image_input_field_names(sig)

    assert fields == {"picture"}


def test_image_input_field_names_empty_for_text_only_signature() -> None:
    """A text-only signature returns an empty image-field set."""
    sig = load_signature_from_code(_VALID_SIG)

    assert image_input_field_names(sig) == set()


def test_image_input_field_names_ignores_image_typed_outputs() -> None:
    """Only inputs typed as Image are returned — outputs are skipped."""
    sig = load_signature_from_code(_IMAGE_SIG_CODE)

    fields = image_input_field_names(sig)

    assert "answer" not in fields
