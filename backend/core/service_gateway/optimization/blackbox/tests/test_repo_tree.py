"""Tests for shipping a repository tree and checking the versions written against it."""

from __future__ import annotations

import base64
import io
import subprocess
import tarfile
from pathlib import Path

import pytest

from .....constants import HARDENING_EXPLOITS_DIR
from ..repo_constraints import _DEFAULT_EXPLOITS
from ..repo_tree import (
    EXPLOITS_DIR,
    apply_patch,
    archive_chunks,
    drop_paths,
    linked_hidden_paths,
    patch_paths,
    patch_violations,
    reset_tree,
    strip_archive,
    unpack_tree,
    version_patch,
)

_GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]


def _tree(root: Path, files: dict[str, str]) -> Path:
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
    subprocess.run(["tar", "-czf", str(archive), "-C", str(source), "."], check=True)
    return archive


def _unpacked(tmp_path: Path, files: dict[str, str]) -> Path:
    """Ship ``files`` through base64 chunks and rebuild them as a checkout.

    Args:
        tmp_path: Scratch folder.
        files: Relative path to text.

    Returns:
        The checkout.
    """
    chunk_paths = []
    for index, chunk in enumerate(archive_chunks(_tree(tmp_path, files))):
        chunk_paths.append(tmp_path / f"chunk.{index}")
        chunk_paths[-1].write_text(chunk)
    return unpack_tree(chunk_paths, tmp_path / "checkout")


def test_a_version_round_trips_through_a_patch(tmp_path: Path) -> None:
    """Capture edits, additions and deletions as one patch that rebuilds the same tree."""
    checkout = _unpacked(tmp_path, {"src/app.py": "x = 1\n", "src/old.py": "gone\n", "README.md": "hi\n"})
    (checkout / "src/app.py").write_text("x = 2\n")
    (checkout / "src/old.py").unlink()
    (checkout / "src/new file.py").write_text("y = 3\n")
    (checkout / "src/blob.bin").write_bytes(bytes(range(256)))

    patch = version_patch(checkout)
    reset_tree(checkout)
    assert (checkout / "src/app.py").read_text() == "x = 1\n"
    assert not (checkout / "src/new file.py").exists()
    apply_patch(checkout, patch)

    assert (checkout / "src/app.py").read_text() == "x = 2\n"
    assert not (checkout / "src/old.py").exists()
    assert (checkout / "src/new file.py").read_text() == "y = 3\n"
    assert (checkout / "src/blob.bin").read_bytes() == bytes(range(256))
    assert set(patch_paths(patch)) == {"src/app.py", "src/old.py", "src/new file.py", "src/blob.bin"}
    assert patch_violations(patch, ["src"]) == []


def test_commits_in_the_checkout_stay_in_the_version(tmp_path: Path) -> None:
    """Diff against the shipped commit, so committing an edit cannot hide it."""
    checkout = _unpacked(tmp_path, {"src/a.py": "a\n"})
    (checkout / "src/a.py").write_text("b\n")
    subprocess.run([*_GIT, "commit", "--quiet", "-am", "agent"], cwd=checkout, check=True)

    assert patch_paths(version_patch(checkout)) == ["src/a.py"]
    reset_tree(checkout)
    assert (checkout / "src/a.py").read_text() == "a\n"


def test_ignored_files_are_left_out_of_a_version(tmp_path: Path) -> None:
    """Keep caches and build output the repository ignores out of the patch."""
    checkout = _unpacked(tmp_path, {".gitignore": "*.log\n", "src/a.py": "a\n"})
    (checkout / "src/run.log").write_text("noise\n")
    (checkout / "src/a.py").write_text("b\n")

    assert patch_paths(version_patch(checkout)) == ["src/a.py"]


