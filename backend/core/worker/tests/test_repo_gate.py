"""Tests for the preflight gate that refuses optimize.toml settings a run's sandbox cannot give."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from core.billing import env_connectors
from core.i18n import t_locale
from core.i18n_keys import I18nKey
from core.service_gateway.optimization.blackbox.repo_constraints import CONSTRAINTS_FILE, ConstraintsError
from core.worker import repo_gate
from core.worker.repo_gate import GateFailure, RepoGateError, check_hidden, check_repository, sandbox_place

_CLEAN = b"""
version = 1
instructions = "docs/intent.md"

[paths]
editable = ["bot"]
protected = ["bot/eval"]

[proposer]
illegal = "docs/rules.md"
"""

# The files the parametrized keys name, so the setup check can read them.
_NAMED_FILES = {
    ".mcp.json": b'{"mcpServers": {}}',
    "eval/.mcp.json": b'{"mcpServers": {}}',
    "rules/run.md": b"- r\n",
    "rules/metric.md": b"- m\n",
}


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
    """Paths, proposer rules and instructions are enforced or advisory, so they pass in both places."""
    archive = _archive(tmp_path, {CONSTRAINTS_FILE: _CLEAN, "docs/intent.md": b"Be fast.\n", "docs/rules.md": b"- r\n"})

    assert check_repository(archive, (), place="local") == []
    assert check_repository(archive, (), place="cloud") == []


def test_a_repository_without_the_file_passes(tmp_path: Path) -> None:
    """No optimize.toml asks for nothing."""
    assert check_repository(_archive(tmp_path, {"bot/app.py": b"x = 1\n"}), (), place="local") == []


def test_an_unenforceable_proposer_mcp_key_fails_on_local_docker(tmp_path: Path) -> None:
    """No box gets the proposer's MCP servers yet (SKY-385), so the run stops instead of ignoring the key."""
    archive = _archive(tmp_path, {CONSTRAINTS_FILE: b'[proposer]\nmcp = ".mcp.json"\n', **_NAMED_FILES})

    failures = check_repository(archive, (), place="local")

    assert failures == [
        GateFailure(
            key="[proposer] mcp",
            place="local",
            reason=(
                "optimize.toml sets [proposer] mcp. The local Docker sandbox cannot enforce "
                "this setting yet. Remove it to run."
            ),
            code=I18nKey.REPO_GATE_UNENFORCED_LOCAL,
            params={"key": "[proposer] mcp"},
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
    raw = (
        b'[services.maps]\nurl = "https://maps.example.com"\n'
        b'headers = { Authorization = "Bearer ${GOOGLE_MAPS_API_KEY}", X-Region = "${MAPS_REGION}" }\n'
    )
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
        (b'[proposer]\nmcp = ".mcp.json"\n', "[proposer] mcp"),
        (b"[proposer]\nnetwork = []\n", "[proposer] network"),
        (b'[proposer]\nnetwork = ["pypi.org"]\n', "[proposer] network"),
        (b'[services.maps]\nurl = "https://api.example.com"\n[proposer]\nservices = ["maps"]\n', "[proposer] services"),
        (b'[target.planner]\nentry = "agents/planner.py:plan"\n', "[target.planner]"),
        (b'[target.planner]\nentry = "agents/planner.py:plan"\nmcp = "eval/.mcp.json"\n', "[target.planner]"),
        (b"[proposer]\nservices = []\n", "[proposer] services"),
        (b'[metric]\nreference = "eval/reference.json"\n', "[metric] reference"),
        (b'[run]\nentry = "geocode.py:geocode"\n', "[run] entry"),
        (b'[run]\nentry = ["pytest", "-q"]\n', "[run] entry"),
        (b'[run]\nentry = "a.py:f"\ntimeout = 30\n', "[run] timeout"),
        (b'[run]\nentry = "a.py:f"\nmcp = ".mcp.json"\n', "[run] mcp"),
        (b'[run]\nentry = "a.py:f"\nillegal = "rules/run.md"\n', "[run] illegal"),
        (b'[metric]\nmcp = "eval/.mcp.json"\n', "[metric] mcp"),
        (b'[metric]\nillegal = "rules/metric.md"\n', "[metric] illegal"),
    ],
)
@pytest.mark.parametrize("place", ["local", "cloud"])
def test_every_unenforced_key_fails_in_every_place(tmp_path: Path, raw: bytes, key: str, place: str) -> None:
    """A written key fails even when its value is empty or the default, because no runtime enforces it yet."""
    failures = check_repository(_archive(tmp_path, {CONSTRAINTS_FILE: raw, **_NAMED_FILES}), (), place=place)  # type: ignore[arg-type]

    assert key in [failure.key for failure in failures]


