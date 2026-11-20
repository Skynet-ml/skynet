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
from typing import Any

import pytest

from core.billing import guest_model_proxy
from core.billing.model_gateway import ROUTE_KEY
from core.billing.usage_tags import LEGACY_USAGE_TAGS_HEADER, USAGE_TAGS_HEADER, with_tags_header
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
    """The shipped proxy sets the legacy relay names that older guest code reads, and the one model URL name."""
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


@pytest.mark.parametrize(
    "box",
    [
        # loop_tools gives the proposer box the key and the model (SKY-776).
        "proposer",
        # repo_scorer._model_env gives the candidate box the same two names.
        "candidate",
    ],
)
def test_a_stock_openai_client_reaches_the_model_through_the_model_url(tmp_path: Path, box: str) -> None:
    """In each box a stock OpenAI client on ``ASCENT_MODEL_URL`` gets answers, tool calls and a stream.

    A request for another model gets an error that names ``ASCENT_MODEL`` and never reaches the supervisor.
    """
    responses = tmp_path / "responses"
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"prefix": "P:", "response_dir": str(responses), "timeout_seconds": 10}))
    env = {**os.environ, "ASCENT_MODEL_KEY": f"{box}-key", "ASCENT_MODEL": "openai/run-model"}
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
