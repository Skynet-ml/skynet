"""Prepare a repository once, then open a candidate box and a metric box for every evaluation.

The trusted parent owns every box here, not the guest that hosts the agent,
so a run's secrets never share a filesystem with the agent that writes the
versions. Each box holds only the code it needs (SKY-398):

- The setup box gets the repository without the metric files. It runs the
  setup command once, with the registry allowlist and without the run's
  secrets. The parent then saves the prepared state: a snapshot when the
  runtime has snapshots, else a tar file in the parent (local Docker keeps
  ``--read-only``, so there is no ``docker commit``). The parent keeps the
  prepared tree's digest.
- The metric setup step runs once in its own box. It installs only the
  packages of ``METRIC_REQUIREMENTS`` and saves a second state that holds no
  repository code. Its network is open only for that install.
- A candidate box C opens for each evaluation from the prepared state,
  checks the digest and lays out the version. The parent ends it after the
  ``[run]`` entry; closing the box is the only cleanup.
- A metric box M opens for each evaluation from the metric state. It holds
  only the metric files and the runner, never a repository tree.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import posixpath
import re
import shlex
import shutil
import tarfile
import tempfile
import threading
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from ....billing.budgets import BudgetError
from ....billing.operation_pricing import UnpricedOperationError
from ....billing.runtime import UsagePendingError
from ....billing.signals import BudgetReached
from ....sandbox.snapshots import snapshot_cache_key
from .repo_tree import (
    MAX_PATCH_BYTES,
    _member_path,
    archive_chunks,
    drop_paths,
    inside,
    patch_violations,
    strip_archive,
)
from .sandbox_contract import CommandResult, OutputSink, SandboxRuntime, SandboxSession, SandboxSpec

logger = logging.getLogger(__name__)

# A snapshot fault falls back to a tar state; a budget refusal must stop the
# run instead, as it does for any other box.
_BUDGET_ERRORS = (BudgetReached, BudgetError, UsagePendingError, UnpricedOperationError)

REPO_DIR = ".app-repo"
BASE_DIR = f"{REPO_DIR}/base"
WORK_DIR = f"{REPO_DIR}/work"
PREPARED_DIR = f"{REPO_DIR}/prepared"
PATCH_FILE = f"{REPO_DIR}/version.patch"
# The compose services run in their own copy of the prepared tree, outside
# REPO_DIR, so the version's checkout and the services never share files.
SERVICES_DIR = ".app-services"
# The metric box's own folder. ``repo_path`` in the metric is METRIC_REPO_DIR:
# the metric files at their repository paths, and nothing else of the tree.
METRIC_DIR = ".app-metric"
METRIC_REPO_DIR = f"{METRIC_DIR}/repo"
# The repository file that lists the metric's packages, one requirement per
# line. The metric setup step installs only these into the metric box.
METRIC_REQUIREMENTS = ".optimize/metric-requirements.txt"
_METRIC_READY = f"{METRIC_DIR}/ready"
# The saved state's parts while the parent reads them out of a box.
_STATE_DIR = ".app-state"
# A multiple of 4, so each base64 part decodes on its own.
_STATE_PART_BYTES = 4 * 1024 * 1024
_SETUP_TIMEOUT_SECONDS = 1_800.0
_GIT_IDENTITY = "-c user.name=app -c user.email=app@localhost -c commit.gpgsign=false"
# A commit may start ``git maintenance`` in the background (detached since
# git 2.47), whose lock files come and go while ``cp -a`` walks ``.git``.
_GIT_NO_MAINTENANCE = "-c maintenance.auto=false -c gc.auto=0"
# Box git reads only the repository's own config: a filter, hook or fsmonitor
# command in $HOME or the XDG folder must not run in a box git call.
GIT = (
    "env HOME=/nonexistent XDG_CONFIG_HOME=/nonexistent GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null"
    " GIT_CONFIG_SYSTEM=/dev/null GIT_ATTR_NOSYSTEM=1 git -c core.attributesFile=/dev/null"
    " -c core.excludesFile=/dev/null -c core.hooksPath=/dev/null -c core.fsmonitor=false"
)
_HASH_TIMEOUT_SECONDS = 300.0
# One requirement specifier: a name, optional extras, markers and versions.
# Options (``-e``, ``--index-url``, ``-r``), paths and URLs fail, so the step
# installs only named packages from the registry and never repository code.
_REQUIREMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9._,-]+\])?\s*([<>=!~;][^@/\\]*)?")
# Puts the metric files into the metric box from a base64 tar.gz. ``-I``:
# nothing in the box may shadow a module. The ``data`` filter, where it
# exists, keeps every member inside the folder; without it, only plain files
# and folders pass.
_INSTALL_METRIC = r"""
import base64, io, os, sys, tarfile

source = sys.argv[1]
with open(source, "rb") as handle:
    data = base64.b64decode(handle.read(), validate=True)
os.unlink(source)
data_filter = getattr(tarfile, "data_filter", None)
with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
    members = archive.getmembers()
    if data_filter is None and not all(member.isfile() or member.isdir() for member in members):
        sys.exit(4)
    for member in members:
        name = os.path.normpath(member.name)
        if os.path.isabs(name) or name == ".." or name.startswith("../"):
            sys.exit(4)
    if data_filter is None:
        archive.extractall(".")
    else:
        archive.extractall(".", filter=data_filter)
