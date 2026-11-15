"""Tests for the setup box, the candidate box and the metric box of a repository workspace."""

from __future__ import annotations

import base64
import builtins
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from core.billing.vercel_usage import PACKAGE_REGISTRY_HOSTS

from .. import repo_workspace
from ..repo_constraints import parse_constraints, scorer_only_paths
from ..repo_tree import unpack_tree, version_patch
from ..repo_workspace import (
    METRIC_REPO_DIR,
    METRIC_SITE_DIR,
    PREPARED_DIR,
    WORK_DIR,
    CandidateBox,
    MetricBox,
    RepoSetupError,
    RepoWorkspace,
    read_metric_requirements,
    redact,
)
from ..sandbox import CommandResult, LocalSubprocessRuntime, SandboxSpec

# The metric setup step's pip install, which the fake box answers without the network.
_PIP_INSTALL = re.compile(r"python3 -m pip install [^&]*")


def _archive(root: Path, files: dict[str, str]) -> Path:
    """Pack ``files`` the way the parent ships a fetched tree.

    Args:
        root: Scratch folder.
        files: Relative path to text.

    Returns:
        The archive.
    """
    source = root / "source"
    for name, text in files.items():
        (source / name).parent.mkdir(parents=True, exist_ok=True)
        (source / name).write_text(text)
    archive = root / "tree.tgz"
    # macOS tar would add AppleDouble ``._`` members that the Linux fetch never makes.
    subprocess.run(
        ["tar", "-czf", str(archive), "-C", str(source), "."], check=True, env={**os.environ, "COPYFILE_DISABLE": "1"}
    )
    return archive


def _patch(root: Path, archive: Path, edits: dict[str, str]) -> str:
    """Write ``edits`` into a copy of the tree and return them as a version.

    Args:
        root: Scratch folder.
        archive: Packed tree.
        edits: Relative path to new text.

    Returns:
        The version's patch.
    """
    root.mkdir(parents=True, exist_ok=True)
    chunk = root / "chunk"
    chunk.write_text(base64.b64encode(archive.read_bytes()).decode())
    checkout = unpack_tree([chunk], root / "agent")
    for name, text in edits.items():
        (checkout / name).parent.mkdir(parents=True, exist_ok=True)
        (checkout / name).write_text(text)
    return version_patch(checkout)


class _Box:
    """Run commands on the host, and record what each box was opened with and sent."""

    def __init__(self, inner: Any, spec: SandboxSpec) -> None:
        """Wrap a local session.

        Args:
            inner: The real local session commands run in.
            spec: The spec the box opened with.
        """
        self._inner = inner
        self.spec = spec
        self.online = not spec.network_disabled
        self.commands: list[tuple[str, dict[str, str] | None]] = []
        self.root_commands: list[str] = []
        self.writes: dict[str, str] = {}
        self.closed = False

    @property
    def path(self) -> Path:
        """Return the box's folder on the host."""
        return self._inner.path

    def write_files(self, files: dict[str, str]) -> None:
        """Write into the wrapped box and record the files."""
        self.writes.update(files)
        self._inner.write_files(files)

    def read_file(self, path: str) -> str | None:
        """Read from the wrapped box."""
        return self._inner.read_file(path)

    def run(self, command: str, *, sudo: bool = False, **options: Any) -> CommandResult:
        """Record the command, and answer the metric setup's pip install without the network.

        A local box refuses root on the developer's machine, so a root
        command is recorded and then runs as the test's user.

        Args:
            command: The shell command.
            sudo: The command asks for root.
            **options: The run's other arguments.

        Returns:
            The command's result.
        """
        self.commands.append((command, options.get("env")))
        if sudo:
            self.root_commands.append(command)
        return self._inner.run(_PIP_INSTALL.sub("true", command), **options)

    def close(self) -> None:
        """Close the wrapped box."""
        self.closed = True
        self._inner.close()


class _Runtime:
    """Open recording boxes over the local runtime."""

    def __init__(self) -> None:
        """Start with no box."""
        self._local = LocalSubprocessRuntime()
        self.boxes: list[_Box] = []

    def open(self, spec: SandboxSpec) -> _Box:
        """Open one recording box."""
        box = _Box(self._local.open(spec), spec)
        self.boxes.append(box)
        return box


_ONLINE = SandboxSpec(lifetime_seconds=60, allowed_hosts=("pypi.org",), operation_key="op")


def _space(archive: Path, runtime: Any, *, setup: str | None, **options: Any) -> RepoWorkspace:
    """Build a workspace over ``archive`` with one secret.

    Args:
        archive: Packed tree.
        runtime: Where its boxes open.
        setup: The setup command.
        **options: More :class:`RepoWorkspace` arguments.

    Returns:
        The workspace.
    """
    return RepoWorkspace(
        runtime=runtime,
        spec=_ONLINE,
        archive=archive,
        editable_paths=["src"],
        readonly_paths=[],
        setup_command=setup,
        secrets={"API_KEY": "very-secret-value"},
        **options,
    )


