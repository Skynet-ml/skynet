"""Turn repository versions into ready checkouts inside one parent-owned sandbox.

The trusted parent owns this box, not the guest that hosts the agent, so a
run's secrets reach the scorer without ever sharing a filesystem with the
agent that writes the versions.

The box may open with network access to package registries so the setup
command can install dependencies. Setup runs once, on the starting commit and
without the run's secrets; the box's network is then switched off and the
switch checked from inside before any version is laid out. Every version
starts from a copy of that prepared tree and runs no setup of its own.

The metric files and the exploit folder leave the box once setup is done,
also from the git history: the parent keeps them and installs them into a
version's checkout only after the version's own code stopped (E38).
"""

from __future__ import annotations

import json
import logging
import posixpath
import shlex
import threading
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .repo_tree import MAX_PATCH_BYTES, archive_chunks, drop_paths, patch_violations
from .sandbox import CommandResult, OutputSink, SandboxRuntime, SandboxSession, SandboxSpec

logger = logging.getLogger(__name__)

# The agent never sees the hidden files, so this reason does not name them,
# and the scorer keeps it out of the rule judge's protected-path message.
HIDDEN_CHANGED = "The metric files changed while the version ran."

REPO_DIR = ".app-repo"
BASE_DIR = f"{REPO_DIR}/base"
WORK_DIR = f"{REPO_DIR}/work"
PREPARED_DIR = f"{REPO_DIR}/prepared"
PATCH_FILE = f"{REPO_DIR}/version.patch"
HIDDEN_FILE = f"{REPO_DIR}/hidden.b64"
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
# Packs the hidden paths that exist in the prepared tree, so the parent can
# hold them while the version's code runs. ``-I``: the tree is the user's.
_PACK_HIDDEN = r"""
import base64, io, os, sys, tarfile

buffer = io.BytesIO()
with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
    for path in sys.argv[2:]:
        if os.path.lexists(path):
            archive.add(path)
with open(sys.argv[1], "w", encoding="ascii") as handle:
    handle.write(base64.b64encode(buffer.getvalue()).decode("ascii"))
"""
# Puts the hidden files back into the checkout once the version's code
# stopped. ``-I``: the checkout is the version's. It removes the archive in
# every case, so no later version reads it. It refuses a folder above a hidden
# path that the version turned into a file or a link, so nothing is written
# through a link, and it removes what the version left at a hidden path
# without following links. Extraction then only makes files and folders
# below real folders (and links that stay inside the checkout, where the
# ``data`` filter exists).
_INSTALL_HIDDEN = r"""
import base64, io, os, shutil, sys, tarfile

source, paths = sys.argv[1], sys.argv[2:]
try:
    for path in paths:
        parts = path.split("/")
        for depth in range(1, len(parts)):
            parent = "/".join(parts[:depth])
            if os.path.islink(parent) or (os.path.lexists(parent) and not os.path.isdir(parent)):
                sys.exit(3)
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        elif os.path.lexists(path):
            os.unlink(path)
    if source:
        with open(source, "rb") as handle:
            data = base64.b64decode(handle.read(), validate=True)
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
finally:
    if source and os.path.lexists(source):
        os.unlink(source)
"""
# The install script's exit code when the version replaced a folder above a hidden path.
_NOT_A_FOLDER = 3
# Linux caps one environment string at 128 KiB; a larger stage answer goes
# through a file write and fresh stamps instead.
_ANSWER_ENV_BYTES = 100_000
# Fingerprints every file under the protected paths. It runs with ``-I`` so a
# module the version drops into the checkout cannot shadow ``hashlib``; links
# and special files are recorded rather than followed or opened. Besides the
# contents it records each file's and folder's inode and change times: any
# write, rename or link moves the change time, and only root can set it back,
# so a file rewritten and then restored while the scorer runs still shows.
# Given APP_ANSWER_PATH, it then writes APP_ANSWER there from inside the box:
# unlike a Vercel file write, that moves no protected file's change time, so
# a stage answer needs no fresh stamps after it.
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
        # The hidden files, packed, held here while no checkout has them (E38).
        self._metric_files: str | None = None
        self._metric_installed = False
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
        if self._watched:
            baseline = self._fingerprint(session, PREPARED_DIR)
            if baseline is None:
                raise RepoSetupError("The repository's protected files could not be fingerprinted.")
            self._protected_baseline = baseline[0]
        if self._hidden:
            self._take_hidden(session)

    def _take_hidden(self, session: SandboxSession) -> None:
        """Move the hidden files out of the prepared tree and its git history into the parent.

        The baseline above still covers them, so the metric a checkout gets
        later must match the starting commit's. Setup output the setup command
        copied elsewhere in the tree is not found here.

        Args:
            session: The box holding the prepared tree.

        Raises:
            RepoSetupError: When the files cannot be packed or removed.
        """
        paths = " ".join(shlex.quote(path) for path in self._hidden)
        packed = session.run(
            f"cd {PREPARED_DIR} && python3 -I -c {shlex.quote(_PACK_HIDDEN)} ../{posixpath.basename(HIDDEN_FILE)} {paths}",
            timeout_seconds=_SETUP_TIMEOUT_SECONDS,
        )
        text = session.read_file(HIDDEN_FILE) if packed.ok else None
        if text is None:
            raise RepoSetupError(f"The metric files could not be packed: {_tail(packed)}")
        git = f"git {_GIT_IDENTITY} {_GIT_NO_MAINTENANCE}"
        # The base commit and every loose object hold the hidden files too;
        # the amended commit replaces it and the old objects are pruned.
        removed = session.run(
            f"set -e; rm -f {HIDDEN_FILE}; cd {PREPARED_DIR}; rm -rf -- {paths};"
            f" if [ -d .git ]; then {git} rm -r -q --cached --ignore-unmatch -- {paths};"
            f" {git} commit -q --amend --allow-empty -m base; {git} reflog expire --expire=now --all;"
            f" {git} gc -q --prune=now; fi",
            timeout_seconds=_SETUP_TIMEOUT_SECONDS,
        )
        if not removed.ok:
            raise RepoSetupError(f"The metric files could not be removed from the tree: {_tail(removed)}")
        self._metric_files = text

    def _fingerprint(
        self, session: SandboxSession, directory: str, answer: tuple[str, str] | None = None
    ) -> tuple[dict[str, str], dict[str, str]] | None:
        """Hash every file under the protected and hidden paths of one tree in the box.

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
        paths = " ".join(shlex.quote(path) for path in self._watched)
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
            self._metric_installed = False
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

    def install_metric_files(self) -> str | None:
        """Put the hidden files back into the current checkout, once the version's code stopped.

        Returns:
            Why the files could not be installed, or ``None`` once they are.
        """
        with self._lock:
            if self._metric_installed or not self._hidden:
                self._metric_installed = True
                return None
            session = self.session()
            paths = " ".join(shlex.quote(path) for path in self._hidden)
            # A fresh name: the version may have left a file or a link at any name it knew.
            source = f"{REPO_DIR}/hidden-{uuid.uuid4().hex}.b64" if self._metric_files is not None else ""
            try:
                if self._metric_files is not None:
                    session.write_files({source: self._metric_files})
                argument = f"../{posixpath.basename(source)}" if source else "''"
                result = session.run(
                    f"cd {WORK_DIR} && python3 -I -c {shlex.quote(_INSTALL_HIDDEN)} {argument} {paths}",
                    timeout_seconds=_SETUP_TIMEOUT_SECONDS,
                )
            except BaseException:
                self._end_box()
                raise
            if not result.ok:
                # The archive may still be in the box; no later version runs in it (E38).
                self._end_box()
                if result.exit_code == _NOT_A_FOLDER:
                    return (
                        "The version replaced a folder of the metric files with a file or a link, so it has no score."
                    )
                return "The metric files could not be installed after the version ran."
            self._metric_installed = True
            return None

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
        with the prepared tree, and inodes and change times with the last
        stamp, so a write the code undoes before it exits usually still
        counts. One gap stays: a stage answer too large for the joined run
        goes in by a file write and is stamped again, so a write the code
        makes and undoes between that answer's check and its new stamp is
        not seen. Call this after the scorer finishes and before its score is
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
        return self._changes(current, before)

    def answer(self, path: str, text: str) -> tuple[str, ...] | None:
        """Check the protected files while the version runs, then write one stage answer into the box.

        The check and the write share one box command, so the stamps taken
        before the run stay valid; an answer too large for that is written
        with a file write and the files are stamped again after it.

        Args:
            path: Where the scorer's ``call`` waits for the answer, relative to the box.
            text: The answer.

        Returns:
            Like :meth:`protected_changes`, or ``None`` when the files could
            not be checked or the answer could not be written.
        """
        with self._lock:
            session = self.session()
            before = self._stamps_before_run
            if not self._watched:
                return () if self._deliver(session, path, text) else None
            if before is not None and len(text.encode("utf-8")) <= _ANSWER_ENV_BYTES:
                current = self._fingerprint(session, WORK_DIR, answer=(path, text))
                if current is not None:
                    return self._changes(current, before)
                # The version gets no score either way; the write only stops
                # the scorer from waiting for its timeout.
                self._deliver(session, path, text)
                return None
            current = self._fingerprint(session, WORK_DIR) if before is not None else None
            if not self._deliver(session, path, text) or current is None or before is None:
                return None
            # The write moved every change time; only a write the version
            # undoes before this point goes unseen, a lasting one still
            # differs from the prepared tree.
            after = self._fingerprint(session, WORK_DIR)
            if after is None:
                return None
            self._stamps_before_run = after[1]
            return self._changes(current, before)

    @staticmethod
    def _deliver(session: SandboxSession, path: str, text: str) -> bool:
        """Write one stage answer with a file write.

        Args:
            session: The scorer's box.
            path: Where the answer goes, relative to the box.
            text: The answer.

        Returns:
            Whether the write succeeded.
        """
        try:
            session.write_files({path: text})
        except Exception as error:
            logger.warning("A stage answer could not reach the scorer box: %s", error)
            return False
        return True

    def _changes(self, current: tuple[dict[str, str], dict[str, str]], before: Mapping[str, str]) -> tuple[str, ...]:
        """Compare one fingerprint with the prepared tree and the stamps taken before the run.

        Args:
            current: Digests and stamps from :meth:`_fingerprint`.
            before: Stamps taken just before the version ran.

        Returns:
            One reason per protected file added, changed or removed, one
            reason for any such hidden file; empty when untouched.
        """
        files, stamps = current
        baseline = self._protected_baseline
        if not self._metric_installed:
            # The hidden files are out of the tree while the version runs; one there is planted.
            baseline = {path: digest for path, digest in baseline.items() if not self._is_hidden(path)}
        changed = {path for path in set(baseline) | set(files) if baseline.get(path) != files.get(path)} | {
            path for path in set(before) | set(stamps) if before.get(path) != stamps.get(path)
        }
        # A folder's times move whenever an entry in it is added or removed;
        # name the folder only when no file in it explains the change.
        changed = sorted(path for path in changed if not any(other.startswith(f"{path}/") for other in changed))
        hidden = [path for path in changed if self._is_hidden(path)]
        reasons = [HIDDEN_CHANGED] if hidden else []
        return (
            *reasons,
            *(
                f"'{path}' is protected by the repository's optimize.toml ([paths] protected),"
                " and the version's code changed it while it ran."
                for path in changed
                if path not in hidden
            ),
        )

    def _is_hidden(self, path: str) -> bool:
        """Return True when a repository-relative path is a hidden path or lies under one.

        Args:
            path: Normalized path from a fingerprint.

        Returns:
            Whether the agent must never see it.
        """
        return any(path == top or path.startswith(f"{top}/") for top in self._hidden)

    def close(self) -> None:
        """Destroy the box."""
        with self._lock:
            self._end_box()

    def _end_box(self) -> None:
        """Destroy the box; the caller holds the lock."""
        if self._session is not None:
            session, self._session = self._session, None
            session.close()
