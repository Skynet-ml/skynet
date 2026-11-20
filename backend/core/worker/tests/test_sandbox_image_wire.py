"""Pin the names the worker and the pinned sandbox image's backend copy exchange.

The DSPy guest runs the image's own ``/app`` copy of the backend, and a resumed
run keeps the image it first opened, so a newer worker talks to older guest
code (and the reverse while an image re-pin rolls out). Each name here crosses
that boundary; changing one breaks every run on an older image.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.billing import guest_model_proxy
from core.billing.model_gateway import ROUTE_KEY
from core.billing.usage_tags import LEGACY_USAGE_TAGS_HEADER, USAGE_TAGS_HEADER, with_tags_header
from core.models.blackbox import BlackboxProposer
from core.service_gateway.optimization.blackbox import loop_tools, repo_scorer, runner
from core.service_gateway.optimization.blackbox.protocol import RepoSeed
from core.service_gateway.optimization.blackbox.sandbox_scorer import ScorerGateway
from core.worker import isolated_runner, vercel_dspy
from core.worker.sandbox_protocol import READABLE_PROTOCOLS, SANDBOX_PROTOCOL


def test_guest_event_framing_and_protocol_keep_their_image_names() -> None:
    """The guest frames events with the prefix older images print and the worker parses."""
    assert isolated_runner.EVENT_PREFIX == "SKYNET_JOB_EVENT "
    assert vercel_dspy.EVENT_PREFIX == isolated_runner.EVENT_PREFIX
    assert SANDBOX_PROTOCOL == 22
    # A bump must decide again which older images can read the new request. An
    # image before 22 uses the old model variable names (SKY-776).
    assert {22} == READABLE_PROTOCOLS


def test_payload_route_keys_keep_their_image_names() -> None:
    """Older guests pop these request keys by name."""
    assert ROUTE_KEY == "_skynet_budget_route"
    source = Path(isolated_runner.__file__).read_text(encoding="utf-8")
    for key in ("_skynet_tools_route", "_skynet_packages_route"):
        assert f'"{key}"' in source


def test_usage_tags_travel_under_both_header_names() -> None:
    """An older proxy forwards, and an older gateway reads, only the legacy header."""
    headers = with_tags_header({}, {"stage": "training"})
    assert headers[LEGACY_USAGE_TAGS_HEADER] == headers[USAGE_TAGS_HEADER]
    assert LEGACY_USAGE_TAGS_HEADER == "x-skynet-usage-tags"


def test_model_proxy_hands_its_command_both_relay_variable_names(tmp_path: Path) -> None:
    """The shipped proxy sets the legacy relay names that older guest code reads, and the one model URL name.

    Args:
        tmp_path: Folder for the proxy's config and its answer mailbox.
    """
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"prefix": "P:", "response_dir": str(tmp_path / "responses"), "timeout_seconds": 5}))
    probe = (
        "import json, os; print(json.dumps({name: os.environ.get(name) for name in "
        "('APP_BUDGET_RELAY_URL', 'SKYNET_BUDGET_RELAY_URL', 'ASCENT_MODEL_URL')}))"
    )
    done = subprocess.run(
        [sys.executable, guest_model_proxy.__file__, str(config), sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    seen = json.loads(done.stdout.strip().splitlines()[-1])
    assert all(value and value.startswith("http://127.0.0.1:") for value in seen.values()), seen
    assert seen["APP_BUDGET_RELAY_URL"] == seen["SKYNET_BUDGET_RELAY_URL"]


# A stock OpenAI client in the box: plain, tool, streamed and wrong-model chat calls.
_CLIENT_PROBE = """
import json, os
import openai
client = openai.OpenAI(base_url=os.environ["ASCENT_MODEL_URL"], api_key=os.environ["ASCENT_MODEL_KEY"], max_retries=0)
model = os.environ["ASCENT_MODEL"]
ask = [{"role": "user", "content": "hi"}]
seen = {}
answer = client.chat.completions.create(model=model, messages=ask, max_tokens=5, temperature=0)
seen["text"] = answer.choices[0].message.content
tool = {"type": "function", "function": {"name": "lookup", "parameters": {"type": "object", "properties": {}}}}
answer = client.chat.completions.create(model=model, messages=ask, tools=[tool])
seen["tool"] = answer.choices[0].message.tool_calls[0].function.name
stream = client.chat.completions.create(model=model, messages=ask, stream=True)
seen["stream"] = "".join(chunk.choices[0].delta.content or "" for chunk in stream if chunk.choices)
try:
    client.chat.completions.create(model="other/model", messages=ask)
except openai.BadRequestError as exc:
    seen["other"] = [exc.status_code, exc.body["message"]]