def _open(space: RepoWorkspace, patch: str) -> CandidateBox:
    """Open a candidate box that must lay the version out.

    Args:
        space: The workspace.
        patch: The version.

    Returns:
        The open box.
    """
    opened = space.open_candidate(patch)
    assert isinstance(opened, CandidateBox), opened
    return opened


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[tuple[RepoWorkspace, _Runtime, Path, Path]]:
    """Open a workspace whose setup builds a file.

    Args:
        tmp_path: Scratch folder.

    Yields:
        The workspace, its runtime, the archive and the scratch folder.
    """
    archive = _archive(tmp_path, {"src/app.py": "x = 1\n", "tests/test_app.py": "t\n"})
    runtime = _Runtime()
    space = _space(archive, runtime, setup='echo "key=${API_KEY:-unset}"; cat src/app.py > built.txt')
    yield space, runtime, archive, tmp_path
    space.close()


def test_setup_runs_once_in_its_own_box_and_each_version_gets_a_new_candidate_box(
    workspace: tuple[RepoWorkspace, _Runtime, Path, Path],
) -> None:
    """Setup runs once online without secrets; each version opens a new offline box from the saved state (SKY-398)."""
    space, runtime, archive, root = workspace

    first = _open(space, _patch(root / "one", archive, {"src/app.py": "x = 2\n"}))
    assert first.session.read_file(f"{WORK_DIR}/src/app.py") == "x = 2\n"
    assert first.session.read_file(f"{WORK_DIR}/built.txt") == "x = 1\n"
    first.session.run(f"touch {WORK_DIR}/stray.txt")
    first.close()
    second = _open(space, "")
    try:
        assert second.session.read_file(f"{WORK_DIR}/src/app.py") == "x = 1\n"
        assert second.session.read_file(f"{WORK_DIR}/stray.txt") is None
    finally:
        second.close()

    setup, *candidates = runtime.boxes
    assert len(candidates) == 2
    assert setup.closed
    assert all(box.closed for box in candidates)
    assert [command for box in runtime.boxes for command, _ in box.commands if "built.txt" in command] == [
        next(command for command, _ in setup.commands if "built.txt" in command)
    ]
    assert setup.online
    assert setup.spec.allowed_hosts == ("pypi.org",)
    assert all(env is None for _, env in setup.commands)
    assert "key=unset" in space.setup_log
    assert all(not box.online and box.spec.snapshot_archive for box in candidates)
    keys = [box.spec.operation_key for box in runtime.boxes]
    assert keys == ["op:setup-0", "op:candidate-1", "op:candidate-2"]


def test_failed_setup_and_broken_patches_are_reported(tmp_path: Path) -> None:
    """Stop on a setup that exits non-zero, and explain a patch that does not apply."""
    archive = _archive(tmp_path, {"src/app.py": "x = 1\n"})
    failing = _space(archive, _Runtime(), setup="echo boom; exit 3")
    with pytest.raises(RepoSetupError, match="exit 3") as failed:
        failing.prepare()
    assert "boom" in str(failed.value)
    with pytest.raises(RepoSetupError, match="exit 3"):
        failing.open_candidate("")

    runtime = _Runtime()
    space = _space(archive, runtime, setup=None)
    try:
        broken = space.open_candidate(
            "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-y\n+z\n"
        )
    finally:
        space.close()
    assert not isinstance(broken, CandidateBox)
    assert broken.problems[0].startswith("The change does not apply cleanly")
    assert runtime.boxes[-1].closed


def test_versions_outside_the_editable_paths_never_reach_a_box(
    workspace: tuple[RepoWorkspace, _Runtime, Path, Path],
) -> None:
    """Refuse a version that edits the tests before any box opens."""
    space, runtime, archive, root = workspace

    result = space.open_candidate(_patch(root / "one", archive, {"tests/test_app.py": "rigged\n"}))

    assert not isinstance(result, CandidateBox)
    assert result.problems == ("'tests/test_app.py' is outside the editable paths.",)
    assert runtime.boxes == []


def test_redact_hides_long_values_only() -> None:
    """Mask real secrets while leaving short values that would scramble ordinary output."""
    assert redact("key=abcdefgh on=1", ["abcdefgh", "1"]) == "key=[secret] on=1"


def test_protected_paths_win_over_editable_ones_before_the_box_opens(tmp_path: Path) -> None:
    """Lay out an allowed edit, but refuse one to a protected judge file inside an editable folder."""
    archive = _archive(tmp_path, {"src/app.py": "x = 1\n", "src/judge.py": "ok = True\n"})
    runtime = _Runtime()
    space = _space(archive, runtime, setup=None, protected_paths=["optimize.toml", "src/judge.py"])
    try:
        rigged = space.open_candidate(_patch(tmp_path / "rigged", archive, {"src/judge.py": "ok = 'always'\n"}))
        assert not isinstance(rigged, CandidateBox)
        assert rigged.problems == ("'src/judge.py' is protected by the repository rules ([paths] protected).",)
        assert runtime.boxes == []
        rules = space.open_candidate(
            _patch(tmp_path / "rules", archive, {"optimize.toml": "[paths]\neditable = ['.']\n"})
        )
        assert not isinstance(rules, CandidateBox)
        allowed = _open(space, _patch(tmp_path / "allowed", archive, {"src/app.py": "x = 2\n"}))
        assert allowed.path == WORK_DIR
        allowed.close()
    finally:
        space.close()


