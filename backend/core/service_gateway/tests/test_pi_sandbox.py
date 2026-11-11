"""Tests for Pi chat sessions that run in their own sandbox (SKY-591).

A fake runtime runs each box command with local ``bash`` in a temporary
directory, and a fake ``pi`` on its ``PATH`` speaks Pi's RPC protocol. The
real model mailbox and guest proxy carry Pi's calls out of the box, so the
whole transport runs without a paid sandbox.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.billing.runtime import UsagePendingError
from core.billing.signals import BudgetReached
from core.service_gateway.agents import pi_bridge
from core.service_gateway.agents.pi_bridge import (
    PiBridgeError,
    PiEventTranslator,
    PiSandbox,
    ToolOutcome,
    pi_command,
    run_pi_turn,
)
from core.service_gateway.optimization.blackbox.sandbox_contract import CommandResult, SandboxSpec

_BUDGET = object()
_SETTINGS = SimpleNamespace(vercel_sandbox_max_lifetime_seconds=3600)

_FAKE_PI = r"""
import json, os, sys, urllib.request

url, token = os.environ["SKYNET_BRIDGE_URL"], os.environ["SKYNET_BRIDGE_TOKEN"]
headers = {"authorization": "Bearer " + token, "content-type": "application/json"}
models = json.load(open(os.path.join(os.environ["PI_CODING_AGENT_DIR"], "models.json")))


def call(path, body=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url + path, data=data, headers=headers, method="GET" if data is None else "POST")
    try:
        return urllib.request.urlopen(request).read().decode()
    except Exception as error:
        return str(error)


def out(record):
    sys.stdout.write(json.dumps(record) + "\n")
    sys.stdout.flush()


with open(".record.json", "w") as handle:
    json.dump({"argv": sys.argv[1:], "models": models, "cwd": os.getcwd()}, handle)
command = json.loads(sys.stdin.readline())
out({"type": "response", "command": "prompt", "success": True})
specs = json.loads(call("/tools"))
out({"type": "turn_start"})
call(models["providers"]["skynet"]["baseUrl"].removeprefix(url) + "/chat/completions", {"messages": [{"role": "user", "content": command["message"]}]})
call("/call", {"tool": specs[0]["name"], "args": {"q": "a"}})
if __SETTLE__:
    # Stands in for Pi's own edit tool, which works on the cloned repository.
    os.makedirs("repo", exist_ok=True)
    with open("repo/notes.txt", "a") as handle:
        handle.write("edited\n")
    out({"type": "agent_settled"})
    sys.stdin.read()
    open(".stopped", "w").close()
else:
    sys.stderr.write("pi crashed\n")
    sys.exit(3)
