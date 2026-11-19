"""Engine-facing contract for black-box optimization.

Mirrors the shape of GEPA's ``optimize_anything`` engine protocol — a task,
an eval server that is the single scoring choke point, and a result — so a
future upstream ``gepa.oa`` release can be adopted without changing the
Skynet-side contract (design brief TODO-4).
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
from collections.abc import Callable, Collection, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol, TypedDict

from gepa.oa.budget import BudgetExhausted

from ....billing.usage_tags import usage_scope
from ..cost_ceiling import CostCeilingExceededError
from ..timing import STAGE_TRAINING
from .runner import ANSWER_FORM

if TYPE_CHECKING:
    from ....sandbox.grants import Grant
    from .native_runtime import NativeOptions
    from .sandbox_contract import CommandResult

logger = logging.getLogger(__name__)

Candidate = str | dict[str, str]
SideInfo = dict[str, Any]
ScorerFn = Callable[[Candidate, Any], tuple[float | None, SideInfo]]
EvalListener = Callable[["EvalServer", float | None], None]

# Side info ``status`` of a case the metric or scorer crashed on. That case has
# no score (``None``), never 0: a crash is not a measurement (SKY-350).
METRIC_ERROR = "metric_error"
# SKY-751: the status of a case whose ``[run]`` entry failed (crash, timeout,
# a function entry that exits before it returns; a command entry's non-zero exit
# is an output). The runner never invents a score, so that case has no score
# too; it keeps its own status so the final run can report the failure text.
RUN_FAILED_STATUS = "run_failed"
STATUS_KEY = "status"
# SKY-367: the pinned upstream engines need a number for every case, and they
# use negative infinity as their own "no score" value. A single case scored
# alone gets it; a batch fills each null case with its version's mean over the
# scored cases (see :func:`fill_unscored`), so the null cases do not change the
# mean upstream ranks by. Only a version with no scored case is UNSCORED.
# SKY-751: a failed [run] entry is the version's fault, not the metric's, so it
# gets no fill: that version is UNSCORED on every case of the batch and ranks
# below every fully scored version.
UNSCORED = float("-inf")


def engine_score(score: float | None) -> float:
    """Give a pinned upstream engine its own "no score" value for a metric error case.

    Args:
        score: A score from :meth:`EvalServer.evaluate`, ``None`` after a metric error.

    Returns:
        The score, or :data:`UNSCORED` when there is none.
    """
    return UNSCORED if score is None else score


def fill_unscored(
    candidates: list[Candidate], scores: list[float | None], side_infos: list[SideInfo] | None = None
) -> list[float]:
    """Give each null case its version's mean over the scored cases in the same batch.

    Args:
        candidates: The version scored on each case, aligned with ``scores``.
        scores: Each case's score, ``None`` after a metric error or a failed ``[run]`` entry.
        side_infos: Each case's side info, when known; it tells a failed ``[run]`` entry apart.

    Returns:
        A number for every case: the score, the mean of the same version's
        scored cases for a null case, or :data:`UNSCORED` when the version
        has no scored case in the batch or a failed ``[run]`` entry.
    """
    infos = side_infos if side_infos is not None else [{} for _ in candidates]
    # A scorer's own run_failed status with a real score is not a failed [run] entry.
    failed = {
        candidate_key(candidate)
        for candidate, score, info in zip(candidates, scores, infos, strict=True)
        if score is None and is_run_failed(info)
    }
    scored: dict[str, list[float]] = {}
    for candidate, score in zip(candidates, scores, strict=True):
        if score is not None:
            scored.setdefault(candidate_key(candidate), []).append(score)
    filled: list[float] = []
    for candidate, score in zip(candidates, scores, strict=True):
        if candidate_key(candidate) in failed:
            score = UNSCORED
        elif score is None:
            values = scored.get(candidate_key(candidate))
            score = sum(values) / len(values) if values else UNSCORED
        filled.append(score)
    return filled


def score_batch(
    score_one: Callable[[Candidate, Any], tuple[float | None, SideInfo]],
    pairs: list[tuple[Candidate, Any]],
    max_workers: int | None,
) -> list[tuple[float, SideInfo]]:
    """Score a batch of pairs at the engine's concurrency and fill the null cases.

    Upstream sends a whole batch in one call and does not fan it out, so the
    batch keeps the per-pair concurrency the engines had before (SKY-367).

    Args:
        score_one: Scores one pair; ``None`` after a metric error.
        pairs: The ``(candidate, case)`` pairs of one upstream batch.
        max_workers: The engine's scorer concurrency.

    Returns:
        One ``(score, side_info)`` per pair, in order, with null cases filled
        by :func:`fill_unscored`; side info still marks each metric error.
    """
    workers = max(1, min(max_workers or 1, len(pairs)))
    if workers == 1:
        results = [score_one(candidate, example) for candidate, example in pairs]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(lambda pair: score_one(*pair), pairs))
    filled = fill_unscored(
        [candidate for candidate, _ in pairs], [score for score, _ in results], [info for _, info in results]
    )
    return [(score, info) for score, (_, info) in zip(filled, results, strict=True)]


def is_metric_error(side_info: SideInfo) -> bool:
    """Return True when the side info marks a case the metric or scorer crashed on.

    Args:
        side_info: What the scorer returned for one case.

    Returns:
        Whether its ``status`` is :data:`METRIC_ERROR`.
    """
    return side_info.get(STATUS_KEY) == METRIC_ERROR


def is_run_failed(side_info: SideInfo) -> bool:
    """Return True when the side info marks a case whose ``[run]`` entry failed (SKY-751).

    Args:
        side_info: What the scorer returned for one case.

    Returns:
        Whether its ``status`` is :data:`RUN_FAILED_STATUS`.
    """
    return isinstance(side_info, dict) and side_info.get(STATUS_KEY) == RUN_FAILED_STATUS


NO_FEEDBACK_WARNING = (
    "The metric gives no feedback, so the proposer sees only the scores. "
    'Add "feedback" to the metric answer to say what was wrong or missing.'
)


def warn_once_without_feedback(scorer: ScorerFn) -> ScorerFn:
    """Wrap a run's scorer so the run log says once that the metric gives no feedback.

    Feedback is optional (SKY-769). A command metric has no static check, so
    this line is how its author learns the proposer gets no text. A named
    score with no feedback gives no line.

    The warning comes once for each run attempt, that is, once in each
    process that runs the job. The state is in memory only, so a run that
    resumes in a new process can warn again. This is by design: no flag is
    persisted.

    Args:
        scorer: The run's scorer.

    Returns:
        The same scorer, which logs one warning at the first score with no feedback.
    """
    lock = threading.Lock()
    warned = False

    def score(candidate: Candidate, case: Any = None) -> tuple[float | None, SideInfo]:
        """Score ``candidate`` on ``case`` with the wrapped scorer.

        Args:
            candidate: The version to score.
            case: The case to score it on.

        Returns:
            What the wrapped scorer returned.
        """
        nonlocal warned
        value, side_info = scorer(candidate, case)
        if value is not None and not side_info.get("feedback"):
            with lock:
                first, warned = not warned, True
            if first:
                logger.warning(NO_FEEDBACK_WARNING)
        return value, side_info

    return score


class BudgetExhaustedError(RuntimeError):
    """Raised by :class:`EvalServer` once its scorer-run cap is reached."""


class ScorerAbortError(RuntimeError):
    """Raised by a scorer to stop the whole run.

    :class:`EvalServer` records every other scorer failure as a metric error
    to keep one bad version from killing a run; this one passes through
    untouched because the scorer has established that no version can be scored.
    """


class FileTooLargeError(Exception):
    """Raised by a capped ``read_file`` of the engine context when a tree file is larger than the cap."""

    def __init__(self, path: str, size: int, max_bytes: int) -> None:
        """Keep the file's size and the cap.

        Args:
            path: The ``/``-separated path that the caller asked for.
            size: The file's length in bytes.
            max_bytes: The cap of the read.
        """
        super().__init__(f"file {path} has {size} bytes, more than the cap of {max_bytes}")
        self.size = size
        self.max_bytes = max_bytes


def candidate_key(candidate: Candidate) -> str:
    """Return a stable identity string for a candidate.

    Args:
        candidate: A text candidate or a named-parts mapping.

    Returns:
        The text itself, or the sorted-key JSON form of a mapping.
    """
    if isinstance(candidate, dict):
        return json.dumps(candidate, sort_keys=True)
    return candidate


@dataclass
class Task:
    """What an engine optimizes: the starting point, the goal and its cases.

    ``cases`` is the train split: the cases an engine scores versions on
    while searching (each case is also a Pareto axis); with none it scores
    a version on its own. ``valset`` is the validation split an engine
    selects its incumbent on when it can; when empty, engines select on
    ``cases``. The test split never reaches a task: the service's final run
    re-scores the starting point and the winner on it afterwards.

    ``context`` is private run context for in-process adapters. No current
    job fills it. When set, it can hold the metric source, so it must never
    be exported to a proposer, a native engine payload, a prompt or an agent
    workspace.
    """

    seed_candidate: Candidate | None
    objective: str | None = None
    background: str | None = None
    cases: list[Any] = field(default_factory=list)
    valset: list[Any] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def has_dataset(self) -> bool:
        """Return True when the task carries cases (multi-task mode)."""
        return bool(self.cases)

    @property
    def str_mode(self) -> bool:
        """Return True when candidates are plain text (including seedless)."""
        return not isinstance(self.seed_candidate, dict)


@dataclass
class Result:
    """What an engine hands back: its best version and what it cost."""

    best_candidate: Candidate
    best_score: float | None
    total_evals: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class CandidateRecord:
    """One distinct version an eval server scored: its running mean, latest side info and crash flags."""

    candidate: Candidate
    total: float
    count: int
    # Root-server call number that first scored this version, so the run's
    # versions can be laid out in the order they appeared.
    first_eval: int
    side_info: SideInfo
    # Kept apart from ``side_info``, which holds only the latest case, so a
    # crash on an earlier case still marks the version.
    crashed: bool = False
    # Set once the metric or scorer crashed on any case: the version then has
    # no score at all, so it can never be the best or be published.
    metric_error: str | None = None
    # SKY-751: whether any case of the version had a failed [run] entry, not
    # only a metric crash. ``metric_error`` then holds the first run failure's text.
    run_failed: bool = False

    @property
    def mean_score(self) -> float | None:
        """Return the mean score across every case this version was scored on, or ``None`` after a metric error."""
        if self.metric_error is not None:
            return None
        return self.total / self.count


# A tool call keeps this text in place of arguments that nest too deep to write or commit (SKY-817). The text is a
# shallow JSON object with no tool fields, so an engine treats it as a bad call for that tool call only, and the run
# goes on. It must stay valid JSON: engines put it unchanged into the next prompt, and a provider that gets text that
# is not a JSON object can fail every later call in the session.
TOO_DEEP_ARGUMENTS = '{"error": "tool call arguments nest too deep"}'
# The most levels that tool call arguments may nest. The engine loop derives its reply bound from it, so a call that
# passes here also commits there. It is far below Python's recursion limit, so later code that reads the arguments
# again does not run out of stack, whatever stack depth it reads them at.
MAX_ARGUMENTS_DEPTH = 97
_JSON_STRUCTURE = re.compile(r'[\[\]{}"\\]')


def nests_deeper(value: Any, levels: int) -> bool:
    """Tell whether a value nests more than a number of levels of objects and arrays, with no recursion.

    The walk also enters mapping keys: the commit's ``sort_keys`` encoding compares keys, and two deep tuple keys
    would make that comparison raise :class:`RecursionError`.

    Args:
        value: The value; a mapping, a list or a tuple is one level.
        levels: The most levels that the value may nest.

    Returns:
        ``True`` when a mapping, a list or a tuple sits deeper than ``levels``.
    """
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, dict):
            children: Any = [*item, *item.values()]
        elif isinstance(item, list | tuple):
            children = item
        else:
            continue
        if depth > levels:
            return True
        stack.extend((child, depth + 1) for child in children)
    return False


def text_nests_deeper(text: str, levels: int) -> bool:
    """Tell whether JSON text opens more than a number of levels of objects and arrays, with no parse.

    The answer does not depend on the stack, unlike a parse that raises :class:`RecursionError` only when it runs out
    of stack. It holds for text that is not valid JSON too, which a later parse can still recurse into.

    Args:
        text: The text; brackets inside JSON strings do not count.
        levels: The most levels that the text may open.

    Returns:
        ``True`` when the text opens more than ``levels`` brackets at one time.
    """
    depth = 0
    in_string = False
    escaped_at = -1
    for match in _JSON_STRUCTURE.finditer(text):
        position, char = match.start(), match.group()
        if position == escaped_at:
            continue
        if in_string:
            if char == "\\":
                escaped_at = position + 1
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > levels:
                return True
        elif char in "]}":
            # A stray closer must not hide later openers: a reader that repairs or skips text still nests them.
            depth = max(depth - 1, 0)
    return False


class ToolDefinition(TypedDict):
    """One tool that the model may call, in the engine loop's flat form.

    ``input_schema`` is the JSON Schema of the call's arguments. :func:`openai_tools` gives the OpenAI form.
    """

    name: str
    description: str
    input_schema: dict[str, Any]


class ToolCall(TypedDict):
    """One tool call that a model reply asks for, in the engine loop's flat form.

    ``id`` is the provider's call id; the engine sends it back with the tool result. ``arguments`` is the
    parsed JSON object, or the raw text when the model sent text that is not a JSON object or that holds a lone
    surrogate, or :data:`TOO_DEEP_ARGUMENTS`. The engine gives the model an error tool result for text, so the model
    can correct its call.
    """

    id: str
    name: str
    arguments: dict[str, Any] | str


def openai_tools(tools: list[ToolDefinition]) -> list[dict[str, Any]]:
    """Convert flat tool definitions to the OpenAI ``tools`` form that LiteLLM takes.

    Args:
        tools: The flat tool definitions.

    Returns:
        One ``{"type": "function", "function": {"name", "description", "parameters"}}`` item for each tool.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["input_schema"],
            },
        }
        for tool in tools
    ]


