"""Run engine loops in the worker, and open boxes only for steps (SKY-719).

The engine loop runs in the worker, inside the run and agent actors. A model
call runs with no box open and commits one ``model`` step. A command or a
metric opens a box only through :meth:`TreeSandbox.run` and
:meth:`TreeSandbox.score`, and each commits one ``command`` step. So every
model response and every command is one step in the agent's log. A failed
model call or box step also commits one step, with no cost.

A resume is a replay. When an agent starts, the loop reads the agent's
committed steps. The engine runs again from its start, and each call that a
committed step already answers gets the recorded answer: no model call and no
box. The first call past the log does real work again. So an engine on this
host must be deterministic, given its step results. A call that does not
match its committed step raises :class:`StepDivergedError`. A run step matches
on its command and its grant list, and a score step on its command, its
outputs tree and its grant list. The tree sandbox writes those step bodies,
and they do not record a run step's input tree or a score step's metric tree.
So a replay cannot see an engine that sends the same command over another
input tree, or that scores the same outputs with another metric tree.

A loop that cannot go on raises :class:`LoopStoppedError`: its replay
diverged, its run lost the fence, no lease is left, the budget refused the
work, a model call cost more than its bound, the run or the step log is
closed, a step failed outside its own work, or an agent could not open: its
committed log has a gap or the step log read fails. The error is a
:class:`ScorerAbortError`, so the eval server and Auto's lanes pass it
through and stop the run. The loop also keeps the error: an engine that
catches it and calls again gets it again, before any work.

A step's own failure is no stop: a failed model call, or a box step that
raises one of the box errors, commits a record that a replay raises again.
Every other error of a step is a stop. It can come after the step committed,
for example from a settle or the durable wait, so the live engine must not
act on it: a resume would replay the committed result instead. A stop gives
the live engine no result at all, so the live run and its resume agree.

Close the run through :meth:`EngineLoop.close`, or straight on the run actor.
Each step checks the run's closed state before its paid work, so no box opens
in a closed run. The step log has no public closed state: when only the step
log closes, one box step runs, fails its commit, and the loop keeps the stop.

Each step checks that the worker still owns the run before its work starts.
The agents of the Auto lanes run in parallel, and each one checks on its own.
So after the fence moves, a fenced worker can make at most one call for each
running agent: the call that was already past that agent's check.

The engine API is small. :meth:`EngineLoop.call_model` takes optional tool
definitions and gives a :class:`ModelReply` with the text, the tool calls and
the cost; the tools are part of the prompt hash that a replay matches.
:meth:`EngineLoop.model` is the same call with no tools, and gives only the
text. :meth:`EngineLoop.put_files`, :meth:`EngineLoop.read_file` and
:meth:`EngineLoop.list_files` write, read and list trees with no box. A tree
write is no step: a tree id is the hash of its content, so a resume that
writes the same files gets the same tree. The loop gets the run's hidden-path
hashes once, when it is built, and ``read_file``, ``list_files`` and every
candidate box leave those paths out; no caller can turn that off. The loop
always hides ``.optimize/exploits`` the same way, as the native runtime hides
the exploit suite from the proposer. A tree with a symlink that is, lies
below or holds a hidden path stops the loop at its first read, listing or run,
and a seed tree stops it in ``context``: the filter matches paths, so the
link's target would stay visible. When the run hides paths of its own, a link
loop or a link that names an absolute path or leaves the tree stops the loop
too, since the loop cannot tell what it reaches. A tree that a ``run`` step
gave, live or replayed, or that ``put_files`` wrote over such a tree, is not
walked: a candidate box never gets a hidden file, so these trees hold no
hidden content, and their links can show none. ``put_files`` refuses to
write, replace or remove a hidden path, and a ``run`` whose command writes one
fails with :class:`TreeRejectedError`, as a box error that a replay raises
again. A refusal confirms that a guessed path is hidden, but never gives its
content.
:meth:`EngineLoop.remaining_cost_usd`
commits the budget room that it reads as a step with no cost, as a durable
workflow step does, and a replay gives the committed room. An engine that
stops on the room then stops at the same place on a resume.
:meth:`EngineLoop.context` puts these calls, ``run`` and the caller's
:class:`RepoSeed` on the :class:`EngineContext`. The engine's seed names the
seed tree without its hidden paths. The raw seed tree id is a hash of the
hidden content too, so the engine never gets it.

Each Auto lane is its own agent. The ``lane`` usage tag already follows a lane
into the threads that it starts, so the tag names the agent. Calls outside a
lane go to the root agent. The steps of one agent run one at a time, in the
order of its log.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar

from sqlalchemy.engine import Engine

from ..billing.budget_amounts import cent_units
from ..billing.budgets import BudgetError
from ..billing.operation_pricing import UnpricedOperationError
from ..billing.runtime import UsagePendingError
from ..billing.usage_tags import LANE_KEY, current_tags
from ..constants import HARDENING_EXPLOITS_DIR
from ..sandbox.grants import METRIC_BOX_GRANTS, Grant, GrantError, resolve_grants
from ..sandbox.trees import (
    BoxFailureError,
    BoxSource,
    FenceLostError,
    GrantDeniedError,
    MetricFailedError,
    TreeRejectedError,
)
from ..service_gateway.optimization.blackbox.harness_bridge import hashed_kind
from ..service_gateway.optimization.blackbox.harness_bridge import hidden_hashes as path_hashes
from ..service_gateway.optimization.blackbox.protocol import (
    Candidate,
    EngineContext,
    EvalServer,
    ModelReply,
    ScorerAbortError,
    SideInfo,
    ToolDefinition,
)
from ..service_gateway.optimization.blackbox.sandbox_contract import CommandResult, SandboxSession
from ..service_gateway.optimization.blackbox.tree_sandbox import (
    RuntimeBoxSource,
    StepCommittedError,
    StepRef,
    TreeSandbox,
)
from ..storage.step_log_store import StepFenceError, step_id_for
from ..storage.tree_store import TreeEntry, TreeStore
from .actors import Agent, LeaseExhaustedError, Run, StepResult
from .step_recovery import last_committed_step, still_owns_run

ROOT_AGENT_ID = "loop"
# Linux follows at most 40 symlinks in one path lookup (MAXSYMLINKS), so a longer chain fails in a box too.
_MAX_LINK_HOPS = 40
# Links can reach a path count that grows fast with their nesting, so a tree that reaches more stops the loop.
_MAX_LINKED_PATHS = 100_000
# A box step that fails with one of these still commits a record, so a replay raises it at the same place.
_BOX_ERRORS = {
    error.__name__: error for error in (BoxFailureError, GrantDeniedError, MetricFailedError, TreeRejectedError)
}
# The budget errors include the settle's BudgetBoundExceededError and BudgetFundingLostError. The tree sandbox
# lets them through so the run stops, but the eval server would record them as metric errors.
_STOP_ERRORS = (
    FenceLostError,
    StepFenceError,
    LeaseExhaustedError,
    BudgetError,
    UsagePendingError,
    UnpricedOperationError,
)

Prompt = str | list[dict[str, Any]]
ModelCall = Callable[[Prompt, list[ToolDefinition] | None], ModelReply]
StepScorer = Callable[["EngineLoop", Candidate, Any], tuple[float | None, SideInfo]]
_Outcome = TypeVar("_Outcome")


class LoopStoppedError(ScorerAbortError):
    """The loop cannot go on.

    Its replay diverged, its run lost the fence, no lease is left, the budget refused the work, a model call
    cost more than its bound, the run or the step log is closed, a step failed outside its own work, or an agent
    could not open because its committed log has a gap or the step log read fails.
    """


class _LinkLeavesTreeError(Exception):
    """A symlink names an absolute path, or a path that leaves the tree, which the loop cannot check."""


class StepDivergedError(LoopStoppedError):
    """A replayed engine asked for a step that its committed log does not hold at that place."""


class ModelCallFailedError(RuntimeError):
    """A model call failed; its committed step gives the same failure on a replay."""

    def __init__(self, error_type: str) -> None:
        """Name the failure by its exception class only.

        The step log keeps the class and not the message: a provider message can hold a route key, a URL
        with a token or response text, and the loop does not know the secret values that a redaction needs.
        The live error and the replayed error give the same text, so a deterministic engine sees no change.

        Args:
            error_type: The class name of the model call's exception.
        """
        super().__init__(f"The model call failed ({error_type}).")
        self.error_type = error_type


def _reply(record: dict[str, Any]) -> ModelReply:
    """Return the reply that a committed model step holds.

    Args:
        record: The committed step.

    Returns:
        Its text, its cost and its tool calls.
    """
    body = record["body"]
    return ModelReply(str(body["text"]), Decimal(body["cost_cents"]), list(body.get("tool_calls", [])))


def _call(record: dict[str, Any]) -> str:
    """Return the loop call that a committed step answers.

    Args:
        record: The committed step.

    Returns:
        ``model``, ``run``, ``score`` or ``room``.
    """
    if record["kind"] == "model":
        return "model"
    body = record["body"]
    return str(body.get("call", "score" if "score" in body else "run"))


@dataclass
class _AgentLog:
    """One agent of the loop and the committed steps that its replay still has to give back."""

    agent: Agent
    replay: deque[dict[str, Any]]
    lock: threading.Lock = field(default_factory=threading.Lock)


class EngineLoop:
    """Host engine loops in the worker: model calls with no box, commands and metrics in boxes."""

    def __init__(
        self,
        run: Run,
        *,
        engine: Engine,
        trees: TreeStore,
        candidates: BoxSource[SandboxSession],
        metrics: RuntimeBoxSource,
        model: ModelCall,
        model_max_cents: Decimal | int | str,
        run_dir: Path,
        registry: Mapping[str, Mapping[str, str]] | None = None,
        hidden_hashes: Mapping[str, str] | None = None,
    ) -> None:
        """Bind the loop to its run and its step layers.

        Args:
            run: The run actor; each agent of the loop is one of its agents.
            engine: Engine with the step log and job tables, for the resume read and the owner check.
            trees: The tree store of the run's step log.
            candidates: Gives the boxes that run candidate commands.
            metrics: Opens a new metric box for each score, so no score runs in a box that another step used.
            model: Calls the model with a prompt and the tool definitions (``None`` for no tools), and returns its
                reply with the cost in cents.
            model_max_cents: The most that one model call can cost; at most one lease.
            run_dir: The loop's local folder.
            registry: The capability registry that the grant lists resolve against.
            hidden_hashes: The run's hidden-path hashes, as in :attr:`RepoSeed.hidden_hashes`. Every candidate
                box, file read and file listing of the loop leaves these paths out, and ``.optimize/exploits``
                too; no call can turn that off. A tree must have no symlink that is, lies below or holds one of
                these paths. When these hashes are not empty, a tree that no run step gave must also have no
                symlink loop and no symlink that names an absolute path or leaves the tree. The loop stops at the
                first read, listing or run of such a tree, or in :meth:`context` for a seed tree.

        Raises:
            TypeError: When ``metrics`` is not a :class:`RuntimeBoxSource`.
            ValueError: When ``model_max_cents`` is below 0, above one lease, or not an amount the ledger can hold
                exactly, so no model call could ever start.
        """
        # Each score must run in its own new box: a pooled box could hold files of an earlier step.
        if not isinstance(metrics, RuntimeBoxSource):
            raise TypeError(f"metrics must be a RuntimeBoxSource, not {type(metrics).__name__}")
        bound = Decimal(str(model_max_cents))
        if bound < 0 or bound > run.chunk_cents:
            raise ValueError(f"model_max_cents {model_max_cents} is not between 0 and one lease of {run.chunk_cents}")
        # Agent.step refuses a bound that the ledger cannot hold on every call, so the loop refuses it once here.
        cent_units(bound)
        self._run = run
        self._engine = engine
        self._trees = trees
        self._registry = registry or {}
        self._seed_hidden = dict(hidden_hashes or {})
        # The native runtime hides the exploit suite from the proposer (a scorer-only path), so the loop hides it
        # too, also when a run's hidden hashes leave it out.
        exploits = path_hashes([HARDENING_EXPLOITS_DIR])
        self._hidden = {**exploits, **self._seed_hidden}
        # Only the run's own hidden paths make the walk strict: with the exploit suite alone, a repository's link
        # loops and absolute links (a checked-in venv) would stop every run.
        self._strict_links = bool(self._seed_hidden.keys() - exploits.keys())
        self._link_checked: set[str] = set()
        # Trees that a run step gave, live or replayed, and trees that put_files wrote over one of them. A candidate
        # box gets no hidden file and a run output with a hidden path is refused, so these trees hold no hidden
        # content and are not walked. A replay adds the same trees in the same order as the live run.
        self._run_outputs: set[str] = set()
        # The loop builds its sandbox, so the sandbox uses the run's step log and the loop's registry.
        self._sandbox = TreeSandbox(trees, run.step_log, candidates, metrics, registry=self._registry)
        self._model = model
        self._model_max_cents = bound
        self.run_dir = run_dir
        self._lock = threading.Lock()
        self._logs: dict[str, _AgentLog] = {}
        self._opening: dict[str, threading.Lock] = {}
        self._stopped: LoopStoppedError | None = None

    def context(self, **fields: Any) -> EngineContext:
        """Return the engine context of the loop: each model call and each command commits a step.

        Concurrency is 1: a step's place in the log must not depend on thread timing. ``read_file``,
        ``list_files`` and ``run_command`` leave out the loop's hidden paths, as :meth:`server` does. The
        context's ``repo_seed`` names the seed tree without its hidden paths, and keeps the seed's path rules.

        Args:
            **fields: Other :class:`EngineContext` fields, such as the run's ``repo_seed``.

        Returns:
            The context; its ``repo_seed`` names the visible seed tree, the same id when nothing is hidden.

        Raises:
            ValueError: When the ``repo_seed`` hides other paths than the loop does.
            LoopStoppedError: When a symlink of the seed tree could show a hidden path, as in :meth:`read_file`;
                the loop keeps it, so a bad seed stops the loop before the engine starts.
            LookupError: When the run cannot see the seed tree or one of its folders.
        """
        seed = fields.get("repo_seed")
        # One set of hidden paths for the whole loop: a seed with other hashes would make the context and the
        # scorer of server() disagree on what a candidate box can read.
        if seed is not None and dict(seed.hidden_hashes) != self._seed_hidden:
            raise ValueError("the repo_seed hides other paths than the engine loop")
        if seed is not None:
            # The raw id is a content hash of the hidden files too, so an engine could test a guess against it.
            fields["repo_seed"] = replace(seed, tree_id=self._visible_tree(self._check_links(seed.tree_id)))
        return EngineContext(
            reflection_lm=self.model,
            run_dir=str(self.run_dir),
            concurrency=1,
            remaining_cost_usd=self.remaining_cost_usd,
            call_model=self.call_model,
            run_command=self.run,
            put_files=self.put_files,
            read_file=self.read_file,
            list_files=self.list_files,
            **fields,
        )

    def server(self, scorer: StepScorer, max_evals: int) -> EvalServer:
        """Return an eval server whose scorer runs its work through :meth:`run` and :meth:`score`.

        Args:
            scorer: Scores ``(loop, candidate, case)``; it may open a box only through the loop.
            max_evals: The most scorer calls.

        Returns:
            The eval server.
        """

        def evaluate(candidate: Candidate, case: Any) -> tuple[float | None, SideInfo]:
            """Score one case on the loop.

            Args:
                candidate: The version to score.
                case: The case.

            Returns:
                The score and the side information.
            """
            return scorer(self, candidate, case)

        return EvalServer(evaluate, max_evals=max_evals)

    def close(self) -> None:
        """Stop the loop, then close its run, so no later step opens a box or calls the model.

        Raises:
            Exception: The first error of :meth:`Run.close`; the loop stays stopped.
        """
        self._stop(LoopStoppedError(f"Run {self._run.run_id} is closed."))
        self._run.close()

    def model(self, prompt: Prompt) -> str:
        """Call the model with no tools, as :meth:`call_model` does, and return only the reply text.

        Args:
            prompt: The prompt text or messages.

        Returns:
            The reply text.
        """
        return self.call_model(prompt).text

    def call_model(self, prompt: Prompt, tools: list[ToolDefinition] | None = None) -> ModelReply:
        """Call the model with no box open, and commit the reply as one ``model`` step.

        Both shapes are flat and provider-neutral. A tool definition is ``{"name", "description",
        "input_schema"}`` (:class:`ToolDefinition`). A returned tool call is ``{"id", "name", "arguments"}``
        (:class:`ToolCall`), with ``arguments`` as a dict, or as raw text when it is not a JSON object. The
        model client converts with :func:`openai_tools` and :func:`tool_calls_from_openai`.

        Args:
            prompt: The prompt text or messages.
            tools: The tool definitions that the model may call; ``None`` for no tools.

        Returns:
            The reply, its tool calls and its cost in cents as the ledger holds it; on a replay, the committed
            reply, with no model call.

        Raises:
            StepDivergedError: When the committed step at this place is not this call, also with other tools.
            LoopStoppedError: When the loop stopped, the prompt or the tools are not JSON, the run is fenced or
                closed, no lease or budget covers the call, the call cost more than its bound, or the agent cannot
                open.
            ModelCallFailedError: When the model call fails; a replay raises a committed failure again.
        """
        with self._stops():
            # A call with no tools keeps the plain prompt hash, so the steps that a plain call committed still match.
            hashed = prompt if tools is None else {"prompt": prompt, "tools": tools}
            try:
                digest = hashlib.sha256(json.dumps(hashed, sort_keys=True).encode()).hexdigest()
            except (TypeError, ValueError) as error:
                # A prompt that does not hash fails the same way on a replay, so it is a kept stop, not a step.
                raise LoopStoppedError(f"The prompt cannot be hashed: {type(error).__name__}: {error}") from error
            log = self._log()
            with log.lock:
                record = self._replayed(log, "model", {"prompt_sha256": digest})
                if record is not None:
                    return _reply(record)
                self._check_owner()
                replies: list[ModelReply] = []
                failures: list[Exception] = []

                def work() -> StepResult:
                    """Call the model, and turn its failure into a step with no cost.

                    Returns:
                        The reply or the failure as a step body, and its cost.
                    """
                    try:
                        reply = self._model(prompt, tools)
                    except _STOP_ERRORS:
                        raise
                    except Exception as error:
                        failures.append(error)
                        return StepResult({"prompt_sha256": digest, "error": type(error).__name__}, Decimal(0))
                    replies.append(reply)
                    body: dict[str, Any] = {"prompt_sha256": digest, "text": reply.text}
                    if reply.tool_calls:
                        body["tool_calls"] = reply.tool_calls
                    return StepResult(body, reply.cost_cents)

                try:
                    step_id = log.agent.step("model", self._model_max_cents, work)
                    # The engine acts on the reply only once its step can never be lost, so a resume never
                    # repeats it.
                    self._durable(step_id)
                    # The ledger rounds the cost up, so the live call reads the committed cost that a replay gives.
                    committed = None if failures else self._run.step_log.get_step(step_id)
                except Exception as error:
                    # The work catches the model's own failures, so any error here is the step's frame: a
                    # closed run or step log, a cost above the bound, a budget stop, or a settle or durable
                    # failure after the commit. The step may be committed, so the engine gets no result.
                    raise LoopStoppedError(f"{type(error).__name__}: {error}") from error
                if failures:
                    raise ModelCallFailedError(type(failures[0]).__name__) from failures[0]
                if committed is None:
                    raise LoopStoppedError(f"Step {step_id} is durable but cannot be read.")
                return _reply(committed)

    def put_files(self, files: Mapping[str, bytes], base_tree_id: str | None = None) -> str:
        """Write files over a tree with no box, and return the new tree's id.

        This is no step: it commits nothing, opens no box and costs nothing. The tree id is the hash of the
        tree's content, so the same files over the same base give the same id, also on a resume. A hidden path
        cannot be written, replaced or removed: a tree id would otherwise confirm a guess of its content. The
        refusal confirms that a guessed path is hidden, but it gives no hidden content. A tree over a base that a
        run step gave is not walked for symlinks, as its base is not. The loop does not enforce the editable,
        read-only and constrained paths of a :class:`RepoSeed`; the caller enforces them where it accepts a
        candidate.

        Args:
            files: The file bytes by ``/``-separated path; a file keeps an existing executable mode.
            base_tree_id: The tree to start from; ``None`` starts from an empty tree.

        Returns:
            The new tree id.

        Raises:
            ValueError: When a path is empty, holds an empty, ``.`` or ``..`` part, or is both a file and a folder;
                or when a write would change a hidden path. The message names only the written path, so it does not
                name another hidden path.
            LookupError: When the run cannot see the base tree or one of its folders.
            LoopStoppedError: When the loop stopped; it writes nothing.
        """
        with self._stops():
            for path in files:
                if hashed_kind(self._hidden, path) is not None:
                    raise ValueError(f"path {path} may not be changed")
            tree_id = self._put_tree(base_tree_id, {tuple(path.split("/")): data for path, data in files.items()})
            if base_tree_id in self._run_outputs:
                self._run_outputs.add(tree_id)
            return tree_id

    def read_file(self, tree_id: str, path: str) -> bytes | None:
        """Read one file of a tree with no box, unless the loop hides it.

        Args:
            tree_id: The tree.
            path: The ``/``-separated path of the file.

        Returns:
            The file bytes, or ``None`` when the path is hidden or is not a file or executable in the tree.

        Raises:
            LoopStoppedError: When the loop stopped, or a symlink of the tree is, lies below or holds a hidden
                path; the loop keeps it.
            LookupError: When the run cannot see the tree or one of its folders.
        """
        with self._stops():
            self._check_links(tree_id)
            if hashed_kind(self._hidden, path) is not None:
                return None
            *folders, name = path.split("/")
            folder = self._folder(tree_id, folders)
            entry = None if folder is None else self._entries(folder).get(name)
            if entry is None or entry.mode not in {"file", "exec"}:
                return None
            return self._trees.get_blob(self._run.run_id, entry.object_id)

    def list_files(self, tree_id: str, path: str = "") -> list[str] | None:
        """List the files under one folder of a tree with no box, and leave out the paths that the loop hides.

        Args:
            tree_id: The tree.
            path: The ``/``-separated folder; ``""`` or ``.`` is the tree's root.

        Returns:
            The sorted paths of the files and executables below the folder, at any depth, from the tree's root,
            without a hidden path; or ``None`` when the path is hidden, holds only hidden paths, or is not a
            folder in the tree.

        Raises:
            LoopStoppedError: When the loop stopped, or a symlink of the tree is, lies below or holds a hidden
                path; the loop keeps it.
            LookupError: When the run cannot see the tree or one of its folders.
        """
        with self._stops():
            self._check_links(tree_id)
            # A hidden folder gives the same answer as a missing one, so the answer does not show that it exists.
            if hashed_kind(self._hidden, path) is not None:
                return None
            parts = [] if path in {"", "."} else path.split("/")
            folder = self._folder(tree_id, parts)
            prefix = "".join(f"{part}/" for part in parts)
            # A folder left empty by the filter is gone from the box too, so it is missing here as well (SKY-353).
            if folder is None or (parts and not self._has_visible(folder, prefix)):
                return None
            files = self._files(folder, prefix)
            return sorted(name for name in files if hashed_kind(self._hidden, name) is None)

    def remaining_cost_usd(self) -> float:
        """Read the run budget's room, and commit it as one step with no cost and no box.

        The room is what the budget did not settle, with the run's unspent leases. Like a durable workflow step,
        a replay gives the committed room and reads no budget, so an engine that stops on it stops at the same
        place on a resume. Spend of other agents after the read is not in the committed room.

        Returns:
            The room in US dollars.

        Raises:
            StepDivergedError: When the committed step at this place is not a room read.
            LoopStoppedError: When the loop stopped, the run is fenced or closed, the agent cannot open, or the
                read or its commit fails.
        """
        with self._stops():
            log = self._log()
            with log.lock:
                record = self._replayed(log, "room", {})
                if record is not None:
                    return float(Decimal(record["body"]["room_cents"]) / 100)
                self._check_owner()
                run, agent = self._run, log.agent
                try:
                    user = run.workspace.user
                    budget = user.budgets.get(run.budget_id, user.username)
                    room = format(budget.available_cents + budget.reserved_cents, "f")
                    # The step log needs a tree on a command step.
                    empty_tree = self._trees.put_tree(run.run_id, [])
                    # Agent.step settles each step on the ledger, and a read has nothing to settle.
                    step_id = run.step_log.commit(
                        run_id=run.run_id,
                        agent_id=agent.agent_id,
                        seq=agent.next_seq,
                        kind="command",
                        execution_generation=run.execution_generation,
                        grant_list_id=run.grant_list_id,
                        body={"call": "room", "room_cents": room},
                        tree_id=empty_tree,
                    )
                    agent.next_seq += 1
                    # The engine acts on the room only once its step can never be lost, as for a model reply.
                    self._durable(step_id)
                except Exception as error:
                    raise LoopStoppedError(f"{type(error).__name__}: {error}") from error
                return float(Decimal(room) / 100)

    def run(
        self,
        tree_id: str,
        command: str,
        grants: tuple[Grant, ...] = (),
    ) -> tuple[CommandResult, str]:
        """Run one command in a box over a tree, as one ``command`` step.

        The box gets the tree without the loop's hidden paths and without ``.optimize/exploits``, so the command cannot
        read them; the tree after the command does not hold them either. Only the metric box gets the hidden files.
        So the tree after the command holds no hidden content, and the loop does not walk its symlinks: a link
        that the command makes, also one with a hidden path's name, can reach only files that the box wrote or saw.

        Args:
            tree_id: The tree the command starts from.
            command: The shell command.
            grants: The box's network grants; empty means no network.

        Returns:
            The command's result and the tree id after it; on a replay, the committed ones, with no box.

        Raises:
            StepDivergedError: When the committed step at this place is not this call.
            LoopStoppedError: When the loop stopped or was closed, the agent cannot open, or the step fails
                outside its box errors: a fence, a budget stop, a tree the run cannot see, a tree with a symlink
                that is, lies below or holds a hidden path, or a commit failure.
            GrantDeniedError, BoxFailureError, TreeRejectedError: The box errors of :meth:`TreeSandbox.run`;
                each commits a record, and a replay raises it again. ``TreeRejectedError`` also comes when the
                command writes a hidden path: a tree id is a content hash, so that tree would confirm a guess
                of the hidden content. The refusal confirms that the path is hidden, but gives no hidden content.
        """
        with self._stops():
            grant_list_id = self._grant_list_id(grants)
            log = self._log()
            with log.lock:
                record = self._replayed(log, "run", {"command": command}, grant_list_id=grant_list_id)
                if record is not None:
                    body = record["body"]
                    after = str(record["tree_id"])
                    self._run_outputs.add(after)
                    return CommandResult(body["exit_code"], body["stdout"], body["stderr"], body["timed_out"]), after
                result, after = self._box_step(
                    log,
                    "run",
                    command,
                    tree_id,
                    grant_list_id,
                    lambda step: self._sandbox.run(
                        step,
                        self._visible_tree(self._check_links(tree_id)),
                        command,
                        grants,
                        refuse=self._holds_hidden,
                    ),
                )
                self._run_outputs.add(after)
                return result, after

    def score(self, outputs_tree_id: str, metric_tree_id: str, command: str) -> float:
        """Run the metric in its own box over an outputs tree, as one ``command`` step.

        Args:
            outputs_tree_id: The tree of the candidate's outputs.
            metric_tree_id: The tree of the metric's files.
            command: The metric command; its last stdout line is the score.

        Returns:
            The score; on a replay, the committed score, with no box.

        Raises:
            StepDivergedError: When the committed step at this place is not this call.
            LoopStoppedError: When the loop stopped or was closed, the agent cannot open, or the step fails
                outside its box errors: a fence, a budget stop, a tree the run cannot see, or a commit failure.
            GrantDeniedError, BoxFailureError, TreeRejectedError, MetricFailedError: The box errors of
                :meth:`TreeSandbox.score`; each commits a record, and a replay raises it again.
        """
        with self._stops():
            grant_list_id = self._grant_list_id(METRIC_BOX_GRANTS)
            log = self._log()
            with log.lock:
                record = self._replayed(
                    log, "score", {"command": command}, tree_id=outputs_tree_id, grant_list_id=grant_list_id
                )
                if record is not None:
                    return float(record["body"]["score"])
                return self._box_step(
                    log,
                    "score",
                    command,
                    outputs_tree_id,
                    grant_list_id,
                    lambda step: self._sandbox.score(step, outputs_tree_id, metric_tree_id, command),
                )

    @contextmanager
    def _stops(self) -> Iterator[None]:
        """Raise a stop that the loop keeps before any work, and keep each new stop.

        Yields:
            Nothing.

        Raises:
            LoopStoppedError: The kept stop, with its own type, such as :class:`StepDivergedError`; or a new one
                when the run lost the fence or the budget stopped it.
        """
        stopped = self._stopped
        if stopped is not None:
            # A new error of the kept type: a StepDivergedError stays one on each later call. The kept instance
            # itself is not raised again, since each raise would add to the traceback that it shares across threads.
            raise type(stopped)(str(stopped)) from stopped
        try:
            yield
        except LoopStoppedError as error:
            self._stop(error)
            raise
        except _STOP_ERRORS as error:
            raise self._stop(LoopStoppedError(f"{type(error).__name__}: {error}")) from error

    def _stop(self, error: LoopStoppedError) -> LoopStoppedError:
        """Keep the first stop of the loop.

        Args:
            error: The stop.

        Returns:
            The same stop.
        """
        with self._lock:
            if self._stopped is None:
                self._stopped = error
        return error

    def _check_owner(self) -> None:
        """Check that the loop did not stop and this worker still owns the run, before a step's paid work.

        Raises:
            LoopStoppedError: The kept stop, with its own type, which a call that was already past :meth:`_stops`
                did not see; or a new one when the run is closed, no longer active at the loop's generation, or the
                owner check fails.
        """
        stopped = self._stopped
        if stopped is not None:
            # A new error of the kept type: a StepDivergedError stays one on each later call. The kept instance
            # itself is not raised again, since each raise would add to the traceback that it shares across threads.
            raise type(stopped)(str(stopped)) from stopped
        run = self._run
        if run.closed:
            raise LoopStoppedError(f"Run {run.run_id} is closed.")
        try:
            owns = still_owns_run(self._engine, run.run_id, run.execution_generation)
        except Exception as error:
            # A failed check, such as a dropped database connection, must stop the loop: as a scorer error the
            # eval server would record a metric error and go on with no committed step, and a replay would shift.
            raise LoopStoppedError(f"Owner check failed: {type(error).__name__}: {error}") from error
        if not owns:
            raise LoopStoppedError(f"Run {run.run_id} is no longer active at generation {run.execution_generation}.")

    def _entries(self, tree_id: str) -> dict[str, TreeEntry]:
        """Return a tree's entries by name.

        Args:
            tree_id: The tree.

        Returns:
            The entries.

        Raises:
            LookupError: When the run cannot see the tree.
        """
        entries = self._trees.get_tree(self._run.run_id, tree_id)
        if entries is None:
            raise LookupError(f"run {self._run.run_id} cannot see tree {tree_id}")
        return {entry.name: entry for entry in entries}

    def _visible_tree(self, tree_id: str) -> str:
        """Return the tree without its hidden paths and without ``.optimize/exploits``.

        Args:
            tree_id: The tree.

        Returns:
            The visible tree's id; the same id when nothing is left out.

        Raises:
            LookupError: When the run cannot see the tree or one of its folders.
        """
        visible = self._visible(tree_id, "")
        return self._trees.put_tree(self._run.run_id, []) if visible is None else visible

    def _visible(self, tree_id: str, prefix: str) -> str | None:
        """Return a folder without its hidden paths, or ``None`` when it holds only hidden paths.

        A folder that the filter leaves empty is dropped, not kept empty: an empty ``eval/`` would show the
        proposer that the folder holds hidden files (SKY-353). A folder that was empty before stays.

        Args:
            tree_id: The folder.
            prefix: The folder's path from the tree's root, with a trailing ``/``, or ``""`` for the root.

        Returns:
            The visible folder's id, the same id when nothing is left out; or ``None`` when the folder has
            entries and the filter leaves none of them.

        Raises:
            LookupError: When the run cannot see the tree or one of its folders.
        """
        entries = self._entries(tree_id)
        kept = []
        for name, entry in entries.items():
            path = f"{prefix}{name}"
            if hashed_kind(self._hidden, path) is not None:
                continue
            if entry.mode == "tree":
                visible = self._visible(entry.object_id, f"{path}/")
                if visible is None:
                    continue
                entry = TreeEntry(name, "tree", visible)
            kept.append(entry)
        if kept == list(entries.values()):
            return tree_id
        if not kept:
            return None
        return self._trees.put_tree(self._run.run_id, kept)

    def _has_visible(self, tree_id: str, prefix: str) -> bool:
        """Tell whether a folder stays after the filter of ``_visible``, and write nothing.

        A read must not put tree objects in the step log: they are rows nobody uses, and the write fails once
        the step log is closed.

        Args:
            tree_id: The folder.
            prefix: The folder's path from the tree's root, with a trailing ``/``, or ``""`` for the root.

        Returns:
            ``False`` when the folder has entries and the filter leaves none of them; else ``True``.

        Raises:
            LookupError: When the run cannot see the tree or one of its folders.
        """
        entries = self._entries(tree_id)
        if not entries:
            return True
        for name, entry in entries.items():
            path = f"{prefix}{name}"
            if hashed_kind(self._hidden, path) is not None:
                continue
            if entry.mode != "tree" or self._has_visible(entry.object_id, f"{path}/"):
                return True
        return False

    def _check_links(self, tree_id: str) -> str:
        """Stop the loop when a symlink of a tree that can hold hidden content could show a hidden path.

        The hidden filter matches tree paths. A hidden path that is a symlink, or lies below a symlinked folder,
        loses only that path, and the link's target stays visible under its real path. The loop holds only path
        hashes, so it walks the tree as a box sees it, through each link to a folder of the tree, and checks each
        path that it reaches through a link. A link back into a folder that holds it, or more than
        ``_MAX_LINKED_PATHS`` paths reached through links, also stops the loop, since the walk cannot check all
        of them. So does a link that names an absolute path, or a path that leaves the tree, also one that comes
        back in: the loop does not know where a box puts the tree, so it cannot tell what such a link reaches.
        When the run hides no path of its own, a link loop, a link that leaves the tree and the limit stop the
        loop only in a tree that holds a hidden path. A tree that a run step gave, or that ``put_files`` wrote over one, holds
        no hidden content, so the loop does not walk it.

        Args:
            tree_id: The tree.

        Returns:
            The same tree id.

        Raises:
            LoopStoppedError: When the tree has such a link; the loop keeps it.
            LookupError: When the run cannot see the tree or one of its folders.
        """
        if tree_id in self._link_checked or tree_id in self._run_outputs:
            return tree_id
        folders: dict[tuple[str, ...], dict[str, TreeEntry]] = {}
        self._index_folders(tree_id, (), folders)
        if any(entry.mode == "symlink" for entries in folders.values() for entry in entries.values()):
            # The message names no path, so it does not show where a hidden path is.
            reason = self._link_problem(folders)
            if reason is not None:
                raise self._stop(LoopStoppedError(f"Tree {tree_id} {reason}."))
        self._link_checked.add(tree_id)
        return tree_id

    def _holds_hidden(self, tree_id: str) -> bool:
        """Tell whether a path of a tree is hidden.

        Args:
            tree_id: The tree.

        Returns:
            Whether the tree holds a hidden path or ``.optimize/exploits``.

        Raises:
            LookupError: When the run cannot see the tree or one of its folders.
        """
        folders: dict[tuple[str, ...], dict[str, TreeEntry]] = {}
        self._index_folders(tree_id, (), folders)
        return self._folders_hold_hidden(folders)

    def _folders_hold_hidden(self, folders: Mapping[tuple[str, ...], Mapping[str, TreeEntry]]) -> bool:
        """Tell whether a path of the indexed folders is hidden.

        Args:
            folders: The entries by name of each folder, by path parts from the tree's root.

        Returns:
            Whether a folder holds a hidden path or ``.optimize/exploits``.
        """
        return any(
            hashed_kind(self._hidden, "/".join((*path, name))) is not None
            for path, entries in folders.items()
            for name in entries
        )

    def _index_folders(
        self, tree_id: str, path: tuple[str, ...], folders: dict[tuple[str, ...], dict[str, TreeEntry]]
    ) -> None:
        """Read the entries of a folder and of each folder below it.

        Args:
            tree_id: The folder's tree id.
            path: The folder's path parts from the tree's root.
            folders: Receives the entries by name, for each folder path.

        Raises:
            LookupError: When the run cannot see the folder or one below it.
        """
        entries = self._entries(tree_id)
        folders[path] = entries
        for name, entry in entries.items():
            if entry.mode == "tree":
                self._index_folders(entry.object_id, (*path, name), folders)

    def _link_problem(self, folders: Mapping[tuple[str, ...], Mapping[str, TreeEntry]]) -> str | None:
        """Walk a tree through its links to folders, and find a link that could show a hidden path.

        A link that leaves the tree, or a link loop, hides from the walk what it reaches, and a hidden path can
        lie behind it: ``a -> .`` reaches ``a/secret.txt``. So when the run hides paths of its own, such a link
        refuses the tree, and so does a walk that reaches more than ``_MAX_LINKED_PATHS`` paths through links,
        which then stops. With the exploit suite alone, these refuse the tree only when it holds a hidden path,
        since repositories hold such links: ``python -m venv`` writes an absolute link on Linux.

        Args:
            folders: The entries by name of each folder, by path parts from the tree's root.

        Returns:
            Why the tree is refused, with no path in it; or ``None`` when no link shows a hidden path.
        """
        # Each item: the path that a box sees, the real folder that it names, whether a link is on the way, and
        # the real folders on the way, which a link must not name again.
        pending: list[tuple[tuple[str, ...], tuple[str, ...], bool, frozenset[tuple[str, ...]]]] = [
            ((), (), False, frozenset({()}))
        ]
        reached = 0
        holds_hidden = False
        problem: str | None = None
        while pending:
            seen, real, linked, above = pending.pop()
            for name, entry in folders[real].items():
                path = (*seen, name)
                hidden = hashed_kind(self._hidden, "/".join(path)) is not None
                holds_hidden = holds_hidden or hidden
                if linked or entry.mode == "symlink":
                    reached += 1
                    if reached > _MAX_LINKED_PATHS:
                        problem = problem or f"reaches more than {_MAX_LINKED_PATHS} paths through symlinks"
                        pending.clear()
                        break
                    if hidden:
                        return "has a symlink at, below or above a hidden path"
                if entry.mode == "symlink":
                    try:
                        target = self._link_target(folders, (*real, name))
                    except _LinkLeavesTreeError:
                        problem = problem or "has a symlink that names an absolute path or leaves the tree"
                        continue
                    if target is None:
                        continue
                    if target in above:
                        problem = problem or "has a symlink loop"
                        continue
                    pending.append((path, target, True, above | {target}))
                elif entry.mode == "tree":
                    pending.append((path, (*real, name), linked, above | {(*real, name)}))
        if problem is None or holds_hidden or self._strict_links:
            return problem
        # A walk that stopped at the limit saw only some paths, so the tree's own paths tell whether it holds one.
        return problem if self._folders_hold_hidden(folders) else None

    def _link_target(
        self, folders: Mapping[tuple[str, ...], Mapping[str, TreeEntry]], link: tuple[str, ...]
    ) -> tuple[str, ...] | None:
        """Resolve a symlink to the folder of the tree that it names, as a box would.

        Args:
            folders: The entries by name of each folder, by path parts from the tree's root.
            link: The real path parts of the symlink.

        Returns:
            The real path parts of the folder; or ``None`` when the target is no folder of the tree: a file, a
            missing path, or a chain of more than ``_MAX_LINK_HOPS`` links.

        Raises:
            _LinkLeavesTreeError: When a link on the way names an absolute path, or a ``..`` leaves the tree's root.
        """
        real = link[:-1]
        names = deque([link[-1]])
        hops = 0
        while names:
            name = names.popleft()
            if name in {"", "."}:
                continue
            if name == "..":
                if not real:
                    raise _LinkLeavesTreeError
                real = real[:-1]
                continue
            entry = folders.get(real, {}).get(name)
            if entry is None:
                return None
            if entry.mode == "symlink":
                hops += 1
                blob = self._trees.get_blob(self._run.run_id, entry.object_id)
                target = (blob or b"").decode("utf-8", "surrogateescape")
                if target.startswith("/"):
                    raise _LinkLeavesTreeError
                if hops > _MAX_LINK_HOPS:
                    return None
                names.extendleft(reversed(target.split("/")))
                continue
            real = (*real, name)
        return real if real in folders else None

    def _folder(self, tree_id: str, parts: list[str]) -> str | None:
        """Walk down a tree to one of its folders.

        Args:
            tree_id: The tree.
            parts: The folder's path parts; empty for the tree itself.

        Returns:
            The folder's tree id, or ``None`` when a part is missing or is not a folder.

        Raises:
            LookupError: When the run cannot see the tree or one of its folders.
        """
        for part in parts:
            entry = self._entries(tree_id).get(part)
            if entry is None or entry.mode != "tree":
                return None
            tree_id = entry.object_id
        return tree_id

    def _files(self, tree_id: str, prefix: str) -> Iterator[str]:
        """Give the paths of the files and executables in a folder, at any depth.

        Args:
            tree_id: The folder's tree id.
            prefix: The folder's path from the tree's root, with a trailing ``/``, or ``""`` for the root.

        Yields:
            Each file path from the tree's root.

        Raises:
            LookupError: When the run cannot see the folder or one below it.
        """
        for name, entry in self._entries(tree_id).items():
            if entry.mode == "tree":
                yield from self._files(entry.object_id, f"{prefix}{name}/")
            elif entry.mode in {"file", "exec"}:
                yield f"{prefix}{name}"

    def _put_tree(self, tree_id: str | None, files: Mapping[tuple[str, ...], bytes], prefix: str = "") -> str:
        """Write files over one tree level, and each changed folder below it.

        Args:
            tree_id: The tree of this level; ``None`` for an empty one.
            files: The file bytes by path parts below this level.
            prefix: This level's path from the tree's root, with a trailing ``/``, or ``""`` for the root.

        Returns:
            The new tree id of this level.

        Raises:
            ValueError: When a name is both a file and a folder, the tree store refuses a name, or a file would
                replace a folder that holds a hidden path.
            LookupError: When the run cannot see the tree.
        """
        entries = {} if tree_id is None else self._entries(tree_id)
        folders: dict[str, dict[tuple[str, ...], bytes]] = {}
        written: set[str] = set()
        for (name, *rest), data in files.items():
            if rest:
                folders.setdefault(name, {})[tuple(rest)] = data
                continue
            old = entries.get(name)
            # put_files already refused each hidden path it writes, and a nested write that turns a file into a
            # folder writes below that file; so only a folder that a file replaces can hold one more.
            if old is not None and old.mode == "tree":
                self._refuse_hidden(old, f"{prefix}{name}")
            mode = "exec" if old is not None and old.mode == "exec" else "file"
            entries[name] = TreeEntry(name, mode, self._trees.put_blob(self._run.run_id, data))
            written.add(name)
        if clash := written & folders.keys():
            raise ValueError(f"paths are both a file and a folder: {sorted(clash)}")
        for name, nested in folders.items():
            old = entries.get(name)
            base = old.object_id if old is not None and old.mode == "tree" else None
            entries[name] = TreeEntry(name, "tree", self._put_tree(base, nested, f"{prefix}{name}/"))
        return self._trees.put_tree(self._run.run_id, entries.values())

    def _refuse_hidden(self, folder: TreeEntry, path: str) -> None:
        """Refuse to replace a folder with a file when a path below the folder is hidden.

        Args:
            folder: The folder's entry.
            path: The folder's path from the tree's root.

        Raises:
            ValueError: When a path below the folder is hidden. The message names only ``path``, as for a write
                to a hidden path, so it does not name the hidden path below it.
            LookupError: When the run cannot see a folder below the entry.
        """
        if any(hashed_kind(self._hidden, below) is not None for below in self._below(folder, path)):
            raise ValueError(f"path {path} may not be changed")

    def _below(self, entry: TreeEntry, path: str) -> Iterator[str]:
        """Give the path of each entry below a folder, at any depth.

        Args:
            entry: The folder's entry.
            path: The folder's path from the tree's root.

        Yields:
            Each path below the folder, from the tree's root.

        Raises:
            LookupError: When the run cannot see the folder or one below it.
        """
        for name, child in self._entries(entry.object_id).items():
            yield f"{path}/{name}"
            if child.mode == "tree":
                yield from self._below(child, f"{path}/{name}")

    def _grant_list_id(self, grants: tuple[Grant, ...]) -> str:
        """Resolve a step's grant list once, before its work.

        Args:
            grants: The step's grants.

        Returns:
            The grant-list id; the run's id when the grants do not resolve, since a denied step has no other.

        Raises:
            LoopStoppedError: When the grants fail with another error than a refusal, such as a grant of the wrong
                type; a replay fails the same way, so the loop keeps the stop.
        """
        try:
            return resolve_grants(grants, self._registry).grant_list_id
        except GrantError:
            return self._run.grant_list_id
        except Exception as error:
            raise LoopStoppedError(f"The grants do not resolve: {type(error).__name__}: {error}") from error

    def _log(self) -> _AgentLog:
        """Return the agent of the calling lane, and open it on its first call.

        Returns:
            The agent's log.

        Raises:
            LoopStoppedError: When the agent's committed log has a gap, the run is closed, or the step log read
                fails.
        """
        agent_id = current_tags().get(LANE_KEY) or ROOT_AGENT_ID
        with self._lock:
            log = self._logs.get(agent_id)
            if log is not None:
                return log
            opening = self._opening.setdefault(agent_id, threading.Lock())
        # An open reads the step log, so it holds only its agent's lock: other lanes and stops go on.
        with opening:
            with self._lock:
                log = self._logs.get(agent_id)
            if log is None:
                try:
                    log = self._open(agent_id)
                except Exception as error:
                    # Also a database error, such as a dropped connection: an open that fails for any reason stops.
                    raise LoopStoppedError(f"Agent {agent_id} cannot open: {type(error).__name__}: {error}") from error
                with self._lock:
                    self._logs[agent_id] = log
        return log

    def _open(self, agent_id: str) -> _AgentLog:
        """Spawn an agent after its committed steps.

        The loop keeps no local copy of a workspace. An agent's session state lives in the tree of its committed
        run steps: the engine passes that tree to its next ``run``, and the box gets it there, live and on a
        replay alike.

        Args:
            agent_id: The agent.

        Returns:
            The agent's log, with every committed step still to replay.

        Raises:
            LookupError: When a step below the agent's newest one is missing.
            RuntimeError: When the run is closed.
        """
        run_id = self._run.run_id
        store = self._run.step_log
        last = last_committed_step(self._engine, store, run_id, agent_id)
        records: list[dict[str, Any]] = []
        for seq in range(0 if last is None else last["seq"] + 1):
            record = store.get_step(step_id_for(run_id, agent_id, seq))
            if record is None:
                raise LookupError(f"step {seq} of agent {agent_id} is missing")
            records.append(record)
        return _AgentLog(self._run.spawn(agent_id, next_seq=len(records)), deque(records))

    def _replayed(
        self,
        log: _AgentLog,
        call: str,
        fingerprint: dict[str, Any],
        *,
        tree_id: str | None = None,
        grant_list_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Take the next committed step of a replay, and check that it is this call.

        A step that does not match stays in the replay.

        Args:
            log: The agent's log.
            call: ``model``, ``run``, ``score`` or ``room``.
            fingerprint: The body fields that the committed step must hold.
            tree_id: The tree that a committed score step must name.
            grant_list_id: The grant-list id that a committed box step must name.

        Returns:
            The committed step, or ``None`` when the replay is over.

        Raises:
            StepDivergedError: When the committed step is another call, or a box step that records an error that
                is not a box error.
            ModelCallFailedError: When the committed model step records a failure.
            TreeSandboxError: The error that the committed box step records.
        """
        if not log.replay:
            return None
        record = log.replay[0]
        body = record["body"]
        if (
            _call(record) != call
            or any(body.get(key) != value for key, value in fingerprint.items())
            or (tree_id is not None and record["tree_id"] != tree_id)
            or (grant_list_id is not None and record["grant_list_id"] != grant_list_id)
        ):
            raise StepDivergedError(f"step {record['step_id']} is not the engine's {call} call")
        if "error" in body and call != "model" and body["error"] not in _BOX_ERRORS:
            # The step stays in the replay, so a later call cannot read past a record that the loop cannot raise.
            raise StepDivergedError(f"step {record['step_id']} records an unknown error {body['error']!r}")
        log.replay.popleft()
        if "error" in body:
            if call == "model":
                raise ModelCallFailedError(body["error"])
            raise _BOX_ERRORS[body["error"]](record["step_id"], body["message"])
        return record

    def _box_step(
        self,
        log: _AgentLog,
        call: str,
        command: str,
        tree_id: str,
        grant_list_id: str,
        work: Callable[[StepRef], _Outcome],
    ) -> _Outcome:
        """Run one box step at the agent's next place, and commit a record when the box step fails.

        Args:
            log: The agent's log; its lock is held.
            call: ``run`` or ``score``.
            command: The step's command.
            tree_id: The step's input tree, which a failure record names.
            grant_list_id: The step's resolved grant-list id, which a failure record names.
            work: Runs the step through the tree sandbox.

        Returns:
            What ``work`` returns.

        Raises:
            GrantDeniedError, BoxFailureError, TreeRejectedError, MetricFailedError: The step's box error, after
                its record commits.
            LoopStoppedError: For every other error, and when the record does not commit.
        """
        agent = log.agent
        step = StepRef(self._run.run_id, agent.agent_id, agent.next_seq, self._run.execution_generation)
        self._check_owner()
        try:
            outcome = work(step)
        except tuple(_BOX_ERRORS.values()) as error:
            message = str(error).removeprefix(f"Step {step.step_id}: ")
            try:
                step_id = self._run.step_log.commit(
                    run_id=step.run_id,
                    agent_id=step.agent_id,
                    seq=step.seq,
                    kind="command",
                    execution_generation=step.execution_generation,
                    grant_list_id=grant_list_id,
                    body={"call": call, "command": command, "error": type(error).__name__, "message": message},
                    tree_id=tree_id,
                )
                agent.next_seq += 1
                self._durable(step_id)
            except Exception as commit_error:
                # A closed step log fails every record, so each later call would open and bill a box again.
                raise LoopStoppedError(f"{type(commit_error).__name__}: {commit_error}") from commit_error
            raise
        except StepCommittedError as error:
            # The step holds its seq even though it may never be durable; a replay gives its result.
            agent.next_seq += 1
            raise LoopStoppedError(f"{type(error).__name__}: {error}") from error
        except Exception as error:
            # Not a box error: a fence or budget stop, a tree the run cannot see, or a commit failure after the
            # box ran. The engine gets no result, so a resume that replays a committed step cannot contradict it.
            raise LoopStoppedError(f"{type(error).__name__}: {error}") from error
        agent.next_seq += 1
        return outcome

    def _durable(self, step_id: str) -> None:
        """Wait until a step is durable behind the run's fence.

        Args:
            step_id: The step.

        Raises:
            FenceLostError: When the run generation is no longer current.
        """
        try:
            self._run.step_log.durable(step_id, self._run.execution_generation)
        except StepFenceError as error:
            raise FenceLostError(step_id, f"The run generation changed at step {error.step_id}.") from error
