"""The setup check words its contract failure in the caller's language, English by default."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.api.auth import AuthenticatedUser, get_authenticated_user
from core.api.preflight_execution import WizardPreflightCheck, WizardPreflightResponse
from core.api.routers import wizard_preflight
from core.api.routers.execution_budgets import ExecutionBudgetResponse
from core.i18n import request_locale, t, t_locale

RUN_CODE = "def run(artifact, input):\n    return artifact\n"
METRIC_CODE = "def score(input, output, expected=None):\n    return 1.0, {'feedback': 'ok'}\n"
ERROR = "SyntaxError: invalid syntax"
TRACE = 'File "metric.py", line 1\n    def score(:\n' + ERROR


def test_missing_scorer_model_message_has_localized_code() -> None:
    """Give the retired picker message a catalog code."""
    message = "Set the model with llm(model=...) in the metric code."
    assert wizard_preflight._message_code(message) == ("submit.preflight.scorer_model", {})
    assert t_locale("he", "submit.preflight.scorer_model") != message


def _spec() -> dict[str, Any]:
    """Build a unified spec as the wizard sends it."""
    return {
        "task": {"objective": "o", "artifact": {"kind": "dspy_program", "starting_version": "p"}},
        "data": {
            "mode": "pairs",
            "cases": [{"input": {"q": str(i)}, "expected": "p"} for i in range(3)],
            "input_fields": ["q"],
        },
        "optimizer": {"choice": "dspy_gepa"},
        "models": {"reflection_model": {"name": "openrouter/task"}},
        "workspace": {"run_code": RUN_CODE, "metric_code": METRIC_CODE},
    }


def _budget() -> ExecutionBudgetResponse:
    """Build a budget snapshot for the response."""
    zero = Decimal(0)
    return ExecutionBudgetResponse(
        id="b1",
        total_cents=100,
        uncapped=False,
        revision=1,
        generation=1,
        state="draft",
        job_id=None,
        setup_spent_cents=zero,
        run_spent_cents=zero,
        reserved_cents=zero,
        available_cents=Decimal(100),
        billed_cents=0,
        wallet_setup_spent_cents=zero,
        wallet_run_spent_cents=zero,
        wallet_reserved_cents=0,
        account_available_cents=100,
        external_spent_cents=zero,
        pending_operations=0,
        blocked_reason=None,
    )


def _failed_sample(*, coded: bool) -> dict[str, Any]:
    """Build the failed metric sample the sandbox reports, with or without its code.

    Args:
        coded: Whether the sandbox's backend already sends ``message_code``.

    Returns:
        One contract check item as JSON.
    """
    params = {"index": 1, "error": ERROR}
    item: dict[str, Any] = {
        "key": "metric.shape",
        "status": "failed",
        "message": t("contract.metric_error", **params),
        "sample_index": 0,
        "detail": TRACE,
    }
    if coded:
        item.update(message_code="contract.metric_error", message_params=params)
    return item


@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Answer every setup check with a failed contract worded in the Hebrew base.

    Args:
        monkeypatch: Replaces the paid setup work.

    Returns:
        The sample the stored contract reports; tests may edit it before calling.
    """
    sample = _failed_sample(coded=True)

    def run(request: Any, user: Any, store: Any) -> WizardPreflightResponse:
        """Return what the sandbox check stored, as the real path does."""
        return WizardPreflightResponse(
            id="p1",
            fingerprint="f",
            status="failed",
            may_advance=False,
            checks=[
                WizardPreflightCheck(
                    key="metric.contract",
                    status="failed",
                    message=f"{sample['message']}\n{TRACE}",
                    field="workspace",
                )
            ],
            budget=_budget(),
            workflow_result={
                "contract": {
                    "ok": False,
                    "optimizer_resolved": "best_of_n",
                    "checks": [sample],
                    "samples": [],
                    "fingerprint": "f",
                }
            },
        )

    monkeypatch.setattr(wizard_preflight, "run_preflight", run)
    monkeypatch.setattr(wizard_preflight, "enforce_submission_rate", lambda username: None)
    monkeypatch.setattr(wizard_preflight, "_materialize_unified_cases", lambda *args, **kwargs: None)
    return sample


def _body(**extra: Any) -> dict[str, Any]:
    """Build a unified evaluation setup check request."""
    return {
        "scope": "evaluation",
        "workflow": "unified",
        "payload": _spec(),
        "execution_budget_id": "b1",
        "execution_budget_revision": 1,
        **extra,
    }


