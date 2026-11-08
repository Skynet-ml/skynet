"""Tests for evolving a repository with ShinkaEvolve as a bundle of changed files."""

from __future__ import annotations

import random
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from .. import harness_bridge, repo_tree
from ..shinka_bundle import (
    BUNDLE_HEAD,
    MAX_FILE_BYTES,
    RepoBundle,
    apply_file_edit,
    choose_patch_type,
    decode_bundle,
    encode_bundle,
    mentioned_paths,
    parse_response,
)


def _chunks(tmp_path: Path, files: dict[str, str | bytes]) -> list[str]:
    """Pack ``files`` the way the parent ships a repository snapshot.

    Args:
        tmp_path: Scratch folder.
        files: Relative path to text or bytes.

    Returns:
        The base64 chunk files.
    """
    source = tmp_path / "source"
    for name, content in files.items():
        (source / name).parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            (source / name).write_bytes(content)
        else:
            (source / name).write_text(content)
    archive = tmp_path / "tree.tgz"
    subprocess.run(["tar", "-czf", str(archive), "-C", str(source), "."], check=True)
    chunks = []
    for index, chunk in enumerate(repo_tree.archive_chunks(archive)):
        chunks.append(str(tmp_path / f"tree.{index}.b64"))
        Path(chunks[-1]).write_text(chunk)
    return chunks


_FILES: dict[str, str | bytes] = {
    "src/app.py": "def answer():\n    return 1\n",
    "src/util.py": "SCALE = 2\n",
    "src/logo.png": b"\x89PNG\x00\x01binary",
    "src/big.txt": "x" * (MAX_FILE_BYTES + 1),
    "vendor/lib.py": "VENDORED = True\n",
    "README.md": "hello\n",
}


@pytest.fixture
def bundle(tmp_path: Path) -> RepoBundle:
    """Unpack a small repository whose editable paths are ``src`` and ``vendor``, ``vendor`` read-only.

    Args:
        tmp_path: Scratch folder.

    Returns:
        The bundle rules bound to the checkout.
    """
    repo = {"chunks": _chunks(tmp_path, _FILES), "editable_paths": ["src", "vendor"], "readonly_paths": ["vendor"]}
    return RepoBundle.prepare(repo, "", tmp_path / "checkout")


@pytest.mark.parametrize(
    "files",
    [
        {},
        {"a.py": "x = 1\n"},
        {"a.py": "no trailing newline", "b/c.md": ""},
        {"tricky.md": f'{BUNDLE_HEAD}\n<!-- SKYNET-FILE {{"path": "x", "lines": 1}} -->\n-->\n\n'},
        {'odd "name" -->.txt': "\n\n\n"},
    ],
)
def test_bundle_round_trips(files: dict[str, str]) -> None:
    """Decode exactly the files encoded, marker-like contents included.

    Args:
        files: Bundle files.
    """
    assert decode_bundle(encode_bundle(files)) == files


@pytest.mark.parametrize(
    "text",
    [
        "just text",
        f"{BUNDLE_HEAD}\nstray line\n",
        f'{BUNDLE_HEAD}\n<!-- SKYNET-FILE {{"path": "a", "lines": 5}} -->\nx\n',
    ],
)
def test_decoding_rejects_a_mangled_bundle(text: str) -> None:
    """Refuse text that lost the bundle layout.

    Args:
        text: Not a well-formed bundle.
    """
    with pytest.raises(ValueError):
        decode_bundle(text)


def test_file_edits_apply_search_replace_full_contents_and_whitespace_drift() -> None:
    """Apply exact and trailing-whitespace-tolerant SEARCH/REPLACE edits, or take a block as full contents."""
    current = "a = 1\nb = 2   \nc = 3\n"
    body = "<<<<<<< SEARCH\na = 1\n=======\na = 10\n>>>>>>> REPLACE\n<<<<<<< SEARCH\nb = 2\nc = 3\n=======\nb = 20\nc = 30\n>>>>>>> REPLACE"

    assert apply_file_edit(body, current) == ("a = 10\nb = 20\nc = 30\n", 2)
    assert apply_file_edit("whole new file", None) == ("whole new file\n", 1)
    with pytest.raises(ValueError, match="does not match"):
        apply_file_edit("<<<<<<< SEARCH\nmissing\n=======\nx\n>>>>>>> REPLACE", current)
    with pytest.raises(ValueError, match="does not exist"):
        apply_file_edit("<<<<<<< SEARCH\na\n=======\nb\n>>>>>>> REPLACE", None)


