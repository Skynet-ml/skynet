"""Tests for the ``/validate-code`` and ``/format-code`` endpoints."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ..routers.code_validation import create_code_validation_router


@pytest.fixture
def cv_client() -> TestClient:
    """Build a ``TestClient`` exposing only the code-validation router.

    Returns:
        A ``TestClient`` over a minimal FastAPI app.
    """
    app = FastAPI()
    app.include_router(create_code_validation_router())
    return TestClient(app, raise_server_exceptions=False)


def test_validate_code_returns_invalid_when_no_code_supplied(cv_client: TestClient) -> None:
    """An empty payload is reported as invalid with field-specific errors."""
    payload = {
        "column_mapping": {"inputs": {"q": "question"}, "outputs": {"a": "answer"}},
    }

    resp = cv_client.post("/validate-code", json=payload)

    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is False
    assert any("signature_code" in e or "metric_code" in e for e in body["errors"])


def test_validate_code_signature_parse_error_is_reported(cv_client: TestClient) -> None:
    """Syntactically invalid signature code surfaces as a validation error."""
    payload = {
        "signature_code": "this is not valid python ???",
        "column_mapping": {"inputs": {"q": "question"}, "outputs": {"a": "answer"}},
    }

    resp = cv_client.post("/validate-code", json=payload)

    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is False
    assert len(body["errors"]) > 0


def test_validate_code_valid_signature_returns_signature_fields(cv_client: TestClient) -> None:
    """A valid signature is reported as such with parsed input/output fields."""
    sig = (
        "import dspy\n"
        "class Sig(dspy.Signature):\n"
        "    question: str = dspy.InputField()\n"
        "    answer: str = dspy.OutputField()\n"
    )
    payload = {
        "signature_code": sig,
        "column_mapping": {"inputs": {"question": "question"}, "outputs": {"answer": "answer"}},
    }

    resp = cv_client.post("/validate-code", json=payload)

    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is True
    assert body["signature_fields"] is not None
    assert "question" in body["signature_fields"]["inputs"]
    assert "answer" in body["signature_fields"]["outputs"]


@pytest.mark.parametrize("site", ["top_level", "decorator", "class_body", "annotation", "field_default"])
def test_validate_code_never_executes_signature_source(cv_client: TestClient, tmp_path: Path, site: str) -> None:
    """Leave every executable signature expression inert while reading its fields.

    Args:
        cv_client: Isolated authoring endpoint client.
        tmp_path: Private directory whose marker proves an execution side effect.
        site: Authored execution surface populated by the test.
    """
    marker = tmp_path / "signature-executed"
    effect = f"__import__('pathlib').Path({str(marker)!r}).write_text('executed')"
    code = (
        (effect + "\n" if site == "top_level" else "")
        + (f"@({effect} or (lambda cls: cls))\n" if site == "decorator" else "")
        + "class Sig(dspy.Signature):\n"
        + (f"    {effect}\n" if site == "class_body" else "")
        + f"    question: {effect if site == 'annotation' else 'str'} = dspy.InputField()\n"
        + f"    answer: str = dspy.OutputField({effect if site == 'field_default' else ''})\n"
    )

    response = cv_client.post(
        "/validate-code",
        json={
            "signature_code": code,
            "column_mapping": {"inputs": {"question": "question"}, "outputs": {"answer": "answer"}},
        },
    )

    assert response.status_code == 200
    assert response.json()["valid"] is True
    if site in {"decorator", "class_body"}:
        assert response.json()["signature_fields"] is None
        assert any("protected setup" in warning for warning in response.json()["warnings"])
    else:
        assert response.json()["signature_fields"] == {"inputs": ["question"], "outputs": ["answer"]}
    assert not marker.exists()


@pytest.mark.parametrize("site", ["top_level", "decorator", "annotation", "argument_default", "body"])
def test_validate_code_never_executes_metric_or_heldout_rows(cv_client: TestClient, tmp_path: Path, site: str) -> None:
    """Keep metric source and a legacy held-out sample inert in the API process.

    Args:
        cv_client: Isolated authoring endpoint client.
        tmp_path: Private directory whose marker proves an execution side effect.
        site: Authored execution surface populated by the test.
    """
    marker = tmp_path / "metric-executed"
    effect = f"__import__('pathlib').Path({str(marker)!r}).write_text('executed')"
    code = (
        (effect + "\n" if site == "top_level" else "")
        + (f"@({effect} or (lambda fn: fn))\n" if site == "decorator" else "")
        + f"def metric(gold: {effect if site == 'annotation' else 'object'}, pred, "
        + f"trace={effect if site == 'argument_default' else 'None'}, pred_name=None, pred_trace=None):\n"
        + (f"    {effect}\n" if site == "body" else "")
        + "    raise AssertionError(gold.answer)\n"
    )

    response = cv_client.post(
        "/validate-code",
        json={
            "metric_code": code,
            "optimizer_name": "gepa",
            "column_mapping": {"inputs": {"question": "question"}, "outputs": {"answer": "answer"}},
            "sample_row": {"question": "held-out question", "answer": "held-out-secret-answer"},
        },
    )

    assert response.status_code == 200
    assert response.json()["valid"] is True
    assert response.json()["errors"] == []
    assert "held-out-secret-answer" not in response.text
    assert not marker.exists()


def test_validate_code_reads_import_aliases_without_importing_modules(cv_client: TestClient) -> None:
    """Read explicit DSPy aliases even when other imports cannot run on the host."""
    response = cv_client.post(
        "/validate-code",
        json={
            "signature_code": (
                "import skynet_test_module_that_does_not_exist\n"
                "from dspy import Signature as Base, InputField as In, OutputField as Out\n"
                "class Sig(Base):\n"
                "    question: str = In()\n"
                "    answer = Out()\n"
            ),
            "metric_code": "import skynet_test_module_that_does_not_exist\ndef metric(*args): return 1.0\n",
            "optimizer_name": "gepa",
            "column_mapping": {"inputs": {"question": "q"}, "outputs": {"answer": "a"}},
        },
    )

    assert response.status_code == 200
    assert response.json()["valid"] is True
    assert response.json()["signature_fields"] == {"inputs": ["question"], "outputs": ["answer"]}


@pytest.mark.parametrize(
    "signature",
    [
        "Sig = dspy.Signature('question -> answer')",
        "def build():\n    return dspy.Signature('question -> answer')\nSig = build()",
        "class Sig(dspy.Signature):\n    question = make_input()\n    answer = dspy.OutputField()",
        "class Sig(dspy.Signature):\n    if use_question:\n        question = dspy.InputField()",
        "from project_signatures import Sig",
    ],
)
def test_validate_code_defers_dynamic_signature_fields(cv_client: TestClient, signature: str) -> None:
    """Keep runtime-built signatures available without inventing fields or executing code.

    Args:
        cv_client: Isolated authoring endpoint client.
        signature: Valid Python whose final DSPy fields require execution.
    """
    response = cv_client.post(
        "/validate-code",
        json={
            "signature_code": signature,
            "column_mapping": {"inputs": {"question": "q"}, "outputs": {"answer": "a"}},
        },
    )

    assert response.status_code == 200
    assert response.json()["valid"] is True
    assert response.json()["signature_fields"] is None
    assert response.json()["errors"] == []
    assert any("protected setup" in warning for warning in response.json()["warnings"])


@pytest.mark.parametrize(
    ("metric", "error"),
    [
        ("def metric(gold, pred, *, trace=None, pred_name=None, pred_trace=None): return 1", "5 arguments"),
        ("async def metric(gold, pred, trace, pred_name, pred_trace): return 1", "synchronous"),
        ("metric = 42", "callable named 'metric'"),
    ],
)
def test_validate_code_reports_static_metric_interface_errors(cv_client: TestClient, metric: str, error: str) -> None:
    """Report visible interface errors without constructing a callable.

    Args:
        cv_client: Isolated authoring endpoint client.
        metric: Source with a statically visible interface error.
        error: Expected useful error text.
    """
    response = cv_client.post(
        "/validate-code",
        json={
            "metric_code": metric,
            "optimizer_name": "gepa",
            "column_mapping": {"inputs": {"question": "q"}, "outputs": {"answer": "a"}},
        },
    )

    assert response.status_code == 200
    assert response.json()["valid"] is False
    assert any(error in item for item in response.json()["errors"])


def test_validate_code_prefers_metric_over_a_helper_named_score(cv_client: TestClient) -> None:
    """A ``metric`` beside a helper named ``score`` still validates, as it did before ``score`` became an entry point."""
    metric = "def score(a, b):\n    return float(a == b)\n\ndef metric(example, pred, trace=None):\n    return score(example, pred)\n"
    mapping = {"inputs": {"q": "question"}, "outputs": {"a": "answer"}}
    response = cv_client.post("/validate-code", json={"metric_code": metric, "column_mapping": mapping})

    assert response.status_code == 200
    assert response.json()["valid"] is True


def test_validate_code_accepts_a_score_entry_point_beside_helpers(cv_client: TestClient) -> None:
    """A repository scorer's ``score`` with a helper validates with the mapping the setup agent's wrapper fills."""
    scorer = "def _run(path):\n    return 1.0\n\ndef score(repo_path, case=None):\n    return _run(repo_path), 'ok'\n"
    mapping = {"inputs": {"repo_path": "repo_path"}, "outputs": {}}
    response = cv_client.post("/validate-code", json={"metric_code": scorer, "column_mapping": mapping})

    assert response.status_code == 200
    assert response.json()["valid"] is True
    assert response.json()["errors"] == []


def test_validate_code_missing_input_column_mapping_reports_error(cv_client: TestClient) -> None:
    """A signature input not present in ``column_mapping.inputs`` is an error."""
    sig = (
        "import dspy\n"
        "class Sig(dspy.Signature):\n"
        "    question: str = dspy.InputField()\n"
        "    answer: str = dspy.OutputField()\n"
    )
    payload = {
        "signature_code": sig,
        "column_mapping": {"inputs": {"q": "question"}, "outputs": {"answer": "answer"}},
    }

    resp = cv_client.post("/validate-code", json=payload)

    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is False
    assert any("question" in e for e in body["errors"])


def test_validate_code_extra_mapped_column_appears_in_warnings(cv_client: TestClient) -> None:
    """Extra mapped columns that aren't in the signature surface as warnings."""
    sig = (
        "import dspy\n"
        "class Sig(dspy.Signature):\n"
        "    question: str = dspy.InputField()\n"
        "    answer: str = dspy.OutputField()\n"
    )
    payload = {
        "signature_code": sig,
        "column_mapping": {
            "inputs": {"question": "question", "topic": "topic"},
            "outputs": {"answer": "answer"},
        },
    }

    resp = cv_client.post("/validate-code", json=payload)

    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is True
    assert any("topic" in w for w in body["warnings"])