"""


class _Handlers:
    """Fake turn handlers that record each tool call and model request."""

    def __init__(self, fail: BaseException | None = None) -> None:
        """Set up empty records.

        Args:
            fail: An exception the model relay raises, or ``None``.
        """
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.bodies: list[dict[str, Any]] = []
        self._fail = fail

    def tool_specs(self) -> list[dict[str, Any]]:
        """Return one tool.

        Returns:
            The tool spec.
        """
        return [{"name": "get_x", "description": "Get x.", "parameters": {"type": "object", "properties": {}}}]

    async def call_tool(self, name: str, args: dict[str, Any]) -> ToolOutcome:
        """Record the call.

        Args:
            name: The tool name.
            args: The call's arguments.

        Returns:
            A fixed result.
        """
        self.calls.append((name, args))
        return ToolOutcome("x=a")

    async def complete(self, body: dict[str, Any]) -> AsyncIterator[str]:
        """Record the request and stream one chunk, or fail.

        Args:
            body: Pi's request.

        Yields:
            One stream chunk.

        Raises:
            BaseException: The configured failure.
        """
        self.bodies.append(body)
        if self._fail is not None:
            raise self._fail
        yield json.dumps({"choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": "stop"}]})


class _Session:
    """A fake sandbox: a temporary directory where commands run with local bash."""

    def __init__(self, root: Path, path_dir: Path, *, run_error: BaseException | None = None) -> None:
        """Bind the box to a directory.

        Args:
            root: The box's working directory.
            path_dir: Directory with the fake ``pi``, put first on ``PATH``.
            run_error: An exception each command raises, as a dead box or a
                budget stop would, or ``None``.
        """
        self.root = root
        self._run_error = run_error
        self.close_error: BaseException | None = None
        self._path_dir = path_dir
        self.writes: list[str] = []
        self.env: Mapping[str, str] = {}
        self.envs: list[Mapping[str, str]] = []
        self.closed = False

    def write_files(self, files: Mapping[str, str]) -> None:
        """Write text files under the working directory.

        Args:
            files: Relative path to content.
        """
        for name, content in files.items():
            self.writes.append(name)
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

    def run(
        self,
        command: str,
        *,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        on_output: Callable[[str, str], None] | None = None,
        sudo: bool = False,
    ) -> CommandResult:
        """Run one command with bash and stream its output lines.

        Args:
            command: The shell command.
            env: Extra environment.
            timeout_seconds: Unused.
            on_output: Receives each output line.
            sudo: Unused.

        Returns:
            The exit code and output.
        """
        self.env = dict(env or {})
        self.envs.append(self.env)
        if self._run_error is not None:
            raise self._run_error
        full_env = {**os.environ, **self.env, "PATH": f"{self._path_dir}:{os.environ['PATH']}"}
        proc = subprocess.Popen(
            ["bash", "--noprofile", "--norc", "-c", command],
            cwd=self.root,
            env=full_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        captured: dict[str, list[str]] = {"stdout": [], "stderr": []}

        def pump(stream: str) -> None:
            """Forward one stream line by line.

            Args:
                stream: ``"stdout"`` or ``"stderr"``.
            """
            for line in getattr(proc, stream):
                captured[stream].append(line)
                if on_output is not None:
                    on_output(stream, line)

        threads = [threading.Thread(target=pump, args=(name,)) for name in captured]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return CommandResult(proc.wait(), "".join(captured["stdout"]), "".join(captured["stderr"]))

    def read_file(self, path: str) -> str | None:
        """Read a file under the working directory.

        Args:
            path: Relative path.

        Returns:
            The text, or ``None`` when absent.
        """
        target = self.root / path
        return target.read_text() if target.exists() else None

    def close(self) -> None:
        """Mark the box closed, then raise the configured close failure.

        Raises:
            BaseException: The configured close failure.
        """
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class _Runtime:
    """A fake sandbox runtime that opens :class:`_Session` boxes."""

    injects_headers = False

    def __init__(
        self,
        root: Path,
        *,
        settle: bool = True,
        protected: bool = True,
        billable: bool = True,
        gate: Any = None,
        run_error: BaseException | None = None,
    ) -> None:
        """Write the fake ``pi`` and keep the opened specs.

        Args:
            root: Directory for the fake ``pi`` and the boxes.
            settle: Whether the fake ``pi`` settles, else it crashes.
            protected: The runtime's ``protected`` flag.
            billable: The runtime's ``billable`` flag.
            gate: A :class:`threading.Event` that each open waits for, or
                ``None`` to open at once.
            run_error: An exception the first box raises on each command.
        """
        self._run_error = run_error
        self.protected = protected
        self.billable = billable
        self._gate = gate
        self.opening = threading.Event()
        self._root = root
        self._bin = root / "bin"
        self._bin.mkdir()
        script = self._bin / "pi"
        script.write_text(f"#!{sys.executable}\n{_FAKE_PI.replace('__SETTLE__', repr(settle))}")
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        self.specs: list[SandboxSpec] = []
        self.sessions: list[_Session] = []

    def open(self, spec: SandboxSpec) -> _Session:
        """Open one box.

        Args:
            spec: The box spec.

        Returns:
            The session.
        """
        self.specs.append(spec)
        self.opening.set()
        if self._gate is not None:
            self._gate.wait(5)
        error = self._run_error if not self.sessions else None
        session = _Session(Path(tempfile.mkdtemp(dir=self._root)), self._bin, run_error=error)
        self.sessions.append(session)
        return session


@pytest.fixture
def sandbox_for(monkeypatch: pytest.MonkeyPatch) -> Callable[..., PiSandbox]:
    """Build a session sandbox whose protected runtime is a given fake.

    Args:
        monkeypatch: Replaces the protected image and runtime lookups.

    Returns:
        A factory from a fake runtime and options to a :class:`PiSandbox`.
    """
    built: dict[str, Any] = {}

    def runtime_for(settings: Any, *, image: str, budget: Any) -> Any:
        """Return the fake runtime and record the arguments.

        Args:
            settings: The settings the sandbox passed.
            image: The image the sandbox passed.
            budget: The budget the sandbox passed.

        Returns:
            The fake runtime.
        """
        built.update(settings=settings, image=image, budget=budget)
        return built["runtime"]

    monkeypatch.setattr(pi_bridge, "protected_image", lambda settings, workflow: f"image-for-{workflow}")
    monkeypatch.setattr(pi_bridge, "protected_runtime", runtime_for)

    def make(runtime: Any, budget: Any = _BUDGET, **options: Any) -> PiSandbox:
        """Bind the fake runtime and build the sandbox.

        Args:
            runtime: The fake runtime.
            budget: The run's budget.
            **options: Other :class:`PiSandbox` options; ``operation_key``
                is ``"op"`` unless given.

        Returns:
            The sandbox.
        """
        built["runtime"] = runtime
        options.setdefault("operation_key", "op")
        sandbox = PiSandbox(_SETTINGS, budget, **options)
        sandbox.built = built  # type: ignore[attr-defined]
        return sandbox

    return make


def _translator(events: list[dict]) -> PiEventTranslator:
    """Build a translator that collects its events.

    Args:
        events: Receives each emitted event.

    Returns:
        The translator.
    """
    return PiEventTranslator(events.append, reply_tool="submit", reply_arg="reply", pending_skip=frozenset())


async def _turn(sandbox: PiSandbox, handlers: _Handlers | None = None, prompt: str = "hi") -> None:
    """Run one turn of the session.

    Args:
        sandbox: The session's sandbox.
        handlers: The turn's handlers.
        prompt: The user message.
    """
    await run_pi_turn(
        binary="unused-host-pi",
        handlers=handlers or _Handlers(),
        translator=_translator([]),
        prompt=prompt,
        sandbox=sandbox,
    )


def _wait_for(check: Callable[[], bool]) -> bool:
    """Wait up to five seconds for a condition set by another thread.

    Args:
        check: The condition.

    Returns:
        Whether it became true.
    """
    deadline = time.monotonic() + 5
    while not check():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


def test_command_without_a_sandbox_keeps_pi_tools_off() -> None:
    """An agent without a sandbox still starts Pi with no built-in tools."""
    argv = pi_command("pi")
    assert "-nbt" in argv
    assert "--tools" not in argv


def test_command_in_a_sandbox_turns_on_pi_tools_and_the_bridge_tools() -> None:
    """A sandbox turn lists Pi's tools and the bridge's tools, and loads add-ons from the box."""
    argv = pi_command("pi", tools=("read", "bash", "get_x"), addon_dir=".pi-turn/1/addons")
    assert "-nbt" not in argv
    assert argv[argv.index("--tools") + 1] == "read,bash,get_x"
    assert argv[argv.index("-e") + 1] == f".pi-turn/1/addons/{pi_bridge.TOOL_BRIDGE_EXTENSION}.ts"


async def test_sandbox_turn_runs_pi_in_the_box_and_reads_files_back(
    tmp_path: Path, sandbox_for: Callable[..., PiSandbox]
) -> None:
    """Pi runs in a protected box with the repository and its tools; the bridge still serves its calls."""
    runtime = _Runtime(tmp_path)
    sandbox = sandbox_for(
        runtime,
        files={"repo/notes.txt": "start\n"},
        read_back=("repo/notes.txt", "repo/missing.txt"),
        tags={"agent": "test"},
        operation_key="op-1",
    )
    handlers = _Handlers()
    await _turn(sandbox, handlers)
    assert sandbox.built == {
        "runtime": runtime,
        "settings": _SETTINGS,
        "image": "image-for-anything",
        "budget": _BUDGET,
    }
    (spec,) = runtime.specs
    assert spec.network_disabled
    assert spec.operation_key == "op-1:box:1"
    assert spec.image == "image-for-anything"
    assert spec.tags == {"agent": "test"}
    assert spec.lifetime_seconds == sandbox.lifetime_seconds == 1800.0
    (session,) = runtime.sessions
    record = json.loads((session.root / ".record.json").read_text())
    assert "-nbt" not in record["argv"]
    assert record["argv"][record["argv"].index("--tools") + 1] == "read,bash,edit,write,grep,find,ls,get_x"
    assert record["cwd"] == str(session.root.resolve())
    # Pi talks to the box's own relay, never to a host address it cannot reach.
    base_url = record["models"]["providers"]["skynet"]["baseUrl"]
    assert base_url.startswith("http://127.0.0.1:")
    assert base_url.endswith("/v1")
    assert handlers.bodies[0]["messages"] == [{"role": "user", "content": "hi"}]
    assert handlers.calls == [("get_x", {"q": "a"})]
    assert sandbox.read_back_files == {"repo/notes.txt": "start\nedited\n", "repo/missing.txt": None}
    # Pi saw the end of its input and stopped by itself.
    assert (session.root / ".stopped").exists()
    assert sandbox.cold_start_seconds is not None
    # The session keeps the box open for the next turn until it closes it.
    assert not session.closed
    await sandbox.aclose()
    assert session.closed


async def test_box_env_holds_no_credential_except_the_bridge_token(
    tmp_path: Path, sandbox_for: Callable[..., PiSandbox]
) -> None:
    """The box gets the turn's bridge token and Pi's offline flags, and no other secret."""
    runtime = _Runtime(tmp_path)
    sandbox = sandbox_for(runtime)
    await _turn(sandbox)
    sandbox.close()
    (spec,) = runtime.specs
    assert not spec.env
    (session,) = runtime.sessions
    assert set(session.env) == {"PI_OFFLINE", "PI_SKIP_VERSION_CHECK", "PI_TELEMETRY", "SKYNET_BRIDGE_TOKEN"}


async def test_turns_of_a_session_reuse_one_box_and_keep_its_state(
    tmp_path: Path, sandbox_for: Callable[..., PiSandbox]
) -> None:
    """A second turn runs in the same box, sees the first turn's edit and gets a new token."""
    runtime = _Runtime(tmp_path)
    sandbox = sandbox_for(runtime, files={"repo/notes.txt": "start\n"}, read_back=("repo/notes.txt",))
    await _turn(sandbox)
    await _turn(sandbox)
    (session,) = runtime.sessions
    assert sandbox.read_back_files == {"repo/notes.txt": "start\nedited\nedited\n"}
    # The caller's files upload once, when the box opens.
    assert session.writes.count("repo/notes.txt") == 1
    first, second = (env["SKYNET_BRIDGE_TOKEN"] for env in session.envs)
    assert first != second
    sandbox.close()
    assert session.closed


