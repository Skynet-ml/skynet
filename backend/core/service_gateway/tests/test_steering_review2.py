"""Test owner-wide pending bounds and bounded steer retention cleanup."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from ...storage.models import AgentSteerMessageModel
from ..agents.steering import SteerLimitError, SteerStore


@pytest.fixture(params=[False, True])
def store(request: pytest.FixtureRequest) -> SteerStore:
    """Build an isolated local or SQL mailbox.

    Args:
        request: Whether this case uses SQL storage.

    Returns:
        An empty mailbox store.
    """
    result = SteerStore()
    if request.param:
        engine = create_engine("sqlite://")
        AgentSteerMessageModel.__table__.create(engine)
        result.bind_engine(engine)
    return result


def test_owner_count_cap_covers_new_keys_and_isolated_owners(store: SteerStore) -> None:
    """Bound new keys without consuming another owner's allowance.

    Args:
        store: An isolated mailbox store.
    """
    for index in range(100):
        store.post("alice", str(index), "x")
    with pytest.raises(SteerLimitError):
        store.post("alice", "new-key", "queue this")
    store.post("bob", "new-key", "private")
    store.withdraw("alice", "0")
    store.post("alice", "new-key", "room restored")


def test_owner_character_cap_covers_new_keys(store: SteerStore) -> None:
    """Bound pending characters across every key owned by one caller.

    Args:
        store: An isolated mailbox store.
    """
    for index in range(10):
        store.post("alice", str(index), "x" * 20_000)
    with pytest.raises(SteerLimitError):
        store.post("alice", "new-key", "x")
    store.take("alice", "0")
    store.post("alice", "new-key", "room restored")


def test_post_purges_only_a_bounded_stale_batch() -> None:
    """Keep a single post from deleting the whole stale table."""
    engine = create_engine("sqlite://")
    AgentSteerMessageModel.__table__.create(engine)
    stale = datetime.now(UTC) - timedelta(days=1)
    with Session(engine) as session:
        session.add_all([
            AgentSteerMessageModel(id=str(index), owner="alice", steer_key="old", text="x", created_at=stale)
            for index in range(501)
        ])
        session.commit()
    store = SteerStore()
    store.bind_engine(engine)
    store.post("alice", "new", "new")
    with Session(engine) as session:
        remaining = session.scalar(select(func.count()).select_from(AgentSteerMessageModel).where(
            AgentSteerMessageModel.steer_key == "old",
        ))
    assert remaining == 401
    assert any(tuple(column.name for column in index.columns) == ("created_at",)
               for index in AgentSteerMessageModel.__table__.indexes)


def test_full_withdraw_retains_delivery_proof_for_a_retry(store: SteerStore) -> None:
    """Preserve a delivered outcome if the first withdrawal response is lost.

    Args:
        store: An isolated mailbox store.
    """
    message_id = store.post("alice", "turn", "read")
    store.take("alice", "turn")
    assert store.withdraw("alice", "turn") == ([], [message_id])
    assert store.withdraw("alice", "turn") == ([], [message_id])


def test_bounded_take_keeps_unread_text_and_order(store: SteerStore) -> None:
    """Leave non-fitting steers available for a later step or withdrawal.

    Args:
        store: An isolated mailbox store.
    """
    first = store.post("alice", "turn", "a" * 1000)
    second = store.post("alice", "turn", "b" * 1000)
    assert store.take("alice", "turn", max_chars=2000) == [(first, "a" * 1000)]
    assert store.take("alice", "turn", max_chars=2000) == [(second, "b" * 1000)]
    oversized = store.post("alice", "turn", "c" * 2001)
    assert store.take("alice", "turn", max_chars=2000) == []
    assert store.withdraw("alice", "turn", [oversized]) == ([oversized], [])
