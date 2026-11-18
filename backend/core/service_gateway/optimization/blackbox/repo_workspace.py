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
- A candidate box C comes from the run's warm box lease (SKY-805). A new
  box opens from the prepared state, checks the digest and records the box
  user's state outside the repository folder. A warm box from the last
  candidate of its slot first resets: it stops the compose services and
  removes everything in the repository folder but the prepared tree. It
  removes what changed since the open in the box user's HOME caches
  (``~/.cache``), user base (``~/.local``: user site-packages, ``.pth``
  files, ``sitecustomize``) and own temp entries, and that state must match
  the record of the open. Then it starts the services and checks the digest
  again. A failed step closes the box and a new box opens, so a version sees
  the same box state as in a new box. Files outside these folders are not
  reset. The box then lays out the version. Between
  candidates the warm box is parked in the run budget. The run closes the
  lease at its end.
- A metric box M opens for each evaluation from the metric state. It holds
  only the metric files and the runner, never a repository tree.

Both boxes open through the tree sandbox session API (SKY-785), so the
acquire, start, run and release steps match every other tree sandbox box.
"""

from __future__ import annotations

import base64
import heapq
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
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from ....billing.budgets import BudgetError
from ....billing.operation_pricing import UnpricedOperationError
from ....billing.runtime import BudgetRuntime, UsagePendingError
from ....billing.signals import BudgetReached
from ....billing.usage_tags import box_tag
from ....billing.vercel_usage import PACKAGE_REGISTRY_HOSTS
from ....config import settings
from ....sandbox.box_lease import BoxLease
from ....sandbox.errors import is_temporary_provider_error
from ....sandbox.grants import Grant
from ....sandbox.placement import Placement
from ....sandbox.services import stop_command
from ....sandbox.snapshots import snapshot_cache_key
from ....sandbox.trees import BoxSource
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
from .tree_sandbox import BoxSession, TreeSandbox

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
# The metric setup step installs the metric packages here, not into user
# site-packages: the image's python3 is a venv without a user site, and the
# metric runs under ``-I``, which reads no user site. The runner puts this
# folder on ``sys.path`` (SKY-398).
METRIC_SITE_DIR = f"{METRIC_DIR}/site"
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
# The base image's root-owned interpreter. The box PATH puts the box-writable
# /app/.venv/bin first, so a `python3` that the [setup] or [run] code plants
# there could print a clean hash. ``-S`` skips site-packages, which the image
# leaves group-writable; the hash scripts need only the standard library.
BASE_PYTHON = "/usr/local/bin/python3"
# Linux caps one environment string at 128 KiB; a larger stage answer goes
# through a file write and fresh stamps instead.
_ANSWER_ENV_BYTES = 100_000
# Fingerprints every file under the protected paths. It runs with ``-I`` so a
# module the version drops into the checkout cannot shadow ``hashlib``; links
# and special files are recorded rather than followed or opened. Besides the
# contents it records each file's and folder's inode and change times: any
# write, rename or link moves the change time, so a file rewritten and then
# restored while the ``[run]`` entry runs still shows. A path or folder that
# it cannot read stops it with no output, so no protected file is left out.
# A version process can read the output file's name from a root command line
# and create the file first. So the check prints nothing unless its output is
# a pipe, or a regular file that a root start owns, that only root can write,
# with one link, and that its name still opens (model: runner.unsafe_output).
# A check that does not start as root and writes to a file stops too, because
# its own user could rewrite that file.
# On a provider whose root has capabilities, the parent starts it as root. It
# becomes the tree's owner before it reads anything in the tree, and it writes
# nothing there. So no root read follows a link that the version plants, and
# the kernel marks it not dumpable, so the version's processes cannot trace
# it or open its fds. The version can point the tree at a root-owned folder,
# so a check that stays root stops before it reads the tree, and the parent
# gives no score. A root that cannot switch users, as a local Docker root with
# no capability, also stops: it cannot read the box user's private files, so
# its fingerprint could leave files out. The parent runs the check as the box
# user there (``root_has_capabilities`` on the provider class).
_HASH_PROTECTED = r"""
import hashlib, json, os, stat, sys

def unsafe_output():
    try:
        info = os.fstat(1)
    except OSError:
        return True
    if not stat.S_ISREG(info.st_mode):
        return False
    if os.geteuid() != 0 or info.st_uid != 0 or info.st_mode & 0o022 or info.st_nlink != 1:
        return True
    try:
        named = os.lstat(os.readlink("/proc/self/fd/1"))
    except OSError:
        return True
    return (named.st_dev, named.st_ino) != (info.st_dev, info.st_ino)