async def test_an_idle_box_closes_and_the_next_turn_opens_a_new_one(
    tmp_path: Path, sandbox_for: Callable[..., PiSandbox]
) -> None:
    """A box closes after the idle time; the session opens a fresh one for its next turn."""
    runtime = _Runtime(tmp_path)
    sandbox = sandbox_for(runtime, idle_seconds=0.05)
    await _turn(sandbox)
    assert _wait_for(lambda: runtime.sessions[0].closed)
    await _turn(sandbox)
    assert len(runtime.sessions) == 2
    assert not runtime.sessions[1].closed
    sandbox.close()


async def test_a_turn_that_would_outlive_the_box_opens_a_new_one(
    tmp_path: Path, sandbox_for: Callable[..., PiSandbox]
) -> None:
    """The session closes a box before a turn that its lifetime cannot cover."""
    runtime = _Runtime(tmp_path)
    sandbox = sandbox_for(runtime, lifetime_seconds=600.0, turn_seconds=600.0)
    await _turn(sandbox)
    await _turn(sandbox)
    first, second = runtime.sessions
    assert first.closed
    assert not second.closed
    # Each box reserves its own budget, so a reopen never replays the first one.
    assert [spec.operation_key for spec in runtime.specs] == ["op:box:1", "op:box:2"]
    sandbox.close()


