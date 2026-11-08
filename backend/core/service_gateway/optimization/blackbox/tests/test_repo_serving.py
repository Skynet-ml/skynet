"""Tests for serving a repository run's best version through its root ``serve.py``."""

from __future__ import annotations

import gzip
import json
import logging
import os
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import pytest

from core.billing.operation_pricing import json_fingerprint
from core.worker import repo_staging

from .. import repo_serving
from ..repo_constraints import ConstraintsError
from ..repo_serving import (
    INTERACTION_KIND,
    check_serve_contract,
    run_repo_serve,
    serve_payload,
    staged_snapshot,
)
from ..repo_tree import REPO_SNAPSHOT_KEY, archive_chunks, strip_archive, unpack_tree, version_patch

_PRE_STRIP_FINGERPRINT = "50df08bd377bfae0c59352e2cabcf4ae64996e51f9f5d7c1a256471c96aea2b6"
_SERVE = "def serve(input):\n    return {'answer': input['question'].upper()}\n"


def _archive(root: Path, files: dict[str, str]) -> Path:
    """Write ``files`` under ``root`` and pack them the way the parent ships a snapshot.

    Args:
        root: Scratch folder.
        files: Relative path to text.

    Returns:
        The packed archive.
    """
    source = root / "source"
    for name, text in files.items():
        (source / name).parent.mkdir(parents=True, exist_ok=True)
        (source / name).write_text(text)
    archive = root / "tree.tgz"
    # macOS tar would add AppleDouble ``._*`` members, which no real snapshot holds.
    subprocess.run(
        ["tar", "-czf", str(archive), "-C", str(source), "."],
        check=True,
        env={**os.environ, "COPYFILE_DISABLE": "1"},
    )
    return archive


def _chunks(root: Path, files: dict[str, str]) -> list[str]:
    """Ship ``files`` as the chunk files the sandbox upload writes.

    Args:
        root: Scratch folder.
        files: Relative path to text.

    Returns:
        Chunk file paths, in order.
    """
    paths = []
    for index, chunk in enumerate(archive_chunks(_archive(root, files))):
        path = root / f"chunk.{index}"
        path.write_text(chunk)
        paths.append(str(path))
    return paths


def _patch(root: Path, files: dict[str, str], edits: dict[str, str]) -> str:
    """Build the patch that turns ``files`` into ``files`` with ``edits`` applied.

    Args:
        root: Scratch folder.
        files: Starting tree.
        edits: Replaced file text.

    Returns:
        A ``git diff --binary`` patch against the starting tree.
    """
    scratch = root / "patch"
    scratch.mkdir()
    checkout = unpack_tree(_chunks(scratch, files), scratch / "checkout")
    for name, text in edits.items():
        (checkout / name).write_text(text)
    return version_patch(checkout)


def _guest_payload(chunks: list[str], patch: str, inputs: dict[str, Any]) -> dict[str, Any]:
    """Build the payload the guest sees after the sandbox upload.

    Args:
        chunks: Uploaded tree chunks.
        patch: Version to serve.
        inputs: Caller input.

    Returns:
        Guest payload.
    """
    return {"patch": patch, "inputs": inputs, REPO_SNAPSHOT_KEY: {"chunks": chunks, "archive": None}}


def test_serve_runs_the_best_version_not_the_starting_code(tmp_path: Path) -> None:
    files = {"serve.py": _SERVE, "lib.py": "X = 1\n"}
    best = "from lib import X\n\ndef serve(input):\n    return {'answer': input['question'][::-1], 'x': X}\n"
    patch = _patch(tmp_path, files, {"serve.py": best})
    guest = tmp_path / "guest"
    guest.mkdir()

    result = run_repo_serve(
        _guest_payload(_chunks(guest, files), patch, {"question": "abc"}),
        {"kind": INTERACTION_KIND, "optimization_id": "opt-1"},
    )

    assert result["outputs"] == {"answer": "cba", "x": 1}
    assert result["optimization_id"] == "opt-1"
    assert result["input_fields"] == ["question"]
    assert result["output_fields"] == ["answer", "x"]


def test_serve_without_a_patch_serves_the_starting_code(tmp_path: Path) -> None:
    result = run_repo_serve(
        _guest_payload(_chunks(tmp_path, {"serve.py": _SERVE}), "", {"question": "hi"}),
        {"optimization_id": "opt-1"},
    )

    assert result["outputs"] == {"answer": "HI"}


