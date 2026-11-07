"""Score repository versions in the trusted parent's box, where the run's secrets live.

The guest that hosts the coding agent never sees the secrets or this box: it
posts each version through the parent's evaluator capability, and the parent
lays it out on a copy of the already set-up tree, then calls the user's
``score(repo_path)`` (or ``score(repo_path, case)``) on it.

A scorer returns a score with its feedback, like any scorer, and may return
per-case results under ``cases``, a mapping of case name to
``{"score", "feedback"}``. The run's score stays the top-level number; each
case becomes a named score that every engine reflects on and keeps on its
Pareto front.
"""

from __future__ import annotations

import json
import math
import re
import shlex
import threading
from collections.abc import Mapping
from typing import Any

from ....billing.model_dispatch import ModelHTTPResult
from . import runner, sandbox_log
from .harness import ENV_API_KEY, ENV_MODEL
from .protocol import STATUS_KEY
from .repo_workspace import REPO_DIR, WORK_DIR, RepoWorkspace, redact
from .rule_judge import RuleJudge
from .sandbox import SandboxSession
from .sandbox_scorer import RUNNER_SOURCE, ScorerGateway, _raise_control

CASES_KEY = "cases"
# The guest names no real endpoint: its requests reach only the parent relay.
REPO_SCORER_URL = "https://scoped-evaluator.invalid/score"
RUNNER_PATH = f"{REPO_DIR}/app_runner.py"
CALLS_DIR = f"{REPO_DIR}/calls"
# The guest waits for the version's checkout and the scorer together; copying a
# prepared tree with its installed dependencies can take a while, so its relay
# gets an allowance on top of the scorer's own timeout.
SETUP_ALLOWANCE_SECONDS = 1_800.0
# The proposer gets only these keys, so the scorer's other side information
# (weights, formulas, metric source) never reveals the metric (SKY-353).
_PROPOSER_KEYS = ("score", "feedback", STATUS_KEY, "scores")
# A traceback names the metric's files and quotes its source lines, and on
# Python 3.11+ adds caret lines under them; every indented line goes. An
# exception group draws the same frames behind "| " margins.
_TRACEBACK = re.compile(
    r"^[ +|]*(?:Exception Group )?Traceback \(most recent call last\):\n"
    r"|^(?P<margin>[ |]*?)  File \".*\n(?:(?P=margin)    .*\n)*",
    re.MULTILINE,
)


class RepoScoreError(Exception):
    """A version could not be scored; the message is safe to show the agent."""


def fold_case_scores(side_info: Mapping[str, Any]) -> dict[str, Any]:
    """Turn the per-case scores a repository scorer returned into named scores.

    Each case becomes a named score, so every engine reflects on it and
    treats it as its own objective, exactly like ``scores``.

    Args:
        side_info: Everything the scorer returned besides its score.

    Returns:
        The side info with ``cases`` folded into ``scores``.

    Raises:
        RepoScoreError: When ``cases`` is not a mapping of names to a finite
            score with its feedback.
    """
    folded = dict(side_info)
    cases = folded.pop(CASES_KEY, None)
    if cases is None:
        return folded
    try:
        named = runner.named_scores(cases)
    except runner.ScorerError as exc:
        raise RepoScoreError(f"'{CASES_KEY}' must map each case name to a score and feedback. {exc}") from exc
    if not all(math.isfinite(entry["score"]) for entry in named.values()):
        raise RepoScoreError(f"'{CASES_KEY}' must map each case name to a finite score.")
    folded["scores"] = {**named, **(folded.get("scores") or {})}
    return folded


