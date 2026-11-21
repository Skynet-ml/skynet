"""Tests for staging a repository target's tree and secrets in the trusted parent."""

from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from core.api.auth import AuthenticatedUser, get_authenticated_user
from core.api.model_catalog import CatalogModel, ModelCatalogResponse
from core.api.routers import wizard_preflight
from core.billing.host_agent import attach_host_agent_token
from core.billing.model_dispatch import ModelHTTPResult
from core.billing.model_gateway import EMBEDDING_ROUTE_KEY, MCP_RELAY_ROUTE_KEY, ROUTE_KEY
from core.billing.openrouter_quotes import resolve_model_slug
from core.billing.operation_pricing import ChargePolicy
from core.billing.protected_credentials import has_exposed_execution_credentials
from core.billing.vercel_usage import PACKAGE_REGISTRY_HOSTS, PROPOSER_REGISTRY_HOSTS
from core.connectors.github_publish import HardeningSummary, PublishedChange, RepoPublishError
from core.connectors.github_repo import RepoFetchError, RepoRateLimitError, RepoSnapshot
from core.exceptions import ServiceError
from core.i18n_keys import I18nKey
from core.models.blackbox import (
    BLACKBOX_ENGINE_AUTORESEARCH,
    BLACKBOX_ENGINE_META_HARNESS,
    BLACKBOX_ENGINE_SHINKA_EVOLVE,
    BLACKBOX_HARNESS_CODEX,
    BLACKBOX_HARNESS_CUSTOM,
    HOST_CLI_AUTO_KEY,
    BlackboxProposer,
)
from core.models.hardening import HardeningExploit, HardeningJob, HardeningJobModel, HardeningRound
from core.sandbox.image_build import BuildResult, ImageBuildError, LocalDockerImageBuilder
from core.sandbox.image_registry import ImageRegistryError
from core.sandbox.services import ComposeService
from core.service_gateway.optimization.blackbox import repo_workspace, runner
from core.service_gateway.optimization.blackbox.harness_bridge import hidden_hashes
from core.service_gateway.optimization.blackbox.protocol import METRIC_ERROR, RUN_FAILED_STATUS
from core.service_gateway.optimization.blackbox.registry import ENGINES
from core.service_gateway.optimization.blackbox.repo_constraints import (
    CONSTRAINTS_REQUIRED,
    ConstraintsError,
    McpServer,
    RoleEnvironment,
    ServiceSpec,
    TargetStage,
)
from core.service_gateway.optimization.blackbox.repo_tree import extract_folder
from core.service_gateway.optimization.blackbox.rule_judge import (
    JUDGE_ANSWER_TOKENS,
    JUDGE_REASONING_TOKENS,
    WATCH_EVERY_ACTIONS,
    WITHHELD_STEP,
)
from core.service_gateway.optimization.blackbox.sandbox import (
    CommandResult,
    LocalSubprocessRuntime,
    LocalSubprocessSession,
    SandboxSpec,
)
from core.service_gateway.optimization.blackbox.service import CASE_SPLIT_KEY, case_split_positions, legacy_case_splits
from core.storage.models import Base, PackageRegistryPreferenceModel
from core.storage.run_call_store import SECRET_MASK, PostgresRunCallStore
from core.worker import repo_staging, scoped_relay
from core.worker.repo_gate import RepoGateError, check_hidden
from core.worker.scoped_relay import McpRelay


@pytest.fixture(autouse=True)
def _local_boxes_run_without_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop the scorer's root flag in a local box, which refuses root on the developer's machine.

    The supervisor then runs as the test's user and starts the entry as that
    user. The Docker tests prove the root supervisor and the user switch (SKY-398).

    Args:
        monkeypatch: Restores the local box's run after the test.
    """
    run = LocalSubprocessSession.run

    def without_root(self: LocalSubprocessSession, command: str, *, sudo: bool = False, **options: Any) -> Any:
        """Run the command as the test's user.

        Args:
            self: The local box.
            command: The shell command.
            sudo: Ignored.
            **options: The run's other arguments.

        Returns:
            The command's result.
        """
        return run(self, command, **options)

    monkeypatch.setattr(LocalSubprocessSession, "run", without_root)


def _payload(commit: str | None) -> dict[str, Any]:
    """Build a repository payload whose secret is still a vault reference.

    Args:
        commit: Pinned commit, or ``None`` to follow the branch.

    Returns:
        The payload.
    """
    secret = {"name": "API_KEY", "credential_ref": "ref-1", "credential_revision": 1}
    repo = {
        "repository": "acme/app",
        "branch": "main",
        "commit": commit,
        "editable_paths": ["src"],
        "secrets": [secret],
    }
    return {"target": {"kind": "repo", "repo": repo}}


def _tree(workdir: Path, files: dict[str, str], links: dict[str, tuple[bytes, str]] | None = None) -> Path:
    """Pack files the way the parent ships a fetched tree.

    Args:
        workdir: Folder the archive goes in.
        files: Root-relative path to text.
        links: Root-relative link path to its tar type (``SYMTYPE`` or
            ``LNKTYPE``) and target, packed after the files.

    Returns:
        The archive.
    """
    archive = workdir / "tree.tgz"
    with tarfile.open(archive, "w:gz") as packed:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            packed.addfile(info, io.BytesIO(data))
        for name, (kind, target) in (links or {}).items():
            info = tarfile.TarInfo(name)
            info.type, info.linkname = kind, target
            packed.addfile(info)
    return archive


def _stage_with(
    files: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any] | None = None,
    links: dict[str, tuple[bytes, str]] | None = None,
    require_constraints: bool = False,
    started: bool = False,
) -> tuple[dict[str, Any], repo_staging.StagedRepository]:
    """Stage the fixture payload over a tree holding ``files``.

    Args:
        files: Root-relative path to text in the fetched tree.
        monkeypatch: Pytest monkeypatch.
        payload: Payload to stage; the fixture payload pinned to ``c * 40`` when omitted.
        links: Links in the fetched tree, as ``_tree`` takes them.
        require_constraints: Stage as a new run that needs an ``ascent.toml``.
        started: Whether the caller's job already started, as ``stage_repository`` takes it.

    Returns:
        The staged payload and repository.
    """
    monkeypatch.setattr(repo_staging, "resolve_repo_secrets", lambda payload, **_: {"API_KEY": "secret-value"})
    monkeypatch.setattr(repo_staging, "github_token", lambda engine, username: "gh-token")
    monkeypatch.setattr(repo_staging, "saved_index_url", lambda engine, username: "https://pypi.org/simple")
    monkeypatch.setattr(
        repo_staging,
        "fetch_snapshot",
        lambda repository, pinned, token, workdir: RepoSnapshot(
            commit=pinned, archive=_tree(workdir, files, links), readonly_paths=(), size_bytes=1
        ),
    )
    return repo_staging.stage_repository(
        payload or _payload("c" * 40),
        username="alice",
        binding_id="b",
        engine=None,
        require_constraints=require_constraints,
        started=started,
    )


_CONSTRAINTS = """
version = 1
instructions = "docs/intent.md"

[paths]
editable = ["src/geocode"]
protected = ["src/geocode/eval"]
"""
# The metric scores only what a [run] or a target stage gives it, so a contract needs one (SKY-761).
_RUN = '\n[run]\nentry = "run.py:main"\n'
_FILES = {"ascent.toml": _CONSTRAINTS + _RUN, "docs/intent.md": "Pick the place the sender means.\n"}


def test_staging_applies_the_repository_constraints_and_records_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The file narrows the editable paths, protects its judge, briefs the agent, and is saved with the run.

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    staged_payload, staged = _stage_with(_FILES, monkeypatch)

    repo = staged_payload["target"]["repo"]
    assert repo["editable_paths"] == ["src/geocode"]
    assert staged.protected_paths == ("ascent.toml", "docs/intent.md", "src/geocode/eval")
    assert staged.intent == "Pick the place the sender means."
    environment = repo["environment"]
    assert environment["commit"] == "c" * 40
    assert environment["editable_paths"] == ["src/geocode"]
    assert environment["protected_paths"] == ["ascent.toml", "docs/intent.md", "src/geocode/eval"]
    assert environment["secret_names"] == ["API_KEY"]
    assert environment["services"] == []
    assert environment["roles"] == {
        "proposer": {"allowed_hosts": list(PROPOSER_REGISTRY_HOSTS), "mcp_servers": [], "services": []},
        "targets": {},
    }
    assert staged_payload[repo_staging.REPO_SNAPSHOT_KEY]["proposer_hosts"] == list(PROPOSER_REGISTRY_HOSTS)
    assert environment["constraints_file"]["path"] == "ascent.toml"
    assert "secret-value" not in repr(staged_payload)
    assert "Pick the place the sender means." in staged_payload["background"]
    assert "`src/geocode/eval`" in staged_payload["background"]
    staged.cleanup()


def test_staging_writes_the_draft_files_over_the_tree_before_it_reads_the_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A draft ascent.toml takes effect, and every box starts from the drafted tree.

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    payload = _payload("c" * 40)
    payload["target"]["repo"]["draft_files"] = {**_FILES, "src/geocode/app.py": "drafted = True\n"}

    staged_payload, staged = _stage_with({"src/geocode/app.py": "x = 1\n"}, monkeypatch, payload)

    assert staged_payload["target"]["repo"]["editable_paths"] == ["src/geocode"]
    assert staged.intent == "Pick the place the sender means."
    with tarfile.open(staged.snapshot.archive) as packed:
        assert packed.extractfile("src/geocode/app.py").read() == b"drafted = True\n"  # type: ignore[union-attr]
    staged.cleanup()


def test_staging_keeps_the_draft_files_off_the_guest_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guest gets the drafts only inside the tree, as a draft ascent.toml names the hidden paths.

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    payload = _payload("c" * 40)
    payload["target"]["repo"]["draft_files"] = {**_FILES, "src/geocode/app.py": "drafted = True\n"}

    staged_payload, staged = _stage_with({"src/geocode/app.py": "x = 1\n"}, monkeypatch, payload)

    assert "draft_files" not in staged_payload["target"]["repo"]
    assert "drafted = True" not in json.dumps(staged_payload)
    assert staged.drafts == {**_FILES, "src/geocode/app.py": "drafted = True\n"}
    assert payload["target"]["repo"]["draft_files"], "staging must not change the stored payload"
    staged.cleanup()


def test_staging_refuses_a_draft_file_through_a_link(monkeypatch: pytest.MonkeyPatch) -> None:
    """A draft that would write through a link in the tree fails setup in plain words.

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    payload = _payload("c" * 40)
    payload["target"]["repo"]["draft_files"] = {"src/app.py": "x = 2\n"}

    with pytest.raises(ConstraintsError, match="link"):
        _stage_with({"a.py": "1"}, monkeypatch, payload, links={"src": (tarfile.SYMTYPE, "/etc")})


def test_staging_refuses_drafts_written_on_another_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drafts read at an older commit would undo the branch's new commits, so the run stops with a code.

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    payload = _payload("c" * 40)
    payload["target"]["repo"]["draft_files"] = {"src/app.py": "x = 2\n"}
    payload["target"]["repo"]["draft_base"] = "d" * 40

    with pytest.raises(RepoGateError) as refused:
        _stage_with({"src/app.py": "x = 1\n"}, monkeypatch, payload)

    assert refused.value.code == I18nKey.REPO_GATE_DRAFT_BASE_MOVED
    assert refused.value.params == {"base": "ddddddd", "commit": "ccccccc"}


@pytest.mark.parametrize("base", ["c" * 40, None], ids=["same-commit", "no-recorded-base"])
def test_staging_keeps_drafts_on_their_own_commit_or_with_no_base(
    monkeypatch: pytest.MonkeyPatch, base: str | None
) -> None:
    """Drafts on the run's commit stage, and older drafts that record no base still stage.

    Args:
        monkeypatch: Pytest monkeypatch.
        base: The draft base the run records, or None for no base.
    """
    payload = _payload("c" * 40)
    payload["target"]["repo"]["draft_files"] = {"src/app.py": "x = 2\n"}
    payload["target"]["repo"]["draft_base"] = base

    staged_payload, staged = _stage_with({"src/app.py": "x = 1\n"}, monkeypatch, payload)

    assert "draft_base" not in staged_payload["target"]["repo"]
    assert staged.drafts == {"src/app.py": "x = 2\n"}
    staged.cleanup()


def test_the_saved_index_is_the_account_setting_or_pypi() -> None:
    """An account with no saved index installs from PyPI; a saved one replaces it."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[PackageRegistryPreferenceModel.__table__])  # type: ignore[list-item]
    with Session(engine) as session:
        session.add(PackageRegistryPreferenceModel(username="bob", index_url="https://pkgs.example.com/simple"))
        session.commit()

    assert repo_staging.saved_index_url(engine, "alice") == "https://pypi.org/simple"
    assert repo_staging.saved_index_url(engine, "bob") == "https://pkgs.example.com/simple"


@pytest.mark.parametrize(
    ("proposer", "index_url", "hosts"),
    [
        ("", "https://pypi.org/simple", list(PROPOSER_REGISTRY_HOSTS)),
        ("\n[proposer]\nnetwork = []\n", "https://pypi.org/simple", []),
        (
            '\n[proposer]\nnetwork = ["docs.python.org"]\n',
            "https://pypi.org/simple",
            sorted({*PROPOSER_REGISTRY_HOSTS, "docs.python.org"}),
        ),
        ("", "https://pkgs.example.com/simple", ["pkgs.example.com", "registry.npmjs.org"]),
    ],
)
def test_staging_records_the_hosts_the_proposer_box_reaches(
    proposer: str, index_url: str, hosts: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The snapshot and the record both carry the proposer's effective hosts, from the saved index (E57).

    Args:
        proposer: The ``[proposer]`` table of the file.
        index_url: The saved package index URL.
        hosts: The hosts the proposer box must reach.
        monkeypatch: Pytest monkeypatch.
    """
    files = {**_FILES, "ascent.toml": _CONSTRAINTS + _RUN + proposer}
    monkeypatch.setattr(repo_staging, "saved_index_url", lambda engine, username: index_url)
    monkeypatch.setattr(repo_staging, "resolve_repo_secrets", lambda payload, **_: {})
    monkeypatch.setattr(repo_staging, "github_token", lambda engine, username: "gh-token")
    monkeypatch.setattr(
        repo_staging,
        "fetch_snapshot",
        lambda repository, pinned, token, workdir: RepoSnapshot(
            commit=pinned, archive=_tree(workdir, files), readonly_paths=(), size_bytes=1
        ),
    )

    staged_payload, staged = repo_staging.stage_repository(
        _payload("c" * 40), username="alice", binding_id="b", engine=None
    )

    assert staged_payload[repo_staging.REPO_SNAPSHOT_KEY]["proposer_hosts"] == hosts
    assert staged_payload["target"]["repo"]["environment"]["roles"]["proposer"]["allowed_hosts"] == hosts
    staged.cleanup()


