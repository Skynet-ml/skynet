"""Test the chat's three approval choices, decline notes and the Jev auto-review (SKY-706).

Pins the chat-rule bounds (one exact tool, one user, one chat, never the
always-confirm or spend tools), the decline note that reaches the agent, the
extras that cross replicas, and the auto-review's allow, deny, error fallback,
wallet reservation and redaction.
"""

from __future__ import annotations

import asyncio
import time
from functools import partial
from typing import Any

import dspy
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ...billing.budgets import BudgetError
from ...billing.tests.test_protected_dispatch import CATALOG
from ...config import settings
from ...service_gateway.agents import approval_choices
from ...service_gateway.agents import auto_review as review_mod
from ...service_gateway.agents.approval_choices import (
    DECLINE_NOTE_MAX_CHARS,
    post_approval_choice,
    take_approval_choice,
)
from ...service_gateway.agents.auto_review import (
    AutoReviewer,
    AutoReviewVerdict,
    parse_review_reply,
    send_protected_review,
)
from ...service_gateway.agents.generalist import (
    _ALWAYS_CONFIRM_TOOLS,
    _NO_CHAT_RULE_TOOLS,
    CHAT_RULES_MAX_TOOLS,
    ApprovalRegistry,
    _TurnAuthoringFlag,
    _wrap_tool_with_approval,
    approval_key,
    chat_rule_allowed,
    chat_rules,
)
from ...service_gateway.agents.steering import SteerStore
from ...service_gateway.optimization.blackbox.classification import ClassificationError
from ...storage.models import (
    AgentSteerMessageModel,
    AgentSteerTurnModel,
    Base,
    BillingCustomerModel,
    ExecutionBudgetModel,
    ExecutionOperationModel,
)
from ..routers import generalist_agent as agent_mod
from ..routers.generalist_agent import ConfirmApprovalRequest, GeneralistAgentRequest
from .conftest import TEST_USER, bypass_auth

_SPEND_TOOLS = (
    "submit_unified_run_optimize_post",
    "clone_job_optimizations",
    "retry_job_optimizations",
    "resume_job_optimizations",
    "restart_job_optimizations",
)


@pytest.fixture(autouse=True)
def _fresh_steer_store(monkeypatch: pytest.MonkeyPatch) -> SteerStore:
    """Give each test its own in-process steer store.

    Args:
        monkeypatch: Replaces the shared store.

    Returns:
        The isolated store.
    """
    store = SteerStore()
    monkeypatch.setattr(approval_choices, "get_steer_store", lambda: store)
    return store


def _tool(name: str, calls: list[dict[str, Any]] | None = None) -> dspy.Tool:
    """Build a tool that records its calls and returns ``"ran"``.

    Args:
        name: The tool name.
        calls: Receives the kwargs of each call.

    Returns:
        The tool.
    """

    async def func(**kwargs: Any) -> str:
        """Record the call.

        Args:
            **kwargs: The call's arguments.

        Returns:
            ``"ran"``.
        """
        if calls is not None:
            calls.append(kwargs)
        return "ran"

    return dspy.Tool(func=func, name=name, desc="test tool", args={}, arg_types={}, arg_desc={})


def _wrap(
    name: str,
    events: list[dict[str, Any]],
    registry: ApprovalRegistry,
    flag: _TurnAuthoringFlag,
    *,
    trust_mode: str = "ask",
    owner: str | None = "alice",
    calls: list[dict[str, Any]] | None = None,
) -> dspy.Tool:
    """Wrap a recording tool with the approval gate.

    Args:
        name: The tool name.
        events: Receives the emitted events.
        registry: The approval registry.
        flag: The turn state shared by the turn's wrappers.
        trust_mode: The trust mode.
        owner: The stream's owner.
        calls: Receives the kwargs of each call that ran.

    Returns:
        The wrapped tool.
    """
    return _wrap_tool_with_approval(
        _tool(name, calls),
        trust_mode=trust_mode,  # type: ignore[arg-type]
        registry=registry,
        emit=events.append,
        outer_loop=asyncio.get_running_loop(),
        authoring_flag=flag,
        approval_owner=owner,
    )


