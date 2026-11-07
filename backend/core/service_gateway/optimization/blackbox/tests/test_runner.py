"""Tests for the in-sandbox runner: the stdlib-only module shipped into every scorer box."""

from __future__ import annotations

import base64
import http.client
import io
import json
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from .. import runner
from ..runner import (
    GatewayClient,
    Image,
    ScorerError,
    accepts_case,
    image_content_part,
    load_scorer_from_code,
    log,
    normalize_score,
    run_call,
    scorer_messages,
    side_info_json_default,
)
from .mocks import FakeGateway

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
PNG_DATA_URL = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode("ascii")
_LLM_SCORER = (
    "def score(candidate, case=None):\n"
    "    return float(llm(candidate, case['input'])), {'asked': candidate, 'feedback': 'judged'}\n"
)
_SYSTEM_PYTHON = Path("/usr/bin/python3")
_INTERPRETERS = [sys.executable] + ([str(_SYSTEM_PYTHON)] if _SYSTEM_PYTHON.is_file() else [])


def test_short_model_matches_resolved_bound_route() -> None:
    """Accept a short declaration resolved by the protected gateway."""
    assert runner.model_matches_bound("gpt-4.1-mini", "openai/gpt-4.1-mini", "gpt-4.1-mini")
    assert not runner.model_matches_bound("gpt-4.1-mini", "openai/gpt-4.1-mini")
    assert not runner.model_matches_bound("other", "openai/gpt-4.1-mini", "gpt-4.1-mini")
    with FakeGateway(reply="ok") as gateway:
        client = GatewayClient(gateway.url, "openai/gpt-4.1-mini", declared_model="gpt-4.1-mini")
        assert client("prompt", model="gpt-4.1-mini") == "ok"
        with pytest.raises(ScorerError, match="must match"):
            client("prompt", model="anthropic/gpt-4.1-mini")
    assert gateway.requests[0]["body"]["model"] == "openai/gpt-4.1-mini"


class _WithScore:
    """Object exposing a numeric ``score`` and a ``feedback`` attribute."""

    score = 0.25
    feedback = "attr"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ((0.5, {"feedback": "ok"}), (0.5, {"feedback": "ok"})),
        ([0.5, "just text"], (0.5, {"feedback": "just text"})),
        ({"score": 0.75, "feedback": "f", "note": "n"}, (0.75, {"feedback": "f", "note": "n"})),
        (
            {"score": 1, "feedback": "f", "scores": {"a": {"score": 1, "feedback": "x"}, "b": (0, "y")}},
            (
                1.0,
                {
                    "feedback": "f",
                    "scores": {"a": {"score": 1.0, "feedback": "x"}, "b": {"score": 0.0, "feedback": "y"}},
                },
            ),
        ),
        (_WithScore(), (0.25, {"feedback": "attr"})),
    ],
)
def test_normalize_score_accepts_documented_shapes(raw: Any, expected: tuple[float, dict[str, Any]]) -> None:
    """Every documented return shape normalizes to ``(score, side_info)``.

    Args:
        raw: What the scorer returned.
        expected: The normalized pair.
    """
    assert normalize_score(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "0.5",
        None,
        {"feedback": "no score"},
        (0.5, {}, "extra"),
        0.5,
        True,
        (0.5, {}),
        (0.5, "  "),
        {"score": 0.75, "note": "n"},
        {"score": 1, "feedback": "f", "scores": {"a": 1.0}},
        {"score": 1, "feedback": "f", "scores": {"a": {"score": 1}}},
        {"score": 1, "feedback": "f", "scores": [1]},
    ],
)
def test_normalize_score_rejects_other_shapes(raw: Any) -> None:
    """Anything else, including any score without feedback, is a scorer contract violation.

    Args:
        raw: An unsupported return value.
    """
    with pytest.raises(ScorerError, match="must return feedback"):
        normalize_score(raw)


