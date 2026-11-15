"""Tests for the setup box, the candidate box and the metric box of a repository workspace."""

from __future__ import annotations

import base64
import builtins
import json
import logging
import os
import re
import shlex
import shutil
import stat as stat_module
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from vercel.sandbox import SandboxApiError, SandboxFilesystemWriteError

from core.billing.vercel_usage import PACKAGE_REGISTRY_HOSTS
from core.sandbox.providers.docker import DockerEnvironment
from core.sandbox.providers.vercel import VercelEnvironment

from .. import repo_workspace
from ..repo_constraints import parse_constraints, scorer_only_paths
from ..repo_scorer import RepoScoreError, RepoScorer, _StageRelay
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
from ..sandbox import AsyncBridgeRuntime, CommandResult, LocalSubprocessRuntime, SandboxSpec
from ..sandbox_broker import ParentSandboxSession
from ..sandbox_runtime import AsyncBridgeSession

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


def test_a_stage_answer_is_checked_as_root_then_written_as_the_box_user(tmp_path: Path) -> None:
    """Check as root without the answer, then write it in a box-user command, so the stamps before the run stay valid."""
    space, archive = _guarded(tmp_path)
    try:
        box = _open(space, _patch(tmp_path / "one", archive, {"src/app.py": "x = 2\n"}))
        box.stamp_before_run()
        session = box.session
        runs, roots = len(session.commands), len(session.root_commands)
        assert box.answer("calls/000001/stages/a.json", '{"output": 1}') == ()
        (check, check_env), (write, write_env) = session.commands[runs:]
        assert session.root_commands[roots:] == [check]
        assert shlex.quote(repo_workspace._HASH_PROTECTED) in check
        assert "APP_ANSWER" not in check
        assert not check_env
        assert shlex.quote(repo_workspace._WRITE_ANSWER) in write
        assert write_env == {"APP_ANSWER": '{"output": 1}'}
        assert session.read_file("calls/000001/stages/a.json") == '{"output": 1}'
        assert box.protected_changes() == ()
        box.close()
    finally:
        space.close()


def test_a_stage_answer_after_a_protected_change_names_it_and_is_still_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Return the change and still write the answer, so the version ends now instead of at its timeout."""
    space, archive = _guarded(tmp_path)
    try:
        box = _open(space, _patch(tmp_path / "one", archive, {"src/app.py": "x = 2\n"}))
        box.stamp_before_run()
        session = box.session
        session.run(f"cd {WORK_DIR} && printf 'a,0\\n' > eval/labels.csv")
        changes = box.answer("calls/000001/stages/b.json", '{"output": 2}')
        assert [problem.split("'")[1] for problem in changes or ()] == ["eval/labels.csv"]
        assert session.read_file("calls/000001/stages/b.json") == '{"output": 2}'

        run = session.run
        monkeypatch.setattr(
            session,
            "run",
            lambda command, **options: (
                CommandResult(exit_code=1)
                if shlex.quote(repo_workspace._WRITE_ANSWER) in command
                else run(command, **options)
            ),
        )
        changes = box.answer("calls/000001/stages/c.json", '{"output": 3}')
        assert [problem.split("'")[1] for problem in changes or ()] == ["eval/labels.csv"]
        box.close()
    finally:
        space.close()


def test_a_stage_answer_that_cannot_be_written_is_unchecked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Return ``None`` when the box-user write fails after a clean check, and still deliver the answer with a file write.

    The version gets no score, but its call ends now and not at its timeout.
    """
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
                CommandResult(exit_code=1)
                if shlex.quote(repo_workspace._WRITE_ANSWER) in command
                else run(command, **options)
            ),
        )
        assert box.answer("calls/000001/stages/a.json", '{"output": 1}') is None
        assert session.read_file("calls/000001/stages/a.json") == '{"output": 1}'
        box.close()
    finally:
        space.close()


class _BoxCallError(RuntimeError):
    """A box call that fails before the command runs, as a sandbox API 429 or 5xx does."""


