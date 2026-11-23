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
  packages of ``METRIC_REQUIREMENTS``, then runs the ``[metric] setup``
  command in ``METRIC_REPO_DIR`` with the metric files there (SKY-740). It
  removes the metric files again and saves a second state that holds no
  repository code and no metric source; the build output stays in it. Its
  network is open only to the package registries, and it gets no secret.
- A candidate box C opens for each evaluation from the prepared state,
  checks the digest and lays out the version. The parent ends it after the
  ``[run]`` entry; closing the box is the only cleanup. No candidate box
  serves a second version (SKY-805): a reset of a used box would have to
  clear every state a version can leave, so each version gets a new box
  from the saved state, and the setup does not run again.
- A metric box M opens for each evaluation from the metric state. It holds
  only the metric files and the runner, never a repository tree.

Both boxes open through the tree sandbox session API (SKY-785), so the
acquire, start, run and release steps match every other tree sandbox box.
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
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from ....billing.budgets import BudgetError
from ....billing.operation_pricing import UnpricedOperationError
from ....billing.runtime import UsagePendingError
from ....billing.signals import BudgetReached
from ....billing.vercel_usage import PACKAGE_REGISTRY_HOSTS
from ....sandbox.errors import is_temporary_provider_error
from ....sandbox.grants import Grant
from ....sandbox.snapshots import snapshot_cache_key
from ....sandbox.trees import BoxSource
from .repo_tree import (
    MAX_PATCH_BYTES,
    SAFE_GIT,
    _member_path,
    archive_chunks,
    archive_links,
    drop_paths,
    inside,
    link_edits,
    patch_paths,
    patch_violations,
    strip_archive,
)
from .sandbox_contract import CommandResult, OutputSink, SandboxRuntime, SandboxSession, SandboxSpec, UserImage
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
# The paths that the version touches, as one shell-quoted line (SKY-906).
PATHS_FILE = f"{REPO_DIR}/version.paths"
# The exit code of the candidate box's link check, which no ``git apply`` or ``cp`` failure gives.
_LINK_EXIT = 3
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
# command in $HOME or the XDG folder must not run in a box git call, and a
# folder in the tree shaped as a bare repository is not a repository (SKY-812).
GIT = (
    "env HOME=/nonexistent XDG_CONFIG_HOME=/nonexistent GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null"
    f" GIT_CONFIG_SYSTEM=/dev/null GIT_ATTR_NOSYSTEM=1 git {shlex.join(SAFE_GIT)} -c core.attributesFile=/dev/null"
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


def _pick_base_python(user_image: bool = False, keep: tuple[str, ...] = ()) -> str:
    """Set ``p`` in a box command to the base interpreter.

    In Skynet's image it falls back to ``python3`` from PATH only where no
    base interpreter exists, such as the local test runtime. The box user
    cannot remove the root-owned one in the image, so the fallback never runs
    there. A user image gives no such promise, so its box never falls back:
    its image check proved the base interpreter root-owned (SKY-737). The
    image's environment can name loader paths, such as ``LD_LIBRARY_PATH``,
    so a user image box starts the interpreter with an empty environment, as
    the image check ran it. That environment holds only ``OPENSSL_CONF=/dev/null``:
    ``hashlib`` loads OpenSSL, whose image config can name an engine library in
    a folder that the box user writes (SKY-737).

    Args:
        user_image: Whether the box runs a user ``[run]`` image.
        keep: Names of the environment variables that the script reads, kept in a user image box.

    Returns:
        A shell prefix that sets ``p`` and ends in ``&& ``, or in a user image
        box in ``/usr/bin/env -i``, ``OPENSSL_CONF`` and the kept variables, so ``"$p"`` follows
        it. It keeps one ``&&`` chain, so a failed ``cd`` before it still stops the hash.
    """
    if user_image:
        kept = "".join(f' {name}="${name}"' for name in keep)
        return f"p={shlex.quote(BASE_PYTHON)} && /usr/bin/env -i OPENSSL_CONF=/dev/null{kept} "
    return f'p={shlex.quote(BASE_PYTHON)} && {{ [ -x "$p" ] || p=python3; }} && '


def absolute_cd(path: str, root: str) -> str:
    """Make a ``cd`` to a box path that the image's ``CDPATH`` and ``PWD`` cannot send elsewhere.

    ``CDPATH`` applies only to a relative target, so a relative path gets
    the box's working directory in front. That folder comes from
    :func:`physical_root`, never from ``$PWD``: bash takes ``PWD`` from the
    environment when it names the working folder, even through a link that
    the box user can later point at a decoy (SKY-737).

    Args:
        path: The folder, absolute or relative to the box's working directory.
        root: The box's working directory from :func:`physical_root`.

    Returns:
        The ``cd`` command.
    """
    return f"cd {shlex.quote(path if path.startswith('/') else f'{root}/{path}')}"


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


def physical_root(session: SandboxSession, user_image: bool) -> str:
    """Return a box's working directory as the kernel names it, with no link in it.

    The trusted interpreter asks ``getcwd``, so neither an image ``PWD`` nor
    a link that the box user owns can change the answer (SKY-737).

    Args:
        session: The open box.
        user_image: Whether the box runs a user ``[run]`` image.

    Returns:
        The absolute directory.

    Raises:
        RepoSetupError: When the box cannot report it.
    """
    located = session.run(
        f'{_pick_base_python(user_image)}"$p" -I -S -c {shlex.quote("import os; print(os.getcwd())")}',
        timeout_seconds=30,
    )
    root = located.stdout.strip().splitlines()[-1] if located.ok and located.stdout.strip() else ""
    if not root.startswith("/"):
        raise RepoSetupError("The box could not report its working directory.")
    return root


def _install_metric_files(session: SandboxSession, files: str | None, python: str = "python3") -> None:
    """Put the packed metric files into ``METRIC_REPO_DIR`` at their repository paths.

    Args:
        session: The metric box or the metric setup box.
        files: The base64 tar.gz from :func:`pack_metric_files`; ``None`` only makes the folder.
        python: The metric box's trusted interpreter.

    Raises:
        RepoSetupError: When the files cannot be installed.
    """
    made = session.run(f"mkdir -p {METRIC_REPO_DIR}", timeout_seconds=60)
    if not made.ok:
        raise RepoSetupError(f"The metric folder could not be made: {_tail(made)}")
    if files is None:
        return
    source = f"{METRIC_DIR}/metric-{uuid.uuid4().hex}.b64"
    session.write_files({source: files})
    result = session.run(
        f"cd {METRIC_REPO_DIR} && {shlex.quote(python)} -I -c {shlex.quote(_INSTALL_METRIC)} ../{posixpath.basename(source)}",
        timeout_seconds=_SETUP_TIMEOUT_SECONDS,
    )
    if not result.ok:
        raise RepoSetupError(f"The metric files could not be installed: {_tail(result)}")


def _remove_metric_files(session: SandboxSession, files: str) -> None:
    """Remove the packed metric files from ``METRIC_REPO_DIR`` and keep the build output.

    The saved metric state must hold no metric source, so no metric box has it
    before the candidate box ends (SKY-398); ``install_metric_files`` puts it
    back later (SKY-740).

    Args:
        session: The metric setup box, after the build.
        files: The base64 tar.gz from :func:`pack_metric_files`.

    Raises:
        RepoSetupError: When the files cannot be removed.
    """
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(files)), mode="r:gz") as packed:
        names = [member.name for member in packed.getmembers() if member.isreg()]
    listing = f"{METRIC_DIR}/remove-{uuid.uuid4().hex}.txt"
    session.write_files({listing: "".join(f"{name}\0" for name in names)})
    result = session.run(
        f"cd {METRIC_REPO_DIR} && xargs -0 rm -f -- < ../{posixpath.basename(listing)}"
        f" && rm -f ../{posixpath.basename(listing)}",
        timeout_seconds=_SETUP_TIMEOUT_SECONDS,
    )
    if not result.ok:
        raise RepoSetupError(f"The metric files could not be removed after the metric setup: {_tail(result)}")


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
            before = self._workspace.fingerprint(self.session, WORK_DIR, self.root)
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
            current = self._workspace.fingerprint(self.session, WORK_DIR, self.root)
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
                current = workspace.fingerprint(self.session, WORK_DIR, self.root)
                if current is None:
                    self._unstamped = True
                    _deliver(self.session, path, text)
                    return None
                # After an unstamped file write the stamps are stale, so only the
                # contents can name a change.
                changes = workspace.changes(current, current[1] if self._unstamped else before)
                try:
                    written = _write_answer(self.session, self.root, path, text, user_image=workspace.user_run_image)
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
            current = workspace.fingerprint(self.session, WORK_DIR, self.root) if before is not None else None
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
            after = workspace.fingerprint(self.session, WORK_DIR, self.root)
            if after is None:
                return changes or None
            self._stamps = after[1]
            self._unstamped = False
            return changes

    def close(self) -> None:
        """Give the box back to its source once; every process the version started ends with it."""
        self.box.release(healthy=True)


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

        Call it only after the candidate box ended (SKY-398).

        Raises:
            RepoSetupError: When the files cannot be installed.
        """
        # The extract overwrites the metric files and keeps the files a
        # [metric] setup build wrote next to them (SKY-740).
        _install_metric_files(self.session, self._workspace.metric_files, self._workspace.metric_python)

    def close(self) -> None:
        """Give the box back to its source once."""
        self.box.release(healthy=True)


def _write_answer(session: SandboxSession, root: str, path: str, text: str, *, user_image: bool = False) -> bool:
    """Write one relay answer with a box-user command, which moves no protected file's change time.

    Args:
        session: The box the answer goes to.
        root: The box's working directory from :func:`physical_root`.
        path: Where the answer goes, relative to the box.
        text: The answer.
        user_image: Whether the box runs a user ``[run]`` image.

    Returns:
        Whether the write succeeded.
    """
    result = session.run(
        f'{_pick_base_python(user_image, ("APP_ANSWER",))}"$p" -I -S -c {shlex.quote(_WRITE_ANSWER)}'
        f" {shlex.quote(f'{root}/{path}')}",
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


def _as_user_image(image: str | UserImage | None) -> UserImage | None:
    """Return a box's user image, with a plain reference as an image that needs no registry access.

    Args:
        image: The user image, its reference, or ``None`` (or empty) for none.

    Returns:
        The user image, or ``None`` for a box on the runtime's default image.
    """
    if isinstance(image, str):
        return UserImage(image) if image else None
    return image


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
        metric_setup: str | None = None,
        run_image: str | UserImage | None = None,
        metric_image: str | UserImage | None = None,
        metric_python: str | None = None,
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
            metric_setup: The ``[metric] setup`` command, run once in the metric
                setup box with the metric files; ``None`` builds nothing.
            run_image: The checked ``[run]`` user image of the setup and candidate
                boxes, with the registry access each box start needs (SKY-891);
                a plain reference has none. ``None`` keeps ``spec.image`` (SKY-737).
            metric_image: The checked ``[metric]`` user image of the metric boxes,
                in the same form; ``None`` keeps ``spec.image``.
            metric_python: The trusted interpreter that ``ascent check --image
                --role metric`` reported for ``metric_image``; ``None`` uses ``python3``.
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
        self._metric_setup = (metric_setup or "").strip() or None
        # Each box start passes its own role's user image to the runtime, which
        # pulls or copies it; a box with none keeps ``spec.image``.
        self._run_user_image = _as_user_image(run_image)
        self._metric_user_image = _as_user_image(metric_image)
        self._run_image = self._run_user_image.reference if self._run_user_image else spec.image
        self.user_run_image = self._run_user_image is not None
        self._metric_image = self._metric_user_image.reference if self._metric_user_image else spec.image
        self.metric_python = metric_python or "python3"
        self._secrets = dict(secrets)
        self._failure: BaseException | None = None
        self._setup_log = ""
        self._prepared = False
        self._skipped: tuple[str, ...] = ()
        # Held in the parent: the protected files after setup, and the
        # prepared tree's digest, which every candidate box checks.
        self._protected_baseline: dict[str, str] = {}
        self._prepared_digest: str | None = None
        self._links: frozenset[str] | None = None
        self.metric_files: str | None = None
        self._requirements: tuple[str, ...] = ()
        self._repo_state: _State | None = None
        self._metric_state: _State | None = None
        self._folder: Path | None = None
        # Each workspace has its own keys, so no other box can reuse its snapshots.
        unique = uuid.uuid4().hex
        self._snapshot_key = snapshot_cache_key(
            image_digest=self._run_image or "",
            setup_command=self._setup or "",
            install_command="",
            lock_files={"workspace": unique},
        )
        self._metric_snapshot_key = snapshot_cache_key(
            image_digest=self._metric_image or "",
            setup_command="",
            install_command="",
            lock_files={"metric": unique},
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

    def prepare(self, *, on_output: OutputSink | None = None, skip: tuple[str, ...] = ()) -> None:
        """Run the setup box and the metric setup step once, and keep both states.

        A failure is remembered, so no later call opens a box again.

        Args:
            on_output: Receives setup output as it streams.
            skip: Roles (``run``, ``metric``) whose setup box does not open. A setup
                check skips a role with a user image, which only the run builds (SKY-893).
                A workspace with a skipped role is not prepared: opening a candidate or
                a metric box, or preparing it again, then raises ``RuntimeError``.

        Raises:
            RepoSetupError: When the repository or the metric packages cannot be set up.
            RuntimeError: When an earlier call skipped a role, because a second setup
                would open a box that already ran or one the check meant to skip.
        """
        with self._lock:
            if self._prepared:
                return
            if self._failure is not None:
                raise self._failure
            if self._skipped:
                raise RuntimeError(
                    f"This workspace skipped the setup of {', '.join(self._skipped)} and prepares no more."
                )
            try:
                self._prepare(on_output, skip)
            except BaseException as error:
                self._failure = error
                raise
            self._prepared = not skip
            if skip:
                self._skipped = skip

    def _prepare_to_score(self) -> None:
        """Prepare the workspace before a box scores a version.

        Raises:
            RuntimeError: When a setup check skipped a role, so the workspace
                does not hold that role's setup (SKY-893).
        """
        if self._skipped:
            raise RuntimeError(f"This workspace skipped the setup of {', '.join(self._skipped)} and scores no version.")
        self.prepare()

    def _prepare(self, on_output: OutputSink | None, skip: tuple[str, ...] = ()) -> None:
        """Split the archive, then save the repository state and the metric state; the caller holds the lock.

        Args:
            on_output: Receives setup output as it streams.
            skip: Roles whose setup box does not open.

        Raises:
            RepoSetupError: When a step fails.
        """
        if self._folder is None:
            self._folder = Path(tempfile.mkdtemp(prefix="repo-workspace-"))
            self._requirements = read_metric_requirements(self._archive)
            self.metric_files = pack_metric_files(self._archive, self._hidden) if self._hidden else None
        if "run" not in skip:
            self._repo_state = self._setup_repository(on_output)
        if "metric" not in skip:
            self._metric_state = self._setup_metric() if self._requirements or self._metric_setup else None

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
            replace(
                self._spec,
                image=self._run_image,
                user_image=self._run_user_image,
                operation_key=self._next_key("setup"),
                services=(),
                snapshot_id=None,
            )
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
        root = physical_root(session, self.user_run_image)
        if self._protected:
            baseline = self.fingerprint(session, PREPARED_DIR, root)
            if baseline is None:
                raise RepoSetupError("The repository's protected files could not be fingerprinted.")
            self._protected_baseline = baseline[0]
        self._prepared_digest = self._digest_prepared(session, root)
        if self._prepared_digest is None:
            raise RepoSetupError("The prepared repository could not be fingerprinted.")

    def _setup_metric(self) -> _State:
        """Install the declared metric packages and run ``[metric] setup`` in a box without repository code.

        The box holds only the metric files, and saves its state for every metric box.

        Returns:
            The saved state.

        Raises:
            RepoSetupError: When the install or the build fails, or the state cannot be saved.
        """
        # The registries, also when the repository has no [setup] command: the
        # spec's own hosts are then empty, and the install would run offline.
        spec = replace(
            self._spec,
            image=self._metric_image,
            user_image=self._metric_user_image,
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
            if self._requirements:
                session.write_files({f"{METRIC_DIR}/requirements.txt": "\n".join(self._requirements) + "\n"})
                result = session.run(
                    f"{shlex.quote(self.metric_python)} -m pip install --target {METRIC_SITE_DIR} --no-input --disable-pip-version-check"
                    f" -r {METRIC_DIR}/requirements.txt && rm -f {METRIC_DIR}/requirements.txt",
                    env={"PIP_BREAK_SYSTEM_PACKAGES": "1"},
                    timeout_seconds=_SETUP_TIMEOUT_SECONDS,
                )
                if not result.ok:
                    log = redact(_tail(result), self._secrets.values())
                    raise RepoSetupError(f"The metric packages ({METRIC_REQUIREMENTS}) could not be installed.\n{log}")
            if self._metric_setup is not None:
                self._build_metric(session)
            ready = session.run(f"touch {_METRIC_READY}", timeout_seconds=60)
            if not ready.ok:
                raise RepoSetupError(f"The metric setup could not be marked ready: {_tail(ready)}")
            return self._save(session, self._metric_snapshot_key, "metric-state")
        finally:
            _close_quietly(session, "The metric setup box")

    def _build_metric(self, session: SandboxSession) -> None:
        """Run the ``[metric] setup`` command once in the metric files' folder, then remove the metric files (SKY-740).

        Args:
            session: The metric setup box, after the package install.

        Raises:
            RepoSetupError: When the files cannot be installed, or the command
                fails or runs out of time; the error holds the output's last lines.
        """
        assert self._metric_setup is not None
        _install_metric_files(session, self.metric_files, self.metric_python)
        build = session.run(f"cd {METRIC_REPO_DIR} && {self._metric_setup}", timeout_seconds=_SETUP_TIMEOUT_SECONDS)
        log = redact(_tail(build), self._secrets.values())
        if build.timed_out:
            raise RepoSetupError(f"The metric setup command ([metric] setup) ran out of time.\n{log}")
        if not build.ok:
            raise RepoSetupError(f"The metric setup command ([metric] setup) failed (exit {build.exit_code}).\n{log}")
        if self.metric_files is not None:
            _remove_metric_files(session, self.metric_files)

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

    def _open_box(self, spec: SandboxSpec, state: _State | None, role: str, grants: tuple[Grant, ...]) -> BoxSession:
        """Open one box from a saved state through the tree sandbox session API (SKY-718, SKY-785).

        Args:
            spec: The box's spec without its state.
            state: The saved state; ``None`` opens the plain image.
            role: ``candidate`` or ``metric``.
            grants: The role's network grants; empty keeps the box offline.

        Returns:
            The session that holds the open box, with a tar state unpacked.

        Raises:
            RepoSetupError: When a tar state cannot be unpacked.
            _SnapshotLostError: When the box cannot open from its snapshot.
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
        try:
            return TreeSandbox.open_session(
                RepoBoxSource(self._runtime, spec), role, grants, start=start, what="A box from a tar state"
            )
        except _BUDGET_ERRORS:
            raise
        except Exception as error:
            # A saved state is a snapshot or a tar file, never both, so an error here with a snapshot is the open's.
            if spec.snapshot_id is None:
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
            image=self._metric_image if kind == "metric" else self._run_image,
            user_image=self._metric_user_image if kind == "metric" else self._run_user_image,
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
        A version that keeps a shipped symlink as a link at a new target or
        name breaks them too.

        Args:
            patch: The version as a git patch against the snapshot.

        Returns:
            One reason per broken rule; empty when the version is allowed.
        """
        # The size limit applies to what the guest sent, not to what is left.
        kept = patch if len(patch.encode("utf-8")) > MAX_PATCH_BYTES else drop_paths(patch, self._hidden)
        problems = patch_violations(kept, self._editable, self._readonly, self._protected)
        if problems:
            return problems
        # The candidate box runs ``git apply`` itself, so a shipped link that
        # the version keeps as a link must be refused here (SKY-906).
        if self._links is None:
            self._links = archive_links(self._archive)
        return [
            f"Symlinks are not allowed; the change retargets, moves, or copies the symlink {path!r}."
            for path in link_edits(kept, self._links)
        ]

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
            RuntimeError: When a setup check skipped a role.
        """
        self._prepare_to_score()
        spec = self._box_spec("candidate", self._run_vcpus, services=True)
        state = self._repo_state
        box = self._open_box(spec, state, "candidate", self._run_grants)
        session = box.box
        try:
            root = physical_root(session, self.user_run_image)
            if self._digest_prepared(session, root) != self._prepared_digest:
                self._drop_snapshots()
                lost = state is not None and state.snapshot_id is not None
                raise (_SnapshotLostError if lost else RepoSetupError)(
                    "The candidate box does not hold the prepared repository."
                )
            self._start_services(session)
            # Setup can make links that the shipped tree does not hold, so the
            # applied tree is checked here too. The paths come from a file, not
            # the command line, which one large version would make too long
            # for the kernel (SKY-906).
            session.write_files({PATCH_FILE: patch, PATHS_FILE: shlex.join(patch_paths(patch))})
            applied = session.run(
                f"set -e; cp -a {PREPARED_DIR} {WORK_DIR}; cd {WORK_DIR};"
                f" if [ -s ../version.patch ]; then {GIT} apply --binary --whitespace=nowarn ../version.patch; fi;"
                ' eval "set -- $(cat ../version.paths)"; for path do if [ -L "$path" ]; then'
                f' echo "Symlinks are not allowed; the change leaves one at $path." >&2; exit {_LINK_EXIT}; fi; done',
                timeout_seconds=_SETUP_TIMEOUT_SECONDS,
            )
            if applied.ok:
                return CandidateBox(self, session, root, box)
        except BaseException:
            box.release_failed("The candidate box")
            raise
        # Outside the try: a budget error of this release must not release the box a second time.
        box.release_failed("The candidate box")
        if applied.exit_code == _LINK_EXIT and not applied.timed_out:
            return Checkout(path=None, problems=(_tail(applied),), blocked=True)
        return Checkout(path=None, problems=(f"The change does not apply cleanly: {_tail(applied)}",))

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
            RuntimeError: When a setup check skipped a role.
        """
        self._prepare_to_score()
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

    def _digest_prepared(self, session: SandboxSession, root: str) -> str | None:
        """Hash the whole prepared tree in a box.

        Args:
            session: The box holding the prepared tree.
            root: The box's working directory from :func:`physical_root`.

        Returns:
            The tree's SHA-256 digest, or ``None`` when the hashing command
            failed or printed something that is not a digest.
        """
        result = session.run(
            f'{absolute_cd(PREPARED_DIR, root)} && {_pick_base_python(self.user_run_image)}"$p" -I -S -c'
            f" {shlex.quote(_HASH_PREPARED)}",
            timeout_seconds=_HASH_TIMEOUT_SECONDS,
        )
        lines = result.stdout.strip().splitlines() if result.ok else []
        digest = lines[-1] if lines else ""
        return digest if len(digest) == 64 and all(char in "0123456789abcdef" for char in digest) else None

    def fingerprint(
        self, session: SandboxSession, directory: str, root: str
    ) -> tuple[dict[str, str], dict[str, str]] | None:
        """Hash every file under the protected paths of one tree in a box.

        Args:
            session: The box holding the tree.
            directory: The tree's folder, relative to the box.
            root: The box's working directory from :func:`physical_root`.

        Returns:
            Repository-relative path to digest, and path to inode and change
            times for every file and folder, or ``None`` when the hashing
            command failed or printed something unreadable.
        """
        paths = " ".join(shlex.quote(path) for path in self._protected)
        result = session.run(
            f'{absolute_cd(directory, root)} && {_pick_base_python(self.user_run_image)}"$p" -I -S -c'
            f" {shlex.quote(_HASH_PROTECTED)} {paths}",
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