"""
# Linux caps one environment string at 128 KiB; a larger stage answer goes
# through a file write and fresh stamps instead.
_ANSWER_ENV_BYTES = 100_000
# Fingerprints every file under the protected paths. It runs with ``-I`` so a
# module the version drops into the checkout cannot shadow ``hashlib``; links
# and special files are recorded rather than followed or opened. Besides the
# contents it records each file's and folder's inode and change times: any
# write, rename or link moves the change time, so a file rewritten and then
# restored while the ``[run]`` entry runs still shows.
# Given APP_ANSWER_PATH, it then writes APP_ANSWER there from inside the box:
# unlike a Vercel file write, that moves no protected file's change time, so
# a relay answer needs no fresh stamps after it.
_HASH_PROTECTED = r"""
import hashlib, json, os, sys

def digest(path):
    if os.path.islink(path):
        return "link:" + os.readlink(path)
    if not os.path.isfile(path):
        return "special"
    sha = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            sha.update(block)
    return sha.hexdigest()

def stamp(path):
    info = os.lstat(path)
    return f"{info.st_ino}:{info.st_mtime_ns}:{info.st_ctime_ns}"

found, stamps = {}, {}
for top in sys.argv[1:]:
    if os.path.isdir(top) and not os.path.islink(top):
        for folder, folders, files in os.walk(top):
            folders[:] = [name for name in folders if name != ".git"]
            stamps[os.path.normpath(folder)] = stamp(folder)
            linked = [name for name in folders if os.path.islink(os.path.join(folder, name))]
            for name in files + linked:
                path = os.path.normpath(os.path.join(folder, name))
                found[path] = digest(path)
                stamps[path] = stamp(path)
    elif os.path.lexists(top):
        found[os.path.normpath(top)] = digest(top)
        stamps[os.path.normpath(top)] = stamp(top)
answer = os.environ.get("APP_ANSWER_PATH")
if answer:
    os.makedirs(os.path.dirname(answer), exist_ok=True)
    with open(answer + ".tmp", "w", encoding="utf-8") as handle:
        handle.write(os.environ["APP_ANSWER"])
    os.replace(answer + ".tmp", answer)
print(json.dumps({"files": found, "stamps": stamps}, sort_keys=True))
"""
# Hashes the whole prepared tree, ``.git`` included: each path's type, mode,
# link target and content. A candidate box from the saved state must hold the
# tree that setup left; only the digest leaves the box.
_HASH_PREPARED = r"""
import hashlib, os, stat

sha = hashlib.sha256()
for folder, folders, files in os.walk(".", followlinks=False):
    folders.sort()
    for name in sorted(folders + files):
        path = os.path.join(folder, name)
        info = os.lstat(path)
        sha.update(f"{path}\0{stat.S_IFMT(info.st_mode)}\0{stat.S_IMODE(info.st_mode)}\0".encode("utf-8", "surrogateescape"))
        if stat.S_ISLNK(info.st_mode):
            sha.update(os.fsencode(os.readlink(path)))
        elif stat.S_ISREG(info.st_mode):
            with open(path, "rb") as handle:
                for block in iter(lambda: handle.read(1 << 20), b""):
                    sha.update(block)
        sha.update(b"\0")
