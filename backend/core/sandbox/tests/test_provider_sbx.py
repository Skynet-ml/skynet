"""Tests for the sbx Cloud provider, against an in-memory fake of the REST API.

No test reaches Docker: an autouse fixture swaps the HTTP transport of every
client for an ``httpx.MockTransport`` that serves a fake sbx account, so the
suite needs no network and no real token.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
import shlex
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from core.sandbox import factory
from core.sandbox.capabilities import SandboxSpecError
from core.sandbox.environment import JOB_TAG, NetworkPolicy
from core.sandbox.errors import (
    SandboxError,
    SandboxFatalError,
    SandboxInfraError,
    SandboxRejectedError,
    SandboxTransientError,
    StartTimeoutError,
)
from core.sandbox.providers import sbx
from core.sandbox.providers.sbx import SbxEnvironment
from core.sandbox.resources import ResourceMode, ResourceRequest, SandboxSpec
from core.sandbox.timeouts import PhaseTimeouts

from .test_environment import SandboxEnvironmentContract

MANAGEMENT_HOST = "connect.docker.com"
POLICY_HOST = "api.sandboxes-cloud.docker.com"
SECRET_BODY = "body-detail-that-must-not-leak"


@dataclass
class Box:
    """Hold one fake sandbox in the fake account."""

    id: str
    uid: str
    display_name: str
    body: dict[str, Any]
    status: str = "creating"
    etag: str = "etag-1"
    files: dict[str, bytes] = field(default_factory=dict)
    dirs: set[str] = field(default_factory=lambda: {"/", "/tmp"})


@dataclass
class Process:
    """Hold one fake process and the output chunks it reports."""

    chunks: list[dict[str, Any]]
    exit_code: object
    hangs: bool = False


@dataclass
class FakeCloud:
    """Hold the shared state of the fake sbx account."""

    boxes: dict[str, Box] = field(default_factory=dict)
    processes: dict[str, Process] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    policies: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    killed: list[str] = field(default_factory=list)
    failures: dict[tuple[str, str], tuple[int, str]] = field(default_factory=dict)
    final_status: str = "running"
    create_hangs: bool = False
    create_fails: int | None = None
    list_hides: int = 0
    hidden: set[str] = field(default_factory=set)
    delete_answers: list[int] = field(default_factory=list)
    process_create_hangs: bool = False
    exec_hangs: bool = False
    credential_token: str = "runtime-token"
    files_written: object = 1
    truncate_download: bool = False
    download_error: str | None = None
    output_override: dict[str, Any] | None = None
    ids: Any = field(default_factory=lambda: itertools.count(1))

    def calls(self, method: str, suffix: str) -> list[httpx.Request]:
        """Return the requests with this method whose path ends with the suffix.

        Args:
            method: The HTTP method.
            suffix: The end of the URL path.

        Returns:
            The matching requests.
        """
        return [r for r in self.requests if r.method == method and r.url.path.endswith(suffix)]

    def run(self, box: Box, body: dict[str, Any]) -> tuple[int, str, str]:
        """Run the provider's process body in a tiny shell model.

        Args:
            box: The sandbox.
            body: The JSON body of the process create call.

        Returns:
            The exit code, stdout and stderr.
        """
        assert body["cmd"][:4] == ["bash", "-c", 'exec bash -c "$1" </dev/null', "skynet"]
        words = shlex.split(body["cmd"][4])
        verb, args = words[0], words[1:]
        env = body["env"]
        if verb == "true":
            return 0, "", ""
        if verb == "false":
            return 1, "", ""
        if verb == "exit":
            return int(args[0]), "", ""
        if verb == "echo":
            return 0, " ".join(args) + "\n", ""
        if verb == "whoami":
            return 0, (body["user"] or "agent") + "\n", ""
        if verb == "printenv":
            return (0, env[args[0]] + "\n", "") if args[0] in env else (1, "", "")
        if verb == "cat":
            if args[0] in box.files:
                return 0, box.files[args[0]].decode("utf-8"), ""
            return 1, "", "No such file\n"
        if verb == "mkdir" and args[0] == "-p":
            for path in args[1:]:
                parts = path.strip("/").split("/")
                box.dirs.update("/" + "/".join(parts[: n + 1]) for n in range(len(parts)))
            return 0, "", ""
        return 127, "", f"{verb}: command not found\n"


CLOUD = FakeCloud()


def _json(status: int, payload: object, **headers: str) -> httpx.Response:
    """Build a JSON answer.

    Args:
        status: The HTTP status.
        payload: The JSON body.
        **headers: More headers.

    Returns:
        The answer.
    """
    return httpx.Response(status, json=payload, headers=headers)


def _not_found() -> httpx.Response:
    """Build a ``notFound`` 404 answer.

    Returns:
        The answer.
    """
    return _json(404, {"code": "notFound"})


def _sandbox(box: Box) -> dict[str, Any]:
    """Render a fake sandbox as the API resource.

    Args:
        box: The sandbox.

    Returns:
        The resource JSON.
    """
    return {
        "name": f"sandboxes/{box.id}",
        "uid": box.uid,
        "displayName": box.display_name,
        "core": {"status": box.status, "etag": box.etag, "endpoint": {"uri": f"https://{box.id}.runtime.test"}},
    }


def _encode(data: bytes) -> str:
    """Encode chunk data as unpadded URL-safe base64, as ProtoJSON may.

    Args:
        data: The bytes.

    Returns:
        The encoded text.
    """
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _chunks(out: str, err: str) -> list[dict[str, Any]]:
    """Build the output chunks of a finished command.

    Args:
        out: The stdout text.
        err: The stderr text.

    Returns:
        The chunks with increasing sequence numbers.
    """
    chunks = []
    for stream, text in (("stdout", out), ("stderr", err)):
        if text:
            chunks.append({"streamSequence": str(len(chunks) + 1), "stream": stream, "data": _encode(text.encode())})
    return chunks


def _download(path: str, box: Box) -> httpx.Response:
    """Build the multipart download answer for one path.

    Args:
        path: The requested path.
        box: The sandbox.

    Returns:
        The ``multipart/mixed`` answer.
    """
    boundary = "fakeboundary"

    def part(name: str, media: str, data: bytes) -> bytes:
        """Render one part."""
        head = f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\nContent-Type: {media}\r\n\r\n'
        return head.encode() + data + b"\r\n"

    if CLOUD.download_error or path not in box.files:
        code = CLOUD.download_error or "notFound"
        body = part("error", "application/json", json.dumps({"failure": {"code": code}}).encode())
    else:
        body = part("metadata", "application/json", json.dumps({"path": path, "mode": 420}).encode())
        body += part("content", "application/octet-stream", box.files[path])
    body += f"--{boundary}--\r\n".encode()
    if CLOUD.truncate_download:
        body = body[: len(body) // 2]
    return httpx.Response(200, content=body, headers={"content-type": f"multipart/mixed; boundary={boundary}"})


def _upload(request: httpx.Request, box: Box) -> httpx.Response:
    """Store the file of one multipart upload.

    Args:
        request: The upload request.
        box: The sandbox.

    Returns:
        The ``filesWritten`` answer.
    """
    boundary = request.headers["content-type"].split("boundary=")[1].encode()
    parts = request.content.split(b"--" + boundary)[1:-1]
    bodies = [p.split(b"\r\n\r\n", 1)[1][: -len(b"\r\n")] for p in parts]
    metadata = json.loads(bodies[0])
    parent = metadata["path"].rsplit("/", 1)[0] or "/"
    if parent not in box.dirs:
        return _json(404, {"code": "notFound"})
    box.files[metadata["path"]] = bodies[1]
    return _json(200, {"filesWritten": CLOUD.files_written})


async def _management(request: httpx.Request) -> httpx.Response:
    """Serve the management API.

    Args:
        request: The request.

    Returns:
        The answer.
    """
    path = request.url.path
    assert request.headers["authorization"] == "Bearer management-token"
    if request.method == "POST" and path == "/sandboxes/v1/sandboxes":
        body = json.loads(request.content)
        number = next(CLOUD.ids)
        box = Box(id=f"sb{number}", uid=f"uid/{number}", display_name=body["displayName"], body=body)
        CLOUD.boxes[box.id] = box
        if CLOUD.create_hangs:
            await asyncio.Event().wait()
        if CLOUD.create_fails is not None:
            return _json(CLOUD.create_fails, {"code": "internal"})
        return _json(200, _sandbox(box))
    if request.method == "GET" and path == "/sandboxes/v1/sandboxes":
        if CLOUD.list_hides:
            CLOUD.list_hides -= 1
            return _json(200, {"sandboxes": []})
        return _json(200, {"sandboxes": [_sandbox(box) for box in CLOUD.boxes.values()]})
    parts = path.split("/")
    box = CLOUD.boxes.get(parts[4]) if len(parts) > 4 else None
    if box is None:
        return _not_found()
    if request.method == "GET":
        if box.status == "deleting":
            del CLOUD.boxes[box.id]
            return _not_found()
        if box.id in CLOUD.hidden:
            return _not_found()
        if box.status == "creating":
            box.status = CLOUD.final_status
            return _json(200, {**_sandbox(box), "core": {**_sandbox(box)["core"], "status": "starting"}})
        return _json(200, _sandbox(box))
    if request.method == "DELETE":
        if request.headers.get("if-match") != box.etag or "idempotency-key" not in request.headers:
            return _json(412, {"code": "failedPrecondition"})
        if CLOUD.delete_answers:
            status = CLOUD.delete_answers.pop(0)
            if status == 412:
                box.etag = "etag-2"
            else:
                box.status = "deleting"
            return _json(status, {"code": "failedPrecondition" if status == 412 else "aborted"})
        box.status = "deleting"
        return _json(200, _sandbox(box))
    if path.endswith("/endpoint-credentials"):
        assert json.loads(request.content)["permissions"] == [
            "sandboxesExec",
            "sandboxesFilesRead",
            "sandboxesFilesWrite",
        ]
        return _json(200, {"token": CLOUD.credential_token, "expireTime": "2099-01-01T00:00:00Z"})
    return _json(400, {"code": "badRequest"})


async def _runtime(request: httpx.Request, box: Box) -> httpx.Response:
    """Serve the runtime API of one sandbox.

    Args:
        request: The request.
        box: The sandbox.

    Returns:
        The answer.
    """
    path = request.url.path
    assert request.headers["authorization"] == f"Bearer {CLOUD.credential_token}"
    if request.method == "POST" and path == "/v1/processes":
        body = json.loads(request.content)
        assert request.headers["idempotency-key"] == body["session"]
        if CLOUD.process_create_hangs:
            await asyncio.Event().wait()
        name = f"p{next(CLOUD.ids)}"
        if CLOUD.exec_hangs:
            CLOUD.processes[name] = Process(chunks=[], exit_code=None, hangs=True)
        else:
            code, out, err = CLOUD.run(box, body)
            CLOUD.processes[name] = Process(chunks=_chunks(out, err), exit_code=code)
        return _json(200, {"name": f"processes/{name}"})
    if path.startswith("/v1/processes/"):
        name = path.split("/")[3]
        process = CLOUD.processes[name]
        if path.endswith("/signal"):
            CLOUD.killed.append(name)
            return _json(200, {})
        if CLOUD.output_override is not None:
            return _json(200, CLOUD.output_override)
        if process.hangs:
            return _json(200, {"chunks": [], "nextResumeFrom": "0"})
        last = len(process.chunks)
        output: dict[str, Any] = {"chunks": process.chunks, "nextResumeFrom": str(last)}
        output["exited"] = {} if process.exit_code == 0 else {"exitCode": process.exit_code}
        return _json(200, output)
    if path == "/v1/files/upload":
        return _upload(request, box)
    if path == "/v1/files/download":
        return _download(request.url.params["paths"], box)
    return _json(400, {"code": "badRequest"})


async def _handle(request: httpx.Request) -> httpx.Response:
    """Route one request of any client to the fake account.

    Args:
        request: The request.

    Returns:
        The answer, or an injected failure.
    """
    CLOUD.requests.append(request)
    for (method, suffix), (status, code) in CLOUD.failures.items():
        if request.method == method and request.url.path.endswith(suffix):
            return _json(status, {"code": code, "message": SECRET_BODY}, **{"x-request-id": "req-1"})
    host = request.url.host
    if host == MANAGEMENT_HOST:
        return await _management(request)
    if host == POLICY_HOST:
        uid = request.url.raw_path.decode().split("/")[3]
        CLOUD.policies.append((uid, json.loads(request.content)))
        return _json(200, {})
    box = CLOUD.boxes.get(host.split(".")[0])
    if box is None:
        return _not_found()
    return await _runtime(request, box)


@pytest.fixture(autouse=True)
def fake_cloud(monkeypatch: pytest.MonkeyPatch) -> FakeCloud:
    """Serve every sbx call from a fresh fake account.

    Args:
        monkeypatch: The pytest monkeypatch fixture.

    Returns:
        The fake account.
    """
    global CLOUD
    CLOUD = FakeCloud()
    monkeypatch.setattr(sbx, "_make_transport", lambda: httpx.MockTransport(_handle))
    monkeypatch.setattr(sbx, "_LIVE", set())
    monkeypatch.setattr(sbx, "POLL_INTERVAL", 0.0)
    monkeypatch.setattr(sbx, "STOP_TIMEOUT", 1.0)
    monkeypatch.setattr(sbx, "ORPHAN_WAIT", 0.3)
    monkeypatch.setattr(sbx, "KILL_TIMEOUT", 1.0)
    monkeypatch.setenv("SBX_TOKEN", "management-token")
    monkeypatch.delenv("SBX_TOKEN_FILE", raising=False)
    return CLOUD


def _spec(**overrides: Any) -> SandboxSpec:
    """Return a small sbx spec.

    Args:
        **overrides: Spec fields to replace.

    Returns:
        The spec.
    """
    return SandboxSpec(**{"lifetime_seconds": 300, "resources": ResourceRequest(cpus=1), **overrides})


class TestSbxContract(SandboxEnvironmentContract):
    """Run the provider contract against the fake sbx account."""

    provider_cls = SbxEnvironment


async def test_start_sends_the_create_body_once() -> None:
    """Create one sandbox with the shape, platform, lifetime and idempotency key."""
    async with SbxEnvironment(_spec(name="trial", image="python:3.12", lifetime_seconds=99.5)):
        (create,) = CLOUD.calls("POST", "/v1/sandboxes")
        body = json.loads(create.content)
        assert create.headers["idempotency-key"]
        assert body["displayName"].startswith("trial-")
        assert body["imageRef"] == "python:3.12"
        assert body["resources"] == {"cpus": 1, "memoryMib": 2048}
        assert body["platform"] == {"os": "linux", "architecture": "amd64"}
        assert body["features"]["timeouts"] == {"timeout": "100s", "onTimeout": "delete"}


async def test_default_image_and_lifetime_cap(caplog: pytest.LogCaptureFixture) -> None:
    """Use the shell template image, and cap the lifetime at 12 hours with a warning."""
    async with SbxEnvironment(_spec(lifetime_seconds=10**6)):
        body = json.loads(CLOUD.calls("POST", "/v1/sandboxes")[0].content)
        assert body["imageRef"] == sbx.DEFAULT_IMAGE
        assert body["features"]["timeouts"]["timeout"] == "43200s"
    assert "caps the sandbox lifetime at 43200 seconds; the spec asks for 1000000" in caplog.text


def test_lifetime_within_the_limit_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    """Log no cap warning for a lifetime of exactly 12 hours."""
    SbxEnvironment(_spec(lifetime_seconds=43_200))
    assert "caps the sandbox lifetime" not in caplog.text


@pytest.mark.parametrize(
    ("resources", "shape"),
    [
        (ResourceRequest(cpus=1), {"cpus": 1, "memoryMib": 2048}),
        (ResourceRequest(cpus=3), {"cpus": 4, "memoryMib": 8192}),
        (ResourceRequest(cpus=1, memory_mb=3000), {"cpus": 2, "memoryMib": 4096}),
        (ResourceRequest(cpus=64, mode=ResourceMode.IGNORE), {"cpus": 1, "memoryMib": 2048}),
    ],
)
async def test_resources_map_to_the_first_fitting_shape(resources: ResourceRequest, shape: dict[str, int]) -> None:
    """Pick the smallest sbx shape that holds the request."""
    async with SbxEnvironment(_spec(resources=resources)):
        assert json.loads(CLOUD.calls("POST", "/v1/sandboxes")[0].content)["resources"] == shape


def test_rejects_memory_above_every_shape() -> None:
    """Reject a memory size that no sbx shape holds."""
    with pytest.raises(SandboxSpecError, match="shape"):
        SbxEnvironment(_spec(resources=ResourceRequest(cpus=1, memory_mb=40_000)))


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (429, SandboxTransientError),
        (503, SandboxTransientError),
        (401, SandboxFatalError),
        (403, SandboxFatalError),
        (400, SandboxRejectedError),
        (409, SandboxRejectedError),
        (422, SandboxRejectedError),
        (418, SandboxInfraError),
    ],
)
async def test_http_errors_map_to_groups_with_no_retry(status: int, error: type[Exception]) -> None:
    """Map each status to one error group, send it once and keep the body out."""
    CLOUD.failures[("POST", "/v1/sandboxes")] = (status, "someCode")
    env = SbxEnvironment(_spec())
    with pytest.raises(error) as caught:
        await env.start()
    assert len(CLOUD.calls("POST", "/v1/sandboxes")) == 1
    assert f"HTTP {status}, someCode, request req-1" in str(caught.value)
    assert SECRET_BODY not in str(caught.value)


async def test_unsafe_error_code_and_request_id_are_hidden() -> None:
    """Replace an unsafe error code and drop an unsafe request id."""
    responses = iter([_json(500, {"code": "bad code!"}, **{"x-request-id": "bad id with spaces"})])
    with pytest.raises(SandboxTransientError) as caught:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: next(responses))) as client:
            await sbx._call(client, "GET", "https://x.test/", action="read")
    assert str(caught.value) == "sbx could not read (HTTP 500, unknown)."


async def test_transport_error_is_transient() -> None:
    """Map a connection failure to the transient group."""

    def fail(request: httpx.Request) -> httpx.Response:
        """Fail the connection."""
        raise httpx.ConnectError("refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        with pytest.raises(SandboxTransientError, match="ConnectError"):
            await sbx._call(client, "GET", "https://x.test/", action="read")


async def test_start_timeout_deletes_the_lost_sandbox() -> None:
    """Raise StartTimeoutError and delete the sandbox whose create never answered."""
    CLOUD.create_hangs = True
    env = SbxEnvironment(_spec(), timeouts=PhaseTimeouts(start=0.2, setup=30, run=30, score=30))
    with pytest.raises(StartTimeoutError):
        await env.start()
    assert CLOUD.boxes == {}
    assert CLOUD.calls("DELETE", "")


async def test_lost_create_finds_a_sandbox_the_list_shows_late() -> None:
    """Poll the list until the sandbox of a timed-out create shows, then delete it."""
    CLOUD.create_hangs = True
    CLOUD.list_hides = 3
    env = SbxEnvironment(_spec(), timeouts=PhaseTimeouts(start=0.2, setup=30, run=30, score=30))
    with pytest.raises(StartTimeoutError):
        await env.start()
    # Three empty polls, the poll that finds it, and the list check of the final 404.
    assert len(CLOUD.calls("GET", "/v1/sandboxes")) == 5
    assert CLOUD.boxes == {}
    assert len(CLOUD.calls("DELETE", "")) == 1


@pytest.mark.parametrize("status", [500, 503])
async def test_create_5xx_deletes_the_sandbox_it_made(status: int) -> None:
    """Treat a 5xx create answer as unknown, and delete the sandbox the list shows late."""
    CLOUD.create_fails = status
    CLOUD.list_hides = 2
    with pytest.raises(SandboxTransientError):
        await SbxEnvironment(_spec()).start()
    assert len(CLOUD.calls("POST", "/v1/sandboxes")) == 1
    assert CLOUD.boxes == {}
    assert len(CLOUD.calls("DELETE", "")) == 1


async def test_cancelled_create_deletes_the_sandbox_it_made() -> None:
    """Delete the sandbox of a cancelled create, also when the list shows it late."""
    CLOUD.create_hangs = True
    CLOUD.list_hides = 2
    env = SbxEnvironment(_spec())
    task = asyncio.ensure_future(env.start())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await env.stop()
    assert CLOUD.boxes == {}
    assert len(CLOUD.calls("DELETE", "")) == 1


@pytest.mark.parametrize("status", [400, 409, 422, 429])
async def test_definite_4xx_create_looks_for_no_sandbox(status: int) -> None:
    """Send no list and no delete after a 4xx create answer."""
    CLOUD.failures[("POST", "/v1/sandboxes")] = (status, "someCode")
    env = SbxEnvironment(_spec())
    with pytest.raises(SandboxError):
        await env.start()
    await env.stop()
    assert CLOUD.calls("GET", "/v1/sandboxes") == []
    assert CLOUD.calls("DELETE", "") == []


async def test_lost_create_with_no_sandbox_logs_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    """Stop polling after ORPHAN_WAIT and log that the sandbox may leak."""
    CLOUD.failures[("POST", "/v1/sandboxes")] = (503, "unavailable")
    with pytest.raises(SandboxTransientError):
        await SbxEnvironment(_spec()).start()
    assert "found no sandbox of the lost create" in caplog.text
    assert CLOUD.calls("DELETE", "") == []


async def test_start_fails_when_the_sandbox_does_not_run() -> None:
    """Raise SandboxInfraError for a failed sandbox and delete it."""
    CLOUD.final_status = "failed"
    env = SbxEnvironment(_spec())
    with pytest.raises(SandboxInfraError, match="did not start: failed"):
        await env.start()
    assert CLOUD.boxes == {}


async def test_invalid_runtime_token_fails_the_start() -> None:
    """Reject a runtime token with a space and delete the sandbox."""
    CLOUD.credential_token = "two words"
    with pytest.raises(SandboxInfraError, match="invalid sandbox token"):
        await SbxEnvironment(_spec()).start()
    assert CLOUD.boxes == {}


async def test_start_runs_only_once() -> None:
    """Refuse a second start of the same environment."""
    env = SbxEnvironment(_spec())
    async with env:
        pass
    with pytest.raises(SandboxInfraError, match="already started"):
        await env.start()


async def test_stop_deletes_with_the_etag() -> None:
    """Send the delete with If-Match and wait until the sandbox is gone."""
    env = SbxEnvironment(_spec())
    await env.start()
    await env.stop()
    (delete,) = CLOUD.calls("DELETE", "")
    assert delete.headers["if-match"] == "etag-1"
    assert delete.headers["idempotency-key"]
    assert CLOUD.boxes == {}
    with pytest.raises(SandboxInfraError, match="not running"):
        await env.exec("true")


async def test_stop_checks_a_not_found_read_against_the_list() -> None:
    """Delete a sandbox that the list shows while its own route answers 404."""
    env = SbxEnvironment(_spec())
    await env.start()
    (box,) = CLOUD.boxes.values()
    CLOUD.hidden.add(box.id)
    await env.stop()
    assert len(CLOUD.calls("DELETE", "")) == 1
    assert CLOUD.boxes == {}


async def test_stop_refreshes_the_etag_after_a_412() -> None:
    """Read the sandbox again after a 412 and delete it with the new ETag."""
    env = SbxEnvironment(_spec())
    await env.start()
    CLOUD.delete_answers = [412]
    await env.stop()
    first, second = CLOUD.calls("DELETE", "")
    assert (first.headers["if-match"], second.headers["if-match"]) == ("etag-1", "etag-2")
    assert CLOUD.boxes == {}


async def test_stop_waits_when_a_409_shows_another_delete() -> None:
    """Wait for the sandbox to go when a 409 answer shows that it is deleting."""
    env = SbxEnvironment(_spec())
    await env.start()
    CLOUD.delete_answers = [409]
    await env.stop()
    assert len(CLOUD.calls("DELETE", "")) == 1
    assert CLOUD.boxes == {}


async def test_stop_logs_a_second_precondition_failure(caplog: pytest.LogCaptureFixture) -> None:
    """Log the delete as failed when the refreshed delete also gets a 412."""
    env = SbxEnvironment(_spec())
    await env.start()
    CLOUD.delete_answers = [412, 412]
    await env.stop()
    assert len(CLOUD.calls("DELETE", "")) == 2
    assert "could not delete the sandbox: SandboxInfraError" in caplog.text


async def test_undecodable_answer_is_an_infra_error() -> None:
    """Map an answer body that httpx cannot decode to the infra group."""

    def broken(_: httpx.Request) -> httpx.Response:
        """Answer with a gzip header and a body that is not gzip."""
        return httpx.Response(200, content=b"not gzip", headers={"content-encoding": "gzip"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(broken)) as client:
        with pytest.raises(SandboxInfraError, match="DecodingError"):
            await sbx._call(client, "GET", "https://x.test/", action="read")


async def test_stop_logs_a_failed_delete(caplog: pytest.LogCaptureFixture) -> None:
    """Log, not raise, a delete that the API refuses."""
    env = SbxEnvironment(_spec())
    await env.start()
    CLOUD.failures[("DELETE", "")] = (500, "internal")
    await env.stop()
    assert "could not delete the sandbox: SandboxTransientError" in caplog.text


async def test_run_timeout_kills_the_command() -> None:
    """Return a timed-out result and kill a command that runs past its limit."""
    async with SbxEnvironment(_spec()) as env:
        CLOUD.exec_hangs = True
        result = await env.exec("sleep 100", timeout_sec=0.1)
        assert (result.exit_code, result.timed_out) == (124, True)
        assert CLOUD.killed == list(CLOUD.processes)


async def test_run_timeout_bounds_a_hanging_command_start() -> None:
    """Return a timed-out result when the process create call does not answer."""
    async with SbxEnvironment(_spec()) as env:
        CLOUD.process_create_hangs = True
        result = await env.exec("sleep 100", timeout_sec=0.1)
        assert (result.exit_code, result.timed_out) == (124, True)
        assert CLOUD.killed == []


async def test_cancelled_exec_kills_the_command() -> None:
    """Kill the command when the caller cancels exec."""
    async with SbxEnvironment(_spec()) as env:
        CLOUD.exec_hangs = True
        task = asyncio.ensure_future(env.exec("sleep 100"))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert CLOUD.killed == list(CLOUD.processes)


async def test_exec_sends_cwd_merged_env_and_root_user() -> None:
    """Send the working directory, the spec env under the call env, and root for sudo."""
    async with SbxEnvironment(_spec(env={"A": "spec", "B": "spec"})) as env:
        assert (await env.exec("whoami", sudo=True)).stdout == "root\n"
        body = json.loads(CLOUD.calls("POST", "/v1/processes")[-1].content)
        assert body["user"] == "root"
        await env.exec("true", cwd="/work", env={"B": "call"})
        body = json.loads(CLOUD.calls("POST", "/v1/processes")[-1].content)
        assert body["workingDir"] == "/work"
        assert body["env"] == {"A": "spec", "B": "call"}
        assert body["user"] == ""


async def test_exec_rejects_an_invalid_env_name() -> None:
    """Reject an environment variable name before any process starts."""
    async with SbxEnvironment(_spec()) as env:
        with pytest.raises(SandboxRejectedError):
            await env.exec("true", env={"BAD-NAME": "x"})
        assert CLOUD.calls("POST", "/v1/processes") == []


async def test_output_decodes_split_utf8_and_skips_repeats() -> None:
    """Stream a character split across chunks and skip a repeated chunk."""
    data = "בטא\n".encode()
    CLOUD.output_override = {
        "chunks": [
            {"streamSequence": 1, "stream": "stdout", "data": _encode(data[:1])},
            {"streamSequence": 1, "stream": "stdout", "data": _encode(data[:1])},
            {"streamSequence": "2", "stream": "stdout", "data": base64.b64encode(data[1:]).decode()},
            {"streamSequence": 3, "stream": "stderr", "data": _encode(b"warn")},
        ],
        "nextResumeFrom": 3,
        "exited": {"exitCode": -1},
    }
    seen: list[tuple[str, str]] = []
    async with SbxEnvironment(_spec()) as env:
        result = await env.exec("anything", on_output=lambda stream, text: seen.append((stream, text)))
    assert result.exit_code == -1
    assert result.stdout == "בטא\n"
    assert result.stderr == "warn"
    assert "".join(text for stream, text in seen if stream == "stdout") == "בטא\n"


@pytest.mark.parametrize(
    "output",
    [
        {"chunks": [], "nextResumeFrom": 5, "exited": {}},
        {"chunks": [{"streamSequence": 0, "stream": "stdout", "data": ""}], "nextResumeFrom": 0},
        {"chunks": [{"streamSequence": 1, "stream": "other", "data": ""}], "nextResumeFrom": 1},
        {"chunks": [{"streamSequence": 1, "stream": "stdout", "data": "!!"}], "nextResumeFrom": 1},
        {"chunks": [], "nextResumeFrom": 0, "exited": {"exitCode": 2**31}},
        {"chunks": [], "nextResumeFrom": 0, "exited": {"exitCode": "1"}},
    ],
)
async def test_invalid_output_is_an_infra_error(output: dict[str, Any]) -> None:
    """Reject a cursor mismatch, a bad chunk or an invalid exit code, and kill the command."""
    CLOUD.output_override = output
    async with SbxEnvironment(_spec()) as env:
        with pytest.raises(SandboxInfraError):
            await env.exec("true")
        assert CLOUD.killed == list(CLOUD.processes)


async def test_output_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail a command whose output runs past the limit."""
    monkeypatch.setattr(sbx, "MAX_OUTPUT_BYTES", 3)
    async with SbxEnvironment(_spec()) as env:
        with pytest.raises(SandboxInfraError, match="more than 3 bytes"):
            await env.exec("echo hello")


