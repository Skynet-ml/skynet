"""Tests for staging a repository target's tree and secrets in the trusted parent."""

from __future__ import annotations

import io
import json
import tarfile
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from core.api.auth import AuthenticatedUser, get_authenticated_user
from core.api.model_catalog import CatalogModel, ModelCatalogResponse
from core.api.routers import wizard_preflight
from core.billing.model_dispatch import ModelHTTPResult
from core.billing.model_gateway import ROUTE_KEY
from core.billing.openrouter_quotes import resolve_model_slug
from core.billing.operation_pricing import ChargePolicy
from core.billing.protected_credentials import has_exposed_execution_credentials
from core.billing.vercel_usage import PACKAGE_REGISTRY_HOSTS
from core.connectors.github_publish import PublishedChange, RepoPublishError
from core.connectors.github_repo import RepoSnapshot
from core.exceptions import ServiceError
from core.service_gateway.optimization.blackbox.protocol import METRIC_ERROR
from core.service_gateway.optimization.blackbox.repo_constraints import ConstraintsError
from core.service_gateway.optimization.blackbox.repo_workspace import _PROBE_OK, NetworkCutoffError
from core.service_gateway.optimization.blackbox.rule_judge import (
    JUDGE_ANSWER_TOKENS,
    JUDGE_REASONING_TOKENS,
    WATCH_EVERY_ACTIONS,
    WITHHELD_STEP,
)
from core.service_gateway.optimization.blackbox.sandbox import CommandResult, SandboxSpec
from core.worker import repo_staging


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


def _tree(workdir: Path, files: dict[str, str]) -> Path:
    """Pack files the way the parent ships a fetched tree.

    Args:
        workdir: Folder the archive goes in.
        files: Root-relative path to text.

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
    return archive


def _stage_with(
    files: dict[str, str], monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any] | None = None
) -> tuple[dict[str, Any], repo_staging.StagedRepository]:
    """Stage the fixture payload over a tree holding ``files``.

    Args:
        files: Root-relative path to text in the fetched tree.
        monkeypatch: Pytest monkeypatch.
        payload: Payload to stage; the fixture payload pinned to ``c * 40`` when omitted.

    Returns:
        The staged payload and repository.
    """
    monkeypatch.setattr(repo_staging, "resolve_repo_secrets", lambda payload, **_: {"API_KEY": "secret-value"})
    monkeypatch.setattr(repo_staging, "github_token", lambda engine, username: "gh-token")
    monkeypatch.setattr(
        repo_staging,
        "fetch_snapshot",
        lambda repository, pinned, token, workdir: RepoSnapshot(
            commit=pinned, archive=_tree(workdir, files), readonly_paths=(), size_bytes=1
        ),
    )
    return repo_staging.stage_repository(payload or _payload("c" * 40), username="alice", binding_id="b", engine=None)


_CONSTRAINTS = """
[paths]
editable = ["src/geocode"]
protected = ["src/geocode/eval"]

[instructions]
intent = "Pick the place the sender means."
"""


def test_staging_applies_the_repository_constraints_and_records_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The file narrows the editable paths, protects its judge, briefs the agent, and is saved with the run."""
    staged_payload, staged = _stage_with({"optimize.toml": _CONSTRAINTS}, monkeypatch)

    repo = staged_payload["target"]["repo"]
    assert repo["editable_paths"] == ["src/geocode"]
    assert staged.protected_paths == ("optimize.toml", "src/geocode/eval")
    assert staged.intent == "Pick the place the sender means."
    environment = repo["environment"]
    assert environment["commit"] == "c" * 40
    assert environment["editable_paths"] == ["src/geocode"]
    assert environment["protected_paths"] == ["optimize.toml", "src/geocode/eval"]
    assert environment["secret_names"] == ["API_KEY"]
    assert environment["services"] == []
    assert environment["constraints_file"]["path"] == "optimize.toml"
    assert "secret-value" not in repr(staged_payload)
    assert "Pick the place the sender means." in staged_payload["background"]
    assert "`src/geocode/eval`" in staged_payload["background"]
    staged.cleanup()