def test_responses_name_their_files_and_open_requests() -> None:
    """Read FILE blocks with their paths and de-duplicated OPEN requests out of an answer."""
    edits, opens = parse_response(
        '<FILE path="./src/a.py">\nx\n</FILE>\n<FILE path="src/b.py">y</FILE>\n<OPEN>src/c.py</OPEN><OPEN>src/c.py</OPEN>'
    )
    assert edits == [("src/a.py", "x"), ("src/b.py", "y")]
    assert opens == ["src/c.py"]


def test_editable_files_follow_the_repository_rules(bundle: RepoBundle) -> None:
    """Offer only small text files under the editable, non-read-only paths."""
    assert bundle.paths() == ["src/app.py", "src/util.py"]
    assert bundle.rule_problem("README.md") == "it is outside the editable paths"
    assert "read-only" in str(bundle.rule_problem("vendor/lib.py"))
    assert bundle.rule_problem("../escape.py") == "it is not a path inside the repository"
    assert bundle.rule_problem("src/new.py") is None


def test_a_diff_edits_each_named_file_and_new_files_join_the_bundle(bundle: RepoBundle) -> None:
    """Apply per-file SEARCH/REPLACE blocks and full new files, keeping only real changes."""
    parent = {"src/util.py": "SCALE = 3\n"}
    response = (
        '<FILE path="src/app.py">\n<<<<<<< SEARCH\n    return 1\n=======\n    return SCALE\n>>>>>>> REPLACE\n</FILE>\n'
        '<FILE path="src/util.py">\n<<<<<<< SEARCH\nSCALE = 3\n=======\nSCALE = 2\n>>>>>>> REPLACE\n</FILE>\n'
        '<FILE path="src/new.py">\nNEW = 1\n</FILE>'
    )

    files, applied, error = bundle.apply(parent, response)

    assert error is None
    assert applied == 3
    # util.py went back to the starting commit's text, so it drops out of the bundle.
    assert files == {"src/app.py": "def answer():\n    return SCALE\n", "src/new.py": "NEW = 1\n"}


@pytest.mark.parametrize(
    ("response", "message"),
    [
        ('<FILE path="README.md">x</FILE>', "outside the editable paths"),
        ('<FILE path="vendor/lib.py">x</FILE>', "read-only"),
        ('<FILE path="src/logo.png">x</FILE>', "binary or larger"),
        ('<FILE path="src/app.py">\n<<<<<<< SEARCH\nnope\n=======\nx\n>>>>>>> REPLACE\n</FILE>', "does not match"),
        ("no blocks at all", "No FILE blocks"),
        ('<FILE path="src/app.py">\ndef answer():\n    return 1\n</FILE>', "leaves every file as it was"),
    ],
)
def test_bad_edits_explain_themselves_to_the_model(bundle: RepoBundle, response: str, message: str) -> None:
    """Reject an edit the rules forbid or that cannot apply, with a message the model can act on.

    Args:
        bundle: Repository bundle rules.
        response: The model's answer.
        message: Expected part of the error.
    """
    files, applied, error = bundle.apply({}, response)
    assert (files, applied) == (None, 0)
    assert message in str(error)


def test_open_requests_show_current_files_without_editing(bundle: RepoBundle) -> None:
    """Answer an OPEN-only reply with the version's current files, not an edit."""
    files, _, message = bundle.apply({"src/util.py": "SCALE = 9\n"}, "<OPEN>src/util.py</OPEN>\n<OPEN>README.md</OPEN>")

    assert files is None
    assert "SCALE = 9" in str(message)
    assert "README.md\nNot available: it is outside the editable paths" in str(message)


