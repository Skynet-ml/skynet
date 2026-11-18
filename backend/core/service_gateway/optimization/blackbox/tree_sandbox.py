"""Run agent steps and metrics on tree ids, each in its own box (SKY-718).

:meth:`TreeSandbox.run` runs one command in a candidate box over a stored
tree and stores the box's tree after the command. :meth:`TreeSandbox.score`
runs the metric in a separate box that gets only the outputs tree and the
metric tree, so no candidate code reaches it. Each call commits one step to
the step log and returns only once that step is durable behind the run's
fence. :class:`RuntimeBoxSource` is the fresh-box-per-call source:
it opens a new box for each acquire and closes it at release.
:class:`RuntimeBoxLease` keeps one warm box for each agent of a run on the
provider that placement picks (SKY-732); the lease key holds the run id, so
two runs never share a box. A metric lease must use ``keep_warm=False``: no
wipe resets /tmp, $HOME and stray processes on every provider, so each score
gets a new box.

A step's process must not live into the next step of a warm box, where it
could write into the unpacked tree. No simple way kills every such process on
every provider: a candidate can leave its process group and session. So the
step goes back healthy only when it can show that no process is left: the
command did not time out, and a scan of /proc after the pack finds no process
that the scan before the unpack did not see. A box whose scan fails or that has
other new processes goes back unhealthy, and the next step gets a new box.

The box user can write a folder on the box PATH (``/app/.venv/bin`` on
Vercel) and its own shell profile. A tool planted there could fake the scan,
or add files to the next step's tree in the unpack. So the scan, the unpack
and the pack run in a clean shell that finds its tools only in ``/usr/bin``
and ``/bin``. A planted tool would also change what the next command finds,
which a new box on a resume does not have. So the scan also prints a state
line, a hash over the metadata of every file in the PATH folders and of the
shell profile files, and a changed line makes the box unhealthy too.

On Vercel, a box user command writes its output and exit code to files in
/tmp that the box user owns, and the control plane reads them back. A stray
process could put the scan before the step in the file of the scan after it.
So the scan runs as root wherever the provider's root has capabilities, the
same rule as the protected-file check of the repo workspace (TS-1). A root
command's output goes into a root-owned folder. Under ``sudo``, root's
``HOME`` and ``PATH`` are its own, so the root scan takes the box user from
``SUDO_UID`` and the box PATH from the image's ``APP_ENTRY_PATH``, and it
fails when one of them is missing.

The limits: a candidate with root in the box can hide a process or change
the root tools. The local subprocess box refuses ``sudo`` and the local
Docker box has a root with no capabilities, so the scan runs as the box user
there. Both give the output through a pipe, but a stray process of the same
user can still trace the scan. Both are for development only. A provider
whose ``sudo`` drops ``APP_ENTRY_PATH``, such as EC2 (dev only), fails each
root scan, so each of its steps gets a new box. Other files outside the
tree, such as in ``$TMPDIR``, ``$HOME`` or the working folder, still reach
the agent's next step.

:meth:`TreeSandbox.open_session` is the session API (SKY-785). It acquires
one box from a :class:`BoxSource`, starts it from a state, and gives the
caller a :class:`BoxSession`: commands with their own ``env``, ``on_output``,
``sudo`` and timeout, the box itself for checks, and one release. It needs no
tree store and commits no step, so a caller such as ``RepoScorer`` that keeps
its own records uses the same box path as :meth:`TreeSandbox.run` and
:meth:`TreeSandbox.score`. It refuses a lease that keeps boxes warm, because
it gives the box back with no process scan.
"""

from __future__ import annotations

import base64
import io
import logging
import math
import shlex
import tarfile
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Any, TypeVar

from ....billing.budgets import BudgetError
from ....billing.operation_pricing import UnpricedOperationError
from ....billing.runtime import UsagePendingError
from ....billing.signals import BudgetReached
from ....sandbox.box_lease import BoxLease
from ....sandbox.grants import METRIC_BOX_GRANTS, Grant, GrantError, resolve_grants
from ....sandbox.placement import VERCEL, Placement, gpu_type, network_grants
from ....sandbox.trees import (
    MAX_ARCHIVE_BYTES,
    BadArchiveError,
    BoxFailureError,
    BoxSource,
    FenceLostError,
    GrantDeniedError,
    MetricFailedError,
    TreeRejectedError,
    tar_to_tree,
    tree_to_tar,
)
from ....storage.step_log_store import StepFenceError, StepLogStore, step_id_for
from ....storage.tree_store import TreeStore
from .repo_tree import ARCHIVE_CHUNK_BYTES
from .sandbox_contract import CommandResult, OutputSink, SandboxRuntime, SandboxSession, SandboxSpec
from .sandbox_runtime import LocalSubprocessSession

logger = logging.getLogger(__name__)

