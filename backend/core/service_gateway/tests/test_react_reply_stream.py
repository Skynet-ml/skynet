"""Tests for the ReActV2 reply streamer.

``ReactReplyStream`` decodes the reply out of the ``submit`` tool-call argument
on the inner ``react`` predictor's ``tool_calls`` field, whether it arrives as
provider tool-call deltas or through DSPy's text protocol. These tests exercise
both extractors on a stand-in program.
"""

from __future__ import annotations

from types import SimpleNamespace

import dspy
import pytest

from core.service_gateway.agents import code as code_module
from core.service_gateway.agents.code import (
    NativeToolArgsStreamListener,
    NativeToolCallStreamListener,
    ReactReplyStream,
    _NativeSubmitArgExtractor,
    _SubmitArgExtractor,
)


class _Sig(dspy.Signature):
    """Reply to the user."""

    user_message: str = dspy.InputField()
    reply: str = dspy.OutputField()


def _noop(x: str) -> str:
    """Echo the argument.

    Args:
        x: Arbitrary string.

    Returns:
        The argument unchanged.
    """
    return x


def _response(field: str, chunk: str, *, last: bool = False) -> dspy.streaming.StreamResponse:
    """Build a ``StreamResponse`` for a given field and chunk.

    Args:
        field: The ``signature_field_name`` the chunk belongs to.
        chunk: The streamed text fragment.
        last: Whether this is the field's terminal chunk.

    Returns:
        A populated ``dspy.streaming.StreamResponse``.
    """
    return dspy.streaming.StreamResponse(
        predict_name="react",
        signature_field_name=field,
        chunk=chunk,
        is_last_chunk=last,
    )


class _SubmitProgram:
    """A stand-in ReActV2 program: just the inner ``react`` predictor."""

    def __init__(self, react: dspy.Predict) -> None:
        """Store the inner predictor that drives the loop.

        Args:
            react: A real predictor so reasoning-listener binding succeeds.
        """
        self.react = react


def test_program_streams_through_two_listeners() -> None:
    """The streamer binds a reply listener and a reasoning listener to the loop predictor."""
    program = dspy.ReActV2(_Sig, tools=[_noop], max_iters=3)
    stream = ReactReplyStream(program, "reply")

    listeners = stream.listeners()
    assert len(listeners) == 2
    assert dspy.streamify(program, stream_listeners=listeners, async_streaming=True) is not None


def test_submit_program_decodes_partial_tool_call_json() -> None:
    """Partial ``submit`` JSON yields the growing reply argument."""
    base = dspy.ReActV2(_Sig, tools=[_noop], max_iters=3)
    stream = ReactReplyStream(_SubmitProgram(base.react), "reply")

    assert stream.reply_delta(_response("reply", "anything")) is None  # not the tool_calls field
    first = stream.reply_delta(_response("tool_calls", '{"tool_calls":[{"name":"submit","args":{"reply":"Hi'))
    second = stream.reply_delta(_response("tool_calls", ' there"}}]}', last=True))
    assert (first or "") + (second or "") == "Hi there"


def test_serial_submit_stream_suppresses_reply_that_races_a_tool() -> None:
    """A mixed tool turn never leaks its premature submit text to the user."""
    base = dspy.ReActV2(_Sig, tools=[_noop], max_iters=3)
    base.react._serial_tool_calls = True
    stream = ReactReplyStream(_SubmitProgram(base.react), "reply")

    first = stream.reply_delta(
        _response(
            "tool_calls",
            '{"tool_calls":[{"name":"count","args":{}},{"name":"submit","args":{"reply":"Checking',
        )
    )
    second = stream.reply_delta(_response("tool_calls", ' now."}}]}', last=True))
    final = stream.reply_delta(
        _response(
            "tool_calls",
            '{"tool_calls":[{"name":"submit","args":{"reply":"You have 3."}}]}',
            last=True,
        )
    )

    assert first is None
    assert second is None
    assert final == "You have 3."


def _serial_stream() -> ReactReplyStream:
    """Build a reply streamer on a serial-tool-call loop, as the generalist runs.

    Returns:
        A ``ReactReplyStream`` whose submit extractor is exclusive.
    """
    base = dspy.ReActV2(_Sig, tools=[_noop], max_iters=3)
    base.react._serial_tool_calls = True
    return ReactReplyStream(_SubmitProgram(base.react), "reply")


