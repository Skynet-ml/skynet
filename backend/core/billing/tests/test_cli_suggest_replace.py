"""Tests that a newer next-message suggestion stops the older CLI run and gets its own answer."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from core.billing import cli_dispatch
from core.billing.cli_dispatch import NEXT_PROMPT_ROLE, CliDispatcher
from core.billing.cli_runners import CliCompletion, CliRunError
from core.billing.tests.test_cli_runners import local_settings

MODEL = "anthropic/claude-haiku-5.5"


class _FakeClaude:
    """A CLI runner whose first call streams until it is stopped and whose later calls answer."""

    def __init__(self) -> None:
        """Set up the sync points."""
        self.first_started = threading.Event()
        self.first_error: list[BaseException] = []
        self.calls = 0
        self.lock = threading.Lock()

    def __call__(self, model_arg: str, prompt: str, schema: Any, settings: Any, cwd: Path, **kwargs: Any) -> Any:
        """Run one fake CLI call.

        Args:
            model_arg: Unused.
            prompt: The prompt, echoed back.
            schema: Unused.
            settings: Unused.
            cwd: Unused.
            **kwargs: ``on_delta`` and ``think``.

        Returns:
            The completion.

        Raises:
            CliRunError: When the first call's stream callback stops it, or it never stops.
        """
        with self.lock:
            self.calls += 1
            first = self.calls == 1
        on_delta = kwargs["on_delta"]
        if first:
            self.first_started.set()
            try:
                for _ in range(500):
                    on_delta("content", ".")
                    threading.Event().wait(0.01)
            except CliRunError as error:
                self.first_error.append(error)
                raise
            raise CliRunError("claude", "the stale run was never stopped", transient=False)
        return CliCompletion(text=prompt, prompt_tokens=1, completion_tokens=1, model_ms=1)


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[_FakeClaude, CliDispatcher]:
    """Install the fake runner and a suggestion dispatcher with fresh pools.

    Args:
        tmp_path: The test's directory.
        monkeypatch: Pytest monkeypatch fixture.

    Returns:
        The fake runner and the dispatcher.
    """
    runner = _FakeClaude()
    monkeypatch.setitem(cli_dispatch.RUNNERS, "claude", runner)
    monkeypatch.setattr(cli_dispatch, "_semaphores", {})
    monkeypatch.setattr(cli_dispatch, "_latest_suggestion", {})
    return runner, CliDispatcher(None, "claude", MODEL, NEXT_PROMPT_ROLE, local_settings(tmp_path))


def test_newer_suggestion_stops_the_older_run(fake: tuple[_FakeClaude, CliDispatcher]) -> None:
    """A new turn's suggestion stops the stale run and still gets an answer."""
    runner, dispatcher = fake
    outcome: list[BaseException | CliCompletion] = []

    def older() -> None:
        """Run the stale suggestion and keep its outcome."""
        try:
            outcome.append(dispatcher._run_label("claude", "claude-haiku-5-5", "old", None))
        except CliRunError as error:
            outcome.append(error)

    thread = threading.Thread(target=older)
    thread.start()
    assert runner.first_started.wait(5)
    newer = dispatcher._run_label("claude", "claude-haiku-5-5", "new", None)
    thread.join(5)

    assert newer.text == "new"
    assert len(runner.first_error) == 1
    assert "newer suggestion replaced" in str(runner.first_error[0])
    assert isinstance(outcome[0], CliRunError)
    assert cli_dispatch._latest_suggestion == {}


def test_replaced_call_waiting_for_a_slot_does_not_run(fake: tuple[_FakeClaude, CliDispatcher]) -> None:
    """A suggestion that a newer one replaced before it started never starts the CLI."""
    runner, _dispatcher = fake
    pool = f"claude:{NEXT_PROMPT_ROLE}"
    replaced = cli_dispatch._replace_suggestion(pool)
    assert cli_dispatch._replace_suggestion(pool) is not replaced
    assert replaced.is_set()
    with pytest.raises(CliRunError, match="newer suggestion replaced"):
        cli_dispatch._stop_when_replaced(replaced, "claude")("content", "x")
    assert runner.calls == 0


def test_labels_still_fail_at_once_when_their_pool_is_busy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A thinking label never waits for, or replaces, another label."""
    monkeypatch.setattr(cli_dispatch, "_semaphores", {})
    dispatcher = CliDispatcher(None, "claude", MODEL, cli_dispatch.THINKING_LABEL_ROLE, local_settings(tmp_path))
    slot = cli_dispatch._semaphore(f"claude:{cli_dispatch.THINKING_LABEL_ROLE}", 1)
    assert slot.acquire(blocking=False)
    try:
        with pytest.raises(CliRunError, match="already running"):
            dispatcher._run_label("claude", "claude-haiku-5-5", "label", None)
    finally:
        slot.release()
