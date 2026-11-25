"""Tests for the interactive-turn billing helpers in ``routers._helpers``.

Covers the 402 balance gate (``enforce_llm_balance``) and the SSE metering
wrapper (``stream_with_llm_metering``) — including the early-teardown path,
where the client drops the stream before the terminal event and the turn must
still be billed from the sink.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ...config import settings
from ...storage.models import Base, BillingCustomerModel, WalletLedgerModel
from ..errors import DomainError
from ..routers import _helpers
from ..routers._helpers import enforce_llm_balance, stream_with_llm_metering


class _StubStore:
    """Job-store double exposing only the engine the billing path touches."""

    def __init__(self, engine: Engine | None) -> None:
        """Store the engine (or None to model a legacy/in-memory store)."""
        self.engine = engine


@pytest.fixture
def engine() -> Iterator[Engine]:
    """Yield an in-memory SQLite engine with the billing tables created."""
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng)
    yield eng
    Base.metadata.drop_all(eng)


def _deplete(engine: Engine, username: str) -> None:
    """Seed a billing row whose grant and paid balance are both exhausted."""
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(
                username=username,
                stripe_customer_id=f"cus_{username}",
                balance_cents=0,
                grant_remaining=0,
            )
        )
        session.commit()


def _fund(engine: Engine, username: str, cents: int = 100_000) -> None:
    """Seed a billing row with a paid balance so a metered turn has funds to draw.

    The clamped debit charges at most what the account holds, so a test that
    asserts a debit landed must start from a funded balance.
    """
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(
                username=username,
                stripe_customer_id=f"cus_{username}",
                balance_cents=cents,
                grant_remaining=0,
            )
        )
        session.commit()


class _FakeLm:
    """History-carrying LM double matching what ``usage_by_model_from_history`` reads."""

    def __init__(self, history: list[dict[str, Any]], model: str = "openrouter/test/unpriced") -> None:
        """Store the canned history entries and reported model id."""
        self.history = history
        self.model = model


def test_enforce_llm_balance_rejects_fresh_account(engine: Engine) -> None:
    """A brand-new account has no starting balance and is refused with a 402."""
    with pytest.raises(DomainError) as err:
        enforce_llm_balance(_StubStore(engine), "new@x.io")
    assert err.value.status_code == 402


def test_enforce_llm_balance_passes_funded_account(engine: Engine) -> None:
    """An account with a purchased balance rides through the gate."""
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(
                username="rich@x.io",
                stripe_customer_id="cus_rich",
                balance_cents=500,
                grant_remaining=0,
            )
        )
        session.commit()
    enforce_llm_balance(_StubStore(engine), "rich@x.io")


def test_enforce_llm_balance_rejects_depleted_account(engine: Engine) -> None:
    """A zero-balance account is refused with the 402 insufficient-balance code."""
    _deplete(engine, "broke@x.io")
    with pytest.raises(DomainError) as err:
        enforce_llm_balance(_StubStore(engine), "broke@x.io")
    assert err.value.status_code == 402


def test_enforce_llm_balance_requires_the_minimum_turn_balance(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """A turn is billed after it runs, so a balance below the per-turn floor is refused up front."""
    monkeypatch.setattr(settings, "interactive_min_balance_cents", 5)
    _fund(engine, "low@x.io", cents=4)
    _fund(engine, "ok@x.io", cents=5)
    with pytest.raises(DomainError) as err:
        enforce_llm_balance(_StubStore(engine), "low@x.io")
    assert err.value.status_code == 402
    enforce_llm_balance(_StubStore(engine), "ok@x.io")


def test_enforce_llm_balance_zero_floor_still_requires_a_positive_balance(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Disabling the floor falls back to the old one-cent minimum, never a free turn."""
    monkeypatch.setattr(settings, "interactive_min_balance_cents", 0)
    _deplete(engine, "broke@x.io")
    _fund(engine, "one@x.io", cents=1)
    with pytest.raises(DomainError):
        enforce_llm_balance(_StubStore(engine), "broke@x.io")
    enforce_llm_balance(_StubStore(engine), "one@x.io")


def test_enforce_llm_balance_rejects_account_in_refund_debt(engine: Engine) -> None:
    """An account carrying refund/chargeback debt has zero balances and is refused."""
    with Session(engine) as session:
        session.add(
            BillingCustomerModel(
                username="debt@x.io",
                stripe_customer_id="cus_debt",
                balance_cents=0,
                grant_remaining=0,
                debt_cents=300,
            )
        )
        session.commit()
    with pytest.raises(DomainError) as err:
        enforce_llm_balance(_StubStore(engine), "debt@x.io")
    assert err.value.code == "billing.insufficient_funds"


def test_enforce_llm_balance_skips_engineless_store(engine: Engine) -> None:
    """A store without a SQL engine streams ungated, matching the submit path."""
    enforce_llm_balance(_StubStore(None), "anyone@x.io")
    enforce_llm_balance(_StubStore(engine), "")


async def test_stream_with_llm_metering_bills_on_completion(engine: Engine) -> None:
    """A fully-drained stream passes events through and writes the debit."""
    _fund(engine, "alice@x.io")
    sink = [_FakeLm([{"usage": {"prompt_tokens": 100_000, "completion_tokens": 40_000}}])]

    async def source() -> AsyncIterator[dict[str, Any]]:
        """Yield one complete synthetic stream."""
        yield {"event": "message_patch", "data": {"chunk": "hi"}}
        yield {"event": "done", "data": {}}

    events = [
        event
        async for event in stream_with_llm_metering(
            source(),
            job_store=_StubStore(engine),
            username="alice@x.io",
            description="Agent chat",
            usage_sink=sink,
        )
    ]
    assert [e["event"] for e in events] == ["message_patch", "done"]
    with Session(engine) as session:
        row = session.query(WalletLedgerModel).one()
    assert row.description == "Agent chat"
    assert row.delta_cents < 0
    assert row.input_tokens == 100_000


