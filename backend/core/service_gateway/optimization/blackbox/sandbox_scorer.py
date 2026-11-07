"""Python scorers behind the sandbox seam: one box per job, one runner call per evaluation.

User scorer code never runs on the worker. :class:`SandboxPythonScorer`
opens a sandbox lazily on the first evaluation, ships :mod:`.runner` and the
scorer source into it once, and turns every ``(candidate, case)`` into a
``calls/NNNNNN/`` directory the runner scores from ``input.json`` into
``output.json``. The per-call inputs never repeat the scorer source. The
scorer's ``llm()`` calls leave the box for the gateway
:func:`scorer_gateway` resolves; their token usage comes back with each
result and lands in a :class:`ScorerUsage` ledger the run's cost ceiling
and billing read like any other language model.
"""

from __future__ import annotations

import json
import logging
import shlex
import sys
import threading
import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from gepa.image import Image

from ....billing.budgets import BudgetError, BudgetInsufficientError
from ....billing.model_gateway import ROUTE_KEY
from ....billing.operation_pricing import UnpricedOperationError
from ....billing.runtime import UsagePendingError
from ....billing.signals import BudgetReached
from ....config import Settings, app_env, settings
from ....exceptions import ServiceError
from ....models.common import ModelConfig
from ...language_models import usage_by_model_from_history
from . import check_terminal, package_setup, runner, sandbox_log
from .agent_eval import gateway_from_settings
from .harness import ENV_API_KEY
from .heartbeat import heartbeat
from .protocol import Candidate, SideInfo
from .sandbox import (
    JOB_TAG,
    SandboxRuntime,
    SandboxSession,
    SandboxSpec,
    scorer_runtime_from_settings,
    unique_sandbox_name,
)

logger = logging.getLogger(__name__)

RUNNER_FILE = "app_runner.py"
RUNNER_SOURCE = Path(runner.__file__).read_text(encoding="utf-8")
CALLS_DIR = "calls"
_DEFAULT_PROBE_TIMEOUT_SECONDS = 45.0
# A probe's box only has to outlive one call plus interpreter start-up.
_PROBE_LIFETIME_ALLOWANCE_SECONDS = 120.0
# apt plus a few wheels finish in about a minute; a stalled mirror must not eat the box's lifetime.
_INSTALL_TIMEOUT_SECONDS = 600.0
DEPENDENCIES_DIR = ".app-dependencies"
_DEPENDENCIES_TIMEOUT_SECONDS = 300.0
_STDERR_TAIL_CHARS = 2_000
_OPENROUTER_PREFIX = "openrouter/"
_OPENROUTER_URL = "https://openrouter.ai/api/v1"
_DATA_IMAGE_PREFIX = "data:image/"
_USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")
_GATEWAY_MISSING = (
    "Scorer models need a gateway the sandboxes can reach: set LITELLM_PROXY_URL and "
    "LITELLM_PROXY_API_KEY, or BLACKBOX_AGENT_GATEWAY_URL and BLACKBOX_AGENT_GATEWAY_API_KEY."
)


@dataclass(frozen=True)
class ScorerGateway:
    """Where a sandboxed scorer's ``llm()`` calls go, and how the run bills them."""

    url: str
    model: str
    api_key: str | None
    billing_model: str
    temperature: float | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    timeout_seconds: float = 120.0
    protected: bool = False
    subscription: bool = False
    gateway_reasoning: bool = False

    def runner_payload(self) -> dict[str, Any]:
        """Return the ``gateway`` section of a runner call — the key travels separately."""
        payload = {
            "url": self.url,
            "model": self.model,
            "declared_model": self.billing_model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "reasoning_effort": self.reasoning_effort,
            "timeout_seconds": self.timeout_seconds,
        }
        if self.protected:
            payload["protected"] = True
        if self.subscription:
            payload["subscription"] = True
        if self.gateway_reasoning:
            payload["gateway_reasoning"] = True
        return payload

    def injected_headers(self) -> dict[str, dict[str, str]]:
        """Return the headers a network edge adds to the box's requests to the gateway host, keyed by host."""
        host = urlsplit(self.url).hostname
        if not host or not self.api_key:
            return {}
        return {host: {"Authorization": f"Bearer {self.api_key}"}}


def _without_provider(name: str) -> str:
    """Drop the LiteLLM provider prefix a custom endpoint never sees.

    Args:
        name: A ``provider/model`` identifier.

    Returns:
        The identifier the endpoint is sent.
    """
    return name.split("/", 1)[1] if "/" in name else name


