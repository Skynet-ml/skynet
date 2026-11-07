"""Tests for the caller-scoped metric-hardening endpoints and their contract shape."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ...config import settings
from ...i18n_en import ENGLISH_MESSAGES
from ...models.hardening import (
    HardeningExploit,
    HardeningFix,
    HardeningFixChecks,
    HardeningJob,
    HardeningJobModel,
    HardeningRound,
)
from ...storage.models import Base
from .. import model_catalog
from ..auth import AuthenticatedUser, get_authenticated_user
from ..model_catalog import CatalogModel, ModelCatalogResponse
from ..routers.hardening import create_hardening_router

_JOB_FIELDS = {
    "id",
    "status",
    "round",
    "rounds",
    "round_cap_usd",
    "reserved_usd",
    "spent_usd",
    "place",
    "attacker_model",
    "judge_model",
    "view",
    "preflight_failures",
    "rounds_detail",
    "suite_count",
    "fixed_count",
    "error",
}
_EXPLOIT_FIELDS = {"id", "kind", "label", "confidence", "reason", "unsure", "patch_diff", "why_md", "user_verdict"}
_BODY = {"draft_id": "draft-1", "attacker_model": "openai/attacker", "judge_model": "openai/judge"}


@pytest.fixture(autouse=True)
def _catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve a catalog that lists the test attacker and judge models.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """
    models = [
        CatalogModel(value=value, label=value, provider="openai", available=True)
        for value in (_BODY["attacker_model"], _BODY["judge_model"])
    ]
    monkeypatch.setattr(model_catalog, "get_catalog_cached", lambda: ModelCatalogResponse(providers=[], models=models))


def _harness() -> tuple[TestClient, Engine, dict[str, str]]:
    """Build a hardening client over an in-memory database.

    Returns:
        The client, the engine, and a holder whose ``user`` names the caller.
    """
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    app = FastAPI()
    app.include_router(create_hardening_router(job_store=SimpleNamespace(engine=engine)))
    caller = {"user": "alice"}
    app.dependency_overrides[get_authenticated_user] = lambda: AuthenticatedUser(
        username=caller["user"], role="user", groups=()
    )
    return TestClient(app), engine, caller


def _exploit(exploit_id: str, verdict: Any = None) -> HardeningExploit:
    """Build a judged exploit.

    Args:
        exploit_id: The id.
        verdict: The user's verdict.

    Returns:
        The exploit.
    """
    return HardeningExploit(
        id=exploit_id,
        kind="case",
        label="exploit",
        confidence=0.9,
        reason="Games the keyword count.",
        unsure=False,
        patch_diff="+A A A",
        why_md="Repeats the keyword.",
        user_verdict=verdict,
    )


def _seed(
    engine: Engine, job: HardeningJob, *, username: str = "alice", draft_id: str = "draft-1", age: int = 0
) -> None:
    """Store a job as the worker would leave it.

    Args:
        engine: The engine.
        job: The job.
        username: The owner.
        draft_id: The setup draft.
        age: How many minutes ago it was created.
    """
    created = datetime.now(UTC) - timedelta(minutes=age)
    with Session(engine) as session:
        session.add(
            HardeningJobModel(
                id=job.id,
                username=username,
                draft_id=draft_id,
                status=job.status,
                job=job.model_dump(mode="json"),
                created_at=created,
                updated_at=created,
            )
        )
        session.commit()


def _job(job_id: str, status: str, **fields: Any) -> HardeningJob:
    """Build a cloud job in a given state.

    Args:
        job_id: The id.
        status: The status.
        **fields: Other fields.

    Returns:
        The job.
    """
    base: dict[str, Any] = {
        "id": job_id,
        "status": status,
        "round": 1,
        "rounds": 5,
        "round_cap_usd": 2.0,
        "reserved_usd": 10.0,
        "spent_usd": 0.7,
        "place": "cloud",
        "attacker_model": "openai/attacker",
        "judge_model": "openai/judge",
        "view": "black_box",
    }
    base.update(fields)
    return HardeningJob(**base)


def test_every_key_has_english_copy() -> None:
    """Each error the router raises renders English text."""
    for key in (
        "hardening.job_not_found",
        "hardening.exploit_not_found",
        "hardening.wrong_state",
        "hardening.job_active",
        "hardening.fix_checks_failed",
    ):
        assert key in ENGLISH_MESSAGES


