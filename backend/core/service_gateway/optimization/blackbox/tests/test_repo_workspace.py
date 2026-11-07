"""Tests for laying out repository versions in the parent-owned scorer box."""

from __future__ import annotations

import base64
import json
import logging
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from core.billing.budgets import BudgetBoundExceededError
from core.billing.runtime import UsagePendingError
from core.exceptions import ServiceError

from .. import repo_workspace
from ..repo_tree import unpack_tree, version_patch
from ..repo_workspace import (
    _PROBE_OK,
    PREPARED_DIR,
    REPO_DIR,
    WORK_DIR,
    NetworkCutoffError,
    RepoSetupError,
    RepoWorkspace,
    redact,
)
from ..sandbox import CommandResult, LocalSubprocessRuntime, SandboxSpec


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
    subprocess.run(["tar", "-czf", str(archive), "-C", str(source), "."], check=True)
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
    """Run commands on the host, but track the network state a real box would have."""

    def __init__(self, inner: Any, *, online: bool, cutoff: str) -> None:
        """Wrap a local session.

        Args:
            inner: The real local session commands run in.
            online: Whether the box opened with network access.
            cutoff: ``"ok"``, ``"refused"`` (the switch raises) or ``"leaky"``
                (the switch returns but hosts stay reachable).
        """
        self._inner = inner
        self.online = online
        self._cutoff = cutoff
        self.commands: list[tuple[str, bool, dict[str, str] | None]] = []
        self.closed = False

    def write_files(self, files: dict[str, str]) -> None:
        """Write into the wrapped box."""
        self._inner.write_files(files)

    def read_file(self, path: str) -> str | None:
        """Read from the wrapped box."""
        return self._inner.read_file(path)

    def run(self, command: str, **options: Any) -> CommandResult:
        """Record the command with the network state it ran under, answering the reachability probe."""
        self.commands.append((command, self.online, options.get("env")))
        if "/dev/tcp/" in command:
            return CommandResult(exit_code=1) if self.online else CommandResult(exit_code=0, stdout=_PROBE_OK)
        return self._inner.run(command, **options)

    def disable_network(self) -> None:
        """Switch the network off, or fail the way the configured provider would."""
        if self._cutoff == "refused":
            raise ServiceError("provider refused the policy change")
        if self._cutoff == "ok":
            self.online = False

    def close(self) -> None:
        """Close the wrapped box."""
        self.closed = True
        self._inner.close()


class _Runtime:
    """Open recording boxes over the local runtime."""

    def __init__(self, cutoff: str = "ok") -> None:
        """Choose how the network switch behaves.

        Args:
            cutoff: Passed to every box opened.
        """
        self._local = LocalSubprocessRuntime()
        self._cutoff = cutoff
        self.boxes: list[_Box] = []

    def open(self, spec: SandboxSpec) -> _Box:
        """Open one recording box."""
        box = _Box(self._local.open(spec), online=not spec.network_disabled, cutoff=self._cutoff)
        self.boxes.append(box)
        return box


_ONLINE = SandboxSpec(lifetime_seconds=60, allowed_hosts=("pypi.org",))


