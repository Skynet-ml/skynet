"""Tests for inferring a repository's setup command from its fetched tree."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from core.service_gateway.optimization.blackbox.repo_setup import (
    SetupPlan,
    apt_packages,
    detect_setup_command,
    infer_setup_command,
    parse_answer,
    read_overview,
)


def _archive(tmp_path: Path, files: dict[str, str]) -> Path:
    """Pack files the way :func:`fetch_snapshot` does.

    Args:
        tmp_path: Scratch folder.
        files: Repository-relative path → content.

    Returns:
        The packed tree.
    """
    archive = tmp_path / "tree.tgz"
    with tarfile.open(archive, "w:gz") as packed:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            packed.addfile(info, io.BytesIO(data))
    return archive


@pytest.mark.parametrize(
    ("paths", "expected"),
    [
        (["pyproject.toml", "uv.lock", "requirements.txt"], "uv sync --frozen"),
        (["pyproject.toml", "poetry.lock"], "poetry install --no-interaction"),
        (["requirements.txt", "pyproject.toml"], "pip install -r requirements.txt"),
        (["pyproject.toml"], "pip install -e ."),
        (["setup.py"], "pip install -e ."),
        (["package.json", "package-lock.json"], "npm ci"),
        (["package.json", "pnpm-lock.yaml"], "pnpm install --frozen-lockfile"),
        (["package.json", "yarn.lock"], "yarn install --frozen-lockfile"),
        (["Cargo.toml", "Cargo.lock"], "cargo build"),
        (["go.mod"], "go build ./..."),
        (["README.md", "main.sh"], None),
        (["sub/requirements.txt", "package.json"], None),
    ],
)
def test_manifests_and_lockfiles_pick_the_setup_command(paths: list[str], expected: str | None) -> None:
    """A root lockfile names the tool; a bare manifest falls back to its plain installer."""
    assert detect_setup_command(paths) == expected


def test_overview_lists_every_file_but_reads_only_key_files(tmp_path: Path) -> None:
    """Show the model manifests, CI and the README, never source files or nested lookalikes."""
    archive = _archive(
        tmp_path,
        {
            "README.md": "# app",
            "pyproject.toml": "[project]\nname='app'",
            ".github/workflows/ci.yml": "run: uv sync",
            "src/app.py": "SECRET_LOGIC = 1",
            "docs/README.md": "nested",
            "uv.lock": "x" * 50_000,
        },
    )

    overview = read_overview(archive)

    assert set(overview.paths) == {
        "README.md",
        "pyproject.toml",
        ".github/workflows/ci.yml",
        "src/app.py",
        "docs/README.md",
        "uv.lock",
    }
    assert set(overview.files) == {"README.md", "pyproject.toml", ".github/workflows/ci.yml"}


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("uv sync --frozen", SetupPlan("uv sync --frozen")),
        ("```bash\nnpm ci\n```", SetupPlan("npm ci")),
        ("`make install`", SetupPlan("make install")),
        ("$ pip install -e .", SetupPlan("pip install -e .")),
        ("none", SetupPlan(None)),
        ("None.", SetupPlan(None)),
        ("pip install .\napt: libpq-dev gcc", SetupPlan("pip install .", ("gcc", "libpq-dev"))),
        ("apt: libgl1\nnone", SetupPlan(None, ("libgl1",))),
        ("make\napt: none", SetupPlan("make")),
        ("first\nsecond", None),
        ("make\napt: gcc\napt: g++", None),
        ("apt: gcc", None),
        ("", None),
        ("x" * 400, None),
    ],
)
def test_model_answers_are_read_as_one_command(answer: str, expected: SetupPlan | None) -> None:
    """Accept one line (code fences stripped) or ``none``, plus one ``apt:`` line; reject anything else."""
    assert parse_answer(answer) == expected


def test_only_valid_debian_package_names_pass_sorted_and_once() -> None:
    """Debian policy names pass; shell syntax, capitals and one-letter names drop."""
    names = ["zlib1g-dev", "g++", "libc6.1", "gcc", "gcc", "Libfoo", "a", "-x", "$(id)", "x;rm", "ok pkg", "", 3]

    assert apt_packages(names) == ("g++", "gcc", "libc6.1", "zlib1g-dev")  # type: ignore[arg-type]


def test_the_prompt_asks_for_packages_and_still_forbids_sudo(tmp_path: Path) -> None:
    """The model is told to name apt packages on their own line and never to use sudo."""
    archive = _archive(tmp_path, {"setup.py": ""})
    prompts: list[str] = []

    def ask(prompt: str) -> str:
        """Record the prompt and name a package."""
        prompts.append(prompt)
        return "pip install -e .\napt: libpq-dev"

    assert infer_setup_command(archive, ask) == SetupPlan("pip install -e .", ("libpq-dev",))
    assert "apt: <package names" in prompts[0]
    assert "Do not run tests, start servers or use sudo." in prompts[0]


def test_the_model_answer_wins_and_sees_the_key_files(tmp_path: Path) -> None:
    """The model's command is used, and its prompt carries the listing, key files and lockfiles."""
    archive = _archive(tmp_path, {"pyproject.toml": "[tool.hatch]", "uv.lock": "lock", "Makefile": "setup:\n\tuv sync"})
    prompts: list[str] = []

    def ask(prompt: str) -> str:
        """Record the prompt and answer like a model."""
        prompts.append(prompt)
        return "make setup"

    assert infer_setup_command(archive, ask) == SetupPlan("make setup")
    assert "[tool.hatch]" in prompts[0]
    assert "Lockfiles present: uv.lock" in prompts[0]
    assert "\tuv sync" in prompts[0]


def test_the_model_can_say_nothing_needs_setting_up(tmp_path: Path) -> None:
    """``none`` from the model means no setup, even when a manifest exists."""
    archive = _archive(tmp_path, {"requirements.txt": ""})

    assert infer_setup_command(archive, lambda prompt: "none") == SetupPlan(None)


@pytest.mark.parametrize("ask", [None, lambda prompt: None, lambda prompt: "a\nb"])
def test_without_a_usable_model_answer_the_manifests_decide(ask: object, tmp_path: Path) -> None:
    """No model, a refused call or a rambling reply all fall back to the lockfile."""
    archive = _archive(tmp_path, {"package.json": "{}", "package-lock.json": "{}"})

    assert infer_setup_command(archive, ask) == SetupPlan("npm ci")  # type: ignore[arg-type]


def test_a_failing_model_call_falls_back(tmp_path: Path) -> None:
    """An error from the model call never stops staging."""
    archive = _archive(tmp_path, {"go.mod": "module x"})

    def ask(prompt: str) -> str:
        """Fail like an unreachable provider."""
        raise RuntimeError("down")

    assert infer_setup_command(archive, ask) == SetupPlan("go build ./...")