def test_a_box_call_error_on_the_answer_write_goes_up_raw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Let a box call error from the answer write go up raw, so a later retry can tell a temporary fault apart."""
    space, archive = _guarded(tmp_path)
    try:
        box = _open(space, _patch(tmp_path / "one", archive, {"src/app.py": "x = 2\n"}))
        box.stamp_before_run()
        session = box.session
        run = session.run

        def failing(command: str, **options: Any) -> CommandResult:
            """Raise for the answer write, and run every other command.

            Args:
                command: The shell command.
                **options: The run's other arguments.

            Returns:
                The command's result.

            Raises:
                _BoxCallError: The command is the answer write.
            """
            if shlex.quote(repo_workspace._WRITE_ANSWER) in command:
                raise _BoxCallError("429")
            return run(command, **options)

        monkeypatch.setattr(session, "run", failing)
        with pytest.raises(_BoxCallError, match="429"):
            box.answer("calls/000001/stages/a.json", '{"output": 1}')
        assert session.read_file("calls/000001/stages/a.json") is None
        box.close()
    finally:
        space.close()


def _write_error(status: int) -> SandboxFilesystemWriteError:
    """Build the SDK's file write error over an HTTP error answer, chained as the SDK chains it.

    Args:
        status: The HTTP status.

    Returns:
        The write error; its cause is the API error.
    """
    api = SandboxApiError(httpx.Response(status), f"HTTP {status}: provider text")
    try:
        raise SandboxFilesystemWriteError(paths=("a",), cwd="/", cause=api) from api
    except SandboxFilesystemWriteError as error:
        return error


class _RefusingSession:
    """A box whose every file write raises one error."""

    def __init__(self, error: BaseException) -> None:
        """Keep the error.

        Args:
            error: What each write raises.
        """
        self.error = error

    def write_files(self, files: Any) -> None:
        """Fail the write.

        Args:
            files: The files to write.

        Raises:
            BaseException: Always, the stored error.
        """
        raise self.error


@pytest.mark.parametrize("status", [429, 503])
def test_a_temporary_answer_file_write_error_goes_up_raw(status: int) -> None:
    """Let a 429 or 5xx on an answer file write go up as the same error, so the relay can tell it is temporary.

    Args:
        status: The HTTP status of the write.
    """
    error = _write_error(status)
    with pytest.raises(SandboxFilesystemWriteError) as raised:
        repo_workspace._deliver(_RefusingSession(error), "a.json", "{}")  # type: ignore[arg-type]
    assert raised.value is error


def test_an_unwatched_candidate_answer_raises_a_temporary_write_error_and_reports_a_lasting_one() -> None:
    """With no protected files, a 429 on the answer write goes up raw; a 404 still gives an unchecked answer."""
    session = _RefusingSession(_write_error(429))
    box = CandidateBox(SimpleNamespace(watched=()), session, "/workspace")  # type: ignore[arg-type]
    with pytest.raises(SandboxFilesystemWriteError) as raised:
        box.answer("calls/000001/stages/a.json", "{}")
    assert raised.value is session.error
    session.error = _write_error(404)
    assert box.answer("calls/000001/stages/a.json", "{}") is None


def test_a_lasting_answer_file_write_error_logs_no_provider_text(caplog: pytest.LogCaptureFixture) -> None:
    """Log only the type of a lasting answer write error: the run log shows it, and its text is the provider's.

    Args:
        caplog: Pytest log capture.
    """
    error = SandboxApiError(httpx.Response(404), "HTTP 404: provider text")
    with caplog.at_level(logging.WARNING):
        assert repo_workspace._deliver(_RefusingSession(error), "a.json", "{}") is False  # type: ignore[arg-type]
    assert "SandboxApiError" in caplog.text
    assert "provider text" not in caplog.text


def _fail_answer_step(monkeypatch: pytest.MonkeyPatch, box: CandidateBox, step: str) -> None:
    """Make one step of the box's next relay answer raise.

    Args:
        monkeypatch: Pytest patcher.
        box: The candidate box.
        step: ``write`` (the answer command), ``check`` (the protected-file
            check) or ``deliver`` (the file write of a large answer, a 503).
    """
    if step == "write":
        run = box.session.run

        def failing(command: str, **options: Any) -> CommandResult:
            """Raise for the answer write, and run every other command.

            Args:
                command: The shell command.
                **options: The run's other arguments.

            Returns:
                The command's result.

            Raises:
                _BoxCallError: The command is the answer write.
            """
            if shlex.quote(repo_workspace._WRITE_ANSWER) in command:
                raise _BoxCallError("429")
            return run(command, **options)

        monkeypatch.setattr(box.session, "run", failing)
    elif step == "check":

        def refuse(*_: Any) -> None:
            """Fail the check before it writes anything.

            Raises:
                _BoxCallError: Always.
            """
            raise _BoxCallError("429")

        monkeypatch.setattr(box._workspace, "fingerprint", refuse)
    else:
        monkeypatch.setattr(repo_workspace, "_ANSWER_ENV_BYTES", 10)
        monkeypatch.setattr(box.session, "write_files", _RefusingSession(_write_error(503)).write_files)


@pytest.mark.parametrize("judge", [False, True], ids=["run", "judge"])
@pytest.mark.parametrize("step", ["write", "check", "deliver"])
def test_a_raised_answer_never_hides_a_protected_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: str, judge: bool
) -> None:
    """Name a protected file the version changed, also when the relay answer after the change raises.

    The answer command and the check write no file, so the final check still
    runs. A large answer's file write may have moved the change times, so the
    change its check found before the write is kept.

    Args:
        tmp_path: Scratch folder.
        monkeypatch: Pytest patcher.
        step: The answer step that raises.
        judge: Whether the scorer runs for the judge.
    """
    space, archive = _guarded(tmp_path)
    try:
        box = _open(space, _patch(tmp_path / "one", archive, {"src/app.py": "x = 2\n"}))
        box.stamp_before_run()
        box.session.run(f"cd {WORK_DIR} && printf 'a,0\\n' > eval/labels.csv")
        with monkeypatch.context() as patched:
            _fail_answer_step(patched, box, step)
            relay = _StageRelay({}, {}, response_dir="calls/000001/stages", max_concurrency=1, env={}, candidate=box)
            relay._answer("a" * 32, {"output": 1})
            relay.close()
        scorer = SimpleNamespace(_rule_judge=None, raw_provider_errors=judge)
        with pytest.raises(RepoScoreError) as raised:
            RepoScorer._check_changes(scorer, relay, [], box)  # type: ignore[arg-type]
        assert "eval/labels.csv" in str(raised.value), str(raised.value)
        assert box.unstamped is (step == "deliver")
        box.close()
    finally:
        space.close()


@pytest.mark.parametrize("judge", [False, True], ids=["run", "judge"])
def test_a_raised_large_answer_write_never_reports_no_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, judge: bool
) -> None:
    """A 503 on a large answer's file write gives the unchecked error, never a clean check.

    The server can do the write before the 503, which moves every change time,
    so the final check cannot tell a change apart.

    Args:
        tmp_path: Scratch folder.
        monkeypatch: Pytest patcher.
        judge: Whether the scorer runs for the judge.
    """
    space, archive = _guarded(tmp_path)
    try:
        box = _open(space, _patch(tmp_path / "one", archive, {"src/app.py": "x = 2\n"}))
        box.stamp_before_run()
        with monkeypatch.context() as patched:
            _fail_answer_step(patched, box, "deliver")
            relay = _StageRelay({}, {}, response_dir="calls/000001/stages", max_concurrency=1, env={}, candidate=box)
            assert relay._answer("a" * 32, {"output": 1}) is False
            relay.close()
        scorer = SimpleNamespace(_rule_judge=None, raw_provider_errors=judge)
        with pytest.raises(Exception) as raised:
            RepoScorer._check_changes(scorer, relay, [], box)  # type: ignore[arg-type]
        if judge:
            assert raised.value is relay.failure
        else:
            assert str(raised.value) == "A relay answer could not be checked or delivered, so the version has no score."
        assert box.unstamped
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

    real_fstat = os.fstat

    def fstat(fd: int) -> os.stat_result:
        """Report the check's output as a pipe, because pytest puts a file there."""
        found = real_fstat(fd)
        if fd != 1:
            return found
        return os.stat_result((stat_module.S_IFIFO | 0o600, *found[1:10]))

    monkeypatch.setattr(os, "geteuid", lambda: state["euid"])
    monkeypatch.setattr(os, "fstat", fstat)
    for name in ("setgroups", "setgid", "setuid"):
        monkeypatch.setattr(os, name, switch(name))
    monkeypatch.setattr(os, "stat", stat)
    for name in ("walk", "open", "unlink", "replace"):
        monkeypatch.setattr(os, name, watch(name, getattr(os, name)))
    monkeypatch.chdir(tree)
    monkeypatch.setattr(sys, "argv", ["-c", "eval"])
    names = {**vars(builtins), "open": watch("read", open)}
    exec(compile(repo_workspace._HASH_PROTECTED, "<check>", "exec"), {"__builtins__": names})
    return events, json.loads(capsys.readouterr().out)