def _space(archive: Path, runtime: Any, *, setup: str | None, spec: SandboxSpec = _ONLINE) -> RepoWorkspace:
    """Build a workspace over ``archive`` with one secret.

    Args:
        archive: Packed tree.
        runtime: Where its box opens.
        setup: The setup command.
        spec: The box's network profile.

    Returns:
        The workspace.
    """
    return RepoWorkspace(
        runtime=runtime,
        spec=spec,
        archive=archive,
        editable_paths=["src"],
        readonly_paths=[],
        setup_command=setup,
        secrets={"API_KEY": "very-secret-value"},
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[tuple[RepoWorkspace, _Runtime, Path, Path]]:
    """Open a workspace whose setup builds a file and leaves a stray one.

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


def test_setup_runs_once_online_without_secrets_and_versions_run_offline(
    workspace: tuple[RepoWorkspace, _Runtime, Path, Path],
) -> None:
    """Prepare the starting commit once with network, then lay every version out on a copy with it off."""
    space, runtime, archive, root = workspace

    first = space.checkout(_patch(root / "one", archive, {"src/app.py": "x = 2\n"}))
    space.session().run(f"touch {WORK_DIR}/stray.txt")
    second = space.checkout("")

    assert first.path == second.path == WORK_DIR
    box = runtime.boxes[0]
    assert len(runtime.boxes) == 1
    setups = [entry for entry in box.commands if "built.txt" in entry[0]]
    assert len(setups) == 1
    assert setups[0][1] is True
    assert setups[0][2] is None
    assert "key=unset" in space.setup_log
    version_runs = [entry for entry in box.commands if "version.patch" in entry[0] or "stray" in entry[0]]
    assert len(version_runs) == 3
    assert all(online is False for _, online, _ in version_runs)
    assert space.session().read_file(f"{WORK_DIR}/built.txt") == "x = 1\n"
    assert space.session().read_file(f"{WORK_DIR}/stray.txt") is None


def test_version_code_never_runs_while_the_network_is_on(
    workspace: tuple[RepoWorkspace, _Runtime, Path, Path],
) -> None:
    """Every command after setup, including the reachability check, runs only once the switch landed."""
    space, runtime, archive, root = workspace

    space.checkout(_patch(root / "one", archive, {"src/app.py": "x = 2\n"}))

    commands = runtime.boxes[0].commands
    probe = next(index for index, entry in enumerate(commands) if "/dev/tcp/" in entry[0])
    assert all(online for _, online, _ in commands[:probe])
    assert not any(online for _, online, _ in commands[probe:])
    assert all("version.patch" not in command for command, _, _ in commands[:probe])


@pytest.mark.parametrize("cutoff", ["refused", "leaky"])
def test_a_failed_network_cutoff_stops_the_workspace(tmp_path: Path, cutoff: str) -> None:
    """Close the box and refuse every version when the network cannot be switched off and confirmed."""
    archive = _archive(tmp_path, {"src/app.py": "x = 1\n"})
    runtime = _Runtime(cutoff)
    space = _space(archive, runtime, setup="true")

    with pytest.raises(NetworkCutoffError):
        space.checkout(_patch(tmp_path / "one", archive, {"src/app.py": "x = 2\n"}))
    with pytest.raises(NetworkCutoffError):
        space.checkout("")

    assert len(runtime.boxes) == 1
    assert runtime.boxes[0].closed
    assert all("version.patch" not in command for command, _, _ in runtime.boxes[0].commands)


class _UnsettledBox(_Box):
    """Close like a cloud box whose final usage cannot be settled."""

    close_error: Exception = UsagePendingError("The cloud sandbox has not reported final network transfer.")

    def close(self) -> None:
        """Close the box, then report that its usage cannot be settled."""
        super().close()
        raise self.close_error


class _UnsettledRuntime(_Runtime):
    """Open boxes that cannot settle their usage when closed."""

    def __init__(self, cutoff: str, close_error: Exception) -> None:
        """Choose the network switch behavior and the settlement error.

        Args:
            cutoff: Passed to every box opened.
            close_error: The error every box raises after it closes.
        """
        super().__init__(cutoff)
        self._close_error = close_error

    def open(self, spec: SandboxSpec) -> _Box:
        """Open one recording box that fails to settle on close."""
        box = _UnsettledBox(self._local.open(spec), online=not spec.network_disabled, cutoff=self._cutoff)
        box.close_error = self._close_error
        self.boxes.append(box)
        return box


@pytest.mark.parametrize(
    "close_error",
    [
        UsagePendingError("The cloud sandbox has not reported final network transfer."),
        BudgetBoundExceededError("The run budget bound is exceeded."),
        RuntimeError("Vercel usage ledger write failed: database is locked."),
    ],
    ids=["usage-pending", "budget", "unexpected"],
)
@pytest.mark.parametrize(
    ("cutoff", "setup", "expected"),
    [("ok", "exit 3", RepoSetupError), ("refused", "true", NetworkCutoffError)],
)
def test_settlement_error_on_close_keeps_the_setup_error(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    cutoff: str,
    setup: str,
    expected: type[Exception],
    close_error: Exception,
) -> None:
    """Show the setup or cutoff failure, and log only the close error type, when the failed box closes."""
    archive = _archive(tmp_path, {"src/app.py": "x = 1\n"})
    runtime = _UnsettledRuntime(cutoff, close_error)
    space = _space(archive, runtime, setup=setup)

    with caplog.at_level(logging.WARNING, logger=repo_workspace.__name__), pytest.raises(expected):
        space.checkout(_patch(tmp_path / "one", archive, {"src/app.py": "x = 2\n"}))

    assert runtime.boxes[0].closed
    warnings = [r for r in caplog.records if r.name == repo_workspace.__name__ and r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert type(close_error).__name__ in warnings[0].getMessage()
    assert str(close_error) not in warnings[0].getMessage()
    assert "vercel" not in warnings[0].getMessage().lower()


def test_a_box_that_cannot_switch_its_network_off_is_refused(tmp_path: Path) -> None:
    """Fail closed when the runtime offers no way to take an online box offline."""
    archive = _archive(tmp_path, {"src/app.py": "x = 1\n"})
    space = _space(archive, LocalSubprocessRuntime(), setup=None)

    with pytest.raises(NetworkCutoffError, match="cannot switch"):
        space.session()


def test_an_offline_box_is_not_switched_or_probed(tmp_path: Path) -> None:
    """A box that opened offline needs no switch, so a run without setup never reaches a registry."""
    archive = _archive(tmp_path, {"src/app.py": "x = 1\n"})
    runtime = _Runtime("refused")
    space = _space(archive, runtime, setup=None, spec=SandboxSpec(lifetime_seconds=60, network_disabled=True))
    try:
        assert space.checkout("").path == WORK_DIR
    finally:
        space.close()

    assert not any(online for _, online, _ in runtime.boxes[0].commands)
    assert all("/dev/tcp/" not in command for command, _, _ in runtime.boxes[0].commands)


def test_failed_setup_and_broken_patches_are_reported(tmp_path: Path) -> None:
    """Stop on a setup that exits non-zero, and explain a patch that does not apply."""
    archive = _archive(tmp_path, {"src/app.py": "x = 1\n"})
    failing = _space(archive, _Runtime(), setup="echo boom; exit 3")
    with pytest.raises(RepoSetupError, match="exit 3") as failed:
        failing.session()
    assert "boom" in str(failed.value)

    space = _space(archive, _Runtime(), setup=None)
    try:
        broken = space.checkout(
            "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-y\n+z\n"
        )
    finally:
        space.close()
    assert broken.problems[0].startswith("The change does not apply cleanly")


def test_versions_outside_the_editable_paths_never_reach_the_box(
    workspace: tuple[RepoWorkspace, _Runtime, Path, Path],
) -> None:
    """Refuse a version that edits the tests before anything runs."""
    space, runtime, archive, root = workspace

    result = space.checkout(_patch(root / "one", archive, {"tests/test_app.py": "rigged\n"}))

    assert result.path is None
    assert result.problems == ("'tests/test_app.py' is outside the editable paths.",)
    assert runtime.boxes == []


def test_redact_hides_long_values_only() -> None:
    """Mask real secrets while leaving short values that would scramble ordinary output."""
    assert redact("key=abcdefgh on=1", ["abcdefgh", "1"]) == "key=[secret] on=1"


def test_protected_paths_win_over_editable_ones_before_the_box_opens(tmp_path: Path) -> None:
    """Lay out an allowed edit, but refuse one to a protected judge file inside an editable folder."""
    archive = _archive(tmp_path, {"src/app.py": "x = 1\n", "src/judge.py": "ok = True\n"})
    runtime = _Runtime()
    space = RepoWorkspace(
        runtime=runtime,
        spec=_ONLINE,
        archive=archive,
        editable_paths=["src"],
        readonly_paths=[],
        setup_command=None,
        secrets={},
        protected_paths=["optimize.toml", "src/judge.py"],
    )
    try:
        rigged = space.checkout(_patch(tmp_path / "rigged", archive, {"src/judge.py": "ok = 'always'\n"}))
        assert rigged.path is None
        assert rigged.problems == (
            "'src/judge.py' is protected by the repository's optimize.toml ([paths] protected).",
        )
        assert runtime.boxes == []
        rules = space.checkout(_patch(tmp_path / "rules", archive, {"optimize.toml": "[paths]\neditable = ['.']\n"}))
        assert rules.path is None
        allowed = space.checkout(_patch(tmp_path / "allowed", archive, {"src/app.py": "x = 2\n"}))
        assert allowed.path == WORK_DIR
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
    space = RepoWorkspace(
        runtime=_Runtime(),
        spec=_ONLINE,
        archive=archive,
        editable_paths=["src"],
        readonly_paths=[],
        setup_command=None,
        secrets={},
        protected_paths=["eval", "optimize.toml"],
    )
    return space, archive


def test_a_source_only_version_whose_code_rewrites_the_labels_is_caught(tmp_path: Path) -> None:
    """Flag protected files the version's code changes while it runs, even though its patch names only source."""
    space, archive = _guarded(tmp_path)
    try:
        version = _patch(tmp_path / "one", archive, {"src/app.py": "x = 2\n"})
        assert space.checkout(version).path == WORK_DIR
        assert space.stamp_before_run() is None
        assert space.protected_changes() == ()

        space.checkout(version)
        space.stamp_before_run()
        space.session().run(f"cd {WORK_DIR} && printf 'a,0\\n' > eval/labels.csv && touch eval/extra.csv")
        assert space.protected_changes() == (
            "'eval/extra.csv' is protected by the repository's optimize.toml ([paths] protected),"
            " and the version's code changed it while it ran.",
            "'eval/labels.csv' is protected by the repository's optimize.toml ([paths] protected),"
            " and the version's code changed it while it ran.",
        )

        space.checkout(version)
        space.stamp_before_run()
        space.session().run(f"rm {WORK_DIR}/eval/judge.py")
        assert [problem.split("'")[1] for problem in space.protected_changes()] == ["eval/judge.py"]
    finally:
        space.close()


def test_a_protected_file_rewritten_and_restored_while_the_version_runs_is_caught(tmp_path: Path) -> None:
    """Flag a protected file the version's code rewrites and then restores, and a protected file it adds and removes."""
    space, archive = _guarded(tmp_path)
    try:
        version = _patch(tmp_path / "one", archive, {"src/app.py": "x = 2\n"})
        space.checkout(version)
        space.stamp_before_run()
        space.session().run(
            f"cd {WORK_DIR} && saved=$(cat eval/labels.csv) && printf 'a,0\\n' > eval/labels.csv"
            " && printf '%s\\n' \"$saved\" > eval/labels.csv"
        )
        assert [problem.split("'")[1] for problem in space.protected_changes()] == ["eval/labels.csv"]

        space.checkout(version)
        space.stamp_before_run()
        space.session().run(f"cd {WORK_DIR} && touch eval/extra.csv && rm eval/extra.csv")
        assert [problem.split("'")[1] for problem in space.protected_changes()] == ["eval"]

        space.checkout(version)
        space.stamp_before_run()
        space.session().run(f"cd {WORK_DIR} && cat eval/labels.csv eval/judge.py > /dev/null")
        assert space.protected_changes() == ()
    finally:
        space.close()


def test_a_stage_answer_checks_the_protected_files_and_is_written_in_one_run(tmp_path: Path) -> None:
    """Write each answer in the check's own run, so the stamps before the run stay valid and a change still shows."""
    space, archive = _guarded(tmp_path)
    try:
        version = _patch(tmp_path / "one", archive, {"src/app.py": "x = 2\n"})
        space.checkout(version)
        space.stamp_before_run()
        box = space.session()
        runs = len(box.commands)
        assert space.answer("calls/000001/stages/a.json", '{"output": 1}') == ()
        assert len(box.commands) == runs + 1
        assert box.read_file("calls/000001/stages/a.json") == '{"output": 1}'
        assert space.protected_changes() == ()

        space.session().run(f"cd {WORK_DIR} && printf 'a,0\\n' > eval/labels.csv")
        changes = space.answer("calls/000001/stages/b.json", '{"output": 2}')
        assert [problem.split("'")[1] for problem in changes or ()] == ["eval/labels.csv"]
        assert box.read_file("calls/000001/stages/b.json") == '{"output": 2}'
    finally:
        space.close()


def test_a_failed_answer_check_is_told_apart_from_a_protected_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Return ``None`` when the check fails, yet still write the answer so the scorer stops waiting."""
    space, archive = _guarded(tmp_path)
    try:
        space.checkout(_patch(tmp_path / "one", archive, {"src/app.py": "x = 2\n"}))
        space.stamp_before_run()
        box = space.session()
        run = box.run
        monkeypatch.setattr(
            box,
            "run",
            lambda command, **options: (
                CommandResult(exit_code=1) if command.startswith("export APP_ANSWER_PATH") else run(command, **options)
            ),
        )
        assert space.answer("calls/000001/stages/a.json", '{"output": 1}') is None
        assert box.read_file("calls/000001/stages/a.json") == '{"output": 1}'
        assert space.protected_changes() == ()
    finally:
        space.close()


def test_a_large_stage_answer_is_written_by_file_and_stamped_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Send an answer too large for the environment through a file write, then stamp the files again."""
    monkeypatch.setattr(repo_workspace, "_ANSWER_ENV_BYTES", 10)
    space, archive = _guarded(tmp_path)
    try:
        space.checkout(_patch(tmp_path / "one", archive, {"src/app.py": "x = 2\n"}))
        space.stamp_before_run()
        text = json.dumps({"output": "x" * 50})
        assert space.answer("calls/000001/stages/a.json", text) == ()
        assert space.session().read_file("calls/000001/stages/a.json") == text
        assert all(not command.startswith("export APP_ANSWER_PATH") for command, _, _ in space.session().commands)
        assert space.protected_changes() == ()
    finally:
        space.close()


def test_tampering_with_the_prepared_tree_is_caught_on_the_next_version(tmp_path: Path) -> None:
    """Compare against fingerprints the parent holds, so rewriting the prepared copy in the box does not hide a change."""
    space, archive = _guarded(tmp_path)
    try:
        version = _patch(tmp_path / "one", archive, {"src/app.py": "x = 2\n"})
        space.checkout(version)
        space.session().run(f"printf 'a,0\\n' > {PREPARED_DIR}/eval/labels.csv")

        space.checkout(version)
        space.stamp_before_run()

        assert [problem.split("'")[1] for problem in space.protected_changes()] == ["eval/labels.csv"]
    finally:
        space.close()


def test_a_workspace_without_protected_paths_skips_the_check(
    workspace: tuple[RepoWorkspace, _Runtime, Path, Path],
) -> None:
    """Run no fingerprinting when the repository protects nothing."""
    space, runtime, _, _ = workspace

    space.checkout("")

    assert space.protected_changes() == ()
    assert all("hashlib" not in command for command, _, _ in runtime.boxes[0].commands)


def test_a_version_that_edits_a_metric_file_is_laid_out_with_the_base_metric(tmp_path: Path) -> None:
    """The metric comes from the base commit: a version's edit to it is dropped, its other edits stay (SKY-353)."""
    archive = _archive(tmp_path, {"src/app.py": "x = 1\n", "src/metric.py": "def score(): return 0.1\n"})
    space = RepoWorkspace(
        runtime=_Runtime(),
        spec=_ONLINE,
        archive=archive,
        editable_paths=["src"],
        readonly_paths=[],
        setup_command=None,
        secrets={},
        hidden_paths=["src/metric.py", ".optimize/exploits"],
    )
    try:
        edits = {"src/app.py": "x = 2\n", "src/metric.py": "def score(): return 1.0\n", ".optimize/exploits/a": "a\n"}
        patch = _patch(tmp_path / "cheat", archive, edits)
        assert space.violations(patch) == []
        laid = space.checkout(patch)
        assert laid.path == WORK_DIR
        box = space.session()
        assert box.read_file(f"{WORK_DIR}/src/metric.py") is None
        assert box.read_file(f"{WORK_DIR}/src/app.py") == "x = 2\n"
        assert space.install_metric_files() is None
        assert box.read_file(f"{WORK_DIR}/src/metric.py") == "def score(): return 0.1\n"
        assert box.read_file(f"{WORK_DIR}/.optimize/exploits/a") is None
    finally:
        space.close()


def _metric_space(tmp_path: Path) -> tuple[RepoWorkspace, _Runtime]:
    """Build a workspace that hides ``eval/metric.py``, laid out with the base version.

    Args:
        tmp_path: Where the archive goes.

    Returns:
        The workspace and its runtime.
    """
    archive = _archive(tmp_path, {"src/app.py": "x = 1\n", "eval/metric.py": "SECRET = 0.3\n"})
    runtime = _Runtime()
    space = RepoWorkspace(
        runtime=runtime,
        spec=_ONLINE,
        archive=archive,
        editable_paths=["src"],
        readonly_paths=[],
        setup_command=None,
        secrets={},
        hidden_paths=["eval/metric.py"],
    )
    assert space.checkout("").path == WORK_DIR
    return space, runtime


def _left_at_close(box: Any, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the names in the box's repo folder just before the box closes.

    Args:
        box: The recording box.
        monkeypatch: Swaps its ``close``.

    Returns:
        A list that fills with the names when the box closes.
    """
    seen: list[str] = []
    close = box.close

    def recording_close() -> None:
        """List the repo folder, then close."""
        seen.extend(box.run(f"ls -a {REPO_DIR}").stdout.split())
        close()

    monkeypatch.setattr(box, "close", recording_close)
    return seen


def test_a_failed_metric_install_removes_the_archive_and_ends_the_box(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No later version reads the metric archive after an install fails (SKY-375, E38)."""
    space, runtime = _metric_space(tmp_path)
    try:
        box = space.session()
        seen = _left_at_close(box, monkeypatch)
        monkeypatch.setattr(space, "_metric_files", "not an archive")

        assert space.install_metric_files() == "The metric files could not be installed after the version ran."

        assert box.closed
        assert seen
        assert not [name for name in seen if name.startswith("hidden")]
        assert space.session() is not box
        assert len(runtime.boxes) == 2
    finally:
        space.close()


def test_a_version_that_turns_a_metric_folder_into_a_file_has_no_score(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refuse the install, remove the archive and end the box when ``eval`` became a file (E38)."""
    space, _ = _metric_space(tmp_path)
    try:
        box = space.session()
        seen = _left_at_close(box, monkeypatch)
        assert box.run(f"cd {WORK_DIR} && rm -rf eval && touch eval").ok

        problem = space.install_metric_files()

        assert problem == "The version replaced a folder of the metric files with a file or a link, so it has no score."
        assert box.closed
        assert seen
        assert not [name for name in seen if name.startswith("hidden")]
    finally:
        space.close()


def test_a_version_that_links_a_metric_folder_outside_gets_no_metric_written_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never write the metric through a link the version put in place of ``eval`` (E38)."""
    space, _ = _metric_space(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        box = space.session()
        seen = _left_at_close(box, monkeypatch)
        assert box.run(f"cd {WORK_DIR} && rm -rf eval && ln -s {outside} eval").ok

        problem = space.install_metric_files()

        assert problem == "The version replaced a folder of the metric files with a file or a link, so it has no score."
        assert list(outside.iterdir()) == []
        assert box.closed
        assert seen
        assert not [name for name in seen if name.startswith("hidden")]
    finally:
        space.close()


_LINKED = "The version replaced a folder of the scoring box with a link, so it has no score."


def test_a_version_that_swaps_its_checkout_for_a_link_gets_no_metric_and_leaves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refuse the install when ``work`` is a link to a folder the next checkout would keep (E38)."""
    space, runtime = _metric_space(tmp_path)
    try:
        box = space.session()
        seen = _left_at_close(box, monkeypatch)
        root = Path(box.run("pwd").stdout.strip())
        assert box.run(f"cd {REPO_DIR} && mv work w2 && ln -s w2 work").ok

        assert space.install_metric_files() == _LINKED

        assert not (root / REPO_DIR / "w2" / "eval" / "metric.py").exists()
        assert box.closed
        assert seen
        assert not [name for name in seen if name.startswith("hidden")]
        assert space.checkout("").path == WORK_DIR
        assert len(runtime.boxes) == 2
    finally:
        space.close()


def test_a_version_that_moves_the_repo_folder_behind_a_link_gets_no_metric_there(tmp_path: Path) -> None:
    """Refuse the install before the archive goes in when ``.app-repo`` is a link (E38)."""
    space, _ = _metric_space(tmp_path)
    outside = tmp_path / "moved"
    outside.mkdir()
    try:
        box = space.session()
        assert box.run(f"mv {REPO_DIR} {outside}/{REPO_DIR} && ln -s {outside}/{REPO_DIR} {REPO_DIR}").ok

        assert space.install_metric_files() == _LINKED

        moved = outside / REPO_DIR
        assert not [path.name for path in moved.iterdir() if path.name.startswith("hidden")]
        assert not (moved / "work" / "eval" / "metric.py").exists()
        assert box.closed
    finally:
        space.close()


def test_a_checkout_removes_what_a_version_left_next_to_its_tree(tmp_path: Path) -> None:
    """Keep only the prepared tree in ``.app-repo``, so no copy of a checkout outlives it (E38)."""
    space, runtime = _metric_space(tmp_path)
    try:
        box = space.session()
        root = Path(box.run("pwd").stdout.strip())
        assert box.run(f"cd {REPO_DIR} && cp -a work w2 && touch loose && ln -s w2 link").ok

        assert space.checkout("").path == WORK_DIR

        assert sorted(path.name for path in (root / REPO_DIR).iterdir()) == ["prepared", "version.patch", "work"]
        assert len(runtime.boxes) == 1
    finally:
        space.close()


def test_a_checkout_in_a_box_whose_prepared_tree_is_a_link_uses_a_fresh_box(tmp_path: Path) -> None:
    """Never copy a checkout from a prepared tree a version replaced with a link (E38)."""
    space, runtime = _metric_space(tmp_path)
    try:
        box = space.session()
        assert box.run(f"cd {REPO_DIR} && mv prepared p2 && ln -s p2 prepared").ok

        assert space.checkout("").path == WORK_DIR

        assert box.closed
        assert len(runtime.boxes) == 2
        assert space.install_metric_files() is None
    finally:
        space.close()


def test_version_code_that_rewrites_the_hidden_metric_while_it_runs_is_caught(tmp_path: Path) -> None:
    """Fingerprint the hidden metric like a protected file, and never name it to the agent (SKY-353)."""
    archive = _archive(tmp_path, {"src/app.py": "x = 1\n", "eval/metric.py": "def score(): return 0.1\n"})
    space = RepoWorkspace(
        runtime=_Runtime(),
        spec=_ONLINE,
        archive=archive,
        editable_paths=["src"],
        readonly_paths=[],
        setup_command=None,
        secrets={},
        hidden_paths=["eval/metric.py", ".optimize/exploits"],
    )
    try:
        version = _patch(tmp_path / "one", archive, {"src/app.py": "x = 2\n"})
        space.checkout(version)
        assert space.stamp_before_run() is None
        space.session().run(f"cd {WORK_DIR} && cat eval/metric.py > /dev/null")
        assert space.protected_changes() == ()

        space.checkout(version)
        space.stamp_before_run()
        space.session().run(f"cd {WORK_DIR} && printf 'def score(): return 1.0\\n' > eval/metric.py")
        assert space.protected_changes() == ("The metric files changed while the version ran.",)

        space.checkout(version)
        space.stamp_before_run()
        space.session().run(f"cd {WORK_DIR} && mkdir -p .optimize/exploits && touch .optimize/exploits/a")
        assert space.protected_changes() == ("The metric files changed while the version ran.",)
    finally:
        space.close()