def test_create_uses_defaults_and_returns_the_contract_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """A new cloud job has 5 rounds at $2, reserves $10 and waits for its preflight."""
    monkeypatch.setattr(settings, "local_mode", False)
    client, _engine, _caller = _harness()
    response = client.post("/hardening/jobs", json=_BODY)

    assert response.status_code == 201
    body = response.json()
    assert set(body) == _JOB_FIELDS
    assert body["status"] == "preflight"
    assert (body["rounds"], body["round_cap_usd"], body["reserved_usd"], body["spent_usd"]) == (5, 2.0, 10.0, 0.0)
    assert (body["place"], body["view"], body["round"]) == ("cloud", "black_box", 0)
    assert body["preflight_failures"] == []
    assert body["rounds_detail"] == []
    assert body["error"] is None
    assert client.get(f"/hardening/jobs/{body['id']}").json() == body


def test_create_stores_white_box_and_runs_free_locally(monkeypatch: pytest.MonkeyPatch) -> None:
    """The job keeps the chosen view; the Mac app reserves nothing."""
    monkeypatch.setattr(settings, "local_mode", True)
    client, _engine, _caller = _harness()
    body = client.post("/hardening/jobs", json={**_BODY, "view": "white_box", "rounds": 3, "round_cap_usd": 1.5}).json()

    assert (body["view"], body["place"], body["rounds"], body["round_cap_usd"], body["reserved_usd"]) == (
        "white_box",
        "local",
        3,
        1.5,
        0.0,
    )


@pytest.mark.parametrize(
    "patch",
    [{"view": "grey_box"}, {"rounds": 0}, {"round_cap_usd": 0}, {"draft_id": ""}, {"attacker_model": ""}],
)
def test_create_refuses_bad_input(patch: dict[str, Any]) -> None:
    """Out-of-contract input fails validation."""
    client, _engine, _caller = _harness()
    assert client.post("/hardening/jobs", json={**_BODY, **patch}).status_code == 422


@pytest.mark.parametrize("field", ["attacker_model", "judge_model"])
def test_create_refuses_an_unknown_model(field: str) -> None:
    """A model outside the catalog gets a 422 and no job; the router never swaps it."""
    client, engine, _caller = _harness()
    response = client.post("/hardening/jobs", json={**_BODY, field: "openai/not-a-model"})

    assert response.status_code == 422
    assert response.json()["detail"] == "models.unknown_model"
    with Session(engine) as session:
        assert session.query(HardeningJobModel).count() == 0


def test_judge_may_use_a_classifier_only_model() -> None:
    """The judge role also takes the classifier-only models; the attacker role does not."""
    client, _engine, _caller = _harness()
    classifier = next(iter(model_catalog.CLASSIFIER_ONLY_MODELS))

    judged = client.post("/hardening/jobs", json={**_BODY, "judge_model": classifier})
    attacked = client.post("/hardening/jobs", json={**_BODY, "draft_id": "draft-2", "attacker_model": classifier})

    assert judged.status_code == 201
    assert judged.json()["judge_model"] == classifier
    assert attacked.status_code == 422


def test_local_mode_takes_a_mapped_subscription_model_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """On the Mac app a subscription model passes when its CLI can run it, and gets a 422 otherwise."""
    monkeypatch.setattr(settings, "local_mode", True)
    client, _engine, _caller = _harness()
    mapped = "subscription/codex/openai/gpt-6.1-sol"

    accepted = client.post("/hardening/jobs", json={**_BODY, "attacker_model": mapped})
    refused = client.post(
        "/hardening/jobs", json={**_BODY, "draft_id": "draft-2", "attacker_model": "subscription/codex/openai/nope"}
    )

    assert accepted.status_code == 201
    assert accepted.json()["attacker_model"] == mapped
    assert refused.status_code == 422


def test_the_database_keeps_one_unfinished_job_per_draft() -> None:
    """The partial unique index refuses a second unfinished job even past the router's SELECT."""
    _client, engine, _caller = _harness()
    _seed(engine, _job("first", "running"))
    _seed(engine, _job("ended", "clean_stop"))
    with pytest.raises(IntegrityError):
        _seed(engine, _job("second", "waiting_review"))


