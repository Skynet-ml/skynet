"""Verify the worker's engine readiness check, the guest task view, the metric guards and the image build."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.exceptions import ServiceError
from core.sandbox.providers import vercel

from .. import native_engines, native_runtime, runner
from ..native_runtime import check_native_runtime, native_task_payload
from ..protocol import Task
from ..sandbox import CODEX_REQUIREMENTS, CODEX_REQUIREMENTS_PATH


class _Distribution:
    """Stand in for the installed GEPA distribution with a chosen ``direct_url.json``."""

    def __init__(self, text: str | None) -> None:
        """Keep the provenance document.

        Args:
            text: The ``direct_url.json`` text, or ``None`` when the file is missing.
        """
        self._text = text

    def read_text(self, name: str) -> str | None:
        """Return the provenance document.

        Args:
            name: The metadata file name.

        Returns:
            The text for ``direct_url.json``, else ``None``.
        """
        return self._text if name == "direct_url.json" else None


def _pinned_gepa(monkeypatch: pytest.MonkeyPatch, commit: str = native_runtime.GEPA_SOURCE) -> None:
    """Make the installed GEPA report ``commit`` as its source.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        commit: The commit the provenance names.
    """
    text = f'{{"url": "https://github.com/gepa-ai/gepa", "vcs_info": {{"commit_id": "{commit}"}}}}'
    monkeypatch.setattr(native_runtime, "distribution", lambda name: _Distribution(text))


def test_the_readiness_check_reports_the_pins_and_each_own_venv_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """The check names every pinned source and probes the AutoSaddler and ShinkaEvolve interpreters (SKY-728).

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """
    _pinned_gepa(monkeypatch)
    probed: list[tuple[str, str]] = []

    def ready(python: str, package: str) -> bool:
        """Record the probe; only AutoSaddler's interpreter is ready.

        Args:
            python: The interpreter.
            package: The package it must import.

        Returns:
            Whether the probe passes.
        """
        probed.append((python, package))
        return package == "autosaddler"

    monkeypatch.setattr(native_runtime, "_own_venv_ready", ready)

    assert check_native_runtime() == {
        "gepa_source": native_runtime.GEPA_SOURCE,
        "meta_harness_source": native_runtime.META_HARNESS_REVISION,
        "autoresearch_version": native_runtime.AUTORESEARCH_VERSION,
        "autosaddler_source": native_runtime.AUTOSADDLER_REVISION,
        "autosaddler_ready": True,
        "shinka_version": native_runtime.SHINKA_VERSION,
        "shinka_ready": False,
    }
    assert probed == [
        (native_runtime.AUTOSADDLER_IMAGE_PYTHON, "autosaddler"),
        (native_runtime.SHINKA_IMAGE_PYTHON, "shinka"),
    ]


@pytest.mark.parametrize("text", [None, '{"vcs_info": {"commit_id": "0000000"}}'])
def test_the_readiness_check_refuses_a_gepa_that_is_not_the_approved_commit(
    monkeypatch: pytest.MonkeyPatch, text: str | None
) -> None:
    """A GEPA with no provenance or another commit fails the check before any probe runs.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
        text: The installed ``direct_url.json``.
    """
    monkeypatch.setattr(native_runtime, "distribution", lambda name: _Distribution(text))
    monkeypatch.setattr(native_runtime, "_own_venv_ready", lambda python, package: pytest.fail("probed"))

    with pytest.raises(ServiceError, match=native_runtime.GEPA_SOURCE):
        check_native_runtime()


def test_an_own_venv_is_ready_only_when_its_interpreter_imports_the_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe needs an interpreter at the floor that finds the package; a missing one is not ready.

    Args:
        tmp_path: Scratch folder.
        monkeypatch: Moves the version floor around the host interpreter.
    """
    monkeypatch.setattr(native_runtime, "AUTOSADDLER_PYTHON_FLOOR", sys.version_info[:2])
    assert native_runtime._own_venv_ready(sys.executable, "json")
    assert not native_runtime._own_venv_ready(sys.executable, "zq_no_such_package")
    assert not native_runtime._own_venv_ready(str(tmp_path / "missing" / "python"), "json")

    monkeypatch.setattr(native_runtime, "AUTOSADDLER_PYTHON_FLOOR", (sys.version_info[0] + 1, 0))
    assert not native_runtime._own_venv_ready(sys.executable, "json")


def test_an_own_venv_probe_that_hangs_is_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """A probe that times out reports the engine as not ready, and never raises.

    Args:
        monkeypatch: Pytest monkeypatch fixture.
    """

    def hang(*args: Any, **kwargs: Any) -> Any:
        """Time out like a stuck interpreter.

        Args:
            args: The command.
            kwargs: The run options.

        Raises:
            subprocess.TimeoutExpired: Always.
        """
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(native_runtime.subprocess, "run", hang)

    assert not native_runtime._own_venv_ready(sys.executable, "json")