def test_edits_outside_the_editable_paths_are_reported(tmp_path: Path) -> None:
    """Name every file a version may not touch, including the targets of a rename."""
    checkout = _unpacked(tmp_path, {"src/a.py": "a\n", "tests/test_a.py": "t\n"})
    (checkout / "tests/test_a.py").write_text("rigged\n")
    (checkout / "lib").mkdir()
    subprocess.run([*_GIT, "mv", "src/a.py", "lib/a.py"], cwd=checkout, check=True)

    problems = patch_violations(version_patch(checkout), ["src"])

    assert any("tests/test_a.py" in problem for problem in problems)
    assert any("lib/a.py" in problem for problem in problems)


def test_editable_root_allows_everything_but_read_only_paths(tmp_path: Path) -> None:
    """Treat ``.`` as the whole tree while submodules and LFS files stay read-only."""
    checkout = _unpacked(tmp_path, {"src/a.py": "a\n", "assets/model.bin": "lfs pointer\n"})
    (checkout / "src/a.py").write_text("b\n")
    (checkout / "assets/model.bin").write_text("swapped\n")

    problems = patch_violations(version_patch(checkout), ["."], ["assets/model.bin"])

    assert problems == ["'assets/model.bin' is a submodule or Git LFS file, which are read-only."]


@pytest.mark.parametrize(
    "header",
    [
        "diff --git a/../etc/passwd b/../etc/passwd",
        "diff --git a/.git/config b/.git/config",
        'diff --git "a/src/\\303\\251.py" "b/src/../../x.py"',
    ],
)
def test_paths_that_escape_the_repository_are_refused(header: str) -> None:
    """Reject traversal and git metadata no matter how the header spells them."""
    assert any("not a path inside" in problem for problem in patch_violations(header + "\n", ["."]))


def test_quoted_unicode_paths_are_decoded() -> None:
    """Read git's octal-escaped names as the real file names."""
    assert patch_paths('diff --git "a/src/\\303\\251.py" "b/src/\\303\\251.py"\n') == ["src/é.py"]


def test_submodule_changes_are_refused() -> None:
    """Refuse any patch that adds or moves a gitlink."""
    patch = "diff --git a/vendor/lib b/vendor/lib\nnew file mode 160000\nindex 0000000..abc1234\n"

    assert any("Submodules are read-only" in problem for problem in patch_violations(patch, ["."]))


def _reserved(path: str) -> str:
    """Write the reason the guard gives for a write to the exploit suite.

    Args:
        path: The refused repository path.

    Returns:
        The exact reason text.
    """
    return f"'{path}' may not be changed."


def test_the_exploit_folder_constants_agree() -> None:
    """Keep the stdlib copy of the suite folder equal to the backend's two copies."""
    assert EXPLOITS_DIR == HARDENING_EXPLOITS_DIR == _DEFAULT_EXPLOITS


@pytest.mark.parametrize(
    "path", [".optimize/exploits/a/patch.diff", ".optimize/exploits", ".optimize/Exploits/a.diff", ".OPTIMIZE/EXPLOITS"]
)
def test_writes_to_the_exploit_suite_are_refused(path: str) -> None:
    """Refuse a version that writes the exploit suite, in any case, even with the whole tree editable."""
    patch = f"diff --git a/{path} b/{path}\nnew file mode 100644\n"

    assert patch_violations(patch, ["."]) == [_reserved(path)]


@pytest.mark.parametrize("mode", ["100644", "120000"])
def test_a_file_at_a_parent_of_the_suite_is_refused(mode: str) -> None:
    """Refuse a ``.optimize`` file or a ``.optimize -> src`` symlink, which would block the suite's patch."""
    patch = (
        f"diff --git a/.optimize b/.optimize\nnew file mode {mode}\nindex 0000000..abc1234\n"
        "--- /dev/null\n+++ b/.optimize\n@@ -0,0 +1 @@\n+src\n\\ No newline at end of file\n"
    )

    assert patch_violations(patch, ["."]) == [_reserved(".optimize")]


