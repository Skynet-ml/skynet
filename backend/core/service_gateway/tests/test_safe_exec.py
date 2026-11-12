"""Subprocess-isolation smoke tests for ``safe_exec``.

These tests spawn real subprocesses — no mocks, no fakes. The whole point
is to verify the boundary actually contains user-authored exec, including
its failure modes (syntax errors, timeouts, wrong return shapes).
"""

from __future__ import annotations

import pytest

from core.exceptions import ServiceError
from core.service_gateway import safe_exec
from core.service_gateway.safe_exec import (
    SignatureIntrospection,
    validate_scorer_code,
    validate_signature_code,
)

_VALID_SIG = """\
import dspy
class QA(dspy.Signature):
    question: str = dspy.InputField()
    answer: str = dspy.OutputField()
"""

_IMAGE_SIG = """\
import dspy
class VisionQA(dspy.Signature):
    picture: dspy.Image = dspy.InputField()
    question: str = dspy.InputField()
    answer: str = dspy.OutputField()
"""


class TestValidateSignatureCode:
    """Tests for ``validate_signature_code``."""

    def test_returns_fields_for_valid_signature(self) -> None:
        """A valid signature returns a ``SignatureIntrospection`` with input/output names."""
        intro = validate_signature_code(_VALID_SIG)

        assert isinstance(intro, SignatureIntrospection)
        assert intro.class_name == "QA"
        assert intro.input_fields == ["question"]
        assert intro.output_fields == ["answer"]
        assert intro.image_input_fields == []

    def test_image_input_fields_surfaced_for_dspy_image_annotation(self) -> None:
        """``dspy.Image``-annotated inputs appear in ``image_input_fields``."""
        intro = validate_signature_code(_IMAGE_SIG)

        assert intro.image_input_fields == ["picture"]
        assert "picture" in intro.input_fields
        assert "question" in intro.input_fields

    def test_syntax_error_surfaces_as_service_error(self) -> None:
        """A syntactically invalid signature raises ``ServiceError``."""
        with pytest.raises(ServiceError, match="syntax error"):
            validate_signature_code("def !!! invalid python")

    def test_no_signature_class_surfaces_as_service_error(self) -> None:
        """Source without a ``dspy.Signature`` subclass raises ``ServiceError``."""
        with pytest.raises(ServiceError, match=r"dspy\.Signature"):
            validate_signature_code("x = 1")

    def test_infinite_loop_is_terminated(self) -> None:
        """A user infinite loop is terminated by the subprocess timeout."""
        # Regression: subprocess must enforce timeout on user code, otherwise
        # a `while True` in module-level scope hangs the validator forever.
        with pytest.raises(ServiceError, match="timeout"):
            validate_signature_code("while True: pass", timeout_seconds=2.0)


class TestBoundedCachePut:
    """The validation memo dicts stay bounded in the long-lived API process."""

    def test_evicts_oldest_at_cap(self) -> None:
        """Insertion past the cap drops the oldest key, never the newest."""
        cache: dict[str, int] = {}
        for i in range(safe_exec._VALIDATION_CACHE_MAX_ENTRIES + 10):
            safe_exec._bounded_cache_put(cache, f"code-{i}", i)

        assert len(cache) == safe_exec._VALIDATION_CACHE_MAX_ENTRIES
        assert "code-0" not in cache
        assert f"code-{safe_exec._VALIDATION_CACHE_MAX_ENTRIES + 9}" in cache

    def test_rewriting_existing_key_does_not_evict(self) -> None:
        """Updating a present key at the cap must not shrink the cache."""
        cache: dict[str, int] = {}
        for i in range(safe_exec._VALIDATION_CACHE_MAX_ENTRIES):
            safe_exec._bounded_cache_put(cache, f"code-{i}", i)
        safe_exec._bounded_cache_put(cache, "code-0", 999)

        assert len(cache) == safe_exec._VALIDATION_CACHE_MAX_ENTRIES
        assert cache["code-0"] == 999


_VALID_SCORER = "def score(candidate, case=None):\n    return len(candidate) / 10, {'length': len(candidate)}\n"


class TestValidateScorerCode:
    """Tests for ``validate_scorer_code``."""

    def test_valid_scorer_passes(self) -> None:
        """A ``score(candidate, case)`` function loads cleanly."""
        validate_scorer_code(_VALID_SCORER)

    def test_metric_entrypoint_is_accepted(self) -> None:
        """The DSPy-style ``metric`` name is accepted as the scorer entrypoint."""
        validate_scorer_code("def metric(candidate, case=None):\n    return 1.0\n")

    def test_missing_function_is_rejected(self) -> None:
        """Code without a scorer function raises ``ServiceError``."""
        with pytest.raises(ServiceError, match="must define a function named 'score"):
            validate_scorer_code("x = 1\n")

    def test_syntax_error_is_rejected(self) -> None:
        """Unparseable code raises ``ServiceError`` mentioning the syntax error."""
        with pytest.raises(ServiceError, match="syntax error"):
            validate_scorer_code("def !!!")

    def test_import_failure_is_rejected(self) -> None:
        """Code that blows up at import time raises ``ServiceError`` with the exception."""
        with pytest.raises(ServiceError, match="failed to load: RuntimeError: boom"):
            validate_scorer_code("raise RuntimeError('boom')\n")

    def test_helpers_are_bound_while_loading(self) -> None:
        """Module-level use of the sandbox helpers (``llm``, ``Image``) loads cleanly."""
        code = "assert callable(llm)\nPLACEHOLDER = Image(base64_data='aGk=')\ndef score(candidate, case=None):\n    return 1.0\n"

        assert validate_scorer_code(code) is None

    def test_helpers_can_be_imported_from_skynet(self) -> None:
        """The explicit ``from skynet import llm, Image`` form loads cleanly too."""
        code = "from skynet import llm, Image\n\ndef score(candidate, case=None):\n    return 1.0\n"

        assert validate_scorer_code(code) is None
