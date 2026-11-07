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

A run whose ``optimize.toml`` has ``[target.<name>]`` stages opens one offline
box for each stage for each candidate evaluation, all before the scorer runs
(E29). The scorer reaches them with ``call(name, input)``: the call is a
framed line on the scorer's stdout, the parent runs the stage in its box, and
writes the answer back into the scorer box. That relay is the stages' only
way out (E27).
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import re
import shlex
import threading
import uuid
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from ....billing.budgets import BudgetError, BudgetInsufficientError
from ....billing.model_dispatch import ModelHTTPResult
from ....billing.operation_pricing import UnpricedOperationError
from ....billing.runtime import UsagePendingError
from ....billing.signals import BudgetReached
from . import runner, sandbox_log
from .harness import ENV_API_KEY, ENV_MODEL
from .protocol import STATUS_KEY
from .repo_constraints import TargetStage
from .repo_services import open_stage_boxes, stage_spec
from .repo_tree import archive_chunks, drop_paths
from .repo_workspace import HIDDEN_CHANGED, REPO_DIR, WORK_DIR, RepoWorkspace, redact
from .rule_judge import RuleJudge
from .sandbox import SandboxSession
from .sandbox_contract import SandboxRuntime
from .sandbox_scorer import RUNNER_SOURCE, ScorerGateway, _raise_control

logger = logging.getLogger(__name__)

