"""Tests for the in-sandbox runner: the stdlib-only module shipped into every scorer box."""

from __future__ import annotations

import base64
import http.client
import io
import json
import os
import shlex
import shutil
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

try:
    import openascent
except ImportError:  # the package needs Python 3.12
    openascent = None

from core.sandbox.providers import vercel

from .. import runner
from ..repo_constraints import McpServer, RoleEnvironment
from ..repo_services import box_mcp_config
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

_needs_openascent = pytest.mark.skipif(openascent is None, reason="openascent needs Python 3.12")
# The OpenAscent package folder a command entry gets, as the parent copies it into a box (SKY-757).
_PACKAGE = str(Path(openascent.__file__).parent) if openascent is not None else ""


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
PNG_DATA_URL = "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode("ascii")
_LLM_SCORER = (
    "def score(candidate, case=None):\n"
    "    return {'score': float(llm(candidate, case['input'])), 'feedback': 'judged ' + candidate}\n"
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


# One row for each row of the SKY-769 decision table, plus the edges of each rule.
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"score": 0.8, "feedback": "Missing the date."}, (0.8, {"feedback": "Missing the date."})),
        ({"score": 0.8}, (0.8, {"feedback": ""})),
        ({"score": 0.8, "feedback": ""}, (0.8, {"feedback": ""})),
        ({"score": 0.8, "feedback": None}, (0.8, {"feedback": ""})),
        ({"score": 0.8, "feedback": "   "}, (0.8, {"feedback": ""})),
        (
            {"score": 0.8, "scores": {"format": {"score": 1.0}}},
            (0.8, {"feedback": "", "scores": {"format": {"score": 1.0, "feedback": ""}}}),
        ),
        (
            {"score": 1, "feedback": "f", "scores": {"a": {"score": 1, "feedback": "x"}, "b": {"score": 0}}},
            (
                1.0,
                {
                    "feedback": "f",
                    "scores": {"a": {"score": 1.0, "feedback": "x"}, "b": {"score": 0.0, "feedback": ""}},
                },
            ),
        ),
    ],
)
def test_normalize_score_accepts_the_answer_object(raw: Any, expected: tuple[float, dict[str, Any]]) -> None:
    """A valid answer object normalizes to ``(score, side_info)``; no feedback is stored as ``""``.

    Args:
        raw: What the metric returned.
        expected: The normalized pair.
    """
    assert normalize_score(raw) == expected


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        (0.8, "must be an object, not a bare number"),
        (True, "must be an object, not a bare number"),
        ((0.8, "Close."), "must be an object, not a pair"),
        ((0.8, {"feedback": "Close."}), "must be an object, not a pair"),
        ([0.8, "Close."], "must be an object, not a pair"),
        (_WithScore(), "must be an object, not _WithScore"),
        (None, "must be an object, not NoneType"),
        ("0.8", "must be an object, not str"),
        ({"score": 0.8, "scores": {"format": 1.0}}, "A named score is an object.*'format' is a bare number"),
        ({"score": 0.8, "scores": {"format": [1.0, "ok"]}}, "A named score is an object.*'format' is a pair"),
        ({"score": 0.8, "scores": [1.0]}, '"scores" must be an object'),
        ({"score": 0.8, "predicted": 41}, "unknown key `predicted`"),
        ({"score": 0.8, "cases": {"a": {"score": 1.0}}}, "unknown key `cases`. Use `scores`."),
        ({"score": 0.8, "feedbak": "OK"}, "unknown key `feedbak`. Did you mean `feedback`?"),
        ({"score": 0.8, "scroes": {}}, "unknown key `scroes`. Did you mean `scores`?"),
        ({"score": 0.8, "status": "run_failed"}, "unknown key `status`"),
        ({"score": "high"}, 'The "score" of the metric answer must be a finite number, not str'),
        ({"score": None}, "must be a finite number, not NoneType"),
        ({"score": True}, "must be a finite number, not bool"),
        ({"score": float("nan")}, "must be a finite number, not nan"),
        ({"score": float("inf")}, "must be a finite number, not inf"),
        ({"feedback": "no score"}, 'has no "score"'),
        ({"score": 0.8, "feedback": ["a"]}, "feedback of the metric answer must be text, not list"),
        ({"score": 0.8, "scores": {"a": {"score": 1, "feedbak": "x"}}}, "named score 'a' has the unknown key"),
        ({"score": 0.8, "scores": {"a": {"score": 1, "scores": {}}}}, "named score 'a' has the unknown key `scores`"),
        ({"score": 0.8, "scores": {"a": {"feedback": "x"}}}, "named score 'a' has no \"score\""),
        ({"score": 0.8, "scores": {"a": {"score": "high"}}}, "named score 'a' must be a finite number"),
        ({"score": 0.8, "scores": {"a": {"score": 1, "feedback": 3}}}, "must be text, not int"),
    ],
)
def test_normalize_score_rejects_every_other_answer(raw: Any, match: str) -> None:
    """Every other answer is an error that names the wrong part and shows the valid form.

    Args:
        raw: An answer that does not have the one valid form.
        match: A part of the expected message.
    """
    with pytest.raises(ScorerError, match=match) as caught:
        normalize_score(raw)
    assert str(caught.value).endswith(runner.ANSWER_FORM)


def test_normalize_score_keeps_skynet_keys_only_when_asked() -> None:
    """``own_keys`` reads a document Skynet built, which may hold its own keys after the check."""
    assert normalize_score({"score": 0.5, "status": "x", "error": "e"}, own_keys=True) == (
        0.5,
        {"status": "x", "error": "e", "feedback": ""},
    )
    with pytest.raises(ScorerError, match="must be an object, not a pair"):
        normalize_score((0.5, "f"), own_keys=True)


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
    """``from openascent import llm, Image`` binds the same helpers as the injected names."""
    code = "from openascent import llm, Image\n\ndef score(c, x=None):\n    return (llm(c), Image.__name__)\n"

    fn = load_scorer_from_code(code, helpers={"llm": lambda *a, **k: "ok", "Image": Image})

    assert fn("x") == ("ok", "Image")


def test_load_scorer_ignores_helpers_imported_from_openascent() -> None:
    """A helper pulled in through the import is no more the user's function than an injected one."""
    code = "from openascent import llm\ndef judge(c, x=None): return llm(c)\n"

    assert load_scorer_from_code(code, helpers={"llm": lambda *a, **k: 1.0})("x") == 1.0


def test_openascent_module_follows_the_helpers_of_the_latest_load() -> None:
    """Each load rebuilds the module, so a scorer importing lazily sees the helpers of its own run."""
    code = "def score(c, x=None):\n    from openascent import llm\n    return llm(c)\n"

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
            "code": "def score(c): return {'score': float(llm(c, reasoning_effort='high'))}",
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
        code = f"def score(c):\n return {{'score': float(llm(c, model={name!r}, reasoning_effort='high'))}}"
        explicit = run_call({**base, "code": code})
        default = run_call({**base, "code": "def score(c):\n return {'score': float(llm(c))}"})
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
    """The output carries the score, the checked side info and an empty usage list."""
    code = (
        "def score(candidate, case):\n"
        "    return {'score': len(candidate) / 10, 'feedback': 'f', 'scores': {'length': {'score': len(candidate)}}}\n"
    )

    result = run_call({"code": code, "candidate": "hello", "case": {"x": 1}, "gateway": None})

    assert result == {
        "score": 0.5,
        "side_info": {"feedback": "f", "scores": {"length": {"score": 5.0, "feedback": ""}}},
        "error": None,
        "usage": [],
    }


def test_run_call_passes_case_only_when_the_function_takes_one() -> None:
    """A ``score(candidate)`` scorer is called without the case; ``score(candidate, case)`` gets it."""
    scores = [
        run_call({"code": code, "candidate": "abc", "case": {"weight": 2}})["score"]
        for code in (
            "def score(candidate): return {'score': len(candidate)}",
            "def score(candidate, case): return {'score': case['weight'] * len(candidate)}",
            "def score(*args): return {'score': len(args)}",
        )
    ]

    assert scores == [3.0, 6.0, 2.0]