def scorer_gateway(config: ModelConfig, settings: Settings) -> ScorerGateway:
    """Resolve the gateway for the model chosen in the Scorer step.

    BYOK configs carry the user's key (the run-path bridge stamps it onto
    ``extra``) and go straight to their provider: the configured ``base_url``,
    else OpenRouter for ``openrouter/`` models. Managed configs go through the
    gateway sandboxes are pointed at — the agent gateway, else the LiteLLM
    proxy — which resolves OpenRouter models without their prefix.

    Args:
        config: The scorer's model configuration.
        settings: The backend settings.

    Returns:
        The gateway the runner posts chat completions to.

    Raises:
        ServiceError: When no reachable gateway can be resolved.
    """
    name = config.name
    effort = config.extra.get("reasoning_effort")
    common: dict[str, Any] = {
        "billing_model": name,
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
        "reasoning_effort": effort if isinstance(effort, str) and effort else None,
        "timeout_seconds": settings.lm_request_timeout_seconds,
        "subscription": config.token_source == "subscription",
    }
    route = config.extra.get(ROUTE_KEY)
    if isinstance(route, dict):
        return ScorerGateway(
            url=route["url"],
            model=route["model"],
            api_key=route["token"],
            protected=True,
            gateway_reasoning=config.token_source != "subscription",
            **common,
        )
    own_key = config.extra.get("api_key")
    if isinstance(own_key, str) and own_key:
        base_url = config.base_url or config.extra.get("api_base")
        if base_url:
            return ScorerGateway(
                url=str(base_url).rstrip("/"), model=_without_provider(name), api_key=own_key, **common
            )
        if name.startswith(_OPENROUTER_PREFIX):
            return ScorerGateway(url=_OPENROUTER_URL, model=name[len(_OPENROUTER_PREFIX) :], api_key=own_key, **common)
        raise ServiceError(
            f"Sandboxed scorers reach a BYOK model through OpenRouter or a base_url; '{name}' has neither."
        )
    gateway = gateway_from_settings(settings)
    if gateway is None:
        raise ServiceError(_GATEWAY_MISSING)
    return ScorerGateway(
        url=gateway.url,
        model=name.removeprefix(_OPENROUTER_PREFIX),
        api_key=gateway.api_key,
        gateway_reasoning=not bool(getattr(settings, "blackbox_agent_gateway_url", None)),
        **common,
    )


class ScorerUsage:
    """Token ledger for one scorer's ``llm()`` calls, shaped like a ``dspy.LM`` for the usage helpers.

    ``model`` and ``history`` are all that ``usage_by_model_from_history``,
    ``lm_call_count`` and the cost ceiling read off a language model.
    """

    def __init__(self, model: str) -> None:
        """Start an empty ledger.

        Args:
            model: The model the pricing table bills these calls as.
        """
        self.model = model
        self.history: list[dict[str, Any]] = []

    def record(self, entries: Iterable[Mapping[str, Any]]) -> None:
        """Append one history entry per ``llm()`` call the runner reported.

        Args:
            entries: Usage dicts with ``prompt_tokens`` / ``completion_tokens`` / ``total_tokens``.
        """
        for entry in entries:
            self.history.append({"usage": {key: int(entry.get(key) or 0) for key in _USAGE_KEYS}})

    def by_model(self) -> dict[str, tuple[int, int]]:
        """Return ``model → (input_tokens, output_tokens)`` for everything recorded."""
        return dict(usage_by_model_from_history(self) or {})


@dataclass(frozen=True)
class ScorerProbeResult:
    """Outcome of scoring one candidate with python scorer code in a sandbox.

    ``error`` is set when the scorer itself raised or returned an unusable
    value; ``score`` and ``side_info`` are then empty. ``usage_by_model``
    is what the scorer's ``llm()`` calls consumed, for billing.
    """

    score: float | None
    side_info: dict[str, Any]
    error: str | None
    usage_by_model: dict[str, tuple[int, int]] = field(default_factory=dict)


def revive_images(side_info: Mapping[str, Any]) -> SideInfo:
    """Turn the data URLs the runner serialized images into back into ``Image`` values.

    Args:
        side_info: JSON-plain side information from a runner call.

    Returns:
        The same mapping with image strings wrapped for the reflection model.
    """
    return {
        key: Image(url=value) if isinstance(value, str) and value.startswith(_DATA_IMAGE_PREFIX) else value
        for key, value in side_info.items()
    }