print(sha.hexdigest())
"""


class RepoSetupError(RuntimeError):
    """The repository could not be prepared, so no version can be scored."""


class _SnapshotLostError(RepoSetupError):
    """A box from a snapshot did not open or did not hold the saved state."""


@dataclass(frozen=True)
class Checkout:
    """One version laid out on disk, or the reason it could not be."""

    path: str | None
    problems: tuple[str, ...] = ()
    # The run's path rules refused the version, as opposed to a change that
    # is too large or does not apply.
    blocked: bool = False


@dataclass
class _State:
    """One saved box state: a runtime snapshot, or a tar.gz file in the parent."""

    snapshot_id: str | None = None
    archive: Path | None = None


def redact(text: str, secrets: Iterable[str]) -> str:
    """Hide every secret value a command could have printed.

    Args:
        text: Command output or scorer feedback.
        secrets: Values to hide; short ones are skipped, since masking them
            would scramble ordinary output.

    Returns:
        The text with each secret replaced by ``[secret]``.
    """
    for value in sorted({value for value in secrets if len(value) >= 6}, key=len, reverse=True):
        text = text.replace(value, "[secret]")
    return text


def _tail(result: CommandResult, limit: int = 4_000) -> str:
    """Keep the end of a command's output, where failures usually are.

    Args:
        result: Finished command.
        limit: Most characters kept.

    Returns:
        The combined output's last ``limit`` characters.
    """
    combined = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
    return combined[-limit:]


def read_metric_requirements(archive: Path) -> tuple[str, ...]:
    """Read the metric's package list from the repository archive.

    Args:
        archive: Packed full tree.

    Returns:
        One requirement per non-empty, non-comment line of
        ``METRIC_REQUIREMENTS``; empty when the file is missing.

    Raises:
        RepoSetupError: When a line is not a plain requirement specifier,
            such as an option, a path or a URL.
    """
    with tarfile.open(archive) as source:
        member = next(
            (item for item in source if item.isreg() and _member_path(item.name) == METRIC_REQUIREMENTS), None
        )
        if member is None:
            return ()
        handle = source.extractfile(member)
        text = handle.read().decode("utf-8", errors="replace") if handle is not None else ""
    lines = [line.split("#", 1)[0].strip() for line in text.splitlines()]
    requirements = tuple(line for line in lines if line)
    for line in requirements:
        if not _REQUIREMENT.fullmatch(line):
            raise RepoSetupError(
                f"{METRIC_REQUIREMENTS}: {line[:100]!r} is not a package requirement such as 'numpy==2.1.0'."
                " Options, paths and URLs are not allowed."
            )
    return requirements


def pack_metric_files(archive: Path, hidden_paths: Iterable[str]) -> str | None:
    """Pack the metric files of the repository archive as a base64 tar.gz, in the parent.

    Only plain files and folders under the hidden paths go in, so no link can
    point the metric box at a file outside them.

    Args:
        archive: Packed full tree.
        hidden_paths: The metric files and exploit folder.

    Returns:
        The packed files, or ``None`` when the archive holds none of them.
    """
    hidden = [_member_path(path) for path in hidden_paths]
    buffer = io.BytesIO()
    found = False
    with tarfile.open(archive) as source, tarfile.open(fileobj=buffer, mode="w:gz") as target:
        for member in source:
            name = _member_path(member.name)
            if not inside(name, hidden) or not (member.isreg() or member.isdir()):
                continue
            copy = tarfile.TarInfo(name)
            copy.type, copy.size, copy.mode, copy.mtime = member.type, member.size, member.mode & 0o755, member.mtime
            target.addfile(copy, source.extractfile(member) if member.isreg() else None)
            found = True
    return base64.b64encode(buffer.getvalue()).decode("ascii") if found else None


def box_root(session: SandboxSession) -> str:
    """Return a box's working directory, which relative paths in it resolve against.

    Args:
        session: The open box.

    Returns:
        The absolute directory.

    Raises:
        RepoSetupError: When the box cannot report it.
    """
    located = session.run("pwd", timeout_seconds=30)
    root = located.stdout.strip().splitlines()[-1] if located.ok and located.stdout.strip() else ""
    if not root.startswith("/"):
        raise RepoSetupError("The box could not report its working directory.")
    return root


def _close_quietly(session: SandboxSession, what: str) -> None:
    """Close a box and log only a failure's type.

    Args:
        session: The box.
        what: Names the box in the log line.
    """
    try:
        session.close()
    except Exception as close_error:
        # Only the type: settlement and ledger texts can name the provider.
        logger.warning("%s did not close cleanly (%s)", what, type(close_error).__name__)


class CandidateBox:
    """One evaluation's candidate box C: the prepared repository with the version laid out."""

    def __init__(self, workspace: RepoWorkspace, session: SandboxSession, root: str) -> None:
        """Bind an open, laid-out box to the workspace that checks its protected files.

        Args:
            workspace: The workspace that opened the box.
            session: The box.
            root: The box's working directory.
        """
        self._workspace = workspace
        self.session = session
        self.root = root
        self.path = WORK_DIR
        self._stamps: dict[str, str] | None = None
        self._lock = threading.Lock()
        self._closed = False

    def stamp_before_run(self) -> str | None:
        """Record the protected files' inodes and change times just before the version runs.

        Call it after the last file the parent writes into the box: a Vercel
        file write moves the change time of every file in the box.

        Returns:
            Why the files could not be stamped, or ``None`` once they are.
        """
        if not self._workspace.watched:
            return None
        with self._lock:
            before = self._workspace.fingerprint(self.session, WORK_DIR)
            if before is None:
                return "The protected files could not be checked before the version ran."
            self._stamps = before[1]
            return None

    def protected_changes(self) -> tuple[str, ...]:
        """Explain how the version's code changed the protected files while it ran.

        Contents are compared with the prepared tree, and inodes and change
        times with the last stamp.

        Returns:
            One reason per protected file added, changed or removed, or why
            they could not be checked; empty when untouched.
        """
        if not self._workspace.watched:
            return ()
        with self._lock:
            current = self._workspace.fingerprint(self.session, WORK_DIR)
            before = self._stamps
        if current is None or before is None:
            return ("The protected files could not be checked after the version ran.",)
        return self._workspace.changes(current, before)

    def answer(self, path: str, text: str) -> tuple[str, ...] | None:
        """Check the protected files while the version runs, then write one relay answer into the box.

        The check and the write share one box command, so the stamps taken
        before the run stay valid; an answer too large for that is written
        with a file write and the files are stamped again after it.

        Args:
            path: Where the waiting client reads the answer, relative to the box.
            text: The answer.

        Returns:
            Like :meth:`protected_changes`, or ``None`` when the files could
            not be checked or the answer could not be written.
        """
        workspace = self._workspace
        with self._lock:
            before = self._stamps
            if not workspace.watched:
                return () if _deliver(self.session, path, text) else None
            if before is not None and len(text.encode("utf-8")) <= _ANSWER_ENV_BYTES:
                current = workspace.fingerprint(self.session, WORK_DIR, answer=(path, text))
                if current is not None:
                    return workspace.changes(current, before)
                _deliver(self.session, path, text)
                return None
            current = workspace.fingerprint(self.session, WORK_DIR) if before is not None else None
            if not _deliver(self.session, path, text) or current is None or before is None:
                return None
            after = workspace.fingerprint(self.session, WORK_DIR)
            if after is None:
                return None
            self._stamps = after[1]
            return workspace.changes(current, before)

    def close(self) -> None:
        """End the box; every process the version started ends with it."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.session.close()


class MetricBox:
    """One evaluation's metric box M: the metric state, the metric files and the runner, no repository."""

    def __init__(self, workspace: RepoWorkspace, session: SandboxSession, root: str) -> None:
        """Bind an open metric box.

        Args:
            workspace: The workspace that opened the box.
            session: The box.
            root: The box's working directory.
        """
        self._workspace = workspace
        self.session = session
        self.root = root
        self.repo_path = f"{root}/{METRIC_REPO_DIR}"
        self._closed = False

    def install_metric_files(self) -> None:
        """Put the metric files into ``METRIC_REPO_DIR`` at their repository paths.

        Call it only after the candidate box ended (SKY-398).

        Raises:
            RepoSetupError: When the files cannot be installed.
        """
        files = self._workspace.metric_files
        made = self.session.run(f"mkdir -p {METRIC_REPO_DIR}", timeout_seconds=60)
        if not made.ok:
            raise RepoSetupError(f"The metric folder could not be made: {_tail(made)}")
        if files is None:
            return
        source = f"{METRIC_DIR}/metric-{uuid.uuid4().hex}.b64"
        self.session.write_files({source: files})
        result = self.session.run(
            f"cd {METRIC_REPO_DIR} && python3 -I -c {shlex.quote(_INSTALL_METRIC)} ../{posixpath.basename(source)}",
            timeout_seconds=_SETUP_TIMEOUT_SECONDS,
        )
        if not result.ok:
            raise RepoSetupError(f"The metric files could not be installed: {_tail(result)}")

    def close(self) -> None:
        """End the box."""
        if self._closed:
            return
        self._closed = True
        self.session.close()


