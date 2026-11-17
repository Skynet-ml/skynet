"""Run agent steps and metrics on tree ids, each in its own box (SKY-718).

:meth:`TreeSandbox.run` runs one command in a candidate box over a stored
tree and stores the box's tree after the command. :meth:`TreeSandbox.score`
runs the metric in a separate box that gets only the outputs tree and the
metric tree, so no candidate code reaches it. Each call commits one step to
the step log. :class:`RuntimeBoxSource` is the fresh-box-per-call source:
it opens a new box for each acquire and closes it at release.
"""

from __future__ import annotations

import base64
import logging
import shlex
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

from ....billing.budgets import BudgetError
from ....billing.operation_pricing import UnpricedOperationError
from ....billing.runtime import UsagePendingError
from ....billing.signals import BudgetReached
from ....sandbox.grants import METRIC_BOX_GRANTS, Grant, GrantError, resolve_grants
from ....sandbox.trees import (
    BoxFailureError,
    BoxSource,
    FenceLostError,
    GrantDeniedError,
    MetricFailedError,
    tar_to_tree,
    tree_to_tar,
)
from ....storage.step_log_store import StepFenceError, StepLogStore, step_id_for
from ....storage.tree_store import TreeStore
from .repo_tree import ARCHIVE_CHUNK_BYTES
from .sandbox_contract import CommandResult, SandboxRuntime, SandboxSession, SandboxSpec

logger = logging.getLogger(__name__)

# Budget signals stop the run; they are not box failures to retry.
_BUDGET_ERRORS = (BudgetReached, BudgetError, UsagePendingError, UnpricedOperationError)
TREE_DIR = "tree"
OUTPUTS_DIR = "outputs"
METRIC_DIR = "metric"
_IN_DIR = ".tree-in"
_OUT_FILE = ".tree-out.b64"
_MOVE_TIMEOUT_SECONDS = 600

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
        """
        try:
            box.close()
        except Exception as error:
            # Only the type: the provider's text can name the box.
            logger.warning("A tree sandbox box did not close (%s)", type(error).__name__)


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
            metrics: Gives the metric boxes; never the same as ``candidates``.
            registry: The capability registry that the grant lists resolve against.
            timeout_seconds: The limit of one command or metric.
        """
        self._trees = trees
        self._step_log = step_log
        self._candidates = candidates
        self._metrics = metrics
        self._registry = registry or {}
        self._timeout = timeout_seconds

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
            BoxFailureError: When the box fails or dies; retry the step.
            FenceLostError: When the run generation is no longer current.
            LookupError: When the run cannot see the tree.
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
            BoxFailureError: When the box fails or dies; retry the step.
            MetricFailedError: When the metric fails or prints no number.
            FenceLostError: When the run generation is no longer current.
            LookupError: When the run cannot see a tree.
        """
        grant_list_id = self._grant_list_id(step, grants)
        outputs = tree_to_tar(self._trees, step.run_id, outputs_tree_id)
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
            BoxFailureError: When the box does not open, fails or dies.
        """
        try:
            box = source.acquire(step.agent_id, grants)
        except _BUDGET_ERRORS:
            raise
        except GrantError as error:
            raise GrantDeniedError(step.step_id, str(error)) from error
        except Exception as error:
            raise BoxFailureError(step.step_id, f"The box did not open ({type(error).__name__}).") from error
        try:
            outcome = work(box)
        except _BUDGET_ERRORS:
            source.release(box, healthy=False)
            raise
        except Exception as error:
            source.release(box, healthy=False)
            raise BoxFailureError(step.step_id, f"The box failed ({type(error).__name__}).") from error
        source.release(box, healthy=True)
        return outcome

    def _unpack(self, box: SandboxSession, archive: bytes, folder: str) -> None:
        """Write an archive into a box and unpack it in a folder.

        Args:
            box: The box.
            archive: A gzip tar archive.
            folder: The folder, relative to the box's working directory.

        Raises:
            RuntimeError: When the archive does not unpack.
        """
        parts = {
            f"{_IN_DIR}/{folder}.{index:04d}.b64": base64.b64encode(
                archive[start : start + ARCHIVE_CHUNK_BYTES]
            ).decode("ascii")
            for index, start in enumerate(range(0, len(archive), ARCHIVE_CHUNK_BYTES))
        }
        box.write_files(parts)
        files = " ".join(shlex.quote(name) for name in parts)
        result = box.run(
            f"set -e; mkdir -p {folder}; cat {files} | base64 -d | tar -xzf - -C {folder}; rm -rf {_IN_DIR}",
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
        """
        result = box.run(
            # macOS tar would add AppleDouble ``._`` files to the tree without COPYFILE_DISABLE.
            f"set -e; COPYFILE_DISABLE=1 tar -czf - -C {folder} . | base64 > {_OUT_FILE}",
            timeout_seconds=_MOVE_TIMEOUT_SECONDS,
        )
        text = box.read_file(_OUT_FILE) if result.ok else None
        if text is None:
            raise RuntimeError(f"The tree could not be read back (exit code {result.exit_code}).")
        return tar_to_tree(self._trees, run_id, base64.b64decode(text))

    def _commit(self, step: StepRef, grant_list_id: str, body: dict[str, Any], tree_id: str) -> str:
        """Commit one command step.

        Args:
            step: The step.
            grant_list_id: The grant list the step ran with.
            body: The step body.
            tree_id: The tree the step names.

        Returns:
            The step id.

        Raises:
            FenceLostError: When an earlier step of the agent failed the fence.
        """
        try:
            return self._step_log.commit(
                run_id=step.run_id,
                agent_id=step.agent_id,
                seq=step.seq,
                kind="command",
                execution_generation=step.execution_generation,
                grant_list_id=grant_list_id,
                body=body,
                tree_id=tree_id,
            )
        except StepFenceError as error:
            raise FenceLostError(step.step_id, f"The run generation changed at step {error.step_id}.") from error
