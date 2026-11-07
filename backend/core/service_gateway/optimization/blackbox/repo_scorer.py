"""Score repository versions in the trusted parent's box, where the run's secrets live.

The guest that hosts the coding agent never sees the secrets or this box: it
posts each version through the parent's evaluator capability, and the parent
lays it out on a copy of the already set-up tree, then calls the user's
``score(repo_path)`` (or ``score(repo_path, case)``) on it.

A repository with a ``[run]`` entry runs the version first, while the metric
files are out of the tree (E38). The entry gets only the case's ``input``
(E42). Every process it started stops before the metric files go in; the
metric then runs in its own process as ``score(repo_path, case, output)``
with the entry's output (E41). A run that times out, raises, returns no JSON,
is killed by a signal or cannot start scores 0 with ``Run failed: ...`` as its
feedback, and the metric is not called. A nonzero exit of a command entry is
an output like any other. Neither process gets the run's secrets: only the
model route reaches the box.

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

The same relay carries the metric's requests to the run's ``[services.<name>]``
that have a url (E45). The parent checks that the calling role lists the
service, adds the service's headers with the secrets filled in, calls the
url, and returns the response unchanged. The version's ``[run]`` entry
reaches its own services the same way, checked against the ``[run]`` role.
The scorer box never holds those secrets. Catalog connectors do not go
through the relay.
"""

from __future__ import annotations

import base64
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

import httpx

from ....billing.budgets import BudgetError, BudgetInsufficientError
from ....billing.mcp_broker import _pinned_address, _PinnedTransport
from ....billing.model_dispatch import ModelHTTPResult
from ....billing.operation_pricing import UnpricedOperationError
from ....billing.runtime import UsagePendingError
from ....billing.signals import BudgetReached
from . import runner, sandbox_log
from .harness import ENV_API_KEY, ENV_MODEL
from .protocol import STATUS_KEY
from .repo_constraints import ServiceSpec, TargetStage
from .repo_services import open_stage_boxes, stage_spec
from .repo_tree import archive_chunks, drop_paths
from .repo_workspace import GIT, HIDDEN_CHANGED, REPO_DIR, WORK_DIR, RepoWorkspace, redact
from .rule_judge import RuleJudge
from .sandbox import SandboxSession
from .sandbox_contract import SandboxRuntime
from .sandbox_scorer import RUNNER_SOURCE, ScorerGateway, _raise_control

logger = logging.getLogger(__name__)