def _guarded(tmp_path: Path) -> tuple[RepoWorkspace, Path]:
    """Build a workspace whose evaluation folder is protected and whose source is editable.

    Args:
        tmp_path: Scratch folder.

    Returns:
        The workspace and its archive.
    """
    archive = _archive(tmp_path, {"src/app.py": "x = 1\n", "eval/labels.csv": "a,1\n", "eval/judge.py": "ok = True\n"})
    space = _space(archive, _Runtime(), setup=None, protected_paths=["eval", "optimize.toml"])
    return space, archive


def test_a_source_only_version_whose_code_rewrites_the_labels_is_caught(tmp_path: Path) -> None:
    """Flag protected files the version's code changes while it runs, even though its patch names only source."""
    space, archive = _guarded(tmp_path)
    try:
        version = _patch(tmp_path / "one", archive, {"src/app.py": "x = 2\n"})
        box = _open(space, version)
        assert box.stamp_before_run() is None
        assert box.protected_changes() == ()
        box.close()

        box = _open(space, version)
        box.stamp_before_run()
        box.session.run(f"cd {WORK_DIR} && printf 'a,0\\n' > eval/labels.csv && touch eval/extra.csv")
        assert box.protected_changes() == (
            "'eval/extra.csv' is protected by the repository rules ([paths] protected),"
            " and the version's code changed it while it ran.",
            "'eval/labels.csv' is protected by the repository rules ([paths] protected),"
            " and the version's code changed it while it ran.",
        )
        box.close()

        box = _open(space, version)
        box.stamp_before_run()
        box.session.run(f"rm {WORK_DIR}/eval/judge.py")
        assert [problem.split("'")[1] for problem in box.protected_changes()] == ["eval/judge.py"]
        box.close()
    finally:
        space.close()


def test_a_protected_file_rewritten_and_restored_while_the_version_runs_is_caught(tmp_path: Path) -> None:
    """Flag a protected file the version's code rewrites and then restores, and a protected file it adds and removes."""
    space, archive = _guarded(tmp_path)
    try:
        version = _patch(tmp_path / "one", archive, {"src/app.py": "x = 2\n"})
        box = _open(space, version)
        box.stamp_before_run()
        box.session.run(
            f"cd {WORK_DIR} && saved=$(cat eval/labels.csv) && printf 'a,0\\n' > eval/labels.csv"
            " && printf '%s\\n' \"$saved\" > eval/labels.csv"
        )
        assert [problem.split("'")[1] for problem in box.protected_changes()] == ["eval/labels.csv"]
        box.close()

        box = _open(space, version)
        box.stamp_before_run()
        box.session.run(f"cd {WORK_DIR} && touch eval/extra.csv && rm eval/extra.csv")
        assert [problem.split("'")[1] for problem in box.protected_changes()] == ["eval"]
        box.close()

        box = _open(space, version)
        box.stamp_before_run()
        box.session.run(f"cd {WORK_DIR} && cat eval/labels.csv eval/judge.py > /dev/null")
        assert box.protected_changes() == ()
        box.close()
    finally:
        space.close()


def test_a_stage_answer_checks_the_protected_files_and_is_written_in_one_run(tmp_path: Path) -> None:
    """Write each answer in the check's own run, so the stamps before the run stay valid and a change still shows."""
    space, archive = _guarded(tmp_path)
    try:
        box = _open(space, _patch(tmp_path / "one", archive, {"src/app.py": "x = 2\n"}))
        box.stamp_before_run()
        session = box.session
        runs = len(session.commands)
        assert box.answer("calls/000001/stages/a.json", '{"output": 1}') == ()
        assert len(session.commands) == runs + 1
        assert session.read_file("calls/000001/stages/a.json") == '{"output": 1}'
        assert box.protected_changes() == ()

        session.run(f"cd {WORK_DIR} && printf 'a,0\\n' > eval/labels.csv")
        changes = box.answer("calls/000001/stages/b.json", '{"output": 2}')
        assert [problem.split("'")[1] for problem in changes or ()] == ["eval/labels.csv"]
        assert session.read_file("calls/000001/stages/b.json") == '{"output": 2}'
        box.close()
    finally:
        space.close()


def test_every_protected_file_check_starts_as_root(tmp_path: Path) -> None:
    """Start each check as root, so a process that the version leaves running cannot rewrite its output (PRE-1)."""
    space, archive = _guarded(tmp_path)
    try:
        box = _open(space, _patch(tmp_path / "one", archive, {"src/app.py": "x = 2\n"}))
        session = box.session
        session.root_commands.clear()
        box.stamp_before_run()
        box.protected_changes()
        box.answer("calls/000001/stages/a.json", '{"output": 1}')
        checks = [command for command, _ in session.commands if shlex.quote(repo_workspace._HASH_PROTECTED) in command]
        assert len(checks) == 3
        assert session.root_commands == checks
        box.close()
    finally:
        space.close()