def test_the_deployed_backend_image_builds_with_shinka_evolve() -> None:
    """The worker runs ShinkaEvolve from the deployed backend image, so its build turns the venv on (SKY-728)."""
    script = (Path(__file__).resolve().parents[6] / "infra/terraform/scripts/build-push.sh").read_text()
    lane = script[script.index("build_backend_lane() {") :]
    lane = lane[: lane.index("\n}\n")]
    assert "--build-arg INCLUDE_SHINKA_EVOLVE=1" in lane


def test_dockerfile_bakes_autosaddler_into_the_venv_the_runtime_uses() -> None:
    """Bake the AutoSaddler venv at the runtime's path from the same pinned requirements (SKY-645)."""
    dockerfile = (Path(__file__).resolve().parents[5] / "Dockerfile").read_text()
    venv = native_runtime.AUTOSADDLER_IMAGE_PYTHON.removesuffix("/bin/python")
    assert f"python -m venv {venv}" in dockerfile
    assert "core/service_gateway/optimization/blackbox/autosaddler_requirements.txt" in dockerfile
    assert f'{native_runtime.AUTOSADDLER_IMAGE_PYTHON} -c "import autosaddler.v2.core.engine"' in dockerfile


def test_dockerfile_bakes_shinka_into_the_venv_the_runtime_uses() -> None:
    """Keep the opt-in ShinkaEvolve venv at the runtime's path, built from the same pinned requirements."""
    dockerfile = (Path(__file__).resolve().parents[5] / "Dockerfile").read_text()
    venv = native_runtime.SHINKA_IMAGE_PYTHON.removesuffix("/bin/python")
    assert "ARG INCLUDE_SHINKA_EVOLVE=false" in dockerfile
    assert '[ "$INCLUDE_SHINKA_EVOLVE" = "1" ]' in dockerfile
    assert f"python -m venv {venv}" in dockerfile
    assert "core/service_gateway/optimization/blackbox/shinka_requirements.txt" in dockerfile
    assert f'{native_runtime.SHINKA_IMAGE_PYTHON} -c "import shinka.core.async_runner"' in dockerfile


def test_dockerfile_ships_the_codex_requirements_file_root_owned() -> None:
    """Bake the exact requirements file as root before ``USER 1001`` (SKY-389)."""
    dockerfile = (Path(__file__).resolve().parents[5] / "Dockerfile").read_text()
    bake = dockerfile.index(f"> {CODEX_REQUIREMENTS_PATH}")
    assert "install -d -o root -g root -m 0755 /etc/codex" in dockerfile
    assert "printf '" + CODEX_REQUIREMENTS.replace("\n", "\\n") + "'" in dockerfile
    assert f"chown root:root {CODEX_REQUIREMENTS_PATH}" in dockerfile
    assert f"chmod 0644 {CODEX_REQUIREMENTS_PATH}" in dockerfile
    assert bake < dockerfile.index("\nUSER 1001\n")


def test_dockerfile_ships_sudo_only_in_the_sandbox_image_and_requires_setpriv() -> None:
    """Install sudo only on opt-in, before ``USER 1001``, and fail the build without the ``no_new_privs`` launcher."""
    dockerfile = (Path(__file__).resolve().parents[5] / "Dockerfile").read_text()
    block = dockerfile[dockerfile.index("ARG INCLUDE_SANDBOX_SUDO=false") : dockerfile.index("\nUSER 1001\n")]
    step = block[block.index("RUN ") : block.index("\n    fi\n")]
    assert step.startswith('RUN if [ "$INCLUDE_SANDBOX_SUDO" = "1" ] || [ "$INCLUDE_SANDBOX_SUDO" = "true" ]; then')
    assert re.search(r"apt-get install[^\n]*\bsudo\b", step)
    assert "'appuser ALL=(ALL) NOPASSWD:ALL'" in step
    assert f"'Defaults secure_path=\"{vercel.ROOT_PATH}\"'" in step
    guard = "/usr/local/libexec/skynet-path-guard"
    assert f"install -d -o root -g root -m 0755 {guard}" in step
    assert f"ln -s /usr/bin/sudo {guard}/sudo" in step
    path = dockerfile.index(f'\nENV PATH="{guard}:/app/.venv/bin:$PATH"\n')
    assert dockerfile.count("\nENV PATH=") == 1
    assert dockerfile.index('\nENV APP_ENTRY_PATH="$PATH"\n') > path
    assert f"ENV {runner.ENV_ENTRY_PATH}=" in dockerfile
    assert f"'export PATH=\"{guard}:/app/.venv/bin:$PATH\"' > /etc/profile.d/skynet-runtime.sh" in dockerfile
    assert "chmod 0440 /etc/sudoers.d/sandbox" in step
    assert "visudo -cf /etc/sudoers.d/sandbox" in step
    assert f"test -x {vercel.UNPRIVILEGED[0]}" in step
    assert len(re.findall(r"apt-get install[^\n]*\bsudo\b", dockerfile)) == 1


