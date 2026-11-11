"""Run one turn of an :class:`AgentProgram` on the Pi harness (SKY-327).

Pi owns the loop; this module keeps the program's contract:

* **Prompt.** The relay renders the program's own prompt with
  :class:`AppendOnlyChatAdapter` (system message, replayed history, the
  turn's input fields) and sends it in place of Pi's system and first user
  message. Pi's later messages (the turn's tool calls and results) follow.
  The tool roster on the wire is the program's, ``submit`` included.
* **Model calls.** Every call goes through the turn's metered ``dspy.LM``,
  so budgets, BYOK routing, usage metering and the served-model reveal are
  unchanged. Its stream chunks are relayed to Pi as they arrive.
* **Tools.** Each call runs the program's own tool object, so approvals,
  ``tool_start`` / ``tool_end`` events and wizard writes are unchanged.
* **Reply.** A ``submit`` the guard accepts ends the turn. A refusal is the
  tool result, as before. A ``submit`` beside another tool call is refused.
  At the loop budget the next call forces ``submit`` with the guard off.
  A program with several outputs whose turn ends in plain text gets one
  more forced ``submit`` call, as ``dspy.ReActV2`` made, so every output
  field is filled.
* **Steering.** A waiting steer joins the prompt as a user message before
  the next model call, from the second call on.
* **Models without native tool calls.** A local CLI subscription route
  (SKY-245) answers in DSPy's ``[[ ## tool_calls ## ]]`` text protocol. The
  relay renders that protocol with the same adapter, parses the reply with
  it and hands Pi the calls as one native tool-call chunk.

The setup agent and the workspace coding agent run here.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from typing import Any

import anyio
import dspy
import litellm
from dspy.adapters.base import _tool_result_content
from dspy.adapters.types.tool import ToolCalls
from dspy.utils.exceptions import AdapterParseError, format_error_for_lm

from ..optimization.retrying_react import PARSE_RETRY_ATTEMPTS
from .agent_program import SUBMIT_TOOL, AgentProgram, AppendOnlyChatAdapter
from .pi_bridge import PiEventTranslator, ToolOutcome, run_pi_turn
from .steering import STEER_APPLIED_EVENT, SteerInbox, claim_delivery

logger = logging.getLogger(__name__)

_DELIVERED = "Delivered to the user."
_SUBMIT_NOT_ALONE = (
    "Not delivered: `submit` was called beside other tools, before their results existed. "
    "Read those results, then call `submit` alone."
)


def _without_text_reasoning(signature: type[dspy.Signature]) -> type[dspy.Signature]:
    """Drop ``dspy.Reasoning`` output fields from the step signature.

    The text protocol would ask the model to write its thoughts as
    ``[[ ## next_thought ## ]]`` text, which Pi would read as the reply. Models
    that reason do so on the native channel, which Pi streams as thinking.

    Args:
        signature: The program's step signature.

    Returns:
        The signature without text reasoning fields.
    """
    for name, field in signature.output_fields.items():
        if field.annotation is dspy.Reasoning:
            signature = signature.delete(name)
    return signature


def _wire_message(message: dict[str, Any]) -> dict[str, Any] | None:
    """Keep the OpenAI chat fields of one of Pi's tail messages.

    Args:
        message: A message from Pi's request.

    Returns:
        The message with only portable fields, or ``None`` to drop it.
    """
    role = message.get("role")
    if role == "assistant":
        kept: dict[str, Any] = {"role": "assistant", "content": message.get("content") or None}
        if message.get("tool_calls"):
            kept["tool_calls"] = message["tool_calls"]
        return kept
    if role == "tool":
        return {"role": "tool", "tool_call_id": message.get("tool_call_id"), "content": message.get("content")}
    if role == "user":
        return {"role": "user", "content": message.get("content")}
    return None


def _chunk_json(chunk: Any) -> str:
    """Serialize one LiteLLM stream chunk for Pi.

    Args:
        chunk: A LiteLLM ``ModelResponseStream``.

    Returns:
        The chunk as OpenAI JSON.
    """
    return chunk.model_dump_json(exclude_none=True)


def _wire_call(index: int, call: Any) -> dict[str, Any]:
    """Render one LM tool call as an OpenAI stream tool-call delta.

    Args:
        index: The call's position in the message.
        call: A LiteLLM tool call object, or the same shape as a dict.

    Returns:
        The tool-call delta.
    """
    if isinstance(call, dict):
        function = call.get("function") or {}
        call_id, name, arguments = call.get("id"), function.get("name"), function.get("arguments")
    else:
        call_id, name, arguments = call.id, call.function.name, call.function.arguments
    return {
        "index": index,
        "id": call_id or f"call_{index}",
        "type": "function",
        "function": {"name": name, "arguments": arguments or "{}"},
    }


def output_chunk(output: Any) -> str:
    """Turn a whole LM output (a cache hit streams nothing) into one stream chunk.

    Args:
        output: One ``dspy.LM`` output: the text, or a dict with ``text``,
            ``reasoning_content`` and ``tool_calls``.

    Returns:
        One OpenAI stream chunk carrying the full message.
    """
    if not isinstance(output, dict):
        output = {"text": output}
    delta: dict[str, Any] = {"role": "assistant", "content": output.get("text") or ""}
    if output.get("reasoning_content"):
        delta["reasoning_content"] = output["reasoning_content"]
    calls = output.get("tool_calls") or []
    if calls:
        delta["tool_calls"] = [_wire_call(index, call) for index, call in enumerate(calls)]
    return json.dumps(
        {
            "id": "skynet-relay",
            "object": "chat.completion.chunk",
            "model": "skynet-relay",
            "choices": [{"index": 0, "delta": delta, "finish_reason": "tool_calls" if calls else "stop"}],
        }
    )


def stop_chunk() -> str:
    """Return a chunk that ends Pi's run with no output.

    Returns:
        One OpenAI stream chunk.
    """
    return json.dumps(
        {
            "id": "skynet-relay",
            "object": "chat.completion.chunk",
            "model": "skynet-relay",
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": "stop"}],
        }
    )


class AgentReplyFormatError(RuntimeError):
    """A model without native tool calls did not answer in the text protocol."""


class ReactOnPi:
    """The :class:`~.pi_bridge.PiTurnHandlers` of one :class:`AgentProgram` turn."""

    def __init__(
        self,
        react: AgentProgram,
        inputs: dict[str, Any],
        lm: dspy.LM,
        *,
        translator: PiEventTranslator,
        output_field: str,
        steer_inbox: SteerInbox | None = None,
    ) -> None:
        """Render the turn's prompt prefix and bind the program's tools.

        Args:
            react: The program; its tools, guarded ``submit`` and loop budget apply.
            inputs: The program's inputs, ``history`` included.
            lm: The turn's language model.
            translator: The turn's event translator; a refused submit takes
                back the reply it streamed.
            output_field: The ``submit`` argument that holds the reply.
            steer_inbox: The turn's steer inbox, or ``None``.
        """
        self._react = react
        self._lm = lm
        self._translator = translator
        self._output_field = output_field
        self._inbox = steer_inbox
        self._adapter = AppendOnlyChatAdapter()
        self._native = bool(getattr(lm, "supports_function_calling", False))
        self._lm_kwargs: dict[str, Any] = {"parallel_tool_calls": False} if self._native else {}
        signature = _without_text_reasoning(react.react.signature)
        call_inputs = {**inputs, "tools": list(react.tools.values())}
        self._signature = self._adapter._call_preprocess(lm, self._lm_kwargs, signature, call_inputs)
        self._prefix = self._adapter.format(self._signature, [], call_inputs)
        # The text protocol ends the prompt with the format reminder, which must stay last.
        self._reminder = None if self._native else self._prefix.pop()
        self._steers: list[tuple[int, dict[str, Any]]] = []
        self._calls = 0
        self._forcing = False
        self._last_tools: list[str] = []
        self._tail: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()
        self.reply: str | None = None
        self.outputs: dict[str, Any] | None = None

    def tool_specs(self) -> list[dict[str, Any]]:
        """Return the program's tools as the model sees them.

        Returns:
            One ``{name, description, parameters}`` dict per tool.
        """
        return [
            {
                "name": spec["function"]["name"],
                "description": spec["function"].get("description") or "",
                "parameters": spec["function"].get("parameters") or {"type": "object", "properties": {}},
            }
            for spec in self._lm_kwargs.get("tools")
            or [tool.format_as_litellm_function_call() for tool in self._react.tools.values()]
        ]

    async def call_tool(self, name: str, args: dict[str, Any]) -> ToolOutcome:
        """Run one tool call with the program's own tool object.

        Args:
            name: The tool name.
            args: The call's arguments.

        Returns:
            The result text for Pi.
        """
        async with self._lock:
            if name == SUBMIT_TOOL:
                return await self._submit(args)
            tool = self._react.tools.get(name)
            if tool is None:
                return ToolOutcome(f"Unknown tool: {name}", is_error=True)
            try:
                # Gated tools block on the event loop for approvals, so they run off it.
                value = await asyncio.to_thread(tool, **args)
            except Exception as error:
                return ToolOutcome(
                    f"Execution error in {name}: {format_error_for_lm(error, traceback_frames=5)}", is_error=True
                )
            return ToolOutcome(_tool_result_content(value))

    async def _submit(self, args: dict[str, Any]) -> ToolOutcome:
        """Run the guarded ``submit`` and end the turn when it is accepted.

        Args:
            args: The submit call's arguments.

        Returns:
            ``Delivered to the user.`` with ``terminate``, or the refusal.
        """
        if self.reply is not None:
            return ToolOutcome(_DELIVERED, terminate=True)
        if any(tool != SUBMIT_TOOL for tool in self._last_tools):
            self._translator.retract()
            return ToolOutcome(_SUBMIT_NOT_ALONE)
        self._react._forcing = self._forcing
        try:
            value = await asyncio.to_thread(self._react.tools[SUBMIT_TOOL], **args)
        except Exception as error:
            self._translator.retract()
            return ToolOutcome(
                f"Execution error in {SUBMIT_TOOL}: {format_error_for_lm(error, traceback_frames=5)}", is_error=True
            )
        finally:
            self._react._forcing = False
        if isinstance(value, dict):
            self.outputs = value
            self.reply = str(value.get(self._output_field) or "")
            return ToolOutcome(_DELIVERED, terminate=True)
        self._translator.retract()
        return ToolOutcome(_tool_result_content(value))

    async def _take_steers(self, anchor: int) -> None:
        """Move waiting steer messages into the prompt, at the current end.

        Args:
            anchor: How many of Pi's tail messages precede the steer.
        """
        inbox = self._inbox
        if inbox is None or self._calls < 2:
            return
        try:
            taken = await asyncio.to_thread(inbox.store.take, inbox.owner, inbox.steer_key, max_chars=inbox.max_chars)
        except Exception:
            logger.exception("Failed to read steer messages for an agent turn")
            return
        if not taken or not await asyncio.to_thread(claim_delivery, inbox, taken):
            return
        text = "\n\n".join(message for _, message in taken)
        content = self._adapter.format_user_message_content(self._signature, {inbox.input_field: text})
        self._steers.append((anchor, {"role": "user", "content": content}))
        inbox.emit(
            {"event": STEER_APPLIED_EVENT, "data": {"ids": [message_id for message_id, _ in taken], "text": text}}
        )

    def _messages(self, tail: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Assemble the prompt: the rendered prefix, Pi's tail, and steers at their places.

        Args:
            tail: Pi's messages after its first user message.

        Returns:
            The chat messages for the model.
        """
        messages = list(self._prefix)
        steers = iter(self._steers)
        pending = next(steers, None)
        for position, message in enumerate([*tail, None]):
            while pending is not None and pending[0] == position:
                messages.append(pending[1])
                pending = next(steers, None)
            if message is not None:
                messages.append(message)
        if self._reminder is not None:
            messages.append(self._reminder)
        return messages

    def _call_kwargs(self) -> dict[str, Any]:
        """Return the LM kwargs for the next call, forcing ``submit`` at the budget.

        Returns:
            The kwargs.
        """
        kwargs = dict(self._lm_kwargs)
        if self._forcing:
            kwargs.pop("parallel_tool_calls", None)
            kwargs["tool_choice"] = {"type": "function", "function": {"name": SUBMIT_TOOL}}
            kwargs["reasoning_effort"] = None
        return kwargs

    async def complete(self, body: dict[str, Any]) -> AsyncIterator[str]:
        """Answer one of Pi's model calls through the turn's metered LM.

        Args:
            body: Pi's chat-completions request.

        Yields:
            OpenAI stream chunks as JSON strings.
        """
        self._calls += 1
        if self.reply is not None or self._calls > self._react.max_iters + 1:
            yield stop_chunk()
            return
        self._forcing = self._calls == self._react.max_iters + 1
        raw = body.get("messages") or []
        first_user = next((i for i, message in enumerate(raw) if message.get("role") == "user"), len(raw))
        tail = [kept for message in raw[first_user + 1 :] if (kept := _wire_message(message))]
        self._tail = tail
        await self._take_steers(len(tail))
        messages = self._messages(tail)
        if not self._native:
            yield await self._text_protocol_reply(messages)
            return
        kwargs = self._call_kwargs()
        send, receive = anyio.create_memory_object_stream[Any](max_buffer_size=1024)
        names: dict[int, str] = {}
        streamed = False

        def call() -> Any:
            """Run the LM call with its chunks sent to the relay stream.

            Returns:
                The LM's outputs.
            """
            with dspy.context(send_stream=send):
                return self._lm(messages=messages, **kwargs)

        async def run() -> Any:
            """Run the call off the event loop and close the stream after it.

            Returns:
                The LM's outputs.
            """
            async with send:
                return await anyio.to_thread.run_sync(call)

        task = asyncio.ensure_future(run())
        async with receive:
            async for chunk in receive:
                streamed = True
                for choice in getattr(chunk, "choices", None) or []:
                    for call_delta in getattr(choice.delta, "tool_calls", None) or []:
                        name = getattr(call_delta.function, "name", None) if call_delta.function else None
                        if name:
                            names[call_delta.index or 0] = name
                yield _chunk_json(chunk)
        outputs = await task
        if not streamed:
            output = outputs[0] if outputs else ""
            for index, call in enumerate((output.get("tool_calls") or []) if isinstance(output, dict) else []):
                names[index] = _wire_call(index, call)["function"]["name"]
            yield output_chunk(output)
        self._last_tools = [names[index] for index in sorted(names)]

    async def force_submit(self, final_text: str) -> None:
        """Make one ``submit`` call with the guard off after a plain-text final message.

        The call goes through the turn's metered LM with ``submit`` forced, as
        ``dspy.ReActV2``'s forced submit did. A reply that still has no
        ``submit`` leaves :attr:`outputs` unset. Only a native model can end
        in plain text: the text protocol answers with tool calls or raises.

        Args:
            final_text: The text of the model's final message.
        """
        if not self._native:
            return
        self._forcing = True
        tail = [*self._tail, {"role": "assistant", "content": final_text}] if final_text else self._tail
        try:
            outputs = await asyncio.to_thread(self._lm, messages=self._messages(tail), **self._call_kwargs())
        except litellm.ContextWindowExceededError as error:
            logger.warning("Forced submit failed: %s", error)
            return
        output = outputs[0] if outputs else ""
        calls = (output.get("tool_calls") or []) if isinstance(output, dict) else []
        wire = [_wire_call(index, call)["function"] for index, call in enumerate(calls)]
        submit = next((call for call in wire if call["name"] == SUBMIT_TOOL), None)
        if submit is None:
            logger.warning("Forced submit returned no submit call")
            return
        self._last_tools = [SUBMIT_TOOL]
        await self._submit(json.loads(submit["arguments"] or "{}"))

    async def _text_protocol_reply(self, messages: list[dict[str, Any]]) -> str:
        """Call a model without native tool calls and hand Pi its calls natively.

        The model writes DSPy's ``[[ ## tool_calls ## ]]`` text protocol, as it
        did in the DSPy loop. A reply that does not parse is resampled with a
        fresh ``rollout_id`` so the LM cache does not return it again.

        Args:
            messages: The rendered prompt.

        Returns:
            One OpenAI stream chunk with the parsed tool calls.

        Raises:
            AgentReplyFormatError: When no attempt yields a tool call.
        """
        field = self._adapter._get_tool_call_output_field_name(self._signature)
        attempts = PARSE_RETRY_ATTEMPTS + 1
        problem = ""
        for attempt in range(attempts):
            kwargs = {"rollout_id": attempt} if attempt else {}
            outputs = await asyncio.to_thread(self._lm, messages=messages, **kwargs)
            output = outputs[0] if outputs else ""
            text = (output.get("text") if isinstance(output, dict) else output) or ""
            try:
                parsed = self._adapter.parse(self._signature, text)
            except AdapterParseError as error:
                problem = str(error)
                logger.warning("Agent text-protocol reply did not parse (attempt %d/%d)", attempt + 1, attempts)
                continue
            calls = parsed.get(field)
            calls = calls.tool_calls if isinstance(calls, ToolCalls) else []
            if self._forcing:
                calls = [call for call in calls if call.name == SUBMIT_TOOL]
            if calls:
                self._last_tools = [call.name for call in calls]
                wire = [
                    {
                        "id": call.id or f"call_{self._calls}_{index}",
                        "function": {"name": call.name, "arguments": json.dumps(call.args or {})},
                    }
                    for index, call in enumerate(calls)
                ]
                return output_chunk({"text": "", "tool_calls": wire})
            problem = "the reply has no tool call"
        raise AgentReplyFormatError(
            f"The model did not answer in the tool-call text protocol after {attempts} attempts: {problem}"
        )


async def run_react_turn_on_pi(
    react: AgentProgram,
    inputs: dict[str, Any],
    lm: dspy.LM,
    *,
    emit: Callable[[dict[str, Any]], None],
    output_field: str,
    input_field: str,
    binary: str,
    pending_skip: frozenset[str],
    steer_inbox: SteerInbox | None = None,
) -> str:
    """Run one program turn on Pi and return the reply.

    Args:
        react: The program.
        inputs: The program's inputs, ``history`` included.
        lm: The turn's language model.
        emit: Sends one stream event.
        output_field: The ``submit`` argument that holds the reply.
        input_field: The input that carries the user's message.
        binary: The ``pi`` executable.
        pending_skip: Tools that get no ``tool_pending`` row.
        steer_inbox: The turn's steer inbox, or ``None``.

    Returns:
        The accepted ``submit`` reply, else the text of the model's final
        message, else an empty string.
    """
    handlers, translator = await _run_program(
        react,
        inputs,
        lm,
        emit=emit,
        output_field=output_field,
        input_field=input_field,
        binary=binary,
        pending_skip=pending_skip,
        steer_inbox=steer_inbox,
    )
    if handlers.reply is not None:
        return handlers.reply
    return translator.text_reply


async def run_react_program_on_pi(
    react: AgentProgram,
    inputs: dict[str, Any],
    lm: dspy.LM,
    *,
    input_field: str,
    binary: str,
) -> dict[str, Any] | str:
    """Run a program with several outputs on Pi, with nothing streamed to the user.

    Args:
        react: The program.
        inputs: The program's inputs.
        lm: The turn's language model.
        input_field: The input that Pi sends as its first user message.
        binary: The ``pi`` executable.

    Returns:
        The accepted ``submit`` outputs, from a forced ``submit`` when the
        model ended in plain text, else the text of the model's final message.
    """
    handlers, translator = await _run_program(
        react,
        inputs,
        lm,
        emit=lambda _event: None,
        output_field="",
        input_field=input_field,
        binary=binary,
        pending_skip=frozenset(),
        steer_inbox=None,
    )
    if handlers.outputs is not None:
        return handlers.outputs
    return translator.text_reply


async def _run_program(
    react: AgentProgram,
    inputs: dict[str, Any],
    lm: dspy.LM,
    *,
    emit: Callable[[dict[str, Any]], None],
    output_field: str,
    input_field: str,
    binary: str,
    pending_skip: frozenset[str],
    steer_inbox: SteerInbox | None,
) -> tuple[ReactOnPi, PiEventTranslator]:
    """Drive one program turn on Pi.

    Args:
        react: The program.
        inputs: The program's inputs.
        lm: The turn's language model.
        emit: Sends one stream event.
        output_field: The ``submit`` argument that holds the reply.
        input_field: The input that Pi sends as its first user message.
        binary: The ``pi`` executable.
        pending_skip: Tools that get no ``tool_pending`` row.
        steer_inbox: The turn's steer inbox, or ``None``.

    Returns:
        The turn's handlers and translator, after Pi stops.
    """
    translator = PiEventTranslator(emit, reply_tool=SUBMIT_TOOL, reply_arg=output_field, pending_skip=pending_skip)
    handlers = ReactOnPi(react, inputs, lm, translator=translator, output_field=output_field, steer_inbox=steer_inbox)
    await run_pi_turn(
        binary=binary, handlers=handlers, translator=translator, prompt=str(inputs.get(input_field) or "")
    )
    # A single output is the final text itself; with several, plain text would
    # leave the other fields empty.
    if handlers.outputs is None and len(react.signature.output_fields) > 1:
        await handlers.force_submit(translator.text_reply)
    return handlers, translator


__all__ = [
    "AgentReplyFormatError",
    "ReactOnPi",
    "output_chunk",
    "run_react_program_on_pi",
    "run_react_turn_on_pi",
    "stop_chunk",
]