def test_serial_submit_reply_streams_before_the_field_ends() -> None:
    """A submit-only step streams its reply as it grows, then closes on an empty last chunk."""
    stream = _serial_stream()

    first = stream.reply_delta(_response("tool_calls", '{"tool_calls":[{"name":"submit","args":{"reply":"You have'))
    second = stream.reply_delta(_response("tool_calls", ' 3."}}]}'))
    closing = stream.reply_delta(_response("tool_calls", "", last=True))

    assert first == "You have"
    assert second == " 3."
    assert closing is None
    assert stream.take_retraction() is False


def test_serial_submit_followed_by_a_tool_retracts_the_streamed_reply() -> None:
    """A submit streamed before a sibling call is taken back once, and the step's real reply restarts."""
    stream = _serial_stream()

    first = stream.reply_delta(_response("tool_calls", '{"tool_calls":[{"name":"submit","args":{"reply":"Checking"}}'))
    assert first == "Checking"
    assert stream.take_retraction() is False

    stream.reply_delta(_response("tool_calls", ',{"name":"count","args":{}}]}', last=True))
    assert stream.take_retraction() is True
    assert stream.take_retraction() is False

    final = stream.reply_delta(
        _response("tool_calls", '{"tool_calls":[{"name":"submit","args":{"reply":"You have 3."}}]}', last=True)
    )
    assert final == "You have 3."


def _lm_chunk(tool_calls: list | None = None, finish: str | None = None) -> SimpleNamespace:
    """Build a stand-in LiteLLM streaming chunk.

    Args:
        tool_calls: The ``delta.tool_calls`` list, or ``None`` for an empty delta.
        finish: The choice's ``finish_reason``, or ``None`` mid-stream.

    Returns:
        An object shaped like a LiteLLM ``ModelResponseStream`` chunk.
    """
    delta = SimpleNamespace(tool_calls=tool_calls, content=None)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish)])


def _tool_call(index: int, name: str | None = None, arguments: str | None = None) -> SimpleNamespace:
    """Build a stand-in streamed tool-call delta.

    Args:
        index: The tool call's stream index.
        name: The function name (present only in the call's first delta).
        arguments: The streaming ``arguments`` JSON fragment.

    Returns:
        An object shaped like one element of ``delta.tool_calls``.
    """
    return SimpleNamespace(index=index, function=SimpleNamespace(name=name, arguments=arguments))


def _drain_native(listener: NativeToolCallStreamListener, stream: ReactReplyStream, chunks: list) -> str:
    """Run raw chunks through the native listener + reply bridge, as the serve loop does.

    Args:
        listener: The native tool-call listener from ``stream.listeners()``.
        stream: The reply stream whose ``_NativeSubmitArgExtractor`` decodes the deltas.
        chunks: The sequence of stand-in LiteLLM chunks to replay.

    Returns:
        The reconstructed reply text.
    """
    out = ""
    for chunk in chunks:
        response = listener.receive(chunk)
        if response is None:
            continue
        delta = stream.reply_delta(response)
        if delta:
            out += delta
    return out


def test_submit_program_defaults_to_text_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    """With native calling inactive, ReActV2 keeps the text tool-call extractor."""
    monkeypatch.setattr(code_module, "native_tool_calling_active", lambda: False)
    base = dspy.ReActV2(_Sig, tools=[_noop], max_iters=3)
    stream = ReactReplyStream(_SubmitProgram(base.react), "reply")

    assert stream._native is False
    assert isinstance(stream._extractor, _SubmitArgExtractor)
    assert not isinstance(stream.listeners()[0], NativeToolCallStreamListener)


def test_native_adapter_falls_back_to_text_when_lm_lacks_tool_calling(monkeypatch: pytest.MonkeyPatch) -> None:
    """The adapter sends no native tools to an LM without tool-calling, so the text extractor must stream."""
    monkeypatch.setattr(code_module, "native_tool_calling_active", lambda: True)
    base = dspy.ReActV2(_Sig, tools=[_noop], max_iters=3)
    stream = ReactReplyStream(_SubmitProgram(base.react), "reply", SimpleNamespace(supports_function_calling=False))

    assert stream._native is False
    assert isinstance(stream._extractor, _SubmitArgExtractor)