def _run_check_as(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tree: Path,
    *,
    euid: int,
    fault: str | None = None,
    status: str | None = "CapEff:\t000001ffffffffff\n",
) -> tuple[list[tuple[str, int]], dict[str, Any]]:
    """Run the protected-file check in this process, with a fake effective user and no real switch.

    Args:
        monkeypatch: Patches ``os`` and the process state.
        capsys: Captures the check's output.
        tree: The checkout the check runs in.
        euid: The effective user that the check starts as.
        fault: ``"refuse"`` makes the switch fail as for a root with no
            capabilities, ``"stay"`` makes ``setuid`` keep root, and
            ``"root-tree"`` makes root own the tree.
        status: The text of ``/proc/self/status``, or ``None`` when it
            cannot be read. The default holds every capability.

    Returns:
        Each user switch and tree access with the effective user at that moment,
        and the check's printed result.
    """
    state = {"euid": euid}
    events: list[tuple[str, int]] = []

    def switch(name: str) -> Any:
        """Make a fake ``os`` user switch.

        Args:
            name: The ``os`` function name.

        Returns:
            The fake function.
        """

        def call(value: Any) -> None:
            """Record the switch, and change the fake effective user on ``setuid``."""
            if fault == "refuse":
                raise PermissionError(name)
            events.append((name, state["euid"]))
            if name == "setuid" and fault != "stay":
                state["euid"] = value

        return call

    def watch(name: str, real: Any) -> Any:
        """Wrap a tree access so that it records the effective user.

        Args:
            name: The event name.
            real: The real function.

        Returns:
            The wrapper.
        """

        def call(*args: Any, **kwargs: Any) -> Any:
            """Record the access, then do it."""
            events.append((f"{name} {args[0]}", state["euid"]))
            return real(*args, **kwargs)

        return call

    real_stat = os.stat

    def stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        """Report root as the tree's owner when the fault asks for it."""
        found = real_stat(path, *args, **kwargs)
        if fault == "root-tree" and path == ".":
            return os.stat_result((found.st_mode, found.st_ino, found.st_dev, found.st_nlink, 0, 0, *found[6:10]))
        return found

    monkeypatch.setattr(os, "geteuid", lambda: state["euid"])
    for name in ("setgroups", "setgid", "setuid"):
        monkeypatch.setattr(os, name, switch(name))
    monkeypatch.setattr(os, "stat", stat)
    for name in ("walk", "open", "unlink", "replace"):
        monkeypatch.setattr(os, name, watch(name, getattr(os, name)))
    monkeypatch.chdir(tree)
    monkeypatch.setattr(sys, "argv", ["-c", "eval"])
    monkeypatch.setenv("APP_ANSWER_PATH", "calls/a.json")
    monkeypatch.setenv("APP_ANSWER", '{"output": 1}')
    read = watch("read", open)

    def reader(path: Any, *args: Any, **kwargs: Any) -> Any:
        """Answer the capability check from ``status``, and watch every other read."""
        if path != "/proc/self/status":
            return read(path, *args, **kwargs)
        if status is None:
            raise PermissionError(path)
        return io.StringIO(status)

    names = {**vars(builtins), "open": reader}
    exec(compile(repo_workspace._HASH_PROTECTED, "<check>", "exec"), {"__builtins__": names})
    return events, json.loads(capsys.readouterr().out)


def _planted_tree(tmp_path: Path) -> Path:
    """Build a checkout whose answer path is a link that the version planted into another file.

    Args:
        tmp_path: Scratch folder.

    Returns:
        The checkout.
    """
    tree = tmp_path / "tree"
    (tree / "eval").mkdir(parents=True)
    (tree / "eval" / "labels.csv").write_text("a,1\n")
    (tree / "calls").mkdir()
    target = tmp_path / "root-only"
    target.write_text("secret\n")
    (tree / "calls" / "a.json.tmp").symlink_to(target)
    return tree


