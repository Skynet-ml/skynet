"""Tests for the sandboxed python scorer: file contract, gateway resolution, usage billing and probes."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from gepa.image import Image

from core.billing.model_gateway import ROUTE_KEY
from core.exceptions import ServiceError
from core.models.common import ModelConfig
from core.service_gateway.language_models import lm_call_count, total_tokens_from_history, usage_by_model_from_history

from .. import sandbox_log
from .. import sandbox_scorer as sandbox_scorer_mod
from ..harness import GatewayConfig
from ..runner import GatewayClient, ScorerError
from ..sandbox import CommandResult, LocalSubprocessRuntime, OutputSink
from ..sandbox_scorer import (
    CALLS_DIR,
    DEPENDENCIES_DIR,
    RUNNER_FILE,
    RUNNER_SOURCE,
    SandboxPythonScorer,
    ScorerGateway,
    ScorerProbeResult,
    ScorerUsage,
    probe_scorer,
    revive_images,
    scorer_gateway,
)
from .mocks import VOWEL_SCORER_CODE, FakeGateway, FakeSandboxRuntime, FakeSandboxSession

Responder = Callable[[dict[str, Any]], "CommandResult | dict[str, Any]"]

_GATEWAY = ScorerGateway(
    url="http://gw.example/v1", model="judge", api_key="k", billing_model="fake/judge", timeout_seconds=9.0
)
_OK = {"score": 0.5, "side_info": {}, "error": None, "usage": []}
_LLM_SCORER = "def score(candidate, case=None):\n    return float(llm(candidate, case['input'])), 'judged'\n"


def test_unprotected_byok_endpoint_accepts_declared_billing_name() -> None:
    """A custom endpoint accepts its original provider-qualified declaration."""
    with FakeGateway(reply="ok") as gateway:
        config = ModelConfig(
            name="openai/gpt-4.1-mini", token_source="byok", base_url=gateway.url, extra={"api_key": "test-key"}
        )
        bound = scorer_gateway(config, SimpleNamespace(lm_request_timeout_seconds=33.0))
        payload = bound.runner_payload()
        client = GatewayClient(payload["url"], payload["model"], declared_model=payload.get("declared_model"))
        assert client("prompt", model="openai/gpt-4.1-mini") == "ok"
        with pytest.raises(ScorerError, match="must match"):
            client("prompt", model="openai/other")
    assert gateway.requests[0]["body"]["model"] == "gpt-4.1-mini"


class _RunnerSession(FakeSandboxSession):
    """Fake box that understands the runner's file contract and answers from a callback."""

    def __init__(self, respond: Responder) -> None:
        """Remember the responder.

        Args:
            respond: Turns the call payload into an output document or a raw command result.
        """
        super().__init__()
        self._respond = respond
        self.envs: list[dict[str, str] | None] = []
        self.timeouts: list[float | None] = []
        self.payloads: list[dict[str, Any]] = []

    def run(
        self,
        command: str,
        *,
        env: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
        on_output: OutputSink | None = None,
    ) -> CommandResult:
        """Execute a runner invocation against the callback.

        Args:
            command: ``python3 app_runner.py <call_dir>``.
            env: Per-call environment.
            timeout_seconds: Per-call timeout.

        Returns:
            The command result (success unless the callback returned one).
        """
        self.commands.append(command)
        self.envs.append(dict(env) if env is not None else None)
        self.timeouts.append(timeout_seconds)
        call_dir = command.split()[-1]
        payload = json.loads(self.files[f"{call_dir}/input.json"])
        self.payloads.append(payload)
        # Like the real runner: the scorer source comes from the box's code file.
        answer = self._respond({"code": self.files.get(sandbox_scorer_mod.runner.CODE_FILE, ""), **payload})
        if isinstance(answer, CommandResult):
            return answer
        self.files[f"{call_dir}/output.json"] = json.dumps(answer)
        return CommandResult(exit_code=0)


