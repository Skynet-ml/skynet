"""Exercise parent-owned sandbox capabilities and streaming child RPC without provider I/O."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict
from typing import Any

import httpx
import pytest

from core.billing.runtime import UsagePendingError
from core.billing.signals import BudgetReached
from core.billing.vercel_usage import PACKAGE_REGISTRY_HOSTS, PROPOSER_REGISTRY_HOSTS
from core.exceptions import InfrastructureInterruptionError, ServiceError

from .. import sandbox_broker
from ..remote_sandbox import RemoteSandboxRuntime, RemoteSandboxSession
from ..sandbox import _CODEX_REQUIREMENTS_COMMAND, _CODEX_REQUIREMENTS_READBACK, CommandResult, SandboxSpec
from ..sandbox_broker import ENGINE_BOX_KEY_PREFIX, SandboxBroker

IMAGE = "vcr.example/skynet/optimizer@sha256:" + "a" * 64


class FakeSession:
    """Keep guest state separate from the parent filesystem."""

    def __init__(self, fail_close: bool = False) -> None:
        """Create an empty guest and optional uncertain settlement failure."""
        self.files: dict[str, str] = {}
        self.close_calls = 0
        self.fail_close = fail_close
        self.root_commands: list[str] = []
        self.readback = _CODEX_REQUIREMENTS_READBACK

    def write_files(self, files: dict[str, str]) -> None:
        """Store guest files without writing on the host."""
        self.files.update(files)

    def read_file(self, path: str) -> str | None:
        """Return guest text or absence."""
        return self.files.get(path)

    def run(self, command: str, **kwargs: Any) -> CommandResult:
        """Emit deterministic output through the broker's output sink, and answer a root command with a read-back."""
        if kwargs.get("sudo"):
            self.root_commands.append(command)
            return CommandResult(exit_code=0, stdout=self.readback)
        if kwargs.get("on_output"):
            kwargs["on_output"]("stdout", command)
        return CommandResult(exit_code=0, stdout=command)

    def close(self) -> None:
        """Record shutdown and retain an uncertain settlement failure when configured."""
        self.close_calls += 1
        if self.fail_close:
            raise UsagePendingError("usage pending")


class FakeRuntime:
    """Record parent-fixed resource profiles without allocating compute."""

    injects_headers = False

    def __init__(self) -> None:
        """Start with no created guest sessions."""
        self.specs: list[SandboxSpec] = []
        self.sessions: list[FakeSession] = []

    def open(self, spec: SandboxSpec) -> FakeSession:
        """Return a distinct fake guest for each admitted creation."""
        self.specs.append(spec)
        session = FakeSession()
        self.sessions.append(session)
        return session


def _open_payload(**changes: Any) -> dict[str, Any]:
    """Create a stable request with optional guest-spec changes."""
    return {"request_id": "one", "spec": {"lifetime_seconds": 60, **changes}}


def test_broker_keeps_profile_and_ownership_in_parent() -> None:
    """Reject resource overrides and cross-broker handles while making open replay safe."""
    runtime = FakeRuntime()
    broker = SandboxBroker(runtime, image=IMAGE, max_lifetime_seconds=120, tags={"skynet_job": "parent-job"})
    payload = _open_payload(tags={"skynet_job": "forged"}, network_disabled=False)
    opened = broker.handle("open", payload)
    assert broker.handle("open", payload) == opened
    assert len(runtime.sessions) == 1
    assert runtime.specs[0].network_disabled is True
    assert runtime.specs[0].tags == {"skynet_job": "parent-job"}
    assert runtime.specs[0].image == IMAGE
    assert set(opened) == {"sandbox_id"}
    for changes in (
        {"image": "mutable:latest"},
        {"vcpus": 8},
        {"lifetime_seconds": 121},
        {"inject_headers": {"host": {"key": "secret"}}},
    ):
        with pytest.raises(ServiceError):
            broker.handle("open", _open_payload(**changes))
    other = SandboxBroker(FakeRuntime(), image=IMAGE, max_lifetime_seconds=120)
    with pytest.raises(ServiceError, match="does not belong"):
        other.handle("read", {**opened, "path": "candidate.py"})
    with pytest.raises(ServiceError, match="different spec"):
        broker.handle("open", _open_payload(lifetime_seconds=90))