def test_a_non_dict_return_is_wrapped_as_output(tmp_path: Path) -> None:
    chunks = _chunks(tmp_path, {"serve.py": "def serve(input):\n    return len(input)\n"})

    result = run_repo_serve(_guest_payload(chunks, "", {"a": 1, "b": 2}), {"optimization_id": "o"})

    assert result["outputs"] == {"output": 2}


def test_a_failing_serve_reports_its_error(tmp_path: Path) -> None:
    chunks = _chunks(tmp_path, {"serve.py": "def serve(input):\n    raise KeyError('missing')\n"})

    with pytest.raises(RuntimeError, match="serve\\(\\) failed: KeyError"):
        run_repo_serve(_guest_payload(chunks, "", {}), {"optimization_id": "o"})


def test_a_missing_tree_is_refused() -> None:
    with pytest.raises(ValueError, match="did not reach the sandbox"):
        run_repo_serve({"patch": "", "inputs": {}}, {"optimization_id": "o"})


@pytest.mark.parametrize(
    ("files", "message"),
    [
        ({"other.py": "x = 1\n"}, "no serve.py"),
        ({"serve.py": "def serve(:\n"}, "does not parse"),
        ({"serve.py": "def run(input):\n    return input\n"}, "top-level function serve"),
        ({"serve.py": "def serve():\n    return 1\n"}, "one argument"),
    ],
)
def test_the_serve_contract_is_checked(tmp_path: Path, files: dict[str, str], message: str) -> None:
    for name, text in files.items():
        (tmp_path / name).write_text(text)

    with pytest.raises(ValueError, match=message):
        check_serve_contract(tmp_path)


def _job() -> dict[str, Any]:
    """Return a stored, finished repository run.

    Returns:
        Job row.
    """
    return {
        "payload": {
            "target": {"kind": "repo", "repo": {"repository": "acme/app", "commit": "a" * 40, "ref": "main"}},
            "scorer": {"model": {"name": "openai/gpt-4o-mini"}},
            "token_source": "managed",
        }
    }


def test_serve_payload_is_secret_free_and_stable() -> None:
    first = serve_payload(_job(), patch="diff", inputs={"q": 1}, optimization_id="opt-1")
    second = serve_payload(_job(), patch="diff", inputs={"q": 1}, optimization_id="opt-1")

    assert first == second
    assert first["repo"] == {"repository": "acme/app", "commit": "a" * 40}
    assert first["model_config"] == {"name": "openai/gpt-4o-mini"}
    assert first["_interaction"] == {"kind": INTERACTION_KIND, "optimization_id": "opt-1"}
    assert REPO_SNAPSHOT_KEY not in first


def test_staged_snapshot_fetches_with_the_owner_token_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: dict[str, Any] = {}

    def fake_fetch(repository: str, commit: str, token: str | None, workdir: Path) -> Any:
        """Record the fetch and pack a small tree."""
        calls.update(repository=repository, commit=commit, token=token, workdir=workdir)
        archive = _archive(workdir, {"serve.py": _SERVE})
        return type("Snapshot", (), {"archive": archive, "commit": commit})()

    monkeypatch.setattr(repo_serving, "github_token", lambda engine, owner: f"token-for-{owner}")
    monkeypatch.setattr(repo_serving, "fetch_snapshot", fake_fetch)
    payload = serve_payload(_job(), patch="", inputs={}, optimization_id="opt-1")

    with staged_snapshot(payload, engine=object(), owner="alice") as staged:
        archive = Path(staged[REPO_SNAPSHOT_KEY]["archive"])
        assert archive.is_file()
        assert REPO_SNAPSHOT_KEY not in payload

    assert calls["token"] == "token-for-alice"
    assert calls["commit"] == "a" * 40
    assert not calls["workdir"].exists()


_HIDDEN_TOML = """instructions = "docs/intent.md"

[paths]
editable = ["src"]

[run]
entry = "src/app.py:main"

[metric]
files = ["eval/scorer.py"]
exploits = "eval/attacks"
reference = "eval/reference.json"
illegal = "eval/metric-rules.md"
mcp = "eval/mcp.json"

[data]
cases = "data/cases.jsonl"
"""
_SCORING_SIDE = {
    "optimize.toml": _HIDDEN_TOML,
    "eval/scorer.py": "def score(repo_path, case):\n    return 1.0\n",
    "eval/attacks/cheat/why.md": "cheat\n",
    "eval/reference.json": "{}\n",
    "eval/metric-rules.md": "- Penalize answers with no postcode.\n",
    "eval/mcp.json": '{"mcpServers": {}}\n',
    "data/cases.jsonl": '{"input": {"question": "q"}, "expected": "Q"}\n',
}
_SERVING_SIDE = {
    "serve.py": _SERVE,
    "docs/intent.md": "Answer the question.\n",
    "src/app.py": "def main(case):\n    return case\n",
}