def escape_surrogates(text: str) -> str:
    """Write each lone surrogate of a text as its ``\\uXXXX`` escape, so the text encodes as UTF-8.

    A model can send the JSON escape of a lone surrogate. The step log stores UTF-8, which has no surrogates, so
    one such reply would stop the run at its commit (SKY-821). Other text stays as it is.

    Args:
        text: The text from the model.

    Returns:
        The text with no lone surrogate.
    """
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


def _has_surrogate(value: Any) -> bool:
    """Return whether a parsed JSON value holds a lone surrogate in a key or a text, at any depth.

    Args:
        value: The parsed value.

    Returns:
        Whether the value cannot encode as UTF-8.
    """
    if isinstance(value, str):
        return escape_surrogates(value) != value
    if isinstance(value, dict):
        return any(_has_surrogate(key) or _has_surrogate(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_has_surrogate(item) for item in value)
    return False


def safe_tool_call(call: ToolCall) -> ToolCall:
    """Return a tool call with no lone surrogate, so its step commits; a safe call stays as it is.

    Arguments that hold one become their JSON text, which writes it as an escape, so the engine refuses only this
    call. The id and the name get their surrogates escaped.

    Args:
        call: The tool call from the model client.

    Returns:
        The call that the step log can store.
    """
    arguments = call["arguments"]
    if isinstance(arguments, str):
        arguments = escape_surrogates(arguments)
    elif _has_surrogate(arguments):
        arguments = escape_surrogates(json.dumps(arguments, default=str, ensure_ascii=False))
    return {
        "id": escape_surrogates(call["id"]) if isinstance(call["id"], str) else call["id"],
        "name": escape_surrogates(call["name"]) if isinstance(call["name"], str) else call["name"],
        "arguments": arguments,
    }


def tool_calls_from_openai(message: Mapping[str, Any]) -> list[ToolCall]:
    """Convert the ``tool_calls`` of an OpenAI-shaped assistant message to the flat form.

    Bad JSON in ``arguments`` does not raise: a model can send it, and a raise would lose the whole reply. The
    call keeps the raw text instead, so the same reply always gives the same calls. ``arguments`` that are
    already a dict stay as they are. Missing or empty ``arguments`` become ``{}``. Any other value, such as a
    list or a number, becomes its JSON text, so the call keeps the value and the arguments stay a dict or text.
    JSON whose parsed object holds a lone surrogate keeps its raw text, and :func:`safe_tool_call` makes each call
    safe for the step log. Text or a value, a dict included, that nests more than ``MAX_ARGUMENTS_DEPTH`` levels
    becomes :data:`TOO_DEEP_ARGUMENTS`, valid JSON or not, so the engine refuses only that call. Deep text must not
    reach the transcript: a later call would read it again, raise :class:`RecursionError` and fail every later model
    call in the session. The bound is a fixed count, not the stack, so the same reply always gives the same calls.

    Args:
        message: The assistant message, for example ``response.choices[0].message.model_dump()``.

    Returns:
        The flat tool calls in their order; empty when the message asks for none.
    """
    calls: list[ToolCall] = []
    for call in message.get("tool_calls") or []:
        function = call["function"]
        raw = function.get("arguments")
        if raw is None or raw == "":
            raw = "{}"
        if (
            text_nests_deeper(raw, MAX_ARGUMENTS_DEPTH)
            if isinstance(raw, str)
            else nests_deeper(raw, MAX_ARGUMENTS_DEPTH)
        ):
            arguments: dict[str, Any] | str = TOO_DEEP_ARGUMENTS
        elif isinstance(raw, dict):
            # Some clients parse the arguments already.
            arguments = raw
        elif not isinstance(raw, str):
            try:
                arguments = json.dumps(raw, default=str)
            except RecursionError:
                # Same stack limit as the parse below: the depth check counts levels, not frames.
                arguments = TOO_DEEP_ARGUMENTS
        else:
            try:
                parsed = json.loads(raw)
            except RecursionError:
                # The depth check counts levels, not stack frames; a deep caller stack can still overflow.
                arguments = TOO_DEEP_ARGUMENTS
            except ValueError:
                arguments = raw
            else:
                arguments = parsed if isinstance(parsed, dict) and not _has_surrogate(parsed) else raw
        calls.append(safe_tool_call({"id": call["id"], "name": function["name"], "arguments": arguments}))
    return calls


@dataclass(frozen=True)
class ModelReply:
    """One model response: its text, the tool calls that it asks for, and its cost in cents."""

    text: str
    cost_cents: Decimal
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass(frozen=True)
class RepoSeed:
    """The run's repository seed tree and the rules for its paths, all repository-relative.

    A symlink of the tree must not be at a hidden path, and no hidden path may be reached through a symlinked
    folder: the engine loop stops on such a tree, because the link's target would stay visible under its real
    path. Links inside a hidden folder count only when the folder is reached through a link, since no box gets
    the folder itself. When the seed hides paths of its own, the tree must also have no other symlink loop and no
    other symlink that names an absolute path or a path that leaves the tree, since the loop cannot tell what it
    reaches. A candidate may change a constrained path, but only within the rules of the constraints file
    (``optimize.toml``). The engine loop does not enforce the editable, read-only and constrained paths; the
    caller enforces them where it accepts a candidate.
    """

    tree_id: str
    editable_paths: tuple[str, ...] = ()
    readonly_paths: tuple[str, ...] = ()
    constrained_paths: tuple[str, ...] = ()
    # The engine never reads hidden content. It can confirm a guessed hidden name, because writes to it are
    # refused. Each key is the SHA-256 of a normalized repository-relative path, built by
    # harness_bridge.hidden_hashes with fold_case=False because hashed_kind does not fold case. The value is its
    # kind. A folder key hides its whole subtree, so check a path with
    # harness_bridge.hashed_kind, never with ``path in hidden_hashes``.
    hidden_hashes: Mapping[str, str] = field(default_factory=dict)


class CallModel(Protocol):
    """Call the model once, as one step of the worker engine loop."""

    def __call__(self, prompt: str | list[dict[str, Any]], tools: list[ToolDefinition] | None = None) -> ModelReply:
        """Call the model with a prompt and optional tool definitions.

        Args:
            prompt: The prompt text or messages.
            tools: The tool definitions that the model may call; ``None`` for no tools.

        Returns:
            The reply, its tool calls and its cost in cents.
        """
        ...


class RunCommand(Protocol):
    """Run one command in a box over a tree, as one step of the worker engine loop."""

    def __call__(self, tree_id: str, command: str, grants: tuple[Grant, ...] = ()) -> tuple[CommandResult, str]:
        """Run a command over a tree.

        Args:
            tree_id: The tree the command starts from.
            command: The shell command.
            grants: The box's network grants; empty means no network.

        Returns:
            The command's result and the tree id after it.
        """
        ...


class PutFiles(Protocol):
    """Write files over a tree with no box."""

    def __call__(
        self, files: Mapping[str, bytes], base_tree_id: str | None = None, *, executables: Collection[str] = ()
    ) -> str:
        """Write files over a base tree.

        Args:
            files: The file bytes by ``/``-separated path.
            base_tree_id: The tree to start from; ``None`` starts from an empty tree.
            executables: The written paths to make executable; another written file keeps an existing
                executable mode.

        Returns:
            The new tree id.
        """
        ...


class ReadFile(Protocol):
    """Read one file of a tree with no box, with an optional byte cap."""

    def __call__(self, tree_id: str, path: str, max_bytes: int | None = None) -> bytes | None:
        """Read a file.

        Args:
            tree_id: The tree.
            path: The ``/``-separated path of the file.
            max_bytes: The most bytes to load; ``None`` loads the file at any size. A file above the cap
                does not load.

        Returns:
            The file bytes, or ``None`` when the path is missing, hidden, or not a file or executable.

        Raises:
            FileTooLargeError: When the file has more than ``max_bytes`` bytes.
        """
        ...


class ListFiles(Protocol):
    """List the files under one folder of a tree with no box."""

    def __call__(self, tree_id: str, path: str = "", *, executable: bool = False) -> list[str] | None:
        """List the files below a folder.

        Args:
            tree_id: The tree.
            path: The ``/``-separated folder; ``""`` is the tree's root.
            executable: List only the executables.

        Returns:
            The sorted file paths, or ``None`` when the folder is missing or hidden.
        """
        ...


@dataclass
class EngineContext:
    """Run-scoped resources every engine receives alongside the task."""

    reflection_lm: Callable[[str | list[dict[str, Any]]], str]
    run_dir: str
    native_options: NativeOptions | None = None
    proposer_token_budget_usd: float | None = None
    check_budget: Callable[[], None] | None = None
    remaining_cost_usd: Callable[[], float] | None = None
    seed: int = 0
    stop_at_score: float | None = None
    # Cap on proposer rounds for engines that iterate (Meta-Harness,
    # AutoSaddler, ShinkaEvolve generations); ``None``
    # runs until the scorer budget is spent.
    max_iterations: int | None = None
    # How many cases an engine may score at once. Only pays off when the
    # scorer itself runs elsewhere (one sandbox per case); 1 for text targets.
    concurrency: int = 1
    # "harness · model" the proposer is told it writes for, on agent targets.
    target_label: str | None = None
    # Job-level progress sink. Engines that can narrate their run (GEPA via
    # the trajectory watcher) stream candidate events through it live.
    progress_callback: Callable[[str, dict[str, Any]], None] | None = None
    # The worker engine loop (SKY-719) sets these; each model call and command is one step there.
    call_model: CallModel | None = None
    run_command: RunCommand | None = None
    put_files: PutFiles | None = None
    read_file: ReadFile | None = None
    list_files: ListFiles | None = None
    # The content id of each visible file of a tree, by path; equal ids mean equal bytes.
    file_ids: Callable[[str], dict[str, str]] | None = None
    repo_seed: RepoSeed | None = None
    # The job id a child engine's forwarded log records carry as ``sandbox_owner``; the worker's log router
    # keeps only its own job's records.
    log_owner: str | None = None
    # Commits ``work()``'s JSON object as one step under ``key``; a resume gives the committed object back.
    record_step: Callable[[Mapping[str, Any], Callable[[], dict[str, Any]]], dict[str, Any]] | None = None


class Engine(Protocol):
    """One optimization engine (GEPA, Best-of-N, ...)."""

    name: str

    def run(self, task: Task, server: EvalServer, ctx: EngineContext) -> Result:
        """Optimize ``task`` scoring only through ``server``.

        Args:
            task: The starting point, goal and cases.
            server: The budgeted scorer every evaluation must go through.
            ctx: Reflection LM, workspace and stop settings for this run.

        Returns:
            The engine's best version and its score.
        """
        ...


class EvalServer:
    """Budgeted scoring choke point shared by every engine in a run.

    Enforces ``max_evals``, keeps a per-candidate mean so a best version is
    always recoverable even when an engine dies mid-run, and — through
    :meth:`lane` — hands out sub-budgets that count against the parent.

    Safe to call from several threads at once: a call is reserved against
    every budget in the chain before the scorer runs, so concurrent
    evaluations can never overspend, and the bookkeeping is locked.
    """

    def __init__(
        self,
        scorer: ScorerFn,
        *,
        max_evals: int,
        parent: EvalServer | None = None,
        on_eval: EvalListener | None = None,
    ) -> None:
        """Create a server over ``scorer`` with a cap of ``max_evals`` calls.

        Args:
            scorer: Callable scoring ``(candidate, case)`` → ``(score, side_info)``.
            max_evals: Maximum scorer calls this server will make.
            parent: Server whose budget this one is a slice of, if any.
            on_eval: Listener called with ``(server, score)`` after each root eval.
        """
        self._scorer = scorer
        self.max_evals = max_evals
        self._parent = parent
        self._on_eval = on_eval
        self.used = 0
        self._records: dict[str, CandidateRecord] = {}
        # Set on the root at the first score: after it, a wrong answer is one version's error.
        self._scored = False
        # Locks are only ever taken child → parent, so lanes cannot deadlock.
        self._lock = threading.Lock()

    @property
    def _root(self) -> EvalServer:
        """Return the top of the lane chain: the server that owns the scorer and listener."""
        server = self
        while server._parent is not None:
            server = server._parent
        return server

    @property
    def remaining(self) -> int:
        """Return how many scorer calls this server may still make."""
        own = max(0, self.max_evals - self.used)
        if self._parent is None:
            return own
        return min(own, self._parent.remaining)

    def lane(self, max_evals: int) -> EvalServer:
        """Open a sub-budget of at most ``max_evals`` calls against this server.

        Args:
            max_evals: Cap for the lane; clamped to what remains here.

        Returns:
            A child server whose evaluations count against both budgets.
        """
        return EvalServer(self._scorer, max_evals=min(max_evals, self.remaining), parent=self)

    def evaluate(self, candidate: Candidate, example: Any = None) -> tuple[float | None, SideInfo]:
        """Score one candidate on one case (or on its own in single-task mode).

        Args:
            candidate: The version to score.
            example: The case to score it on, or ``None`` when there are no cases.

        Returns:
            The score and the scorer's side information. A metric error case
            returns ``None`` with ``status: metric_error``.

        Raises:
            BudgetExhaustedError: When no scorer calls remain.
        """
        root = self._root
        self._reserve()
        with usage_scope(stage=STAGE_TRAINING):
            score, side_info = root._score(candidate, example)
        # Per-call heartbeat at DEBUG so it surfaces only in the Logs tab's
        # verbose view: normal logs show aggregates, verbose logs show each
        # call. Counted against the run-wide budget.
        if score is None:
            logger.debug("scorer eval %d/%d %s: %s", root.used, root.max_evals, METRIC_ERROR, side_info.get("error"))
        else:
            logger.debug("scorer eval %d/%d score=%.3f", root.used, root.max_evals, score)
        self._account(candidate, score, side_info)
        return score, side_info

    def charge(self, candidate: Candidate | None, score: float | None, side_info: SideInfo | None = None) -> None:
        """Count one evaluation scored outside the scorer against every budget in the chain.

        An in-process adapter that runs the metric itself calls this, so its
        metric calls stay inside the run's scorer-run budget and progress.

        Args:
            candidate: The version that was scored, or ``None`` when the
                caller cannot name it; an unnamed evaluation is counted but
                not recorded in the version history.
            score: The score it got, or ``None`` when the metric crashed.
            side_info: What the metric said about it.

        Raises:
            BudgetExhaustedError: When no scorer calls remain.
        """
        self._reserve()
        self._account(candidate, score, side_info or {})

    def _account(self, candidate: Candidate | None, score: float | None, side_info: SideInfo) -> None:
        """Record a reserved evaluation in every server of the chain and notify the listener.

        Args:
            candidate: The scored version, or ``None`` to count it without recording.
            score: Its score, or ``None`` after a metric error.
            side_info: What the scorer said about it.
        """
        root = self._root
        if score is not None:
            root._scored = True
        server: EvalServer | None = self
        while server is not None and candidate is not None:
            with server._lock:
                server._record(candidate, score, side_info)
            server = server._parent
        if root._on_eval is not None:
            with root._lock:
                root._on_eval(root, score)

    def _reserve(self) -> None:
        """Claim one scorer call here and in every ancestor, or claim nothing.

        Raises:
            BudgetExhaustedError: When this server or an ancestor has no
                calls left; no budget in the chain is touched in that case.
        """
        with self._lock:
            if self.used >= self.max_evals:
                raise BudgetExhaustedError(f"scorer-run budget of {self.max_evals} exhausted")
            if self._parent is not None:
                self._parent._reserve()
            self.used += 1

    def _score(self, candidate: Candidate, example: Any) -> tuple[float | None, SideInfo]:
        """Call the scorer, turning a crash into a metric error with no score.

        A scorer that raises on one version marks that version as bad — it
        must not kill the run (GEPA's ``raise_on_exception=False`` semantics,
        applied here so every engine gets them).

        Args:
            candidate: The version to score.
            example: The case to score it on.

        Returns:
            The score and side information, or ``(None, {"error": ..., "status": "metric_error"})``.

        Raises:
            ScorerAbortError: When the scorer asks to stop the run, or the
                metric answer has a wrong form before any version has a score.
        """
        try:
            score, side_info = self._scorer(candidate, example)
        except (ScorerAbortError, BudgetExhaustedError, BudgetExhausted, CostCeilingExceededError):
            raise
        except Exception as exc:
            # A wrong answer form usually comes on every version, so going on
            # spends the whole budget on errors. After a score, it is one version's error.
            if ANSWER_FORM in str(exc) and not self._scored:
                raise ScorerAbortError(str(exc)) from exc
            error = f"{type(exc).__name__}: {exc}"
            logger.warning("scorer raised on a candidate (%s): %s", METRIC_ERROR, error)
            return None, {"error": error, STATUS_KEY: METRIC_ERROR}
        # A nested server (an Auto lane) scores through the upstream server,
        # which hands back UNSCORED or a batch-filled number; the status marks
        # it as no score.
        # A scorer's own ``run_failed`` status never drops a score; only a
        # failed [run] entry, which sends no score, keeps it (SKY-751). In an
        # Auto lane that entry arrives as UNSCORED, not None.
        failed_run = is_run_failed(side_info) and (score is None or not math.isfinite(score))
        if score is None or is_metric_error(side_info) or failed_run:
            return None, {**side_info, STATUS_KEY: RUN_FAILED_STATUS if failed_run else METRIC_ERROR}
        return score, side_info

    def _record(self, candidate: Candidate, score: float | None, side_info: SideInfo) -> None:
        """Fold ``score`` into the candidate's running mean and keep its latest side info.

        Args:
            candidate: The scored version.
            score: Its score on the case just evaluated, or ``None`` after a
                metric error or a failed ``[run]`` entry (``status: run_failed``).
            side_info: What the scorer said about it on that case.
        """
        key = candidate_key(candidate)
        record = self._records.get(key)
        failed = score is None or is_metric_error(side_info)
        failed_run = score is None and side_info.get(STATUS_KEY) == RUN_FAILED_STATUS
        # A failed [run] entry carries its failure text as feedback, not as an error.
        fallback = (side_info.get("feedback") or RUN_FAILED_STATUS) if failed_run else METRIC_ERROR
        error = str(side_info.get("error") or fallback) if failed else None
        crashed = failed or "error" in side_info
        # A failed case adds nothing to the total: the version's mean is None.
        value = 0.0 if failed or score is None else float(score)
        if record is None:
            self._records[key] = CandidateRecord(
                candidate, value, 1, self._root.used, side_info, crashed, error, failed_run
            )
            return
        record.total += value
        record.count += 1
        record.side_info = side_info
        record.crashed = record.crashed or crashed
        # Any failed [run] case marks the version, whatever failed first, so
        # the error text then is the run failure's, to match its status.
        if failed_run and not record.run_failed:
            record.metric_error = error
            record.run_failed = True
        elif record.metric_error is None:
            record.metric_error = error

    @property
    def history(self) -> list[CandidateRecord]:
        """Return every distinct version this server scored, in first-seen order."""
        return list(self._records.values())

    def mean_score(self, candidate: Candidate) -> float | None:
        """Return the mean score recorded for ``candidate``, if any.

        Args:
            candidate: The version to look up.

        Returns:
            Its mean score across the cases scored so far, or ``None`` when it
            was not scored or hit a metric error.
        """
        record = self._records.get(candidate_key(candidate))
        return None if record is None else record.mean_score

    def metric_error(self, candidate: Candidate) -> str | None:
        """Return the error text of the metric error ``candidate`` hit, if any.

        Args:
            candidate: The version to look up.

        Returns:
            The first metric error's text, or ``None`` when the version was
            not scored or never hit one.
        """
        record = self._records.get(candidate_key(candidate))
        return None if record is None else record.metric_error

    def run_failed(self, candidate: Candidate) -> bool:
        """Return whether any case of ``candidate`` had a failed ``[run]`` entry (SKY-751).

        Args:
            candidate: The version to look up.

        Returns:
            ``True`` when a ``[run]`` entry of the version failed on any case,
            also after a metric crash, else ``False`` (also when it was not scored).
        """
        record = self._records.get(candidate_key(candidate))
        return record is not None and record.run_failed

    @property
    def best_candidate(self) -> Candidate | None:
        """Return the candidate with the highest mean score, if any was scored without a metric error."""
        scored = [record for record in self._records.values() if record.mean_score is not None]
        if not scored:
            return None
        return max(scored, key=lambda record: record.mean_score).candidate

    @property
    def best_score(self) -> float | None:
        """Return the highest mean score recorded, if any."""
        best = self.best_candidate
        return None if best is None else self.mean_score(best)