def test_guest_boxes_never_open_network_access() -> None:
    """Refuse every host a guest asks for, and open its boxes offline."""
    runtime = FakeRuntime()
    broker = SandboxBroker(runtime, image=IMAGE, max_lifetime_seconds=120)
    for hosts in (["evil.example"], ["api.anthropic.com"], "api.anthropic.com"):
        with pytest.raises(ServiceError, match="network access"):
            broker.handle("open", _open_payload(allowed_hosts=hosts))
    broker.handle("open", _open_payload())
    assert runtime.specs[0].network_disabled is True
    assert runtime.specs[0].inject_headers == {}


def test_the_guest_route_never_reaches_a_package_registry() -> None:
    """However the guest asks, its boxes stay offline, even while the parent's box is online."""
    runtime = FakeRuntime()
    broker = SandboxBroker(runtime, image=IMAGE, max_lifetime_seconds=120)
    parent = broker.parent_runtime(PACKAGE_REGISTRY_HOSTS).open(
        SandboxSpec(lifetime_seconds=60, allowed_hosts=PACKAGE_REGISTRY_HOSTS, operation_key="repo-scorer:job")
    )
    for hosts in (["pypi.org"], list(PACKAGE_REGISTRY_HOSTS), ["api.anthropic.com", "registry.npmjs.org"]):
        with pytest.raises(ServiceError, match="network access"):
            broker.handle("open", _open_payload(allowed_hosts=hosts))
    for action in ("parent_runtime", "_open_parent", "open_parent"):
        with pytest.raises(ServiceError):
            broker.handle(action, _open_payload(allowed_hosts=["pypi.org"]))
    assert len(runtime.specs) == 1
    assert runtime.specs[0].allowed_hosts == PACKAGE_REGISTRY_HOSTS
    assert runtime.specs[0].inject_headers == {}
    assert runtime.specs[0].env == {}
    with pytest.raises(ServiceError):
        broker.handle("run", {"sandbox_id": "0", "command": "true"})
    broker.close()
    assert runtime.sessions[0].close_calls == 1
    parent.close()


def test_a_guest_box_reaches_only_the_hosts_the_parent_granted() -> None:
    """Open a proposer box on granted registries, and refuse any host beyond them (E57)."""
    runtime = FakeRuntime()
    broker = SandboxBroker(runtime, image=IMAGE, max_lifetime_seconds=120)
    broker.allow_guest_hosts(PROPOSER_REGISTRY_HOSTS)
    payload = {"request_id": "proposer", "spec": {"lifetime_seconds": 60, "allowed_hosts": ["pypi.org", "pypi.org"]}}
    opened = broker.handle("open", payload)
    assert broker.handle("open", payload) == opened
    assert runtime.specs[0].allowed_hosts == ("pypi.org",)
    assert runtime.specs[0].network_disabled is False
    assert runtime.specs[0].inject_headers == {}
    for hosts in (["raw.githubusercontent.com"], ["pypi.org", "github.com"], "pypi.org", [1]):
        with pytest.raises(ServiceError, match="network access"):
            broker.handle("open", _open_payload(allowed_hosts=hosts))
    with pytest.raises(ServiceError, match="different spec"):
        broker.handle("open", {"request_id": "proposer", "spec": {"lifetime_seconds": 60}})
    broker.handle("open", {"request_id": "offline", "spec": {"lifetime_seconds": 60}})
    assert runtime.specs[1].network_disabled is True
    assert runtime.specs[1].allowed_hosts == ()


def test_an_empty_grant_keeps_every_guest_box_offline() -> None:
    """Treat ``network = []`` as no hosts at all, never as open egress."""
    runtime = FakeRuntime()
    broker = SandboxBroker(runtime, image=IMAGE, max_lifetime_seconds=120)
    broker.allow_guest_hosts(())
    with pytest.raises(ServiceError, match="network access"):
        broker.handle("open", _open_payload(allowed_hosts=["pypi.org"]))
    broker.handle("open", _open_payload(allowed_hosts=[]))
    assert runtime.specs[0].network_disabled is True
    for hosts in (("*",), ("",), ("*.example.com",)):
        with pytest.raises(ServiceError, match="named hosts"):
            broker.allow_guest_hosts(hosts)


