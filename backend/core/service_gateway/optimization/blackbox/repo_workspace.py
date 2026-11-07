"""Turn repository versions into ready checkouts inside one parent-owned sandbox.

The trusted parent owns this box, not the guest that hosts the agent, so a
run's secrets reach the scorer without ever sharing a filesystem with the
agent that writes the versions.

The box may open with network access to package registries so the setup
command can install dependencies. Setup runs once, on the starting commit and
without the run's secrets; the box's network is then switched off and the
switch checked from inside before any version is laid out. Every version
starts from a copy of that prepared tree and runs no setup of its own.
"""

from __future__ import annotations

import json
import logging
import posixpath
import shlex
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .repo_tree import MAX_PATCH_BYTES, archive_chunks, drop_paths, patch_violations
from .sandbox import CommandResult, OutputSink, SandboxRuntime, SandboxSession, SandboxSpec

logger = logging.getLogger(__name__)

REPO_DIR = ".app-repo"
BASE_DIR = f"{REPO_DIR}/base"
WORK_DIR = f"{REPO_DIR}/work"
PREPARED_DIR = f"{REPO_DIR}/prepared"
PATCH_FILE = f"{REPO_DIR}/version.patch"
_SETUP_TIMEOUT_SECONDS = 1_800.0
_GIT_IDENTITY = "-c user.name=app -c user.email=app@localhost -c commit.gpgsign=false"
# A commit may start ``git maintenance`` in the background (detached since
# git 2.47), whose lock files come and go while ``cp -a`` walks ``.git`` and
# fail the copy with "cannot stat .git/objects/maintenance.lock".
_GIT_NO_MAINTENANCE = "-c maintenance.auto=false -c gc.auto=0"
_PROBE_TIMEOUT_SECONDS = 120.0
_PROBE_ATTEMPTS = 5
_PROBE_OK = "app-network-closed"
_HASH_TIMEOUT_SECONDS = 300.0
# Fingerprints every file under the protected paths. It runs with ``-I`` so a
# module the version drops into the checkout cannot shadow ``hashlib``; links
# and special files are recorded rather than followed or opened. Besides the
# contents it records each file's and folder's inode and change times: any
# write, rename or link moves the change time, and only root can set it back,
# so a file rewritten and then restored while the scorer runs still shows.
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
print(json.dumps({"files": found, "stamps": stamps}, sort_keys=True))
"""


class RepoSetupError(RuntimeError):
    """The repository could not be prepared, so no version can be scored."""


class NetworkCutoffError(RuntimeError):
    """The scoring box's network could not be switched off and confirmed; the run must stop."""


def _probe_command(hosts: Iterable[str]) -> str:
    """Build the in-box check that every formerly allowed host is now unreachable.

    A connection that opens is the only evidence of an open network, so the
    check passes only when it finishes and prints its marker with none open.
    It retries a few times because a policy change can take a moment to land.

    Args:
        hosts: Hosts the box could reach before the switch.

    Returns:
        A shell command printing ``_PROBE_OK`` once no host accepts a connection.
    """
    checks = " ".join(
        f"if timeout 5 bash -c {shlex.quote(f'exec 3<>/dev/tcp/{host}/443')} 2>/dev/null; then open=1; fi;"
        for host in hosts
    )
    return (
        f"for attempt in $(seq {_PROBE_ATTEMPTS}); do open=0; {checks}"
        f' if [ "$open" = 0 ]; then echo {_PROBE_OK}; exit 0; fi; sleep 2; done; exit 1'
    )


@dataclass(frozen=True)
class Checkout:
    """One version laid out on disk, or the reason it could not be."""

    path: str | None
    problems: tuple[str, ...] = ()
    # The run's path rules refused the version, as opposed to a change that
    # is too large or does not apply.
    blocked: bool = False


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


