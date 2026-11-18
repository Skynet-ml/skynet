"""The load-test pools have a fixed thread cap, whatever the agent or run count (SKY-727)."""

import dataclasses

import pytest

from load_tests.scenarios import chaos_gate, swarm_10k


@pytest.mark.parametrize("agents", [0, 1, 31, 32, 33, 1_250, 10_000, 1_000_000])
def test_swarm_driver_threads_have_a_cap(agents: int) -> None:
    """Check that one swarm shard process starts at most the capped driver count.

    Args:
        agents: The shard's agents.
    """
    count = swarm_10k.driver_count(agents)
    assert 1 <= count <= swarm_10k.MAX_DRIVERS_PER_SHARD
    assert count <= max(1, agents)


@pytest.mark.parametrize("workers", [0, 1, 2, 8, 9, 10_000])
def test_chaos_pool_has_a_cap(workers: int) -> None:
    """Check that the chaos gate runs at most the capped number of runs at once.

    Args:
        workers: The asked number of runs at once.
    """
    assert 1 <= chaos_gate.pool_size(workers) <= chaos_gate.MAX_WORKERS


def test_swarm_rejects_too_many_shards() -> None:
    """Check that the swarm refuses more shard processes than the cap, before it starts any."""
    config = dataclasses.replace(swarm_10k.default_config(), shards=swarm_10k.MAX_SHARDS + 1)
    with pytest.raises(ValueError, match="shard processes"):
        swarm_10k.run(config)


@pytest.mark.parametrize(
    ("agents", "shards"),
    [(0, 2), (1, 2), (800, 2), (1_000, 2), (1_601, 3), (5_000, 7), (10_000, 13), (1_000_000, 32)],
)
def test_swarm_default_shards_follow_the_load(agents: int, shards: int) -> None:
    """Check that the default shard count is one shard for each 800 agents, from 2 to the cap.

    Args:
        agents: The swarm's agents.
        shards: The expected shard count.
    """
    assert swarm_10k.AGENTS_PER_SHARD == 800
    assert swarm_10k.default_shards(agents) == shards


def test_swarm_shards_env_overrides_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that ``LOAD_TEST_SWARM_SHARDS`` overrides the default shard count.

    Args:
        monkeypatch: Sets the environment.
    """
    monkeypatch.setenv("LOAD_TEST_SWARM_AGENTS", "10000")
    monkeypatch.delenv("LOAD_TEST_SWARM_SHARDS", raising=False)
    assert swarm_10k.default_config().shards == 13
    monkeypatch.setenv("LOAD_TEST_SWARM_SHARDS", "8")
    assert swarm_10k.default_config().shards == 8


@pytest.mark.parametrize("agents", [1, 1_000, 5_700, 10_000])
def test_swarm_pace_offers_the_target_rate(agents: int) -> None:
    """Check that the paced agents together offer the target rate and its margin, at seeded offsets in one interval.

    Args:
        agents: The swarm's agents.
    """
    interval = swarm_10k.step_interval(agents)
    assert swarm_10k.OFFERED_LOAD_MARGIN == 1.05
    offered = swarm_10k.TARGET_COMMITS_PER_SECOND * swarm_10k.OFFERED_LOAD_MARGIN
    assert interval == pytest.approx(agents / offered)
    assert agents / interval == pytest.approx(offered)
    assert agents / interval > swarm_10k.TARGET_COMMITS_PER_SECOND
    offsets = [swarm_10k.start_offset("swarm-000000", f"agent-{i:05d}", interval) for i in range(50)]
    assert all(0 <= offset < interval for offset in offsets)
    assert len(set(offsets)) == len(offsets)
    assert offsets == [swarm_10k.start_offset("swarm-000000", f"agent-{i:05d}", interval) for i in range(50)]