@pytest.mark.parametrize(
    ("patch", "path"),
    [
        (
            "diff --git a/.optimize/exploits/a.diff b/notes/a.diff\nsimilarity index 100%\n"
            "rename from .optimize/exploits/a.diff\nrename to notes/a.diff\n",
            ".optimize/exploits/a.diff",
        ),
        (
            "diff --git a/notes/a.diff b/.optimize/exploits/a.diff\nsimilarity index 100%\n"
            "rename from notes/a.diff\nrename to .optimize/exploits/a.diff\n",
            ".optimize/exploits/a.diff",
        ),
        (
            "diff --git a/.optimize/exploits/a.diff b/.optimize/exploits/a.diff\ndeleted file mode 100644\n"
            "index abc1234..0000000\n--- a/.optimize/exploits/a.diff\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n",
            ".optimize/exploits/a.diff",
        ),
        (
            "diff --git a/.optimize/exploits/run.sh b/.optimize/exploits/run.sh\nold mode 100644\nnew mode 100755\n",
            ".optimize/exploits/run.sh",
        ),
        (
            "diff --git a/.optimize/exploits/blob.bin b/.optimize/exploits/blob.bin\nnew file mode 100644\n"
            "index 0000000..abc1234\nGIT binary patch\nliteral 3\nKcmZ?wbNK)P\n\nliteral 0\nHcmV?d00001\n\n",
            ".optimize/exploits/blob.bin",
        ),
        (
            'diff --git "a/.optimize/exploits/caf\\303\\251.diff" "b/.optimize/exploits/caf\\303\\251.diff"\n'
            "new file mode 100644\n",
            ".optimize/exploits/caf\u00e9.diff",
        ),
    ],
    ids=["rename-out", "rename-in", "delete", "mode-change", "binary", "quoted"],
)
def test_every_kind_of_suite_change_is_refused(patch: str, path: str) -> None:
    """Refuse renames, deletes, mode changes, binary files and quoted paths in the suite."""
    assert patch_violations(patch, ["."]) == [_reserved(path)]


@pytest.mark.parametrize("folder", ["eval/attacks", "Eval/Attacks"])
def test_writes_to_a_configured_exploit_folder_are_refused(folder: str) -> None:
    """Guard the ``[metric] exploits`` folder the same way as the default one, in any case."""
    patch = "diff --git a/eval/attacks/x.diff b/eval/attacks/x.diff\nnew file mode 100644\n"

    assert patch_violations(patch, ["."]) == []
    assert patch_violations(patch, ["."], exploit_paths=[folder]) == [_reserved("eval/attacks/x.diff")]


def test_paths_next_to_the_exploit_suite_stay_editable() -> None:
    """Match the suite as a folder, not as a name prefix."""
    patch = "diff --git a/.optimize/exploits2.md b/.optimize/exploits2.md\nnew file mode 100644\n"

    assert patch_violations(patch, ["."]) == []


def test_oversized_patches_are_refused() -> None:
    """Stop a version larger than one sandbox upload before it is parsed."""
    assert "larger than" in patch_violations("+" * (9 * 1024 * 1024), ["."])[0]


def test_archive_chunks_rebuild_the_archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Split an archive into base64 pieces that decode back to the same bytes."""
    monkeypatch.setattr("core.service_gateway.optimization.blackbox.repo_tree.ARCHIVE_CHUNK_BYTES", 7)
    archive = tmp_path / "blob"
    archive.write_bytes(bytes(range(50)))

    pieces = list(archive_chunks(archive))

    assert len(pieces) == 8
    assert b"".join(base64.b64decode(piece) for piece in pieces) == bytes(range(50))


def test_protected_paths_are_refused_even_under_the_whole_tree(tmp_path: Path) -> None:
    """A protected file is named with its rule while an editable sibling passes."""
    checkout = _unpacked(tmp_path, {"src/a.py": "a\n", "eval/labels.json": "[]\n"})
    (checkout / "src/a.py").write_text("b\n")
    (checkout / "eval/labels.json").write_text("[1]\n")

    problems = patch_violations(version_patch(checkout), ["."], [], ["eval"])

    assert problems == ["'eval/labels.json' is protected by the repository rules ([paths] protected)."]


def test_strip_archive_leaves_out_every_hidden_path(tmp_path: Path) -> None:
    """The agent's copy has no metric file and no exploit folder; every other file stays (SKY-353)."""
    archive = _tree(
        tmp_path,
        {
            "src/app.py": "x = 1\n",
            "eval/scorer.py": "def score(): ...\n",
            "eval/data.csv": "1\n",
            "evaluation/keep.py": "kept\n",
            ".optimize/exploits/cheat.py": "cheat\n",
        },
    )

    stripped = strip_archive(archive, ["eval/scorer.py", ".optimize/exploits"], tmp_path / "agent.tgz")

    with tarfile.open(stripped) as packed:
        names = {name.removeprefix("./") for name in packed.getnames()}
    assert {"src/app.py", "eval/data.csv", "evaluation/keep.py"} <= names
    assert not {name for name in names if name.startswith(("eval/scorer.py", ".optimize/exploits"))}
    with tarfile.open(archive) as full:
        assert "eval/scorer.py" in {name.removeprefix("./") for name in full.getnames()}