def test_the_box_lifetime_never_exceeds_the_deployment_limit() -> None:
    """A session lifetime above the deployment's sandbox lifetime is cut to it."""
    assert PiSandbox(_SETTINGS, _BUDGET, lifetime_seconds=10_000.0).lifetime_seconds == 3600


async def test_a_cancel_while_the_box_opens_closes_the_box(
    tmp_path: Path, sandbox_for: Callable[..., PiSandbox]
) -> None:
    """A turn cancelled during a slow open closes the box when the open ends."""
    gate = threading.Event()
    runtime = _Runtime(tmp_path, gate=gate)
    sandbox = sandbox_for(runtime)
    turn = asyncio.ensure_future(_turn(sandbox))
    assert await asyncio.to_thread(runtime.opening.wait, 5)
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn
    gate.set()
    assert _wait_for(lambda: bool(runtime.sessions) and runtime.sessions[0].closed)
    assert sandbox._session is None
    assert not sandbox._busy


async def test_a_box_slower_than_the_cold_start_limit_fails_the_turn_and_closes(
    tmp_path: Path, sandbox_for: Callable[..., PiSandbox]
) -> None:
    """The cold-start limit fails the turn, and the late box closes when it arrives."""
    gate = threading.Event()
    runtime = _Runtime(tmp_path, gate=gate)
    sandbox = sandbox_for(runtime, cold_start_limit_seconds=0.05)
    with pytest.raises(PiBridgeError, match="did not open within"):
        await _turn(sandbox)
    gate.set()
    assert _wait_for(lambda: bool(runtime.sessions) and runtime.sessions[0].closed)
    assert sandbox.cold_start_seconds is None