def _members(archive: Path) -> set[str]:
    """List the regular files in a packed tree.

    Args:
        archive: Packed tree.

    Returns:
        Each file's repository-relative path.
    """
    with tarfile.open(archive, "r:gz") as packed:
        return {member.name.removeprefix("./") for member in packed.getmembers() if member.isfile()}


def test_the_serving_archive_has_no_scoring_side_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The serving guest runs repository code, so it gets the stripped tree, like the candidate (E62)."""

    def fake_fetch(repository: str, commit: str, token: str | None, workdir: Path) -> Any:
        """Pack a tree with every scoring-side key set."""
        archive = _archive(workdir, {**_SCORING_SIDE, **_SERVING_SIDE})
        return type("Snapshot", (), {"archive": archive, "commit": commit})()

    monkeypatch.setattr(repo_serving, "github_token", lambda engine, owner: None)
    monkeypatch.setattr(repo_serving, "fetch_snapshot", fake_fetch)
    payload = serve_payload(_job(), patch="", inputs={}, optimization_id="opt-1")

    with staged_snapshot(payload, engine=None, owner=None) as staged:
        served = _members(Path(staged[REPO_SNAPSHOT_KEY]["archive"]))

    assert served == set(_SERVING_SIDE)
    for path in _SCORING_SIDE:
        assert path not in served


def _stage(monkeypatch: pytest.MonkeyPatch, files: dict[str, str], patch: str = "") -> Any:
    """Point the serving fetch at a real packed tree of ``files``.

    Args:
        monkeypatch: Replaces the GitHub token and fetch.
        files: The commit's tree.
        patch: The stored best version to serve.

    Returns:
        The ``staged_snapshot`` context for a finished run at that commit.
    """

    def fake_fetch(repository: str, commit: str, token: str | None, workdir: Path) -> Any:
        """Pack ``files`` as the commit's snapshot."""
        return type("Snapshot", (), {"archive": _archive(workdir, files), "commit": commit})()

    monkeypatch.setattr(repo_serving, "github_token", lambda engine, owner: None)
    monkeypatch.setattr(repo_serving, "fetch_snapshot", fake_fetch)
    return staged_snapshot(
        serve_payload(_job(), patch=patch, inputs={}, optimization_id="opt-1"), engine=None, owner=None
    )


def test_a_replay_stored_before_the_strip_keeps_its_request_fingerprint() -> None:
    """The stored serve request never held the tree, so rows written before the strip still replay."""
    payload = serve_payload(_job(), patch="diff", inputs={"q": 1}, optimization_id="opt-1")

    # Pinned from origin/staging at 3ebe6f732, before the serving tree was stripped.
    assert json_fingerprint(payload) == _PRE_STRIP_FINGERPRINT


def test_a_commit_from_before_optimize_toml_still_serves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A run pinned before the constraints file existed serves its tree, without the default exploit folder."""
    files = {"serve.py": _SERVE, "src/app.py": "X = 1\n", ".optimize/exploits/cheat.md": "cheat\n"}

    with _stage(monkeypatch, files) as staged:
        served = _members(Path(staged[REPO_SNAPSHOT_KEY]["archive"]))

    assert served == {"serve.py", "src/app.py"}


def test_a_commit_whose_optimize_toml_no_longer_validates_fails_clearly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refuse to serve when the hidden list is unknown, and name the broken file."""
    files = {"serve.py": _SERVE, "optimize.toml": '[metric]\nfiles = ["eval/scorer.py"]\nunknown_key = 1\n'}

    with pytest.raises(ConstraintsError, match=r"optimize\.toml"), _stage(monkeypatch, files):
        pytest.fail("the snapshot must not be yielded")