@pytest.mark.parametrize(
    ("raw", "key"),
    [
        (b'[services.cache]\nurl = "https://api.example.com"\n', "[services.cache]"),
        (
            b'[services.maps]\nurl = "https://api.example.com"\n[run]\nentry = "a.py:f"\nservices = ["maps"]\n',
            "[run] services",
        ),
        (b'[run]\nentry = "a.py:f"\nservices = []\n', "[run] services"),
        (b'[services.grader]\nurl = "https://api.example.com"\n[metric]\nservices = ["grader"]\n', "[metric] services"),
        (b"[metric]\nservices = []\n", "[metric] services"),
    ],
)
@pytest.mark.parametrize("place", ["local", "cloud"])
def test_stage_services_pass_in_every_place(tmp_path: Path, raw: bytes, key: str, place: str) -> None:
    """The stage relay enforces the run and metric service lists, so the gate stops refusing them (SKY-385)."""
    failures = check_repository(_archive(tmp_path, {CONSTRAINTS_FILE: raw, **_NAMED_FILES}), (), place=place)  # type: ignore[arg-type]

    assert key not in [failure.key for failure in failures]


def test_every_failure_has_a_hebrew_message(tmp_path: Path) -> None:
    """Each failure renders in Hebrew with its own parameters, so the setup check shows it in the user's language."""
    raw = b'[proposer]\nnetwork = []\n[services.maps]\nurl = "https://api.example.com"\nheaders = { Authorization = "${GOOGLE_MAPS_API_KEY}" }\n'
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


def _mcp_archive(tmp_path: Path, server: str) -> Path:
    """Pack a ``[metric] mcp`` file with one server.

    Args:
        tmp_path: Scratch folder.
        server: The server's JSON object.

    Returns:
        The archive.
    """
    raw = b'[metric]\nmcp = "eval/.mcp.json"\n'
    mcp = b'{"mcpServers": {"grader": ' + server.encode() + b"}}"
    return _archive(tmp_path, {CONSTRAINTS_FILE: raw, "eval/.mcp.json": mcp})


def test_a_missing_mcp_server_secret_fails(tmp_path: Path) -> None:
    """A user-bound ``${NAME}`` in a role's MCP file needs a run secret, and the message names the file (E49)."""
    archive = _mcp_archive(tmp_path, '{"url": "https://grader.example.com/mcp", "headers": {"X-Key": "${GRADER_KEY}"}}')
    secret_codes = (I18nKey.REPO_GATE_MISSING_SECRET, I18nKey.REPO_GATE_MISSING_MCP_SECRET)

    missing = [f for f in check_repository(archive, (), place="cloud") if f.code in secret_codes]

    assert [(f.key, f.code, f.params) for f in missing] == [
        (
            "[metric] mcp secrets",
            I18nKey.REPO_GATE_MISSING_MCP_SECRET,
            {"server": "grader", "path": "eval/.mcp.json", "secret": "GRADER_KEY"},
        )
    ]
    assert not [f for f in check_repository(archive, {"GRADER_KEY"}, place="cloud") if f.code in secret_codes]