# Budget signals stop the run; they are not box failures to retry.
_BUDGET_ERRORS = (BudgetReached, BudgetError, UsagePendingError, UnpricedOperationError)
TREE_DIR = "tree"
OUTPUTS_DIR = "outputs"
METRIC_DIR = "metric"
_IN_DIR = ".tree-in"
_OUT_DIR = ".tree-out"
_TAR_FILE = ".tree-out.tgz"
_MOVE_TIMEOUT_SECONDS = 600
_MAX_OUT_PARTS = -(-MAX_ARCHIVE_BYTES // ARCHIVE_CHUNK_BYTES)
# The base64 text of the largest archive: 4 characters per 3 bytes, plus a line break per 76 on Linux,
# plus one padding group and one line break for each part, since each part is encoded on its own.
_MAX_B64_CHARS = 4 * -(-MAX_ARCHIVE_BYTES // 3)
_MAX_OUT_FILE_BYTES = _MAX_B64_CHARS + -(-_MAX_B64_CHARS // 76) + 5 * _MAX_OUT_PARTS
# The pack command's exit code for an archive over the limit; the box's own tar errors use 1 and 2.
_TOO_LARGE_EXIT = 97
_CHECK_TIMEOUT_SECONDS = 30
_SCAN_STDERR_CHARS = 200
# Runs a control script in a clean shell. env -i drops the box environment, so a tool comes only from the
# root-owned /usr/bin and /bin, and not from a folder that the box user can write. The box locale passes
# through, so tar names files as the box shell does. BOX_PATH and HOME give the scan the folders to check.
_CLEAN_ENV = '/usr/bin/env -i PATH=/usr/bin:/bin HOME="$HOME" LANG="$LANG" LC_ALL="$LC_ALL" BOX_PATH="$PATH"'
_CLEAN_BASH = " /bin/bash --noprofile --norc -c "
_CLEAN_SHELL = _CLEAN_ENV + _CLEAN_BASH
# Prints a "state <hash>" line, then one "pid start-ticks" line for each process, except the scan's own shell,
# its ancestors and its children (the loop, awk and sort), and the kernel threads (pid 2 and its children, only
# when pid 2 is kthreadd: on Docker and gVisor it can be a normal process). A process of the box user can rename
# itself kthreadd, so pid 2 must also have the PF_KTHREAD bit (0x00200000) in its flags, field 9, which only the
# kernel sets. mawk has no bitwise operators, so awk tests the bit with division. A pid with its start time names one
# process, also when the pid is used again. The scan reads /proc/<pid>/stat itself, because the box image
# (python:3.12-slim) has no ps. The name in a stat line can hold spaces and parens, so the fields come after the
# last ")": the parent pid is field 4 and the start time in clock ticks is field 22. A process that ends during
# the read has no stat file any more and is skipped. Any other read failure, a bad line or no process at all
# fails the scan. SCAN_PROC_ROOT is for tests only: env -i in PROCESS_SCAN drops it, so the box cannot set it.
# The hash covers the metadata of each file in the box PATH folders and of the shell profile files. stat -L
# follows a link to its target. The change time of a file changes on each write, and the box user cannot set it
# back. stat has GNU flags in a box and BSD flags on a macOS host.
# A root scan under sudo gets root's HOME and PATH, so it checks the home of the user that sudo names and the
# image's box PATH. A provider that already runs as root sets no SUDO_UID and keeps the box values.
SCAN_SCRIPT = (
    'set -o pipefail; if [ -n "$SUDO_UID" ] && [ "$(id -u)" = 0 ]; then'
    ' [ "$SUDO_UID" != 0 ] && [ -n "$ENTRY_PATH" ] || exit 1;'
    ' HOME=$(getent passwd "$SUDO_UID" | cut -d: -f6) && [ -n "$HOME" ] || exit 1; BOX_PATH=$ENTRY_PATH; fi;'
    " if stat -c %i / >/dev/null 2>&1; then fmt=(-L -c '%n %i %s %Y %Z');"
    " else fmt=(-L -f '%N %i %z %m %c'); fi;"
    ' state=$({ printf "%s\\n" "$BOX_PATH" | tr : "\\n" | while IFS= read -r d; do'
    ' find -H "$d" -maxdepth 1 -exec stat "${fmt[@]}" {} +; done;'
    ' for f in .bash_profile .bash_login .profile .bashrc; do stat "${fmt[@]}" "$HOME/$f"; done; } 2>&1 | cksum);'
    ' echo "state $state"; proc=${SCAN_PROC_ROOT:-/proc}; k=0;'
    ' if [ "$(cat "$proc/2/comm" 2>/dev/null)" = kthreadd ]; then k=2; fi; shopt -s nullglob;'
    ' for f in "$proc"/[0-9]*/stat; do if ! { IFS= read -r l < "$f"; } 2>/dev/null; then'
    ' [ -e "$f" ] && exit 1; continue; fi; [[ $l == *")"* ]] || exit 1;'
    ' printf "%s%s\\n" "${l%% *}" "${l##*)}"; done | awk -v me=$$ -v k=$k \''
    "$1 !~ /^[0-9]+$/ || $3 !~ /^[0-9]+$/ || $21 !~ /^[0-9]+$/ {bad = 1} {parent[$1] = $3; start[$1] = $21}"
    " $1 == 2 {flags = $8} END {if (bad || !NR) exit 1; if (int(flags / 2097152) % 2 != 1) k = 0;"
    " p = me; while ((p in parent) && p > 1) {skip[p] = 1; p = parent[p]}"
    " for (pid in start) if (!(pid in skip) && parent[pid] != me && !(k && (pid == k || parent[pid] == k)))"
    " print pid, start[pid]}"
    "' | LC_ALL=C sort"
)
# sudo keeps APP_ENTRY_PATH (the image's sudoers has !env_reset) and sets SUDO_UID; env -i would drop both.
_SUDO_ENV = ' ENTRY_PATH="$APP_ENTRY_PATH" SUDO_UID="$SUDO_UID"'
PROCESS_SCAN = _CLEAN_ENV + _SUDO_ENV + _CLEAN_BASH + shlex.quote(SCAN_SCRIPT)

_Result = TypeVar("_Result")


class RuntimeBoxSource:
    """Open a fresh box for each acquire and close it at release."""

    def __init__(
        self, runtime: SandboxRuntime, spec: SandboxSpec, registry: Mapping[str, Mapping[str, str]] | None = None
    ) -> None:
        """Bind the source to a runtime and the spec of its boxes.

        Args:
            runtime: Opens the boxes.
            spec: The box spec; the grants replace its network fields.
            registry: Maps each capability host to the headers that the edge
                injects for it; ``None`` holds no credential.
        """
        self._runtime = runtime
        self._spec = spec
        self._registry = registry or {}

    @property
    def registry(self) -> Mapping[str, Mapping[str, str]]:
        """Return the capability registry that this source resolves grants against."""
        return self._registry

    def acquire(self, agent_id: str, grants: Sequence[Grant]) -> SandboxSession:
        """Open a new box whose network is the grant list.

        Args:
            agent_id: The agent that runs the step; each call gets its own box.
            grants: The step's grants; empty means no network.

        Returns:
            The open box.

        Raises:
            GrantError: When the grant list does not resolve.
        """
        return self._runtime.open(resolve_grants(grants, self._registry).apply(self._spec))

    def release(self, box: SandboxSession, healthy: bool) -> None:
        """Close the box; this source never gives a box twice.

        Args:
            box: A box that :meth:`acquire` gave.
            healthy: Not used: a closed box is never reused.

        Raises:
            BudgetReached: And the other budget errors, from the close's settlement.
        """
        _close_box(box)


class RuntimeBoxLease(BoxLease[SandboxSession]):
    """Keep one warm box for each agent on the runtime of its placed provider (SKY-732)."""

    # Every budget error from an idle box's settlement must stop the run, so each one wins over a box error.
    stop_errors = _BUDGET_ERRORS

    def __init__(
        self,
        runtimes: Mapping[str, SandboxRuntime],
        spec: SandboxSpec,
        registry: Mapping[str, Mapping[str, str]] | None = None,
        **options: Any,
    ) -> None:
        """Bind the lease to one runtime for each provider and the spec of its boxes.

        Args:
            runtimes: Maps each provider name that placement can pick to the runtime that opens its boxes.
            spec: The box spec; the network grants replace its network fields.
            registry: Maps each capability host to the headers that the edge
                injects for it; ``None`` holds no credential.
            **options: The :class:`BoxLease` options, such as ``vercel_cap`` and ``keep_warm``.

        Raises:
            ValueError: When the cap, the spill provider or sleep_after_seconds
                is not valid, or no runtime opens the Vercel boxes or the
                spill provider's boxes.
            TypeError: When keep_warm is not a bool, or a function option is not callable.
        """
        self._runtimes = runtimes
        self._spec = spec
        self._registry = registry or {}
        # The lease checks the options first, so the spill provider below is a valid name.
        super().__init__(self._open_on_runtime, _close_box, check=_box_alive, **options)
        # Checked now: a missing spill runtime would fail only at the cap, as a retryable box failure.
        missing = sorted({VERCEL, self._spill} - set(runtimes))
        if missing:
            raise ValueError(f"The lease needs a runtime for each provider it can place on: {', '.join(missing)}.")

    @property
    def registry(self) -> Mapping[str, Mapping[str, str]]:
        """Return the capability registry that this lease resolves grants against."""
        return self._registry

    def acquire(self, agent_id: str, grants: Sequence[Grant]) -> SandboxSession:
        """Return the agent's warm box, or open a new one; refuse a GPU box or a denied grant first.

        Args:
            agent_id: The agent.
            grants: The box's grants.

        Returns:
            The box.

        Raises:
            GrantError: When the box needs a GPU, or the network grants do not resolve.
        """
        # Both refusals come before the claim, so the agent keeps its warm box and no start is spent.
        gpu = gpu_type(grants)
        if gpu is not None:
            # The blackbox spec has no GPU field, so a runtime here would open the box without the GPU.
            raise GrantError(f"No tree sandbox runtime opens a box with a {gpu} GPU.")
        resolve_grants(network_grants(grants), self._registry)
        return super().acquire(agent_id, grants)

    def _open_on_runtime(self, placement: Placement, grants: Sequence[Grant]) -> SandboxSession:
        """Open a new box on the placed provider's runtime.

        Args:
            placement: The provider of the box; :meth:`acquire` refuses a GPU first.
            grants: The box's grants.

        Returns:
            The open box.

        Raises:
            GrantError: When the network grants do not resolve.
            LookupError: When no runtime opens the placed provider's boxes.
        """
        runtime = self._runtimes.get(placement.provider)
        if runtime is None:
            raise LookupError(f"No runtime opens {placement.provider} boxes.")
        return runtime.open(resolve_grants(network_grants(grants), self._registry).apply(self._spec))


def _close_box(box: SandboxSession) -> None:
    """Close a box, and log only the type of a close failure.

    Args:
        box: The box.

    Raises:
        BudgetReached: And the other budget errors, from the close's settlement.
    """
    try:
        box.close()
    except _BUDGET_ERRORS:
        raise
    except Exception as error:
        # Only the type: the provider's text can name the box.
        logger.warning("A tree sandbox box did not close (%s)", type(error).__name__)


def _processes(box: SandboxSession) -> frozenset[str] | None:
    """Return the state line of a box and its processes, each as its pid and start time.

    Args:
        box: The box.

    Returns:
        The lines of :data:`PROCESS_SCAN`, or ``None`` when the scan fails,
        so the caller cannot show that no process is left.

    Raises:
        BudgetReached: And the other budget errors; the run stops.
    """
    try:
        result = box.run(PROCESS_SCAN, timeout_seconds=_CHECK_TIMEOUT_SECONDS, sudo=_scan_as_root(box))
    except _BUDGET_ERRORS:
        raise
    except Exception as error:
        # Only the type: the provider's text can name the box.
        logger.warning("The process scan of a %s box did not run (%s)", type(box).__name__, type(error).__name__)
        return None
    lines = frozenset(result.stdout.splitlines())
    if result.ok and not result.timed_out and lines:
        return lines
    # The first stderr line names the cause, such as a box without /proc. The box controls that text, so a cap
    # keeps a long line out of the log.
    first = next(iter(result.stderr.splitlines()), "")[:_SCAN_STDERR_CHARS]
    logger.warning(
        "The process scan of a %s box failed (exit code %s, timed out %s): %s",
        type(box).__name__,
        result.exit_code,
        result.timed_out,
        first,
    )
    return None


def _scan_as_root(box: SandboxSession) -> bool:
    """Return whether the process scan of a box runs as root.

    A box user command on Vercel writes its output to a file that the box
    user owns, so a stray process could replace the scan's lines. A root
    command's output goes into a root-owned folder. The rule is the one of the
    repo workspace's protected check (TS-1): a provider whose root has no
    capability, local Docker, gives the output through a pipe and runs the
    scan as the box user, and an unknown box keeps root.

    Args:
        box: The box.

    Returns:
        False for the local subprocess box, which refuses ``sudo``, and for a
        provider whose root has no capability; True for any other box.
    """
    if isinstance(box, LocalSubprocessSession):
        return False
    return bool(getattr(box, "root_has_capabilities", True))


def _clean(script: str) -> str:
    """Wrap a control script so it runs in the clean shell.

    Args:
        script: The bash script.

    Returns:
        The command line that runs the script with only the root-owned tools.
    """
    return _CLEAN_SHELL + shlex.quote(script)


def _box_alive(box: SandboxSession) -> bool:
    """Return whether a warm box still runs a command.

    Args:
        box: The box.

    Returns:
        False when the box fails or dies.

    Raises:
        BudgetReached: And the other budget errors; the run stops.
    """
    try:
        return box.run("true", timeout_seconds=_CHECK_TIMEOUT_SECONDS).ok
    except _BUDGET_ERRORS:
        raise
    except Exception:
        return False


class BoxSession:
    """Hold one box from a source for the caller's commands and checks, and give it back once."""

    def __init__(self, source: BoxSource[SandboxSession], box: SandboxSession) -> None:
        """Bind an acquired box to the source that takes it back.

        Args:
            source: The source that gave the box.
            box: The open box; the caller may also run its own checks on it.
        """
        self.source = source
        self.box = box
        self._released = False
        self._lock = threading.Lock()

    def run(
        self,
        command: str,
        *,
        timeout_seconds: float,
        env: Mapping[str, str] | None = None,
        on_output: OutputSink | None = None,
        sudo: bool = False,
    ) -> CommandResult:
        """Run one command in the box with this call's own timeout.

        Args:
            command: The shell command line.
            timeout_seconds: The limit of this command.
            env: Extra environment for this command only.
            on_output: Receives each piece of output while the command runs.
            sudo: Run as root; only the control plane may ask for it.

        Returns:
            The command's exit code, output and whether the timeout fired.
        """
        options: dict[str, Any] = {}
        # Only the options the caller set reach the box, so a box gets the same call as before the session API.
        if env is not None:
            options["env"] = env
        if on_output is not None:
            options["on_output"] = on_output
        if sudo:
            options["sudo"] = True
        return self.box.run(command, timeout_seconds=timeout_seconds, **options)

    def release(self, healthy: bool) -> None:
        """Give the box back to its source; a second call does nothing.

        Args:
            healthy: False when the box failed, so no later step may use it.

        Raises:
            Exception: The source's release error, raw, including the budget errors.
        """
        with self._lock:
            if self._released:
                return
            # Marked first: a release that raises must not release the box again.
            self._released = True
        self.source.release(self.box, healthy)

    def release_failed(self, what: str) -> None:
        """Give a failed box back as unhealthy, and log only a release failure's type.

        Args:
            what: Names the box in the log line.

        Raises:
            BudgetReached: And the other budget errors of the release, in place of the box's own failure.
        """
        try:
            self.release(healthy=False)
        except _BUDGET_ERRORS:
            # A budget stop must not be lost, so it replaces the box's own failure.
            raise
        except Exception as release_error:
            # Only the type: settlement and ledger texts can name the provider. The caller raises or
            # returns the box's own failure.
            logger.warning("%s did not close cleanly (%s)", what, type(release_error).__name__)

    def __enter__(self) -> BoxSession:
        """Return this session for a ``with`` block that releases the box at its end.

        Returns:
            This session.
        """
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Release the box, as healthy only when the block raised nothing.

        Args:
            exc_type: The type of the block's exception, if any.
            exc: The block's exception, if any.
            traceback: The block's traceback, if any.
        """
        self.release(healthy=exc is None)


@dataclass(frozen=True)
class StepRef:
    """Name one step in the step log: the run, the agent, its place and its generation."""

    run_id: str
    agent_id: str
    seq: int
    execution_generation: int

    @property
    def step_id(self) -> str:
        """Return the step id, ``run_id:agent_id:seq``."""
        return step_id_for(self.run_id, self.agent_id, self.seq)

    @property
    def box_key(self) -> str:
        """Return the key of the agent's box, ``run_id:agent_id``, so two runs with one agent id never share a box."""
        # A run id holds no ":", so the key names one run and one agent.
        return f"{self.run_id}:{self.agent_id}"


class TreeSandbox:
    """Run commands and metrics on stored trees, in boxes from two sources."""

    def __init__(
        self,
        trees: TreeStore,
        step_log: StepLogStore,
        candidates: BoxSource[SandboxSession],
        metrics: BoxSource[SandboxSession],
        *,
        registry: Mapping[str, Mapping[str, str]] | None = None,
        timeout_seconds: float = 600,
    ) -> None:
        """Bind the layer to its stores and box sources.

        Args:
            trees: Reads the input trees and stores the new ones.
            step_log: Gets one step for each finished call.
            candidates: Gives the boxes that run candidate commands.
            metrics: Gives the metric boxes; it must never give a box that a candidate step used.
            registry: The capability registry that the grant lists resolve against.
            timeout_seconds: The limit of one command or metric.

        Raises:
            ValueError: When a runtime box source has another registry, or
                the metric boxes come from a lease that keeps boxes warm.
        """
        self._trees = trees
        self._step_log = step_log
        self._candidates = candidates
        self._metrics = metrics
        self._registry = registry or {}
        self._timeout = timeout_seconds
        if isinstance(metrics, BoxLease) and metrics.keeps_warm:
            # A warm box keeps /tmp, $HOME and stray processes from the last score or candidate,
            # and no wipe clears them on every provider, so each score needs a new box.
            raise ValueError("The metric boxes must come from a lease with keep_warm=False.")
        for source in (candidates, metrics):
            # The layer records the grant list that it resolves, so a box must open with the same registry.
            if isinstance(source, (RuntimeBoxSource, RuntimeBoxLease)) and source.registry != self._registry:
                raise ValueError("A runtime box source must use the same capability registry as the tree sandbox.")

    @staticmethod
    def open_session(
        source: BoxSource[SandboxSession],
        agent_id: str,
        grants: Sequence[Grant],
        *,
        start: Callable[[SandboxSession], None] | None = None,
        what: str = "A tree sandbox box",
    ) -> BoxSession:
        """Acquire one box from a source and start it from a state.

        The session commits no step; a caller that keeps a step log commits
        its own steps. The caller releases the session in a ``finally`` block
        or uses it as a ``with`` block.

        Args:
            source: Gives the box.
            agent_id: The agent or role that uses the box.
            grants: The box's network grants; empty means no network.
            start: Brings the new box to its starting state, for example by
                unpacking a saved state; ``None`` keeps the box as it opened.
            what: Names the box in the log line of a failed release.

        Returns:
            The session that holds the started box.

        Raises:
            ValueError: When the source keeps boxes warm.
            Exception: The source's acquire error or the start's error, raw.
            BudgetReached: And the other budget errors of the release after
                a failed start, in place of the start's own error.
        """
        if isinstance(source, BoxLease) and source.keeps_warm:
            # A session gives the box back with no process scan and keys it by the bare agent id, so a stray
            # process of one session, or of another run's agent with the same id, could reach the next session.
            raise ValueError("A box session needs a source that does not keep boxes warm.")
        session = BoxSession(source, source.acquire(agent_id, grants))
        if start is None:
            return session
        try:
            start(session.box)
        except BaseException:
            session.release_failed(what)
            raise
        return session

    def run(self, step: StepRef, tree_id: str, command: str, grants: Sequence[Grant]) -> tuple[CommandResult, str]:
        """Run one command in a candidate box over a tree.

        Args:
            step: The step this command is.
            tree_id: The workspace tree the command starts from.
            command: The shell command; it runs in the tree's folder.
            grants: The box's network grants.

        Returns:
            The command's result and the tree id after the command. A
            command that fails still returns: its exit code is output.

        Raises:
            GrantDeniedError: When the grants do not resolve.
            BoxFailureError: When the box fails, dies or does not go back;
                retry the step.
            TreeRejectedError: When the box's tree does not pack, is over a
                size limit or is not a valid tree; do not retry.
            FenceLostError: When the run generation is no longer current.
            LookupError: When the run cannot see the tree.
            BudgetReached: And the other budget errors; the run stops.
        """
        grant_list_id = self._grant_list_id(step, grants)
        archive = tree_to_tar(self._trees, step.run_id, tree_id)
        # A fresh-box source closes every box at its release, so only another source needs the scans.
        fresh = isinstance(self._candidates, RuntimeBoxSource) or (
            isinstance(self._candidates, BoxLease) and not self._candidates.keeps_warm
        )

        def work(box: SandboxSession) -> tuple[tuple[CommandResult, str], bool]:
            """Unpack the tree, run the command, store the new tree and say whether the box can serve a next step.

            Args:
                box: The candidate box.

            Returns:
                The command's result and the new tree id, and False when a
                process of the command can still be alive in the box.
            """
            # Before the unpack, so nothing that the unpack starts can join the baseline.
            before = None if fresh else _processes(box)
            self._unpack(box, archive, TREE_DIR)
            result = box.run(f"cd {TREE_DIR} && {command}", timeout_seconds=self._timeout)
            new_tree_id = self._pack(box, step.run_id, TREE_DIR)
            if result.timed_out:
                # The kill reaches only the command's process group, so a process outside it can live on.
                return (result, new_tree_id), False
            if fresh:
                return (result, new_tree_id), True
            after = _processes(box)
            return (result, new_tree_id), before is not None and after is not None and after <= before

        result, new_tree_id = self._in_box(self._candidates, step, grants, work)
        body = {
            "command": command,
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "timed_out": result.timed_out,
        }
        self._commit(step, grant_list_id, body, new_tree_id)
        return result, new_tree_id

    def score(
        self,
        step: StepRef,
        outputs_tree_id: str,
        metric_tree_id: str,
        command: str,
        grants: Sequence[Grant] = METRIC_BOX_GRANTS,
    ) -> float:
        """Run the metric in its own box over the outputs tree.

        The box gets the outputs tree in ``outputs/`` and the metric tree in
        ``metric/``, and nothing else: the candidate tree never enters it.

        Args:
            step: The step this score is.
            outputs_tree_id: The tree of the candidate's outputs.
            metric_tree_id: The tree of the metric's files.
            command: The metric command; its last stdout line is the score.
            grants: The metric box's grants; the default is no network.

        Returns:
            The score.

        Raises:
            GrantDeniedError: When the grants do not resolve.
            BoxFailureError: When the box fails, dies or does not go back;
                retry the step.
            MetricFailedError: When the metric fails or prints no finite number.
            TreeRejectedError: When the outputs tree holds a link; do not retry.
            FenceLostError: When the run generation is no longer current.
            LookupError: When the run cannot see a tree.
            BudgetReached: And the other budget errors; the run stops.
        """
        grant_list_id = self._grant_list_id(step, grants)
        outputs = tree_to_tar(self._trees, step.run_id, outputs_tree_id)
        # A candidate link such as ``answer.txt -> ../metric/expected.txt`` or ``/proc/self/environ``
        # would let the metric read its own hidden files as the candidate's output. A link that stays
        # in ``outputs/`` can still leave it through another link to a folder, so no link enters.
        with tarfile.open(fileobj=io.BytesIO(outputs), mode="r:gz") as archive:
            if any(member.issym() or member.islnk() for member in archive):
                raise TreeRejectedError(step.step_id, "The outputs tree holds a link.")
        metric = tree_to_tar(self._trees, step.run_id, metric_tree_id)

        def work(box: SandboxSession) -> tuple[CommandResult, bool]:
            """Unpack the two trees and run the metric.

            Args:
                box: The metric box.

            Returns:
                The metric command's result, and False when it timed out, so
                its processes can still be alive in the box.
            """
            self._unpack(box, outputs, OUTPUTS_DIR)
            self._unpack(box, metric, METRIC_DIR)
            result = box.run(command, timeout_seconds=self._timeout)
            return result, not result.timed_out

        result = self._in_box(self._metrics, step, grants, work)
        lines = result.stdout.strip().splitlines()
        try:
            if not result.ok or not lines:
                raise ValueError
            value = float(lines[-1])
            # NaN or infinity is no score, and json.dumps would write a non-standard token to the step log.
            if not math.isfinite(value):
                raise ValueError
        except ValueError:
            raise MetricFailedError(step.step_id, f"The metric gave no score (exit code {result.exit_code}).") from None
        self._commit(step, grant_list_id, {"command": command, "score": value}, outputs_tree_id)
        return value

    def _grant_list_id(self, step: StepRef, grants: Sequence[Grant]) -> str:
        """Resolve the grants before any box opens.

        Args:
            step: The step that asks for the grants.
            grants: The grants.

        Returns:
            The grant-list id the step records.

        Raises:
            GrantDeniedError: When the grants do not resolve.
        """
        try:
            # A gpu:<type> grant places the box; it is not a network grant.
            return resolve_grants(network_grants(grants), self._registry).grant_list_id
        except GrantError as error:
            raise GrantDeniedError(step.step_id, str(error)) from error

    def _in_box(
        self,
        source: BoxSource[SandboxSession],
        step: StepRef,
        grants: Sequence[Grant],
        work: Callable[[SandboxSession], tuple[_Result, bool]],
    ) -> _Result:
        """Do one piece of work in a box from a source and give the box back.

        Args:
            source: Gives the box.
            step: The step the work is.
            grants: The box's grants.
            work: What to do in the box; it also returns False when the box
                must not serve another step, so it goes back unhealthy.

        Returns:
            The first value that ``work`` returns.

        Raises:
            GrantDeniedError: When the source refuses the grants.
            BoxFailureError: When the box does not open, fails, dies or does not go back.
            TreeRejectedError: When the box's tree is over a size limit or not a valid tree.
            BudgetReached: And the other budget errors, from the work or the release.
        """
        try:
            # Not open_session, which refuses a warm lease: run() scans the processes of a warm box itself.
            session = BoxSession(source, source.acquire(step.box_key, grants))
        except _BUDGET_ERRORS:
            raise
        except GrantError as error:
            raise GrantDeniedError(step.step_id, str(error)) from error
        except Exception as error:
            raise BoxFailureError(step.step_id, f"The box did not open ({type(error).__name__}).") from error
        healthy = done = False
        try:
            outcome, healthy = work(session.box)
            done = True
        except _BUDGET_ERRORS:
            raise
        except BadArchiveError as error:
            raise TreeRejectedError(step.step_id, str(error)) from error
        except Exception as error:
            raise BoxFailureError(step.step_id, f"The box failed ({type(error).__name__}).") from error
        finally:
            # Also on a BaseException such as a cancel, so the box always goes back.
            try:
                session.release(healthy)
            except _BUDGET_ERRORS:
                # A budget stop wins over the step's own error, so the run stops.
                raise
            except Exception as error:
                if done:
                    # Only the type: the provider's text can name the box.
                    raise BoxFailureError(step.step_id, f"The box did not go back ({type(error).__name__}).") from error
                # The step's own error is the one the caller acts on.
                logger.warning("A failed tree sandbox box did not go back (%s)", type(error).__name__)
        return outcome

    def _unpack(self, box: SandboxSession, archive: bytes, folder: str) -> None:
        """Write an archive into a box and unpack it in an emptied folder.

        Args:
            box: The box.
            archive: A gzip tar archive.
            folder: The folder, relative to the box's working directory.

        Raises:
            RuntimeError: When the archive does not unpack.
        """
        parts = []
        for index, start in enumerate(range(0, len(archive), ARCHIVE_CHUNK_BYTES)):
            name = f"{_IN_DIR}/{folder}.{index:04d}.b64"
            # The broker caps one write request at 16 MiB in total, so each chunk is its own write.
            box.write_files({name: base64.b64encode(archive[start : start + ARCHIVE_CHUNK_BYTES]).decode("ascii")})
            parts.append(name)
        files = " ".join(shlex.quote(name) for name in parts)
        # A source can give back a used box, so old files in the folder must not join the tree.
        result = box.run(
            _clean(
                f"set -e; rm -rf {folder}; mkdir -p {folder}; cat {files} | base64 -d | tar -xzf - -C {folder};"
                f" rm -rf {_IN_DIR}"
            ),
            timeout_seconds=_MOVE_TIMEOUT_SECONDS,
        )
        if not result.ok:
            raise RuntimeError(f"The tree did not unpack (exit code {result.exit_code}).")

    def _pack(self, box: SandboxSession, run_id: str, folder: str) -> str:
        """Store a box folder as a tree.

        Args:
            box: The box.
            run_id: The run that owns the new objects.
            folder: The folder, relative to the box's working directory.

        Returns:
            The folder's tree id.

        Raises:
            RuntimeError: When the folder cannot be read back.
            BadArchiveError: When the folder does not pack, is over a size limit or is not a valid tree.
        """
        result = box.run(
            # macOS tar would add AppleDouble ``._`` files to the tree without COPYFILE_DISABLE.
            # The box checks the archive bytes against the archive limit first, so a normal oversized tree
            # stops in the box.
            # tar writes a file first, so set -e sees its exit code; a pipe would hide it.
            # The broker caps one read at 16 MiB, so the archive goes out in numbered base64 parts,
            # one read each, and the command prints the part count.
            _clean(
                f"set -e; COPYFILE_DISABLE=1 tar -czf {_TAR_FILE} -C {folder} .;"
                f' [ "$(wc -c < {_TAR_FILE})" -le {MAX_ARCHIVE_BYTES} ] || exit {_TOO_LARGE_EXIT};'
                f" rm -rf {_OUT_DIR}; mkdir {_OUT_DIR};"
                f" split -b {ARCHIVE_CHUNK_BYTES} -a 4 {_TAR_FILE} {_OUT_DIR}/raw.;"
                f' i=0; for part in {_OUT_DIR}/raw.*; do base64 < "$part" > {_OUT_DIR}/$i.b64; rm "$part";'
                " i=$((i + 1)); done;"
                " echo $i",
            ),
            timeout_seconds=_MOVE_TIMEOUT_SECONDS,
        )
        if result.exit_code == _TOO_LARGE_EXIT:
            raise BadArchiveError(f"The tree archive is larger than {MAX_ARCHIVE_BYTES} bytes.")
        if not result.ok:
            # The candidate's command controls the box state that the pack reads, and a box or provider
            # failure raises an exception, not an exit code. So a new box would fail the same way.
            raise BadArchiveError(f"The tree did not pack (exit code {result.exit_code}).")
        try:
            count = int(result.stdout.strip())
        except ValueError:
            count = 0
        # The candidate controls the box, so the count is checked like any other box output.
        if not 0 < count <= _MAX_OUT_PARTS:
            raise BadArchiveError("The tree pack did not report a valid part count.")
        texts = []
        size = 0
        for index in range(count):
            text = box.read_file(f"{_OUT_DIR}/{index}.b64")
            if text is None:
                raise RuntimeError(f"Tree part {index} could not be read back.")
            size += len(text)
            # The candidate controls the box, so the parts can grow after the check.
            if size > _MAX_OUT_FILE_BYTES:
                raise BadArchiveError(f"The tree archive is larger than {_MAX_OUT_FILE_BYTES} base64 bytes.")
            texts.append(text)
        return tar_to_tree(self._trees, run_id, b"".join(base64.b64decode(text) for text in texts))

    def _commit(self, step: StepRef, grant_list_id: str, body: dict[str, Any], tree_id: str) -> None:
        """Commit one command step and wait until it is durable behind the fence.

        The wait means a worker whose generation is stale never gets a
        normal result back: the fence verdict of this very step comes first.

        Args:
            step: The step.
            grant_list_id: The grant list the step ran with.
            body: The step body.
            tree_id: The tree the step names.

        Raises:
            FenceLostError: When this step or an earlier step of the agent failed the fence.
        """
        try:
            step_id = self._step_log.commit(
                run_id=step.run_id,
                agent_id=step.agent_id,
                seq=step.seq,
                kind="command",
                execution_generation=step.execution_generation,
                grant_list_id=grant_list_id,
                body=body,
                tree_id=tree_id,
            )
            self._step_log.durable(step_id, step.execution_generation)
        except StepFenceError as error:
            raise FenceLostError(step.step_id, f"The run generation changed at step {error.step_id}.") from error
