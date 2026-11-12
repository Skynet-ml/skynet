"""Tests for the async OpenShift sandbox provider.

A fake ``oc`` script replaces the real CLI. It keeps each "pod" as a JSON
manifest and a directory in ``tmp_path``, and runs ``oc exec`` commands on
this machine inside that directory. No test reaches a cluster.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import textwrap
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from core.sandbox import factory
from core.sandbox.capabilities import SandboxSpecError
from core.sandbox.environment import JOB_TAG, SandboxEnvironment
from core.sandbox.errors import SandboxFatalError, SandboxInfraError, StartTimeoutError
from core.sandbox.factory import PROVIDERS, ProductionNotAllowedError, create_environment, load_provider
from core.sandbox.providers import openshift as openshift_provider
from core.sandbox.providers.openshift import OpenshiftEnvironment, _job_label, _pod_name
from core.sandbox.resources import ResourceMode, ResourceRequest, SandboxSpec
from core.sandbox.tests.test_environment import SandboxEnvironmentContract

IMAGE = "registry.example/skynet/task:1"
STATE_ENV = "SKYNET_FAKE_OC_STATE"

FAKE_OC = textwrap.dedent(
    """
    import json, os, sys
    from pathlib import Path

    state = Path(os.environ["SKYNET_FAKE_OC_STATE"])
    pods = state / "pods"
    pods.mkdir(parents=True, exist_ok=True)
    args = sys.argv[1:]
    with open(state / "calls.jsonl", "a", encoding="utf-8") as log:
        log.write(json.dumps(args) + "\\n")
    if args[:1] == ["-n"]:
        args = args[2:]
    verb = args[0] if args else ""

    def fail(name):
        path = state / name
        if path.exists():
            sys.stderr.write(path.read_text())
            sys.exit(1)

    if verb == "whoami":
        fail("whoami_fail")
        print("developer")
    elif verb == "apply":
        fail("apply_error")
        manifest = json.loads(sys.stdin.read())
        name = manifest["metadata"]["name"]
        (pods / f"{name}.json").write_text(json.dumps(manifest))
        (state / "root" / name / "tmp").mkdir(parents=True, exist_ok=True)
        print(f"pod/{name} created")
    elif verb == "get" and args[1] == "pod":
        name = args[2]
        if not (pods / f"{name}.json").exists():
            sys.stderr.write(f'Error from server (NotFound): pods "{name}" not found\\n')
            sys.exit(1)
        override = state / "status.json"
        if override.exists():
            status = json.loads(override.read_text())
        else:
            status = {"phase": "Running", "containerStatuses": [{"name": "main", "ready": True}]}
        print(json.dumps({"status": status}))
    elif verb == "get" and args[1] == "pods":
        key, value = args[args.index("-l") + 1].split("=", 1)
        for path in sorted(pods.glob("*.json")):
            labels = json.loads(path.read_text())["metadata"]["labels"]
            if labels.get(key) == value:
                print(f"pod/{path.stem}")
    elif verb == "delete":
        fail("delete_fail")
        name = args[2]
        (pods / f"{name}.json").unlink(missing_ok=True)
        print(f'pod "{name}" deleted')
    elif verb == "exec":
        name = args[args.index("-c") - 1]
        if not (pods / f"{name}.json").exists():
            sys.stderr.write(f'Error from server (NotFound): pods "{name}" not found\\n')
            sys.exit(1)
        root = state / "root" / name
        os.chdir(root)
        env = {"PATH": os.environ["PATH"], "TMPDIR": str(root / "tmp")}
        command = args[args.index("--") + 1 :]
        os.execvpe(command[0], command, env)
    else:
        sys.stderr.write(f"fake oc: unknown call {args}\\n")
        sys.exit(2)
    """
)


@pytest.fixture
def oc_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Install the fake ``oc`` and point the provider at it.

    Args:
        tmp_path: The test's directory.
        monkeypatch: Sets the class defaults and the fake's state directory.

    Returns:
        The fake's state directory.
    """
    state = tmp_path / "oc-state"
    state.mkdir()
    oc_bin = tmp_path / "oc"
    oc_bin.write_text(f"#!{sys.executable}\n{FAKE_OC}")
    oc_bin.chmod(oc_bin.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv(STATE_ENV, str(state))
    monkeypatch.setattr(OpenshiftEnvironment, "default_oc_bin", str(oc_bin))
    monkeypatch.setattr(OpenshiftEnvironment, "default_namespace", None)
    monkeypatch.setattr(OpenshiftEnvironment, "poll_seconds", 0.0)
    return state


def _calls(state: Path) -> list[list[str]]:
    """Read every ``oc`` call that the fake logged.

    Args:
        state: The fake's state directory.

    Returns:
        The argument lists, in order.
    """
    path = state / "calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def _verbs(state: Path) -> list[str]:
    """Return the verb of every logged ``oc`` call.

    Args:
        state: The fake's state directory.

    Returns:
        The verbs, in order.
    """
    return [(call[2:] if call[:1] == ["-n"] else call)[0] for call in _calls(state)]


def _spec(**overrides: Any) -> SandboxSpec:
    """Return a small spec with an image.

    Args:
        **overrides: Spec fields to replace.

    Returns:
        The spec.
    """
    return SandboxSpec(**{"lifetime_seconds": 300, "resources": ResourceRequest(cpus=1), "image": IMAGE, **overrides})


def _manifest(state: Path, env: OpenshiftEnvironment) -> dict[str, Any]:
    """Read the manifest that the fake stored for a pod.

    Args:
        state: The fake's state directory.
        env: The started sandbox.

    Returns:
        The pod manifest.
    """
    return json.loads((state / "pods" / f"{env.pod_name}.json").read_text())


@pytest.mark.usefixtures("oc_state")
class TestOpenshiftEnvironmentContract(SandboxEnvironmentContract):
    """Run the provider contract against the fake ``oc``."""

    provider_cls = OpenshiftEnvironment

    def make_spec(self, **overrides: Any) -> SandboxSpec:
        """Return the contract spec with the test image.

        Args:
            **overrides: Spec fields to replace.

        Returns:
            The spec.
        """
        return super().make_spec(**{"image": IMAGE, **overrides})

    async def test_upload_then_download_round_trips(self, sandbox: SandboxEnvironment) -> None:
        """Read back the exact text, through a path relative to the container's cwd.

        The fake runs on this machine, so ``/tmp`` would be the host's ``/tmp``.
        """
        await sandbox.upload_files({"contract/a.txt": "alpha\r\nבטא\n"})
        assert await sandbox.download_file("contract/a.txt") == "alpha\r\nבטא\n"
        assert (await sandbox.exec("cat contract/a.txt")).stdout == "alpha\r\nבטא\n"

    async def test_download_missing_file_returns_none(self, sandbox: SandboxEnvironment) -> None:
        """Return None for a relative path that does not exist."""
        assert await sandbox.download_file("contract/missing.txt") is None


def test_factory_registers_openshift_for_development_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register the provider with no extra and refuse it in production."""
    monkeypatch.setattr(factory.settings, "app_environment", "")
    entry = PROVIDERS["openshift"]
    assert (entry.module, entry.cls, entry.extra, entry.production_allowed) == (
        "core.sandbox.providers.openshift",
        "OpenshiftEnvironment",
        None,
        False,
    )
    assert list(PROVIDERS)[-1] == "openshift"
    assert load_provider("openshift") is OpenshiftEnvironment
    monkeypatch.setattr(factory.settings, "app_environment", "production")
    with pytest.raises(ProductionNotAllowedError):
        load_provider("openshift")


def test_factory_creates_the_provider_with_its_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pass provider kwargs through ``create_environment``."""
    monkeypatch.setattr(factory.settings, "app_environment", "")
    env = create_environment("openshift", _spec(image=None), image=IMAGE, namespace="team-a")
    assert isinstance(env, OpenshiftEnvironment)
    assert env._image == IMAGE
    assert env._namespace == "team-a"


def test_preflight_passes_when_oc_is_logged_in(oc_state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run ``oc whoami`` through the factory and return when it succeeds."""
    monkeypatch.setattr(factory.settings, "app_environment", "")
    factory.preflight("openshift")
    assert _verbs(oc_state) == ["whoami"]


def test_preflight_fails_when_oc_is_not_logged_in(oc_state: Path) -> None:
    """Raise SandboxFatalError when ``oc whoami`` fails."""
    (oc_state / "whoami_fail").write_text("error: You must be logged in to the server (Unauthorized)\n")
    with pytest.raises(SandboxFatalError, match="oc login"):
        OpenshiftEnvironment.preflight()


def test_preflight_fails_when_oc_is_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Raise SandboxFatalError when no ``oc`` is on PATH."""
    monkeypatch.setattr(OpenshiftEnvironment, "default_oc_bin", str(tmp_path / "no-oc"))
    with pytest.raises(SandboxFatalError, match="needs the oc CLI"):
        OpenshiftEnvironment.preflight()


def test_an_image_is_required() -> None:
    """Refuse a sandbox with no image: the provider builds none."""
    with pytest.raises(SandboxSpecError, match="needs an image"):
        OpenshiftEnvironment(_spec(image=None))


def test_a_disabled_network_is_refused() -> None:
    """Refuse a spec that asks for no egress: the provider only has open egress."""
    with pytest.raises(SandboxSpecError):
        OpenshiftEnvironment(_spec(network_disabled=True))


def test_the_spec_image_wins_over_the_provider_image() -> None:
    """Use the spec's image when both name one."""
    assert OpenshiftEnvironment(_spec(), image="other:1")._image == IMAGE


async def test_the_manifest_bounds_the_pod_and_carries_the_job_label(oc_state: Path) -> None:
    """Write a manifest with the deadline, labels, ServiceAccount and security settings."""
    async with OpenshiftEnvironment(_spec(lifetime_seconds=90.2, tags={JOB_TAG: "job-1"}, name="My Task")) as env:
        manifest = _manifest(oc_state, env)
    assert env.pod_name is not None
    assert env.pod_name.startswith("my-task-")
    assert manifest["metadata"]["labels"] == {"skynet": "sandbox", "skynet.job": "job-1"}
    pod = manifest["spec"]
    assert pod["activeDeadlineSeconds"] == 91
    assert pod["restartPolicy"] == "Never"
    assert pod["serviceAccountName"] == "skynet-task"
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"] == {"runAsUser": 0}
    container = pod["containers"][0]
    assert container["name"] == "main"
    assert container["image"] == IMAGE
    assert "env" not in container
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert "NET_RAW" not in container["securityContext"]["capabilities"]["add"]


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (ResourceMode.REQUEST, {"requests": {"cpu": "2", "memory": "512Mi", "ephemeral-storage": "1024Mi"}}),
        (ResourceMode.AUTO, {"requests": {"cpu": "2", "memory": "512Mi", "ephemeral-storage": "1024Mi"}}),
        (ResourceMode.LIMIT, {"limits": {"cpu": "2", "memory": "512Mi", "ephemeral-storage": "1024Mi"}}),
        (
            ResourceMode.GUARANTEE,
            {
                "requests": {"cpu": "2", "memory": "512Mi", "ephemeral-storage": "1024Mi"},
                "limits": {"cpu": "2", "memory": "512Mi", "ephemeral-storage": "1024Mi"},
            },
        ),
        (ResourceMode.IGNORE, None),
    ],
)
def test_resources_follow_the_resource_mode(mode: ResourceMode, expected: dict[str, Any] | None) -> None:
    """Map each resource mode onto pod requests and limits."""
    spec = _spec(resources=ResourceRequest(cpus=2, memory_mb=512, disk_mb=1024, mode=mode))
    container = OpenshiftEnvironment(spec)._manifest("pod-1")["spec"]["containers"][0]
    assert container.get("resources") == expected


