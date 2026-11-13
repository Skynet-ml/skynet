"""Tests for the lead agent briefs, the parallel attacker sessions, the edge-case tree and the noise check (SKY-682)."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

import pytest

from core.models.hardening import HardeningSession
from core.worker import hardening_sessions as hs


def _call(name: str, **args: Any) -> dict[str, Any]:
    """Build one parsed model reply with one tool call.

    Args:
        name: The tool.
        **args: The call's arguments.

    Returns:
        The reply as ``parse_message`` gives it.
    """
    return {"text": "", "tool_calls": [{"id": f"id-{name}", "function": {"name": name, "arguments": json.dumps(args)}}]}


_PATCH = _call(hs.WRITE_PATCH_TOOL, kind="case", patch_diff="--- a/x\n+++ b/x\n+y\n", why_md="Games it.")


async def _fake_turn(*, binary: str, handlers: Any, translator: Any, prompt: str) -> None:
    """Drive the handlers as Pi does: one model step, then its tool call, until a stop.

    Args:
        binary: Ignored.
        handlers: The session.
        translator: Ignored.
        prompt: Ignored.
    """
    while True:
        chunks = [chunk async for chunk in handlers.complete({})]
        delta = json.loads(chunks[-1])["choices"][0].get("delta") or {}
        calls = delta.get("tool_calls") or []
        if not calls:
            return
        for call in calls:
            outcome = await handlers.call_tool(call["function"]["name"], json.loads(call["function"]["arguments"]))
            if outcome.terminate:
                return


def _session(replies: list[dict[str, Any]], score: Any = None, *, stop: bool = False) -> hs.AttackSession:
    """Build one session that answers with ``replies`` in order, then with plain text.

    Args:
        replies: The parsed model replies.
        score: The session's scorer.
        stop: What ``should_stop`` returns.

    Returns:
        The session.
    """
    queue = list(replies)
    return hs.AttackSession(
        HardeningSession(index=1, brief="b"),
        prompt="p",
        ask=lambda messages, tools, limit: queue.pop(0) if queue else {"text": "done", "tool_calls": []},
        score=score or (lambda patch: {"score": 0.9, "feedback": "ok"}),
        model="openai/attacker",
        should_stop=lambda: stop,
        publish=lambda: None,
        lock=threading.Lock(),
    )


def _run(session: hs.AttackSession) -> hs.AttackSession:
    """Run one session to its end with the fake Pi turn.

    Args:
        session: The session.

    Returns:
        The same session.
    """
    asyncio.run(_fake_turn(binary="pi", handlers=session, translator=None, prompt=""))
    return session


_FULL_TREE = [
    {"type": "ScoreCompare", "exploit_score": 1.0, "base_score": 0.5},
    {"type": "Diff", "diff": "+x"},
    {"type": "IOPair", "input": "q", "expected": "a", "got": "a a a"},
    {"type": "Table", "columns": ["case", "score"], "rows": [["one", 1.0], ["two", None]]},
    {"type": "Callout", "tone": "danger", "text": "Games the count."},
    {"type": "Chart", "kind": "bar", "points": [{"x": "a", "y": 1.0}]},
    {"type": "Text", "text": "Why."},
    {"type": "Steps", "items": ["Run it.", {"type": "Text", "text": "Read it."}, {"type": "Steps", "items": ["x"]}]},
]


def test_a_tree_of_every_catalog_node_is_valid() -> None:
    """Every catalog component validates, and the defaults are filled in."""
    tree = hs.validate_edge_case(_FULL_TREE)

    assert tree is not None
    assert [node["type"] for node in tree] == [node["type"] for node in _FULL_TREE]
    # No label keys: the page shows its own translated labels.
    assert tree[0] == {"type": "ScoreCompare", "exploit_score": 1.0, "base_score": 0.5}
    assert "title" not in tree[5]
    assert hs.validate_edge_case(json.dumps(_FULL_TREE)) == tree


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "not json",
        [],
        {"type": "Text", "text": "x"},
        [{"type": "Video", "url": "x"}],
        [{"type": "Text", "text": "x", "extra": 1}],
        [{"type": "Callout", "tone": "loud", "text": "x"}],
        [{"type": "Chart", "kind": "pie", "points": [{"x": 1, "y": 1}]}],
        [{"type": "Chart", "kind": "line", "points": [{"x": i, "y": i} for i in range(201)]}],
        [{"type": "Table", "columns": ["a"], "rows": [["x"]] * 51}],
        [{"type": "Table", "columns": ["a", "b"], "rows": [["only one"]]}],
        [{"type": "Steps", "items": [{"type": "Steps", "items": [{"type": "Text", "text": "too deep"}]}]}],
        [{"type": "Text", "text": "x"}] * 31,
        [{"type": "Steps", "items": [{"type": "Text", "text": "x"}] * 15}] * 2,
        [{"type": "Steps", "items": [{"type": "Table", "columns": ["a", "b"], "rows": [["only one"]]}]}],
        [{"type": "Table", "columns": [str(i) for i in range(21)], "rows": []}],
        [{"type": "Table", "columns": ["x" * 121], "rows": []}],
        [{"type": "Table", "columns": ["a"], "rows": [["x" * 4_001]]}],
        [{"type": "Steps", "items": ["x"] * 51}],
        [{"type": "Steps", "items": [{"type": "Steps", "items": ["x" * 4_001]}]}],
        [{"type": "Chart", "kind": "bar", "points": [{"x": "x" * 121, "y": 1}]}],
        [{"type": "ScoreCompare", "exploit_score": float("nan"), "base_score": 0.1}],
        [{"type": "Chart", "kind": "line", "points": [{"x": 1, "y": float("inf")}]}],
        [{"type": "Table", "columns": ["a"], "rows": [[float("-inf")]]}],
    ],
    ids=[
        "none",
        "bad-json",
        "empty",
        "not-a-list",
        "unknown-type",
        "extra-field",
        "bad-tone",
        "bad-chart-kind",
        "too-many-points",
        "too-many-rows",
        "row-width",
        "depth-3",
        "31-nodes",
        "32-nodes-nested",
        "row-width-nested",
        "21-columns",
        "long-column",
        "long-cell",
        "51-steps",
        "long-inner-step",
        "long-chart-x",
        "nan-score",
        "inf-chart-y",
        "inf-cell",
    ],
)
def test_an_invalid_tree_is_dropped(raw: Any) -> None:
    """A tree outside the catalog schema or its limits gives None, so the card falls back to the patch."""
    assert hs.validate_edge_case(raw) is None


def test_score_compare_labels_are_kept_when_given() -> None:
    """A session's own labels round-trip unchanged."""
    node = {
        "type": "ScoreCompare",
        "exploit_score": 1.0,
        "base_score": 0.5,
        "exploit_label": "Hack",
        "base_label": "Real",
    }

    assert hs.validate_edge_case([node]) == [node]