def test_create_maps_a_lost_race_to_409(monkeypatch: pytest.MonkeyPatch) -> None:
    """When a parallel create wins the race, the loser gets the job_active 409, not a 500."""
    client, engine, _caller = _harness()
    original = Session.scalars

    def _no_active(self: Session, statement: Any, *args: Any, **kwargs: Any) -> Any:
        """Seed the winner, then hide it from the router's active-job SELECT.

        Args:
            self: The session.
            statement: The query.
            *args: Other positional arguments.
            **kwargs: Other keyword arguments.

        Returns:
            The query result, empty for the active-job check.
        """
        if "hardening_jobs.status IN" in str(statement):
            monkeypatch.setattr(Session, "scalars", original)
            _seed(engine, _job("winner", "preflight"))
            return original(self, statement.where(HardeningJobModel.id == "none"), *args, **kwargs)
        return original(self, statement, *args, **kwargs)

    monkeypatch.setattr(Session, "scalars", _no_active)
    response = client.post("/hardening/jobs", json=_BODY)

    assert response.status_code == 409
    assert response.json()["detail"] == ENGLISH_MESSAGES["hardening.job_active"]


def test_one_unfinished_job_per_draft() -> None:
    """A draft with an unfinished job refuses another; a finished one allows it."""
    client, engine, _caller = _harness()
    first = client.post("/hardening/jobs", json=_BODY)
    second = client.post("/hardening/jobs", json=_BODY)

    assert second.status_code == 409
    assert second.json()["detail"] == ENGLISH_MESSAGES["hardening.job_active"]
    assert client.post("/hardening/jobs", json={**_BODY, "draft_id": "draft-2"}).status_code == 201
    with Session(engine) as session:
        row = session.get(HardeningJobModel, first.json()["id"])
        assert row is not None
        row.status = "clean_stop"
        session.commit()
    assert client.post("/hardening/jobs", json=_BODY).status_code == 201


def test_latest_returns_the_newest_job_of_the_draft_or_null() -> None:
    """GET with draft_id returns the newest job of that draft, and null when there is none."""
    client, engine, _caller = _harness()
    _seed(engine, _job("old", "clean_stop"), age=10)
    _seed(engine, _job("new", "budget_stop"), age=1)
    _seed(engine, _job("other", "done"), draft_id="draft-2")

    assert client.get("/hardening/jobs", params={"draft_id": "draft-1"}).json()["id"] == "new"
    response = client.get("/hardening/jobs", params={"draft_id": "missing"})
    assert response.status_code == 200
    assert response.json() is None
    assert client.get("/hardening/jobs").status_code == 422


def test_jobs_are_owner_scoped() -> None:
    """Another user cannot read, list or change the caller's job."""
    client, engine, caller = _harness()
    _seed(
        engine,
        _job(
            "j1",
            "waiting_review",
            rounds_detail=[HardeningRound(index=1, status="waiting_review", exploits=[_exploit("e1")])],
        ),
    )
    caller["user"] = "mallory"

    response = client.get("/hardening/jobs/j1")
    assert response.status_code == 404
    assert response.json()["detail"] == ENGLISH_MESSAGES["hardening.job_not_found"]
    assert client.get("/hardening/jobs", params={"draft_id": "draft-1"}).json() is None
    assert client.post("/hardening/jobs/j1/exploits/e1/verdict", json={"confirmed": True}).status_code == 404
    assert client.post("/hardening/jobs/j1/fix", json={"approve": True}).status_code == 404
    assert client.post("/hardening/jobs/j1/retry").status_code == 404


def test_verdicts_move_the_job_to_the_fix() -> None:
    """When every exploit has a verdict and one is confirmed, the worker drafts the fix."""
    client, engine, _caller = _harness()
    round_one = HardeningRound(index=1, status="waiting_review", exploits=[_exploit("e1"), _exploit("e2")])
    _seed(engine, _job("j1", "waiting_review", rounds_detail=[round_one]))

    first = client.post("/hardening/jobs/j1/exploits/e1/verdict", json={"confirmed": True}).json()
    assert first["status"] == "waiting_review"
    exploit = first["rounds_detail"][0]["exploits"][0]
    assert set(exploit) == _EXPLOIT_FIELDS
    assert exploit["user_verdict"] == "confirmed"

    second = client.post("/hardening/jobs/j1/exploits/e2/verdict", json={"confirmed": False}).json()
    assert second["status"] == "running"
    assert second["rounds_detail"][0]["status"] == "running"
    assert client.get("/hardening/jobs/j1").json() == second