@pytest.mark.parametrize(
    ("code", "error"),
    [
        ("def score(c, case=None): raise ValueError('bad candidate')", "ValueError: bad candidate"),
        ("def score(c, case=None): raise SystemExit(3)", "SystemExit: 3"),
        ("def score(c, case=None): return 'nope'", "must be an object, not str"),
        ("def score(c, case=None): return 0.5", "must be an object, not a bare number"),
        ("def score(c, case=None): return 0.5, 'f'", "must be an object, not a pair"),
        ("def score(c, case=None): return {'score': 0.5, 'feedbak': 'f'}", "Did you mean `feedback`?"),
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
        "side_info": {"feedback": "judged judge"},
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
        "code": "def score(candidate, case=None):\n    return {'score': 0.5, 'feedback': 'f'}\n",
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
        "side_info": {"feedback": "f"},
        "error": None,
        "usage": [],
    }
    assert not (call_dir / (runner.OUTPUT_FILE + ".part")).exists()


def test_an_isolated_runner_imports_user_site_packages_without_their_pth_files(tmp_path: Path) -> None:
    """Under ``python3 -I`` a metric still imports a user-site dependency, and no ``.pth`` file there runs (E38, E40)."""
    base = getattr(sys, "_base_executable", sys.executable)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    folder = subprocess.run(
        [base, "-c", "import site; print(site.getusersitepackages())"],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    ).stdout.strip()
    user_site = Path(folder)
    user_site.mkdir(parents=True)
    (user_site / "userdep.py").write_text("VALUE = 0.5\n", encoding="utf-8")
    marker = tmp_path / "pth-ran"
    (user_site / "plant.pth").write_text(f"import pathlib; pathlib.Path({str(marker)!r}).write_text('x')\n")
    call_dir = tmp_path / "calls" / "000001"
    call_dir.mkdir(parents=True)
    payload = {"code": "import userdep\ndef score(candidate, case=None):\n    return {'score': userdep.VALUE}\n"}
    (call_dir / runner.INPUT_FILE).write_text(json.dumps({**payload, "candidate": "x", "case": None}), encoding="utf-8")

    completed = subprocess.run(
        [base, "-I", "-B", runner.__file__, str(call_dir)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=tmp_path,
        env=env,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads((call_dir / runner.OUTPUT_FILE).read_text(encoding="utf-8"))["score"] == 0.5
    assert not marker.exists()


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


def test_scorer_code_imports_log_from_openascent() -> None:
    """``from openascent import log`` works in the box and in validation alike."""
    fn = load_scorer_from_code(
        "from openascent import log\ndef score(c, case=None):\n    return {'score': 1.0}\n", helpers={"log": log}
    )
    assert fn("x") == {"score": 1.0}


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


def test_stage_client_yields_each_streamed_part_and_removes_its_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An answer with ``"more": true`` is followed by ``<id>.1.json`` and on, until a part has ``"more": false``."""

    class Relay(io.StringIO):
        """Write the answer and both parts for each frame."""

        def write(self, text: str) -> int:
            """Write the streamed answer files for one frame.

            Args:
                text: One frame, on its own line.

            Returns:
                The number of characters taken.
            """
            identity = json.loads(text.strip().removeprefix("PFX:"))["id"]
            (tmp_path / f"{identity}.json").write_text(json.dumps({"status": 200, "more": True}))
            (tmp_path / f"{identity}.1.json").write_text(json.dumps({"body": "YQ==", "more": True}))
            (tmp_path / f"{identity}.2.json").write_text(json.dumps({"more": False}))
            return len(text)

    monkeypatch.setattr(sys, "__stdout__", Relay())
    client = runner.StageClient({"prefix": "PFX:", "response_dir": str(tmp_path), "names": []})
    assert list(client.answers({"mcp": "search"})) == [
        {"status": 200, "more": True},
        {"body": "YQ==", "more": True},
        {"more": False},
    ]
    assert list(tmp_path.iterdir()) == []


def test_stage_client_tells_the_parent_to_stop_a_stream_it_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Closing the answers before the end part sends a stop frame with the stream's id; a whole answer sends none."""
    frames: list[dict[str, Any]] = []

    class Relay(io.StringIO):
        """Record each frame, and answer a request frame with a stream that has not ended."""

        def write(self, text: str) -> int:
            """Record one frame and write its first answer files.

            Args:
                text: One frame, on its own line.

            Returns:
                The number of characters taken.
            """
            frame = json.loads(text.strip().removeprefix("PFX:"))
            frames.append(frame)
            if "stop" not in frame:
                more = frame["mcp"] == "events"
                (tmp_path / f"{frame['id']}.json").write_text(json.dumps({"status": 200, "more": more}))
                (tmp_path / f"{frame['id']}.1.json").write_text(json.dumps({"body": "YQ==", "more": True}))
            return len(text)

    monkeypatch.setattr(sys, "__stdout__", Relay())
    client = runner.StageClient({"prefix": "PFX:", "response_dir": str(tmp_path), "names": []})
    answers = client.answers({"mcp": "events"})
    assert next(answers)["more"] is True
    assert next(answers) == {"body": "YQ==", "more": True}
    answers.close()
    whole = client.answers({"mcp": "search"})
    assert next(whole) == {"status": 200, "more": False}
    whole.close()
    request, stop, plain = frames
    assert stop["stop"] == request["id"]
    assert len(stop["id"]) == 32
    assert stop["id"] != request["id"]
    assert "stop" not in plain


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
        "from openascent import call, StageError\n"
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


def _stage_call(tmp_path: Path, port: int, call: str = "one", mcp: tuple[str, ...] = ("search",)) -> tuple[Path, Path]:
    """Lay out a stage tree and one call folder whose relay config names its MCP servers.

    Args:
        tmp_path: The test's folder, which stands for the stage box.
        port: The relay port the config asks for.
        call: The call's folder name.
        mcp: The MCP servers the config names.

    Returns:
        The stage tree and the call folder.
    """
    tree = tmp_path / "tree"
    tree.mkdir(exist_ok=True)
    call_dir = tmp_path / "calls" / call
    call_dir.mkdir(parents=True)
    (call_dir / runner.INPUT_FILE).write_text("3")
    config = {
        "prefix": "APP_STAGE_test:",
        "response_dir": "../relay",
        "names": [],
        "services": [],
        "mcp": list(mcp),
        "mcp_file": ".mcp.json",
        "port": port,
    }
    (call_dir / runner.STAGE_RELAY_FILE).write_text(json.dumps(config))
    return tree, call_dir


def test_stage_mode_serves_the_stage_relay_and_names_the_calls_own_mcp_file(tmp_path: Path) -> None:
    """``--stage`` with a relay config gives the entry its MCP variables and the call's own MCP file (SKY-624)."""
    tree, call_dir = _stage_call(tmp_path, 0)
    (tree / "stage.py").write_text(
        "import os\n"
        "def run(value):\n"
        "    return {key: os.environ[key] for key in ('ASCENT_MCP_SEARCH_URL', 'ASCENT_MCP_CONFIG')}\n"
    )
    completed = subprocess.run(
        [sys.executable, str(Path(runner.__file__)), runner.STAGE_FLAG, str(call_dir), "stage.py:run"],
        cwd=tree,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    env = json.loads((call_dir / runner.OUTPUT_FILE).read_text())["output"]
    url = urllib.parse.urlsplit(env["ASCENT_MCP_SEARCH_URL"])
    assert url.hostname == "127.0.0.1"
    assert url.port
    assert url.path == "/mcp/search"
    assert env["ASCENT_MCP_CONFIG"] == str((call_dir / "mcp.json").resolve())
    assert json.loads((call_dir / "mcp.json").read_text()) == {
        "mcpServers": {"search": {"type": "http", "url": env["ASCENT_MCP_SEARCH_URL"]}}
    }


def test_two_parallel_stage_calls_in_one_box_each_get_their_own_port_and_mcp_file(tmp_path: Path) -> None:
    """Two calls that run at the same time in one stage box serve their relays on two ports (SKY-624)."""
    entry = (
        "import json, os, time\n"
        "def run(value):\n"
        "    time.sleep(1.0)\n"
        "    path = os.environ['ASCENT_MCP_CONFIG']\n"
        "    return {'url': os.environ['ASCENT_MCP_SEARCH_URL'], 'file': path, 'config': json.load(open(path))}\n"
    )
    calls = [_stage_call(tmp_path, 0, call) for call in ("one", "two")]
    tree = calls[0][0]
    (tree / "stage.py").write_text(entry)
    processes = [
        subprocess.Popen(
            [sys.executable, str(Path(runner.__file__)), runner.STAGE_FLAG, str(call_dir), "stage.py:run"],
            cwd=tree,
            stderr=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
        )
        for _, call_dir in calls
    ]
    for process in processes:
        _, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
    outputs = [json.loads((call_dir / runner.OUTPUT_FILE).read_text())["output"] for _, call_dir in calls]

    ports = {urllib.parse.urlsplit(output["url"]).port for output in outputs}
    assert len(ports) == 2
    for (_, call_dir), output in zip(calls, outputs, strict=True):
        assert output["file"] == str((call_dir / "mcp.json").resolve())
        assert output["config"] == {"mcpServers": {"search": {"type": "http", "url": output["url"]}}}


def test_a_calls_own_mcp_file_keeps_servers_whose_variable_names_collide(tmp_path: Path) -> None:
    """Servers "a-b" and "a_b" share one variable name, so the call's MCP file must name each one's own URL."""
    tree, call_dir = _stage_call(tmp_path, 0, mcp=("a-b", "a_b"))
    (tree / "stage.py").write_text("def run(value):\n    return value\n")
    completed = subprocess.run(
        [sys.executable, str(Path(runner.__file__)), runner.STAGE_FLAG, str(call_dir), "stage.py:run"],
        cwd=tree,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    servers = json.loads((call_dir / "mcp.json").read_text())["mcpServers"]
    assert {name: urllib.parse.urlsplit(entry["url"]).path for name, entry in servers.items()} == {
        "a-b": "/mcp/a-b",
        "a_b": "/mcp/a_b",
    }


@_needs_openascent
def test_stage_command_mode_serves_the_relay_and_uses_the_two_files(tmp_path: Path) -> None:
    """``--stage-command`` gives the command ``ASCENT_INPUT`` and the relay's variables, and keeps its ``output`` (SKY-757)."""
    tree, call_dir = _stage_call(tmp_path, 0)
    script = (
        "import json, os, sys\n"
        "assert sys.stdin.read() == ''\n"
        "print('a debug line')\n"
        "value = json.load(open(os.environ['ASCENT_INPUT']))\n"
        "answer = {'input': value, 'url': os.environ['ASCENT_MCP_SEARCH_URL']}\n"
        "json.dump({'output': answer}, open(os.environ['ASCENT_OUTPUT'], 'w'))\n"
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(runner.__file__)),
            runner.STAGE_COMMAND_FLAG,
            str(call_dir),
            _PACKAGE,
            "30",
            sys.executable,
            "-c",
            script,
        ],
        cwd=tree,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "a debug line" in completed.stderr
    written = json.loads((call_dir / runner.OUTPUT_FILE).read_text())["output"]
    assert written["input"] == {"input": 3}
    assert written["url"].startswith("http://127.0.0.1:")
    assert written["url"].endswith("/mcp/search")


@_needs_openascent
def test_stage_command_mode_without_a_relay_config_sets_no_relay_variable(tmp_path: Path) -> None:
    """A call with no relay config runs the command with the box's own environment only."""
    call_dir = tmp_path / "call"
    call_dir.mkdir()
    (call_dir / runner.INPUT_FILE).write_text("{}")
    script = (
        "import json, os\n"
        "names = sorted(k for k in os.environ if k.startswith('ASCENT_MCP'))\n"
        "json.dump({'output': names}, open(os.environ['ASCENT_OUTPUT'], 'w'))\n"
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(runner.__file__)),
            runner.STAGE_COMMAND_FLAG,
            str(call_dir),
            _PACKAGE,
            "30",
            sys.executable,
            "-c",
            script,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        env={key: value for key, value in os.environ.items() if not key.startswith("ASCENT_MCP")},
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads((call_dir / runner.OUTPUT_FILE).read_text()) == {"output": []}


@_needs_openascent
def test_a_failed_stage_command_writes_the_package_error(tmp_path: Path) -> None:
    """A command stage that exits nonzero writes ``{"error": ...}`` with the cause and its stderr tail (SKY-757)."""
    call_dir = tmp_path / "call"
    call_dir.mkdir()
    (call_dir / runner.INPUT_FILE).write_text("null")
    completed = subprocess.run(
        [
            *(sys.executable, runner.__file__, runner.STAGE_COMMAND_FLAG, str(call_dir), _PACKAGE, "30"),
            *("sh", "-c", "echo broken >&2; exit 4"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    document = json.loads((call_dir / runner.OUTPUT_FILE).read_text())
    assert document["error"].startswith("The command exited with code 4.")
    assert document["stderr"] == "broken\n"


def test_a_command_error_drops_a_split_secret_at_the_edge_of_a_log_the_box_cut() -> None:
    """The box's cut can leave the end of a secret first; the parent drops it before it masks (SKY-757).

    The repeated secret masks down to less than ``COMMAND_LOG_CHARS``, so
    only the drop keeps the split copy out of the error.
    """
    secret = "tok-" + "".join(f"{i:03d}" for i in range(40))
    log = (secret * 60)[-4_000:]
    assert not log.startswith(secret)
    document = {"error": "The command exited with code 1.", "stderr": log, "stderr_cut": True}
    error = runner.command_error(document, lambda text: text.replace(secret, "[secret]"), [secret, ""])
    assert error.startswith("The command exited with code 1.\nstderr: [secret][secret]")
    assert not any(secret[i : i + 8] in error for i in range(len(secret) - 7))


@_needs_openascent
def test_the_box_marks_a_log_it_cut(tmp_path: Path) -> None:
    """A command log over the box's bound reaches the parent cut, with a ``_cut`` mark (SKY-757)."""
    call_dir = tmp_path / "call"
    call_dir.mkdir()
    (call_dir / runner.INPUT_FILE).write_text("null")
    script = "import sys; sys.stderr.write('e' * 5000); print('short'); sys.exit(1)"
    completed = subprocess.run(
        [
            *(sys.executable, runner.__file__, runner.STAGE_COMMAND_FLAG, str(call_dir), _PACKAGE, "30"),
            *(sys.executable, "-c", script),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    document = json.loads((call_dir / runner.OUTPUT_FILE).read_text())
    assert document["stderr"] == "e" * runner._COMMAND_LOG_BOUND
    assert document["stderr_cut"] is True
    assert document["stdout"] == "short\n"
    assert "stdout_cut" not in document


@_needs_openascent
def test_the_box_logs_stdout_then_stderr_on_separate_lines(tmp_path: Path) -> None:
    """The box writes a line break between a stdout without one and the stderr (SKY-757)."""
    call_dir = tmp_path / "call"
    call_dir.mkdir()
    (call_dir / runner.INPUT_FILE).write_text("null")
    script = "import sys; sys.stdout.write('out-text'); sys.stderr.write('err-text'); sys.exit(1)"
    completed = subprocess.run(
        [
            *(sys.executable, runner.__file__, runner.STAGE_COMMAND_FLAG, str(call_dir), _PACKAGE, "30"),
            *(sys.executable, "-c", script),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "out-text\nerr-text" in completed.stderr


@_needs_openascent
def test_the_box_logs_no_part_of_a_secret_that_the_package_tail_cut_splits(tmp_path: Path) -> None:
    """OpenAscent keeps the last 64 KiB of a log; the box drops the cut first line before it logs (SKY-757)."""
    secret = "test-value-1234567890abcdef"
    call_dir = tmp_path / "call"
    call_dir.mkdir()
    (call_dir / runner.INPUT_FILE).write_text("null")
    # The tail starts 12 characters before the end of the secret.
    after = 64 * 1024 - 12
    script = f"import sys; sys.stderr.write('a' * 100 + {secret!r} + '\\n' + 'b' * {after - 1}); sys.exit(1)"
    completed = subprocess.run(
        [
            *(sys.executable, runner.__file__, runner.STAGE_COMMAND_FLAG, str(call_dir), _PACKAGE, "30"),
            *(sys.executable, "-c", script),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[:200]
    assert "b" * 1_000 in completed.stderr
    assert not [i for i in range(len(secret) - 7) if secret[i : i + 8] in completed.stderr]


def test_a_command_error_cuts_a_long_cause_so_both_tails_fit_the_run_feedback() -> None:
    """A long cause keeps its first ``COMMAND_CAUSE_CHARS``, after the mask (SKY-757)."""
    secret = "secret-value-9876"
    cause = "x" * (runner.COMMAND_CAUSE_CHARS - 5) + secret + "y" * 1_000
    document = {"error": cause, "stdout": "o" * 1_000, "stderr": "e" * 1_000}
    error = runner.command_error(document, lambda text: text.replace(secret, "[secret]"), [secret])
    head, stderr, stdout = error.split("\n")
    assert head == ("x" * (runner.COMMAND_CAUSE_CHARS - 5) + "[secret]")[: runner.COMMAND_CAUSE_CHARS - 1] + "…"
    assert "secr" not in error
    assert (stderr, stdout) == (
        "stderr: " + "e" * runner.COMMAND_LOG_CHARS,
        "stdout: " + "o" * runner.COMMAND_LOG_CHARS,
    )
    assert len(error) <= 2_000 - len("Run failed: ")


def test_a_command_error_masks_each_log_before_it_cuts_it() -> None:
    """The cause comes first, then each masked log's last ``COMMAND_LOG_CHARS``, stderr first (SKY-757)."""
    secret = "secret-value-9876"
    document = {"error": "The command exited with code 1.", "stdout": "out", "stderr": secret + "x" * 795}
    error = runner.command_error(document, lambda text: text.replace(secret, "[secret]"), [secret])
    cause, stderr, stdout = error.split("\n")
    assert cause == "The command exited with code 1."
    assert stderr == "stderr: " + ("[secret]" + "x" * 795)[-runner.COMMAND_LOG_CHARS :]
    assert stdout == "stdout: out"
    assert "9876" not in error


def test_a_stage_command_without_the_package_writes_an_error(tmp_path: Path) -> None:
    """A box that has no package folder fails the call with a plain reason, not a crash."""
    call_dir = tmp_path / "call"
    call_dir.mkdir()
    (call_dir / runner.INPUT_FILE).write_text("null")
    code = runner.run_stage_command(call_dir, str(tmp_path / "missing"), 5, ["true"])
    assert code == 0
    error = json.loads((call_dir / runner.OUTPUT_FILE).read_text())["error"]
    assert error == "ScorerError: The box has no OpenAscent package, so the command cannot run."


def test_service_proxy_relays_each_request_through_the_parent() -> None:
    """A request to the loopback url becomes one relay message, and the parent's answer comes back unchanged."""

    class Client:
        """Answer each message the way the parent relay does."""

        def __init__(self) -> None:
            """Start with no messages."""
            self.sent: list[dict[str, Any]] = []

        def answers(self, message: dict[str, Any]) -> Iterator[dict[str, Any]]:
            """Record the message and answer it.

            Args:
                message: The request the proxy sends.

            Yields:
                A service response, or a refusal for the second service.
            """
            self.sent.append(message)
            if message["service"] == "blocked one":
                yield {"status": 403, "error": "Service 'blocked one' is not listed for the [run] role."}
                return
            yield {"status": 201, "headers": [["X-Upstream", "yes"]], "body": base64.b64encode(b"graded").decode()}

    client = Client()
    urls = runner.start_service_proxy(client, ["judge-api", "blocked one"])  # type: ignore[arg-type]
    assert set(urls) == {"ASCENT_SERVICE_JUDGE_API_URL", "ASCENT_SERVICE_BLOCKED_ONE_URL"}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(urls["ASCENT_SERVICE_JUDGE_API_URL"] + "v1/grade?x=1", data=b"hi", method="POST")
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
        opener.open(urls["ASCENT_SERVICE_BLOCKED_ONE_URL"] + "x")
    assert refused.value.code == 403
    assert b"not listed for the [run] role" in refused.value.read()


def test_service_proxy_decodes_a_chunked_body_and_sends_each_header_once() -> None:
    """A chunked request body reaches the parent decoded, and the service's Date and Server come back once."""

    class Client:
        """Answer each message with the service's own Date and Server."""

        def __init__(self) -> None:
            """Start with no messages."""
            self.sent: list[dict[str, Any]] = []

        def answers(self, message: dict[str, Any]) -> Iterator[dict[str, Any]]:
            """Record the message and answer it.

            Args:
                message: The request the proxy sends.

            Yields:
                A service response with its own Date and Server headers.
            """
            self.sent.append(message)
            headers = [["Date", "Mon, 02 Nov 2026 10:00:00 GMT"], ["Server", "judge"]]
            yield {"status": 200, "headers": headers, "body": base64.b64encode(b"ok").decode()}

    client = Client()
    url = urllib.parse.urlsplit(runner.start_service_proxy(client, ["judge"])["ASCENT_SERVICE_JUDGE_URL"])  # type: ignore[arg-type]
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


def test_mcp_proxy_passes_an_event_stream_on_part_by_part() -> None:
    """A streamed MCP answer reaches the box as one body with no length, each part in order, and then the close."""

    class Client:
        """Answer with an event stream in two parts."""

        def answers(self, message: dict[str, Any]) -> Iterator[dict[str, Any]]:
            """Yield the head and each part the way the parent relay writes them.

            Args:
                message: The request the proxy sends.

            Yields:
                The streamed answer, then its parts.
            """
            headers = [["Content-Type", "text/event-stream"]]
            yield {"status": 200, "headers": headers, "body": base64.b64encode(b"data: one\n\n").decode(), "more": True}
            yield {"body": base64.b64encode(b"data: two\n\n").decode(), "more": True}
            yield {"more": False}

    url = runner.start_service_proxy(Client(), [], ["search"])["ASCENT_MCP_SEARCH_URL"]  # type: ignore[arg-type]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url) as response:
        assert response.status == 200
        assert response.headers["Content-Type"] == "text/event-stream"
        assert response.headers["Content-Length"] is None
        assert response.read() == b"data: one\n\ndata: two\n\n"


def test_mcp_proxy_closes_the_answers_when_the_client_leaves() -> None:
    """A client that leaves an endless event stream makes the proxy close its answers, so the parent stops the stream."""
    closed = threading.Event()

    class Client:
        """Answer with an event stream that never ends."""

        def answers(self, message: dict[str, Any]) -> Iterator[dict[str, Any]]:
            """Yield the head, then parts until the proxy closes the generator.

            Args:
                message: The request the proxy sends.

            Yields:
                The streamed answer, then its parts.
            """
            part = base64.b64encode(b"data: " + b"x" * 65536 + b"\n\n").decode()
            try:
                yield {"status": 200, "headers": [["Content-Type", "text/event-stream"]], "body": "", "more": True}
                while True:
                    time.sleep(0.01)
                    yield {"body": part, "more": True}
            except GeneratorExit:
                closed.set()
                raise

    url = urllib.parse.urlsplit(runner.start_service_proxy(Client(), [], ["search"])["ASCENT_MCP_SEARCH_URL"])  # type: ignore[arg-type]
    connection = http.client.HTTPConnection(url.hostname, url.port, timeout=10)
    connection.request("GET", url.path)
    response = connection.getresponse()
    assert response.status == 200
    response.read(10)
    response.close()
    connection.close()
    assert closed.wait(10)


def test_mcp_proxy_relays_each_server_and_names_the_role_mcp_file() -> None:
    """``/mcp/<name>`` becomes one ``mcp`` message, and the box's MCP variable names the role's own file (E49, E55)."""

    class Client:
        """Answer each message the way the parent relay does."""

        def __init__(self) -> None:
            """Start with no messages."""
            self.sent: list[dict[str, Any]] = []

        def answers(self, message: dict[str, Any]) -> Iterator[dict[str, Any]]:
            """Record the message and answer it.

            Args:
                message: The request the proxy sends.

            Yields:
                An MCP server response.
            """
            self.sent.append(message)
            yield {
                "status": 200,
                "headers": [["Content-Type", "application/json"]],
                "body": base64.b64encode(b"{}").decode(),
            }

    client = Client()
    env = runner.start_service_proxy(client, [], ["search tool"], mcp_file=".mcp.json")  # type: ignore[arg-type]
    assert set(env) == {"ASCENT_MCP_SEARCH_TOOL_URL", runner.MCP_CONFIG_ENV}
    url = env["ASCENT_MCP_SEARCH_TOOL_URL"]
    assert url.startswith("http://127.0.0.1:")
    assert url.endswith("/mcp/search%20tool")
    assert env[runner.MCP_CONFIG_ENV] == str(Path(".mcp.json").resolve())
    role = RoleEnvironment(mcp=".mcp.json", mcp_servers=(McpServer(name="search tool", url="https://x.test/mcp"),))
    assert box_mcp_config(role, url.split("/mcp/")[0]) == {"mcpServers": {"search tool": {"type": "http", "url": url}}}

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(urllib.request.Request(url, data=b'{"jsonrpc":"2.0"}', method="POST")) as response:
        assert response.status == 200
        assert response.read() == b"{}"
    [message] = client.sent
    assert message["mcp"] == "search tool"
    assert "service" not in message
    assert message["path"] == ""
    assert base64.b64decode(message["body"]) == b'{"jsonrpc":"2.0"}'

    base = url.split("/mcp/")[0]
    with pytest.raises(urllib.error.HTTPError) as unknown:
        opener.open(base + "/other/search%20tool")
    assert unknown.value.code == 404
    assert len(client.sent) == 1


def test_relay_env_starts_no_proxy_when_the_role_reaches_nothing() -> None:
    """A role with no service and no MCP server gets no loopback variables."""
    assert runner._relay_env(object(), {"services": [], "mcp": []}) == {}  # type: ignore[arg-type]
    assert runner._relay_env(object(), {}) == {}  # type: ignore[arg-type]


def test_box_gets_only_the_ascent_helper_names() -> None:
    """The box env holds only ``ASCENT_*`` service and MCP names, and no old ``SKYNET_*`` name (SKY-768)."""

    class Client:
        """Answer nothing; the test only reads the env names."""

        def answers(self, message: dict[str, Any]) -> Iterator[dict[str, Any]]:
            """Answer no message.

            Args:
                message: The request the proxy sends.

            Yields:
                Nothing.
            """
            yield from ()

    env = runner.start_service_proxy(Client(), ["judge"], ["search"], mcp_file=".mcp.json")  # type: ignore[arg-type]
    assert set(env) == {"ASCENT_SERVICE_JUDGE_URL", "ASCENT_MCP_SEARCH_URL", "ASCENT_MCP_CONFIG"}
    assert runner.MCP_CONFIG_ENV == "ASCENT_MCP_CONFIG"
    assert not [name for name in env if name.startswith("SKYNET_")]


def test_metric_cannot_import_the_old_skynet_helper_module() -> None:
    """An import of ``call`` from the old ``skynet`` module fails; the ``openascent`` import works (SKY-768)."""
    helpers = {"call": lambda *a, **k: 1.0}
    old_module = "skynet"
    with pytest.raises(ScorerError, match="ModuleNotFoundError"):
        load_scorer_from_code(f"from {old_module} import call\ndef score(c, x=None): return call()\n", helpers=helpers)
    fn = load_scorer_from_code("from openascent import call\ndef score(c, x=None): return call()\n", helpers=helpers)
    assert fn("x") == 1.0
    assert runner.HELPER_MODULE_NAME == "openascent"
    assert "openascent" in sys.modules
    assert "skynet" not in sys.modules


def test_helper_module_and_copied_openascent_package_coexist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The helper module and the OpenAscent files a metric box gets both keep the ``openascent`` name (SKY-768, SKY-738).

    Args:
        tmp_path: Stands for the metric folder that holds the copied package.
        monkeypatch: Puts that folder on ``sys.path`` and restores ``sys.modules``.
    """
    package = tmp_path / "openascent"
    package.mkdir()
    (package / "__init__.py").write_text("COPIED = True\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setitem(sys.modules, "openascent", sys.modules.get("openascent"))
    code = (
        "import openascent\nfrom openascent import call\n"
        "def score(c, x=None): return call(), hasattr(openascent, 'COPIED')\n"
    )

    assert load_scorer_from_code(code, helpers={"call": lambda: 1.0})("x") == (1.0, False)
    copied = runner._load_openascent(package)
    assert copied.COPIED is True
    assert sys.modules["openascent"] is copied
    assert load_scorer_from_code(code, helpers={"call": lambda: 2.0})("x") == (2.0, False)


_CANDIDATE_CODE = """
import json
import subprocess
import sys
import time


def echo(value):
    return {"got": value}


def leaves_a_sleeper(value):
    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"], start_new_session=True)
    return value


def boom(value):
    raise ValueError("boom")


def not_json(value):
    return object()


def slow(value):
    time.sleep(30)


def fakes_its_time(value):
    # Patch every clock in this process and report a fake time; neither reaches the supervisor.
    time.sleep(value)
    for name in ("monotonic", "perf_counter", "time", "process_time"):
        setattr(time, name, lambda: 0.0)
    return {"elapsed_s": 0.0}
"""


def _candidate(
    tmp_path: Path, entry: Any, value: Any = None, timeout: float = 30, logs: list[str] | None = None
) -> dict[str, Any]:
    """Run the ``[run]`` supervisor once in a fresh folder, as the candidate box does.

    Args:
        tmp_path: Scratch folder; the candidate's code lives in ``cand.py`` there.
        entry: The ``[run]`` entry.
        value: The case input.
        timeout: The run timeout.
        logs: When given, gets the supervisor's stderr: the logs the box streams.

    Returns:
        The supervisor's output document.
    """
    (tmp_path / "cand.py").write_text(_CANDIDATE_CODE, encoding="utf-8")
    call_dir = tmp_path / "call"
    call_dir.mkdir(exist_ok=True)
    payload = {"entry": entry, "input": value, "timeout": timeout, "stages": None}
    if isinstance(entry, list):
        payload[runner.ASCENT_PACKAGE_PARAM] = _PACKAGE
    (call_dir / runner.INPUT_FILE).write_text(json.dumps(payload), encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, runner.__file__, runner.CANDIDATE_FLAG, str(call_dir)],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE if logs is not None else None,
        text=True,
        check=True,
        timeout=timeout + 60,
    )
    stdout = completed.stdout
    if logs is not None:
        logs.append(completed.stderr)
    [frame] = [line for line in stdout.splitlines() if line.startswith(runner.RESULT_PREFIX)]
    assert not (call_dir / runner.OUTPUT_FILE).exists()
    return json.loads(frame[len(runner.RESULT_PREFIX) :])


def test_a_function_entry_gets_the_input_and_returns_its_json_value(tmp_path: Path) -> None:
    """A ``path.py:function`` entry gets the case input; its JSON return value is the output."""
    ran = _candidate(tmp_path, "cand.py:echo", {"a": 1})
    assert ran.pop(runner.ELAPSED_PARAM) >= 0
    assert ran == {"output": {"got": {"a": 1}}, "error": None}


_TWO_FILE_COMMAND = (
    "import json, os, sys\n"
    "assert sys.stdin.read() == ''\n"
    "print('a debug line that is not the output')\n"
    "value = json.load(open(os.environ['ASCENT_INPUT']))\n"
    "answer = {'got': value, 'path': os.environ['ASCENT_OUTPUT']}\n"
    "json.dump({'output': answer}, open(os.environ['ASCENT_OUTPUT'], 'w'))\n"
)


@_needs_openascent
def test_a_command_entry_reads_ascent_input_and_writes_ascent_output(tmp_path: Path) -> None:
    """The command gets ``{"input": ...}`` in ``ASCENT_INPUT`` and an empty stdin; its file's ``output`` is the output (SKY-757).

    Its print is not part of the output, and each call gets a new output path.
    """
    first = _candidate(tmp_path, [sys.executable, "-c", _TWO_FILE_COMMAND], {"a": 1})
    second = _candidate(tmp_path, [sys.executable, "-c", _TWO_FILE_COMMAND], {"a": 1})
    assert first.pop(runner.ELAPSED_PARAM) >= 0
    path = first["output"].pop("path")
    assert first == {"output": {"got": {"input": {"a": 1}}}, "error": None}
    assert second["output"]["path"] != path


@_needs_openascent
def test_a_command_entry_with_no_cases_gets_a_null_input(tmp_path: Path) -> None:
    """A run with no cases writes ``{"input": null}`` to the input file."""
    ran = _candidate(tmp_path, ["sh", "-c", 'printf \'{"output": %s}\' "$(cat "$ASCENT_INPUT")" > "$ASCENT_OUTPUT"'])
    assert ran["output"] == {"input": None}


@_needs_openascent
def test_a_command_that_only_prints_has_no_output(tmp_path: Path) -> None:
    """A command that prints its answer but writes no output file fails with the package's exact reason and its stdout."""
    ran = _candidate(tmp_path, ["sh", "-c", "echo '{\"output\": 1}'"])
    assert ran["output"] is None
    assert ran["error"] == "The command wrote no output file. Write the result as JSON to the path in ASCENT_OUTPUT."
    assert ran["stdout"] == '{"output": 1}\n'


@_needs_openascent
def test_a_command_output_file_is_capped(tmp_path: Path) -> None:
    """An output file over ``MAX_RUN_STDOUT_BYTES`` fails the run."""
    script = "import json, os\njson.dump({'output': 'x' * 2_000_000}, open(os.environ['ASCENT_OUTPUT'], 'w'))\n"
    ran = _candidate(tmp_path, [sys.executable, "-c", script])
    assert ran["output"] is None
    assert f"larger than {runner.MAX_RUN_STDOUT_BYTES} bytes" in ran["error"]


@_needs_openascent
def test_a_command_output_near_the_limit_passes_the_supervisor_cap(tmp_path: Path) -> None:
    """An output file just under the package limit still passes after the child writes it again (SKY-757).

    ``1e15`` grows the most when it is written again, so the supervisor's cap must allow for it.
    """
    count = (runner.MAX_RUN_STDOUT_BYTES - 100) // 5
    script = (
        "import os\n"
        f"open(os.environ['ASCENT_OUTPUT'], 'w').write('{{\"output\": [' + ','.join(['1e15'] * {count}) + ']}}')\n"
    )
    ran = _candidate(tmp_path, [sys.executable, "-c", script])
    assert ran["error"] is None
    assert len(ran["output"]) == count


@_needs_openascent
@pytest.mark.parametrize(("char", "count"), [("\x7f", 1_000_000), ("\U0001f600", 250_000)], ids=["del", "emoji"])
def test_a_command_string_output_near_the_limit_passes_the_supervisor_cap(
    tmp_path: Path, char: str, count: int
) -> None:
    """A raw character that an ASCII-only dump would escape keeps its size in the answer (SKY-757).

    A DEL grew from 1 byte to 6, and an emoji from 4 bytes to 12.
    """
    script = (
        "import os\n"
        f"text = '{{\"output\": \"' + chr({ord(char)}) * {count} + '\"}}'\n"
        "open(os.environ['ASCENT_OUTPUT'], 'w', encoding='utf-8').write(text)\n"
    )
    ran = _candidate(tmp_path, [sys.executable, "-c", script])
    assert ran["error"] is None
    assert ran["output"] == char * count


def test_the_supervisor_refuses_a_command_answer_over_its_cap(tmp_path: Path) -> None:
    """A huge answer file in place of the child's own fails the call, and the supervisor reads no more than the cap."""
    path = tmp_path / runner.OUTPUT_FILE
    path.write_bytes(b'{"output": "' + b"x" * 200 + b'"}')
    assert runner._read_answer(path, None, 100) == {"error": "The command's answer file is larger than 100 bytes."}
    assert runner._read_answer(path, None, 300) == {"output": "x" * 200}
    assert runner._read_answer(path, None) == {"output": "x" * 200}


_DETACHED_CHILD = (
    "import json, os, subprocess\n"
    "subprocess.Popen(['sleep', '30'], start_new_session=True)\n"
    "json.dump({'output': 7}, open(os.environ['ASCENT_OUTPUT'], 'w'))\n"
)


@_needs_openascent
def test_a_command_entry_does_not_wait_for_a_child_in_its_own_session(tmp_path: Path) -> None:
    """A child that the command starts in a new session neither holds the result nor counts in the run time (SKY-757)."""
    ran = _candidate(tmp_path, [sys.executable, "-c", _DETACHED_CHILD])
    assert ran.pop(runner.ELAPSED_PARAM) < 5
    assert ran == {"output": 7, "error": None}


@pytest.mark.parametrize(
    ("entry", "error"),
    [
        ("cand.py:boom", "boom"),
        ("cand.py:not_json", "JSON"),
        ("cand.py:slow", "timeout after 1 s"),
    ],
)
def test_run_failures_have_no_output(tmp_path: Path, entry: Any, error: str) -> None:
    """A raise, a non-JSON return and a timeout of a function entry are failures."""
    # Only the timeout cases get the short limit; a loaded test host can
    # take over a second just to start Python.
    ran = _candidate(tmp_path, entry, timeout=1 if "timeout" in error else 30)
    assert ran["output"] is None
    assert error in ran["error"]


@_needs_openascent
@pytest.mark.parametrize(
    ("entry", "error"),
    [
        (["sh", "-c", "sleep 30"], "longer than the time limit of 1"),
        (["no-such-command-e38"], "The command cannot start"),
        (["sh", "-c", "kill -9 $$"], "stopped by signal 9"),
        (["sh", "-c", 'echo \'{"output": 1}\' > "$ASCENT_OUTPUT"; echo why >&2; exit 3'], "exited with code 3"),
        (["sh", "-c", 'echo "{not json" > "$ASCENT_OUTPUT"'], "not valid JSON"),
        (["sh", "-c", 'echo \'{"result": 1}\' > "$ASCENT_OUTPUT"'], '{"output": <any JSON value>}'),
    ],
)
def test_command_run_failures_have_no_output(tmp_path: Path, entry: Any, error: str) -> None:
    """A timeout, a start failure, a signal, a nonzero exit, bad JSON and the wrong form fail a command (SKY-757)."""
    ran = _candidate(tmp_path, entry, timeout=1 if "time limit" in error else 30)
    assert ran["output"] is None
    assert error in ran["error"]
    if "code 3" in error:
        assert ran["stderr"] == "why\n"


@_needs_openascent
def test_a_failed_command_puts_its_stdout_and_stderr_in_the_logs(tmp_path: Path) -> None:
    """A command that fails keeps its stdout tail next to its stderr, in the logs and in the error (SKY-757)."""
    logs: list[str] = []
    ran = _candidate(tmp_path, ["sh", "-c", "echo said-on-stdout; echo said-on-stderr >&2; exit 1"], logs=logs)
    assert ran["output"] is None
    assert "said-on-stdout\nsaid-on-stderr\n" in logs[0]
    assert ran["error"].startswith("The command exited with code 1.")
    assert runner.command_error(ran, str, []).endswith("\nstderr: said-on-stderr\n\nstdout: said-on-stdout\n")


def test_the_supervisor_does_not_wait_for_a_process_the_candidate_left(tmp_path: Path) -> None:
    """A process in its own session neither holds the result nor counts in the run time (SKY-398).

    Closing the candidate box ends that process; the supervisor only kills
    the entry's own process group.
    """
    ran = _candidate(tmp_path, "cand.py:leaves_a_sleeper", 7)
    assert ran.pop(runner.ELAPSED_PARAM) < 5
    assert ran == {"output": 7, "error": None}


def test_a_stage_client_names_its_role_in_every_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each frame carries the box's role, so the parent can refuse a frame from the other box (SKY-398)."""
    stream = io.StringIO()
    monkeypatch.setattr(sys, "__stdout__", stream)
    client = runner.StageClient({"prefix": "@@", "response_dir": "r", "names": [], "role": "run"})
    client._send({"kind": "stop", "role": "metric"})
    [frame] = [line for line in stream.getvalue().splitlines() if line]
    assert json.loads(frame.removeprefix("@@"))["role"] == "run"


def test_accepts_output_counts_the_third_argument() -> None:
    """Pass the output to ``score(repo_path, case, output)``, and to ``*args`` only after a run."""
    assert runner.accepts_output(lambda repo, case, output: 0, ran=False)
    assert not runner.accepts_output(lambda repo, case=None: 0, ran=True)
    assert runner.accepts_output(lambda *args: 0, ran=True)
    assert not runner.accepts_output(lambda *args: 0, ran=False)


def test_the_supervisor_times_the_entry_whatever_clock_the_candidate_patches(tmp_path: Path) -> None:
    """The run time comes from the supervisor's process; a patched clock or a faked output cannot lower it (SKY-642)."""
    ran = _candidate(tmp_path, "cand.py:fakes_its_time", 0.4)
    assert ran["error"] is None
    assert ran["output"] == {"elapsed_s": 0.0}
    assert ran[runner.ELAPSED_PARAM] >= 0.4


@_needs_openascent
def test_the_supervisor_times_a_command_entry(tmp_path: Path) -> None:
    """A command entry gets the same time from the supervisor (SKY-642)."""
    ran = _candidate(tmp_path, ["sh", "-c", 'sleep 0.4; echo \'{"output": 1}\' > "$ASCENT_OUTPUT"'])
    assert ran["error"] is None
    assert 0.4 <= ran[runner.ELAPSED_PARAM] < 30


@_needs_openascent
def test_a_timed_out_entry_still_reports_its_time(tmp_path: Path) -> None:
    """A timeout is a failure; its time is about the timeout (SKY-642)."""
    ran = _candidate(tmp_path, ["sh", "-c", "sleep 30"], timeout=1)
    assert "time limit" in ran["error"]
    assert 1 <= ran[runner.ELAPSED_PARAM] < 30


def test_accepts_elapsed_needs_the_name_after_the_output() -> None:
    """Pass ``elapsed_s`` only to a metric that names it after ``output``, never to ``*args`` or ``**kwargs`` (SKY-642)."""
    assert runner.accepts_elapsed(lambda repo, case, output=None, elapsed_s=None: 0)
    assert runner.accepts_elapsed(lambda repo, case, output=None, *, elapsed_s=None: 0)
    assert not runner.accepts_elapsed(lambda repo, case, output=None: 0)
    assert not runner.accepts_elapsed(lambda repo, case, elapsed_s=None: 0)
    assert not runner.accepts_elapsed(lambda *args: 0)
    assert not runner.accepts_elapsed(lambda repo, case, output=None, **kwargs: 0)


def _metric_call(tmp_path: Path, code: str, document: dict[str, Any]) -> dict[str, Any]:
    """Run one metric call in a fresh folder, as the scorer box does.

    Args:
        tmp_path: Scratch folder.
        code: The metric's source.
        document: The rest of ``input.json``.

    Returns:
        The runner's output document.
    """
    call_dir = tmp_path / "call"
    call_dir.mkdir()
    payload = {"code": code, "candidate": str(tmp_path), "case": None, "gateway": None, **document}
    (call_dir / runner.INPUT_FILE).write_text(json.dumps(payload), encoding="utf-8")
    subprocess.run([sys.executable, runner.__file__, str(call_dir)], cwd=tmp_path, check=True, timeout=60)
    return json.loads((call_dir / runner.OUTPUT_FILE).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("def score(repo_path, case, output=None, elapsed_s=None):\n    return {'score': elapsed_s}\n", 0.75),
        ("def score(repo_path, case, output=None, *, elapsed_s=None):\n    return {'score': elapsed_s}\n", 0.75),
        ("def score(repo_path, case, output):\n    return {'score': output['n']}\n", 0.5),
        ("def score(repo_path, case=None):\n    return {'score': 0.25}\n", 0.25),
        ("def score(*args):\n    return {'score': len(args) / 10}\n", 0.3),
    ],
)
def test_the_metric_gets_the_run_time_only_when_it_asks(tmp_path: Path, code: str, expected: float) -> None:
    """A metric that names ``elapsed_s`` gets the time; older 1-, 2- and 3-argument metrics get the same call (SKY-642)."""
    result = _metric_call(tmp_path, code, {"output": {"n": 0.5}, runner.ELAPSED_PARAM: 0.75})
    assert result["error"] is None
    assert result["score"] == pytest.approx(expected)


def test_a_metric_without_a_run_gets_no_time(tmp_path: Path) -> None:
    """With no ``[run]`` entry, ``elapsed_s`` keeps its default (SKY-642)."""
    code = "def score(repo_path, case, output=None, elapsed_s=None):\n    return {'score': 1.0 if elapsed_s is None else 0.0}\n"
    assert _metric_call(tmp_path, code, {})["score"] == 1.0


def test_a_metric_that_starts_a_process_after_the_run_is_refused(tmp_path: Path) -> None:
    """With an output in the input, the metric may start no process, even when it catches the refusal (E40)."""
    call_dir = tmp_path / "call"
    call_dir.mkdir()
    code = (
        "import subprocess\n"
        "def score(repo_path, case, output):\n"
        "    try:\n"
        "        subprocess.run(['true'])\n"
        "    except Exception:\n"
        "        pass\n"
        "    return 1.0\n"
    )
    document = {"code": code, "candidate": str(tmp_path), "case": None, "gateway": None, "output": {"x": 1}}
    (call_dir / runner.INPUT_FILE).write_text(json.dumps(document), encoding="utf-8")
    subprocess.run([sys.executable, runner.__file__, str(call_dir)], cwd=tmp_path, check=True, timeout=60)
    result = json.loads((call_dir / runner.OUTPUT_FILE).read_text(encoding="utf-8"))
    assert result["score"] is None
    assert result["error"] == runner.RERUN_REFUSED


def test_the_metric_gets_the_output_as_its_third_argument(tmp_path: Path) -> None:
    """``score(repo_path, case, output)`` receives the run's output and the full case."""
    call_dir = tmp_path / "call"
    call_dir.mkdir()
    code = "def score(repo_path, case, output):\n    return {'score': output['n'] / 10, 'feedback': case['expected']}\n"
    case = {"input": 1, "expected": "e"}
    document = {"code": code, "candidate": str(tmp_path), "case": case, "gateway": None, "output": {"n": 4}}
    (call_dir / runner.INPUT_FILE).write_text(json.dumps(document), encoding="utf-8")
    subprocess.run([sys.executable, runner.__file__, str(call_dir)], cwd=tmp_path, check=True, timeout=60)
    result = json.loads((call_dir / runner.OUTPUT_FILE).read_text(encoding="utf-8"))
    assert result["score"] == pytest.approx(0.4)
    assert result["side_info"]["feedback"] == "e"


def test_the_metric_site_goes_before_the_image_packages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The metric's installed packages win over the image's, and a missing or repeated folder changes nothing."""
    site = tmp_path / "site"
    site.mkdir()
    monkeypatch.setattr(sys, "path", ["/usr/lib/python313.zip", "/app/.venv/lib/python3.13/site-packages"])

    runner._add_metric_site(str(site))
    runner._add_metric_site(str(site))
    runner._add_metric_site(str(tmp_path / "missing"))
    runner._add_metric_site(None)

    assert sys.path == ["/usr/lib/python313.zip", str(site), "/app/.venv/lib/python3.13/site-packages"]


def test_the_wrapper_file_names_match_the_vercel_exec_wrapper() -> None:
    """The supervisor checks the names that the Vercel root exec wrapper writes, so they stay equal (SKY-398, TU-1)."""
    _, out_path, _, sentinel = vercel._wrapper("true", "0" * 32, sudo=True)
    folder = f"{runner.WRAPPER_FOLDER_PREFIX}{'0' * 32}"
    assert runner.WRAPPER_FOLDER_PREFIX == vercel.ROOT_OUTPUT_PREFIX
    assert (out_path, sentinel) == (f"{folder}/{runner.WRAPPER_OUTPUT_NAME}", f"{folder}/{runner.WRAPPER_EXIT_NAME}")


_TOKEN = "ab" * 16
_FOLDER = f"/tmp/.app-root-output-{_TOKEN}"


def _wrapper_stdout(
    monkeypatch: pytest.MonkeyPatch, path: str, *, folder_uid: int = 0, folder_mode: int = stat.S_IFDIR | 0o755
) -> list[str]:
    """Make the supervisor see a root stdout file at ``path`` in a folder with the given owner and mode.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
        path: The name behind fd 1.
        folder_uid: The owner of ``path``'s folder.
        folder_mode: The type and mode of ``path``'s folder.

    Returns:
        The names that the supervisor creates, filled as it runs.
    """
    output = os.stat_result((stat.S_IFREG | 0o600, 7, 1, 1, 0, 0, 0, 0, 0, 0))
    folder = os.stat_result((folder_mode, 8, 1, 2, folder_uid, 0, 0, 0, 0, 0))
    names = {path: output, path.rpartition("/")[0]: folder}
    created: list[str] = []

    def lstat(name: str) -> os.stat_result:
        """Return the fake entry at ``name``.

        Args:
            name: The path.

        Returns:
            Its fake status.

        Raises:
            FileNotFoundError: No fake entry has that name.
        """
        if name not in names:
            raise FileNotFoundError(name)
        return names[name]

    def create(name: str, flags: int, mode: int) -> int:
        """Record the created name.

        Args:
            name: The path.
            flags: The open flags.
            mode: The file mode.

        Returns:
            A fake fd.
        """
        created.append(name)
        return 99

    monkeypatch.setattr(runner.os, "fstat", lambda fd: output)
    monkeypatch.setattr(runner.os, "lstat", lstat)
    monkeypatch.setattr(runner.os, "geteuid", lambda: 0)
    monkeypatch.setattr(runner.os, "fchmod", lambda fd, mode: None)
    monkeypatch.setattr(runner.os, "open", create)
    monkeypatch.setattr(runner.os, "close", lambda fd: None)
    monkeypatch.setattr(runner.Path, "readlink", lambda self: Path(path))
    return created


def test_a_root_file_in_the_wrappers_root_folder_is_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Accept the root wrapper's own output file, and create its exit code file in the same folder (TU-1)."""
    created = _wrapper_stdout(monkeypatch, f"{_FOLDER}/output.out")

    assert runner.unsafe_output() is None
    assert created == [f"{_FOLDER}/exit"]


@pytest.mark.parametrize(
    ("path", "options", "refusal"),
    [
        (f"/tmp/.app-output-{_TOKEN}.out", {}, "is not a file of the exec wrapper"),
        (f"{_FOLDER}/output.out", {"folder_uid": 1000}, "is not a root-only folder"),
        (f"{_FOLDER}/output.out", {"folder_mode": stat.S_IFDIR | 0o775}, "is not a root-only folder"),
        (f"{_FOLDER}/output.out", {"folder_mode": stat.S_IFDIR | 0o757}, "is not a root-only folder"),
        (f"{_FOLDER}/output.out", {"folder_mode": stat.S_IFLNK | 0o777}, "is not a root-only folder"),
    ],
    ids=["old-name", "box-user-folder", "group-writable", "other-writable", "link"],
)
def test_a_root_file_outside_a_root_only_wrapper_folder_is_refused(
    monkeypatch: pytest.MonkeyPatch, path: str, options: dict[str, int], refusal: str
) -> None:
    """Refuse the old ``/tmp`` name and a folder that another user could plant into, and create no exit file (TU-1)."""
    created = _wrapper_stdout(monkeypatch, path, **options)

    assert refusal in (runner.unsafe_output() or "")
    assert created == []


_UNSAFE_OUTPUT = (
    "import importlib.util,sys;"
    "spec=importlib.util.spec_from_file_location('r',sys.argv[1]);m=importlib.util.module_from_spec(spec);"
    "sys.modules['r']=m;spec.loader.exec_module(m);sys.stderr.write(repr(m.unsafe_output()))"
)


def test_a_pipe_stdout_is_safe_for_the_result_frame() -> None:
    """The runtime holds the other end of a pipe, so the entry cannot replace it."""
    done = subprocess.run(
        [sys.executable, "-c", _UNSAFE_OUTPUT, runner.__file__], capture_output=True, text=True, timeout=60, check=False
    )

    assert done.stderr == "None"


def test_a_file_stdout_that_is_not_the_wrappers_root_file_is_refused(tmp_path: Path) -> None:
    """A stdout file that the test's user owns, outside the wrapper's names, could carry a forged frame."""
    with (tmp_path / "out").open("w") as out:
        done = subprocess.run(
            [sys.executable, "-c", _UNSAFE_OUTPUT, runner.__file__],
            stdout=out,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
            check=False,
        )

    # repr() quotes the refusal with double quotes when it holds an apostrophe.
    assert done.stderr.strip("'\"").startswith("The supervisor")


# Runs the runner as a root supervisor would: root, with or without
# capabilities, and a libc whose prctl records its calls and can fail. With a
# call folder it runs the whole candidate path, else only the user choice and
# the entry's PATH.
_ROOT_SUPERVISOR = """
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("r", sys.argv[1])
m = importlib.util.module_from_spec(spec)
sys.modules["r"] = m
spec.loader.exec_module(m)
m.os.geteuid = lambda: 0
m._has_capabilities = lambda: sys.argv[2] == "caps"
calls = []


class Libc:
    def prctl(self, *args):
        calls.append(list(args))
        return -1 if sys.argv[3] == "fails" else 0


m.ctypes.CDLL = lambda *args, **kwargs: Libc()
if len(sys.argv) > 4:
    report = {"code": m.main([m.CANDIDATE_FLAG, sys.argv[4]])}
else:
    try:
        env = dict(m.os.environ)
        report = {"chosen": m._entry_user(env), "path": env.get("PATH")}
    except m.EntryUserError as error:
        report = {"refused": str(error)}
report["calls"] = calls
print(json.dumps(report))
"""


def _root_supervisor(
    tmp_path: Path, capabilities: str, prctl: str, *call_dir: str, entry_path: str | None = None
) -> dict[str, Any]:
    """Run ``_ROOT_SUPERVISOR`` in the tree ``tmp_path`` and return its report.

    Args:
        tmp_path: The candidate tree, owned by the test user.
        capabilities: ``caps`` when the fake root holds capabilities.
        prctl: ``fails`` when every prctl call fails.
        call_dir: The call folder, to run the whole candidate path.
        entry_path: The image's ``APP_ENTRY_PATH``, or ``None`` for an image without it.

    Returns:
        The report: the choice and the entry's PATH, the refusal or the exit
        code, and the prctl calls.
    """
    env = {key: value for key, value in os.environ.items() if key != runner.ENV_ENTRY_PATH}
    if entry_path is not None:
        env[runner.ENV_ENTRY_PATH] = entry_path
    done = subprocess.run(
        [sys.executable, "-c", _ROOT_SUPERVISOR, runner.__file__, capabilities, prctl, *call_dir],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return json.loads(done.stdout.splitlines()[-1])


@pytest.mark.parametrize(("capabilities", "switched"), [("caps", True), ("none", False)])
def test_a_root_with_capabilities_drops_the_entry_to_the_tree_owner_also_with_a_pipe(
    tmp_path: Path, capabilities: str, switched: bool
) -> None:
    """Only a root with no capability, as in a local Docker box, keeps the entry as itself (SKY-398)."""
    chosen = _root_supervisor(tmp_path, capabilities, "works")["chosen"]
    assert chosen == ({"user": os.getuid(), "group": os.getgid(), "extra_groups": []} if switched else {})


@pytest.mark.parametrize(
    ("capabilities", "entry_path", "path"),
    [
        ("caps", "/app/.venv/bin:/usr/bin", "/app/.venv/bin:/usr/bin"),
        ("caps", None, os.environ.get("PATH")),
        ("caps", "", os.environ.get("PATH")),
        ("none", "/app/.venv/bin:/usr/bin", os.environ.get("PATH")),
    ],
)
def test_only_a_user_switch_gives_the_entry_the_image_box_path(
    tmp_path: Path, capabilities: str, entry_path: str | None, path: str | None
) -> None:
    """sudo gives root a secure PATH, so the switched entry gets the box PATH with the [setup] venv back."""
    assert _root_supervisor(tmp_path, capabilities, "works", entry_path=entry_path)["path"] == path


@pytest.mark.parametrize(("capabilities", "calls"), [("caps", [[38, 1, 0, 0, 0]]), ("none", [])])
def test_only_a_user_switch_sets_no_new_privs_on_the_supervisor(
    tmp_path: Path, capabilities: str, calls: list[list[int]]
) -> None:
    """The entry that runs as the tree owner inherits no_new_privs, so the box's sudo cannot raise it (SKY-398)."""
    assert _root_supervisor(tmp_path, capabilities, "works")["calls"] == calls


def test_the_entry_does_not_start_when_no_new_privs_cannot_be_set(tmp_path: Path) -> None:
    """A root supervisor that cannot set no_new_privs refuses the run and never starts the entry."""
    call_dir = tmp_path / "call"
    call_dir.mkdir()
    payload = {"entry": ["sh", "-c", "touch started"], "input": None, "timeout": 30, "stages": None}
    (call_dir / runner.INPUT_FILE).write_text(json.dumps(payload), encoding="utf-8")
    assert _root_supervisor(tmp_path, "caps", "works")["chosen"]["user"] == os.getuid()

    report = _root_supervisor(tmp_path, "caps", "fails", str(call_dir))

    assert report["code"] == 3
    assert [38, 1, 0, 0, 0] in report["calls"]
    assert not (tmp_path / "started").exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="no_new_privs is a Linux process flag")
def test_the_supervisor_really_holds_no_new_privs_after_a_user_switch_choice(tmp_path: Path) -> None:
    """The real prctl call sets the flag that the entry inherits."""
    script = (
        "import importlib.util,sys;"
        "spec=importlib.util.spec_from_file_location('r',sys.argv[1]);m=importlib.util.module_from_spec(spec);"
        "sys.modules['r']=m;spec.loader.exec_module(m);m.os.geteuid=lambda:0;m._has_capabilities=lambda:True;"
        "before=open('/proc/self/status').read();m._entry_user({});"
        "print(before);print('--- after ---');print(open('/proc/self/status').read())"
    )
    done = subprocess.run(
        [sys.executable, "-c", script, runner.__file__], cwd=tmp_path, capture_output=True, text=True, check=True
    )
    before, after = done.stdout.split("--- after ---")
    if "NoNewPrivs:\t1" in before:
        pytest.skip("the test process already holds no_new_privs, so the call cannot show a change")
    assert "NoNewPrivs:\t0" in before
    assert "NoNewPrivs:\t1" in after


def _command_metric(tmp_path: Path, script: str, **extra: Any) -> dict[str, Any]:
    """Run the runner on a command ``[metric] entry`` whose bash script is ``script`` (SKY-738).

    Args:
        tmp_path: The test's folder, which stands for the metric box.
        script: The body of ``metric.sh``.
        **extra: Input keys to add or replace; ``drop`` names keys to remove.

    Returns:
        The runner's ``output.json``.
    """
    return _command_metric_run(tmp_path, script, **extra)[0]


def _command_metric_run(
    tmp_path: Path,
    script: str,
    *,
    environ: dict[str, str] | None = None,
    key: str | None = None,
    **extra: Any,
) -> tuple[dict[str, Any], str]:
    """Run the runner on a command ``[metric] entry``, as ``_command_metric`` does, and keep its stderr.

    Args:
        tmp_path: The test's folder, which stands for the metric box.
        script: The body of ``metric.sh``.
        environ: More variables for the runner's environment.
        key: The model key, written to the call's key file as the scorer does.
        **extra: Input keys to add or replace; ``drop`` names keys to remove.

    Returns:
        The runner's ``output.json`` and its stderr.
    """
    drop = extra.pop("drop", ())
    box = tmp_path / "box"
    box.mkdir()
    (box / "metric.sh").write_text(script)
    call_dir = tmp_path / "calls" / "one"
    call_dir.mkdir(parents=True)
    payload = {
        "candidate": str(box),
        "case": {"input": "q", "expected": "a"},
        "output": "a",
        runner.ELAPSED_PARAM: 0.25,
        "gateway": None,
        runner.METRIC_ENTRY_PARAM: ["bash", "metric.sh"],
        runner.METRIC_TIMEOUT_PARAM: 20,
        runner.ASCENT_PACKAGE_PARAM: _PACKAGE,
        **extra,
    }
    for key in drop:
        payload.pop(key)
    (call_dir / runner.INPUT_FILE).write_text(json.dumps(payload))
    if key is not None:
        (call_dir / runner.MODEL_KEY_FILE).write_text(key)
    completed = subprocess.run(
        [sys.executable, "-I", runner.__file__, str(call_dir)],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=box,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), **(environ or {})},
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads((call_dir / runner.OUTPUT_FILE).read_text()), completed.stderr


@_needs_openascent
def test_a_command_metric_reads_ascent_input_and_writes_ascent_output(tmp_path: Path) -> None:
    """The command gets the documented input fields and its answer becomes the score (SKY-738)."""
    script = (
        'cp "$ASCENT_INPUT" seen.json\n'
        'printf \'{"score": 0.75, "feedback": "close", "scores": {"exact": {"score": 1}}}\' > "$ASCENT_OUTPUT"\n'
    )
    output = _command_metric(tmp_path, script)

    assert output["error"] is None
    assert output["score"] == 0.75
    assert output["side_info"]["feedback"] == "close"
    assert json.loads((tmp_path / "box" / "seen.json").read_text()) == {
        "input": "q",
        "output": "a",
        "expected": "a",
        "elapsed_s": 0.25,
    }


@_needs_openascent
def test_a_command_metric_without_expected_or_elapsed_gets_only_input_and_output(tmp_path: Path) -> None:
    """``expected`` and ``elapsed_s`` are optional: a case without them gives the command neither key."""
    script = 'cp "$ASCENT_INPUT" seen.json\necho \'{"score": 1}\' > "$ASCENT_OUTPUT"\n'
    payload = {"candidate": "x", "case": {"input": "q"}, "output": None}
    output = _command_metric(tmp_path, script, **payload, drop=(runner.ELAPSED_PARAM,))
    seen = json.loads((tmp_path / "box" / "seen.json").read_text())

    assert output["score"] == 1.0
    assert seen == {"input": "q", "output": None}


@_needs_openascent
@pytest.mark.parametrize(
    ("script", "entry", "cause"),
    [
        ("echo bad >&2\nexit 3\n", ["bash", "metric.sh"], "nonzero_exit"),
        ("exit 0\n", ["bash", "metric.sh"], "no_output"),
        ('echo "{not json" > "$ASCENT_OUTPUT"\n', ["bash", "metric.sh"], "invalid_json"),
        ('echo \'{"feedback": "no score"}\' > "$ASCENT_OUTPUT"\n', ["bash", "metric.sh"], "invalid_form"),
        ("exit 0\n", ["./missing.sh"], "start_failed"),
        ("sleep 30\n", ["bash", "metric.sh"], "time_limit"),
    ],
)
def test_a_failed_command_metric_names_its_cause(tmp_path: Path, script: str, entry: list[str], cause: str) -> None:
    """Each failure has no score and the OpenAscent cause code, and an exit code when there is one."""
    output = _command_metric(tmp_path, script, **{runner.METRIC_ENTRY_PARAM: entry, runner.METRIC_TIMEOUT_PARAM: 1})

    assert output["score"] is None
    assert output["cause"] == cause
    assert output["error"]
    if cause == "nonzero_exit":
        assert output["exit_code"] == 3
        assert "bad" in output["error"]


_needs_curl = pytest.mark.skipif(shutil.which("curl") is None, reason="the helper tests call the helpers with curl")
# Prints the status and body of one helper POST: post <url> <json body>.
_CURL_POST = (
    "post() { curl -s --noproxy '*' -o body.out -w '%{http_code}' -X POST"
    " -H 'Content-Type: application/json' -d \"$2\" \"$1\"; printf ' '; cat body.out; echo; }\n"
)


def _events(stderr: str) -> list[dict[str, Any]]:
    """Return the ``log()`` events that a runner wrote to its stderr.

    Args:
        stderr: The runner's stderr.

    Returns:
        Each event line, decoded.
    """
    return [
        json.loads(line[len(runner.EVENT_PREFIX) :])
        for line in stderr.splitlines()
        if line.startswith(runner.EVENT_PREFIX)
    ]


@_needs_openascent
@_needs_curl
def test_a_command_metric_without_a_judge_model_gets_only_the_log_url(tmp_path: Path) -> None:
    """Without a gateway the command gets ``ASCENT_LOG_URL`` on loopback and no model variable (SKY-767)."""
    script = 'env | grep "^ASCENT_" | sort > seen.env\necho \'{"score": 1}\' > "$ASCENT_OUTPUT"\n'
    output = _command_metric(tmp_path, script)
    seen = dict(line.split("=", 1) for line in (tmp_path / "box" / "seen.env").read_text().splitlines())

    assert output["score"] == 1.0
    assert set(seen) == {"ASCENT_INPUT", "ASCENT_OUTPUT", runner.LOG_URL_ENV}
    assert urllib.parse.urlsplit(seen[runner.LOG_URL_ENV]).hostname == "127.0.0.1"


@_needs_openascent
@_needs_curl
def test_the_log_url_writes_the_same_event_as_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A POST to ``ASCENT_LOG_URL`` gives the run log the line that ``log()`` gives; a bad body is refused."""
    script = (
        _CURL_POST
        + 'post "$ASCENT_LOG_URL" \'{"event": "judge.retry", "fields": {"attempt": 2, "level": "x"}}\' > answers\n'
        + 'post "$ASCENT_LOG_URL" \'{"event": "", "fields": {}}\' >> answers\n'
        + 'post "$ASCENT_LOG_URL" \'{"event": "a", "extra": 1}\' >> answers\n'
        + 'echo \'{"score": 1}\' > "$ASCENT_OUTPUT"\n'
    )
    output, stderr = _command_metric_run(tmp_path, script)
    answers = (tmp_path / "box" / "answers").read_text().splitlines()
    expected = io.StringIO()
    monkeypatch.setattr(sys, "__stderr__", expected)
    runner.log("judge.retry", attempt=2)

    assert output["score"] == 1.0
    assert answers[0] == "200 {}"
    assert [answer.split(" ", 1)[0] for answer in answers[1:]] == ["400", "400"]
    assert all('{"error": ' in answer for answer in answers[1:])
    want = _events(expected.getvalue())[0]
    want["fields"]["level"] = "x"
    assert _events(stderr) == [want]


@_needs_openascent
@_needs_curl
def test_the_model_url_sends_the_judge_model_through_the_gateway_and_keeps_the_key(tmp_path: Path) -> None:
    """With a gateway the command reaches the model through the runner; the key stays out of its environment."""
    script = (
        _CURL_POST
        + 'env | grep -c "^APP_API_KEY=" > keys || true\n'
        + 'printf "%s|%s\\n" "$ASCENT_MODEL" "${ASCENT_MODEL_KEY-unset}" > model\n'
        + 'url="$ASCENT_MODEL_URL/chat/completions"\n'
        + 'post "$url" "{\\"model\\": \\"$ASCENT_MODEL\\", \\"messages\\": [{\\"role\\": \\"user\\",'
        + ' \\"content\\": \\"hi\\"}], \\"max_tokens\\": 5}" > answers\n'
        + 'post "$url" \'{"model": "other/model", "messages": []}\' >> answers\n'
        + 'post "$url" \'{"messages": [], "tools": []}\' >> answers\n'
        + 'post "$url" \'{"messages": [], "stream": true}\' >> answers\n'
        + 'echo \'{"score": 1}\' > "$ASCENT_OUTPUT"\n'
    )
    with FakeGateway(reply="judged", usage=(7, 2)) as gateway:
        gateway_payload = {"url": gateway.url, "model": "judge-model", "declared_model": "openai/judge-model"}
        output = _command_metric(tmp_path, script, environ={"APP_API_KEY": "sk-test"}, gateway=gateway_payload)
    box = tmp_path / "box"
    answers = (box / "answers").read_text().splitlines()

    assert output["score"] == 1.0
    assert output["usage"] == [{"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9}]
    assert (box / "keys").read_text().strip() == "0"
    assert (box / "model").read_text().strip() == "openai/judge-model|"
    status, body = answers[0].split(" ", 1)
    assert status == "200"
    assert json.loads(body)["choices"][0]["message"]["content"] == "judged"
    assert len(gateway.requests) == 1
    assert gateway.requests[0]["authorization"] == "Bearer sk-test"
    assert gateway.requests[0]["body"]["model"] == "judge-model"
    assert gateway.requests[0]["body"]["max_tokens"] == 5
    assert [answer.split(" ", 1)[0] for answer in answers[1:]] == ["400", "400", "400"]
    assert runner.MODEL_ENV in json.loads(answers[1].split(" ", 1)[1])["error"]


@_needs_openascent
@_needs_curl
def test_a_budget_stop_on_the_model_url_stops_the_metric_call(tmp_path: Path) -> None:
    """A protected gateway's budget stop reaches the output as ``control``, not as the command's score."""
    script = (
        _CURL_POST
        + 'post "$ASCENT_MODEL_URL/chat/completions" \'{"messages": [{"role": "user", "content": "x"}]}\' > answers\n'
        + 'echo \'{"score": 1}\' > "$ASCENT_OUTPUT"\n'
    )

    class Stopped(FakeGateway):
        """Answer every request with the protected gateway's budget stop."""

        def answer(self, request: dict[str, Any]) -> tuple[int, dict[str, Any]]:
            """Record the request and refuse it.

            Args:
                request: The recorded request.

            Returns:
                A 402 with the ``budget_reached`` error.
            """
            self.requests.append(request)
            return 402, {"error": {"code": "budget_reached", "message": "The run budget is spent."}}

    with Stopped() as gateway:
        payload = {"url": gateway.url, "model": "judge-model", "protected": True}
        output = _command_metric(tmp_path, script, gateway=payload)

    assert (tmp_path / "box" / "answers").read_text().split(" ", 1)[0] == "503"
    assert output["score"] is None
    assert output["control"] == {"code": "budget_reached", "message": "The run budget is spent."}


@_needs_openascent
def test_the_command_env_holds_no_model_key_mailbox_or_relay_token(tmp_path: Path) -> None:
    """Each key, model mailbox and tool relay name stays out of the command; other variables stay in."""
    hidden = dict.fromkeys(runner._COMMAND_HIDDEN_ENV, "secret-value")
    output = _command_metric(
        tmp_path, 'env > seen\necho \'{"score": 1}\' > "$ASCENT_OUTPUT"\n', environ={**hidden, "KEEP_ME": "1"}
    )
    seen = (tmp_path / "box" / "seen").read_text()

    assert output["score"] == 1.0
    assert {"APP_API_KEY", "SKYNET_API_KEY", "APP_BUDGET_RELAY_URL", "SKYNET_BUDGET_RELAY_URL"} <= set(hidden)
    assert {"APP_GATEWAY_URL", "APP_TOOL_RELAY_TOKEN", "OPENAI_BASE_URL", "ANTHROPIC_BASE_URL"} <= set(hidden)
    assert "secret-value" not in seen
    assert "KEEP_ME=1" in seen.splitlines()


@_needs_openascent
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc and PR_SET_DUMPABLE are Linux only")
def test_a_command_metric_cannot_read_the_runner_environment(tmp_path: Path) -> None:
    """The runner holds the model key in its start environment; its child command cannot read it from /proc."""
    script = (
        'tr "\\0" " " < "/proc/$PPID/cmdline" > parent\n'
        'if tr "\\0" "\\n" < "/proc/$PPID/environ" 2>/dev/null | grep -q "^APP_API_KEY="; then\n'
        "  echo read > seen\nelse\n  echo refused > seen\nfi\n"
        'echo \'{"score": 1}\' > "$ASCENT_OUTPUT"\n'
    )
    output = _command_metric(tmp_path, script, environ={"APP_API_KEY": "sk-test"})
    box = tmp_path / "box"

    assert output["score"] == 1.0
    assert Path(runner.__file__).name in (box / "parent").read_text()
    assert (box / "seen").read_text().strip() == "refused"


@_needs_openascent
@_needs_curl
def test_the_model_key_file_reaches_the_gateway_and_is_gone_before_the_command(tmp_path: Path) -> None:
    """The runner reads the scorer's key file and deletes it before the command starts; the model call uses the key."""
    script = (
        _CURL_POST
        + 'if [ -e "$CALL_DIR/model_key" ]; then echo present > keyfile; else echo gone > keyfile; fi\n'
        + 'env | grep -c "sk-file" > keys || true\n'
        + 'post "$ASCENT_MODEL_URL/chat/completions" \'{"messages": [{"role": "user", "content": "x"}]}\' > answers\n'
        + 'echo \'{"score": 1}\' > "$ASCENT_OUTPUT"\n'
    )
    call_dir = tmp_path / "calls" / "one"
    with FakeGateway(reply="judged") as gateway:
        payload = {"url": gateway.url, "model": "judge-model"}
        output = _command_metric(tmp_path, script, environ={"CALL_DIR": str(call_dir)}, key="sk-file", gateway=payload)
    box = tmp_path / "box"

    assert output["score"] == 1.0
    assert runner.MODEL_KEY_FILE == "model_key"
    assert (box / "keyfile").read_text().strip() == "gone"
    assert not (call_dir / runner.MODEL_KEY_FILE).exists()
    assert (box / "keys").read_text().strip() == "0"
    assert (box / "answers").read_text().split(" ", 1)[0] == "200"
    assert [request["authorization"] for request in gateway.requests] == ["Bearer sk-file"]


# Lists each process whose environ the command can read, and each secret it finds there.
_PROC_SCAN = (
    "for p in /proc/[0-9]*; do\n"
    '  if text=$(tr "\\0" "\\n" < "$p/environ" 2>/dev/null); then\n'
    '    echo "${p#/proc/}" >> readable\n'
    '    for secret in "sk-scan" "http://relay.invalid/v1" "relay-token-scan"; do\n'
    '      case "$text" in *"$secret"*) echo "${p#/proc/} $secret" >> found ;; esac\n'
    "    done\n"
    "  fi\n"
    "done\n"
    "touch found\n"
    'echo \'{"score": 1}\' > "$ASCENT_OUTPUT"\n'
)


@_needs_openascent
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="/proc and PR_SET_DUMPABLE are Linux only")
@pytest.mark.parametrize("chain", ["vercel", "contained", "vercel_key_in_env"])
def test_no_process_the_command_can_read_holds_the_key_or_relay_secrets(tmp_path: Path, chain: str) -> None:
    """A command metric scans every readable ``/proc/*/environ`` and finds no key, relay address or relay token.

    ``vercel`` uses the provider's own wrapper: a wrapper shell and ``timeout``
    stay alive above the runner with the command env, which holds no secret
    now. ``contained`` copies only the metric box command of a contained
    session: the shell ``exec``s the runner, whose environment holds the relay
    address and token. It does not copy the session's own parent process.
    ``vercel_key_in_env`` is the old scorer, which put the key in the command
    env: the scan must find it in the wrapper shell, so the scan works.
    """
    box = tmp_path / "box"
    box.mkdir()
    (box / "metric.sh").write_text(_PROC_SCAN)
    call_dir = tmp_path / "calls" / "one"
    call_dir.mkdir(parents=True)
    payload = {
        "candidate": str(box),
        "case": {"input": "q"},
        "output": "a",
        "gateway": {"url": "http://127.0.0.1:9/v1", "model": "judge-model"},
        runner.METRIC_ENTRY_PARAM: ["bash", "metric.sh"],
        runner.METRIC_TIMEOUT_PARAM: 20,
        runner.ASCENT_PACKAGE_PARAM: _PACKAGE,
    }
    (call_dir / runner.INPUT_FILE).write_text(json.dumps(payload))
    (call_dir / runner.MODEL_KEY_FILE).write_text("sk-scan")
    start = f"cd {shlex.quote(str(box))} && exec {shlex.quote(sys.executable)} -I -B -u"
    start += f" {shlex.quote(runner.__file__)} {shlex.quote(str(call_dir))}"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"}
    if chain == "vercel_key_in_env":
        env["APP_API_KEY"] = "sk-scan"
    if chain.startswith("vercel"):
        inner = f"/usr/bin/timeout --signal=KILL 60s /bin/bash {' '.join(vercel.SHELL_FLAGS)} {shlex.quote(start)}"
        script = vercel._wrapper(inner, uuid.uuid4().hex, sudo=False)[0]
    else:
        env.update({"APP_BUDGET_RELAY_URL": "http://relay.invalid/v1", "APP_TOOL_RELAY_TOKEN": "relay-token-scan"})
        script = start
    argv = ["/bin/bash", *vercel.SHELL_FLAGS, script]
    process = subprocess.Popen(argv, env=env)
    assert process.wait(timeout=60) == 0

    assert json.loads((call_dir / runner.OUTPUT_FILE).read_text())["score"] == 1.0
    found = (box / "found").read_text().splitlines()
    if chain == "vercel_key_in_env":
        assert f"{process.pid} sk-scan" in found
    else:
        assert found == []
    if chain.startswith("vercel"):
        assert str(process.pid) in (box / "readable").read_text().split()


def test_the_metric_command_path_blocks_tracing_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run_metric_command`` makes the runner non-dumpable before it starts anything; ``run_call`` does not."""

    class BlockedError(Exception):
        """Raised by the stand-in for ``_block_tracing``."""

    def blocked() -> None:
        """Stop the call where the runner blocks tracing."""
        raise BlockedError

    monkeypatch.setattr(runner, "_block_tracing", blocked)
    with pytest.raises(BlockedError):
        runner.run_metric_command({})
    assert "_block_tracing" not in runner.run_call.__code__.co_names


def test_stage_names_that_share_a_variable_get_no_variable() -> None:
    """``a-b`` and ``a_b`` (or ``Judge`` and ``judge``) would reach each other's stage, so neither gets a URL."""
    helpers = runner.MetricHelpers(None, ["a-b", "a_b", "Judge", "judge", "ok"], None, None)
    try:
        stages = {name for name in helpers.env if name.startswith(runner.STAGE_ENV_PREFIX)}
    finally:
        helpers.close()
    assert stages == {"ASCENT_STAGE_OK_URL"}


def test_stage_env_name_follows_the_openascent_rule() -> None:
    """Each ASCII letter is upper case, each digit stays, and each other character is ``_``."""
    assert runner.stage_env_name("double") == "ASCENT_STAGE_DOUBLE_URL"
    assert runner.stage_env_name("judge-2.v") == "ASCENT_STAGE_JUDGE_2_V_URL"
    assert runner.stage_env_name("straße") == "ASCENT_STAGE_STRA_E_URL"


def test_metric_helpers_refuse_a_stage_outside_the_run_and_relay_one_inside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stage route answers the relay's output, and refuses an unknown stage, a bad body and a failed stage."""
    sent: list[dict[str, Any]] = []
    answers = {"good": {"output": {"doubled": 4}}, "bad": {"error": "Stage 'bad' passed its 5s timeout."}}
    monkeypatch.setattr(sys, "__stdout__", _answer_stage_calls(tmp_path, answers, sent))
    relay = runner.StageClient({"prefix": "PFX:", "response_dir": str(tmp_path), "names": ["good", "bad"]})
    helpers = runner.MetricHelpers(relay, ["good", "bad"], None, None)

    def post(url: str, body: Any) -> tuple[int, Any]:
        """POST one JSON body and return the status and the decoded answer."""
        request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    try:
        good = helpers.env["ASCENT_STAGE_GOOD_URL"]
        base = good.rsplit("/stage/", 1)[0]
        assert post(good, {"input": {"x": 2}}) == (200, {"output": {"doubled": 4}})
        assert post(f"{base}/stage/other", {"input": 1})[0] == 404
        assert post(good, {"input": 1, "more": 2})[0] == 400
        status, body = post(helpers.env["ASCENT_STAGE_BAD_URL"], {"input": None})
        assert (status, "timeout" in body["error"]) == (502, True)
        assert post(f"{base}/model/chat/completions", {"messages": []})[0] == 404
    finally:
        helpers.close()
    assert [frame["stage"] for frame in sent] == ["good", "bad"]
    assert runner.MODEL_URL_ENV not in helpers.env


def _post_raw(url: str, data: bytes) -> tuple[int, Any]:
    """POST raw bytes and return the status and the decoded JSON answer.

    Args:
        url: The helper URL.
        data: The request body.

    Returns:
        The HTTP status and the decoded answer.
    """
    request = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_metric_helpers_refuse_deep_nesting_and_non_finite_numbers_with_400(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """A deeply nested body and ``NaN`` or ``Infinity`` get a 400 JSON answer and print no traceback."""
    helpers = runner.MetricHelpers(None, [], None, None)
    try:
        log_url = helpers.env[runner.LOG_URL_ENV]
        answers = [_post_raw(log_url, body) for body in (b"[" * 100_000, b"NaN", b'{"x": Infinity}', b"-Infinity")]
    finally:
        helpers.close()

    assert answers == [(400, {"error": "The request body is not JSON."})] * 4
    assert "Traceback" not in capfd.readouterr().err


def test_metric_helpers_answer_an_unexpected_failure_with_500_and_no_traceback(
    capfd: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A helper failure outside ``_HelperError`` gets a 500 JSON answer, and stderr, where log() events go, stays clean."""
    helpers = runner.MetricHelpers(None, [], None, None)

    def fail(path: str, body: Any) -> dict[str, Any]:
        """Fail as an unexpected bug would."""
        raise RuntimeError("secret detail")

    monkeypatch.setattr(helpers, "answer", fail)
    try:
        answer = _post_raw(helpers.env[runner.LOG_URL_ENV], b"{}")
    finally:
        helpers.close()

    assert answer == (500, {"error": "The helper failed: RuntimeError."})
    assert "Traceback" not in capfd.readouterr().err


def test_metric_helpers_print_no_traceback_when_the_client_leaves_before_the_answer(
    capfd: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A metric command that closes its socket before the answer (``curl --max-time``) leaves stderr clean."""
    helpers = runner.MetricHelpers(None, [], None, None)
    left, done = threading.Event(), threading.Event()

    def slow(path: str, body: Any) -> dict[str, Any]:
        """Answer only after the client left, with a body larger than a socket buffer."""
        left.wait(10)
        time.sleep(0.2)
        return {"output": "x" * 4_000_000}

    finish = helpers._server.shutdown_request

    def shutdown_request(request: Any) -> None:
        """Mark the end of the request, which comes after any error report."""
        finish(request)
        done.set()

    monkeypatch.setattr(helpers, "answer", slow)
    monkeypatch.setattr(helpers._server, "shutdown_request", shutdown_request)
    port = urllib.parse.urlsplit(helpers.env[runner.LOG_URL_ENV]).port
    try:
        client = socket.create_connection(("127.0.0.1", port), timeout=10)
        client.sendall(b"POST /log HTTP/1.1\r\nHost: x\r\nContent-Length: 2\r\n\r\n{}")
        # Linger 0 makes close send a reset, as a killed curl does.
        client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        client.close()
        left.set()
        assert done.wait(10)
    finally:
        helpers.close()

    assert "Traceback" not in capfd.readouterr().err


def test_the_metric_command_is_the_one_process_the_runner_may_start(tmp_path: Path) -> None:
    """The audit hook lets exactly one start of the command through; a second start or another command is refused."""
    script = (
        "import subprocess, sys\n"
        f"sys.path.insert(0, {str(Path(runner.__file__).parent)!r})\n"
        "import runner\n"
        "refused = runner.refuse_processes(allow=['true', 'x'])\n"
        "results = []\n"
        "for argv in (['false'], ['true', 'x'], ['true', 'x']):\n"
        "    try:\n"
        "        subprocess.run(argv, check=False)\n"
        "        results.append('ran')\n"
        "    except runner.ScorerError:\n"
        "        results.append('refused')\n"
        "print(results, len(refused))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60, cwd=tmp_path, check=False
    )

    assert completed.stdout.strip() == "['refused', 'ran', 'refused'] 2", completed.stderr


def test_without_an_allowed_command_every_process_start_is_refused(tmp_path: Path) -> None:
    """A Python metric keeps the old rule: no process start at all (E40)."""
    script = (
        "import subprocess, sys\n"
        f"sys.path.insert(0, {str(Path(runner.__file__).parent)!r})\n"
        "import runner\n"
        "refused = runner.refuse_processes()\n"
        "try:\n"
        "    subprocess.run(['true'], check=False)\n"
        "except runner.ScorerError:\n"
        "    print('refused', len(refused))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60, cwd=tmp_path, check=False
    )

    assert completed.stdout.strip() == "refused 1", completed.stderr


def test_a_python_metric_entry_calls_the_function_it_names() -> None:
    """``[metric] entry = "metric.py:grade"`` calls ``grade``, not the default ``score`` (SKY-738)."""
    code = "def score(c, case=None):\n    return 0.0\ndef grade(c, case=None):\n    return 0.5\n"

    assert load_scorer_from_code(code, entrypoint="grade")("x") == 0.5
    assert load_scorer_from_code(code, entrypoint="score")("x") == 0.0
    with pytest.raises(ScorerError, match="defines no function 'judge'"):
        load_scorer_from_code(code, entrypoint="judge")
