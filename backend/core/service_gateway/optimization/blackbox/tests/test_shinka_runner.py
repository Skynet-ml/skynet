"""Check the ShinkaEvolve guest runner's pure helpers without the upstream package installed."""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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
    assert runner["max_evaluation_jobs"] == 2


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


def test_scores_become_upstream_metrics() -> None:
    """Report the mean as the combined score, named scores as public metrics and feedback as text."""
    metrics = shinka_runner.summarize_scores(
        [
            (1.0, {"feedback": "great", "scores": {"tone": {"score": 0.5, "feedback": "flat"}}}),
            (0.0, {"scores": {"tone": 1.0}}),
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


def test_rule_blocker_writes_a_rule_block_run_event(capsys: pytest.CaptureFixture[str]) -> None:
    """Frame a path-rule block as a structured log line the parent forwards as ``rule.block``."""
    shinka_runner.rule_blocker(shinka_runner.EvaluatorMailbox("testnonce", 1.0))("Cannot change 'app.py'.")

    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("APP_NATIVE_LOG testnonce ")]
    assert len(lines) == 1
    record = json.loads(lines[0].split(" ", 2)[2])
    assert record["event"] == repo_tree.RULE_BLOCK_EVENT
    assert record["source"] == "engine"
    assert record["fields"] == {"rule": repo_tree.PATH_RULE, "reason": "Cannot change 'app.py'."}


def test_refusal_logger_writes_a_proposer_refusal_run_event(capsys: pytest.CaptureFixture[str]) -> None:
    """Frame a hidden-path refusal as a run-log line with the tool and kind only."""
    shinka_runner.refusal_logger(shinka_runner.EvaluatorMailbox("testnonce", 1.0))({"tool": "read", "kind": "config"})

    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("APP_NATIVE_LOG testnonce ")]
    assert len(lines) == 1
    record = json.loads(lines[0].split(" ", 2)[2])
    assert record["event"] == "proposer.refusal"
    assert record["level"] == "WARNING"
    assert record["fields"] == {"tool": "read", "kind": "config"}
    assert "optimize.toml" not in lines[0]


def test_agent_editor_guards_sessions_and_logs_their_refusals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pass the bundle's hidden list to each session and send its refusals to the run log; the edit still lands.

    Args:
        tmp_path: Run folder.
        monkeypatch: Replaces the harness session.
    """
    chunks, _ = _repo(tmp_path)
    refusals: list[dict[str, str]] = []
    bundle = shinka_bundle.RepoBundle.prepare(
        {"chunks": chunks, "editable_paths": ["src"], "hidden_paths": ["optimize.toml", "eval/metric.py"]},
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

    assert calls[0]["proposer"]["hidden_paths"] == ["optimize.toml", "eval/metric.py"]
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

    assert "hidden_paths" not in calls[0]["proposer"]


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
    assert ledger.usage_by_model[_EDITOR_MODEL]["total_tokens"] == 150
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
