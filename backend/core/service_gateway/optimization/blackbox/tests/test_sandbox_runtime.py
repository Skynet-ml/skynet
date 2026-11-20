"""Tests the blackbox sandbox runtimes.

``sandbox_runtime`` is a leaf module and ``sandbox`` re-exports it unchanged (SKY-486).
``_async_spec`` maps the blackbox GPU to the provider GPU type, the Modal bridge sends it,
and every other runtime refuses a GPU spec before a box starts (SKY-804).
"""

import ast
import os
import shlex
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from core.exceptions import ServiceError
from core.sandbox.snapshots import refuse_secrets

from .. import sandbox, sandbox_runtime
from ..remote_sandbox import RemoteSandboxRuntime
from ..sandbox_broker import _ParentSandboxRuntime
from ..sandbox_contract import SandboxSpec

_GPU_SPEC = SandboxSpec(lifetime_seconds=60, operation_key="op", gpu="a100")

_MOVED_NAMES = (
    "_RUNTIME_OVERRIDE",
    "current_sandbox_runtime",
    "sandbox_runtime_context",
    "LocalSubprocessSession",
    "LocalSubprocessRuntime",
    "ContainedSubprocessRuntime",
    "_private_root",
    "_contained_environment",
    "_kill_process_group",
    "AsyncBridgeSession",
    "AsyncBridgeRuntime",
    "_bridge_errors",
    "_async_spec",
    "_ASYNC_CLOSE_BUDGET_SECONDS",
    "COVERAGE_SLICE_SECONDS",
    "_SHELL_FLAGS",
    "CODEX_REQUIREMENTS",
    "CODEX_REQUIREMENTS_PATH",
    "_CODEX_REQUIREMENTS_CHECK",
    "_CODEX_REQUIREMENTS_COMMAND",
    "_CODEX_REQUIREMENTS_READBACK",
    "_require_codex_requirements",
    "write_codex_requirements",
    "check_codex_requirements",
    "load_provider",
)

# The check stubs the two parent packages, so it measures only what
# ``sandbox_runtime`` itself pulls in, not what a package ``__init__`` loads.
_LEAF_CHECK = """
import pathlib
import sys
import types

import core

root = pathlib.Path(core.__file__).parent
for name in ("core.service_gateway", "core.service_gateway.optimization"):
    package = types.ModuleType(name)
    package.__path__ = [str(root.joinpath(*name.split(".")[1:]))]
    sys.modules[name] = package
import core.service_gateway.optimization.blackbox.sandbox_runtime
forbidden = (
    "core.service_gateway.optimization.blackbox.sandbox",
    "core.service_gateway.optimization.blackbox.docker_sandbox",
    "vercel",
)
print(",".join(name for name in forbidden if name in sys.modules))
"""
_BLACKBOX = "core.service_gateway.optimization.blackbox"
_FORBIDDEN_DIRECT_IMPORTS = ("httpx", "vercel", f"{_BLACKBOX}.sandbox", f"{_BLACKBOX}.docker_sandbox")


@pytest.mark.parametrize("name", _MOVED_NAMES)
def test_sandbox_reexports_the_same_object(name: str) -> None:
    """Check that ``sandbox`` exposes each moved name as the exact object in ``sandbox_runtime``.

    Args:
        name: The moved name.
    """
    assert getattr(sandbox, name) is getattr(sandbox_runtime, name)