def test_the_missing_mcp_secret_message_names_the_server_the_file_and_the_fix() -> None:
    """The MCP message names the server, the secret and the file, and says to add the secret to the run."""
    params = {"server": "grader", "path": "eval/.mcp.json", "secret": "GRADER_KEY"}

    english = t_locale("en", I18nKey.REPO_GATE_MISSING_MCP_SECRET, **params)
    hebrew = t_locale("he", I18nKey.REPO_GATE_MISSING_MCP_SECRET, **params)

    for text in (english, hebrew):
        assert all(value in text for value in params.values())
    assert "Add the secret to the run" in english
    assert "service" not in english


_MAPS = (
    b'[services.maps]\nurl = "https://mapstools.googleapis.com/mcp"\n'
    b'headers = { Authorization = "Bearer ${GOOGLE_MAPS_KEY}" }\n'
)


def _managed_failures(archive: Path) -> list[GateFailure]:
    """Run the gate with ``GOOGLE_MAPS_KEY`` bound to Skynet's key and keep the managed-key failures.

    Args:
        archive: The packed tree.

    Returns:
        The failures with the managed-key code.
    """
    failures = check_repository(archive, (), place="cloud", bindings={"GOOGLE_MAPS_KEY": "managed"})
    return [failure for failure in failures if failure.code == I18nKey.REPO_GATE_MANAGED_SECRET_UNAVAILABLE]


def test_a_managed_binding_with_a_skynet_key_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A managed name on a catalog host with a Skynet key needs no run secret (E50)."""
    monkeypatch.setattr(env_connectors, "managed_key_host", lambda host: host == "mapstools.googleapis.com")
    monkeypatch.setattr(repo_gate, "managed_key_host", env_connectors.managed_key_host)

    failures = check_repository(
        _archive(tmp_path, {CONSTRAINTS_FILE: _MAPS}), (), place="cloud", bindings={"GOOGLE_MAPS_KEY": "managed"}
    )

    assert [
        f
        for f in failures
        if f.code in (I18nKey.REPO_GATE_MISSING_SECRET, I18nKey.REPO_GATE_MANAGED_SECRET_UNAVAILABLE)
    ] == []


def test_a_managed_binding_on_a_host_with_no_skynet_key_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The setup check stops a managed name when this deployment holds no key for its host (E50)."""
    monkeypatch.setattr(repo_gate, "managed_key_host", lambda host: False)

    [failure] = _managed_failures(_archive(tmp_path, {CONSTRAINTS_FILE: _MAPS}))

    assert failure.key == "[services.maps] secrets"
    assert failure.params == {"secret": "GOOGLE_MAPS_KEY", "host": "mapstools.googleapis.com", "known": "-"}


def test_a_managed_binding_on_a_host_outside_the_catalog_fails(tmp_path: Path) -> None:
    """Managed is allowed only for a catalog host, so another host fails even with keys configured (E50)."""
    raw = _MAPS.replace(b"mapstools.googleapis.com", b"maps.example.com")

    [failure] = _managed_failures(_archive(tmp_path, {CONSTRAINTS_FILE: raw}))

    assert failure.params["secret"] == "GOOGLE_MAPS_KEY"
    assert failure.params["host"] == "maps.example.com"


def test_a_stdio_mcp_server_fails_the_setup_check(tmp_path: Path) -> None:
    """Only http servers are supported now (E64), so the gate refuses a stdio server before any sandbox starts."""
    archive = _mcp_archive(tmp_path, '{"command": "npx", "args": ["maps"], "env": {"KEY": "${GOOGLE_MAPS_KEY}"}}')

    with pytest.raises(ConstraintsError, match=r"Only http servers are supported now"):
        check_repository(archive, ("GOOGLE_MAPS_KEY",), place="cloud", bindings={"GOOGLE_MAPS_KEY": "managed"})