def test_load_scorer_prefers_score_then_metric_then_single_function() -> None:
    """Entrypoint lookup order: ``score``, ``metric``, the only function defined."""
    both = "def metric(c, x=None): return 0.0\ndef score(c, x=None): return 1.0\n"
    metric_only = "def metric(c, x=None): return 0.5\n"
    single = "import math\ndef judge(c, x=None): return 0.75\n"

    assert load_scorer_from_code(both)("x") == 1.0
    assert load_scorer_from_code(metric_only)("x") == 0.5
    assert load_scorer_from_code(single)("x") == 0.75


def test_load_scorer_ignores_imported_and_injected_functions() -> None:
    """Neither imports nor the injected helpers count as the user's single function."""
    code = "from math import sqrt\ndef judge(c, x=None): return 1.0\n"

    assert load_scorer_from_code(code, helpers={"llm": lambda *a, **k: ""})("x") == 1.0


def test_load_scorer_lets_the_code_import_its_helpers() -> None:
    """``from skynet import llm, Image`` binds the same helpers as the injected names."""
    code = "from skynet import llm, Image\n\ndef score(c, x=None):\n    return (llm(c), Image.__name__)\n"

    fn = load_scorer_from_code(code, helpers={"llm": lambda *a, **k: "ok", "Image": Image})

    assert fn("x") == ("ok", "Image")


def test_load_scorer_ignores_helpers_imported_from_skynet() -> None:
    """A helper pulled in through the import is no more the user's function than an injected one."""
    code = "from skynet import llm\ndef judge(c, x=None): return llm(c)\n"

    assert load_scorer_from_code(code, helpers={"llm": lambda *a, **k: 1.0})("x") == 1.0


def test_skynet_module_follows_the_helpers_of_the_latest_load() -> None:
    """Each load rebuilds the module, so a scorer importing lazily sees the helpers of its own run."""
    code = "def score(c, x=None):\n    from skynet import llm\n    return llm(c)\n"

    load_scorer_from_code(code, helpers={"llm": lambda *a, **k: 0.25})

    assert load_scorer_from_code(code, helpers={"llm": lambda *a, **k: 0.5})("x") == 0.5


@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("def !!!", "syntax error"),
        ("raise RuntimeError('boom')", "failed to load: RuntimeError: boom"),
        ("x = 1", "must define a function named 'score"),
        ("def a(c): return 1\ndef b(c): return 2\n", "must define a function named 'score"),
    ],
)
def test_load_scorer_reports_unusable_code(code: str, message: str) -> None:
    """Broken or ambiguous scorer code raises a ``ScorerError`` naming the problem.

    Args:
        code: The scorer source.
        message: Expected fragment of the error.
    """
    with pytest.raises(ScorerError, match=message):
        load_scorer_from_code(code)


def test_accepts_case_reads_the_positional_arity() -> None:
    """``score(candidate)`` gets no case; ``score(candidate, case)`` and ``score(*args)`` do."""
    assert accepts_case(load_scorer_from_code("def score(candidate): return 1")) is False
    assert accepts_case(load_scorer_from_code("def score(candidate, case): return 1")) is True
    assert accepts_case(load_scorer_from_code("def score(*args): return 1")) is True
    assert accepts_case(load_scorer_from_code("def score(candidate, *, case=None): return 1")) is False


