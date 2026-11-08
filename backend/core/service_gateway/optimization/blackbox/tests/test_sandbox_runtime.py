"""Tests that ``sandbox_runtime`` is a leaf module and ``sandbox`` re-exports it unchanged (SKY-486)."""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from .. import sandbox, sandbox_runtime

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

# ``core.service_gateway/__init__`` imports ``DspyService``, which loads the legacy
# module for any import under the package. The check stubs the two parent packages,
# so it measures only what ``sandbox_runtime`` itself pulls in.
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