def test_a_finished_run_with_a_legacy_inline_illegal_list_refuses_to_serve(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run pinned while ``[proposer] illegal`` took inline rules now fails closed, because its hidden list is unknown."""
    files = {
        "serve.py": _SERVE,
        "optimize.toml": '[metric]\nfiles = ["eval/scorer.py"]\n\n[proposer]\nillegal = ["Never read eval/"]\n',
        "eval/scorer.py": f"# {_LEAK}\n",
    }

    with (
        pytest.raises(ConstraintsError, match=r"\[proposer\] illegal must be the path of a markdown rule file"),
        _stage(monkeypatch, files),
    ):
        pytest.fail("the snapshot must not be yielded")


def test_a_best_patch_that_edits_a_hidden_file_serves_only_its_visible_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An older best version edits the metric too; serving drops that part and applies the rest."""
    serve = "from src.app import X\n\ndef serve(input):\n    return {'x': X}\n"
    files = {
        "serve.py": serve,
        "src/app.py": "X = 1\n",
        "optimize.toml": '[metric]\nfiles = ["eval/scorer.py"]\n',
        "eval/scorer.py": "def score(repo_path, case):\n    return 0.0\n",
    }
    patch = _patch(tmp_path, files, {"src/app.py": "X = 2\n", "eval/scorer.py": f"# {_LEAK}\n"})
    assert "eval/scorer.py" in patch

    with _stage(monkeypatch, files, patch) as staged:
        assert "eval/scorer.py" not in staged["patch"]
        assert "src/app.py" in staged["patch"]
        guest = tmp_path / "guest"
        guest.mkdir()
        chunks = []
        for index, chunk in enumerate(archive_chunks(Path(staged[REPO_SNAPSHOT_KEY]["archive"]))):
            (guest / f"chunk.{index}").write_text(chunk)
            chunks.append(str(guest / f"chunk.{index}"))
        result = run_repo_serve(_guest_payload(chunks, staged["patch"], {}), {"optimization_id": "opt-1"})

    assert result["outputs"] == {"x": 2}


def test_a_best_patch_that_moves_a_hidden_file_out_serves_without_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rename from a hidden path would carry the metric into the served tree, so serving drops it."""
    metric = "".join(f"# {_LEAK} {line}\n" for line in range(20))
    files = {"serve.py": _SERVE, "optimize.toml": '[metric]\nfiles = ["eval"]\n', "eval/scorer.py": metric}
    scratch = tmp_path / "patch"
    scratch.mkdir()
    checkout = unpack_tree(_chunks(scratch, files), scratch / "checkout")
    (checkout / "eval" / "scorer.py").rename(checkout / "copied.py")
    patch = version_patch(checkout)
    assert "rename from eval/scorer.py" in patch

    with _stage(monkeypatch, files, patch) as staged:
        assert staged["patch"] == ""


_FOLDER_TOML = """[metric]
files = ["scoring"]
exploits = "scoring/attacks"
reference = "scoring/reference.json"

[data]
cases = "scoring/cases.jsonl"
"""
_FILE_TOML = """[metric]
files = ["metric.py"]
reference = "reference.json"

[data]
cases = "cases.jsonl"
"""
_LEAK = "SCORING_SECRET_4e2b"


@pytest.mark.parametrize(
    ("hidden", "served_side", "roots"),
    [
        pytest.param(
            {
                "optimize.toml": _FOLDER_TOML,
                "scoring/score.py": f"def score(repo_path, case):\n    return 1.0  # {_LEAK}\n",
                "scoring/helpers/util.py": f"K = '{_LEAK}'\n",
                "scoring/attacks/cheat/why.md": f"{_LEAK}\n",
                "scoring/reference.json": f'{{"k": "{_LEAK}"}}\n',
                "scoring/cases.jsonl": f'{{"input": {{"q": "a"}}, "expected": "{_LEAK}"}}\n',
            },
            {"serve.py": _SERVE, "src/app.py": "X = 1\n"},
            ("optimize.toml", "scoring"),
            id="hidden-folder",
        ),
        pytest.param(
            {
                "optimize.toml": _FILE_TOML,
                "metric.py": f"def score(repo_path, case):\n    return 1.0  # {_LEAK}\n",
                "reference.json": f'{{"k": "{_LEAK}"}}\n',
                "cases.jsonl": f'{{"input": {{"q": "a"}}, "expected": "{_LEAK}"}}\n',
                ".optimize/exploits/cheat.md": f"{_LEAK}\n",
            },
            {"serve.py": _SERVE, "metric_notes.md": "Public notes.\n"},
            ("optimize.toml", "metric.py", "reference.json", "cases.jsonl", ".optimize/exploits"),
            id="single-hidden-files",
        ),
    ],
)
def test_no_scoring_file_reaches_the_served_tar_metadata_or_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    hidden: dict[str, str],
    served_side: dict[str, str],
    roots: tuple[str, ...],
) -> None:
    """Check every tar member and byte, the yielded payload and every log record for the scoring side (E62).

    Args:
        tmp_path: Scratch folder.
        monkeypatch: Replaces the fetch.
        caplog: Captures every log record at debug level.
        hidden: Scoring-side files, each holding the leak marker.
        served_side: Files the serving guest needs.
        roots: The hidden files and folders the commit's ``optimize.toml`` names.
    """
    caplog.set_level(logging.DEBUG)

    with _stage(monkeypatch, {**hidden, **served_side}) as staged:
        archive = Path(staged[REPO_SNAPSHOT_KEY]["archive"])
        with tarfile.open(archive, "r:gz") as packed:
            members = packed.getmembers()
        raw = gzip.decompress(archive.read_bytes())
        metadata = json.dumps({**staged, REPO_SNAPSHOT_KEY: {**staged[REPO_SNAPSHOT_KEY], "archive": None}})

    names = {member.name.removeprefix("./") for member in members}
    assert {member.name.removeprefix("./") for member in members if member.isfile()} == set(served_side)
    for root in roots:
        assert not any(name == root or name.startswith(root + "/") for name in names), root
    assert _LEAK.encode() not in raw
    logs = caplog.text + "".join(str(record.args) for record in caplog.records)
    for path in hidden:
        assert path not in metadata
        assert path not in logs
    assert _LEAK not in metadata
    assert _LEAK not in logs


_RULES = "- Never read the scoring folder.\n"


def test_a_metric_rule_file_inside_a_hidden_folder_is_dropped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The scorer's own rule file is scoring side, so the serving tree leaves it out with its folder."""
    files = {
        "optimize.toml": '[metric]\nfiles = ["scoring"]\nillegal = "scoring/rules.md"\n',
        "scoring/score.py": "def score(repo_path, case):\n    return 1.0\n",
        "scoring/rules.md": _RULES,
        "serve.py": _SERVE,
    }

    with _stage(monkeypatch, files) as staged:
        served = _members(Path(staged[REPO_SNAPSHOT_KEY]["archive"]))

    assert served == {"serve.py"}


@pytest.mark.parametrize(
    ("setting", "toml"),
    [
        ("instructions", 'instructions = "scoring/rules.md"\n'),
        ("[proposer] illegal", '[proposer]\nillegal = "scoring/rules.md"\n'),
        ("[run] illegal", '[run]\nentry = "serve.py:serve"\nillegal = "scoring/rules.md"\n'),
    ],
)
def test_a_visible_rule_file_inside_a_hidden_folder_refuses_to_serve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, setting: str, toml: str
) -> None:
    """Refuse the commit instead of serving a tree whose visible rule file the strip would drop (E62).

    Args:
        tmp_path: Scratch folder.
        monkeypatch: Replaces the fetch.
        setting: The visible rule setting under test.
        toml: The commit's constraints file, with ``scoring`` hidden by ``[metric]``.
    """
    files = {
        "optimize.toml": toml + '\n[metric]\nfiles = ["scoring"]\n',
        "scoring/score.py": "def score(repo_path, case):\n    return 1.0\n",
        "scoring/rules.md": _RULES,
        "serve.py": _SERVE,
    }

    with pytest.raises(ConstraintsError, match="inside the hidden scoring files"), _stage(monkeypatch, files):
        pytest.fail("the snapshot must not be yielded")