def test_the_parent_box_reaches_only_funded_registries_and_carries_no_credentials() -> None:
    """Refuse non-registry hosts, hosts beyond the funded list, and credentials on the parent's box."""
    broker = SandboxBroker(FakeRuntime(), image=IMAGE, max_lifetime_seconds=120)
    with pytest.raises(ServiceError, match="package registries"):
        broker.parent_runtime(("api.anthropic.com",))
    with pytest.raises(ServiceError, match="package registries"):
        broker.parent_runtime(("pypi.org", "evil.example"))
    narrow = broker.parent_runtime(("pypi.org",))
    with pytest.raises(ServiceError, match="not funded"):
        narrow.open(SandboxSpec(lifetime_seconds=60, allowed_hosts=("registry.npmjs.org",), operation_key="k"))
    with pytest.raises(ServiceError, match="credentials"):
        narrow.open(SandboxSpec(lifetime_seconds=60, allowed_hosts=("pypi.org",), env={"A": "b"}, operation_key="k"))
    with pytest.raises(ServiceError, match="duration"):
        narrow.open(SandboxSpec(lifetime_seconds=600, allowed_hosts=("pypi.org",), operation_key="k"))


def test_a_parent_box_without_a_network_switch_fails_closed() -> None:
    """A provider session that cannot change its policy cannot be taken offline, so the switch refuses."""
    parent = SandboxBroker(FakeRuntime(), image=IMAGE, max_lifetime_seconds=120).parent_runtime(("pypi.org",))
    session = parent.open(SandboxSpec(lifetime_seconds=60, allowed_hosts=("pypi.org",), operation_key="k"))
    with pytest.raises(ServiceError, match="cannot switch"):
        session.disable_network()


def test_a_closed_parent_box_leaves_the_broker_and_the_open_one_still_closes_with_it() -> None:
    """A run opens stage boxes for each evaluation, so the broker must not keep the closed ones until the job ends."""
    runtime = FakeRuntime()
    broker = SandboxBroker(runtime, image=IMAGE, max_lifetime_seconds=120)
    parent = broker.parent_runtime(())
    first, second, third = (parent.open(SandboxSpec(lifetime_seconds=60, operation_key=key)) for key in ("a", "b", "c"))
    runtime.sessions[1].fail_close = True
    first.close()
    with pytest.raises(UsagePendingError):
        second.close()
    assert broker._parent_sessions == [third]
    broker.close()
    assert [session.close_calls for session in runtime.sessions] == [1, 1, 1]


def test_broker_rejects_host_paths_and_closes_every_session_after_failure() -> None:
    """Do not let one uncertain settlement leave other owned compute running."""
    runtime = FakeRuntime()
    broker = SandboxBroker(runtime, image=IMAGE, max_lifetime_seconds=120)
    first = broker.handle("open", _open_payload())
    broker.handle("open", {**_open_payload(), "request_id": "two"})
    for path in ("/etc/passwd", "../private", "a/../../private"):
        with pytest.raises(ServiceError):
            broker.handle("read", {**first, "path": path})
    runtime.sessions[0].fail_close = True
    with pytest.raises(UsagePendingError):
        broker.close()
    assert [session.close_calls for session in runtime.sessions] == [1, 1]
    with pytest.raises(ServiceError, match="closed"):
        broker.handle("open", {**_open_payload(), "request_id": "three"})


