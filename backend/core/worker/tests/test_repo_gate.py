"""Tests for the preflight gate that refuses optimize.toml settings a run's sandbox cannot give."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from core.billing.env_connectors import CONNECTORS
from core.i18n import t_locale
from core.i18n_keys import I18nKey
from core.service_gateway.optimization.blackbox.repo_constraints import CONSTRAINTS_FILE
from core.worker import repo_gate
from core.worker.repo_gate import GateFailure, RepoGateError, check_hidden, check_repository, sandbox_place

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
        (b"[proposer]\ntools = []\n", "[proposer] tools"),
        (b"[proposer]\nservices = []\n", "[proposer] services"),
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
    raw = b'[proposer]\nnetwork = []\n[metric]\nreference = "eval/reference.json"\n'
    failures = check_repository(_archive(tmp_path, {CONSTRAINTS_FILE: raw}), (), place="local")

    error = RepoGateError(failures)

    assert error.failures == failures
    assert error.code == I18nKey.REPO_GATE_UNENFORCED_LOCAL
    assert error.params == {"key": "[proposer] network"}
    assert "[proposer] network" in str(error)
    assert "[metric] reference" in str(error)


@pytest.mark.parametrize("place", ["local", "cloud"])
def test_proposer_allowed_hosts_fail_with_their_own_message(tmp_path: Path, place: str) -> None:
    """The proposer gets no allowed hosts yet, so the failure has its own code and names the hosts (SKY-354)."""
    raw = b'[proposer]\nnetwork = ["pypi.org", "files.pythonhosted.org"]\n'
    failures = check_repository(_archive(tmp_path, {CONSTRAINTS_FILE: raw}), (), place=place)  # type: ignore[arg-type]

    assert [(failure.key, failure.code) for failure in failures] == [
        ("[proposer] network", I18nKey.REPO_GATE_PROPOSER_HOSTS_UNSUPPORTED)
    ]
    assert failures[0].params == {"hosts": "pypi.org, files.pythonhosted.org"}
    assert "pypi.org, files.pythonhosted.org" in failures[0].reason


@pytest.mark.parametrize("place", ["local", "cloud"])
def test_a_service_that_names_no_connector_fails_with_its_own_message(tmp_path: Path, place: str) -> None:
    """A service is a catalog connector, so an unknown name gets its own code and the known names (SKY-354)."""
    raw = b"[services.cache]\n[services.maps]\n"
    failures = check_repository(_archive(tmp_path, {CONSTRAINTS_FILE: raw}), (), place=place)  # type: ignore[arg-type]

    codes = {failure.key: failure.code for failure in failures}
    assert codes["[services.cache]"] == I18nKey.REPO_GATE_UNKNOWN_CONNECTOR
    assert codes["[services.maps]"] != I18nKey.REPO_GATE_UNKNOWN_CONNECTOR
    unknown = next(failure for failure in failures if failure.key == "[services.cache]")
    assert unknown.params == {"service": "cache", "known": ", ".join(CONNECTORS)}
    assert "is not a connector" in unknown.reason


@pytest.mark.parametrize("place", ["local", "cloud"])
def test_the_specific_checks_do_not_depend_on_enforced(
    tmp_path: Path, place: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a later issue enforces these keys, proposer hosts and unknown connectors still stop the run."""
    enforced = {name: kinds | {"services", "proposer.network"} for name, kinds in repo_gate.ENFORCED.items()}
    monkeypatch.setattr(repo_gate, "ENFORCED", enforced)
    raw = b'[proposer]\nnetwork = ["api.example.com"]\n[services.nosuch]\n[services.maps]\n'

    failures = check_repository(_archive(tmp_path, {CONSTRAINTS_FILE: raw}), (), place=place)  # type: ignore[arg-type]

    assert [(failure.key, failure.code) for failure in failures] == [
        ("[proposer] network", I18nKey.REPO_GATE_PROPOSER_HOSTS_UNSUPPORTED),
        ("[services.nosuch]", I18nKey.REPO_GATE_UNKNOWN_CONNECTOR),
    ]


def test_the_new_messages_exist_in_hebrew() -> None:
    """Each new code renders in the Hebrew base catalog with its placeholders filled."""
    hosts = t_locale("he", I18nKey.REPO_GATE_PROPOSER_HOSTS_UNSUPPORTED, hosts="pypi.org")
    connector = t_locale("he", I18nKey.REPO_GATE_UNKNOWN_CONNECTOR, service="cache", known="maps")

    assert "pypi.org" in hosts
    assert "cache" in connector
    assert "maps" in connector


def test_local_mode_is_the_local_place() -> None:
    """Local mode runs on Docker on this computer; every other deployment uses the cloud sandbox."""
    assert sandbox_place(True) == "local"
    assert sandbox_place(False) == "cloud"


@pytest.mark.parametrize(
    "raw",
    [
        b'[metric]\nfiles = ["eval/scorer.py"]\n',
        b"[metric]\nfiles = []\n",
        b'[metric]\nexploits = "eval/exploits"\n',
        b'[metric]\nexploits = ".optimize/exploits"\n',
    ],
)
@pytest.mark.parametrize("place", ["local", "cloud"])
def test_metric_files_and_exploits_are_enforced_in_every_place(tmp_path: Path, raw: bytes, place: str) -> None:
    """Staging strips the metric files and the exploit folder from the agent's tree, so the keys pass (SKY-353)."""
    assert check_repository(_archive(tmp_path, {CONSTRAINTS_FILE: raw}), (), place=place) == []  # type: ignore[arg-type]