def test_serving_strips_by_the_one_hidden_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Serving calls the staging hidden list itself, so it holds no copy of the rule (C3)."""
    assert repo_serving.hidden_repo_paths is repo_staging.hidden_repo_paths
    seen: list[tuple[str, ...]] = []

    def spy(archive: Path, hidden_paths: tuple[str, ...], destination: Path) -> Path:
        """Record the list the strip gets, then strip for real."""
        seen.append(tuple(hidden_paths))
        return strip_archive(archive, hidden_paths, destination)

    monkeypatch.setattr(repo_serving, "strip_archive", spy)
    monkeypatch.setattr(repo_serving, "hidden_repo_paths", lambda constraints: ("src",))
    files = {"serve.py": _SERVE, "src/app.py": "X = 1\n", ".optimize/exploits/cheat.md": "cheat\n"}

    with _stage(monkeypatch, files) as staged:
        served = _members(Path(staged[REPO_SNAPSHOT_KEY]["archive"]))

    assert seen == [("src",)]
    assert served == {"serve.py", ".optimize/exploits/cheat.md"}


def _stage_tree(monkeypatch: pytest.MonkeyPatch, files: dict[str, str], links: dict[str, str]) -> Any:
    """Point the serving fetch at a packed tree that also holds symlinks.

    Args:
        monkeypatch: Replaces the GitHub token and fetch.
        files: Relative path to text.
        links: Relative link path to its relative target.

    Returns:
        The ``staged_snapshot`` context for a finished run at that commit.
    """

    def fake_fetch(repository: str, commit: str, token: str | None, workdir: Path) -> Any:
        """Write the files and links, then pack them as the commit's snapshot."""
        source = workdir / "source"
        for name, target in links.items():
            (source / name).parent.mkdir(parents=True, exist_ok=True)
            (source / name).symlink_to(target)
        return type("Snapshot", (), {"archive": _archive(workdir, files), "commit": commit})()

    monkeypatch.setattr(repo_serving, "github_token", lambda engine, owner: None)
    monkeypatch.setattr(repo_serving, "fetch_snapshot", fake_fetch)
    return staged_snapshot(serve_payload(_job(), patch="", inputs={}, optimization_id="opt-1"), engine=None, owner=None)