def test_strip_archive_drops_a_link_to_a_hidden_file(tmp_path: Path) -> None:
    """A hard link to a metric file would carry its bytes, so it goes too."""
    archive = tmp_path / "tree.tgz"
    with tarfile.open(archive, "w:gz") as packed:
        data = tarfile.TarInfo("eval/scorer.py")
        data.size = 1
        packed.addfile(data, io.BytesIO(b"x"))
        link = tarfile.TarInfo("src/copy.py")
        link.type, link.linkname = tarfile.LNKTYPE, "eval/scorer.py"
        packed.addfile(link)

    with tarfile.open(strip_archive(archive, ["eval"], tmp_path / "agent.tgz")) as packed:
        assert packed.getnames() == []


def _linked_tree(archive: Path, links: dict[str, tuple[bytes, str]]) -> Path:
    """Pack one real scoring file and the given links.

    Args:
        archive: Archive path to write.
        links: Link path to its tar type and target.

    Returns:
        The archive.
    """
    with tarfile.open(archive, "w:gz") as packed:
        data = tarfile.TarInfo("real/score.py")
        data.size = 1
        packed.addfile(data, io.BytesIO(b"x"))
        for name, (kind, target) in links.items():
            link = tarfile.TarInfo(name)
            link.type, link.linkname = kind, target
            packed.addfile(link)
    return archive


@pytest.mark.parametrize(
    ("links", "hidden", "linked"),
    [
        pytest.param({"scoring": (tarfile.SYMTYPE, "real")}, ["scoring"], ["scoring"], id="hidden-is-a-symlink"),
        pytest.param({"./scoring/": (tarfile.SYMTYPE, "real")}, ["scoring"], ["scoring"], id="member-name-form"),
        pytest.param({"eval": (tarfile.SYMTYPE, "real")}, ["eval/score.py"], ["eval/score.py"], id="below-a-symlink"),
        pytest.param({"eval/x": (tarfile.SYMTYPE, "../../etc")}, ["eval"], ["eval"], id="symlink-out-of-tree"),
        pytest.param({"eval/x": (tarfile.SYMTYPE, "/etc/passwd")}, ["eval"], ["eval"], id="absolute-symlink"),
        pytest.param({"eval/x": (tarfile.LNKTYPE, "real/score.py")}, ["eval"], ["eval"], id="hard-link-out"),
        pytest.param({"eval/x": (tarfile.LNKTYPE, "eval/y")}, ["eval"], [], id="hard-link-inside"),
        pytest.param({"src/x": (tarfile.SYMTYPE, "../real")}, ["eval", "real"], [], id="link-outside-hidden"),
        pytest.param({}, ["real"], [], id="no-links"),
    ],
)
def test_linked_hidden_paths_finds_each_link_that_would_leak(
    tmp_path: Path, links: dict[str, tuple[bytes, str]], hidden: list[str], linked: list[str]
) -> None:
    """The strip matches names, so a link at, above or inside a hidden path would leave its target visible."""
    assert linked_hidden_paths(_linked_tree(tmp_path / "tree.tgz", links), hidden) == linked


