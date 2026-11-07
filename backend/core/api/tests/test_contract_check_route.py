"""The contract-check routes: static answers, budget gating and the sandboxed result."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.api import preflight_execution
from core.api.auth import AuthenticatedUser, get_authenticated_user
from core.api.preflight_progress import progress_observer
from core.api.routers import wizard_preflight
from core.billing.budgets import AccountFundsError, BudgetInsufficientError
from core.i18n import t, t_locale
from core.service_gateway.optimization.blackbox import check_terminal
from core.worker.failure_events import failure_event

RUN_CODE = "def run(artifact, input):\n    return artifact\n"
METRIC_CODE = "def score(input, output, expected=None):\n    return 1.0, {'feedback': 'ok'}\n"


def _spec(*, run_code: str = RUN_CODE, budget: bool = True) -> dict[str, Any]:
    """Build a unified spec as the wizard sends it."""
    spec: dict[str, Any] = {
        "task": {"objective": "o", "artifact": {"kind": "dspy_program", "starting_version": "p"}},
        "data": {
            "mode": "pairs",
            "cases": [{"input": {"q": str(i)}, "expected": "p"} for i in range(3)],
            "input_fields": ["q"],
        },
        "optimizer": {"choice": "dspy_gepa"},
        "models": {"reflection_model": {"name": "openrouter/task"}},
        "workspace": {"run_code": run_code, "metric_code": METRIC_CODE},
    }
    if budget:
        spec["execution_budget_id"] = "b1"
        spec["execution_budget_revision"] = 1
    return spec


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Replace paid preflight work with a recorder that returns a passing contract."""
    recorded: list[Any] = []

    def run(request: Any, user: Any, store: Any) -> Any:
        """Record the request and answer with an in-box contract result."""
        recorded.append(request)
        return SimpleNamespace(
            terminal=None,
            status="succeeded",
            checks=[],
            workflow_result={
                "contract": {
                    "ok": True,
                    "optimizer_resolved": "best_of_n",
                    "metric_shape": "blackbox_side_info",
                    "checks": [],
                    "samples": [],
                    "fingerprint": "f",
                }
            },
        )

    monkeypatch.setattr(wizard_preflight, "run_preflight", run)
    monkeypatch.setattr(wizard_preflight, "enforce_submission_rate", lambda username: None)
    monkeypatch.setattr(wizard_preflight, "_materialize_unified_cases", lambda *args, **kwargs: None)
    return recorded


def _client() -> TestClient:
    """Mount the router behind a signed-in user."""
    app = FastAPI()
    app.include_router(wizard_preflight.create_wizard_preflight_router(job_store=object()))
    app.dependency_overrides[get_authenticated_user] = lambda: AuthenticatedUser("alice", "user", ())
    return TestClient(app)


def test_static_route_reports_a_mismatch_without_running(calls: list[Any]) -> None:
    """The static route answers 200 with ``ok=false`` and never dispatches work."""
    with _client() as client:
        response = client.post("/optimizations/contract-check/static", json={"spec": _spec(run_code="x = 1\n")})

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert calls == []


def test_sandbox_route_skips_the_box_when_static_checks_fail(calls: list[Any]) -> None:
    """A signature mismatch is returned as is, without spending."""
    with _client() as client:
        response = client.post("/optimizations/contract-check", json={"spec": _spec(run_code="x = 1\n")})

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert calls == []


def test_sandbox_route_needs_a_budget(calls: list[Any]) -> None:
    """Without a spending budget the check is refused with 402."""
    with _client() as client:
        response = client.post("/optimizations/contract-check", json={"spec": _spec(budget=False)})

    assert response.status_code == 402
    assert response.json()["detail"] == t_locale("en", "submit.preflight.contract_budget_missing")
    assert calls == []


def test_sandbox_route_returns_the_in_box_contract(calls: list[Any]) -> None:
    """A fitting spec runs as a unified evaluation preflight and returns its contract."""
    with _client() as client:
        response = client.post("/optimizations/contract-check", json={"spec": _spec()})

    assert response.status_code == 200
    assert response.json()["ok"] is True
    [request] = calls
    assert (request.scope, request.workflow) == ("evaluation", "unified")


def test_sandbox_route_forwards_the_sample_count(calls: list[Any]) -> None:
    """The wizard's sample count reaches the preflight; over the maximum is a 422."""
    with _client() as client:
        response = client.post("/optimizations/contract-check", json={"spec": _spec(), "sample_count": 5})
        too_many = client.post("/optimizations/contract-check", json={"spec": _spec(), "sample_count": 50})

    assert response.status_code == 200
    assert too_many.status_code == 422
    [request] = calls
    assert request.contract_sample_count == 5


