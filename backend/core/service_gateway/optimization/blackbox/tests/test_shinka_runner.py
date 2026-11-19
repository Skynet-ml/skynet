"""Check the ShinkaEvolve guest runner's pure helpers without the upstream package installed."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import random
import re
import subprocess
import sys
import tarfile
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from core.models.blackbox import BlackboxShinkaSettings

from .. import repo_tree, shinka_bundle, shinka_runner


def _payload(**overrides: Any) -> dict[str, Any]:
    """Build the parent payload fields the config builder reads.

    Args:
        **overrides: Payload fields replacing the defaults.

    Returns:
        A payload with default ShinkaEvolve settings and two model routes.
    """
    return {
        "engine_id": "shinka_evolve",
        "max_evals": 20,
        "max_iterations": None,
        "shinka": BlackboxShinkaSettings().model_dump(),
        "shinka_models": [
            {"model": "claude-test", "url": "https://gw.example/v1", "key_env": "APP_SHINKA_KEY_0"},
            {"model": "gpt-test", "url": "https://gw.example/v1", "key_env": "APP_SHINKA_KEY_1"},
        ],
        "shinka_meta_key_env": "APP_SHINKA_META_KEY",
        "task": {"objective": "Be concise.", "background": ""},
        **overrides,
    }


@pytest.mark.parametrize(
    "seed",
    [
        "Answer briefly.\n\nUse <b>bold</b> sparingly.",
        "",
        {"system": "You are terse.", "user": "Q: {question}\n\nA:"},
        {"a -->": "x", 'weird "name"': "line one\n\nline three\n", "empty": ""},
    ],
)
def test_program_packing_round_trips(seed: Any) -> None:
    """Split a packed program back into exactly the starting version.

    Args:
        seed: Text or named parts.
    """
    program = shinka_runner.pack_program(seed)
    names = list(seed) if isinstance(seed, dict) else None

    assert program.count("EVOLVE-BLOCK-START") == (len(seed) if isinstance(seed, dict) else 1)
    assert shinka_runner.unpack_program(program, names) == seed


def test_unpacking_keeps_edits_inside_blocks_and_drops_text_outside() -> None:
    """Take only the evolved text inside the markers, per part."""
    program = shinka_runner.pack_program({"system": "old", "user": "keep"})
    evolved = "A stray preface.\n" + program.replace("\nold\n", "\nnew system\nsecond line\n")

    assert shinka_runner.unpack_program(evolved, ["system", "user"]) == {
        "system": "new system\nsecond line",
        "user": "keep",
    }


@pytest.mark.parametrize(
    "mangle",
    [
        lambda program: program.replace("<!-- EVOLVE-BLOCK-END -->", "", 1),
        lambda program: program.replace('"system"', '"other"'),
        lambda program: program + "\n<!-- EVOLVE-BLOCK-START -->\nextra\n<!-- EVOLVE-BLOCK-END -->\n",
    ],
)
def test_unpacking_rejects_a_program_that_lost_its_layout(mangle: Any) -> None:
    """Refuse a program whose markers were lost, renamed or duplicated.

    Args:
        mangle: Edit that breaks the program's layout.
    """
    program = mangle(shinka_runner.pack_program({"system": "a", "user": "b"}))

    with pytest.raises(ValueError):
        shinka_runner.unpack_program(program, ["system", "user"])


def test_packing_refuses_a_seed_that_already_carries_markers() -> None:
    """Refuse a starting version whose text would be mistaken for a block boundary."""
    with pytest.raises(ValueError, match="already contains"):
        shinka_runner.pack_program("<!-- EVOLVE-BLOCK-END -->")


def test_config_maps_every_setting_onto_upstream_fields(tmp_path: Path) -> None:
    """Translate the request's settings and routes into upstream's three configs.

    Args:
        tmp_path: Results directory.
    """
    settings = BlackboxShinkaSettings(
        num_islands=3, parent_selection="power_law", patch_diff=0.5, patch_full=0.5, patch_cross=0.0
    ).model_dump()
    config = shinka_runner.build_config(_payload(shinka=settings, max_iterations=4), str(tmp_path), 2)
    evolution, database, runner = config["evolution"], config["database"], config["runner"]

    assert evolution["llm_models"] == [
        "local/claude-test@https://gw.example/v1?api_key_env=APP_SHINKA_KEY_0",
        "local/gpt-test@https://gw.example/v1?api_key_env=APP_SHINKA_KEY_1",
    ]
    assert evolution["meta_llm_models"] == ["local/claude-test@https://gw.example/v1?api_key_env=APP_SHINKA_META_KEY"]
    assert evolution["patch_types"] == ["diff", "full", "cross"]
    assert evolution["patch_type_probs"] == [0.5, 0.5, 0.0]
    assert evolution["llm_dynamic_selection"] == "ucb"
    assert evolution["llm_dynamic_selection_kwargs"] == {"cost_aware_coef": 0.0}
    assert evolution["embedding_model"] is None
    assert evolution["num_generations"] == 5
    assert evolution["use_text_feedback"] is True
    assert "Be concise." in evolution["task_sys_msg"]
    assert database["num_islands"] == 3
    assert database["parent_selection_strategy"] == "power_law"
    assert database["db_path"] == str(tmp_path / "programs.sqlite")
    assert (runner["max_evaluation_jobs"], runner["max_proposal_jobs"]) == (2, 2)


def test_on_the_engine_loop_the_config_runs_one_seeded_job_of_each_kind(tmp_path: Path) -> None:
    """Seed the model bandit and run one job of each kind, so the worker's step order is fixed (SKY-724).

    Args:
        tmp_path: Results directory.
    """
    config = shinka_runner.build_config(_payload(transport="stdio", seed=7), str(tmp_path), 2)

    assert config["evolution"]["llm_dynamic_selection_kwargs"] == {"cost_aware_coef": 0.0, "seed": 7}
    assert (config["runner"]["max_evaluation_jobs"], config["runner"]["max_proposal_jobs"]) == (1, 1)


def test_config_turns_meta_notes_off(tmp_path: Path) -> None:
    """Leave upstream's meta notes unset when the run turned them off.

    Args:
        tmp_path: Results directory.
    """
    settings = BlackboxShinkaSettings(meta_notes=False).model_dump()
    evolution = shinka_runner.build_config(_payload(shinka=settings), str(tmp_path), 1)["evolution"]

    assert evolution["meta_llm_models"] is None
    assert evolution["meta_rec_interval"] is None


def test_config_wires_duplicate_rejection(tmp_path: Path) -> None:
    """Embed through the embeddings route and judge with the first optimization model on its own token.

    Args:
        tmp_path: Results directory.
    """
    settings = BlackboxShinkaSettings(novelty=True, code_embed_sim_threshold=0.9, max_novelty_attempts=4)
    payload = _payload(
        shinka=settings.model_dump(),
        shinka_embedding={"model": "openai/emb", "url": "https://gw.example/v1", "key_env": "APP_SHINKA_EMBED_KEY"},
        shinka_novelty_key_env="APP_SHINKA_NOVELTY_KEY",
    )
    evolution = shinka_runner.build_config(payload, str(tmp_path), 1)["evolution"]

    assert evolution["embedding_model"] == "local/openai/emb@https://gw.example/v1?api_key_env=APP_SHINKA_EMBED_KEY"
    assert evolution["novelty_llm_models"] == [
        "local/claude-test@https://gw.example/v1?api_key_env=APP_SHINKA_NOVELTY_KEY"
    ]
    assert evolution["code_embed_sim_threshold"] == 0.9
    assert evolution["max_novelty_attempts"] == 4


def test_config_leaves_duplicate_rejection_off_by_default(tmp_path: Path) -> None:
    """Configure no embeddings or judge unless the run turned duplicate rejection on.

    Args:
        tmp_path: Results directory.
    """
    payload = _payload(
        shinka_embedding={"model": "openai/emb", "url": "https://gw.example/v1", "key_env": "APP_SHINKA_EMBED_KEY"},
        shinka_novelty_key_env="APP_SHINKA_NOVELTY_KEY",
    )
    evolution = shinka_runner.build_config(payload, str(tmp_path), 1)["evolution"]

    assert evolution["embedding_model"] is None
    assert evolution["novelty_llm_models"] is None


def test_config_needs_a_model(tmp_path: Path) -> None:
    """Refuse a payload without any optimization model route.

    Args:
        tmp_path: Results directory.
    """
    with pytest.raises(ValueError, match="at least one optimization model"):
        shinka_runner.build_config(_payload(shinka_models=[]), str(tmp_path), 1)


@pytest.mark.parametrize(
    ("max_evals", "examples", "max_iterations", "expected"),
    [(20, 2, None, 10), (20, 2, 3, 4), (20, 3, 50, 6), (1, 1, 0, 1)],
)
def test_generation_budget_fits_the_scorer_budget(
    max_evals: int, examples: int, max_iterations: int | None, expected: int
) -> None:
    """Create no more programs than the scorer budget and the version limit allow.

    Args:
        max_evals: Scorer runs admitted.
        examples: Scorer runs per program.
        max_iterations: Optional version limit.
        expected: Upstream ``num_generations``.
    """
    assert shinka_runner.generation_budget(max_evals, examples, max_iterations) == expected


def test_generation_budget_refuses_a_budget_below_one_program() -> None:
    """Refuse a budget that cannot score even the starting version."""
    with pytest.raises(ValueError):
        shinka_runner.generation_budget(1, 2, None)


def test_model_names_route_through_the_gateway() -> None:
    """Name each route in upstream's OpenAI-compatible form, refusing names it cannot parse."""
    assert (
        shinka_runner.local_model_name("m", "https://gw/v1?x=1", "VAR") == "local/m@https://gw/v1?x=1&api_key_env=VAR"
    )
    assert shinka_runner.local_model_name("m", "https://gw/v1", None) == "local/m@https://gw/v1"
    with pytest.raises(ValueError):
        shinka_runner.local_model_name("user@model", "https://gw/v1", "VAR")


def test_usage_kind_tells_meta_notes_from_mutations() -> None:
    """Tag the meta-notes route's calls apart from every mutation call, even on the same model."""
    meta = "local/m@https://gw/v1?api_key_env=APP_SHINKA_META_KEY"
    mutation = "local/m@https://gw/v1?api_key_env=APP_SHINKA_KEY_0"

    assert shinka_runner.usage_kind(meta, "APP_SHINKA_META_KEY") == "meta_notes"
    assert shinka_runner.usage_kind(mutation, "APP_SHINKA_META_KEY") == "mutation"
    judge = "local/m@https://gw/v1?api_key_env=APP_SHINKA_NOVELTY_KEY"
    assert shinka_runner.usage_kind(judge, "APP_SHINKA_META_KEY", "APP_SHINKA_NOVELTY_KEY") == "novelty_judge"
    assert json.loads(shinka_runner.usage_header("mutation")["x-app-usage-tags"]) == {
        "caller": "proposer",
        "kind": "mutation",
    }


def test_usage_ledger_counts_a_routed_call_under_its_gateway_model() -> None:
    """Key usage by the gateway model, the same with or without upstream, and keep other names as given."""
    ledger = shinka_runner.UsageLedger(None)

    ledger.record(_EDITOR_MODEL, SimpleNamespace(input_tokens=100, output_tokens=50))
    ledger.record("local/claude-test@https://gw.example/v1", SimpleNamespace(input_tokens=1, output_tokens=2))
    ledger.record("local/claude-test@http://engine-loop.invalid/v1", SimpleNamespace(input_tokens=4, output_tokens=8))
    ledger.record("gpt-test", SimpleNamespace(input_tokens=3, output_tokens=None))
    # Upstream parses no model from a URL with nothing after the scheme, so the name stays whole.
    ledger.record("local/m@https://", SimpleNamespace(input_tokens=5, output_tokens=6))

    assert ledger.usage_by_model == {
        "claude-test": {"prompt_tokens": 105, "completion_tokens": 60, "total_tokens": 165},
        "gpt-test": {"prompt_tokens": 3, "completion_tokens": 0, "total_tokens": 3},
        "local/m@https://": {"prompt_tokens": 5, "completion_tokens": 6, "total_tokens": 11},
    }