def test_image_content_part_reads_every_documented_shape(tmp_path: Path) -> None:
    """Bytes, paths, URLs, base64, ``Image`` objects and ready parts all become ``image_url`` parts."""
    file_path = tmp_path / "view.jpg"
    file_path.write_bytes(b"\xff\xd8\xff" + b"\x00" * 8)
    part = {"type": "image_url", "image_url": {"url": "https://x/y.png"}}
    assert image_content_part(part) is part
    assert image_content_part(PNG_BYTES)["image_url"]["url"] == PNG_DATA_URL
    assert image_content_part(bytearray(PNG_BYTES))["image_url"]["url"] == PNG_DATA_URL
    assert image_content_part(PNG_DATA_URL)["image_url"]["url"] == PNG_DATA_URL
    assert image_content_part("https://x/y.png")["image_url"]["url"] == "https://x/y.png"
    assert image_content_part(str(file_path))["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert image_content_part(base64.b64encode(PNG_BYTES).decode("ascii"))["image_url"]["url"] == PNG_DATA_URL
    assert image_content_part(Image(base64_data="aGk=", media_type="image/png"))["type"] == "image_url"


def test_image_content_part_rejects_what_it_cannot_read() -> None:
    """Unreadable images name the helper argument in the error."""
    with pytest.raises(ScorerError, match=r"llm\(images=\.\.\.\)"):
        image_content_part(42)
    with pytest.raises(ScorerError, match=r"llm\(images=\.\.\.\)"):
        image_content_part("not base64 at all!!")
    with pytest.raises(ScorerError, match="url, a path or base64_data"):
        Image().to_openai_content_part()


def test_scorer_messages_attach_images_to_the_user_turn() -> None:
    """``prompt`` alone is the user turn; with ``input`` it becomes the system turn; images ride on the user turn."""
    assert scorer_messages("hi") == [{"role": "user", "content": "hi"}]
    assert scorer_messages("system", "case input") == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "case input"},
    ]
    messages = scorer_messages("Rate this", images=PNG_BYTES)
    assert messages == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Rate this"},
                {"type": "image_url", "image_url": {"url": PNG_DATA_URL}},
            ],
        }
    ]
    with_input = scorer_messages("system", "case input", images=[PNG_BYTES, PNG_BYTES])
    assert with_input[0] == {"role": "system", "content": "system"}
    assert [part["type"] for part in with_input[1]["content"]] == ["text", "image_url", "image_url"]


def test_side_info_json_default_inlines_images_as_data_urls() -> None:
    """Images serialize as data URLs; anything else falls back to ``str()``."""
    assert side_info_json_default(Image(base64_data="aGk=", media_type="image/png")) == "data:image/png;base64,aGk="
    assert side_info_json_default(object()).startswith("<object object")