def test_the_check_becomes_the_tree_owner_before_it_reads_or_writes_the_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Drop root before the first tree access, and never follow a link planted at the answer path (PRE-1)."""
    tree = _planted_tree(tmp_path)
    owner = tree.stat().st_uid
    assert owner != 0
    events, result = _run_check_as(monkeypatch, capsys, tree, euid=0)
    assert [name for name, _ in events[:3]] == ["setgroups", "setgid", "setuid"]
    accesses = events[3:]
    assert [name for name, _ in accesses] == [
        "walk eval",
        "read eval/labels.csv",
        "unlink calls/a.json.tmp",
        "open calls/a.json.tmp",
        "replace calls/a.json.tmp",
    ]
    assert {user for _, user in accesses} == {owner}
    assert set(result["files"]) == {"eval/labels.csv"}
    assert (tree / "calls" / "a.json").read_text() == '{"output": 1}'
    assert not (tree / "calls" / "a.json").is_symlink()
    assert (tmp_path / "root-only").read_text() == "secret\n"


@pytest.mark.parametrize("fault", ["root-tree", "refuse", "stay"])
def test_a_root_check_that_cannot_leave_root_stops_before_the_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], fault: str
) -> None:
    """Fail closed when root owns the tree, the switch fails, or the check is still root after it (PRE-1)."""
    tree = _planted_tree(tmp_path)
    with pytest.raises(SystemExit) as stopped:
        _run_check_as(monkeypatch, capsys, tree, euid=0, fault=fault)
    assert stopped.value.code not in (0, None)
    output = capsys.readouterr()
    assert output.out == ""
    assert not (tree / "calls" / "a.json").exists()
    assert (tmp_path / "root-only").read_text() == "secret\n"


def test_a_root_without_capabilities_hashes_and_answers_as_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Run as a root with no capability, as runner._entry_user does in a local Docker box (PRE-1)."""
    tree = _planted_tree(tmp_path)
    events, result = _run_check_as(monkeypatch, capsys, tree, euid=0, status="CapEff:\t0000000000000000\n")
    assert not [name for name, _ in events if name.startswith("set")]
    assert {user for _, user in events} == {0}
    assert set(result["files"]) == {"eval/labels.csv"}
    assert (tree / "calls" / "a.json").read_text() == '{"output": 1}'
    assert (tmp_path / "root-only").read_text() == "secret\n"


@pytest.mark.parametrize("status", [None, "Name:\tpython3\n"], ids=["unreadable", "no-CapEff-line"])
def test_a_root_whose_capabilities_are_unknown_still_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], status: str | None
) -> None:
    """Count an unreadable or incomplete ``/proc/self/status`` as holding capabilities."""
    tree = _planted_tree(tmp_path)
    with pytest.raises(SystemExit) as stopped:
        _run_check_as(monkeypatch, capsys, tree, euid=0, fault="refuse", status=status)
    assert stopped.value.code not in (0, None)
    assert capsys.readouterr().out == ""
    assert not (tree / "calls" / "a.json").exists()


def test_a_box_user_check_needs_no_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Skip the switch for a non-root check, as in the local test runtime."""
    tree = _planted_tree(tmp_path)
    start = os.geteuid()
    events, result = _run_check_as(monkeypatch, capsys, tree, euid=start)
    assert not [name for name, _ in events if name.startswith("set")]
    assert {user for _, user in events} == {start}
    assert set(result["files"]) == {"eval/labels.csv"}
    assert (tree / "calls" / "a.json").read_text() == '{"output": 1}'
    assert (tmp_path / "root-only").read_text() == "secret\n"


def test_a_failed_answer_check_is_told_apart_from_a_protected_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Return ``None`` when the check fails, yet still write the answer so the scorer stops waiting."""
    space, archive = _guarded(tmp_path)
    try:
        box = _open(space, _patch(tmp_path / "one", archive, {"src/app.py": "x = 2\n"}))
        box.stamp_before_run()
        session = box.session
        run = session.run
        monkeypatch.setattr(
            session,
            "run",
            lambda command, **options: (
                CommandResult(exit_code=1) if command.startswith("export APP_ANSWER_PATH") else run(command, **options)
            ),
        )
        assert box.answer("calls/000001/stages/a.json", '{"output": 1}') is None
        assert session.read_file("calls/000001/stages/a.json") == '{"output": 1}'
        assert box.protected_changes() == ()
        box.close()
    finally:
        space.close()


def test_a_large_stage_answer_is_written_by_file_and_stamped_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Send an answer too large for the environment through a file write, then stamp the files again."""
    monkeypatch.setattr(repo_workspace, "_ANSWER_ENV_BYTES", 10)
    space, archive = _guarded(tmp_path)
    try:
        box = _open(space, _patch(tmp_path / "one", archive, {"src/app.py": "x = 2\n"}))
        box.stamp_before_run()
        text = json.dumps({"output": "x" * 50})
        assert box.answer("calls/000001/stages/a.json", text) == ()
        assert box.session.read_file("calls/000001/stages/a.json") == text
        assert all(not command.startswith("export APP_ANSWER_PATH") for command, _ in box.session.commands)
        assert box.protected_changes() == ()
        box.close()
    finally:
        space.close()


def test_a_write_into_the_prepared_tree_never_reaches_the_next_version(tmp_path: Path) -> None:
    """Each candidate box opens from the saved state, so one version's writes end with its box (E38, SKY-398)."""
    space, archive = _guarded(tmp_path)
    try:
        version = _patch(tmp_path / "one", archive, {"src/app.py": "x = 2\n"})
        box = _open(space, version)
        box.session.run(f"printf 'a,0\\n' > {PREPARED_DIR}/eval/labels.csv && touch {PREPARED_DIR}/planted.py")
        box.close()

        box = _open(space, version)
        assert box.session.read_file(f"{WORK_DIR}/eval/labels.csv") == "a,1\n"
        assert box.session.read_file(f"{WORK_DIR}/planted.py") is None
        box.stamp_before_run()
        assert box.protected_changes() == ()
        box.close()
    finally:
        space.close()


