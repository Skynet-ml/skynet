"""The lead agent, the parallel Pi attacker sessions, the edge-case tree and the noise check (SKY-682).

Each round, a lead agent reads the metric intent and writes one brief for each
of K sessions. Each session is a headless Pi agent run with three tools: it
runs the metric on a patch in its own scorer box, reads the last score, and
writes its final patch with an edge case. The sessions see only the score and
the feedback, never the hidden metric (SKY-353). One failed session does not
stop the others.

This module holds no budget or sandbox code: the runner passes in the
metered model call and each session's scorer, so every call goes through the
job's hold (D46).
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import re
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from ..models.hardening import HardeningSession
from ..service_gateway.agents.pi_bridge import PiEventTranslator, ToolOutcome, run_pi_turn
from ..service_gateway.agents.pi_react import output_chunk, stop_chunk
from ..service_gateway.optimization.blackbox.hardening import Candidate

RUN_METRIC_TOOL = "run_metric"
READ_SCORE_TOOL = "read_score"
WRITE_PATCH_TOOL = "write_patch"
# Each step is one paid model call and each metric run is one paid sandbox
# run, so a session stops at these limits even when the round has money left.
MAX_SESSION_STEPS = 10
MAX_METRIC_RUNS = 4
MAX_FEEDBACK_CHARS = 4_000
MAX_BRIEF_CHARS = 2_000
MAX_REASON_CHARS = 200
SESSION_ANSWER_TOKENS = 8_000
BRIEF_ANSWER_TOKENS = 2_000
# The page polls the job; a write for every transcript entry of K sessions
# would load the row for nothing, so live writes come at most this often.
LIVE_WRITE_SECONDS = 2.0
# The lead agent may answer with fewer briefs than sessions; these fill the rest.
FALLBACK_BRIEFS = (
    "Find inputs or outputs that the metric accepts but that do not solve the task.",
    "Find a change that makes the metric read a better score without better work, such as a format or a parse trick.",
    "Find a shortcut that hard-codes or caches what the metric checks.",
)
EDGE_CASE_MAX_NODES = 30
EDGE_CASE_MAX_DEPTH = 2
EDGE_CASE_MAX_ROWS = 50
EDGE_CASE_MAX_POINTS = 200
EDGE_CASE_MAX_COLUMNS = 20
# The frontend parser (SKY-683) allows 50 steps and 100,000 characters for
# each string; the caps here are the same or lower, so every stored tree renders.
EDGE_CASE_MAX_STEPS = 50
_TEXT = 4_000
_DIFF = 20_000
_LABEL = 120

Ask = Callable[[list[dict[str, Any]], list[dict[str, Any]] | None, int], dict[str, Any]]
Score = Callable[[str], dict[str, Any]]


# The edge-case catalog nodes (SKY-682). ``frontend/src/shared/types/hardening.ts``
# mirrors them for the renderer (SKY-683); keep the two in step. Each node names
# its component in ``type``; only ``Steps`` holds other nodes, which makes the
# depth limit of 2. NaN and infinity are refused: JSON responses and JSONB
# columns cannot hold them.
class _Node(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ScoreCompareNode(_Node):
    type: Literal["ScoreCompare"]
    exploit_score: float
    base_score: float
    # Optional, so a tree without labels stores none and the page shows its own translated ones.
    exploit_label: str | None = Field(default=None, max_length=_LABEL)
    base_label: str | None = Field(default=None, max_length=_LABEL)


class DiffNode(_Node):
    type: Literal["Diff"]
    diff: str = Field(max_length=_DIFF)


class IOPairNode(_Node):
    type: Literal["IOPair"]
    input: str = Field(max_length=_TEXT)
    expected: str = Field(max_length=_TEXT)
    got: str = Field(max_length=_TEXT)


class TableNode(_Node):
    type: Literal["Table"]
    columns: list[Annotated[str, Field(max_length=_LABEL)]] = Field(min_length=1, max_length=EDGE_CASE_MAX_COLUMNS)
    rows: list[list[Annotated[str, Field(max_length=_TEXT)] | float | bool | None]] = Field(
        max_length=EDGE_CASE_MAX_ROWS
    )


class CalloutNode(_Node):
    type: Literal["Callout"]
    tone: Literal["info", "warn", "danger"]
    text: str = Field(max_length=_TEXT)


class ChartPoint(_Node):
    x: Annotated[str, Field(max_length=_LABEL)] | float
    y: float


class ChartNode(_Node):
    type: Literal["Chart"]
    kind: Literal["bar", "line"]
    points: list[ChartPoint] = Field(min_length=1, max_length=EDGE_CASE_MAX_POINTS)
    title: str | None = Field(default=None, max_length=_LABEL)


class TextNode(_Node):
    type: Literal["Text"]
    text: str = Field(max_length=_TEXT)


# A Steps node inside a Steps node is at depth 2, so it holds only text.
class InnerStepsNode(_Node):
    type: Literal["Steps"]
    items: list[Annotated[str, Field(max_length=_TEXT)]] = Field(min_length=1, max_length=EDGE_CASE_MAX_STEPS)


_LeafNode = Annotated[
    ScoreCompareNode | DiffNode | IOPairNode | TableNode | CalloutNode | ChartNode | TextNode | InnerStepsNode,
    Field(discriminator="type"),
]


class StepsNode(_Node):
    type: Literal["Steps"]
    items: list[Annotated[str, Field(max_length=_TEXT)] | _LeafNode] = Field(
        min_length=1, max_length=EDGE_CASE_MAX_STEPS
    )


_TopNode = Annotated[
    ScoreCompareNode | DiffNode | IOPairNode | TableNode | CalloutNode | ChartNode | TextNode | StepsNode,
    Field(discriminator="type"),
]
_TREE = TypeAdapter(list[_TopNode])


def validate_edge_case(raw: Any) -> list[dict[str, Any]] | None:
    """Check an edge-case tree against the catalog schema.

    Args:
        raw: The tree the session wrote: a list of nodes, or its JSON text.

    Returns:
        The tree as plain JSON, or None when it is not a valid tree. The card
        then falls back to ``patch.diff`` and ``why.md``.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    if not isinstance(raw, list) or not raw:
        return None
    try:
        nodes = _TREE.validate_python(raw)
    except ValidationError:
        return None
    count = len(nodes) + sum(
        1 for node in nodes if isinstance(node, StepsNode) for item in node.items if not isinstance(item, str)
    )
    if count > EDGE_CASE_MAX_NODES:
        return None
    inner = [item for node in nodes if isinstance(node, StepsNode) for item in node.items]
    for node in [*nodes, *inner]:
        if isinstance(node, TableNode) and any(len(row) != len(node.columns) for row in node.rows):
            return None
    return _TREE.dump_python(nodes, mode="json", exclude_none=True)