async def test_upload_sends_one_multipart_file() -> None:
    """Send the metadata and content parts with the file mode."""
    async with SbxEnvironment(_spec()) as env:
        await env.upload_files({"/tmp/a.txt": "alpha"})
        (upload,) = CLOUD.calls("POST", "/v1/files/upload")
        boundary = upload.headers["content-type"].split("boundary=")[1]
        assert (
            upload.content
            == (
                f'--{boundary}\r\nContent-Disposition: form-data; name="metadata"\r\n'
                'Content-Type: application/json\r\n\r\n{"path":"/tmp/a.txt","mode":420}'
                f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="content"\r\n'
                f"Content-Type: application/octet-stream\r\n\r\nalpha\r\n--{boundary}--\r\n"
            ).encode()
        )
        (mkdir,) = CLOUD.calls("POST", "/v1/processes")
        assert json.loads(mkdir.content)["cmd"][4] == "mkdir -p /tmp"


async def test_upload_needs_one_written_file() -> None:
    """Fail an upload that sbx does not confirm."""
    CLOUD.files_written = 0
    async with SbxEnvironment(_spec()) as env:
        with pytest.raises(SandboxInfraError, match="exactly one"):
            await env.upload_files({"/tmp/a.txt": "alpha"})


async def test_truncated_download_is_an_infra_error() -> None:
    """Reject a download that ends before the closing boundary."""
    async with SbxEnvironment(_spec()) as env:
        await env.upload_files({"/tmp/a.txt": "alpha"})
        CLOUD.truncate_download = True
        with pytest.raises(SandboxInfraError):
            await env.download_file("/tmp/a.txt")