def test_a_box_whose_prepared_tree_does_not_match_the_digest_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The parent checks the digest it kept; a box with another tree ends before any version is laid out (SKY-398)."""
    runtime = _Runtime()
    space = _space(_archive(tmp_path, {"src/app.py": "x = 1\n"}), runtime, setup=None)
    try:
        space.prepare()
        monkeypatch.setattr(space, "_prepared_digest", "0" * 64)
        with pytest.raises(RepoSetupError, match="does not hold the prepared repository"):
            space.open_candidate("")
        assert runtime.boxes[-1].closed
        assert all("version.patch" not in command for command, _ in runtime.boxes[-1].commands)
    finally:
        space.close()


def test_a_python3_that_the_version_plants_on_path_never_hashes_the_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hash with the base interpreter, so a planted ``python3`` cannot hide a protected change (PRE-1).

    The box user can write ``/app/.venv/bin``, which is on the box PATH. The
    planted ``python3`` replays a clean fingerprint and the prepared digest.
    """
    planted = tmp_path / "planted"
    planted.mkdir()
    monkeypatch.setattr(repo_workspace, "BASE_PYTHON", sys.executable, raising=False)
    archive = _archive(tmp_path, {"src/app.py": "x = 1\n", "eval/labels.csv": "a,1\n"})
    spec = replace(_ONLINE, env={"PATH": os.pathsep.join([str(planted), os.environ.get("PATH", "/usr/bin:/bin")])})
    space = RepoWorkspace(
        runtime=_Runtime(),
        spec=spec,
        archive=archive,
        editable_paths=["src"],
        readonly_paths=[],
        setup_command=None,
        secrets={},
        protected_paths=["eval"],
    )
    try:
        box = _open(space, "")
        clean = space.fingerprint(box.session, WORK_DIR)
        box.close()
        assert clean is not None
        marker = tmp_path / "planted-python3-ran"
        forged = json.dumps({"files": clean[0], "stamps": clean[1]})
        tool = planted / "python3"
        tool.write_text(
            "#!/bin/sh\n"
            f"touch {shlex.quote(str(marker))}\n"
            f'case "$*" in *"def digest(path)"*) echo {shlex.quote(forged)};;'
            f" *) echo {space._prepared_digest};; esac\n"
        )
        tool.chmod(0o755)
        box = _open(space, "")
        try:
            assert box.stamp_before_run() is None
            box.session.run(f"printf 'a,0\\n' > {WORK_DIR}/eval/labels.csv")
            assert [problem.split("'")[1] for problem in box.protected_changes()] == ["eval/labels.csv"]
        finally:
            box.close()
        assert not marker.exists()
    finally:
        space.close()


def test_a_workspace_without_protected_paths_skips_the_check(
    workspace: tuple[RepoWorkspace, _Runtime, Path, Path],
) -> None:
    """Run no fingerprinting when the repository protects nothing."""
    space, runtime, _, _ = workspace

    box = _open(space, "")

    assert box.protected_changes() == ()
    box.close()
    assert all("def digest(path)" not in command for each in runtime.boxes for command, _ in each.commands)


_SCORING_TOML = (
    'version = 1\n[paths]\neditable = ["src"]\n\n'
    '[run]\nentry = "a.py:f"\n\n'
    '[metric]\nfiles = ["eval/scorer.py"]\nreference = "eval/reference.json"\n'
    'illegal = "docs/metric_rules.md"\nmcp = "eval/.mcp.json"\n\n'
    '[data]\ncases = "data/cases.jsonl"\n'
)
_SCORING_SIDE = {
    "optimize.toml": _SCORING_TOML,
    "eval/scorer.py": "def score(): return 0.1\n",
    "eval/reference.json": "{}\n",
    "docs/metric_rules.md": "- No cheating.\n",
    "eval/.mcp.json": "{}\n",
    "data/cases.jsonl": '{"input": 1, "expected": 2}\n',
    ".optimize/exploits/cheat.py": "cheat\n",
}


def _scoring_space(tmp_path: Path, runtime: _Runtime, **options: Any) -> tuple[RepoWorkspace, Path]:
    """Build a workspace over a repository with a full scoring side.

    Args:
        tmp_path: Scratch folder.
        runtime: Where its boxes open.
        **options: More :class:`RepoWorkspace` arguments.

    Returns:
        The workspace and its archive.
    """
    archive = _archive(tmp_path, {"src/app.py": "x = 1\n", **_SCORING_SIDE})
    hidden = scorer_only_paths(parse_constraints(_SCORING_TOML.encode()))
    return _space(archive, runtime, setup="true", hidden_paths=hidden, **options), archive