CASES_KEY = "cases"
# The guest names no real endpoint: its requests reach only the parent relay.
REPO_SCORER_URL = "https://scoped-evaluator.invalid/score"
RUNNER_PATH = f"{REPO_DIR}/app_runner.py"
_LINKED_BOX = "The version replaced a folder of the scoring box with a link, so it has no score."
CALLS_DIR = f"{REPO_DIR}/calls"
# The version's code can write next to the runner and into $HOME, so the
# runner starts without its own folder, user site-packages or PYTHON* env on
# its path: a planted json.py or .pth would run before any check (E38, E40).
# -I drops PYTHONDONTWRITEBYTECODE and PYTHONUNBUFFERED, so -B and -u keep them.
_ISOLATED = "-I -B -u"
STAGE_DIR = ".app-stage"
_STAGE_TREE = f"{STAGE_DIR}/tree"
# Unpacking the tree and applying the version in a stage box; the box's hold
# covers this on top of the scorer's own timeout.
_STAGE_LAYOUT_SECONDS = 600.0
# The guest waits for the version's checkout, its stage boxes' layout and the
# scorer together; copying a prepared tree with its installed dependencies can
# take a while, so its relay gets an allowance on top of the scorer's own
# timeout. The guest does not know whether the run has stages, so every
# repository run gets the layout time too; it only matters when the parent hangs.
SETUP_ALLOWANCE_SECONDS = 1_800.0 + _STAGE_LAYOUT_SECONDS
_STAGE_ERROR_CHARS = 500
_SERVICE_TIMEOUT_SECONDS = 120.0
# The answer is written into the scorer box, base64-encoded.
_SERVICE_BODY_BYTES = 4 * 1024 * 1024
_SERVICE_PLACEHOLDER = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")
_HTTP_METHOD = re.compile(r"[A-Z]{1,16}")
# Connection framing is the relay's own on each side; httpx decodes the body.
_HOP_HEADERS = frozenset(
    {"connection", "content-length", "host", "keep-alive", "proxy-authorization", "te", "transfer-encoding", "upgrade"}
)
_DECODED_HEADERS = frozenset({"content-encoding", "content-length", "connection", "keep-alive", "transfer-encoding"})
# The version's [run] entry and the metric run in separate processes, each
# with its own relay, so each service frame is checked against its own role (E37).
_METRIC_ROLE = "metric"
_RUN_ROLE = "run"
# The candidate supervisor's own allowance on top of the [run] timeout: it
# still has to kill what the entry left and write its output.
_RUN_STOP_SECONDS = 60.0
_RUN_FEEDBACK_CHARS = 2_000
_RUN_FAILED = "Run failed: "
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
_FRAME_FILE = re.compile(r'File "([^"]*)"')


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
        services: Mapping[str, ServiceSpec] | None = None,
        service_roles: Mapping[str, tuple[str, ...]] | None = None,
        run_entry: str | tuple[str, ...] | None = None,
        run_timeout: float = 60.0,
    ) -> None:
        """Bind the user's scorer code to a repository workspace.

        Args:
            workspace: Parent-owned box holding the pristine tree.
            code: Scorer source defining ``score(repo_path)``, ``score(repo_path, case)``
                or ``score(repo_path, case, output)``.
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
            services: The run's ``[services.<name>]``; those with a url
                go through the relay, and their secrets stay out of the box.
            service_roles: The services each role lists, by role name;
                ``run`` for the ``[run]`` entry and ``metric`` for the scorer.
            run_entry: The ``[run]`` entry: ``path/file.py:function`` or a
                command; ``None`` runs no version code before the metric.
            run_timeout: Longest one ``[run]`` call may run.

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
        self._services = {name: spec for name, spec in (services or {}).items() if spec.url is not None}
        self._service_roles = dict(service_roles or {})
        self._run_entry = run_entry
        self._run_timeout = run_timeout
        self._workspace = workspace
        self._gateway = gateway
        self._code = code
        self._timeout_seconds = timeout_seconds
        self._rule_judge = rule_judge
        self._root: str | None = None
        self._root_session: SandboxSession | None = None
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
            call = f"{CALLS_DIR}/{self._calls:06d}"
            call_dir = f"{root}/{call}"
            # Importing a protected package would otherwise write __pycache__
            # into it and fail the protected-path check. Skipping the bytecode
            # beats ignoring __pycache__, where a version could plant a .pyc
            # that the judge then loads. No run secret reaches the box (E38).
            env = {**self._model_env(), "PYTHONDONTWRITEBYTECODE": "1"}
            document: dict[str, Any] = {
                "code": self._code,
                "candidate": f"{root}/{checkout.path}",
                "case": case,
                "gateway": gateway.runner_payload() if gateway is not None else None,
            }
            if self._run_entry is not None:
                ran = self._run_candidate(session, root, checkout.path, call, case, env, secrets)
                if ran.get("error"):
                    error = _scrub(_RUN_FAILED + redact(f"{ran['error']}", secrets), root, self._hidden)
                    error = error[len(_RUN_FAILED) :]
                    return {
                        "score": 0.0,
                        "feedback": _RUN_FAILED + error[-(_RUN_FEEDBACK_CHARS - len(_RUN_FAILED)) :],
                    }
                document["output"] = ran.get("output")
                # The version's code could rewrite the runner, swap the calls
                # folder for a link, leave files in this call's folder for the
                # metric run to read, or plant a module next to the runner
                # (E38, E40). Its processes are stopped now.
                reset = session.run(
                    f"if [ -L {CALLS_DIR} ] || [ ! -d {CALLS_DIR} ]; then exit 3; fi;"
                    f" find {REPO_DIR} -mindepth 1 -maxdepth 1 ! -name work ! -name prepared ! -name calls"
                    f" -exec rm -rf {{}} +; rm -rf {shlex.quote(call)}",
                    timeout_seconds=30,
                )
                if not reset.ok:
                    self._workspace.close()
                    raise RepoScoreError(_LINKED_BOX)
                session.write_files({RUNNER_PATH: RUNNER_SOURCE})
            not_installed = self._workspace.install_metric_files()
            if not_installed:
                raise RepoScoreError(not_installed)
            with self._stage_relay(patch, f"{call}/stages") as relay:
                if relay is not None:
                    document["stages"] = relay.config(root)
                session.write_files({f"{call}/{runner.INPUT_FILE}": json.dumps(document)})
                unstamped = self._workspace.stamp_before_run()
                if unstamped:
                    raise RepoScoreError(unstamped)
                # A sink makes the session stream stderr live, which is where log() events travel.
                with sandbox_log.event_scope(source="scorer"):
                    result = session.run(
                        f"cd {shlex.quote(checkout.path)} && python3 {_ISOLATED} {shlex.quote(f'{root}/{RUNNER_PATH}')}"
                        f" {shlex.quote(call_dir)}",
                        env=env,
                        timeout_seconds=self._timeout_seconds,
                        on_output=relay.output if relay is not None else sandbox_log.ignore_output,
                    )
            if result.timed_out:
                # The runner never stopped the metric's processes; only a new box does.
                self._workspace.close()
                raise RepoScoreError(f"The scorer exceeded its {self._timeout_seconds:g}s timeout.")
            self._check_changes(relay, secrets)
            text = session.read_file(f"{call}/{runner.OUTPUT_FILE}")
        # The version's code runs next to the hidden metric and can print or
        # raise its contents, so the guest gets only a fixed message; the
        # details stay in the server log (SKY-353).
        if text is None:
            logger.warning(
                "Repository scorer did not finish (exit %s): %s",
                result.exit_code,
                redact((result.stderr or result.stdout)[-2_000:], secrets),
            )
            self._workspace.close()
            raise RepoScoreError(f"The scorer did not finish (exit {result.exit_code}).")
        output = json.loads(redact(text, secrets))
        stopped = output.get("stopped") is True
        if not stopped:
            # A process of the metric call could write into the prepared tree
            # or the next checkout; a new box is the only sure stop (E38).
            self._workspace.close()
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
        if not stopped:
            raise RepoScoreError("The version's code left processes that could not be stopped, so it has no score.")
        side_info = fold_case_scores(output.get("side_info") or {})
        return {**side_info, "score": score}

    def _run_candidate(
        self,
        session: SandboxSession,
        root: str,
        path: str,
        call: str,
        case: Any,
        env: Mapping[str, str],
        secrets: list[str],
    ) -> dict[str, Any]:
        """Run the ``[run]`` entry on the case's input while the metric files are out of the tree.

        Every earlier call's folder goes first: each holds the metric source
        and a full case. The supervisor reports whether it stopped every
        process the entry started; when it cannot, the box is destroyed, so no
        process of this version can read the next version's metric files.

        Args:
            session: The workspace's box.
            root: The box's working directory.
            path: The version's checkout, relative to the box.
            call: This call's folder, relative to the box.
            case: The case being scored; only its ``input`` reaches the entry.
            env: The model route, the box's only environment from the parent.
            secrets: Values to keep out of anything that leaves the box.

        Returns:
            ``{"output", "error"}`` from the supervisor.

        Raises:
            RepoScoreError: When the version changed a protected file, or not
                every process it started could be shown to have stopped.
        """
        supervisor = f"{call}/candidate"
        payload: dict[str, Any] = {
            "entry": self._run_entry if isinstance(self._run_entry, str) else list(self._run_entry or ()),
            "input": case.get("input") if isinstance(case, Mapping) else case,
            "timeout": self._run_timeout,
            "token": uuid.uuid4().hex,
            "stages": None,
        }
        with self._service_relay(_RUN_ROLE, f"{supervisor}/stages") as relay:
            if relay is not None:
                payload["stages"] = relay.config(root)
            session.write_files({f"{supervisor}/{runner.INPUT_FILE}": json.dumps(payload)})
            unstamped = self._workspace.stamp_before_run()
            if unstamped:
                raise RepoScoreError(unstamped)
            calls = shlex.quote(CALLS_DIR)
            with sandbox_log.event_scope(source="run"):
                result = session.run(
                    f"find {calls} -mindepth 1 -maxdepth 1 ! -name {shlex.quote(call.rsplit('/', 1)[1])}"
                    f" -exec rm -rf {{}} +; cd {shlex.quote(path)} && python3 {_ISOLATED} {shlex.quote(f'{root}/{RUNNER_PATH}')}"
                    f" {runner.CANDIDATE_FLAG} {shlex.quote(f'{root}/{supervisor}')}",
                    env=env,
                    timeout_seconds=self._run_timeout + _RUN_STOP_SECONDS,
                    on_output=relay.output if relay is not None else sandbox_log.ignore_output,
                )
        self._check_changes(relay, secrets)
        text = session.read_file(f"{supervisor}/{runner.OUTPUT_FILE}")
        try:
            ran = json.loads(text) if text is not None else None
        except ValueError:
            ran = None
        if not isinstance(ran, dict) or ran.get("stopped") is not True:
            logger.warning(
                "The [run] supervisor did not stop every process (exit %s): %s",
                result.exit_code,
                redact((result.stderr or result.stdout)[-2_000:], secrets),
            )
            # A process that outlived its supervisor could read the next
            # version's metric files; a new box is the only sure stop.
            self._workspace.close()
            raise RepoScoreError("The version's code left processes that could not be stopped, so it has no score.")
        return ran

    def _check_changes(self, relay: _StageRelay | None, secrets: list[str]) -> None:
        """Refuse the version when its code changed a protected or hidden file while it ran.

        Args:
            relay: The finished call's relay, whose answers checked the files too.
            secrets: Values to keep out of the refusal.

        Raises:
            RepoScoreError: When a file changed, or a check could not run.
        """
        judge = self._rule_judge
        unchecked = relay is not None and relay.unchecked
        # After a failed answer check the answer went in by a file write, which moves
        # every ctime, so only the checks the answers passed can still name a change.
        final = () if unchecked else self._workspace.protected_changes()
        # Each stage answer checks again, so a lasting change would repeat once per answer.
        changes = tuple(dict.fromkeys((*(relay.changes if relay is not None else ()), *final)))
        # A hidden change is not a [paths] rule the agent knows, so it
        # skips the rule judge and its "do not repeat it" message.
        tampered = [change for change in changes if change != HIDDEN_CHANGED]
        if tampered:
            # The version's code names these files and can read the secrets.
            reason = redact(" ".join(tampered), secrets)
            raise RepoScoreError(judge.blocked([reason]) if judge is not None else reason)
        if changes:
            raise RepoScoreError(HIDDEN_CHANGED)
        if unchecked:
            raise RepoScoreError("A stage answer could not be checked or delivered, so the version has no score.")

    def _model_env(self) -> dict[str, str]:
        """Return the env that points code in the box at the run's model route.

        Returns:
            ``APP_API_KEY`` and ``APP_MODEL``, or nothing without a route.
        """
        gateway = self._gateway
        if gateway is None or not gateway.api_key:
            return {}
        return {ENV_API_KEY: gateway.api_key, ENV_MODEL: gateway.model}

    @contextlib.contextmanager
    def _stage_relay(self, patch: str, response_dir: str) -> Iterator[_StageRelay | None]:
        """Open this evaluation's stage boxes, lay the version out in each, and relay calls to them.

        Every box opens before the scorer's first sample, or none stays open
        (E29, E32). Each box has its own hold, keyed to this evaluation and
        its stage (E31), and every box closes when the evaluation ends, also
        on a failure.

        Args:
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
            with self._service_relay(_METRIC_ROLE, response_dir) as relay:
                yield relay
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
        relay = self._relay(boxes, response_dir)
        try:
            kept = drop_paths(patch, self._hidden)
            try:
                with ThreadPoolExecutor(max_workers=len(boxes), thread_name_prefix="stage-layout") as pool:
                    laid_out = list(pool.map(lambda box: self._lay_out(box, kept), boxes.values()))
            except (BudgetReached, BudgetError, UsagePendingError, UnpricedOperationError):
                raise
            except Exception as error:
                logger.warning("Stage box layout failed: %s", error)
                laid_out = [False]
            if not all(laid_out):
                raise RepoScoreError("A stage box could not lay out the version, so it has no score.")
            yield relay
        finally:
            relay.close()

    @contextlib.contextmanager
    def _service_relay(self, role: str, response_dir: str) -> Iterator[_StageRelay | None]:
        """Relay one call's service requests, checked against one role, with no stage boxes.

        Args:
            role: ``run`` for the ``[run]`` entry, ``metric`` for the scorer.
            response_dir: The call's answer folder, relative to the box.

        Yields:
            The relay, or ``None`` when the role lists no service with a url.
        """
        if not any(name in self._services for name in self._service_roles.get(role, ())):
            yield None
            return
        relay = self._relay({}, response_dir, role=role)
        try:
            yield relay
        finally:
            relay.close()

    def _relay(
        self, boxes: Mapping[str, SandboxSession], response_dir: str, *, role: str = _METRIC_ROLE
    ) -> _StageRelay:
        """Build the relay for one call, checking service requests against one role.

        Args:
            boxes: Each stage's open box, by stage name.
            response_dir: The call's answer folder, relative to the box.
            role: The role whose services the caller may reach.

        Returns:
            The relay for this call.
        """
        return _StageRelay(
            boxes,
            self._stages,
            response_dir=response_dir,
            max_concurrency=self._max_concurrency,
            env=self._model_env(),
            workspace=self._workspace,
            services=self._services,
            role=role,
            allowed=self._service_roles.get(role, ()),
            secrets=self._workspace.secrets,
        )

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
            f" cd {_STAGE_TREE}; if [ -s ../version.patch ]; then {GIT} apply --binary --whitespace=nowarn ../version.patch; fi",
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
        document = _for_proposer(document, self._root, self._hidden)
        notes = self._rule_judge.take_notes() if self._rule_judge is not None else []
        if notes:
            feedback = document.get("feedback")
            document["feedback"] = "\n\n".join([*notes, *([str(feedback)] if feedback else [])])
        return ModelHTTPResult(200, "application/json", json.dumps(document).encode())

    def _box_root(self, session: SandboxSession) -> str:
        """Return the box's working directory, and write the runner into the box.

        Each checkout clears the runner with everything else next to the
        tree, and a failed version can end the box, so it goes in every time.

        Args:
            session: The workspace's open box.

        Returns:
            The absolute directory relative paths in the box resolve against.

        Raises:
            RepoScoreError: When the box cannot report its directory.
        """
        if self._root is None or self._root_session is not session:
            located = session.run("pwd", timeout_seconds=30)
            root = located.stdout.strip()
            if not located.ok or not root.startswith("/"):
                raise RepoScoreError("The scoring box could not report its working directory.")
            self._root, self._root_session = root, session
        session.write_files({RUNNER_PATH: RUNNER_SOURCE})
        return self._root

    def close(self) -> None:
        """Destroy the workspace box."""
        self._workspace.close()