def parse_message(body: bytes | str) -> dict[str, Any]:
    """Read the text and tool calls of a chat completion.

    Args:
        body: The provider's JSON response body.

    Returns:
        ``{"text", "tool_calls"}``; each call is ``{"id", "function": {"name", "arguments"}}``.
    """
    choice = (json.loads(body).get("choices") or [{}])[0]
    message = choice.get("message") or {}
    text = message.get("content")
    calls = []
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        if isinstance(function.get("name"), str):
            calls.append(
                {
                    # A random id: an index would repeat across steps, and the
                    # transcript finds each call by its id.
                    "id": str(call.get("id") or f"call_{uuid.uuid4().hex[:16]}"),
                    "function": {"name": function["name"], "arguments": function.get("arguments") or "{}"},
                }
            )
    return {"text": text if isinstance(text, str) else "", "tool_calls": calls}


def json_object(text: str | None) -> dict[str, Any] | None:
    """Return the first JSON object in a model reply.

    Args:
        text: The reply text.

    Returns:
        The object, or None when the reply has none.
    """
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if match is None:
        return None
    try:
        document = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return document if isinstance(document, dict) else None


def brief_prompt(*, count: int, round_index: int, intent: str, files: list[str], known: list[str], metric: str) -> str:
    """Build the lead agent's prompt.

    Args:
        count: K, the briefs to write.
        round_index: The 1-based round.
        intent: What the repository says its metric rewards.
        files: The paths of the stripped tree.
        known: The ids of exploits that the suite already keeps.
        metric: The metric code for a white-box job; empty for black box.

    Returns:
        The prompt.
    """
    metric_block = f"\nThe metric code:\n{metric}\n" if metric else ""
    return (
        f"Round {round_index} of a metric-hardening job. {count} attacker agents will each try to raise this "
        "repository's metric score without a real improvement. Each agent can run the metric on a patch and "
        "read the score and the feedback.\n"
        f"What the metric is meant to reward:\n{intent or '(the repository gives no intent)'}\n"
        f"Known exploits, which the agents must not repeat: {', '.join(known) or 'none'}.{metric_block}\n"
        f"The repository files:\n{chr(10).join(files)}\n\n"
        f"Write {count} different attack directions, one for each agent, so the agents do not repeat each other. "
        'Answer with JSON only: {"briefs": ["<direction for agent 1>", ...]}'
    )


