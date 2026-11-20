"""Exercise the Meta-Harness and AutoResearch prompts, evidence and stop rules."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from gepa.oa.budget import BudgetTracker
from gepa.oa.config import OptimizeAnythingConfig
from gepa.oa.eval_server import EvalServer
from gepa.oa.task import Task

from core.service_gateway.optimization.blackbox import native_engines


def _config(engine: str, **engine_config: Any) -> OptimizeAnythingConfig:
    """Build the run config the sandbox runner would build.

    Args:
        engine: Engine id.
        **engine_config: Engine knobs beyond the model.

    Returns:
        A config with a temporary run directory left for the caller to set.
    """
    return OptimizeAnythingConfig(engine=engine, max_evals=10, engine_config={"model": "claude-test", **engine_config})


def test_pinned_prompts_match_upstream_checksums() -> None:
    """Refuse to run on prompt files that differ from the pinned upstream revisions."""
    probe = OptimizeAnythingConfig(engine="meta_harness", engine_config={"model": "probe"})
    assert native_engines.MetaHarnessEngine(probe).skill()
    for relative, digest in native_engines.ASSET_CHECKSUMS.items():
        text = (native_engines.PROMPTS_DIR / relative).read_text(encoding="utf-8")
        assert hashlib.sha256(text.encode("utf-8")).hexdigest() == digest


def test_adapt_fails_loudly_on_drifted_or_ambiguous_snippets() -> None:
    """Every substitution must match exactly once so a pin bump cannot silently change meaning."""
    assert native_engines.adapt("one two three", [("two", "2"), ("one", "three", "all")]) == "all"
    with pytest.raises(RuntimeError, match="matched 0 times"):
        native_engines.adapt("one two", [("three", "3")])
    with pytest.raises(RuntimeError, match="matched 2 times"):
        native_engines.adapt("two two", [("two", "2")])
    with pytest.raises(RuntimeError, match="inverted"):
        native_engines.adapt("one two", [("two", "one", "x")])


def test_adapted_prompts_replace_the_upstream_domains() -> None:
    """The nanochat and memory-system wording gives way to the evaluator and candidate files."""
    skill = native_engines.MetaHarnessEngine(_config("meta_harness", max_candidates_per_iter=2)).skill()
    assert "2 new candidates" in skill
    assert "CANDIDATES: <name1>, <name2>" in skill
    assert "agents/<name>.txt" in skill
    assert "MemorySystem" not in skill
    assert "dataset" not in skill.lower()
    # SKY-750: a minimize run's avg_val is negative, so the note sits beside the val.json description.
    assert "`infos`). " + native_engines._SCALE_NOTE in skill
    brief = native_engines.AutoResearchEngine(_config("autoresearch"))
    brief.example_ids = ["a"]
    text = brief._brief()
    assert "./eval.sh <file> <example_id>" in text
    assert "BUDGET_EXHAUSTED" in text


def test_meta_harness_refuses_a_candidate_file_over_its_byte_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A candidate file over ``MAX_CANDIDATE_BYTES`` is too large; a file at the cap passes the size check.

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Pytest's monkeypatch.
    """
    monkeypatch.setattr(native_engines, "MAX_CANDIDATE_BYTES", 10)
    config = _config("meta_harness")
    config.run_dir = str(tmp_path / "run")
    engine = native_engines.MetaHarnessEngine(config)
    agents = engine.work_dir / "agents"
    agents.mkdir(parents=True)
    (agents / f"big{engine.suffix}").write_text("x" * 11)
    (agents / f"fits{engine.suffix}").write_text("x" * 10)

    assert engine._validate({"name": "big", "file": f"agents/big{engine.suffix}"}) == native_engines.CANDIDATE_TOO_LARGE
    assert engine._validate({"name": "fits", "file": f"agents/fits{engine.suffix}"}) != (
        native_engines.CANDIDATE_TOO_LARGE
    )