def test_remote_round_trip_streams_output_and_supports_callback_file_actions() -> None:
    """Retain native evaluator mailbox callbacks across the parent control stream."""
    runtime = FakeRuntime()
    wrapped = []

    def command_runner(session: FakeSession, command: str, **kwargs: Any) -> CommandResult:
        """Stand in for the parent's model mailbox command wrapper."""
        wrapped.append((session, command))
        return session.run(command, **kwargs)

    broker = SandboxBroker(runtime, image=IMAGE, max_lifetime_seconds=120, command_runner=command_runner)
    requests = []

    def parent(request: httpx.Request) -> httpx.Response:
        """Map authenticated RPC to broker actions with the production NDJSON protocol."""
        assert request.url.path == "/v1/_sandbox"
        assert request.headers["Authorization"] == "Bearer opaque-control"
        body = json.loads(request.content)
        requests.append(body)
        if body["action"] != "run":
            return httpx.Response(200, json=broker.handle(body["action"], body["payload"]))
        frames = []
        result = broker.run(
            body["payload"], lambda stream, data: frames.append({"type": "output", "stream": stream, "data": data})
        )
        frames.append({"type": "result", **asdict(result)})
        return httpx.Response(
            200,
            content="\n".join(json.dumps(frame) for frame in frames),
            headers={"content-type": "application/x-ndjson"},
        )

    with httpx.Client(transport=httpx.MockTransport(parent)) as client:
        remote = RemoteSandboxRuntime("http://127.0.0.1:9000/v1", "opaque-control", client=client)
        session = remote.open(SandboxSpec(lifetime_seconds=60))
        session.write_files({"input.txt": "hello"})
        assert session.read_file("input.txt") == "hello"
        output = []

        def on_output(stream: str, data: str) -> None:
            """Perform a nested file response while the NDJSON command is being consumed."""
            output.append((stream, data))
            session.write_files({"mailbox/response.json": "done"})

        result = session.run("candidate", on_output=on_output)
        session.close()
        session.close()
    assert result.stdout == "candidate"
    assert output == [("stdout", "candidate")]
    assert len(wrapped) == 1
    assert runtime.sessions[0].files["mailbox/response.json"] == "done"
    assert runtime.sessions[0].close_calls == 1
    assert len([request for request in requests if request["action"] == "close"]) == 1


@pytest.mark.parametrize("stream", [True, False])
def test_remote_budget_stop_is_a_control_signal_not_a_command_failure(stream: bool) -> None:
    """Propagate the parent's normal budget stop through both JSON and NDJSON responses."""

    def parent(request: httpx.Request) -> httpx.Response:
        """Return the same typed stop in either response encoding."""
        error = {"type": "error", "error_type": "BudgetReached", "message": "allowance spent"}
        return httpx.Response(200, content=json.dumps(error)) if stream else httpx.Response(409, json=error)

    with httpx.Client(transport=httpx.MockTransport(parent)) as client:
        remote = RemoteSandboxRuntime("http://127.0.0.1:9000", "opaque-control", client=client)
        with pytest.raises(BudgetReached, match="allowance spent"):
            if stream:
                RemoteSandboxSession(remote, "session", 60).run("candidate")
            else:
                remote.open(SandboxSpec(lifetime_seconds=60))


def test_remote_rejects_missing_terminal_result_without_replaying() -> None:
    """Treat a truncated command stream as uncertainty and send only one physical request."""
    requests = []

    def parent(request: httpx.Request) -> httpx.Response:
        """End after a progress frame without inventing a command result."""
        requests.append(request)
        return httpx.Response(200, content='{"type":"output","stream":"stdout","data":"started"}\n')

    with httpx.Client(transport=httpx.MockTransport(parent)) as client:
        remote = RemoteSandboxRuntime("http://127.0.0.1:9000", "opaque-control", client=client)
        with pytest.raises(InfrastructureInterruptionError, match="before its final result"):
            RemoteSandboxSession(remote, "session", 60).run("candidate")
    assert len(requests) == 1


def test_the_broker_refuses_a_root_command_from_the_child() -> None:
    """A child's run payload never reaches the box as root, whatever value it sends."""
    runtime = FakeRuntime()
    broker = SandboxBroker(runtime, image=IMAGE, max_lifetime_seconds=120)
    opened = broker.handle("open", _open_payload())
    for value in (True, 1, "yes"):
        with pytest.raises(ServiceError, match="cannot run as root"):
            broker.run({**opened, "command": "id -u", "sudo": value})
    assert runtime.sessions[0].root_commands == []
    assert broker.run({**opened, "command": "id -u", "sudo": False}).ok