def _tail(text: str) -> str:
    """Return the end of a command's output, enough to show why it died.

    Args:
        text: Captured stdout or stderr.

    Returns:
        The last ``_STDERR_TAIL_CHARS`` characters, stripped.
    """
    return text[-_STDERR_TAIL_CHARS:].strip()


def _sandbox_name(job_id: str | None) -> str | None:
    """Return a fresh sandbox name that carries the job id.

    Args:
        job_id: The job the scorer belongs to, if any.

    Returns:
        The name, or ``None`` for anonymous boxes.
    """
    if not job_id:
        return None
    return unique_sandbox_name(f"app-scorer-{job_id}")


def _raise_control(result: Mapping[str, Any]) -> None:
    """Propagate a protected runner stop before interpreting scorer output.

    Args:
        result: Decoded runner response.

    Raises:
        BudgetReached: When the parent's run stop latch is set.
        BudgetError: When the next operation has insufficient coverage.
        UsagePendingError: When an attempted operation needs reconciliation.
        UnpricedOperationError: When the parent cannot bound the operation.
    """
    control = result.get("control")
    if not isinstance(control, dict):
        return
    code, message = control.get("code"), str(control.get("message") or "Protected scorer stopped.")
    if code == "budget_reached":
        raise BudgetReached(message)
    if code == "budget_insufficient":
        raise BudgetInsufficientError(message)
    if code == "unpriced_operation":
        raise UnpricedOperationError(message)
    raise UsagePendingError(message)


