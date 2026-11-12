"""Auto on a repository: five explore lanes, then GEPA from the winner's patch (SKY-316)."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from core.models.blackbox import BlackboxRunRequest, BlackboxStrategy

from .. import auto, gepa_engine
from ..gepa_engine import GepaEngine
from ..harness import GatewayConfig
from ..native_runtime import NativeOptions
from ..protocol import EngineContext, EvalServer, Result, Task
from ..registry import EngineCapabilities
from ..upstream import AUTO_ENGINES
from .mocks import make_ctx

CAPS = EngineCapabilities(proposer_available=True)
REPO = {"chunks": [], "editable_paths": ["src"], "readonly_paths": [], "constrained": False}


def _patch(name: str) -> str:
    """Build the fake patch one engine proposes.

    Args:
        name: Engine identifier.

    Returns:
        A unified diff that names the engine.
    """
    return f"diff --git a/src/{name}.py b/src/{name}.py\n+# {name}\n"


class RepoFixtureEngine:
    """Propose one patch per engine and record what each run received."""

    calls: list[dict[str, Any]] = []
    lock = threading.Lock()

    def __init__(self, name: str) -> None:
        """Set the engine identifier.

        Args:
            name: Engine identifier.
        """
        self.name = name

    def run(self, task: Task, server: EvalServer, ctx: EngineContext) -> Result:
        """Score this engine's patch with the whole lane allowance.

        Args:
            task: Task with the scheduler's chosen starting patch.
            server: Lane scorer allowance.
            ctx: Lane context carrying the repository options.

        Returns:
            The engine's patch and its score.
        """
        with self.lock:
            self.calls.append(
                {
                    "name": self.name,
                    "seed": task.seed_candidate,
                    "repo": None if ctx.native_options is None else ctx.native_options.repo,
                }
            )
        candidate = _patch(self.name) if self.name != "gepa" or not task.seed_candidate else task.seed_candidate
        scores = [server.evaluate(candidate, None)[0] for _ in range(server.remaining)]
        return Result(candidate, sum(scores) / len(scores), len(scores))


def score(candidate: str, example: Any = None) -> tuple[float, dict[str, Any]]:
    """Rank Meta-Harness's patch first.

    Args:
        candidate: The patch under test.
        example: Ignored.

    Returns:
        Deterministic score and empty feedback.
    """
    return (1.0 if "meta_harness" in candidate else 0.5), {}


def _native() -> NativeOptions:
    """Build native options for a repository run.

    Returns:
        Options whose ``repo`` field marks the run as a repository run.
    """
    return NativeOptions(
        runtime="vercel",
        model="openai/test",
        gateway=GatewayConfig(url="https://example.com", api_key="key"),
        max_token_cost=None,
        repo=dict(REPO),
    )


def test_repository_run_with_auto_passes_validation() -> None:
    """A repository run may pick Auto, and Auto is the default strategy."""
    request = BlackboxRunRequest.model_validate(
        {
            "recipe": "repo",
            "scorer": {"kind": "python", "metric_code": "def score(repo_path):\n    return 1.0\n"},
            "reflection_model_config": {"name": "gpt-4o"},
            "budget": {"max_evals": 12},
            "target": {"kind": "repo", "repo": {"repository": "octo/hello", "editable_paths": ["src"]}},
        }
    )
    assert request.strategy.mode == "auto"


def test_auto_on_a_repository_explores_five_lanes_then_continues_gepa_from_the_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every explore lane gets the repository options; GEPA continues from the winning patch.

    Args:
        tmp_path: Artifact directory.
        monkeypatch: Pytest monkeypatch fixture.
    """
    RepoFixtureEngine.calls = []
    monkeypatch.setattr(auto, "get_engine", lambda name, caps: RepoFixtureEngine(name))
    server = EvalServer(score, max_evals=2 * (len(AUTO_ENGINES) + 1))

    result, lanes = auto.run_strategy(
        BlackboxStrategy(), Task(""), server, make_ctx(str(tmp_path), native_options=_native()), caps=CAPS
    )

    assert len(AUTO_ENGINES) == 5
    assert [lane.phase for lane in lanes] == ["explore"] * 5 + ["continue"]
    assert sorted(lane.engine for lane in lanes[:5]) == sorted(AUTO_ENGINES)
    explore, continuation = RepoFixtureEngine.calls[:5], RepoFixtureEngine.calls[5]
    assert all(call["seed"] == "" for call in explore)
    assert all(call["repo"] == REPO for call in RepoFixtureEngine.calls)
    assert continuation["name"] == "gepa"
    assert continuation["seed"] == _patch("meta_harness")
    assert result.best_candidate == _patch("meta_harness")
    assert result.best_score == 1.0


def test_gepa_on_a_repository_runs_the_native_repository_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GEPA with repository options hands the lane to the native ``gepa_repo`` engine.

    Args:
        tmp_path: Artifact directory.
        monkeypatch: Pytest monkeypatch fixture.
    """
    seen: list[tuple[str, Any]] = []

    def fake_native(name: str, task: Task, server: EvalServer, ctx: EngineContext) -> Result:
        """Record the native engine call.

        Args:
            name: Native engine name.
            task: The lane task.
            server: The lane scorer.
            ctx: The lane context.

        Returns:
            A fixed result.
        """
        seen.append((name, task.seed_candidate))
        return Result(task.seed_candidate, 1.0, 0)

    monkeypatch.setattr(gepa_engine, "run_native_engine", fake_native)
    server = EvalServer(score, max_evals=2)

    GepaEngine().run(Task(_patch("meta_harness")), server, make_ctx(str(tmp_path), native_options=_native()))

    assert seen == [("gepa_repo", _patch("meta_harness"))]