def test_the_default_cold_start_limit_is_thirty_seconds() -> None:
    """A session waits at most 30 s for its box unless the caller sets a limit."""
    assert pi_bridge.PI_COLD_START_LIMIT_SECONDS == 30.0
    assert PiSandbox(_SETTINGS, _BUDGET).cold_start_limit_seconds == 30.0


@pytest.mark.parametrize(
    ("runtime_options", "budget", "image", "message"),
    [
        ({}, _BUDGET, None, "No immutable sandbox image"),
        ({"protected": False}, _BUDGET, "image", "protected sandbox runtime"),
        ({}, None, "image", "needs a budget"),
        ({}, "no-key", "image", "needs an operation key"),
    ],
)
async def test_a_session_refuses_a_box_that_would_not_be_protected_and_metered(
    tmp_path: Path,
    sandbox_for: Callable[..., PiSandbox],
    monkeypatch: pytest.MonkeyPatch,
    runtime_options: dict[str, bool],
    budget: Any,
    image: str | None,
    message: str,
) -> None:
    """No image, an unprotected runtime, or a billable box with no budget or key fails before any box opens."""
    runtime = _Runtime(tmp_path, **runtime_options)
    options = {"operation_key": None} if budget == "no-key" else {"budget": budget}
    sandbox = sandbox_for(runtime, **options)
    monkeypatch.setattr(pi_bridge, "protected_image", lambda settings, workflow: image)
    handlers = _Handlers()
    with pytest.raises(PiBridgeError, match=message):
        await _turn(sandbox, handlers)
    assert runtime.specs == []
    assert handlers.bodies == []


async def test_a_bridge_tool_with_the_name_of_a_pi_tool_is_refused(
    tmp_path: Path, sandbox_for: Callable[..., PiSandbox]
) -> None:
    """A bridge tool named like a Pi tool fails the turn before a box opens."""
    runtime = _Runtime(tmp_path)
    handlers = _Handlers()
    handlers.tool_specs = lambda: [{"name": "grep", "description": "", "parameters": {}}]  # type: ignore[method-assign]
    # Pi's grep is off in this session, and the name is still refused.
    with pytest.raises(PiBridgeError, match=r"\['grep'\]"):
        await _turn(sandbox_for(runtime, builtin_tools=("read",)), handlers)
    assert runtime.specs == []


async def test_sandbox_turn_reports_an_early_exit_and_keeps_the_session_usable(
    tmp_path: Path, sandbox_for: Callable[..., PiSandbox]
) -> None:
    """Pi that exits before it settles fails the turn with its stderr, and the session ends the turn."""
    runtime = _Runtime(tmp_path, settle=False)
    sandbox = sandbox_for(runtime)
    with pytest.raises(PiBridgeError, match="exited before the turn finished: pi crashed"):
        await _turn(sandbox)
    assert not sandbox._busy
    sandbox.close()
    assert runtime.sessions[0].closed


class _BudgetStopError(RuntimeError):
    """Stands in for a budget stop raised in the model relay."""


async def test_sandbox_turn_re_raises_a_relay_failure_as_itself(
    tmp_path: Path, sandbox_for: Callable[..., PiSandbox]
) -> None:
    """A budget stop in the model relay ends a sandbox turn with that same exception."""
    runtime = _Runtime(tmp_path)
    sandbox = sandbox_for(runtime)
    stop = _BudgetStopError("budget reached")
    with pytest.raises(_BudgetStopError) as caught:
        await _turn(sandbox, _Handlers(fail=stop))
    assert caught.value is stop
    sandbox.close()
    assert runtime.sessions[0].closed