def test_a_short_or_long_table_row_is_dropped_at_both_depths() -> None:
    """Every row has exactly one cell for each column, as the frontend parser needs (SKY-683)."""
    for row in (["a"], ["a", "b", "c"]):
        table = {"type": "Table", "columns": ["x", "y"], "rows": [["a", "b"], row]}
        assert hs.validate_edge_case([table]) is None
        assert hs.validate_edge_case([{"type": "Steps", "items": [table]}]) is None
    good = {"type": "Table", "columns": ["x", "y"], "rows": [["a", 1.0]]}
    assert hs.validate_edge_case([{"type": "Steps", "items": [good]}]) is not None


def test_fifty_steps_are_valid() -> None:
    """A Steps node may hold 50 items, the frontend parser's limit (SKY-683)."""
    assert hs.validate_edge_case([{"type": "Steps", "items": ["x"] * 50}]) is not None


def test_parse_message_reads_text_and_tool_calls() -> None:
    """The chat reply gives its text and each named tool call, with an id."""
    body = json.dumps(
        {
            "choices": [
                {
                    "message": {
                        "content": "hi",
                        "tool_calls": [{"function": {"name": "run_metric", "arguments": "{}"}}, {"function": {}}],
                    }
                }
            ]
        }
    )

    first, second = hs.parse_message(body), hs.parse_message(body)

    assert first["text"] == "hi"
    assert [c["function"] for c in first["tool_calls"]] == [{"name": "run_metric", "arguments": "{}"}]
    # A missing id gets a new one each step, so two steps never share an id.
    assert first["tool_calls"][0]["id"].startswith("call_")
    assert first["tool_calls"][0]["id"] != second["tool_calls"][0]["id"]