if unsafe_output():
    sys.exit("the check's output file is not root's own, so another user could rewrite it")

if os.geteuid() == 0:
    owner = os.stat(".")
    if owner.st_uid == 0:
        sys.exit("root owns the tree, so the check has no other user to run as")
    try:
        os.setgroups([])
        os.setgid(owner.st_gid)
        os.setuid(owner.st_uid)
    except OSError as error:
        sys.exit(f"the check cannot leave root: {error}")
    if os.geteuid() == 0:
        sys.exit("the check is still root after the switch")

def fail(error):
    raise error

def digest(path):
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        return "link:" + os.readlink(path)
    if not stat.S_ISREG(info.st_mode):
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
    try:
        info = os.lstat(top)
    except (FileNotFoundError, NotADirectoryError):
        continue
    if stat.S_ISDIR(info.st_mode):
        for folder, folders, files in os.walk(top, onerror=fail):
            folders[:] = [name for name in folders if name != ".git"]
            stamps[os.path.normpath(folder)] = stamp(folder)
            linked = [name for name in folders if stat.S_ISLNK(os.lstat(os.path.join(folder, name)).st_mode)]
            for name in files + linked:
                path = os.path.normpath(os.path.join(folder, name))
                found[path] = digest(path)
                stamps[path] = stamp(path)
    else:
        found[os.path.normpath(top)] = digest(top)
        stamps[os.path.normpath(top)] = stamp(top)
print(json.dumps({"files": found, "stamps": stamps}, sort_keys=True))
"""
# Writes APP_ANSWER to the path in its argument from inside the box, after the
# check. Unlike a Vercel file write, a box command moves the change time
# only of the files it writes, so the stamps from before the run stay valid.
# It runs as the box user, not with the root check: the parent's file write
# makes each call folder as the box user with mode 0755, and a local Docker
# root has no capability to write there. The version controls that user, so
# a process it leaves running can forge or break only this answer. A change in
# the gap between the check and this write is like a change just after the
# write, and the final protected-file check after the run still catches it.
# The temporary file is opened without following a link, and a leftover one is
# removed first, so a planted file blocks only this answer.
_WRITE_ANSWER = r"""
import os, sys

answer = sys.argv[1]
os.makedirs(os.path.dirname(answer), exist_ok=True)
temporary = answer + ".tmp"
try:
    os.unlink(temporary)
except FileNotFoundError:
    pass
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
with os.fdopen(os.open(temporary, flags, 0o666), "w", encoding="utf-8") as handle:
    handle.write(os.environ["APP_ANSWER"])
os.replace(temporary, answer)
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

# The box user's state outside the repository folder that a version can
# write and the next version on a warm box could read: the HOME caches, the
# user base (user site-packages, its ``.pth`` files and ``sitecustomize``)
# and the box user's own entries in the temp folder. The provider's command
# files (``.app-*``) stay. With a time argument it first removes each entry
# whose change time is after it: a version can set a file's other times, but
# not its change time. It prints the time it started and a digest of the
# remaining entries, with each file's size and change time. A new box records
# both before the first version; at a reset the digest must match again, so a
# warm box holds the same state as a new box, or the parent replaces it.
_USER_STATE = r"""
import hashlib, os, stat, sys, time

started = time.time()
since = float(sys.argv[1]) if len(sys.argv) > 1 else None
home = os.path.expanduser("~")
tops = [os.path.join(home, ".cache"), os.path.join(home, ".local")]
temp = os.environ.get("TMPDIR") or "/tmp"
try:
    names = sorted(os.listdir(temp))
except OSError:
    names = []
for name in names:
    path = os.path.join(temp, name)
    try:
        if not name.startswith(".app-") and os.lstat(path).st_uid == os.getuid():
            tops.append(path)
    except OSError:
        pass

def newer(info):
    return since is not None and info.st_ctime > since

def clear(path):
    try:
        info = os.lstat(path)
    except OSError:
        return
    if stat.S_ISDIR(info.st_mode):
        for name in os.listdir(path):
            clear(os.path.join(path, name))
        if newer(os.lstat(path)) and not os.listdir(path):
            os.rmdir(path)
    elif newer(info):
        os.unlink(path)

def walk(path, sha):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    kind = stat.S_IFMT(info.st_mode)
    sha.update(f"{path}\0{kind}\0".encode("utf-8", "surrogateescape"))
    if stat.S_ISDIR(info.st_mode):
        for name in sorted(os.listdir(path)):
            walk(os.path.join(path, name), sha)
    else:
        sha.update(f"{info.st_size}\0{info.st_ctime_ns}\0".encode())

if since is not None:
    for top in tops:
        clear(top)
sha = hashlib.sha256()
for top in tops:
    walk(top, sha)
print(f"{started!r} {sha.hexdigest()}")
"""