async def test_env_values_stay_off_the_command_line_and_the_manifest(oc_state: Path) -> None:
    """Send spec and exec env values on stdin only."""
    async with OpenshiftEnvironment(_spec(env={"SPEC_SECRET": "spec-value-123"})) as env:
        result = await env.exec('printf "%s %s" "$SPEC_SECRET" "$EXEC_SECRET"', env={"EXEC_SECRET": "exec-value-456"})
        manifest = json.dumps(_manifest(oc_state, env))
    assert result.stdout == "spec-value-123 exec-value-456"
    logged = json.dumps(_calls(oc_state))
    for value in ("spec-value-123", "exec-value-456"):
        assert value not in logged
        assert value not in manifest


async def test_exec_runs_in_the_cwd(oc_state: Path) -> None:
    """Change into the working directory before the command runs."""
    async with OpenshiftEnvironment(_spec()) as env:
        await env.upload_files({"work dir/x.txt": "x"})
        result = await env.exec("cat x.txt", cwd="work dir")
    assert result.stdout == "x"


async def test_exec_timeout_kills_the_command_and_reports_timed_out(oc_state: Path) -> None:
    """Kill the command at its limit and return ``timed_out=True``."""
    async with OpenshiftEnvironment(_spec()) as env:
        started = time.monotonic()
        result = await env.exec("sleep 30", timeout_sec=1)
        elapsed = time.monotonic() - started
        assert result.timed_out
        assert result.exit_code == 124
        assert elapsed < 10
        plain = await env.exec("exit 124", timeout_sec=5)
        assert plain.exit_code == 124
        assert not plain.timed_out