def test_scores_become_upstream_metrics() -> None:
    """Report the mean as the combined score, named scores as public metrics and feedback as text."""
    metrics = shinka_runner.summarize_scores(
        [
            (1.0, {"feedback": "great", "scores": {"tone": {"score": 0.5, "feedback": "flat"}}}),
            (0.0, {"scores": {"tone": {"score": 1.0}}}),
        ]
    )

    assert metrics["combined_score"] == 0.5
    assert metrics["public"] == {"tone": 0.75}
    assert metrics["private"] == {"case_0": 1.0, "case_1": 0.0}
    assert "great" in metrics["text_feedback"]
    assert "tone: 0.5 (flat)" in metrics["text_feedback"]


def test_generation_is_read_from_the_program_path() -> None:
    """Read upstream's generation from its ``gen_<n>`` directory."""
    assert shinka_runner.generation_of("/run/results/gen_12/main.py") == 12
    assert shinka_runner.generation_of("/run/results/main.py") is None


class _Mailbox:
    """Collect progress lines in place of the parent transport."""

    def __init__(self) -> None:
        """Start with no lines."""
        self.lines: list[dict[str, Any]] = []

    def emit(self, prefix: str, payload: dict[str, Any]) -> None:
        """Keep one progress payload.

        Args:
            prefix: Event family, ignored.
            payload: Progress fields.
        """
        self.lines.append(payload)


def test_lineage_reporter_names_each_versions_parent_and_depth(tmp_path: Path) -> None:
    """Report scored versions with their parent generation and lineage depth from upstream's database.

    Args:
        tmp_path: Directory for a stand-in program database.
    """
    db_path = tmp_path / "programs.sqlite"
    with shinka_runner.contextlib.closing(shinka_runner.sqlite3.connect(db_path)) as db:
        db.execute("CREATE TABLE programs (id TEXT, generation INTEGER, parent_id TEXT)")
        db.executemany(
            "INSERT INTO programs VALUES (?, ?, ?)",
            [("p0", 0, None), ("p1", 1, "p0"), ("p2", 2, "p1"), ("p2-copy", 2, "p0")],
        )
        db.commit()
    scorer = shinka_runner.ProgramScorer.__new__(shinka_runner.ProgramScorer)
    scorer._lock = shinka_runner.threading.Lock()
    scorer.scored = {generation: {"candidate": "x", "score": 0.1, "per_example": []} for generation in (0, 1, 2, 3)}
    mailbox = _Mailbox()
    reporter = shinka_runner.LineageReporter(mailbox, scorer, db_path)

    reporter.poll()
    assert [(line["candidate_id"], line["parent_id"], line["generation"]) for line in mailbox.lines] == [
        (0, None, 0),
        (1, 0, 1),
        (2, 1, 2),
    ]
    reporter.poll(final=True)
    assert mailbox.lines[-1]["candidate_id"] == 3
    assert mailbox.lines[-1]["parent_id"] is None


def test_lineage_reporter_reads_only_the_programs_filed_since_its_last_poll(tmp_path: Path) -> None:
    """Read only new program rows on each poll, and still link a new version to a parent read before.

    Each upstream row holds the program's code, so a poll that read every row
    would cost CPU that grows with the square of the run length.

    Args:
        tmp_path: Directory for a stand-in program database.
    """
    db_path = tmp_path / "programs.sqlite"
    with shinka_runner.contextlib.closing(shinka_runner.sqlite3.connect(db_path)) as db:
        # Upstream's own column order: the code comes before the lineage columns.
        db.execute(
            "CREATE TABLE programs (id TEXT PRIMARY KEY, code TEXT NOT NULL, parent_id TEXT, generation INTEGER)"
        )
        db.executemany("INSERT INTO programs VALUES (?, ?, ?, ?)", [("p0", "x" * 9000, None, 0), ("p1", "y", "p0", 1)])
        db.commit()
    scorer = shinka_runner.ProgramScorer.__new__(shinka_runner.ProgramScorer)
    scorer._lock = shinka_runner.threading.Lock()
    scorer.scored = {generation: {"candidate": "x", "score": 0.1, "per_example": []} for generation in (0, 1)}
    mailbox = _Mailbox()
    reporter = shinka_runner.LineageReporter(mailbox, scorer, db_path)
    reporter.poll()
    assert [(line["candidate_id"], line["parent_id"]) for line in mailbox.lines] == [(0, None), (1, 0)]
    # A poll that read the old rows again would take this changed generation.
    with shinka_runner.contextlib.closing(shinka_runner.sqlite3.connect(db_path)) as db:
        db.execute("UPDATE programs SET generation = 99 WHERE id = 'p1'")
        db.commit()

    reporter.poll()
    assert len(mailbox.lines) == 2
    with shinka_runner.contextlib.closing(shinka_runner.sqlite3.connect(db_path)) as db:
        db.execute("INSERT INTO programs VALUES ('p2', 'z', 'p1', 2)")
        db.commit()
    scorer.scored[2] = {"candidate": "z", "score": 0.2, "per_example": []}
    reporter.poll()

    assert reporter.generation_by_id == {"p0": 0, "p1": 1, "p2": 2}
    assert [(line["candidate_id"], line["parent_id"], line["generation"]) for line in mailbox.lines[2:]] == [(2, 1, 2)]


def test_the_scorer_takes_each_request_out_of_the_queue_once_it_handles_it(tmp_path: Path) -> None:
    """Remove a handled request file, so each poll lists only new requests and its cost does not grow with the run.

    Args:
        tmp_path: Scratch folder for the queue.
    """
    scorer = shinka_runner.ProgramScorer(
        mailbox=shinka_runner.EvaluatorMailbox("testnonce", 5.0),
        queue=tmp_path / "queue",
        examples=[None],
        part_names=None,
        max_concurrency=1,
        stop_at_score=None,
    )
    answer_fields = {"correct": True, "error": None, "metrics": {}}
    scorer.score_program = lambda program_path: answer_fields  # type: ignore[method-assign]
    (tmp_path / "queue/requests/a.json").write_text(json.dumps({"program_path": "gen_1/main.md"}))

    scorer.poll()
    answer = tmp_path / "queue/responses/a.json"
    deadline = time.monotonic() + 5
    while not answer.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    scorer.stop()

    assert json.loads(answer.read_text())["correct"] is True
    assert list((tmp_path / "queue/requests").iterdir()) == []


def test_lineage_reporter_adds_the_agents_tool_calls_to_each_version(tmp_path: Path) -> None:
    """Sum every agent session spent on a generation into that version's progress line.

    Args:
        tmp_path: Directory for a stand-in program database.
    """
    db_path = tmp_path / "programs.sqlite"
    with shinka_runner.contextlib.closing(shinka_runner.sqlite3.connect(db_path)) as db:
        db.execute("CREATE TABLE programs (id TEXT, generation INTEGER, parent_id TEXT)")
        db.executemany("INSERT INTO programs VALUES (?, ?, ?)", [("p0", 0, None), ("p1", 1, "p0")])
        db.commit()
    scorer = shinka_runner.ProgramScorer.__new__(shinka_runner.ProgramScorer)
    scorer._lock = shinka_runner.threading.Lock()
    scorer.scored = {generation: {"candidate": "x", "score": 0.1, "per_example": []} for generation in (0, 1)}
    editor = shinka_runner.AgentEditor({}, tmp_path, ledger=shinka_runner.UsageLedger(None), deadline=1e12)
    editor.record_tool_calls(1, {"tool_calls": 40, "tool_cap_reached": False})
    editor.record_tool_calls(1, {"tool_calls": 100, "tool_cap_reached": True})
    mailbox = _Mailbox()

    shinka_runner.LineageReporter(mailbox, scorer, db_path, editor).poll()

    assert "tool_calls" not in mailbox.lines[0]
    assert mailbox.lines[1]["tool_calls"] == 140
    assert mailbox.lines[1]["tool_cap_reached"] is True
    assert mailbox.lines[1]["agent_sessions"] == 2


class _RepoScorer:
    """Score repository patches by checking them out on a fresh copy of the starting tree."""

    def __init__(self, chunks: list[str], root: Path) -> None:
        """Keep the shipped tree to apply each patch onto.

        Args:
            chunks: The repository snapshot chunks.
            root: Scratch folder for checkouts.
        """
        self.chunks = chunks
        self.root = root
        self.candidates: list[Any] = []
        self.total_evals = 0

    def evaluate(
        self, candidate: Any, example: Any = None, *, candidate_id: int | None = None
    ) -> tuple[float, dict[str, Any]]:
        """Score a patch by the value of ``SCALE`` it leaves in ``src/util.py``.

        Args:
            candidate: The patch the runner sent.
            example: Visible case, ignored.
            candidate_id: Version number.

        Returns:
            The score and feedback naming the file.
        """
        self.candidates.append(candidate)
        self.total_evals += 1
        checkout = repo_tree.unpack_tree(self.chunks, self.root / f"eval-{self.total_evals}")
        repo_tree.apply_patch(checkout, candidate)
        scale = int((checkout / "src/util.py").read_text().split("=")[1])
        return scale / 10, {"feedback": f"src/util.py sets SCALE={scale}"}

    def emit(self, prefix: str, payload: dict[str, Any]) -> None:
        """Drop progress lines.

        Args:
            prefix: Event family.
            payload: Progress fields.
        """


def _repo(tmp_path: Path) -> tuple[list[str], shinka_bundle.RepoBundle]:
    """Ship a two-file repository and bind a bundle to its checkout.

    Args:
        tmp_path: Scratch folder.

    Returns:
        The snapshot chunks and the bundle rules.
    """
    source = tmp_path / "source"
    (source / "src").mkdir(parents=True)
    (source / "src/app.py").write_text("from util import SCALE\n")
    (source / "src/util.py").write_text("SCALE = 2\n")
    archive = tmp_path / "tree.tgz"
    subprocess.run(["tar", "-czf", str(archive), "-C", str(source), "."], check=True)
    chunks = []
    for index, chunk in enumerate(repo_tree.archive_chunks(archive)):
        chunks.append(str(tmp_path / f"tree.{index}.b64"))
        Path(chunks[-1]).write_text(chunk)
    bundle = shinka_bundle.RepoBundle.prepare({"chunks": chunks, "editable_paths": ["src"]}, "", tmp_path / "checkout")
    return chunks, bundle


def test_repository_versions_reach_the_scorer_as_patches(tmp_path: Path) -> None:
    """Materialize each bundle program into the run's patch and score it like any repository engine.

    Args:
        tmp_path: Scratch folder.
    """
    chunks, bundle = _repo(tmp_path)
    mailbox = _RepoScorer(chunks, tmp_path / "evals")
    scorer = shinka_runner.ProgramScorer(
        mailbox=mailbox,
        queue=tmp_path / "queue",
        examples=[None],
        part_names=None,
        max_concurrency=1,
        stop_at_score=None,
        bundle=bundle,
    )
    program = tmp_path / "results" / "gen_4" / "main.md"
    program.parent.mkdir(parents=True)
    program.write_text(shinka_bundle.encode_bundle({"src/util.py": "SCALE = 7\n"}))

    answer = scorer.score_program(str(program))

    assert answer["correct"] is True
    assert answer["metrics"]["combined_score"] == pytest.approx(0.7)
    assert repo_tree.patch_paths(mailbox.candidates[0]) == ["src/util.py"]
    assert scorer.best is not None
    assert scorer.best["best_candidate"] == mailbox.candidates[0]
    program.write_text(shinka_bundle.encode_bundle({"../escape": "x\n"}))
    assert "not a path inside the repository" in scorer.score_program(str(program))["error"]