def test_drop_paths_removes_the_sections_that_touch_a_hidden_path() -> None:
    """A version's edit to a metric file is ignored; its other edits stay."""
    patch = (
        "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
        "diff --git a/eval/scorer.py b/eval/scorer.py\n--- a/eval/scorer.py\n+++ b/eval/scorer.py\n"
        "@@ -1 +1 @@\n-a\n+b\n"
    )

    kept = drop_paths(patch, ["eval/scorer.py"])

    assert patch_paths(kept) == ["src/app.py"]
    assert drop_paths(patch, []) == patch
    assert drop_paths("", ["eval"]) == ""


@pytest.mark.parametrize(
    ("setting", "value"),
    [("diff.mnemonicPrefix", "true"), ("diff.noprefix", "true"), ("diff.srcPrefix", "z/")],
)
def test_git_config_in_the_checkout_cannot_change_the_patch_headers(tmp_path: Path, setting: str, value: str) -> None:
    """The agent controls its checkout's git config, so a version always keeps ``a/``/``b/`` headers (SKY-353)."""
    checkout = _unpacked(tmp_path, {"src/a.py": "x = 1\n", "eval/scorer.py": "a\n"})
    subprocess.run(["git", "config", setting, value], cwd=checkout, check=True)
    (checkout / "eval/scorer.py").write_text("b\n")

    patch = version_patch(checkout)

    assert patch.startswith("diff --git a/eval/scorer.py b/eval/scorer.py\n")
    assert drop_paths(patch, ["eval/scorer.py"]) == ""


_HUNK = "@@ -1 +1 @@\n-a\n+b\n"


@pytest.mark.parametrize(
    "patch",
    [
        "diff --git c/eval/scorer.py i/eval/scorer.py\n--- c/eval/scorer.py\n+++ i/eval/scorer.py\n" + _HUNK,
        "diff --git a/src/a.py b/src/a.py\n--- c/eval/scorer.py\n+++ i/eval/scorer.py\n" + _HUNK,
        "--- a/eval/scorer.py\n+++ b/eval/scorer.py\n" + _HUNK,
        "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
        "--- a/eval/scorer.py\n+++ b/eval/scorer.py\n" + _HUNK,
        "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1,2 +1,2 @@\n-x = 1\n?junk\n",
    ],
    ids=["other-prefixes", "other-file-lines", "no-git-header", "after-a-hunk", "corrupt-hunk"],
)
def test_a_header_that_cannot_be_read_is_refused(patch: str) -> None:
    """``git apply`` would apply these, so a file header Skynet cannot read stops the version."""
    kept = drop_paths(patch, ["eval/scorer.py"])

    assert any("cannot read" in problem for problem in patch_violations(kept, ["."]))


def test_header_like_lines_inside_a_hunk_are_allowed() -> None:
    """A removed ``-- a/x`` line and an added ``++ b/x`` line look like headers but belong to the hunk."""
    patch = (
        "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n"
        "@@ -1,2 +1,2 @@\n--- a/x\n+++ b/x\n \n\\ No newline at end of file\n"
    )

    assert patch_violations(patch, ["src"]) == []


@pytest.mark.parametrize("breaker", ["\f", "\u2028", "\r"], ids=["form-feed", "line-separator", "lone-cr"])
def test_a_line_break_python_knows_but_git_does_not_stays_inside_its_hunk_line(breaker: str) -> None:
    """``git apply`` splits only on ``\\n``, so other line breaks must not shift the hunk count (SKY-353)."""
    patch = (
        "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n"
        f"@@ -1,3 +1,3 @@\n-x = 1\n+x = 'A{breaker}B'\n y = 2\n-z = 3\n+++ b/eval/scorer.py\n"
    )

    assert patch_paths(patch) == ["src/a.py"]
    assert patch_violations(patch, ["src"]) == []