def test_read_briefs_keeps_k_briefs_and_fills_the_rest() -> None:
    """The lead agent's briefs come first; fallback briefs fill any that are missing."""
    reply = 'Here: {"briefs": ["  one  ", "", 3, "two", "three", "four"]}'

    assert hs.read_briefs(reply, 3) == ["one", "two", "three"]
    assert hs.read_briefs("no json", 2) == list(hs.FALLBACK_BRIEFS[:2])
    assert hs.read_briefs('{"briefs": ["one"]}', 2) == ["one", hs.FALLBACK_BRIEFS[1]]
    assert hs.read_briefs('{"briefs": "abc"}', 2) == list(hs.FALLBACK_BRIEFS[:2])


def test_read_noise_decision() -> None:
    """A clear answer keeps its reason; an unclear answer runs the repeats."""
    assert hs.read_noise_decision('{"deterministic": true, "reason": "Pure math."}') == (True, "Pure math.")
    assert hs.read_noise_decision('{"deterministic": false}') == (False, "No reason given.")
    assert hs.read_noise_decision("maybe")[0] is False
    assert hs.read_noise_decision('{"deterministic": "yes"}')[0] is False


def test_spread() -> None:
    """The spread is the highest score minus the lowest, and needs two scores."""
    assert hs.spread([0.5, 0.9, 0.7]) == pytest.approx(0.4)
    assert hs.spread([0.5]) is None


def test_prompts_hide_the_metric_for_black_box_and_show_it_for_white_box() -> None:
    """Only a white-box job puts the metric code into the prompts (SKY-353)."""
    black = hs.session_prompt(brief="b", round_index=1, tree="a.py", known=[], metric="")
    white = hs.session_prompt(brief="b", round_index=1, tree="a.py", known=["e1"], metric="def score(): ...")

    assert "The metric code" not in black
    assert "def score(): ..." in white
    assert "e1" in white
    assert "The metric code" not in hs.brief_prompt(count=3, round_index=1, intent="", files=[], known=[], metric="")


def test_a_session_runs_the_metric_then_writes_its_patch() -> None:
    """The session sees only the score and the feedback, and write_patch ends it with a claim."""
    scored: list[str] = []

    def score(patch: str) -> dict[str, Any]:
        """Score one patch.

        Args:
            patch: The patch.

        Returns:
            A score with hidden side info.
        """
        scored.append(patch)
        return {"score": 0.9, "feedback": "f" * 5000, "side_info": {"hidden": "metric/secret.py"}}

    edge = [{"type": "Text", "text": "x"}]
    replies = [
        _call(hs.RUN_METRIC_TOOL, patch_diff="+a"),
        _call(hs.READ_SCORE_TOOL),
        _call(hs.WRITE_PATCH_TOOL, kind="method", patch_diff="+a", why_md="Pads it.", edge_case=edge),
    ]
    session = _run(_session(replies, score))

    assert scored == ["+a"]
    tool_results = [m["content"] for m in session._messages if m["role"] == "tool"]
    seen = json.loads(tool_results[0])
    assert set(seen) == {"score", "feedback"}
    assert len(seen["feedback"]) == hs.MAX_FEEDBACK_CHARS
    assert "secret" not in "".join(tool_results)
    assert json.loads(tool_results[1]) == seen
    assert session.candidate == hs.Candidate(
        kind="method", patch_diff="+a", why_md="Pads it.", edge_case=edge, session=1
    )
    calls = [entry["toolCalls"][0] for entry in session.record.transcript]
    assert [(c["tool"], c["status"]) for c in calls] == [
        ("run_metric", "done"),
        ("read_score", "done"),
        ("write_patch", "done"),
    ]
    assert calls[0]["payload"] == {"score": 0.9}
    assert all(c["endedAt"] is not None for c in calls)


def test_a_transcript_entry_keeps_a_cut_text_and_the_model_keeps_it_all() -> None:
    """The stored step text is cut, so the polled row stays small; the next model step still reads all of it."""
    long_text = "t" * (hs.MAX_TRANSCRIPT_CHARS + 100)
    session = _run(_session([{**_PATCH, "text": long_text}]))

    assert session.record.transcript[0]["content"] == long_text[: hs.MAX_TRANSCRIPT_CHARS]
    assert any(m["role"] == "assistant" and m["content"] == long_text for m in session._messages)


@pytest.mark.parametrize("why", ["", "   ", None])
def test_write_patch_needs_a_why(why: Any) -> None:
    """A claim without a reason is refused, so the session can try again."""
    session = _run(_session([_call(hs.WRITE_PATCH_TOOL, kind="case", patch_diff="+x", why_md=why)]))

    assert session.candidate is None
    assert session._messages[-2]["content"].startswith("write_patch needs")