class RepoScorer:
    """Score one repository version at a time in the parent's workspace box."""

    def __init__(
        self,
        workspace: RepoWorkspace,
        code: str,
        *,
        timeout_seconds: float,
        rule_judge: RuleJudge | None = None,
        gateway: ScorerGateway | None = None,
    ) -> None:
        """Bind the user's scorer code to a repository workspace.

        Args:
            workspace: Parent-owned box holding the pristine tree.
            code: Scorer source defining ``score(repo_path)`` or ``score(repo_path, case)``.
            timeout_seconds: Longest one scorer call may run, setup excluded.
            rule_judge: The repository's constraints-file enforcement, when it
                has an ``optimize.toml``: path-rule refusals then get the fixed
                illegal-action message, and the watch hook's warnings open
                the next scoring feedback. It never changes a score.
            gateway: The run's scoped model route from the trusted parent.
                The scorer's ``llm()`` and the version's own code reach it
                with ``APP_API_KEY`` and ``APP_MODEL``; the box itself stays
                offline and talks to the parent through the model mailbox.
        """
        self._workspace = workspace
        self._gateway = gateway
        self._code = code
        self._timeout_seconds = timeout_seconds
        self._rule_judge = rule_judge
        self._root: str | None = None
        self._calls = 0
        self._lock = threading.Lock()

    def score(self, patch: str, case: Any = None) -> dict[str, Any]:
        """Check out one version and score it.

        Args:
            patch: The version as a git patch against the starting commit.
            case: The case to score it on, when the run has cases.

        Returns:
            ``{"score": ..., **side_info}``, secrets redacted.

        Raises:
            RepoScoreError: When the version breaks the path rules or does not
                apply, changes a protected file while it runs, or the scorer
                fails or returns no score.
            BudgetReached: When the run's budget stopped a model call.
            BudgetError: When a model call had insufficient budget coverage.
            UsagePendingError: When a model call needs reconciliation.
            UnpricedOperationError: When the parent cannot bound a model call.
        """
        gateway = self._gateway
        secrets = list(self._workspace.secrets.values())
        if gateway is not None and gateway.api_key:
            secrets.append(gateway.api_key)
        judge = self._rule_judge
        # Checkout and scoring share one lock: the next version's checkout
        # replaces the tree this one is scored on.
        with self._lock:
            checkout = self._workspace.checkout(patch)
            if checkout.path is None:
                if judge is not None and checkout.blocked:
                    raise RepoScoreError(judge.blocked(checkout.problems))
                raise RepoScoreError(" ".join(checkout.problems))
            session = self._workspace.session()
            root = self._box_root(session)
            self._calls += 1
            call_dir = f"{root}/{CALLS_DIR}/{self._calls:06d}"
            session.write_files(
                {
                    f"{CALLS_DIR}/{self._calls:06d}/{runner.INPUT_FILE}": json.dumps(
                        {
                            "code": self._code,
                            "candidate": f"{root}/{checkout.path}",
                            "case": case,
                            "gateway": gateway.runner_payload() if gateway is not None else None,
                        }
                    )
                }
            )
            unstamped = self._workspace.stamp_before_run()
            if unstamped:
                raise RepoScoreError(unstamped)
            # A sink makes the session stream stderr live, which is where log() events travel.
            with sandbox_log.event_scope(source="scorer"):
                result = session.run(
                    f"cd {shlex.quote(checkout.path)} && python3 {shlex.quote(f'{root}/{RUNNER_PATH}')}"
                    f" {shlex.quote(call_dir)}",
                    # Importing a protected package would otherwise write
                    # __pycache__ into it and fail the protected-path check.
                    # Skipping the bytecode beats ignoring __pycache__, where a
                    # version could plant a .pyc that the judge then loads.
                    env={**self._workspace.secrets, **self._model_env(), "PYTHONDONTWRITEBYTECODE": "1"},
                    timeout_seconds=self._timeout_seconds,
                    on_output=sandbox_log.ignore_output,
                )
            if result.timed_out:
                raise RepoScoreError(f"The scorer exceeded its {self._timeout_seconds:g}s timeout.")
            tampered = self._workspace.protected_changes()
            if tampered:
                # The version's code names these files and can read the secrets.
                reason = redact(" ".join(tampered), secrets)
                raise RepoScoreError(judge.blocked([reason]) if judge is not None else reason)
            text = session.read_file(f"{CALLS_DIR}/{self._calls:06d}/{runner.OUTPUT_FILE}")
        if text is None:
            output = redact(_scrub(result.stderr or result.stdout, root)[-2_000:], secrets)
            raise RepoScoreError(f"The scorer did not finish (exit {result.exit_code}): {output}")
        output = json.loads(redact(text, secrets))
        _raise_control(output)
        if output.get("error") or output.get("score") is None:
            raise RepoScoreError(str(output.get("error") or "The scorer returned no score."))
        side_info = fold_case_scores(output.get("side_info") or {})
        return {**side_info, "score": float(output["score"])}

    def _model_env(self) -> dict[str, str]:
        """Return the env that points code in the box at the run's model route.

        The route values come after the run secrets, so a secret named like
        them cannot redirect the box to another key.

        Returns:
            ``APP_API_KEY`` and ``APP_MODEL``, or nothing without a route.
        """
        gateway = self._gateway
        if gateway is None or not gateway.api_key:
            return {}
        return {ENV_API_KEY: gateway.api_key, ENV_MODEL: gateway.model}

    def prepare(self) -> None:
        """Set the repository up and take its box offline before any version is sent.

        Raises:
            RepoSetupError: When the repository cannot be unpacked or set up.
            NetworkCutoffError: When the box's network cannot be switched off and confirmed.
        """
        with self._lock:
            self._workspace.session()

    def dispatch(self, body: Mapping[str, Any]) -> ModelHTTPResult:
        """Answer one guest scoring request through the evaluator capability.

        Args:
            body: ``{"candidate": <patch>, "case": ...}`` from the guest.

        Returns:
            200 with the score and side information, or 422 with why the
            version could not be scored, as text. Rule warnings the watch
            judge left since the last reply open the feedback of a 200.

        Raises:
            ValueError: When the body is not a patch and an optional case.
        """
        candidate = body.get("candidate")
        if set(body).difference({"candidate", "case"}) or not isinstance(candidate, str):
            raise ValueError("A repository version must be sent as a patch and an optional case.")
        try:
            document = self.score(candidate, body.get("case"))
        except RepoScoreError as error:
            # Plain text: the guest's relay client quotes the body in its error.
            return ModelHTTPResult(422, "text/plain; charset=utf-8", _scrub(str(error), self._root).encode())
        document = _for_proposer(document, self._root)
        notes = self._rule_judge.take_notes() if self._rule_judge is not None else []
        if notes:
            feedback = document.get("feedback")
            document["feedback"] = "\n\n".join([*notes, *([str(feedback)] if feedback else [])])
        return ModelHTTPResult(200, "application/json", json.dumps(document).encode())

    def _box_root(self, session: SandboxSession) -> str:
        """Return the box's working directory, installing the runner on first use.

        Args:
            session: The workspace's open box.

        Returns:
            The absolute directory relative paths in the box resolve against.

        Raises:
            RepoScoreError: When the box cannot report its directory.
        """
        if self._root is None:
            located = session.run("pwd", timeout_seconds=30)
            root = located.stdout.strip()
            if not located.ok or not root.startswith("/"):
                raise RepoScoreError("The scoring box could not report its working directory.")
            session.write_files({RUNNER_PATH: RUNNER_SOURCE})
            self._root = root
        return self._root

    def close(self) -> None:
        """Destroy the workspace box."""
        self._workspace.close()