def test_a_case_with_no_score_fails_the_program_with_its_run_failure(tmp_path: Path, monkeypatch: Any) -> None:
    """A null parent score fails the program, and its "Run failed" text reaches the proposer (SKY-751).

    Args:
        tmp_path: Working folder with the parent's response files.
        monkeypatch: Working folder and fixed request ids.
    """
    monkeypatch.chdir(tmp_path)
    ids = iter(["r0", "r1"])
    monkeypatch.setattr(shinka_runner.uuid, "uuid4", lambda: SimpleNamespace(hex=next(ids)))
    (tmp_path / "rpc").mkdir()
    (tmp_path / "rpc" / "r0.json").write_text(json.dumps({"score": 0.5, "info": {"feedback": "fine"}}))
    failure = {"feedback": "Run failed: Traceback: boom", "status": "run_failed"}
    (tmp_path / "rpc" / "r1.json").write_text(json.dumps({"score": None, "info": failure}))
    scorer = shinka_runner.ProgramScorer(
        mailbox=shinka_runner.EvaluatorMailbox("testnonce", 5.0),
        queue=tmp_path / "queue",
        examples=[{"id": "a"}, {"id": "b"}],
        part_names=None,
        max_concurrency=1,
        stop_at_score=None,
    )
    program = tmp_path / "results" / "gen_2" / "main.md"
    program.parent.mkdir(parents=True)
    program.write_text(shinka_runner.pack_program("Answer."))

    answer = scorer.score_program(str(program))

    assert answer["correct"] is False
    assert answer["error"] == "1 of 2 case(s) have no score."
    assert answer["metrics"]["text_feedback"] == "Case 1 has no score:\nRun failed: Traceback: boom"
    assert scorer.best is None
    assert scorer.scored == {}


def _two_case_answer(
    tmp_path: Path, monkeypatch: Any, second: dict[str, Any], stop_at_score: float | None = None
) -> tuple[Any, dict[str, Any]]:
    """Score one program on two cases: the first scores 0.5, the second gets ``second`` from the parent.

    Args:
        tmp_path: Working folder with the parent's response files.
        monkeypatch: Working folder and fixed request ids.
        second: The parent's response for the second case.
        stop_at_score: Score that stops the run, if any.

    Returns:
        The program scorer and its answer for the program.
    """
    monkeypatch.chdir(tmp_path)
    ids = iter(["r0", "r1"])
    monkeypatch.setattr(shinka_runner.uuid, "uuid4", lambda: SimpleNamespace(hex=next(ids)))
    (tmp_path / "rpc").mkdir()
    (tmp_path / "rpc" / "r0.json").write_text(json.dumps({"score": 0.5, "info": {"feedback": "fine"}}))
    (tmp_path / "rpc" / "r1.json").write_text(json.dumps(second))
    scorer = shinka_runner.ProgramScorer(
        mailbox=shinka_runner.EvaluatorMailbox("testnonce", 5.0),
        queue=tmp_path / "queue",
        examples=[{"id": "a"}, {"id": "b"}],
        part_names=None,
        max_concurrency=1,
        stop_at_score=stop_at_score,
    )
    program = tmp_path / "results" / "gen_2" / "main.md"
    program.parent.mkdir(parents=True)
    program.write_text(shinka_runner.pack_program("Answer."))
    return scorer, scorer.score_program(str(program))


def test_a_metric_error_case_keeps_the_programs_mean_over_its_scored_cases(tmp_path: Path, monkeypatch: Any) -> None:
    """A metric error is the metric's fault, so the program keeps its mean over the scored cases (SKY-367, SKY-751).

    Args:
        tmp_path: Working folder with the parent's response files.
        monkeypatch: Working folder and fixed request ids.
    """
    scorer, answer = _two_case_answer(tmp_path, monkeypatch, {"score": None, "info": {"error": "KeyError: 'x'"}})

    assert answer["correct"] is True
    assert answer["metrics"]["combined_score"] == 0.5
    assert answer["metrics"]["private"] == {"case_0": 0.5}
    assert "Case 1 has no score: the metric failed:\nKeyError: 'x'" in answer["metrics"]["text_feedback"]
    assert scorer.best is None
    assert scorer.scored[2]["per_example"] == [("0", 0.5)]


def test_a_metric_error_version_cannot_reach_stop_at_score(tmp_path: Path, monkeypatch: Any) -> None:
    """A version with a metric error keeps its mean for selection, but it cannot stop the run or become best (SKY-751).

    Args:
        tmp_path: Working folder with the parent's response files.
        monkeypatch: Working folder and fixed request ids.
    """
    second = {"score": None, "info": {"error": "KeyError: 'x'"}}
    scorer, answer = _two_case_answer(tmp_path, monkeypatch, second, stop_at_score=0.4)

    assert answer["correct"] is True
    assert answer["metrics"]["combined_score"] == 0.5
    assert not scorer.target_reached.is_set()
    assert scorer.best is None
    assert scorer.failed_versions == 1


def test_a_program_the_metric_fails_on_every_case_has_no_score(tmp_path: Path, monkeypatch: Any) -> None:
    """With no scored case, a metric error leaves the program with no mean, so it fails (SKY-751).

    Args:
        tmp_path: Working folder with the parent's response files.
        monkeypatch: Working folder and fixed request ids.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(shinka_runner.uuid, "uuid4", lambda: SimpleNamespace(hex="r0"))
    (tmp_path / "rpc").mkdir()
    (tmp_path / "rpc" / "r0.json").write_text(json.dumps({"score": None, "info": {"error": "KeyError: 'x'"}}))
    scorer = shinka_runner.ProgramScorer(
        mailbox=shinka_runner.EvaluatorMailbox("testnonce", 5.0),
        queue=tmp_path / "queue",
        examples=[None],
        part_names=None,
        max_concurrency=1,
        stop_at_score=None,
    )
    program = tmp_path / "results" / "gen_1" / "main.md"
    program.parent.mkdir(parents=True)
    program.write_text(shinka_runner.pack_program("Answer."))

    answer = scorer.score_program(str(program))

    assert answer["correct"] is False
    assert answer["error"] == "1 of 1 case(s) have no score."
    assert scorer.best is None


class _NoProgramRunner:
    """Stand in for upstream's runner: it finishes without submitting one program."""

    def __init__(self, **_: Any) -> None:
        """Take upstream's keyword arguments.

        Args:
            **_: Upstream's constructor arguments, ignored.
        """

    def run(self) -> None:
        """Finish at once, as when upstream submits no generation."""


class _FailedProgramRunner(_NoProgramRunner):
    """Stand in for upstream's runner: it submits the seed and waits for the scorer's answer."""

    def run(self) -> None:
        """Queue the seed as generation 0 and wait for the scorer's answer, as ``evaluate.py`` does."""
        program = Path("shinka-run/results/gen_0/main.md")
        program.parent.mkdir(parents=True)
        program.write_text(shinka_runner.pack_program("Answer."), encoding="utf-8")
        queue = Path("shinka-run/eval-queue")
        (queue / "requests" / "q0.json").write_text(json.dumps({"program_path": str(program.resolve())}))
        deadline = time.monotonic() + 4.0
        while not (queue / "responses" / "q0.json").exists() and time.monotonic() < deadline:
            time.sleep(0.05)


_RUN_FAILED_RESPONSE = {"score": None, "info": {"feedback": "Run failed: Traceback: boom", "status": "run_failed"}}