print("RESULT:" + json.dumps(seen), flush=True)
"""


def _model_answer(request: dict[str, Any]) -> dict[str, Any]:
    """Answer one mailbox request the way the supervisor relays a model's answer.

    Args:
        request: The request document that the proxy printed.

    Returns:
        The response document: a tool call, a streamed body or a plain answer.
    """
    body = json.loads(base64.b64decode(request["body"]))
    if body.get("stream"):
        chunk = {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": body["model"]}
        parts = [{**chunk, "choices": [{"index": 0, "delta": {"content": word}}]} for word in ("str", "eamed")]
        content = "".join(f"data: {json.dumps(part)}\n\n" for part in parts) + "data: [DONE]\n\n"
        return {"status": 200, "content_type": "text/event-stream", "body": base64.b64encode(content.encode()).decode()}
    message: dict[str, Any] = {"role": "assistant", "content": "answered"}
    if body.get("tools"):
        call = {"id": "t1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}
        message = {"role": "assistant", "content": None, "tool_calls": [call]}
    answer = {
        "id": "c",
        "object": "chat.completion",
        "created": 0,
        "model": body["model"],
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
    }
    return {
        "status": 200,
        "content_type": "application/json",
        "body": base64.b64encode(json.dumps(answer).encode()).decode(),
    }


def _box_env(box: str) -> dict[str, str]:
    """Build the model env that the real code gives a protected run's box.

    Args:
        box: ``proposer`` for the ``[proposer] entry`` box, ``candidate`` for the version's own box.

    Returns:
        The env that the box's model proxy and command get.
    """
    key = f"{box}-key"
    if box == "proposer":
        ctx = SimpleNamespace(repo_seed=RepoSeed(tree_id="seed"))
        options = SimpleNamespace(
            model="openai/run-model",
            proposer=BlackboxProposer(run_command="my-agent"),
            gateway=SimpleNamespace(url="", api_key=key),
            budget_route={"token": key},
            repo={"file_proposer": True},
        )
        env = loop_tools.FileProposer(ctx, options, ".lab")._env  # type: ignore[arg-type]
        assert env is not None
        return env
    gateway = ScorerGateway(url="", model="openai/run-model", api_key=key, billing_model="openai/run-model")
    return repo_scorer.RepoScorer._model_env(SimpleNamespace(_gateway=gateway))  # type: ignore[arg-type]


@pytest.mark.parametrize("box", ["proposer", "candidate"])
def test_a_stock_openai_client_reaches_the_model_through_the_model_url(tmp_path: Path, box: str) -> None:
    """In each box a stock OpenAI client on ``ASCENT_MODEL_URL`` gets answers, tool calls and a stream.

    A request for another model gets an error that names ``ASCENT_MODEL`` and never reaches the supervisor.

    Args:
        tmp_path: Folder for the proxy's config and its answer mailbox.
        box: The box whose real env builder gives the model env.
    """
    responses = tmp_path / "responses"
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"prefix": "P:", "response_dir": str(responses), "timeout_seconds": 10}))
    env = {**os.environ, **_box_env(box)}
    proxy = subprocess.Popen(
        [sys.executable, guest_model_proxy.__file__, str(config), sys.executable, "-c", _CLIENT_PROBE],
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    )
    requests: list[dict[str, Any]] = []
    result: dict[str, Any] = {}
    try:
        assert proxy.stdout is not None
        for line in proxy.stdout:
            if line.startswith("P:"):
                request = json.loads(line[2:])
                requests.append(request)
                staged = responses / f"{request['id']}.tmp"
                staged.write_text(json.dumps(_model_answer(request)))
                staged.rename(responses / f"{request['id']}.json")
            elif line.startswith("RESULT:"):
                result = json.loads(line[len("RESULT:") :])
        assert proxy.wait(timeout=30) == 0
    finally:
        proxy.kill()

    assert result["text"] == "answered"
    assert result["tool"] == "lookup"
    assert result["stream"] == "streamed"
    status, message = result["other"]
    assert status == 400
    assert "ASCENT_MODEL" in message
    assert "other/model" in message
    assert len(requests) == 3
    assert {request["path"] for request in requests} == {"/v1/chat/completions"}
    assert {request["headers"]["Authorization"] for request in requests} == {f"Bearer {box}-key"}


@pytest.mark.parametrize(
    "model", ["openai/run-model", "litellm_proxy/openai/run-model", "openrouter/openai/run-model", " openai/run-model/"]
)
def test_the_proxy_lets_through_every_name_of_the_run_model(monkeypatch: pytest.MonkeyPatch, model: str) -> None:
    """A transport prefix names the same model, so the box does not refuse it (SKY-776).

    Args:
        monkeypatch: Sets ``ASCENT_MODEL`` for the proxy.
        model: A name of the run's model.
    """
    monkeypatch.setenv(guest_model_proxy.MODEL_ENV, "openai/run-model")

    assert guest_model_proxy._other_model_refusal("/v1/chat/completions", json.dumps({"model": model}).encode()) is None


@pytest.mark.parametrize("model", ["other/model", "litellm_proxy/other/model", None])
def test_the_proxy_refuses_another_model_and_names_the_model_variable(
    monkeypatch: pytest.MonkeyPatch, model: str | None
) -> None:
    """A request for another model, with or without a prefix, gets the error that names ``ASCENT_MODEL``.

    Args:
        monkeypatch: Sets ``ASCENT_MODEL`` for the proxy.
        model: A name of another model, or no model.
    """
    monkeypatch.setenv(guest_model_proxy.MODEL_ENV, "openai/run-model")

    refusal = guest_model_proxy._other_model_refusal("/v1/chat/completions", json.dumps({"model": model}).encode())

    assert refusal is not None
    error = json.loads(refusal)["error"]
    assert error["code"] == "model_not_allowed"
    assert "ASCENT_MODEL" in error["message"]


@pytest.mark.parametrize(
    ("model", "bound"),
    [
        ("openai/m", "openai/m"),
        ("litellm_proxy/openai/m", "openai/m"),
        ("openrouter/openai/m", "litellm_proxy/openai/m"),
        ("/openai/m/", "openai/m"),
        ("openai/other", "openai/m"),
        ("m", "openai/m"),
    ],
)
def test_the_proxy_matches_model_names_by_the_gateway_rule(model: str, bound: str) -> None:
    """The proxy's copy of the rule gives the same answer as ``runner.model_matches_bound``.

    Args:
        model: The name that a request gives.
        bound: The run's model.
    """
    assert guest_model_proxy.model_matches(model, bound) == runner.model_matches_bound(model, bound)