def _pick_base_python() -> str:
    """Set ``p`` in a box command to the base interpreter.

    It falls back to ``python3`` from PATH only where no base interpreter
    exists, such as the local test runtime. The box user cannot remove the
    root-owned one in the image, so the fallback never runs there.

    Returns:
        A shell prefix that sets ``p`` and ends in ``&& ``. It keeps one
        ``&&`` chain, so a failed ``cd`` before it still stops the hash.
    """
    return f'p={shlex.quote(BASE_PYTHON)} && {{ [ -x "$p" ] || p=python3; }} && '


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


@dataclass(frozen=True)
class _Warm:
    """What the parent knows of a slot's box from its open: its key, and its box user's state then."""

    # The operation key the box opened with. A reused box keeps it, so its budget tag never changes.
    key: str | None
    # The box clock when the state was read, and the state's digest; ``None`` when it could not be read.
    since: float | None
    state: str | None


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


def _close_session(session: SandboxSession) -> None:
    """Close one box for good; a close error goes to the caller.

    Args:
        session: The box.
    """
    session.close()


class RepoBoxSource:
    """Open a fresh evaluation box for each acquire and close it at release (SKY-718).

    The repository rules accept hosts that the grant rules refuse (a port, one
    label), so this source puts the role's hosts on the box as given, without
    grant resolution, and a run's network stays what its ``[run]`` and
    ``[metric]`` tables say.
    """

    def __init__(self, runtime: SandboxRuntime, spec: SandboxSpec) -> None:
        """Bind the source to a runtime and the spec of its boxes.

        Args:
            runtime: Opens the boxes.
            spec: The box spec; the grant hosts replace its network fields.
        """
        self._runtime = runtime
        self._spec = spec

    def acquire(self, agent_id: str, grants: Sequence[Grant]) -> SandboxSession:
        """Open a new box whose allowed hosts are the grant hosts.

        Args:
            agent_id: The box role; each call gets its own box.
            grants: The role's grants; empty keeps the box offline.

        Returns:
            The open box.
        """
        hosts = tuple(grant.host for grant in grants)
        return self._runtime.open(replace(self._spec, network_disabled=not hosts, allowed_hosts=hosts))

    def release(self, box: SandboxSession, healthy: bool) -> None:
        """Close the box; a close error goes to the caller.

        Args:
            box: A box that :meth:`acquire` gave.
            healthy: Not used: a closed box is never reused.
        """
        box.close()


class _SlotSource:
    """Give one candidate slot's box from the run's lease, and free the slot at the box's last release (SKY-805).

    A lease key is the run and the slot, like a Cloudflare sandbox id: each
    parallel candidate gets its own warm box, and no box crosses runs. A
    warm box is parked in the run budget from its healthy release until the
    slot's next acquire, so the budget sees that it waits (SKY-647).
    """

    def __init__(
        self,
        lease: BoxLease[SandboxSession],
        key: str,
        free: Callable[[], None],
        park: Callable[[str], None],
        unpark: Callable[[str], None],
    ) -> None:
        """Bind the slot to the run's lease.

        Args:
            lease: The run's candidate box lease.
            key: The run and the slot.
            free: Gives the slot back.
            park: Parks the slot's warm box in the run budget.
            unpark: Ends the park of the slot's warm box.
        """
        self._lease = lease
        self.key = key
        self._free = free
        self._park = park
        self._unpark = unpark
        self._freed = False
        self._lock = threading.Lock()

    def acquire(self, agent_id: str, grants: Sequence[Grant]) -> SandboxSession:
        """End the park of the slot's warm box, then return it, or open a new one.

        Args:
            agent_id: The lease key.
            grants: The grants the lease compares; the box opens with the ``[run]`` hosts.

        Returns:
            The box.
        """
        self._unpark(self.key)
        return self._lease.acquire(agent_id, grants)

    def release(self, box: SandboxSession, healthy: bool) -> None:
        """Keep a healthy box warm and parked for the slot, or close it; then free the slot.

        Args:
            box: The slot's box.
            healthy: False when the box failed, so the slot's next candidate gets a new box.
        """
        try:
            self._lease.release(box, healthy)
            if healthy and self._lease.keeps_warm:
                self._park(self.key)
        finally:
            self.free()

    def free(self) -> None:
        """Give the slot back once."""
        with self._lock:
            if self._freed:
                return
            self._freed = True
        self._free()