async def test_stream_with_llm_metering_adds_stats_to_done(engine: Engine) -> None:
    """The done event carries the turn's token split and timing for the reply footer."""
    _fund(engine, "alice@x.io")
    sink = [_FakeLm([{"usage": {"prompt_tokens": 1_200, "completion_tokens": 300}}])]

    async def source() -> AsyncIterator[dict[str, Any]]:
        """Yield one reply token and a done event."""
        yield {"event": "message_patch", "data": {"chunk": "hi"}}
        yield {"event": "done", "data": {"model": "m"}}

    events = [
        event
        async for event in stream_with_llm_metering(
            source(),
            job_store=_StubStore(engine),
            username="alice@x.io",
            description="Agent chat",
            usage_sink=sink,
        )
    ]
    done = events[-1]["data"]
    assert done["model"] == "m"
    stats = done["stats"]
    assert (stats["input_tokens"], stats["output_tokens"]) == (1_200, 300)
    assert stats["duration_ms"] >= stats["ttft_ms"] >= 0


async def test_stream_with_llm_metering_marks_the_final_answer_start(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``final_answer_at`` is the wall-clock time of the first reply token after the last tool call."""
    _fund(engine, "alice@x.io")
    clock = iter([1.0, 2.0, 3.0])
    monkeypatch.setattr(_helpers, "time", SimpleNamespace(monotonic=time.monotonic, time=lambda: next(clock)))

    async def source() -> AsyncIterator[dict[str, Any]]:
        """Yield text, a tool call, the final answer in two tokens, and done."""
        yield {"event": "message_patch", "data": {"chunk": "Let me look."}}
        yield {"event": "tool_start", "data": {}}
        yield {"event": "tool_end", "data": {}}
        yield {"event": "message_patch", "data": {"chunk": "Found"}}
        yield {"event": "message_patch", "data": {"chunk": " it."}}
        yield {"event": "done", "data": {}}

    events = [
        event
        async for event in stream_with_llm_metering(
            source(),
            job_store=_StubStore(engine),
            username="alice@x.io",
            description="Agent chat",
            usage_sink=[_FakeLm([{"usage": {"prompt_tokens": 10, "completion_tokens": 5}}])],
        )
    ]
    assert events[-1]["data"]["stats"]["final_answer_at"] == 2000


async def test_stream_with_llm_metering_sends_stats_for_calls_after_done(engine: Engine) -> None:
    """Usage added after ``done`` arrives in one last ``turn_stats`` event; an unchanged turn sends none."""
    _fund(engine, "alice@x.io")
    lm = _FakeLm([{"usage": {"prompt_tokens": 1_200, "completion_tokens": 300}}])

    async def source(late: bool) -> AsyncIterator[dict[str, Any]]:
        """Yield a done event, then optionally make one more model call.

        Args:
            late: Whether a call lands after ``done``.
        """
        yield {"event": "done", "data": {}}
        if late:
            lm.history.append({"usage": {"prompt_tokens": 100, "completion_tokens": 20}})
            yield {"event": "thinking_summary", "data": {"index": 0, "text": "Pairs."}}

    async def run(late: bool) -> list[dict[str, Any]]:
        """Drain one metered stream.

        Args:
            late: Whether a call lands after ``done``.

        Returns:
            The streamed events.
        """
        return [
            event
            async for event in stream_with_llm_metering(
                source(late), job_store=_StubStore(engine), username="alice@x.io", description="x", usage_sink=[lm]
            )
        ]

    events = await run(late=True)
    assert [e["event"] for e in events] == ["done", "thinking_summary", "turn_stats"]
    done, final = events[0]["data"]["stats"], events[-1]["data"]["stats"]
    assert (final["input_tokens"], final["output_tokens"]) == (1_300, 320)
    assert final["duration_ms"] == done["duration_ms"]
    assert final["reply_output_tokens"] == done["reply_output_tokens"] == 300
    assert [e["event"] for e in await run(late=False)] == ["done"]


async def test_stream_with_llm_metering_bills_on_early_teardown(engine: Engine) -> None:
    """A stream dropped before its terminal event still bills the sink's usage."""
    _fund(engine, "alice@x.io")
    sink = [_FakeLm([{"usage": {"prompt_tokens": 50_000, "completion_tokens": 5_000}}])]

    async def source() -> AsyncIterator[dict[str, Any]]:
        """Yield a stream that the caller will close after its first event."""
        yield {"event": "message_patch", "data": {"chunk": "hi"}}
        yield {"event": "done", "data": {}}

    stream = stream_with_llm_metering(
        source(),
        job_store=_StubStore(engine),
        username="alice@x.io",
        description="Agent chat",
        usage_sink=sink,
    )
    assert (await anext(stream))["event"] == "message_patch"
    await stream.aclose()
    with Session(engine) as session:
        row = session.query(WalletLedgerModel).one()
    assert row.delta_cents < 0


async def test_stream_with_llm_metering_skips_empty_sink(engine: Engine) -> None:
    """A turn whose run function never built an LM bills nothing."""

    async def source() -> AsyncIterator[dict[str, Any]]:
        """Yield an error without model usage."""
        yield {"event": "error", "data": {"error": "boom"}}

    events = [
        event
        async for event in stream_with_llm_metering(
            source(),
            job_store=_StubStore(engine),
            username="alice@x.io",
            description="Agent chat",
            usage_sink=[],
        )
    ]
    assert [e["event"] for e in events] == ["error"]
    with Session(engine) as session:
        assert session.query(WalletLedgerModel).count() == 0