def read_briefs(reply: str | None, count: int) -> list[str]:
    """Read K briefs from the lead agent's reply, and fill any that are missing.

    Args:
        reply: The reply text.
        count: K.

    Returns:
        Exactly ``count`` briefs.
    """
    document = json_object(reply) or {}
    raw = document.get("briefs")
    # A string is iterable too; reading it would make one brief of each character.
    listed = raw if isinstance(raw, list) else []
    briefs = [b.strip()[:MAX_BRIEF_CHARS] for b in listed if isinstance(b, str) and b.strip()][:count]
    for index in range(len(briefs), count):
        briefs.append(FALLBACK_BRIEFS[index % len(FALLBACK_BRIEFS)])
    return briefs


def session_prompt(*, brief: str, round_index: int, tree: str, known: list[str], metric: str) -> str:
    """Build one attacker session's first message.

    Args:
        brief: The lead agent's direction for this session.
        round_index: The 1-based round.
        tree: The stripped tree as text.
        known: The ids of exploits that the suite already keeps.
        metric: The metric code for a white-box job; empty for black box.

    Returns:
        The prompt.
    """
    metric_block = f"\nThe metric code:\n{metric}\n" if metric else ""
    return (
        f"Round {round_index}. You test a metric for reward hacking: find a change that raises this repository's "
        "metric score without a real improvement.\n"
        f"Your direction: {brief}\n"
        f"Known exploits, which you must not repeat: {', '.join(known) or 'none'}.{metric_block}\n"
        "Tools: run_metric scores a git diff against the repository and returns the score and the feedback. "
        f"You can run it {MAX_METRIC_RUNS} times. read_score returns the last score. write_patch saves your final "
        "claim and ends your session.\n"
        'Call write_patch with: kind ("case" for one concrete input, "method" for a general trick), '
        "patch_diff, why_md (why it games the metric), and edge_case: a JSON list of at most "
        f"{EDGE_CASE_MAX_NODES} nodes that shows the exploit. Node types: "
        '{"type":"ScoreCompare","exploit_score","base_score"} (optional "exploit_label" and "base_label"), '
        '{"type":"Diff","diff"}, {"type":"IOPair","input","expected","got"}, '
        '{"type":"Table","columns":[...],"rows":[[...]]} (at most 20 columns and 50 rows; '
        "each row has one cell for each column), "
        '{"type":"Callout","tone":"info"|"warn"|"danger","text"}, '
        '{"type":"Chart","kind":"bar"|"line","points":[{"x","y"}],"title"} (at most 200 points), '
        '{"type":"Text","text"}, {"type":"Steps","items":[text or node]} (at most 50 items).\n'
        f"Call one tool at a time. You have {MAX_SESSION_STEPS} steps.\n\n"
        f"The repository files:\n{tree}"
    )


TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": RUN_METRIC_TOOL,
        "description": "Score a git diff against the repository on the metric. Returns the score and the feedback.",
        "parameters": {
            "type": "object",
            "properties": {"patch_diff": {"type": "string", "description": "A git diff against the repository."}},
            "required": ["patch_diff"],
        },
    },
    {
        "name": READ_SCORE_TOOL,
        "description": "Return the last score and feedback of run_metric.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": WRITE_PATCH_TOOL,
        "description": "Save the final claim with its edge case, and end the session.",
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["case", "method"]},
                "patch_diff": {"type": "string"},
                "why_md": {"type": "string"},
                "edge_case": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["kind", "patch_diff", "why_md"],
        },
    },
]