def test_a_metric_file_the_agent_can_still_see_fails_and_names_the_path(tmp_path: Path) -> None:
    """The gate compares the agent's tree with the hidden paths and names each file still visible."""
    (tmp_path / "full").mkdir()
    (tmp_path / "agent").mkdir()
    original = _archive(tmp_path / "full", {"src/app.py": b"x\n", "eval/scorer.py": b"s\n"})
    leaked = _archive(tmp_path / "agent", {"src/app.py": b"x\n", "./eval/scorer.py": b"s\n"})
    clean = _archive(tmp_path, {"src/app.py": b"x\n"})

    [failure] = check_hidden(
        original, leaked, ["eval", ".optimize/exploits"], place="cloud", optional_paths=[".optimize/exploits"]
    )

    assert failure.key == "[metric] files"
    assert failure.code == I18nKey.REPO_GATE_UNENFORCED_CLOUD
    assert "eval/scorer.py" in failure.reason
    assert (
        check_hidden(
            original, clean, ["eval", ".optimize/exploits"], place="cloud", optional_paths=[".optimize/exploits"]
        )
        == []
    )


def test_a_link_that_points_into_a_hidden_folder_fails(tmp_path: Path) -> None:
    """A symbolic link inside a hidden folder that points at a visible file is not hidden by the strip."""
    archive = tmp_path / "tree.tgz"
    with tarfile.open(archive, "w:gz") as packed:
        data = tarfile.TarInfo("src/weights.json")
        data.size = 1
        packed.addfile(data, io.BytesIO(b"w"))
        link = tarfile.TarInfo("eval/weights.json")
        link.type, link.linkname = tarfile.SYMTYPE, "../src/weights.json"
        packed.addfile(link)
    (tmp_path / "agent").mkdir()
    agent = _archive(tmp_path / "agent", {"src/weights.json": b"w"})

    [failure] = check_hidden(archive, agent, ["eval"], place="local")

    assert "src/weights.json" in failure.reason


def test_a_metric_file_below_a_linked_folder_fails(tmp_path: Path) -> None:
    """A hidden path under a linked folder lives at the link's target, so the gate stops the run."""
    archive = tmp_path / "tree.tgz"
    with tarfile.open(archive, "w:gz") as packed:
        data = tarfile.TarInfo("scoring/metric.py")
        data.size = 1
        packed.addfile(data, io.BytesIO(b"m"))
        link = tarfile.TarInfo("eval")
        link.type, link.linkname = tarfile.SYMTYPE, "scoring"
        packed.addfile(link)
    (tmp_path / "agent").mkdir()
    agent = _archive(tmp_path / "agent", {"scoring/metric.py": b"m"})

    [failure] = check_hidden(archive, agent, ["eval/metric.py"], place="cloud")

    assert "eval/metric.py" in failure.reason


def _linked(tmp_path: Path, files: dict[str, bytes], links: dict[str, str]) -> Path:
    """Pack files and symbolic links into one archive.

    Args:
        tmp_path: Scratch folder.
        files: Root-relative path to bytes.
        links: Root-relative link path to its target.

    Returns:
        The archive.
    """
    archive = tmp_path / "tree.tgz"
    with tarfile.open(archive, "w:gz") as packed:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            packed.addfile(info, io.BytesIO(data))
        for name, target in links.items():
            link = tarfile.TarInfo(name)
            link.type, link.linkname = tarfile.SYMTYPE, target
            packed.addfile(link)
    return archive


def test_a_hidden_link_to_another_hidden_file_passes(tmp_path: Path) -> None:
    """A link whose target stays inside the hidden paths shows the agent nothing (SKY-353)."""
    original = _linked(
        tmp_path,
        {"src/app.py": b"x\n", "eval/impl.py": b"m\n", ".optimize/exploits/a.md": b"a\n"},
        {"eval/metric.py": "impl.py", ".optimize/exploits/b.md": "a.md"},
    )
    (tmp_path / "agent").mkdir()
    agent = _archive(tmp_path / "agent", {"src/app.py": b"x\n"})

    hidden = ["eval/metric.py", "eval/impl.py", ".optimize/exploits"]
    assert check_hidden(original, agent, hidden, place="cloud", optional_paths=[".optimize/exploits"]) == []


def test_a_metric_path_that_matches_no_file_fails(tmp_path: Path) -> None:
    """A misspelled ``[metric] files`` entry hides nothing, so the run stops; a missing exploit folder is fine."""
    original = _archive(tmp_path, {"src/app.py": b"x\n", "eval/metric.py": b"m\n"})
    (tmp_path / "agent").mkdir()
    agent = _archive(tmp_path / "agent", {"src/app.py": b"x\n"})
    exploits = [".optimize/exploits"]

    [failure] = check_hidden(
        original, agent, ["eval/metrics.py", "eval/metric.py", *exploits], place="local", optional_paths=exploits
    )

    assert failure.key == "[metric] files"
    assert "eval/metrics.py" in failure.reason
    assert check_hidden(original, agent, ["eval/metric.py", *exploits], place="local", optional_paths=exploits) == []
