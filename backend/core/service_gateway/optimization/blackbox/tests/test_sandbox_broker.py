"""Exercise parent-owned sandbox capabilities and streaming child RPC without provider I/O."""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from core.billing.runtime import UsagePendingError
from core.billing.signals import BudgetReached
from core.billing.vercel_usage import PACKAGE_REGISTRY_HOSTS, PROPOSER_REGISTRY_HOSTS
from core.exceptions import InfrastructureInterruptionError, ServiceError
from core.sandbox.snapshots import BuiltSnapshot, SnapshotCache

from ..remote_sandbox import RemoteSandboxRuntime, RemoteSandboxSession
from ..sandbox import _CODEX_REQUIREMENTS_COMMAND, _CODEX_REQUIREMENTS_READBACK, CommandResult, SandboxSpec
from ..sandbox_broker import SandboxBroker

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


def test_more_than_sixteen_engine_boxes_open_at_once_with_no_wait() -> None:
    """Skynet sets no platform box cap, so 24 engine opens over three job brokers all run together (SKY-540)."""
    count = 24
    # Every open waits here until all of them are inside the provider at once, so any
    # cap below the count breaks the barrier and fails the test instead of hanging it.
    together = threading.Barrier(count, timeout=10)

    class TogetherRuntime(FakeRuntime):
        """Hold each box creation until every other creation runs too."""

        def open(self, spec: SandboxSpec) -> FakeSession:
            """Wait for all creations, then create the box."""
            together.wait()
            with lock:
                return super().open(spec)

    lock = threading.Lock()
    runtimes = [TogetherRuntime() for _ in range(3)]
    brokers = [SandboxBroker(runtime, image=IMAGE, max_lifetime_seconds=120) for runtime in runtimes]
    opened: list[tuple[SandboxBroker, dict[str, Any]]] = []
    errors: list[BaseException] = []

    def one_open(index: int) -> None:
        """Open one engine box the way a job child asks for it."""
        broker = brokers[index % 3]
        try:
            # The old SKY-426 engine box identity, so this test fails if that cap comes back.
            request = {"request_id": f"native-engine:{index}", "spec": {"lifetime_seconds": 60}}
            result = broker.handle("open", request)
            with lock:
                opened.append((broker, result))
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=one_open, args=(index,), daemon=True) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert len(opened) == count
    assert sum(len(runtime.sessions) for runtime in runtimes) == count
    for broker, result in opened:
        broker.handle("close", result)
    assert all(session.close_calls == 1 for runtime in runtimes for session in runtime.sessions)


def test_a_cancelled_job_broker_closes_every_box_once_and_refuses_new_ones() -> None:
    """A cancel closes the job broker: each open box closes once, and later opens and closes fail."""
    runtime = FakeRuntime()
    broker = SandboxBroker(runtime, image=IMAGE, max_lifetime_seconds=120)
    first = broker.handle("open", {"request_id": "engine-a", "spec": {"lifetime_seconds": 60}})
    broker.handle("open", {"request_id": "engine-b", "spec": {"lifetime_seconds": 60}})
    broker.handle("close", first)

    broker.close()

    assert [session.close_calls for session in runtime.sessions] == [1, 1]
    with pytest.raises(ServiceError, match="closed"):
        broker.handle("open", {"request_id": "engine-c", "spec": {"lifetime_seconds": 60}})
    with pytest.raises(ServiceError):
        broker.handle("close", first)
    assert len(runtime.sessions) == 2


def test_after_the_run_deadline_boxes_use_no_snapshot_and_the_snapshots_are_deleted() -> None:
    """Stop snapshot use at the run deadline, so no use stretches storage past its hold."""
    deleted: list[str] = []

    async def delete(built: BuiltSnapshot) -> None:
        """Record a deleted snapshot."""
        deleted.append(built.snapshot_id)

    async def build(name: str) -> BuiltSnapshot:
        """Build one fake snapshot."""
        return BuiltSnapshot(snapshot_id="snap-1", builder_name=name)

    cache = SnapshotCache("run", datetime.now(UTC) + timedelta(hours=1), delete=delete)
    asyncio.run(cache.get_or_build("key", build))
    runtime = FakeRuntime()
    broker = SandboxBroker(runtime, image=IMAGE, max_lifetime_seconds=120, snapshots=cache)
    parent = broker.parent_runtime(("pypi.org",))
    spec = SandboxSpec(lifetime_seconds=60, allowed_hosts=("pypi.org",), operation_key="k", snapshot_id="snap-1")
    assert parent.supports_snapshots
    parent.open(spec)
    assert runtime.specs[-1].snapshot_id == "snap-1"

    cache.run_deadline = datetime.now(UTC) - timedelta(seconds=1)
    assert not parent.supports_snapshots
    assert deleted == ["snap-1"]
    parent.open(replace(spec, operation_key="k2"))
    assert runtime.specs[-1].snapshot_id is None
    assert parent.snapshot(runtime.sessions[-1], "key") is None
    broker.close()
    assert deleted == ["snap-1"]


def test_after_the_run_deadline_a_failed_snapshot_delete_runs_once_until_the_run_end() -> None:
    """Close the expired cache once, so a failing delete does not slow every later box."""
    calls: list[str] = []

    async def failing_delete(built: BuiltSnapshot) -> None:
        """Fail every delete before the run end.

        Args:
            built: The snapshot to delete.

        Raises:
            RuntimeError: Always.
        """
        calls.append(built.snapshot_id)
        raise RuntimeError("provider error")

    async def build(name: str) -> BuiltSnapshot:
        """Build one fake snapshot."""
        return BuiltSnapshot(snapshot_id="snap-1", builder_name=name)

    cache = SnapshotCache("run", datetime.now(UTC) - timedelta(seconds=1), delete=failing_delete)
    asyncio.run(cache.get_or_build("key", build))
    runtime = FakeRuntime()
    broker = SandboxBroker(runtime, image=IMAGE, max_lifetime_seconds=120, snapshots=cache)
    parent = broker.parent_runtime(("pypi.org",))
    spec = SandboxSpec(lifetime_seconds=60, allowed_hosts=("pypi.org",), operation_key="k", snapshot_id="snap-1")
    for number in range(4):
        parent.open(replace(spec, operation_key=f"k{number}"))
    assert not parent.supports_snapshots
    assert calls == ["snap-1"]
    assert all(opened.snapshot_id is None for opened in runtime.specs)
    with pytest.raises(RuntimeError):
        broker.close()
    assert calls == ["snap-1", "snap-1"]
