"""Tests for a Pi turn that runs in its own sandbox (SKY-591).

A fake runtime runs each box command with local ``bash`` in a temporary
directory, and a fake ``pi`` on its ``PATH`` speaks Pi's RPC protocol. The
real model mailbox and guest proxy carry Pi's calls out of the box, so the
whole transport runs without a paid sandbox.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
from collections.abc import AsyncIterator, Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

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

    def __init__(self, root: Path, path_dir: Path) -> None:
        """Bind the box to a directory.

        Args:
            root: The box's working directory.
            path_dir: Directory with the fake ``pi``, put first on ``PATH``.
        """
        self.root = root
        self._path_dir = path_dir
        self.writes: list[str] = []
        self.env: Mapping[str, str] = {}
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
        """Mark the box closed."""
        self.closed = True


class _Runtime:
    """A fake sandbox runtime that opens :class:`_Session` boxes."""

    injects_headers = False

    def __init__(self, root: Path, *, settle: bool = True) -> None:
        """Write the fake ``pi`` and keep the opened specs.

        Args:
            root: Directory for the fake ``pi`` and the boxes.
            settle: Whether the fake ``pi`` settles, else it crashes.
        """
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
        session = _Session(Path(tempfile.mkdtemp(dir=self._root)), self._bin)
        self.sessions.append(session)
        return session


def _translator(events: list[dict]) -> PiEventTranslator:
    """Build a translator that collects its events.

    Args:
        events: Receives each emitted event.

    Returns:
        The translator.
    """
    return PiEventTranslator(events.append, reply_tool="submit", reply_arg="reply", pending_skip=frozenset())


def test_command_without_a_sandbox_keeps_pi_tools_off() -> None:
    """An agent without a sandbox still starts Pi with no built-in tools."""
    argv = pi_command("pi")
    assert "-nbt" in argv
    assert "--tools" not in argv


def test_command_in_a_sandbox_turns_on_pi_tools_and_the_bridge_tools() -> None:
    """A sandbox turn lists Pi's tools and the bridge's tools, and loads add-ons from the box."""
    argv = pi_command("pi", tools=("read", "bash", "get_x"), addon_dir=".pi-turn/addons")
    assert "-nbt" not in argv
    assert argv[argv.index("--tools") + 1] == "read,bash,get_x"
    assert argv[argv.index("-e") + 1] == f".pi-turn/addons/{pi_bridge.TOOL_BRIDGE_EXTENSION}.ts"


async def test_sandbox_turn_runs_pi_in_the_box_and_reads_files_back(tmp_path: Path) -> None:
    """Pi runs in its own box with the repository and its tools; the bridge still serves its calls."""
    runtime = _Runtime(tmp_path)
    sandbox = PiSandbox(
        runtime=runtime,
        files={"repo/notes.txt": "start\n"},
        read_back=("repo/notes.txt", "repo/missing.txt"),
        tags={"agent": "test"},
        operation_key="op-1",
    )
    handlers = _Handlers()
    events: list[dict] = []
    await run_pi_turn(
        binary="unused-host-pi", handlers=handlers, translator=_translator(events), prompt="hi", sandbox=sandbox
    )
    (spec,) = runtime.specs
    assert spec.network_disabled
    assert spec.operation_key == "op-1"
    assert spec.tags == {"agent": "test"}
    (session,) = runtime.sessions
    assert session.closed
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
    assert "SKYNET_BRIDGE_TOKEN" in session.env


async def test_sandbox_turn_reports_an_early_exit_and_closes_the_box(tmp_path: Path) -> None:
    """Pi that exits before it settles fails the turn with its stderr, and the box still closes."""
    runtime = _Runtime(tmp_path, settle=False)
    with pytest.raises(PiBridgeError, match="exited before the turn finished: pi crashed"):
        await run_pi_turn(
            binary="pi", handlers=_Handlers(), translator=_translator([]), prompt="q", sandbox=PiSandbox(runtime)
        )
    assert runtime.sessions[0].closed


class _BudgetStopError(RuntimeError):
    """Stands in for a budget stop raised in the model relay."""


async def test_sandbox_turn_re_raises_a_relay_failure_as_itself(tmp_path: Path) -> None:
    """A budget stop in the model relay ends a sandbox turn with that same exception."""
    runtime = _Runtime(tmp_path)
    stop = _BudgetStopError("budget reached")
    with pytest.raises(_BudgetStopError) as caught:
        await run_pi_turn(
            binary="pi",
            handlers=_Handlers(fail=stop),
            translator=_translator([]),
            prompt="q",
            sandbox=PiSandbox(runtime),
        )
    assert caught.value is stop
    assert runtime.sessions[0].closed