def test_validate_code_metric_parse_error_is_reported(cv_client: TestClient) -> None:
    """Syntactically invalid metric code surfaces as a validation error."""
    payload = {
        "metric_code": "def metric(: this is garbage",
        "column_mapping": {"inputs": {"q": "question"}, "outputs": {"a": "answer"}},
    }

    resp = cv_client.post("/validate-code", json=payload)

    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is False
    assert len(body["errors"]) > 0


def test_validate_code_valid_metric_without_signature_returns_valid(cv_client: TestClient) -> None:
    """A valid metric without signature code passes validation cleanly."""
    metric = "def metric(example, pred, trace=None):\n    return float(example.answer == pred.answer)\n"
    payload = {
        "metric_code": metric,
        "column_mapping": {"inputs": {"q": "question"}, "outputs": {"a": "answer"}},
    }

    resp = cv_client.post("/validate-code", json=payload)

    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is True
    assert body["errors"] == []


def test_validate_code_gepa_rejects_metric_with_too_few_params(cv_client: TestClient) -> None:
    """GEPA optimizers require five-parameter metrics; fewer is rejected."""
    metric = "def metric(example, pred, trace=None): return 1.0"
    payload = {
        "metric_code": metric,
        "optimizer_name": "gepa",
        "column_mapping": {"inputs": {"q": "question"}, "outputs": {"a": "answer"}},
    }

    resp = cv_client.post("/validate-code", json=payload)

    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is False
    assert any("GEPA" in e for e in body["errors"])