def test_patches_are_written_where_upstream_reads_them(bundle: RepoBundle, tmp_path: Path) -> None:
    """Write the child bundle as ``main.md`` and a per-file diff, the way upstream's applier does."""
    original = encode_bundle({})
    code, applied, output, error, diff, patch_path = bundle.write_patch(
        original, '<FILE path="src/util.py">\nSCALE = 5\n</FILE>', tmp_path / "gen_1"
    )

    assert error is None
    assert applied == 1
    assert output == tmp_path / "gen_1" / "main.md"
    assert decode_bundle(output.read_text()) == decode_bundle(str(code)) == {"src/util.py": "SCALE = 5\n"}
    assert "--- a/src/util.py" in str(diff)
    assert patch_path is not None
    assert patch_path.exists()


def test_changes_cover_only_the_changed_files(bundle: RepoBundle) -> None:
    """Render a version for novelty as diffs of the files it changes and nothing else."""
    changes = bundle.changes_text({"src/util.py": "SCALE = 4\n"})

    assert "-SCALE = 2" in changes
    assert "+SCALE = 4" in changes
    assert "app.py" not in changes
    assert "README" not in changes


def test_materializing_a_bundle_yields_the_runs_patch(bundle: RepoBundle, tmp_path: Path) -> None:
    """Turn a bundle into the ``git diff`` the parent scores, against the fetched commit."""
    patch = bundle.materialize({"src/util.py": "SCALE = 7\n", "src/new.py": "N = 1\n"})

    assert repo_tree.patch_paths(patch) == ["src/new.py", "src/util.py"]
    assert repo_tree.patch_violations(patch, ["src", "vendor"], ["vendor"]) == []
    fresh = repo_tree.unpack_tree(_chunks(tmp_path / "again", _FILES), tmp_path / "fresh")
    repo_tree.apply_patch(fresh, patch)
    assert (fresh / "src/util.py").read_text() == "SCALE = 7\n"
    assert bundle.materialize({}) == ""
    with pytest.raises(ValueError, match="outside the editable paths"):
        bundle.materialize({"README.md": "x\n"})


def _blocked_bundle(tmp_path: Path, constrained: bool) -> tuple[RepoBundle, list[str]]:
    """Unpack the test repository with a recorder for path-rule blocks.

    Args:
        tmp_path: Scratch folder.
        constrained: Whether a constraints file governs the run.

    Returns:
        The bundle rules and the list each block's reason goes to.
    """
    blocks: list[str] = []
    repo = {
        "chunks": _chunks(tmp_path, _FILES),
        "editable_paths": ["src", "vendor"],
        "readonly_paths": ["vendor"],
        "constrained": constrained,
    }
    return RepoBundle.prepare(repo, "", tmp_path / "checkout", on_block=blocks.append), blocks


@pytest.mark.parametrize(
    ("path", "rule"), [("README.md", "outside the editable paths"), ("vendor/lib.py", "read-only")]
)
def test_a_constrained_path_block_gives_the_illegal_action_message_and_logs_the_reason(
    tmp_path: Path, path: str, rule: str
) -> None:
    """Under a constraints file, a path-rule refusal reads the fixed message; the reason goes to the block log.

    Args:
        tmp_path: Scratch folder.
        path: Path the model tries to change.
        rule: Expected part of the logged reason.
    """
    bundle, blocks = _blocked_bundle(tmp_path, constrained=True)

    files, applied, error = bundle.apply({}, f'<FILE path="{path}">x</FILE>')

    assert (files, applied, error) == (None, 0, repo_tree.ILLEGAL_ACTION_MESSAGE)
    assert len(blocks) == 1
    assert path in blocks[0]
    assert rule in blocks[0]
    with pytest.raises(ValueError) as refused:
        bundle.materialize({path: "x\n"})
    assert str(refused.value) == repo_tree.ILLEGAL_ACTION_MESSAGE
    assert len(blocks) == 2


def test_a_constrained_bundle_keeps_other_refusals_as_they_were(tmp_path: Path) -> None:
    """Refuse a binary file with its own message: only path rules are illegal actions."""
    bundle, blocks = _blocked_bundle(tmp_path, constrained=True)

    _, _, error = bundle.apply({}, '<FILE path="src/logo.png">x</FILE>')

    assert "binary or larger" in str(error)
    assert blocks == []