def test_static_route_reports_whether_the_metric_calls_llm(calls: list[Any]) -> None:
    """``uses_llm`` mirrors the metric source, so the wizard asks for a metric model only when needed."""
    spec = _spec()
    spec["workspace"]["metric_code"] = (
        "from skynet import llm\ndef score(input, output, expected=None):\n    return float(llm('j', output)), {}\n"
    )
    with _client() as client:
        plain = client.post("/optimizations/contract-check/static", json={"spec": _spec()})
        judged = client.post("/optimizations/contract-check/static", json={"spec": spec})

    assert plain.json()["uses_llm"] is False
    assert judged.json()["uses_llm"] is True


def test_sandbox_route_gives_an_older_images_items_their_message_codes(
    calls: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pinned sandbox image can predate ``message_code``; the client still gets a code on every item.

    Without it, an English UI rendered the Hebrew base text of each item.
    """
    error = "KeyError: 'product'"
    traceback = f'Traceback (user code):\n  File "<run_code>", line 10, in run\n{error}'
    legacy_checks: list[dict[str, Any]] = [
        {"key": key, "status": "passed", "message": t("contract.passed")}
        for key in ("workspace.run_signature", "metric.signature", "metric.model")
    ]
    for index in range(3):
        legacy_checks += [
            {
                "key": "workspace.run",
                "status": "failed",
                "message": t("contract.run_error", index=index + 1, error=error),
                "sample_index": index,
                "detail": traceback,
            },
            {
                "key": "metric.shape",
                "status": "skipped",
                "message": t("contract.shape.blackbox"),
                "sample_index": index,
            },
        ]
    legacy = {
        "ok": False,
        "optimizer_resolved": "best_of_n",
        "metric_shape": "blackbox_side_info",
        "checks": legacy_checks,
        "samples": [{"index": i, "input_preview": "{}"} for i in range(3)],
        "fingerprint": "f",
    }
    monkeypatch.setattr(
        wizard_preflight,
        "run_preflight",
        lambda request, user, store: SimpleNamespace(
            terminal=None, status="failed", checks=[], workflow_result={"contract": legacy}
        ),
    )
    with _client() as client:
        body = client.post("/optimizations/contract-check", json={"spec": _spec()}).json()

    assert body["ok"] is False
    assert all(item["message_code"] for item in body["checks"])
    runs = [item for item in body["checks"] if item["key"] == "workspace.run"]
    assert [(item["message_code"], item["message_params"]) for item in runs] == [
        ("contract.run_error", {"index": i + 1, "error": error}) for i in range(3)
    ]
    assert {item["message_code"] for item in body["checks"] if item["key"] == "metric.shape"} == {
        "contract.shape.blackbox"
    }
    assert {item["message_code"] for item in body["checks"] if item["sample_index"] is None} == {"contract.passed"}


SPENT = "The next operation needs up to $1.35, but only $0.53 of the $0.53 limit remain."


@pytest.mark.parametrize(
    ("key", "code"),
    [("budget", "submit.unified.contract_budget"), ("setup", "submit.unified.contract_failed")],
)
def test_sandbox_route_tells_a_spent_budget_from_a_code_mismatch(
    monkeypatch: pytest.MonkeyPatch, calls: list[Any], key: str, code: str
) -> None:
    """A check stopped by the spending limit says so, instead of blaming the code."""
    failed = SimpleNamespace(key=key, status="failed", message=SPENT)
    monkeypatch.setattr(
        wizard_preflight,
        "run_preflight",
        lambda request, user, store: SimpleNamespace(
            terminal=None, status="failed", checks=[failed], workflow_result=None
        ),
    )
    with _client() as client:
        body = client.post("/optimizations/contract-check", json={"spec": _spec()}).json()

    [run] = [item for item in body["checks"] if item["key"] == "workspace.run"]
    assert (run["message_code"], run["message_params"]) == (code, {"error": SPENT})
    assert run["message"] == t_locale("en", code, error=SPENT)
    assert run["detail"] is None


@pytest.mark.parametrize(
    ("error", "key", "field"),
    [
        (BudgetInsufficientError(SPENT), "budget", None),
        (AccountFundsError("The account cannot fund the next operation."), "budget", "balance"),
        (RuntimeError(SPENT), "setup", None),
    ],
)
def test_preflight_reports_a_reservation_over_the_limit_as_a_budget_check(
    monkeypatch: pytest.MonkeyPatch, error: Exception, key: str, field: str | None
) -> None:
    """A runtime stopped by the budget fails the ``budget`` check; any other failure stays ``setup``.

    The sandbox supervisor reports every failure as an error event, so this
    drives the real ``_verify_anything`` through that event rather than
    raising past it. A stop the account balance caused names the balance, so
    the wizard does not offer to raise a limit that was never the problem.
    """

    class _Gateway:
        """Stand in for the model gateway without any paid transport."""

        budget_refusal = None

        def __init__(self, runtime: Any) -> None:
            """Ignore the budget runtime."""

        def protect_payload(self, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            """Hand the payload back unchanged."""
            return payload

        def close(self) -> None:
            """Nothing to settle."""

    def dispatch(payload: Any, artifact_id: str, events: Any, start_method: str) -> None:
        """Report the failure the way ``run_vercel_dspy`` does: as an error event."""
        events.put(failure_event(error))

    monkeypatch.setattr(preflight_execution, "ModelGateway", _Gateway)
    monkeypatch.setattr(preflight_execution, "bind_protected_sandbox", lambda *args, **kwargs: {})
    monkeypatch.setattr(preflight_execution, "payload_uses_token_source", lambda *args, **kwargs: False)
    monkeypatch.setattr(preflight_execution, "run_vercel_dspy", dispatch)
    request = preflight_execution.WizardPreflightRequest(
        scope="evaluation",
        workflow="anything",
        payload={"scorer": {"kind": "python", "metric_code": "def metric(candidate, case):\n    return 1.0\n"}},
        execution_budget_id="draft",
        execution_budget_revision=1,
    )

    status, result = preflight_execution._perform_preflight(
        request,
        SimpleNamespace(username="alice"),
        object(),
        SimpleNamespace(id="budget", generation=1),
        {"id": "setup"},
        attempt=1,
        engine=object(),
    )

    assert status == "failed"
    expected = {"key": key, "status": "failed", "message": str(error)}
    assert result["checks"] == [{**expected, "field": field} if field else expected]


@pytest.mark.parametrize(("refusal", "raised"), [(SPENT, BudgetInsufficientError), (None, ValueError)])
def test_unified_guest_failure_after_a_refused_call_is_a_budget_error(
    monkeypatch: pytest.MonkeyPatch, refusal: str | None, raised: type[Exception]
) -> None:
    """A guest that failed after the limit refused one of its model calls reports the budget, not the code."""

    def guest(payload: dict[str, Any], artifact_id: str, events: Any, start_method: str) -> None:
        """Fail in the guest the way a sample stopped by the limit does."""
        events.put({"type": "error", "error": "The code does not match", "traceback": ""})

    monkeypatch.setattr(preflight_execution, "run_vercel_dspy", guest)
    gateway = _RouteGateway(200)
    gateway.budget_refusal = BudgetInsufficientError(refusal) if refusal else None

    with pytest.raises(raised, match=r"\$1\.35" if refusal else "does not match"):
        preflight_execution._verify_unified(gateway, _spec(budget=False), scope="execution", identity="setup-1")


@pytest.mark.parametrize(("refusal", "raised"), [(SPENT, BudgetInsufficientError), (None, ValueError)])
def test_unified_direct_failure_after_a_refused_call_is_a_budget_error(
    monkeypatch: pytest.MonkeyPatch, refusal: str | None, raised: type[Exception]
) -> None:
    """An evaluation-scope check that failed after the limit refused a model call reports the budget."""

    def check(payload: dict[str, Any], scope: str, **kwargs: Any) -> dict[str, Any]:
        """Fail the way a sample stopped by the limit does."""
        raise ValueError("The code does not match")

    monkeypatch.setattr(preflight_execution, "verify_unified_in_sandbox", check)
    gateway = SimpleNamespace(budget_refusal=refusal, parent_sandbox_runtime=object)

    with pytest.raises(raised, match=r"\$1\.35" if refusal else "does not match"):
        preflight_execution._verify_unified(gateway, {}, scope="evaluation", identity="setup-1")


def test_unified_direct_check_uses_the_parent_box_and_reports_its_phases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Evaluation scope opens the parent's box directly and reports ``sandbox`` then ``evaluator``."""
    runtime = object()
    seen: dict[str, Any] = {}

    def check(payload: dict[str, Any], scope: str, **kwargs: Any) -> dict[str, Any]:
        """Record the runtime, emit the warm-box phase and answer with checks."""
        seen.update(kwargs)
        kwargs["on_ready"]()
        return {"checks": [], "workflow_result": {"contract": {"ok": True}}}

    monkeypatch.setattr(preflight_execution, "verify_unified_in_sandbox", check)
    phases: list[str] = []
    token = progress_observer.set(phases.append)
    try:
        result = preflight_execution._verify_unified(
            SimpleNamespace(budget_refusal=None, parent_sandbox_runtime=lambda: runtime),
            {"spec": True},
            scope="evaluation",
            identity="setup-1",
        )
    finally:
        progress_observer.reset(token)

    assert (seen["runtime"], seen["identity"]) == (runtime, "setup-1")
    assert phases == ["sandbox", "evaluator"]
    assert result["workflow_result"]["contract"]["ok"] is True


class _RouteGateway:
    """Answer the model route checks of a unified run without any paid transport."""

    budget_refusal = None

    def __init__(self, outcome: Exception | int) -> None:
        """Keep the refusal to raise, or the HTTP status to answer, for every route check."""
        self.outcome = outcome
        self.dispatched: list[str] = []

    def model_routes(self) -> list[dict[str, str]]:
        """List the one route the spec configures."""
        return [{"role": "optimization", "model": "openrouter/task", "token": "route-token"}]

    def dispatch_guest(self, token: str, path: str, body: dict[str, Any], headers: dict[str, str]) -> Any:
        """Refuse the call as the ledger does, or answer it with the configured status."""
        self.dispatched.append(token)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return SimpleNamespace(status=self.outcome, content=b'{"error": {"message": "no such model"}}')


def _sandbox_recorder(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the outer sandbox with one that records its start and answers a ready contract."""
    started: list[str] = []

    def guest(payload: dict[str, Any], artifact_id: str, events: Any, start_method: str) -> None:
        """Report the contract and optimizer checks the guest verifier returns."""
        started.append(artifact_id)
        checks = [{"key": "metric.contract", "status": "succeeded"}, {"key": "optimizer", "status": "succeeded"}]
        events.put({"type": "preflight_result", "result": {"checks": checks}})

    monkeypatch.setattr(preflight_execution, "run_vercel_dspy", guest)
    return started


def test_unified_execution_check_refuses_a_limit_below_one_call_before_the_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A limit that cannot hold one model call fails at once instead of after the contract check."""
    started = _sandbox_recorder(monkeypatch)
    gateway = _RouteGateway(BudgetInsufficientError(SPENT))

    with pytest.raises(BudgetInsufficientError, match=r"\$1\.35"):
        preflight_execution._verify_unified(gateway, _spec(budget=False), scope="execution", identity="setup-1")

    assert gateway.dispatched == ["route-token"]
    assert started == []


def test_unified_execution_check_stops_at_a_rejected_model_route(monkeypatch: pytest.MonkeyPatch) -> None:
    """A model the provider rejects ends the check with that route's failure, without opening the sandbox."""
    started = _sandbox_recorder(monkeypatch)

    result = preflight_execution._verify_unified(
        _RouteGateway(404), _spec(budget=False), scope="execution", identity="setup-1"
    )

    [check] = result["checks"]
    assert (check["key"], check["status"], check["field"]) == ("model.optimization", "failed", "optimization")
    assert "HTTP 404: no such model" in check["message"]
    assert started == []


def test_unified_execution_check_reports_model_routes_beside_the_optimizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Passing route checks sit before the optimizer check in execution scope."""
    started = _sandbox_recorder(monkeypatch)
    gateway = _RouteGateway(200)

    result = preflight_execution._verify_unified(gateway, _spec(budget=False), scope="execution", identity="setup-1")

    assert [check["key"] for check in result["checks"]] == ["metric.contract", "model.optimization", "optimizer"]
    assert started == ["preflight-setup-1"]
    assert gateway.dispatched == ["route-token"]


def test_unified_evaluation_check_never_probes_the_models_or_the_guest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Evaluation scope runs the parent-direct contract check: no route probes, no guest sandbox."""
    started = _sandbox_recorder(monkeypatch)
    gateway = _RouteGateway(200)
    gateway.parent_sandbox_runtime = object

    def check(payload: dict[str, Any], scope: str, **kwargs: Any) -> dict[str, Any]:
        """Answer the contract and optimizer checks the workspace box returns."""
        return {
            "checks": [{"key": "metric.contract", "status": "succeeded"}, {"key": "optimizer", "status": "succeeded"}]
        }

    monkeypatch.setattr(preflight_execution, "verify_unified_in_sandbox", check)

    result = preflight_execution._verify_unified(gateway, _spec(budget=False), scope="evaluation", identity="setup-1")

    assert [check["key"] for check in result["checks"]] == ["metric.contract", "optimizer"]
    assert started == []
    assert gateway.dispatched == []


def test_stream_route_emits_phases_then_the_result(calls: list[Any]) -> None:
    """The streaming route delivers phase events and the same contract the plain route returns."""
    with _client() as client:
        response = client.post("/optimizations/contract-check/stream", json={"spec": _spec()})

    assert response.status_code == 200
    body = response.text
    assert "event: result\n" in body
    assert '"ok": true' in body
    [request] = calls
    assert (request.scope, request.workflow) == ("evaluation", "unified")


def test_stream_route_reports_a_missing_budget_as_an_error_event(calls: list[Any]) -> None:
    """A refused check ends the stream with an ``error`` event, not a bare HTTP status."""
    with _client() as client:
        response = client.post("/optimizations/contract-check/stream", json={"spec": _spec(budget=False)})

    assert response.status_code == 200
    assert "event: error\n" in response.text
    assert calls == []


def test_static_route_words_its_messages_in_the_request_locale(calls: list[Any]) -> None:
    """An English caller reads English messages; a Hebrew browser with no body locale reads Hebrew."""
    with _client() as client:
        english = client.post("/optimizations/contract-check/static", json={"spec": _spec(), "locale": "en"}).json()
        base = client.post(
            "/optimizations/contract-check/static", json={"spec": _spec()}, headers={"Accept-Language": "he"}
        ).json()

    coded = [item for item in english["checks"] if item["message_code"]]
    assert coded
    for item in coded:
        assert item["message"] == t_locale("en", item["message_code"], **item["message_params"])
    passed = next(item for item in base["checks"] if item["message_code"] == "contract.passed")
    assert passed["message"] == t("contract.passed")
    assert t_locale("en", "contract.passed") != t("contract.passed")


def test_sandbox_route_words_a_budget_refusal_in_the_request_locale(
    calls: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sampled check's own items follow the request locale too."""
    monkeypatch.setattr(
        wizard_preflight,
        "run_preflight",
        lambda request, user, store: SimpleNamespace(
            status="failed",
            checks=[SimpleNamespace(key="budget", status="failed", message=SPENT)],
            workflow_result={},
            terminal=None,
        ),
    )
    with _client() as client:
        body = client.post("/optimizations/contract-check", json={"spec": _spec(), "locale": "en"}).json()

    [refusal] = [item for item in body["checks"] if item["key"] == "workspace.run"]
    assert refusal["message_code"] == "submit.unified.contract_budget"
    assert refusal["message"] == t_locale("en", "submit.unified.contract_budget", error=SPENT)


def test_stream_route_streams_the_live_transcript_and_returns_it_with_the_result(
    calls: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each transcript event reaches the viewer as a ``terminal`` event before the result carries the whole log."""

    def run(request: Any, user: Any, store: Any) -> Any:
        """Record a note on the check's live transcript, then answer with it stored."""
        recorder = check_terminal.current_recorder()
        assert recorder is not None
        recorder.note("box_ready", seconds=1.5)
        return SimpleNamespace(
            status="succeeded",
            checks=[],
            terminal=recorder.snapshot(),
            workflow_result={
                "contract": {"ok": True, "optimizer_resolved": "best_of_n", "checks": [], "fingerprint": "f"}
            },
        )

    monkeypatch.setattr(wizard_preflight, "run_preflight", run)
    with _client() as client:
        body = client.post("/optimizations/contract-check/stream", json={"spec": _spec()}).text

    terminal_at = body.index("event: terminal\n")
    assert terminal_at < body.index("event: result\n")
    assert '"note": "box_ready"' in body
    result = json.loads(body.split("event: result\ndata: ", 1)[1].split("\n", 1)[0])
    assert result["terminal"]["events"][0]["note"] == "box_ready"


def test_preflight_records_onto_the_streaming_routes_empty_recorder(monkeypatch: pytest.MonkeyPatch) -> None:
    """A unified check keeps the route's still-empty live recorder instead of starting a silent one."""
    seen: list[Any] = []
    monkeypatch.setattr(
        preflight_execution, "_run_preflight", lambda request, user, job_store, terminal: seen.append(terminal)
    )
    live: list[dict[str, Any]] = []
    recorder = check_terminal.CheckTerminalRecorder(emit=live.append)
    request = preflight_execution.WizardPreflightRequest(
        scope="evaluation", workflow="unified", payload={}, execution_budget_id="b", execution_budget_revision=1
    )

    with check_terminal.recording(recorder):
        preflight_execution.run_preflight(request, SimpleNamespace(username="u"), job_store=None)
    seen[0].note("box_ready", seconds=1.0)

    assert seen == [recorder]
    assert [event["note"] for event in live] == ["box_ready"]
