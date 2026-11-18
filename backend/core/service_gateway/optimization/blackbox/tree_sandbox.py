"""Run agent steps and metrics on tree ids, each in its own box (SKY-718).

:meth:`TreeSandbox.run` runs one command in a candidate box over a stored
tree and stores the box's tree after the command. :meth:`TreeSandbox.score`
runs the metric in a separate box that gets only the outputs tree and the
metric tree, so no candidate code reaches it. Each call commits one step to
the step log and returns only once that step is durable behind the run's
fence. :class:`RuntimeBoxSource` is the fresh-box-per-call source:
it opens a new box for each acquire and closes it at release.

:meth:`TreeSandbox.open_session` is the session API (SKY-785). It acquires
one box from a :class:`BoxSource`, starts it from a state, and gives the
caller a :class:`BoxSession`: commands with their own ``env``, ``on_output``,
``sudo`` and timeout, the box itself for checks, and one release. It needs no
tree store and commits no step, so a caller such as ``RepoScorer`` that keeps
its own records uses the same box path as :meth:`TreeSandbox.run` and
:meth:`TreeSandbox.score`.
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
from ....sandbox.grants import METRIC_BOX_GRANTS, Grant, GrantError, resolve_grants
from ....sandbox.trees import (
    MAX_ARCHIVE_BYTES,
    BadArchiveError,
    BoxFailureError,
    BoxSource,
    FenceLostError,
    GrantDeniedError,
    MetricFailedError,
    TreeRejectedError,
    TreeSandboxError,
    tar_to_tree,
    tree_to_tar,
)
from ....storage.step_log_store import StepFenceError, StepLogStore, step_id_for
from ....storage.tree_store import TreeStore
from .repo_tree import ARCHIVE_CHUNK_BYTES
from .sandbox_contract import CommandResult, OutputSink, SandboxRuntime, SandboxSession, SandboxSpec

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
        try:
            box.close()
        except _BUDGET_ERRORS:
            raise
        except Exception as error:
            # Only the type: the provider's text can name the box.
            logger.warning("A tree sandbox box did not close (%s)", type(error).__name__)


class StepCommittedError(TreeSandboxError):
    """Signal a step that committed, then failed in its durable wait: the next step must take the next seq."""


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
            ValueError: When a runtime box source has another registry.
        """
        self._trees = trees
        self._step_log = step_log
        self._candidates = candidates
        self._metrics = metrics
        self._registry = registry or {}
        self._timeout = timeout_seconds
        for source in (candidates, metrics):
            # The layer records the grant list that it resolves, so a box must open with the same registry.
            if isinstance(source, RuntimeBoxSource) and source.registry != self._registry:
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
            Exception: The source's acquire error or the start's error, raw.
            BudgetReached: And the other budget errors of the release after
                a failed start, in place of the start's own error.
        """
        session = BoxSession(source, source.acquire(agent_id, grants))
        if start is None:
            return session
        try:
            start(session.box)
        except BaseException:
            session.release_failed(what)
            raise
        return session

    def run(
        self,
        step: StepRef,
        tree_id: str,
        command: str,
        grants: Sequence[Grant],
        *,
        refuse: Callable[[str], bool] | None = None,
    ) -> tuple[CommandResult, str]:
        """Run one command in a candidate box over a tree.

        Args:
            step: The step this command is.
            tree_id: The workspace tree the command starts from.
            command: The shell command; it runs in the tree's folder.
            grants: The box's network grants.
            refuse: Tells, before the step commits, whether the tree after the command must not be kept.

        Returns:
            The command's result and the tree id after the command. A
            command that fails still returns: its exit code is output.

        Raises:
            GrantDeniedError: When the grants do not resolve.
            BoxFailureError: When the box fails, dies or does not go back;
                retry the step.
            TreeRejectedError: When the box's tree does not pack, is over a
                size limit, is not a valid tree or is refused; do not retry.
            FenceLostError: When the run generation is no longer current.
            StepCommittedError: When the step committed and then did not become durable.
            LookupError: When the run cannot see the tree.
            BudgetReached: And the other budget errors; the run stops.
        """
        grant_list_id = self._grant_list_id(step, grants)
        archive = tree_to_tar(self._trees, step.run_id, tree_id)

        def work(box: SandboxSession) -> tuple[CommandResult, str]:
            """Unpack the tree, run the command and store the new tree.

            Args:
                box: The candidate box.

            Returns:
                The command's result and the new tree id.
            """
            self._unpack(box, archive, TREE_DIR)
            result = box.run(f"cd {TREE_DIR} && {command}", timeout_seconds=self._timeout)
            return result, self._pack(box, step.run_id, TREE_DIR)

        result, new_tree_id = self._in_box(self._candidates, step, grants, work)
        if refuse is not None and refuse(new_tree_id):
            # The message names no path, so it does not show which path is refused.
            raise TreeRejectedError(step.step_id, "The command wrote a path that it may not write.")
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
            StepCommittedError: When the step committed and then did not become durable.
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

        def work(box: SandboxSession) -> CommandResult:
            """Unpack the two trees and run the metric.

            Args:
                box: The metric box.

            Returns:
                The metric command's result.
            """
            self._unpack(box, outputs, OUTPUTS_DIR)
            self._unpack(box, metric, METRIC_DIR)
            return box.run(command, timeout_seconds=self._timeout)

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
            return resolve_grants(grants, self._registry).grant_list_id
        except GrantError as error:
            raise GrantDeniedError(step.step_id, str(error)) from error

    def _in_box(
        self,
        source: BoxSource[SandboxSession],
        step: StepRef,
        grants: Sequence[Grant],
        work: Callable[[SandboxSession], _Result],
    ) -> _Result:
        """Do one piece of work in a box from a source and give the box back.

        Args:
            source: Gives the box.
            step: The step the work is.
            grants: The box's grants.
            work: What to do in the box.

        Returns:
            What ``work`` returns.

        Raises:
            GrantDeniedError: When the source refuses the grants.
            BoxFailureError: When the box does not open, fails, dies or does not go back.
            TreeRejectedError: When the box's tree is over a size limit or not a valid tree.
            BudgetReached: And the other budget errors, from the work or the release.
        """
        try:
            session = self.open_session(source, step.agent_id, grants)
        except _BUDGET_ERRORS:
            raise
        except GrantError as error:
            raise GrantDeniedError(step.step_id, str(error)) from error
        except Exception as error:
            raise BoxFailureError(step.step_id, f"The box did not open ({type(error).__name__}).") from error
        healthy = False
        try:
            outcome = work(session.box)
            healthy = True
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
                if healthy:
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
            f"set -e; rm -rf {folder}; mkdir -p {folder}; cat {files} | base64 -d | tar -xzf - -C {folder}; rm -rf {_IN_DIR}",
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
            # The box checks the archive bytes against the archive limit first, so a normal oversized tree stops in the box.
            # tar writes a file first, so set -e sees its exit code; a pipe would hide it.
            # The tree store refuses a ``.git`` part in any letter case (SKY-810), so a command that runs
            # ``git init`` would fail the step; the box's own ``.git`` is left out, as harness snapshots do.
            # The broker caps one read at 16 MiB, so the archive goes out in numbered base64 parts,
            # one read each, and the command prints the part count.
            f"set -e; COPYFILE_DISABLE=1 tar -czf {_TAR_FILE} --exclude='[.][gG][iI][tT]' -C {folder} .;"
            f' [ "$(wc -c < {_TAR_FILE})" -le {MAX_ARCHIVE_BYTES} ] || exit {_TOO_LARGE_EXIT};'
            f" rm -rf {_OUT_DIR}; mkdir {_OUT_DIR}; split -b {ARCHIVE_CHUNK_BYTES} -a 4 {_TAR_FILE} {_OUT_DIR}/raw.;"
            f' i=0; for part in {_OUT_DIR}/raw.*; do base64 < "$part" > {_OUT_DIR}/$i.b64; rm "$part"; i=$((i + 1)); done;'
            " echo $i",
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
            StepCommittedError: When the step committed and its durable wait failed another way.
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
            try:
                self._step_log.durable(step_id, step.execution_generation)
            except StepFenceError:
                raise
            except Exception as error:
                # The commit returned, so the seq is taken: a retry at it would get the old id and store nothing.
                raise StepCommittedError(step.step_id, f"The step committed, then {type(error).__name__}.") from error
        except StepFenceError as error:
            raise FenceLostError(step.step_id, f"The run generation changed at step {error.step_id}.") from error
