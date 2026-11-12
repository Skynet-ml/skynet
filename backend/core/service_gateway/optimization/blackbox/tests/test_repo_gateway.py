"""Tests for model calls from a repository's scoring box through the trusted parent's gateway.

The box runs through the production path: the run's sandbox broker opens the
parent's box, and every command in it reaches models only through the model
mailbox, so the box needs no network and never holds a provider key.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from core.billing.budgets import BudgetService
from core.billing.model_gateway import ROUTE_KEY, ModelGateway
from core.billing.model_mailbox import ModelMailbox
from core.billing.runtime import BudgetRuntime
from core.config import settings
from core.exceptions import ServiceError
from core.storage.models import Base, BillingCustomerModel
from core.worker import repo_staging

from ..preflight import verify_anything_in_sandbox
from ..repo_scorer import RepoScorer
from ..repo_workspace import RepoWorkspace
from ..sandbox import LocalSubprocessRuntime, LocalSubprocessSession, SandboxSpec
from ..sandbox_broker import SandboxBroker
from .test_repo_workspace import _archive

IMAGE = "fixture@sha256:" + "a" * 64
MANAGED_KEY = "upstream-secret"
BYOK_KEY = "private-provider-key"
CATALOG = {
    "id": "fixture/text",
    "endpoints": [
        {
            "tag": "fixture",
            "provider_name": "Fixture",
            "context_length": 1000,
            "max_completion_tokens": 1000,
            "pricing": {"prompt": "0.00001", "completion": "0.00002"},
        }
    ],
}
# The version's own code calls the route the way any OpenAI-compatible SDK would.
APP = (
    """
import json, os, urllib.request

def ask(text):
    body = json.dumps({
        "model": os.environ["APP_MODEL"],
        "max_tokens": 16,
        "messages": [{"role": "user", "content": text}],
    }).encode()
    request = urllib.request.Request(
        os.environ["OPENAI_BASE_URL"] + "/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + os.environ["APP_API_KEY"]},
    )
    with urllib.request.urlopen(request) as response:
        return json.load(response)["choices"][0]["message"]["content"]
"""
    f"""

def run(value):
    leaked = any(key in item for item in os.environ.values() for key in ({MANAGED_KEY!r}, {BYOK_KEY!r}))
    return {{"direct": ask("candidate prompt"), "leaked": leaked, "has_key": bool(os.environ.get("APP_API_KEY"))}}
"""
)
# The metric box holds no repository file, so the version's own call runs
# in the candidate box through the [run] entry, and the metric gets its output (SKY-398).
SCORER = f"""
import os

def score(repo_path, case, output):
    direct = output["direct"]
    judged = llm("judge prompt")
    leaked = output["leaked"] or any(
        key in value for value in os.environ.values() for key in ({MANAGED_KEY!r}, {BYOK_KEY!r})
    )
    return {{
        "score": 1.0 if direct == "OK" and judged == "OK" else 0.0,
        "feedback": direct + " " + judged,
        "secret": os.environ.get("API_KEY"),
        "leaked": leaked,
        "has_key": output["has_key"] and bool(os.environ.get("APP_API_KEY")),
    }}
"""


@pytest.fixture(autouse=True)
def root_commands_in_local_boxes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Drop the scorer's root flag in a local box, which refuses root on the developer's machine.

    The supervisor then runs as the test's user and starts the entry as that
    user. The Docker tests prove the root supervisor and the user switch (SKY-398).

    Args:
        monkeypatch: Restores the local box's run after the test.

    Returns:
        The commands that asked for root, in order.
    """
    run = LocalSubprocessSession.run
    root_commands: list[str] = []

    def without_root(self: LocalSubprocessSession, command: str, *, sudo: bool = False, **options: Any) -> Any:
        """Run the command as the test's user, and record a root request.

        Args:
            self: The local box.
            command: The shell command.
            sudo: Recorded, then ignored.
            **options: The run's other arguments.

        Returns:
            The command's result.
        """
        if sudo:
            root_commands.append(command)
        return run(self, command, **options)

    monkeypatch.setattr(LocalSubprocessSession, "run", without_root)
    return root_commands


@pytest.fixture(autouse=True)
def _at_cost_markup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the usage markup so a metered call has a known cost.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setattr(settings, "usage_markup", 1.0)