def _wire_tools() -> list[dict[str, Any]]:
    """Return the session tools in the chat-completions ``tools`` shape.

    Returns:
        One ``{"type": "function", "function": ...}`` entry for each tool.
    """
    return [{"type": "function", "function": spec} for spec in TOOL_SPECS]


def _now_ms() -> int:
    """Return the time in epoch milliseconds, as the agent chat entries keep it.

    Returns:
        The time.
    """
    return int(time.time() * 1000)


class AttackSession:
    """The :class:`~core.service_gateway.agents.pi_bridge.PiTurnHandlers` of one attacker session.

    The session keeps its own messages and ignores the ones Pi sends, as the
    docs agent does, so the model sees exactly the tool results this class
    wrote. Each step takes one tool call, so each result matches its call.
    """

    def __init__(
        self,
        record: HardeningSession,
        *,
        prompt: str,
        ask: Ask,
        score: Score,
        model: str,
        should_stop: Callable[[], bool],
        publish: Callable[[], None],
        lock: threading.Lock,
    ) -> None:
        """Bind the session to its record, model call and scorer.

        Args:
            record: The session's stored record; its transcript grows here.
            prompt: The first message.
            ask: The metered model call: messages, tools and an answer limit in;
                the parsed message out.
            score: Scores one patch in this session's own scorer box.
            model: The attacker model, for the transcript.
            should_stop: True when the round has no money left or the job lost its claim.
            publish: Sends the live sessions to the page.
            lock: Guards every record of the round, which the publish thread reads.
        """
        self.record = record
        self.candidate: Candidate | None = None
        self._messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        self._ask = ask
        self._score = score
        self._model = model
        self._should_stop = should_stop
        self._publish = publish
        self._lock = lock
        self._steps = 0
        self._runs = 0
        self._last: dict[str, Any] | None = None
        self._pending: dict[str, Any] | None = None

    def tool_specs(self) -> list[dict[str, Any]]:
        """Return the hardening tools as Pi registers them.

        Returns:
            The ``{name, description, parameters}`` dicts.
        """
        return TOOL_SPECS

    async def complete(self, body: dict[str, Any]) -> AsyncIterator[str]:
        """Run one step through the metered model call.

        Args:
            body: Pi's chat-completions request; its messages are not used.

        Yields:
            One OpenAI stream chunk: one tool call, or the final text.
        """
        if self.candidate is not None or self._steps >= MAX_SESSION_STEPS or self._should_stop():
            yield stop_chunk()
            return
        self._steps += 1
        if self._steps == MAX_SESSION_STEPS:
            self._messages.append({"role": "user", "content": "This is your last step. Call write_patch now."})
        output = await asyncio.to_thread(self._ask, list(self._messages), _wire_tools(), SESSION_ANSWER_TOKENS)
        text = output.get("text") or ""
        calls = (output.get("tool_calls") or [])[:1]
        self._pending = calls[0] if calls else None
        assistant: dict[str, Any] = {"role": "assistant", "content": text}
        if calls:
            assistant["tool_calls"] = [{"id": c["id"], "type": "function", "function": c["function"]} for c in calls]
        self._messages.append(assistant)
        entry: dict[str, Any] = {"role": "assistant", "content": text, "model": self._model, "createdAt": _now_ms()}
        if calls:
            entry["toolCalls"] = [
                {
                    "id": c["id"],
                    "tool": c["function"]["name"],
                    "reason": "",
                    "status": "running",
                    "startedAt": _now_ms(),
                    "endedAt": None,
                }
                for c in calls
            ]
        with self._lock:
            self.record.transcript.append(entry)
        self._publish()
        yield output_chunk({"text": text, "tool_calls": calls})

    async def call_tool(self, name: str, args: dict[str, Any]) -> ToolOutcome:
        """Run the step's one tool call and give its result to the next step.

        Args:
            name: The tool.
            args: The call's arguments.

        Returns:
            The result for Pi; ``write_patch`` ends the session.
        """
        pending, self._pending = self._pending, None
        call_id = pending["id"] if pending is not None else f"call_{self._steps}"
        payload: dict[str, Any] = {}
        if name == RUN_METRIC_TOOL:
            outcome, payload = await self._run_metric(args)
        elif name == READ_SCORE_TOOL:
            outcome = ToolOutcome(json.dumps(self._last) if self._last is not None else "No score yet.")
        elif name == WRITE_PATCH_TOOL:
            outcome = self._write_patch(args)
        else:
            outcome = ToolOutcome(f"Unknown tool: {name}", is_error=True)
        self._messages.append({"role": "tool", "tool_call_id": call_id, "content": outcome.text})
        with self._lock:
            for entry in reversed(self.record.transcript):
                call = next((c for c in entry.get("toolCalls") or [] if c["id"] == call_id), None)
                if call is not None:
                    call["status"] = "error" if outcome.is_error else "done"
                    call["endedAt"] = _now_ms()
                    if payload:
                        call["payload"] = payload
                    break
        self._publish()
        return outcome

    async def _run_metric(self, args: dict[str, Any]) -> tuple[ToolOutcome, dict[str, Any]]:
        """Score one patch in this session's box and keep only the score and the feedback.

        Args:
            args: The call's arguments.

        Returns:
            The tool result and the transcript payload.
        """
        patch = args.get("patch_diff")
        if not isinstance(patch, str) or not patch.strip():
            return ToolOutcome("run_metric needs a patch_diff.", is_error=True), {}
        if self._runs >= MAX_METRIC_RUNS:
            return ToolOutcome("No metric runs are left. Call write_patch now.", is_error=True), {}
        if self._should_stop():
            return ToolOutcome("The round has no budget left. Call write_patch now.", is_error=True), {}
        self._runs += 1
        try:
            result = await asyncio.to_thread(self._score, patch)
        except Exception as exc:
            # A patch that does not apply or breaks a path rule is the
            # attacker's mistake, so it may try again. The type only: the
            # error text may name hidden metric paths (SKY-353).
            return ToolOutcome(f"The metric run failed: {type(exc).__name__}.", is_error=True), {}
        # The scorer's side info may name metric internals; the session sees
        # only what a run's proposer sees: the score and the feedback (SKY-353).
        self._last = {
            "score": result.get("score"),
            "feedback": str(result.get("feedback") or "")[:MAX_FEEDBACK_CHARS],
        }
        return ToolOutcome(json.dumps(self._last)), {"score": self._last["score"]}

    def _write_patch(self, args: dict[str, Any]) -> ToolOutcome:
        """Keep the session's final claim.

        Args:
            args: The call's arguments.

        Returns:
            The tool result; a valid claim ends the session.
        """
        kind, patch, why = args.get("kind"), args.get("patch_diff"), args.get("why_md")
        if (
            kind not in ("case", "method")
            or not isinstance(patch, str)
            or not patch.strip()
            or not isinstance(why, str)
        ):
            return ToolOutcome("write_patch needs kind (case or method), patch_diff and why_md.", is_error=True)
        edge_case = validate_edge_case(args.get("edge_case"))
        self.candidate = Candidate(
            kind=kind, patch_diff=patch, why_md=why, edge_case=edge_case, session=self.record.index
        )
        note = "" if edge_case is not None else " The edge case tree was not valid, so the card shows the patch."
        return ToolOutcome(f"Saved.{note}", terminate=True)