CASES_KEY = "cases"
# The guest names no real endpoint: its requests reach only the parent relay.
REPO_SCORER_URL = "https://scoped-evaluator.invalid/score"
RUNNER_PATH = f"{REPO_DIR}/app_runner.py"
CALLS_DIR = f"{REPO_DIR}/calls"
# The guest waits for the version's checkout and the scorer together; copying a
# prepared tree with its installed dependencies can take a while, so its relay
# gets an allowance on top of the scorer's own timeout.
SETUP_ALLOWANCE_SECONDS = 1_800.0
STAGE_DIR = ".app-stage"
_STAGE_TREE = f"{STAGE_DIR}/tree"
# Unpacking the tree and applying the version in a stage box; the box's hold
# covers this on top of the scorer's own timeout.
_STAGE_LAYOUT_SECONDS = 600.0
_STAGE_ERROR_CHARS = 500
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
        stages: Mapping[str, TargetStage] | None = None,
        stage_runtime: SandboxRuntime | None = None,
        stage_archive: Path | None = None,
        hidden_paths: tuple[str, ...] = (),
        max_concurrency: int = 1,
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
            stages: The ``[target.<name>]`` stages the scorer may ``call``;
                none keeps the scorer as it was.
            stage_runtime: Opens the stage boxes; required with stages.
            stage_archive: The tree each stage box gets, without the hidden
                metric files; required with stages.
            hidden_paths: Metric files and the exploit folder, dropped from
                each version before a stage box applies it.
            max_concurrency: How many calls to one stage run at the same time.

        Raises:
            ValueError: When stages are given without a runtime or a tree.
        """
        if stages and (stage_runtime is None or stage_archive is None):
            raise ValueError("Target stages need a runtime and a tree to open their boxes with.")
        self._stages = dict(stages or {})
        self._stage_runtime = stage_runtime
        self._stage_archive = stage_archive
        self._hidden = hidden_paths
        self._max_concurrency = max(1, max_concurrency)
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
            document = {
                "code": self._code,
                "candidate": f"{root}/{checkout.path}",
                "case": case,
                "gateway": gateway.runner_payload() if gateway is not None else None,
            }
            with self._stage_relay(session, patch, f"{CALLS_DIR}/{self._calls:06d}/stages") as relay:
                if relay is not None:
                    document["stages"] = relay.config(root)
                session.write_files({f"{CALLS_DIR}/{self._calls:06d}/{runner.INPUT_FILE}": json.dumps(document)})
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
                        on_output=relay.output if relay is not None else sandbox_log.ignore_output,
                    )
            if result.timed_out:
                raise RepoScoreError(f"The scorer exceeded its {self._timeout_seconds:g}s timeout.")
            changes = self._workspace.protected_changes()
            # A hidden change is not a [paths] rule the agent knows, so it
            # skips the rule judge and its "do not repeat it" message.
            tampered = [change for change in changes if change != HIDDEN_CHANGED]
            if tampered:
                # The version's code names these files and can read the secrets.
                reason = redact(" ".join(tampered), secrets)
                raise RepoScoreError(judge.blocked([reason]) if judge is not None else reason)
            if changes:
                raise RepoScoreError(HIDDEN_CHANGED)
            text = session.read_file(f"{CALLS_DIR}/{self._calls:06d}/{runner.OUTPUT_FILE}")
        # The version's code runs next to the hidden metric and can print or
        # raise its contents, so the guest gets only a fixed message; the
        # details stay in the server log (SKY-353).
        if text is None:
            logger.warning(
                "Repository scorer did not finish (exit %s): %s",
                result.exit_code,
                redact((result.stderr or result.stdout)[-2_000:], secrets),
            )
            raise RepoScoreError(f"The scorer did not finish (exit {result.exit_code}).")
        output = json.loads(redact(text, secrets))
        _raise_control(output)
        if output.get("error"):
            logger.warning("Repository scorer raised: %s", output["error"])
            raise RepoScoreError("The scorer raised an error, so the version has no score.")
        if output.get("score") is None:
            raise RepoScoreError("The scorer returned no score.")
        try:
            score = float(output["score"])
        except (TypeError, ValueError):
            # The error text would echo the value, which the version's code controls.
            raise RepoScoreError("The scorer returned a score that is not a number.") from None
        side_info = fold_case_scores(output.get("side_info") or {})
        return {**side_info, "score": score}

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

    @contextlib.contextmanager
    def _stage_relay(self, scorer_box: SandboxSession, patch: str, response_dir: str) -> Iterator[_StageRelay | None]:
        """Open this evaluation's stage boxes, lay the version out in each, and relay calls to them.

        Every box opens before the scorer's first sample, or none stays open
        (E29, E32). Each box has its own hold, keyed to this evaluation and
        its stage (E31), and every box closes when the evaluation ends, also
        on a failure.

        Args:
            scorer_box: The scorer's box, where answers are written.
            patch: The version being scored.
            response_dir: The call's answer folder, relative to the scorer box.

        Yields:
            The relay, or ``None`` when the run has no stages.

        Raises:
            RepoScoreError: When a stage box cannot open for a reason other
                than the budget, or cannot lay out the version.
            BudgetReached: When the budget refuses a stage box.
            BudgetError: When a stage box's hold does not fit the budget.
            UsagePendingError: When earlier usage still needs reconciliation.
            UnpricedOperationError: When the parent cannot bound a stage box.
        """
        if not self._stages:
            yield None
            return
        evaluation_id = uuid.uuid4().hex
        specs = {
            name: stage_spec(
                name,
                stage,
                evaluation_id=evaluation_id,
                lifetime_seconds=self._timeout_seconds + _STAGE_LAYOUT_SECONDS,
                tags={},
            )
            for name, stage in self._stages.items()
        }
        try:
            boxes = open_stage_boxes(self._stage_runtime, specs)
        except (BudgetReached, BudgetError, UsagePendingError, UnpricedOperationError):
            raise
        except Exception as error:
            logger.warning("Stage boxes did not open: %s", error)
            raise RepoScoreError("The stage boxes could not open, so the version has no score.") from None
        relay = _StageRelay(
            boxes,
            self._stages,
            scorer_box=scorer_box,
            response_dir=response_dir,
            max_concurrency=self._max_concurrency,
            env=self._model_env(),
        )
        try:
            kept = drop_paths(patch, self._hidden)
            with ThreadPoolExecutor(max_workers=len(boxes), thread_name_prefix="stage-layout") as pool:
                laid_out = list(pool.map(lambda box: self._lay_out(box, kept), boxes.values()))
            if not all(laid_out):
                raise RepoScoreError("A stage box could not lay out the version, so it has no score.")
            yield relay
        finally:
            relay.close()

    def _lay_out(self, box: SandboxSession, patch: str) -> bool:
        """Unpack the tree without the hidden metric into one stage box and apply the version.

        Stage boxes get no setup command, so the tree runs with the image's
        own tools only.

        Args:
            box: One open stage box.
            patch: The version, hidden paths already dropped.

        Returns:
            Whether the tree and the version are in place.
        """
        parts = []
        for index, chunk in enumerate(archive_chunks(self._stage_archive)):
            name = f"{STAGE_DIR}/tree.{index:04d}.b64"
            box.write_files({name: chunk})
            parts.append(name)
        box.write_files(
            {f"{STAGE_DIR}/{RUNNER_PATH.rsplit('/', 1)[1]}": RUNNER_SOURCE, f"{STAGE_DIR}/version.patch": patch}
        )
        files = " ".join(shlex.quote(part) for part in parts)
        result = box.run(
            f"set -e; mkdir -p {_STAGE_TREE}; cat {files} | base64 -d | tar -xzf - -C {_STAGE_TREE}; rm -f {files};"
            f" cd {_STAGE_TREE}; if [ -s ../version.patch ]; then git apply --binary --whitespace=nowarn ../version.patch; fi",
            timeout_seconds=_STAGE_LAYOUT_SECONDS,
        )
        if not result.ok:
            logger.warning(
                "Stage box layout failed (exit %s): %s", result.exit_code, (result.stderr or result.stdout)[-2_000:]
            )
        return result.ok

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


class _StageRelay:
    """Answer one scorer call's ``call(name, input)`` frames from its open stage boxes.

    Calls to one stage run in parallel, each in its own process and folder,
    up to ``max_concurrency`` at a time (E34).
    """

    def __init__(
        self,
        boxes: Mapping[str, SandboxSession],
        stages: Mapping[str, TargetStage],
        *,
        scorer_box: SandboxSession,
        response_dir: str,
        max_concurrency: int,
        env: Mapping[str, str],
    ) -> None:
        """Bind the open stage boxes to the scorer box that waits for their answers.

        Args:
            boxes: Each stage's open box, by stage name.
            stages: Each stage's settings, by stage name.
            scorer_box: The scorer's box, where answers are written.
            response_dir: The call's answer folder, relative to the scorer box.
            max_concurrency: How many calls to one stage run at the same time.
            env: The run's model route, so stage code reaches models only
                through the parent's mailbox.
        """
        self._boxes = dict(boxes)
        self._stages = stages
        self._scorer_box = scorer_box
        self._response_dir = response_dir
        self._env = dict(env)
        # A fresh nonce for each evaluation: version code cannot forge a frame
        # it never saw, and a frame from an earlier call never matches.
        self._prefix = f"APP_STAGE_{uuid.uuid4().hex}:"
        self._pools = {
            name: ThreadPoolExecutor(max_workers=max_concurrency, thread_name_prefix=f"stage-{name}")
            for name in self._boxes
        }
        self._refusals = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stage-refusal")
        self._pending = ""
        self._seen: set[str] = set()
        self._lock = threading.Lock()

    def config(self, root: str) -> dict[str, Any]:
        """Describe the relay to the runner in the scorer box.

        Args:
            root: The scorer box's working directory.

        Returns:
            ``{"prefix", "response_dir", "names"}`` for the call's input.
        """
        return {"prefix": self._prefix, "response_dir": f"{root}/{self._response_dir}", "names": list(self._boxes)}

    def output(self, stream: str, data: str) -> None:
        """Take the scorer's output as it streams and hand each call frame to its stage's pool.

        The mailbox calls this under its own lock, so it only queues work.

        Args:
            stream: ``stdout`` or ``stderr``.
            data: The next piece of output.
        """
        if stream != "stdout":
            sandbox_log.ignore_output(stream, data)
            return
        with self._lock:
            self._pending += data
            lines = self._pending.split("\n")
            self._pending = lines.pop()
            for line in lines:
                if not line.startswith(self._prefix):
                    sandbox_log.ignore_output(stream, line + "\n")
                    continue
                try:
                    frame = json.loads(line[len(self._prefix) :])
                    identity, name = frame["id"], frame["stage"]
                except (ValueError, KeyError, TypeError):
                    continue
                if (
                    not isinstance(identity, str)
                    or len(identity) != 32
                    or any(char not in "0123456789abcdef" for char in identity)
                    or identity in self._seen
                ):
                    continue
                self._seen.add(identity)
                pool = self._pools.get(name) if isinstance(name, str) else None
                if pool is None:
                    self._refusals.submit(self._answer, identity, {"error": f"Unknown stage {name!r}."})
                else:
                    pool.submit(self._call, name, identity, frame.get("input"))

    def _call(self, name: str, identity: str, value: Any) -> None:
        """Run one stage call in its box and write the answer for the scorer.

        Args:
            name: The stage.
            identity: The call's id, also its folder and answer name.
            value: The JSON input.
        """
        try:
            response = self._run(name, identity, value)
        except BudgetReached as error:
            response = {"control": {"code": "budget_reached", "message": str(error)}}
        except BudgetInsufficientError as error:
            response = {"control": {"code": "budget_insufficient", "message": str(error)}}
        except UnpricedOperationError as error:
            response = {"control": {"code": "unpriced_operation", "message": str(error)}}
        except (BudgetError, UsagePendingError) as error:
            response = {"control": {"code": "usage_pending", "message": str(error)}}
        except Exception as error:
            logger.warning("Stage %s call failed in the parent: %s", name, error)
            response = {"error": f"Stage {name!r} could not run."}
        self._answer(identity, response)

    def _run(self, name: str, identity: str, value: Any) -> dict[str, Any]:
        """Run one stage call in the stage's box.

        A Python entry runs through the runner's stage mode; a command reads
        the input on stdin and writes its JSON output on stdout.

        Args:
            name: The stage.
            identity: The call's id, also its folder name.
            value: The JSON input.

        Returns:
            ``{"output": ...}``, or ``{"error": ...}`` when the stage failed,
            passed its timeout, or returned no valid JSON.
        """
        stage, box = self._stages[name], self._boxes[name]
        folder = f"{STAGE_DIR}/calls/{identity}"
        box.write_files({f"{folder}/{runner.INPUT_FILE}": json.dumps(value)})
        relative = f"../calls/{identity}"
        if isinstance(stage.entry, str):
            command = (
                f"cd {_STAGE_TREE} && python3 ../{RUNNER_PATH.rsplit('/', 1)[1]} {runner.STAGE_FLAG}"
                f" {relative} {shlex.quote(stage.entry)}"
            )
        else:
            command = (
                f"cd {_STAGE_TREE} && {shlex.join(stage.entry)}"
                f" < {relative}/{runner.INPUT_FILE} > {relative}/{runner.OUTPUT_FILE}"
            )
        result = box.run(command, env=self._env, timeout_seconds=stage.timeout, on_output=sandbox_log.ignore_output)
        if result.timed_out:
            return {"error": f"Stage {name!r} passed its {stage.timeout:g}s timeout."}
        if not result.ok:
            tail = (result.stderr or result.stdout).strip()[-_STAGE_ERROR_CHARS:]
            return {"error": f"Stage {name!r} failed (exit {result.exit_code}): {tail}"}
        text = box.read_file(f"{folder}/{runner.OUTPUT_FILE}")
        try:
            output = json.loads(text) if text else None
        except ValueError:
            output = None
        if isinstance(stage.entry, str):
            if isinstance(output, dict) and "output" in output:
                return {"output": output["output"]}
            if isinstance(output, dict) and output.get("error"):
                return {"error": f"Stage {name!r} raised {output['error']}"}
            return {"error": f"Stage {name!r} returned no valid output."}
        if text is None or not text.strip():
            return {"error": f"Stage {name!r} returned no output."}
        if output is None and text.strip() != "null":
            return {"error": f"Stage {name!r} returned output that is not JSON."}
        return {"output": output}

    def _answer(self, identity: str, response: Mapping[str, Any]) -> None:
        """Write one answer where the scorer's ``call`` waits for it.

        Args:
            identity: The call's id.
            response: ``{"output"}``, ``{"error"}`` or ``{"control"}``.
        """
        try:
            self._scorer_box.write_files({f"{self._response_dir}/{identity}.json": json.dumps(response)})
        except Exception as error:
            # The scorer then waits until its own timeout, which fails the version.
            logger.warning("A stage answer could not reach the scorer box: %s", error)

    def close(self) -> None:
        """Stop queued calls and close every stage box."""
        for pool in (*self._pools.values(), self._refusals):
            pool.shutdown(wait=False, cancel_futures=True)
        for box in self._boxes.values():
            with contextlib.suppress(Exception):
                box.close()


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