class _InstallSession(_RunnerSession):
    """Runner session that also answers a non-runner install command from a fixed result."""

    def __init__(self, respond: Responder, install: CommandResult) -> None:
        """Remember the install outcome.

        Args:
            respond: Turns the call payload into an output document.
            install: What the install command returns.
        """
        super().__init__(respond)
        self._install = install

    def run(
        self,
        command: str,
        *,
        env: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
        on_output: OutputSink | None = None,
    ) -> CommandResult:
        """Answer the install command from the fixed result, runner calls as usual.

        Args:
            command: The command line.
            env: Per-call environment.
            timeout_seconds: Per-call timeout.

        Returns:
            The command result.
        """
        if not command.startswith("python3 "):
            self.commands.append(command)
            self.timeouts.append(timeout_seconds)
            return self._install
        return super().run(command, env=env, timeout_seconds=timeout_seconds, on_output=on_output)


class _SetupSession(_RunnerSession):
    """Runner session that also plays the dependency setup script from its request file."""

    def __init__(self, respond: Responder, extend: dict[str, Any]) -> None:
        """Remember how an ``extend`` request is answered.

        Args:
            respond: Turns the call payload into an output document.
            extend: The setup script's result document for ``extend`` requests.
        """
        super().__init__(respond)
        self._extend = extend
        self.requests: list[dict[str, Any]] = []

    def run(
        self,
        command: str,
        *,
        env: dict[str, str] | None = None,
        timeout_seconds: float | None = None,
        on_output: OutputSink | None = None,
    ) -> CommandResult:
        """Answer setup requests from the recorded request file, runner calls as usual.

        Args:
            command: The command line.
            env: Per-call environment.
            timeout_seconds: Per-call timeout.

        Returns:
            The command result.
        """
        if "setup.py" not in command:
            return super().run(command, env=env, timeout_seconds=timeout_seconds, on_output=on_output)
        self.commands.append(command)
        request = json.loads(self.files[f"{DEPENDENCIES_DIR}/request.json"])
        self.requests.append(request)
        outcome = {"ok": True, "result": {"installed": True}} if request["action"] == "install" else self._extend
        self.files[f"{DEPENDENCIES_DIR}/result.json"] = json.dumps(outcome)
        return CommandResult(exit_code=0)


_LOCK = {"python": "3.12.0", "imports": ["json", "numpy"], "artifacts": [{"name": "numpy", "sha256": "n" * 64}]}
_ROUTE = {"url": "http://relay.test", "token": "package-token"}
_SETUP_COMMAND = f"python3 {DEPENDENCIES_DIR}/setup.py {DEPENDENCIES_DIR}/request.json"


def _runtime(respond: Responder, *, injects_headers: bool = False) -> FakeSandboxRuntime:
    """Runtime whose boxes answer through ``respond``.

    Args:
        respond: The responder.
        injects_headers: Whether the runtime claims a header-injecting network edge.

    Returns:
        A fake runtime.
    """
    return FakeSandboxRuntime(lambda: _RunnerSession(respond), injects_headers=injects_headers)