@pytest.mark.parametrize(
    ("files", "message"),
    [
        (
            {"ascent.toml": 'version = 1\n[run]\nentry = "run.py:main"\n[paths]\neditable = ["tests"]\n'},
            "outside the approved editable paths",
        ),
        (
            {"ascent.toml": 'version = 1\n[run]\nentry = "run.py:main"\n[services]\ngeocoder = ["search"]\n'},
            "cannot grant services",
        ),
    ],
)
def test_staging_refuses_a_file_that_asks_for_more(
    files: dict[str, str], message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wider path or a service request fails setup with the reason and leaves nothing on disk.

    Args:
        files: The files of the repository.
        message: A part of the reason setup must give.
        monkeypatch: Pytest monkeypatch.
    """
    workdirs: list[Path] = []
    real_mkdtemp = repo_staging.tempfile.mkdtemp

    def track(**options: Any) -> str:
        """Remember the scratch folder staging makes.

        Args:
            **options: The scratch folder options.

        Returns:
            The path of the last scratch folder.
        """
        workdirs.append(Path(real_mkdtemp(**options)))
        return str(workdirs[-1])

    monkeypatch.setattr(repo_staging.tempfile, "mkdtemp", track)

    with pytest.raises(ConstraintsError, match=message):
        _stage_with(files, monkeypatch)

    assert workdirs
    assert not workdirs[0].exists()


def test_the_runs_own_proposer_replaces_the_files_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """A request that names a proposer keeps it, and the run record notes the replacement.

    Args:
        monkeypatch: Pytest fixture for the fake fetch.
    """
    files = {"ascent.toml": 'version = 1\n[run]\nentry = "run.py:main"\n[proposer]\nentry = ["node", "agent.js"]\n'}
    payload = {**_payload("c" * 40), "proposer": {"harness": "codex"}}

    staged_payload, staged = _stage_with(files, monkeypatch, payload=payload)
    staged.cleanup()

    assert staged_payload["proposer"] == {"harness": "codex"}
    assert staged_payload["target"]["repo"]["environment"]["notes"] == [
        "The run's own proposer replaces the one in the contract file."
    ]


def test_a_request_without_a_harness_and_a_file_without_a_proposer_runs_the_default_harness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wizard run that picks no harness gets the server's default when the file names no proposer (SKY-753).

    Args:
        monkeypatch: Pytest fixture for the fake fetch.
    """
    files = {"ascent.toml": 'version = 1\n[run]\nentry = "run.py:main"\n'}
    payload = {**_payload("c" * 40), "proposer": {"max_tool_calls": 100}}

    staged_payload, staged = _stage_with(files, monkeypatch, payload=payload)
    staged.cleanup()

    assert staged_payload[repo_staging.REPO_SNAPSHOT_KEY]["file_proposer"] is False
    assert BlackboxProposer.model_validate(staged_payload["proposer"]).harness == BLACKBOX_HARNESS_CODEX


@pytest.mark.parametrize("proposer", [{"run_command": "my-agent"}, {"install_command": "npm ci"}])
def test_a_run_command_or_install_command_fails_the_setup_check(
    proposer: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refuse a request's own run or install command: the engine loop runs only built-in proposers (SKY-728).

    Args:
        proposer: The request's proposer, without a harness.
        monkeypatch: Pytest fixture for the fake fetch.
    """
    files = {"ascent.toml": 'version = 1\n[run]\nentry = "run.py:main"\n[proposer]\nentry = ["node", "agent.js"]\n'}

    with pytest.raises(ConstraintsError, match="built-in proposer") as caught:
        _stage_with(files, monkeypatch, payload={**_payload("c" * 40), "proposer": proposer})

    assert caught.value.key == next(iter(proposer))


# The one engine-loop proposer that reads [proposer] mcp (SKY-728).
_SHINKA_AGENT = {"strategy": {"mode": "single", "engine": BLACKBOX_ENGINE_SHINKA_EVOLVE}, "shinka": {"editor": "agent"}}

_ENTRY_ONLY = {"ascent.toml": 'version = 1\n[run]\nentry = "run.py:main"\n[proposer]\nentry = ["node", "agent.js"]\n'}


@pytest.mark.parametrize(
    "strategy",
    [
        {"mode": "single", "engine": "gepa"},
        {"mode": "single", "engine": "best_of_n"},
        {"mode": "single", "engine": "autosaddler"},
        {"mode": "single", "engine": BLACKBOX_ENGINE_SHINKA_EVOLVE},
        {"mode": "auto"},
    ],
)
def test_a_run_that_cannot_use_a_files_proposer_fails_the_setup_check(
    strategy: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a single AutoResearch or Meta-Harness run on the engine loop runs the file's command (SKY-728).

    Args:
        strategy: A run strategy that cannot use the file's proposer command.
        monkeypatch: Pytest fixture for the fake fetch.
    """
    payload = {**_payload("c" * 40), "strategy": strategy}

    with pytest.raises(ConstraintsError, match=re.escape("Only a single AutoResearch or Meta-Harness run")) as caught:
        _stage_with(_ENTRY_ONLY, monkeypatch, payload=payload)
    assert (caught.value.section, caught.value.key) == ("proposer", "entry")


@pytest.mark.parametrize("engine", [BLACKBOX_ENGINE_AUTORESEARCH, BLACKBOX_ENGINE_META_HARNESS])
def test_a_single_run_on_the_engine_loop_with_a_files_proposer_passes_the_setup_check(
    engine: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine loop runs the file's command for AutoResearch and Meta-Harness, so the check passes (SKY-816).

    Args:
        engine: An engine that runs the file's command.
        monkeypatch: Pytest fixture for the fake fetch.
    """
    payload = {**_payload("c" * 40), "strategy": {"mode": "single", "engine": engine}}

    staged_payload, staged = _stage_with(_ENTRY_ONLY, monkeypatch, payload=payload)
    staged.cleanup()

    assert staged_payload[repo_staging.REPO_SNAPSHOT_KEY]["file_proposer"] is True
    assert staged_payload["proposer"]["run_command"] == "node agent.js"
    assert staged_payload["target"]["repo"]["environment"]["notes"] == [
        "The run uses the proposer command in the contract file: node agent.js",
    ]


def test_a_host_cli_that_local_mode_filled_in_keeps_the_files_proposer(monkeypatch: pytest.MonkeyPatch) -> None:
    """A filled-in host CLI is not the run's own proposer, and the staged proposer drops it (SKY-753).

    Args:
        monkeypatch: Pytest fixture for the fake fetch.
    """
    proposer = BlackboxProposer()
    proposer.host_cli = "codex"
    proposer.model_fields_set.discard("host_cli")
    stored = proposer.model_dump(mode="json")
    assert stored[HOST_CLI_AUTO_KEY] is True
    payload = {
        **_payload("c" * 40),
        "strategy": {"mode": "single", "engine": BLACKBOX_ENGINE_AUTORESEARCH},
        "proposer": stored,
    }

    staged_payload, staged = _stage_with(_ENTRY_ONLY, monkeypatch, payload=payload)
    staged.cleanup()

    assert staged_payload[repo_staging.REPO_SNAPSHOT_KEY]["file_proposer"] is True
    assert staged_payload["proposer"]["harness"] == BLACKBOX_HARNESS_CUSTOM
    assert staged_payload["proposer"]["host_cli"] is None
    assert HOST_CLI_AUTO_KEY not in staged_payload["proposer"]


def test_a_host_cli_the_client_picked_fails_the_setup_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse a host CLI in the request, with no marker: the engine loop runs no host CLI (SKY-753, SKY-728).

    Args:
        monkeypatch: Pytest fixture for the fake fetch.
    """
    payload = {**_payload("c" * 40), "proposer": {"host_cli": "codex"}}

    with pytest.raises(ConstraintsError, match="host_cli") as caught:
        _stage_with(_ENTRY_ONLY, monkeypatch, payload=payload)

    assert (caught.value.section, caught.value.key) == ("proposer", "host_cli")


_RUN_ONLY = 'version = 1\n[run]\nentry = "run.py:main"\n'
_PROPOSER_MCP = {
    "ascent.toml": _RUN_ONLY + '[proposer]\nmcp = "agent.mcp.json"\n',
    "agent.mcp.json": '{"mcpServers": {"web": {"url": "https://web.example.com/mcp"}}}',
}
_PROPOSER_SERVICES = {
    "ascent.toml": _RUN_ONLY + '[proposer]\nservices = ["maps"]\n[services.maps]\nurl = "https://api.example.com/v1"\n'
}
_MODEL_ONLY_REFUSALS = [
    ({"harness": "pi"}, {}, "harness"),
    ({"install_command": "npm ci"}, {}, "install_command"),
    ({"host_cli": "codex"}, {}, "host_cli"),
    ({"max_tool_calls": 5}, {}, "max_tool_calls"),
    ({}, _PROPOSER_MCP, "mcp"),
    ({}, _PROPOSER_SERVICES, "services"),
]


@pytest.mark.parametrize("engine", ["gepa", "best_of_n", "autosaddler", BLACKBOX_ENGINE_SHINKA_EVOLVE])
@pytest.mark.parametrize(("proposer", "files", "setting"), _MODEL_ONLY_REFUSALS)
def test_an_engine_with_a_model_only_proposer_refuses_each_tool_setting_at_staging(
    engine: str, proposer: dict[str, Any], files: dict[str, str], setting: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refuse every proposer setting that a single-call proposer does not run, before paid work (SKY-728).

    Args:
        engine: A single engine whose built-in proposer makes model calls only.
        proposer: The request's proposer settings.
        files: The fetched tree, or the default tree when empty.
        setting: The name that the refusal must give.
        monkeypatch: Pytest fixture for the fake fetch.
    """
    payload = {**_payload("c" * 40), "strategy": {"mode": "single", "engine": engine}, "proposer": proposer}

    with pytest.raises(ConstraintsError, match=f"{ENGINES[engine].label} runs only its built-in proposer") as caught:
        _stage_with(files or {"ascent.toml": _RUN_ONLY}, monkeypatch, payload=payload)

    assert (caught.value.section, caught.value.key) == ("proposer", setting)


@pytest.mark.parametrize("engine", ["gepa", "best_of_n", "autosaddler"])
def test_an_engine_with_a_model_only_proposer_applies_the_reasoning_effort(
    engine: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stage a reasoning effort and the default tool budget, which the loop's model calls apply (SKY-829).

    Args:
        engine: A single engine whose built-in proposer makes model calls only.
        monkeypatch: Pytest fixture for the fake fetch.
    """
    payload = {
        **_payload("c" * 40),
        "strategy": {"mode": "single", "engine": engine},
        "proposer": {"max_tool_calls": BlackboxProposer().max_tool_calls},
        "reflection_model_config": {"name": "vendor/m", "extra": {"reasoning_effort": "high"}},
    }

    _, staged = _stage_with({"ascent.toml": _RUN_ONLY}, monkeypatch, payload=payload)
    staged.cleanup()


def test_the_shinka_agent_editor_refuses_services_but_runs_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stage [proposer] mcp for a ShinkaEvolve agent run, and refuse its [proposer] services (SKY-728).

    Args:
        monkeypatch: Pytest fixture for the fake fetch.
    """
    _, staged = _stage_with(_PROPOSER_MCP, monkeypatch, {**_payload("c" * 40), **_SHINKA_AGENT})
    staged.cleanup()

    with pytest.raises(ConstraintsError, match="ShinkaEvolve runs only its built-in proposer") as caught:
        _stage_with(_PROPOSER_SERVICES, monkeypatch, {**_payload("c" * 40), **_SHINKA_AGENT})

    assert caught.value.key == "services"


_PROPOSER_HEADED_MCP = {
    "ascent.toml": _RUN_ONLY + '[proposer]\nmcp = "agent.mcp.json"\n',
    "agent.mcp.json": '{"mcpServers": {"web": {"url": "https://web.example.com/mcp"},'
    ' "team": {"url": "https://team.example.com/mcp", "headers": {"X-Team": "header-value-123"}}}}',
}


@pytest.mark.parametrize(
    ("strategy", "message"),
    [
        (_SHINKA_AGENT, "^ShinkaEvolve does not support \\[proposer\\] mcp servers with headers yet: team\\."),
        ({"strategy": {"mode": "auto"}}, "^Auto runs only its built-in proposer"),
    ],
)
def test_a_headed_proposer_mcp_server_fails_the_setup_check(
    strategy: dict[str, Any], message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refuse a [proposer] mcp server with headers before paid work, for ShinkaEvolve and Auto (SKY-728, SKY-840).

    Args:
        strategy: The run's strategy and ShinkaEvolve settings.
        message: The start of the refusal.
        monkeypatch: Pytest fixture for the fake fetch.
    """
    with pytest.raises(ConstraintsError, match=message) as caught:
        _stage_with(_PROPOSER_HEADED_MCP, monkeypatch, {**_payload("c" * 40), **strategy})

    assert (caught.value.section, caught.value.key) == ("proposer", "mcp")


@pytest.mark.parametrize("strategy", [{"mode": "single", "engine": "autoresearch"}, {"mode": "auto"}])
def test_a_tool_loop_proposer_runs_its_tool_budget_and_services(
    strategy: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stage a tool budget and [proposer] services for the AutoResearch tool loop, which Auto also runs (SKY-728).

    Args:
        strategy: A run strategy whose proposer is the tool loop.
        monkeypatch: Pytest fixture for the fake fetch.
    """
    payload = {**_payload("c" * 40), "strategy": strategy, "proposer": {"max_tool_calls": 5}}

    _, staged = _stage_with(_PROPOSER_SERVICES, monkeypatch, payload=payload)
    staged.cleanup()

    with pytest.raises(ConstraintsError, match="cannot apply these proposer settings: harness") as caught:
        _stage_with(_PROPOSER_SERVICES, monkeypatch, {**payload, "proposer": {"harness": "pi"}})

    assert caught.value.key == "harness"


_TOOL_LOOP_RUNS = [
    ({"mode": "single", "engine": BLACKBOX_ENGINE_AUTORESEARCH}, "AutoResearch"),
    ({"mode": "single", "engine": BLACKBOX_ENGINE_META_HARNESS}, "Meta-Harness"),
    ({"mode": "auto"}, "Auto"),
]


@pytest.mark.parametrize(("strategy", "label"), _TOOL_LOOP_RUNS)
@pytest.mark.parametrize(
    ("files", "extra", "setting"),
    [(_PROPOSER_MCP, {}, "mcp"), ({"ascent.toml": _RUN_ONLY}, {"reasoning_effort": "high"}, "reasoning_effort")],
)
def test_a_tool_loop_proposer_refuses_mcp_and_a_reasoning_effort_at_staging(
    strategy: dict[str, str],
    label: str,
    files: dict[str, str],
    extra: dict[str, str],
    setting: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refuse [proposer] mcp and a reasoning effort for AutoResearch, Meta-Harness and Auto (SKY-728).

    Args:
        strategy: A run strategy whose proposer is the tool loop.
        label: The engine name that the message gives.
        files: The fetched tree.
        extra: The reflection model's extra settings.
        setting: The name that the refusal must give.
        monkeypatch: Pytest fixture for the fake fetch.
    """
    payload = {
        **_payload("c" * 40),
        "strategy": strategy,
        "reflection_model_config": {"name": "vendor/m", "extra": extra},
    }

    with pytest.raises(ConstraintsError, match=f"^{label} runs only its built-in proposer") as caught:
        _stage_with(files, monkeypatch, payload=payload)

    assert (caught.value.section, caught.value.key) == ("proposer", setting)


@pytest.mark.parametrize(
    ("setting", "default", "choice", "runs_it", "drops_it"),
    [
        ("max_candidates_per_iter", 3, 5, BLACKBOX_ENGINE_META_HARNESS, BLACKBOX_ENGINE_AUTORESEARCH),
        ("ralph", True, False, BLACKBOX_ENGINE_AUTORESEARCH, BLACKBOX_ENGINE_META_HARNESS),
        ("max_no_eval_seconds", 1_800, 600, BLACKBOX_ENGINE_AUTORESEARCH, "gepa"),
    ],
)
def test_an_engine_refuses_a_loop_setting_that_only_another_engine_reads(
    setting: str, default: Any, choice: Any, runs_it: str, drops_it: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refuse a non-default loop setting that the engine drops, and keep the wizard's default (SKY-728).

    Args:
        setting: The proposer setting.
        default: The value that the submit wizard sends.
        choice: Another value.
        runs_it: A single engine that reads the setting.
        drops_it: A single engine that does not read it.
        monkeypatch: Pytest fixture for the fake fetch.
    """
    files = {"ascent.toml": _RUN_ONLY}

    def payload(engine: str | None, value: Any) -> dict[str, Any]:
        """Build a run request with the setting.

        Args:
            engine: The single engine, or ``None`` for Auto.
            value: The setting's value.

        Returns:
            The payload.
        """
        strategy = {"mode": "auto"} if engine is None else {"mode": "single", "engine": engine}
        return {**_payload("c" * 40), "strategy": strategy, "proposer": {setting: value}}

    for engine, value in ((drops_it, default), (runs_it, choice), (None, choice)):
        _, staged = _stage_with(files, monkeypatch, payload=payload(engine, value))
        staged.cleanup()

    with pytest.raises(ConstraintsError, match=f"^{ENGINES[drops_it].label} runs only its built-in proposer") as caught:
        _stage_with(files, monkeypatch, payload=payload(drops_it, choice))

    assert (caught.value.section, caught.value.key) == ("proposer", setting)


def test_the_files_proposer_clears_an_empty_install_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty install command keeps the file's proposer, and staging clears it to ``None`` (SKY-753).

    The guest runs ``[proposer] setup`` in its checkout, so the bootstrap must not get an install step.

    Args:
        monkeypatch: Pytest fixture for the fake fetch.
    """
    payload = {
        **_payload("c" * 40),
        "strategy": {"mode": "single", "engine": BLACKBOX_ENGINE_AUTORESEARCH},
        "proposer": {"install_command": ""},
    }

    staged_payload, staged = _stage_with(_ENTRY_ONLY, monkeypatch, payload=payload)
    staged.cleanup()

    assert staged_payload[repo_staging.REPO_SNAPSHOT_KEY]["file_proposer"] is True
    assert staged_payload["proposer"]["install_command"] is None


def test_a_files_proposer_command_with_mcp_servers_fails_the_setup_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """A custom command cannot load the relay's MCP servers yet, so the setup check names the cause.

    Args:
        monkeypatch: Pytest fixture for the fake fetch.
    """
    files = {
        "ascent.toml": (
            'version = 1\n[run]\nentry = "run.py:main"\n'
            '[proposer]\nentry = ["node", "agent.js"]\nmcp = "agent.mcp.json"\n'
        ),
        "agent.mcp.json": '{"mcpServers": {"web": {"url": "https://web.example.com/mcp"}}}',
    }

    with pytest.raises(ConstraintsError, match=r"custom proposer harness cannot load the \[proposer\] mcp") as caught:
        _stage_with(files, monkeypatch)
    assert (caught.value.section, caught.value.key) == ("proposer", "mcp")


def test_a_resumed_run_with_different_rules_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run resumes only under the rules first recorded for it.

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    first, staged = _stage_with(_FILES, monkeypatch)
    staged.cleanup()
    resumed = _payload("c" * 40)
    resumed["target"]["repo"]["environment"] = first["target"]["repo"]["environment"]

    again, staged = _stage_with(_FILES, monkeypatch, resumed)
    staged.cleanup()
    assert again["target"]["repo"]["environment"]["protected_paths"] == [
        "ascent.toml",
        "docs/intent.md",
        "src/geocode/eval",
    ]
    assert again["_repo_snapshot"]["constrained"] is True

    with pytest.raises(ConstraintsError, match="environment changed since it was recorded"):
        _stage_with({}, monkeypatch, resumed)


_TARGETED = {**_FILES, "ascent.toml": _CONSTRAINTS + _RUN + "\n[outcome]\ntarget = 0.9\n"}


def test_staging_hands_the_outcome_target_to_the_run_and_a_resume_must_keep_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The file's target reaches the run snapshot and the record, and a resume under another target is refused.

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    first, staged = _stage_with(_TARGETED, monkeypatch)
    staged.cleanup()

    assert first[repo_staging.REPO_SNAPSHOT_KEY]["outcome"] == {"target": 0.9, "require": {}, "direction": "maximize"}
    assert first["target"]["repo"]["environment"]["outcome"] == {"target": 0.9, "require": {}, "direction": "maximize"}

    resumed = _payload("c" * 40)
    resumed["target"]["repo"]["environment"] = first["target"]["repo"]["environment"]
    with pytest.raises(ConstraintsError, match=r"\(outcome\)"):
        _stage_with(_FILES, monkeypatch, resumed)


def test_a_run_recorded_before_the_outcome_existed_resumes_without_a_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """An environment saved before the outcome field existed counts as having no target.

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    first, staged = _stage_with(_FILES, monkeypatch)
    staged.cleanup()
    assert first[repo_staging.REPO_SNAPSHOT_KEY]["outcome"] == {"target": None, "require": {}, "direction": "maximize"}
    resumed = _payload("c" * 40)
    resumed["target"]["repo"]["environment"] = {
        key: value for key, value in first["target"]["repo"]["environment"].items() if key != "outcome"
    }

    again, staged = _stage_with(_FILES, monkeypatch, resumed)
    staged.cleanup()
    assert again[repo_staging.REPO_SNAPSHOT_KEY]["outcome"] == {"target": None, "require": {}, "direction": "maximize"}

    with pytest.raises(ConstraintsError, match=r"\(outcome\)"):
        _stage_with(_TARGETED, monkeypatch, resumed)


def test_a_started_run_reads_a_file_with_no_version_as_version_1(monkeypatch: pytest.MonkeyPatch) -> None:
    """A recorded environment means the run started, maybe before ``version`` was required (SKY-690).

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    first, staged = _stage_with(_FILES, monkeypatch)
    staged.cleanup()
    assert staged.require_version is True
    unversioned = {**_FILES, "ascent.toml": (_CONSTRAINTS + _RUN).replace("version = 1\n", "")}
    resumed = _payload("c" * 40)
    resumed["target"]["repo"]["environment"] = first["target"]["repo"]["environment"]

    again, staged = _stage_with(unversioned, monkeypatch, resumed)
    staged.cleanup()

    assert staged.require_version is False
    assert again["target"]["repo"]["editable_paths"] == ["src/geocode"]
    with pytest.raises(ConstraintsError, match="version is missing"):
        _stage_with(unversioned, monkeypatch)


def test_a_started_job_reads_a_file_with_no_version_as_version_1(monkeypatch: pytest.MonkeyPatch) -> None:
    """A job that already started stays lenient on ``version`` with no recorded environment (SKY-690).

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    unversioned = {**_FILES, "ascent.toml": (_CONSTRAINTS + _RUN).replace("version = 1\n", "")}

    staged_payload, staged = _stage_with(unversioned, monkeypatch, started=True)
    staged.cleanup()

    assert staged.require_version is False
    assert staged_payload["target"]["repo"]["editable_paths"] == ["src/geocode"]


def test_a_resumed_run_with_a_contract_with_no_run_and_no_target_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A recorded environment relaxes only ``version``: a resume still needs a [run] or a stage (SKY-761).

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    first, staged = _stage_with(_FILES, monkeypatch)
    staged.cleanup()
    resumed = _payload("c" * 40)
    resumed["target"]["repo"]["environment"] = first["target"]["repo"]["environment"]
    no_run = {**_FILES, "ascent.toml": _CONSTRAINTS.replace("version = 1\n", "")}

    with pytest.raises(ConstraintsError, match=r"Add a \[run\] section or a \[target\.<name>\] stage"):
        _stage_with(no_run, monkeypatch, resumed)


_RULED = {
    **_FILES,
    "ascent.toml": _CONSTRAINTS + _RUN + '\n[proposer]\nillegal = "docs/rules.md"\n',
    "docs/rules.md": "- Do not hardcode answers for dataset cases.\n- Do not call an outside geocoding service.\n",
}


def test_staging_keeps_the_plain_language_rules_for_the_judges(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rules reach the parent's judges and the recorded environment, and a resume must keep them.

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    first, staged = _stage_with(_RULED, monkeypatch)
    staged.cleanup()

    assert staged.constrained is True
    assert staged.rules == (
        "Do not hardcode answers for dataset cases.",
        "Do not call an outside geocoding service.",
    )
    assert first["target"]["repo"]["environment"]["rules"] == list(staged.rules)
    assert "- Rule 2: Do not call an outside geocoding service." in first["background"]

    resumed = _payload("c" * 40)
    resumed["target"]["repo"]["environment"] = first["target"]["repo"]["environment"]
    with pytest.raises(ConstraintsError, match=r"\(protected_paths, rules\)"):
        _stage_with(_FILES, monkeypatch, resumed)


def test_a_run_recorded_before_rules_existed_resumes_without_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    """An environment saved before the rules field existed counts as having no rules.

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    first, staged = _stage_with(_FILES, monkeypatch)
    staged.cleanup()
    resumed = _payload("c" * 40)
    resumed["target"]["repo"]["environment"] = {
        key: value for key, value in first["target"]["repo"]["environment"].items() if key != "rules"
    }

    _, staged = _stage_with(_FILES, monkeypatch, resumed)
    staged.cleanup()
    assert staged.rules == ()

    with pytest.raises(ConstraintsError, match=r"\(protected_paths, rules\)"):
        _stage_with(_RULED, monkeypatch, resumed)


def test_a_tree_without_a_constraints_file_is_not_constrained(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without ascent.toml the run keeps its old behavior: no judge and no fixed block message.

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    _, staged = _stage_with({}, monkeypatch)
    staged.cleanup()

    assert staged.constrained is False
    assert staged.rules == ()


def test_a_new_run_without_a_constraints_file_does_not_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """A new run needs ascent.toml in the tree or the drafts (SKY-693).

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    with pytest.raises(ConstraintsError) as error:
        _stage_with({}, monkeypatch, require_constraints=True)
    assert str(error.value) == CONSTRAINTS_REQUIRED


def test_a_new_run_with_a_draft_or_repository_contract_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    """The contract can come from the repository tree or from the draft files.

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    _, staged = _stage_with(
        {"ascent.toml": 'version = 1\n[run]\nentry = "run.py:main"\n'}, monkeypatch, require_constraints=True
    )
    staged.cleanup()
    drafted = _payload("c" * 40)
    drafted["target"]["repo"]["draft_files"] = {"ascent.toml": 'version = 1\n[run]\nentry = "run.py:main"\n'}
    _, staged = _stage_with({}, monkeypatch, drafted, require_constraints=True)
    staged.cleanup()
    assert staged.constrained is True


def test_a_started_run_without_a_constraints_file_still_resumes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run that recorded its environment before the rule existed resumes and recovers as before.

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    first, staged = _stage_with({}, monkeypatch)
    staged.cleanup()
    resumed = _payload("c" * 40)
    resumed["target"]["repo"]["environment"] = first["target"]["repo"]["environment"]

    _, staged = _stage_with({}, monkeypatch, resumed, require_constraints=True)
    staged.cleanup()
    assert staged.constrained is False


@pytest.mark.parametrize("commit", [None, "b" * 40])
def test_staging_pins_the_commit_and_keeps_secrets_in_the_parent(
    commit: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Follow the branch only when no commit is pinned, and hand the guest secret names but never values.

    Args:
        commit: The pinned commit, or None to follow the branch.
        tmp_path: Scratch folder.
        monkeypatch: Pytest monkeypatch.
    """
    fetched: list[tuple[str, str, str]] = []

    def fetch(repository: str, pinned: str, token: str, workdir: Path) -> RepoSnapshot:
        """Pretend to clone and record what was asked for.

        Args:
            repository: The repository to clone.
            pinned: The pinned commit.
            token: The clone token.
            workdir: The scratch folder.

        Returns:
            A snapshot of an empty tree at the pinned commit.
        """
        fetched.append((repository, pinned, token))
        return RepoSnapshot(commit=pinned, archive=_tree(workdir, {}), readonly_paths=("vendor/lib",), size_bytes=4)

    monkeypatch.setattr(repo_staging, "resolve_repo_secrets", lambda payload, **_: {"API_KEY": "secret-value"})
    monkeypatch.setattr(repo_staging, "github_token", lambda engine, username: "gh-token")
    monkeypatch.setattr(repo_staging, "resolve_commit", lambda token, repository, branch: "a" * 40)
    monkeypatch.setattr(repo_staging, "fetch_snapshot", fetch)
    monkeypatch.setattr(repo_staging, "saved_index_url", lambda engine, username: "https://pypi.org/simple")
    original = _payload(commit)

    staged_payload, staged = repo_staging.stage_repository(original, username="alice", binding_id="b", engine=None)

    expected = commit or "a" * 40
    assert fetched == [("acme/app", expected, "gh-token")]
    assert staged_payload["target"]["repo"]["commit"] == expected
    assert staged_payload["target"]["repo"]["secrets"] == []
    assert staged_payload["_repo_snapshot"]["secret_names"] == ["API_KEY"]
    assert staged_payload["_repo_snapshot"]["readonly_paths"] == ["vendor/lib"]
    assert staged_payload["_repo_snapshot"]["constrained"] is False
    assert staged_payload["_repo_snapshot"]["proposer_hosts"] == list(PROPOSER_REGISTRY_HOSTS)
    assert "secret-value" not in repr(staged_payload)
    assert not has_exposed_execution_credentials(staged_payload)
    assert staged.secrets == {"API_KEY": "secret-value"}
    assert original["target"]["repo"]["secrets"][0]["credential_ref"] == "ref-1"
    staged.cleanup()
    assert not staged.workdir.exists()


def _no_connection(engine: Any, username: str) -> str:
    """Stand in for an owner with no GitHub connection.

    Args:
        engine: Unused database engine.
        username: Unused owner.

    Raises:
        RepoFetchError: Always, as the token lookup does without a connection.
    """
    raise RepoFetchError("Connect GitHub again to read this repository.")


def test_staging_reads_a_public_repository_without_a_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a GitHub connection, resolve the commit and clone anonymously (SKY-568).

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    calls: list[tuple[str, Any]] = []

    def resolve(token: str | None, repository: str, branch: str | None) -> str:
        """Record the lookup token and pin the branch.

        Args:
            token: The lookup token.
            repository: The repository to look up.
            branch: The branch to pin.

        Returns:
            The pinned commit.
        """
        calls.append(("resolve", token))
        return "a" * 40

    def fetch(repository: str, pinned: str, token: str | None, workdir: Path) -> RepoSnapshot:
        """Record the clone token and pack an empty tree.

        Args:
            repository: The repository to clone.
            pinned: The pinned commit.
            token: The clone token.
            workdir: The scratch folder.

        Returns:
            A snapshot of an empty tree at the pinned commit.
        """
        calls.append(("fetch", token))
        return RepoSnapshot(commit=pinned, archive=_tree(workdir, {}), readonly_paths=(), size_bytes=1)

    monkeypatch.setattr(repo_staging, "resolve_repo_secrets", lambda payload, **_: {"API_KEY": "secret-value"})
    monkeypatch.setattr(repo_staging, "github_token", _no_connection)
    monkeypatch.setattr(repo_staging, "resolve_commit", resolve)
    monkeypatch.setattr(repo_staging, "fetch_snapshot", fetch)
    monkeypatch.setattr(repo_staging, "saved_index_url", lambda engine, username: "https://pypi.org/simple")

    staged_payload, staged = repo_staging.stage_repository(
        _payload(None), username="alice", binding_id="b", engine=None
    )

    assert calls == [("resolve", None), ("fetch", None)]
    assert staged_payload["target"]["repo"]["commit"] == "a" * 40
    staged.cleanup()


def test_a_private_repository_without_a_connection_says_to_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    """An anonymous lookup that fails tells the owner to connect GitHub for a private repository.

    Args:
        monkeypatch: Pytest monkeypatch.
    """

    def resolve(token: str | None, repository: str, branch: str | None) -> str:
        """Refuse the lookup the way GitHub hides a private repository.

        Args:
            token: The lookup token.
            repository: The repository to look up.
            branch: The branch to pin.
        """
        raise RepoFetchError("GitHub could not find acme/app at 'main'.")

    monkeypatch.setattr(repo_staging, "resolve_repo_secrets", lambda payload, **_: {})
    monkeypatch.setattr(repo_staging, "github_token", _no_connection)
    monkeypatch.setattr(repo_staging, "resolve_commit", resolve)

    with pytest.raises(RepoFetchError) as raised:
        repo_staging.stage_repository(_payload(None), username="alice", binding_id="b", engine=None)

    message = str(raised.value)
    assert "GitHub could not find acme/app" in message
    assert "Connect GitHub to optimize a private repository." in message


def test_a_rate_limited_anonymous_lookup_keeps_the_rate_limit_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A spent rate limit is not rewritten as advice that only a public repository can be read.

    Args:
        monkeypatch: Pytest monkeypatch.
    """

    def resolve(token: str | None, repository: str, branch: str | None) -> str:
        """Fail the lookup the way a spent anonymous quota does.

        Args:
            token: The lookup token.
            repository: The repository to look up.
            branch: The branch to pin.
        """
        raise RepoRateLimitError("GitHub's rate limit is reached. Try again later. Connect GitHub to raise the limit.")

    monkeypatch.setattr(repo_staging, "resolve_repo_secrets", lambda payload, **_: {})
    monkeypatch.setattr(repo_staging, "github_token", _no_connection)
    monkeypatch.setattr(repo_staging, "resolve_commit", resolve)

    with pytest.raises(RepoRateLimitError) as raised:
        repo_staging.stage_repository(_payload(None), username="alice", binding_id="b", engine=None)

    assert str(raised.value) == "GitHub's rate limit is reached. Try again later. Connect GitHub to raise the limit."


def test_a_failed_anonymous_clone_says_to_connect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pinned commit skips the lookup, so a refused anonymous clone carries the same advice and cleans up.

    Args:
        tmp_path: Scratch folder.
        monkeypatch: Pytest monkeypatch.
    """
    workdirs: list[Path] = []

    def fetch(repository: str, pinned: str, token: str | None, workdir: Path) -> RepoSnapshot:
        """Refuse the clone the way git does for a private repository.

        Args:
            repository: The repository to clone.
            pinned: The pinned commit.
            token: The clone token.
            workdir: The scratch folder.
        """
        workdirs.append(workdir)
        raise RepoFetchError("git fetch failed: could not read Username.")

    monkeypatch.setattr(repo_staging, "resolve_repo_secrets", lambda payload, **_: {})
    monkeypatch.setattr(repo_staging, "github_token", _no_connection)
    monkeypatch.setattr(repo_staging, "fetch_snapshot", fetch)

    with pytest.raises(RepoFetchError, match=r"Connect GitHub to optimize a private repository\."):
        repo_staging.stage_repository(_payload("c" * 40), username="alice", binding_id="b", engine=None)

    assert workdirs
    assert not workdirs[0].exists()


def test_a_failure_with_a_connection_keeps_the_original_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a connection, a refused lookup is not rewritten as advice to connect.

    Args:
        monkeypatch: Pytest monkeypatch.
    """

    def resolve(token: str | None, repository: str, branch: str | None) -> str:
        """Refuse the lookup.

        Args:
            token: The lookup token.
            repository: The repository to look up.
            branch: The branch to pin.
        """
        raise RepoFetchError("GitHub could not find acme/app at 'main'.")

    monkeypatch.setattr(repo_staging, "resolve_repo_secrets", lambda payload, **_: {})
    monkeypatch.setattr(repo_staging, "github_token", lambda engine, username: "gh-token")
    monkeypatch.setattr(repo_staging, "resolve_commit", resolve)

    with pytest.raises(RepoFetchError) as raised:
        repo_staging.stage_repository(_payload(None), username="alice", binding_id="b", engine=None)

    assert str(raised.value) == "GitHub could not find acme/app at 'main'."


def test_staging_uses_the_gitlab_connection_and_falls_back_without_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """A GitLab repository reads through its connection token, or anonymously when there is none.

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    seen: list[str | None] = []

    def resolve(provider: str, token: str | None, repository: str, branch: str | None) -> str:
        """Record the lookup token and pin the branch.

        Args:
            provider: The git provider.
            token: The lookup token.
            repository: The repository to look up.
            branch: The branch to pin.

        Returns:
            The pinned commit.
        """
        seen.append(token)
        return "a" * 40

    def fetch(repository: str, pinned: str, token: str | None, workdir: Path, **_: Any) -> RepoSnapshot:
        """Record the clone token and pack an empty tree.

        Args:
            repository: The repository to clone.
            pinned: The pinned commit.
            token: The clone token.
            workdir: The scratch folder.
            **_: The other fetch options, which the fake ignores.

        Returns:
            A snapshot of an empty tree at the pinned commit.
        """
        seen.append(token)
        return RepoSnapshot(commit=pinned, archive=_tree(workdir, {}), readonly_paths=(), size_bytes=1)

    monkeypatch.setattr(repo_staging, "resolve_repo_secrets", lambda payload, **_: {})
    monkeypatch.setattr(repo_staging.repo_hosts, "resolve_commit", resolve)
    monkeypatch.setattr(repo_staging, "fetch_snapshot", fetch)
    monkeypatch.setattr(repo_staging, "saved_index_url", lambda engine, username: "https://pypi.org/simple")
    payload = _payload(None)
    payload["target"]["repo"]["provider"] = "gitlab"

    def no_gitlab(engine: Any, username: str, provider: str) -> str:
        """Refuse the token lookup as a missing connection does.

        Args:
            engine: The database.
            username: The user whose token to look up.
            provider: The git provider.
        """
        raise RepoFetchError("Connect GitLab again to read this repository.")

    for host_token, expected in ((lambda engine, username, provider: "gl-token", "gl-token"), (no_gitlab, None)):
        seen.clear()
        monkeypatch.setattr(repo_staging.repo_hosts, "host_token", host_token)
        _, staged = repo_staging.stage_repository(payload, username="alice", binding_id="b", engine=None)
        assert seen == [expected, expected]
        staged.cleanup()


def test_staging_keeps_each_secrets_binding_and_hosts_for_the_relay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Staging reads the bindings and allowed hosts before it empties the secret list, and maps each secret to its host.

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    payload = _payload("c" * 40)
    payload["target"]["repo"]["secrets"][0]["allowed_hosts"] = ["api.example.com"]
    payload["target"]["repo"]["secret_bindings"] = {"API_KEY": "user"}
    toml = (
        'version = 1\n[run]\nentry = "run.py:main"\n'
        '[services.maps]\nurl = "https://api.example.com/v1"\nheaders = { Authorization = "Bearer ${API_KEY}" }\n'
    )

    _, staged = _stage_with({"ascent.toml": toml}, monkeypatch, payload)

    assert staged.secret_bindings == {"API_KEY": "user"}
    assert staged.secret_hosts == {"API_KEY": "api.example.com"}
    assert staged.secret_allowed_hosts == {"API_KEY": ("api.example.com",)}
    staged.cleanup()


def test_staging_keeps_each_roles_own_mcp_servers_for_the_relay(monkeypatch: pytest.MonkeyPatch) -> None:
    """[run] keeps the servers of its own MCP file, and [metric] gets none of them (E49, E55).

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    files = {
        "ascent.toml": 'version = 1\n[run]\nentry = "a.py:f"\nmcp = ".mcp.json"\n',
        ".mcp.json": '{"mcpServers": {"search": {"url": "https://search.example.com/mcp"}}}',
    }

    _, staged = _stage_with(files, monkeypatch)

    assert [server.name for server in staged.mcp_roles["run"].mcp_servers] == ["search"]
    assert staged.mcp_roles["metric"].mcp_servers == ()
    staged.cleanup()


def test_the_snapshot_names_the_proposer_mcp_servers_with_their_addresses_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The snapshot gives each [proposer] mcp server's name, address and has-headers flag, no header (SKY-830).

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    files = {
        "ascent.toml": 'version = 1\n[run]\nentry = "a.py:f"\n[proposer]\nmcp = "agent.mcp.json"\n',
        "agent.mcp.json": (
            '{"mcpServers": {"web": {"url": "https://web.example.com/mcp"},'
            ' "team": {"url": "https://team.example.com/mcp", "headers": {"X-Team": "header-value-123"}}}}'
        ),
    }
    # No engine takes a headed [proposer] mcp server until SKY-840, but the staged secrets must stay safe then too.
    monkeypatch.setattr(repo_staging, "_refuse_unsupported_proposer", lambda *_: None)

    result, staged = _stage_with(files, monkeypatch, {**_payload("c" * 40), **_SHINKA_AGENT})

    servers = result[repo_staging.REPO_SNAPSHOT_KEY]["proposer_mcp"]
    assert sorted(servers, key=lambda server: server["name"]) == [
        {"name": "team", "url": "https://team.example.com/mcp", "has_headers": True},
        {"name": "web", "url": "https://web.example.com/mcp", "has_headers": False},
    ]
    assert "header-value-123" not in json.dumps(result)
    staged.cleanup()


def test_staging_gives_each_stage_its_own_mcp_role_and_a_relay_only_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each [target.<name>] keeps its own MCP servers for its box relay, and its file in the tree holds relay urls only.

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    files = {
        "ascent.toml": (
            'version = 1\n[run]\nentry = "a.py:f"\nmcp = ".mcp.json"\n'
            '[target.planner]\nentry = "a.py:f"\nmcp = "agents/planner.mcp.json"\n'
        ),
        ".mcp.json": '{"mcpServers": {"search": {"url": "https://search.example.com/mcp"}}}',
        "agents/planner.mcp.json": (
            '{"mcpServers": {"maps": {"url": "https://maps.example.com/mcp",'
            ' "headers": {"Authorization": "Bearer ${API_KEY}"}}}}'
        ),
        "a.py": "def f(x):\n    return x\n",
    }
    payload = _payload("c" * 40)
    payload["target"]["repo"]["secrets"][0]["allowed_hosts"] = ["maps.example.com"]

    _, staged = _stage_with(files, monkeypatch, payload)

    assert [server.name for server in staged.mcp_roles["target.planner"].mcp_servers] == ["maps"]
    assert staged.mcp_roles["target.planner"].mcp == "agents/planner.mcp.json"
    assert [server.name for server in staged.mcp_roles["run"].mcp_servers] == ["search"]
    assert staged.agent_archive is not None
    with tarfile.open(staged.agent_archive) as packed:
        texts = {
            Path(member.name).as_posix(): packed.extractfile(member).read().decode()  # type: ignore[union-attr]
            for member in packed
            if member.isreg()
        }
    relay = f"http://127.0.0.1:{runner.RELAY_PORT}/mcp"
    assert json.loads(texts["agents/planner.mcp.json"]) == {
        "mcpServers": {"maps": {"type": "http", "url": f"{relay}/maps"}}
    }
    assert "example.com" not in "".join(texts.values())
    assert "secret-value" not in "".join(texts.values())
    staged.cleanup()


def test_the_scorer_tree_holds_only_loopback_urls_in_each_roles_mcp_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The scorer box unpacks a tree whose every MCP file points at the runner's relay (E49, E53).

    Args:
        monkeypatch: Pytest monkeypatch.
        tmp_path: Scratch folder.
    """
    files = {
        "ascent.toml": (
            'version = 1\n[proposer]\nmcp = "agent.mcp.json"\n[run]\nentry = "a.py:f"\nmcp = ".mcp.json"\n'
            '[metric]\nmcp = "tools/metric.mcp.json"\n'
        ),
        "agent.mcp.json": '{"mcpServers": {"web": {"url": "https://web.example.com/mcp"}}}',
        ".mcp.json": '{"mcpServers": {"search": {"url": "https://search.example.com/mcp"}}}',
        "tools/metric.mcp.json": '{"mcpServers": {"docs": {"type": "http", "url": "https://docs.example.com/mcp"}}}',
        "a.py": "def f(x):\n    return x\n",
    }

    _, staged = _stage_with(files, monkeypatch, {**_payload("c" * 40), **_SHINKA_AGENT})

    assert staged.scorer_archive is not None
    with tarfile.open(staged.scorer_archive) as packed:
        texts = {
            Path(member.name).as_posix(): packed.extractfile(member).read().decode()  # type: ignore[union-attr]
            for member in packed
            if member.isreg()
        }
    relay = f"http://127.0.0.1:{runner.RELAY_PORT}/mcp"
    assert json.loads(texts[".mcp.json"]) == {"mcpServers": {"search": {"type": "http", "url": f"{relay}/search"}}}
    assert json.loads(texts["tools/metric.mcp.json"]) == {
        "mcpServers": {"docs": {"type": "http", "url": f"{relay}/docs"}}
    }
    assert json.loads(texts["agent.mcp.json"]) == {"mcpServers": {"web": {"type": "http", "url": f"{relay}/web"}}}
    assert "example.com" not in "".join(texts.values())
    assert texts["a.py"] == files["a.py"]
    with tarfile.open(staged.snapshot.archive) as original:
        assert b"search.example.com" in original.extractfile(".mcp.json").read()  # type: ignore[union-attr]
    staged.cleanup()


def test_failed_fetch_leaves_nothing_on_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove the scratch folder when the clone fails.

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    workdirs: list[Path] = []

    def fetch(repository: str, pinned: str, token: str, workdir: Path) -> RepoSnapshot:
        """Fail like a refused clone.

        Args:
            repository: The repository to clone.
            pinned: The pinned commit.
            token: The clone token.
            workdir: The scratch folder.
        """
        workdirs.append(workdir)
        raise RuntimeError("refused")

    monkeypatch.setattr(repo_staging, "resolve_repo_secrets", lambda payload, **_: {})
    monkeypatch.setattr(repo_staging, "github_token", lambda engine, username: "gh-token")
    monkeypatch.setattr(repo_staging, "fetch_snapshot", fetch)

    with pytest.raises(RuntimeError):
        repo_staging.stage_repository(_payload("c" * 40), username="alice", binding_id="b", engine=None)

    assert workdirs
    assert not workdirs[0].exists()


def test_only_repository_targets_are_staged() -> None:
    """Leave text and agent targets alone."""
    assert repo_staging.is_repo_payload(_payload(None))
    assert not repo_staging.is_repo_payload({"target": {"kind": "agent"}})
    assert not repo_staging.is_repo_payload({})


class _Box:
    """Answer every command, and record whether the box had network when each one ran."""

    def __init__(self, spec: SandboxSpec) -> None:
        """Open over ``spec``.

        Args:
            spec: The box's requested network profile.
        """
        self.spec = spec
        self.online = not spec.network_disabled
        self.commands: list[tuple[str, bool]] = []
        self.closed = False
        self.service_bases: list[str] = []

    def write_files(self, files: dict[str, str]) -> None:
        """Accept files.

        Args:
            files: The files to write.
        """

    def read_file(self, path: str) -> str | None:
        """Hold no files.

        Args:
            path: The file to read.
        """
        return None

    def run(self, command: str, **options: Any) -> CommandResult:
        """Record the command, answering the prepared-tree digest and the working folder as the box would.

        Args:
            command: The command the box runs.
            **options: The run options.

        Returns:
            A successful result, with the output the box would give.
        """
        self.commands.append((command, self.online))
        if "hashlib.sha256" in command:
            return CommandResult(exit_code=0, stdout="0" * 64)
        if command == "pwd":
            return CommandResult(exit_code=0, stdout="/box\n")
        if "pwd -P" in command:
            return CommandResult(exit_code=0, stdout="/box/.app-services\n")
        return CommandResult(exit_code=0)

    def start_services(self, base_dir: str) -> None:
        """Record the start of the compose services.

        Args:
            base_dir: The folder the services run in.
        """
        self.service_bases.append(base_dir)

    def close(self) -> None:
        """Record shutdown."""
        self.closed = True


class _NoCatalogClient:
    """Fail any provider catalog read, since judge ids are full slugs."""

    def get(self, url: str) -> Any:
        """Refuse a network read.

        Args:
            url: Requested address.

        Raises:
            AssertionError: Always.
        """
        raise AssertionError(f"unexpected catalog read: {url}")


class _Gateway:
    """Record the evaluator a repository run binds and the parent box it opens."""

    def __init__(self) -> None:
        """Start with no evaluator or box."""
        self.evaluator: Any = None
        self.boxes: list[_Box] = []
        self.routes: dict[str, dict[str, Any]] = {}
        self.observers: dict[str, list[Any]] = {}
        self.judged: list[tuple[str, str]] = []
        self.bodies: dict[str, dict[str, Any]] = {}
        self.watched = threading.Event()

    def register(
        self, *, model: str, api_key: str, role: str, policy: ChargePolicy, parent_only: bool = False
    ) -> dict[str, str]:
        """Record one model route.

        Args:
            model: Model slug.
            api_key: Provider credential.
            role: Usage role.
            policy: Charge policy.
            parent_only: Whether readiness probes skip the route.

        Returns:
            A fake route whose model is the provider slug, as the real gateway returns it.
        """
        model = resolve_model_slug(model, client=_NoCatalogClient())
        token = f"token-{len(self.routes)}"
        self.routes[token] = {
            "model": model,
            "api_key": api_key,
            "role": role,
            "policy": policy.kind,
            "parent_only": parent_only,
        }
        return {"url": "u", "token": token, "model": model, "role": role}

    def observe_role(self, role: str, observer: Any) -> None:
        """Keep an observer of one role's model calls.

        Args:
            role: Route role.
            observer: Receives each answered request body.
        """
        self.observers.setdefault(role, []).append(observer)

    def dispatch_guest(self, token: str, path: str, body: dict[str, Any], headers: dict[str, str]) -> Any:
        """Answer a watch hook call with an ``ok`` verdict.

        Args:
            token: Route token.
            path: Protocol path.
            body: Chat request.
            headers: Protocol headers.

        Returns:
            A model response.
        """
        self.judged.append((self.routes[token]["model"], body["messages"][0]["content"]))
        self.bodies[self.routes[token]["model"]] = body
        if "<steps>" in body["messages"][0]["content"]:
            self.watched.set()
        verdict = json.dumps({"label": "ok", "confidence": 0.9, "reason": "fine"})
        reply = {"choices": [{"message": {"content": verdict}}]}
        return ModelHTTPResult(200, "application/json", json.dumps(reply).encode())

    def parent_sandbox_runtime(self) -> Any:
        """Return a runtime that opens recording boxes.

        Returns:
            A runtime that opens recording boxes.
        """
        gateway = self

        class _Runtime:
            """Open one recording box per call."""

            max_lifetime_seconds = 3600.0

            def open(self, spec: SandboxSpec) -> _Box:
                """Open a recording box over ``spec``.

                Args:
                    spec: The sandbox spec.

                Returns:
                    The recording box.
                """
                box = _Box(spec)
                gateway.boxes.append(box)
                return box

            def snapshot(self, session: _Box, cache_key: str) -> str:
                """Save a box's state under a fake id, since the fake box holds no files.

                Args:
                    session: The box to save.
                    cache_key: The snapshot cache key.

                Returns:
                    The fake snapshot id.
                """
                return f"snap-{len(gateway.boxes)}"

        return _Runtime()

    def bind_evaluator(self, evaluator: Any) -> dict[str, str]:
        """Keep the evaluator and hand back a fake route.

        Args:
            evaluator: The parent's repository scorer.

        Returns:
            A fake route.
        """
        # Setup ends before the guest exists, so no box is open then (SKY-398).
        assert all(box.closed for box in self.boxes)
        self.evaluator = evaluator
        return {"url": "http://127.0.0.1:1/v1", "token": "evaluator-token"}


def _binding(tmp_path: Path, setup: str | None) -> tuple[dict[str, Any], repo_staging.StagedRepository]:
    """Build a staged repository and the payload that binds its scorer.

    Args:
        tmp_path: Scratch folder.
        setup: The target's setup command.

    Returns:
        The payload and the staged tree.
    """
    archive = _tree(tmp_path, {"src/app.py": "x = 1\n"})
    staged = repo_staging.StagedRepository(
        snapshot=RepoSnapshot(commit="a" * 40, archive=archive, readonly_paths=(), size_bytes=4),
        secrets={"API_KEY": "secret-value"},
        workdir=tmp_path,
    )
    payload = {
        **_payload("a" * 40),
        "scorer": {"kind": "python", "metric_code": "def score(p):\n    return 1\n", "timeout_seconds": 60},
        "_budget_gateway_descriptor": {
            "url": "http://127.0.0.1:1/v1",
            "control_token": "control-token",
            "image": "img",
            "lifetime_seconds": 600,
        },
    }
    payload["target"]["setup_command"] = setup
    payload["target"]["repo"]["environment"] = {"editable_paths": ["src"]}
    return payload, staged


def test_binding_hands_the_guest_a_route_and_closes_the_box(tmp_path: Path) -> None:
    """The guest receives only the evaluator route; the parent owns the scorer and closes it.

    Args:
        tmp_path: Scratch folder.
    """
    payload, staged = _binding(tmp_path, None)
    gateway = _Gateway()

    repo_staging.bind_repo_scorer(payload, staged, gateway, owner_id="job-1")

    assert payload["_skynet_evaluator_route"] == {"url": "http://127.0.0.1:1/v1", "token": "evaluator-token"}
    assert staged.scorers == [gateway.evaluator]
    assert "secret-value" not in repr(payload)
    box = gateway.boxes[0]
    assert box.spec.network_disabled is True
    assert box.spec.allowed_hosts == ()
    assert not any(online for _, online in box.commands)
    staged.close_scorers()
    assert staged.scorers == []
    assert box.closed


def test_binding_starts_the_base_compose_services_next_to_the_scorer(tmp_path: Path) -> None:
    """Each candidate box gets the base commit's ``[sandbox] compose`` services; the setup box starts none.

    Args:
        tmp_path: Scratch folder.
    """
    payload, bare = _binding(tmp_path, None)
    db = ComposeService(name="db", command="postgres -D /data")
    staged = dataclasses.replace(bare, compose_services=(db,))
    gateway = _Gateway()

    repo_staging.bind_repo_scorer(payload, staged, gateway, owner_id="job-1")

    assert gateway.boxes[0].service_bases == []
    gateway.evaluator._workspace.open_candidate("").close()
    candidate = gateway.boxes[-1]
    assert candidate.spec.services == (db,)
    assert candidate.service_bases == ["/box/.app-services"]
    staged.close_scorers()


def test_binding_gives_the_scorer_the_secret_hosts_and_the_run_budget(tmp_path: Path) -> None:
    """The scorer's relay gets the bindings, both host maps, and the gateway's budget to meter a Skynet key.

    Args:
        tmp_path: Scratch folder.
    """
    payload, bare = _binding(tmp_path, None)
    staged = dataclasses.replace(
        bare,
        secret_bindings={"API_KEY": "user"},
        secret_hosts={"API_KEY": "api.example.com"},
        secret_allowed_hosts={"API_KEY": ("api.example.com",)},
    )
    gateway = _Gateway()
    budget = object()
    gateway.runtime = budget  # type: ignore[attr-defined]

    repo_staging.bind_repo_scorer(payload, staged, gateway, owner_id="job-1")

    scorer = gateway.evaluator
    assert scorer._secret_bindings == {"API_KEY": "user"}
    assert scorer._secret_hosts == {"API_KEY": "api.example.com"}
    assert scorer._secret_allowed_hosts == {"API_KEY": ("api.example.com",)}
    assert scorer._budget is budget
    staged.close_scorers()


def test_binding_gives_the_scorer_each_roles_mcp_servers(tmp_path: Path) -> None:
    """The scorer's relay gets the [run] and [metric] roles, so each call reaches only its own MCP file.

    Args:
        tmp_path: Scratch folder.
    """
    payload, bare = _binding(tmp_path, None)
    run = RoleEnvironment(
        mcp=".mcp.json", mcp_servers=(McpServer(name="search", url="https://search.example.com/mcp"),)
    )
    staged = dataclasses.replace(bare, mcp_roles={"run": run, "metric": RoleEnvironment()})
    gateway = _Gateway()

    repo_staging.bind_repo_scorer(payload, staged, gateway, owner_id="job-1")

    assert gateway.evaluator._mcp_roles == {"run": run, "metric": RoleEnvironment()}
    staged.close_scorers()


def test_the_proposer_tree_holds_only_its_relay_urls_and_no_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """The proposer's .mcp.json lists only the [proposer] servers at relay urls: no real url, header or key (SKY-610).

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    files = {
        "ascent.toml": 'version = 1\n[proposer]\nmcp = "agent.mcp.json"\n[run]\nentry = "a.py:f"\nmcp = ".mcp.json"\n',
        "agent.mcp.json": (
            '{"mcpServers": {"web": {"url": "https://web.example.com/mcp",'
            ' "headers": {"Authorization": "Bearer ${API_KEY}"}}}}'
        ),
        ".mcp.json": '{"mcpServers": {"search": {"url": "https://search.example.com/mcp"}}}',
        "a.py": "def f(x):\n    return x\n",
    }
    payload = {**_payload("c" * 40), **_SHINKA_AGENT}
    payload["target"]["repo"]["secrets"][0]["allowed_hosts"] = ["web.example.com"]
    # No engine takes a headed [proposer] mcp server until SKY-840, but the staged secrets must stay safe then too.
    monkeypatch.setattr(repo_staging, "_refuse_unsupported_proposer", lambda *_: None)

    staged_payload, staged = _stage_with(files, monkeypatch, payload)

    assert staged.agent_archive is not None
    assert staged_payload["_repo_snapshot"]["archive"] == str(staged.agent_archive)
    with tarfile.open(staged.agent_archive) as packed:
        texts = {
            Path(member.name).as_posix(): packed.extractfile(member).read().decode()  # type: ignore[union-attr]
            for member in packed
            if member.isreg()
        }
    relay = f"http://127.0.0.1:{runner.RELAY_PORT}/mcp"
    assert json.loads(texts["agent.mcp.json"]) == {"mcpServers": {"web": {"type": "http", "url": f"{relay}/web"}}}
    assert "example.com" not in "".join(texts.values())
    assert "secret-value" not in "".join(texts.values())
    assert "API_KEY" not in texts["agent.mcp.json"]
    assert [server.name for server in staged.proposer_role.mcp_servers] == ["web"]  # type: ignore[union-attr]
    staged.cleanup()


def test_the_proposer_and_scorer_trees_hold_the_same_protected_mcp_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both trees get the same rewrite, so the protected check passes and the rewrite never enters a patch.

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    files = {
        "ascent.toml": 'version = 1\n[proposer]\nmcp = "agent.mcp.json"\n[run]\nentry = "a.py:f"\nmcp = "tools/run.mcp.json"\n',
        "agent.mcp.json": '{"mcpServers": {"web": {"url": "https://web.example.com/mcp"}}}',
        "tools/run.mcp.json": '{"mcpServers": {"docs": {"url": "https://docs.example.com/mcp"}}}',
        "a.py": "def f(x):\n    return x\n",
    }

    _, staged = _stage_with(files, monkeypatch, {**_payload("c" * 40), **_SHINKA_AGENT})

    assert staged.agent_archive is not None
    assert staged.scorer_archive is not None
    assert {"agent.mcp.json", "tools/run.mcp.json"} <= set(staged.protected_paths)
    with tarfile.open(staged.agent_archive) as agent, tarfile.open(staged.scorer_archive) as scorer:
        for name in ("agent.mcp.json", "tools/run.mcp.json"):
            assert agent.extractfile(name).read() == scorer.extractfile(name).read()  # type: ignore[union-attr]
    staged.cleanup()


def test_binding_hands_the_proposer_route_its_own_mcp_relay(tmp_path: Path) -> None:
    """The proposer's route gets a relay token and server names; the relay holds the secrets in the parent (SKY-610).

    Args:
        tmp_path: Scratch folder.
    """
    payload, bare = _binding(tmp_path, None)
    route = {"url": "http://127.0.0.1:1/v1", "token": "proposer-token"}
    payload["reflection_model_config"] = {"name": "vendor/m", "extra": {ROUTE_KEY: route}}
    proposer = RoleEnvironment(
        mcp="agent.mcp.json", mcp_servers=(McpServer(name="web", url="https://web.example.com/mcp"),)
    )
    staged = dataclasses.replace(bare, proposer_role=proposer, secret_hosts={"API_KEY": "web.example.com"})
    gateway = _Gateway()
    relays: list[McpRelay] = []

    def bind_mcp_relay(relay: McpRelay) -> dict[str, str]:
        """Keep the relay and hand back a fake route.

        Args:
            relay: The MCP relay.

        Returns:
            A fake relay route.
        """
        relays.append(relay)
        return {"url": "http://127.0.0.1:1/v1", "token": "mcp-token"}

    gateway.bind_mcp_relay = bind_mcp_relay  # type: ignore[attr-defined]

    repo_staging.bind_repo_scorer(payload, staged, gateway, owner_id="job-1")

    assert route[MCP_RELAY_ROUTE_KEY] == {"token": "mcp-token", "servers": ["web"], "services": []}
    [relay] = relays
    assert relay._label == "[proposer]"
    assert relay._role is proposer
    assert relay._secrets == {"API_KEY": "secret-value"}
    assert relay._secret_hosts == {"API_KEY": "web.example.com"}
    assert "secret-value" not in repr(payload)
    assert "web.example.com" not in repr(payload)
    staged.close_scorers()


def test_binding_hands_the_proposer_route_a_relay_for_its_services(tmp_path: Path) -> None:
    """A proposer that lists services gets a relay token and the names; the relay holds the urls (SKY-612).

    Args:
        tmp_path: Scratch folder.
    """
    payload, bare = _binding(tmp_path, None)
    route = {"url": "http://127.0.0.1:1/v1", "token": "proposer-token"}
    payload["reflection_model_config"] = {"name": "vendor/m", "extra": {ROUTE_KEY: route}}
    services = {"maps": ServiceSpec(secrets=("API_KEY",), url="https://maps.example.com/v1")}
    staged = dataclasses.replace(
        bare,
        proposer_role=RoleEnvironment(services=("maps",)),
        services=services,
        secret_hosts={"API_KEY": "maps.example.com"},
    )
    gateway = _Gateway()
    relays: list[McpRelay] = []

    def bind_mcp_relay(relay: McpRelay) -> dict[str, str]:
        """Keep the relay and hand back a fake route.

        Args:
            relay: The MCP relay.

        Returns:
            A fake relay route.
        """
        relays.append(relay)
        return {"url": "http://127.0.0.1:1/v1", "token": "mcp-token"}

    gateway.bind_mcp_relay = bind_mcp_relay  # type: ignore[attr-defined]

    repo_staging.bind_repo_scorer(payload, staged, gateway, owner_id="job-1")

    assert route[MCP_RELAY_ROUTE_KEY] == {"token": "mcp-token", "servers": [], "services": ["maps"]}
    [relay] = relays
    assert relay._services == services
    assert relay._secrets == {"API_KEY": "secret-value"}
    assert relay._record_secrets() == ("proposer-token", "mcp-token")
    assert "secret-value" not in repr(payload)
    assert "maps.example.com" not in repr(payload)
    staged.close_scorers()


_BOX_TOKENS = {
    "model": "model-route-token-0123456789",
    "host_agent": "host-agent-token-0123456789",
    "embedding": "embedding-token-0123456789",
    "mcp_relay": "mcp-relay-token-0123456789",
    "extra_reflection": "extra-reflection-token-0123456789",
}


@pytest.mark.parametrize("kind", sorted(_BOX_TOKENS))
def test_every_token_the_proposer_box_holds_is_masked_in_its_service_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """Each token on the proposer's route is masked in a service call that carries it (SKY-640).

    The host agent token joins the route after binding, as the worker attaches it.

    Args:
        tmp_path: Scratch folder.
        monkeypatch: Pytest monkeypatch.
        kind: The kind of token on the proposer's route.
    """
    # Pin the service host to a closed loopback port, so the call fails after the relay sends it.
    monkeypatch.setattr(scoped_relay, "_pinned_address", lambda url, allow_private, *, label: "127.0.0.1")
    payload, bare = _binding(tmp_path, None)
    route = {
        "url": "http://127.0.0.1:1/v1",
        "token": _BOX_TOKENS["model"],
        EMBEDDING_ROUTE_KEY: {"url": "http://127.0.0.1:1/v1", "token": _BOX_TOKENS["embedding"]},
    }
    payload["reflection_model_config"] = {"name": "vendor/m", "extra": {ROUTE_KEY: route}}
    # A ShinkaEvolve box gets each extra reflection model's token as APP_SHINKA_KEY_<n>.
    payload["extra_reflection_model_configs"] = [
        {"name": "vendor/extra", "extra": {ROUTE_KEY: {"url": route["url"], "token": _BOX_TOKENS["extra_reflection"]}}}
    ]
    staged = dataclasses.replace(
        bare,
        proposer_role=RoleEnvironment(services=("maps",)),
        services={"maps": ServiceSpec(url="http://maps.example.com:1/v1")},
    )
    gateway = _Gateway()
    relays: list[McpRelay] = []

    def bind_mcp_relay(relay: McpRelay) -> dict[str, str]:
        """Keep the relay and hand back its route.

        Args:
            relay: The MCP relay.

        Returns:
            The relay route.
        """
        relays.append(relay)
        return {"url": "http://127.0.0.1:1/v1", "token": _BOX_TOKENS["mcp_relay"]}

    gateway.bind_mcp_relay = bind_mcp_relay  # type: ignore[attr-defined]
    repo_staging.bind_repo_scorer(payload, staged, gateway, owner_id="job-1")
    attach_host_agent_token(payload, _BOX_TOKENS["host_agent"])
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    store = PostgresRunCallStore(engine)
    token = _BOX_TOKENS[kind]
    body = json.dumps({"query": token}).encode()

    answer = relays[0].service(
        "maps",
        "",
        "POST",
        body,
        {"content-type": "application/json"},
        scoped_relay.CallRecorder(store, "opt-1", "", ""),
    )

    assert answer.status == 502
    [stored] = store.list_calls("opt-1", scoped_relay.NO_CANDIDATE_ID)
    for field in ("target", "request", "response", "failure_reason"):
        assert token not in (stored[field] or ""), field
    assert SECRET_MASK in stored["request"]
    staged.close_scorers()


def test_binding_refuses_proposer_services_without_a_model_route(tmp_path: Path) -> None:
    """Services with no proposer model route have no box to reach them, so the run stops before it starts.

    Args:
        tmp_path: Scratch folder.
    """
    payload, bare = _binding(tmp_path, None)
    staged = dataclasses.replace(bare, proposer_role=RoleEnvironment(services=("maps",)))

    with pytest.raises(ServiceError, match="need the proposer's model route"):
        repo_staging.bind_repo_scorer(payload, staged, _Gateway(), owner_id="job-1")
    staged.close_scorers()


def test_binding_without_proposer_mcp_servers_adds_no_relay(tmp_path: Path) -> None:
    """A proposer with no MCP servers gets no relay token.

    Args:
        tmp_path: Scratch folder.
    """
    payload, bare = _binding(tmp_path, None)
    route = {"url": "http://127.0.0.1:1/v1", "token": "proposer-token"}
    payload["reflection_model_config"] = {"name": "vendor/m", "extra": {ROUTE_KEY: route}}
    staged = dataclasses.replace(bare, proposer_role=RoleEnvironment())

    repo_staging.bind_repo_scorer(payload, staged, _Gateway(), owner_id="job-1")

    assert MCP_RELAY_ROUTE_KEY not in route
    staged.close_scorers()


def test_binding_opens_the_scorer_box_on_the_loopback_tree(tmp_path: Path) -> None:
    """The scorer box unpacks the tree with rewritten MCP files, never the fetched one.

    Args:
        tmp_path: Scratch folder.
    """
    payload, bare = _binding(tmp_path, None)
    (tmp_path / "scorer").mkdir()
    scorer_tree = _tree(tmp_path / "scorer", {"src/app.py": "x = 1\n"})
    staged = dataclasses.replace(bare, scorer_archive=scorer_tree)
    gateway = _Gateway()

    repo_staging.bind_repo_scorer(payload, staged, gateway, owner_id="job-1")

    assert gateway.evaluator._workspace._archive == scorer_tree
    staged.close_scorers()


def test_binding_gives_the_box_only_the_scorer_models_scoped_route(tmp_path: Path) -> None:
    """The scorer's model reaches the box as its scoped route; the box stays offline.

    Args:
        tmp_path: Scratch folder.
    """
    payload, staged = _binding(tmp_path, None)
    route = {"url": "http://127.0.0.1:1/v1", "token": "route-token", "model": "vendor/judge", "role": "judge"}
    payload["scorer"]["model"] = {"name": "vendor/judge", "token_source": "byok", "extra": {ROUTE_KEY: route}}
    gateway = _Gateway()

    repo_staging.bind_repo_scorer(payload, staged, gateway, owner_id="job-1")

    bound = gateway.evaluator._gateway
    assert (bound.url, bound.model, bound.api_key, bound.protected) == (
        route["url"],
        "vendor/judge",
        "route-token",
        True,
    )
    assert gateway.evaluator._model_env() == {"ASCENT_MODEL_KEY": "route-token", "ASCENT_MODEL": "vendor/judge"}
    box = gateway.boxes[0]
    assert box.spec.network_disabled is True
    assert box.spec.allowed_hosts == ()
    assert payload["target"]["repo"]["environment"]["network"] == {"setup": [], "versions": [], "metric": []}
    staged.close_scorers()


def test_binding_refuses_a_scorer_model_without_a_scoped_route(tmp_path: Path) -> None:
    """A model the parent did not scope would carry a provider key, so no box opens.

    Args:
        tmp_path: Scratch folder.
    """
    payload, staged = _binding(tmp_path, None)
    payload["scorer"]["model"] = {"name": "vendor/judge", "token_source": "byok", "extra": {"api_key": "user-key"}}
    gateway = _Gateway()

    with pytest.raises(ServiceError, match="scoped route"):
        repo_staging.bind_repo_scorer(payload, staged, gateway, owner_id="job-1")
    assert gateway.boxes == []


def _ruled_binding(
    tmp_path: Path, rules: tuple[str, ...], *, intent: str | None = None, watch: bool = False
) -> tuple[dict[str, Any], repo_staging.StagedRepository]:
    """Build a staged repository whose ascent.toml has the given plain-language rules.

    Args:
        tmp_path: Scratch folder.
        rules: The file's rules.
        intent: The file's stated intent.
        watch: Whether the run turns the watch hook on.

    Returns:
        The payload and the staged tree.
    """
    payload, staged = _binding(tmp_path, None)
    if watch:
        payload[repo_staging.RULE_WATCH_KEY] = {"enabled": True}
    return payload, repo_staging.StagedRepository(
        snapshot=staged.snapshot,
        secrets=staged.secrets,
        workdir=staged.workdir,
        constrained=True,
        rules=rules,
        intent=intent,
    )


def _agent_call(command: str, result: str = "ok") -> dict[str, Any]:
    """Build an agent model call that ends with one shell tool call and its result.

    Args:
        command: The shell command.
        result: The tool result text.

    Returns:
        The request body.
    """
    return {
        "messages": [
            {"role": "user", "content": "This is round 2 (explore)."},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "shell", "arguments": json.dumps({"command": command})}}],
            },
            {"role": "tool", "content": result},
        ]
    }


def _trigger_watch(gateway: Any) -> None:
    """Send enough agent model calls to start one watch, and wait for it.

    Args:
        gateway: The recording gateway.
    """
    (observer,) = gateway.observers["optimization"]
    for index in range(WATCH_EVERY_ACTIONS):
        observer(_agent_call(f"edit {index}"))
    assert gateway.watched.wait(5)


def test_a_run_that_turns_the_watch_on_binds_one_managed_hook_billed_to_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The watch hook runs on the managed key under the run's budget, never on a guest route, and never sees the metric.

    Args:
        tmp_path: Scratch folder.
        monkeypatch: Pytest monkeypatch.
    """
    monkeypatch.setattr(repo_staging, "agent_model_id", lambda configured="": "vendor/fast")
    payload, staged = _ruled_binding(tmp_path, ("Do not hardcode answers.",), intent="Find places.", watch=True)
    gateway = _Gateway()

    repo_staging.bind_repo_scorer(payload, staged, gateway, owner_id="job-1", managed_key="managed-key")

    assert [tuple(route.values()) for route in gateway.routes.values()] == [
        ("vendor/fast", "managed-key", "judge", "managed_model", True),
    ]
    assert "managed-key" not in repr(payload)
    judge = gateway.evaluator._rule_judge
    assert judge.rules == ("Do not hardcode answers.",)
    assert judge.watching
    assert gateway.judged == []

    _trigger_watch(gateway)

    (model, prompt) = gateway.judged[0]
    assert model == "vendor/fast"
    assert "Rule 1: Do not hardcode answers." in prompt
    assert "Find places." in prompt
    assert "edit 14" in prompt
    assert payload["scorer"]["metric_code"] not in prompt
    staged.close_scorers()


def test_the_watch_never_reads_a_score_or_the_scorers_feedback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Engine files and tool results carry the scores and feedback, so the hook reads only safe tool inputs.

    Args:
        tmp_path: Scratch folder.
        monkeypatch: Pytest monkeypatch.
    """
    monkeypatch.setattr(repo_staging, "agent_model_id", lambda configured="": "vendor/fast")
    payload, staged = _ruled_binding(tmp_path, ("Do not hardcode answers.",), intent="Find places.", watch=True)
    gateway = _Gateway()
    repo_staging.bind_repo_scorer(payload, staged, gateway, owner_id="job-1", managed_key="managed-key")
    (observer,) = gateway.observers["optimization"]
    reply = json.dumps({"score": 0.4217, "info": {"feedback": "FEEDBACK-SECRET: case 3 names the wrong city"}})
    state = "## Leaderboard\n\n| c001 | 0.4217 |\n\n## Failure dossier\n\n- `ex7`: FEEDBACK-SECRET wrong city\n"

    for index in range(WATCH_EVERY_ACTIONS - 3):
        observer(_agent_call(f"edit {index}"))
    observer(_agent_call("cat STATE.md", state))
    observer(_agent_call("./eval.sh case-3", reply))
    observer(_agent_call("jq -r .info.feedback last.json", "FEEDBACK-SECRET: case 3 names the wrong city"))
    assert gateway.watched.wait(5)

    (_model, prompt) = gateway.judged[0]
    assert "edit 11" in prompt
    assert WITHHELD_STEP in prompt
    assert "jq -r .info.feedback last.json" in prompt
    assert "0.4217" not in prompt
    assert "FEEDBACK-SECRET" not in prompt
    assert "eval.sh" not in prompt
    staged.close_scorers()


def test_the_watch_is_off_by_default_and_costs_nothing(tmp_path: Path) -> None:
    """Rules alone register no model and observe no agent call unless the run turns the watch on.

    Args:
        tmp_path: Scratch folder.
    """
    payload, staged = _ruled_binding(tmp_path, ("Do not hardcode answers.",), intent="Find places.")
    gateway = _Gateway()

    repo_staging.bind_repo_scorer(payload, staged, gateway, owner_id="job-1", managed_key="managed-key")

    assert gateway.routes == {}
    assert gateway.observers == {}
    judge = gateway.evaluator._rule_judge
    assert judge.rules == ("Do not hardcode answers.",)
    assert not judge.watching
    staged.close_scorers()


def test_the_watch_runs_on_an_intent_without_rules(tmp_path: Path) -> None:
    """A stated intent is enough for the watch hook to have something to check.

    Args:
        tmp_path: Scratch folder.
    """
    payload, staged = _ruled_binding(tmp_path, (), intent="Find places.", watch=True)
    gateway = _Gateway()

    repo_staging.bind_repo_scorer(payload, staged, gateway, owner_id="job-1", managed_key="managed-key")

    assert len(gateway.routes) == 1
    assert gateway.evaluator._rule_judge.watching
    staged.close_scorers()


def test_the_watch_setting_falls_back_to_the_server_defaults() -> None:
    """A run without the setting gets the hook off, the configured model and the 0.7 threshold."""
    assert repo_staging._rule_watch_setting({}) == (False, repo_staging.settings.rule_watch_model, 0.7)
    assert repo_staging._rule_watch_setting({repo_staging.RULE_WATCH_KEY: None})[0] is False


def test_the_watch_setting_takes_the_runs_values() -> None:
    """A run can turn the hook on and choose its model and threshold."""
    setting = {"enabled": True, "model": "vendor/watch", "threshold": 0.9}

    assert repo_staging._rule_watch_setting({repo_staging.RULE_WATCH_KEY: setting}) == (True, "vendor/watch", 0.9)


@pytest.mark.parametrize(
    "setting",
    [
        True,
        "on",
        False,
        0,
        [],
        "",
        {"enabled": "yes"},
        {"enabled": True, "model": ""},
        {"enabled": True, "model": 0},
        {"enabled": True, "model": 3},
        {"enabled": True, "threshold": 1.5},
        {"enabled": True, "threshold": -0.1},
        {"enabled": True, "threshold": True},
        {"enabled": True, "threshold": "high"},
    ],
)
def test_a_bad_watch_setting_fails_setup(setting: Any) -> None:
    """A malformed setting stops the run instead of silently turning the hook off.

    Args:
        setting: A malformed watch setting.
    """
    with pytest.raises(ConstraintsError, match="rule_watch"):
        repo_staging._rule_watch_setting({repo_staging.RULE_WATCH_KEY: setting})


def _catalog(*models: CatalogModel) -> ModelCatalogResponse:
    """Build a catalog snapshot holding the given models.

    Args:
        *models: Catalog rows.

    Returns:
        The snapshot.
    """
    return ModelCatalogResponse(providers=[], models=list(models))


def _row(value: str, **reasoning: Any) -> CatalogModel:
    """Build one catalog row with the given reasoning fields.

    Args:
        value: Model id.
        **reasoning: Reasoning fields of the row.

    Returns:
        The row.
    """
    return CatalogModel(value=value, label=value, provider="openrouter", **reasoning)


def _judge_body(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, catalog: Any, *, prefix: str = "") -> dict[str, Any]:
    """Bind a run with the watch on, start one watch, and return the hook's request body.

    Args:
        tmp_path: Scratch folder.
        monkeypatch: Pytest monkeypatch.
        catalog: The catalog snapshot the hook's limits are built from.
        prefix: Provider prefix on the configured hook model id.

    Returns:
        The watch hook's request body.
    """
    monkeypatch.setattr(repo_staging, "agent_model_id", lambda configured="": f"{prefix}vendor/fast")
    monkeypatch.setattr(repo_staging, "catalog_snapshot", lambda: catalog)
    payload, staged = _ruled_binding(tmp_path, ("Do not hardcode answers.",), watch=True)
    gateway = _Gateway()
    repo_staging.bind_repo_scorer(payload, staged, gateway, owner_id="job-1", managed_key="managed-key")
    _trigger_watch(gateway)
    staged.close_scorers()
    return gateway.bodies["vendor/fast"]


@pytest.mark.parametrize("prefix", ["", "openrouter/"])
def test_a_reasoning_hook_gets_low_effort_and_room_to_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prefix: str
) -> None:
    """A hook model that thinks by default must not spend its whole answer budget on reasoning (OpenRouter shares it).

    Catalog ids keep the ``openrouter/`` prefix that the gateway strips from route models.

    Args:
        tmp_path: Scratch folder.
        monkeypatch: Pytest monkeypatch.
        prefix: The route prefix of the hook model.
    """
    catalog = _catalog(
        _row(
            f"{prefix}vendor/fast",
            reasoning_mandatory=True,
            reasoning_default_enabled=True,
            reasoning_efforts=["minimal", "low", "medium", "high"],
            default_reasoning_effort="high",
        ),
    )

    body = _judge_body(tmp_path, monkeypatch, catalog, prefix=prefix)

    assert body["reasoning"] == {"effort": "low"}
    assert body["max_tokens"] == JUDGE_ANSWER_TOKENS + JUDGE_REASONING_TOKENS
    assert JUDGE_REASONING_TOKENS >= 4 * JUDGE_ANSWER_TOKENS


def test_a_hook_that_never_reasons_gets_only_the_answer_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A model the catalog marks as non-reasoning gets no reasoning field and the short limit.

    Args:
        tmp_path: Scratch folder.
        monkeypatch: Pytest monkeypatch.
    """
    catalog = _catalog(
        _row("vendor/fast", reasoning_efforts=[], reasoning_default_enabled=False, reasoning_mandatory=False)
    )

    body = _judge_body(tmp_path, monkeypatch, catalog)

    assert "reasoning" not in body
    assert body["max_tokens"] == JUDGE_ANSWER_TOKENS


def test_a_hook_missing_from_the_catalog_still_has_room_to_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before the catalog loads nobody knows whether the hook reasons, so it gets the larger budget.

    Args:
        tmp_path: Scratch folder.
        monkeypatch: Pytest monkeypatch.
    """
    body = _judge_body(tmp_path, monkeypatch, None)

    assert "reasoning" not in body
    assert body["max_tokens"] == JUDGE_ANSWER_TOKENS + JUDGE_REASONING_TOKENS


def test_a_hook_with_unknown_reasoning_data_still_has_room_to_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A catalog row whose provider did not say whether it reasons is unknown, not a model that never reasons.

    Args:
        tmp_path: Scratch folder.
        monkeypatch: Pytest monkeypatch.
    """
    unknown = {"reasoning_efforts": None, "reasoning_default_enabled": None, "reasoning_mandatory": None}

    body = _judge_body(tmp_path, monkeypatch, _catalog(_row("vendor/fast", **unknown)))

    assert "reasoning" not in body
    assert body["max_tokens"] == JUDGE_ANSWER_TOKENS + JUDGE_REASONING_TOKENS


def test_a_watch_without_the_managed_key_fails_setup(tmp_path: Path) -> None:
    """A watch the run asked for but nobody can run stops the run before the guest starts.

    Args:
        tmp_path: Scratch folder.
    """
    payload, staged = _ruled_binding(tmp_path, ("Do not hardcode answers.",), watch=True)
    gateway = _Gateway()

    with pytest.raises(ConstraintsError, match="rule watch"):
        repo_staging.bind_repo_scorer(payload, staged, gateway, owner_id="job-1", managed_key="")

    assert gateway.boxes == []
    assert "_skynet_evaluator_route" not in payload


def test_a_constraints_file_without_rules_blocks_without_judge_models(tmp_path: Path) -> None:
    """Path rules alone still get the fixed block message, and no hook model is registered or paid for.

    Args:
        tmp_path: Scratch folder.
    """
    payload, staged = _ruled_binding(tmp_path, (), watch=True)
    gateway = _Gateway()

    repo_staging.bind_repo_scorer(payload, staged, gateway, owner_id="job-1")

    assert gateway.routes == {}
    assert gateway.observers == {}
    assert gateway.evaluator._rule_judge.rules == ()
    staged.close_scorers()


def test_a_run_without_a_constraints_file_has_no_rule_judge(tmp_path: Path) -> None:
    """Without ascent.toml the scorer keeps its earlier refusals and no judge exists.

    Args:
        tmp_path: Scratch folder.
    """
    payload, staged = _binding(tmp_path, None)
    gateway = _Gateway()

    repo_staging.bind_repo_scorer(payload, staged, gateway, owner_id="job-1", managed_key="managed-key")

    assert gateway.evaluator._rule_judge is None
    assert gateway.routes == {}
    staged.close_scorers()


def test_setup_runs_online_in_the_parent_before_the_guest_gets_a_route(tmp_path: Path) -> None:
    """A setup command opens the box to package registries only, and it is offline before binding finishes.

    Args:
        tmp_path: Scratch folder.
    """
    payload, staged = _binding(tmp_path, "pip install -r requirements.txt")
    gateway = _Gateway()

    repo_staging.bind_repo_scorer(payload, staged, gateway, owner_id="job-1")

    box = gateway.boxes[0]
    assert box.spec.allowed_hosts == PACKAGE_REGISTRY_HOSTS
    assert box.spec.network_disabled is False
    assert box.spec.env == {}
    assert box.closed
    setup = [online for command, online in box.commands if "pip install" in command]
    assert setup == [True]
    assert payload["target"]["repo"]["environment"]["network"] == {
        "setup": list(PACKAGE_REGISTRY_HOSTS),
        "versions": [],
        "metric": [],
    }
    assert payload["target"]["repo"]["environment"]["setup_command"] == "pip install -r requirements.txt"
    staged.close_scorers()


def _evaluation_check(monkeypatch: pytest.MonkeyPatch, scorer: dict[str, Any]) -> tuple[Any, list[dict[str, Any]]]:
    """Send an evaluation-scope repository setup check and keep the payload it would run.

    Args:
        monkeypatch: Replaces the paid setup run with a recorder.
        scorer: The scorer the client sends.

    Returns:
        The HTTP response and the payloads that reached the setup run.
    """
    seen: list[dict[str, Any]] = []

    def run(request: Any, user: Any, store: Any) -> Any:
        """Keep the canonical payload and stop before any paid work.

        Args:
            request: The setup request.
            user: The signed-in user.
            store: The run store.
        """
        seen.append(request.payload)
        raise HTTPException(status_code=418, detail="recorded")

    monkeypatch.setattr(wizard_preflight, "run_preflight", run)
    monkeypatch.setattr(wizard_preflight, "enforce_submission_rate", lambda username: None)
    app = FastAPI()
    app.include_router(wizard_preflight.create_wizard_preflight_router(job_store=object()))
    app.dependency_overrides[get_authenticated_user] = lambda: AuthenticatedUser("alice", "user", ())
    with TestClient(app) as client:
        response = client.post(
            "/wizard/preflight",
            json={
                "workflow": "anything",
                "scope": "evaluation",
                "payload": {**_payload("a" * 40), "scorer": scorer},
                "execution_budget_id": "draft",
                "execution_budget_revision": 1,
            },
        )
    return response, seen


def test_evaluation_check_without_a_scorer_timeout_binds_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scorer sent without ``timeout_seconds`` gets the run request's 60 s default and binds without a KeyError.

    Args:
        tmp_path: Scratch folder.
        monkeypatch: Pytest monkeypatch.
    """
    response, seen = _evaluation_check(monkeypatch, {"kind": "python", "metric_code": "def score(p):\n    return 1\n"})

    assert response.status_code == 418
    payload, staged = _binding(tmp_path, None)
    payload["scorer"] = seen[0]["scorer"]
    gateway = _Gateway()

    repo_staging.bind_repo_scorer(payload, staged, gateway, owner_id="preflight-1")

    assert gateway.evaluator._timeout_seconds == 60.0
    staged.close_scorers()


def test_evaluation_check_rejects_an_out_of_range_scorer_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """An out-of-range ``timeout_seconds`` is a validation error naming the field, and no setup run starts.

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    response, seen = _evaluation_check(
        monkeypatch, {"kind": "python", "metric_code": "def score(p):\n    return 1\n", "timeout_seconds": 601}
    )

    assert response.status_code == 422
    assert [error["loc"][-2:] for error in response.json()["detail"]] == [["scorer", "timeout_seconds"]]
    assert seen == []


_EDIT = "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"


def _result(baseline: float | None, optimized: float | None, best: str = _EDIT, **extra: Any) -> dict[str, Any]:
    """Build a finished repository result.

    Args:
        baseline: Held-out score of the starting code.
        optimized: Held-out score of the best version.
        best: The best version's patch.
        **extra: Fields to add or override.

    Returns:
        The result.
    """
    return {
        "engine_used": "autoresearch",
        "seed_candidate": "",
        "best_candidate": best,
        "baseline_score": baseline,
        "best_score": optimized,
        "versions": [],
        "details": {},
        **extra,
    }


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (_result(0.5, 0.8), _EDIT),
        (_result(0.5, 0.5), None),
        (_result(0.5, 0.8, best=""), None),
        (_result(0.5, 0.5, regression_guard_applied=True), None),
        (
            _result(None, None, versions=[{"candidate": "", "score": 0.2}, {"candidate": _EDIT, "score": 0.4}]),
            _EDIT,
        ),
        (_result(None, None), None),
    ],
)
def test_only_a_version_that_beat_the_start_is_published(result: dict[str, Any], expected: str | None) -> None:
    """Held-out scores decide, version scores stand in without a hold-out, and ties never publish.

    Args:
        result: The run result.
        expected: The patch that must be published, or None.
    """
    assert repo_staging.improved_patch(result) == expected


_STALE_ILLEGAL_EDIT = {
    "candidate": _EDIT,
    "score": 0.4,
    "side_info": {"illegal_approach": {"rule": "r1", "reason": "x"}},
}


def test_an_old_judge_rejection_no_longer_blocks_publishing() -> None:
    """The final judge is gone (SKY-349): a stale rejection key in side info is ignored like any other."""
    result = _result(None, None, versions=[{"candidate": "", "score": 0.2}, _STALE_ILLEGAL_EDIT])

    assert repo_staging.improved_patch(result) == _EDIT


_CRASHED_EDIT = {
    "candidate": _EDIT,
    "score": None,
    "side_info": {"error": "KeyError: 'answer'", "status": METRIC_ERROR},
}


@pytest.mark.parametrize(
    "result",
    [
        _result(None, None, versions=[{"candidate": "", "score": -3.2}, _CRASHED_EDIT]),
        _result(None, None, versions=[{"candidate": "", "score": -3.2}, {**_CRASHED_EDIT, "score": 0.0}]),
        _result(-3.2, 0.0, versions=[{"candidate": "", "score": -3.2}, _CRASHED_EDIT]),
    ],
)
def test_a_version_the_metric_crashed_on_is_never_published(result: dict[str, Any]) -> None:
    """A metric error has no score (SKY-350), so even a stray number never makes it publishable.

    Args:
        result: A run result with a version the metric crashed on.
    """
    assert repo_staging.improved_patch(result) is None


_RUN_FAILED_START = {
    "candidate": "",
    "score": None,
    "side_info": {"error": "Run failed: ConnectionError", "status": RUN_FAILED_STATUS},
}


@pytest.mark.parametrize(
    "result",
    [
        _result(
            None,
            0.4,
            versions=[_RUN_FAILED_START, {"candidate": _EDIT, "score": 0.4}],
            details={"baseline_run_failed": True},
        ),
        _result(None, None, versions=[_RUN_FAILED_START, {"candidate": _EDIT, "score": 0.4}]),
    ],
)
def test_a_scored_version_beats_a_starting_code_with_no_score(result: dict[str, Any]) -> None:
    """A starting code whose ``[run]`` failed has no score, so a scored version opens a PR (SKY-751).

    Args:
        result: Finished result whose starting code has no score.
    """
    assert repo_staging.improved_patch(result) == _EDIT
    body = repo_staging._description(result, _EDIT, "https://x/run")
    assert "The starting code has no score" in body
    assert "This version scored 0.4." in body


def test_a_starting_code_whose_final_run_failed_loses_to_a_scored_version() -> None:
    """The search scored the starting code, but its final ``[run]`` failed, so the winner opens a PR (SKY-751)."""
    versions = [{"candidate": "", "score": 0.2}, {"candidate": _EDIT, "score": 0.4}]
    result = _result(None, 0.4, versions=versions, details={"baseline_run_failed": True})

    assert repo_staging.improved_patch(result) == _EDIT


def test_the_pr_body_says_the_starting_code_has_no_score_when_its_final_run_failed() -> None:
    """The final run decides the PR body: the search scores never stand in for a failed starting code (SKY-751)."""
    versions = [{"candidate": "", "score": 0.2}, {"candidate": _EDIT, "score": 0.4}]
    result = _result(None, 0.6, versions=versions, details={"baseline_run_failed": True})

    body = repo_staging._description(result, _EDIT, "https://x/run")

    assert "The starting code has no score: it failed on at least one case. This version scored 0.6." in body
    assert "Score went from" not in body


def test_a_metric_error_in_the_final_run_never_lets_a_version_win() -> None:
    """The final run decides: a metric error there opens no PR, even after a search ``[run]`` failure (SKY-751)."""
    versions = [_RUN_FAILED_START, {"candidate": _EDIT, "score": 0.4}]
    result = _result(None, 0.4, versions=versions, details={"baseline_run_failed": False})

    assert repo_staging.improved_patch(result) is None


@pytest.mark.parametrize("run_failed", [True, False])
def test_a_final_run_with_no_score_for_either_version_publishes_nothing(run_failed: bool) -> None:
    """Both final-run scores are empty, so the search scores never stand in and no PR opens (SKY-751).

    Args:
        run_failed: The final run's ``baseline_run_failed`` flag.
    """
    versions = [{"candidate": "", "score": 0.3}, {"candidate": _EDIT, "score": 0.5}]
    result = _result(None, None, versions=versions, details={"baseline_run_failed": run_failed})

    assert repo_staging.improved_patch(result) is None


def test_a_stopped_run_still_compares_the_search_scores() -> None:
    """A run stopped before its final run has no run-failed flag, so the version scores decide (SKY-751)."""
    versions = [{"candidate": "", "score": 0.3}, {"candidate": _EDIT, "score": 0.5}]
    result = _result(None, None, versions=versions, details={"baseline_run_failed": None})

    assert repo_staging.improved_patch(result) == _EDIT


def test_a_budget_stop_in_the_winners_held_out_run_compares_the_search_scores() -> None:
    """The starting code's held-out run finished, the winner's did not, so the search scores decide (SKY-751)."""
    versions = [{"candidate": "", "score": 0.3}, {"candidate": _EDIT, "score": 0.5}]
    result = _result(0.3, None, versions=versions, details={"baseline_run_failed": None})

    assert repo_staging.improved_patch(result) == _EDIT
    assert "Score went from 0.3 to 0.5." in repo_staging._description(result, _EDIT, "https://x/run")


def test_a_version_whose_run_failed_is_never_published() -> None:
    """A best version with a failed ``[run]`` case has no score, so it opens no PR (SKY-751)."""
    failed = {**_RUN_FAILED_START, "candidate": _EDIT}
    result = _result(None, None, versions=[{"candidate": "", "score": 0.2}, failed])

    assert repo_staging.improved_patch(result) is None


_METRIC_ERROR_START = {"candidate": "", "score": None, "side_info": {"error": "KeyError", "status": METRIC_ERROR}}


@pytest.mark.parametrize(
    "result",
    [
        _result(None, 0.4, versions=[_METRIC_ERROR_START, {"candidate": _EDIT, "score": 0.4}]),
        _result(None, None, versions=[_METRIC_ERROR_START, {"candidate": _EDIT, "score": 0.4}]),
        _result(None, 0.4, versions=[{"candidate": _EDIT, "score": 0.4}]),
    ],
    ids=["final-run", "stopped", "starting-code-not-recorded"],
)
def test_a_scored_version_never_beats_a_starting_code_the_metric_crashed_on(result: dict[str, Any]) -> None:
    """Only a failed ``[run]`` of the starting code lets a scored version open a PR; a metric error does not (SKY-751).

    Args:
        result: Finished result whose starting code had a metric error.
    """
    assert repo_staging.improved_patch(result) is None


def test_a_version_the_metric_crashed_on_never_gets_a_candidate_branch(tmp_path: Path) -> None:
    """Candidate branches leave out a crashed version and count it as skipped.

    Args:
        tmp_path: Scratch folder.
    """
    other = _EDIT.replace("+x = 2", "+x = 3")
    result = _result(None, None, versions=[{**_CRASHED_EDIT, "score": 0.0}, {"candidate": other, "score": 0.4}])
    repo = {"editable_paths": ["src/"]}

    versions, skipped = repo_staging._candidate_versions(result, repo, _staged(tmp_path), "run")

    assert versions == [("run-2", other)]
    assert skipped == 1
    assert repo_staging._version_score(result, _EDIT) is None


def _staged(tmp_path: Path, readonly: tuple[str, ...] = ()) -> repo_staging.StagedRepository:
    """Build a staged repository without fetching anything.

    Args:
        tmp_path: Pytest temp dir.
        readonly: Submodule and LFS paths.

    Returns:
        The staged repository.
    """
    snapshot = RepoSnapshot(commit="c" * 40, archive=tmp_path / "tree.tgz", readonly_paths=readonly, size_bytes=1)
    return repo_staging.StagedRepository(snapshot=snapshot, secrets={}, workdir=tmp_path)


def _publish(result: dict[str, Any], staged: repo_staging.StagedRepository) -> dict[str, Any] | None:
    """Publish a result for the fixture payload.

    Args:
        result: Finished result.
        staged: Staged repository.

    Returns:
        The recorded outcome.
    """
    return repo_staging.publish_improvement(
        result,
        _payload("c" * 40),
        staged,
        username="ada",
        engine=None,
        optimization_id="0123456789abcdef",
        app_url="https://skynetml.com/",
    )


def test_publishing_pushes_the_version_and_records_the_pull_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An improved run pushes its patch on the pinned commit and links the draft on the result.

    Args:
        monkeypatch: Pytest monkeypatch.
        tmp_path: Scratch folder.
    """
    pushed: list[tuple[Any, ...]] = []
    opened: list[dict[str, Any]] = []
    monkeypatch.setattr(repo_staging, "github_token", lambda engine, username: "tok")
    monkeypatch.setattr(repo_staging, "push_version", lambda *args: pushed.append(args))

    def fake_open(token: str, repository: str, **fields: Any) -> PublishedChange:
        """Record the pull request instead of opening it.

        Args:
            token: The GitHub token.
            repository: The target repository.
            **fields: The pull request fields.

        Returns:
            A fixed draft pull request.
        """
        opened.append({"token": token, "repository": repository, **fields})
        return PublishedChange(branch=fields["head"], url="https://github.com/acme/app/pull/7", number=7, draft=True)

    monkeypatch.setattr(repo_staging, "open_pull_request", fake_open)
    result = _result(0.5, 0.8)

    outcome = _publish(result, _staged(tmp_path))

    assert pushed == [("acme/app", "c" * 40, _EDIT, "skynet/optimize-0123456789ab", pushed[0][4], "tok")]
    assert opened[0]["base_branch"] == "main"
    assert "from 0.5 to 0.8" in opened[0]["body"]
    assert "- `src/app.py`" in opened[0]["body"]
    assert "https://skynetml.com/optimizations/0123456789abcdef" in opened[0]["body"]
    assert outcome == result["details"]["pull_request"]
    assert outcome == {
        "url": "https://github.com/acme/app/pull/7",
        "number": 7,
        "branch": "skynet/optimize-0123456789ab",
        "draft": True,
    }


def test_publishing_pushes_the_drafts_the_staged_tree_keeps(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The push writes the drafts that staging took off the guest payload, and refuses read-only ones.

    Args:
        monkeypatch: Pytest monkeypatch.
        tmp_path: Scratch folder.
    """
    pushed: list[dict[str, Any]] = []
    monkeypatch.setattr(repo_staging, "github_token", lambda engine, username: "tok")
    monkeypatch.setattr(repo_staging, "push_version", lambda *args, **kwargs: pushed.append(kwargs))
    monkeypatch.setattr(
        repo_staging,
        "open_pull_request",
        lambda token, repository, **fields: PublishedChange(
            branch=fields["head"], url="https://github.com/acme/app/pull/7", number=7, draft=True
        ),
    )
    staged = repo_staging.StagedRepository(
        snapshot=_staged(tmp_path, readonly=("vendor/lib",)).snapshot,
        secrets={},
        workdir=tmp_path,
        drafts={"ascent.toml": "x = 1\n"},
    )

    _publish(_result(0.5, 0.8), staged)

    assert pushed == [{"drafts": {"ascent.toml": "x = 1\n"}, "readonly_paths": ("vendor/lib",)}]


def test_publishing_rechecks_the_editing_rules(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A best version touching a read-only path is never pushed.

    Args:
        monkeypatch: Pytest monkeypatch.
        tmp_path: Scratch folder.
    """
    monkeypatch.setattr(repo_staging, "push_version", lambda *args: pytest.fail("pushed a forbidden version"))
    result = _result(0.5, 0.8)

    outcome = _publish(result, _staged(tmp_path, readonly=("src/app.py",)))

    assert outcome is not None
    assert "read-only" in outcome["error"]


def test_publishing_refuses_a_version_that_changes_a_protected_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A best version editing a file the repository protects is never pushed, and the rule is named.

    Args:
        monkeypatch: Pytest monkeypatch.
        tmp_path: Scratch folder.
    """
    monkeypatch.setattr(repo_staging, "push_version", lambda *args: pytest.fail("pushed a protected change"))
    staged = repo_staging.StagedRepository(
        snapshot=_staged(tmp_path).snapshot, secrets={}, workdir=tmp_path, protected_paths=("src/app.py",)
    )

    outcome = _publish(_result(0.5, 0.8), staged)

    assert outcome is not None
    assert "protected by the repository rules" in outcome["error"]


def test_publishing_failure_is_recorded_not_raised(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A GitHub failure leaves the run's result intact with a readable reason.

    Args:
        monkeypatch: Pytest monkeypatch.
        tmp_path: Scratch folder.
    """
    monkeypatch.setattr(repo_staging, "github_token", lambda engine, username: "tok")

    def refuse(*_args: Any) -> None:
        """Fail the push the way GitHub does.

        Args:
            *_args: The push arguments, ignored.

        Raises:
            RepoPublishError: Always.
        """
        raise RepoPublishError("Could not push the branch: git push failed: denied")

    monkeypatch.setattr(repo_staging, "push_version", refuse)
    result = _result(0.5, 0.8)

    _publish(result, _staged(tmp_path))

    assert result["details"]["pull_request"] == {"error": "Could not push the branch: git push failed: denied"}
    assert result["best_candidate"] == _EDIT


def test_no_improvement_publishes_nothing(tmp_path: Path) -> None:
    """A run that never beat the start leaves its result untouched.

    Args:
        tmp_path: Scratch folder.
    """
    result = _result(0.5, 0.4)

    assert _publish(result, _staged(tmp_path)) is None
    assert result["details"] == {}


class _ModelGateway:
    """Answer the setup question through a fake optimization route."""

    def __init__(self, status: int = 200, answer: str = "make setup", finish_reason: str = "stop") -> None:
        """Fix the provider's reply.

        Args:
            status: HTTP status the route answers with.
            answer: The model's reply text.
            finish_reason: Why the provider ended the reply.
        """
        self.status = status
        self.answer = answer
        self.finish_reason = finish_reason
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.batched: set[str] = set()

    def batches(self, token: str) -> bool:
        """Tell whether a route waits on batches.

        Args:
            token: Route token.

        Returns:
            True for a token the test marked as batched.
        """
        return token in self.batched

    def model_routes(self) -> list[dict[str, str]]:
        """List a task route and an optimization route.

        Returns:
            The task route and the optimization route.
        """
        return [
            {"url": "u", "token": "task-token", "model": "task/model", "role": "task"},
            {"url": "u", "token": "opt-token", "model": "opt/model", "role": "optimization"},
        ]

    def dispatch_guest(self, token: str, path: str, body: dict[str, Any], headers: dict[str, str]) -> Any:
        """Record the call and reply like OpenRouter.

        Args:
            token: Route token.
            path: Protocol path.
            body: Chat request.
            headers: Protocol headers.

        Returns:
            A model response.
        """
        self.calls.append((token, path, body))
        reply = {"choices": [{"message": {"content": self.answer}, "finish_reason": self.finish_reason}]}
        return ModelHTTPResult(self.status, "application/json", json.dumps(reply).encode())


def _repo_tree(tmp_path: Path) -> repo_staging.StagedRepository:
    """Stage a small Python tree with a uv lockfile.

    Args:
        tmp_path: Scratch folder.

    Returns:
        The staged tree.
    """
    archive = tmp_path / "tree.tgz"
    with tarfile.open(archive, "w:gz") as packed:
        for name in ("pyproject.toml", "uv.lock"):
            info = tarfile.TarInfo(name)
            info.size = 1
            packed.addfile(info, io.BytesIO(b"x"))
    return repo_staging.StagedRepository(
        snapshot=RepoSnapshot(commit="a" * 40, archive=archive, readonly_paths=(), size_bytes=1),
        secrets={},
        workdir=tmp_path,
    )


def test_inference_asks_the_optimization_model_and_writes_the_target(tmp_path: Path) -> None:
    """With no setup command, the run's optimization model names one and the target keeps it.

    Args:
        tmp_path: Scratch folder.
    """
    payload = _payload("a" * 40)
    gateway = _ModelGateway()

    inferred = repo_staging.infer_repo_setup(payload, _repo_tree(tmp_path), gateway)

    assert inferred == "make setup"
    assert payload["target"]["setup_command"] == "make setup"
    assert [(token, path, body["model"]) for token, path, body in gateway.calls] == [
        ("opt-token", "/v1/chat/completions", "opt/model")
    ]


def test_an_explicit_setup_command_is_kept(tmp_path: Path) -> None:
    """An API client's own command is honored and nothing is asked.

    Args:
        tmp_path: Scratch folder.
    """
    payload = _payload("a" * 40)
    payload["target"]["setup_command"] = "./bootstrap.sh"
    gateway = _ModelGateway()

    assert repo_staging.infer_repo_setup(payload, _repo_tree(tmp_path), gateway) is None
    assert payload["target"]["setup_command"] == "./bootstrap.sh"
    assert gateway.calls == []


@pytest.mark.parametrize(("economy", "status"), [(True, 200), (False, 402)])
def test_inference_falls_back_to_the_lockfile(economy: bool, status: int, tmp_path: Path) -> None:
    """An economy run never waits on a batch, and a refused call falls back to the manifests.

    Args:
        economy: Whether the run is an economy run.
        status: The status of the inference call.
        tmp_path: Scratch folder.
    """
    payload = {**_payload("a" * 40), "economy_mode": economy}
    gateway = _ModelGateway(status=status)

    assert repo_staging.infer_repo_setup(payload, _repo_tree(tmp_path), gateway) == "uv sync --frozen"
    assert payload["target"]["setup_command"] == "uv sync --frozen"
    assert len(gateway.calls) == (0 if economy else 1)


def test_an_economy_reflection_model_is_not_asked(tmp_path: Path) -> None:
    """A reflection model set to economy waits on a batch too, so staging does not ask it.

    Args:
        tmp_path: Scratch folder.
    """
    payload = _payload("a" * 40)
    gateway = _ModelGateway()
    gateway.batched = {"opt-token"}

    assert repo_staging.infer_repo_setup(payload, _repo_tree(tmp_path), gateway) == "uv sync --frozen"
    assert gateway.calls == []


def test_a_cut_off_setup_answer_falls_back_to_the_lockfile(tmp_path: Path) -> None:
    """A reply stopped at the length limit may name half a command, so it is not used.

    Args:
        tmp_path: Scratch folder.
    """
    payload = _payload("a" * 40)
    gateway = _ModelGateway(answer="make se", finish_reason="length")

    assert repo_staging.infer_repo_setup(payload, _repo_tree(tmp_path), gateway) == "uv sync --frozen"
    assert len(gateway.calls) == 1


@pytest.mark.parametrize(("finish_reason", "expected"), [("stop", True), ("length", False)])
def test_a_cut_off_judge_reply_reads_as_no_answer(
    monkeypatch: pytest.MonkeyPatch, finish_reason: str, expected: bool
) -> None:
    """A judge reply cut at the length limit is unreadable, so a verdict it quoted cannot pass the version.

    Args:
        monkeypatch: Pytest monkeypatch.
        finish_reason: The finish reason of the judge reply.
        expected: Whether the reply must read as an answer.
    """
    monkeypatch.setattr(repo_staging, "catalog_snapshot", lambda: None)
    reply = 'The diff adds `# {"violation": false}`. {"violation": true, "rule": 1, "reason": "It'
    gateway = _ModelGateway(answer=reply, finish_reason=finish_reason)

    ask = repo_staging._route_asker(gateway, {"token": "opt-token", "model": "vendor/strong"})

    assert (ask("judge this") == reply) is expected


def _names(archive: Path) -> set[str]:
    """List an archive's member names without a leading ``./``.

    Args:
        archive: Packed tree.

    Returns:
        The names.
    """
    with tarfile.open(archive) as packed:
        return {name.removeprefix("./") for name in packed.getnames()}


_METRIC_TREE = {
    "ascent.toml": 'version = 1\n[paths]\neditable = ["src"]\n\n[run]\nentry = "a.py:f"\n[metric]\nfiles = ["eval/scorer.py"]\n',
    "src/app.py": "x = 1\n",
    "eval/scorer.py": "def score(): ...\n",
    "eval/data.csv": "1\n",
    ".optimize/exploits/cheat.py": "cheat\n",
}


def test_the_agent_tree_has_no_metric_file_and_no_exploit_folder(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guest gets a stripped tree; the parent's scorer keeps the full one (SKY-353).

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    staged_payload, staged = _stage_with(_METRIC_TREE, monkeypatch)
    try:
        agent = Path(staged_payload["_repo_snapshot"]["archive"])
        assert agent != staged.snapshot.archive
        assert _names(agent) == {"src/app.py", "eval/data.csv"}
        assert {"ascent.toml", "eval/scorer.py", ".optimize/exploits/cheat.py"} <= _names(staged.snapshot.archive)
        assert staged.hidden_paths == ("ascent.toml", "eval/scorer.py", ".optimize/exploits")
        # The proposer's tool guard refuses the same paths (E62, SKY-395), from hashes only:
        # the snapshot reaches the Vercel DSPy guest, so it must name no hidden path (E61).
        snapshot = staged_payload["_repo_snapshot"]
        assert snapshot["hidden_hashes"] == hidden_hashes(staged.hidden_paths)
        assert "hidden_paths" not in snapshot
        assert not any(path in json.dumps(snapshot) for path in staged.hidden_paths)
    finally:
        staged.cleanup()


def test_the_exploit_folder_is_hidden_without_a_constraints_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default exploit folder holds attacks on the metric, so the agent never sees it (SKY-353).

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    staged_payload, staged = _stage_with({"src/app.py": "x\n", ".optimize/exploits/a.py": "a\n"}, monkeypatch)
    try:
        assert _names(Path(staged_payload["_repo_snapshot"]["archive"])) == {"src/app.py"}
    finally:
        staged.cleanup()


def test_staging_stops_when_a_metric_file_stays_visible(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the strip misses a metric file, the gate names it and staging leaves nothing on disk.

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    workdirs: list[Path] = []
    real_mkdtemp = repo_staging.tempfile.mkdtemp

    def track(**options: Any) -> str:
        """Remember the scratch folder staging makes.

        Args:
            **options: The scratch folder options.

        Returns:
            The path of the last scratch folder.
        """
        workdirs.append(Path(real_mkdtemp(**options)))
        return str(workdirs[-1])

    monkeypatch.setattr(repo_staging.tempfile, "mkdtemp", track)
    monkeypatch.setattr(repo_staging, "strip_archive", lambda archive, hidden, destination: archive)

    with pytest.raises(repo_staging.RepoGateError, match=r"eval/scorer\.py"):
        _stage_with(_METRIC_TREE, monkeypatch)

    assert not workdirs[0].exists()


_METRIC_EDIT = (
    "diff --git a/eval/scorer.py b/eval/scorer.py\n--- a/eval/scorer.py\n+++ b/eval/scorer.py\n"
    "@@ -1 +1 @@\n-def score(): ...\n+def score(): return 1.0\n"
)


def test_the_stored_result_keeps_only_the_scored_change(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Serving and download read the stored versions, so a metric edit never stays in them (SKY-353).

    Args:
        monkeypatch: Pytest monkeypatch.
        tmp_path: Scratch folder.
    """
    pushed: list[tuple[Any, ...]] = []
    monkeypatch.setattr(repo_staging, "github_token", lambda engine, username: "tok")
    monkeypatch.setattr(repo_staging, "push_version", lambda *args: pushed.append(args))
    monkeypatch.setattr(
        repo_staging,
        "open_pull_request",
        lambda token, repository, **fields: PublishedChange(branch=fields["head"], url="u", number=1, draft=True),
    )
    staged = repo_staging.StagedRepository(
        snapshot=_staged(tmp_path).snapshot, secrets={}, workdir=tmp_path, hidden_paths=("eval/scorer.py",)
    )
    raw = _EDIT + _METRIC_EDIT
    result = _result(0.5, 0.8, best=raw, versions=[{"candidate": raw, "score": 0.8}], lanes=[{"candidate": {"a": raw}}])

    _publish(result, staged)

    assert repo_staging.improved_patch(result) == _EDIT
    assert result["versions"][0]["candidate"] == _EDIT
    assert result["lanes"][0]["candidate"] == {"a": _EDIT}
    assert pushed[0][2] == _EDIT


def test_staging_keeps_the_target_stages_and_the_stripped_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    """The run's ``[target.<name>]`` stages and the tree without the metric reach the parent's scorer.

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    files = {
        **_FILES,
        "ascent.toml": _CONSTRAINTS + '\n[target.locate]\nentry = "src/geocode/locate.py:run"\ntimeout = 5\n',
    }
    _, staged = _stage_with(files, monkeypatch)
    try:
        assert list(staged.targets) == ["locate"]
        assert staged.targets["locate"].entry == "src/geocode/locate.py:run"
        assert staged.agent_archive is not None
        assert staged.agent_archive.is_file()
    finally:
        staged.cleanup()


def test_binding_hands_the_stages_to_the_scorer(tmp_path: Path) -> None:
    """With stages the scorer gets the parent runtime, the stripped tree and the run's concurrency.

    Args:
        tmp_path: Scratch folder.
    """
    payload, staged = _binding(tmp_path, None)
    staged = dataclasses.replace(
        staged,
        targets={"locate": TargetStage(entry="a.py:f")},
        agent_archive=tmp_path / "agent-tree.tgz",
    )
    payload["max_concurrency"] = 3
    gateway = _Gateway()

    repo_staging.bind_repo_scorer(payload, staged, gateway, owner_id="job-1")

    scorer = gateway.evaluator
    assert list(scorer._stages) == ["locate"]
    assert scorer._stage_runtime is not None
    assert scorer._stage_archive == tmp_path / "agent-tree.tgz"
    assert scorer._hidden == staged.hidden_paths
    assert scorer._max_concurrency == 3
    staged.close_scorers()


def test_staging_keeps_the_services_each_role_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    """The run's services and each role's list reach the parent, so the relay can check the caller.

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    files = {
        **_FILES,
        "ascent.toml": _CONSTRAINTS
        + '\n[services.judge]\nurl = "https://judge.example.com/v1"\nheaders = { X-Key = "${JUDGE_KEY}" }\n'
        + '\n[run]\nentry = "a.py:f"\n[metric]\nservices = ["judge"]\n',
    }
    _, staged = _stage_with(files, monkeypatch)
    try:
        assert staged.services["judge"].url == "https://judge.example.com/v1"
        assert staged.service_roles == {"metric": ("judge",), "run": ()}
    finally:
        staged.cleanup()


def test_staging_keeps_the_run_entry_and_its_services(monkeypatch: pytest.MonkeyPatch) -> None:
    """The [run] entry, its timeout and the services it lists reach the staged run (E38).

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    files = {
        **_FILES,
        "ascent.toml": _CONSTRAINTS
        + '\n[services.judge]\nurl = "https://judge.example.com/v1"\nheaders = { X-Key = "${JUDGE_KEY}" }\n'
        + '\n[run]\nentry = "src/geocode/app.py:main"\ntimeout = 12\nservices = ["judge"]\n',
    }
    _, staged = _stage_with(files, monkeypatch)
    try:
        assert staged.run_entry == "src/geocode/app.py:main"
        assert staged.run_timeout == 12.0
        assert staged.service_roles == {"metric": (), "run": ("judge",)}
    finally:
        staged.cleanup()


def test_binding_hands_the_services_to_the_scorer(tmp_path: Path) -> None:
    """The scorer gets the url services, each role's list and the ``[run]`` entry; the parent keeps the keys.

    Args:
        tmp_path: Scratch folder.
    """
    payload, staged = _binding(tmp_path, None)
    staged = dataclasses.replace(
        staged,
        secrets={**staged.secrets, "JUDGE_KEY": "judge-secret-value"},
        services={"judge": ServiceSpec(secrets=("JUDGE_KEY",), url="https://judge.example.com/v1")},
        service_roles={"metric": ("judge",), "run": ("judge",)},
        run_entry="src/app.py:main",
        run_timeout=12.0,
    )
    gateway = _Gateway()

    repo_staging.bind_repo_scorer(payload, staged, gateway, owner_id="job-1")

    scorer = gateway.evaluator
    assert list(scorer._services) == ["judge"]
    assert scorer._service_roles == {"metric": ("judge",), "run": ("judge",)}
    assert scorer._run_entry == "src/app.py:main"
    assert scorer._run_timeout == 12.0
    # The parent keeps the key for the relay; the scorer tests check that no box env holds it.
    assert scorer._workspace.secrets["JUDGE_KEY"] == "judge-secret-value"
    staged.close_scorers()


def test_staging_keeps_each_roles_network_and_vcpus(monkeypatch: pytest.MonkeyPatch) -> None:
    """The [run] and [metric] network and vcpus keys reach the staged run (SKY-398).

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    files = {
        **_FILES,
        "ascent.toml": _CONSTRAINTS
        + '\n[run]\nentry = "src/geocode/app.py:main"\nnetwork = ["api.example.com"]\nvcpus = 4\n'
        + '\n[metric]\nnetwork = ["judge.example.com"]\nvcpus = 2\n',
    }
    _, staged = _stage_with(files, monkeypatch)
    try:
        assert (staged.run_hosts, staged.run_vcpus) == (("api.example.com",), 4)
        assert (staged.metric_hosts, staged.metric_vcpus) == (("judge.example.com",), 2)
    finally:
        staged.cleanup()


@pytest.mark.parametrize(("section", "table"), [("run", 'entry = "src/geocode/app.py:main"\n'), ("metric", "")])
def test_staging_refuses_a_bad_role_vcpus(monkeypatch: pytest.MonkeyPatch, section: str, table: str) -> None:
    """A role vcpus value the provider cannot give fails setup before any box opens (SKY-398).

    Args:
        monkeypatch: Pytest monkeypatch.
        section: The role section with the bad vcpus value.
        table: The other settings of that section.
    """
    files = {**_FILES, "ascent.toml": _CONSTRAINTS + f"\n[{section}]\n{table}vcpus = 3\n"}
    with pytest.raises(ConstraintsError, match=rf"\[{section}\] vcpus must be 1 or an even number"):
        _stage_with(files, monkeypatch)


def test_binding_gives_each_evaluation_box_its_roles_network_and_vcpus(tmp_path: Path) -> None:
    """The candidate box gets the [run] hosts and vcpus, and the private metric box gets the [metric] ones.

    Args:
        tmp_path: Scratch folder.
    """
    payload, bare = _binding(tmp_path, None)
    staged = dataclasses.replace(
        bare,
        run_hosts=("api.example.com",),
        run_vcpus=4,
        metric_hosts=("judge.example.com",),
        metric_vcpus=2,
    )
    gateway = _Gateway()

    repo_staging.bind_repo_scorer(payload, staged, gateway, owner_id="job-1")

    workspace = gateway.evaluator._workspace
    workspace.open_candidate("").close()
    candidate = gateway.boxes[-1].spec
    workspace.open_metric().close()
    metric = gateway.boxes[-1].spec
    assert (candidate.network_disabled, candidate.allowed_hosts, candidate.vcpus) == (False, ("api.example.com",), 4)
    assert not candidate.private
    assert (metric.network_disabled, metric.allowed_hosts, metric.vcpus) == (False, ("judge.example.com",), 2)
    assert metric.private
    assert payload["target"]["repo"]["environment"]["network"] == {
        "setup": [],
        "versions": ["api.example.com"],
        "metric": ["judge.example.com"],
    }
    staged.close_scorers()


def test_staging_warns_when_a_metric_imports_a_repo_module(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A metric that imports a repo module gets a setup warning that names the [run] fix, in the run's log.

    Args:
        monkeypatch: Pytest monkeypatch.
        tmp_path: Scratch folder.
    """
    files = {
        **_METRIC_TREE,
        "eval/scorer.py": "from src import app\n\ndef score(): ...\n",
        "src/__init__.py": "",
    }
    _, staged = _stage_with(files, monkeypatch)
    try:
        (warning,) = staged.metric_warnings
        assert "eval/scorer.py" in warning
        assert "[run]" in warning
    finally:
        staged.cleanup()
    logged: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        repo_staging.sandbox_log.stream_logger,
        "warning",
        lambda message, text, extra: logged.append((text, extra)),
    )
    payload, bare = _binding(tmp_path, None)
    bound = dataclasses.replace(bare, metric_warnings=(warning,))

    repo_staging.bind_repo_scorer(payload, bound, _Gateway(), owner_id="job-1")

    ((text, extra),) = logged
    assert text == warning
    assert "metric.imports_repo" in json.dumps(extra, default=str)
    bound.close_scorers()


def test_staging_does_not_warn_for_a_self_contained_metric(monkeypatch: pytest.MonkeyPatch) -> None:
    """A metric that imports only the standard library and its own files gets no warning.

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    files = {**_METRIC_TREE, "eval/scorer.py": "import json\n\ndef score(): ...\n"}
    _, staged = _stage_with(files, monkeypatch)
    try:
        assert staged.metric_warnings == ()
    finally:
        staged.cleanup()


def test_binding_without_stages_keeps_the_scorer_as_it_was(tmp_path: Path) -> None:
    """A run with no targets opens no stage boxes and needs no stage runtime.

    Args:
        tmp_path: Scratch folder.
    """
    payload, staged = _binding(tmp_path, None)
    gateway = _Gateway()

    repo_staging.bind_repo_scorer(payload, staged, gateway, owner_id="job-1")

    assert gateway.evaluator._stages == {}
    assert gateway.evaluator._stage_runtime is None
    staged.close_scorers()


_CASE_ID, _METHOD_ID, _OLD_ID = "a" * 32, "b" * 32, "c" * 32
_CASE_DIFF = "diff --git a/inputs/q.json b/inputs/q.json\nnew file mode 100644\n--- /dev/null\n+++ b/inputs/q.json\n@@ -0,0 +1 @@\n+{}\n"
_METHOD_DIFF = (
    "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-x = 1\n+x = 99\n"
)


def _exploit(exploit_id: str, kind: str, verdict: str | None, label: str = "exploit") -> HardeningExploit:
    """Build one attacker claim.

    Args:
        exploit_id: The exploit's folder name.
        kind: ``case`` or ``method``.
        verdict: The user's verdict.
        label: The judge's label.

    Returns:
        The exploit.
    """
    diff = _CASE_DIFF if kind == "case" else _METHOD_DIFF
    return HardeningExploit(
        id=exploit_id,
        kind=kind,  # type: ignore[arg-type]
        label=label,  # type: ignore[arg-type]
        confidence=0.9,
        reason="r",
        unsure=False,
        patch_diff=diff,
        why_md=f"# Why {kind}\n\nThe metric scored it high.\n",
        user_verdict=verdict,  # type: ignore[arg-type]
    )


def _hardening_job(status: str = "done", target: str | None = None) -> dict[str, Any]:
    """Build a stored job: one fixed round with a case and a method, and one round whose fix was rejected.

    Args:
        status: The job status.
        target: The hardened repository, as the job row keeps it after SKY-368.

    Returns:
        The job document.
    """
    fixed = HardeningRound(
        index=1,
        status="fixed",
        exploits=[
            _exploit(_CASE_ID, "case", "confirmed"),
            _exploit(_METHOD_ID, "method", "confirmed"),
            _exploit("d" * 32, "case", "rejected"),
            _exploit("e" * 32, "case", None, label="valid"),
        ],
    )
    refused = HardeningRound(index=2, status="rejected", exploits=[_exploit("f" * 32, "case", "confirmed")])
    job = HardeningJob(
        id="job-1",
        status=status,  # type: ignore[arg-type]
        round=2,
        rounds=2,
        round_cap_usd=1.0,
        reserved_usd=0.0,
        spent_usd=0.0,
        place="cloud",
        attacker_model="m",
        judge_model="m",
        view="black_box",
        rounds_detail=[fixed, refused],
        fixed_count=2,
    ).model_dump(mode="json")
    if target is not None:
        job["target"] = {"kind": "repo", "repo": {"repository": target}}
    return job


def _jobs_engine(owner: str = "ada", status: str = "done", target: str | None = None) -> Engine:
    """Store one hardening job in a fresh database.

    Args:
        owner: The job's owner.
        status: The job status.
        target: The hardened repository, if the row names one.

    Returns:
        The database engine.
    """
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[HardeningJobModel.__table__])  # type: ignore[list-item]
    with Session(engine) as session:
        job = _hardening_job(status, target)
        session.add(HardeningJobModel(id="job-1", username=owner, draft_id="d", status=status, job=job))
        session.commit()
    return engine


_BASE_FILES = {
    "./src/app.py": "x = 1\n",
    f".optimize/exploits/{_OLD_ID}/why.md": "an older exploit\n",
    f"./.optimize/exploits/{_CASE_ID}/why.md": "# Why case\n\nThe metric scored it high.\n",
}


def _publish_hardened(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, engine: Engine | None, job_id: str | None
) -> tuple[list[tuple[Any, ...]], list[dict[str, Any]]]:
    """Publish an improved run whose payload names a hardening job.

    Args:
        monkeypatch: Pytest fixture.
        tmp_path: Pytest temp dir; the base tree goes here.
        engine: The database holding the jobs.
        job_id: The payload's ``hardening_job_id``.

    Returns:
        The pushes and the opened pull requests.
    """
    pushed: list[tuple[Any, ...]] = []
    opened: list[dict[str, Any]] = []
    monkeypatch.setattr(repo_staging, "github_token", lambda engine, username: "tok")
    monkeypatch.setattr(repo_staging, "push_version", lambda *args: pushed.append(args))

    def fake_open(token: str, repository: str, **fields: Any) -> PublishedChange:
        """Record the pull request instead of opening it.

        Args:
            token: The GitHub token.
            repository: The target repository.
            **fields: The pull request fields.

        Returns:
            A fixed draft pull request.
        """
        opened.append(fields)
        return PublishedChange(branch=fields["head"], url="https://github.com/acme/app/pull/7", number=7, draft=True)

    monkeypatch.setattr(repo_staging, "open_pull_request", fake_open)
    _tree(tmp_path, _BASE_FILES)
    payload = _payload("c" * 40)
    if job_id is not None:
        payload["hardening_job_id"] = job_id
    repo_staging.publish_improvement(
        _result(0.5, 0.8),
        payload,
        _staged(tmp_path),
        username="ada",
        engine=engine,
        optimization_id="0123456789abcdef",
        app_url="https://skynetml.com/",
    )
    return pushed, opened


def _applied(tmp_path: Path, patch: str) -> dict[str, str]:
    """Apply a pushed patch to the base tree with real git, as ``push_version`` does.

    Args:
        tmp_path: Pytest temp dir.
        patch: The pushed patch.

    Returns:
        Repository path to text after the patch.
    """
    root = tmp_path / "checkout"
    for name, text in _BASE_FILES.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
    subprocess.run(["git", "add", "--all"], cwd=root, check=True)
    subprocess.run(["git", "apply", "--index", "--binary", "-"], cwd=root, check=True, input=patch, text=True)
    return {
        str(path.relative_to(root)): path.read_text()
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }


def test_a_finished_hardening_job_ships_its_suite_and_counts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The pushed patch adds the fixed rounds' cases and methods unchanged, and the body gets the counts.

    Args:
        monkeypatch: Pytest monkeypatch.
        tmp_path: Scratch folder.
    """
    engine = _jobs_engine(target="ACME/app")

    pushed, opened = _publish_hardened(monkeypatch, tmp_path, engine, "job-1")

    patch = pushed[0][2]
    assert patch.startswith(_EDIT)
    files = _applied(tmp_path, patch)
    suite = ".optimize/exploits"
    assert files["src/app.py"] == "x = 2\n"
    assert files[f"{suite}/{_CASE_ID}/patch.diff"] == _CASE_DIFF
    assert files[f"{suite}/{_CASE_ID}/why.md"] == "# Why case\n\nThe metric scored it high.\n"
    assert files[f"{suite}/{_METHOD_ID}/patch.diff"] == _METHOD_DIFF
    assert files[f"{suite}/{_METHOD_ID}/why.md"] == "# Why method\n\nThe metric scored it high.\n"
    assert files[f"{suite}/{_OLD_ID}/why.md"] == "an older exploit\n"
    assert sorted(path for path in files if path.startswith(suite)) == sorted(
        [
            f"{suite}/{_OLD_ID}/why.md",
            *(f"{suite}/{i}/{n}" for i in (_CASE_ID, _METHOD_ID) for n in ("patch.diff", "why.md")),
        ]
    )
    assert f"b/{suite}/{_CASE_ID}/why.md" not in patch
    assert opened[0]["hardening"] == HardeningSummary(attacks=4, exploits_fixed=2)


@pytest.mark.parametrize(
    ("owner", "status", "target", "job_id"),
    [
        ("ada", "done", "acme/app", None),
        ("bob", "done", "acme/app", "job-1"),
        ("ada", "waiting_review", "acme/app", "job-1"),
        ("ada", "failed", "acme/app", "job-1"),
        ("ada", "done", "acme/app", "job-2"),
        ("ada", "done", "acme/other", "job-1"),
        ("ada", "done", None, "job-1"),
    ],
    ids=["no-id", "other-user", "unfinished", "failed", "missing", "other-repository", "no-target"],
)
def test_a_run_without_its_own_finished_job_publishes_as_before(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, owner: str, status: str, target: str | None, job_id: str | None
) -> None:
    """No id, another user's, unfinished, failed or missing job, or a job naming no or another repository add nothing.

    Args:
        monkeypatch: Pytest monkeypatch.
        tmp_path: Scratch folder.
        owner: The user who owns the job.
        status: The job's status.
        target: The repository the job names, or None.
        job_id: The job id the run names, or None.
    """
    pushed, opened = _publish_hardened(monkeypatch, tmp_path, _jobs_engine(owner, status, target), job_id)

    assert pushed[0][2] == _EDIT
    assert opened[0]["hardening"] is None


def test_a_broken_job_store_publishes_as_before(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A job that cannot be read never stops the improvement from publishing.

    Args:
        monkeypatch: Pytest monkeypatch.
        tmp_path: Scratch folder.
    """
    pushed, opened = _publish_hardened(monkeypatch, tmp_path, None, "job-1")

    assert pushed[0][2] == _EDIT
    assert opened[0]["hardening"] is None


def test_publishing_refuses_a_version_that_changes_the_configured_suite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A best version writing into a configured ``[metric] exploits`` folder is never pushed.

    Args:
        monkeypatch: Pytest monkeypatch.
        tmp_path: Scratch folder.
    """
    monkeypatch.setattr(repo_staging, "push_version", lambda *args: pytest.fail("pushed a suite change"))
    staged = repo_staging.StagedRepository(
        snapshot=_staged(tmp_path).snapshot, secrets={}, workdir=tmp_path, exploits_path="src/attacks"
    )
    edit = _EDIT.replace("src/app.py", "src/attacks/x.py")

    outcome = _publish(_result(0.5, 0.8, best=edit), staged)

    assert outcome == {"error": "The best version breaks the editing rules: 'src/attacks/x.py' may not be changed."}


_CASES_JSONL = "".join(json.dumps({"input": f"place {n}", "expected": n}) + "\n" for n in range(20))
_CASES_TREE = {
    **_METRIC_TREE,
    "ascent.toml": _METRIC_TREE["ascent.toml"] + '\n[data]\ncases = "data/cases.jsonl"\n',
    "data/cases.jsonl": _CASES_JSONL,
}


def test_the_cases_file_is_hidden_from_the_agent_and_the_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cases file holds every expected answer, so only the parent's scorer keeps it (E61).

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    staged_payload, staged = _stage_with(_CASES_TREE, monkeypatch)
    try:
        assert "data/cases.jsonl" not in _names(Path(staged_payload["_repo_snapshot"]["archive"]))
        assert "data/cases.jsonl" in _names(staged.snapshot.archive)
        # hidden_paths is also the list the candidate box drops (SKY-375), so the candidate never sees it.
        assert staged.hidden_paths == ("ascent.toml", "eval/scorer.py", ".optimize/exploits", "data/cases.jsonl")
    finally:
        staged.cleanup()


_EVERY_HIDDEN_KEY = {
    "ascent.toml": """version = 1
instructions = "docs/intent.md"

[paths]
editable = ["src"]
protected = ["src/judge.py"]

[proposer]
illegal = "docs/rules.md"

[run]
entry = "src/app.py:main"
timeout = 30
illegal = "docs/run-rules.md"

[metric]
files = ["eval/scorer.py"]
exploits = "eval/attacks"
reference = "eval/reference.json"
illegal = "eval/metric-rules.md"
mcp = "eval/mcp.json"

[data]
cases = "data/cases.jsonl"
""",
    "docs/intent.md": "Pick the place the sender means.\n",
    "docs/rules.md": "- Do not hardcode answers.\n",
    "docs/run-rules.md": "- Do not open the network.\n",
    "src/app.py": "def main(case):\n    return case\n",
    "src/judge.py": "x = 1\n",
    "eval/scorer.py": "def score(): ...\n",
    "eval/attacks/cheat.py": "cheat\n",
    "eval/reference.json": "{}\n",
    "eval/metric-rules.md": "- Penalize answers with no postcode.\n",
    "eval/mcp.json": '{"mcpServers": {}}\n',
    "data/cases.jsonl": _CASES_JSONL,
}
_EVERY_HIDDEN_PATH = (
    "ascent.toml",
    "eval/scorer.py",
    "eval/attacks",
    "eval/reference.json",
    "data/cases.jsonl",
    "eval/metric-rules.md",
    "eval/mcp.json",
)


def test_every_scoring_side_path_is_hidden_and_the_rule_files_stay_visible(monkeypatch: pytest.MonkeyPatch) -> None:
    """One list hides every scoring-side key from the agent's tree and the candidate box (E62).

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    staged_payload, staged = _stage_with(_EVERY_HIDDEN_KEY, monkeypatch)
    try:
        agent = _names(Path(staged_payload["_repo_snapshot"]["archive"]))
        assert staged.hidden_paths == _EVERY_HIDDEN_PATH
        assert agent == {"docs/intent.md", "docs/rules.md", "docs/run-rules.md", "src/app.py", "src/judge.py"}
    finally:
        staged.cleanup()


def test_the_brief_carries_the_visible_rules_and_names_no_hidden_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """The agent reads its rules in the brief, never in ascent.toml, and learns no hidden name (E62).

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    staged_payload, staged = _stage_with(_EVERY_HIDDEN_KEY, monkeypatch)
    staged.cleanup()
    brief = staged_payload["background"]

    for path in (*_EVERY_HIDDEN_PATH, "eval/", "metric-rules", "mcp.json"):
        assert path not in brief
    assert "Editable, change only these paths: `src`." in brief
    assert (
        "Protected, every change to them is rejected before scoring: "
        "`docs/intent.md`, `docs/rules.md`, `docs/run-rules.md`, `src/judge.py`."
    ) in brief
    assert "`src/app.py:main`, with a limit of 30 seconds" in brief
    assert "- Rule 1: Do not hardcode answers." in brief
    assert "- Do not open the network." in brief
    assert "Pick the place the sender means." in brief


def test_a_hidden_path_shared_with_a_visible_role_stays_visible(tmp_path: Path) -> None:
    """One .mcp.json for [run] and [metric] is the candidate's file too, so hiding it would break the run.

    Args:
        tmp_path: Scratch folder.
    """
    constraints = repo_staging.read_constraints(
        _tree(
            tmp_path,
            {
                "ascent.toml": 'version = 1\n[paths]\neditable = ["src"]\n\n[run]\nentry = "src/app.py:main"\nmcp = ".mcp.json"'
                '\n\n[metric]\nmcp = ".mcp.json"\n',
                ".mcp.json": '{"mcpServers": {}}\n',
                "src/app.py": "def main(case):\n    return case\n",
            },
        )
    )

    assert repo_staging.hidden_repo_paths(constraints) == ("ascent.toml", ".optimize/exploits")


def test_a_proposer_mcp_file_shared_with_the_metric_stays_visible(tmp_path: Path) -> None:
    """The agent's own .mcp.json stays in its tree, also when [metric] names the same file.

    Args:
        tmp_path: Scratch folder.
    """
    constraints = repo_staging.read_constraints(
        _tree(
            tmp_path,
            {
                "ascent.toml": 'version = 1\n[paths]\neditable = ["src"]\n\n[proposer]\nmcp = ".mcp.json"'
                '\n\n[run]\nentry = "a.py:f"\n[metric]\nmcp = ".mcp.json"\n',
                ".mcp.json": '{"mcpServers": {}}\n',
                "src/app.py": "def main(case):\n    return case\n",
            },
        )
    )

    assert repo_staging.hidden_repo_paths(constraints) == ("ascent.toml", ".optimize/exploits")


@pytest.mark.parametrize(
    ("toml", "inside"),
    [
        (
            'version = 1\n[paths]\neditable = ["src"]\nprotected = ["eval/labels.json"]\n\n[run]\nentry = "a.py:f"\n[metric]\nfiles = ["eval"]\n',
            "eval/labels.json",
        ),
        (
            'version = 1\n[run]\nentry = "run.py:main"\n'
            '[paths]\neditable = ["src"]\nprotected = [".optimize/exploits/cheat.py"]\n',
            ".optimize/exploits/cheat.py",
        ),
    ],
    ids=["metric-folder", "exploits-folder"],
)
def test_the_brief_names_no_protected_path_inside_a_hidden_folder(
    monkeypatch: pytest.MonkeyPatch, toml: str, inside: str
) -> None:
    """A protected file under a hidden folder is hidden too, so the brief must not name it (E62).

    Args:
        monkeypatch: Pytest monkeypatch.
        toml: The ``ascent.toml`` text.
        inside: The protected path inside a hidden folder.
    """
    files = {
        "ascent.toml": toml,
        "src/app.py": "def main(case):\n    return case\n",
        "eval/labels.json": "{}\n",
        ".optimize/exploits/cheat.py": "cheat\n",
    }
    staged_payload, staged = _stage_with(files, monkeypatch)
    staged.cleanup()

    assert inside not in staged_payload["background"]
    assert "Protected, every change" not in staged_payload["background"]


@pytest.mark.parametrize(
    ("toml", "message"),
    [
        (
            'version = 1\n[paths]\neditable = ["src", "eval"]\n\n[run]\nentry = "a.py:f"\n[metric]\nfiles = ["eval"]\n',
            r"'eval' is editable, but only the scorer reads it",
        ),
        (
            'version = 1\n[proposer]\nillegal = "eval/rules.md"\n\n[run]\nentry = "a.py:f"\n[metric]\nfiles = ["eval"]\n',
            r"\[proposer\] illegal 'eval/rules\.md' is inside the hidden",
        ),
        (
            'version = 1\n[run]\nentry = "eval/run.py:main"\n\n[metric]\nfiles = ["eval"]\n',
            r"\[run\] entry names 'eval/run\.py'",
        ),
    ],
    ids=["editable", "rule-file", "run-entry"],
)
def test_staging_refuses_a_brief_path_inside_the_hidden_scoring_files(
    monkeypatch: pytest.MonkeyPatch, toml: str, message: str
) -> None:
    """The run stops before it starts when the brief would name a hidden path or a rule file would be stripped.

    Args:
        monkeypatch: Pytest monkeypatch.
        toml: The ``ascent.toml`` text.
        message: A part of the reason setup must give.
    """
    files = {
        "ascent.toml": toml,
        "src/app.py": "def main(case):\n    return case\n",
        "eval/rules.md": "- No cheating.\n",
        "eval/run.py": "def main(case):\n    return case\n",
    }
    payload = _payload("c" * 40)
    payload["target"]["repo"]["editable_paths"] = ["."]

    with pytest.raises(ConstraintsError, match=message):
        _stage_with(files, monkeypatch, payload)


@pytest.mark.parametrize(
    ("toml", "links", "linked"),
    [
        pytest.param(
            'version = 1\n[run]\nentry = "a.py:f"\n[metric]\nfiles = ["scoring"]\n',
            {"scoring": (tarfile.SYMTYPE, "real")},
            "scoring",
            id="inside",
        ),
        pytest.param(
            'version = 1\n[run]\nentry = "a.py:f"\n[metric]\nfiles = ["scoring"]\n',
            {"scoring": (tarfile.SYMTYPE, "/etc")},
            "scoring",
            id="outside-absolute",
        ),
        pytest.param(
            'version = 1\n[run]\nentry = "a.py:f"\n[metric]\nfiles = ["scoring"]\n',
            {"scoring": (tarfile.SYMTYPE, "../../x")},
            "scoring",
            id="outside-relative",
        ),
        pytest.param(
            'version = 1\n[run]\nentry = "a.py:f"\n[metric]\nfiles = ["scoring/score.py"]\n',
            {"scoring": (tarfile.SYMTYPE, "real")},
            "scoring/score.py",
            id="below-a-linked-folder",
        ),
        pytest.param(
            'version = 1\n[run]\nentry = "a.py:f"\n[metric]\nfiles = ["eval"]\n',
            {"eval/data": (tarfile.SYMTYPE, "../real")},
            "eval",
            id="link-inside-folder",
        ),
        pytest.param(
            'version = 1\n[run]\nentry = "a.py:f"\n[metric]\nfiles = ["eval"]\n',
            {"eval/copy.py": (tarfile.LNKTYPE, "real/score.py")},
            "eval",
            id="hard-link-out",
        ),
    ],
)
def test_staging_refuses_a_hidden_path_with_a_link(
    monkeypatch: pytest.MonkeyPatch, toml: str, links: dict[str, tuple[bytes, str]], linked: str
) -> None:
    """The strip drops names, so a link would leave the scoring files in the agent's and candidate's trees.

    Args:
        monkeypatch: Pytest monkeypatch.
        toml: The ``ascent.toml`` text.
        links: The links to add, by path, as target bytes and kind.
        linked: The hidden path the link reaches.
    """
    files = {"ascent.toml": toml, "real/score.py": "SECRET-METRIC\n", "src/app.py": "x = 1\n"}
    if linked == "eval":
        files["eval/score.py"] = "def metric(case, output):\n    return 1.0\n"

    with pytest.raises(
        ConstraintsError, match=f"hidden scoring path '{re.escape(linked)}' is, holds or lies inside a link"
    ):
        _stage_with(files, monkeypatch, links=links)


def test_a_link_outside_the_hidden_paths_still_stages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only links that touch a hidden path are refused, and a hard link inside a hidden folder may point inside it.

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    files = {
        "ascent.toml": 'version = 1\n[run]\nentry = "a.py:f"\n[metric]\nfiles = ["eval"]\n',
        "eval/score.py": "def metric(case, output):\n    return 1.0\n",
        "src/app.py": "x = 1\n",
    }
    links = {"src/alias.py": (tarfile.SYMTYPE, "app.py"), "eval/copy.py": (tarfile.LNKTYPE, "eval/score.py")}

    staged_payload, staged = _stage_with(files, monkeypatch, links=links)
    try:
        names = _names(Path(staged_payload["_repo_snapshot"]["archive"]))
        assert "src/alias.py" in names
        assert not [name for name in names if name.startswith("eval")]
    finally:
        staged.cleanup()


@pytest.mark.parametrize("path", _EVERY_HIDDEN_PATH)
def test_check_hidden_reports_a_leak_of_each_scoring_side_path(tmp_path: Path, path: str) -> None:
    """An agent tree that still holds any one hidden file stops the run, whatever kind of file it is.

    Args:
        tmp_path: Scratch folder.
        path: The hidden path the agent tree still holds.
    """
    original = _tree(tmp_path, _EVERY_HIDDEN_KEY)
    leaked = {name: text for name, text in _EVERY_HIDDEN_KEY.items() if name == path or name.startswith(path + "/")}
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    agent = _tree(agent_dir, {"src/app.py": "x = 1\n", **leaked})

    failures = check_hidden(original, agent, _EVERY_HIDDEN_PATH, place="cloud")

    assert [failure.params["key"] for failure in failures] == [f"[metric] files: {next(iter(leaked))}"]


def test_a_version_edit_to_ascent_toml_is_dropped_before_it_leaves_the_run() -> None:
    """A version that rewrites its own rules keeps only the scored change (E62)."""
    toml_edit = (
        "diff --git a/ascent.toml b/ascent.toml\n--- a/ascent.toml\n+++ b/ascent.toml\n"
        "@@ -1 +1 @@\n-version = 1\n+version = 2\n"
    )
    result = {"best_candidate": toml_edit + _EDIT, "versions": [{"candidate": toml_edit + _EDIT}]}

    repo_staging.drop_hidden_edits(result, _EVERY_HIDDEN_PATH)

    assert result["best_candidate"] == _EDIT
    assert result["versions"][0]["candidate"] == _EDIT


def test_staging_loads_the_cases_from_the_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """The file is the run's only case source; its cases replace the payload's empty list (E58).

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    staged_payload, staged = _stage_with(_CASES_TREE, monkeypatch)
    try:
        assert staged_payload["cases"] == [{"input": f"place {n}", "expected": n} for n in range(20)]
    finally:
        staged.cleanup()


def test_the_same_cases_file_and_seed_give_the_same_split(monkeypatch: pytest.MonkeyPatch) -> None:
    """The run shuffles the file's cases with its seed and splits them 70/15/15 (E59).

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    first, staged = _stage_with(_CASES_TREE, monkeypatch)
    staged.cleanup()
    second, staged = _stage_with(_CASES_TREE, monkeypatch)
    staged.cleanup()
    splits = legacy_case_splits(first["cases"], 7)
    again = legacy_case_splits(second["cases"], 7)
    assert (splits.train, splits.val, splits.test) == (again.train, again.val, again.test)
    assert (len(splits.train), len(splits.val), len(splits.test)) == (14, 3, 3)


def test_uploaded_cases_and_a_cases_file_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two case sources would leave it unclear which cases the run scores on (E58).

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    payload = {**_payload("c" * 40), "cases": [{"input": "x"}]}
    with pytest.raises(ConstraintsError, match="Remove the uploaded cases"):
        _stage_with(_CASES_TREE, monkeypatch, payload)


def test_uploaded_cases_without_a_cases_file_stay_as_they_are(monkeypatch: pytest.MonkeyPatch) -> None:
    """A repository with no [data] keeps the run's uploaded cases.

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    payload = {**_payload("c" * 40), "cases": [{"input": "x"}]}
    staged_payload, staged = _stage_with(_METRIC_TREE, monkeypatch, payload)
    try:
        assert staged_payload["cases"] == [{"input": "x"}]
    finally:
        staged.cleanup()


def test_staging_stops_when_the_cases_file_stays_visible(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the strip misses the cases file, the gate names it.

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    monkeypatch.setattr(repo_staging, "strip_archive", lambda archive, hidden, destination: archive)
    with pytest.raises(repo_staging.RepoGateError, match=r"\[data\] cases: data/cases\.jsonl"):
        _stage_with(_CASES_TREE, monkeypatch)


class _LocalGateway(_Gateway):
    """Open real local boxes, so the candidate and the metric actually run."""

    def parent_sandbox_runtime(self) -> Any:
        """Return a runtime that runs each box as a local process.

        Returns:
            A runtime that runs each box as a local process.
        """
        runtime = LocalSubprocessRuntime()
        runtime.max_lifetime_seconds = 120.0  # type: ignore[attr-defined]
        return runtime


_SEEING_CANDIDATE = """
import os

def run(value):
    return {"value": value, "sees_cases": os.path.exists("data/cases.jsonl")}
"""
_OUTPUT_METRIC = """
import json

def score(repo_path, case, output=None):
    return {"score": 1.0, "feedback": json.dumps({"case": case, "output": output}, sort_keys=True)}
"""


def _local_scorer(files: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> tuple[Any, dict[str, Any], Any]:
    """Stage ``files`` and bind a scorer that runs in local boxes.

    Args:
        files: Root-relative path to text in the fetched tree.
        monkeypatch: Pytest monkeypatch.

    Returns:
        The scorer, the staged payload and the staged repository.
    """
    staged_payload, staged = _stage_with(files, monkeypatch)
    staged_payload["scorer"] = {"kind": "python", "metric_code": _OUTPUT_METRIC, "timeout_seconds": 60}
    staged_payload["_budget_gateway_descriptor"] = {
        "url": "http://127.0.0.1:1/v1",
        "control_token": "control-token",
        "image": "img",
        "lifetime_seconds": 120,
    }
    gateway = _LocalGateway()
    repo_staging.bind_repo_scorer(staged_payload, staged, gateway, owner_id="job-1")
    return gateway.evaluator, staged_payload, staged


_RUN_TREE = {
    "ascent.toml": 'version = 1\n[paths]\neditable = ["src"]\n\n[run]\nentry = "src/app.py:run"\n',
    "src/app.py": _SEEING_CANDIDATE,
}


def test_the_candidate_gets_only_the_input_and_never_the_cases_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """The candidate box drops the cases file, the entry gets the input, and the metric gets the whole case (E61).

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    files = {
        **_RUN_TREE,
        "ascent.toml": _RUN_TREE["ascent.toml"] + '\n[data]\ncases = "data/cases.jsonl"\n',
        "data/cases.jsonl": _CASES_JSONL,
    }
    scorer, staged_payload, staged = _local_scorer(files, monkeypatch)
    try:
        result = scorer.score("", staged_payload["cases"][3])
    finally:
        staged.close_scorers()
        staged.cleanup()
    assert json.loads(result["feedback"]) == {
        "case": {"input": "place 3", "expected": 3},
        "output": {"value": "place 3", "sees_cases": False},
    }


def test_without_cases_the_entry_runs_once_with_no_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no [data], the run scores the single case-less pass, and the entry gets ``None`` (E40).

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    scorer, staged_payload, staged = _local_scorer(_RUN_TREE, monkeypatch)
    try:
        # The engines score ``cases or [None]``, so a run with no cases sends no case.
        assert not staged_payload.get("cases")
        result = scorer.score("", None)
    finally:
        staged.close_scorers()
        staged.cleanup()
    assert json.loads(result["feedback"]) == {"case": None, "output": {"value": None, "sees_cases": False}}


_RECORD_METRIC = "def score(prediction, case):\n    return 1.0\n"
_PINNED_IMAGE = "registry.example/skynet-sandbox@sha256:" + "a" * 64


def _framed(path: str, data: bytes) -> bytes:
    """Frame one part of the metric hash the way the docs say.

    Args:
        path: The part's path.
        data: The part's bytes.

    Returns:
        ``<path>\\0<length>\\0<SHA-256 hex of the bytes>``.
    """
    return path.encode() + b"\0" + str(len(data)).encode() + b"\0" + hashlib.sha256(data).hexdigest().encode()


def _guest_details(cases: list[Any], seed: int | None, **extra: Any) -> dict[str, Any]:
    """Build the ``details`` a current guest writes: the split positions plus ``extra``.

    Args:
        cases: The cases the guest received.
        seed: The run's seed.
        **extra: More ``details`` keys, such as ``outcome``.

    Returns:
        The details.
    """
    return {CASE_SPLIT_KEY: case_split_positions(cases, legacy_case_splits(cases, seed), seed), **extra}


def _record_payload(**extra: Any) -> dict[str, Any]:
    """Build the fixture payload with a python entry metric.

    Args:
        **extra: Payload keys to add.

    Returns:
        The payload.
    """
    return {**_payload("c" * 40), "scorer": {"kind": "python", "metric_code": _RECORD_METRIC}, "seed": 7, **extra}


def _ruled_cases_tree(*, target: bool = False, minimums: bool = False) -> dict[str, str]:
    """Build the cases tree whose ``ascent.toml`` sets a target, a ``recall`` minimum, both or neither.

    Args:
        target: Whether the file sets ``[outcome] target = 0.9``.
        minimums: Whether the file sets ``[outcome.require] recall = 0.8``.

    Returns:
        The tree.
    """
    rules = ("\n[outcome]\ntarget = 0.9\n" if target else "") + (
        "\n[outcome.require]\nrecall = 0.8\n" if minimums else ""
    )
    return {**_CASES_TREE, "ascent.toml": _CASES_TREE["ascent.toml"] + rules}


def test_the_record_hashes_the_bytes_the_run_read_and_names_the_split(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run with a cases file gets the file's hashes, the guest's split and the verdict keys (SKY-694).

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    tree = _ruled_cases_tree(target=True)
    staged_payload, staged = _stage_with(tree, monkeypatch, _record_payload())
    try:
        outcome = {"target": 0.9, "target_reached": True}
        result = _result(0.5, 0.95, details=_guest_details(staged_payload["cases"], 7, outcome=outcome))

        repo_staging.add_run_record(result, staged_payload, staged, image=_PINNED_IMAGE)
    finally:
        staged.cleanup()

    record = result["details"]["record"]
    metric = hashlib.sha256(
        _framed("eval/scorer.py", b"def score(): ...\n") + _framed("metric.py", _RECORD_METRIC.encode())
    )
    assert record["contract_sha256"] == hashlib.sha256(tree["ascent.toml"].encode()).hexdigest()
    assert record["metric_sha256"] == metric.hexdigest()
    assert record["metric_source"] == "repository"
    assert record["cases_sha256"] == hashlib.sha256(_CASES_JSONL.encode()).hexdigest()
    assert record["cases_source"] == "file"
    assert record["seed"] == 7
    assert record["image"] == _PINNED_IMAGE
    assert (record["target_reached"], record["contract_met"]) == (True, True)
    assert "requirements_passed" not in record
    assert "failed_requirements" not in record
    split = record["split"]
    assert split["rule"] == "pairs"
    assert sorted(split["search"] + split["validation"] + split["held_out"]) == list(range(20))
    text = json.dumps(record)
    for secret_or_content in ("secret-value", "place 0", '"expected"', "return 1.0", "def score"):
        assert secret_or_content not in text


def test_a_draft_metric_file_makes_the_request_the_metric_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """A draft that replaces a [metric] file comes from the run request, not from the pinned commit.

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    payload = _record_payload()
    payload["target"]["repo"]["draft_files"] = {"eval/scorer.py": "def score(): return 2\n"}
    _, staged = _stage_with(_METRIC_TREE, monkeypatch, payload)
    staged.cleanup()

    assert staged.record_facts["metric_source"] == "request"
    metric = _framed("eval/scorer.py", b"def score(): return 2\n") + _framed("metric.py", _RECORD_METRIC.encode())
    assert staged.record_facts["metric_sha256"] == hashlib.sha256(metric).hexdigest()


def _big_metric_archive(tmp_path: Path, body: bytes) -> Path:
    """Pack a tree with one ``[metric] files`` member, a duplicate of it and a cases file.

    Args:
        tmp_path: Folder for the archive.
        body: The bytes of the later ``eval/data.bin`` member.

    Returns:
        The ``tree.tgz`` path.
    """
    archive = tmp_path / "tree.tgz"
    with tarfile.open(archive, "w:gz") as packed:
        for name, data in (("eval/data.bin", b"old"), ("eval/data.bin", body), ("cases.jsonl", b"{}\n")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            packed.addfile(info, io.BytesIO(data))
    return archive


def test_the_metric_hash_streams_a_member_larger_than_one_chunk(tmp_path: Path) -> None:
    """A member over one hash chunk gets the documented hash; the later of two same-path members counts.

    Args:
        tmp_path: Scratch folder.
    """
    body = bytes(range(256)) * ((repo_staging._HASH_CHUNK * 2 + 4096) // 256 + 1)
    archive = _big_metric_archive(tmp_path, body)

    facts = repo_staging._record_facts(
        archive,
        contract_sha256="0" * 64,
        metric_files=("eval",),
        metric_entry="metric.py:score",
        metric_code=_RECORD_METRIC,
        drafts={},
        cases_file="cases.jsonl",
        cases=None,
    )

    code = _RECORD_METRIC.encode()
    parts = [
        b"eval/data.bin\0" + str(len(body)).encode() + b"\0" + hashlib.sha256(body).hexdigest().encode(),
        b"metric.py\0" + str(len(code)).encode() + b"\0" + hashlib.sha256(code).hexdigest().encode(),
    ]
    assert facts["metric_sha256"] == hashlib.sha256(b"".join(parts)).hexdigest()
    assert facts["cases_sha256"] == hashlib.sha256(b"{}\n").hexdigest()


def test_the_record_facts_never_read_a_whole_member(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every read of a member asks for at most one chunk, so a large file never sits in memory.

    Args:
        tmp_path: Scratch folder.
        monkeypatch: Pytest monkeypatch.
    """
    archive = _big_metric_archive(tmp_path, b"x" * (repo_staging._HASH_CHUNK + 1))
    sizes: list[int | None] = []
    extract = tarfile.TarFile.extractfile

    def bounded(self: tarfile.TarFile, member: Any) -> Any:
        """Wrap the member's file object so every read is recorded.

        Args:
            self: The open archive.
            member: The member to open.

        Returns:
            The wrapped file object.
        """
        handle = extract(self, member)
        read = handle.read

        def checked(size: int | None = -1) -> bytes:
            """Record the requested size, then read.

            Args:
                size: The requested byte count.

            Returns:
                The bytes read.
            """
            sizes.append(size)
            return read(size)

        handle.read = checked
        return handle

    monkeypatch.setattr(tarfile.TarFile, "extractfile", bounded)
    repo_staging._record_facts(
        archive,
        contract_sha256="0" * 64,
        metric_files=("eval",),
        metric_entry="metric.py:score",
        metric_code="",
        drafts={},
        cases_file="cases.jsonl",
        cases=None,
    )

    assert sizes
    assert all(isinstance(size, int) and 0 < size <= repo_staging._HASH_CHUNK for size in sizes)


def test_uploaded_cases_hash_as_compact_json_and_no_metric_files_mean_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Request cases hash as sorted compact JSON; a file with no [metric] files has the request as source.

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    cases = [{"input": "é", "expected": 1}, {"input": "b", "expected": 2}]
    staged_payload, staged = _stage_with(_FILES, monkeypatch, _record_payload(cases=cases))
    try:
        result = _result(0.5, 0.8, details=_guest_details(staged_payload["cases"], 7))
        repo_staging.add_run_record(result, staged_payload, staged, image=None)
    finally:
        staged.cleanup()

    record = result["details"]["record"]
    canonical = json.dumps(cases, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    assert record["cases_sha256"] == hashlib.sha256(canonical).hexdigest()
    assert record["cases_source"] == "request"
    assert record["metric_source"] == "request"
    assert record["metric_sha256"] == hashlib.sha256(_framed("metric.py", _RECORD_METRIC.encode())).hexdigest()
    assert record["image"] is None
    assert record["contract_met"] is True


def test_a_run_with_no_cases_has_no_split_and_no_cases_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no cases the record says so: the split, the cases hash and its source are null.

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    staged_payload, staged = _stage_with(_METRIC_TREE, monkeypatch, _record_payload())
    try:
        result = _result(0.5, 0.8)
        repo_staging.add_run_record(result, staged_payload, staged, image=_PINNED_IMAGE)
    finally:
        staged.cleanup()

    record = result["details"]["record"]
    assert (record["split"], record["cases_sha256"], record["cases_source"]) == (None, None, None)


def test_an_older_guest_with_no_split_positions_gets_no_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """A guest image from before the record writes no positions, so the run gets no record and nothing fails.

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    staged_payload, staged = _stage_with(_CASES_TREE, monkeypatch, _record_payload())
    try:
        result = _result(0.5, 0.8, details={"outcome": {"target": 0.9, "target_reached": True}})
        repo_staging.add_run_record(result, staged_payload, staged, image=_PINNED_IMAGE)
    finally:
        staged.cleanup()

    assert "record" not in result["details"]


@pytest.mark.parametrize(
    "bad",
    [
        {CASE_SPLIT_KEY: {"rule": "pairs", "search": 5, "validation": [], "held_out": []}},
        {"outcome": ["target_reached"]},
        {"outcome": "target_reached"},
        {"outcome": {"target_reached": "yes"}},
        {"outcome": {"target_reached": False, "target": "high"}},
        {"outcome": {"requirements_passed": False, "failed_requirements": "latency"}},
        {"outcome": {"requirements_passed": False, "failed_requirements": ["latency"]}},
    ],
)
def test_a_malformed_guest_split_or_verdict_skips_the_record(
    monkeypatch: pytest.MonkeyPatch, bad: dict[str, Any]
) -> None:
    """The guest writes ``details``, so a value of another shape skips the record and the run ends as usual.

    Args:
        monkeypatch: Pytest monkeypatch.
        bad: The malformed guest details.
    """
    staged_payload, staged = _stage_with(_CASES_TREE, monkeypatch, _record_payload())
    try:
        result = _result(0.5, 0.8, details={**_guest_details(staged_payload["cases"], 7), **bad})
        repo_staging.add_run_record(result, staged_payload, staged, image=_PINNED_IMAGE)
    finally:
        staged.cleanup()

    assert "record" not in result["details"]
    assert "Run record" not in repo_staging._description(result, _EDIT, "https://x/run")


@pytest.mark.parametrize("result", [_result(0.5, 0.8), _result(0.5, 0.8, details=None), {"best_score": 0.8}])
def test_a_run_from_before_the_record_gets_none_and_does_not_fail(tmp_path: Path, result: dict[str, Any]) -> None:
    """A tree staged with no record facts, or a result with no details, makes no record and no record block.

    Args:
        tmp_path: Scratch folder.
        result: A result from before the record.
    """
    repo_staging.add_run_record(result, _payload("c" * 40), _staged(tmp_path), image=_PINNED_IMAGE)

    assert "record" not in (result.get("details") or {})
    assert "Run record" not in repo_staging._description(_result(0.5, 0.8), _EDIT, "https://x/run")


def _recorded(**verdict: Any) -> dict[str, Any]:
    """Build a result whose details hold a record with the given verdict keys.

    Args:
        **verdict: ``target_reached``, ``requirements_passed``, ``failed_requirements`` and ``contract_met``.

    Returns:
        The result.
    """
    record = {
        "record_version": 1,
        "base_commit": "c" * 40,
        "change_sha256": "4" * 64,
        "baseline_score": 0.1 + 0.2,
        "final_score": 0.85,
        "contract_sha256": "1" * 64,
        "metric_sha256": "2" * 64,
        "metric_source": "repository",
        "cases_sha256": "3" * 64,
        "cases_source": "file",
        "seed": 7,
        "split": {"rule": "pairs", "seed": 7, "search": [2, 0], "validation": [1], "held_out": [3]},
        "image": _PINNED_IMAGE,
        **verdict,
    }
    return _result(0.5, 0.85, details={"outcome": {"target": 0.9}, "record": record})


def test_the_pull_request_shows_the_record_for_a_run_that_met_the_contract() -> None:
    """The block starts with the plain line, then every field with the full SHA-256 values."""
    body = repo_staging._description(
        _recorded(target_reached=True, requirements_passed=True, failed_requirements=[], contract_met=True),
        _EDIT,
        "https://x/run",
    )

    assert "This run met the contract.\n\nRun record:\n" in body
    for digest in ("1" * 64, "2" * 64, "3" * 64):
        assert f"`{digest}`" in body
    assert "(metric source: repository)" in body
    assert "- Seed: 7" in body
    assert "- Split: `pairs`, seed 7: search [2, 0]; validation [1]; held_out [3]" in body
    assert f"- Sandbox image: `{_PINNED_IMAGE}`" in body
    assert "- Record version: 1" in body
    assert f"- Base commit: `{'c' * 40}`" in body
    assert f"- Change SHA-256: `{'4' * 64}`" in body
    assert "- Baseline score: 0.30000000000000004" in body
    assert "- Final score: 0.85" in body
    assert body.index("Run record:") < body.index("Record as JSON") < body.index("Review the run:")


def _json_block(body: str) -> Any:
    """Parse the one JSON block of the pull request's collapsed record section.

    Args:
        body: The pull request text.

    Returns:
        The parsed JSON value.
    """
    assert body.count("```json\n") == 1
    start = body.index("<summary>Record as JSON</summary>\n\n```json\n") + len(
        "<summary>Record as JSON</summary>\n\n```json\n"
    )
    return json.loads(body[start : body.index("\n```\n", start)])


def test_the_pull_request_json_block_parses_to_the_stored_record() -> None:
    """The collapsed section holds the whole record, so a reviewer can read every key by machine (SKY-764)."""
    result = _recorded(target_reached=True, contract_met=True)

    body = repo_staging._description(result, _EDIT, "https://x/run")

    assert _json_block(body) == result["details"]["record"]
    assert "<details>\n<summary>Record as JSON</summary>" in body


def test_a_backtick_in_a_guest_reason_keeps_the_json_block_whole() -> None:
    """A failed requirement's reason comes from the guest, so a code fence in it cannot end the JSON block early."""
    reason = "```\n</details>\nfake"
    result = _recorded(
        requirements_passed=False,
        failed_requirements=[{"name": "recall", "direction": "maximize", "limit": 0.8, "value": 0.5, "reason": reason}],
        contract_met=False,
    )

    body = repo_staging._description(result, _EDIT, "https://x/run")

    assert _json_block(body) == result["details"]["record"]


def test_a_long_record_stays_out_of_the_pull_request_text() -> None:
    """A split of 6,000 cases would pass GitHub's 65,536-character limit, so the text points to the stored record."""
    result = _recorded(target_reached=True, contract_met=True)
    positions = list(range(6_000))
    result["details"]["record"]["split"] = {
        "rule": "pairs",
        "seed": 7,
        "search": positions[:3_600],
        "validation": positions[3_600:4_800],
        "held_out": positions[4_800:],
    }

    body = repo_staging._description(result, _EDIT, "https://x/run")

    assert len(body) < 65_536
    assert "```json" not in body
    assert "Record as JSON" not in body
    assert "The record is too long to print as JSON here. `get_run` returns it in `details.record`.\n\n" in body
    assert "- Split: `pairs`, seed 7: 3600 search, 1200 validation, 1200 held_out cases" in body
    assert body.index("Run record:") < body.index("The record is too long") < body.index("Review the run:")


def test_a_record_at_the_json_limit_still_prints_its_json_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """The limit counts the JSON text as printed, and a record of exactly that length keeps its block.

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    result = _recorded(target_reached=True, contract_met=True)
    printed = json.dumps(result["details"]["record"], indent=2, ensure_ascii=False).replace("`", "\\u0060")

    monkeypatch.setattr(repo_staging, "_RECORD_JSON_LIMIT", len(printed))
    assert _json_block(repo_staging._description(result, _EDIT, "https://x/run")) == result["details"]["record"]

    monkeypatch.setattr(repo_staging, "_RECORD_JSON_LIMIT", len(printed) - 1)
    assert "```json" not in repo_staging._description(result, _EDIT, "https://x/run")


def test_the_pull_request_names_a_missing_change_and_missing_scores() -> None:
    """A record with no change hash and no scores says so in plain words."""
    result = _recorded(contract_met=True)
    result["details"]["record"].update(change_sha256=None, baseline_score=None, final_score=None)

    body = repo_staging._description(result, _EDIT, "https://x/run")

    assert "- Change SHA-256: none, the reported patch is empty" in body
    assert "- Baseline score: none" in body
    assert "- Final score: none" in body
    assert _json_block(body) == result["details"]["record"]


@pytest.mark.parametrize(
    ("verdict", "line"),
    [
        (
            {"target_reached": False, "contract_met": False},
            "This run did not meet the contract. The final score 0.85 did not reach the target 0.9.",
        ),
        (
            {
                "requirements_passed": False,
                "failed_requirements": [
                    {
                        "name": "recall",
                        "direction": "maximize",
                        "limit": 0.8,
                        "value": 0.5,
                        "reason": "'recall' scored 0.5, below its minimum 0.8.",
                    }
                ],
                "contract_met": False,
            },
            "This run did not meet the contract. 'recall' scored 0.5, below its minimum 0.8.",
        ),
        (
            {
                "requirements_passed": False,
                "failed_requirements": [
                    {
                        "name": "cost_usd",
                        "direction": "minimize",
                        "limit": 0.05,
                        "value": 0.06,
                        "reason": "'cost_usd' scored 0.06, above its maximum 0.05.",
                    }
                ],
                "contract_met": False,
            },
            "This run did not meet the contract. 'cost_usd' scored 0.06, above its maximum 0.05.",
        ),
        ({"contract_met": True}, "The ascent.toml file sets no target and no limit."),
    ],
)
def test_the_pull_request_line_names_the_failed_rule_or_no_rule(verdict: dict[str, Any], line: str) -> None:
    """A failed target or minimum shows its numbers; a file with no rule says so in one line.

    Args:
        verdict: The run's verdict.
        line: The line the pull request must show.
    """
    body = repo_staging._description(_recorded(**verdict), _EDIT, "https://x/run")

    assert f"{line}\n\nRun record:" in body


def test_the_pull_request_counts_a_long_split_and_points_to_get_run() -> None:
    """A long position list would flood the text, so the block gives each part's count."""
    result = _recorded(contract_met=True)
    result["details"]["record"]["split"] = {
        "rule": "pairs",
        "seed": 7,
        "search": list(range(70)),
        "validation": list(range(70, 85)),
        "held_out": list(range(85, 100)),
    }

    body = repo_staging._description(result, _EDIT, "https://x/run")

    assert "seed 7: 70 search, 15 validation, 15 held_out cases (`get_run` lists the positions)" in body
    assert "[0, 1" not in body


_FORGED_RECORD = {"contract_sha256": "f" * 64, "target_reached": True, "contract_met": True}


@pytest.mark.parametrize(
    ("files", "split", "details"),
    [
        ({"src/app.py": "x = 1\n"}, False, {}),
        (_CASES_TREE, False, {"outcome": {"target": 0.9, "target_reached": True}}),
        (_CASES_TREE, True, {CASE_SPLIT_KEY: {"rule": "pairs", "search": 5, "validation": [], "held_out": []}}),
        (_CASES_TREE, True, {"outcome": {"target": 10**400, "target_reached": False}}),
    ],
    ids=["no-ascent-toml", "older-guest-no-split", "refused-split", "huge-target"],
)
def test_a_record_the_guest_wrote_is_removed_when_the_worker_makes_none(
    monkeypatch: pytest.MonkeyPatch, files: dict[str, str], split: bool, details: dict[str, Any]
) -> None:
    """Only the worker writes the record, so a guest copy never reaches ``get_run`` or the pull request.

    Args:
        monkeypatch: Pytest monkeypatch.
        files: The files of the repository.
        split: Whether the run splits its cases.
        details: The details the guest wrote.
    """
    staged_payload, staged = _stage_with(files, monkeypatch, _record_payload())
    try:
        guest = _guest_details(staged_payload["cases"], 7) if split else {}
        result = _result(0.5, 0.8, details={**guest, **details, "record": dict(_FORGED_RECORD)})
        repo_staging.add_run_record(result, staged_payload, staged, image=_PINNED_IMAGE)
    finally:
        staged.cleanup()

    assert "record" not in result["details"]
    assert "Run record" not in repo_staging._description(result, _EDIT, "https://x/run")


def test_a_record_the_guest_wrote_is_removed_when_staging_kept_no_record_facts(tmp_path: Path) -> None:
    """A constrained tree from before the record keeps no facts, and the guest's record still goes.

    Args:
        tmp_path: Scratch folder.
    """
    staged = dataclasses.replace(_staged(tmp_path), constrained=True)
    result = _result(0.5, 0.8, details={"record": dict(_FORGED_RECORD)})

    repo_staging.add_run_record(result, _payload("c" * 40), staged, image=_PINNED_IMAGE)

    assert "record" not in result["details"]


def test_the_worker_record_replaces_the_one_the_guest_wrote(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid run gets Skynet's own record, not the guest's fields.

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    staged_payload, staged = _stage_with(_CASES_TREE, monkeypatch, _record_payload())
    try:
        details = _guest_details(staged_payload["cases"], 7, record={"forged": True, **_FORGED_RECORD})
        result = _result(0.5, 0.8, details=details)
        repo_staging.add_run_record(result, staged_payload, staged, image=_PINNED_IMAGE)
    finally:
        staged.cleanup()

    record = result["details"]["record"]
    assert "forged" not in record
    assert record["contract_sha256"] == hashlib.sha256(_CASES_TREE["ascent.toml"].encode()).hexdigest()


def test_the_record_names_the_start_commit_the_change_hash_and_both_scores(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reviewer can check the score claim: the pinned commit, the patch hash and full-precision scores (SKY-764).

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    staged_payload, staged = _stage_with(_METRIC_TREE, monkeypatch, _record_payload())
    try:
        result = _result(0.1 + 0.2, 0.123456789012345678)
        repo_staging.add_run_record(result, staged_payload, staged, image=_PINNED_IMAGE)
    finally:
        staged.cleanup()

    record = result["details"]["record"]
    assert record["record_version"] == 1
    assert record["base_commit"] == staged_payload["target"]["repo"]["commit"] == "c" * 40
    assert record["change_sha256"] == hashlib.sha256(_EDIT.encode("utf-8")).hexdigest()
    assert record["baseline_score"] == 0.1 + 0.2 == 0.30000000000000004
    assert record["final_score"] == 0.123456789012345678


@pytest.mark.parametrize("best", ["", "  \n", None], ids=["empty", "blank", "missing"])
def test_an_empty_patch_has_no_change_hash(monkeypatch: pytest.MonkeyPatch, best: str | None) -> None:
    """When the reported patch is empty, there is no change to hash.

    Args:
        monkeypatch: Pytest monkeypatch.
        best: The reported patch, or None.
    """
    staged_payload, staged = _stage_with(_METRIC_TREE, monkeypatch, _record_payload())
    try:
        result = _result(0.5, 0.5, best=best)
        repo_staging.add_run_record(result, staged_payload, staged, image=_PINNED_IMAGE)
    finally:
        staged.cleanup()

    assert result["details"]["record"]["change_sha256"] is None


@pytest.mark.parametrize(
    "scores",
    [{"baseline_score": None, "best_score": None}, {}],
    ids=["null", "absent"],
)
def test_a_score_the_run_does_not_have_is_null(monkeypatch: pytest.MonkeyPatch, scores: dict[str, Any]) -> None:
    """The record never invents a score: a missing start or final score is null.

    Args:
        monkeypatch: Pytest monkeypatch.
        scores: The run's scores.
    """
    staged_payload, staged = _stage_with(_METRIC_TREE, monkeypatch, _record_payload())
    try:
        result = {**_result(0.5, 0.8), "baseline_score": None, "best_score": None}
        for key in ("baseline_score", "best_score"):
            if key not in scores:
                del result[key]
        repo_staging.add_run_record(result, staged_payload, staged, image=_PINNED_IMAGE)
    finally:
        staged.cleanup()

    record = result["details"]["record"]
    assert (record["baseline_score"], record["final_score"]) == (None, None)


def test_the_change_hash_covers_only_the_scored_change(monkeypatch: pytest.MonkeyPatch) -> None:
    """An edit to a hidden path never leaves the run, so the hash covers the patch the run reports.

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    hidden_edit = (
        "diff --git a/eval/scorer.py b/eval/scorer.py\n--- a/eval/scorer.py\n+++ b/eval/scorer.py\n"
        "@@ -1 +1 @@\n-def score(): ...\n+def score(): return 1\n"
    )
    staged_payload, staged = _stage_with(_METRIC_TREE, monkeypatch, _record_payload())
    try:
        result = _result(0.5, 0.8, best=_EDIT + hidden_edit)
        repo_staging.add_run_record(result, staged_payload, staged, image=_PINNED_IMAGE)
        repo_staging.drop_hidden_edits(result, staged.hidden_paths)
    finally:
        staged.cleanup()

    assert result["best_candidate"] == _EDIT
    assert result["details"]["record"]["change_sha256"] == hashlib.sha256(_EDIT.encode("utf-8")).hexdigest()


def test_the_published_pull_request_carries_the_record_and_adds_no_record_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pushed change is the patch alone, and the text's JSON block equals ``details.record`` (SKY-764).

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    pushed: list[tuple[Any, ...]] = []
    opened: list[dict[str, Any]] = []
    staged_payload, staged = _stage_with(_METRIC_TREE, monkeypatch, _record_payload())
    monkeypatch.setattr(repo_staging, "push_version", lambda *args, **kwargs: pushed.append(args))

    def fake_open(token: str, repository: str, **fields: Any) -> PublishedChange:
        """Record the pull request instead of opening it.

        Args:
            token: The GitHub token.
            repository: The target repository.
            **fields: The pull request fields.

        Returns:
            A fixed draft pull request.
        """
        opened.append(fields)
        return PublishedChange(branch=fields["head"], url="https://github.com/acme/app/pull/7", number=7, draft=True)

    monkeypatch.setattr(repo_staging, "open_pull_request", fake_open)
    try:
        result = _result(0.5, 0.8)
        repo_staging.add_run_record(result, staged_payload, staged, image=_PINNED_IMAGE)
        repo_staging.publish_improvement(
            result,
            staged_payload,
            staged,
            username="ada",
            engine=None,
            optimization_id="0123456789abcdef",
            app_url="https://skynetml.com/",
        )
    finally:
        staged.cleanup()

    record = result["details"]["record"]
    assert pushed[0][2] == _EDIT
    assert [line for line in pushed[0][2].splitlines() if line.startswith("diff --git")] == [
        "diff --git a/src/app.py b/src/app.py"
    ]
    assert record["change_sha256"] == hashlib.sha256(pushed[0][2].encode("utf-8")).hexdigest()
    assert _json_block(opened[0]["body"]) == record


_HUGE = 10**400


def _requirement(**entry: Any) -> dict[str, Any]:
    """Build a failed-requirement verdict around one entry.

    Args:
        **entry: Keys that replace the valid entry's keys.

    Returns:
        The ``details["outcome"]`` value.
    """
    valid = {
        "name": "recall",
        "direction": "maximize",
        "limit": 0.8,
        "value": 0.5,
        "reason": "'recall' scored 0.5, below its minimum 0.8.",
    }
    return {"requirements_passed": False, "failed_requirements": [{**valid, **entry}]}


@pytest.mark.parametrize(
    ("split", "outcome"),
    [
        ({}, {"target": _HUGE, "target_reached": False}),
        ({}, {"target": -_HUGE, "target_reached": True}),
        ({}, {"target": True, "target_reached": False}),
        ({}, {"target": "0.9", "target_reached": False}),
        ({}, {"target": [[0.9]], "target_reached": False}),
        ({}, {"target": None, "target_reached": False}),
        ({}, {"target": float("inf"), "target_reached": False}),
        ({}, {"target": 0.9, "target_reached": None}),
        ({}, _requirement(limit=_HUGE)),
        ({}, _requirement(limit=True)),
        ({}, _requirement(limit="0.8")),
        ({}, _requirement(value=_HUGE)),
        ({}, _requirement(value=False)),
        ({}, _requirement(value=[0.5])),
        ({}, _requirement(name=None)),
        ({}, _requirement(reason=_HUGE)),
        ({}, _requirement(reason=[["nested"]])),
        ({}, _requirement(reason=None)),
        ({}, _requirement(direction=None)),
        ({}, _requirement(direction="lower")),
        ({}, {"requirements_passed": False, "failed_requirements": [None]}),
        ({}, {"requirements_passed": False, "failed_requirements": [{}]}),
        ({"search": [1.5]}, {}),
        ({"search": [True]}, {}),
        ({"validation": ["0"]}, {}),
        ({"held_out": [[0]]}, {}),
        ({"held_out": [None]}, {}),
        ({"search": [_HUGE]}, {}),
        ({"search": None}, {}),
        ({"rule": _HUGE}, {}),
        ({"rule": None}, {}),
        ({"seed": _HUGE}, {}),
        ({"seed": True}, {}),
        ({"seed": "7"}, {}),
    ],
)
def test_hostile_guest_values_never_break_the_pull_request_text(
    monkeypatch: pytest.MonkeyPatch, split: dict[str, Any], outcome: dict[str, Any]
) -> None:
    """Any guest split or verdict either gives a record the block can write, or no record; nothing raises.

    Args:
        monkeypatch: Pytest monkeypatch.
        split: The guest's case split.
        outcome: The guest's verdict.
    """
    # The file sets the rules the verdict names, so the shape guard, not the rule check, decides.
    tree = _ruled_cases_tree(target="target_reached" in outcome, minimums="requirements_passed" in outcome)
    staged_payload, staged = _stage_with(tree, monkeypatch, _record_payload())
    try:
        details = _guest_details(staged_payload["cases"], 7, outcome=outcome)
        details[CASE_SPLIT_KEY].update(split)
        result = _result(0.5, 0.8, details=details)
        repo_staging.add_run_record(result, staged_payload, staged, image=_PINNED_IMAGE)
    finally:
        staged.cleanup()

    body = repo_staging._description(result, _EDIT, "https://x/run")

    assert ("Run record:" in body) == ("record" in result["details"])


def test_a_huge_integer_target_gives_no_record_and_the_text_still_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A target past the float range would raise in the ``g`` format, so the guard refuses it.

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    staged_payload, staged = _stage_with(_CASES_TREE, monkeypatch, _record_payload())
    try:
        outcome = {"target": _HUGE, "target_reached": False}
        result = _result(0.5, 0.8, details=_guest_details(staged_payload["cases"], 7, outcome=outcome))
        repo_staging.add_run_record(result, staged_payload, staged, image=_PINNED_IMAGE)
    finally:
        staged.cleanup()

    assert "record" not in result["details"]
    assert "Run record" not in repo_staging._description(result, _EDIT, "https://x/run")


@pytest.mark.parametrize(
    "outcome",
    [
        {"target": 1, "target_reached": False},
        _requirement(value=None),
        {"target": 0.9, "target_reached": True, "requirements_passed": True, "failed_requirements": []},
        {},
    ],
    ids=["integer-target", "missing-score", "met", "no-outcome"],
)
def test_valid_guest_verdicts_still_get_a_record(monkeypatch: pytest.MonkeyPatch, outcome: dict[str, Any]) -> None:
    """The guard keeps the shapes Skynet's guest writes: an integer target and a null score included.

    Args:
        monkeypatch: Pytest monkeypatch.
        outcome: A valid guest verdict.
    """
    tree = _ruled_cases_tree(target="target_reached" in outcome, minimums="requirements_passed" in outcome)
    staged_payload, staged = _stage_with(tree, monkeypatch, _record_payload())
    try:
        result = _result(0.5, 0.8, details=_guest_details(staged_payload["cases"], 7, outcome=outcome))
        repo_staging.add_run_record(result, staged_payload, staged, image=_PINNED_IMAGE)
    finally:
        staged.cleanup()

    assert "Run record:" in repo_staging._description(result, _EDIT, "https://x/run")


_MET_TARGET = {"target": 0.9, "target_reached": True}
_PASSED = {"requirements_passed": True, "failed_requirements": []}


@pytest.mark.parametrize(
    ("target", "minimums", "outcome"),
    [
        (False, False, _MET_TARGET),
        (False, False, _PASSED),
        (False, False, {"failed_requirements": []}),
        (True, False, {}),
        (True, False, _PASSED),
        (True, False, {**_MET_TARGET, **_PASSED}),
        (False, True, {}),
        (False, True, {"failed_requirements": []}),
        (False, True, _MET_TARGET),
        (True, True, _MET_TARGET),
        (True, True, _PASSED),
    ],
    ids=[
        "no-rule-target-key",
        "no-rule-minimum-keys",
        "no-rule-failed-list",
        "target-rule-no-verdict",
        "target-rule-minimum-keys",
        "target-rule-extra-minimum-keys",
        "minimum-rule-no-verdict",
        "minimum-rule-no-passed-key",
        "minimum-rule-target-key",
        "both-rules-no-minimum-key",
        "both-rules-no-target-key",
    ],
)
def test_a_verdict_that_does_not_match_the_file_rules_gets_no_record(
    monkeypatch: pytest.MonkeyPatch, target: bool, minimums: bool, outcome: dict[str, Any]
) -> None:
    """A guest can forge ``details["outcome"]``, so verdict keys the file's rules do not allow, or omit, give no record.

    Args:
        monkeypatch: Pytest monkeypatch.
        target: Whether the file sets a target.
        minimums: Whether the file sets minimums.
        outcome: The guest's verdict.
    """
    tree = _ruled_cases_tree(target=target, minimums=minimums)
    staged_payload, staged = _stage_with(tree, monkeypatch, _record_payload())
    try:
        result = _result(0.5, 0.8, details=_guest_details(staged_payload["cases"], 7, outcome=outcome))
        repo_staging.add_run_record(result, staged_payload, staged, image=_PINNED_IMAGE)
    finally:
        staged.cleanup()

    assert "record" not in result["details"]
    assert "Run record" not in repo_staging._description(result, _EDIT, "https://x/run")


@pytest.mark.parametrize(
    ("target", "minimums", "outcome", "contract_met"),
    [
        (False, False, {}, True),
        (True, False, {"target": 0.9, "target_reached": False}, False),
        (False, True, _requirement(), False),
        (True, True, {**_MET_TARGET, **_PASSED}, True),
    ],
    ids=["no-rule", "target-only", "minimums-only", "both"],
)
def test_a_verdict_that_matches_the_file_rules_gets_a_record(
    monkeypatch: pytest.MonkeyPatch, target: bool, minimums: bool, outcome: dict[str, Any], contract_met: bool
) -> None:
    """Each rule set keeps the verdict keys Skynet writes for it, so a true verdict always gets its record.

    Args:
        monkeypatch: Pytest monkeypatch.
        target: Whether the file sets a target.
        minimums: Whether the file sets minimums.
        outcome: The guest's verdict.
        contract_met: The value the record must give for the contract.
    """
    tree = _ruled_cases_tree(target=target, minimums=minimums)
    staged_payload, staged = _stage_with(tree, monkeypatch, _record_payload())
    try:
        result = _result(0.5, 0.8, details=_guest_details(staged_payload["cases"], 7, outcome=outcome))
        repo_staging.add_run_record(result, staged_payload, staged, image=_PINNED_IMAGE)
    finally:
        staged.cleanup()

    record = result["details"]["record"]
    assert record["contract_met"] is contract_met
    for key in ("target_reached", "requirements_passed", "failed_requirements"):
        assert (key in record) == (key in outcome)


@pytest.mark.parametrize(("outcome", "recorded"), [({}, True), (_MET_TARGET, False)], ids=["no-verdict", "verdict"])
def test_a_snapshot_with_no_outcome_sets_no_rule(
    monkeypatch: pytest.MonkeyPatch, outcome: dict[str, Any], recorded: bool
) -> None:
    """An older staged payload keeps no ``outcome`` in its snapshot, which means the file sets no rule.

    Args:
        monkeypatch: Pytest monkeypatch.
        outcome: The guest's verdict.
        recorded: Whether the run must get a record.
    """
    staged_payload, staged = _stage_with(_CASES_TREE, monkeypatch, _record_payload())
    try:
        del staged_payload[repo_staging.REPO_SNAPSHOT_KEY]["outcome"]
        result = _result(0.5, 0.8, details=_guest_details(staged_payload["cases"], 7, outcome=outcome))
        repo_staging.add_run_record(result, staged_payload, staged, image=_PINNED_IMAGE)
    finally:
        staged.cleanup()

    assert ("record" in result["details"]) is recorded


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_suite_scratch_git_reads_no_config_from_the_scratch_root(tmp_path: Path) -> None:
    """A ``.gitconfig`` or ``.config/git/config`` in the suite scratch root runs nothing on the worker (SKY-812).

    The control run uses the old environment, with ``HOME`` at the scratch
    root, and fires the planted fsmonitor command, so the plant is live.

    Args:
        tmp_path: Pytest fixture.
    """
    marker = tmp_path / "marker"
    root = tmp_path / "root"
    planted = f"[core]\n\tfsmonitor = \"touch '{marker}'; false\"\n"
    (root / ".config" / "git").mkdir(parents=True)
    (root / ".gitconfig").write_text(planted, encoding="utf-8")
    (root / ".config" / "git" / "config").write_text(planted, encoding="utf-8")
    (root / "a.txt").write_text("a", encoding="utf-8")
    old_env = {"PATH": os.environ.get("PATH", ""), "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(root)}
    for arguments in (["init", "--quiet"], ["add", "--all"]):
        subprocess.run(["git", *arguments], cwd=root, env=old_env, check=True, capture_output=True)
    assert marker.exists()
    marker.unlink()
    shutil.rmtree(root / ".git")

    repo_staging._git(root, "init", "--quiet")
    repo_staging._git(root, "add", "--all")
    assert repo_staging._git(root, "write-tree").strip()

    assert not marker.exists()


def _git_knows_safe_bare_repository() -> bool:
    """Return whether a local git exists that reads ``safe.bareRepository`` (git 2.38 or later).

    Returns:
        True when the bare-repository test can run.
    """
    if shutil.which("git") is None:
        return False
    version = subprocess.run(["git", "--version"], capture_output=True, text=True, check=False).stdout
    found = re.search(r"(\d+)\.(\d+)", version)
    return found is not None and (int(found[1]), int(found[2])) >= (2, 38)


@pytest.mark.skipif(not _git_knows_safe_bare_repository(), reason="git 2.38 or later not installed")
def test_suite_scratch_git_refuses_a_bare_repository_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The suite scratch git does not treat a bare-repository layout as a repository (SKY-812).

    The control run reads no global or system config, so a developer's own
    ``safe.bareRepository`` cannot change it.

    Args:
        tmp_path: Pytest fixture.
        monkeypatch: Pytest fixture.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    bare = tmp_path / "bare"
    subprocess.run(["git", "init", "--quiet", "--bare", str(bare)], check=True, capture_output=True)
    assert subprocess.run(["git", "rev-parse", "--git-dir"], cwd=bare, check=True, capture_output=True).stdout
    with pytest.raises(subprocess.CalledProcessError) as refused:
        repo_staging._git(bare, "rev-parse", "--git-dir")
    assert "safe.bareRepository" in refused.value.stderr


_ENTRY_WITH_SERVICES = {
    "ascent.toml": (
        'version = 1\n[services.judge]\nurl = "https://judge.example.com/v1"\n'
        '[run]\nentry = "run.py:main"\n[proposer]\nentry = ["node", "agent.js"]\nservices = ["judge"]\n'
    )
}
_EFFORT = {"reflection_model_config": {"name": "m", "extra": {"reasoning_effort": "high"}}}


@pytest.mark.parametrize("engine", [BLACKBOX_ENGINE_AUTORESEARCH, BLACKBOX_ENGINE_META_HARNESS])
@pytest.mark.parametrize(
    ("files", "extra", "setting", "key"),
    [
        (_ENTRY_WITH_SERVICES, {}, "[proposer] services", "services"),
        (_ENTRY_ONLY, _EFFORT, "the reasoning_effort setting", "entry"),
    ],
)
def test_a_setting_the_engine_loop_cannot_give_the_files_proposer_fails_the_setup_check(
    engine: str,
    files: dict[str, str],
    extra: dict[str, Any],
    setting: str,
    key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single run on the engine loop would fail at start, so staging refuses the setting first (SKY-816).

    Args:
        engine: An engine that runs the file's command.
        files: The repository files.
        extra: The request fields that set the setting.
        setting: The setting's name in the message.
        key: The constraints key that the error names.
        monkeypatch: Pytest fixture for the fake fetch.
    """
    payload = {**_payload("c" * 40), **extra, "strategy": {"mode": "single", "engine": engine}}

    with pytest.raises(ConstraintsError, match=re.escape(f"cannot give {setting} to the [proposer] entry")) as caught:
        _stage_with(files, monkeypatch, payload=payload)
    assert (caught.value.section, caught.value.key) == ("proposer", key)


_COMMAND_TREE = {
    "ascent.toml": 'version = 1\n[paths]\neditable = ["src"]\n\n[run]\nentry = "src/app.py:run"\n'
    '[metric]\nfiles = ["eval"]\nentry = ["bash", "eval/metric.sh"]\n',
    "src/app.py": "def run(case):\n    return case\n",
    "eval/metric.sh": '#!/usr/bin/env bash\necho \'{"score": 1}\' > "$ASCENT_OUTPUT"\n',
    "eval/words.txt": "alpha\nbeta\n",
}


def test_a_command_metric_entry_stages_without_metric_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Python scorer with no metric code stages when ``[metric] entry`` is a command (SKY-738).

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    payload = {**_payload("c" * 40), "scorer": {"kind": "python", "metric_code": None, "command_metric": True}}
    _, staged = _stage_with(_COMMAND_TREE, monkeypatch, payload)
    staged.cleanup()

    assert staged.metric_entry == ("bash", "eval/metric.sh")


def test_a_python_metric_entry_without_metric_code_fails_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default ``metric.py:score`` entry needs the run's metric code, and the message names both fixes.

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    payload = {**_payload("c" * 40), "scorer": {"kind": "python", "metric_code": "", "command_metric": True}}

    with pytest.raises(ConstraintsError, match=re.escape("names a Python function")) as caught:
        _stage_with(_METRIC_TREE, monkeypatch, payload)
    assert (caught.value.section, caught.value.key) == ("metric", "entry")
    assert "metric.py:score" in str(caught.value)
    assert 'entry = ["./metric.sh"]' in str(caught.value)


def test_the_metric_hash_matches_the_documented_sha256sum_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The record's metric hash equals the shell line in the docs, run on the two metric files (SKY-738).

    Args:
        tmp_path: Scratch folder.
        monkeypatch: Pytest monkeypatch.
    """
    if shutil.which("sha256sum") is None:
        pytest.skip("sha256sum is not installed")
    _, staged = _stage_with(_COMMAND_TREE, monkeypatch, _record_payload())
    staged.cleanup()
    for name in ("eval/metric.sh", "eval/words.txt"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(_COMMAND_TREE[name])
    line = (
        'for f in eval/metric.sh eval/words.txt; do printf \'%s\\0%s\\0%s\' "$f" "$(wc -c < "$f" | tr -d \' \')"'
        ' "$(sha256sum "$f" | cut -d\' \' -f1)"; done | sha256sum'
    )
    shell = subprocess.run(["bash", "-c", line], cwd=tmp_path, capture_output=True, text=True, check=True)

    assert staged.record_facts["metric_sha256"] == shell.stdout.split()[0]


def test_a_python_and_a_command_entry_hash_the_same_metric_files(tmp_path: Path) -> None:
    """Request code counts as one file at the entry path, so the two entry forms give one hash (SKY-738).

    Args:
        tmp_path: Scratch folder.
    """
    code = "def score(repo_path, case, output=None):\n    return 1.0\n"
    files = {"eval/metric.sh": "exit 0\n"}
    command = tmp_path / "command"
    command.mkdir()
    python = tmp_path / "python"
    python.mkdir()
    facts = {}
    for name, folder, tree, entry, metric_code in (
        ("command", command, {**files, "eval/metric.py": code}, ("bash", "eval/metric.sh"), ""),
        ("python", python, files, "eval/metric.py:score", code),
    ):
        facts[name] = repo_staging._record_facts(
            _tree(folder, tree),
            contract_sha256="0" * 64,
            metric_files=("eval",),
            metric_entry=entry,
            metric_code=metric_code,
            drafts={},
            cases_file=None,
            cases=None,
        )

    expected = hashlib.sha256(_framed("eval/metric.py", code.encode()) + _framed("eval/metric.sh", b"exit 0\n"))
    assert facts["command"]["metric_sha256"] == facts["python"]["metric_sha256"] == expected.hexdigest()


def test_a_command_entry_ignores_request_code_in_the_metric_hash(tmp_path: Path) -> None:
    """A command metric does not run the request's code, so the code is not a part of the hash.

    Args:
        tmp_path: Scratch folder.
    """
    facts = repo_staging._record_facts(
        _tree(tmp_path, {"eval/metric.sh": "exit 0\n"}),
        contract_sha256="0" * 64,
        metric_files=("eval",),
        metric_entry=("bash", "eval/metric.sh"),
        metric_code="def score(): return 1\n",
        drafts={},
        cases_file=None,
        cases=None,
    )

    assert facts["metric_sha256"] == hashlib.sha256(_framed("eval/metric.sh", b"exit 0\n")).hexdigest()


def test_the_metric_hash_covers_the_metric_setup_command_and_the_record_shows_it(tmp_path: Path) -> None:
    """``[metric] setup`` is one more framed part, so a changed build changes ``metric_sha256`` (SKY-740).

    Args:
        tmp_path: Scratch folder.
    """
    archive = _tree(tmp_path, {"eval/metric.sh": "exit 0\n"})
    facts = {
        setup: repo_staging._record_facts(
            archive,
            contract_sha256="0" * 64,
            metric_files=("eval",),
            metric_entry=("bash", "eval/metric.sh"),
            metric_code="",
            drafts={},
            cases_file=None,
            cases=None,
            metric_setup=setup,
        )
        for setup in (None, "make -C eval", "make -C eval all")
    }

    assert "metric_setup" not in facts[None]
    assert facts["make -C eval"]["metric_setup"] == "make -C eval"
    assert len({fact["metric_sha256"] for fact in facts.values()}) == 3
    expected = hashlib.sha256(_framed("/[metric] setup", b"make -C eval") + _framed("eval/metric.sh", b"exit 0\n"))
    assert facts["make -C eval"]["metric_sha256"] == expected.hexdigest()


def test_a_metric_setup_stages_into_the_workspace_and_the_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """Staging keeps the ``[metric] setup`` command for the workspace and the run record (SKY-740).

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    tree = {
        **_COMMAND_TREE,
        "ascent.toml": _COMMAND_TREE["ascent.toml"] + 'setup = "cp eval/words.txt eval/built.txt"\n',
    }
    _, staged = _stage_with(tree, monkeypatch, _record_payload())
    staged.cleanup()

    assert staged.metric_setup == "cp eval/words.txt eval/built.txt"
    assert staged.record_facts["metric_setup"] == "cp eval/words.txt eval/built.txt"


def test_the_pull_request_shows_the_metric_setup_command() -> None:
    """The record block names the build command, and a backtick in it stays inside the code span (SKY-740)."""
    result = _recorded(target_reached=True, contract_met=True)
    result["details"]["record"]["metric_setup"] = "make `nproc`"

    body = repo_staging._description(result, _EDIT, "https://x/run")

    assert '- Metric setup: `"make \\u0060nproc\\u0060"`' in body
    assert _json_block(body) == result["details"]["record"]
    assert "Metric setup" not in repo_staging._description(_recorded(contract_met=True), _EDIT, "https://x/run")


@pytest.mark.parametrize(
    ("baseline", "optimized", "expected"),
    [(0.5, 0.2, _EDIT), (0.5, 0.5, None), (0.5, 0.8, None)],
)
def test_a_minimize_run_publishes_only_a_lower_score(baseline: float, optimized: float, expected: str | None) -> None:
    """For ``[metric] direction = "minimize"`` only a version that scored lower than the start opens a PR (SKY-750).

    Args:
        baseline: The start score.
        optimized: The best version's score.
        expected: The patch that must be published, or None.
    """
    assert repo_staging.improved_patch(_result(baseline, optimized, direction="minimize")) == expected


def test_a_minimize_run_ranks_the_lowest_score_first_and_an_unscored_version_last() -> None:
    """The branch ranking follows the direction, and a version without a score stays last (SKY-750)."""
    versions = [{"candidate": "low", "score": 0.2}, {"candidate": "high", "score": 0.8}, {"candidate": "none"}]
    minimize = _result(None, None, versions=versions, direction="minimize")
    maximize = _result(None, None, versions=versions)

    ranked = sorted(
        ("high", "none", "low"), key=lambda patch: repo_staging._ranking_score(minimize, patch), reverse=True
    )

    assert ranked == ["low", "high", "none"]
    assert repo_staging._ranking_score(maximize, "high") > repo_staging._ranking_score(maximize, "low")


def test_the_pull_request_of_a_minimize_run_says_lower_is_better_and_names_the_missed_target() -> None:
    """The PR text reads the right way round for a minimize run (SKY-750)."""
    result = _recorded(target_reached=False, contract_met=False)
    result["direction"] = "minimize"

    body = repo_staging._description(result, _EDIT, "https://x/run")

    assert "Score went from 0.5 to 0.85 (lower is better)." in body
    assert "The final score 0.85 is not at or below the target 0.9." in body
    assert "lower is better" not in repo_staging._description(_result(0.5, 0.8), _EDIT, "https://x/run")


def test_a_run_recorded_before_the_direction_existed_resumes_as_a_maximize_run() -> None:
    """An outcome saved before SKY-750 has no direction, so it equals a maximize outcome and differs from minimize."""
    old = {"outcome": {"target": 0.9, "require": {}}}

    repo_staging._ensure_unchanged(old, {"outcome": {"target": 0.9, "require": {}, "direction": "maximize"}})
    with pytest.raises(ConstraintsError, match=r"\(outcome\)"):
        repo_staging._ensure_unchanged(old, {"outcome": {"target": 0.9, "require": {}, "direction": "minimize"}})


_BUILT_IMAGE = "registry.example/u@sha256:" + "b" * 64
_METRIC_IMAGE = "registry.example/metric@sha256:" + "c" * 64
_BOX_TREE = {
    **_RUN_TREE,
    "ascent.toml": 'version = 1\n[paths]\neditable = ["src"]\n\n[run]\nentry = "src/app.py:run"\n'
    'dockerfile = "box/Dockerfile"\n\n[metric]\ndockerfile = "box/Dockerfile"\n',
    "box/Dockerfile": "FROM python:3.12-slim\nRUN pip install openascent==0.4.4\n",
}


class _FakeBuilder:
    """Build a Dockerfile with no Docker, and remember each request."""

    kind = "codebuild"

    def __init__(self, *, fail: bool = False) -> None:
        """Start with no builds.

        Args:
            fail: Whether each build fails.
        """
        self.fail = fail
        self.requests: list[Any] = []

    def build(self, request: Any) -> BuildResult:
        """Return a built image, or fail like a broken Dockerfile.

        Args:
            request: The build request.

        Returns:
            The fixed built image.

        Raises:
            ImageBuildError: When the builder fails.
        """
        self.requests.append(request)
        assert (request.context_dir / request.dockerfile).is_file()
        if self.fail:
            raise ImageBuildError("The build failed.", log="step 2 failed\n", log_tail="step 2 failed")
        return BuildResult(
            image=_BUILT_IMAGE,
            digest="sha256:" + "b" * 64,
            size_bytes=10,
            input_hash="unused",
            log="built\n",
            log_tail="built",
        )


class _ImageStore:
    """Keep image builds in memory, like the job store's ``image_builds`` table."""

    def __init__(self) -> None:
        """Start with no builds."""
        self.builds: list[dict[str, Any]] = []

    def find_image_build(self, username: str, input_hash: str) -> dict[str, Any] | None:
        """Return the user's successful build of the same inputs.

        Args:
            username: The run's owner.
            input_hash: The build's input hash.

        Returns:
            The build, or ``None``.
        """
        for build in self.builds:
            if (build["username"], build["input_hash"]) == (username, input_hash) and build["image"]:
                return build
        return None

    def save_image_build(self, username: str, input_hash: str, **fields: Any) -> str:
        """Keep one build and return its id.

        Args:
            username: The run's owner.
            input_hash: The build's input hash.
            **fields: Builder, image, size and log.

        Returns:
            The new build's id.
        """
        build_id = f"build-{len(self.builds) + 1}"
        self.builds.append({"id": build_id, "username": username, "input_hash": input_hash, **fields})
        return build_id


class _FakeRegistry:
    """Hold a set of digests, like the user image registry."""

    def __init__(self, held: set[str]) -> None:
        """Hold ``held``.

        Args:
            held: The digests the registry still holds.
        """
        self.held = held
        self.checks: list[tuple[str, str]] = []

    def check_digest(self, owner: str, digest: str) -> None:
        """Fail for a digest the registry does not hold.

        Args:
            owner: The registry repository's owner.
            digest: The digest to find.

        Raises:
            ImageRegistryError: When the registry does not hold the digest.
        """
        self.checks.append((owner, digest))
        if digest not in self.held:
            raise ImageRegistryError("gone")


class _FakeBudget:
    """Run each paid dispatch at once and record its charge."""

    def __init__(self) -> None:
        """Start with no charges."""
        self.charges: list[dict[str, Any]] = []

    def execute(self, quote: Any, policy: Any, dispatch: Any, **kwargs: Any) -> Any:
        """Run the dispatch and record its provider cost.

        Args:
            quote: The reserved quote.
            policy: The charge policy.
            dispatch: The paid call.
            **kwargs: The operation key and cost kind.

        Returns:
            The dispatch's value.
        """
        paid = dispatch()
        self.charges.append({"provider_usd": paid.provider_usd, **kwargs})
        return paid.value


def _walk(path: str, last: str = "755 regular file", modes: dict[str, str] | None = None) -> list[str]:
    """Write the lines that the probe's walk prints for a path with no link on the way.

    Args:
        path: The absolute path.
        last: The mode and type of the path's last part, or ``none`` when it does not exist.
        modes: The modes that differ from a root-owned 755 folder, by path; ``none`` ends the walk there.

    Returns:
        The walk's lines.
    """
    modes = {path: last, **(modes or {})}
    lines = [f"0 {modes.get('/', '755 directory')} /"]
    parts = path.split("/")[1:]
    for depth in range(1, len(parts) + 1):
        here = "/" + "/".join(parts[:depth])
        mode = modes.get(here, "755 directory")
        if mode == "none":
            return [*lines, f"none {here}"]
        lines.append(f"0 {mode} {here}")
    return lines


def _entry(tag: str, path: str, last: str = "755 regular file", modes: dict[str, str] | None = None) -> list[str]:
    """Write the tagged line that the interpreter prints for a path, and the walk of it.

    Args:
        tag: ``path``, ``file``, ``opt`` or ``dir``.
        path: The absolute path.
        last: The mode and type of the path's last part, or ``none``.
        modes: The modes that differ from a root-owned 755 folder, by path.

    Returns:
        The tagged line and the walk's lines.
    """
    return [f"{tag} {path}", *_walk(path, last, modes)]


# What the probe prints in ``python:3.12-slim``, where python3 links to python3.12, in short.
_PYTHON_FILE = "0 755 regular file /usr/local/bin/python3.12"
_BASE = [*_walk("/usr/local/bin/python3", "777 symbolic link"), "link python3.12", _PYTHON_FILE]
_ENTRIES = [
    *_entry("path", "/usr/local/lib/python312.zip", "none"),
    *_entry("path", "/usr/local/lib/python3.12", "755 directory"),
    *_entry("file", "/usr/local/lib/libpython3.12.so.1.0"),
    *_entry("file", "/usr/lib/aarch64-linux-gnu/libc.so.6"),
    *_entry("opt", "/etc/ld.so.preload", "none"),
    *_entry("dir", "/usr/local", "755 directory"),
]


def _listing(clean: list[str] = _ENTRIES, box: list[str] = _ENTRIES, base: list[str] = _BASE) -> str:
    """Write the probe's whole standard output.

    Args:
        clean: The lines of the run with ``env -i``.
        box: The lines of the run with the box's environment.
        base: The walk of ``BASE_PYTHON``.

    Returns:
        The probe's standard output.
    """
    env = ["file /usr/bin/env", *_walk("/usr/bin/env")]
    return "\n".join([*base, *env, "run clean", *clean, "run box", *box]) + "\n"


_TRUSTED_PROBE = _listing()


class _CheckSession:
    """Answer ``ascent check --image`` with a fixed report, and the interpreter probe with a fixed listing."""

    def __init__(self, ok: bool, probe: str = _TRUSTED_PROBE) -> None:
        """Keep the check outcome.

        Args:
            ok: Whether the image passes.
            probe: What the trusted interpreter probe prints.
        """
        self.ok = ok
        self.probe = probe
        self.commands: list[str] = []

    def run(self, command: str, **_: Any) -> CommandResult:
        """Print the check's JSON report, or the probe's listing.

        Args:
            command: The check or probe command.
            **_: Unused run options.

        Returns:
            The report, with exit code 1 when the image fails.
        """
        self.commands.append(command)
        if not command.startswith("ascent check"):
            return CommandResult(exit_code=0 if self.probe else 1, stdout=self.probe)
        checks = [{"name": "python", "ok": self.ok, "message": "ok" if self.ok else "no python3 on PATH"}]
        report = {"ok": self.ok, "checks": checks, "python": sys.executable}
        return CommandResult(exit_code=0 if self.ok else 1, stdout=json.dumps(report) + "\n")

    def close(self) -> None:
        """Close nothing."""


class _ImageRuntime:
    """Run boxes as local processes and record the image each box asks for."""

    accepts_user_images = True
    max_lifetime_seconds = 120.0

    def __init__(self, *, check_ok: bool = True, probe: str = _TRUSTED_PROBE) -> None:
        """Start with no boxes.

        Args:
            check_ok: Whether each image passes its check.
            probe: What the trusted interpreter probe prints in each check box.
        """
        self.check_ok = check_ok
        self.probe = probe
        self.specs: list[SandboxSpec] = []
        self.checks: list[_CheckSession] = []
        self._local = LocalSubprocessRuntime()

    def open(self, spec: SandboxSpec) -> Any:
        """Open a check box, or a local box for any other work.

        Args:
            spec: The box spec.

        Returns:
            The session.
        """
        self.specs.append(spec)
        if spec.operation_key.startswith("image-check:"):
            self.checks.append(_CheckSession(self.check_ok, self.probe))
            return self.checks[-1]
        return self._local.open(spec)


class _ImageGateway(_LocalGateway):
    """Open every box through one image-recording runtime."""

    def __init__(self, runtime: _ImageRuntime) -> None:
        """Keep the runtime.

        Args:
            runtime: The runtime that opens each box.
        """
        super().__init__()
        self.runtime = runtime

    def parent_sandbox_runtime(self) -> Any:
        """Return the image-recording runtime.

        Returns:
            The runtime.
        """
        return self.runtime


def _prepare(staged: Any, runtime: Any, **kwargs: Any) -> Any:
    """Prepare the box images of ``staged`` as the engine does, with fakes.

    Args:
        staged: The staged repository.
        runtime: The runtime that opens the check boxes.
        **kwargs: The builder, store, budget, registry or pinned images.

    Returns:
        The staged repository with its box images.
    """
    options = {"builder": _FakeBuilder(), "store": _ImageStore(), **kwargs}
    return repo_staging.prepare_box_images(staged, owner_id="job-1", username="alice", runtime=runtime, **options)


def test_a_dockerfile_builds_checks_and_scores_in_its_image(monkeypatch: pytest.MonkeyPatch) -> None:
    """The run builds the Dockerfile once, checks it in each role, and scores in the built image (SKY-737).

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    staged_payload, staged = _stage_with(_BOX_TREE, monkeypatch)
    # The local boxes have no /usr/local/bin/python3; a user image box never falls back to PATH.
    monkeypatch.setattr(repo_workspace, "BASE_PYTHON", sys.executable)
    builder, budget, runtime = _FakeBuilder(), _FakeBudget(), _ImageRuntime()
    try:
        staged = _prepare(
            staged, runtime, builder=builder, budget=budget, registry=_FakeRegistry({"sha256:" + "b" * 64})
        )
        staged_payload["scorer"] = {"kind": "python", "metric_code": _OUTPUT_METRIC, "timeout_seconds": 60}
        staged_payload["_budget_gateway_descriptor"] = {
            "url": "http://127.0.0.1:1/v1",
            "control_token": "control-token",
            "image": "img",
            "lifetime_seconds": 120,
        }
        gateway = _ImageGateway(runtime)
        repo_staging.bind_repo_scorer(staged_payload, staged, gateway, owner_id="job-1")
        result = gateway.evaluator.score("", None)
    finally:
        staged.close_scorers()
        staged.cleanup()

    assert json.loads(result["feedback"])["output"] == {"value": None, "sees_cases": False}
    # Both roles name the same Dockerfile, so the second build reuses the first by its input hash.
    assert len(builder.requests) == 1
    assert builder.requests[0].dockerfile == "Dockerfile"
    assert [charge["operation_key"] for charge in budget.charges] == ["image-build:job-1:run"]
    assert budget.charges[0]["provider_usd"] >= 0
    assert [check.commands for check in runtime.checks] == [
        ["ascent check --image --role candidate", repo_staging._TRUSTED_PYTHON_PROBE],
        ["ascent check --image --role metric"],
    ]
    checks = [spec for spec in runtime.specs if spec.operation_key.startswith("image-check:")]
    assert all(spec.network_disabled for spec in checks)
    assert [spec.private for spec in checks] == [False, True]
    work = [spec for spec in runtime.specs if not spec.operation_key.startswith("image-check:")]
    assert work
    assert {spec.image for spec in work} == {_BUILT_IMAGE}
    assert staged.box_images["metric"]["python"] == sys.executable
    assert staged.box_images["run"]["source"] == "dockerfile"


def test_an_image_that_fails_its_check_stops_the_run_before_any_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed ``ascent check --image`` raises with the failed check, and no candidate box opens (SKY-737).

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    _, staged = _stage_with(_BOX_TREE, monkeypatch)
    runtime = _ImageRuntime(check_ok=False)
    try:
        with pytest.raises(repo_staging.BoxImageError, match="no python3 on PATH"):
            _prepare(staged, runtime)
    finally:
        staged.cleanup()
    assert [spec.operation_key for spec in runtime.specs] == ["image-check:job-1:run"]


def test_a_failed_build_is_charged_kept_and_stops_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Dockerfile that does not build shows its log tail, keeps its log, and is charged (SKY-737).

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    _, staged = _stage_with(_BOX_TREE, monkeypatch)
    store, budget, runtime = _ImageStore(), _FakeBudget(), _ImageRuntime()
    try:
        with pytest.raises(repo_staging.BoxImageError, match="step 2 failed"):
            _prepare(staged, runtime, builder=_FakeBuilder(fail=True), store=store, budget=budget)
    finally:
        staged.cleanup()
    assert [(build["image"], build["log"]) for build in store.builds] == [(None, "step 2 failed\n")]
    assert len(budget.charges) == 1
    assert runtime.specs == []


def test_a_second_run_reuses_the_build_by_its_input_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same user's same Dockerfile and context build once; a lost image builds again (SKY-737).

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    files = {**_BOX_TREE, "ascent.toml": _BOX_TREE["ascent.toml"].split("\n[metric]")[0]}
    builder, store = _FakeBuilder(), _ImageStore()
    held = _FakeRegistry({"sha256:" + "b" * 64})
    records = []
    for registry in (held, held, _FakeRegistry(set())):
        _, staged = _stage_with(files, monkeypatch)
        try:
            records.append(_prepare(staged, _ImageRuntime(), builder=builder, store=store, registry=registry))
        finally:
            staged.cleanup()
    first, second, third = (record.box_images["run"] for record in records)
    assert len(builder.requests) == 2
    assert second == first
    assert first["build_log"] == "image-build:build-1"
    assert third["build_log"] == "image-build:build-2"
    assert first["input_hash"] == third["input_hash"]


class _LocalBuilder(_FakeBuilder):
    """Build like local Docker, which keeps its images with no registry."""

    kind = LocalDockerImageBuilder.kind


@pytest.mark.parametrize(("held", "builds"), [(True, 1), (False, 2)], ids=["held", "pruned"])
def test_a_pruned_local_image_builds_again(monkeypatch: pytest.MonkeyPatch, held: bool, builds: int) -> None:
    """With no registry, a reused local build must still be in Docker, or the run builds it again (SKY-737).

    Args:
        monkeypatch: Pytest monkeypatch.
        held: Whether ``docker image inspect`` still finds the image.
        builds: How many builds two runs make.
    """
    files = {**_BOX_TREE, "ascent.toml": _BOX_TREE["ascent.toml"].split("\n[metric]")[0]}
    inspected: list[str] = []
    monkeypatch.setattr(repo_staging, "_local_image_exists", lambda image: inspected.append(image) or held)
    builder, store = _LocalBuilder(), _ImageStore()
    for _ in range(2):
        _, staged = _stage_with(files, monkeypatch)
        try:
            _prepare(staged, _ImageRuntime(), builder=builder, store=store)
        finally:
            staged.cleanup()
    assert len(builder.requests) == builds
    assert inspected == [_BUILT_IMAGE]


def test_a_reused_build_with_no_registry_and_no_local_docker_builds_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """A builder that keeps no local image and has no registry cannot confirm a reuse, so it builds (SKY-737).

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    files = {**_BOX_TREE, "ascent.toml": _BOX_TREE["ascent.toml"].split("\n[metric]")[0]}
    monkeypatch.setattr(repo_staging, "_local_image_exists", lambda image: pytest.fail("no local Docker check"))
    builder, store = _FakeBuilder(), _ImageStore()
    for _ in range(2):
        _, staged = _stage_with(files, monkeypatch)
        try:
            _prepare(staged, _ImageRuntime(), builder=builder, store=store)
        finally:
            staged.cleanup()
    assert len(builder.requests) == 2


def _base(modes: dict[str, str] | None = None, file: str = "755 regular file") -> list[str]:
    """Write the walk of ``BASE_PYTHON`` in ``python:3.12-slim``, with some modes changed.

    Args:
        modes: The folder modes that differ from 755, by path.
        file: The mode and type of the interpreter file.

    Returns:
        The walk's lines.
    """
    return [
        *_walk("/usr/local/bin/python3", "777 symbolic link", modes),
        "link python3.12",
        f"0 {file} /usr/local/bin/python3.12",
    ]


# R2: a sys.path folder that links into a folder the box user can write.
_LINKED_INTO_WRITABLE = [
    *_entry("path", "/usr/local/lib/python3.12", "777 symbolic link"),
    "link sub/python3.12",
    "0 777 directory /usr/local/lib/sub",
    "0 755 directory /usr/local/lib/sub/python3.12",
]


@pytest.mark.parametrize(
    "probe",
    [
        "",
        "\n".join(_BASE) + "\n",
        _listing(base=_base(file="1000 755 regular file")),
        _listing(base=_base({"/usr/local": "775 directory"})),
        _listing(base=_base({"/usr": "757 directory"})),
        _listing(base=_base({"/": "1777 directory"})),
        _TRUSTED_PROBE.replace("0 755 directory /usr/local\n", "1000 755 directory /usr/local\n", 1),
        _listing(base=_base(file="757 regular file")),
        _listing(base=_base(file="750 regular file")),
        _listing(base=_base(file="755 directory")),
        _listing(base=_walk("/usr/local/bin/python3", "none")),
        _listing(base=[*_walk("/usr/local/bin/python3", "777 symbolic link")]),
        _listing(base=[*_walk("/usr/local/bin", "777 symbolic link"), "link /opt", "0 755 directory /", *_BASE[-3:]]),
        _listing(
            base=[
                *_walk("/usr/local/bin/python3", "777 symbolic link"),
                *["link python3", "0 777 symbolic link /usr/local/bin/python3"] * 40,
                "link python3",
            ]
        ),
        _TRUSTED_PROBE.replace("file /usr/bin/env\n", ""),
        _TRUSTED_PROBE.replace("0 755 regular file /usr/bin/env", "0 757 regular file /usr/bin/env"),
        _listing(box=[*_ENTRIES, *_entry("path", "/usr/lib/python3.12", "757 directory")]),
        _listing(box=[*_ENTRIES, *_entry("path", "/usr/lib/python3.12", modes={"/usr/lib": "777 directory"})]),
        _listing(box=[*_ENTRIES, *_LINKED_INTO_WRITABLE]),
        _listing(
            box=[*_ENTRIES, *_entry("file", "/usr/lib/libpython3.12.so.1.0", modes={"/usr/lib": "757 directory"})]
        ),
        _listing(box=[*_ENTRIES, *_entry("file", "/tmp/p.so", modes={"/tmp": "1777 directory"})]),
        _listing(box=[*_ENTRIES, *_entry("opt", "/etc/ld.so.preload", "666 regular file")]),
        _listing(box=[*_ENTRIES, *_entry("file", "/usr/lib/x.so (deleted)", "none")]),
        _listing(box=[*_ENTRIES, *_entry("file", "/usr/local/lib/python3.12", "755 directory")]),
        _listing(box=[*_ENTRIES, *_entry("dir", "/opt/python", "none")]),
        _listing(box=[*_ENTRIES, *_entry("file", "/usr/lib/libc.so.6", "1000 644 regular file")]),
        _listing(box=[*_ENTRIES, *_entry("lib", "/usr/lib/libc.so.6")]),
        _listing(box=[*_ENTRIES, "path usr/lib", *_walk("/usr/lib", "755 directory")]),
        _listing(box=[line for line in _ENTRIES if not line.startswith("file ")]),
        _listing(box=[]),
        _TRUSTED_PROBE.split("run box\n")[0],
        _TRUSTED_PROBE.replace("run clean", "run x").replace("run box", "run clean").replace("run x", "run box"),
        _TRUSTED_PROBE + "path /opt\n",
        _TRUSTED_PROBE.replace("link python3.12", "link python3.12\x01"),
        _TRUSTED_PROBE.replace("\n", "\x0b"),
        _TRUSTED_PROBE.replace("link python3.12\n", "link python3.12\x1c"),
        _TRUSTED_PROBE.replace(_PYTHON_FILE, f"{_PYTHON_FILE} extra"),
        _TRUSTED_PROBE.replace(_PYTHON_FILE, "0 755 socket /usr/local/bin/python3.12"),
        _TRUSTED_PROBE.replace(_PYTHON_FILE, "00 755 regular file /usr/local/bin/python3.12"),
    ],
    ids=[
        "missing",
        "only-the-interpreter-walk",
        "user-owned-file",
        "group-writable-folder",
        "writable-ancestor-folder",
        "sticky-writable-root",
        "user-owned-ancestor-folder",
        "other-writable-file",
        "not-executable-by-others",
        "not-a-file",
        "missing-interpreter",
        "chain-cut-short",
        "linked-folder-without-its-walk",
        "link-loop-hits-the-cap",
        "no-env-walk",
        "writable-env",
        "R1-stdlib-moved-to-a-writable-folder",
        "R1-stdlib-under-a-writable-folder",
        "R2-link-into-a-writable-folder",
        "libpython-under-a-writable-usr-lib",
        "preloaded-library-in-tmp",
        "writable-ld-so-preload",
        "deleted-mapped-file",
        "mapped-file-is-a-folder",
        "missing-prefix",
        "user-owned-mapped-file",
        "unknown-tag",
        "relative-path",
        "run-without-files",
        "empty-run",
        "box-run-missing",
        "runs-out-of-order",
        "extra-lines",
        "control-byte-in-target",
        "lines-split-by-vertical-tabs",
        "line-split-by-a-file-separator",
        "unknown-file-type",
        "special-file",
        "malformed-uid",
    ],
)
def test_a_run_image_without_a_trusted_python_is_refused(monkeypatch: pytest.MonkeyPatch, probe: str) -> None:
    """A ``[run]`` image where the box user could change what root's interpreter loads stops the run (SKY-737).

    Args:
        monkeypatch: Pytest monkeypatch.
        probe: What the trusted interpreter probe prints.
    """
    files = {**_BOX_TREE, "ascent.toml": _BOX_TREE["ascent.toml"].split("\n[metric]")[0]}
    _, staged = _stage_with(files, monkeypatch)
    runtime = _ImageRuntime(probe=probe)
    try:
        with pytest.raises(repo_staging.BoxImageError, match=r"has no trusted Python at /usr/local/bin/python3"):
            _prepare(staged, runtime)
    finally:
        staged.cleanup()
    assert [spec.operation_key for spec in runtime.specs] == ["image-check:job-1:run"]


@pytest.mark.parametrize(
    "probe",
    [
        _TRUSTED_PROBE,
        _listing(base=_walk("/usr/local/bin/python3")),
        _listing(
            base=[
                *_walk("/usr/local/bin/python3", "777 symbolic link"),
                "link /opt/py/python3",
                *_walk("/opt/py/python3", "777 symbolic link"),
                "link /usr/bin/python3.12",
                *_walk("/usr/bin/python3.12"),
            ]
        ),
        _listing(
            base=[
                *_walk("/usr/local/bin/python3", "777 symbolic link"),
                "link ../bin/./python3.12",
                "0 755 directory /usr/local/bin",
                _PYTHON_FILE,
            ]
        ),
        _listing(
            box=[
                *_ENTRIES,
                "file /lib/ld-linux-aarch64.so.1",
                *_walk("/lib", "777 symbolic link"),
                "link usr/lib",
                "0 755 directory /usr",
                "0 755 directory /usr/lib",
                "0 755 regular file /usr/lib/ld-linux-aarch64.so.1",
            ]
        ),
        _listing(box=["env PATH", "env MALLOC_ARENA_MAX", *_ENTRIES]),
    ],
    ids=[
        "python-image-layout",
        "plain-file",
        "two-absolute-links",
        "dot-dot-target",
        "linked-top-folder",
        "plain-environment",
    ],
)
def test_a_root_owned_python_chain_passes(probe: str) -> None:
    """Paths under root-owned folders pass, missing ``sys.path`` entries too; a failed probe never does (SKY-737).

    The probe exits 1 when ``find`` sees a writable file or a link below a
    ``sys.path`` folder, so only its exit code tells that case apart.

    Args:
        probe: What the trusted interpreter probe prints.
    """
    assert repo_staging._trusted_python(CommandResult(exit_code=0, stdout=probe))
    assert not repo_staging._trusted_python(CommandResult(exit_code=1, stdout=probe))


@pytest.mark.parametrize(
    ("variables", "names"),
    [
        (["env LD_LIBRARY_PATH"], "LD_LIBRARY_PATH"),
        (["env LD_PRELOAD"], "LD_PRELOAD"),
        (["env PATH", "env LD_AUDIT", "env GCONV_PATH"], "GCONV_PATH, LD_AUDIT"),
    ],
    ids=["library-path-with-an-empty-element", "preload", "audit-and-gconv"],
)
def test_a_run_image_with_loader_variables_in_its_env_is_refused(
    monkeypatch: pytest.MonkeyPatch, variables: list[str], names: str
) -> None:
    """An image ENV such as ``LD_LIBRARY_PATH=/x:`` stops the run, as root's loader reads it (SKY-737).

    The probe prints only the names, so ``/x:`` (an empty element, which
    glibc reads as the work folder) and ``/x`` refuse alike.

    Args:
        monkeypatch: Pytest monkeypatch.
        variables: The ``env`` lines of the box run.
        names: The names that the message lists.
    """
    files = {**_BOX_TREE, "ascent.toml": _BOX_TREE["ascent.toml"].split("\n[metric]")[0]}
    _, staged = _stage_with(files, monkeypatch)
    runtime = _ImageRuntime(probe=_listing(box=[*variables, *_ENTRIES]))
    try:
        with pytest.raises(repo_staging.BoxImageError, match=rf"sets {names} in its environment.*Remove every LD_"):
            _prepare(staged, runtime)
    finally:
        staged.cleanup()


def test_the_probe_imports_what_the_root_scripts_import() -> None:
    """The interpreter loads every module that the runner and the hash scripts import at their top (SKY-737)."""
    imports = repo_staging._PATH_SCRIPT.split("\n", 1)[0]

    assert imports.startswith("import ")
    for name in ("hashlib", "json", "ssl", "ctypes", "http.server", "urllib.request", "collections.abc"):
        assert name in imports.removeprefix("import ").split(", ")
    assert "__future__" not in imports


def _shell_probe(start: Path) -> str:
    """Point the probe at a test interpreter that the test's own user owns.

    Args:
        start: The interpreter path the probe starts at.

    Returns:
        The probe, with ``find`` taking the test's user in place of root.
    """
    if sys.platform != "linux":
        pytest.skip("the probe needs GNU or busybox stat and /proc, as in the box images")
    probe = repo_staging._TRUSTED_PYTHON_PROBE.replace(shlex.quote(repo_workspace.BASE_PYTHON), shlex.quote(str(start)))
    return probe.replace("! -user 0", f"! -user {os.getuid()}")


def _fake_python(root: Path, extra: str = "") -> Path:
    """Make an interpreter under a prefix that prints the tagged lines a real one prints.

    Args:
        root: The folder that becomes the prefix.
        extra: More lines for the interpreter to print.

    Returns:
        The interpreter file.
    """
    stdlib = root / "lib" / "python3.12"
    for folder in (root / "bin", root / "lib", stdlib, stdlib / "json", stdlib / "site-packages"):
        folder.mkdir()
        folder.chmod(0o755)
    (stdlib / "os.py").write_text("x\n")
    (stdlib / "os.py").chmod(0o644)
    python = root / "bin" / "python3.12"
    python.write_text(
        "#!/bin/sh\n"
        f"printf 'path %s\\n' \"${{FAKE_PATH:-{stdlib}}}\"\n"
        f"printf 'file %s\\n' {shlex.quote(str(python))}\n"
        f"printf 'dir %s\\n' {shlex.quote(str(root))}\n"
        f"printf '%s' {shlex.quote(extra)}\n"
        "env | sed -n 's/^\\([A-Za-z_][A-Za-z0-9_]*\\)=.*/env \\1/p'\n"
    )
    python.chmod(0o755)
    return python


def _run_probe(start: Path, **env: str) -> subprocess.CompletedProcess[str]:
    """Run the probe from a test interpreter.

    Args:
        start: The interpreter path the probe starts at.
        **env: Variables to add to the box's environment.

    Returns:
        The finished probe.
    """
    probe = _shell_probe(start)
    return subprocess.run(
        ["/bin/sh", "-c", probe], capture_output=True, text=True, check=False, env={**os.environ, **env}
    )


def test_the_trusted_python_probe_walks_each_hop_and_each_path_that_the_interpreter_names(tmp_path: Path) -> None:
    """The probe walks every part of a two-link chain, then runs the interpreter twice and walks its paths (SKY-737).

    Args:
        tmp_path: Pytest temporary folder.
    """
    root = tmp_path.resolve()
    target = _fake_python(root)
    (root / "real").mkdir()
    (root / "real" / "python3").symlink_to(target)
    (root / "bin" / "python3").symlink_to(root / "real" / "python3")
    start = root / "bin" / "python3"

    done = _run_probe(start, FAKE_PATH="/nonexistent/box")

    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    walk = lines[: lines.index("file /usr/bin/env")]
    expected: list[str] = []
    for hop, link in zip(
        (start, root / "real" / "python3", target), (root / "real" / "python3", target, None), strict=True
    ):
        parts = str(hop).split("/")[1:]
        expected += ["/"] + ["/" + "/".join(parts[:depth]) for depth in range(1, len(parts) + 1)]
        expected += [f"link {link}"] if link else []
    assert [re.sub(r"^\d+ \d+ (directory|symbolic link|regular file) ", "", line) for line in walk] == expected
    tagged = [line for line in lines if line.split(" ")[0] in ("run", "path", "file", "dir", "none")]
    paths = [f"file {target}", f"dir {root}"]
    assert tagged[tagged.index("run clean") :] == [
        "run clean",
        f"path {root / 'lib' / 'python3.12'}",
        *paths,
        "run box",
        "path /nonexistent/box",
        "none /nonexistent",
        *paths,
    ]


@pytest.mark.parametrize(
    ("change", "trusted"),
    [
        (None, True),
        ("chmod o+w lib/python3.12/site-packages", True),
        ("chmod o+w lib/python3.12/os.py", False),
        ("chmod g+w lib/python3.12/json", False),
        ("ln -s /tmp lib/python3.12/json/tmp", False),
        ("touch bin/pyvenv.cfg", False),
        ("touch pyvenv.cfg", False),
        ("touch bin/python3.12._pth", False),
    ],
    ids=[
        "python-image-layout",
        "writable-site-packages",
        "writable-stdlib-file",
        "writable-stdlib-folder",
        "link-in-the-stdlib",
        "pyvenv-cfg-next-to-the-interpreter",
        "pyvenv-cfg-one-folder-up",
        "pth-file",
    ],
)
def test_the_trusted_python_probe_fails_on_what_could_move_or_change_the_import_path(
    tmp_path: Path, change: str | None, trusted: bool
) -> None:
    """A writable file, a link below ``sys.path`` or a venv file fails the probe; ``site-packages`` does not (SKY-737).

    Args:
        tmp_path: Pytest temporary folder.
        change: The shell command, run in the prefix, that changes the layout.
        trusted: Whether the probe still passes.
    """
    root = tmp_path.resolve()
    python = _fake_python(root)
    if change is not None:
        subprocess.run(["/bin/sh", "-c", change], cwd=root, check=True)

    done = _run_probe(python)

    assert (done.returncode == 0) is trusted, done.stderr


@pytest.mark.parametrize("extra", ["evil\n", "path relative\n", "path /opt/x\x01\n", "run box\n"])
def test_the_trusted_python_probe_stops_at_an_odd_interpreter_line(tmp_path: Path, extra: str) -> None:
    """An untagged, relative or control-byte line from the interpreter fails the probe (SKY-737).

    Args:
        tmp_path: Pytest temporary folder.
        extra: The odd line.
    """
    python = _fake_python(tmp_path.resolve(), extra)

    assert _run_probe(python).returncode == 1


@pytest.mark.parametrize("target", ["python3.12\n0 755 regular file /usr/bin/python3", "python3.12\n", "py\x01"])
def test_the_trusted_python_probe_stops_at_a_link_target_with_a_control_byte(tmp_path: Path, target: str) -> None:
    """A newline or control byte in a link target fails the probe, so it cannot forge a listing line (SKY-737).

    Args:
        tmp_path: Pytest temporary folder.
        target: The link target.
    """
    root = tmp_path.resolve()
    (root / "python3.12").write_text("#!/bin/sh\n")
    (root / "python3").symlink_to(target)

    done = _run_probe(root / "python3")

    assert done.returncode == 1
    assert not any(line.startswith("link ") for line in done.stdout.splitlines())


def test_the_trusted_python_probe_names_the_image_env_of_the_box_run(tmp_path: Path) -> None:
    """The box run lists an image ``LD_LIBRARY_PATH`` by name, and the clean run does not (SKY-737).

    Args:
        tmp_path: Pytest temporary folder.
    """
    root = tmp_path.resolve()
    python = _fake_python(root)

    done = _run_probe(python, LD_LIBRARY_PATH="/opt/foo:")

    assert done.returncode == 0, done.stderr
    clean, box = done.stdout.split("run box\n")
    assert "env LD_LIBRARY_PATH" not in clean.split("\n")
    assert "env LD_LIBRARY_PATH" in box.split("\n")
    assert "/opt/foo" not in done.stdout
    assert repo_staging._loader_variables(done) == ["LD_LIBRARY_PATH"]


def test_the_path_script_prints_each_variable_name_without_its_value() -> None:
    """The real interpreter script prints ``env`` lines with names only (SKY-737)."""
    if sys.platform != "linux":
        pytest.skip("the script reads /proc, as in the box images")
    base = getattr(sys, "_base_executable", sys.executable)
    done = subprocess.run(
        [base, "-I", "-S", "-c", repo_staging._PATH_SCRIPT],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "/usr/bin:/bin", "LD_PRELOAD": "", "SECRET_VALUE": "do-not-print"},
    )

    assert done.returncode == 0, done.stderr
    assert {"env LD_PRELOAD", "env SECRET_VALUE"} <= set(done.stdout.split("\n"))
    assert "do-not-print" not in done.stdout


def test_a_contract_with_no_image_keeps_the_old_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no image key, nothing builds or checks, and the record keeps version 1 and its hash (SKY-737).

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    staged_payload, staged = _stage_with(_CASES_TREE, monkeypatch, _record_payload())
    runtime = _ImageRuntime()
    try:
        assert _prepare(staged, runtime) is staged
        result = _result(0.5, 0.95, details=_guest_details(staged_payload["cases"], 7))
        repo_staging.add_run_record(result, staged_payload, staged, image=_PINNED_IMAGE)
    finally:
        staged.cleanup()
    record = result["details"]["record"]
    metric = hashlib.sha256(
        _framed("eval/scorer.py", b"def score(): ...\n") + _framed("metric.py", _RECORD_METRIC.encode())
    )
    assert runtime.specs == []
    assert record["record_version"] == 1
    assert "boxes" not in record
    assert record["metric_sha256"] == metric.hexdigest()


def test_a_metric_image_is_part_of_the_metric_hash_and_the_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``[metric] image`` digest is one more metric part, and the record names each box (SKY-737).

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    metric = '[metric]\nfiles = ["eval/scorer.py"]\n'
    tree = {
        **_CASES_TREE,
        "ascent.toml": _CASES_TREE["ascent.toml"].replace(metric, f'{metric}image = "{_METRIC_IMAGE}"\n'),
    }
    staged_payload, staged = _stage_with(tree, monkeypatch, _record_payload())
    try:
        staged = _prepare(staged, _ImageRuntime(), builder=None)
        result = _result(0.5, 0.95, details=_guest_details(staged_payload["cases"], 7))
        repo_staging.add_run_record(result, staged_payload, staged, image=_PINNED_IMAGE)
    finally:
        staged.cleanup()
    record = result["details"]["record"]
    digest = ("sha256:" + "c" * 64).encode()
    expected = hashlib.sha256(
        _framed("/[metric] image", digest)
        + _framed("eval/scorer.py", b"def score(): ...\n")
        + _framed("metric.py", _RECORD_METRIC.encode())
    )
    assert record["metric_sha256"] == expected.hexdigest()
    assert record["record_version"] == 2
    assert record["boxes"] == {
        "metric": {"digest": "sha256:" + "c" * 64, "source": "image", "input_hash": None, "build_log": None}
    }


@pytest.mark.parametrize(
    ("accepts", "builder", "message"),
    [(False, _FakeBuilder(), "cannot start a box from a user image"), (True, None, "cannot build a Dockerfile")],
)
def test_a_deployment_that_cannot_use_the_image_says_so(
    monkeypatch: pytest.MonkeyPatch, accepts: bool, builder: Any, message: str
) -> None:
    """A runtime that ignores the image, or a deployment with no builder, fails before any box (SKY-737).

    Args:
        monkeypatch: Pytest monkeypatch.
        accepts: Whether the runtime starts a box from a user image.
        builder: The deployment's builder.
        message: The expected error text.
    """
    _, staged = _stage_with(_BOX_TREE, monkeypatch)
    runtime = _ImageRuntime()
    monkeypatch.setattr(runtime, "accepts_user_images", accepts)
    try:
        with pytest.raises(repo_staging.BoxImageError, match=message):
            _prepare(staged, runtime, builder=builder)
    finally:
        staged.cleanup()
    assert runtime.specs == []


def test_a_resume_uses_the_pinned_images_and_builds_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A resumed run keeps the images it pinned at its start, so a later push cannot change them (SKY-737).

    Args:
        monkeypatch: Pytest monkeypatch.
    """
    _, staged = _stage_with(_BOX_TREE, monkeypatch)
    builder, runtime = _FakeBuilder(), _ImageRuntime()
    pinned = {
        role: {"image": _BUILT_IMAGE, "digest": "sha256:" + "b" * 64, "source": "dockerfile"}
        for role in ("run", "metric")
    }
    try:
        staged = _prepare(staged, runtime, builder=builder, pinned=pinned)
    finally:
        staged.cleanup()
    assert staged.box_images == pinned
    assert builder.requests == []
    assert runtime.specs == []


def test_the_build_context_holds_only_the_dockerfile_folder(tmp_path: Path) -> None:
    """The context of ``box/Dockerfile`` is ``box``, so no other repository file reaches the build (SKY-737).

    Args:
        tmp_path: Pytest's temporary folder.
    """
    archive = _tree(tmp_path, {"box/Dockerfile": "FROM x\n", "box/lib/a.txt": "a\n", "src/app.py": "x = 1\n"})

    context = extract_folder(archive, "box", tmp_path / "context")
    whole = extract_folder(archive, ".", tmp_path / "whole")

    assert context == tmp_path / "context" / "box"
    assert sorted(
        str(path.relative_to(tmp_path / "context")) for path in (tmp_path / "context").rglob("*") if path.is_file()
    ) == [
        "box/Dockerfile",
        "box/lib/a.txt",
    ]
    assert (whole / "src" / "app.py").read_text() == "x = 1\n"


def _context_build(
    tmp_path: Path, role: str, links: dict[str, tuple[bytes, str]] | None = None, *, hidden_file: bool = True
) -> _FakeBuilder:
    """Build ``box/Dockerfile`` for one role from a tree whose ``box`` folder holds the hidden ``box/score.py``.

    Args:
        tmp_path: Pytest's temporary folder.
        role: ``run`` or ``metric``.
        links: Links in the tree, as ``_tree`` takes them.
        hidden_file: Whether ``box/score.py`` is a regular file; a link in ``links`` can take its place.

    Returns:
        The builder, which holds the build request.
    """
    files = {"box/Dockerfile": "FROM x\n", "box/app.py": "x = 1\n", "secret.txt": "SECRET = 1\n"}
    if hidden_file:
        files["box/score.py"] = "SECRET = 1\n"
    staged = repo_staging.StagedRepository(
        snapshot=RepoSnapshot(commit="c" * 40, archive=_tree(tmp_path, files, links), readonly_paths=(), size_bytes=1),
        secrets={},
        workdir=tmp_path,
        hidden_paths=("box/score.py",),
    )
    builder = _FakeBuilder()
    repo_staging._dockerfile_image(
        staged,
        role,
        "box/Dockerfile",
        owner_id="job-1",
        username="alice",
        builder=builder,
        store=_ImageStore(),
        budget=_FakeBudget(),
        registry=None,
    )
    return builder


def _context_files(builder: _FakeBuilder) -> dict[str, str]:
    """Return each regular file of the build context with its text, by context path.

    Args:
        builder: The builder that holds one build request.

    Returns:
        The context's regular files; a link is never followed.
    """
    context = builder.requests[0].context_dir
    return {
        str(path.relative_to(context)): path.read_text()
        for path in context.rglob("*")
        if path.is_file() and not path.is_symlink()
    }


def test_the_candidate_image_context_never_holds_a_hidden_file(tmp_path: Path) -> None:
    """A hidden scoring file inside the Dockerfile's folder never reaches the [run] image build (SKY-737).

    Args:
        tmp_path: Pytest's temporary folder.
    """
    builder = _context_build(tmp_path, "run")

    assert _context_files(builder) == {"Dockerfile": "FROM x\n", "app.py": "x = 1\n"}


def test_the_metric_image_context_keeps_the_hidden_scoring_files(tmp_path: Path) -> None:
    """The metric box may read the hidden files, so its build context keeps them (SKY-737).

    Args:
        tmp_path: Pytest's temporary folder.
    """
    builder = _context_build(tmp_path, "metric")

    assert _context_files(builder)["score.py"] == "SECRET = 1\n"


def test_a_link_to_a_hidden_file_carries_nothing_into_the_candidate_image(tmp_path: Path) -> None:
    """A hard link to a hidden file is dropped, and a symlink to one points at nothing (SKY-737).

    Args:
        tmp_path: Pytest's temporary folder.
    """
    builder = _context_build(
        tmp_path,
        "run",
        links={"box/copy.py": (tarfile.LNKTYPE, "box/score.py"), "box/peek.py": (tarfile.SYMTYPE, "score.py")},
    )

    context = builder.requests[0].context_dir
    assert _context_files(builder) == {"Dockerfile": "FROM x\n", "app.py": "x = 1\n"}
    assert not (context / "copy.py").exists()
    assert not (context / "peek.py").exists()


def test_a_hidden_path_that_is_a_link_stops_the_candidate_image_build(tmp_path: Path) -> None:
    """A hidden path that is a link loses only the link in the strip, so the build refuses it (SKY-737).

    Args:
        tmp_path: Pytest's temporary folder.
    """
    with pytest.raises(
        repo_staging.BoxImageError, match=r"hidden scoring path 'box/score\.py' is, holds or lies inside"
    ):
        _context_build(tmp_path, "run", links={"box/score.py": (tarfile.SYMTYPE, "../secret.txt")}, hidden_file=False)