def test_dockerfile_closes_root_path_programs_to_the_box_user() -> None:
    """Close the installed programs in ``/usr/local/bin`` and fail a sudo build if root's ``PATH`` stays open."""
    dockerfile = (Path(__file__).resolve().parents[5] / "Dockerfile").read_text()
    assert (
        "pip install --no-cache-dir uv \\\n        && chmod 0755 /usr/local/bin/uv /usr/local/bin/uvx \\" in dockerfile
    )
    assert "chmod 0755 /usr/local/bin/deno" in dockerfile
    assert "chmod +x /usr/local/bin/deno" not in dockerfile
    assert "tar --no-same-owner -xzf /tmp/github-mcp-server.tar.gz -C /usr/local/bin github-mcp-server" in dockerfile
    assert "chmod 0755 /usr/local/bin/github-mcp-server" in dockerfile
    user = dockerfile.index("\nUSER 1001\n")
    start = dockerfile.rindex("\nRUN ", 0, dockerfile.index("open=$(find "))
    step = dockerfile[start:user]
    assert step.startswith('\nRUN if [ "$INCLUDE_SANDBOX_SUDO" = "1" ] || [ "$INCLUDE_SANDBOX_SUDO" = "true" ]; then')
    assert step.rstrip().endswith("fi")
    assert "set -e;" in step
    assert 'if [ -n "$open" ]; then' in step
    assert "exit 1;" in step
    folders = re.search(r"open=\$\(find -L ((?:/\S+ )+)\\\( ! -user root -o -perm /022 \\\) -print\)", step)
    assert folders
    assert "-type l" not in step
    checked = set(folders.group(1).split())
    assert checked == {"/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin"}
    # /bin and /sbin link to /usr/bin and /usr/sbin in the base image.
    merged = {"/bin": "/usr/bin", "/sbin": "/usr/sbin"}
    assert {merged.get(folder, folder) for folder in vercel.ROOT_PATH.split(":")} <= checked
    for install in ("apt-get install", "pip install", "uv sync", "unzip ", "tar ", "/etc/codex/requirements.toml"):
        assert dockerfile.rindex(install) < start


def test_a_bare_case_reaches_the_guest_whole_without_expected() -> None:
    """A case with no ``input`` key is its own input in the parent, so the guest gets the same input."""
    task = Task("seed", cases=[{"question": "2+2?", "answer": "4"}, {"question": "3+3?", "expected": "6"}])

    sent = native_task_payload("autosaddler", task)["train_set"]

    assert sent == [
        {"id": 0, "input": {"question": "2+2?", "answer": "4"}},
        {"id": 1, "input": {"question": "3+3?"}},
    ]


@pytest.mark.parametrize(
    ("case", "guest_input"),
    [
        ({"question": "2+2?", "expected": "4"}, {"question": "2+2?"}),
        ({"question": "2+2?"}, {"question": "2+2?"}),
        ({"input": None, "expected": "4"}, {}),
        ({"input": {"question": "2+2?"}, "expected": "4"}, {"question": "2+2?"}),
    ],
)
def test_the_guest_gets_the_input_and_no_expected_key(case: dict[str, Any], guest_input: dict[str, Any]) -> None:
    """The guest gets the case's input without ``expected``, and an empty input becomes ``{}``.

    Args:
        case: A train case.
        guest_input: The input the guest must get.
    """
    [sent] = native_task_payload("autosaddler", Task("seed", cases=[case]))["train_set"]

    assert sent == {"id": 0, "input": guest_input}
    assert "expected" not in sent
    assert "expected" not in sent["input"]


def test_native_fill_unscored_gives_a_failed_run_version_no_score() -> None:
    """A version with a failed [run] case is negative infinity on every case; a scorer's own status stays (SKY-751)."""
    inf = float("-inf")
    failed = {"status": native_engines.RUN_FAILED_STATUS}

    assert native_engines.fill_unscored(["x", "x", "y"], [0.9, inf, 0.3], [{}, failed, {}]) == [inf, inf, 0.3]
    assert native_engines.fill_unscored(["x", "x"], [0.4, 0.6], [failed, {}]) == [0.4, 0.6]


def test_native_fill_unscored_fills_per_version() -> None:
    """A null case takes its own version's scored mean; a version with no scored case stays negative infinity."""
    inf = float("-inf")

    assert native_engines.fill_unscored(["x", "x", "y", "y"], [0.2, inf, inf, inf]) == [0.2, 0.2, inf, inf]


def test_scored_threshold_stopper_ignores_a_crashed_version() -> None:
    """Only a version with no crashed case can reach ``stop_at_score`` (SKY-367)."""
    state = SimpleNamespace(
        program_candidates=[{"current_candidate": "crashed"}, {"current_candidate": "ok"}],
        program_full_scores_val_set=[1.0, 0.5],
    )
    crashed = lambda candidate: native_engines.unwrap_text_candidate(candidate) == "crashed"  # noqa: E731

    assert not native_engines.ScoredThresholdStopper(0.9, crashed)(state)
    assert native_engines.ScoredThresholdStopper(0.5, crashed)(state)


def test_unsupported_runtime_reason_names_the_cloud_sandbox_not_its_vendor() -> None:
    """Keep the sandbox vendor out of the reason a user sees for an unsupported runtime."""
    reason = native_runtime.native_runtime_unavailable_reason("docker", SimpleNamespace(local_mode=False))

    assert reason == "Production optimizations run in the managed cloud sandbox."
    assert "vercel" not in reason.lower()