def _execute_with(
    tmp_path: Path, monkeypatch: Any, runner: type, responses: Sequence[dict[str, Any]] = (_RUN_FAILED_RESPONSE,)
) -> dict[str, Any]:
    """Run ``execute`` with one case per parent response and ``runner`` in place of upstream's runner.

    Args:
        tmp_path: Working folder for the run.
        monkeypatch: Replaces upstream's runner and configuration classes.
        runner: The stand-in runner class.
        responses: The parent's response for each case of the seed.

    Returns:
        The run's result document.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(shinka_runner, "ShinkaEvolveRunner", runner)
    for name in ("EvolutionConfig", "DatabaseConfig", "LocalJobConfig"):
        monkeypatch.setattr(shinka_runner, name, SimpleNamespace)
    monkeypatch.setattr(shinka_runner.UsageLedger, "install", lambda self: None)
    ids = iter([f"r{index}" for index in range(len(responses))])
    monkeypatch.setattr(shinka_runner.uuid, "uuid4", lambda: SimpleNamespace(hex=next(ids)))
    (tmp_path / "rpc").mkdir()
    for index, response in enumerate(responses):
        (tmp_path / "rpc" / f"r{index}.json").write_text(json.dumps(response))
    train_set = [{"id": str(index)} for index in range(len(responses))]
    payload = _payload(
        nonce="testnonce",
        timeout_seconds=5.0,
        task={"objective": "Be concise.", "background": "", "seed_candidate": "Answer.", "train_set": train_set},
    )
    return shinka_runner.execute(payload)


def test_a_run_where_every_scored_version_fails_keeps_the_seed_and_says_why(
    tmp_path: Path, monkeypatch: Any, capfd: pytest.CaptureFixture[str]
) -> None:
    """When every scored version fails a case, the run keeps the seed with no score and logs why (SKY-751).

    Args:
        tmp_path: Working folder for the run.
        monkeypatch: Replaces upstream's runner and configuration classes.
        capfd: Captures the framed log lines.
    """
    document = _execute_with(tmp_path, monkeypatch, _FailedProgramRunner)

    assert "error" not in document
    assert (document["best_candidate"], document["best_score"]) == ("Answer.", None)
    lines = [line for line in capfd.readouterr().out.splitlines() if line.startswith("APP_NATIVE_LOG testnonce ")]
    assert [json.loads(line.split(" ", 2)[2])["message"] for line in lines] == [
        "No version has a score: each one failed on at least one case. The run keeps the starting version."
    ]


def test_a_run_whose_every_version_has_a_metric_error_keeps_the_seed_and_says_why(
    tmp_path: Path, monkeypatch: Any, capfd: pytest.CaptureFixture[str]
) -> None:
    """A metric error on one case leaves the seed with no score, and the run still finishes (SKY-751).

    Args:
        tmp_path: Working folder for the run.
        monkeypatch: Replaces upstream's runner and configuration classes.
        capfd: Captures the framed log lines.
    """
    responses = [{"score": 0.5, "info": {"feedback": "fine"}}, {"score": None, "info": {"error": "KeyError: 'x'"}}]

    document = _execute_with(tmp_path, monkeypatch, _FailedProgramRunner, responses)

    assert "error" not in document
    assert (document["best_candidate"], document["best_score"]) == ("Answer.", None)
    lines = [line for line in capfd.readouterr().out.splitlines() if line.startswith("APP_NATIVE_LOG testnonce ")]
    assert [json.loads(line.split(" ", 2)[2])["message"] for line in lines] == [
        "No version has a score: each one failed on at least one case. The run keeps the starting version."
    ]


def test_a_metric_error_version_stays_in_the_version_tree_as_a_parent(tmp_path: Path, monkeypatch: Any) -> None:
    """The lineage reporter sends a metric-error version, so its child's parent resolves (SKY-751).

    Args:
        tmp_path: Working folder with the parent's response files.
        monkeypatch: Working folder and fixed request ids.
    """
    scorer, _ = _two_case_answer(tmp_path, monkeypatch, {"score": None, "info": {"error": "KeyError: 'x'"}})
    scorer.scored[3] = {"candidate": "y", "score": 0.7, "per_example": [("0", 0.7), ("1", 0.7)]}
    db_path = tmp_path / "programs.sqlite"
    with shinka_runner.contextlib.closing(shinka_runner.sqlite3.connect(db_path)) as db:
        db.execute("CREATE TABLE programs (id TEXT, generation INTEGER, parent_id TEXT)")
        db.executemany("INSERT INTO programs VALUES (?, ?, ?)", [("p2", 2, None), ("p3", 3, "p2")])
        db.commit()
    mailbox = _Mailbox()

    shinka_runner.LineageReporter(mailbox, scorer, db_path).poll()

    assert [(line["candidate_id"], line["parent_id"]) for line in mailbox.lines] == [(2, None), (3, 2)]
    assert mailbox.lines[0]["per_example"] == [("0", 0.5)]


def test_a_run_that_scores_no_program_at_all_fails_with_an_error(
    tmp_path: Path, monkeypatch: Any, capfd: pytest.CaptureFixture[str]
) -> None:
    """When upstream submits no program for scoring, the run fails explicitly, as Best-of-N does (SKY-751).

    Args:
        tmp_path: Working folder for the run.
        monkeypatch: Replaces upstream's runner and configuration classes.
        capfd: Captures the framed log lines.
    """
    document = _execute_with(tmp_path, monkeypatch, _NoProgramRunner)

    assert document["error"] == "ShinkaEvolve finished without scoring any version."
    assert "best_candidate" not in document
    assert "No version has a score" not in capfd.readouterr().out


def test_repository_mode_swaps_prompts_edits_and_novelty_input(tmp_path: Path, monkeypatch: Any) -> None:
    """Route upstream's prompt sampling, patch application and embeddings through the bundle.

    Args:
        tmp_path: Scratch folder.
        monkeypatch: Replaces upstream's async runner module.
    """
    _, bundle = _repo(tmp_path)
    embedded: list[str] = []

    async def fake_embedding(exec_fname: str, client: Any, max_chars: int = 10000) -> tuple[Any, float]:
        """Record what upstream would embed.

        Args:
            exec_fname: File to embed.
            client: Embedding client.
            max_chars: Input cap.

        Returns:
            A stand-in vector and no cost.
        """
        embedded.append(exec_fname)
        return [1.0], 0.0

    upstream = SimpleNamespace(apply_patch_async=None, get_code_embedding_async=fake_embedding)
    monkeypatch.setattr(shinka_runner, "shinka_async_runner", upstream)
    sampler = SimpleNamespace(
        patch_types=["diff", "full", "cross"],
        patch_type_probs=[1.0, 0.0, 0.0],
        task_sys_msg="TASK",
        use_text_feedback=True,
    )
    shinka_runner.install_repo_mode(bundle, SimpleNamespace(prompt_sampler=sampler))
    parent = SimpleNamespace(code=shinka_bundle.encode_bundle({}), combined_score=0.2, text_feedback="")

    system, user, kind = sampler.sample(parent, [], [], None)
    assert kind == "diff"
    assert system.startswith("TASK")
    assert "src/util.py" in user
    assert sampler.sample_fix(parent, [])[2] == "fix"

    response = '<FILE path="src/util.py">\n<<<<<<< SEARCH\nSCALE = 2\n=======\nSCALE = 5\n>>>>>>> REPLACE\n</FILE>'
    code, applied, output, error, _, _ = asyncio.run(
        upstream.apply_patch_async(parent.code, response, str(tmp_path / "gen_1"), patch_type="full")
    )
    assert error is None
    assert applied == 1
    assert shinka_bundle.decode_bundle(code) == {"src/util.py": "SCALE = 5\n"}

    vector, _ = asyncio.run(upstream.get_code_embedding_async(str(output), None))
    assert vector == [1.0]
    embedded_text = Path(embedded[0]).read_text()
    assert "+SCALE = 5" in embedded_text
    assert "app.py" not in embedded_text
    empty = tmp_path / "gen_0" / "main.md"
    empty.parent.mkdir()
    empty.write_text(shinka_bundle.encode_bundle({}))
    assert asyncio.run(upstream.get_code_embedding_async(str(empty), None)) == (None, 0.0)


def test_repository_config_uses_the_file_aware_system_message(tmp_path: Path) -> None:
    """Give repository runs the bundle system message and room for file-opening rounds.

    Args:
        tmp_path: Directory upstream would write to.
    """
    config = shinka_runner.build_config(_payload(), str(tmp_path), 1, repo=True)["evolution"]
    text = shinka_runner.build_config(_payload(), str(tmp_path), 1)["evolution"]

    assert config["task_sys_msg"].startswith(shinka_bundle.REPO_SYSTEM_MESSAGE)
    assert config["max_patch_attempts"] == text["max_patch_attempts"] + shinka_bundle.OPEN_ROUNDS


_EDITOR_MODEL = "local/claude-test@https://gw.example/v1?api_key_env=APP_SHINKA_KEY_0"


def _editor_config() -> dict[str, Any]:
    """Build the ``shinka_editor`` payload block for one optimization model.

    Returns:
        The block, with a launch reading ``APP_SHINKA_KEY_0``.
    """
    launch = {
        "run_command": "pi",
        "files": {".app/pi/models.json": "{}"},
        "env": {},
        "output_format": "pi",
        "model": "claude-test",
        "key_env": "APP_SHINKA_KEY_0",
        "price": {"input": 0.0, "output": 0.0},
    }
    return {"max_tool_calls": 7, "launches": [launch]}


class _Selection:
    """Record the bandit updates the editor makes."""

    def __init__(self) -> None:
        """Start with no updates."""
        self.submitted: list[str] = []
        self.costs: list[tuple[str, float]] = []

    def update_submitted(self, model_name: str) -> None:
        """Record a submission.

        Args:
            model_name: Arm submitted.
        """
        self.submitted.append(model_name)

    def update_cost(self, arm: str, cost: float) -> None:
        """Record a cost.

        Args:
            arm: Arm charged.
            cost: Its cost.
        """
        self.costs.append((arm, cost))


def _fake_session(edit: Callable[[Path], None], calls: list[dict[str, Any]], *, returncode: int = 0) -> Any:
    """Stand in for ``harness_bridge.run_session``: write the launch files, then apply an edit.

    Args:
        edit: Change the fake agent makes in its workspace.
        calls: Receives each call's launch, prompt and key.
        returncode: Exit status the session reports.

    Returns:
        The fake.
    """

    def run(proposer: dict[str, Any], *, workspace: Path, prompt: str, api_key: str, **_: Any) -> Any:
        """Edit the workspace like an agent would.

        Args:
            proposer: The launch.
            workspace: The scratch directory.
            prompt: The agent's task.
            api_key: The route key.
            **_: Other run options.

        Returns:
            A finished session.
        """
        calls.append({"proposer": proposer, "prompt": prompt, "api_key": api_key})
        for relative, content in proposer["files"].items():
            (workspace / relative).parent.mkdir(parents=True, exist_ok=True)
            (workspace / relative).write_text(content)
        edit(workspace)
        return shinka_runner.harness_bridge.SessionOutcome(
            text="Done.\nNAME: sharper_rules\nDESCRIPTION: Tightened the rules.",
            stdout="",
            stderr="boom" if returncode else "",
            returncode=returncode,
            usage={"input_tokens": 120, "output_tokens": 30},
            cost_usd=0.25,
            duration_seconds=1.0,
            tool_calls=3,
        )

    return run


def _install_editor(tmp_path: Path, bundle: Any = None, part_names: list[str] | None = None) -> tuple[Any, Any, Any]:
    """Install the agent editor on a stand-in upstream runner.

    Args:
        tmp_path: Run folder.
        bundle: Repository bundle, or ``None`` for a text target.
        part_names: The text target's part names.

    Returns:
        ``(runner, selection, ledger)``.
    """
    ledger = shinka_runner.UsageLedger(None)
    editor = shinka_runner.AgentEditor(
        _editor_config(), tmp_path, ledger=ledger, deadline=1e12, part_names=part_names, bundle=bundle
    )
    sampler = SimpleNamespace(
        task_sys_msg="TASK",
        use_text_feedback=True,
        sample=lambda **_: ("TASK\nANSWER FORMAT", "Current program\n\n# Task\nRewrite it.", "full"),
    )
    selection = _Selection()
    runner = SimpleNamespace(
        llm=SimpleNamespace(get_kwargs=lambda model_sample_probs=None: {"model_name": _EDITOR_MODEL}),
        llm_selection=selection,
        prompt_sampler=sampler,
        results_dir=str(tmp_path / "results"),
    )
    shinka_runner.install_agent_editor(editor, runner)
    return runner, selection, ledger


def test_a_constrained_agent_edit_outside_the_paths_is_an_illegal_action(tmp_path: Path, monkeypatch: Any) -> None:
    """Under a constraints file, refuse the agent's version for a file outside the editable paths, and log it.

    The refused version is never written or scored, so it uses no evaluation.

    Args:
        tmp_path: Run folder.
        monkeypatch: Replaces the harness session.
    """
    chunks, _ = _repo(tmp_path)
    blocks: list[str] = []
    bundle = shinka_bundle.RepoBundle.prepare(
        {"chunks": chunks, "editable_paths": ["src"], "constrained": True},
        "",
        tmp_path / "constrained-checkout",
        on_block=blocks.append,
    )

    def edit(workspace: Path) -> None:
        """Change an editable file and a file outside the editable paths.

        Args:
            workspace: Scratch checkout.
        """
        (workspace / "src/util.py").write_text("SCALE = 9\n")
        (workspace / "app.py").write_text("tampered\n")

    monkeypatch.setattr(shinka_runner.harness_bridge, "run_session", _fake_session(edit, []))
    runner, _, _ = _install_editor(tmp_path, bundle=bundle)
    parent = SimpleNamespace(code=shinka_bundle.encode_bundle({}))

    patch, meta, success = asyncio.run(runner._run_patch_async(parent, [], [], 2))

    assert (patch, success) == (None, False)
    assert meta["last_error_msg"] == repo_tree.ILLEGAL_ACTION_MESSAGE
    assert len(blocks) == 1
    assert "app.py" in blocks[0]
    assert not (tmp_path / "results" / "gen_2").exists()


def test_rule_blocker_writes_a_rule_block_run_event(capfd: pytest.CaptureFixture[str]) -> None:
    """Frame a path-rule block as a structured log line the parent forwards as ``rule.block``.

    Args:
        capfd: Captures the line the blocker prints.
    """
    shinka_runner.rule_blocker(shinka_runner.EvaluatorMailbox("testnonce", 1.0))("Cannot change 'app.py'.")

    lines = [line for line in capfd.readouterr().out.splitlines() if line.startswith("APP_NATIVE_LOG testnonce ")]
    assert len(lines) == 1
    record = json.loads(lines[0].split(" ", 2)[2])
    assert record["event"] == repo_tree.RULE_BLOCK_EVENT
    assert record["source"] == "engine"
    assert record["fields"] == {"rule": repo_tree.PATH_RULE, "reason": "Cannot change 'app.py'."}


def test_refusal_logger_writes_a_proposer_refusal_run_event(capfd: pytest.CaptureFixture[str]) -> None:
    """Frame a hidden-path refusal as a run-log line with the tool and kind only.

    Args:
        capfd: Captures the line the logger prints.
    """
    shinka_runner.refusal_logger(shinka_runner.EvaluatorMailbox("testnonce", 1.0))({"tool": "read", "kind": "config"})

    lines = [line for line in capfd.readouterr().out.splitlines() if line.startswith("APP_NATIVE_LOG testnonce ")]
    assert len(lines) == 1
    record = json.loads(lines[0].split(" ", 2)[2])
    assert record["event"] == "proposer.refusal"
    assert record["level"] == "WARNING"
    assert record["fields"] == {"tool": "read", "kind": "config"}
    assert "optimize.toml" not in lines[0]


def test_agent_editor_guards_sessions_and_logs_their_refusals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pass the bundle's hidden hashes to each session and send its refusals to the run log; the edit still lands.

    Args:
        tmp_path: Run folder.
        monkeypatch: Replaces the harness session.
    """
    chunks, _ = _repo(tmp_path)
    refusals: list[dict[str, str]] = []
    bundle = shinka_bundle.RepoBundle.prepare(
        {
            "chunks": chunks,
            "editable_paths": ["src"],
            "hidden_hashes": shinka_runner.harness_bridge.hidden_hashes(["optimize.toml", "eval/metric.py"]),
        },
        "",
        tmp_path / "guarded-checkout",
        on_refusal=refusals.append,
    )
    calls: list[dict[str, Any]] = []
    fake = _fake_session(lambda workspace: (workspace / "src/util.py").write_text("SCALE = 7\n"), calls)

    def refused(proposer: dict[str, Any], **kwargs: Any) -> Any:
        """Run the fake session and report one refused read.

        Args:
            proposer: The launch.
            **kwargs: Other run options.

        Returns:
            The finished session with a refusal.
        """
        outcome = fake(proposer, **kwargs)
        outcome.refusals = [{"tool": "read", "kind": "config"}]
        return outcome

    monkeypatch.setattr(shinka_runner.harness_bridge, "run_session", refused)
    runner, _, _ = _install_editor(tmp_path, bundle=bundle)
    parent = SimpleNamespace(code=shinka_bundle.encode_bundle({}))

    patch, _, success = asyncio.run(runner._run_patch_async(parent, [], [], 1))

    assert calls[0]["proposer"]["hidden_hashes"] == shinka_runner.harness_bridge.hidden_hashes(
        ["optimize.toml", "eval/metric.py"]
    )
    assert refusals == [{"tool": "read", "kind": "config"}]
    assert success is True
    assert patch is not None