def _post(path: str, body: dict[str, Any], headers: dict[str, str] | None = None) -> Any:
    """Send one setup check as a signed-in user.

    Args:
        path: Route to call.
        body: Request body.
        headers: Extra request headers.

    Returns:
        The HTTP response.
    """
    app = FastAPI()
    app.include_router(wizard_preflight.create_wizard_preflight_router(job_store=object()))
    app.dependency_overrides[get_authenticated_user] = lambda: AuthenticatedUser("alice", "user", ())
    with TestClient(app) as client:
        # TestClient sends no Accept-Language unless a test sets one.
        return client.post(path, json=body, headers=headers or {})


def _contract_check(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the ``metric.contract`` check of a setup check response."""
    return next(check for check in payload["checks"] if check["key"] == "metric.contract")


def test_english_account_without_accept_language_reads_english(stored: dict[str, Any]) -> None:
    """With no locale and no ``Accept-Language`` the failure reads in English, not Hebrew."""
    response = _post("/wizard/preflight", _body())

    assert response.status_code == 200
    check = _contract_check(response.json())
    english = t_locale("en", "contract.metric_error", index=1, error=ERROR)
    assert english != t("contract.metric_error", index=1, error=ERROR)
    assert check["message"] == f"{english}\n{TRACE}"
    assert check["message_code"] == "contract.metric_error"
    assert check["message_params"] == {"index": 1, "error": ERROR}


def test_ui_locale_wins_over_the_browser_language(stored: dict[str, Any]) -> None:
    """An English UI in a Hebrew browser still reads English."""
    response = _post("/wizard/preflight", _body(locale="en"), headers={"Accept-Language": "he-IL,he;q=0.9"})

    english = t_locale("en", "contract.metric_error", index=1, error=ERROR)
    assert _contract_check(response.json())["message"] == f"{english}\n{TRACE}"


def test_accept_language_is_used_when_the_body_names_no_locale(stored: dict[str, Any]) -> None:
    """A Hebrew ``Accept-Language`` with no body locale keeps the Hebrew wording."""
    response = _post("/wizard/preflight", _body(), headers={"Accept-Language": "he"})

    hebrew = t("contract.metric_error", index=1, error=ERROR)
    assert _contract_check(response.json())["message"] == f"{hebrew}\n{TRACE}"


def test_an_uncoded_sandbox_failure_is_recovered_and_worded_in_english(stored: dict[str, Any]) -> None:
    """An older sandbox image sends no message code; the host recovers it from the Hebrew text."""
    stored.clear()
    stored.update(_failed_sample(coded=False))

    check = _contract_check(_post("/wizard/preflight", _body()).json())

    assert check["message_code"] == "contract.metric_error"
    assert check["message"].startswith(t_locale("en", "contract.metric_error", index=1, error=ERROR))


def test_stream_result_reads_english_without_accept_language(stored: dict[str, Any]) -> None:
    """The streaming setup check words its final result the same way."""
    response = _post("/wizard/preflight/stream", _body())

    frames = [
        json.loads(line.removeprefix("data: ")) for line in response.text.splitlines() if line.startswith("data: ")
    ]
    result = frames[-1]
    english = t_locale("en", "contract.metric_error", index=1, error=ERROR)
    assert _contract_check(result)["message"] == f"{english}\n{TRACE}"


@pytest.mark.parametrize(
    ("explicit", "header", "expected"),
    [
        (None, None, "en"),
        ("", "", "en"),
        ("fr-CA", "he", "fr-CA"),
        (None, "he-IL,he;q=0.9,en;q=0.8", "he-IL"),
        (None, "en;q=0.5, ar;q=0.9", "ar"),
        (None, "*, de;q=0", "en"),
        ("not a tag!", "pt-BR", "pt-BR"),
    ],
)
def test_request_locale_prefers_the_ui_then_the_browser_then_english(
    explicit: str | None, header: str | None, expected: str
) -> None:
    """The body locale wins, then the best-ranked browser language, then English."""
    assert request_locale(explicit, header) == expected


def test_nested_contract_checks_read_english_without_accept_language(stored: dict[str, Any]) -> None:
    """API callers that read ``workflow_result.contract`` get English, not the stored Hebrew."""
    payload = _post("/wizard/preflight", _body()).json()

    nested = payload["workflow_result"]["contract"]["checks"][0]
    assert nested["message"] == t_locale("en", "contract.metric_error", index=1, error=ERROR)
    assert nested["message_code"] == "contract.metric_error"
    assert stored["message"] == t("contract.metric_error", index=1, error=ERROR)


def _fixed_text_response(message: str) -> WizardPreflightResponse:
    """Build a pending setup check whose usage check stores a fixed English text.

    Args:
        message: The fixed text the setup check stored.

    Returns:
        A setup check result with that text on a check and on its pending reason.
    """
    return WizardPreflightResponse.model_validate(
        {
            "id": "p2",
            "fingerprint": "f",
            "status": "pending",
            "may_advance": False,
            "pending_reason": {"category": "usage_reconciliation", "message": message},
            "checks": [
                {"key": "scorer", "status": "succeeded", "message": "Custom scorer note."},
                {"key": "usage", "status": "pending", "message": message},
            ],
            "budget": _budget().model_dump(),
        }
    )


@pytest.mark.parametrize(
    ("message", "key"),
    [
        ("Provider or sandbox usage is awaiting final confirmation.", "submit.preflight.usage_pending"),
        ("The total budget has no remaining allowance.", "submit.preflight.budget_exhausted"),
        ("Evaluator loads; scoring waits for the first generated candidate.", "submit.preflight.scorer_ready"),
    ],
)
@pytest.mark.parametrize("locale", ["he", "de"])
def test_fixed_english_texts_are_worded_in_the_callers_locale(
    monkeypatch: pytest.MonkeyPatch, message: str, key: str, locale: str
) -> None:
    """A known fixed English text gets its catalog code and reads in the caller's language."""
    monkeypatch.setattr(wizard_preflight, "run_preflight", lambda *args: _fixed_text_response(message))
    monkeypatch.setattr(wizard_preflight, "enforce_submission_rate", lambda username: None)
    monkeypatch.setattr(wizard_preflight, "_materialize_unified_cases", lambda *args, **kwargs: None)

    payload = _post("/wizard/preflight", _body(locale=locale)).json()

    usage = next(check for check in payload["checks"] if check["key"] == "usage")
    worded = t_locale(locale, key)
    assert worded != message
    assert usage["message"] == worded
    assert usage["message_code"] == key
    assert payload["pending_reason"]["message"] == worded
    scorer = next(check for check in payload["checks"] if check["key"] == "scorer")
    assert scorer["message"] == "Custom scorer note."
    assert "message_code" not in scorer


def test_every_fixed_text_has_a_catalog_entry_in_english() -> None:
    """Each mapped key renders, in English, the exact text it replaces."""
    for message, key in wizard_preflight.FIXED_MESSAGE_CODES.items():
        assert t_locale("en", key) == message


def test_stream_error_reads_in_the_callers_locale(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unexpected failure streams its error in the caller's language, English by default."""

    def fail(*args: Any) -> WizardPreflightResponse:
        """Fail as an unexpected setup error does."""
        raise RuntimeError("boom")

    monkeypatch.setattr(wizard_preflight, "run_preflight", fail)
    monkeypatch.setattr(wizard_preflight, "enforce_submission_rate", lambda username: None)
    monkeypatch.setattr(wizard_preflight, "_materialize_unified_cases", lambda *args, **kwargs: None)

    for headers, locale in (({}, "en"), ({"Accept-Language": "he"}, "he")):
        response = _post("/wizard/preflight/stream", _body(), headers=headers)
        frames = [
            json.loads(line.removeprefix("data: ")) for line in response.text.splitlines() if line.startswith("data: ")
        ]
        assert frames[-1] == {"detail": t_locale(locale, "submit.preflight.could_not_finish")}


def test_static_contract_check_uses_accept_language_then_english(monkeypatch: pytest.MonkeyPatch) -> None:
    """The static contract check reads English with no locale, and follows ``Accept-Language``."""
    monkeypatch.setattr(wizard_preflight, "enforce_submission_rate", lambda username: None)
    spec = _spec()
    spec["workspace"]["metric_code"] = "def score(:\n"

    english = _post("/optimizations/contract-check/static", {"spec": spec}).json()
    hebrew = _post("/optimizations/contract-check/static", {"spec": spec}, headers={"Accept-Language": "he"}).json()

    failed = next(item for item in english["checks"] if item["status"] == "failed")
    assert failed["message"] == t_locale("en", failed["message_code"], **failed["message_params"])
    assert failed["message"] != t(failed["message_code"], **failed["message_params"])
    hebrew_failed = next(item for item in hebrew["checks"] if item["key"] == failed["key"])
    assert hebrew_failed["message"] == t(failed["message_code"], **failed["message_params"])


BUDGET_TEXT = "The next operation needs up to $1.35, but only $0.53 of the $0.53 limit remain."


@pytest.mark.parametrize(
    "message",
    [
        "The account has no available funds for a preview.",
        "Setup checks are incomplete.",
        "Setup execution failed.",
        "The runtime did not return setup evidence.",
        "Setup did not return a complete set of readiness checks.",
    ],
)
def test_setup_fallback_texts_are_worded_in_the_callers_locale(monkeypatch: pytest.MonkeyPatch, message: str) -> None:
    """The setup fallback texts have catalog codes and read in Hebrew for a Hebrew caller."""
    monkeypatch.setattr(wizard_preflight, "run_preflight", lambda *args: _fixed_text_response(message))
    monkeypatch.setattr(wizard_preflight, "enforce_submission_rate", lambda username: None)
    monkeypatch.setattr(wizard_preflight, "_materialize_unified_cases", lambda *args, **kwargs: None)

    payload = _post("/wizard/preflight", _body(locale="he")).json()

    usage = next(check for check in payload["checks"] if check["key"] == "usage")
    code = wizard_preflight.FIXED_MESSAGE_CODES[message]
    assert usage["message_code"] == code
    assert usage["message"] == t_locale("he", code) != message


def test_budget_limit_text_keeps_its_amounts_in_the_callers_locale(monkeypatch: pytest.MonkeyPatch) -> None:
    """The spending limit text is coded with its amounts as params, on the check and the pending reason."""
    monkeypatch.setattr(wizard_preflight, "run_preflight", lambda *args: _fixed_text_response(BUDGET_TEXT))
    monkeypatch.setattr(wizard_preflight, "enforce_submission_rate", lambda username: None)
    monkeypatch.setattr(wizard_preflight, "_materialize_unified_cases", lambda *args, **kwargs: None)

    english = _post("/wizard/preflight", _body()).json()
    hebrew = _post("/wizard/preflight", _body(locale="he")).json()

    params = {"needed": "$1.35", "remaining": "$0.53", "limit": "$0.53"}
    usage = next(check for check in english["checks"] if check["key"] == "usage")
    assert (usage["message_code"], usage["message_params"]) == (wizard_preflight.BUDGET_LIMIT_CODE, params)
    assert usage["message"] == BUDGET_TEXT
    worded = t_locale("he", wizard_preflight.BUDGET_LIMIT_CODE, **params)
    assert worded != BUDGET_TEXT
    assert "$1.35" in worded
    assert next(check for check in hebrew["checks"] if check["key"] == "usage")["message"] == worded
    assert hebrew["pending_reason"]["message"] == worded


SANDBOXES_TEXT = "The next operation needs up to $0.35, but this check's sandboxes hold $0.10 of the $0.40 that remain."


def test_sandbox_budget_text_keeps_its_amounts_in_the_callers_locale(monkeypatch: pytest.MonkeyPatch) -> None:
    """The setup refusal for budget held by the check's own sandboxes is coded with its amounts as params."""
    monkeypatch.setattr(wizard_preflight, "run_preflight", lambda *args: _fixed_text_response(SANDBOXES_TEXT))
    monkeypatch.setattr(wizard_preflight, "enforce_submission_rate", lambda username: None)
    monkeypatch.setattr(wizard_preflight, "_materialize_unified_cases", lambda *args, **kwargs: None)

    english = _post("/wizard/preflight", _body()).json()
    hebrew = _post("/wizard/preflight", _body(locale="he")).json()

    params = {"needed": "$0.35", "held": "$0.10", "remaining": "$0.40"}
    usage = next(check for check in english["checks"] if check["key"] == "usage")
    assert (usage["message_code"], usage["message_params"]) == (wizard_preflight.BUDGET_SANDBOXES_CODE, params)
    assert usage["message"] == SANDBOXES_TEXT == t_locale("en", wizard_preflight.BUDGET_SANDBOXES_CODE, **params)
    worded = t_locale("he", wizard_preflight.BUDGET_SANDBOXES_CODE, **params)
    assert worded != SANDBOXES_TEXT
    assert all(amount in worded for amount in params.values())
    assert next(check for check in hebrew["checks"] if check["key"] == "usage")["message"] == worded
    assert hebrew["pending_reason"]["message"] == worded


def test_contract_check_without_a_budget_is_refused_in_the_callers_locale(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 402 detail reads in English by default and follows ``Accept-Language``."""
    monkeypatch.setattr(wizard_preflight, "enforce_submission_rate", lambda username: None)

    english = _post("/optimizations/contract-check", {"spec": _spec()})
    hebrew = _post("/optimizations/contract-check", {"spec": _spec()}, headers={"Accept-Language": "he"})

    assert english.status_code == hebrew.status_code == 402
    assert english.json()["detail"] == "Choose a spending budget before running the contract check."
    assert hebrew.json()["detail"] == t_locale("he", "submit.preflight.contract_budget_missing")
