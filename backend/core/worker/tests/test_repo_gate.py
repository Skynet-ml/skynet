"""Tests for the preflight gate that refuses optimize.toml settings a run's sandbox cannot give."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from core.i18n import t_locale
from core.i18n_keys import I18nKey
from core.service_gateway.optimization.blackbox.repo_constraints import CONSTRAINTS_FILE
from core.worker.repo_gate import GateFailure, RepoGateError, check_repository, sandbox_place

_CLEAN = b"""
version = 1

[paths]
editable = ["bot"]
protected = ["bot/eval"]

[rules]
illegal = ["Do not hardcode answers."]

[proposer]
illegal = ["Do not read the metric."]

[instructions]
intent = "Answer the question."
"""


def _archive(tmp_path: Path, files: dict[str, bytes]) -> Path:
    """Pack files the way the parent ships a fetched tree.

    Args:
        tmp_path: Scratch folder.
        files: Root-relative path to bytes.

    Returns:
        The archive.
    """
    archive = tmp_path / "tree.tgz"
    with tarfile.open(archive, "w:gz") as packed:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            packed.addfile(info, io.BytesIO(data))
    return archive


def test_a_clean_file_passes(tmp_path: Path) -> None:
    """Paths, rules, proposer rules and instructions are enforced or advisory, so they pass in both places."""
    archive = _archive(tmp_path, {CONSTRAINTS_FILE: _CLEAN})

    assert check_repository(archive, (), place="local") == []
    assert check_repository(archive, (), place="cloud") == []


def test_a_repository_without_the_file_passes(tmp_path: Path) -> None:
    """No optimize.toml asks for nothing."""
    assert check_repository(_archive(tmp_path, {"bot/app.py": b"x = 1\n"}), (), place="local") == []


def test_an_unenforceable_tools_key_fails_on_local_docker(tmp_path: Path) -> None:
    """Local Docker cannot limit the proposer's tools yet, so the run stops instead of ignoring the key."""
    archive = _archive(tmp_path, {CONSTRAINTS_FILE: b'[proposer]\ntools = ["bash"]\n'})

    failures = check_repository(archive, (), place="local")

    assert failures == [
        GateFailure(
            key="[proposer] tools",
            place="local",
            reason=(
                "optimize.toml sets [proposer] tools. The local Docker sandbox cannot enforce "
                "this setting yet. Remove it to run."
            ),
            code=I18nKey.REPO_GATE_UNENFORCED_LOCAL,
            params={"key": "[proposer] tools"},
        )
    ]


def test_the_cloud_names_its_own_sandbox(tmp_path: Path) -> None:
    """The message says which sandbox cannot enforce the key."""
    archive = _archive(tmp_path, {CONSTRAINTS_FILE: b"[proposer]\nnetwork = []\n"})

    [failure] = check_repository(archive, (), place="cloud")

    assert failure.place == "cloud"
    assert failure.code == I18nKey.REPO_GATE_UNENFORCED_CLOUD
    assert "cloud sandbox" in failure.reason


def test_a_missing_secret_name_fails(tmp_path: Path) -> None:
    """A service that needs a secret the run does not have fails with the service and the secret name."""
    raw = b'[services.maps]\nsecrets = ["GOOGLE_MAPS_API_KEY", "MAPS_REGION"]\n'
    archive = _archive(tmp_path, {CONSTRAINTS_FILE: raw})

    failures = check_repository(archive, {"MAPS_REGION": "us"}, place="cloud")

    missing = [failure for failure in failures if failure.code == I18nKey.REPO_GATE_MISSING_SECRET]
    assert missing == [
        GateFailure(
            key="[services.maps] secrets",
            place="cloud",
            reason=(
                "The service maps in optimize.toml needs the secret GOOGLE_MAPS_API_KEY, but this run has "
                "no secret with that name. Add the secret to the run or remove the name from the file."
            ),
            code=I18nKey.REPO_GATE_MISSING_SECRET,
            params={"service": "maps", "secret": "GOOGLE_MAPS_API_KEY"},
        )
    ]


@pytest.mark.parametrize(
    ("raw", "key"),
    [
        (b'[proposer]\ntools = ["bash"]\n', "[proposer] tools"),
        (b"[proposer]\nnetwork = []\n", "[proposer] network"),
        (b'[proposer]\nnetwork = ["pypi.org"]\n', "[proposer] network"),
        (b'[services.maps]\n[proposer]\nservices = ["maps"]\n', "[proposer] services"),
        (b'[target.planner]\nentry = "agents/planner.py:plan"\n', "[target.planner]"),
        (b"[services.cache]\n", "[services.cache]"),
        (b'[metric]\nfiles = ["eval/scorer.py"]\n', "[metric] files"),
        (b'[metric]\nexploits = "eval/exploits"\n', "[metric] exploits"),
        (b'[metric]\nexploits = ".optimize/exploits"\n', "[metric] exploits"),
        (b"[proposer]\ntools = []\n", "[proposer] tools"),
        (b"[proposer]\nservices = []\n", "[proposer] services"),
        (b"[metric]\nfiles = []\n", "[metric] files"),
        (b'[metric]\nreference = "eval/reference.json"\n', "[metric] reference"),
    ],
)
@pytest.mark.parametrize("place", ["local", "cloud"])
def test_every_unenforced_key_fails_in_every_place(tmp_path: Path, raw: bytes, key: str, place: str) -> None:
    """A written key fails even when its value is empty or the default, because no runtime enforces it yet."""
    failures = check_repository(_archive(tmp_path, {CONSTRAINTS_FILE: raw}), (), place=place)  # type: ignore[arg-type]

    assert key in [failure.key for failure in failures]


def test_every_failure_has_a_hebrew_message(tmp_path: Path) -> None:
    """Each failure renders in Hebrew with its own parameters, so the setup check shows it in the user's language."""
    raw = b'[proposer]\ntools = ["bash"]\n[services.maps]\nsecrets = ["GOOGLE_MAPS_API_KEY"]\n'
    failures = check_repository(_archive(tmp_path, {CONSTRAINTS_FILE: raw}), (), place="local")

    assert {failure.code for failure in failures} == {
        I18nKey.REPO_GATE_UNENFORCED_LOCAL,
        I18nKey.REPO_GATE_MISSING_SECRET,
    }
    for failure in failures:
        hebrew = t_locale("he", failure.code, **failure.params)
        assert hebrew != failure.code
        assert all(value in hebrew for value in failure.params.values())
    assert (
        t_locale("he", I18nKey.REPO_GATE_UNENFORCED_CLOUD, key="[metric] files") != I18nKey.REPO_GATE_UNENFORCED_CLOUD
    )


def test_the_error_carries_every_failure_and_the_first_code(tmp_path: Path) -> None:
    """The worker's error lists every reason and keeps the first one's code for a client that shows one message."""
    raw = b"[proposer]\nnetwork = []\n[metric]\nfiles = []\n"
    failures = check_repository(_archive(tmp_path, {CONSTRAINTS_FILE: raw}), (), place="local")

    error = RepoGateError(failures)

    assert error.failures == failures
    assert error.code == I18nKey.REPO_GATE_UNENFORCED_LOCAL
    assert error.params == {"key": "[proposer] network"}
    assert "[proposer] network" in str(error)
    assert "[metric] files" in str(error)


def test_local_mode_is_the_local_place() -> None:
    """Local mode runs on Docker on this computer; every other deployment uses the cloud sandbox."""
    assert sandbox_place(True) == "local"
    assert sandbox_place(False) == "cloud"