def test_an_unconstrained_path_block_keeps_naming_the_path(tmp_path: Path) -> None:
    """Without a constraints file, the refusal names the path and logs no block."""
    bundle, blocks = _blocked_bundle(tmp_path, constrained=False)

    _, _, error = bundle.apply({}, '<FILE path="README.md">x</FILE>')

    assert error == "Cannot change 'README.md': it is outside the editable paths."
    with pytest.raises(ValueError, match="outside the editable paths"):
        bundle.materialize({"README.md": "x\n"})
    assert blocks == []


def test_a_seed_patch_becomes_the_starting_commit(tmp_path: Path) -> None:
    """Commit the seed version first, so bundles hold only changes beyond it and patches still include it."""
    chunks = _chunks(tmp_path, {"src/app.py": "A = 1\n"})
    base = repo_tree.unpack_tree(chunks, tmp_path / "base")
    (base / "src/app.py").write_text("A = 2\n")
    seed = repo_tree.version_patch(base)
    bundle = RepoBundle.prepare({"chunks": chunks, "editable_paths": ["src"]}, seed, tmp_path / "checkout")

    assert bundle.start_text("src/app.py") == "A = 2\n"
    assert bundle.materialize({}) == seed
    assert "+A = 3" in bundle.materialize({"src/app.py": "A = 3\n"})


def test_feedback_mentions_name_files_by_path_or_unique_name() -> None:
    """Pick files the scorer's feedback names, by full path or a file name only one file has."""
    paths = ["src/app.py", "src/util.py", "lib/util.py", "src/a.py"]
    feedback = "Traceback: File src/app.py line 3; util.py failed; data.py missing; a.pyc ignored"

    assert mentioned_paths(feedback, paths) == ["src/app.py"]
    assert mentioned_paths("see a.py and lib/util.py", paths) == ["src/a.py", "lib/util.py"]


def test_crossover_needs_a_partner() -> None:
    """Never pick crossover without another version, and fall back when only crossover is weighted."""
    rng = random.Random(0)
    picks = {choose_patch_type(["diff", "full", "cross"], [0.2, 0.2, 0.6], False, rng) for _ in range(50)}
    assert picks == {"diff", "full"}
    assert choose_patch_type(["diff", "full", "cross"], [0.0, 0.0, 1.0], False, rng) in {"diff", "full"}
    assert choose_patch_type(["diff", "full", "cross"], [0.0, 0.0, 1.0], True, rng) == "cross"


def _program(files: dict[str, str], score: float | None = None, feedback: str = "") -> SimpleNamespace:
    """Stand in for an upstream program record.

    Args:
        files: Bundle files.
        score: Combined score.
        feedback: Scorer feedback.

    Returns:
        An object with the fields the prompt builder reads.
    """
    return SimpleNamespace(code=encode_bundle(files), combined_score=score, text_feedback=feedback)


def test_prompts_show_the_tree_changed_files_and_files_feedback_mentions(bundle: RepoBundle) -> None:
    """Show the editable files, the parent's changed files and the files its feedback names."""
    parent = _program({"src/util.py": "SCALE = 3\n"}, 0.5, "AssertionError in src/app.py: expected 6")

    system, user = bundle.prompt("diff", parent, system_message="TASK", meta_recommendations="try caching")

    assert system.startswith("TASK")
    assert "try caching" in system
    assert "SEARCH" in system
    assert "<OPEN>" in system
    assert "src/app.py\nsrc/util.py" in user
    assert "## src/util.py\n```\nSCALE = 3\n```" in user
    assert "# Files the feedback mentions\n## src/app.py" in user
    assert "Score: 0.5" in user


