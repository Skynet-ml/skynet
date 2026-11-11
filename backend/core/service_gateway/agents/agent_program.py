"""Agent program definitions and their cache-stable prompt rendering (SKY-327).

The Pi harness runs every agent loop. This module holds what Pi does not
own: the program definition (tools, ``submit`` and the step signature), the
adapter that renders its prompt, and the replay of earlier turns as history.

Stock DSPy's text ``ChatAdapter`` rewrites the start of the prompt on every
call: the tool roster and the output-format reminder sit in the first user
message and move behind the history later. Provider prompt caches match on
the prefix, so nearly every input token is billed fresh. The adapter here
keeps the message list append-only instead: tools travel on the provider's
native ``tools=`` channel, and the format reminder never sits inside a
message that later calls replay.
"""

from __future__ import annotations

import textwrap
from collections.abc import Callable, Iterable
from typing import Any

import dspy
from dspy.adapters.chat_adapter import ChatAdapter
from dspy.adapters.types.tool import ToolCallResults, ToolCalls
from dspy.adapters.utils import get_field_description_string
from dspy.utils.exceptions import AdapterParseError

SUBMIT_TOOL = "submit"
_DELIVERED = "Delivered to the user."


class AppendOnlyChatAdapter(ChatAdapter):
    """A native-tool-calling ``ChatAdapter`` whose prompts only ever grow at the end."""

    def __init__(self) -> None:
        """Enable native function calling and keep one prompt shape for every call.

        The JSON fallback is off because it re-renders the whole prompt in a
        different shape, which both misses the cache and hides the failure from
        the resampling predictor that already handles it.
        """
        super().__init__(use_native_function_calling=True, use_json_adapter_fallback=False)

    def user_message_output_requirements(self, signature: type[dspy.Signature]) -> str | None:
        """Keep the format reminder out of every replayed user message.

        Args:
            signature: The signature being rendered.

        Returns:
            Always ``None``; :meth:`format` appends the reminder on its own.
        """
        return None

    def format_system_message(self, signature: type[dspy.Signature]) -> str:
        """Render the system prompt, dropping the text protocol when nothing uses it.

        Once tool calls and reasoning ride native channels no output field is
        left, and DSPy's field-structure boilerplate would only tell the model
        to end with a text marker instead of calling a tool.

        Args:
            signature: The signature after native fields were removed.

        Returns:
            The system message text.
        """
        if signature.output_fields:
            return super().format_system_message(signature)
        visible = {
            name: field for name, field in signature.input_fields.items() if field.annotation is not dspy.History
        }
        return (
            f"{textwrap.dedent(signature.instructions).strip()}\n\n"
            "Each user turn carries these fields, each under its own `[[ ## name ## ]]` header:\n"
            f"{get_field_description_string(visible)}"
        )

    def format_assistant_message_content(
        self, signature: type[dspy.Signature], outputs: dict[str, Any], missing_field_message: str | None = None
    ) -> str:
        """Render replayed assistant text, or nothing when no output field is textual.

        Args:
            signature: The signature after native fields were removed.
            outputs: The recorded output values.
            missing_field_message: Placeholder for absent fields.

        Returns:
            The assistant text. Empty when every output rides a native channel,
            so a user turn with no recorded reply adds no stray assistant message.
        """
        if not signature.output_fields:
            return ""
        return super().format_assistant_message_content(signature, outputs, missing_field_message)

    def format(
        self, signature: type[dspy.Signature], demos: list[dict[str, Any]], inputs: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Render the messages with the format reminder as a trailing message of its own.

        Args:
            signature: The signature after native fields were removed.
            demos: Few-shot examples.
            inputs: The call's input values.

        Returns:
            The chat messages. Everything before the optional trailing reminder
            is a prefix of what the next loop call renders.
        """
        messages = super().format(signature, demos, inputs)
        if signature.output_fields:
            messages.append({"role": "user", "content": super().user_message_output_requirements(signature)})
        return messages

    def parse(self, signature: type[dspy.Signature], completion: str) -> dict[str, Any]:
        """Parse a text-protocol reply, accepting tool calls sent without their field header.

        On the text protocol (a subscription CLI route has no native tools) the
        tool calls are often the only output field, and their value is itself
        ``{"tool_calls": [...]}``. Claude then sometimes writes that JSON
        object and the closing marker but drops the ``[[ ## tool_calls ## ]]``
        header, so every resample and the forced submit fail the same way and
        the turn ends with no answer.

        Args:
            signature: The signature after native fields were removed.
            completion: The raw LM text.

        Returns:
            The parsed output fields.

        Raises:
            AdapterParseError: When the reply does not parse even with the
                missing tool-call header restored.
        """
        try:
            return super().parse(signature, completion)
        except AdapterParseError as err:
            field = self._get_tool_call_output_field_name(signature)
            if field is None or f"[[ ## {field} ## ]]" in completion or not completion.lstrip().startswith("{"):
                raise
            try:
                return super().parse(signature, f"[[ ## {field} ## ]]\n{completion}")
            except AdapterParseError:
                raise err from None


class AgentProgram:
    """The tools, step signature and loop budget of one agent; Pi runs the loop.

    ``dspy.ReActV2`` builds the program definition (the ``submit`` tool and
    the step signature with its instructions). Nothing here runs a loop:
    :class:`~.pi_react.ReactOnPi` renders the prompt and runs the tools.
    """

    def __init__(
        self,
        signature: type[dspy.Signature] | str,
        tools: list[Any],
        max_iters: int = 20,
        *,
        submit_guard: Callable[[dict[str, Any]], str | None] | None = None,
    ) -> None:
        """Build the program definition.

        Args:
            signature: Task signature, as for ``dspy.ReActV2``.
            tools: Tool roster, as for ``dspy.ReActV2``.
            max_iters: Model calls per user turn before ``submit`` is forced.
            submit_guard: Reads a ``submit`` call's outputs and returns why the
                turn may not end yet, or ``None`` to let it end. A refusal is
                the submit's tool result, so the model reads it and goes on.
                The forced submit at the loop budget is never refused.
        """
        definition = dspy.ReActV2(signature, tools, max_iters=max_iters)
        self.signature = definition.signature
        self.tools = definition.tools
        self.react = definition.react
        self.max_iters = max_iters
        self._forcing = False
        if submit_guard is not None:
            self._guard_submit(submit_guard)

    def _guard_submit(self, guard: Callable[[dict[str, Any]], str | None]) -> None:
        """Run ``guard`` before every ``submit`` call.

        Only a dict return ends the turn, so a refusal returned as text reads
        to the model as an ordinary tool result.

        Args:
            guard: As for ``submit_guard`` in :meth:`__init__`.
        """
        tool = self.tools[SUBMIT_TOOL]
        submit = tool.func

        def guarded(**kwargs: Any) -> Any:
            """Refuse the submit when the guard objects, else submit.

            Args:
                **kwargs: The submit call's outputs.

            Returns:
                The guard's refusal, or the submitted outputs.
            """
            if not self._forcing and (refusal := guard(kwargs)):
                return refusal
            return submit(**kwargs)

        tool.func = guarded


def history_from_turns(
    turns: Iterable[tuple[str, str] | tuple[str, str, list[dict[str, Any]]]],
    *,
    input_field: str,
    output_field: str,
) -> dspy.History:
    """Rebuild native loop history from plain ``(role, text)`` conversation turns.

    An earlier assistant reply is replayed the way the loop itself records one:
    as a ``submit`` call. The inner signature has no reply field, so a plain
    assistant text would be dropped from the prompt. An assistant turn may carry
    a third element, the trace of the tool calls it made, each a
    ``{tool, status, result}`` dict (``args`` optional); those are replayed
    one call per loop step ahead of the ``submit``, as the loop records them.

    Args:
        turns: Earlier turns, oldest first, with roles ``"user"`` and ``"assistant"``.
        input_field: Signature input that carries the user's text.
        output_field: Signature output that carries the assistant's reply.

    Returns:
        History to pass as the ``history`` input of the next turn.
    """
    messages: list[dict[str, Any]] = []
    pending: dict[str, Any] = {}
    for index, turn in enumerate(turns):
        role, text = turn[0], turn[1]
        if role == "user":
            if pending:
                messages.append(pending)
            pending = {input_field: text}
            continue
        trace = turn[2] if len(turn) > 2 else []
        for step, call in enumerate(trace):
            messages.append({**pending, "tool_calls": _replayed_call(
                f"turn_{index}_{step}", call["tool"], call.get("args") or {},
                call.get("result") or "", call.get("status") == "error",
            )})  # fmt: skip
            pending = {}
        submit = _replayed_call(f"turn_{index}", SUBMIT_TOOL, {output_field: text}, _DELIVERED, False)
        messages.append({**pending, "tool_calls": submit})
        pending = {}
    if pending:
        messages.append(pending)
    return dspy.History(messages=messages)


def _replayed_call(call_id: str, name: str, args: dict[str, Any], result: str, is_error: bool) -> ToolCalls:
    """Build one settled tool call for a replayed history event.

    Args:
        call_id: Tool call id; results are matched to calls by it.
        name: Tool name.
        args: Arguments the call was made with.
        result: The result text the tool returned.
        is_error: Whether the call failed.

    Returns:
        The call with its result attached.
    """
    calls = ToolCalls(tool_calls=[ToolCalls.ToolCall(id=call_id, name=name, args=args)])
    results = ToolCallResults.from_tool_calls_and_values(calls, [result], [is_error])
    return calls.model_copy(update={"tool_call_results": results})


__all__ = ["SUBMIT_TOOL", "AgentProgram", "AppendOnlyChatAdapter", "history_from_turns"]