def _planted_tree(tmp_path: Path) -> Path:
    """Build a checkout whose answer's temporary path is a link that the version planted into another file.

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
    """Drop root before the first tree access, and write nothing into the tree (PRE-1)."""
    tree = _planted_tree(tmp_path)
    owner = tree.stat().st_uid
    assert owner != 0
    events, result = _run_check_as(monkeypatch, capsys, tree, euid=0)
    assert [name for name, _ in events[:3]] == ["setgroups", "setgid", "setuid"]
    accesses = events[3:]
    assert [name for name, _ in accesses] == ["walk eval", "read eval/labels.csv"]
    assert {user for _, user in accesses} == {owner}
    assert set(result["files"]) == {"eval/labels.csv"}
    assert not (tree / "calls" / "a.json").exists()
    assert (tmp_path / "root-only").read_text() == "secret\n"


def test_the_answer_write_replaces_a_planted_link_and_never_follows_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Remove a leftover temporary file and open the new one without following a link."""
    tree = _planted_tree(tmp_path)
    monkeypatch.chdir(tree)
    monkeypatch.setattr(sys, "argv", ["-c", str(tree / "calls" / "000001" / "a.json")])
    monkeypatch.setenv("APP_ANSWER", '{"output": 1}')
    (tree / "calls" / "000001").mkdir()
    (tree / "calls" / "000001" / "a.json.tmp").symlink_to(tmp_path / "root-only")
    exec(compile(repo_workspace._WRITE_ANSWER, "<write>", "exec"), {"__builtins__": builtins})
    written = tree / "calls" / "000001" / "a.json"
    assert written.read_text() == '{"output": 1}'
    assert not written.is_symlink()
    assert not (tree / "calls" / "000001" / "a.json.tmp").exists()
    assert (tmp_path / "root-only").read_text() == "secret\n"