def test_agent_editor_adds_no_guard_without_a_hidden_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Leave the launch unguarded when the run hides no path.

    Args:
        tmp_path: Run folder.
        monkeypatch: Replaces the harness session.
    """
    _, bundle = _repo(tmp_path)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        shinka_runner.harness_bridge,
        "run_session",
        _fake_session(lambda workspace: (workspace / "src/util.py").write_text("SCALE = 7\n"), calls),
    )
    runner, _, _ = _install_editor(tmp_path, bundle=bundle)

    asyncio.run(runner._run_patch_async(SimpleNamespace(code=shinka_bundle.encode_bundle({})), [], [], 1))

    assert "hidden_hashes" not in calls[0]["proposer"]


def test_config_turns_the_patch_mix_into_full_rewrites_for_the_agent_editor(tmp_path: Path) -> None:
    """Sample only full-rewrite prompts in agent mode, whatever patch mix the settings hold.

    Args:
        tmp_path: Results directory.
    """
    settings = BlackboxShinkaSettings(editor="agent").model_dump()
    evolution = shinka_runner.build_config(_payload(shinka=settings), str(tmp_path), 1)["evolution"]

    assert evolution["patch_types"] == ["full"]
    assert evolution["patch_type_probs"] == [1.0]


def test_editor_setting_accepts_only_the_two_modes() -> None:
    """Default to upstream's single call and refuse an unknown editor."""
    assert BlackboxShinkaSettings().editor == "single_call"
    assert BlackboxShinkaSettings(editor="agent").editor == "agent"
    with pytest.raises(ValueError):
        BlackboxShinkaSettings(editor="swarm")


def test_agent_prompt_swaps_the_answer_format_for_editing_in_place(monkeypatch: Any) -> None:
    """Drop upstream's full-rewrite format and task, and ask the agent to edit the file itself.

    Args:
        monkeypatch: Supplies upstream's answer formats.
    """
    monkeypatch.setattr(shinka_runner, "FULL_SYS_FORMATS", ["\nRewrite the program. Use <NAME> and <CODE>."])
    system = "TASK\n\nRewrite the program. Use <NAME> and <CODE>."
    user = "Here is the program.\n\nFeedback: too long.\n\n# Task\n\nRewrite the program."

    prompt = shinka_runner.agent_prompt(system, user, repo=False)

    assert "<CODE>" not in prompt
    assert "Rewrite the program." not in prompt
    assert "Feedback: too long." in prompt
    assert prompt.startswith("TASK")
    assert "program.md" in prompt
    assert "NAME:" in prompt


def test_agent_answer_names_and_describes_the_edit() -> None:
    """Read the closing NAME and DESCRIPTION lines, tolerating their absence."""
    assert shinka_runner.parse_agent_answer("ok\n**NAME:** `tighter`\nDESCRIPTION: Cut filler.\nMore.") == (
        "tighter",
        "Cut filler.\nMore.",
    )
    assert shinka_runner.parse_agent_answer(None) == (None, None)
    assert shinka_runner.parse_agent_answer("no markers") == (None, None)


def test_agent_editor_writes_a_text_version_from_the_agents_edits(tmp_path: Path, monkeypatch: Any) -> None:
    """Run the agent on the parent's file and hand upstream the edited program as a full replacement.

    Args:
        tmp_path: Run folder.
        monkeypatch: Replaces the harness session and the route key.
    """
    monkeypatch.setenv("APP_SHINKA_KEY_0", "route-key")
    calls: list[dict[str, Any]] = []

    def edit(workspace: Path) -> None:
        """Rewrite the system part.

        Args:
            workspace: Scratch directory.
        """
        program = workspace / "program.md"
        program.write_text(program.read_text().replace("\nold\n", "\nnew rules\n"))

    monkeypatch.setattr(shinka_runner.harness_bridge, "run_session", _fake_session(edit, calls))
    runner, selection, ledger = _install_editor(tmp_path, part_names=["system", "user"])
    parent = SimpleNamespace(code=shinka_runner.pack_program({"system": "old", "user": "keep"}))

    patch, meta, success = asyncio.run(runner._run_patch_async(parent, [], [], 3, "notes"))

    assert success is True
    assert "+new rules" in patch
    assert "-old" in patch
    written = (tmp_path / "results" / "gen_3" / "main.md").read_text()
    assert shinka_runner.unpack_program(written, ["system", "user"]) == {"system": "new rules", "user": "keep"}
    assert meta["patch_type"] == "agent"
    assert meta["patch_name"] == "sharper_rules"
    assert meta["patch_description"] == "Tightened the rules."
    assert meta["api_costs"] == pytest.approx(0.25)
    assert meta["tool_calls"] == 3
    assert meta["model_name"] == _EDITOR_MODEL
    assert meta["num_applied"] == 1
    assert selection.submitted == [_EDITOR_MODEL]
    assert selection.costs == [(_EDITOR_MODEL, 0.25)]
    assert ledger.usage_by_model["claude-test"]["total_tokens"] == 150
    assert calls[0]["api_key"] == "route-key"
    assert calls[0]["proposer"]["max_tool_calls"] == 7
    assert calls[0]["proposer"]["instructions_file"] is None
    assert "Current program" in calls[0]["prompt"]
    assert not list((tmp_path / "agent-edits").iterdir())


@pytest.mark.parametrize("returncode", [0, 1])
def test_agent_editor_reports_no_version_for_an_unchanged_or_failed_session(
    tmp_path: Path, monkeypatch: Any, returncode: int
) -> None:
    """Hand upstream a failed attempt when the agent changed nothing or its session failed.

    Args:
        tmp_path: Run folder.
        monkeypatch: Replaces the harness session.
        returncode: Exit status of the fake session.
    """
    monkeypatch.setattr(
        shinka_runner.harness_bridge, "run_session", _fake_session(lambda _: None, [], returncode=returncode)
    )
    runner, selection, _ = _install_editor(tmp_path)
    parent = SimpleNamespace(code=shinka_runner.pack_program("Answer briefly."))

    patch, meta, success = asyncio.run(runner._run_patch_async(parent, [], [], 1))

    assert (patch, success) == (None, False)
    assert meta["patch_type"] == "agent"
    assert meta["api_costs"] == pytest.approx(0.25)
    assert ("unchanged" if returncode == 0 else "boom") in meta["last_error_msg"]
    assert selection.costs == [(_EDITOR_MODEL, 0.25)]
    assert not (tmp_path / "results" / "gen_1").exists()


def test_agent_editor_runs_on_a_real_checkout_of_a_repository_version(tmp_path: Path, monkeypatch: Any) -> None:
    """Lay the parent bundle out over the repository, and keep only allowed file changes.

    Args:
        tmp_path: Run folder.
        monkeypatch: Replaces the harness session.
    """
    _, bundle = _repo(tmp_path)
    seen: dict[str, str] = {}

    def edit(workspace: Path) -> None:
        """Change the editable file, a file outside the editable paths, and add one.

        Args:
            workspace: Scratch checkout.
        """
        seen["util"] = (workspace / "src/util.py").read_text()
        (workspace / "src/util.py").write_text("SCALE = 9\n")
        (workspace / "src/extra.py").write_text("EXTRA = 1\n")
        (workspace / "app.py").write_text("tampered\n")

    monkeypatch.setattr(shinka_runner.harness_bridge, "run_session", _fake_session(edit, []))
    runner, _, _ = _install_editor(tmp_path, bundle=bundle)
    parent = SimpleNamespace(code=shinka_bundle.encode_bundle({"src/util.py": "SCALE = 4\n"}))

    patch, meta, success = asyncio.run(runner._run_patch_async(parent, [], [], 2))

    assert success is True
    assert seen["util"] == "SCALE = 4\n"
    files = shinka_bundle.decode_bundle((tmp_path / "results" / "gen_2" / "main.md").read_text())
    assert files == {"src/util.py": "SCALE = 9\n", "src/extra.py": "EXTRA = 1\n"}
    assert "+SCALE = 9" in patch
    assert meta["num_applied"] == 2
    assert not list((tmp_path / "agent-edits").iterdir())


def test_runner_git_refuses_a_bare_repository_layout(
    planted_bare_repo: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runner's git helper does not treat a bare-repository layout as a repository (SKY-812).

    Args:
        planted_bare_repo: Fixture.
        monkeypatch: Pytest fixture.
    """
    repo, _ = planted_bare_repo
    with pytest.raises(RuntimeError, match=r"safe\.bareRepository"):
        shinka_runner._git(repo, "rev-parse", "--git-dir")
    monkeypatch.setattr(repo_tree, "SAFE_GIT", ())
    assert shinka_runner._git(repo, "rev-parse", "--git-dir").strip() == "."


class _QueryResult:
    """Stand in for upstream's ``QueryResult``: keep every field it is built with."""

    def __init__(self, **fields: Any) -> None:
        """Keep the fields.

        Args:
            **fields: The result's fields.
        """
        self.__dict__.update(fields)


def _connected_mailbox(
    reply: Callable[[dict[str, Any]], dict[str, Any]],
) -> tuple[shinka_runner.EvaluatorMailbox, list[dict[str, Any]]]:
    """Connect a mailbox to a fake worker that answers each request on the mailbox's reply stream.

    Args:
        reply: Builds the worker's reply fields for one request.

    Returns:
        The mailbox and the requests it sent.
    """
    mailbox = shinka_runner.EvaluatorMailbox("testnonce", 5.0)
    read_end, write_end = os.pipe()
    replies = os.fdopen(write_end, "w", encoding="utf-8")
    mailbox.listen(os.fdopen(read_end, encoding="utf-8"))
    sent: list[dict[str, Any]] = []

    def emit(prefix: str, payload: dict[str, Any]) -> None:
        """Answer one request the way the worker does: one JSON line with the request's id.

        Args:
            prefix: The event family.
            payload: The request.
        """
        assert prefix == "APP_NATIVE_RPC "
        sent.append(payload)
        replies.write(json.dumps({"id": payload["id"], **reply(payload)}) + "\n")
        replies.flush()

    mailbox.emit = emit  # type: ignore[method-assign]
    return mailbox, sent


def test_every_upstream_query_becomes_one_worker_model_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """Send upstream's sync and async queries to the worker's model and build upstream's result from the reply.

    Args:
        monkeypatch: Replaces upstream's ``shinka.llm.llm`` module and ``QueryResult``.
    """
    llm = SimpleNamespace(query=None, query_async=None)
    monkeypatch.setattr(shinka_runner, "shinka_llm", llm)
    monkeypatch.setattr(shinka_runner, "QueryResult", _QueryResult)
    mailbox, sent = _connected_mailbox(lambda request: {"text": f"reply {len(request['messages'])}"})
    shinka_runner.install_worker_models(mailbox)
    history = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "answer"}]

    first = llm.query(model_name="local/m", msg="improve", system_msg="system", msg_history=history, temperature=0.5)
    second = asyncio.run(llm.query_async(model_name="local/m", msg="again", system_msg="", msg_history=[]))

    assert [request["kind"] for request in sent] == ["model", "model"]
    assert sent[0]["messages"] == [
        {"role": "system", "content": "system"},
        *history,
        {"role": "user", "content": "improve"},
    ]
    assert sent[1]["messages"] == [{"role": "user", "content": "again"}]
    assert (first.content, first.input_tokens, first.output_tokens, first.kwargs) == (
        "reply 4",
        0,
        0,
        {"temperature": 0.5},
    )
    assert first.new_msg_history[-2:] == [
        {"role": "user", "content": "improve"},
        {"role": "assistant", "content": "reply 4"},
    ]
    assert second.content == "reply 1"