def test_verdict_errors() -> None:
    """An unknown exploit is 404; a job that does not wait for review is 409."""
    client, engine, _caller = _harness()
    _seed(
        engine,
        _job(
            "j1",
            "waiting_review",
            rounds_detail=[HardeningRound(index=1, status="waiting_review", exploits=[_exploit("e1")])],
        ),
    )
    _seed(engine, _job("j2", "running"), draft_id="draft-2")

    missing = client.post("/hardening/jobs/j1/exploits/nope/verdict", json={"confirmed": True})
    assert missing.status_code == 404
    assert missing.json()["detail"] == ENGLISH_MESSAGES["hardening.exploit_not_found"]
    wrong = client.post("/hardening/jobs/j2/exploits/e1/verdict", json={"confirmed": True})
    assert wrong.status_code == 409
    assert wrong.json()["detail"] == ENGLISH_MESSAGES["hardening.wrong_state"]
    assert client.post("/hardening/jobs/j1/exploits/e1/verdict", json={}).status_code == 422


def _waiting_fix(job_id: str, *, suite_replay: str = "pass") -> HardeningJob:
    """Build a job whose round-1 fix waits for the user.

    Args:
        job_id: The id.
        suite_replay: The suite replay check result.

    Returns:
        The job.
    """
    fix = HardeningFix(
        diff="--- a/metric.py\n+++ b/metric.py\n",
        checks=HardeningFixChecks(suite_replay=suite_replay, base_score_unchanged="pass"),
    )
    round_one = HardeningRound(index=1, status="waiting_fix", exploits=[_exploit("e1", "confirmed")], fix=fix)
    return _job(job_id, "waiting_fix", rounds_detail=[round_one])


def test_fix_contract_and_approval() -> None:
    """The fix carries its checks, with a null reference check; approval resumes the job."""
    client, engine, _caller = _harness()
    _seed(engine, _waiting_fix("j1"))

    fix = client.get("/hardening/jobs/j1").json()["rounds_detail"][0]["fix"]
    assert fix == {
        "diff": "--- a/metric.py\n+++ b/metric.py\n",
        "checks": {"suite_replay": "pass", "base_score_unchanged": "pass", "reference_not_lower": None},
        "user_decision": None,
    }
    approved = client.post("/hardening/jobs/j1/fix", json={"approve": True}).json()
    assert approved["status"] == "running"
    assert approved["rounds_detail"][0]["status"] == "approved"
    assert approved["rounds_detail"][0]["fix"]["user_decision"] == "approved"


def test_failed_check_blocks_approval_and_reject_then_retry() -> None:
    """A failed check refuses approval; reject ends the job; retry drafts the fix again."""
    client, engine, _caller = _harness()
    _seed(engine, _waiting_fix("j1", suite_replay="fail"))

    blocked = client.post("/hardening/jobs/j1/fix", json={"approve": True})
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == ENGLISH_MESSAGES["hardening.fix_checks_failed"]

    rejected = client.post("/hardening/jobs/j1/fix", json={"approve": False}).json()
    assert rejected["status"] == "rejected"
    assert rejected["reserved_usd"] == rejected["spent_usd"]

    retried = client.post("/hardening/jobs/j1/retry").json()
    assert retried["status"] == "running"
    assert retried["reserved_usd"] == 10.0
    assert retried["rounds_detail"][0]["status"] == "running"
    assert retried["rounds_detail"][0]["fix"] is None
    assert client.post("/hardening/jobs/j1/fix", json={"approve": True}).status_code == 409


def test_retry_refuses_an_active_job_and_reruns_a_failed_preflight() -> None:
    """Retry is 409 on an active job and sends a failed preflight back to preflight."""
    client, engine, _caller = _harness()
    _seed(engine, _job("j1", "running"))
    _seed(engine, _job("j2", "preflight_failed", preflight_failures=["metric.missing"]), draft_id="draft-2")

    assert client.post("/hardening/jobs/j1/retry").status_code == 409
    body = client.post("/hardening/jobs/j2/retry").json()
    assert (body["status"], body["preflight_failures"]) == ("preflight", [])
    with Session(engine) as session:
        row = session.get(HardeningJobModel, "j2")
        assert row is not None
        assert row.status == "preflight"