class RepoWorkspace:
    """Unpack a repository snapshot once, prepare it, then rebuild a clean checkout for every version.

    Every version starts from a fresh copy of the prepared tree, so nothing one
    version's code writes into the checkout reaches the next. Calls are
    serialized: versions are scored one at a time.
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
    ) -> None:
        """Bind a snapshot to a runtime without opening anything yet.

        Args:
            runtime: Where the box is opened.
            spec: Lifetime, labels and network policy for the box. A box that
                is not deny-all from the start is switched off after setup.
            archive: Packed tree from :func:`fetch_snapshot`.
            editable_paths: Files and folders versions may change.
            readonly_paths: Submodules and Git LFS files.
            setup_command: Shell command run once on the starting commit.
            secrets: Environment the scorer receives; setup never does.
            protected_paths: Files and folders no version may change, even
                inside an editable path.
            hidden_paths: The metric files and exploit folder the agent never
                sees. A version's edits to them are dropped, so every version
                is scored with the base commit's metric, and they are
                fingerprinted like the protected paths.
        """
        self._runtime = runtime
        self._spec = spec
        self._archive = archive
        self._editable = tuple(editable_paths)
        self._readonly = tuple(readonly_paths)
        self._protected = tuple(protected_paths)
        self._hidden = tuple(posixpath.normpath(path) for path in hidden_paths)
        # The version's code runs next to the metric, so it could rewrite the
        # metric while it runs; the patch check alone never sees that (SKY-353).
        self._watched = tuple(dict.fromkeys((*self._protected, *self._hidden)))
        self._setup = (setup_command or "").strip() or None
        self._secrets = dict(secrets)
        self._session: SandboxSession | None = None
        self._failure: BaseException | None = None
        self._setup_log = ""
        # Held in the parent, not the box: a version's code can rewrite any
        # copy of the tree inside the box, the prepared one included.
        self._protected_baseline: dict[str, str] = {}
        self._stamps_before_run: dict[str, str] | None = None
        self._lock = threading.Lock()

    @property
    def secrets(self) -> dict[str, str]:
        """Return the environment versions run with."""
        return dict(self._secrets)

    @property
    def setup_log(self) -> str:
        """Return the end of the setup command's output, once it has run."""
        return self._setup_log

    def session(self, *, on_output: OutputSink | None = None) -> SandboxSession:
        """Return the open box, preparing it on first use.

        Preparation unpacks the snapshot, runs the setup command once on a copy
        laid out where versions will live, keeps that as the prepared tree, and
        takes the box offline. A failure is remembered, so no later call
        reopens a box and nothing runs in one that was not taken offline.

        Args:
            on_output: Receives setup output as it streams.

        Returns:
            The offline box holding the prepared tree under ``PREPARED_DIR``.

        Raises:
            RepoSetupError: When the snapshot cannot be unpacked or set up.
            NetworkCutoffError: When the network cannot be switched off and confirmed.
        """
        if self._session is not None:
            return self._session
        if self._failure is not None:
            raise self._failure
        session = self._runtime.open(self._spec)
        try:
            self._prepare(session, on_output)
            if not self._spec.network_disabled:
                self._cut_network(session)
        except BaseException as error:
            self._failure = error
            # Unsettled usage stays in the ledger for reconciliation; the setup
            # error is what the user can act on, so no close error may replace
            # it. Only the type is logged: settlement and ledger texts can name
            # the provider.
            try:
                session.close()
            except Exception as close_error:
                logger.warning(
                    "Repository setup failed; the sandbox did not close cleanly (%s)",
                    type(close_error).__name__,
                )
            raise
        self._session = session
        return session

    def _prepare(self, session: SandboxSession, on_output: OutputSink | None) -> None:
        """Unpack the snapshot and run the setup command once on the starting commit.

        Setup runs on a copy at ``WORK_DIR`` because tools such as virtualenvs
        and editable installs record absolute paths, and every version is laid
        out at that same path later. It gets no secrets: the box may still
        reach package registries while it runs.

        Args:
            session: The freshly opened box.
            on_output: Receives setup output as it streams.

        Raises:
            RepoSetupError: When the snapshot cannot be unpacked or set up.
        """
        parts = []
        for index, chunk in enumerate(archive_chunks(self._archive)):
            name = f"{REPO_DIR}/tree.{index:04d}.b64"
            session.write_files({name: chunk})
            parts.append(name)
        files = " ".join(shlex.quote(part) for part in parts)
        result = session.run(
            f"set -e; mkdir -p {BASE_DIR}; cat {files} | base64 -d | tar -xzf - -C {BASE_DIR}; rm -f {files};"
            f" cd {BASE_DIR}; git init -q; git add -A -f; git {_GIT_IDENTITY} {_GIT_NO_MAINTENANCE} commit -q --allow-empty -m base;"
            f" cd - >/dev/null; rm -rf {WORK_DIR} {PREPARED_DIR}; cp -a {BASE_DIR} {WORK_DIR}",
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
        if self._watched:
            baseline = self._fingerprint(session, PREPARED_DIR)
            if baseline is None:
                raise RepoSetupError("The repository's protected files could not be fingerprinted.")
            self._protected_baseline = baseline[0]

    def _fingerprint(self, session: SandboxSession, directory: str) -> tuple[dict[str, str], dict[str, str]] | None:
        """Hash every file under the protected and hidden paths of one tree in the box.

        Args:
            session: The box holding the tree.
            directory: The tree's folder, relative to the box.

        Returns:
            Repository-relative path to digest, and path to inode and change
            times for every file and folder, or ``None`` when the hashing
            command failed or printed something unreadable.
        """
        paths = " ".join(shlex.quote(path) for path in self._watched)
        result = session.run(
            f"cd {shlex.quote(directory)} && python3 -I -c {shlex.quote(_HASH_PROTECTED)} {paths}",
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

    def _cut_network(self, session: SandboxSession) -> None:
        """Switch the box offline and confirm it from inside before any version runs.

        Args:
            session: The prepared box.

        Raises:
            NetworkCutoffError: When the switch is unsupported, fails, or a
                formerly allowed host still accepts connections.
        """
        switch = getattr(session, "disable_network", None)
        if switch is None:
            raise NetworkCutoffError("This sandbox cannot switch its network off, so versions cannot run in it.")
        try:
            switch()
        except Exception as error:
            raise NetworkCutoffError(f"The scoring box's network could not be switched off: {error}") from error
        hosts = self._spec.allowed_hosts or ("pypi.org",)
        probe = session.run(_probe_command(hosts), timeout_seconds=_PROBE_TIMEOUT_SECONDS)
        if not probe.ok or _PROBE_OK not in probe.stdout:
            raise NetworkCutoffError("The scoring box could still reach the network after it was switched off.")

    def violations(self, patch: str) -> list[str]:
        """Explain how a version breaks the run's path rules, without touching the box.

        Edits to hidden paths are not checked: :meth:`checkout` drops them.

        Args:
            patch: The version as a git patch against the snapshot.

        Returns:
            One reason per broken rule; empty when the version is allowed.
        """
        # The size limit applies to what the guest sent, not to what is left.
        kept = patch if len(patch.encode("utf-8")) > MAX_PATCH_BYTES else drop_paths(patch, self._hidden)
        return patch_violations(kept, self._editable, self._readonly, self._protected)

    def checkout(self, patch: str) -> Checkout:
        """Lay out one version on a copy of the prepared tree.

        Args:
            patch: The version as ``git diff --binary`` against the snapshot.

        Returns:
            The checkout's path, relative to the box, or why the version is
            not allowed or does not apply. The base commit's metric files stay
            in place whatever the version wrote to them.
        """
        problems = self.violations(patch)
        if problems:
            return Checkout(path=None, problems=tuple(problems), blocked=len(patch.encode("utf-8")) <= MAX_PATCH_BYTES)
        patch = drop_paths(patch, self._hidden)
        with self._lock:
            self._stamps_before_run = None
            session = self.session()
            session.write_files({PATCH_FILE: patch})
            applied = session.run(
                f"set -e; rm -rf {WORK_DIR}; cp -a {PREPARED_DIR} {WORK_DIR}; cd {WORK_DIR};"
                f" if [ -s ../version.patch ]; then git apply --binary --whitespace=nowarn ../version.patch; fi",
                timeout_seconds=_SETUP_TIMEOUT_SECONDS,
            )
            if not applied.ok:
                return Checkout(path=None, problems=(f"The change does not apply cleanly: {_tail(applied)}",))
            return Checkout(path=WORK_DIR)

    def stamp_before_run(self) -> str | None:
        """Record the protected files' inodes and change times just before the version runs.

        Call it after the last file the host writes into the box and right
        before the scorer starts: a Vercel file write moves the change time of
        every file in the box, so stamps taken earlier would flag every
        protected file.

        Returns:
            Why the files could not be stamped, or ``None`` once they are.
        """
        if not self._watched:
            return None
        with self._lock:
            before = self._fingerprint(self.session(), WORK_DIR)
            if before is None:
                return "The protected files could not be checked before the version ran."
            self._stamps_before_run = before[1]
            return None

    def protected_changes(self) -> tuple[str, ...]:
        """Explain how the version that last ran changed the protected files.

        The patch check only sees the files a version names; its code can
        still write to the judge's files while it runs. Contents are compared
        with the prepared tree, and inodes and change times with the checkout
        just before the run, so a write the code undoes before it exits still
        counts. Call this after the scorer finishes and before its score is
        accepted.

        Returns:
            One reason per protected file added, changed or removed since
            setup, one reason for any such hidden file, or why they could not
            be checked; empty when untouched.
        """
        if not self._watched:
            return ()
        with self._lock:
            current = self._fingerprint(self.session(), WORK_DIR)
            before = self._stamps_before_run
        if current is None or before is None:
            return ("The protected files could not be checked after the version ran.",)
        files, stamps = current
        changed = {
            path
            for path in set(self._protected_baseline) | set(files)
            if self._protected_baseline.get(path) != files.get(path)
        } | {path for path in set(before) | set(stamps) if before.get(path) != stamps.get(path)}
        # A folder's times move whenever an entry in it is added or removed;
        # name the folder only when no file in it explains the change.
        changed = sorted(path for path in changed if not any(other.startswith(f"{path}/") for other in changed))
        hidden = [path for path in changed if any(path == top or path.startswith(f"{top}/") for top in self._hidden)]
        # The agent never sees the hidden files, so the reason does not name them.
        reasons = ["The version's code changed the repository's metric files while it ran."] if hidden else []
        return (
            *reasons,
            *(
                f"'{path}' is protected by the repository's optimize.toml ([paths] protected),"
                " and the version's code changed it while it ran."
                for path in changed
                if path not in hidden
            ),
        )

    def close(self) -> None:
        """Destroy the box."""
        with self._lock:
            if self._session is not None:
                session, self._session = self._session, None
                session.close()