def test_a_link_that_comes_back_after_the_unlink_stops_the_answer_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail the write and keep the link's target when a link comes back between the unlink and the open."""
    target = tmp_path / "other"
    target.write_text("secret\n")
    answer = tmp_path / "calls" / "a.json"
    answer.parent.mkdir()
    real_unlink = os.unlink

    def unlink_then_plant(path: str, *args: Any, **kwargs: Any) -> None:
        """Run the real unlink, then put a link to another file at the same path.

        Args:
            path: The temporary file the write removes.
            *args: Unlink arguments.
            **kwargs: Unlink options.
        """
        try:
            real_unlink(path, *args, **kwargs)
        finally:
            Path(path).symlink_to(target)

    monkeypatch.setattr(os, "unlink", unlink_then_plant)
    monkeypatch.setattr(sys, "argv", ["-c", str(answer)])
    monkeypatch.setenv("APP_ANSWER", '{"output": 1}')
    with pytest.raises(FileExistsError):
        exec(compile(repo_workspace._WRITE_ANSWER, "<write>", "exec"), {"__builtins__": builtins})
    assert target.read_text() == "secret\n"
    assert not answer.exists()


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


def test_a_root_without_capabilities_fails_closed_and_reads_no_tree_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Stop a root that cannot switch users, as in a local Docker box, before it reads the tree (TS-1).

    That root cannot read the box user's private files, so its fingerprint
    could leave files out. The parent runs the check as the box user there.
    """
    tree = _planted_tree(tmp_path)
    with pytest.raises(SystemExit) as stopped:
        _run_check_as(monkeypatch, capsys, tree, euid=0, fault="refuse")
    assert stopped.value.code not in (0, None)
    assert capsys.readouterr().out == ""


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
    assert not (tree / "calls" / "a.json").exists()
    assert (tmp_path / "root-only").read_text() == "secret\n"


# Makes the check see a root start and an output file that the box user owns,
# as when a version plants the wrapper's output file before root's redirect.
_FOREIGN_OUTPUT = """
import os
real_fstat = os.fstat
def fstat(fd):
    found = real_fstat(fd)
    if fd != 1:
        return found
    return os.stat_result((found.st_mode, found.st_ino, found.st_dev, found.st_nlink, 1000, 1000, *found[6:10]))