@pytest.fixture
def gateway(tmp_path: Path) -> Iterator[tuple[ModelGateway, list[httpx.Request]]]:
    """Start the real parent gateway on a funded wallet, a fake provider and a parent box broker.

    Args:
        tmp_path: Scratch folder.

    Yields:
        The gateway and the requests the provider received.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'gateway.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(username="alice", stripe_customer_id="fixture", balance_cents=50, grant_remaining=0)
        )
        session.commit()
    ledger = BudgetService(engine=engine)
    budget = ledger.create("alice", 20, idempotency_key="fixture")
    model_gateway = ModelGateway(
        BudgetRuntime(ledger, username="alice", budget_id=budget.id, generation=0, phase="setup")
    )
    model_gateway._client.close()
    sent: list[httpx.Request] = []

    def provider(request: httpx.Request) -> httpx.Response:
        """Return a priced catalog and a measured completion.

        Args:
            request: The upstream request.

        Returns:
            The provider's reply.
        """
        if request.method == "GET":
            return httpx.Response(200, json={"data": CATALOG})
        sent.append(request)
        return httpx.Response(
            200,
            json={
                "id": f"gen-{len(sent)}",
                "usage": {"cost": "0.001", "prompt_tokens": 3, "completion_tokens": 1},
                "choices": [{"message": {"role": "assistant", "content": "OK"}}],
            },
        )

    model_gateway._client = httpx.Client(transport=httpx.MockTransport(provider), follow_redirects=True)
    model_gateway.bind_sandbox(
        SandboxBroker(
            LocalSubprocessRuntime(),
            image=IMAGE,
            max_lifetime_seconds=120,
            command_runner=ModelMailbox(model_gateway.dispatch_guest).run,
            parent_command_runner=ModelMailbox(model_gateway.dispatch_route).run,
        ),
        image=IMAGE,
        lifetime_seconds=120,
    )
    model_gateway.fund_parent_sandbox(())
    yield model_gateway, sent
    model_gateway.close()
    engine.dispose()


def _score(
    model_gateway: ModelGateway,
    tmp_path: Path,
    model: dict[str, Any],
    managed_key: str,
    metric_code: str = SCORER,
    run_entry: str | None = "src/app.py:run",
) -> dict[str, Any]:
    """Scope a repository run's scorer model, then score its baseline in the parent's boxes.

    Args:
        model_gateway: The run's gateway.
        tmp_path: Scratch folder.
        model: The scorer's model configuration.
        managed_key: The platform's provider key.
        metric_code: The scorer the metric box runs.
        run_entry: The ``[run]`` entry the candidate box runs, if any.

    Returns:
        The protected payload's scorer and the scored document.
    """
    protected = model_gateway.protect_payload(
        {"scorer": {"kind": "python", "metric_code": metric_code, "model": model}}, managed_key=managed_key
    )
    shipped = json.dumps({**protected, "scorer": {**protected["scorer"], "metric_code": ""}})
    assert MANAGED_KEY not in shipped
    assert BYOK_KEY not in shipped
    spec = SandboxSpec(lifetime_seconds=120, image=IMAGE, network_disabled=True, operation_key="repo-scorer:job")
    scorer = RepoScorer(
        RepoWorkspace(
            runtime=model_gateway.parent_sandbox_runtime(),
            spec=spec,
            archive=_archive(tmp_path, {"src/app.py": APP}),
            editable_paths=["src"],
            readonly_paths=[],
            setup_command=None,
            secrets={"API_KEY": "run-secret-value"},
        ),
        metric_code,
        timeout_seconds=60,
        gateway=repo_staging._repo_model_gateway(protected["scorer"]),
        run_entry=run_entry,
    )
    try:
        return {"scorer": protected["scorer"], "document": scorer.score("")}
    finally:
        scorer.close()


def test_a_managed_call_from_the_box_is_metered(
    gateway: tuple[ModelGateway, list[httpx.Request]], tmp_path: Path, root_commands_in_local_boxes: list[str]
) -> None:
    """Version code and the scorer's llm() reach the managed route; the parent pays and meters both."""
    model_gateway, sent = gateway
    result = _score(model_gateway, tmp_path, {"name": "fixture/text", "max_tokens": 16}, MANAGED_KEY)
    # The [run] supervisor runs as root, and the mailbox still wraps it, so the entry keeps its model route.
    supervisors = [command for command in root_commands_in_local_boxes if "--candidate" in command]
    assert supervisors
    assert all(command.startswith("python3 -I .app-model-") for command in supervisors)

    document = result["document"]
    assert document["score"] == 1.0
    assert document["leaked"] is False
    assert document["has_key"] is True
    # No run secret enters the box (E38); the parent keeps it for redaction.
    assert document["secret"] is None
    assert len(sent) == 2
    assert all(request.headers["authorization"] == f"Bearer {MANAGED_KEY}" for request in sent)
    route = result["scorer"]["model"]["extra"][ROUTE_KEY]
    assert model_gateway._routes[route["token"]].policy.kind == "managed_model"
    spent = model_gateway.runtime.service.get(model_gateway.runtime.budget_id, "alice").setup_spent_cents
    assert spent == Decimal("0.2")


