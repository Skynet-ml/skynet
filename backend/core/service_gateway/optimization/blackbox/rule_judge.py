"""Watch a repository run against its intent and plain-language rules.

A repository's ``optimize.toml`` may state the run's intent under
``[instructions] intent`` and list illegal approaches under ``[rules]
illegal``. Path rules cannot catch either, so an optional watch hook reads the
agent's recent steps against them during the run:

- It is off by default and turned on per run.
- Every :data:`WATCH_EVERY_ACTIONS` agent actions, it reads the last
  :data:`WATCH_EVERY_ACTIONS` steps, the intent and the rules.
- It answers with a typed label (``ok``, ``off_intent`` or ``breaks_rule``)
  and a confidence. At or above the run's threshold, a non-``ok`` label
  queues a warning for the agent's next scoring reply.
- It never stops the run and never changes a score. Every verdict goes into
  the run log.

The hook never sees the metric, the scorer, its data or its labels, so it
cannot leak them to the agent or judge by the score. A step that runs the
scoring script or carries a score reply is withheld before it reaches the
hook, because the reply holds the score and the scorer's feedback. The final
judge that scored rejected versions 0 is gone (SKY-349): a hint is enough,
and a wrong rejection lost real improvements.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .... import run_log
from . import sandbox_log
from .classification import HOOK_LABELS, ClassificationError, HookClassification, answer_format, parse_hook_reply
from .repo_constraints import CONSTRAINTS_FILE, numbered_rules
from .repo_tree import ILLEGAL_ACTION_MESSAGE, PATH_RULE, RULE_BLOCK_EVENT

logger = logging.getLogger(__name__)

WATCH_EVERY_ACTIONS = 15
DEFAULT_WARN_CONFIDENCE = 0.7
JUDGE_ANSWER_TOKENS = 600
# OpenRouter counts reasoning tokens inside max_tokens. A judge that reasons
# gets this much more, or its thinking can use the whole limit and leave an
# empty verdict that is still billed.
JUDGE_REASONING_TOKENS = 8_000
_MAX_STEPS_CHARS = 60_000
_MAX_INTENT_CHARS = 4_000
_MAX_ACTION_CHARS = 2_000
# One step is the agent's latest tool call and its result: the last two turns
# of a model call. Older turns were already seen by earlier calls.
_STEP_TURNS = 2
_EVAL_SCRIPT = "eval.sh"
# A score reply holds "score" and "info"; ``info`` carries the scorer's
# feedback. The keys can be JSON-escaped once or more inside a turn.
_SCORE_REPLY_KEYS = tuple(re.compile(rf'\\*"{key}\\*"\s*:') for key in ("score", "info"))
WITHHELD_STEP = "The agent ran the scoring script. The call and its reply are withheld."

EVENT_BLOCK = RULE_BLOCK_EVENT
EVENT_NOTE = "rule.note"
EVENT_VERDICT = "rule.verdict"
_EVENT_TITLES = {
    EVENT_BLOCK: "Illegal action blocked",
    EVENT_NOTE: "Rule warning sent to the agent",
    EVENT_VERDICT: "Watch hook verdict",
}
_NOTES = {
    "off_intent": "Check the run's intent in the constraints file. Your recent work seems to move away from it: {reason}",
    "breaks_rule": "Check the constraints file. You are probably about to break one of its rules: {reason}",
}

Ask = Callable[[str], "str | None"]
Emit = Callable[[dict[str, Any]], None]
RunWatch = Callable[[Callable[[], None]], None]


def watch_prompt(intent: str | None, rules: Sequence[str], steps: Sequence[str]) -> str:
    """Write the watch hook's prompt for the agent's recent steps.

    Args:
        intent: The run's stated intent, when the repository gives one.
        rules: The plain-language rules.
        steps: The agent's recent steps, oldest first.

    Returns:
        The prompt. It holds no metric, scorer, data or labels. The steps
        hold no score or scorer feedback when they come from
        :func:`describe_model_call`.
    """
    rules_text = "\n".join(f"{label}: {rule}" for label, rule in numbered_rules(rules)) or "(no rules)"
    intent_text = (intent or "").strip()[:_MAX_INTENT_CHARS] or "(no intent given)"
    recent = "\n".join(f"- {step}" for step in steps)[-_MAX_STEPS_CHARS:]
    return (
        "You watch a code optimization agent. The repository's"
        f" {CONSTRAINTS_FILE} gives the run's intent and its rules.\n\n"
        f"<intent>\n{intent_text}\n</intent>\n\n<rules>\n{rules_text}\n</rules>\n\n"
        'Read the agent\'s recent steps. Use "breaks_rule" when the work goes toward a rule violation,'
        ' "off_intent" when it moves away from the intent, and "ok" otherwise. Flag only clear signs,'
        " not ordinary work.\n\n"
        f"{answer_format(HOOK_LABELS)}\n\n<steps>\n{recent}\n</steps>"
    )


def describe_model_call(body: Mapping[str, Any]) -> str:
    """Describe one agent model call as a step for the watch hook.

    Each call the coding agent makes carries its latest tool call and its
    result as its last turns, in any of the chat, messages or responses
    formats. The system prompt and instructions are left out. A step that
    runs the scoring script, or that carries a score reply, becomes
    :data:`WITHHELD_STEP`, so the hook never reads a score or the scorer's
    feedback.

    Args:
        body: The model request the agent sent.

    Returns:
        The end of the call's latest turns as JSON text, at most
        ``_MAX_ACTION_CHARS`` characters, or :data:`WITHHELD_STEP`.
    """
    turns = body.get("messages") if isinstance(body.get("messages"), list) else body.get("input")
    if isinstance(turns, str):
        latest: Any = turns
    elif isinstance(turns, list):
        latest = [turn for turn in turns if not (isinstance(turn, dict) and turn.get("role") == "system")]
        latest = latest[-_STEP_TURNS:]
    else:
        latest = []
    text = json.dumps(latest, ensure_ascii=False, default=str)
    if _EVAL_SCRIPT in text or all(key.search(text) for key in _SCORE_REPLY_KEYS):
        return WITHHELD_STEP
    return text[-_MAX_ACTION_CHARS:]


def run_log_emitter(owner: str) -> Emit:
    """Return an event sink that writes rule events into one run's log.

    The hook runs on the parent's gateway threads, which are not the job's,
    so the events go through the sandbox log router, which persists a
    record by its owner whatever thread wrote it.

    Args:
        owner: The run the events belong to.

    Returns:
        A function that logs one event as a typed host record.
    """

    def emit(event: dict[str, Any]) -> None:
        """Log one rule event.

        Args:
            event: ``{"kind", ...fields}``; every key besides ``kind`` becomes a field.
        """
        kind = str(event["kind"])
        fields = {key: value for key, value in event.items() if key != "kind"}
        level = logging.WARNING if kind == EVENT_BLOCK else logging.INFO
        sandbox_log.stream_logger.log(
            level,
            "%s: %s",
            _EVENT_TITLES.get(kind, kind),
            ", ".join(f"{key}={value}" for key, value in fields.items()),
            extra=run_log.event_extra(source="host", event=kind, fields=fields, sandbox_owner=owner),
        )

    return emit


def _start_thread(watch: Callable[[], None]) -> None:
    """Run one watch off the agent's request path.

    Args:
        watch: The watch to run.
    """
    threading.Thread(target=watch, daemon=True, name="rule-watch").start()


class RuleJudge:
    """Record path-rule blocks and, when on, watch the agent against its intent and rules."""

    def __init__(
        self,
        rules: Sequence[str],
        *,
        intent: str | None = None,
        ask: Ask | None,
        emit: Emit,
        threshold: float = DEFAULT_WARN_CONFIDENCE,
        run_watch: RunWatch = _start_thread,
    ) -> None:
        """Bind the intent and rules to the watch hook model.

        Args:
            rules: The plain-language rules, in file order.
            intent: The run's stated intent, when the repository gives one.
            ask: Sends one prompt to the watch hook model and returns its
                reply, or ``None`` when the call failed. ``None`` turns the
                watch off: path-rule blocks are still recorded and no model
                is asked.
            emit: Records one run event.
            threshold: The lowest confidence at which a non-``ok`` verdict
                warns the agent.
            run_watch: Runs one watch; by default on its own thread, so the
                agent's model call never waits for it.
        """
        self.rules = tuple(rules)
        self.intent = intent
        self._ask = ask
        self._emit = emit
        self._threshold = threshold
        self._run_watch = run_watch
        self._steps: list[str] = []
        self._since_watch = 0
        self._notes: list[str] = []
        self._lock = threading.Lock()

    @property
    def watching(self) -> bool:
        """Return whether the watch hook asks a model."""
        return self._ask is not None

    def blocked(self, problems: Sequence[str]) -> str:
        """Record a version the path rules refused and return what the agent reads.

        Args:
            problems: Why the gate refused it, for the run's events only.

        Returns:
            The fixed illegal-action message.
        """
        self._emit({"kind": EVENT_BLOCK, "rule": PATH_RULE, "reason": " ".join(problems)})
        return ILLEGAL_ACTION_MESSAGE

    def record_action(self, action: str) -> None:
        """Count one agent step and watch the last steps every :data:`WATCH_EVERY_ACTIONS` steps.

        Args:
            action: A short description of what the agent did.
        """
        if self._ask is None:
            return
        with self._lock:
            self._steps.append(action)
            del self._steps[:-WATCH_EVERY_ACTIONS]
            self._since_watch += 1
            if self._since_watch < WATCH_EVERY_ACTIONS:
                return
            self._since_watch = 0
            recent = list(self._steps)
        self._run_watch(lambda: self._watch(recent))

    def _watch(self, steps: Sequence[str]) -> None:
        """Ask the watch hook about the recent steps, log its verdict, and queue a warning when it is sure.

        A failed watch is logged and dropped: the hook never stops the run.

        Args:
            steps: The recent steps, oldest first.
        """
        ask = self._ask
        if ask is None:
            return
        try:
            verdict = parse_hook_reply(ask(watch_prompt(self.intent, self.rules, steps)))
        except ClassificationError as error:
            logger.warning("The rule watch got an answer it could not read: %s", error)
            return
        except Exception:  # isolation boundary: a watch must never stop the run
            logger.warning("The rule watch could not ask its model.", exc_info=True)
            return
        warned = self._warns(verdict)
        self._emit(
            {
                "kind": EVENT_VERDICT,
                "label": verdict.label,
                "confidence": verdict.confidence,
                "reason": verdict.reason,
                "warned": warned,
            }
        )
        if not warned:
            return
        with self._lock:
            self._notes.append(_NOTES[verdict.label].format(reason=verdict.reason))
        self._emit({"kind": EVENT_NOTE, "label": verdict.label, "reason": verdict.reason})

    def _warns(self, verdict: HookClassification) -> bool:
        """Return whether a verdict is sure enough to warn the agent.

        Args:
            verdict: The watch hook's answer.

        Returns:
            ``True`` for a non-``ok`` label at or above the threshold.
        """
        return verdict.label != "ok" and verdict.confidence >= self._threshold

    def take_notes(self) -> list[str]:
        """Return and clear the warnings the agent has not read yet.

        Returns:
            The pending notes, oldest first.
        """
        with self._lock:
            notes, self._notes = self._notes, []
        return notes