def test_the_codex_requirements_action_uses_only_the_parent_command() -> None:
    """Ignore every field but the box handle, so a child cannot choose the path, content or command."""
    runtime = FakeRuntime()
    broker = SandboxBroker(runtime, image=IMAGE, max_lifetime_seconds=120)
    opened = broker.handle("open", _open_payload())
    forged = {"path": "/etc/sudoers", "content": "evil", "command": "rm -rf /", "sudo": True}
    assert broker.handle("codex_requirements", {**opened, **forged}) == {}
    assert runtime.sessions[0].root_commands == [_CODEX_REQUIREMENTS_COMMAND]
    other = SandboxBroker(FakeRuntime(), image=IMAGE, max_lifetime_seconds=120)
    with pytest.raises(ServiceError, match="does not belong"):
        other.handle("codex_requirements", opened)


def test_a_bad_codex_requirements_read_back_fails_the_action() -> None:
    """A missing or user-owned file reads back wrong, so the action raises and Codex never starts."""
    runtime = FakeRuntime()
    broker = SandboxBroker(runtime, image=IMAGE, max_lifetime_seconds=120)
    opened = broker.handle("open", _open_payload())
    for readback in ("", "user:user:644\nuser:user:644\n", _CODEX_REQUIREMENTS_READBACK.replace("false", "true")):
        runtime.sessions[0].readback = readback
        with pytest.raises(ServiceError, match="Codex cannot start"):
            broker.handle("codex_requirements", opened)


def test_the_remote_session_sends_only_the_action_and_its_box() -> None:
    """The child asks for the fixed action by name and never sends a root command over the generic route."""
    runtime = FakeRuntime()
    broker = SandboxBroker(runtime, image=IMAGE, max_lifetime_seconds=120)
    requests = []

    def parent(request: httpx.Request) -> httpx.Response:
        """Answer the JSON control actions with the real broker."""
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json=broker.handle(body["action"], body["payload"]))

    with httpx.Client(transport=httpx.MockTransport(parent)) as client:
        remote = RemoteSandboxRuntime("http://127.0.0.1:9000", "opaque-control", client=client)
        session = remote.open(SandboxSpec(lifetime_seconds=60))
        session.write_codex_requirements()
        with pytest.raises(ServiceError, match="cannot run sandbox commands as root"):
            session.run("id -u", sudo=True)
    [action] = [request for request in requests if request["action"] == "codex_requirements"]
    assert set(action["payload"]) == {"sandbox_id"}
    assert all(request["action"] != "run" for request in requests)
    assert runtime.sessions[0].root_commands == [_CODEX_REQUIREMENTS_COMMAND]


def test_a_parent_session_forwards_root_commands() -> None:
    """The parent's own root setup command reaches the box as root, and a plain command does not."""
    runtime = FakeRuntime()
    broker = SandboxBroker(runtime, image=IMAGE, max_lifetime_seconds=120)
    session = broker.parent_runtime(()).open(SandboxSpec(lifetime_seconds=60, operation_key="k"))
    assert session.run("id -u", sudo=True).stdout == _CODEX_REQUIREMENTS_READBACK
    assert session.run("true").stdout == "true"
    assert runtime.sessions[0].root_commands == ["id -u"]


def _limit_engine_boxes(monkeypatch: pytest.MonkeyPatch, limit: int) -> threading.BoundedSemaphore:
    """Give the parent brokers a fresh engine box limit and a short closed-broker poll.

    Args:
        monkeypatch: Pytest fixture for replacing the module permits.
        limit: How many engine boxes all brokers may hold at once.

    Returns:
        The new permits.
    """
    permits = threading.BoundedSemaphore(limit)
    monkeypatch.setattr(sandbox_broker, "_ENGINE_BOX_PERMITS", permits)
    monkeypatch.setattr(sandbox_broker, "_PERMIT_POLL_SECONDS", 0.01)
    return permits