def test_sandbox_runtime_imports_without_the_legacy_modules() -> None:
    """Check that a fresh interpreter imports ``sandbox_runtime`` without the legacy or Vercel modules, parent packages stubbed."""
    backend = Path(__file__).resolve().parents[5]
    result = subprocess.run(
        [sys.executable, "-c", _LEAF_CHECK],
        cwd=backend,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == ""


def test_patch_on_sandbox_reaches_the_moved_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check that a patch on the old module also patches ``sandbox_runtime``, and that undo restores both.

    Args:
        monkeypatch: The pytest monkeypatch fixture.
    """
    original = sandbox_runtime._ASYNC_CLOSE_BUDGET_SECONDS
    with monkeypatch.context() as patch:
        patch.setattr(sandbox, "_ASYNC_CLOSE_BUDGET_SECONDS", 0.5)
        assert sandbox_runtime._ASYNC_CLOSE_BUDGET_SECONDS == 0.5
    assert original == sandbox_runtime._ASYNC_CLOSE_BUDGET_SECONDS
    assert original == sandbox._ASYNC_CLOSE_BUDGET_SECONDS


def test_sandbox_runtime_has_no_direct_legacy_imports() -> None:
    """Check that no import statement in ``sandbox_runtime`` names httpx, the Vercel SDK or the legacy modules."""
    tree = ast.parse(Path(sandbox_runtime.__file__).read_text())
    package = _BLACKBOX.split(".")
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = package[: len(package) - node.level + 1] if node.level else []
            module = ".".join([*base, *(node.module or "").split(".")]).strip(".")
            imported.add(module)
            imported.update(f"{module}.{alias.name}" for alias in node.names)
    clashes = {
        name
        for name in imported
        for banned in _FORBIDDEN_DIRECT_IMPORTS
        if name == banned or name.startswith(f"{banned}.")
    }
    assert clashes == set()


class _CreateCalledError(Exception):
    """Stop the bridge open after the provider spec is recorded."""


def _no_open_session(*_args: Any) -> Any:
    """Fail the test when the bridge starts a box.

    Raises:
        AssertionError: Always.
    """
    raise AssertionError("No box may start.")


def test_async_spec_passes_the_gpu_as_the_provider_gpu_type() -> None:
    """Carry the blackbox GPU into the provider spec, and leave it unset without one."""
    assert sandbox_runtime._async_spec(_GPU_SPEC).gpu_type == "a100"
    assert sandbox_runtime._async_spec(SandboxSpec(lifetime_seconds=60)).gpu_type is None


def test_the_modal_bridge_sends_the_gpu_type_to_the_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the Modal provider the GPU type, with no real Modal call."""
    seen: list[Any] = []

    def record(provider: str, spec: Any, **_options: Any) -> Any:
        """Record the provider and its spec, then stop the open.

        Args:
            provider: The provider name.
            spec: The provider spec.
            **_options: The provider options.

        Raises:
            _CreateCalledError: Always.
        """
        seen.append((provider, spec.gpu_type))
        raise _CreateCalledError

    monkeypatch.setattr(sandbox_runtime, "create_environment", record)
    runtime = sandbox_runtime.AsyncBridgeRuntime("modal")
    with pytest.raises(_CreateCalledError):
        runtime.open(_GPU_SPEC)
    assert seen == [("modal", "a100")]
    assert runtime.gpu_types


@pytest.mark.parametrize("provider", ["vercel", "docker"])
def test_other_bridge_providers_refuse_a_gpu_before_a_box_starts(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    """Refuse the GPU spec with a clear error from the provider's spec check."""
    monkeypatch.setattr(sandbox_runtime, "open_session", _no_open_session)
    with pytest.raises(ServiceError, match=f"The {provider} sandbox cannot start a box with GPU type 'a100'"):
        # No network, so the Docker egress check passes and the GPU check is what refuses.
        sandbox_runtime.AsyncBridgeRuntime(provider).open(replace(_GPU_SPEC, network_disabled=True))
    # The warm box lease reads this flag to refuse the GPU before it claims a box.
    assert not sandbox_runtime.AsyncBridgeRuntime(provider).gpu_types


def test_the_local_runtimes_refuse_a_gpu(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse the GPU spec on the local and contained runtimes before any workspace opens."""
    monkeypatch.setattr(sandbox_runtime, "LocalSubprocessSession", _no_open_session)
    with pytest.raises(ServiceError, match="local sandbox cannot start a box with GPU type 'a100'"):
        sandbox_runtime.LocalSubprocessRuntime().open(_GPU_SPEC)
    with pytest.raises(ServiceError, match="contained sandbox cannot start a box with GPU type 'a100'"):
        sandbox_runtime.ContainedSubprocessRuntime().open(_GPU_SPEC)


def test_a_contained_command_gets_the_mailbox_but_no_tool_relay_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """A contained command reaches models through the mailbox and never sees the guest's tool token (SKY-880).

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    relay = "http://127.0.0.1:9/v1"
    monkeypatch.setenv("APP_BUDGET_RELAY_URL", relay)
    monkeypatch.setenv("APP_TOOL_RELAY_TOKEN", "tool-token")
    monkeypatch.setenv("SKYNET_TOOL_RELAY_TOKEN", "tool-token")
    session = sandbox_runtime.ContainedSubprocessRuntime().open(SandboxSpec(lifetime_seconds=60))
    try:
        result = session.run("env")
    finally:
        session.close()
    assert result.ok
    assert f"OPENAI_BASE_URL={relay}" in result.stdout.splitlines()
    assert "tool-token" not in result.stdout


def test_a_contained_metric_box_does_not_open_without_a_pid_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``private`` contained spec fails closed when the namespace probe fails; a plain spec still opens (SKY-885).

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    monkeypatch.setenv("APP_BUDGET_RELAY_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setattr(sandbox_runtime, "_pid_namespace_ready", False)
    monkeypatch.setattr(sandbox_runtime, "_PID_NAMESPACE", ("/nonexistent/unshare", "--"))
    with pytest.raises(ServiceError, match="needs its own user and PID namespaces"):
        sandbox_runtime.ContainedSubprocessRuntime().open(SandboxSpec(lifetime_seconds=60, private=True))
    monkeypatch.setattr(sandbox_runtime, "_PID_NAMESPACE", ("/bin/sh", "-c", "exit 1", "--"))
    with pytest.raises(ServiceError, match="needs its own user and PID namespaces"):
        sandbox_runtime.ContainedSubprocessRuntime().open(SandboxSpec(lifetime_seconds=60, private=True))
    assert not sandbox_runtime._pid_namespace_ready
    sandbox_runtime.ContainedSubprocessRuntime().open(SandboxSpec(lifetime_seconds=60)).close()


def test_only_a_contained_metric_box_runs_its_commands_in_the_pid_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``private`` contained box puts the namespace prefix before each command; other boxes do not (SKY-885).

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    monkeypatch.setenv("APP_BUDGET_RELAY_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setattr(sandbox_runtime, "_pid_namespace_ready", True)
    monkeypatch.setattr(sandbox_runtime, "_PID_NAMESPACE", ("env", "WRAPPED=yes"))
    outputs = []
    for runtime, private in (
        (sandbox_runtime.ContainedSubprocessRuntime(), True),
        (sandbox_runtime.ContainedSubprocessRuntime(), False),
        (sandbox_runtime.LocalSubprocessRuntime(), True),
    ):
        session = runtime.open(SandboxSpec(lifetime_seconds=60, private=private))
        try:
            outputs.append(session.run('echo "${WRAPPED:-no}"').stdout.strip())
        finally:
            session.close()
    assert outputs == ["yes", "no", "no"]


def _require_user_namespaces() -> None:
    """Skip the test when this host refuses unprivileged user namespaces, unless ``SKY_REQUIRE_USERNS=1``.

    The ``skynet-ci/backend-gates`` Dagger check sets the variable (SKY-886).
    Set it on a Linux host that must run these tests, so a seccomp profile
    that blocks the namespaces fails the test there instead of skipping it
    (SKY-885).
    """
    try:
        sandbox_runtime._require_pid_namespace()
    except ServiceError:
        if os.environ.get("SKY_REQUIRE_USERNS") == "1":
            raise
        pytest.skip("This host refuses unprivileged user and PID namespaces.")


def test_a_contained_metric_box_sees_only_its_own_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real contained metric box lists only its own processes in /proc and cannot unmount it (SKY-885).

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    _require_user_namespaces()
    monkeypatch.setenv("APP_BUDGET_RELAY_URL", "http://127.0.0.1:9/v1")
    session = sandbox_runtime.ContainedSubprocessRuntime().open(SandboxSpec(lifetime_seconds=60, private=True))
    umount = "import ctypes; print(ctypes.CDLL(None, use_errno=True).umount2(b'/proc', 2))"
    try:
        listed = session.run("echo $$; ls /proc").stdout.split()
        unmounted = session.run(f"{shlex.quote(sys.executable)} -I -c {shlex.quote(umount)}")
    finally:
        session.close()
    assert listed[0] == "1"
    assert str(os.getpid()) not in listed
    assert unmounted.stdout.strip() == "-1"


def test_the_pid_namespace_probe_refuses_a_root_user_inside(monkeypatch: pytest.MonkeyPatch) -> None:
    """The probe fails closed when the namespace user is root and can unmount the new /proc (SKY-885).

    A root caller under ``--map-current-user`` stays root inside, as ``--map-root-user`` makes it here.

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    _require_user_namespaces()
    root_inside = tuple(
        "--map-root-user" if flag == "--map-current-user" else flag for flag in sandbox_runtime._PID_NAMESPACE
    )
    monkeypatch.setattr(sandbox_runtime, "_pid_namespace_ready", False)
    monkeypatch.setattr(sandbox_runtime, "_PID_NAMESPACE", root_inside)
    with pytest.raises(ServiceError, match="needs its own user and PID namespaces"):
        sandbox_runtime._require_pid_namespace()


class _NoClient:
    """Fail the test when the guest sends a request to its parent."""

    def post(self, *_args: Any, **_kwargs: Any) -> Any:
        """Fail on any request.

        Raises:
            AssertionError: Always.
        """
        raise AssertionError("The guest may not ask its parent for a GPU box.")


class _NoBroker:
    """Fail the test when the parent runtime opens a box."""

    def _open_parent(self, _spec: SandboxSpec) -> Any:
        """Fail on any open.

        Raises:
            AssertionError: Always.
        """
        raise AssertionError("No parent box may start.")


def test_the_remote_and_parent_runtimes_refuse_a_gpu() -> None:
    """Refuse the GPU spec on the guest and parent paths, which carry no GPU field."""
    remote = RemoteSandboxRuntime("http://127.0.0.1:9000", "opaque-control", client=_NoClient())  # type: ignore[arg-type]
    with pytest.raises(ServiceError, match="parent sandbox cannot start a box with GPU type 'a100'"):
        remote.open(_GPU_SPEC)
    parent = _ParentSandboxRuntime(_NoBroker(), ())  # type: ignore[arg-type]
    with pytest.raises(ServiceError, match="parent's box cannot start with GPU type 'a100'"):
        parent.open(_GPU_SPEC)


def test_async_spec_adds_no_env_so_a_box_can_become_a_snapshot() -> None:
    """``_async_spec`` adds no env, so ``refuse_secrets`` accepts a box with none (SKY-812)."""
    spec = sandbox_runtime._async_spec(sandbox_runtime.SandboxSpec(lifetime_seconds=60))
    assert spec.env == {}
    refuse_secrets(spec)