class SandboxPythonScorer:
    """A python scorer that runs inside a sandbox: one box per job, one runner call per evaluation.

    Calls share the box and run in parallel, each in its own call folder, so
    the engines' ``max_concurrency`` bounds them. Only opening the box and
    installing packages are serialized. ``usage`` is the ledger the run
    bills, or ``None`` when the scorer has no model.
    """

    def __init__(
        self,
        code: str,
        *,
        runtime: SandboxRuntime,
        gateway: ScorerGateway | None,
        timeout_seconds: float,
        lifetime_seconds: float | None = None,
        job_id: str | None = None,
        install_command: str | None = None,
        dependency_lock: Mapping[str, Any] | None = None,
        dependency_route: Mapping[str, str] | None = None,
    ) -> None:
        """Bind scorer code to a runtime without opening anything yet.

        Args:
            code: User-authored scorer source.
            runtime: Where the box is opened.
            gateway: Where the scorer's ``llm()`` calls go; ``None`` when no model was chosen.
            timeout_seconds: Longest a single call may run.
            lifetime_seconds: Requested box lifetime; the configured ceiling when unset, and never above it.
            job_id: Names and tags the box after the job, when known.
            install_command: Shell command run once in every fresh box before the first call.
            dependency_lock: Exact wheel set authenticated by the trusted parent.
            dependency_route: Scoped capability for retrieving the locked artifacts.
        """
        self._code = code
        self._install_command = (install_command or "").strip() or None
        self._dependency_lock = dict(dependency_lock) if dependency_lock is not None else None
        # Import roots already accounted for: the lock's, plus those of every
        # candidate scanned since, whether or not their packages installed.
        self._covered: set[str] = set(self._dependency_lock["imports"]) if self._dependency_lock is not None else set()
        self._dependency_route = (
            dict(dependency_route)
            if dependency_route is not None
            else {
                "url": app_env("APP_BUDGET_RELAY_URL", ""),
                "token": app_env("APP_PACKAGE_RELAY_TOKEN", ""),
            }
        )
        self._runtime = runtime
        self._protected = bool(getattr(runtime, "protected", False) or (gateway and gateway.protected))
        if getattr(runtime, "protected", False) and gateway is not None and not gateway.protected:
            raise ServiceError("Protected scorers require an opaque model route from the trusted parent.")
        self._gateway = gateway
        self._timeout_seconds = timeout_seconds
        ceiling = settings.vercel_sandbox_max_lifetime_seconds
        # The network edge adds the key to the gateway calls when the runtime has one, so the
        # box never holds it; otherwise it rides in the runner's environment, one call at a time.
        injected = gateway.injected_headers() if gateway is not None and runtime.injects_headers else {}
        self._env_key = gateway.api_key if gateway is not None and not injected else None
        self._job_id = job_id
        self._spec = SandboxSpec(
            lifetime_seconds=ceiling if lifetime_seconds is None else min(lifetime_seconds, ceiling),
            tags={JOB_TAG: job_id} if job_id else {},
            inject_headers=injected,
            network_disabled=self._protected,
            operation_key=f"scorer:{uuid.uuid4().hex}" if self._protected else None,
            private=True,
        )
        self._session: SandboxSession | None = None
        self._calls = 0
        # ``_lock`` guards the box and the dependency lock only; the calls
        # themselves run outside it, and the counter only needs uniqueness.
        self._lock = threading.Lock()
        self._calls_lock = threading.Lock()
        self.usage: ScorerUsage | None = ScorerUsage(gateway.billing_model) if gateway is not None else None

    def __call__(self, candidate: Candidate, case: Any = None) -> tuple[float, SideInfo]:
        """Score ``candidate`` on ``case`` for the engines.

        Args:
            candidate: The version to score.
            case: The case to score it on, if the task has cases.

        Returns:
            The score and side information, images revived for the reflection model.

        Raises:
            ServiceError: When the scorer raised, timed out, returned no usable
                value, or the box could not run it.
        """
        probe = self.run(candidate, case)
        if probe.error is not None or probe.score is None:
            raise ServiceError(probe.error or "scorer returned no score.")
        return probe.score, revive_images(probe.side_info)

    def run(self, candidate: Candidate, case: Any = None) -> ScorerProbeResult:
        """Score ``candidate`` on ``case`` and report the scorer's own failure as data.

        Args:
            candidate: The version to score.
            case: The case to score it on, if any.

        Returns:
            Score and JSON-plain side information, or the scorer's error, plus
            what its ``llm()`` calls consumed.

        Raises:
            ServiceError: When the box could not run the scorer at all.
        """
        payload = {
            "candidate": candidate,
            "case": case,
            "gateway": self._gateway.runner_payload() if self._gateway is not None else None,
        }
        with self._lock:
            blocked = self._extend(candidate)
            if blocked is not None:
                return ScorerProbeResult(score=None, side_info={}, error=blocked)
            session = self._open()
        try:
            result = self._invoke(payload)
        except (ServiceError, BudgetError, UsagePendingError):
            raise
        except Exception:
            if self._protected:
                raise
            # The box died under us (lifetime reached, host recycled): one fresh box, one more try.
            logger.warning("scorer sandbox failed; reopening", exc_info=True)
            with self._lock:
                # A parallel call may have reopened it already; that new box stays.
                if self._session is session:
                    self._discard()
            result = self._invoke(payload)
        _raise_control(result)
        entries = list(result.get("usage") or [])
        if self.usage is not None:
            self.usage.record(entries)
        call_usage = ScorerUsage(self._gateway.billing_model) if self._gateway is not None else None
        if call_usage is not None:
            call_usage.record(entries)
        raw_score = result.get("score")
        return ScorerProbeResult(
            score=float(raw_score) if raw_score is not None else None,
            side_info=dict(result.get("side_info") or {}),
            error=result.get("error"),
            usage_by_model=call_usage.by_model() if call_usage is not None else {},
        )

    def check_ready(self) -> dict[str, Any]:
        """Load the scorer in its real sandbox without evaluating an invented candidate.

        Returns:
            The loaded entrypoint and its input/model configuration evidence.

        Raises:
            ServiceError: When imports or scorer entrypoint loading fail.
            BudgetError: When sandbox/model admission has insufficient coverage.
            UsagePendingError: When attempted usage needs reconciliation.
        """
        payload = {
            "mode": "readiness",
            "gateway": self._gateway.runner_payload() if self._gateway is not None else None,
        }
        result = self._invoke(payload)
        _raise_control(result)
        readiness = result.get("readiness")
        if result.get("error") or not isinstance(readiness, dict) or not readiness.get("ready"):
            raise ServiceError(str(result.get("error") or "Scorer readiness could not be verified."))
        return readiness

    def close(self) -> None:
        """Destroy the box and propagate any unresolved protected usage."""
        with self._lock:
            self._discard()

    def _invoke(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Run one runner call in the open (or newly opened) box.

        Args:
            payload: The runner's ``input.json`` content.

        Returns:
            The runner's ``output.json`` content, or a timeout error in its shape.

        Raises:
            ServiceError: When the runner produced no output.
        """
        with self._lock:
            session = self._open()
        with self._calls_lock:
            self._calls += 1
            number = self._calls
        call_dir = f"{CALLS_DIR}/{number:06d}"
        started = time.perf_counter()
        session.write_files(
            {f"{call_dir}/{runner.INPUT_FILE}": json.dumps(payload, default=runner.side_info_json_default)}
        )
        # A sink makes the session stream stderr live, which is where log() events travel.
        with sandbox_log.event_scope(source="scorer"):
            result = session.run(
                f"python3 {RUNNER_FILE} {shlex.quote(call_dir)}",
                env={
                    **({ENV_API_KEY: self._env_key} if self._env_key else {}),
                    **({"PYTHONPATH": f"{DEPENDENCIES_DIR}/site"} if self._dependency_lock is not None else {}),
                    # The protected image has no display or GPU; PyOpenGL-based renderers
                    # (pyrender and friends) would otherwise try to open an X display.
                    **({"PYOPENGL_PLATFORM": "osmesa"} if self._protected else {}),
                }
                or None,
                timeout_seconds=self._timeout_seconds,
                on_output=sandbox_log.ignore_output,
            )
        logger.debug(
            "scorer call %d finished in %.1fs (exit %s)", number, time.perf_counter() - started, result.exit_code
        )
        if result.timed_out:
            return {
                "score": None,
                "side_info": {},
                "error": f"scorer exceeded the {self._timeout_seconds:g}s timeout",
                "usage": [],
            }
        text = session.read_file(f"{call_dir}/{runner.OUTPUT_FILE}")
        if text is None:
            raise ServiceError(
                f"scorer sandbox failed (exit {result.exit_code}): {_tail(result.stderr or result.stdout)}"
            )
        return json.loads(text)

    def _open(self) -> SandboxSession:
        """Return the job's box, opening it and installing the runner on first use."""
        if self._session is None:
            # Named per open: a box discarded after a failure may still hold its
            # name while Vercel tears it down, so the replacement never reuses it.
            name = _sandbox_name(self._job_id)
            logger.info("scorer sandbox: opening %s (lifetime %.0fs)", name or "a box", self._spec.lifetime_seconds)
            opened = time.perf_counter()
            check_terminal.note("box_opening")
            session = self._runtime.open(replace(self._spec, name=name))
            logger.info("scorer sandbox: ready in %.1fs", time.perf_counter() - opened)
            check_terminal.note("box_ready", seconds=round(time.perf_counter() - opened, 1))
            try:
                session.write_files({RUNNER_FILE: RUNNER_SOURCE, runner.CODE_FILE: self._code})
                self._install(session)
            except BaseException:
                session.close()
                raise
            self._session = session
        return self._session

    def _install(self, session: SandboxSession) -> None:
        """Run the scorer's install command in a box that just opened.

        Args:
            session: The fresh box.

        Raises:
            ServiceError: When the command fails or outruns its allowance.
        """
        if self._dependency_lock is not None:
            if not self._dependency_route.get("token"):
                raise ServiceError("Pinned dependencies require the protected package relay.")
            session.write_files({f"{DEPENDENCIES_DIR}/setup.py": Path(package_setup.__file__).read_text()})
            self._setup(session, "install", {"lock": self._dependency_lock})
        if self._install_command is None:
            return
        logger.info("scorer sandbox: install running (allowance %.0fs)", _INSTALL_TIMEOUT_SECONDS)
        started = time.perf_counter()
        with heartbeat(logger, "scorer sandbox", "install", _INSTALL_TIMEOUT_SECONDS):
            result = session.run(self._install_command, timeout_seconds=_INSTALL_TIMEOUT_SECONDS)
        if result.timed_out:
            raise ServiceError(f"scorer install command exceeded the {_INSTALL_TIMEOUT_SECONDS:g}s allowance")
        if not result.ok:
            raise ServiceError(
                f"scorer install command failed (exit {result.exit_code}): {_tail(result.stderr or result.stdout)}"
            )
        logger.info("scorer sandbox: install done in %.1fs", time.perf_counter() - started)

    def _setup(self, session: SandboxSession, action: str, request: Mapping[str, Any]) -> dict[str, Any]:
        """Run one dependency operation in the box through the parent's package relay.

        Args:
            session: The open box.
            action: ``install`` or ``extend``.
            request: The operation's inputs beyond the action and relay route.

        Returns:
            The operation's result document.

        Raises:
            ServiceError: When the operation failed or produced no result.
        """
        session.write_files(
            {
                f"{DEPENDENCIES_DIR}/request.json": json.dumps(
                    {"action": action, "route": self._dependency_route, **request}
                )
            }
        )
        result = session.run(
            f"python3 {DEPENDENCIES_DIR}/setup.py {DEPENDENCIES_DIR}/request.json",
            timeout_seconds=_DEPENDENCIES_TIMEOUT_SECONDS,
        )
        raw = session.read_file(f"{DEPENDENCIES_DIR}/result.json")
        outcome = json.loads(raw) if raw else {}
        _raise_control(outcome)
        if not result.ok or not outcome.get("ok"):
            raise ServiceError(
                outcome.get("error")
                or _tail(result.stderr or result.stdout)
                or "Pinned package installation did not complete."
            )
        return dict(outcome.get("result") or {})

    def _extend(self, candidate: Candidate) -> str | None:
        """Install the packages ``candidate`` imports that no earlier scan covered.

        The optimizer writes candidates the signed lock never saw, so each new
        import root costs one setup run in the box before the candidate scores.
        A failed install is the candidate's problem, reported as scorer feedback
        rather than ending the job; budget stops still propagate.

        Args:
            candidate: The version about to be scored.

        Returns:
            Why the candidate cannot run, or ``None`` when its imports are satisfied.
        """
        if self._dependency_lock is None:
            return None
        roots = {
            root
            for root in package_setup._candidate_roots(candidate)
            if root not in sys.stdlib_module_names and root != runner.HELPER_MODULE_NAME
        } - self._covered
        if not roots:
            return None
        self._covered |= roots
        logger.info("scorer sandbox: candidate imports %s; extending dependencies", ", ".join(sorted(roots)))
        session = self._open()
        try:
            added = self._setup(session, "extend", {"lock": self._dependency_lock, "candidate": candidate})
        except ServiceError as error:
            return f"candidate packages could not be installed: {error}"
        self._dependency_lock = {
            **self._dependency_lock,
            "imports": sorted(set(self._dependency_lock["imports"]) | set(added.get("imports") or [])),
            "artifacts": [*self._dependency_lock["artifacts"], *(added.get("artifacts") or [])],
        }
        return None

    def _discard(self) -> None:
        """Close the box, if any, so the next call opens a fresh one."""
        if self._session is not None:
            session = self._session
            self._session = None
            session.close()
            logger.info("scorer sandbox: closed after %d call(s)", self._calls)


def probe_scorer(
    *,
    scorer_code: str,
    candidate: Any,
    case: Any = None,
    scorer_model: dict[str, Any] | None = None,
    timeout_seconds: float = _DEFAULT_PROBE_TIMEOUT_SECONDS,
    runtime: SandboxRuntime | None = None,
    install_command: str | None = None,
) -> ScorerProbeResult:
    """Score one candidate with python scorer code in a throwaway sandbox.

    A scorer that raised on the candidate is reported via
    ``ScorerProbeResult.error`` — not as an exception from this function.
    Infrastructure, budget, and pending-usage signals propagate to setup.

    Args:
        scorer_code: User-authored scorer source.
        candidate: The version to score.
        case: The case to score it on, if any.
        scorer_model: Serialized ``ModelConfig`` for the scorer's ``llm()``
            helper, if a model was chosen.
        timeout_seconds: Longest the call may run.
        runtime: Where the box is opened; the configured runtime when unset.
        install_command: Shell command run once in the box before the call.

    Returns:
        The score and side information, or the scorer's error, plus what
        its ``llm()`` calls consumed.

    Raises:
        ServiceError: When the gateway cannot be resolved or the box fails.
        BudgetError: When covered funding does not permit the probe.
        UsagePendingError: When execution or final runtime billing remains uncertain.
    """
    gateway = scorer_gateway(ModelConfig.model_validate(scorer_model), settings) if scorer_model else None
    scorer = SandboxPythonScorer(
        scorer_code,
        runtime=runtime or scorer_runtime_from_settings(settings),
        gateway=gateway,
        timeout_seconds=timeout_seconds,
        lifetime_seconds=timeout_seconds
        + _PROBE_LIFETIME_ALLOWANCE_SECONDS
        + (_INSTALL_TIMEOUT_SECONDS if (install_command or "").strip() else 0.0),
        job_id="probe",
        install_command=install_command,
    )
    try:
        return scorer.run(candidate, case)
    finally:
        scorer.close()