@pytest.mark.parametrize(
    "path",
    [
        "http://evil.example/x",
        "//evil.example/x",
        "/v1/../call",
        "/v1/%2e%2e/call",
        "\\\\evil\\x",
        "/v1/models",
        "/call?next=http://evil.example",
        "/call/",
        "/CALL",
        "/Tools",
    ],
)
def test_the_relay_refuses_any_path_outside_the_bridge_routes(path: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """A guest path that is not an exact bridge route gets 404, and the host sends no request."""
    dispatch, client = pi_bridge._forward("http://127.0.0.1:9")
    sent: list[Any] = []
    monkeypatch.setattr(client, "request", lambda *args, **kwargs: sent.append((args, kwargs)))
    try:
        result = dispatch("token", path, {}, {})
    finally:
        client.close()
    assert result.status == 404
    assert sent == []
    assert client.follow_redirects is False


async def test_a_second_turn_while_one_runs_is_refused(tmp_path: Path, sandbox_for: Callable[..., PiSandbox]) -> None:
    """A session runs one turn at a time; a concurrent turn fails and the first still finishes."""
    gate = threading.Event()
    runtime = _Runtime(tmp_path, gate=gate)
    sandbox = sandbox_for(runtime)
    first = asyncio.ensure_future(_turn(sandbox))
    assert await asyncio.to_thread(runtime.opening.wait, 5)
    with pytest.raises(PiBridgeError, match="still runs"):
        await _turn(sandbox)
    gate.set()
    await first
    assert len(runtime.sessions) == 1
    sandbox.close()


async def test_a_closed_session_refuses_turns_and_closes_a_box_that_opens_late(
    tmp_path: Path, sandbox_for: Callable[..., PiSandbox]
) -> None:
    """A box whose open ends after close() closes at once, and no later turn runs."""
    gate = threading.Event()
    runtime = _Runtime(tmp_path, gate=gate)
    sandbox = sandbox_for(runtime)
    turn = asyncio.ensure_future(_turn(sandbox))
    assert await asyncio.to_thread(runtime.opening.wait, 5)
    sandbox.close()
    gate.set()
    with pytest.raises(PiBridgeError, match="closed while its sandbox opened"):
        await turn
    assert runtime.sessions[0].closed
    with pytest.raises(PiBridgeError, match="session is closed"):
        await _turn(sandbox)
    assert len(runtime.specs) == 1


class _ProviderError(RuntimeError):
    """Stands in for a sandbox provider failure."""


@pytest.mark.parametrize("error", [_ProviderError("box gone"), BudgetReached("sandbox budget reached")])
async def test_a_failed_box_passes_its_error_through_and_is_dropped(
    tmp_path: Path, sandbox_for: Callable[..., PiSandbox], error: BaseException
) -> None:
    """A provider failure or budget stop ends the turn as itself, closes the box, and the next turn opens a new one."""
    runtime = _Runtime(tmp_path, run_error=error)
    sandbox = sandbox_for(runtime)
    with pytest.raises(type(error)) as caught:
        await _turn(sandbox)
    assert caught.value is error
    assert runtime.sessions[0].closed
    await _turn(sandbox)
    assert len(runtime.sessions) == 2
    sandbox.close()


@pytest.mark.parametrize(
    ("error", "level"), [(RuntimeError("close failed"), "WARNING"), (UsagePendingError("bill pending"), "ERROR")]
)
async def test_an_idle_close_that_raises_is_logged_and_the_box_is_dropped(
    tmp_path: Path,
    sandbox_for: Callable[..., PiSandbox],
    caplog: pytest.LogCaptureFixture,
    error: Exception,
    level: str,
) -> None:
    """A failing idle close does not break the session; an unsettled bill logs as an error."""
    runtime = _Runtime(tmp_path)
    sandbox = sandbox_for(runtime, idle_seconds=0.05)
    await _turn(sandbox)
    runtime.sessions[0].close_error = error
    with caplog.at_level("WARNING", logger=pi_bridge.logger.name):
        assert _wait_for(lambda: any(record.levelname == level for record in caplog.records))
    assert sandbox._session is None
    await _turn(sandbox)
    assert len(runtime.sessions) == 2
    sandbox.close()


async def test_a_turn_inbox_is_new_each_turn(tmp_path: Path, sandbox_for: Callable[..., PiSandbox]) -> None:
    """Each turn reads commands from its own random folder."""
    runtime = _Runtime(tmp_path)
    sandbox = sandbox_for(runtime)
    await _turn(sandbox)
    await _turn(sandbox)
    sandbox.close()
    inboxes = {name.rsplit("/", 2)[0] for name in runtime.sessions[0].writes if "/in/" in name}
    assert len(inboxes) == 2