def test_a_byok_call_from_the_box_uses_the_users_key_in_the_parent(
    gateway: tuple[ModelGateway, list[httpx.Request]], tmp_path: Path
) -> None:
    """A BYOK route sends the user's key upstream from the parent; the box never holds it."""
    model_gateway, sent = gateway
    model = {
        "name": "openrouter/fixture/text",
        "token_source": "byok",
        "byok_provider": "openrouter",
        "max_tokens": 16,
        "extra": {"api_key": BYOK_KEY},
    }
    result = _score(model_gateway, tmp_path, model, "")

    document = result["document"]
    assert document["score"] == 1.0
    assert document["leaked"] is False
    assert len(sent) == 2
    assert all(request.headers["authorization"] == f"Bearer {BYOK_KEY}" for request in sent)
    route = result["scorer"]["model"]["extra"][ROUTE_KEY]
    assert model_gateway._routes[route["token"]].policy.kind == "byok_model"


def test_a_scorer_model_without_a_scoped_route_is_refused() -> None:
    """A model the parent did not scope would hand the box a key or a network path, so binding fails."""
    with pytest.raises(ServiceError, match="scoped route"):
        repo_staging._repo_model_gateway(
            {"kind": "python", "model": {"name": "x/y", "token_source": "byok", "extra": {"api_key": BYOK_KEY}}}
        )
    assert repo_staging._repo_model_gateway({"kind": "python"}) is None


class _Evaluator:
    """Record any scoring request that reaches the parent's evaluator."""

    def __init__(self) -> None:
        """Start with no requests."""
        self.calls: list[Any] = []

    def dispatch(self, body: Any) -> Any:
        """Record a request; a box must never get here.

        Args:
            body: The posted candidate.

        Returns:
            Nothing useful, because reaching here fails the test.
        """
        self.calls.append(body)
        raise AssertionError("A parent box reached the evaluator.")


def test_a_box_cannot_reach_any_capability_beyond_its_model_route(
    gateway: tuple[ModelGateway, list[httpx.Request]], tmp_path: Path
) -> None:
    """The box's code is refused at the evaluator, budget and model paths, even with a leaked token, and never blocks."""
    model_gateway, sent = gateway
    evaluator = _Evaluator()
    evaluator_token = model_gateway.bind_evaluator(evaluator)["token"]
    probe = f"""
import json, os, urllib.error, urllib.request

def _status(path, token):
    request = urllib.request.Request(
        os.environ["OPENAI_BASE_URL"] + path,
        data=json.dumps({{"model": os.environ["APP_MODEL"], "messages": []}}).encode(),
        headers={{"Content-Type": "application/json", "Authorization": "Bearer " + token}},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code

def score(repo_path, case, output):
    route = os.environ["APP_API_KEY"]
    statuses = [
        _status("/_evaluator", route),
        _status("/_evaluator", {evaluator_token!r}),
        _status("/_budget/state", route),
        _status("/chat/completions", {evaluator_token!r}),
    ]
    return {{"score": 0.0, "feedback": json.dumps(statuses)}}
"""
    started = time.monotonic()
    result = _score(model_gateway, tmp_path, {"name": "fixture/text", "max_tokens": 16}, MANAGED_KEY, probe, None)

    assert time.monotonic() - started < 60
    statuses = json.loads(result["document"]["feedback"])
    assert len(statuses) == 4
    assert all(status >= 400 for status in statuses)
    assert evaluator.calls == []
    assert sent == []