def test_autoresearch_directives_follow_the_evidence(tmp_path: Path) -> None:
    """Complementary frontier members trigger a combine round; stalls trigger a pivot.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    engine = native_engines.AutoResearchEngine(_config("autoresearch"))
    engine.work_dir = tmp_path
    (tmp_path / "archive").mkdir()
    engine.round = 1
    assert engine._directive(0, False) == "survey"
    specialist = native_engines.Observation("x", 0.5, {"a": 1.0, "b": 0.0}, {}, True, 1)
    generalist = native_engines.Observation("y", 0.625, {"a": 0.5, "b": 0.75}, {}, True, 1)
    for observation in (specialist, generalist):
        engine._observe(observation)
    assert native_engines.pareto_front(engine._table(), engine._ranking()) == ["y", "x"]
    assert engine._partner() == ("x", ["a"])
    engine.round, engine.directives = 2, ["survey"]
    assert engine._directive(0, True) == "combine"
    engine.directives.append("combine")
    assert engine._directive(0, True) == "exploit"
    assert engine._directive(1, False) == "explore"
    assert engine._directive(2, False) == "pivot"
    table = engine._table()
    assert "`c001` beats the leader `c002` on `a`" in engine._directive_text("combine", table, ["y", "x"])


def test_autoresearch_checkpoints_every_single_task_answer(tmp_path: Path) -> None:
    """Without cases each scored version becomes a checkpoint, so the run view can chart it.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    task = Task(name="single", seed_candidate="seed", objective="be better")
    single = EvalServer(
        task, lambda candidate, example=None: (len(candidate) / 10, {"feedback": "ok"}), BudgetTracker(max_evals=5)
    )
    config = _config("autoresearch")
    config.run_dir = str(tmp_path / "run")
    engine = native_engines.AutoResearchEngine(config)
    engine.example_ids = []
    single.start()
    try:
        engine._layout(task, single)
        restore = engine._record(single)
        single.evaluate("seed")
        single.evaluate("better")
        restore()
    finally:
        single.stop()
    ids = single._candidate_registry
    assert [(e["candidate_id"], e["val_score"]) for e in single.progress_log] == [
        (ids["seed"], 0.4),
        (ids["better"], 0.6),
    ]


def test_named_scores_become_pareto_columns_without_cases() -> None:
    """Without cases, named scores are the per-example table's columns, so the front and combine still work.

    A named score without feedback (SKY-769) still is a column, with an empty note.
    """
    named_a = {"scores": {"accuracy": {"score": 1.0, "feedback": "all right"}, "style": {"score": 0.0}}}
    named_b = {"scores": {"accuracy": {"score": 0.5, "feedback": "half"}, "style": {"score": 1.0, "feedback": "nice"}}}

    columns_a, notes_a = native_engines.named_score_columns([named_a])
    columns_b, _ = native_engines.named_score_columns([named_b])

    assert columns_a == {"score:accuracy": 1.0, "score:style": 0.0}
    assert notes_a == {"score:accuracy": "all right", "score:style": ""}
    assert columns_b == {"score:accuracy": 0.5, "score:style": 1.0}
    table = {"a": columns_a, "b": columns_b}
    assert native_engines.pareto_front(table, {"a": 0.5, "b": 0.75}) == ["b", "a"]


def test_a_missing_column_never_beats_a_negative_engine_score() -> None:
    """A minimize run gives negative engine scores; a missing column must not count as 0.0 and win (SKY-750)."""
    table = {"a": {"e1": -0.5, "e2": -0.5}, "b": {"e1": -0.4}}

    assert native_engines.pareto_front(table, {"a": -0.5, "b": -0.4}) == ["b", "a"]


def test_a_missing_score_shows_as_n_a_in_the_agent_report() -> None:
    """The report writes ``n/a`` for a missing column, not ``-inf`` (SKY-750)."""
    assert native_engines._shown(native_engines._MISSING_SCORE) == "n/a"
    assert native_engines._shown(-0.5) == "-0.500"


def test_the_task_brief_says_scores_are_on_the_engine_scale() -> None:
    """The agent learns that higher is always better and that a score may be negative (SKY-750)."""
    task = Task(name="t", seed_candidate="seed", objective="be better")

    for example_ids in ([], ["e1"]):
        assert "higher is always better, and a score may be negative" in native_engines.task_brief(task, example_ids)


def test_named_scores_average_over_cases_with_their_feedback() -> None:
    """With cases, each named score's column is its mean, and every case's note is kept."""
    infos = [
        {"scores": {"accuracy": {"score": 1.0, "feedback": "case one fine"}}},
        {"scores": {"accuracy": {"score": 0.0, "feedback": "case two wrong"}}},
    ]

    columns, notes = native_engines.named_score_columns(infos)

    assert columns == {"score:accuracy": 0.5}
    assert "case one fine" in notes["score:accuracy"]
    assert "case two wrong" in notes["score:accuracy"]