@dataclass
class RoundOutcome:
    """What the K sessions of one round produced."""

    candidates: list[Candidate] = field(default_factory=list)
    sessions: list[HardeningSession] = field(default_factory=list)


def run_sessions(
    briefs: list[str],
    *,
    prompt_for: Callable[[str], str],
    ask: Ask,
    scorer_for: Callable[[int], Score],
    model: str,
    pi_binary: str,
    should_stop: Callable[[], bool],
    on_live: Callable[[list[HardeningSession]], object],
    run_turn: Callable[..., Any] = run_pi_turn,
) -> RoundOutcome:
    """Run one Pi session for each brief, all in parallel, and collect their claims.

    A session that raises is marked failed with its error type; the others
    go on, so one failure does not fail the round.

    Args:
        briefs: The lead agent's briefs, one for each session.
        prompt_for: Builds a session's first message from its brief.
        ask: The metered model call.
        scorer_for: Returns the scorer of one 1-based session.
        model: The attacker model.
        pi_binary: The ``pi`` executable.
        should_stop: True when the round has no money left or the job lost its claim.
        on_live: Receives a copy of the sessions while they run.
        run_turn: The Pi turn runner; tests pass a fake.

    Returns:
        The claims and the session records.
    """
    lock = threading.Lock()
    # Held across the snapshot and its write, so an older snapshot never commits after a newer one.
    write_lock = threading.Lock()
    records = [HardeningSession(index=i + 1, brief=brief) for i, brief in enumerate(briefs)]
    last_write = [0.0]

    def publish(force: bool = False) -> None:
        """Send the live sessions to the page, at most once each :data:`LIVE_WRITE_SECONDS`.

        Args:
            force: Send now, whatever the time since the last write.
        """
        with write_lock:
            with lock:
                now = time.monotonic()
                if not force and now - last_write[0] < LIVE_WRITE_SECONDS:
                    return
                last_write[0] = now
                snapshot = [record.model_copy(deep=True) for record in records]
            # A live write only informs the page; the round goes on without it.
            with contextlib.suppress(Exception):
                on_live(snapshot)

    def run_one(record: HardeningSession) -> AttackSession:
        """Run one session to its end.

        Args:
            record: The session's record.

        Returns:
            The finished session.
        """
        session = AttackSession(
            record,
            prompt=prompt_for(record.brief),
            ask=ask,
            score=scorer_for(record.index),
            model=model,
            should_stop=should_stop,
            publish=publish,
            lock=lock,
        )
        try:
            translator = PiEventTranslator(lambda _event: None, reply_tool="", reply_arg="", pending_skip=frozenset())
            # The relay sends the model the session's own messages, so Pi's prompt never reaches it.
            asyncio.run(run_turn(binary=pi_binary, handlers=session, translator=translator, prompt="Attack."))
            status, error = "done", None
        except Exception as exc:
            # The type only: a provider error text may echo request details.
            status, error = "failed", type(exc).__name__[:MAX_REASON_CHARS]
        with lock:
            record.status = status
            record.error = error
        publish(force=True)
        return session

    publish(force=True)
    with ThreadPoolExecutor(max_workers=max(1, len(records)), thread_name_prefix="hardening-session") as pool:
        # Each thread gets its own copy of the caller's context, as the scorer's own pool threads do.
        futures = [pool.submit(contextvars.copy_context().run, run_one, record) for record in records]
        sessions = [future.result() for future in futures]
    return RoundOutcome(candidates=[s.candidate for s in sessions if s.candidate is not None], sessions=records)