def _free_permits(permits: threading.BoundedSemaphore, limit: int) -> int:
    """Count the engine box permits nobody holds, and leave the count unchanged.

    Args:
        permits: The patched engine box permits.
        limit: The patched box limit.

    Returns:
        How many permits a new engine box can take now.
    """
    taken = 0
    while taken < limit and permits.acquire(blocking=False):
        taken += 1
    for _ in range(taken):
        permits.release()
    return taken


def _engine_open(name: str) -> dict[str, Any]:
    """Create an engine box request, as a job child sends it through RemoteSandboxRuntime.

    Args:
        name: A unique suffix for the request identity.

    Returns:
        The open payload.
    """
    return {"request_id": f"{ENGINE_BOX_KEY_PREFIX}{name}", "spec": {"lifetime_seconds": 60}}


class _OpenInThread:
    """Run one broker open on a thread, so a test can see whether it waits."""

    def __init__(self, broker: SandboxBroker, payload: dict[str, Any]) -> None:
        """Start the open at once.

        Args:
            broker: The broker that serves the open.
            payload: The open request.
        """
        self.result: dict[str, Any] | None = None
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, args=(broker, payload), daemon=True)
        self.thread.start()

    def _run(self, broker: SandboxBroker, payload: dict[str, Any]) -> None:
        """Keep the open result or its error.

        Args:
            broker: The broker that serves the open.
            payload: The open request.
        """
        try:
            self.result = broker.handle("open", payload)
        except BaseException as error:
            self.error = error

    def finished(self, timeout: float) -> bool:
        """Wait for the open to end.

        Args:
            timeout: The most seconds to wait.

        Returns:
            Whether the open ended.
        """
        self.thread.join(timeout=timeout)
        return not self.thread.is_alive()


def test_engine_boxes_of_all_jobs_share_one_parent_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two job brokers in one worker parent hold at most the limit, and a waiting open goes on after a close."""
    permits = _limit_engine_boxes(monkeypatch, 2)
    first, second = FakeRuntime(), FakeRuntime()
    job_one = SandboxBroker(first, image=IMAGE, max_lifetime_seconds=120)
    job_two = SandboxBroker(second, image=IMAGE, max_lifetime_seconds=120)
    held = [job_one.handle("open", _engine_open("a")), job_two.handle("open", _engine_open("b"))]

    waiting = _OpenInThread(job_two, _engine_open("c"))
    assert not waiting.finished(0.2)
    assert len(first.sessions) + len(second.sessions) == 2

    job_one.handle("close", held[0])
    assert waiting.finished(5)
    assert waiting.error is None
    assert waiting.result is not None
    assert first.sessions[0].close_calls == 1
    assert len(second.sessions) == 2
    assert _free_permits(permits, 2) == 0

    job_two.handle("close", held[1])
    job_two.handle("close", waiting.result)
    assert _free_permits(permits, 2) == 2
    job_one.close()
    job_two.close()


def test_many_job_threads_never_hold_more_than_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Six engine runs over three job brokers never hold more than two boxes at once, and all finish."""
    permits = _limit_engine_boxes(monkeypatch, 2)
    lock = threading.Lock()
    live = [0]
    peak = [0]

    class CountedRuntime(FakeRuntime):
        """Count each open box and keep the peak."""

        def open(self, spec: SandboxSpec) -> FakeSession:
            """Count the box as open, then hold it a moment so runs overlap."""
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
            time.sleep(0.02)
            return super().open(spec)

    brokers = [SandboxBroker(CountedRuntime(), image=IMAGE, max_lifetime_seconds=120) for _ in range(3)]
    errors: list[BaseException] = []

    def one_run(index: int) -> None:
        """Open one engine box, use it a moment, and close it."""
        broker = brokers[index % 3]
        try:
            opened = broker.handle("open", _engine_open(str(index)))
            time.sleep(0.02)
            with lock:
                live[0] -= 1
            broker.handle("close", opened)
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=one_run, args=(index,)) for index in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert peak[0] == 2
    assert _free_permits(permits, 2) == 2


