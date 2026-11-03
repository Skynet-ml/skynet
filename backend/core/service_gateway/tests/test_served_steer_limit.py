"""Test served-chat steering limits at the real driver's mailbox boundary."""

from typing import Any

import dspy
import pytest

from ..agents import react_serve
from ..agents.steering import SteerInbox, SteerInputError, deliver_steering
from .test_react_serve import test_live_chat_steers_next_step_without_extra_metered_calls as run_live_chat


@pytest.mark.parametrize("messages", [["x" * 2001], ["x" * 1000, "y" * 1000], ["x" * 2000], []])
async def test_served_steer_input_never_exceeds_user_message_limit(
    monkeypatch: pytest.MonkeyPatch, messages: list[str],
) -> None:
    """Keep oversize input unread and bound each joined served-chat delivery.

    Args:
        monkeypatch: Capture the inbox attached by the real served-chat driver.
        messages: Additional steers posted after a normal live-driver turn.
    """
    attached: list[tuple[Any, SteerInbox]] = []
    attach = react_serve.attach_steering

    def capture(program: Any, inbox: SteerInbox) -> None:
        """Record and attach the real driver's inbox.

        Args:
            program: The served ReAct loop.
            inbox: The server's configured mailbox.
        """
        attached.append((program, inbox))
        attach(program, inbox)

    monkeypatch.setattr(react_serve, "attach_steering", capture)
    await run_live_chat(monkeypatch, True)
    program, inbox = attached[0]
    if messages == ["x" * 2001]:
        with pytest.raises(SteerInputError):
            inbox.store.post("alice", "turn", messages[0])
        return
    posted = [inbox.store.post("alice", "turn", message) for message in messages]
    kwargs: dict[str, Any] = {"history": dspy.History(messages=[])}
    deliver_steering(program.react, kwargs)
    delivered = kwargs.get(inbox.input_field, "")
    assert len(delivered) <= 2000
    if messages == ["x" * 2001]:
        assert delivered == ""
        assert inbox.store.withdraw("alice", "turn", posted)[0] == posted
    elif messages:
        assert delivered == messages[0]
        if len(messages) == 2:
            assert inbox.store.withdraw("alice", "turn", posted)[0] == [posted[1]]
    else:
        assert delivered == ""