def test_concurrent_upstream_queries_reach_the_worker_in_creation_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """Send gathered async queries in the order upstream creates them, every run, so a replay never diverges.

    Upstream gathers meta-note queries concurrently. The worker commits each
    model reply as one step by position, so a resume replays them only if the
    order is the same every run.

    Args:
        monkeypatch: Replaces upstream's ``shinka.llm.llm`` module and ``QueryResult``.
    """
    llm = SimpleNamespace(query=None, query_async=None)
    monkeypatch.setattr(shinka_runner, "shinka_llm", llm)
    monkeypatch.setattr(shinka_runner, "QueryResult", _QueryResult)
    draws = random.Random(7)

    async def gathered() -> list[Any]:
        """Start three queries at once, the way upstream's meta summarizer does.

        Returns:
            The query results.
        """
        queries = (llm.query_async(model_name="local/m", msg=word, system_msg="", msg_history=[]) for word in "abc")
        return await asyncio.wait_for(asyncio.gather(*queries), timeout=10)

    committed: list[tuple[str, str]] | None = None
    # Each run leaves one idle reply-reader thread, so the run count stays small.
    for _ in range(10):
        mailbox, sent = _connected_mailbox(lambda request: {"text": request["messages"][-1]["content"]})
        send = mailbox.emit

        def jittered(prefix: str, payload: dict[str, Any], send: Callable[..., None] = send) -> None:
            """Delay each send by a random time, the way thread scheduling does.

            Args:
                prefix: The event family.
                payload: The request.
                send: The fake worker's send.
            """
            time.sleep(draws.uniform(0, 0.01))
            send(prefix, payload)

        mailbox.emit = jittered  # type: ignore[method-assign]
        shinka_runner.install_worker_models(mailbox)
        results = asyncio.run(gathered())
        steps = [(request["id"], request["messages"][-1]["content"]) for request in sent]
        # The step log matches each committed step by its position on a replay.
        if committed is None:
            committed = steps
        assert steps == committed
        assert [result.content for result in results] == ["a", "b", "c"]
    assert committed == [("0", "a"), ("1", "b"), ("2", "c")]


def test_a_failed_worker_model_call_raises_an_error_upstream_retries() -> None:
    """Raise an ``Exception`` for a failed model call, so upstream's own retry loop handles it."""
    mailbox, _ = _connected_mailbox(lambda request: {"error": "ModelCallFailedError"})

    with pytest.raises(RuntimeError, match="ModelCallFailedError"):
        mailbox.complete([{"role": "user", "content": "hi"}])
    assert not mailbox.stopped.is_set()


def test_an_evaluation_waits_for_the_worker_reply_on_stdin() -> None:
    """Score a version through the worker's reply line, and count the evaluation."""
    mailbox, sent = _connected_mailbox(lambda request: {"score": 0.75, "info": {"feedback": "ok"}})

    assert mailbox.evaluate("text", {"id": 0}, candidate_id=3) == (0.75, {"feedback": "ok"})
    assert sent == [{"kind": "evaluate", "candidate": "text", "example": {"id": 0}, "candidate_id": 3, "id": "0"}]
    assert mailbox.total_evals == 1


def test_an_evaluation_on_stdin_turns_a_null_score_into_negative_infinity() -> None:
    """Read a worker reply with no score as a case with no score, as the file transport does (SKY-751)."""
    mailbox, _ = _connected_mailbox(lambda request: {"score": None, "info": {"error": "KeyError: 'x'"}})

    assert mailbox.evaluate("text", {"id": 0}, candidate_id=3) == (float("-inf"), {"error": "KeyError: 'x'"})
    assert mailbox.total_evals == 1


@pytest.mark.parametrize(
    ("max_concurrency", "sent_cases"),
    [(1, [0, 1]), (3, [0, 1, 2])],
    ids=["one-case-at-a-time", "cases-in-parallel"],
)
def test_a_failed_case_ends_its_version_before_the_next_request_so_a_replay_keeps_the_step_order(
    max_concurrency: int, sent_cases: list[int], tmp_path: Path
) -> None:
    """Skip a version's cases not yet started after a crashed case, and answer only when no case of it still runs.

    The worker commits each request as one step by position. A case left
    running after the answer races the next proposal's model request, so a
    replay could see another order and fail the resume.

    Args:
        max_concurrency: Cases the scorer runs at once; the engine loop runs one.
        sent_cases: The cases whose requests reach the worker.
        tmp_path: Scratch folder for the queue and the program.
    """

    def reply(request: dict[str, Any]) -> dict[str, Any]:
        """Crash the middle case, score the others and answer the model.

        Args:
            request: The request.

        Returns:
            The worker's reply fields.
        """
        if request["kind"] == "model":
            return {"text": "next version"}
        # A null score is a scored failure since SKY-751; a non-numeric one still crashes the case.
        return {"score": "boom"} if request["example"]["id"] == 1 else {"score": 1.0}

    mailbox, sent = _connected_mailbox(reply)
    send = mailbox.emit
    last_case_started = threading.Event()

    def slow_last_case(prefix: str, payload: dict[str, Any]) -> None:
        """Delay the request of the last case, as a slow thread would.

        With cases in parallel, the crash waits until the last case started,
        so that case is one the scorer must wait for, not one it skips.

        Args:
            prefix: The event family.
            payload: The request, or a progress event.
        """
        if prefix != "APP_NATIVE_RPC ":
            return
        case = payload["example"]["id"] if payload["kind"] == "evaluate" else None
        if case == 2:
            last_case_started.set()
            time.sleep(0.3)
        if case == 1 and max_concurrency > 1:
            assert last_case_started.wait(5)
        send(prefix, payload)

    mailbox.emit = slow_last_case  # type: ignore[method-assign]
    scorer = shinka_runner.ProgramScorer(
        mailbox=mailbox,
        queue=tmp_path / "queue",
        examples=[{"id": 0}, {"id": 1}, {"id": 2}],
        part_names=None,
        max_concurrency=max_concurrency,
        stop_at_score=None,
    )
    program = tmp_path / "results" / "gen_3" / "main.md"
    program.parent.mkdir(parents=True)
    program.write_text(shinka_runner.pack_program("text"))

    with pytest.raises(ValueError):
        scorer.score_program(str(program))
    mailbox.complete([{"role": "user", "content": "next proposal"}])
    time.sleep(0.5)

    steps = [(request["kind"], (request.get("example") or {}).get("id")) for request in sent]
    # With cases in parallel, the order of the first two requests is up to the threads.
    assert sorted(steps[:-1]) == [("evaluate", case) for case in sent_cases]
    assert steps[-1] == ("model", None)
    scorer.stop()


def test_a_closed_reply_stream_stops_the_run() -> None:
    """Stop at once when the worker closes the reply stream, instead of waiting for the timeout."""
    mailbox = shinka_runner.EvaluatorMailbox("testnonce", 60.0)
    mailbox.emit = lambda prefix, payload: None  # type: ignore[method-assign]
    mailbox.listen(iter(())).join(5.0)
    started = time.monotonic()

    with pytest.raises(shinka_runner.EvaluationStopped, match="closed its reply stream"):
        mailbox.evaluate("text")
    assert mailbox.stopped.is_set()
    # The reply timeout is 60 seconds; a closed stream must not wait it out.
    assert time.monotonic() - started < 5.0


def test_a_reply_whose_id_is_not_a_string_is_not_filed() -> None:
    """Drop a reply with a number id: request ids are strings, so it can answer no request."""
    mailbox = shinka_runner.EvaluatorMailbox("testnonce", 60.0)

    mailbox.listen(iter(['{"id": 0, "score": 1.0}\n', '{"id": null, "score": 1.0}\n'])).join(5.0)

    assert mailbox._replies == {}


def test_the_gate_holds_a_proposal_until_the_previous_version_is_scored_and_filed() -> None:
    """Start upstream's next proposal only once no job, completed-job work or side effect is in flight."""
    order: list[str] = []
    runner = SimpleNamespace(running_jobs=["job"], failed_jobs_for_retry={}, pending=1, side_effects=True)

    async def propose(generation: int, task_id: str) -> str:
        """Record the proposal.

        Args:
            generation: Upstream's generation number.
            task_id: Upstream's task id.

        Returns:
            The task id.
        """
        order.append(f"propose {generation}")
        return task_id

    runner._generate_proposal_async = propose
    runner._get_completed_job_work_count = lambda: runner.pending
    runner._has_background_side_effect_work = lambda: runner.side_effects
    shinka_runner.install_sequential_gate(runner)

    async def scenario() -> str:
        """Finish the job, its filing and its side effects one after another while the proposal waits.

        Returns:
            The proposal's result.
        """
        proposal = asyncio.create_task(runner._generate_proposal_async(1, "task-1"))
        for step in ("job", "filing", "side effects"):
            await asyncio.sleep(0.12)
            order.append(f"{step} done")
            if step == "job":
                runner.running_jobs = []
            elif step == "filing":
                runner.pending = 0
            else:
                runner.side_effects = False
        return await proposal

    assert asyncio.run(scenario()) == "task-1"
    assert order == ["job done", "filing done", "side effects done", "propose 1"]


@pytest.mark.parametrize("busy", ["running_jobs", "retries", "completed_work", "side_effects"])
def test_each_gate_condition_alone_holds_a_proposal(busy: str) -> None:
    """Hold the next proposal while any one of the gate's conditions is still true.

    Args:
        busy: The one condition that is true when the proposal starts.
    """
    order: list[str] = []
    state: dict[str, Any] = {"running_jobs": [], "retries": {}, "completed_work": 0, "side_effects": False}
    busy_values = {"running_jobs": ["job"], "retries": {"job-1": object()}, "completed_work": 1, "side_effects": True}
    state[busy] = busy_values[busy]
    runner = SimpleNamespace(running_jobs=state["running_jobs"], failed_jobs_for_retry=state["retries"])

    async def propose(generation: int, task_id: str) -> str:
        """Record the proposal.

        Args:
            generation: Upstream's generation number.
            task_id: Upstream's task id.

        Returns:
            The task id.
        """
        order.append(f"propose {generation}")
        return task_id

    runner._generate_proposal_async = propose
    runner._get_completed_job_work_count = lambda: state["completed_work"]
    runner._has_background_side_effect_work = lambda: state["side_effects"]
    shinka_runner.install_sequential_gate(runner)

    async def scenario() -> str:
        """Clear the one condition while the proposal waits.

        Returns:
            The proposal's result.
        """
        proposal = asyncio.create_task(runner._generate_proposal_async(3, "task-3"))
        await asyncio.sleep(0.12)
        order.append("cleared")
        runner.running_jobs = []
        runner.failed_jobs_for_retry = {}
        state.update(completed_work=0, side_effects=False)
        return await proposal

    assert asyncio.run(scenario()) == "task-3"
    assert order == ["cleared", "propose 3"]


def test_the_gate_holds_a_proposal_while_a_database_retry_is_queued() -> None:
    """Start upstream's next proposal only once its retry queue of failed database writes is empty."""
    order: list[str] = []
    runner = SimpleNamespace(running_jobs=[], failed_jobs_for_retry={"job-1": object()})

    async def propose(generation: int, task_id: str) -> str:
        """Record the proposal.

        Args:
            generation: Upstream's generation number.
            task_id: Upstream's task id.

        Returns:
            The task id.
        """
        order.append(f"propose {generation}")
        return task_id

    runner._generate_proposal_async = propose
    runner._get_completed_job_work_count = lambda: 0
    runner._has_background_side_effect_work = lambda: False
    shinka_runner.install_sequential_gate(runner)

    async def scenario() -> str:
        """Finish the retry while the proposal waits.

        Returns:
            The proposal's result.
        """
        proposal = asyncio.create_task(runner._generate_proposal_async(2, "task-2"))
        await asyncio.sleep(0.12)
        order.append("retry done")
        runner.failed_jobs_for_retry.clear()
        return await proposal

    assert asyncio.run(scenario()) == "task-2"
    assert order == ["retry done", "propose 2"]