def test_repo_gepa_reflection_shows_each_named_score_with_its_feedback() -> None:
    """GEPA gets numeric named scores for its frontier; the repo proposer still reads each one's feedback."""
    shaped = native_engines.gepa_named_scores(
        {"feedback": "overall ok", "scores": {"tests": {"score": 0.5, "feedback": "two failing"}}}
    )
    assert shaped["scores"] == {"tests": 0.5}
    record = {
        "feedback": shaped["feedback"],
        "Scores (Higher is Better)": shaped["scores"],
        "Feedback per score": shaped["Feedback per score"],
    }

    text = native_engines.record_text(record)

    assert "overall ok" in text
    assert "tests: 0.5" in text
    assert "two failing" in text


class _NamedCostServer:
    """Answer ``evaluate_examples`` with a fixed score and one minimize ``cost_usd`` named score per version."""

    def __init__(self, costs: dict[str, float]) -> None:
        """Keep each version's raw cost.

        Args:
            costs: Raw ``cost_usd`` of each candidate text.
        """
        self.costs = costs

    def evaluate_examples(self, candidate: str, example_ids: Any = None) -> tuple[float, dict[str, Any]]:
        """Score a version 0.5 with its raw cost, marked minimize as the eval server marks it.

        Args:
            candidate: The candidate text.
            example_ids: Unused; the version is scored on one case, ``e1``.

        Returns:
            The mean score and the per-case scores and side info.
        """
        cost = {"score": self.costs[candidate], "feedback": "spend", "direction": "minimize"}
        side_info = {"scores": {"cost_usd": cost, "recall": {"score": 0.9, "feedback": ""}}}
        return 0.5, {"scores": {"e1": 0.5}, "infos": {"e1": side_info}}

    def log_progress(self, score: float, candidate: str) -> None:
        """Ignore progress; the test reads the written files.

        Args:
            score: The version's score.
            candidate: The candidate text.
        """


def test_the_lowest_minimize_named_score_leads_its_column(tmp_path: Path) -> None:
    """Of three versions, the lowest raw cost leads ``score:cost_usd``; val.json shows the raw cost (SKY-775)."""
    engine = native_engines.MetaHarnessEngine(_config("meta_harness"))
    engine.logs_dir = tmp_path
    engine.example_ids = ["e1"]
    server = _NamedCostServer({"a": 0.06, "b": 0.02, "c": 0.04})

    for name in ("a", "b", "c"):
        engine._benchmark(server, name, name)  # type: ignore[arg-type]
    engine._write_frontier()

    frontier = json.loads((tmp_path / "frontier_val.json").read_text())
    assert frontier["score:cost_usd"] == {"system": "b", "val_accuracy": -0.02}
    table = {name: entry["scores"] for name, entry in engine.results.items()}
    assert native_engines.pareto_front(table, {"a": 0.5, "b": 0.5, "c": 0.5}) == ["b"]
    shown = json.loads((tmp_path / "results" / "b" / "val.json").read_text())["named_scores"]
    assert shown["cost_usd"] == {"score": 0.02, "feedback": "spend", "direction": "minimize"}
    assert shown["recall"] == {"score": 0.9, "feedback": ""}


def test_the_brief_and_columns_say_lower_is_better_for_a_minimize_named_score() -> None:
    """The agent reads that a lower-is-better column holds the negated value (SKY-775)."""
    columns, notes = native_engines.named_score_columns(
        [{"scores": {"cost_usd": {"score": 0.05, "feedback": "", "direction": "minimize"}}}]
    )

    assert columns == {"score:cost_usd": -0.05}
    assert notes["score:cost_usd"].startswith("Lower is better")
    assert "holds its negated value" in native_engines.task_brief(Task(name="t", seed_candidate="s"), [])


def test_gepa_reflection_shows_a_minimize_named_score_raw_with_its_direction() -> None:
    """GEPA's frontier holds the negated cost; the proposer text shows the raw cost and says lower is better."""
    record = {
        "Scores (Higher is Better)": {"cost_usd": -0.06, "recall": 0.8},
        "Lower is better": ["cost_usd"],
    }

    text = native_engines.record_text(record)

    assert "cost_usd: 0.06 (lower is better)" in text
    assert "recall: 0.8" in text
    assert "recall: 0.8 (" not in text


def test_a_budget_stop_is_not_a_cli_failure() -> None:
    """Sessions the engine stopped or that ran out of budget end the run normally."""
    for outcome in (
        native_engines.ProposerOutcome("s", 0.0, True, "", budget_exhausted=True, killed=False, returncode=1),
        native_engines.ProposerOutcome("s", 0.0, True, "", budget_exhausted=False, killed=True, returncode=-15),
        native_engines.ProposerOutcome("s", 0.0, False, "done", budget_exhausted=False, killed=False, returncode=0),
    ):
        outcome.raise_if_failed("round1")