def _alive(pid: int) -> bool:
    """Say whether a process still runs.

    Args:
        pid: The process id.

    Returns:
        True while the process exists.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def test_a_cancelled_exec_kills_the_command(oc_state: Path) -> None:
    """Kill the command's process group when the caller cancels the exec."""
    async with OpenshiftEnvironment(_spec()) as env:
        pid_file = oc_state / "root" / str(env.pod_name) / "pid"
        task = asyncio.create_task(env.exec("echo $$ > pid; exec sleep 30"))
        for _ in range(200):
            if pid_file.exists() and pid_file.read_text().strip():
                break
            await asyncio.sleep(0.05)
        pid = int(pid_file.read_text())
        assert _alive(pid)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(100):
            if not _alive(pid):
                break
            await asyncio.sleep(0.05)
        assert not _alive(pid)
        assert (await env.exec("echo still")).stdout.strip() == "still"


async def test_exec_before_start_raises(oc_state: Path) -> None:
    """Refuse to run a command before start."""
    with pytest.raises(RuntimeError, match="not running"):
        await OpenshiftEnvironment(_spec()).exec("true")


async def test_upload_refuses_an_empty_path(oc_state: Path) -> None:
    """Refuse an empty sandbox path."""
    async with OpenshiftEnvironment(_spec()) as env:
        with pytest.raises(SandboxSpecError):
            await env.upload_files({"": "x"})