@pytest.mark.parametrize(
    ("files", "message"),
    [
        ({"optimize.toml": '[paths]\neditable = ["tests"]\n'}, "outside the approved editable paths"),
        ({"optimize.toml": '[services]\ngeocoder = ["search"]\n'}, "cannot grant services"),
    ],
)
def test_staging_refuses_a_file_that_asks_for_more(
    files: dict[str, str], message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wider path or a service request fails setup with the reason and leaves nothing on disk."""
    workdirs: list[Path] = []
    real_mkdtemp = repo_staging.tempfile.mkdtemp

    def track(**options: Any) -> str:
        """Remember the scratch folder staging makes."""
        workdirs.append(Path(real_mkdtemp(**options)))
        return str(workdirs[-1])

    monkeypatch.setattr(repo_staging.tempfile, "mkdtemp", track)

    with pytest.raises(ConstraintsError, match=message):
        _stage_with(files, monkeypatch)

    assert workdirs
    assert not workdirs[0].exists()


def test_a_resumed_run_with_different_rules_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run resumes only under the rules first recorded for it."""
    first, staged = _stage_with({"optimize.toml": _CONSTRAINTS}, monkeypatch)
    staged.cleanup()
    resumed = _payload("c" * 40)
    resumed["target"]["repo"]["environment"] = first["target"]["repo"]["environment"]

    again, staged = _stage_with({"optimize.toml": _CONSTRAINTS}, monkeypatch, resumed)
    staged.cleanup()
    assert again["target"]["repo"]["environment"]["protected_paths"] == ["optimize.toml", "src/geocode/eval"]
    assert again["_repo_snapshot"]["constrained"] is True

    with pytest.raises(ConstraintsError, match="environment changed since it was recorded"):
        _stage_with({}, monkeypatch, resumed)


_RULED = (
    _CONSTRAINTS
    + """
[rules]
illegal = ["Do not hardcode answers for dataset cases.", "Do not call an outside geocoding service."]
"""
)


def test_staging_keeps_the_plain_language_rules_for_the_judges(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rules reach the parent's judges and the recorded environment, and a resume must keep them."""
    first, staged = _stage_with({"optimize.toml": _RULED}, monkeypatch)
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
    with pytest.raises(ConstraintsError, match=r"\(rules\)"):
        _stage_with({"optimize.toml": _CONSTRAINTS}, monkeypatch, resumed)


def test_a_run_recorded_before_rules_existed_resumes_without_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    """An environment saved before the rules field existed counts as having no rules."""
    first, staged = _stage_with({"optimize.toml": _CONSTRAINTS}, monkeypatch)
    staged.cleanup()
    resumed = _payload("c" * 40)
    resumed["target"]["repo"]["environment"] = {
        key: value for key, value in first["target"]["repo"]["environment"].items() if key != "rules"
    }

    _, staged = _stage_with({"optimize.toml": _CONSTRAINTS}, monkeypatch, resumed)
    staged.cleanup()
    assert staged.rules == ()

    with pytest.raises(ConstraintsError, match=r"\(rules\)"):
        _stage_with({"optimize.toml": _RULED}, monkeypatch, resumed)


def test_a_tree_without_a_constraints_file_is_not_constrained(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without optimize.toml the run keeps its old behavior: no judge and no fixed block message."""
    _, staged = _stage_with({}, monkeypatch)
    staged.cleanup()

    assert staged.constrained is False
    assert staged.rules == ()


@pytest.mark.parametrize("commit", [None, "b" * 40])
def test_staging_pins_the_commit_and_keeps_secrets_in_the_parent(
    commit: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Follow the branch only when no commit is pinned, and hand the guest secret names but never values."""
    fetched: list[tuple[str, str, str]] = []

    def fetch(repository: str, pinned: str, token: str, workdir: Path) -> RepoSnapshot:
        """Pretend to clone and record what was asked for."""
        fetched.append((repository, pinned, token))
        return RepoSnapshot(commit=pinned, archive=_tree(workdir, {}), readonly_paths=("vendor/lib",), size_bytes=4)

    monkeypatch.setattr(repo_staging, "resolve_repo_secrets", lambda payload, **_: {"API_KEY": "secret-value"})
    monkeypatch.setattr(repo_staging, "github_token", lambda engine, username: "gh-token")
    monkeypatch.setattr(repo_staging, "resolve_commit", lambda token, repository, branch: "a" * 40)
    monkeypatch.setattr(repo_staging, "fetch_snapshot", fetch)
    original = _payload(commit)

    staged_payload, staged = repo_staging.stage_repository(original, username="alice", binding_id="b", engine=None)

    expected = commit or "a" * 40
    assert fetched == [("acme/app", expected, "gh-token")]
    assert staged_payload["target"]["repo"]["commit"] == expected
    assert staged_payload["target"]["repo"]["secrets"] == []
    assert staged_payload["_repo_snapshot"]["secret_names"] == ["API_KEY"]
    assert staged_payload["_repo_snapshot"]["readonly_paths"] == ["vendor/lib"]
    assert staged_payload["_repo_snapshot"]["constrained"] is False
    assert "secret-value" not in repr(staged_payload)
    assert not has_exposed_execution_credentials(staged_payload)
    assert staged.secrets == {"API_KEY": "secret-value"}
    assert original["target"]["repo"]["secrets"][0]["credential_ref"] == "ref-1"
    staged.cleanup()
    assert not staged.workdir.exists()


def test_failed_fetch_leaves_nothing_on_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove the scratch folder when the clone fails."""
    workdirs: list[Path] = []

    def fetch(repository: str, pinned: str, token: str, workdir: Path) -> RepoSnapshot:
        """Fail like a refused clone."""
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
    """Answer every command, tracking the network state a real box would have."""

    def __init__(self, spec: SandboxSpec, cutoff_works: bool) -> None:
        """Open over ``spec``.

        Args:
            spec: The box's requested network profile.
            cutoff_works: Whether switching the network off succeeds.
        """
        self.spec = spec
        self.online = not spec.network_disabled
        self._cutoff_works = cutoff_works
        self.commands: list[tuple[str, bool]] = []
        self.closed = False

    def write_files(self, files: dict[str, str]) -> None:
        """Accept files."""

    def read_file(self, path: str) -> str | None:
        """Hold no files."""
        return None

    def run(self, command: str, **options: Any) -> CommandResult:
        """Record the command, answering the reachability check as the box would."""
        self.commands.append((command, self.online))
        if "/dev/tcp/" in command:
            return CommandResult(exit_code=1) if self.online else CommandResult(exit_code=0, stdout=_PROBE_OK)
        return CommandResult(exit_code=0)

    def disable_network(self) -> None:
        """Switch the network off, or fail like a provider that refused."""
        if not self._cutoff_works:
            raise ServiceError("policy update refused")
        self.online = False

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

    def __init__(self, *, cutoff_works: bool = True) -> None:
        """Start with no evaluator or box.

        Args:
            cutoff_works: Whether the parent box's network switch succeeds.
        """
        self.evaluator: Any = None
        self.boxes: list[_Box] = []
        self._cutoff_works = cutoff_works
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
        """Return a runtime that opens recording boxes."""
        gateway = self

        class _Runtime:
            """Open one recording box per call."""

            def open(self, spec: SandboxSpec) -> _Box:
                """Open a recording box over ``spec``."""
                box = _Box(spec, gateway._cutoff_works)
                gateway.boxes.append(box)
                return box

        return _Runtime()

    def bind_evaluator(self, evaluator: Any) -> dict[str, str]:
        """Keep the evaluator and hand back a fake route.

        Args:
            evaluator: The parent's repository scorer.

        Returns:
            A fake route.
        """
        assert all(not box.online for box in self.boxes)
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
    archive = tmp_path / "tree.tgz"
    archive.write_bytes(b"tree")
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
    """The guest receives only the evaluator route; the parent owns the scorer and closes it."""
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


def test_binding_gives_the_box_only_the_scorer_models_scoped_route(tmp_path: Path) -> None:
    """The scorer's model reaches the box as its scoped route; the box stays offline."""
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
    assert gateway.evaluator._model_env() == {"APP_API_KEY": "route-token", "APP_MODEL": "vendor/judge"}
    box = gateway.boxes[0]
    assert box.spec.network_disabled is True
    assert box.spec.allowed_hosts == ()
    assert payload["target"]["repo"]["environment"]["network"] == {"setup": [], "versions": []}
    staged.close_scorers()


def test_binding_refuses_a_scorer_model_without_a_scoped_route(tmp_path: Path) -> None:
    """A model the parent did not scope would carry a provider key, so no box opens."""
    payload, staged = _binding(tmp_path, None)
    payload["scorer"]["model"] = {"name": "vendor/judge", "token_source": "byok", "extra": {"api_key": "user-key"}}
    gateway = _Gateway()

    with pytest.raises(ServiceError, match="scoped route"):
        repo_staging.bind_repo_scorer(payload, staged, gateway, owner_id="job-1")
    assert gateway.boxes == []


def _ruled_binding(
    tmp_path: Path, rules: tuple[str, ...], *, intent: str | None = None, watch: bool = False
) -> tuple[dict[str, Any], repo_staging.StagedRepository]:
    """Build a staged repository whose optimize.toml has the given plain-language rules.

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


def _trigger_watch(gateway: Any) -> None:
    """Send enough agent model calls to start one watch, and wait for it.

    Args:
        gateway: The recording gateway.
    """
    (observer,) = gateway.observers["optimization"]
    for index in range(WATCH_EVERY_ACTIONS):
        observer({"messages": [{"role": "user", "content": f"edit {index}"}]})
    assert gateway.watched.wait(5)


def test_a_run_that_turns_the_watch_on_binds_one_managed_hook_billed_to_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The watch hook runs on the managed key under the run's budget, never on a guest route, and never sees the metric."""
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
    """An eval.sh call and its reply carry the score and feedback, so the hook gets a withheld step instead."""
    monkeypatch.setattr(repo_staging, "agent_model_id", lambda configured="": "vendor/fast")
    payload, staged = _ruled_binding(tmp_path, ("Do not hardcode answers.",), intent="Find places.", watch=True)
    gateway = _Gateway()
    repo_staging.bind_repo_scorer(payload, staged, gateway, owner_id="job-1", managed_key="managed-key")
    (observer,) = gateway.observers["optimization"]
    reply = json.dumps({"score": 0.4217, "info": {"feedback": "FEEDBACK-SECRET: case 3 names the wrong city"}})

    for index in range(WATCH_EVERY_ACTIONS - 1):
        observer({"messages": [{"role": "user", "content": f"edit {index}"}]})
    observer(
        {
            "messages": [
                {"role": "user", "content": "edit 13"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": "shell", "arguments": json.dumps({"command": "./eval.sh case-3"})}}
                    ],
                },
                {"role": "tool", "content": reply},
            ]
        }
    )
    assert gateway.watched.wait(5)

    (_model, prompt) = gateway.judged[0]
    assert "edit 12" in prompt
    assert WITHHELD_STEP in prompt
    assert "0.4217" not in prompt
    assert "FEEDBACK-SECRET" not in prompt
    assert "eval.sh" not in prompt
    staged.close_scorers()


def test_the_watch_is_off_by_default_and_costs_nothing(tmp_path: Path) -> None:
    """Rules alone register no model and observe no agent call unless the run turns the watch on."""
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
    """A stated intent is enough for the watch hook to have something to check."""
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
    """A malformed setting stops the run instead of silently turning the hook off."""
    with pytest.raises(ConstraintsError, match="rule_watch"):
        repo_staging._rule_watch_setting({repo_staging.RULE_WATCH_KEY: setting})


def _catalog(*models: CatalogModel) -> ModelCatalogResponse:
    """Build a catalog snapshot holding the given models.

    Args:
        models: Catalog rows.

    Returns:
        The snapshot.
    """
    return ModelCatalogResponse(providers=[], models=list(models))


def _row(value: str, **reasoning: Any) -> CatalogModel:
    """Build one catalog row with the given reasoning fields.

    Args:
        value: Model id.
        reasoning: Reasoning fields of the row.

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
    """A model the catalog marks as non-reasoning gets no reasoning field and the short limit."""
    catalog = _catalog(
        _row("vendor/fast", reasoning_efforts=[], reasoning_default_enabled=False, reasoning_mandatory=False)
    )

    body = _judge_body(tmp_path, monkeypatch, catalog)

    assert "reasoning" not in body
    assert body["max_tokens"] == JUDGE_ANSWER_TOKENS


def test_a_hook_missing_from_the_catalog_still_has_room_to_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before the catalog loads nobody knows whether the hook reasons, so it gets the larger budget."""
    body = _judge_body(tmp_path, monkeypatch, None)

    assert "reasoning" not in body
    assert body["max_tokens"] == JUDGE_ANSWER_TOKENS + JUDGE_REASONING_TOKENS


def test_a_hook_with_unknown_reasoning_data_still_has_room_to_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A catalog row whose provider did not say whether it reasons is unknown, not a model that never reasons."""
    unknown = {"reasoning_efforts": None, "reasoning_default_enabled": None, "reasoning_mandatory": None}

    body = _judge_body(tmp_path, monkeypatch, _catalog(_row("vendor/fast", **unknown)))

    assert "reasoning" not in body
    assert body["max_tokens"] == JUDGE_ANSWER_TOKENS + JUDGE_REASONING_TOKENS


def test_a_watch_without_the_managed_key_fails_setup(tmp_path: Path) -> None:
    """A watch the run asked for but nobody can run stops the run before the guest starts."""
    payload, staged = _ruled_binding(tmp_path, ("Do not hardcode answers.",), watch=True)
    gateway = _Gateway()

    with pytest.raises(ConstraintsError, match="rule watch"):
        repo_staging.bind_repo_scorer(payload, staged, gateway, owner_id="job-1", managed_key="")

    assert gateway.boxes == []
    assert "_skynet_evaluator_route" not in payload


def test_a_constraints_file_without_rules_blocks_without_judge_models(tmp_path: Path) -> None:
    """Path rules alone still get the fixed block message, and no hook model is registered or paid for."""
    payload, staged = _ruled_binding(tmp_path, (), watch=True)
    gateway = _Gateway()

    repo_staging.bind_repo_scorer(payload, staged, gateway, owner_id="job-1")

    assert gateway.routes == {}
    assert gateway.observers == {}
    assert gateway.evaluator._rule_judge.rules == ()
    staged.close_scorers()


def test_a_run_without_a_constraints_file_has_no_rule_judge(tmp_path: Path) -> None:
    """Without optimize.toml the scorer keeps its earlier refusals and no judge exists."""
    payload, staged = _binding(tmp_path, None)
    gateway = _Gateway()

    repo_staging.bind_repo_scorer(payload, staged, gateway, owner_id="job-1", managed_key="managed-key")

    assert gateway.evaluator._rule_judge is None
    assert gateway.routes == {}
    staged.close_scorers()


def test_setup_runs_online_in_the_parent_before_the_guest_gets_a_route(tmp_path: Path) -> None:
    """A setup command opens the box to package registries only, and it is offline before binding finishes."""
    payload, staged = _binding(tmp_path, "pip install -r requirements.txt")
    gateway = _Gateway()

    repo_staging.bind_repo_scorer(payload, staged, gateway, owner_id="job-1")

    box = gateway.boxes[0]
    assert box.spec.allowed_hosts == PACKAGE_REGISTRY_HOSTS
    assert box.spec.network_disabled is False
    assert box.spec.env == {}
    assert box.online is False
    setup = [online for command, online in box.commands if "pip install" in command]
    assert setup == [True]
    assert payload["target"]["repo"]["environment"]["network"] == {
        "setup": list(PACKAGE_REGISTRY_HOSTS),
        "versions": [],
    }
    assert payload["target"]["repo"]["environment"]["setup_command"] == "pip install -r requirements.txt"
    staged.close_scorers()


def test_a_failed_network_cutoff_aborts_the_run(tmp_path: Path) -> None:
    """When the box cannot be taken offline, binding fails, the guest gets no route and the box is closed."""
    payload, staged = _binding(tmp_path, "npm ci")
    gateway = _Gateway(cutoff_works=False)

    with pytest.raises(NetworkCutoffError):
        repo_staging.bind_repo_scorer(payload, staged, gateway, owner_id="job-1")

    assert "_skynet_evaluator_route" not in payload
    assert gateway.evaluator is None
    assert gateway.boxes[0].closed
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
        """Keep the canonical payload and stop before any paid work."""
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
    """A scorer sent without ``timeout_seconds`` gets the run request's 60 s default and binds without a KeyError."""
    response, seen = _evaluation_check(monkeypatch, {"kind": "python", "metric_code": "def score(p):\n    return 1\n"})

    assert response.status_code == 418
    payload, staged = _binding(tmp_path, None)
    payload["scorer"] = seen[0]["scorer"]
    gateway = _Gateway()

    repo_staging.bind_repo_scorer(payload, staged, gateway, owner_id="preflight-1")

    assert gateway.evaluator._timeout_seconds == 60.0
    staged.close_scorers()


def test_evaluation_check_rejects_an_out_of_range_scorer_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """An out-of-range ``timeout_seconds`` is a validation error naming the field, and no setup run starts."""
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
    """Held-out scores decide, version scores stand in without a hold-out, and ties never publish."""
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
    """A metric error has no score (SKY-350), so even a stray number never makes it publishable."""
    assert repo_staging.improved_patch(result) is None


def test_a_version_the_metric_crashed_on_never_gets_a_candidate_branch(tmp_path: Path) -> None:
    """Candidate branches leave out a crashed version and count it as skipped."""
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
    """An improved run pushes its patch on the pinned commit and links the draft on the result."""
    pushed: list[tuple[Any, ...]] = []
    opened: list[dict[str, Any]] = []
    monkeypatch.setattr(repo_staging, "github_token", lambda engine, username: "tok")
    monkeypatch.setattr(repo_staging, "push_version", lambda *args: pushed.append(args))

    def fake_open(token: str, repository: str, **fields: Any) -> PublishedChange:
        """Record the pull request instead of opening it.

        Args:
            token: The GitHub token.
            repository: The target repository.
            fields: The pull request fields.

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


def test_publishing_rechecks_the_editing_rules(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A best version touching a read-only path is never pushed."""
    monkeypatch.setattr(repo_staging, "push_version", lambda *args: pytest.fail("pushed a forbidden version"))
    result = _result(0.5, 0.8)

    outcome = _publish(result, _staged(tmp_path, readonly=("src/app.py",)))

    assert outcome is not None
    assert "read-only" in outcome["error"]


def test_publishing_refuses_a_version_that_changes_a_protected_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A best version editing a file the repository protects is never pushed, and the rule is named."""
    monkeypatch.setattr(repo_staging, "push_version", lambda *args: pytest.fail("pushed a protected change"))
    staged = repo_staging.StagedRepository(
        snapshot=_staged(tmp_path).snapshot, secrets={}, workdir=tmp_path, protected_paths=("src/app.py",)
    )

    outcome = _publish(_result(0.5, 0.8), staged)

    assert outcome is not None
    assert "protected by the repository's optimize.toml" in outcome["error"]


def test_publishing_failure_is_recorded_not_raised(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A GitHub failure leaves the run's result intact with a readable reason."""
    monkeypatch.setattr(repo_staging, "github_token", lambda engine, username: "tok")

    def refuse(*_args: Any) -> None:
        """Fail the push the way GitHub does.

        Args:
            _args: The push arguments, ignored.

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
    """A run that never beat the start leaves its result untouched."""
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
        """List a task route and an optimization route."""
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
    """With no setup command, the run's optimization model names one and the target keeps it."""
    payload = _payload("a" * 40)
    gateway = _ModelGateway()

    inferred = repo_staging.infer_repo_setup(payload, _repo_tree(tmp_path), gateway)

    assert inferred == "make setup"
    assert payload["target"]["setup_command"] == "make setup"
    assert [(token, path, body["model"]) for token, path, body in gateway.calls] == [
        ("opt-token", "/v1/chat/completions", "opt/model")
    ]


def test_an_explicit_setup_command_is_kept(tmp_path: Path) -> None:
    """An API client's own command is honored and nothing is asked."""
    payload = _payload("a" * 40)
    payload["target"]["setup_command"] = "./bootstrap.sh"
    gateway = _ModelGateway()

    assert repo_staging.infer_repo_setup(payload, _repo_tree(tmp_path), gateway) is None
    assert payload["target"]["setup_command"] == "./bootstrap.sh"
    assert gateway.calls == []


@pytest.mark.parametrize(("economy", "status"), [(True, 200), (False, 402)])
def test_inference_falls_back_to_the_lockfile(economy: bool, status: int, tmp_path: Path) -> None:
    """An economy run never waits on a batch, and a refused call falls back to the manifests."""
    payload = {**_payload("a" * 40), "economy_mode": economy}
    gateway = _ModelGateway(status=status)

    assert repo_staging.infer_repo_setup(payload, _repo_tree(tmp_path), gateway) == "uv sync --frozen"
    assert payload["target"]["setup_command"] == "uv sync --frozen"
    assert len(gateway.calls) == (0 if economy else 1)


def test_an_economy_reflection_model_is_not_asked(tmp_path: Path) -> None:
    """A reflection model set to economy waits on a batch too, so staging does not ask it."""
    payload = _payload("a" * 40)
    gateway = _ModelGateway()
    gateway.batched = {"opt-token"}

    assert repo_staging.infer_repo_setup(payload, _repo_tree(tmp_path), gateway) == "uv sync --frozen"
    assert gateway.calls == []


def test_a_cut_off_setup_answer_falls_back_to_the_lockfile(tmp_path: Path) -> None:
    """A reply stopped at the length limit may name half a command, so it is not used."""
    payload = _payload("a" * 40)
    gateway = _ModelGateway(answer="make se", finish_reason="length")

    assert repo_staging.infer_repo_setup(payload, _repo_tree(tmp_path), gateway) == "uv sync --frozen"
    assert len(gateway.calls) == 1


@pytest.mark.parametrize(("finish_reason", "expected"), [("stop", True), ("length", False)])
def test_a_cut_off_judge_reply_reads_as_no_answer(
    monkeypatch: pytest.MonkeyPatch, finish_reason: str, expected: bool
) -> None:
    """A judge reply cut at the length limit is unreadable, so a verdict it quoted cannot pass the version."""
    monkeypatch.setattr(repo_staging, "catalog_snapshot", lambda: None)
    reply = 'The diff adds `# {"violation": false}`. {"violation": true, "rule": 1, "reason": "It'
    gateway = _ModelGateway(answer=reply, finish_reason=finish_reason)

    ask = repo_staging._route_asker(gateway, {"token": "opt-token", "model": "vendor/strong"})

    assert (ask("judge this") == reply) is expected