def test_an_invalid_edge_case_keeps_the_claim_without_a_tree() -> None:
    """A bad tree is dropped, and the claim stays for the patch fallback."""
    reply = _call(hs.WRITE_PATCH_TOOL, kind="case", patch_diff="+a", why_md="w", edge_case=[{"type": "Nope"}])
    session = _run(_session([reply]))

    assert session.candidate is not None
    assert session.candidate.edge_case is None


def test_a_metric_error_names_only_its_type_and_the_session_goes_on() -> None:
    """A patch that breaks the scorer is the attacker's mistake; the error text may name hidden paths."""

    def score(patch: str) -> dict[str, Any]:
        """Fail like a bad patch.

        Args:
            patch: The patch.

        Raises:
            ValueError: Always.
        """
        raise ValueError("metric/secret.py does not apply")

    session = _run(_session([_call(hs.RUN_METRIC_TOOL, patch_diff="+a"), _PATCH], score))

    results = [m["content"] for m in session._messages if m["role"] == "tool"]
    assert results[0] == "The metric run failed: ValueError."
    assert session.record.transcript[0]["toolCalls"][0]["status"] == "error"
    assert session.candidate is not None


def test_the_session_stops_at_the_metric_run_cap() -> None:
    """Each metric run is a paid sandbox run, so a session gets at most MAX_METRIC_RUNS."""
    scored: list[str] = []
    replies = [_call(hs.RUN_METRIC_TOOL, patch_diff=f"+{i}") for i in range(hs.MAX_METRIC_RUNS + 1)]
    session = _run(_session(replies, lambda patch: scored.append(patch) or {"score": 0.1}))

    assert len(scored) == hs.MAX_METRIC_RUNS
    last = [m["content"] for m in session._messages if m["role"] == "tool"][-1]
    assert last.startswith("No metric runs are left")


def test_the_session_stops_at_the_step_cap() -> None:
    """Each step is a paid model call, so a session takes at most MAX_SESSION_STEPS."""
    replies = [_call(hs.READ_SCORE_TOOL) for _ in range(hs.MAX_SESSION_STEPS + 5)]
    session = _run(_session(replies))

    assert len(session.record.transcript) == hs.MAX_SESSION_STEPS
    assert session._messages[-3]["content"].startswith("This is your last step")


def test_a_stopped_round_asks_the_model_nothing() -> None:
    """When the round has no money left, the session ends before its next model call."""
    session = _run(_session([_PATCH], stop=True))

    assert session.record.transcript == []
    assert session.candidate is None


def test_a_round_that_stops_during_a_step_does_not_run_the_metric() -> None:
    """The stop check also runs before a paid metric run, not only before a model call."""
    checks = iter([False, True])
    scored: list[str] = []
    session = hs.AttackSession(
        HardeningSession(index=1, brief="b"),
        prompt="p",
        ask=lambda messages, tools, limit: _call(hs.RUN_METRIC_TOOL, patch_diff="+x"),
        score=lambda patch: scored.append(patch) or {"score": 1.0},
        model="m",
        should_stop=lambda: next(checks, True),
        publish=lambda: None,
        lock=threading.Lock(),
    )

    _run(session)

    assert scored == []
    assert session._messages[-1]["content"].startswith("The round has no budget left")


def test_live_writes_commit_in_snapshot_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """A slow live write never lets an older snapshot land after a newer one."""
    monkeypatch.setattr(hs, "LIVE_WRITE_SECONDS", 0.0)
    written: list[int] = []

    def slow(sessions: list[HardeningSession]) -> None:
        """Record how far the sessions got, slowly.

        Args:
            sessions: The live sessions.
        """
        progress = sum(len(s.transcript) + (s.status != "running") for s in sessions)
        time.sleep(0.005)
        written.append(progress)

    replies = [_call(hs.READ_SCORE_TOOL), _call(hs.READ_SCORE_TOOL), _PATCH]
    queues: dict[str, list[dict[str, Any]]] = {}

    def ask(messages: list[dict[str, Any]], tools: Any, limit: int) -> dict[str, Any]:
        """Give each session the same three steps.

        Args:
            messages: The session's messages.
            tools: The tools.
            limit: The answer limit.

        Returns:
            The session's next reply.
        """
        return queues.setdefault(messages[0]["content"], list(replies)).pop(0)

    hs.run_sessions(
        ["one", "two", "three"],
        prompt_for=lambda brief: brief,
        ask=ask,
        scorer_for=lambda index: lambda patch: {"score": 1.0},
        model="m",
        pi_binary="pi",
        should_stop=lambda: False,
        on_live=slow,
        run_turn=_fake_turn,
    )

    assert written == sorted(written)
    assert written[-1] == 3 * 3 + 3