def test_crossover_prompts_compare_the_two_versions_file_by_file(bundle: RepoBundle) -> None:
    """Show each file the two versions disagree on, from both sides, and ask for full files."""
    parent = _program({"src/util.py": "SCALE = 3\n"}, 0.5)
    partner = _program({"src/util.py": "SCALE = 4\n", "src/new.py": "N = 1\n"}, 0.7)
    other = _program({"src/app.py": "def answer():\n    return 2\n"}, 0.6)

    system, user = bundle.prompt("cross", parent, system_message="TASK", inspirations=[partner, other], partner=partner)

    assert "complete new contents" in system
    assert "### src/util.py in the current version\n## src/util.py\n```\nSCALE = 3" in user
    assert "### src/util.py in the other version\n## src/util.py\n```\nSCALE = 4" in user
    assert "### src/new.py in the current version\n## src/new.py\n```\n(absent)" in user
    assert "# Earlier versions" in user
    assert "+    return 2" in user


# The worker's hidden list lands with SKY-386 and SKY-387; these tests use a fixed one.
_HIDDEN = ["optimize.toml", "src/metric", ".optimize/exploits", "src/reference.json", "src/cases.jsonl"]
_HIDDEN_FILES = [
    "optimize.toml",
    "src/metric/score.py",
    ".optimize/exploits/log.md",
    "src/reference.json",
    "src/cases.jsonl",
]


def _hidden_bundle(tmp_path: Path) -> tuple[RepoBundle, list[dict[str, str]]]:
    """Unpack the test repository plus scoring files, with a recorder for hidden-path refusals.

    Args:
        tmp_path: Scratch folder.

    Returns:
        The bundle rules and the list each refusal goes to.
    """
    refusals: list[dict[str, str]] = []
    files = {**_FILES, **dict.fromkeys(_HIDDEN_FILES, "secret = 1\n")}
    repo = {
        "chunks": _chunks(tmp_path, files),
        "editable_paths": ["src", ".optimize", "optimize.toml"],
        "hidden_paths": _HIDDEN,
    }
    return RepoBundle.prepare(repo, "", tmp_path / "checkout", on_refusal=refusals.append), refusals


@pytest.mark.parametrize("path", _HIDDEN_FILES)
def test_a_hidden_path_is_refused_on_open_and_on_edit_and_the_refusal_is_reported(tmp_path: Path, path: str) -> None:
    """Refuse an open or an edit of each hidden path by exact name, show no content, and report the tool and kind."""
    bundle, refusals = _hidden_bundle(tmp_path)
    kind = "config" if path == "optimize.toml" else "scoring"

    _, _, message = bundle.apply({}, f"<OPEN>{path}</OPEN>")
    assert harness_bridge.GUARD_REFUSAL in str(message)
    assert "secret" not in str(message)

    files, applied, error = bundle.apply({}, f'<FILE path="{path}">x</FILE>')
    assert (files, applied, error) == (None, 0, harness_bridge.GUARD_REFUSAL)
    assert refusals == [{"tool": "open", "kind": kind}, {"tool": "edit", "kind": kind}]


@pytest.mark.parametrize("path", ["./optimize.toml", "src/../optimize.toml", ".//optimize.toml", "/optimize.toml"])
def test_another_form_of_a_hidden_path_is_refused_and_reported(tmp_path: Path, path: str) -> None:
    """Refuse and report an open or an edit of a hidden path written in a form that normalizes to it."""
    bundle, refusals = _hidden_bundle(tmp_path)

    _, _, message = bundle.apply({}, f"<OPEN>{path}</OPEN>")
    assert harness_bridge.GUARD_REFUSAL in str(message)
    assert "Not available" not in str(message)

    files, applied, error = bundle.apply({}, f'<FILE path="{path}">x</FILE>')
    assert (files, applied, error) == (None, 0, harness_bridge.GUARD_REFUSAL)
    assert refusals == [{"tool": "open", "kind": "config"}, {"tool": "edit", "kind": "config"}]


def test_hidden_paths_stay_out_of_the_file_list_and_other_files_still_open(tmp_path: Path) -> None:
    """Leave hidden paths out of the editable files the prompt lists, and keep other files open."""
    bundle, refusals = _hidden_bundle(tmp_path)

    listed = bundle.paths()
    assert "src/app.py" in listed
    assert not set(_HIDDEN_FILES) & set(listed)
    _, _, message = bundle.apply({}, "<OPEN>src/util.py</OPEN>")
    assert "SCALE = 2" in str(message)
    assert refusals == []