class _StageRelay:
    """Answer one scorer call's ``call(name, input)`` and service frames.

    Calls to one stage run in parallel, each in its own process and folder,
    up to ``max_concurrency`` at a time (E34). Service requests run in their
    own pool with the same limit.
    """

    def __init__(
        self,
        boxes: Mapping[str, SandboxSession],
        stages: Mapping[str, TargetStage],
        *,
        response_dir: str,
        max_concurrency: int,
        env: Mapping[str, str],
        workspace: RepoWorkspace,
        services: Mapping[str, ServiceSpec] | None = None,
        role: str = _METRIC_ROLE,
        allowed: tuple[str, ...] = (),
        secrets: Mapping[str, str] | None = None,
    ) -> None:
        """Bind the open stage boxes to the scorer box that waits for their answers.

        Args:
            boxes: Each stage's open box, by stage name.
            stages: Each stage's settings, by stage name.
            response_dir: The call's answer folder, relative to the scorer box.
            max_concurrency: How many calls to one stage run at the same time.
            env: The run's model route, so stage code reaches models only
                through the parent's mailbox.
            workspace: The scorer box's workspace, which checks the
                protected files and writes each answer into the scorer box.
            services: The run's services with a url, by name.
            role: The role that each service request is checked against:
                ``run`` for the ``[run]`` entry, ``metric`` for the scorer (E37).
            allowed: The services that ``role`` lists.
            secrets: The run's secret values, filled into service headers
                here in the parent.
        """
        self._boxes = dict(boxes)
        self._stages = stages
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
        self._services = {name: spec for name, spec in (services or {}).items() if spec.url is not None}
        self._catalog = {name for name, spec in (services or {}).items() if spec.url is None}
        self._role = role
        self._allowed = frozenset(allowed)
        self._secrets = dict(secrets or {})
        self._service_pool = ThreadPoolExecutor(max_workers=max_concurrency, thread_name_prefix="stage-service")
        self._pending = ""
        self._seen: set[str] = set()
        self._lock = threading.Lock()
        self._workspace = workspace
        self._answer_lock = threading.Lock()
        self._closed = False
        self.changes: list[str] = []
        # A failed check is not tampering, so it never reaches the rule judge.
        self.unchecked = False

    def config(self, root: str) -> dict[str, Any]:
        """Describe the relay to the runner in the scorer box.

        Args:
            root: The scorer box's working directory.

        Returns:
            ``{"prefix", "response_dir", "names", "services"}`` for the
            call's input; ``services`` lists what the role may call.
        """
        return {
            "prefix": self._prefix,
            "response_dir": f"{root}/{self._response_dir}",
            "names": list(self._boxes),
            "services": [name for name in self._services if name in self._allowed],
        }

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
                # Another thread of the scorer may have left a partial line just before the frame.
                start = line.find(self._prefix)
                if start < 0:
                    sandbox_log.ignore_output(stream, line + "\n")
                    continue
                if start:
                    sandbox_log.ignore_output(stream, line[:start] + "\n")
                try:
                    frame = json.loads(line[start + len(self._prefix) :])
                    identity = frame["id"]
                    name = frame["service"] if "service" in frame else frame["stage"]
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
                if "service" in frame:
                    refusal = self._refusal(name)
                    if refusal is None:
                        self._service_pool.submit(self._service, name, identity, frame)
                    else:
                        self._refusals.submit(self._answer, identity, refusal)
                    continue
                pool = self._pools.get(name) if isinstance(name, str) else None
                if pool is None:
                    self._refusals.submit(self._answer, identity, {"error": f"Unknown stage {name!r}."})
                else:
                    pool.submit(self._call, name, identity, frame.get("input"))

    def _refusal(self, name: Any) -> dict[str, Any] | None:
        """Check that the calling role may reach a service through the relay.

        Args:
            name: The service the frame names.

        Returns:
            The refusal answer, or ``None`` when the request may go out.
        """
        if not isinstance(name, str) or (name not in self._services and name not in self._catalog):
            return {"status": 404, "error": f"Unknown service {name!r}."}
        if name not in self._allowed:
            return {"status": 403, "error": f"Service {name!r} is not listed for the [{self._role}] role."}
        if name in self._catalog:
            return {
                "status": 403,
                "error": f"Service {name!r} has no url; only services with a url go through the relay.",
            }
        return None

    def _service(self, name: str, identity: str, frame: Mapping[str, Any]) -> None:
        """Send one service request and write the response for the scorer.

        Args:
            name: The service.
            identity: The request's id, also its answer name.
            frame: The guest's ``path``, ``method``, ``headers`` and ``body``.
        """
        try:
            response = self._request(self._services[name], name, frame)
        except Exception as error:
            logger.warning("Service %s request failed in the parent: %s", name, error)
            response = {"status": 502, "error": f"Service {name!r} could not be reached."}
        self._answer(identity, response)

    def _request(self, spec: ServiceSpec, name: str, frame: Mapping[str, Any]) -> dict[str, Any]:
        """Call a service's url with its headers and the run's secrets filled in.

        The host must resolve to public addresses only, and the connection
        stays on that address and origin; redirects come back to the guest
        unchanged.

        Args:
            spec: The service's declaration.
            name: The service.
            frame: The guest's ``path``, ``method``, ``headers`` and ``body``.

        Returns:
            ``{"status", "headers", "body"}`` with a base64 body, or
            ``{"status", "error"}`` when the request cannot go out.

        Raises:
            ValueError: When the frame is malformed or the host resolves to a
                private, loopback or link-local address.
            httpx.HTTPError: When the service cannot be reached.
        """
        method, path = frame.get("method"), frame.get("path", "")
        if not isinstance(method, str) or not _HTTP_METHOD.fullmatch(method) or not isinstance(path, str):
            return {"status": 400, "error": f"Service {name!r} got a malformed request."}
        url = str(spec.url).rstrip("/") + "/" + path.lstrip("/")
        origin, target = httpx.URL(str(spec.url)), httpx.URL(url)
        if (target.scheme, target.host, target.port) != (origin.scheme, origin.host, origin.port):
            return {"status": 400, "error": f"Service {name!r} got a path outside its url."}
        overrides = {key.lower(): value for key, value in spec.request_headers.items()}
        headers = [
            (pair[0], pair[1])
            for pair in frame.get("headers") or ()
            if isinstance(pair, list | tuple)
            and len(pair) == 2
            and all(isinstance(part, str) for part in pair)
            and pair[0].lower() not in _HOP_HEADERS
            and pair[0].lower() not in overrides
        ]
        for key, template in spec.request_headers.items():
            missing = [secret for secret in _SERVICE_PLACEHOLDER.findall(template) if secret not in self._secrets]
            if missing:
                return {
                    "status": 502,
                    "error": f"Service {name!r} needs secret {missing[0]}, which the run does not have.",
                }
            headers.append((key, _SERVICE_PLACEHOLDER.sub(lambda match: self._secrets[match.group(1)], template)))
        body = base64.b64decode(frame.get("body") or "", validate=True)
        transport = _PinnedTransport(
            str(spec.url), _pinned_address(str(spec.url), False, label=f"service {name!r} url")
        )
        try:
            with (
                httpx.Client(
                    transport=transport, follow_redirects=False, trust_env=False, timeout=_SERVICE_TIMEOUT_SECONDS
                ) as client,
                client.stream(method, target, headers=headers, content=body) as response,
            ):
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > _SERVICE_BODY_BYTES:
                        return {"status": 502, "error": f"Service {name!r} answered with more than 4 MiB."}
                return {
                    "status": response.status_code,
                    "headers": [
                        [key, value]
                        for key, value in response.headers.multi_items()
                        if key.lower() not in _DECODED_HEADERS
                    ],
                    "body": base64.b64encode(bytes(content)).decode("ascii"),
                }
        finally:
            transport.close()

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
            response: ``{"output"}``, ``{"error"}``, ``{"control"}``, or a
                service's ``{"status", ...}``.
        """
        with self._answer_lock:
            if self._closed:
                return
            changes = self._workspace.answer(f"{self._response_dir}/{identity}.json", json.dumps(response))
            if changes is None:
                # The scorer may wait until its own timeout; either way the version gets no score.
                self.unchecked = True
            else:
                self.changes.extend(changes)

    def close(self) -> None:
        """Stop queued calls, keep late answers out of the scorer box, and close every stage box."""
        with self._answer_lock:
            self._closed = True
        for pool in (*self._pools.values(), self._refusals, self._service_pool):
            pool.shutdown(wait=False, cancel_futures=True)
        for box in self._boxes.values():
            with contextlib.suppress(Exception):
                box.close()


def _scrub(text: str, root: str | None, hidden: tuple[str, ...] | list[str] = ()) -> str:
    """Remove traceback frames and the scorer box's absolute paths from text the proposer reads.

    "Run failed" feedback keeps the frames in the version's own files, so the
    proposer can find the line that broke (E41); every other frame goes.

    Args:
        text: An error or a feedback string from the scorer box.
        root: The box's working directory; ``None`` before it is known.
        hidden: The metric files and exploit folder; their frames always go.

    Returns:
        The text with repository-relative paths and without the metric's frames.
    """
    text = text if text.endswith("\n") else f"{text}\n"
    if text.startswith(_RUN_FAILED):
        text = _TRACEBACK.sub(lambda match: _repo_frame(match.group(0), root, hidden), text)
    else:
        text = _TRACEBACK.sub("", text)
    text = text.rstrip("\n")
    if root:
        text = (
            text.replace(f"{root}/{WORK_DIR}/", "")
            .replace(f"{root}/{WORK_DIR}", ".")
            .replace(f"{root}/", "")
            .replace(root, "")
        )
    return text


def _repo_frame(block: str, root: str | None, hidden: tuple[str, ...] | list[str]) -> str:
    """Return one traceback frame if it is in a version file, else nothing.

    Args:
        block: A ``_TRACEBACK`` match: the header line, or one frame with its source lines.
        root: The box's working directory; ``None`` before it is known.
        hidden: The metric files and exploit folder.

    Returns:
        The block unchanged when it is the header or a frame in a visible
        repository file, else ``""``.
    """
    found = _FRAME_FILE.search(block)
    if found is None:
        return block
    path = found.group(1)
    work = f"{root}/{WORK_DIR}/" if root else None
    if work and path.startswith(work):
        path = path[len(work) :]
    # Frames in the runner, the standard library or packages say nothing
    # about the version and name box paths; "<frozen ...>" frames are noise.
    elif path.startswith(("/", "<")):
        return ""
    if any(path == entry.rstrip("/") or path.startswith(f"{entry.rstrip('/')}/") for entry in hidden):
        return ""
    return block


def _for_proposer(
    document: Mapping[str, Any], root: str | None, hidden: tuple[str, ...] | list[str] = ()
) -> dict[str, Any]:
    """Keep only the score, the feedback and the named scores of a scoring result.

    Args:
        document: What :meth:`RepoScorer.score` returned.
        root: The box's working directory, scrubbed from the feedback.
        hidden: The metric files and exploit folder, whose frames never reach the proposer.

    Returns:
        The reply the guest receives.
    """
    reply = {key: document[key] for key in _PROPOSER_KEYS if key in document}
    if isinstance(reply.get("feedback"), str):
        reply["feedback"] = _scrub(reply["feedback"], root, hidden)
    if isinstance(reply.get("scores"), Mapping):
        reply["scores"] = {
            name: _named_score(entry, root, hidden) if isinstance(entry, Mapping) else entry
            for name, entry in reply["scores"].items()
        }
    return reply


def _named_score(
    entry: Mapping[str, Any], root: str | None, hidden: tuple[str, ...] | list[str] = ()
) -> dict[str, Any]:
    """Keep only the score and the scrubbed feedback of one named score.

    Args:
        entry: One value of the result's ``scores`` mapping.
        root: The box's working directory, scrubbed from the feedback.
        hidden: The metric files and exploit folder.

    Returns:
        The named score the guest receives.
    """
    kept = {key: entry[key] for key in ("score", "feedback") if key in entry}
    if isinstance(kept.get("feedback"), str):
        kept["feedback"] = _scrub(kept["feedback"], root, hidden)
    return kept