def _usage(*pairs: tuple[int, int]) -> list[dict[str, int]]:
    """Build runner usage entries.

    Args:
        *pairs: ``(prompt_tokens, completion_tokens)`` per llm call.

    Returns:
        Usage entries as the runner reports them.
    """
    return [{"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c} for p, c in pairs]


def test_sandbox_scorer_installs_the_runner_once_and_runs_one_call_per_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runner ships on first use, every call gets its own directory, and the gateway key travels only via env."""
    # Pin the ceiling: a developer's .env may raise it above the 2700 default.
    monkeypatch.setattr(sandbox_scorer_mod.settings, "vercel_sandbox_max_lifetime_seconds", 2_700)
    runtime = _runtime(lambda payload: {**_OK, "score": len(payload["candidate"]), "usage": _usage((3, 1))})
    scorer = SandboxPythonScorer(
        "def score(c): return 1", runtime=runtime, gateway=_GATEWAY, timeout_seconds=7.5, job_id="job-1"
    )

    assert scorer("abc") == (3.0, {})
    assert scorer("hello", {"input": "x"}) == (5.0, {})
    scorer.close()

    [spec] = runtime.specs
    assert spec.name.startswith("app-scorer-job-1-")
    assert spec.tags == {"skynet_job": "job-1"}
    assert spec.lifetime_seconds == 2_700
    assert spec.inject_headers == {}
    [box] = runtime.sessions
    assert box.files[RUNNER_FILE] == RUNNER_SOURCE
    assert box.commands == [f"python3 {RUNNER_FILE} {CALLS_DIR}/000001", f"python3 {RUNNER_FILE} {CALLS_DIR}/000002"]
    assert box.envs == [{"APP_API_KEY": "k"}, {"APP_API_KEY": "k"}]
    assert box.timeouts == [7.5, 7.5]
    assert box.payloads[1]["candidate"] == "hello"
    assert box.payloads[1]["case"] == {"input": "x"}
    assert "code" not in box.payloads[1]
    assert box.files[sandbox_scorer_mod.runner.CODE_FILE] == "def score(c): return 1"
    assert spec.private is True
    assert box.payloads[1]["gateway"] == {
        "url": "http://gw.example/v1",
        "model": "judge",
        "declared_model": "fake/judge",
        "temperature": None,
        "max_tokens": None,
        "reasoning_effort": None,
        "timeout_seconds": 9.0,
    }
    assert "api_key" not in box.payloads[1]["gateway"]
    assert box.closed is True
    assert scorer.usage is not None
    assert usage_by_model_from_history(scorer.usage) == {"fake/judge": (6, 2)}
    assert lm_call_count(scorer.usage) == 2
    assert total_tokens_from_history(scorer.usage) == 8


def test_sandbox_scorer_without_a_model_sends_no_gateway_and_bills_nothing() -> None:
    """No model chosen: the payload has no gateway, the env carries no key, and there is no usage ledger."""
    runtime = _runtime(lambda payload: _OK)
    scorer = SandboxPythonScorer("def score(c): return 0.5", runtime=runtime, gateway=None, timeout_seconds=5)

    assert scorer("x") == (0.5, {})

    [box] = runtime.sessions
    assert box.payloads[0]["gateway"] is None
    assert box.envs == [None]
    assert runtime.specs[0].name is None
    assert runtime.specs[0].tags == {}
    assert scorer.usage is None


def test_sandbox_scorer_reports_scorer_errors_and_still_bills_the_tokens() -> None:
    """``run()`` returns the error as data; ``__call__`` raises it; tokens spent before the failure are billed."""
    runtime = _runtime(
        lambda payload: {"score": None, "side_info": {}, "error": "ValueError: bad candidate", "usage": _usage((4, 4))}
    )
    scorer = SandboxPythonScorer(
        "def score(c): raise ValueError('bad candidate')", runtime=runtime, gateway=_GATEWAY, timeout_seconds=5
    )

    probe = scorer.run("x")
    assert probe == ScorerProbeResult(
        score=None, side_info={}, error="ValueError: bad candidate", usage_by_model={"fake/judge": (4, 4)}
    )
    with pytest.raises(ServiceError, match="ValueError: bad candidate"):
        scorer("x")

    assert usage_by_model_from_history(scorer.usage) == {"fake/judge": (8, 8)}


def test_sandbox_scorer_revives_images_for_the_optimizer_but_not_for_probes() -> None:
    """Data-URL side info becomes ``Image`` objects on the engine path and stays a plain string in ``run()``."""
    data_url = "data:image/png;base64,aGk="
    runtime = _runtime(lambda payload: {**_OK, "side_info": {"render": data_url, "note": "n"}})
    scorer = SandboxPythonScorer("def score(c): return 0.5", runtime=runtime, gateway=None, timeout_seconds=5)

    score, side_info = scorer("x")
    probe = scorer.run("x")

    assert score == 0.5
    assert isinstance(side_info["render"], Image)
    assert side_info["render"].url == data_url
    assert side_info["note"] == "n"
    assert probe.side_info == {"render": data_url, "note": "n"}


def test_sandbox_scorer_turns_a_timed_out_call_into_a_scorer_error() -> None:
    """The box's timeout is reported as the scorer's timeout, in the user's units."""
    runtime = _runtime(lambda payload: CommandResult(exit_code=137, timed_out=True))
    scorer = SandboxPythonScorer("def score(c): return 1", runtime=runtime, gateway=None, timeout_seconds=2.5)

    assert scorer.run("x").error == "scorer exceeded the 2.5s timeout"
    with pytest.raises(ServiceError, match=r"scorer exceeded the 2\.5s timeout"):
        scorer("x")


def test_sandbox_scorer_reports_a_runner_crash_with_the_tail_of_stderr() -> None:
    """No ``output.json`` means the runner itself died: the exit code and the end of stderr are surfaced."""
    stderr = "x" * 5_000 + "\nTraceback: boom"
    runtime = _runtime(lambda payload: CommandResult(exit_code=3, stderr=stderr))
    scorer = SandboxPythonScorer("def score(c): return 1", runtime=runtime, gateway=None, timeout_seconds=5)

    with pytest.raises(ServiceError, match=r"(?s)scorer sandbox failed \(exit 3\): .*Traceback: boom$") as excinfo:
        scorer("x")

    assert len(str(excinfo.value)) <= len("scorer sandbox failed (exit 3): ") + 2_000


def test_sandbox_scorer_reopens_the_box_once_when_it_dies() -> None:
    """A dead box (a raw exception from the session) is discarded and the call retried in a fresh one."""
    attempts: list[int] = []

    def respond(payload: dict[str, Any]) -> dict[str, Any]:
        """Fail the first call outright, then answer normally."""
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("box went away")
        return _OK

    runtime = _runtime(respond)
    scorer = SandboxPythonScorer("def score(c): return 1", runtime=runtime, gateway=None, timeout_seconds=5)

    assert scorer("x") == (0.5, {})
    assert scorer("y") == (0.5, {})

    first, second = runtime.sessions
    assert first.closed is True
    assert second.closed is False
    assert second.commands == [f"python3 {RUNNER_FILE} {CALLS_DIR}/000002", f"python3 {RUNNER_FILE} {CALLS_DIR}/000003"]
    assert RUNNER_FILE in second.files


def test_sandbox_scorer_gives_up_after_one_reopen() -> None:
    """A box that dies twice in a row surfaces the underlying error."""

    def respond(payload: dict[str, Any]) -> dict[str, Any]:
        """Always fail."""
        raise OSError("still dead")

    scorer = SandboxPythonScorer("def score(c): return 1", runtime=_runtime(respond), gateway=None, timeout_seconds=5)

    with pytest.raises(OSError, match="still dead"):
        scorer("x")


def test_sandbox_scorer_runs_the_install_command_once_per_box_before_the_runner() -> None:
    """The install command runs right after the runner ships, once per box, never per call."""
    runtime = FakeSandboxRuntime(lambda: _InstallSession(lambda payload: _OK, CommandResult(exit_code=0)))
    scorer = SandboxPythonScorer(
        "def score(c): return 1",
        runtime=runtime,
        gateway=None,
        timeout_seconds=5,
        install_command="  pip install numpy  ",
    )

    assert scorer("x") == (0.5, {})
    assert scorer("y") == (0.5, {})

    [box] = runtime.sessions
    assert box.commands == [
        "pip install numpy",
        f"python3 {RUNNER_FILE} {CALLS_DIR}/000001",
        f"python3 {RUNNER_FILE} {CALLS_DIR}/000002",
    ]
    assert box.timeouts[0] == sandbox_scorer_mod._INSTALL_TIMEOUT_SECONDS


def test_sandbox_scorer_fails_clearly_when_the_install_command_fails() -> None:
    """A failed install closes the box and surfaces the exit code with the tail of stderr, without a retry."""
    install = CommandResult(exit_code=1, stderr="E: Unable to locate package libfoo\n")
    runtime = FakeSandboxRuntime(lambda: _InstallSession(lambda payload: _OK, install))
    scorer = SandboxPythonScorer(
        "def score(c): return 1",
        runtime=runtime,
        gateway=None,
        timeout_seconds=5,
        install_command="apt-get install libfoo",
    )

    with pytest.raises(
        ServiceError, match=r"scorer install command failed \(exit 1\): E: Unable to locate package libfoo"
    ):
        scorer("x")

    [box] = runtime.sessions
    assert box.closed is True
    assert box.commands == ["apt-get install libfoo"]


def test_sandbox_scorer_treats_an_install_timeout_as_a_failure() -> None:
    """An install that outruns its allowance fails the box instead of leaving a half-built one."""
    install = CommandResult(exit_code=137, timed_out=True)
    runtime = FakeSandboxRuntime(lambda: _InstallSession(lambda payload: _OK, install))
    scorer = SandboxPythonScorer(
        "def score(c): return 1", runtime=runtime, gateway=None, timeout_seconds=5, install_command="pip install torch"
    )

    with pytest.raises(ServiceError, match=r"scorer install command exceeded the 600s allowance"):
        scorer("x")


def test_sandbox_scorer_close_is_safe_before_and_after_use() -> None:
    """Closing an unopened or already-closed scorer does nothing."""
    runtime = _runtime(lambda payload: _OK)
    scorer = SandboxPythonScorer("def score(c): return 1", runtime=runtime, gateway=None, timeout_seconds=5)

    scorer.close()
    scorer("x")
    scorer.close()
    scorer.close()

    assert runtime.sessions[0].closed is True
    assert len(runtime.sessions) == 1


def test_scorer_usage_records_entries_in_the_lm_history_shape() -> None:
    """Missing counters default to zero so the shared usage helpers can read the ledger."""
    ledger = ScorerUsage("m")

    ledger.record([{"prompt_tokens": 2}, {}])

    assert ledger.model == "m"
    assert ledger.history == [
        {"usage": {"prompt_tokens": 2, "completion_tokens": 0, "total_tokens": 0}},
        {"usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}},
    ]
    assert ledger.by_model() == {"m": (2, 0)}


def test_scorer_gateway_for_managed_models_uses_the_shared_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    """Managed models go through the LiteLLM gateway with the ``openrouter/`` prefix stripped."""
    monkeypatch.setattr(
        sandbox_scorer_mod,
        "gateway_from_settings",
        lambda settings: GatewayConfig(url="http://gw/v1", api_key="gw-key"),
    )
    config = ModelConfig(
        name="openrouter/openai/gpt-4o", temperature=0.3, max_tokens=50, extra={"reasoning_effort": "high"}
    )

    gateway = scorer_gateway(config, SimpleNamespace(lm_request_timeout_seconds=33.0))

    assert gateway == ScorerGateway(
        url="http://gw/v1",
        model="openai/gpt-4o",
        api_key="gw-key",
        billing_model="openrouter/openai/gpt-4o",
        temperature=0.3,
        max_tokens=50,
        reasoning_effort="high",
        timeout_seconds=33.0,
        gateway_reasoning=True,
    )
    assert gateway.runner_payload() == {
        "url": "http://gw/v1",
        "model": "openai/gpt-4o",
        "declared_model": "openrouter/openai/gpt-4o",
        "temperature": 0.3,
        "max_tokens": 50,
        "reasoning_effort": "high",
        "timeout_seconds": 33.0,
        "gateway_reasoning": True,
    }


def test_scorer_gateway_for_managed_models_needs_a_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a reachable gateway a managed scorer model is a configuration error."""
    monkeypatch.setattr(sandbox_scorer_mod, "gateway_from_settings", lambda settings: None)

    with pytest.raises(ServiceError, match="LITELLM_PROXY_URL"):
        scorer_gateway(ModelConfig(name="openrouter/openai/gpt-4o"), SimpleNamespace(lm_request_timeout_seconds=33.0))


def test_subscription_gateway_passes_cli_route_to_runner() -> None:
    """Keep the source and billing name when the protected route changes the model name."""
    config = ModelConfig(
        name="subscription/claude/anthropic/claude-opus-5.5",
        token_source="subscription",
        cli_provider="claude",
        extra={ROUTE_KEY: {"url": "http://gw/v1", "model": "anthropic/claude-opus-5.5", "token": "key"}},
    )
    payload = scorer_gateway(config, SimpleNamespace(lm_request_timeout_seconds=33.0)).runner_payload()
    assert payload["subscription"] is True
    assert payload["model"] == "anthropic/claude-opus-5.5"
    assert payload["declared_model"] == config.name


@pytest.mark.parametrize(
    ("config", "url", "model"),
    [
        (
            ModelConfig(name="openrouter/anthropic/claude", token_source="byok", extra={"api_key": "byok"}),
            "https://openrouter.ai/api/v1",
            "anthropic/claude",
        ),
        (
            ModelConfig(
                name="openai/gpt-4o",
                token_source="byok",
                base_url="https://proxy.example/v1/",
                extra={"api_key": "byok"},
            ),
            "https://proxy.example/v1",
            "gpt-4o",
        ),
        (
            ModelConfig(
                name="gpt-4o", token_source="byok", extra={"api_key": "byok", "api_base": "https://proxy.example/v1"}
            ),
            "https://proxy.example/v1",
            "gpt-4o",
        ),
    ],
)
def test_scorer_gateway_for_byok_models_talks_to_the_provider_directly(
    config: ModelConfig, url: str, model: str
) -> None:
    """BYOK models use the user's key against OpenRouter or their own base URL.

    Args:
        config: The BYOK model.
        url: Expected gateway URL.
        model: Expected wire model name.
    """
    gateway = scorer_gateway(config, SimpleNamespace(lm_request_timeout_seconds=12.0))

    assert (gateway.url, gateway.model, gateway.api_key, gateway.billing_model) == (url, model, "byok", config.name)
    assert gateway.timeout_seconds == 12.0


def test_scorer_gateway_for_byok_models_needs_a_reachable_endpoint() -> None:
    """A BYOK model with neither an OpenRouter prefix nor a base URL cannot be reached from a box."""
    with pytest.raises(ServiceError, match="OpenRouter or a base_url"):
        scorer_gateway(
            ModelConfig(name="gpt-4o", token_source="byok", extra={"api_key": "byok"}),
            SimpleNamespace(lm_request_timeout_seconds=1.0),
        )


def test_revive_images_only_touches_data_urls() -> None:
    """Image data URLs become ``Image`` objects; other strings and values are untouched."""
    revived = revive_images({"render": "data:image/png;base64,aGk=", "url": "https://x/y.png", "n": 1})

    assert isinstance(revived["render"], Image)
    assert revived["render"].url == "data:image/png;base64,aGk="
    assert revived["url"] == "https://x/y.png"
    assert revived["n"] == 1


def test_probe_scorer_runs_the_real_runner_through_the_local_runtime() -> None:
    """End to end on the host: the vowel scorer scores a seed and returns its side info."""
    probe = probe_scorer(scorer_code=VOWEL_SCORER_CODE, candidate="aeiou", runtime=LocalSubprocessRuntime())

    assert probe == ScorerProbeResult(
        score=1.0, side_info={"feedback": "5 vowel(s)", "vowels": 5}, error=None, usage_by_model={}
    )


@pytest.mark.parametrize(
    ("code", "error"),
    [
        ("def score(c, case=None): raise ValueError('bad candidate')", "ValueError: bad candidate"),
        ("def score(c, case=None): return 'nope'", "must return feedback"),
        ("def score(c, case=None): return 1.0", "must return feedback"),
        ("def !!!", "scorer code has a syntax error"),
        (_LLM_SCORER, "Add model= to llm() or check the reflection model"),
    ],
)
def test_probe_scorer_reports_failures_as_data(code: str, error: str) -> None:
    """Probe failures come back in ``error`` rather than as exceptions.

    Args:
        code: The scorer source.
        error: Expected fragment of the error.
    """
    probe = probe_scorer(scorer_code=code, candidate="x", case={"input": "i"}, runtime=LocalSubprocessRuntime())

    assert probe.score is None
    assert error in (probe.error or "")


def test_probe_scorer_carries_large_side_info_and_images_back_from_the_box() -> None:
    """Multi-megabyte side info, image objects and odd types all survive the file round trip."""
    code = (
        "def score(candidate, case=None):\n"
        "    return 0.5, {'feedback': 'f', 'blob': 'x' * 3_000_000, 'render': Image(base64_data='aGk=', media_type='image/png'), 'odd': {1, 2}}\n"
    )

    probe = probe_scorer(scorer_code=code, candidate="x", runtime=LocalSubprocessRuntime())

    assert probe.error is None
    assert probe.score == 0.5
    assert len(probe.side_info["blob"]) == 3_000_000
    assert probe.side_info["render"] == "data:image/png;base64,aGk="
    assert probe.side_info["odd"] == "{1, 2}"


def test_probe_scorer_stops_a_scorer_that_outruns_its_timeout() -> None:
    """A hanging scorer is killed at the deadline and reported as a timeout."""
    started = time.monotonic()

    probe = probe_scorer(
        scorer_code="import time\ndef score(c, case=None):\n    time.sleep(30)\n    return 1\n",
        candidate="x",
        timeout_seconds=1,
        runtime=LocalSubprocessRuntime(),
    )

    assert probe.error == "scorer exceeded the 1s timeout"
    assert time.monotonic() - started < 10


def test_probe_scorer_calls_the_gateway_from_inside_the_box_and_bills_the_tokens() -> None:
    """The scorer's ``llm()`` reaches the gateway with the key from the environment and its usage is returned."""
    with FakeGateway(reply="0.75", usage=(5, 2)) as judge:
        gateway = ScorerGateway(url=judge.url, model="judge", api_key="secret-key", billing_model="fake/judge")
        runtime = LocalSubprocessRuntime()
        scorer = SandboxPythonScorer(_LLM_SCORER, runtime=runtime, gateway=gateway, timeout_seconds=30)
        try:
            probe = scorer.run("rate me", {"input": "the text"})
        finally:
            scorer.close()

    assert probe == ScorerProbeResult(
        score=0.75, side_info={"feedback": "judged"}, error=None, usage_by_model={"fake/judge": (5, 2)}
    )
    [request] = judge.requests
    assert request["authorization"] == "Bearer secret-key"
    assert request["body"]["model"] == "judge"
    assert request["body"]["messages"] == [
        {"role": "system", "content": "rate me"},
        {"role": "user", "content": "the text"},
    ]
    assert usage_by_model_from_history(scorer.usage) == {"fake/judge": (5, 2)}


def test_sandbox_scorer_leaves_the_key_to_the_network_edge_when_the_runtime_injects_it() -> None:
    """On a runtime with a header-injecting edge the key rides in the spec; the box's environment never sees it."""
    runtime = _runtime(lambda payload: _OK, injects_headers=True)
    scorer = SandboxPythonScorer("def score(c): return 1", runtime=runtime, gateway=_GATEWAY, timeout_seconds=5)

    scorer("abc")
    scorer.close()

    [spec] = runtime.specs
    assert spec.inject_headers == {"gw.example": {"Authorization": "Bearer k"}}
    [box] = runtime.sessions
    assert box.envs == [None]
    assert "api_key" not in box.payloads[0]["gateway"]


def test_sandbox_scorer_injects_nothing_without_a_key() -> None:
    """A keyless gateway adds no injection rule and no environment."""
    runtime = _runtime(lambda payload: _OK, injects_headers=True)
    gateway = ScorerGateway(url="http://gw.example/v1", model="m", api_key=None, billing_model="b")
    scorer = SandboxPythonScorer("def score(c): return 1", runtime=runtime, gateway=gateway, timeout_seconds=5)

    scorer("abc")
    scorer.close()

    assert runtime.specs[0].inject_headers == {}
    assert runtime.sessions[0].envs == [None]


def test_sandbox_scorer_caps_the_lifetime_at_the_configured_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """``VERCEL_SANDBOX_MAX_LIFETIME_SECONDS`` is the default lifetime and the cap on a requested one."""
    monkeypatch.setattr(sandbox_scorer_mod.settings, "vercel_sandbox_max_lifetime_seconds", 100.0)
    lifetimes = []
    for requested in (None, 50.0, 500.0):
        runtime = _runtime(lambda payload: _OK)
        scorer = SandboxPythonScorer(
            "def score(c): return 1", runtime=runtime, gateway=None, timeout_seconds=5, lifetime_seconds=requested
        )
        scorer("abc")
        scorer.close()
        lifetimes.append(runtime.specs[0].lifetime_seconds)

    assert lifetimes == [100.0, 50.0, 100.0]


def test_sandbox_scorer_extends_the_lock_when_a_candidate_imports_something_new() -> None:
    """A candidate's new third-party import triggers one extend run; the merged lock reinstalls in a new box."""
    added = {"ok": True, "result": {"imports": ["pyrender"], "artifacts": [{"name": "pyrender", "sha256": "p" * 64}]}}
    runtime = FakeSandboxRuntime(lambda: _SetupSession(lambda payload: _OK, added))
    scorer = SandboxPythonScorer(
        "def score(c): return 1",
        runtime=runtime,
        gateway=None,
        timeout_seconds=5,
        dependency_lock=_LOCK,
        dependency_route=_ROUTE,
    )

    assert scorer("import numpy\nimport json\n") == (0.5, {})
    assert scorer("import pyrender\nimport os\n") == (0.5, {})
    assert scorer("import pyrender\n") == (0.5, {})

    [box] = runtime.sessions
    assert box.commands == [
        _SETUP_COMMAND,
        f"python3 {RUNNER_FILE} {CALLS_DIR}/000001",
        _SETUP_COMMAND,
        f"python3 {RUNNER_FILE} {CALLS_DIR}/000002",
        f"python3 {RUNNER_FILE} {CALLS_DIR}/000003",
    ]
    assert [request["action"] for request in box.requests] == ["install", "extend"]
    assert box.requests[1]["candidate"] == "import pyrender\nimport os\n"
    assert box.requests[1]["lock"] == _LOCK
    assert box.requests[1]["route"] == _ROUTE

    scorer._discard()
    assert scorer("import pyrender\n") == (0.5, {})
    [_, fresh] = runtime.sessions
    assert fresh.requests[0]["action"] == "install"
    assert fresh.requests[0]["lock"]["imports"] == ["json", "numpy", "pyrender"]
    assert [artifact["name"] for artifact in fresh.requests[0]["lock"]["artifacts"]] == ["numpy", "pyrender"]


def test_sandbox_scorer_reports_an_uninstallable_candidate_package_as_feedback() -> None:
    """When a candidate's package cannot be installed the candidate fails, the job continues."""
    refused = {"ok": False, "error": "No matching distribution found for pyrenderx"}
    runtime = FakeSandboxRuntime(lambda: _SetupSession(lambda payload: _OK, refused))
    scorer = SandboxPythonScorer(
        "def score(c): return 1",
        runtime=runtime,
        gateway=None,
        timeout_seconds=5,
        dependency_lock=_LOCK,
        dependency_route=_ROUTE,
    )

    probe = scorer.run("import pyrenderx\n")
    assert probe.score is None
    assert probe.error == "candidate packages could not be installed: No matching distribution found for pyrenderx"
    assert scorer("import numpy\n") == (0.5, {})

    [box] = runtime.sessions
    assert [request["action"] for request in box.requests] == ["install", "extend"]
    assert box.commands[-1] == f"python3 {RUNNER_FILE} {CALLS_DIR}/000001"
    assert box.closed is False


def test_scorer_log_events_reach_the_run_log_tagged_with_their_candidate_and_case() -> None:
    """End to end through the real runner: ``log()`` streams out, prints stay hidden."""
    code = (
        "from skynet import log\n"
        "def score(candidate, case=None):\n"
        "    log('judge.retry', 'retrying the judge', level='warning', attempt=2)\n"
        "    print('hidden from the log')\n"
        "    return 1.0, 'ok'\n"
    )
    records: list[logging.LogRecord] = []
    handler = logging.Handler(logging.DEBUG)
    handler.emit = records.append  # type: ignore[method-assign]
    sandbox_log.stream_logger.addHandler(handler)
    try:
        with sandbox_log.event_scope(candidate="3", case="1"):
            probe = probe_scorer(scorer_code=code, candidate="x", runtime=LocalSubprocessRuntime())
    finally:
        sandbox_log.stream_logger.removeHandler(handler)
    assert probe.score == 1.0
    [event] = [record for record in records if record.run_log["event"]]
    assert event.levelno == logging.WARNING
    assert event.getMessage() == "retrying the judge"
    assert event.run_log == {
        "source": "scorer",
        "event": "judge.retry",
        "fields": {"attempt": 2},
        "candidate": "3",
        "case": "1",
    }
    assert not any("hidden from the log" in record.getMessage() for record in records)