def test_gateway_client_posts_chat_completions_and_records_usage() -> None:
    """One call is one bearer-authenticated POST carrying the model, sampling options and chat."""
    with FakeGateway(reply="judged", usage=(7, 2)) as gateway:
        llm = GatewayClient(
            gateway.url + "/",
            "gpt-x",
            api_key="k",
            temperature=0.2,
            max_tokens=64,
            reasoning_effort="high",
            timeout_seconds=5,
        )

        assert llm("system", "user text") == "judged"

    [request] = gateway.requests
    assert request["path"] == "/v1/chat/completions"
    assert request["authorization"] == "Bearer k"
    assert request["body"] == {
        "model": "gpt-x",
        "messages": [{"role": "system", "content": "system"}, {"role": "user", "content": "user text"}],
        "temperature": 0.2,
        "max_tokens": 64,
        "reasoning_effort": "high",
    }
    assert llm.usage == [{"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9}]


def test_gateway_client_guards_model_and_applies_per_call_overrides() -> None:
    """Keep the billed route model fixed while applying call settings."""
    with FakeGateway(reply="ok") as gateway:
        client = GatewayClient(gateway.url, "openai/a", temperature=0.1, max_tokens=30)
        assert (
            client("prompt", model="openrouter/openai/a", temperature=0.0, max_tokens=20, reasoning_effort="low")
            == "ok"
        )
        with pytest.raises(ScorerError, match="must match"):
            client("prompt", model="openai/b")
    [request] = gateway.requests
    assert request["body"]["model"] == "openai/a"
    assert request["body"]["temperature"] == 0.0
    assert request["body"]["max_tokens"] == 20
    assert request["body"]["reasoning_effort"] == "low"
    assert "skynet_cli_effort" not in request["body"]


def test_openrouter_gateway_translates_reasoning_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    """Send OpenRouter's native reasoning settings with a per-call effort."""
    requests: list[dict[str, Any]] = []
    client = GatewayClient("https://openrouter.ai/api/v1", "anthropic/example")

    def post(data: bytes) -> dict[str, Any]:
        """Record the request body and return a fixed completion."""
        requests.append(json.loads(data))
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(client, "_post", post)
    assert client("prompt", reasoning_effort="high") == "ok"
    assert requests[0]["reasoning"] == {"effort": "high", "summary": "auto"}


def test_protected_gateway_translates_reasoning_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    """Honor the gateway flag passed to the runner for a protected route."""
    requests: list[dict[str, Any]] = []

    def post(_self: GatewayClient, data: bytes) -> dict[str, Any]:
        """Capture the body without contacting a gateway."""
        requests.append(json.loads(data))
        return {"choices": [{"message": {"content": "0.5"}}]}

    monkeypatch.setattr(GatewayClient, "_post", post)
    result = run_call(
        {
            "code": "def score(c): return float(llm(c, reasoning_effort='high')), {'feedback': 'ok'}",
            "candidate": "x",
            "gateway": {"url": "http://gateway", "model": "anthropic/x", "protected": True, "gateway_reasoning": True},
        }
    )
    assert result["error"] is None
    assert requests[0]["reasoning"] == {"effort": "high", "summary": "auto"}


def test_subscription_runner_forwards_explicit_cli_effort() -> None:
    """Send an explicit per-call effort without changing the bound model."""
    name = "subscription/claude/anthropic/claude-opus-5.5"
    model = "anthropic/claude-opus-5.5"
    with FakeGateway(reply="0.75") as gateway:
        base = {
            "candidate": "judge",
            "gateway": {
                "url": gateway.url,
                "model": model,
                "declared_model": name,
                "subscription": True,
                "reasoning_effort": "low",
            },
        }
        code = f"def score(c):\n return float(llm(c, model={name!r}, reasoning_effort='high')), {{'feedback': 'ok'}}"
        explicit = run_call({**base, "code": code})
        default = run_call({**base, "code": "def score(c):\n return float(llm(c)), {'feedback': 'ok'}"})
    assert explicit["error"] is None
    assert default["error"] is None
    first, second = [request["body"] for request in gateway.requests]
    assert first["model"] == second["model"] == model
    assert first["skynet_cli_effort"] == "high"
    assert "skynet_cli_effort" not in second


def test_gateway_client_sends_images_and_raw_messages_without_a_key() -> None:
    """Images become content parts, ``messages=`` goes through verbatim, and no key means no header."""
    with FakeGateway() as gateway:
        llm = GatewayClient(gateway.url, "m")

        assert llm("Rate this", images=[PNG_BYTES]) == "0.5"
        raw = [{"role": "user", "content": "raw"}]
        assert llm(messages=raw) == "0.5"
        with pytest.raises(ScorerError, match="needs a prompt"):
            llm()
        with pytest.raises(ScorerError, match="list of chat messages"):
            llm(messages="nope")  # type: ignore[arg-type]

    first, second = gateway.requests
    assert first["authorization"] is None
    assert first["body"]["messages"][0]["content"][1]["image_url"]["url"] == PNG_DATA_URL
    assert second["body"] == {"model": "m", "messages": raw, "max_tokens": runner.DEFAULT_MAX_TOKENS}


def test_gateway_client_always_caps_the_output() -> None:
    """Without a configured cap ``llm()`` still sends one, since the budget relay refuses an uncapped call."""
    with FakeGateway() as gateway:
        GatewayClient(gateway.url, "m")("p")

    [request] = gateway.requests
    assert request["body"]["max_tokens"] == runner.DEFAULT_MAX_TOKENS > 0


def test_gateway_client_retries_transient_statuses_and_gives_up_on_others(monkeypatch: pytest.MonkeyPatch) -> None:
    """5xx/429 answers are retried a few times; a 4xx is reported at once with the body."""
    monkeypatch.setattr(runner, "_BACKOFF_SECONDS", 0.0)
    with FakeGateway(statuses=[503, 429]) as gateway:
        assert GatewayClient(gateway.url, "m")("p") == "0.5"
    assert len(gateway.requests) == 3

    with (
        FakeGateway(statuses=[401]) as gateway,
        pytest.raises(ScorerError, match=r"HTTP 401: \{\"error\": \"try again\"\}"),
    ):
        GatewayClient(gateway.url, "m")("p")
    assert len(gateway.requests) == 1

    with FakeGateway(statuses=[500, 500, 500]) as gateway, pytest.raises(ScorerError, match="HTTP 500"):
        GatewayClient(gateway.url, "m")("p")
    assert len(gateway.requests) == 3


def test_gateway_client_reports_unreachable_gateways(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dead endpoint surfaces as an ``llm()`` failure, not a raw socket error."""
    monkeypatch.setattr(runner, "_BACKOFF_SECONDS", 0.0)
    with FakeGateway() as gateway:
        url = gateway.url

    with pytest.raises(ScorerError, match=r"llm\(\) request failed"):
        GatewayClient(url, "m", timeout_seconds=2)("p")


def test_run_call_scores_and_serializes_side_info() -> None:
    """The output carries the score, JSON-plain side info (images as data URLs) and an empty usage list."""
    code = (
        "def score(candidate, case):\n"
        "    return len(candidate) / 10, {\n"
        "        'feedback': 'f', 'length': len(candidate), 'render': Image(base64_data='aGk='), 'odd': {1}\n"
        "    }\n"
    )

    result = run_call({"code": code, "candidate": "hello", "case": {"x": 1}, "gateway": None})

    assert result == {
        "score": 0.5,
        "side_info": {"feedback": "f", "length": 5, "render": "data:image/png;base64,aGk=", "odd": "{1}"},
        "error": None,
        "usage": [],
    }


def test_run_call_passes_case_only_when_the_function_takes_one() -> None:
    """A ``score(candidate)`` scorer is called without the case; ``score(candidate, case)`` gets it."""
    scores = [
        run_call({"code": code, "candidate": "abc", "case": {"weight": 2}})["score"]
        for code in (
            "def score(candidate): return len(candidate), 'f'",
            "def score(candidate, case): return case['weight'] * len(candidate), 'f'",
            "def score(*args): return len(args), 'f'",
        )
    ]

    assert scores == [3.0, 6.0, 2.0]


@pytest.mark.parametrize(
    ("code", "error"),
    [
        ("def score(c, case=None): raise ValueError('bad candidate')", "ValueError: bad candidate"),
        ("def score(c, case=None): raise SystemExit(3)", "SystemExit: 3"),
        ("def score(c, case=None): return 'nope'", "must return feedback"),
        ("def score(c, case=None): return 0.5", "must return feedback"),
        ("def !!!", "scorer code has a syntax error"),
        (_LLM_SCORER, "The metric calls llm() but has no model. Add model= to llm() or check the reflection model."),
    ],
)
def test_run_call_reports_scorer_failures_as_data(code: str, error: str) -> None:
    """Whatever the scorer does wrong lands in ``error``; the runner itself never raises.

    Args:
        code: The scorer source.
        error: Expected fragment of the reported error.
    """
    result = run_call({"code": code, "candidate": "x", "case": {"input": "i"}, "gateway": None})

    assert result["score"] is None
    assert error in result["error"]


def test_run_call_binds_llm_to_the_gateway_with_the_key_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``llm()`` posts to the payload's gateway, authenticates from the environment and reports its usage."""
    monkeypatch.setenv(runner.ENV_API_KEY, "env-key")
    with FakeGateway(reply="0.75", usage=(3, 1)) as gateway:
        payload = {
            "code": _LLM_SCORER,
            "candidate": "judge",
            "case": {"input": "text"},
            "gateway": {
                "url": gateway.url,
                "model": "m",
                "temperature": None,
                "max_tokens": None,
                "reasoning_effort": None,
                "timeout_seconds": 5,
            },
        }

        result = run_call(payload)

    assert result == {
        "score": 0.75,
        "side_info": {"asked": "judge", "feedback": "judged"},
        "error": None,
        "usage": [{"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}],
    }
    [request] = gateway.requests
    assert request["authorization"] == "Bearer env-key"
    assert request["body"]["messages"] == [{"role": "system", "content": "judge"}, {"role": "user", "content": "text"}]


def test_run_call_prefers_a_key_in_the_payload_over_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit ``api_key`` in the gateway section wins over ``APP_API_KEY``."""
    monkeypatch.setenv(runner.ENV_API_KEY, "env-key")
    with FakeGateway() as gateway:
        run_call(
            {
                "code": _LLM_SCORER,
                "candidate": "j",
                "case": {"input": "t"},
                "gateway": {"url": gateway.url, "model": "m", "api_key": "inline"},
            }
        )

    assert gateway.requests[0]["authorization"] == "Bearer inline"


def test_run_call_reports_usage_even_when_the_scorer_fails_after_calling_llm() -> None:
    """Tokens spent before the scorer blew up are still billed."""
    code = "def score(c, case=None):\n    llm('p')\n    raise ValueError('late')\n"
    with FakeGateway() as gateway:
        result = run_call({"code": code, "candidate": "x", "gateway": {"url": gateway.url, "model": "m"}})

    assert result["error"] == "ValueError: late"
    assert result["usage"] == [{"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}]


@pytest.mark.parametrize("interpreter", _INTERPRETERS)
def test_main_runs_the_file_contract_end_to_end(tmp_path: Path, interpreter: str) -> None:
    """``python3 app_runner.py <call_dir>`` turns ``input.json`` into ``output.json`` on every supported interpreter.

    Args:
        tmp_path: Pytest fixture.
        interpreter: The python that runs the runner (the venv's, and the 3.9 system one when present).
    """
    call_dir = tmp_path / "calls" / "000001"
    call_dir.mkdir(parents=True)
    payload = {
        "code": "def score(candidate, case=None):\n    return 0.5, {'feedback': 'f', 'render': Image(base64_data='aGk=')}\n",
        "candidate": "x",
        "case": None,
        "gateway": None,
    }
    (call_dir / runner.INPUT_FILE).write_text(json.dumps(payload), encoding="utf-8")

    completed = subprocess.run(
        [interpreter, runner.__file__, str(call_dir)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads((call_dir / runner.OUTPUT_FILE).read_text(encoding="utf-8")) == {
        "score": 0.5,
        "side_info": {"feedback": "f", "render": "data:image/png;base64,aGk="},
        "error": None,
        "usage": [],
    }
    assert not (call_dir / (runner.OUTPUT_FILE + ".part")).exists()


def test_main_wants_exactly_one_argument(capsys: pytest.CaptureFixture[str]) -> None:
    """Anything but a single call directory is a usage error."""
    assert runner.main([]) == 2
    assert runner.main(["a", "b"]) == 2
    assert "usage:" in capsys.readouterr().err


def test_log_writes_one_framed_event_to_the_real_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fields of any type travel as strict JSON; a redirected sys.stderr does not swallow it."""
    real, redirected = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "__stderr__", real)
    monkeypatch.setattr(sys, "stderr", redirected)
    log("judge.retry", "retrying", level="warning", attempt=2, ratio=float("nan"), path=Path("/x"))
    assert redirected.getvalue() == ""
    line = real.getvalue()
    assert line.startswith(runner.EVENT_PREFIX)
    assert line.endswith("\n")
    assert json.loads(line[len(runner.EVENT_PREFIX) :]) == {
        "level": "WARNING",
        "logger": "scorer",
        "message": "retrying",
        "event": "judge.retry",
        "fields": {"attempt": 2, "ratio": "nan", "path": "/x"},
    }


@pytest.mark.parametrize(("event", "level"), [("", "INFO"), ("  ", "INFO"), ("ok", "CRITICAL"), ("ok", "loud")])
def test_log_rejects_a_missing_name_or_an_unknown_level(event: str, level: str) -> None:
    """A scorer learns about a bad call at once, not from a silent log."""
    with pytest.raises(ValueError):
        log(event, level=level)


def test_scorer_code_imports_log_from_skynet() -> None:
    """``from skynet import log`` works in the box and in validation alike."""
    fn = load_scorer_from_code(
        "from skynet import log\ndef score(c, case=None):\n    return 1.0, 'ok'\n", helpers={"log": log}
    )
    assert fn("x") == (1.0, "ok")


def test_env_value_prefers_the_new_name_and_falls_back_to_the_legacy_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read ``APP_*`` first, then the pre-rename ``SKYNET_*`` spelling an older caller sets."""
    monkeypatch.delenv("APP_API_KEY", raising=False)
    monkeypatch.delenv("SKYNET_API_KEY", raising=False)
    assert runner.env_value("APP_API_KEY") is None
    monkeypatch.setenv("SKYNET_API_KEY", "old")
    assert runner.env_value("APP_API_KEY") == "old"
    monkeypatch.setenv("APP_API_KEY", "new")
    assert runner.env_value("APP_API_KEY") == "new"


def _answer_stage_calls(responses: Path, answers: dict[str, dict[str, Any]], sent: list[dict[str, Any]]) -> Any:
    """Build a fake stdout that answers each stage frame the way the parent relay does.

    Args:
        responses: The folder the client polls for answers.
        answers: The stage name to the answer the parent writes.
        sent: Collects every frame the client sends.

    Returns:
        A writable stream to put in place of ``sys.__stdout__``.
    """

    class Relay(io.StringIO):
        """Answer each complete frame line at once."""

        def write(self, text: str) -> int:
            """Record a frame and write its answer.

            Args:
                text: One frame, on its own line.

            Returns:
                The number of characters taken.
            """
            assert text.startswith("\nPFX:")
            assert text.endswith("\n")
            frame = json.loads(text.strip().removeprefix("PFX:"))
            sent.append(frame)
            (responses / f"{frame['id']}.json").write_text(json.dumps(answers[frame["stage"]]))
            return len(text)

    return Relay()


def test_stage_call_returns_the_output_and_raises_stage_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``call`` sends one frame, returns the stage output, and turns a stage failure into ``StageError``."""
    sent: list[dict[str, Any]] = []
    answers = {"good": {"output": {"doubled": 4}}, "bad": {"error": "Stage 'bad' passed its 5s timeout."}}
    monkeypatch.setattr(sys, "__stdout__", _answer_stage_calls(tmp_path, answers, sent))
    client = runner.StageClient({"prefix": "PFX:", "response_dir": str(tmp_path), "names": ["good", "bad"]})
    assert client("good", {"x": 2}) == {"doubled": 4}
    assert sent[0]["stage"] == "good"
    assert sent[0]["input"] == {"x": 2}
    assert len(sent[0]["id"]) == 32
    with pytest.raises(runner.StageError, match="timeout"):
        client("bad", None)
    assert list(tmp_path.iterdir()) == []


def test_stage_call_with_an_unknown_name_sends_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unknown stage name fails at once with the run's stage names, and no frame leaves the box."""
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(sys, "__stdout__", _answer_stage_calls(tmp_path, {}, sent))
    client = runner.StageClient({"prefix": "PFX:", "response_dir": str(tmp_path), "names": ["good"]})
    with pytest.raises(runner.StageError, match=r"Unknown stage 'nope'.*good"):
        client("nope", 1)
    assert sent == []


def test_stage_call_raises_the_budget_control(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A budget stop on the parent reaches the scorer as the gateway's control signal."""
    answers = {"good": {"control": {"code": "budget_reached", "message": "spent"}}}
    monkeypatch.setattr(sys, "__stdout__", _answer_stage_calls(tmp_path, answers, []))
    client = runner.StageClient({"prefix": "PFX:", "response_dir": str(tmp_path), "names": ["good"]})
    with pytest.raises(runner.GatewayControl):
        client("good", 1)


def test_a_run_without_stages_has_a_call_that_explains_itself() -> None:
    """Scorer code that imports ``call`` still loads; calling it says the run has no stages."""
    fn = load_scorer_from_code(
        "from skynet import call, StageError\n"
        "def score(c, case=None):\n"
        "    try:\n"
        "        call('x', 1)\n"
        "    except StageError as error:\n"
        "        return 0.0, str(error)\n",
        helpers={runner.STAGE_HELPER_NAME: runner.missing_stage, runner.STAGE_ERROR_NAME: runner.StageError},
    )
    assert "no target stages" in fn("c")[1]


@pytest.mark.parametrize(
    ("source", "answer"),
    [
        ("def run(value):\n    return {'twice': value * 2}\n", {"output": {"twice": 6}}),
        ("def run(value):\n    raise ValueError('bad input')\n", {"error": "ValueError: bad input"}),
    ],
)
def test_stage_mode_runs_the_entry_function(tmp_path: Path, source: str, answer: dict[str, Any]) -> None:
    """``--stage`` loads ``path:function`` from the tree and writes its output or its error."""
    tree = tmp_path / "tree"
    (tree / "pkg").mkdir(parents=True)
    (tree / "pkg" / "stage.py").write_text(source)
    call_dir = tmp_path / "calls" / "one"
    call_dir.mkdir(parents=True)
    (call_dir / runner.INPUT_FILE).write_text("3")
    completed = subprocess.run(
        [sys.executable, str(Path(runner.__file__)), runner.STAGE_FLAG, str(call_dir), "pkg/stage.py:run"],
        cwd=tree,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads((call_dir / runner.OUTPUT_FILE).read_text()) == answer


def test_service_proxy_relays_each_request_through_the_parent() -> None:
    """A request to the loopback url becomes one relay message, and the parent's answer comes back unchanged."""

    class Client:
        """Answer each message the way the parent relay does."""

        def __init__(self) -> None:
            """Start with no messages."""
            self.sent: list[dict[str, Any]] = []

        def exchange(self, message: dict[str, Any]) -> dict[str, Any]:
            """Record the message and answer it.

            Args:
                message: The request the proxy sends.

            Returns:
                A service response, or a refusal for the second service.
            """
            self.sent.append(message)
            if message["service"] == "blocked one":
                return {"status": 403, "error": "Service 'blocked one' is not listed for the [run] role."}
            return {"status": 201, "headers": [["X-Upstream", "yes"]], "body": base64.b64encode(b"graded").decode()}

    client = Client()
    urls = runner.start_service_proxy(client, ["judge-api", "blocked one"])  # type: ignore[arg-type]
    assert set(urls) == {"SKYNET_SERVICE_JUDGE_API_URL", "SKYNET_SERVICE_BLOCKED_ONE_URL"}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(urls["SKYNET_SERVICE_JUDGE_API_URL"] + "v1/grade?x=1", data=b"hi", method="POST")
    with opener.open(request) as response:
        assert response.status == 201
        assert response.headers["X-Upstream"] == "yes"
        assert response.read() == b"graded"
    [message] = client.sent
    assert message["service"] == "judge-api"
    assert message["path"] == "v1/grade?x=1"
    assert message["method"] == "POST"
    assert base64.b64decode(message["body"]) == b"hi"
    with pytest.raises(urllib.error.HTTPError) as refused:
        opener.open(urls["SKYNET_SERVICE_BLOCKED_ONE_URL"] + "x")
    assert refused.value.code == 403
    assert b"not listed for the [run] role" in refused.value.read()


def test_service_proxy_decodes_a_chunked_body_and_sends_each_header_once() -> None:
    """A chunked request body reaches the parent decoded, and the service's Date and Server come back once."""

    class Client:
        """Answer each message with the service's own Date and Server."""

        def __init__(self) -> None:
            """Start with no messages."""
            self.sent: list[dict[str, Any]] = []

        def exchange(self, message: dict[str, Any]) -> dict[str, Any]:
            """Record the message and answer it.

            Args:
                message: The request the proxy sends.

            Returns:
                A service response with its own Date and Server headers.
            """
            self.sent.append(message)
            headers = [["Date", "Mon, 02 Nov 2026 10:00:00 GMT"], ["Server", "judge"]]
            return {"status": 200, "headers": headers, "body": base64.b64encode(b"ok").decode()}

    client = Client()
    url = urllib.parse.urlsplit(runner.start_service_proxy(client, ["judge"])["SKYNET_SERVICE_JUDGE_URL"])  # type: ignore[arg-type]
    connection = http.client.HTTPConnection(url.hostname, url.port, timeout=10)
    connection.putrequest("POST", url.path + "v1/grade")
    connection.putheader("Transfer-Encoding", "chunked")
    connection.endheaders()
    for chunk in (b"hello ", b"chunked world"):
        connection.send(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
    connection.send(b"0\r\nX-Trailer: 1\r\n\r\n")
    response = connection.getresponse()
    assert response.status == 200
    assert response.read() == b"ok"
    assert response.headers.get_all("Date") == ["Mon, 02 Nov 2026 10:00:00 GMT"]
    assert response.headers.get_all("Server") == ["judge"]
    connection.close()
    [message] = client.sent
    assert base64.b64decode(message["body"]) == b"hello chunked world"
