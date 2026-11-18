"""Fixtures shared by the black-box tests."""

from __future__ import annotations

import re
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from core.service_gateway.optimization.blackbox.sandbox import LocalSubprocessRuntime, sandbox_runtime_context


@pytest.fixture(autouse=True)
def local_scorer_runtime(request: pytest.FixtureRequest) -> Iterator[None]:
    """Bind the local adapter except while its settings resolver is under test.

    Args:
        request: Current test metadata used to exempt the resolver test module.

    Yields:
        Control while the deterministic sandbox override is active.
    """
    if request.path.name in {"test_sandbox.py", "test_sandbox_local.py"} or request.node.name == (
        "test_runtime_context_overrides_settings_and_restores_nested_bindings"
    ):
        yield
        return
    with sandbox_runtime_context(LocalSubprocessRuntime()):
        yield


@pytest.fixture
def planted_bare_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Make a tree folder shaped as a bare repository whose config runs a command (SKY-812).

    The folder is a real bare clone, so plain git treats it as a repository.
    Its ``info/attributes`` sends every file through a filter, and the filter
    creates the marker file when git runs it.

    Args:
        tmp_path: Pytest temporary folder.

    Returns:
        The bare folder, which holds one commit with ``x``, and the marker path.
    """
    git = shutil.which("git")
    version = subprocess.run([git, "--version"], capture_output=True, text=True, check=False).stdout if git else ""
    found = re.search(r"(\d+)\.(\d+)", version)
    if found is None or (int(found[1]), int(found[2])) < (2, 38):
        pytest.skip("git 2.38 or later not installed")
    source = tmp_path / "source"
    source.mkdir()
    (source / "x").write_text("hi\n")
    identity = ["-c", "user.name=t", "-c", "user.email=t@localhost", "-c", "commit.gpgsign=false"]
    for arguments in (["init", "-q"], ["add", "x"], [*identity, "commit", "-q", "-m", "x"]):
        subprocess.run(["git", *arguments], cwd=source, check=True, capture_output=True)
    repo = tmp_path / "tree" / "vendor"
    subprocess.run(["git", "clone", "-q", "--bare", str(source), str(repo)], check=True, capture_output=True)
    marker = tmp_path / "marker"
    (repo / "info").mkdir(exist_ok=True)
    (repo / "info" / "attributes").write_text("* filter=pwn\n")
    with (repo / "config").open("a") as config:
        config.write(f'[filter "pwn"]\n\tsmudge = "touch \'{marker}\'; cat"\n\tclean = "touch \'{marker}\'; cat"\n')
    return repo, marker