def test_native_submit_program_decodes_provider_tool_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Native calling: the submit call's provider ``arguments`` reconstruct the reply.

    A parallel non-submit tool call streams on index 0 and must be ignored; the
    submit call on index 1 carries the user-visible reply.
    """
    monkeypatch.setattr(code_module, "native_tool_calling_active", lambda: True)
    base = dspy.ReActV2(_Sig, tools=[_noop], max_iters=3)
    stream = ReactReplyStream(_SubmitProgram(base.react), "reply")

    assert stream._native is True
    assert isinstance(stream._extractor, _NativeSubmitArgExtractor)
    listener = stream.listeners()[0]
    assert isinstance(listener, NativeToolCallStreamListener)

    chunks = [
        _lm_chunk([_tool_call(0, name="edit_signature", arguments="")]),
        _lm_chunk([_tool_call(0, arguments='{"code":"x"}')]),
        _lm_chunk([_tool_call(1, name="submit", arguments="")]),
        _lm_chunk([_tool_call(1, arguments='{"reply":"Hi')]),
        _lm_chunk([_tool_call(1, arguments=' there"}')]),
        _lm_chunk(finish="tool_calls"),
    ]
    assert _drain_native(listener, stream, chunks) == "Hi there"


def test_native_listener_buffers_args_arriving_before_submit_name() -> None:
    """Args streamed before the ``submit`` name is seen are buffered, then flushed."""
    listener = NativeToolCallStreamListener(predict=None, allow_reuse=True)
    extractor = _NativeSubmitArgExtractor("reply")

    chunks = [
        _lm_chunk([_tool_call(0, arguments='{"reply":"par')]),
        _lm_chunk([_tool_call(0, name="submit", arguments="tial")]),
        _lm_chunk([_tool_call(0, arguments=' done"}')]),
        _lm_chunk(finish="stop"),
    ]
    out = ""
    for chunk in chunks:
        response = listener.receive(chunk)
        if response is None:
            continue
        delta = extractor.feed(response.chunk)
        if response.is_last_chunk:
            extractor.reset()
        if delta:
            out += delta
    assert out == "partial done"


def test_native_listener_resets_between_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reused listener + extractor decode a second turn cleanly after finish_reason."""
    monkeypatch.setattr(code_module, "native_tool_calling_active", lambda: True)
    base = dspy.ReActV2(_Sig, tools=[_noop], max_iters=3)
    stream = ReactReplyStream(_SubmitProgram(base.react), "reply")
    listener = stream.listeners()[0]

    first = _drain_native(
        listener,
        stream,
        [
            _lm_chunk([_tool_call(0, name="submit", arguments='{"reply":"one"}')]),
            _lm_chunk(finish="stop"),
        ],
    )
    second = _drain_native(
        listener,
        stream,
        [
            _lm_chunk([_tool_call(0, name="submit", arguments='{"reply":"two"}')]),
            _lm_chunk(finish="stop"),
        ],
    )
    assert first == "one"
    assert second == "two"


_DRAFTS = {"edit_run": "new_code", "edit_metric": "new_code"}


def _rebuild(deltas: list[tuple[str, int, str]]) -> dict[str, str]:
    """Fold ``(tool, offset, delta)`` triples into each tool's draft, as the editor does.

    Args:
        deltas: The triples ``tool_draft_deltas`` returned, in order.

    Returns:
        Tool name mapped to the draft text rebuilt from its deltas.
    """
    drafts: dict[str, str] = {}
    for tool, offset, delta in deltas:
        drafts[tool] = drafts.get(tool, "")[:offset] + delta
    return drafts