async def test_download_error_part_is_an_infra_error() -> None:
    """Report a download error other than not found."""
    async with SbxEnvironment(_spec()) as env:
        CLOUD.download_error = "permissionDenied"
        with pytest.raises(SandboxInfraError, match="permissionDenied"):
            await env.download_file("/tmp/a.txt")


async def test_download_not_found_answer_returns_none() -> None:
    """Return None for a notFound 404 download answer."""
    async with SbxEnvironment(_spec()) as env:
        CLOUD.failures[("GET", "/v1/files/download")] = (404, "notFound")
        assert await env.download_file("/tmp/missing") is None


async def test_download_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail a download whose answer runs past the size limit (SKY-535)."""
    async with SbxEnvironment(_spec()) as env:
        await env.upload_files({"/tmp/a.txt": "alpha"})
        assert await env.download_file("/tmp/a.txt") == "alpha"
        monkeypatch.setattr(sbx, "MAX_DOWNLOAD_BYTES", 16)
        with pytest.raises(SandboxInfraError, match="above the 16 byte limit"):
            await env.download_file("/tmp/a.txt")


@pytest.mark.parametrize(
    ("status", "error"),
    [(503, SandboxTransientError), (403, SandboxFatalError), (404, SandboxInfraError)],
)
async def test_download_error_status(status: int, error: type[SandboxError]) -> None:
    """Map a streamed download error status to the error of its group."""
    async with SbxEnvironment(_spec()) as env:
        CLOUD.failures[("GET", "/v1/files/download")] = (status, "otherError")
        with pytest.raises(error, match=f"HTTP {status}, otherError"):
            await env.download_file("/tmp/a.txt")


@pytest.mark.parametrize(
    ("spec_fields", "body"),
    [
        ({"network_disabled": True}, {"mode": "deny-all"}),
        ({"allowed_hosts": ("pypi.org", "pypi.org")}, {"mode": "deny-all", "allowNetworks": ["pypi.org"]}),
    ],
)
async def test_start_sets_the_network_policy(spec_fields: dict[str, Any], body: dict[str, Any]) -> None:
    """Send the spec policy to the quoted uid URL with the management token."""
    async with SbxEnvironment(_spec(**spec_fields)):
        assert CLOUD.policies == [("uid%2F1", body)]
        (put,) = CLOUD.calls("PUT", "/network-policy")
        assert put.headers["authorization"] == "Bearer management-token"


async def test_open_spec_sends_no_policy_and_change_sends_allow_all() -> None:
    """Send no policy for open egress, then send each change."""
    async with SbxEnvironment(_spec()) as env:
        assert CLOUD.policies == []
        await env.disable_network()
        await env.set_network_policy(NetworkPolicy())
        assert [body for _, body in CLOUD.policies] == [{"mode": "deny-all"}, {"mode": "allow-all"}]


async def test_stop_job_sandboxes_counts_running_sandboxes() -> None:
    """Count only the started sandboxes of the job."""
    started = SbxEnvironment(_spec(tags={JOB_TAG: "job-1"}))
    idle = SbxEnvironment(_spec(tags={JOB_TAG: "job-1"}))
    await started.start()
    assert await SbxEnvironment.stop_job_sandboxes("job-1") == 1
    assert await SbxEnvironment.stop_job_sandboxes("job-1") == 0
    await idle.stop()


@pytest.mark.parametrize("name", ["other/x", "sandboxes/", "sandboxes/a/b", "sandboxes/..", None])
def test_resource_path_rejects_invalid_names(name: object) -> None:
    """Reject a resource name that is not one plain id of the collection."""
    with pytest.raises(SandboxInfraError):
        sbx._resource_path(name, "sandboxes")


def test_resource_path_quotes_the_id() -> None:
    """Quote the resource id in the path."""
    assert sbx._resource_path("sandboxes/a b", "sandboxes") == "/v1/sandboxes/a%20b"


def test_preflight_needs_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail when python-multipart is missing."""
    monkeypatch.setattr(sbx, "MultipartParser", None)
    with pytest.raises(SandboxFatalError, match="`sbx` extra"):
        SbxEnvironment.preflight()