def test_validate_code_gepa_accepts_metric_with_five_params(cv_client: TestClient) -> None:
    """GEPA accepts a metric with the full five-parameter signature."""
    metric = (
        "import dspy\n"
        "def metric(gold, pred, trace, pred_name, pred_trace):\n"
        "    return dspy.Prediction(score=1.0, feedback='ok')\n"
    )
    payload = {
        "metric_code": metric,
        "optimizer_name": "gepa",
        "column_mapping": {"inputs": {"q": "question"}, "outputs": {"a": "answer"}},
    }

    resp = cv_client.post("/validate-code", json=payload)

    assert resp.status_code == 200
    body = resp.json()
    assert not any("GEPA" in e for e in body["errors"])


def test_validate_code_defers_metric_behavior_to_protected_continue(cv_client: TestClient) -> None:
    """Check the declared interface without fabricating a prediction or scoring it."""
    metric = (
        "import dspy\n"
        "def metric(gold, pred, trace=None, pred_name=None, pred_trace=None):\n"
        "    g = getattr(gold, 'answer', None) or gold.get('answer') if isinstance(gold, dict) else None\n"
        "    p = getattr(pred, 'answer', None)\n"
        "    return dspy.Prediction(score=float(g == p), feedback='')\n"
    )
    payload = {
        "metric_code": metric,
        "optimizer_name": "gepa",
        "column_mapping": {"inputs": {"question": "question"}, "outputs": {"answer": "answer"}},
        "sample_row": {"question": "Capital of France?", "answer": "Paris"},
    }

    resp = cv_client.post("/validate-code", json=payload)

    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is True
    assert body["errors"] == []
    assert any("not executed" in warning for warning in body["warnings"])