def test_failed_engine_box_open_releases_the_permit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A provider failure during the open gives the permit back."""
    permits = _limit_engine_boxes(monkeypatch, 1)

    class FailingRuntime(FakeRuntime):
        """Fail every box creation."""

        def open(self, spec: SandboxSpec) -> FakeSession:
            """Fail like a provider that cannot create the box."""
            raise ServiceError("no box")

    broker = SandboxBroker(FailingRuntime(), image=IMAGE, max_lifetime_seconds=120)
    with pytest.raises(ServiceError, match="no box"):
        broker.handle("open", _engine_open("a"))

    assert _free_permits(permits, 1) == 1


def test_cancelled_job_releases_its_permits_and_stops_its_waiting_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cancel closes the job broker, which frees its held permit and ends its waiting open."""
    permits = _limit_engine_boxes(monkeypatch, 1)
    other_runtime = FakeRuntime()
    cancelled = SandboxBroker(FakeRuntime(), image=IMAGE, max_lifetime_seconds=120)
    other = SandboxBroker(other_runtime, image=IMAGE, max_lifetime_seconds=120)
    cancelled.handle("open", _engine_open("held"))

    stuck = _OpenInThread(cancelled, _engine_open("stuck"))
    other_open = _OpenInThread(other, _engine_open("next"))
    assert not stuck.finished(0.2)

    cancelled.close()
    assert stuck.finished(5)
    assert isinstance(stuck.error, ServiceError)
    assert other_open.finished(5)
    assert other_open.error is None
    assert other_open.result is not None
    assert len(other_runtime.sessions) == 1
    other.close()
    assert _free_permits(permits, 1) == 1


def test_failed_box_close_still_releases_the_permit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A close with unresolved usage still frees the permit, and a second close frees nothing more."""
    permits = _limit_engine_boxes(monkeypatch, 1)

    class PendingRuntime(FakeRuntime):
        """Open boxes whose close reports unresolved usage."""

        def open(self, spec: SandboxSpec) -> FakeSession:
            """Return a box whose close fails."""
            session = FakeSession(fail_close=True)
            self.sessions.append(session)
            return session

    broker = SandboxBroker(PendingRuntime(), image=IMAGE, max_lifetime_seconds=120)
    opened = broker.handle("open", _engine_open("a"))
    with pytest.raises(UsagePendingError):
        broker.handle("close", opened)
    with pytest.raises(ServiceError):
        broker.handle("close", opened)
    broker.close()

    assert _free_permits(permits, 1) == 1


def test_replayed_engine_open_takes_no_second_permit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A child that retries the same open gets the same box and holds one permit."""
    permits = _limit_engine_boxes(monkeypatch, 2)
    runtime = FakeRuntime()
    broker = SandboxBroker(runtime, image=IMAGE, max_lifetime_seconds=120)

    opened = broker.handle("open", _engine_open("a"))
    assert broker.handle("open", _engine_open("a")) == opened
    assert len(runtime.sessions) == 1
    assert _free_permits(permits, 2) == 1
    broker.close()
    assert _free_permits(permits, 2) == 2


def test_scorer_and_pool_boxes_open_while_the_run_holds_the_only_permit(monkeypatch: pytest.MonkeyPatch) -> None:
    """At a limit of 1, a run's scorer and pool boxes do not wait on the permit their run holds."""
    permits = _limit_engine_boxes(monkeypatch, 1)
    runtime = FakeRuntime()
    broker = SandboxBroker(runtime, image=IMAGE, max_lifetime_seconds=120)
    broker.handle("open", _engine_open("run"))

    scorer = _OpenInThread(broker, {"request_id": "scorer:abc", "spec": {"lifetime_seconds": 60}})
    pool = _OpenInThread(broker, {"request_id": "pool-box", "spec": {"lifetime_seconds": 60}})
    assert scorer.finished(5)
    assert pool.finished(5)
    assert scorer.error is None
    assert pool.error is None
    assert len(runtime.sessions) == 3
    assert _free_permits(permits, 1) == 0
    broker.close()
    assert _free_permits(permits, 1) == 1