@pytest.mark.parametrize("token", ["", "   ", "has space", "tab\tin", "del\x7f"])
def test_preflight_rejects_an_invalid_token(monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    """Reject an empty token or one with control or space characters."""
    monkeypatch.setenv("SBX_TOKEN", token)
    with pytest.raises(SandboxFatalError, match="SBX_TOKEN"):
        SbxEnvironment.preflight()


def test_preflight_rejects_a_missing_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail when no token is set."""
    monkeypatch.delenv("SBX_TOKEN")
    with pytest.raises(SandboxFatalError, match="SBX_TOKEN_FILE"):
        SbxEnvironment.preflight()


async def test_token_file_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """Read the token file before the token variable."""
    token_file = tmp_path / "token"
    token_file.write_text("management-token\n")
    monkeypatch.setenv("SBX_TOKEN", "other")
    monkeypatch.setenv("SBX_TOKEN_FILE", str(token_file))
    async with SbxEnvironment(_spec()) as env:
        assert (await env.exec("true")).ok


def test_unreadable_token_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """Fail when the token file cannot be read, without its path in the message."""
    monkeypatch.setenv("SBX_TOKEN_FILE", str(tmp_path / "missing"))
    with pytest.raises(SandboxFatalError, match="FileNotFoundError"):
        SbxEnvironment.preflight()


def test_factory_registers_sbx_for_development_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register sbx with production_allowed=False and refuse it in production."""
    entry = factory.PROVIDERS["sbx"]
    assert entry.production_allowed is False
    monkeypatch.setattr(factory.settings, "app_environment", "staging")
    assert factory.load_provider("sbx") is SbxEnvironment
    monkeypatch.setattr(factory.settings, "app_environment", "production")
    with pytest.raises(factory.ProductionNotAllowedError, match="sbx"):
        factory.load_provider("sbx")