def _deliver(session: SandboxSession, path: str, text: str) -> bool:
    """Write one relay answer with a file write.

    Args:
        session: The box the answer goes to.
        path: Where the answer goes, relative to the box.
        text: The answer.

    Returns:
        Whether the write succeeded.
    """
    try:
        session.write_files({path: text})
    except Exception as error:
        logger.warning("A relay answer could not reach the box: %s", error)
        return False
    return True


class RepoWorkspace:
    """Prepare a repository once, then open a fresh candidate box and metric box for each evaluation.

    No box outlives its evaluation, so nothing one version's code writes or
    starts reaches the next version or the metric.
    """

    def __init__(
        self,
        *,
        runtime: SandboxRuntime,
        spec: SandboxSpec,
        archive: Path,
        editable_paths: Iterable[str],
        readonly_paths: Iterable[str],
        setup_command: str | None,
        secrets: Mapping[str, str],
        protected_paths: Iterable[str] = (),
        hidden_paths: Iterable[str] = (),
        run_hosts: Iterable[str] = (),
        run_vcpus: int | None = None,
        metric_hosts: Iterable[str] = (),
        metric_vcpus: int | None = None,
    ) -> None:
        """Bind a snapshot to a runtime without opening anything yet.

        Args:
            runtime: Where the boxes are opened.
            spec: Lifetime, labels, compose services and the setup box's
                network policy; the registry allowlist is the setup's and the
                metric setup step's only network.
            archive: Packed full tree from :func:`fetch_snapshot`.
            editable_paths: Files and folders versions may change.
            readonly_paths: Submodules and Git LFS files.
            setup_command: Shell command run once on the starting commit.
            secrets: Environment the scorer receives; setup never does.
            protected_paths: Files and folders no version may change, even
                inside an editable path.
            hidden_paths: The metric files and exploit folder. Only the
                metric box gets them; a version's edits to them are dropped.
            run_hosts: The ``[run] network`` hosts of the candidate box; empty keeps it offline.
            run_vcpus: The ``[run] vcpus`` of the candidate box; ``None`` keeps ``spec.vcpus``.
            metric_hosts: The ``[metric] network`` hosts of the metric box; empty keeps it offline.
            metric_vcpus: The ``[metric] vcpus`` of the metric box; ``None`` keeps ``spec.vcpus``.
        """
        self._runtime = runtime
        self._spec = spec
        self._archive = archive
        self._editable = tuple(editable_paths)
        self._readonly = tuple(readonly_paths)
        self._protected = tuple(protected_paths)
        self._hidden = tuple(posixpath.normpath(path) for path in hidden_paths)
        self._run_hosts = tuple(run_hosts)
        self._run_vcpus = run_vcpus
        self._metric_hosts = tuple(metric_hosts)
        self._metric_vcpus = metric_vcpus
        self._setup = (setup_command or "").strip() or None
        self._secrets = dict(secrets)
        self._failure: BaseException | None = None
        self._setup_log = ""
        self._prepared = False
        # Held in the parent: the protected files after setup, and the
        # prepared tree's digest, which every candidate box checks.
        self._protected_baseline: dict[str, str] = {}
        self._prepared_digest: str | None = None
        self.metric_files: str | None = None
        self._requirements: tuple[str, ...] = ()
        self._repo_state: _State | None = None
        self._metric_state: _State | None = None
        self._folder: Path | None = None
        # Each workspace has its own keys, so no other box can reuse its snapshots.
        unique = uuid.uuid4().hex
        self._snapshot_key = snapshot_cache_key(
            image_digest=spec.image or "",
            setup_command=self._setup or "",
            install_command="",
            lock_files={"workspace": unique},
        )
        self._metric_snapshot_key = snapshot_cache_key(
            image_digest=spec.image or "", setup_command="", install_command="", lock_files={"metric": unique}
        )
        self._snapshots_off = False
        self._opened = 0
        self._lock = threading.Lock()

    @property
    def secrets(self) -> dict[str, str]:
        """Return the environment versions run with."""
        return dict(self._secrets)

    @property
    def setup_log(self) -> str:
        """Return the end of the setup command's output, once it has run."""
        return self._setup_log

    @property
    def watched(self) -> tuple[str, ...]:
        """Return the protected paths the candidate box checks."""
        return self._protected

    def prepare(self, *, on_output: OutputSink | None = None) -> None:
        """Run the setup box and the metric setup step once, and keep both states.

        A failure is remembered, so no later call opens a box again.

        Args:
            on_output: Receives setup output as it streams.

        Raises:
            RepoSetupError: When the repository or the metric packages cannot be set up.
        """
        with self._lock:
            if self._prepared:
                return
            if self._failure is not None:
                raise self._failure
            try:
                self._prepare(on_output)
            except BaseException as error:
                self._failure = error
                raise
            self._prepared = True

    def _prepare(self, on_output: OutputSink | None) -> None:
        """Split the archive, then save the repository state and the metric state; the caller holds the lock.

        Args:
            on_output: Receives setup output as it streams.

        Raises:
            RepoSetupError: When a step fails.
        """
        if self._folder is None:
            self._folder = Path(tempfile.mkdtemp(prefix="repo-workspace-"))
            self._requirements = read_metric_requirements(self._archive)
            self.metric_files = pack_metric_files(self._archive, self._hidden) if self._hidden else None
        self._repo_state = self._setup_repository(on_output)
        self._metric_state = self._setup_metric() if self._requirements else None

    def _next_key(self, kind: str) -> str | None:
        """Return a new operation key for one box, so each box reserves its own hold (E31).

        Args:
            kind: What the box is for: ``setup``, ``metric-setup``, ``candidate`` or ``metric``.

        Returns:
            ``<key>:<kind>-<n>``, or ``None`` when the spec has no key.
        """
        number = self._opened
        self._opened += 1
        key = self._spec.operation_key
        return None if key is None else f"{key}:{kind}-{number}"

    def _setup_repository(self, on_output: OutputSink | None) -> _State:
        """Open the setup box, run setup without the metric files, save its state and end the box.

        Args:
            on_output: Receives setup output as it streams.

        Returns:
            The saved state.

        Raises:
            RepoSetupError: When the tree cannot be unpacked, set up or saved.
        """
        assert self._folder is not None
        stripped = strip_archive(self._archive, self._hidden, self._folder / "repo.tar.gz")
        session = self._runtime.open(
            replace(self._spec, operation_key=self._next_key("setup"), services=(), snapshot_id=None)
        )
        try:
            self._unpack_and_setup(session, stripped, on_output)
            return self._save(session, self._snapshot_key, "repo-state")
        finally:
            _close_quietly(session, "The setup box")

    def _unpack_and_setup(self, session: SandboxSession, archive: Path, on_output: OutputSink | None) -> None:
        """Unpack the tree and run the setup command once on the starting commit.

        Setup runs at ``WORK_DIR`` because virtualenvs and editable installs
        record absolute paths, and each candidate box lays the version out at
        that same path.

        Args:
            session: The setup box.
            archive: The tree without the metric files.
            on_output: Receives setup output as it streams.

        Raises:
            RepoSetupError: When the tree cannot be unpacked or set up.
        """
        parts = []
        for index, chunk in enumerate(archive_chunks(archive)):
            name = f"{REPO_DIR}/tree.{index:04d}.b64"
            session.write_files({name: chunk})
            parts.append(name)
        files = " ".join(shlex.quote(part) for part in parts)
        result = session.run(
            f"set -e; mkdir -p {BASE_DIR}; cat {files} | base64 -d | tar -xzf - -C {BASE_DIR}; rm -f {files};"
            f" cd {BASE_DIR}; {GIT} init -q; {GIT} add -A -f; {GIT} {_GIT_IDENTITY} {_GIT_NO_MAINTENANCE} commit -q --allow-empty -m base;"
            f" cd - >/dev/null; rm -rf {WORK_DIR} {PREPARED_DIR}; mv {BASE_DIR} {WORK_DIR}",
            timeout_seconds=_SETUP_TIMEOUT_SECONDS,
        )
        if not result.ok:
            raise RepoSetupError(f"The repository could not be unpacked: {_tail(result)}")
        if self._setup is not None:
            setup = session.run(
                f"cd {WORK_DIR} && {self._setup}", timeout_seconds=_SETUP_TIMEOUT_SECONDS, on_output=on_output
            )
            self._setup_log = redact(_tail(setup), self._secrets.values())
            if setup.timed_out:
                raise RepoSetupError(f"The setup command ran out of time.\n{self._setup_log}")
            if not setup.ok:
                raise RepoSetupError(f"The setup command failed (exit {setup.exit_code}).\n{self._setup_log}")
        moved = session.run(f"mv {WORK_DIR} {PREPARED_DIR}", timeout_seconds=_SETUP_TIMEOUT_SECONDS)
        if not moved.ok:
            raise RepoSetupError(f"The prepared repository could not be kept: {_tail(moved)}")
        if self._protected:
            baseline = self.fingerprint(session, PREPARED_DIR)
            if baseline is None:
                raise RepoSetupError("The repository's protected files could not be fingerprinted.")
            self._protected_baseline = baseline[0]
        self._prepared_digest = self._digest_prepared(session)
        if self._prepared_digest is None:
            raise RepoSetupError("The prepared repository could not be fingerprinted.")

    def _setup_metric(self) -> _State:
        """Install only the declared metric packages in a box without repository code, and save its state.

        Returns:
            The saved state.

        Raises:
            RepoSetupError: When the install fails or the state cannot be saved.
        """
        hosts = tuple(self._spec.allowed_hosts)
        spec = replace(
            self._spec,
            operation_key=self._next_key("metric-setup"),
            services=(),
            snapshot_id=None,
            private=True,
            vcpus=self._metric_vcpus or self._spec.vcpus,
            network_disabled=not hosts,
            allowed_hosts=hosts,
        )
        session = self._runtime.open(spec)
        try:
            session.write_files({f"{METRIC_DIR}/requirements.txt": "\n".join(self._requirements) + "\n"})
            result = session.run(
                f"python3 -m pip install --user --no-input --disable-pip-version-check -r {METRIC_DIR}/requirements.txt"
                f" && rm -f {METRIC_DIR}/requirements.txt && touch {_METRIC_READY}",
                env={"PIP_BREAK_SYSTEM_PACKAGES": "1"},
                timeout_seconds=_SETUP_TIMEOUT_SECONDS,
            )
            if not result.ok:
                log = redact(_tail(result), self._secrets.values())
                raise RepoSetupError(f"The metric packages ({METRIC_REQUIREMENTS}) could not be installed.\n{log}")
            return self._save(session, self._metric_snapshot_key, "metric-state")
        finally:
            _close_quietly(session, "The metric setup box")

    def _save(self, session: SandboxSession, key: str, name: str) -> _State:
        """Save a box's state as a snapshot, or as a tar.gz in the parent when the runtime has none.

        Args:
            session: The box, after its setup.
            key: The snapshot key.
            name: The tar file's name in the parent.

        Returns:
            The saved state.

        Raises:
            RepoSetupError: When the tar cannot be made or read.
        """
        take = getattr(self._runtime, "snapshot", None)
        if take is not None and not self._snapshots_off:
            try:
                snapshot_id = take(session, key)
            except _BUDGET_ERRORS:
                raise
            except Exception as error:
                # Only the type: the provider's text can name the snapshot.
                logger.warning("The snapshot failed (%s); keeping the state as a tar", type(error).__name__)
                self._snapshots_off = True
                snapshot_id = None
            if snapshot_id is not None:
                return _State(snapshot_id=snapshot_id)
        return _State(archive=self._tar_state(session, name))

    def _tar_state(self, session: SandboxSession, name: str) -> Path:
        """Pack the box's working directory and copy it into the parent in parts.

        Args:
            session: The box.
            name: The file's name in the parent.

        Returns:
            The tar.gz file in the parent.

        Raises:
            RepoSetupError: When the tar cannot be made or read.
        """
        assert self._folder is not None
        packed = session.run(
            f"set -e; rm -rf {_STATE_DIR}; mkdir {_STATE_DIR};"
            # No ``.`` member: the box user may not own its working directory,
            # so a restore that sets the mode or time of ``.`` fails there.
            f" find . -mindepth 1 -maxdepth 1 ! -name {_STATE_DIR} ! -name .cache -print0"
            f" | tar -czf - --null -T - | base64 -w0"
            f" | split -a 4 -b {_STATE_PART_BYTES} - {_STATE_DIR}/part.; ls {_STATE_DIR}",
            timeout_seconds=_SETUP_TIMEOUT_SECONDS,
        )
        parts = sorted(line.strip() for line in packed.stdout.splitlines() if line.strip().startswith("part."))
        if not packed.ok or not parts:
            raise RepoSetupError(f"The prepared box could not be packed: {_tail(packed)}")
        path = self._folder / f"{name}.tar.gz"
        with path.open("wb") as handle:
            for part in parts:
                text = session.read_file(f"{_STATE_DIR}/{part}")
                if text is None:
                    raise RepoSetupError("The prepared box could not be read.")
                handle.write(base64.b64decode(text.strip()))
        return path

    def _open_box(self, spec: SandboxSpec, state: _State | None) -> SandboxSession:
        """Open one box from a saved state.

        Args:
            spec: The box's spec without its state.
            state: The saved state; ``None`` opens the plain image.

        Returns:
            The open box, with a tar state unpacked.

        Raises:
            RepoSetupError: When a tar state cannot be unpacked.
            _SnapshotLostError: When the box cannot open from its snapshot.
        """
        if state is not None and state.snapshot_id is not None:
            spec = replace(spec, snapshot_id=state.snapshot_id)
        elif state is not None and state.archive is not None:
            spec = replace(spec, snapshot_archive=str(state.archive))
        try:
            session = self._runtime.open(spec)
        except _BUDGET_ERRORS:
            raise
        except Exception as error:
            if spec.snapshot_id is None:
                raise
            # Only the type: the provider's text can name the snapshot.
            logger.warning("A box did not open from its snapshot (%s)", type(error).__name__)
            self._drop_snapshots()
            raise _SnapshotLostError("A box could not open from the saved snapshot.") from None
        if state is None or state.archive is None:
            return session
        try:
            parts = []
            for index, chunk in enumerate(archive_chunks(state.archive)):
                name = f"{_STATE_DIR}/state.{index:04d}.b64"
                session.write_files({name: chunk})
                parts.append(name)
            files = " ".join(shlex.quote(part) for part in parts)
            result = session.run(
                f"set -e; cat {files} | base64 -d | tar -xzf - -C .; rm -rf {_STATE_DIR}",
                timeout_seconds=_SETUP_TIMEOUT_SECONDS,
            )
            if not result.ok:
                raise RepoSetupError(f"The saved box state could not be unpacked: {_tail(result)}")
        except BaseException:
            _close_quietly(session, "A box from a tar state")
            raise
        return session

    def _box_spec(self, kind: str, hosts: tuple[str, ...], vcpus: int | None, *, services: bool) -> SandboxSpec:
        """Return the spec of one evaluation box.

        Args:
            kind: ``candidate`` or ``metric``.
            hosts: The role's ``network`` hosts; empty keeps the box offline.
            vcpus: The role's ``vcpus``; ``None`` keeps the setup's.
            services: Whether the box runs the compose services.

        Returns:
            The spec, with its own operation key.
        """
        with self._lock:
            key = self._next_key(kind)
        return replace(
            self._spec,
            operation_key=key,
            network_disabled=not hosts,
            allowed_hosts=hosts,
            vcpus=vcpus or self._spec.vcpus,
            services=self._spec.services if services else (),
            private=kind == "metric",
            snapshot_id=None,
            snapshot_archive=None,
        )

    def violations(self, patch: str) -> list[str]:
        """Explain how a version breaks the run's path rules, without opening a box.

        Edits to hidden paths are not checked: the candidate box drops them.

        Args:
            patch: The version as a git patch against the snapshot.

        Returns:
            One reason per broken rule; empty when the version is allowed.
        """
        # The size limit applies to what the guest sent, not to what is left.
        kept = patch if len(patch.encode("utf-8")) > MAX_PATCH_BYTES else drop_paths(patch, self._hidden)
        return patch_violations(kept, self._editable, self._readonly, self._protected)

    def open_candidate(self, patch: str) -> CandidateBox | Checkout:
        """Open a candidate box from the prepared state and lay out one version.

        Args:
            patch: The version as ``git diff --binary`` against the snapshot.

        Returns:
            The open box, or why the version is not allowed or does not apply.

        Raises:
            RepoSetupError: When the workspace is not prepared or the box does
                not hold the prepared tree.
        """
        problems = self.violations(patch)
        if problems:
            return Checkout(path=None, problems=tuple(problems), blocked=len(patch.encode("utf-8")) <= MAX_PATCH_BYTES)
        patch = drop_paths(patch, self._hidden)
        try:
            return self._open_candidate(patch)
        except _SnapshotLostError:
            # Snapshots are now off, so this try sets up again with tar states.
            return self._open_candidate(patch)

    def _open_candidate(self, patch: str) -> CandidateBox | Checkout:
        """Open one candidate box from the saved state and apply an allowed version.

        Args:
            patch: The version, without its edits to hidden paths.

        Returns:
            The open box, or why the version does not apply.

        Raises:
            RepoSetupError: When the workspace is not prepared or the box does
                not hold the prepared tree.
            _SnapshotLostError: When a box from a snapshot did not open or did not hold the tree.
        """
        self.prepare()
        spec = self._box_spec("candidate", self._run_hosts, self._run_vcpus, services=True)
        state = self._repo_state
        session = self._open_box(spec, state)
        try:
            if self._digest_prepared(session) != self._prepared_digest:
                self._drop_snapshots()
                lost = state is not None and state.snapshot_id is not None
                raise (_SnapshotLostError if lost else RepoSetupError)(
                    "The candidate box does not hold the prepared repository."
                )
            self._start_services(session)
            session.write_files({PATCH_FILE: patch})
            applied = session.run(
                f"set -e; cp -a {PREPARED_DIR} {WORK_DIR}; cd {WORK_DIR};"
                f" if [ -s ../version.patch ]; then {GIT} apply --binary --whitespace=nowarn ../version.patch; fi",
                timeout_seconds=_SETUP_TIMEOUT_SECONDS,
            )
            if not applied.ok:
                _close_quietly(session, "The candidate box")
                return Checkout(path=None, problems=(f"The change does not apply cleanly: {_tail(applied)}",))
            return CandidateBox(self, session, box_root(session))
        except BaseException:
            _close_quietly(session, "The candidate box")
            raise

    def open_metric(self) -> MetricBox:
        """Open a metric box from the metric state, without any repository file.

        Returns:
            The open box; the caller installs the metric files once the candidate box ended.

        Raises:
            RepoSetupError: When the workspace is not prepared or the box does
                not hold the metric packages.
        """
        try:
            return self._open_metric()
        except _SnapshotLostError:
            # Snapshots are now off, so this try sets up again with tar states.
            return self._open_metric()

    def _open_metric(self) -> MetricBox:
        """Open one metric box from the metric state.

        Returns:
            The open box.

        Raises:
            RepoSetupError: When the workspace is not prepared or the box does
                not hold the metric packages.
            _SnapshotLostError: When a box from a snapshot did not open or did not hold the packages.
        """
        self.prepare()
        spec = self._box_spec("metric", self._metric_hosts, self._metric_vcpus, services=False)
        state = self._metric_state
        session = self._open_box(spec, state)
        try:
            if state is not None:
                ready = session.run(f"test -f {_METRIC_READY}", timeout_seconds=60)
                if not ready.ok:
                    self._drop_snapshots()
                    lost = state.snapshot_id is not None
                    raise (_SnapshotLostError if lost else RepoSetupError)(
                        "The metric box does not hold the metric packages."
                    )
            return MetricBox(self, session, box_root(session))
        except BaseException:
            _close_quietly(session, "The metric box")
            raise

    def _drop_snapshots(self) -> None:
        """Make the next evaluation set up again and keep tar states, after a snapshot did not restore."""
        with self._lock:
            if self._snapshots_off:
                return
            logger.warning("A box from a snapshot did not hold its saved state; the run keeps tar states instead")
            self._snapshots_off = True
            self._prepared = False

    def _start_services(self, session: SandboxSession) -> None:
        """Start the compose services on a copy of the prepared tree.

        Args:
            session: The candidate box.

        Raises:
            RepoSetupError: When the box cannot start services or the copy fails.
        """
        if not self._spec.services:
            return
        start = getattr(session, "start_services", None)
        if start is None:
            raise RepoSetupError("This sandbox cannot start the repository's compose services.")
        # Setup ran at WORK_DIR, so venv scripts and editable installs name
        # that path; text files in the copy are pointed at the copy instead.
        copied = session.run(
            f'set -e; work="$(pwd -P)/{WORK_DIR}"; lwork="$(pwd)/{WORK_DIR}"; rm -rf {SERVICES_DIR};'
            f' cp -a {PREPARED_DIR} {SERVICES_DIR}; cd {SERVICES_DIR}; here="$(pwd -P)";'
            ' grep -rlIF -e "$work" -e "$lwork" . | while IFS= read -r f; do t="$(mktemp)"; chmod u+w "$f"'
            ' && sed -e "s#$work#$here#g" -e "s#$lwork#$here#g" "$f" > "$t" && cat "$t" > "$f"'
            ' || { rm -f "$t"; exit 1; }; rm -f "$t"; done; pwd -P',
            timeout_seconds=_SETUP_TIMEOUT_SECONDS,
        )
        lines = copied.stdout.strip().splitlines() if copied.ok else []
        if not lines or not lines[-1].startswith("/"):
            raise RepoSetupError(f"The repository could not be copied for the compose services: {_tail(copied)}")
        start(lines[-1])

    def _digest_prepared(self, session: SandboxSession) -> str | None:
        """Hash the whole prepared tree in a box.

        Args:
            session: The box holding the prepared tree.

        Returns:
            The tree's SHA-256 digest, or ``None`` when the hashing command
            failed or printed something that is not a digest.
        """
        result = session.run(
            f"cd {PREPARED_DIR} && python3 -I -c {shlex.quote(_HASH_PREPARED)}", timeout_seconds=_HASH_TIMEOUT_SECONDS
        )
        lines = result.stdout.strip().splitlines() if result.ok else []
        digest = lines[-1] if lines else ""
        return digest if len(digest) == 64 and all(char in "0123456789abcdef" for char in digest) else None

    def fingerprint(
        self, session: SandboxSession, directory: str, answer: tuple[str, str] | None = None
    ) -> tuple[dict[str, str], dict[str, str]] | None:
        """Hash every file under the protected paths of one tree in a box.

        Args:
            session: The box holding the tree.
            directory: The tree's folder, relative to the box.
            answer: A path relative to the box and the text to write there
                once the hashing is done, in the same command.

        Returns:
            Repository-relative path to digest, and path to inode and change
            times for every file and folder, or ``None`` when the hashing
            command failed or printed something unreadable.
        """
        paths = " ".join(shlex.quote(path) for path in self._protected)
        target = f'export APP_ANSWER_PATH="$PWD"/{shlex.quote(answer[0])} && ' if answer is not None else ""
        result = session.run(
            f"{target}cd {shlex.quote(directory)} && python3 -I -c {shlex.quote(_HASH_PROTECTED)} {paths}",
            env={"APP_ANSWER": answer[1]} if answer is not None else None,
            timeout_seconds=_HASH_TIMEOUT_SECONDS,
        )
        if not result.ok:
            return None
        try:
            found = json.loads(result.stdout.strip().splitlines()[-1])
        except (IndexError, ValueError):
            return None
        if not isinstance(found, dict) or not isinstance(found.get("files"), dict):
            return None
        if not isinstance(found.get("stamps"), dict):
            return None
        return found["files"], found["stamps"]

    def changes(self, current: tuple[dict[str, str], dict[str, str]], before: Mapping[str, str]) -> tuple[str, ...]:
        """Compare one fingerprint with the prepared tree and the stamps taken before the run.

        Args:
            current: Digests and stamps from :meth:`fingerprint`.
            before: Stamps taken just before the version ran.

        Returns:
            One reason per protected file added, changed or removed; empty when untouched.
        """
        files, stamps = current
        baseline = self._protected_baseline
        changed = {path for path in set(baseline) | set(files) if baseline.get(path) != files.get(path)} | {
            path for path in set(before) | set(stamps) if before.get(path) != stamps.get(path)
        }
        # A folder's times move whenever an entry in it is added or removed;
        # name the folder only when no file in it explains the change.
        changed = sorted(path for path in changed if not any(other.startswith(f"{path}/") for other in changed))
        return tuple(
            f"'{path}' is protected by the repository rules ([paths] protected),"
            " and the version's code changed it while it ran."
            for path in changed
        )

    def close(self) -> None:
        """Delete the parent's copies of the saved states; no box stays open between evaluations."""
        with self._lock:
            if self._folder is not None:
                shutil.rmtree(self._folder, ignore_errors=True)
                self._folder = None
                self._prepared = False

    def release_snapshot(self) -> None:
        """Delete this workspace's snapshots when its owner is done with them.

        A budget error is raised; any other failure is left for the run end.

        Raises:
            BudgetReached: And the other budget errors, from the snapshot delete.
        """
        release = getattr(self._runtime, "release_snapshot", None)
        if release is None:
            return
        with self._lock:
            for key in (self._snapshot_key, self._metric_snapshot_key):
                try:
                    release(key)
                except _BUDGET_ERRORS:
                    raise
                except Exception as error:
                    logger.warning("A snapshot was not deleted (%s); the run end retries it", type(error).__name__)