def test_seeding_makes_upstream_draws_and_program_ids_repeat(monkeypatch: pytest.MonkeyPatch) -> None:
    """Draw the same random numbers and program ids after the same seed.

    The test restores the ``random`` and NumPy random states that the seeding changes.

    Args:
        monkeypatch: Restores ``uuid.uuid4`` and gives the runner NumPy.
    """
    monkeypatch.setattr(uuid, "uuid4", uuid.uuid4)
    # The test environment has no upstream, so the runner may have no NumPy of its own.
    monkeypatch.setattr(shinka_runner, "np", np)
    state = random.getstate()
    np_state = np.random.get_state()
    try:
        shinka_runner.seed_randomness(5)
        first = (random.random(), np.random.random(), _draw_a(), _draw_a())
        shinka_runner.seed_randomness(5)
        second = (random.random(), np.random.random(), _draw_a(), _draw_a())
    finally:
        random.setstate(state)
        np.random.set_state(np_state)

    assert first == second
    assert uuid.UUID(first[2]).version == 4
    assert first[2] != first[3]


def _draw_a() -> str:
    """Draw one program id from this line.

    Returns:
        The id.
    """
    return str(uuid.uuid4())


def _draw_b() -> str:
    """Draw one program id from another line.

    Returns:
        The id.
    """
    return str(uuid.uuid4())