@pytest.mark.parametrize("place", ["local", "cloud"])
def test_the_specific_checks_do_not_depend_on_enforced(
    tmp_path: Path, place: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a later issue enforces these keys, proposer hosts still stop the run."""
    enforced = {name: kinds | {"services", "proposer.network"} for name, kinds in repo_gate.ENFORCED.items()}
    monkeypatch.setattr(repo_gate, "ENFORCED", enforced)
    raw = b'[proposer]\nnetwork = ["api.example.com"]\n[services.maps]\nurl = "https://api.example.com"\n'

    failures = check_repository(_archive(tmp_path, {CONSTRAINTS_FILE: raw}), (), place=place)  # type: ignore[arg-type]

    assert [(failure.key, failure.code) for failure in failures] == [
        ("[proposer] network", I18nKey.REPO_GATE_PROPOSER_HOSTS_UNSUPPORTED),
    ]


def test_the_new_messages_exist_in_hebrew() -> None:
    """Each new code renders in the Hebrew base catalog with its placeholders filled."""
    hosts = t_locale("he", I18nKey.REPO_GATE_PROPOSER_HOSTS_UNSUPPORTED, hosts="pypi.org")
    mcp = t_locale(
        "he", I18nKey.REPO_GATE_MISSING_MCP_SECRET, server="grader", path="eval/.mcp.json", secret="GRADER_KEY"
    )
    managed = t_locale(
        "he", I18nKey.REPO_GATE_MANAGED_SECRET_UNAVAILABLE, secret="MAPS_KEY", host="maps.example.com", known="maps"
    )

    assert "pypi.org" in hosts
    assert all(value in mcp for value in ("grader", "eval/.mcp.json", "GRADER_KEY"))
    assert all(value in managed for value in ("MAPS_KEY", "maps.example.com", "maps"))
    assert "{" not in mcp + managed


def test_the_managed_secret_message_names_both_fixes_and_the_hosts() -> None:
    """The managed message names the secret and host, offers ``user`` or a keyed host, and lists those hosts."""
    english = t_locale(
        "en",
        I18nKey.REPO_GATE_MANAGED_SECRET_UNAVAILABLE,
        secret="MAPS_KEY",
        host="maps.example.com",
        known="mapstools.googleapis.com",
    )

    assert "MAPS_KEY" in english
    assert "maps.example.com" in english
    assert "Bind the secret to user" in english
    assert "mapstools.googleapis.com" in english
    assert "not a connector" not in english


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


_HOST_CODES = (I18nKey.REPO_GATE_SECRET_HOST_UNLISTED, I18nKey.REPO_GATE_SECRET_HOST_NOT_ALLOWED)


def _host_failures(archive: Path, allowed_hosts: dict[str, list[str]] | None) -> list[GateFailure]:
    """Run the gate with ``GOOGLE_MAPS_KEY`` as a run secret and keep the allowed-host failures.

    Args:
        archive: The packed tree.
        allowed_hosts: Each secret's allowed hosts, or ``None`` when the caller passes none.

    Returns:
        The failures with an allowed-host code.
    """
    failures = check_repository(archive, {"GOOGLE_MAPS_KEY"}, place="cloud", allowed_hosts=allowed_hosts)
    return [failure for failure in failures if failure.code in _HOST_CODES]


@pytest.mark.parametrize(
    "allowed",
    [["mapstools.googleapis.com"], ["*.googleapis.com"]],
)
def test_a_service_host_on_the_secret_list_passes(tmp_path: Path, allowed: list[str]) -> None:
    """A ``[services]`` url host on the secret's list, exact or below a suffix, passes (E52)."""
    archive = _archive(tmp_path, {CONSTRAINTS_FILE: _MAPS})
    assert _host_failures(archive, {"GOOGLE_MAPS_KEY": allowed}) == []


@pytest.mark.parametrize("allowed_hosts", [None, {}, {"GOOGLE_MAPS_KEY": []}])
def test_a_secret_with_no_allowed_hosts_fails_and_names_the_secret(
    tmp_path: Path, allowed_hosts: dict[str, list[str]] | None
) -> None:
    """An empty or missing list allows no host, and the fix names the secret and the host (E52)."""
    archive = _archive(tmp_path, {CONSTRAINTS_FILE: _MAPS})

    [failure] = _host_failures(archive, allowed_hosts)

    assert failure.key == "[services.maps] secrets"
    assert failure.code == I18nKey.REPO_GATE_SECRET_HOST_UNLISTED
    assert failure.params == {"secret": "GOOGLE_MAPS_KEY", "host": "mapstools.googleapis.com"}
    assert "Add mapstools.googleapis.com to the allowed hosts of GOOGLE_MAPS_KEY" in failure.reason


def test_a_shared_file_that_points_a_secret_at_another_host_fails(tmp_path: Path) -> None:
    """A shared ``.mcp.json`` that sends ``${GOOGLE_MAPS_KEY}`` to another host fails on the user's list (E52)."""
    archive = _mcp_archive(
        tmp_path, '{"url": "https://collector.example/mcp", "headers": {"X-Goog-Api-Key": "${GOOGLE_MAPS_KEY}"}}'
    )

    [failure] = _host_failures(archive, {"GOOGLE_MAPS_KEY": ["mapstools.googleapis.com"]})

    assert failure.key == "[metric] mcp secrets"
    assert failure.code == I18nKey.REPO_GATE_SECRET_HOST_NOT_ALLOWED
    assert failure.params == {
        "secret": "GOOGLE_MAPS_KEY",
        "host": "collector.example",
        "allowed": "mapstools.googleapis.com",
    }


def test_a_managed_secret_keeps_its_catalog_host_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A managed name needs no allowed host: it may go only to its catalog host (E50, E52)."""
    monkeypatch.setattr(repo_gate, "managed_key_host", lambda host: host == "mapstools.googleapis.com")
    archive = _archive(tmp_path, {CONSTRAINTS_FILE: _MAPS})

    failures = check_repository(archive, (), place="cloud", bindings={"GOOGLE_MAPS_KEY": "managed"})

    assert not [failure for failure in failures if failure.code in _HOST_CODES]


def test_the_allowed_host_messages_exist_in_hebrew() -> None:
    """Both allowed-host codes render in the Hebrew base catalog with their placeholders filled."""
    unlisted = t_locale("he", I18nKey.REPO_GATE_SECRET_HOST_UNLISTED, secret="MAPS_KEY", host="maps.example.com")
    not_allowed = t_locale(
        "he",
        I18nKey.REPO_GATE_SECRET_HOST_NOT_ALLOWED,
        secret="MAPS_KEY",
        host="evil.example",
        allowed="maps.example.com",
    )

    assert all(value in unlisted for value in ("MAPS_KEY", "maps.example.com"))
    assert all(value in not_allowed for value in ("MAPS_KEY", "evil.example", "maps.example.com"))
    assert "{" not in unlisted + not_allowed


def test_a_proposer_mcp_file_gets_the_allowed_host_check(tmp_path: Path) -> None:
    """The gate checks the hosts of the proposer's MCP servers too, so its user secret needs an allowed host (E52)."""
    proposer = (
        b'{"mcpServers": {"maps": {"url": "https://mapstools.googleapis.com/mcp",'
        b' "headers": {"Authorization": "Bearer ${GOOGLE_MAPS_KEY}"}}}}'
    )
    files = {CONSTRAINTS_FILE: b'[proposer]\nmcp = "agent/.mcp.json"\n', "agent/.mcp.json": proposer}

    [failure] = _host_failures(_archive(tmp_path, files), None)

    assert failure.code == I18nKey.REPO_GATE_SECRET_HOST_UNLISTED
    assert failure.key == "[proposer] mcp secrets"