def _scrub(text: str, root: str | None) -> str:
    """Remove traceback frames and the scorer box's absolute paths from text the proposer reads.

    Args:
        text: An error or a feedback string from the scorer box.
        root: The box's working directory; ``None`` before it is known.

    Returns:
        The text with repository-relative paths and without traceback frames.
    """
    text = _TRACEBACK.sub("", text if text.endswith("\n") else f"{text}\n").rstrip("\n")
    if root:
        text = (
            text.replace(f"{root}/{WORK_DIR}/", "")
            .replace(f"{root}/{WORK_DIR}", ".")
            .replace(f"{root}/", "")
            .replace(root, "")
        )
    return text


def _for_proposer(document: Mapping[str, Any], root: str | None) -> dict[str, Any]:
    """Keep only the score, the feedback and the named scores of a scoring result.

    Args:
        document: What :meth:`RepoScorer.score` returned.
        root: The box's working directory, scrubbed from the feedback.

    Returns:
        The reply the guest receives.
    """
    reply = {key: document[key] for key in _PROPOSER_KEYS if key in document}
    if isinstance(reply.get("feedback"), str):
        reply["feedback"] = _scrub(reply["feedback"], root)
    if isinstance(reply.get("scores"), Mapping):
        reply["scores"] = {
            name: _named_score(entry, root) if isinstance(entry, Mapping) else entry
            for name, entry in reply["scores"].items()
        }
    return reply


def _named_score(entry: Mapping[str, Any], root: str | None) -> dict[str, Any]:
    """Keep only the score and the scrubbed feedback of one named score.

    Args:
        entry: One value of the result's ``scores`` mapping.
        root: The box's working directory, scrubbed from the feedback.

    Returns:
        The named score the guest receives.
    """
    kept = {key: entry[key] for key in ("score", "feedback") if key in entry}
    if isinstance(kept.get("feedback"), str):
        kept["feedback"] = _scrub(kept["feedback"], root)
    return kept