def test_each_seeded_id_source_repeats_its_ids_in_any_interleaving(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give each call line the same ids whether another line draws before or after it.

    Upstream draws a proposal task id before the sequential gate, so that draw
    can fall on either side of the previous version's program ids. The test
    restores the ``random`` and NumPy random states that the seeding changes.

    Args:
        monkeypatch: Restores ``uuid.uuid4``.
    """
    monkeypatch.setattr(uuid, "uuid4", uuid.uuid4)
    state = random.getstate()
    np_state = shinka_runner.np.random.get_state() if shinka_runner.np is not None else None
    try:
        shinka_runner.seed_randomness(5)
        live = (_draw_a(), _draw_b(), _draw_a())
        shinka_runner.seed_randomness(5)
        replay_b = _draw_b()
        replay = (_draw_a(), replay_b, _draw_a())
    finally:
        random.setstate(state)
        if np_state is not None:
            shinka_runner.np.random.set_state(np_state)

    assert live == replay
    assert len(set(live)) == 3


@pytest.mark.parametrize(
    ("timeout", "time_limit"), [(7 * 24 * 3600.0, "168:00:00"), (90061.5, "25:01:02"), (59.0, "0:00:59")]
)
def test_on_the_engine_loop_the_job_time_limit_is_the_runs_timeout(timeout: float, time_limit: str) -> None:
    """Set upstream's ``hh:mm:ss`` job time to the run's timeout on the engine loop, and leave it unset elsewhere.

    With ``time`` set, upstream 0.0.7 takes its hung-evaluation limit from it,
    not from the measured evaluation times that a replay shortens.

    Args:
        timeout: The run's timeout in seconds.
        time_limit: The expected job time.
    """
    payload = {"transport": "stdio", "timeout_seconds": timeout}

    assert shinka_runner.job_fields(payload) == {"python_executable": sys.executable, "time": time_limit}
    assert shinka_runner.job_fields({"timeout_seconds": timeout}) == {"python_executable": sys.executable}


def test_upstreams_hung_evaluation_limit_does_not_follow_the_measured_times_on_the_engine_loop() -> None:
    """Run the pinned upstream's limit rule: a near-zero and a long measured average give the same limit."""
    pytest.importorskip("shinka.core.async_runner")
    limits = set()
    for average in (0.001, 1000.0):
        runner = SimpleNamespace(
            job_config=shinka_runner.LocalJobConfig(
                **shinka_runner.job_fields({"transport": "stdio", "timeout_seconds": 7 * 24 * 3600.0})
            ),
            scheduler=SimpleNamespace(job_type="local"),
            _evaluation_seconds_ewma=average,
        )
        limits.add(shinka_runner.ShinkaEvolveRunner._get_evaluation_runtime_limit_seconds(runner))

    assert limits == {7 * 24 * 3600.0}


def test_upstream_accepts_the_engine_loop_models_without_key_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run the pinned upstream's model check on the engine-loop config, in the child's environment.

    The engine starts the child with no key variable, and upstream 0.0.7
    refuses a model whose ``api_key_env`` names an unset variable.

    Args:
        tmp_path: Scratch folder.
        monkeypatch: Replaces the environment with the child's.
    """
    async_runner = pytest.importorskip("shinka.core.async_runner")
    for name in list(os.environ):
        monkeypatch.delenv(name)
    # The variables ``ShinkaEvolveEngine._drive`` gives the child.
    for name, value in {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "SHINKA_PRICING_MODE": "offline",
    }.items():
        monkeypatch.setenv(name, value)
    config = shinka_runner.build_config(_payload(transport="stdio", timeout_seconds=60.0), str(tmp_path / "results"), 1)
    evolution = shinka_runner.EvolutionConfig(**config["evolution"])

    assert evolution.meta_llm_models
    async_runner._validate_evo_config_model_env_access(evolution)


def test_in_the_sandbox_an_evaluation_waits_for_the_runtime_reply_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Send a uuid4 hex id with no kind, and read the reply from ``rpc/<id>.json``, as the sandbox runtime expects.

    Args:
        tmp_path: Scratch folder.
        monkeypatch: Moves into the scratch folder.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "rpc").mkdir()
    mailbox = shinka_runner.EvaluatorMailbox("testnonce", 5.0)
    sent: list[dict[str, Any]] = []

    def emit(prefix: str, payload: dict[str, Any]) -> None:
        """Record the request and write the runtime's reply file.

        Args:
            prefix: The frame prefix.
            payload: The request.
        """
        sent.append(payload)
        (tmp_path / "rpc" / f"{payload['id']}.json").write_text(json.dumps({"score": 0.5, "info": {}}))

    mailbox.emit = emit  # type: ignore[method-assign]

    assert mailbox.evaluate("text", {"id": 0}, candidate_id=3) == (0.5, {})
    assert uuid.UUID(hex=sent[0]["id"]).hex == sent[0]["id"]
    assert sent == [{"id": sent[0]["id"], "candidate": "text", "example": {"id": 0}, "candidate_id": 3}]


@pytest.mark.parametrize(
    ("path", "problem"),
    [("../outside.py", "outside the repository"), ("/tmp/outside.py", "outside the repository")],
)
def test_a_path_that_leaves_the_checkout_is_refused(tmp_path: Path, path: str, problem: str) -> None:
    """Refuse a ``..`` path and an absolute path by where they resolve.

    Args:
        tmp_path: Scratch folder.
        path: The path a version writes.
        problem: Words the refusal names.
    """
    (tmp_path / "checkout").mkdir()

    with pytest.raises(ValueError, match=problem):
        shinka_runner.checked_target(tmp_path / "checkout", path)


def test_a_path_through_a_symlinked_folder_is_refused_even_inside_the_checkout(tmp_path: Path) -> None:
    """Refuse a path whose parent folder is a symlink, also when the link points inside the checkout.

    Args:
        tmp_path: Scratch folder.
    """
    checkout = tmp_path / "checkout"
    (checkout / "src").mkdir(parents=True)
    (checkout / "docs").symlink_to("src")

    with pytest.raises(ValueError, match="crosses a symlink"):
        shinka_runner.checked_target(checkout, "docs/app.py")
    assert shinka_runner.checked_target(checkout, "src/app.py") == checkout.resolve() / "src/app.py"


def _symlink_patch(target: str) -> str:
    """Write a seed patch that adds ``src/link``, a symlink to a target.

    Args:
        target: Where the link points.

    Returns:
        The patch, as ``git diff`` writes it.
    """
    return (
        "diff --git a/src/link b/src/link\n"
        "new file mode 120000\n"
        "--- /dev/null\n"
        "+++ b/src/link\n"
        "@@ -0,0 +1 @@\n"
        f"+{target}\n"
        "\\ No newline at end of file\n"
    )


def _ship(tmp_path: Path, source: Path) -> list[str]:
    """Pack a folder as the run's tree chunks.

    Args:
        tmp_path: Scratch folder.
        source: The folder.

    Returns:
        The chunk files.
    """
    archive = tmp_path / "tree.tgz"
    subprocess.run(["tar", "-czf", str(archive), "-C", str(source), "."], check=True)
    chunks = []
    for index, chunk in enumerate(repo_tree.archive_chunks(archive)):
        chunks.append(str(tmp_path / f"tree.{index}.b64"))
        Path(chunks[-1]).write_text(chunk)
    return chunks


def test_a_version_cannot_write_past_its_editable_paths_through_a_shipped_symlink(tmp_path: Path) -> None:
    """Refuse a write through a symlink the repository ships, which would reach a file outside the editable paths.

    Args:
        tmp_path: Scratch folder.
    """
    source = tmp_path / "source"
    (source / "src").mkdir(parents=True)
    (source / "scoring").mkdir()
    (source / "scoring/metric.py").write_text("SCORE = 1\n")
    (source / "src/cfg").symlink_to("../scoring")
    bundle = shinka_bundle.RepoBundle.prepare(
        {"chunks": _ship(tmp_path, source), "editable_paths": ["src"]}, "", tmp_path / "checkout"
    )
    shinka_runner.guard_materialize(bundle)

    with pytest.raises(ValueError, match="crosses a symlink"):
        bundle.materialize({"src/cfg/metric.py": "SCORE = 100\n"})
    assert (tmp_path / "checkout/scoring/metric.py").read_text() == "SCORE = 1\n"


def test_a_version_cannot_write_outside_the_checkout_through_a_seeded_symlink(tmp_path: Path) -> None:
    """Refuse a write through a symlink to a folder outside the checkout, before any file is written.

    Args:
        tmp_path: Scratch folder.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    source = tmp_path / "source"
    (source / "src").mkdir(parents=True)
    (source / "src/app.py").write_text("x = 1\n")
    bundle = shinka_bundle.RepoBundle.prepare(
        {"chunks": _ship(tmp_path, source), "editable_paths": ["src"]},
        _symlink_patch(str(outside)),
        tmp_path / "checkout",
    )
    shinka_runner.guard_materialize(bundle)

    with pytest.raises(ValueError, match="outside the repository"):
        bundle.materialize({"src/link/planted.py": "x = 2\n"})
    assert not (outside / "planted.py").exists()


def _repo_payload(tmp_path: Path, seed: str, chunks: list[str]) -> dict[str, Any]:
    """Build a repository run's payload.

    Args:
        tmp_path: Scratch folder.
        seed: The seed version's patch.
        chunks: The tree's chunk files.

    Returns:
        The payload.
    """
    return _payload(
        nonce="testnonce",
        timeout_seconds=5.0,
        task={"seed_candidate": seed, "train_set": []},
        repo={"chunks": chunks, "editable_paths": ["src"]},
    )


def test_a_seed_patch_that_adds_a_symlink_is_refused_before_the_checkout_is_made(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refuse a seed version that adds a symlink, before git applies it.

    Args:
        tmp_path: Scratch folder.
        monkeypatch: Moves into the scratch folder and replaces the upstream runner.
    """
    chunks, _ = _repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(shinka_runner, "ShinkaEvolveRunner", object)

    with pytest.raises(ValueError, match="file mode 120000"):
        shinka_runner.execute(_repo_payload(tmp_path, _symlink_patch("/etc"), chunks))
    assert not (tmp_path / "shinka-run" / "repo-checkout").exists()


def test_a_tree_that_ships_git_metadata_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse a shipped tree with a ``.git`` folder, whose hooks and config git would run on the worker.

    Args:
        tmp_path: Scratch folder.
        monkeypatch: Moves into the scratch folder and replaces the upstream runner.
    """
    source = tmp_path / "source"
    (source / ".git").mkdir(parents=True)
    (source / ".git/config").write_text("[core]\n\tfsmonitor = touch planted\n")
    (source / "src").mkdir()
    (source / "src/app.py").write_text("x = 1\n")
    chunks = _ship(tmp_path, source)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(shinka_runner, "ShinkaEvolveRunner", object)

    with pytest.raises(ValueError, match="unsafe entry"):
        shinka_runner.execute(_repo_payload(tmp_path, "", chunks))
    assert not (tmp_path / "shinka-run" / "repo-checkout").exists()


@pytest.mark.parametrize(
    "line",
    ["new file mode\t120000", "new file mode 120000\r", "new file mode  120000 "],
    ids=["tab", "carriage-return", "extra-blanks"],
)
def test_a_symlink_mode_with_odd_blanks_or_line_ends_is_refused(line: str) -> None:
    """Refuse a symlink mode line whatever blanks or line end surround the mode.

    Args:
        line: The patch's mode line.
    """
    patch = _symlink_patch("/etc").replace("new file mode 120000", line)

    with pytest.raises(ValueError, match="file mode 120000"):
        shinka_runner.refuse_unsafe_patch(patch)


def test_the_patch_check_reads_the_whole_patch_from_inside_a_repository_subfolder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read git's summary of every path, also when the runner's folder is inside another repository.

    Inside a repository, ``git apply`` drops the paths outside the current
    folder, so its summary would miss the symlink.

    Args:
        tmp_path: Scratch folder.
        monkeypatch: Moves into a repository's subfolder and turns off the text check.
    """
    subprocess.run(["git", "init", "-q", str(tmp_path / "repo")], check=True)
    (tmp_path / "repo/sub").mkdir()
    monkeypatch.chdir(tmp_path / "repo/sub")
    # Only git's summary can see the mode now.
    monkeypatch.setattr(shinka_runner, "_PATCH_MODE", re.compile(r"(?!)"))

    with pytest.raises(ValueError, match="file mode 120000"):
        shinka_runner.refuse_unsafe_patch(_symlink_patch("/etc"))


def test_git_summary_of_a_mode_change_to_a_symlink_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read the new mode from git's ``mode change OLD => NEW`` summary line.

    Args:
        monkeypatch: Turns off the text check, so only git's summary can see the mode.
    """
    monkeypatch.setattr(shinka_runner, "_PATCH_MODE", re.compile(r"(?!)"))
    patch = "diff --git a/f b/f\nold mode 100644\nnew mode 120000\n"

    with pytest.raises(ValueError, match="file mode 120000"):
        shinka_runner.refuse_unsafe_patch(patch)


def test_a_symlink_that_the_patch_check_misses_is_refused_after_the_seed_is_applied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refuse the checkout when the applied seed made a symlink the shipped tree did not have.

    Args:
        tmp_path: Scratch folder.
        monkeypatch: Moves into the scratch folder and replaces the upstream runner and the patch check.
    """
    chunks, _ = _repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(shinka_runner, "ShinkaEvolveRunner", object)
    monkeypatch.setattr(shinka_runner, "refuse_unsafe_patch", lambda patch: None)

    with pytest.raises(ValueError, match="adds or changes the symlink 'src/link'"):
        shinka_runner.execute(_repo_payload(tmp_path, _symlink_patch("/etc"), chunks))


def test_a_symlink_the_tree_ships_unchanged_is_kept(tmp_path: Path) -> None:
    """Keep a repository's own symlink: only a new or retargeted one is refused.

    Args:
        tmp_path: Scratch folder.
    """
    source = tmp_path / "source"
    (source / "src").mkdir(parents=True)
    (source / "src/app.py").write_text("x = 1\n")
    (source / "src/alias.py").symlink_to("app.py")
    chunks = _ship(tmp_path, source)
    links = shinka_runner.refuse_unsafe_tree(chunks)
    bundle = shinka_bundle.RepoBundle.prepare({"chunks": chunks, "editable_paths": ["src"]}, "", tmp_path / "checkout")

    assert links == {"src/alias.py": "app.py"}
    shinka_runner.refuse_new_links(bundle.checkout, links)
    (bundle.checkout / "src/alias.py").unlink()
    (bundle.checkout / "src/alias.py").symlink_to("/etc/passwd")
    with pytest.raises(ValueError, match=r"adds or changes the symlink 'src/alias\.py'"):
        shinka_runner.refuse_new_links(bundle.checkout, links)


def _tree_chunk(tmp_path: Path, member: tarfile.TarInfo) -> list[str]:
    """Pack one tar entry as the run's tree chunk.

    Args:
        tmp_path: Scratch folder.
        member: The entry.

    Returns:
        The chunk file.
    """
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tree:
        tree.addfile(member)
    chunk = tmp_path / "tree.0.b64"
    chunk.write_text(base64.b64encode(buffer.getvalue()).decode("ascii"))
    return [str(chunk)]


@pytest.mark.parametrize("kind", [tarfile.LNKTYPE, tarfile.FIFOTYPE], ids=["hard-link", "fifo"])
def test_a_tree_with_a_hard_link_or_a_special_file_is_refused(tmp_path: Path, kind: bytes) -> None:
    """Refuse a shipped tree entry that is not a file, a folder or a symlink.

    Args:
        tmp_path: Scratch folder.
        kind: The tar entry type.
    """
    member = tarfile.TarInfo("src/odd")
    member.type = kind
    member.linkname = "src/app.py" if kind == tarfile.LNKTYPE else ""

    with pytest.raises(ValueError, match="unsafe entry: src/odd"):
        shinka_runner.refuse_unsafe_tree(_tree_chunk(tmp_path, member))


def _git_checkout(tmp_path: Path) -> Path:
    """Make an empty git checkout.

    Args:
        tmp_path: Scratch folder.

    Returns:
        The checkout.
    """
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    return checkout


def test_a_checkout_with_a_special_file_is_refused(tmp_path: Path) -> None:
    """Refuse a checkout in which the seed made a named pipe.

    Args:
        tmp_path: Scratch folder.
    """
    checkout = _git_checkout(tmp_path)
    os.mkfifo(checkout / "pipe")

    with pytest.raises(ValueError, match="adds the special file 'pipe'"):
        shinka_runner.refuse_new_links(checkout, {})


def test_a_checkout_with_a_staged_gitlink_is_refused(tmp_path: Path) -> None:
    """Refuse a checkout whose index holds a gitlink, which no folder walk can see.

    Args:
        tmp_path: Scratch folder.
    """
    checkout = _git_checkout(tmp_path)
    subprocess.run(
        ["git", "update-index", "--add", "--cacheinfo", f"160000,{'1' * 40},vendor/lib"], cwd=checkout, check=True
    )

    with pytest.raises(ValueError, match="adds the gitlink 'vendor/lib'"):
        shinka_runner.refuse_new_links(checkout, {})


def test_a_path_that_names_a_folder_is_refused(tmp_path: Path) -> None:
    """Refuse a write to a path that is a folder in the checkout.

    Args:
        tmp_path: Scratch folder.
    """
    checkout = tmp_path / "checkout"
    (checkout / "src").mkdir(parents=True)

    with pytest.raises(ValueError, match="'src' is not a regular file"):
        shinka_runner.checked_target(checkout, "src")


class _IdleRunner:
    """Stand in for upstream's runner: it scores nothing."""

    def __init__(self, **fields: Any) -> None:
        """Start with no jobs.

        Args:
            **fields: Upstream's runner arguments, ignored.
        """
        self.running_jobs: list[Any] = []
        self.failed_jobs_for_retry: dict[str, Any] = {}

    async def _generate_proposal_async(self, generation: int, task_id: str) -> None:
        """Make no proposal.

        Args:
            generation: Upstream's generation number.
            task_id: Upstream's proposal task id.
        """

    def run(self) -> None:
        """End at once."""


def test_execute_rejects_a_version_that_writes_through_a_shipped_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject, without scoring, a version that writes through a symlink the tree ships.

    The scorer under test is the one ``execute`` builds.

    Args:
        tmp_path: Scratch folder.
        monkeypatch: Moves into the scratch folder and replaces upstream and the reply stream.
    """
    scorers: list[shinka_runner.ProgramScorer] = []

    class RecordingScorer(shinka_runner.ProgramScorer):
        """Record each scorer that ``execute`` builds."""

        def __init__(self, **fields: Any) -> None:
            """Build the scorer and record it.

            Args:
                **fields: The scorer's arguments.
            """
            super().__init__(**fields)
            scorers.append(self)

    source = tmp_path / "source"
    (source / "src").mkdir(parents=True)
    (source / "src/app.py").write_text("x = 1\n")
    (source / "scoring").mkdir()
    (source / "scoring/metric.py").write_text("SCORE = 1\n")
    (source / "src/cfg").symlink_to("../scoring")
    chunks = _ship(tmp_path, source)
    monkeypatch.chdir(tmp_path)
    for name in ("EvolutionConfig", "LocalJobConfig", "DatabaseConfig"):
        monkeypatch.setattr(shinka_runner, name, SimpleNamespace)
    monkeypatch.setattr(shinka_runner, "ShinkaEvolveRunner", _IdleRunner)
    monkeypatch.setattr(shinka_runner, "ProgramScorer", RecordingScorer)
    monkeypatch.setattr(shinka_runner, "install_repo_mode", lambda bundle, runner: None)
    monkeypatch.setattr(shinka_runner, "install_worker_models", lambda mailbox: None)
    monkeypatch.setattr(shinka_runner, "seed_randomness", lambda seed: None)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    shinka_runner.execute({**_repo_payload(tmp_path, "", chunks), "transport": "stdio"})
    program = tmp_path / "results" / "gen_1" / "main.md"
    program.parent.mkdir(parents=True)
    program.write_text(shinka_bundle.encode_bundle({"src/cfg/metric.py": "SCORE = 100\n"}))

    answer = scorers[0].score_program(str(program))

    assert answer["metrics"]["text_feedback"].startswith("Rejected without scoring")
    assert "crosses a symlink" in answer["error"]
    checkout = tmp_path / "shinka-run" / "repo-checkout"
    assert (checkout / "scoring/metric.py").read_text() == "SCORE = 1\n"


def test_on_the_engine_loop_execute_scores_one_case_at_a_time_behind_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Score one case at a time and gate each proposal on the engine loop, whatever concurrency the payload asks.

    A resume replays its steps in order, so neither may depend on thread timing.

    Args:
        tmp_path: Scratch folder.
        monkeypatch: Moves into the scratch folder and replaces upstream and the reply stream.
    """
    scorers: list[shinka_runner.ProgramScorer] = []
    runners: list[_IdleRunner] = []

    class RecordingScorer(shinka_runner.ProgramScorer):
        """Record each scorer that ``execute`` builds."""

        def __init__(self, **fields: Any) -> None:
            """Build the scorer and record it.

            Args:
                **fields: The scorer's arguments.
            """
            super().__init__(**fields)
            scorers.append(self)

    def runner(**fields: Any) -> _IdleRunner:
        """Build an idle runner and record it.

        Args:
            **fields: Upstream's runner arguments.

        Returns:
            The runner.
        """
        runners.append(_IdleRunner(**fields))
        return runners[-1]

    monkeypatch.chdir(tmp_path)
    for name in ("EvolutionConfig", "LocalJobConfig", "DatabaseConfig"):
        monkeypatch.setattr(shinka_runner, name, SimpleNamespace)
    monkeypatch.setattr(shinka_runner, "ShinkaEvolveRunner", runner)
    monkeypatch.setattr(shinka_runner, "ProgramScorer", RecordingScorer)
    monkeypatch.setattr(shinka_runner, "install_worker_models", lambda mailbox: None)
    monkeypatch.setattr(shinka_runner, "seed_randomness", lambda seed: None)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    shinka_runner.execute(
        _payload(
            nonce="testnonce",
            timeout_seconds=5.0,
            transport="stdio",
            max_concurrency=4,
            task={"seed_candidate": "seed", "train_set": []},
        )
    )

    assert scorers[0]._cases._max_workers == 1
    assert runners[0]._generate_proposal_async.__qualname__ == "install_sequential_gate.<locals>.gated"