async def _pending(events: list[dict[str, Any]], count: int = 1) -> dict[str, Any]:
    """Wait for the ``count``-th ``pending_approval`` event.

    Args:
        events: The emitted events.
        count: Which pending event to wait for, counted from 1.

    Returns:
        The event's data.
    """
    for _ in range(200):
        pending = [e for e in events if e["event"] == "pending_approval"]
        if len(pending) >= count:
            return pending[count - 1]["data"]
        await asyncio.sleep(0.01)
    raise AssertionError("no pending_approval event")


def _resolved(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the data of every ``approval_resolved`` event.

    Args:
        events: The emitted events.

    Returns:
        The event data, in order.
    """
    return [e["data"] for e in events if e["event"] == "approval_resolved"]


@pytest.mark.parametrize("tool", sorted(_ALWAYS_CONFIRM_TOOLS | _NO_CHAT_RULE_TOOLS))
def test_no_chat_rule_covers_the_always_confirm_spend_or_settings_tools(tool: str) -> None:
    """The protected, spend and settings tools ask on every call.

    Args:
        tool: A tool a chat rule may never cover.
    """
    assert not chat_rule_allowed(tool)
    assert chat_rules([tool]) == set()


def test_every_spend_tool_is_excluded() -> None:
    """Each tool that spends the balance is in the exclusion set."""
    assert set(_SPEND_TOOLS) <= _NO_CHAT_RULE_TOOLS
    assert "change_setting" in _NO_CHAT_RULE_TOOLS


@pytest.mark.parametrize("tool", ["delete_job_optimizations", "rename_job_optimizations", "move_folder"])
def test_a_chat_rule_covers_an_exact_gated_tool(tool: str) -> None:
    """A tool that asks in Ask mode and spends nothing may get a chat rule.

    Args:
        tool: An eligible tool.
    """
    assert chat_rule_allowed(tool)


@pytest.mark.parametrize(
    "tool", ["", "list_jobs_optimizations", "delete_job_optimizations ", "DELETE_JOB_OPTIMIZATIONS", "delete_*", "*"]
)
def test_unknown_padded_or_pattern_names_get_no_rule(tool: str) -> None:
    """A rule names one exact gated tool; no pattern, case change or padding matches.

    Args:
        tool: A name that is not an exact gated tool.
    """
    assert chat_rules([tool]) == set()


def test_chat_rules_keep_a_bounded_list() -> None:
    """Only the first names up to the bound count."""
    names = ["move_folder"] * CHAT_RULES_MAX_TOOLS + ["delete_job_optimizations"]
    assert chat_rules(names) == {"move_folder"}


def test_the_turn_request_bounds_its_chat_rules() -> None:
    """The turn request refuses an over-long list or name."""
    too_many = ["move_folder"] * (CHAT_RULES_MAX_TOOLS + 1)
    with pytest.raises(ValidationError):
        GeneralistAgentRequest.model_validate({"user_message": "hi", "chat_allowed_tools": too_many})
    with pytest.raises(ValidationError):
        GeneralistAgentRequest.model_validate({"user_message": "hi", "chat_allowed_tools": ["x" * 129]})


def test_the_confirm_request_bounds_the_note_and_the_scope() -> None:
    """A note over 500 characters or an unknown scope is refused."""
    with pytest.raises(ValidationError):
        ConfirmApprovalRequest(call_id="c", approved=False, note="n" * (DECLINE_NOTE_MAX_CHARS + 1))
    with pytest.raises(ValidationError):
        ConfirmApprovalRequest.model_validate({"call_id": "c", "approved": True, "scope": "forever"})
    assert ConfirmApprovalRequest(call_id="c", approved=True).scope == "once"


async def test_a_chat_rule_skips_the_prompt_for_its_tool_only() -> None:
    """A rule for one tool lets it run; another gated tool still asks."""
    events: list[dict[str, Any]] = []
    registry = ApprovalRegistry()
    flag = _TurnAuthoringFlag()
    flag.chat_allowed = chat_rules(["delete_job_optimizations"])
    assert await _wrap("delete_job_optimizations", events, registry, flag).func._async_body() == "ran"
    assert not [e for e in events if e["event"] == "pending_approval"]

    call = asyncio.create_task(_wrap("move_folder", events, registry, flag).func._async_body())
    pending = await _pending(events)
    assert pending["tool"] == "move_folder"
    registry.resolve(approval_key(pending["id"], "alice"), True)
    assert await call == "ran"


@pytest.mark.parametrize("tool", sorted(_ALWAYS_CONFIRM_TOOLS - {"request_secret"}) + list(_SPEND_TOOLS))
async def test_a_forged_rule_never_skips_a_protected_or_spend_prompt(tool: str) -> None:
    """A name put into the turn's rules without the filter still asks.

    Args:
        tool: A tool a rule may never cover.
    """
    events: list[dict[str, Any]] = []
    registry = ApprovalRegistry()
    flag = _TurnAuthoringFlag()
    flag.chat_allowed = {tool}
    for mode in ("ask", "auto_safe"):
        call = asyncio.create_task(_wrap(tool, events, registry, flag, trust_mode=mode).func._async_body())
        pending = await _pending(events, 1 if mode == "ask" else 2)
        assert pending["chat_rule"] is False
        registry.resolve(approval_key(pending["id"], "alice"), False)
        assert await call == "User declined"


async def test_allow_for_this_chat_adds_the_rule_for_later_calls() -> None:
    """The second choice lets later calls of the same tool run in this turn."""
    events: list[dict[str, Any]] = []
    registry = ApprovalRegistry()
    flag = _TurnAuthoringFlag()
    tool = _wrap("delete_job_optimizations", events, registry, flag)
    call = asyncio.create_task(tool.func._async_body())
    pending = await _pending(events)
    assert pending["chat_rule"] is True
    post_approval_choice("alice", pending["id"], "conversation", None)
    registry.resolve(approval_key(pending["id"], "alice"), True)
    assert await call == "ran"
    assert _resolved(events)[-1]["scope"] == "conversation"
    assert flag.chat_allowed == {"delete_job_optimizations"}

    assert await tool.func._async_body() == "ran"
    assert len([e for e in events if e["event"] == "pending_approval"]) == 1


async def test_allow_for_this_chat_is_ignored_for_a_spend_tool() -> None:
    """A conversation scope on a spend tool stays a one-time approval."""
    events: list[dict[str, Any]] = []
    registry = ApprovalRegistry()
    flag = _TurnAuthoringFlag()
    call = asyncio.create_task(_wrap("submit_unified_run_optimize_post", events, registry, flag).func._async_body())
    pending = await _pending(events)
    post_approval_choice("alice", pending["id"], "conversation", None)
    registry.resolve(approval_key(pending["id"], "alice"), True)
    assert await call == "ran"
    assert _resolved(events)[-1]["scope"] == "once"
    assert flag.chat_allowed == set()


async def test_another_users_extras_never_reach_the_call() -> None:
    """Extras posted by another account are not read; only the owner's count."""
    events: list[dict[str, Any]] = []
    registry = ApprovalRegistry()
    flag = _TurnAuthoringFlag()
    call = asyncio.create_task(_wrap("delete_job_optimizations", events, registry, flag).func._async_body())
    pending = await _pending(events)
    post_approval_choice("mallory", pending["id"], "conversation", None)
    assert registry.resolve(approval_key(pending["id"], "mallory"), True) is False
    registry.resolve(approval_key(pending["id"], "alice"), True)
    assert await call == "ran"
    assert _resolved(events)[-1]["scope"] == "once"
    assert flag.chat_allowed == set()


async def test_a_rule_stays_in_its_own_chat() -> None:
    """A new turn state, as another chat gets, starts with no rules."""
    events: list[dict[str, Any]] = []
    registry = ApprovalRegistry()
    first = _TurnAuthoringFlag()
    call = asyncio.create_task(_wrap("delete_job_optimizations", events, registry, first).func._async_body())
    pending = await _pending(events)
    post_approval_choice("alice", pending["id"], "conversation", None)
    registry.resolve(approval_key(pending["id"], "alice"), True)
    await call

    other = _TurnAuthoringFlag()
    second = asyncio.create_task(_wrap("delete_job_optimizations", events, registry, other).func._async_body())
    pending = await _pending(events, 2)
    registry.resolve(approval_key(pending["id"], "alice"), False)
    assert await second == "User declined"


async def test_a_decline_note_reaches_the_agent_bounded_and_clean() -> None:
    """The note goes to the agent as quoted user text, clipped and without control characters."""
    events: list[dict[str, Any]] = []
    registry = ApprovalRegistry()
    calls: list[dict[str, Any]] = []
    flag = _TurnAuthoringFlag()
    call = asyncio.create_task(
        _wrap("delete_job_optimizations", events, registry, flag, calls=calls).func._async_body()
    )
    pending = await _pending(events)
    note = "Archive it instead\x00\x1b[31m" + "x" * 1000
    post_approval_choice("alice", pending["id"], "once", note)
    registry.resolve(approval_key(pending["id"], "alice"), False)
    result = await call
    assert calls == []
    assert result.startswith('User declined. The user said instead: "Archive it instead[31m')
    assert "\x00" not in result
    assert "\x1b" not in result
    shown = _resolved(events)[-1]["note"]
    assert len(shown) == DECLINE_NOTE_MAX_CHARS
    end = [e for e in events if e["event"] == "tool_end"][-1]["data"]
    assert end["status"] == "error"
    assert end["result"] == result


async def test_an_approval_drops_the_note() -> None:
    """A note sent with an approval does not reach the agent."""
    events: list[dict[str, Any]] = []
    registry = ApprovalRegistry()
    flag = _TurnAuthoringFlag()
    call = asyncio.create_task(_wrap("delete_job_optimizations", events, registry, flag).func._async_body())
    pending = await _pending(events)
    post_approval_choice("alice", pending["id"], "once", "ignored")
    registry.resolve(approval_key(pending["id"], "alice"), True)
    assert await call == "ran"
    assert "note" not in _resolved(events)[-1]


def test_the_extras_cross_replicas_through_the_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """A confirm on one replica stores the extras; the stream's replica reads them once.

    Args:
        monkeypatch: Points each side at its own store bound to one database.
    """
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    AgentSteerMessageModel.__table__.create(engine)
    AgentSteerTurnModel.__table__.create(engine)
    confirm_side, stream_side = SteerStore(), SteerStore()
    confirm_side.bind_engine(engine)
    stream_side.bind_engine(engine)
    monkeypatch.setattr(approval_choices, "get_steer_store", lambda: confirm_side)
    post_approval_choice("alice", "abc", "once", "Use the copy")
    monkeypatch.setattr(approval_choices, "get_steer_store", lambda: stream_side)
    assert take_approval_choice("bob", "abc").note == ""
    assert take_approval_choice("alice", "abc").note == "Use the copy"
    assert take_approval_choice("alice", "abc").note == ""


def test_the_confirm_route_stores_the_extras_before_the_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    """The route stores the note under the caller, then resolves the call.

    Args:
        monkeypatch: Replaces the approval registry.
    """
    seen: list[str] = []

    class _Registry:
        """Record what the steer store held when the decision landed."""

        def resolve_or_persist(self, key: str, approved: bool) -> bool:
            """Record the stored note and accept the decision.

            Args:
                key: The approval key.
                approved: The decision.

            Returns:
                True.
            """
            seen.append(take_approval_choice(TEST_USER.username, "call1").note)
            return True

    monkeypatch.setattr(agent_mod, "get_approval_registry", _Registry)
    app = FastAPI()
    app.include_router(agent_mod.create_generalist_agent_router())
    bypass_auth(app)
    response = TestClient(app).post(
        "/optimizations/generalist-agent/confirm",
        json={"call_id": "call1", "approved": False, "note": "Rename it instead"},
    )
    assert response.status_code == 200
    assert seen == ["Rename it instead"]


def _reviewer(reply: Any) -> tuple[AutoReviewer, list]:
    """Build a reviewer whose sender returns ``reply`` and records its prompts.

    Args:
        reply: The reply text, or an exception to raise.

    Returns:
        The reviewer and the list of prompts the sender got.
    """
    prompts: list = []

    def send(messages: list[dict[str, str]]) -> str:
        """Return the fixed reply.

        Args:
            messages: The prompt.

        Returns:
            The reply text.
        """
        prompts.append(messages)
        if isinstance(reply, Exception):
            raise reply
        return reply

    return AutoReviewer(send=send, request="Rename my latest run to Q3"), prompts


async def test_an_allow_lets_the_safe_mutation_run_without_a_prompt() -> None:
    """In auto-safe mode an allow runs the call with one review."""
    events: list[dict[str, Any]] = []
    flag = _TurnAuthoringFlag()
    flag.auto_review, prompts = _reviewer('{"label": "allow", "confidence": 0.9, "reason": "Asked for."}')
    tool = _wrap("rename_job_optimizations", events, ApprovalRegistry(), flag, trust_mode="auto_safe")
    assert await tool.func._async_body() == "ran"
    assert not [e for e in events if e["event"] == "pending_approval"]
    assert len(prompts) == 1


async def test_a_deny_stops_the_call_until_the_user_approves_anyway() -> None:
    """A deny shows the reason; "Approve anyway" runs the call through the normal confirm."""
    events: list[dict[str, Any]] = []
    registry = ApprovalRegistry()
    calls: list[dict[str, Any]] = []
    flag = _TurnAuthoringFlag()
    flag.auto_review, _ = _reviewer('{"label": "deny", "confidence": 0.8, "reason": "Not the run you named."}')
    tool = _wrap("rename_job_optimizations", events, registry, flag, trust_mode="auto_safe", calls=calls)
    call = asyncio.create_task(tool.func._async_body())
    pending = await _pending(events)
    assert pending["auto_review"] == {"denied": True, "reason": "Not the run you named."}
    assert calls == []
    registry.resolve(approval_key(pending["id"], "alice"), True)
    assert await call == "ran"
    assert _resolved(events)[-1]["auto_review_denied"] is True


@pytest.mark.parametrize("reply", [RuntimeError("timeout"), "not json", '{"label": "maybe", "reason": "x"}'])
async def test_a_failed_review_asks_the_user_and_never_allows(reply: Any) -> None:
    """An error or a bad reply falls back to the normal prompt, with no denial shown.

    Args:
        reply: A failing LM reply.
    """
    events: list[dict[str, Any]] = []
    registry = ApprovalRegistry()
    flag = _TurnAuthoringFlag()
    flag.auto_review, _ = _reviewer(reply)
    call = asyncio.create_task(
        _wrap("rename_job_optimizations", events, registry, flag, trust_mode="auto_safe").func._async_body()
    )
    pending = await _pending(events)
    assert "auto_review" not in pending
    registry.resolve(approval_key(pending["id"], "alice"), False)
    assert await call == "User declined"


async def test_a_review_that_times_out_asks_the_user() -> None:
    """A slow review counts as failed."""
    reviewer, _ = _reviewer('{"label": "allow", "confidence": 1, "reason": "ok"}')
    reviewer._timeout = 0.01
    reviewer._send = lambda _messages: time.sleep(0.5) or "late"
    assert await reviewer.review("rename_job_optimizations", {}) is None


async def test_the_review_masks_secrets_in_the_arguments() -> None:
    """Secret-like keys and credential-shaped values never reach the classifier."""
    reviewer, prompts = _reviewer('{"label": "allow", "confidence": 1, "reason": "ok"}')
    secret = "sk-or-v1-" + "a" * 48
    verdict = await reviewer.review(
        "update_wizard_state", {"api_key": "plain-value-123", "notes": f"use {secret}", "job_name": "Q3"}
    )
    assert verdict == AutoReviewVerdict(allowed=True, reason="ok")
    text = str(prompts[0])
    assert "plain-value-123" not in text
    assert secret not in text
    assert "Q3" in text
    assert "Rename my latest run to Q3" in text


async def test_the_review_runs_only_where_auto_safe_skips_the_prompt() -> None:
    """Read-only tools, destructive tools and Ask mode get no review."""
    flag = _TurnAuthoringFlag()
    flag.auto_review, prompts = _reviewer('{"label": "allow", "confidence": 1, "reason": "ok"}')
    events: list[dict[str, Any]] = []
    registry = ApprovalRegistry()
    assert await _wrap("list_jobs_optimizations", events, registry, flag, trust_mode="auto_safe").func._async_body()
    call = asyncio.create_task(
        _wrap("delete_job_optimizations", events, registry, flag, trust_mode="auto_safe").func._async_body()
    )
    pending = await _pending(events)
    registry.resolve(approval_key(pending["id"], "alice"), False)
    await call
    assert prompts == []


def test_the_reviewer_uses_jev_for_its_classifier_role() -> None:
    """The review model is TypeSafe Jev under the auto-review role."""
    assert review_mod.AUTO_REVIEW_MODEL == "openrouter/typesafe/jev-router"
    assert review_mod.AUTO_REVIEW_ROLE == "auto_review"


@pytest.mark.parametrize(
    "reply",
    [
        None,
        "",
        '{"label": "allow", "reason": ""}',
        '{"label": "allow", "reason": "a"} {"label": "deny", "reason": "b"}',
    ],
)
def test_the_review_parser_refuses_a_bad_reply(reply: str | None) -> None:
    """No object, no reason or two answers all raise.

    Args:
        reply: A bad reply.
    """
    with pytest.raises(ClassificationError):
        parse_review_reply(reply)


def test_the_review_parser_clips_the_reason() -> None:
    """A long reason is clipped for the chat row."""
    verdict = parse_review_reply('```json\n{"label": "DENY", "confidence": 1, "reason": "' + "r" * 500 + '"}\n```')
    assert verdict.allowed is False
    assert len(verdict.reason) == 200


_JEV_SLUG = "typesafe/jev-router"
_ALLOW_REPLY = '{"label": "allow", "confidence": 1, "reason": "Asked for."}'


class _Provider:
    """A synthetic OpenRouter that records each paid call and the ledger state it saw."""

    def __init__(self, engine: Any, status: int = 200) -> None:
        """Bind the provider to the test wallet.

        Args:
            engine: The test database.
            status: The status of each chat answer.
        """
        self.engine = engine
        self.status = status
        self.posts: list[httpx.Request] = []
        self.states_at_call: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """Answer the price lookup and the chat call.

        Args:
            request: The outgoing request.

        Returns:
            Live-shaped prices, or a chat answer with a reported cost.
        """
        if request.method == "GET":
            return httpx.Response(200, json={"data": {**CATALOG, "id": _JEV_SLUG}})
        self.posts.append(request)
        with Session(self.engine) as session:
            operation = session.scalar(select(ExecutionOperationModel))
            self.states_at_call.append(operation.state if operation else "none")
        if self.status >= 400:
            return httpx.Response(self.status, json={"error": {"message": "provider failed"}})
        return httpx.Response(
            200,
            json={
                "id": "gen-review",
                "model": _JEV_SLUG,
                "choices": [{"message": {"content": _ALLOW_REPLY}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 40, "completion_tokens": 12, "cost": 0.001},
            },
        )


@pytest.fixture
def wallet(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Run the real reservation and settlement code over a synthetic provider.

    Args:
        tmp_path: Private database directory.
        monkeypatch: Replaces only the provider network and the privacy lookup.

    Returns:
        The test database, with 100 cents for ``alice``.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'review.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(username="alice", stripe_customer_id="fixture", balance_cents=100, grant_remaining=0)
        )
        session.commit()
    monkeypatch.setattr(settings, "usage_markup", 1.0)
    monkeypatch.setattr(review_mod, "data_policy_for", lambda username: "deny")
    return engine


def _route(monkeypatch: pytest.MonkeyPatch, provider: _Provider) -> None:
    """Send the review's HTTP calls to ``provider``.

    Args:
        monkeypatch: Patches the review module's HTTP client.
        provider: The synthetic provider.
    """
    factory = httpx.Client
    monkeypatch.setattr(
        review_mod.httpx, "Client", lambda **kwargs: factory(transport=httpx.MockTransport(provider), **kwargs)
    )


def _ledger(engine: Any) -> tuple[list[str], list[str], list[int], int]:
    """Read the review's ledger rows.

    Args:
        engine: The test database.

    Returns:
        The operation states, the budget states, the charged wallet units and
        the wallet balance in cents.
    """
    with Session(engine) as session:
        operations = list(session.scalars(select(ExecutionOperationModel)))
        budgets = list(session.scalars(select(ExecutionBudgetModel)))
        balance = session.scalar(select(BillingCustomerModel.balance_cents))
    return (
        [o.state for o in operations],
        [b.state for b in budgets],
        [o.actual_wallet_units for o in operations],
        balance,
    )


def test_the_review_reserves_before_the_provider_call_and_settles_after(
    wallet: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wallet holds the priced maximum while Jev runs, then pays the reported cost.

    Args:
        wallet: The test database.
        monkeypatch: Routes the provider network.
    """
    provider = _Provider(wallet)
    _route(monkeypatch, provider)
    reply = send_protected_review(wallet, "alice", "synthetic-platform-key", [{"role": "user", "content": "x"}])
    assert reply == _ALLOW_REPLY
    assert provider.states_at_call == ["dispatched"]
    assert provider.posts[0].headers["authorization"] == "Bearer synthetic-platform-key"
    operations, budgets, charged, _ = _ledger(wallet)
    assert operations == ["settled"]
    assert budgets == ["closed"]
    assert charged[0] > 0


async def test_an_empty_wallet_skips_the_review_and_asks_the_user(wallet: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no funds the reservation fails, Jev gets no call, and the normal prompt shows.

    Args:
        wallet: The test database.
        monkeypatch: Routes the provider network.
    """
    with Session(wallet) as session:
        session.scalar(select(BillingCustomerModel)).balance_cents = 0
        session.commit()
    provider = _Provider(wallet)
    _route(monkeypatch, provider)
    with pytest.raises(BudgetError):
        send_protected_review(wallet, "alice", "synthetic-platform-key", [{"role": "user", "content": "x"}])
    events: list[dict[str, Any]] = []
    registry = ApprovalRegistry()
    flag = _TurnAuthoringFlag()
    flag.auto_review = AutoReviewer(
        send=partial(send_protected_review, wallet, "alice", "synthetic-platform-key"), request="Rename it"
    )
    calls: list[dict[str, Any]] = []
    call = asyncio.create_task(
        _wrap(
            "rename_job_optimizations", events, registry, flag, trust_mode="auto_safe", calls=calls
        ).func._async_body()
    )
    pending = await _pending(events)
    assert "auto_review" not in pending
    assert calls == []
    registry.resolve(approval_key(pending["id"], "alice"), False)
    assert await call == "User declined"
    assert provider.posts == []
    operations, _, _, balance = _ledger(wallet)
    assert operations == []
    assert balance == 0


async def test_a_provider_error_releases_the_reservation_and_asks_the_user(
    wallet: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed Jev call charges nothing, closes its budget, and falls back to the prompt.

    Args:
        wallet: The test database.
        monkeypatch: Routes the provider network.
    """
    provider = _Provider(wallet, status=500)
    _route(monkeypatch, provider)
    reviewer = AutoReviewer(
        send=partial(send_protected_review, wallet, "alice", "synthetic-platform-key"), request="Rename it"
    )
    assert await reviewer.review("rename_job_optimizations", {"job_id": "j1"}) is None
    assert provider.states_at_call == ["dispatched"]
    operations, budgets, charged, balance = _ledger(wallet)
    assert operations == ["released"]
    assert budgets == ["closed"]
    assert charged == [0]
    assert balance == 100