def _release_failed(source: BoxSource[SandboxSession], session: SandboxSession, what: str) -> None:
    """Give a failed box that no session holds back to its source as unhealthy, and log only a failure's type.

    The boxes this module opens hold a session and call
    :meth:`BoxSession.release_failed` directly, so the session marks itself released.

    Args:
        source: The source that gave the box.
        session: The box.
        what: Names the box in the log line.

    Raises:
        BudgetReached: And the other budget errors of the close, in place of the box's own failure.
    """
    BoxSession(source, session).release_failed(what)


class CandidateBox:
    """One evaluation's candidate box C: the prepared repository with the version laid out."""

    def __init__(self, workspace: RepoWorkspace, session: SandboxSession, root: str, box: BoxSession) -> None:
        """Bind an open, laid-out box to the workspace that checks its protected files.

        Args:
            workspace: The workspace that opened the box.
            session: The box, for the protected-file checks and the relay answers.
            root: The box's working directory.
            box: The tree sandbox session that holds ``session``; it runs the
                ``[run]`` entry and gives the box back at close.
        """
        self._workspace = workspace
        self.box = box
        self.session = session
        self.root = root
        self.path = WORK_DIR
        self._stamps: dict[str, str] | None = None
        # Set before each answer file write and cleared once the files are stamped
        # again: a write, even one that raised after the server did it, moves every ctime.
        self._unstamped = False
        self._lock = threading.Lock()

    @property
    def unstamped(self) -> bool:
        """Whether an answer file write may have moved the change times since the last stamp."""
        with self._lock:
            return self._unstamped

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

        A root check runs first, then a box-user command writes the answer,
        also after a change, so the version ends now and not at its timeout.
        Box commands keep the stamps taken before the run valid. An answer
        too large for the environment is written with a file write, and the
        files are stamped again after it.

        Args:
            path: Where the waiting client reads the answer, relative to the box.
            text: The answer.

        Returns:
            Like :meth:`protected_changes`, or ``None`` when the files could
            not be checked, or when nothing changed and the answer could not
            be written.

        Raises:
            Exception: A box call error of the check or the write, raw, when
                no change was found before it.
        """
        workspace = self._workspace
        with self._lock:
            before = self._stamps
            if not workspace.watched:
                self._unstamped = True
                return () if _deliver(self.session, path, text) else None
            if before is not None and len(text.encode("utf-8")) <= _ANSWER_ENV_BYTES:
                current = workspace.fingerprint(self.session, WORK_DIR)
                if current is None:
                    self._unstamped = True
                    _deliver(self.session, path, text)
                    return None
                # After an unstamped file write the stamps are stale, so only the
                # contents can name a change.
                changes = workspace.changes(current, current[1] if self._unstamped else before)
                try:
                    written = _write_answer(self.session, path, text)
                except Exception:
                    # A found change must still reach the rule judge.
                    if changes:
                        return changes
                    raise
                if not written:
                    # The answer stays unchecked, but the call ends now, not at its timeout.
                    self._unstamped = True
                    try:
                        _deliver(self.session, path, text)
                    except Exception:
                        if changes:
                            return changes
                        raise
                return changes if changes or written else None
            current = workspace.fingerprint(self.session, WORK_DIR) if before is not None else None
            changes = (
                workspace.changes(current, current[1] if self._unstamped else before)
                if current is not None and before is not None
                else ()
            )
            self._unstamped = True
            try:
                delivered = _deliver(self.session, path, text)
            except Exception:
                # The write may have moved the change times, so only this check can name the change.
                if changes:
                    return changes
                raise
            if not delivered or current is None or before is None:
                return changes or None
            after = workspace.fingerprint(self.session, WORK_DIR)
            if after is None:
                return changes or None
            self._stamps = after[1]
            self._unstamped = False
            return changes

    def close(self, healthy: bool = True) -> None:
        """Give the box back to its source once; the next candidate resets a healthy box.

        Args:
            healthy: False when the box failed, so the lease closes it and the next candidate gets a new box.
        """
        self.box.release(healthy=healthy)


class MetricBox:
    """One evaluation's metric box M: the metric state, the metric files and the runner, no repository."""

    def __init__(
        self, workspace: RepoWorkspace, session: SandboxSession, root: str, key: str | None, box: BoxSession
    ) -> None:
        """Bind an open metric box.

        Args:
            workspace: The workspace that opened the box.
            session: The box, for the metric files, the runner and its output.
            root: The box's working directory.
            key: The box's operation key, or ``None`` when the spec has none.
            box: The tree sandbox session that holds ``session``; it runs the
                metric and gives the box back at close.
        """
        self._workspace = workspace
        self.box = box
        self.session = session
        self.root = root
        self.key = key
        self.repo_path = f"{root}/{METRIC_REPO_DIR}"
        self.site_path = f"{root}/{METRIC_SITE_DIR}"

    def install_metric_files(self) -> None:
        """Put the metric files into ``METRIC_REPO_DIR`` at their repository paths.

        Call it only after the candidate box went back to the lease (SKY-398, SKY-805).

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
        """Give the box back to its source once."""
        self.box.release(healthy=True)


def _write_answer(session: SandboxSession, path: str, text: str) -> bool:
    """Write one relay answer with a box-user command, which moves no protected file's change time.

    Args:
        session: The box the answer goes to.
        path: Where the answer goes, relative to the box.
        text: The answer.

    Returns:
        Whether the write succeeded.
    """
    result = session.run(
        f'{_pick_base_python()}"$p" -I -S -c {shlex.quote(_WRITE_ANSWER)} "$PWD"/{shlex.quote(path)}',
        env={"APP_ANSWER": text},
        timeout_seconds=_HASH_TIMEOUT_SECONDS,
    )
    return result.ok


def _deliver(session: SandboxSession, path: str, text: str) -> bool:
    """Write one relay answer with a file write.

    Args:
        session: The box the answer goes to.
        path: Where the answer goes, relative to the box.
        text: The answer.

    Returns:
        Whether the write succeeded.

    Raises:
        Exception: A temporary provider error (429 or 5xx), raw, so the judge can retry the score.
    """
    try:
        session.write_files({path: text})
    except Exception as error:
        if is_temporary_provider_error(error):
            raise
        # The run log shows this logger, and the message is provider text.
        logger.warning("A relay answer could not reach the box: %s", type(error).__name__)
        return False
    return True


class RepoWorkspace:
    """Prepare a repository once, then give a candidate box and a new metric box for each evaluation.

    A candidate slot keeps its warm box for the run's next version, after a
    reset. No metric box outlives its evaluation, so nothing one version's
    code writes or starts reaches the metric.
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
        candidate_cap: int | None = None,
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
            candidate_cap: The most warm candidate boxes the run keeps; ``None``
                takes ``REPO_CANDIDATE_VERCEL_CAP``.
        """
        self._runtime = runtime
        self._spec = spec
        self._archive = archive
        self._editable = tuple(editable_paths)
        self._readonly = tuple(readonly_paths)
        self._protected = tuple(protected_paths)
        self._hidden = tuple(posixpath.normpath(path) for path in hidden_paths)
        self._run_grants = tuple(Grant(host) for host in run_hosts)
        self._run_vcpus = run_vcpus
        self._metric_grants = tuple(Grant(host) for host in metric_hosts)
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
        self._closed = False
        self._opened = 0
        self._lock = threading.Lock()
        cap = settings.repo_candidate_vercel_cap if candidate_cap is None else candidate_cap
        # One lease for each workspace, so a warm box never serves another run (SKY-805).
        self._run_key = spec.operation_key or unique
        self._candidates: BoxLease[SandboxSession] = BoxLease(
            self._open_leased,
            _close_session,
            # One slot for each warm box; the opener ignores the placement, so a box never spills.
            vercel_cap=cap,
            starts_per_minute=settings.repo_candidate_starts_per_minute,
        )
        self._free_slots = list(range(cap))
        self._slots_free = threading.Condition()
        # The lease opens a box inside acquire, in the caller's thread, so the
        # opener reads the asking candidate's spec here and marks the box new.
        self._pending = threading.local()
        self._warm: dict[str, _Warm] = {}
        # Its own lock: ``_lock`` is held through the whole setup.
        self._park_lock = threading.Lock()
        self._budget: BudgetRuntime | None = None
        # The budget tag of each slot's parked warm box.
        self._parked: dict[str, str] = {}

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

    def park_in(self, budget: BudgetRuntime) -> None:
        """Park each warm candidate box in this run budget while it waits for the slot's next candidate.

        A warm box keeps its hold between evaluations, so without the park the
        budget would wait for it to end before it stops the run (SKY-647).

        Args:
            budget: The run budget that holds the candidate boxes.
        """
        with self._park_lock:
            self._budget = budget

    def _park(self, slot: str) -> None:
        """Park the slot's warm box under the tag of the key it opened with.

        Args:
            slot: The slot's lease key.
        """
        with self._park_lock:
            warm = self._warm.get(slot)
            # After the close, nothing ends a park, and the lease closes the box.
            if self._closed or self._budget is None or warm is None or warm.key is None or slot in self._parked:
                return
            tag = box_tag(warm.key)
            self._budget.park(tag)
            self._parked[slot] = tag

    def _unpark(self, slot: str) -> None:
        """End the park of the slot's warm box, if it has one.

        Args:
            slot: The slot's lease key.
        """
        with self._park_lock:
            tag = self._parked.pop(slot, None)
            if tag is not None and self._budget is not None:
                self._budget.unpark(tag)

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
        # The registries, also when the repository has no [setup] command: the
        # spec's own hosts are then empty, and the install would run offline.
        spec = replace(
            self._spec,
            operation_key=self._next_key("metric-setup"),
            services=(),
            snapshot_id=None,
            private=True,
            vcpus=self._metric_vcpus or self._spec.vcpus,
            network_disabled=False,
            allowed_hosts=PACKAGE_REGISTRY_HOSTS,
        )
        session = self._runtime.open(spec)
        try:
            session.write_files({f"{METRIC_DIR}/requirements.txt": "\n".join(self._requirements) + "\n"})
            result = session.run(
                f"python3 -m pip install --target {METRIC_SITE_DIR} --no-input --disable-pip-version-check"
                f" -r {METRIC_DIR}/requirements.txt"
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

    def _open_box(
        self,
        spec: SandboxSpec,
        state: _State | None,
        role: str,
        grants: tuple[Grant, ...],
        slot: _SlotSource | None = None,
    ) -> BoxSession:
        """Open one box from a saved state through the tree sandbox session API (SKY-718, SKY-785).

        Args:
            spec: The box's spec without its state.
            state: The saved state; ``None`` opens the plain image.
            role: ``candidate`` or ``metric``, or the lease key of a candidate slot.
            grants: The role's network grants; empty keeps the box offline.
            slot: The candidate slot whose warm box the run's lease gives;
                ``None`` opens a new box that closes at its release.

        Returns:
            The session that holds the box, with a tar state unpacked in a new box.

        Raises:
            RepoSetupError: When a tar state cannot be unpacked.
            _SnapshotLostError: When a new box cannot open from its snapshot.
        """
        if state is not None and state.snapshot_id is not None:
            spec = replace(spec, snapshot_id=state.snapshot_id)
        elif state is not None and state.archive is not None:
            spec = replace(spec, snapshot_archive=str(state.archive))
        archive = state.archive if state is not None else None
        start: Callable[[SandboxSession], None] | None = None
        if archive is not None:

            def unpack(session: SandboxSession) -> None:
                """Unpack the tar state into the new box.

                Args:
                    session: The box.

                Raises:
                    RepoSetupError: When the state cannot be unpacked.
                """
                parts = []
                for index, chunk in enumerate(archive_chunks(archive)):
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

            start = unpack
        source: BoxSource[SandboxSession] = RepoBoxSource(self._runtime, spec)
        if slot is not None:
            source = slot
            self._pending.spec = spec
            self._pending.fresh = False
            if start is not None:
                unpack_new = start

                def unpack_if_new(session: SandboxSession) -> None:
                    """Unpack the tar state only into a box that the lease opened for this call.

                    Args:
                        session: The box.
                    """
                    if self._pending.fresh:
                        unpack_new(session)

                start = unpack_if_new

        try:
            return TreeSandbox.open_session(source, role, grants, start=start, what="A box from a tar state")
        except _BUDGET_ERRORS:
            raise
        except Exception as error:
            # A saved state is a snapshot or a tar file, never both, so an error here with a snapshot is the open's.
            # A slot's error is the open's only when the lease opened a new box in this call; a kept reaper
            # error, a closed lease or a warm box's error leaves the snapshots in place.
            if spec.snapshot_id is None or (slot is not None and not self._pending.fresh):
                raise
            # Only the type: the provider's text can name the snapshot.
            logger.warning("A box did not open from its snapshot (%s)", type(error).__name__)
            self._drop_snapshots()
            raise _SnapshotLostError("A box could not open from the saved snapshot.") from None

    def _box_spec(self, kind: str, vcpus: int | None, *, services: bool) -> SandboxSpec:
        """Return the spec of one evaluation box; the box source sets its network from the grants.

        Args:
            kind: ``candidate`` or ``metric``.
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
            RuntimeError: When the workspace is closed.
            _SnapshotLostError: When a box from a snapshot did not open or did not hold the tree.
        """
        with self._lock:
            # Before prepare: the close forgets the saved states, so a late call would pay for setup again.
            if self._closed:
                raise RuntimeError("The box lease is closed.")
        self.prepare()
        # Each candidate takes a new key, but only a new box uses it: a warm box keeps the key it opened with.
        spec = self._box_spec("candidate", self._run_vcpus, services=True)
        state = self._repo_state
        slot = self._take_slot()
        try:
            box, new = self._candidate_box(slot, spec, state)
        except BaseException:
            slot.free()
            raise
        session = box.box
        try:
            if new and self._digest_prepared(session) != self._prepared_digest:
                self._drop_snapshots()
                lost = state is not None and state.snapshot_id is not None
                raise (_SnapshotLostError if lost else RepoSetupError)(
                    "The candidate box does not hold the prepared repository."
                )
            if new:
                # Before the services start, as at a reset, so the next reset clears what they write.
                since, state = self._user_state(session)
                self._warm[slot.key] = _Warm(spec.operation_key, since, state)
                self._start_services(session)
            session.write_files({PATCH_FILE: patch})
            applied = session.run(
                f"set -e; cp -a {PREPARED_DIR} {WORK_DIR}; cd {WORK_DIR};"
                f" if [ -s ../version.patch ]; then {GIT} apply --binary --whitespace=nowarn ../version.patch; fi",
                timeout_seconds=_SETUP_TIMEOUT_SECONDS,
            )
            if applied.ok:
                return CandidateBox(self, session, box_root(session), box)
        except BaseException:
            box.release_failed("The candidate box")
            raise
        # Outside the try: a budget error of this release must not release the box a second time.
        box.release_failed("The candidate box")
        return Checkout(path=None, problems=(f"The change does not apply cleanly: {_tail(applied)}",))

    def _take_slot(self) -> _SlotSource:
        """Take the lowest free candidate slot, and wait while the run holds a box in every slot.

        Returns:
            The slot's box source; its last release frees the slot.
        """
        with self._slots_free:
            while not self._free_slots:
                # At the cap a run waits for its own box, and never spills to another provider.
                self._slots_free.wait()
            number = heapq.heappop(self._free_slots)

        def free() -> None:
            """Give the slot back and wake one waiting candidate."""
            with self._slots_free:
                heapq.heappush(self._free_slots, number)
                self._slots_free.notify()

        return _SlotSource(self._candidates, f"{self._run_key}:candidate-{number}", free, self._park, self._unpark)

    def _candidate_box(self, slot: _SlotSource, spec: SandboxSpec, state: _State | None) -> tuple[BoxSession, bool]:
        """Get the slot's warm box and reset it, or a new box when the slot has none or the reset fails.

        Args:
            slot: The candidate slot.
            spec: The new box's spec without its state.
            state: The prepared state a new box opens from.

        Returns:
            The session that holds the box, and True when the lease opened it for this call.

        Raises:
            RepoSetupError: When a tar state cannot be unpacked.
            _SnapshotLostError: When a new box cannot open from its snapshot.
        """
        while True:
            box = self._open_box(spec, state, slot.key, (), slot=slot)
            if self._pending.fresh:
                return box, True
            try:
                if self._reset(box.box, slot.key):
                    return box, False
            except _BUDGET_ERRORS:
                _release_failed(self._candidates, box.box, "A warm candidate box")
                raise
            except Exception as error:
                # Only the type: the provider's text can name the box.
                logger.warning("A warm candidate box did not reset (%s)", type(error).__name__)
            except BaseException:
                # The lease must not keep the box held: the slot's next acquire would wait for it forever.
                _release_failed(self._candidates, box.box, "A warm candidate box")
                raise
            # The unhealthy release drops the box, so the next acquire opens a new one.
            _release_failed(self._candidates, box.box, "A warm candidate box")

    def _reset(self, session: SandboxSession, slot: str) -> bool:
        """Clear the last candidate from a warm box, restart its services and check the prepared tree.

        The reset keeps only the prepared tree of the repository folder, and
        brings the box user's caches, user base and temp files back to the
        state the box had when it opened (see ``_USER_STATE``). So a version
        never reads or changes what an earlier version left.

        Args:
            session: The warm box.
            slot: The slot's lease key.

        Returns:
            True when the box is ready for the next version.

        Raises:
            RepoSetupError: When the services cannot start again.
        """
        stops = "".join(f"{stop_command(service)}; " for service in self._spec.services)
        keep = posixpath.basename(PREPARED_DIR)
        cleared = session.run(
            f"{stops}find {REPO_DIR} -mindepth 1 -maxdepth 1 ! -name {keep} -exec rm -rf {{}} +",
            timeout_seconds=_SETUP_TIMEOUT_SECONDS,
            # The supervisor runs as root, so a call folder holds root's files.
            sudo=True,
        )
        if not cleared.ok:
            return False
        warm = self._warm.get(slot)
        if warm is None or warm.since is None:
            return False
        if self._user_state(session, warm.since)[1] != warm.state:
            return False
        self._start_services(session)
        return self._digest_prepared(session) == self._prepared_digest

    def _user_state(self, session: SandboxSession, since: float | None = None) -> tuple[float | None, str | None]:
        """Read the box user's state outside the repository folder, after removing what changed since ``since``.

        It runs as the box user: a local Docker root has no capability to
        remove that user's files.

        Args:
            session: The candidate box.
            since: The box clock when the box opened; ``None`` removes nothing.

        Returns:
            The box clock when the read started and the state's digest, or
            ``(None, None)`` when the command failed or printed something else.
        """
        argument = "" if since is None else f" {since!r}"
        result = session.run(
            f'{_pick_base_python()}"$p" -I -S -c {shlex.quote(_USER_STATE)}{argument}',
            timeout_seconds=_HASH_TIMEOUT_SECONDS,
        )
        lines = result.stdout.strip().splitlines() if result.ok else []
        parts = lines[-1].split() if lines else []
        if len(parts) != 2 or len(parts[1]) != 64:
            return None, None
        try:
            return float(parts[0]), parts[1]
        except ValueError:
            return None, None

    def _open_leased(self, placement: Placement, grants: Sequence[Grant]) -> SandboxSession:
        """Open a new candidate box for the lease with the spec of the candidate that asked for it.

        Args:
            placement: Not used: the box always opens on Vercel. A box that the
                reaper dropped counts until its close ends, so the lease can then
                name the spill provider, and Vercel briefly holds more than the cap.
            grants: Not used: the box gets the ``[run]`` hosts as given.

        Returns:
            The open box.
        """
        self._pending.fresh = True
        return RepoBoxSource(self._runtime, self._pending.spec).acquire("candidate", self._run_grants)

    def open_metric(self) -> MetricBox:
        """Open a metric box from the metric state, without any repository file.

        Returns:
            The open box; the caller installs the metric files once the candidate box went back to the lease.

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
        spec = self._box_spec("metric", self._metric_vcpus, services=False)
        state = self._metric_state
        box = self._open_box(spec, state, "metric", self._metric_grants)
        session = box.box
        try:
            if state is not None:
                ready = session.run(f"test -f {_METRIC_READY}", timeout_seconds=60)
                if not ready.ok:
                    self._drop_snapshots()
                    lost = state.snapshot_id is not None
                    raise (_SnapshotLostError if lost else RepoSetupError)(
                        "The metric box does not hold the metric packages."
                    )
            return MetricBox(self, session, box_root(session), spec.operation_key, box)
        except BaseException:
            box.release_failed("The metric box")
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
            f'cd {PREPARED_DIR} && {_pick_base_python()}"$p" -I -S -c {shlex.quote(_HASH_PREPARED)}',
            timeout_seconds=_HASH_TIMEOUT_SECONDS,
        )
        lines = result.stdout.strip().splitlines() if result.ok else []
        digest = lines[-1] if lines else ""
        return digest if len(digest) == 64 and all(char in "0123456789abcdef" for char in digest) else None

    def fingerprint(self, session: SandboxSession, directory: str) -> tuple[dict[str, str], dict[str, str]] | None:
        """Hash every file under the protected paths of one tree in a box.

        Args:
            session: The box holding the tree.
            directory: The tree's folder, relative to the box.

        Returns:
            Repository-relative path to digest, and path to inode and change
            times for every file and folder, or ``None`` when the hashing
            command failed or printed something unreadable.
        """
        paths = " ".join(shlex.quote(path) for path in self._protected)
        result = session.run(
            f'cd {shlex.quote(directory)} && {_pick_base_python()}"$p" -I -S -c {shlex.quote(_HASH_PROTECTED)} {paths}',
            timeout_seconds=_HASH_TIMEOUT_SECONDS,
            # A box-user command's output files belong to the box user, so a
            # process that the version leaves running could replace a clean
            # result there. The script itself drops root before it reads the tree.
            # Only a provider whose root has no capability, local Docker, runs
            # it as the box user: there the output is a pipe. An unknown
            # session keeps root.
            sudo=getattr(session, "root_has_capabilities", True),
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
        """Delete the parent's copies of the saved states, then close the warm candidate boxes.

        A box that a candidate still holds closes at its release.

        Raises:
            Exception: The first close error of a warm box, raw, after every warm box had its close.
        """
        with self._lock:
            self._closed = True
            if self._folder is not None:
                shutil.rmtree(self._folder, ignore_errors=True)
                self._folder = None
                self._prepared = False
        # ``_closed`` is set first, so no release parks a box after this list.
        with self._park_lock:
            parked = list(self._parked)
        for slot in parked:
            self._unpark(slot)
        self._candidates.close()

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
