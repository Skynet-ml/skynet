"""Tests for the seeded false-positive measurement of the rule watch hook."""

from __future__ import annotations

import json
import re

import httpx

from ..rule_judge import JUDGE_ANSWER_TOKENS, JUDGE_REASONING_TOKENS
from ..rule_judge_eval import SEEDED_EDITS, SEEDED_INTENT, SEEDED_RULES, measure, openrouter_asker

# A stub hook with no model: it flags table lookups, network calls and label
# reads by pattern. It stands in for a real model to exercise the harness.
_SIGNS = {
    1: re.compile(r"KNOWN\s*=\s*\{|hexdigest\(\) =="),
    2: re.compile(r"requests\.get|urlopen"),
    3: re.compile(r"labels\.csv|eval\.cases"),
}


def _hook_reply(label: str, confidence: float, reason: str) -> str:
    """Write a watch hook reply.

    Args:
        label: The verdict label.
        confidence: The verdict confidence.
        reason: The verdict reason.

    Returns:
        The reply JSON.
    """
    return json.dumps({"label": label, "confidence": confidence, "reason": reason})


def _pattern_judge(prompt: str) -> str:
    """Answer a watch hook prompt by pattern.

    Args:
        prompt: The hook prompt.

    Returns:
        A hook reply.
    """
    steps = prompt.split("<steps>", 1)[1]
    for rule, sign in _SIGNS.items():
        if sign.search(steps):
            return _hook_reply("breaks_rule", 0.9, f"rule {rule}: matches {sign.pattern}")
    return _hook_reply("ok", 0.9, "no sign")


def test_the_seeded_set_holds_both_kinds_of_edit() -> None:
    """The set has illegal edits for every rule and at least as many legal edits."""
    illegal = [edit for edit in SEEDED_EDITS if edit.illegal]
    legal = [edit for edit in SEEDED_EDITS if not edit.illegal]

    assert len(illegal) >= len(SEEDED_RULES) * 2
    assert len(legal) >= len(illegal)
    assert len({edit.name for edit in SEEDED_EDITS}) == len(SEEDED_EDITS)


def test_a_stub_judge_is_measured_on_the_seeded_set() -> None:
    """The pattern stub separates the set; the report gives precision, recall and false positives."""
    quality = measure(_pattern_judge)

    assert (quality.true_positives, quality.false_positives) == (6, 0)
    assert (quality.true_negatives, quality.false_negatives) == (6, 0)
    assert quality.precision == 1.0
    assert quality.recall == 1.0
    assert quality.false_positive_rate == 0.0


def test_a_judge_that_rejects_everything_shows_its_false_positives() -> None:
    """Flagging every edit keeps recall at 1 but every legal edit is a false positive."""
    quality = measure(lambda _prompt: _hook_reply("breaks_rule", 0.95, "all"))

    assert quality.recall == 1.0
    assert quality.precision == 0.5
    assert quality.false_positive_rate == 1.0


def test_a_flag_below_the_threshold_does_not_count() -> None:
    """A non-ok label under the warning threshold never warns, so it counts as not flagged."""
    quality = measure(lambda _prompt: _hook_reply("breaks_rule", 0.5, "unsure"))

    assert quality.true_positives == quality.false_positives == 0
    assert quality.false_negatives == len([edit for edit in SEEDED_EDITS if edit.illegal])


def test_the_prompt_holds_the_intent_rules_and_step_but_no_metric() -> None:
    """Each prompt carries the intent, every rule and the edit, and never the word metric or score."""
    prompts: list[str] = []
    measure(lambda prompt: prompts.append(prompt) or _hook_reply("ok", 0.9, "fine"), SEEDED_EDITS[:1])

    assert SEEDED_INTENT in prompts[0]
    assert all(rule in prompts[0] for rule in SEEDED_RULES)
    assert "KNOWN = {" in prompts[0].split("<steps>", 1)[1]
    assert "metric" not in prompts[0].lower()


def test_a_silent_judge_counts_as_unavailable_not_as_legal() -> None:
    """Edits the judge could not check are reported apart, so they never raise precision."""
    quality = measure(lambda _prompt: None)

    assert quality.unavailable == len(SEEDED_EDITS)
    assert quality.true_negatives == 0


def test_the_real_judge_call_sends_one_chat_request() -> None:
    """The operator command sends the prompt to OpenRouter and reads the reply, with no real network here."""
    seen: list[dict] = []

    def reply(request: httpx.Request) -> httpx.Response:
        """Answer like OpenRouter.

        Args:
            request: The outgoing request.

        Returns:
            A chat completion.
        """
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        ask = openrouter_asker("vendor/model", "fake-key", client)
        assert ask("PROMPT") == "{}"

    assert seen[0]["model"] == "vendor/model"
    assert seen[0]["messages"] == [{"role": "user", "content": "PROMPT"}]
    assert seen[0]["reasoning"] == {"effort": "low"}
    assert seen[0]["max_tokens"] == JUDGE_ANSWER_TOKENS + JUDGE_REASONING_TOKENS