@pytest.mark.parametrize(
    "stderr",
    [
        'Error from server (Forbidden): pods "x" is forbidden: error looking up service account ns/skynet-task: '
        'serviceaccount "skynet-task" not found\n',
        "error: You must be logged in to the server (Unauthorized)\n",
    ],
)
async def test_auth_and_service_account_failures_are_fatal(oc_state: Path, stderr: str) -> None:
    """Raise SandboxFatalError, and still delete the pod name, when the cluster refuses the login or RBAC."""
    (oc_state / "apply_error").write_text(stderr)
    env = OpenshiftEnvironment(_spec())
    with pytest.raises(SandboxFatalError, match="ServiceAccount"):
        async with env:
            pass
    assert _verbs(oc_state) == ["apply", "delete"]


async def test_other_apply_failures_are_infra_errors(oc_state: Path) -> None:
    """Raise SandboxInfraError for a refusal that is not about auth."""
    (oc_state / "apply_error").write_text("Error from server (InternalError): etcdserver: request timed out\n")
    with pytest.raises(SandboxInfraError, match="etcdserver"):
        await OpenshiftEnvironment(_spec()).start()


@pytest.mark.parametrize(
    ("status", "match"),
    [
        ({"phase": "Failed", "reason": "Evicted"}, "phase Failed"),
        (
            {
                "phase": "Pending",
                "containerStatuses": [
                    {"name": "main", "ready": False, "state": {"waiting": {"reason": "ImagePullBackOff"}}}
                ],
            },
            "ImagePullBackOff",
        ),
    ],
)
async def test_a_failed_pod_raises_once_without_a_retry(oc_state: Path, status: dict[str, Any], match: str) -> None:
    """Raise SandboxInfraError for a terminal phase or an image pull failure, after one apply."""
    (oc_state / "status.json").write_text(json.dumps(status))
    env = OpenshiftEnvironment(_spec())
    with pytest.raises(SandboxInfraError, match=match):
        async with env:
            pass
    verbs = _verbs(oc_state)
    assert verbs.count("apply") == 1
    assert verbs[-1] == "delete"