os.fstat = fstat
os.geteuid = lambda: 0
"""


def _run_check_process(
    tree: Path, stdout: Any, prelude: str = "", paths: tuple[str, ...] = ("eval",)
) -> subprocess.CompletedProcess[str]:
    """Run the protected-file check as its own process, as the box does.

    Args:
        tree: The checkout the check runs in.
        stdout: Where the check's output goes: a file or ``subprocess.PIPE``.
        prelude: Code that runs before the check, to fake the process state.
        paths: The protected paths.

    Returns:
        The finished process.
    """
    return subprocess.run(
        [sys.executable, "-I", "-S", "-c", prelude + repo_workspace._HASH_PROTECTED, *paths],
        cwd=tree,
        stdout=stdout,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )


def test_a_root_check_whose_output_file_the_box_user_owns_prints_nothing(tmp_path: Path) -> None:
    """Fail closed when root's output file belongs to another user, who could rewrite a clean result (TR-1)."""
    tree = _planted_tree(tmp_path)
    output = tmp_path / "check.out"
    with output.open("w") as handle:
        done = _run_check_process(tree, handle, _FOREIGN_OUTPUT)
    assert done.returncode != 0
    assert "output" in done.stderr
    assert output.read_text() == ""


def test_a_box_user_check_with_an_output_file_prints_nothing(tmp_path: Path) -> None:
    """Fail closed when a check that does not start as root writes to a file, which its own user can rewrite."""
    tree = _planted_tree(tmp_path)
    output = tmp_path / "check.out"
    with output.open("w") as handle:
        done = _run_check_process(tree, handle)
    assert done.returncode != 0
    assert "output" in done.stderr
    assert output.read_text() == ""


def test_a_check_with_a_pipe_output_prints_the_fingerprint(tmp_path: Path) -> None:
    """Skip the output file check for a pipe, as the local runtimes give."""
    tree = _planted_tree(tmp_path)
    done = _run_check_process(tree, subprocess.PIPE)
    assert done.returncode == 0, done.stderr
    assert set(json.loads(done.stdout)["files"]) == {"eval/labels.csv"}


class _SudoRecordingSession:
    """A box that records whether each command asks for root, and prints an empty fingerprint."""

    def __init__(self, **attributes: Any) -> None:
        """Start with no command, and set the given attributes.

        Args:
            **attributes: Attributes such as ``root_has_capabilities``.
        """
        self.sudo: list[bool] = []
        vars(self).update(attributes)

    def run(self, command: str, *, sudo: bool = False, **options: Any) -> CommandResult:
        """Record ``sudo`` and print an empty fingerprint.

        Args:
            command: Shell command line.
            sudo: The command asks for root.
            **options: Other run options.

        Returns:
            A clean result with an empty fingerprint.
        """
        self.sudo.append(sudo)
        return CommandResult(exit_code=0, stdout='{"files": {}, "stamps": {}}')


@pytest.mark.parametrize(
    ("attributes", "sudo"),
    [({"root_has_capabilities": False}, False), ({"root_has_capabilities": True}, True), ({}, True)],
    ids=["capless-root", "root", "unknown"],
)
def test_the_check_runs_as_root_unless_the_provider_root_has_no_capability(
    tmp_path: Path, attributes: dict[str, bool], sudo: bool
) -> None:
    """Run the check as the box user only where the provider's root has no capability; else keep root (TS-1)."""
    space, _ = _guarded(tmp_path)
    try:
        session = _SudoRecordingSession(**attributes)
        assert space.fingerprint(session, WORK_DIR) == ({}, {})  # type: ignore[arg-type]
        assert session.sudo == [sudo]
    finally:
        space.close()


@pytest.mark.parametrize(("provider", "capable"), [(DockerEnvironment, False), (VercelEnvironment, True)])
def test_the_session_takes_the_root_kind_from_the_provider_class(provider: type, capable: bool) -> None:
    """Read the root's kind from the provider class, never from the box, through each session wrapper (TS-1)."""
    environment = object.__new__(provider)
    environment.spec = SimpleNamespace(env={}, tags={})
    session = AsyncBridgeSession(SimpleNamespace(environment=environment))  # type: ignore[arg-type]
    assert session.root_has_capabilities is capable
    assert ParentSandboxSession(session, None).root_has_capabilities is capable


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a folder without permission")
@pytest.mark.parametrize("protected", ["eval", "eval/private/labels.csv"], ids=["walk", "path"])
def test_a_protected_folder_the_check_cannot_read_stops_it(tmp_path: Path, protected: str) -> None:
    """Fail closed on a protected folder that the check cannot read, so no file in it is left out (TS-1)."""
    tree = _planted_tree(tmp_path)
    private = tree / "eval" / "private"
    private.mkdir()
    (private / "labels.csv").write_text("a,1\n")
    private.chmod(0)
    try:
        done = _run_check_process(tree, subprocess.PIPE, paths=(protected,))
    finally:
        private.chmod(0o700)
    assert done.returncode != 0
    assert done.stdout == ""


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
                CommandResult(exit_code=1)
                if shlex.quote(repo_workspace._HASH_PROTECTED) in command
                else run(command, **options)
            ),
        )
        assert box.answer("calls/000001/stages/a.json", '{"output": 1}') is None
        assert session.read_file("calls/000001/stages/a.json") == '{"output": 1}'
        assert all(shlex.quote(repo_workspace._WRITE_ANSWER) not in command for command, _ in session.commands)
        monkeypatch.undo()
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
        assert all(shlex.quote(repo_workspace._WRITE_ANSWER) not in command for command, _ in box.session.commands)
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


