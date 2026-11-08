"""Tests for serving a repository run's best version through its root ``serve.py``."""

from __future__ import annotations

import os
import subprocess
import tarfile
from pathlib import Path
from typing import Any

import pytest

from .. import repo_serving
from ..repo_serving import (
    INTERACTION_KIND,
    check_serve_contract,
    run_repo_serve,
    serve_payload,
    staged_snapshot,
)
from ..repo_tree import REPO_SNAPSHOT_KEY, archive_chunks, unpack_tree, version_patch

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