def test_text_protocol_streams_the_edit_code_while_it_is_written(monkeypatch: pytest.MonkeyPatch) -> None:
    """An edit_run call's new_code comes out piece by piece, before the call is complete."""
    monkeypatch.setattr(code_module, "native_tool_calling_active", lambda: False)
    base = dspy.ReActV2(_Sig, tools=[_noop], max_iters=3)
    stream = ReactReplyStream(_SubmitProgram(base.react), "reply", tool_drafts=_DRAFTS)

    pieces = [
        '{"tool_calls":[{"name":"edit_run","args":{"reason":"fix","new_code":"def run(',
        "artifact, input):\\n",
        '    return 1"}}]}',
    ]
    deltas = []
    for i, piece in enumerate(pieces):
        got = stream.tool_draft_deltas(_response("tool_calls", piece, last=i == len(pieces) - 1))
        deltas.append(got)
    assert deltas[0] == [("edit_run", 0, "def run(")]
    assert deltas[1] == [("edit_run", 8, "artifact, input):\n")]
    assert _rebuild([d for batch in deltas for d in batch]) == {"edit_run": "def run(artifact, input):\n    return 1"}
    assert stream.tool_draft_deltas(_response("reasoning", "x")) == []


def test_text_protocol_draft_restarts_on_the_next_loop_iteration(monkeypatch: pytest.MonkeyPatch) -> None:
    """After a field ends, a retried edit starts a fresh draft at offset 0; submit never streams as code."""
    monkeypatch.setattr(code_module, "native_tool_calling_active", lambda: False)
    base = dspy.ReActV2(_Sig, tools=[_noop], max_iters=3)
    stream = ReactReplyStream(_SubmitProgram(base.react), "reply", tool_drafts=_DRAFTS)

    first = stream.tool_draft_deltas(
        _response("tool_calls", '{"tool_calls":[{"name":"edit_metric","args":{"new_code":"a"}}]}', last=True)
    )
    second = stream.tool_draft_deltas(
        _response("tool_calls", '{"tool_calls":[{"name":"edit_metric","args":{"new_code":"b"}}]}', last=True)
    )
    reply = stream.tool_draft_deltas(
        _response("tool_calls", '{"tool_calls":[{"name":"submit","args":{"reply":"done"}}]}', last=True)
    )
    assert first == [("edit_metric", 0, "a")]
    assert second == [("edit_metric", 0, "b")]
    assert reply == []


def test_without_drafts_nothing_streams_as_code() -> None:
    """A stream built without ``tool_drafts`` leaves tool arguments alone."""
    base = dspy.ReActV2(_Sig, tools=[_noop], max_iters=3)
    stream = ReactReplyStream(_SubmitProgram(base.react), "reply")
    chunk = _response("tool_calls", '{"tool_calls":[{"name":"edit_run","args":{"new_code":"x"}}]}', last=True)
    assert stream.tool_draft_deltas(chunk) == []


def test_native_calling_streams_the_edit_code_from_provider_deltas(monkeypatch: pytest.MonkeyPatch) -> None:
    """Native calling: a third listener forwards every call's deltas and the edit's code streams out."""
    monkeypatch.setattr(code_module, "native_tool_calling_active", lambda: True)
    base = dspy.ReActV2(_Sig, tools=[_noop], max_iters=3)
    stream = ReactReplyStream(_SubmitProgram(base.react), "reply", tool_drafts=_DRAFTS)
    listeners = stream.listeners()
    assert len(listeners) == 3
    args_listener = listeners[2]
    assert isinstance(args_listener, NativeToolArgsStreamListener)

    chunks = [
        _lm_chunk([_tool_call(0, name="edit_run", arguments="")]),
        _lm_chunk([_tool_call(0, arguments='{"reason":"fix","new_code":"def ')]),
        _lm_chunk([_tool_call(0, arguments='run(): pass"}')]),
        _lm_chunk([_tool_call(1, name="submit", arguments='{"reply":"ok"}')]),
        _lm_chunk(finish="tool_calls"),
    ]
    deltas = []
    for chunk in chunks:
        response = args_listener.receive(chunk)
        if response is not None:
            deltas.append(stream.tool_draft_deltas(response))
    flat = [d for batch in deltas for d in batch]
    assert [len(batch) for batch in deltas[1:3]] == [1, 1]
    assert _rebuild(flat) == {"edit_run": "def run(): pass"}

    again = args_listener.receive(_lm_chunk([_tool_call(0, name="edit_run", arguments='{"new_code":"x"}')]))
    assert again is not None
    assert stream.tool_draft_deltas(again) == [("edit_run", 0, "x")]