def test_a_hidden_path_with_a_trailing_slash_is_still_dropped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``scoring/`` names the same folder as ``scoring``."""
    files = {
        "optimize.toml": '[metric]\nfiles = ["scoring/"]\n',
        "scoring/score.py": f"# {_LEAK}\n",
        "serve.py": _SERVE,
    }

    with _stage(monkeypatch, files) as staged:
        served = _members(Path(staged[REPO_SNAPSHOT_KEY]["archive"]))

    assert served == {"serve.py"}


def test_a_hidden_path_with_a_dot_slash_prefix_refuses_to_serve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``./`` path is refused when the file is read, so no unmatched hidden path reaches the strip."""
    files = {"optimize.toml": '[metric]\nfiles = ["./scoring"]\n', "scoring/score.py": "x = 1\n", "serve.py": _SERVE}

    with pytest.raises(ConstraintsError, match="must be relative"), _stage(monkeypatch, files):
        pytest.fail("the snapshot must not be yielded")


def test_hidden_paths_match_case_exactly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Paths compare case by case, like git and the Linux sandbox, so ``Scoring`` does not hide ``scoring``."""
    files = {"optimize.toml": '[metric]\nfiles = ["Scoring"]\n', "scoring/score.py": "x = 1\n", "serve.py": _SERVE}

    with _stage(monkeypatch, files) as staged:
        served = _members(Path(staged[REPO_SNAPSHOT_KEY]["archive"]))

    assert served == {"serve.py", "scoring/score.py"}


def test_a_symlink_into_a_hidden_folder_carries_no_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A link stays in the served tree, but its target does not, so the guest reads nothing through it."""
    files = {"optimize.toml": '[metric]\nfiles = ["scoring"]\n', "scoring/score.py": f"# {_LEAK}\n", "serve.py": _SERVE}
    links = {"peek.py": "scoring/score.py", "peekdir": "scoring"}

    with _stage_tree(monkeypatch, files, links) as staged:
        archive = Path(staged[REPO_SNAPSHOT_KEY]["archive"])
        raw = gzip.decompress(archive.read_bytes())
        guest = tmp_path / "guest"
        guest.mkdir()
        chunks = []
        for index, chunk in enumerate(archive_chunks(archive)):
            (guest / f"chunk.{index}").write_text(chunk)
            chunks.append(str(guest / f"chunk.{index}"))
        tree = unpack_tree(chunks, guest / "tree")

    assert _LEAK.encode() not in raw
    assert (tree / "peek.py").is_symlink()
    assert not (tree / "peek.py").exists()
    assert not (tree / "peekdir").exists()
    assert not (tree / "scoring").exists()


def test_a_hidden_path_that_is_a_symlink_refuses_to_serve(monkeypatch: pytest.MonkeyPatch) -> None:
    """The strip would drop only the link and keep its target, so serving refuses an older run's commit."""
    files = {"optimize.toml": '[metric]\nfiles = ["scoring"]\n', "real/score.py": f"# {_LEAK}\n", "serve.py": _SERVE}

    with (
        pytest.raises(ConstraintsError, match=r"hidden scoring path 'scoring' is, holds or lies inside a link"),
        _stage_tree(monkeypatch, files, {"scoring": "real"}),
    ):
        pass