# --- Local Docker boxes (``pytest -m docker``) ---

_IMAGE = "skynet-local-sandbox:0.1.0-fa7455ae46e8-1794372282390"
_DOCKER_UP = (
    shutil.which("docker") is not None
    and subprocess.run(["docker", "image", "inspect", _IMAGE], capture_output=True, check=False).returncode == 0
)


def _containers(tag: str) -> list[str]:
    """Return the ids of the containers labelled with ``tag`` that are still there after up to 60 s.

    Args:
        tag: The ``skynet_test`` label.

    Returns:
        The container ids.
    """
    deadline = time.monotonic() + 60
    while True:
        listed = subprocess.run(
            ["docker", "ps", "-a", "-q", "--filter", f"label=skynet_test={tag}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        if not listed or time.monotonic() > deadline:
            return listed
        time.sleep(1)


@pytest.mark.docker
@pytest.mark.skipif(not _DOCKER_UP, reason=f"Docker or the local {_IMAGE} image is missing.")
def test_a_stage_answer_reaches_a_call_folder_that_the_box_user_made_in_docker() -> None:
    """Write the answer where a root with no capability cannot, as the call folder is in a local Docker box.

    The parent's file write makes the call folder as the box user with mode
    0755, and a local Docker root holds no capability to write into it.
    """
    tag = uuid.uuid4().hex
    session = AsyncBridgeRuntime("docker", image=_IMAGE).open(
        SandboxSpec(lifetime_seconds=300, network_disabled=True, tags={"skynet_test": tag})
    )
    try:
        space = SimpleNamespace(_protected=("eval/metric.py",), watched=True, changes=lambda current, before: ())
        space.fingerprint = lambda box, directory, **options: RepoWorkspace.fingerprint(
            space, box, directory, **options
        )
        call = ".app-repo/calls/000001/candidate"
        session.write_files({f"{WORK_DIR}/eval/metric.py": "x = 1\n", f"{call}/input.json": "{}"})
        box = CandidateBox(space, session, "/workspace")
        assert box.stamp_before_run() is None
        assert box.answer(f"{call}/stages/x.json", '{"output": 1}') == ()
        assert session.read_file(f"{call}/stages/x.json") == '{"output": 1}'
    finally:
        session.close()
    assert _containers(tag) == []


@pytest.mark.docker
@pytest.mark.skipif(not _DOCKER_UP, reason=f"Docker or the local {_IMAGE} image is missing.")
def test_the_check_reads_private_files_that_the_box_user_made_in_docker() -> None:
    """Fingerprint a 0600 file and a 0700 folder of the box user, as a local Docker root cannot read them (TS-1)."""
    tag = uuid.uuid4().hex
    session = AsyncBridgeRuntime("docker", image=_IMAGE).open(
        SandboxSpec(lifetime_seconds=300, network_disabled=True, tags={"skynet_test": tag})
    )
    try:
        space = SimpleNamespace(_protected=("eval",), watched=True)
        session.write_files({f"{WORK_DIR}/eval/metric.py": "x = 1\n", f"{WORK_DIR}/eval/hidden/data.py": "y = 2\n"})
        made = session.run(f"chmod 0600 {WORK_DIR}/eval/metric.py && chmod 0700 {WORK_DIR}/eval/hidden")
        assert made.exit_code == 0
        files, _ = RepoWorkspace.fingerprint(space, session, WORK_DIR)  # type: ignore[arg-type]
        assert sorted(files) == ["eval/hidden/data.py", "eval/metric.py"]
    finally:
        session.close()
    assert _containers(tag) == []