def test_route_dispatch_refuses_every_non_route_token_and_capability_path(
    gateway: tuple[ModelGateway, list[httpx.Request]],
) -> None:
    """Only a registered model route token on a model path passes the parent box's dispatch."""
    model_gateway, _ = gateway
    evaluator_token = model_gateway.bind_evaluator(_Evaluator())["token"]
    protected = model_gateway.protect_payload(
        {"scorer": {"kind": "python", "metric_code": "", "model": {"name": "fixture/text"}}}, managed_key=MANAGED_KEY
    )
    route = protected["scorer"]["model"]["extra"][ROUTE_KEY]["token"]
    for token, path in (
        (evaluator_token, "/v1/_evaluator"),
        (evaluator_token, "/v1/chat/completions"),
        (route, "/v1/_evaluator"),
        (route, "/v1/_packages"),
        (route, "/v1/_mcp"),
        (route, "/v1/_budget/state"),
        ("unknown", "/v1/chat/completions"),
    ):
        with pytest.raises(ValueError, match="only its scoped model route"):
            model_gateway.dispatch_route(token, path, {}, {})


_READ_README = "def read(value):\n    with open('README') as handle:\n        return handle.read()\n"


def _repo_setup_check(
    model_gateway: ModelGateway,
    tmp_path: Path,
    metric_code: str,
    run_entry: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Run a repository setup check through a real parent ``RepoScorer`` on a checkout whose README is ``hello``.

    Args:
        model_gateway: The real parent gateway.
        tmp_path: Scratch folder for the checkout archive.
        metric_code: The repository's metric.
        run_entry: The ``[run]`` entry; ``run.py:read`` returns the README.
        **extra: Payload keys to add, such as ``seed_candidate``.

    Returns:
        The setup check's result.
    """
    scorer = RepoScorer(
        RepoWorkspace(
            runtime=model_gateway.parent_sandbox_runtime(),
            spec=SandboxSpec(lifetime_seconds=120, image=IMAGE, network_disabled=True, operation_key="repo-scorer:job"),
            archive=_archive(tmp_path, {"README": "hello", "run.py": _READ_README}),
            editable_paths=["README"],
            readonly_paths=[],
            setup_command=None,
            secrets={},
        ),
        metric_code,
        timeout_seconds=60,
        run_entry=run_entry,
    )
    payload = {
        "scorer": {"kind": "python", "metric_code": metric_code},
        "target": {"kind": "repo", "repo": {"repository": "octo/hello", "editable_paths": ["README"]}},
        "cases": [{"input": "x"}],
        "_skynet_evaluator_route": model_gateway.bind_evaluator(scorer),
        **extra,
    }
    try:
        return verify_anything_in_sandbox(
            payload, scope="evaluation", identity="repo-check", runtime=LocalSubprocessRuntime()
        )
    finally:
        scorer.close()


def test_the_setup_check_scores_the_checkout_the_run_scores(
    gateway: tuple[ModelGateway, list[httpx.Request]], tmp_path: Path
) -> None:
    """The setup check scores through the parent's repository scorer, so the metric gets the real checkout's output (SKY-644).

    The metric box holds no repository file (SKY-398), so the ``[run]``
    entry reads the checkout and the metric scores its output.
    """
    metric_code = (
        "def score(repo_path, case, output):\n"
        "    return {'score': 1.0 if output == 'hello' else 0.0, 'feedback': str(output)}\n"
    )

    result = _repo_setup_check(gateway[0], tmp_path, metric_code, run_entry="run.py:read", seed_candidate="")

    assert result["checks"] == [{"key": "scorer", "status": "succeeded", "field": "scorer"}]
    assert result["scorer_result"]["score"] == 1.0


def test_a_repository_metric_that_raises_fails_the_setup_check_without_a_seed(
    gateway: tuple[ModelGateway, list[httpx.Request]], tmp_path: Path
) -> None:
    """A repository has no seed, so the check scores the checkout itself and a metric that raises fails it (SKY-644)."""
    metric_code = (
        "from pathlib import Path\n"
        "def score(repo_path, case):\n"
        "    return {'score': float(Path(repo_path, 'MISSING').read_text()), 'feedback': ''}\n"
    )

    result = _repo_setup_check(gateway[0], tmp_path, metric_code)

    [check] = result["checks"]
    assert check["key"] == "scorer"
    assert check["status"] == "failed"
    assert "The scorer raised an error" in check["message"]
    assert "evaluator_readiness" not in result