def test_validate_code_gepa_accepts_metric_with_correct_dot_access(cv_client: TestClient) -> None:
    """Accept a declared GEPA interface without evaluating its field access."""
    metric = (
        "import dspy\n"
        "def metric(gold, pred, trace=None, pred_name=None, pred_trace=None):\n"
        "    return dspy.Prediction(score=float(gold.answer == pred.answer), feedback='')\n"
    )
    payload = {
        "metric_code": metric,
        "optimizer_name": "gepa",
        "column_mapping": {"inputs": {"question": "question"}, "outputs": {"answer": "answer"}},
        "sample_row": {"question": "Capital of France?", "answer": "Paris"},
    }

    resp = cv_client.post("/validate-code", json=payload)

    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is True
    assert body["errors"] == []


def test_validate_code_returns_422_on_missing_column_mapping(cv_client: TestClient) -> None:
    """``column_mapping`` is required; its absence is a 422."""
    resp = cv_client.post("/validate-code", json={"signature_code": "x = 1"})

    assert resp.status_code == 422


def test_validate_code_returns_422_on_invalid_column_mapping(cv_client: TestClient) -> None:
    """``column_mapping.inputs`` must be non-empty; an empty dict is a 422."""
    payload = {
        "signature_code": "x = 1",
        "column_mapping": {"inputs": {}, "outputs": {"a": "answer"}},
    }

    resp = cv_client.post("/validate-code", json=payload)

    assert resp.status_code == 422


def test_format_code_happy_path_returns_200_and_formatted_code(cv_client: TestClient) -> None:
    """Unformatted but valid Python is normalised by ruff."""
    payload = {"code": "x=1+2\ny  =  3\n"}

    resp = cv_client.post("/format-code", json=payload)

    assert resp.status_code == 200
    body = resp.json()
    assert body["error"] is None
    assert isinstance(body["code"], str)
    assert isinstance(body["changed"], bool)


def test_format_code_already_formatted_returns_changed_false(cv_client: TestClient) -> None:
    """Well-formatted code is reported as unchanged."""
    payload = {"code": "x = 1 + 2\ny = 3\n"}

    resp = cv_client.post("/format-code", json=payload)

    assert resp.status_code == 200
    body = resp.json()
    assert body["error"] is None
    assert body["changed"] is False
    assert body["code"] == payload["code"]


def test_format_code_invalid_python_returns_200_with_error_set(cv_client: TestClient) -> None:
    """Syntactically invalid input keeps the original code and reports an error."""
    payload = {"code": "def foo(:\n    pass\n"}

    resp = cv_client.post("/format-code", json=payload)

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == payload["code"]
    assert body["changed"] is False
    assert body["error"] is not None
    assert len(body["error"]) > 0


def test_format_code_empty_payload_returns_422(cv_client: TestClient) -> None:
    """An empty body is rejected with a 422 since ``code`` is required."""
    resp = cv_client.post("/format-code", json={})

    assert resp.status_code == 422


def test_format_code_roundtrip_is_stable(cv_client: TestClient) -> None:
    """Formatting is idempotent: the second pass leaves the result unchanged."""
    payload = {"code": "x=1\ny=2\n"}

    first = cv_client.post("/format-code", json=payload)
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["error"] is None

    second = cv_client.post("/format-code", json={"code": first_body["code"]})
    assert second.status_code == 200
    second_body = second.json()

    assert second_body["changed"] is False
    assert second_body["code"] == first_body["code"]


def test_validate_code_react_metric_accepts_two_arg_signature(cv_client: TestClient) -> None:
    """Accept the declared ReAct interface without applying the GEPA arity requirement."""
    payload = {
        "metric_code": "def metric(example, rollout):\n    return 1.0\n",
        "column_mapping": {"inputs": {"q": "question"}, "outputs": {"a": "answer"}},
        "sample_row": {"question": "hi", "answer": "yo"},
        "optimizer_name": "gepa",
        "module_name": "react",
    }

    resp = cv_client.post("/validate-code", json=payload)

    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is True, body["errors"]


def test_validate_code_non_react_two_arg_metric_hits_gepa_gate(cv_client: TestClient) -> None:
    """The same 2-arg metric on a non-react GEPA run is rejected by the 5-arg gate."""
    payload = {
        "metric_code": "def metric(example, rollout):\n    return 1.0\n",
        "column_mapping": {"inputs": {"q": "question"}, "outputs": {"a": "answer"}},
        "optimizer_name": "gepa",
    }

    resp = cv_client.post("/validate-code", json=payload)

    assert resp.status_code == 200
    body = resp.json()
    assert body["valid"] is False
    assert any("5 arguments" in e for e in body["errors"])