async def test_a_pod_that_stays_pending_times_out(oc_state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Raise StartTimeoutError when the pod is not ready in time."""
    monkeypatch.setattr(OpenshiftEnvironment, "ready_seconds", 0.2)
    monkeypatch.setattr(OpenshiftEnvironment, "poll_seconds", 0.05)
    (oc_state / "status.json").write_text(json.dumps({"phase": "Pending"}))
    with pytest.raises(StartTimeoutError):
        async with OpenshiftEnvironment(_spec()):
            pass
    assert _verbs(oc_state)[-1] == "delete"


async def test_start_twice_raises(oc_state: Path) -> None:
    """Refuse a second start of the same sandbox."""
    async with OpenshiftEnvironment(_spec()) as env:
        with pytest.raises(RuntimeError, match="already started"):
            await env.start()


async def test_stop_deletes_once_and_logs_a_failure(oc_state: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Delete the pod once; log a failed delete and do not raise."""
    env = OpenshiftEnvironment(_spec())
    await env.start()
    (oc_state / "delete_fail").write_text("error: connection refused\n")
    await env.stop()
    await env.stop()
    assert _verbs(oc_state).count("delete") == 1
    assert "connection refused" in caplog.text


async def test_stop_job_sandboxes_continues_after_a_failure(oc_state: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Count only the confirmed deletes, and try every pod."""
    job_id = f"job-{uuid.uuid4().hex}"
    envs = [OpenshiftEnvironment(_spec(tags={JOB_TAG: job_id})) for _ in range(3)]
    for env in envs:
        await env.start()
    failing = envs[0].pod_name
    real_delete = openshift_provider._delete_pod

    async def flaky_delete(oc_bin: str, namespace: str | None, name: str) -> bool:
        """Fail the delete of the first pod only."""
        if name == failing:
            return False
        return await real_delete(oc_bin, namespace, name)

    monkeypatch.setattr(openshift_provider, "_delete_pod", flaky_delete)
    assert await OpenshiftEnvironment.stop_job_sandboxes(job_id) == 2
    assert _verbs(oc_state).count("delete") == 2
    monkeypatch.undo()
    for env in envs:
        await env.stop()


async def test_namespace_reaches_every_call(oc_state: Path) -> None:
    """Pass ``-n`` to every ``oc`` call when a namespace is set."""
    async with OpenshiftEnvironment(_spec(tags={JOB_TAG: "j"}), namespace="team-a") as env:
        await env.exec("true")
    await OpenshiftEnvironment.stop_job_sandboxes("j", namespace="team-a")
    assert all(call[:2] == ["-n", "team-a"] for call in _calls(oc_state))


def test_pod_names_are_dns_safe() -> None:
    """Turn any stem into a short DNS-1123 name with a random suffix."""
    name = _pod_name("Hello_World!!" + "x" * 80)
    assert name.startswith("hello-world-")
    assert len(name) <= 59
    assert all(char in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in name)
    assert _pod_name(None).startswith("skynet-")
    assert _pod_name("***").startswith("skynet-")
    assert _pod_name("a") != _pod_name("a")


def test_job_labels_are_valid_label_values() -> None:
    """Keep a valid job id, and hash one that a label cannot hold."""
    assert _job_label("job-1") == "job-1"
    hashed = _job_label("job/with spaces")
    assert hashed.startswith("sha256-")
    assert len(hashed) == 63
    assert _job_label("x" * 64) != _job_label("x" * 65)
    assert _job_label("job/with spaces") == hashed