def test_the_candidate_box_never_holds_the_scoring_side_files(tmp_path: Path) -> None:
    """The setup box and the candidate box get the tree without the metric files (E61, SKY-398)."""
    assert scorer_only_paths(parse_constraints(_SCORING_TOML.encode())) == (
        "optimize.toml",
        "eval/scorer.py",
        ".optimize/exploits",
        "eval/reference.json",
        "data/cases.jsonl",
        "docs/metric_rules.md",
        "eval/.mcp.json",
    )
    runtime = _Runtime()
    space, archive = _scoring_space(tmp_path, runtime)
    try:
        box = _open(space, _patch(tmp_path / "one", archive, {"src/app.py": "x = 2\n"}))
        found = box.session.run(f"cd {WORK_DIR} && cat {' '.join(_SCORING_SIDE)} 2>/dev/null; true")
        assert found.stdout == ""
        assert box.session.read_file(f"{WORK_DIR}/src/app.py") == "x = 2\n"
        box.close()
    finally:
        space.close()
    texts = [text for each in runtime.boxes for text in (*each.writes.values(), *(c for c, _ in each.commands))]
    assert not [text for text in texts if "return 0.1" in text or "No cheating" in text]


def test_the_metric_box_holds_only_the_metric_files_at_their_repository_paths(tmp_path: Path) -> None:
    """M has no repository tree; ``repo_path`` holds just the hidden files (SKY-398)."""
    runtime = _Runtime()
    space, _ = _scoring_space(tmp_path, runtime)
    try:
        metric = space.open_metric()
        assert isinstance(metric, MetricBox)
        assert metric.repo_path == f"{metric.root}/{METRIC_REPO_DIR}"
        metric.install_metric_files()
        listed = metric.session.run(f"cd {METRIC_REPO_DIR} && find . -type f | sort").stdout.split()
        assert listed == sorted(f"./{name}" for name in _SCORING_SIDE)
        assert metric.session.run("test ! -e .app-repo").ok
        assert metric.session.spec.private
        metric.close()
    finally:
        space.close()


def test_a_version_that_edits_a_metric_file_is_scored_with_the_base_metric(tmp_path: Path) -> None:
    """The metric comes from the base commit: a version's edit to it is dropped, its other edits stay (SKY-353)."""
    archive = _archive(tmp_path, {"src/app.py": "x = 1\n", "src/metric.py": "def score(): return 0.1\n"})
    space = _space(archive, _Runtime(), setup=None, hidden_paths=["src/metric.py", ".optimize/exploits"])
    try:
        edits = {"src/app.py": "x = 2\n", "src/metric.py": "def score(): return 1.0\n", ".optimize/exploits/a": "a\n"}
        patch = _patch(tmp_path / "cheat", archive, edits)
        assert space.violations(patch) == []
        box = _open(space, patch)
        assert box.session.read_file(f"{WORK_DIR}/src/metric.py") is None
        assert box.session.read_file(f"{WORK_DIR}/src/app.py") == "x = 2\n"
        box.close()
        metric = space.open_metric()
        metric.install_metric_files()
        assert metric.session.read_file(f"{METRIC_REPO_DIR}/src/metric.py") == "def score(): return 0.1\n"
        assert metric.session.read_file(f"{METRIC_REPO_DIR}/src/app.py") is None
        assert metric.session.read_file(f"{METRIC_REPO_DIR}/.optimize/exploits/a") is None
        metric.close()
    finally:
        space.close()