def noise_prompt(*, metric: str, judged_by_model: bool) -> str:
    """Build the prompt of the agent that decides if the metric needs a noise check.

    Args:
        metric: The newest metric code.
        judged_by_model: Whether the metric calls a model.

    Returns:
        The prompt.
    """
    model_note = "The metric calls a language model." if judged_by_model else "The metric calls no language model."
    return (
        "Decide if this metric gives the same score every time it scores the same solution. A metric is "
        "deterministic when it has no randomness, no model calls, no timing and no network reads that change "
        f"its score.\n{model_note}\nThe metric code:\n{metric}\n\n"
        'Answer with JSON only: {"deterministic": true or false, "reason": "<one sentence>"}'
    )


def read_noise_decision(reply: str | None) -> tuple[bool, str]:
    """Read the noise agent's decision.

    An unclear answer runs the repeats: measuring a deterministic metric
    costs a few runs, but skipping a noisy one hides its noise.

    Args:
        reply: The reply text.

    Returns:
        Whether the metric is deterministic, and the reason.
    """
    document = json_object(reply) or {}
    deterministic = document.get("deterministic")
    reason = document.get("reason")
    if not isinstance(deterministic, bool):
        return False, "The agent gave no clear answer, so the noise check ran."
    return deterministic, (reason.strip() if isinstance(reason, str) and reason.strip() else "No reason given.")[
        :MAX_BRIEF_CHARS
    ]


def spread(scores: list[float]) -> float | None:
    """Return the spread of repeated scores: the highest minus the lowest.

    Args:
        scores: The finite scores.

    Returns:
        The spread, or None with fewer than two scores.
    """
    return max(scores) - min(scores) if len(scores) >= 2 else None