def test_a_skipped_live_write_does_not_wait_for_another_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """While one session's final write is slow, another session's skipped writes return at once."""
    monkeypatch.setattr(hs, "LIVE_WRITE_SECONDS", 1000.0)
    blocking = threading.Event()
    release = threading.Event()
    released: list[bool] = []

    def on_live(sessions: list[HardeningSession]) -> None:
        """Hold the first write that shows an ended session until the other session moves on.

        Args:
            sessions: The live sessions.
        """
        if not released and any(s.status != "running" for s in sessions):
            blocking.set()
            released.append(release.wait(timeout=3))

    slow_steps = [_call(hs.READ_SCORE_TOOL), _call(hs.READ_SCORE_TOOL), _PATCH]

    def ask(messages: list[dict[str, Any]], tools: Any, limit: int) -> dict[str, Any]:
        """End the fast session at once; step the slow one while the fast one writes.

        Args:
            messages: The session's messages.
            tools: The tools.
            limit: The answer limit.

        Returns:
            The session's next reply.
        """
        if messages[0]["content"] == "fast":
            return _PATCH
        if len(slow_steps) == 3:
            blocking.wait(timeout=3)
        elif len(slow_steps) == 2:
            release.set()
        return slow_steps.pop(0)

    hs.run_sessions(
        ["fast", "slow"],
        prompt_for=lambda brief: brief,
        ask=ask,
        scorer_for=lambda index: lambda patch: {"score": 1.0},
        model="m",
        pi_binary="pi",
        should_stop=lambda: False,
        on_live=on_live,
        run_turn=_fake_turn,
    )

    assert released == [True]


def test_run_sessions_runs_each_brief_in_parallel_and_one_failure_fails_only_its_session() -> None:
    """Every session runs at the same time; a raising session is marked failed with its type only."""
    barrier = threading.Barrier(3, timeout=5)
    lanes: list[int] = []
    live: list[list[HardeningSession]] = []

    def ask(messages: list[dict[str, Any]], tools: Any, limit: int) -> dict[str, Any]:
        """Answer each session once, after all three started.

        Args:
            messages: The session's messages.
            tools: The tools.
            limit: The answer limit.

        Returns:
            A write_patch call.

        Raises:
            RuntimeError: For the second session.
        """
        barrier.wait()
        if "direction two" in messages[0]["content"]:
            raise RuntimeError("provider said: key sk-123 is bad")
        return _PATCH

    def scorer_for(index: int) -> Any:
        """Record the lane each session asks for.

        Args:
            index: The 1-based session.

        Returns:
            A scorer.
        """
        lanes.append(index)
        return lambda patch: {"score": 1.0}

    outcome = hs.run_sessions(
        ["direction one", "direction two", "direction three"],
        prompt_for=lambda brief: f"Your direction: {brief}",
        ask=ask,
        scorer_for=scorer_for,
        model="openai/attacker",
        pi_binary="pi",
        should_stop=lambda: False,
        on_live=live.append,
        run_turn=_fake_turn,
    )

    assert [s.status for s in outcome.sessions] == ["done", "failed", "done"]
    assert outcome.sessions[1].error == "RuntimeError"
    assert [c.session for c in outcome.candidates] == [1, 3]
    assert sorted(lanes) == [1, 2, 3]
    assert live[0][0].status == "running"
    assert [s.status for s in live[-1]] == ["done", "failed", "done"]
    assert "sk-123" not in json.dumps([s.model_dump() for s in live[-1]])


def test_a_failed_live_write_does_not_stop_the_round() -> None:
    """The live write only informs the page."""

    def broken(sessions: list[HardeningSession]) -> None:
        """Fail every live write.

        Args:
            sessions: The live sessions.

        Raises:
            OSError: Always.
        """
        raise OSError("db down")

    outcome = hs.run_sessions(
        ["one"],
        prompt_for=lambda brief: brief,
        ask=lambda messages, tools, limit: _PATCH,
        scorer_for=lambda index: lambda patch: {"score": 1.0},
        model="m",
        pi_binary="pi",
        should_stop=lambda: False,
        on_live=broken,
        run_turn=_fake_turn,
    )

    assert [s.status for s in outcome.sessions] == ["done"]
    assert len(outcome.candidates) == 1