def test_a_failed_metric_install_is_a_setup_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A metric archive that does not unpack fails the evaluation, not the parent."""
    space, _ = _scoring_space(tmp_path, _Runtime())
    try:
        metric = space.open_metric()
        monkeypatch.setattr(space, "metric_files", "not an archive")
        with pytest.raises(RepoSetupError, match="metric files could not be installed"):
            metric.install_metric_files()
        metric.close()
    finally:
        space.close()


def test_each_role_sizes_and_walls_off_its_own_box(tmp_path: Path) -> None:
    """``[run]`` hosts and vCPUs reach C, ``[metric]`` hosts and vCPUs reach M (SKY-398)."""
    runtime = _Runtime()
    space, _ = _scoring_space(
        tmp_path, runtime, run_hosts=["api.example.com"], run_vcpus=4, metric_hosts=(), metric_vcpus=2
    )
    try:
        _open(space, "").close()
        space.open_metric().close()
    finally:
        space.close()
    _, candidate, metric = runtime.boxes
    assert (candidate.spec.network_disabled, candidate.spec.allowed_hosts, candidate.spec.vcpus) == (
        False,
        ("api.example.com",),
        4,
    )
    assert (metric.spec.network_disabled, metric.spec.allowed_hosts, metric.spec.vcpus) == (True, (), 2)
    assert not candidate.spec.private
    assert metric.spec.private
    assert candidate.spec.operation_key != metric.spec.operation_key


def test_the_metric_setup_installs_only_the_declared_packages_in_a_box_without_repository_code(
    tmp_path: Path,
) -> None:
    """The metric setup box gets the package list only, and its state holds no repository file (SKY-398).

    The repository has no ``[setup]`` command, so its own spec is offline; the metric setup box still
    reaches the package registries, and pip installs into the metric state's own folder.
    """
    runtime = _Runtime()
    archive = _archive(
        tmp_path,
        {
            "src/app.py": "SOURCE = 1\n",
            "eval/metric.py": "METRIC = 1\n",
            ".optimize/metric-requirements.txt": "# the metric's packages\nnumpy==2.1.0\n\nscipy>=1.13\n",
        },
    )
    space = RepoWorkspace(
        runtime=runtime,
        spec=SandboxSpec(lifetime_seconds=60, network_disabled=True, operation_key="op"),
        archive=archive,
        editable_paths=["src"],
        readonly_paths=[],
        setup_command=None,
        secrets={},
        hidden_paths=["eval/metric.py"],
    )
    try:
        space.prepare()
        metric = space.open_metric()
        assert metric.session.run("test ! -e .app-repo").ok
        metric.close()
        assert metric.session.spec.snapshot_archive
        with tarfile.open(metric.session.spec.snapshot_archive) as state:
            names = state.getnames()
    finally:
        space.close()
    assert not [name for name in names if "src" in name or "app.py" in name or "metric.py" in name]
    setup, metric_setup, metric_box = runtime.boxes
    assert metric_setup.spec.operation_key == "op:metric-setup-1"
    assert not setup.online
    assert metric_setup.online
    assert metric_setup.spec.allowed_hosts == PACKAGE_REGISTRY_HOSTS
    assert metric_setup.writes == {".app-metric/requirements.txt": "numpy==2.1.0\nscipy>=1.13\n"}
    [install] = [command for command, _ in metric_setup.commands if "pip install" in command]
    assert f"pip install --target {METRIC_SITE_DIR} " in install
    assert "--user" not in install
    assert metric.site_path.endswith(f"/{METRIC_SITE_DIR}")
    assert setup.closed
    assert metric_setup.closed
    assert not metric_box.online


@pytest.mark.parametrize("line", ["-r other.txt", "--index-url https://x.example.com", "./local", "git+https://x/y"])
def test_a_metric_requirement_that_is_not_a_package_fails_setup(tmp_path: Path, line: str) -> None:
    """Options, paths and URLs could install repository code or reach other hosts, so they fail."""
    archive = _archive(tmp_path, {".optimize/metric-requirements.txt": f"numpy\n{line}\n"})
    with pytest.raises(RepoSetupError, match="is not a package requirement"):
        read_metric_requirements(archive)


class _SnapshotRuntime(_Runtime):
    """A runtime with snapshots: the saved state is the box's folder, copied by key."""

    def __init__(self, *, failing: bool = False) -> None:
        """Choose whether each snapshot fails.

        Args:
            failing: Raise from every snapshot.
        """
        super().__init__()
        self._failing = failing
        self.saved: dict[str, Path] = {}

    def snapshot(self, session: _Box, cache_key: str) -> str | None:
        """Copy the box's folder as the saved state."""
        if self._failing:
            raise RuntimeError("provider text that names the snapshot")
        copy = Path(tempfile.mkdtemp(prefix="snapshot-"))
        subprocess.run(["cp", "-a", f"{session.path}/.", str(copy)], check=True)
        self.saved[cache_key] = copy
        return f"snap-{len(self.saved)}"

    def open(self, spec: SandboxSpec) -> _Box:
        """Open a box, restoring a snapshot's folder into it."""
        box = super().open(spec)
        if spec.snapshot_id is not None:
            source = list(self.saved.values())[int(spec.snapshot_id.split("-")[1]) - 1]
            subprocess.run(["cp", "-a", f"{source}/.", str(box.path)], check=True)
        return box


def test_a_runtime_with_snapshots_opens_each_candidate_box_from_the_snapshot(tmp_path: Path) -> None:
    """On Vercel the saved state is a snapshot, named in ``snapshot_id`` (SKY-398)."""
    runtime = _SnapshotRuntime()
    archive = _archive(tmp_path, {"src/app.py": "x = 1\n"})
    space = RepoWorkspace(
        runtime=runtime,
        spec=_ONLINE,
        archive=archive,
        editable_paths=["src"],
        readonly_paths=[],
        setup_command="cat src/app.py > built.txt",
        secrets={},
    )
    try:
        space.prepare()
        box = _open(space, "")
        assert box.session.read_file(f"{WORK_DIR}/built.txt") == "x = 1\n"
        box.close()
    finally:
        space.close()
    assert runtime.boxes[-1].spec.snapshot_id == "snap-1"
    assert runtime.boxes[-1].spec.snapshot_archive is None


def test_a_failed_snapshot_keeps_the_state_as_a_tar(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A snapshot fault falls back to a tar state, and the log names only the error type."""
    runtime = _SnapshotRuntime(failing=True)
    space = _space(_archive(tmp_path, {"src/app.py": "x = 1\n"}), runtime, setup=None)
    try:
        _open(space, "").close()
    finally:
        space.close()
    assert runtime.boxes[-1].spec.snapshot_archive
    assert "provider text" not in caplog.text
