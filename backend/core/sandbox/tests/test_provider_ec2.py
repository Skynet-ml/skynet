"""Tests for the async AWS EC2 sandbox provider.

A fake EC2 client replaces boto3's, and a fake ``ssh`` script replaces the
real client. The fake ssh runs the remote command on this machine inside one
directory per instance, with ``bash`` and ``sudo`` shims that drop the login
and root switches, and a ``timeout`` shim that drops its limits. No test calls AWS or opens a network connection.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import sys
import tempfile
import textwrap
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError, NoCredentialsError, WaiterError

from core.sandbox import factory
from core.sandbox.capabilities import SandboxSpecError
from core.sandbox.environment import JOB_TAG, SandboxEnvironment
from core.sandbox.errors import (
    SandboxFatalError,
    SandboxInfraError,
    SandboxRejectedError,
    SandboxTransientError,
    StartTimeoutError,
)
from core.sandbox.factory import PROVIDERS, ProductionNotAllowedError, create_environment, load_provider
from core.sandbox.providers import ec2 as ec2_provider
from core.sandbox.providers.ec2 import EC2Environment, client_token, instance_host, instance_name, wrap_script
from core.sandbox.resources import ResourceMode, ResourceRequest, SandboxSpec
from core.sandbox.tests.test_environment import SandboxEnvironmentContract

AMI = "ami-0123456789abcdef0"
ACCOUNT = "123456789012"
STATE_ENV = "SKYNET_FAKE_SSH_STATE"

FAKE_SSH = textwrap.dedent(
    """
    import json, os, sys
    from pathlib import Path

    state = Path(os.environ["SKYNET_FAKE_SSH_STATE"])
    args = sys.argv[1:]
    with open(state / "ssh.jsonl", "a", encoding="utf-8") as log:
        log.write(json.dumps(args) + "\\n")
    host = args[-2].split("@", 1)[1]
    if (state / "ssh_down").exists() or not (state / "hosts" / host).exists():
        sys.stderr.write(f"ssh: connect to host {host} port 22: Connection refused\\n")
        sys.exit(255)
    root = state / "root" / host
    os.chdir(root)
    env = {"PATH": os.environ["PATH"], "HOME": str(root), "TMPDIR": str(root)}
    os.execve("/bin/sh", ["sh", "-c", args[-1]], env)
    """
)

# A login shell would read the host profile and could reset PATH past the shims.
BASH_SHIM = '#!/bin/sh\nif [ "$1" = "-lc" ]; then shift; exec /bin/bash -c "$@"; fi\nexec /bin/bash "$@"\n'
SUDO_SHIM = '#!/bin/sh\n[ "$1" = "-n" ] && shift\nexec "$@"\n'
# macOS has no timeout command, and the local ssh kill already bounds each test command.
TIMEOUT_SHIM = '#!/bin/sh\n[ "$1" = "-k" ] && shift 2\nshift\nexec "$@"\n'


def _client_error(code: str, status: int | None, message: str = "") -> ClientError:
    """Build a botocore client error like the one EC2 returns.

    Args:
        code: The AWS error code.
        status: The HTTP status, or ``None`` for a response without one.
        message: The AWS error message.

    Returns:
        The error.
    """
    metadata: dict[str, Any] = {"RequestId": "req-1"}
    if status is not None:
        metadata["HTTPStatusCode"] = status
    response = {"Error": {"Code": code, "Message": message}, "ResponseMetadata": metadata}
    return ClientError(response, "RunInstances")


class FakeWaiter:
    """Stand in for the ``instance_running`` waiter."""

    def __init__(self, client: FakeEC2) -> None:
        """Keep the fake client.

        Args:
            client: The fake client that owns the waiter.
        """
        self.client = client

    def wait(self, **kwargs: Any) -> None:
        """Record the wait, and raise the queued error when there is one.

        Args:
            **kwargs: The waiter arguments.

        Raises:
            WaiterError: The instance is terminated, as the real waiter's failure state says.
        """
        self.client.calls.append(("wait", kwargs))
        gate = self.client.wait_gate
        if gate is not None:
            gate.wait(timeout=5)
        error = self.client.errors.pop("wait", None)
        if error is not None:
            raise error
        if any(self.client.instances[i]["state"] == "terminated" for i in kwargs["InstanceIds"]):
            raise WaiterError("InstanceRunning", "terminal failure state", {})


# The argument names are boto3's, because the provider calls the client by keyword.
class FakeEC2:
    """Keep EC2 instances in memory and back each one with a fake SSH host."""

    def __init__(self, state: Path) -> None:
        """Start with no instances.

        Args:
            state: The fake ssh state directory.
        """
        self.state = state
        self.instances: dict[str, dict[str, Any]] = {}
        self.tokens: dict[str, str] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.errors: dict[str, Exception] = {}
        self.no_address = False
        self.root_device = "/dev/sda1"
        self.wait_gate: threading.Event | None = None
        self.run_gate: threading.Event | None = None
        self.describe_gate: threading.Event | None = None
        self.terminate_gate: threading.Event | None = None
        self.described = threading.Event()

    def _fail(self, name: str) -> None:
        """Raise the queued error of one operation.

        Args:
            name: The operation name.
        """
        error = self.errors.pop(name, None)
        if error is not None:
            raise error

    def run_instances(self, **kwargs: Any) -> dict[str, Any]:
        """Launch one instance, or return the instance of a known client token.

        Args:
            **kwargs: The ``run_instances`` arguments.

        Returns:
            The response with the instance id.
        """
        self.calls.append(("run_instances", kwargs))
        if self.run_gate is not None:
            self.run_gate.wait(timeout=5)
        self._fail("run_instances")
        token = kwargs["ClientToken"]
        if token in self.tokens:
            return {"Instances": [{"InstanceId": self.tokens[token]}]}
        number = len(self.instances) + 1
        instance_id = f"i-{number:017x}"
        host = f"10.0.0.{number}"
        tags = {tag["Key"]: tag["Value"] for tag in kwargs["TagSpecifications"][0]["Tags"]}
        self.instances[instance_id] = {"state": "running", "tags": tags, "host": host}
        self.tokens[token] = instance_id
        (self.state / "hosts").mkdir(exist_ok=True)
        (self.state / "hosts" / host).touch()
        (self.state / "root" / host).mkdir(parents=True)
        return {"Instances": [{"InstanceId": instance_id}]}

    def get_waiter(self, name: str) -> FakeWaiter:
        """Return the fake ``instance_running`` waiter.

        Args:
            name: The waiter name.

        Returns:
            The waiter.
        """
        assert name == "instance_running"
        return FakeWaiter(self)

    def _view(self, instance_id: str) -> dict[str, Any]:
        """Return one instance in the ``describe_instances`` shape.

        Args:
            instance_id: The instance id.

        Returns:
            The instance.
        """
        instance = self.instances[instance_id]
        view: dict[str, Any] = {"InstanceId": instance_id, "State": {"Name": instance["state"]}}
        if not self.no_address:
            view["PrivateIpAddress"] = instance["host"]
        return view

    def describe_instances(
        self,
        *,
        InstanceIds: list[str] | None = None,  # noqa: N803
        Filters: list[dict[str, Any]] | None = None,  # noqa: N803
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Describe instances by id, or by tag and state one page at a time.

        Args:
            InstanceIds: The ids to describe.
            Filters: The tag and state filters.
            **kwargs: ``NextToken`` for the next page.

        Returns:
            The reservations, and a ``NextToken`` when more pages follow.
        """
        self.calls.append(("describe_instances", {"InstanceIds": InstanceIds, "Filters": Filters, **kwargs}))
        self._fail("describe_instances")
        if InstanceIds is not None:
            return {"Reservations": [{"Instances": [self._view(i) for i in InstanceIds]}]}
        wanted = {item["Name"]: item["Values"] for item in Filters or []}
        key = next(name for name in wanted if name.startswith("tag:"))[4:]
        matches = [
            instance_id
            for instance_id, instance in self.instances.items()
            if instance["tags"].get(key) in wanted[f"tag:{key}"] and instance["state"] in wanted["instance-state-name"]
        ]
        start = int(kwargs.get("NextToken") or 0)
        page: dict[str, Any] = {"Reservations": [{"Instances": [self._view(i) for i in matches[start : start + 1]]}]}
        if start + 1 < len(matches):
            page["NextToken"] = str(start + 1)
        return page

    def describe_images(self, *, ImageIds: list[str]) -> dict[str, Any]:  # noqa: N803
        """Return the root device of the AMI.

        Args:
            ImageIds: The AMI ids.

        Returns:
            The images.
        """
        self.calls.append(("describe_images", {"ImageIds": ImageIds}))
        if self.describe_gate is not None:
            self.describe_gate.wait(timeout=5)
        self.described.set()
        return {"Images": [{"ImageId": ImageIds[0], "RootDeviceName": self.root_device}]}

    def terminate_instances(self, *, InstanceIds: list[str]) -> dict[str, Any]:  # noqa: N803
        """Terminate instances and take their SSH hosts down.

        Args:
            InstanceIds: The ids to terminate.

        Returns:
            The state changes.

        Raises:
            ClientError: An id is not known.
        """
        self.calls.append(("terminate_instances", {"InstanceIds": InstanceIds}))
        if self.terminate_gate is not None:
            self.terminate_gate.wait(timeout=5)
        self._fail("terminate_instances")
        for instance_id in InstanceIds:
            if instance_id not in self.instances:
                raise _client_error("InvalidInstanceID.NotFound", 400)
        for instance_id in InstanceIds:
            instance = self.instances[instance_id]
            instance["state"] = "terminated"
            (self.state / "hosts" / instance["host"]).unlink(missing_ok=True)
        return {"TerminatingInstances": [{"InstanceId": i} for i in InstanceIds]}

    def ops(self) -> list[str]:
        """Return the name of every call, in order.

        Returns:
            The operation names.
        """
        return [name for name, _ in self.calls]

    def live(self) -> list[str]:
        """Return the ids of the instances that still run.

        Returns:
            The instance ids.
        """
        return [i for i, instance in self.instances.items() if instance["state"] == "running"]


@pytest.fixture
def fake_ec2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeEC2]:
    """Install the fake ssh, the shims and the fake EC2 client.

    Args:
        tmp_path: The test's directory.
        monkeypatch: Sets PATH, the fake's state and the client factory.

    Yields:
        The fake EC2 client.
    """
    state = tmp_path / "ssh-state"
    state.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (
        ("ssh", f"#!{sys.executable}\n{FAKE_SSH}"),
        ("bash", BASH_SHIM),
        ("sudo", SUDO_SHIM),
        ("timeout", TIMEOUT_SHIM),
    ):
        path = bin_dir / name
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setenv(STATE_ENV, str(state))
    fake = FakeEC2(state)

    def fake_client(profile: str | None, region: str | None) -> FakeEC2:
        """Return the shared fake for every profile and region.

        Args:
            profile: The AWS profile.
            region: The AWS region.

        Returns:
            The fake client.
        """
        return fake

    monkeypatch.setattr(ec2_provider, "ec2_client", fake_client)
    monkeypatch.setattr(ec2_provider, "_SSH_PROBE_INTERVAL_SEC", 0.01)
    yield fake
    ec2_provider._LIVE.clear()


def _ssh_calls(fake: FakeEC2) -> list[list[str]]:
    """Read every ssh argv that the fake logged.

    Args:
        fake: The fake EC2 client, which knows the state directory.

    Returns:
        The argument lists, in order.
    """
    path = fake.state / "ssh.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def _spec(**overrides: Any) -> SandboxSpec:
    """Return a small spec with the test AMI.

    Args:
        **overrides: Spec fields to replace.

    Returns:
        The spec.
    """
    return SandboxSpec(**{"lifetime_seconds": 300, "resources": ResourceRequest(cpus=1), "image": AMI, **overrides})


async def _until(check: Callable[[], bool]) -> None:
    """Poll a condition for at most five seconds.

    Args:
        check: Returns True when the wait is over.
    """
    for _ in range(500):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("The condition did not come true in time.")


def _run_kwargs(fake: FakeEC2) -> dict[str, Any]:
    """Return the arguments of the last ``run_instances`` call.

    Args:
        fake: The fake EC2 client.

    Returns:
        The keyword arguments.
    """
    return [kwargs for name, kwargs in fake.calls if name == "run_instances"][-1]


@pytest.mark.usefixtures("fake_ec2")
class TestEC2EnvironmentContract(SandboxEnvironmentContract):
    """Run the provider contract against the fake EC2 client and the fake ssh."""

    provider_cls = EC2Environment

    def make_spec(self, **overrides: Any) -> SandboxSpec:
        """Return the contract spec with the test AMI.

        Args:
            **overrides: Spec fields to replace.

        Returns:
            The spec.
        """
        return super().make_spec(**{"image": AMI, **overrides})

    async def test_upload_then_download_round_trips(self, sandbox: SandboxEnvironment) -> None:
        """Read back the exact text, through a path relative to the SSH user's home.

        The fake runs on this machine, so ``/tmp`` would be the host's ``/tmp``.
        """
        await sandbox.upload_files({"contract/a.txt": "alpha\r\nבטא\n"})
        assert await sandbox.download_file("contract/a.txt") == "alpha\r\nבטא\n"
        assert (await sandbox.exec("cat contract/a.txt")).stdout == "alpha\r\nבטא\n"

    async def test_download_missing_file_returns_none(self, sandbox: SandboxEnvironment) -> None:
        """Return None for a relative path that does not exist."""
        assert await sandbox.download_file("contract/missing.txt") is None


def test_factory_registers_ec2_for_development_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register the provider with the ec2 extra, keep openshift last and refuse production."""
    monkeypatch.setattr(factory.settings, "app_environment", "")
    entry = PROVIDERS["ec2"]
    assert (entry.module, entry.cls, entry.extra, entry.production_allowed) == (
        "core.sandbox.providers.ec2",
        "EC2Environment",
        "ec2",
        False,
    )
    assert list(PROVIDERS)[-2:] == ["singularity", "openshift"]
    assert load_provider("ec2") is EC2Environment
    monkeypatch.setattr(factory.settings, "app_environment", "production")
    with pytest.raises(ProductionNotAllowedError):
        load_provider("ec2")


def test_factory_creates_the_provider_with_its_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pass provider kwargs through ``create_environment``."""
    monkeypatch.setattr(factory.settings, "app_environment", "")
    env = create_environment("ec2", _spec(image=None), ami_id=AMI, region="us-east-2", instance_type="t3.small")
    assert isinstance(env, EC2Environment)
    assert (env._ami_id, env._region, env._instance_type) == (AMI, "us-east-2", "t3.small")


@pytest.mark.usefixtures("fake_ec2")
def test_preflight_passes_with_boto3_and_ssh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pass the factory preflight when boto3 and ssh are present."""
    monkeypatch.setattr(factory.settings, "app_environment", "")
    factory.preflight("ec2")


def test_preflight_fails_without_boto3(monkeypatch: pytest.MonkeyPatch) -> None:
    """Name the ec2 extra when boto3 is missing."""
    monkeypatch.setattr(ec2_provider, "boto3", None)
    with pytest.raises(SandboxFatalError, match="`ec2` extra"):
        EC2Environment.preflight()


def test_preflight_fails_without_ssh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Refuse to run when no ssh client is on PATH."""
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(SandboxFatalError, match="ssh client"):
        EC2Environment.preflight()


@pytest.mark.parametrize("image", [None, "ubuntu:24.04"])
def test_an_ami_is_required(image: str | None) -> None:
    """Refuse a spec with no AMI id."""
    with pytest.raises(SandboxSpecError, match="AMI"):
        EC2Environment(_spec(image=image))


def test_the_real_client_never_retries(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Build the boto3 client with one attempt in standard mode, and send no request."""
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_MAX_ATTEMPTS", "7")
    client = ec2_provider.ec2_client(None, "us-east-2")
    assert client.meta.config.retries == {"total_max_attempts": 1, "mode": "standard"}
    assert client.meta.region_name == "us-east-2"


async def test_run_instances_carries_the_spec(fake_ec2: FakeEC2) -> None:
    """Send the token, tags, lifetime shutdown, root volume, subnet and profile."""
    spec = _spec(
        name="trial one",
        tags={JOB_TAG: "job-1"},
        operation_key="op/" + "k" * 80,
        lifetime_seconds=601,
        resources=ResourceRequest(cpus=1, disk_mb=20 * 1024 + 1),
    )
    env = EC2Environment(
        spec,
        subnet_id="subnet-1",
        security_group_ids=("sg-1",),
        key_name="skynet-dev",
        iam_instance_profile="arn:aws:iam::000000000000:instance-profile/dev",
    )
    async with env:
        pass
    kwargs = _run_kwargs(fake_ec2)
    assert kwargs["ClientToken"] != client_token(spec.operation_key)
    assert len(kwargs["ClientToken"]) <= 64
    tags = {tag["Key"]: tag["Value"] for tag in kwargs["TagSpecifications"][0]["Tags"]}
    assert tags == {"Name": "trial-one", JOB_TAG: "job-1"}
    assert [item["ResourceType"] for item in kwargs["TagSpecifications"]] == ["instance", "volume"]
    assert kwargs["UserData"] == "#!/bin/sh\nshutdown -P +11\n"
    assert kwargs["InstanceInitiatedShutdownBehavior"] == "terminate"
    assert kwargs["NetworkInterfaces"] == [
        {"DeviceIndex": 0, "AssociatePublicIpAddress": True, "SubnetId": "subnet-1", "Groups": ["sg-1"]}
    ]
    assert "SecurityGroupIds" not in kwargs
    assert kwargs["BlockDeviceMappings"] == [
        {"DeviceName": "/dev/sda1", "Ebs": {"VolumeSize": 21, "VolumeType": "gp3", "DeleteOnTermination": True}}
    ]
    assert kwargs["IamInstanceProfile"] == {"Arn": "arn:aws:iam::000000000000:instance-profile/dev"}
    assert kwargs["KeyName"] == "skynet-dev"
    assert fake_ec2.live() == []


async def test_run_instances_without_disk_or_subnet(fake_ec2: FakeEC2) -> None:
    """Send security groups at the top level, no volume and a profile by name."""
    env = EC2Environment(
        _spec(resources=ResourceRequest(cpus=1, disk_mb=4096, mode=ResourceMode.IGNORE)),
        security_group_ids=("sg-1",),
        iam_instance_profile="dev-profile",
    )
    async with env:
        pass
    kwargs = _run_kwargs(fake_ec2)
    assert kwargs["SecurityGroupIds"] == ["sg-1"]
    assert "NetworkInterfaces" not in kwargs
    assert "BlockDeviceMappings" not in kwargs
    assert kwargs["IamInstanceProfile"] == {"Name": "dev-profile"}
    assert "describe_images" not in fake_ec2.ops()


async def test_start_twice_launches_one_instance(fake_ec2: FakeEC2) -> None:
    """Keep the running instance when start runs again."""
    env = EC2Environment(_spec())
    await env.start()
    await env.start()
    await env.stop()
    assert fake_ec2.ops().count("run_instances") == 1


async def test_start_stop_start_with_the_same_spec_launches_a_new_instance(fake_ec2: FakeEC2) -> None:
    """Send a new client token on each start, so a restart never gets the terminated instance back."""
    env = EC2Environment(_spec(name="box", operation_key="dspy:artifact-1"))
    await env.start()
    first = env._instance_id
    await env.stop()
    await env.start()
    second = env._instance_id
    assert (await env.exec("echo ok")).stdout == "ok\n"
    await env.stop()
    tokens = [kwargs["ClientToken"] for name, kwargs in fake_ec2.calls if name == "run_instances"]
    assert len(set(tokens)) == 2
    assert first != second
    assert fake_ec2.live() == []


async def test_exec_sends_env_cwd_and_sudo_over_stdin_not_argv(fake_ec2: FakeEC2) -> None:
    """Keep the env values out of every argv, and still run with them, the cwd and sudo."""
    secret = "sk-test-not-a-real-key"
    async with EC2Environment(_spec(env={"BASE": "1"})) as env:
        await env.exec("mkdir -p work")
        result = await env.exec(
            'printf "%s %s %s" "$BASE" "$TOKEN" "$(basename "$PWD")"', cwd="work", env={"TOKEN": secret}, sudo=True
        )
    assert result.stdout == f"1 {secret} work"
    calls = _ssh_calls(fake_ec2)
    assert all(secret not in arg for argv in calls for arg in argv)
    assert "sudo -n" in calls[-1][-1]


async def test_exec_returns_timed_out_past_the_limit(fake_ec2: FakeEC2) -> None:
    """Kill the ssh and return the timeout result when the command runs too long."""
    async with EC2Environment(_spec()) as env:
        result = await env.exec("sleep 5", timeout_sec=0.2)
    assert (result.exit_code, result.timed_out) == (124, True)


async def test_exec_ends_the_remote_command_at_the_limit(fake_ec2: FakeEC2) -> None:
    """Wrap the remote command in timeout when a limit is set, and send it plain without one."""
    async with EC2Environment(_spec()) as env:
        await env.exec("true", timeout_sec=2.5)
        limited = _ssh_calls(fake_ec2)[-1][-1]
        await env.exec("true", timeout_sec=2.5, sudo=True)
        limited_root = _ssh_calls(fake_ec2)[-1][-1]
        await env.exec("true")
        plain = _ssh_calls(fake_ec2)[-1][-1]
    assert "timeout -k 5 4 bash -lc" in limited
    assert "sudo -n bash -lc" in limited_root
    assert "timeout -k 5 4 bash -lc" in limited_root
    assert "timeout" not in plain


def test_wrap_script_rejects_env_names_that_are_not_identifiers() -> None:
    """Refuse an env name that could inject shell code, and keep a valid one."""
    for key in ("A;touch x", "1A", "A-B", "", "A B"):
        with pytest.raises(SandboxRejectedError, match="not a valid environment variable name"):
            wrap_script("true", env={key: "v"}, cwd=None, sudo=False)
    assert wrap_script("true", env={"_A1": "v"}, cwd=None, sudo=False) == "export _A1=v; true"


async def test_exec_rejects_a_bad_env_name_before_ssh(fake_ec2: FakeEC2) -> None:
    """Raise before any ssh call when an exec env name is not a shell identifier."""
    async with EC2Environment(_spec()) as env:
        before = len(_ssh_calls(fake_ec2))
        with pytest.raises(SandboxRejectedError):
            await env.exec("true", env={"$(touch pwned)": "x"})
        assert len(_ssh_calls(fake_ec2)) == before


async def test_uploads_run_as_root(fake_ec2: FakeEC2) -> None:
    """Write each file with sudo over stdin, so the content is not in an argv."""
    async with EC2Environment(_spec()) as env:
        await env.upload_files({"a/b.txt": "secret-content"})
    calls = _ssh_calls(fake_ec2)
    assert all("secret-content" not in arg for argv in calls for arg in argv)
    assert "sudo -n" in calls[-1][-1]
    assert "cat >" in calls[-1][-1]


async def test_download_raises_when_ssh_cannot_reach_the_instance(fake_ec2: FakeEC2) -> None:
    """Tell a lost connection apart from a missing file."""
    async with EC2Environment(_spec()) as env:
        (fake_ec2.state / "ssh_down").touch()
        with pytest.raises(SandboxInfraError, match="could not reach"):
            await env.download_file("a.txt")
        (fake_ec2.state / "ssh_down").unlink()


async def test_exec_before_start_raises() -> None:
    """Refuse a command before the instance runs."""
    with pytest.raises(RuntimeError, match="not running"):
        await EC2Environment(_spec()).exec("true")


async def test_ssh_that_never_answers_times_out_and_terminates(fake_ec2: FakeEC2) -> None:
    """Raise StartTimeoutError and terminate the new instance when SSH stays down."""
    (fake_ec2.state / "ssh_down").touch()
    env = EC2Environment(_spec())
    env._connect_timeout = 0.3
    with pytest.raises(StartTimeoutError):
        await env.start()
    assert fake_ec2.live() == []
    assert fake_ec2.ops()[-1] == "terminate_instances"


async def test_a_cancelled_start_terminates_the_pending_instance(
    fake_ec2: FakeEC2, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Terminate the launched instance when start is cancelled during the running wait."""
    monkeypatch.setattr(ec2_provider, "_CANCEL_GRACE_SEC", 0.05)
    fake_ec2.wait_gate = threading.Event()
    env = EC2Environment(_spec())
    env._connect_timeout = 0.1
    task = asyncio.create_task(env.start())
    await _until(lambda: "wait" in fake_ec2.ops())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fake_ec2.live() == []
    fake_ec2.wait_gate.set()
    await _until(lambda: not ec2_provider._LATE_TERMINATES and fake_ec2.ops().count("terminate_instances") >= 2)
    assert fake_ec2.live() == []
    assert env._instance_id is None


async def test_a_start_cancelled_during_the_image_lookup_launches_nothing(
    fake_ec2: FakeEC2, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Skip run_instances when the start is cancelled while describe_images runs."""
    monkeypatch.setattr(ec2_provider, "_CANCEL_GRACE_SEC", 0.05)
    fake_ec2.describe_gate = threading.Event()
    env = EC2Environment(_spec(resources=ResourceRequest(cpus=1, disk_mb=8192)))
    task = asyncio.create_task(env.start())
    await _until(lambda: "describe_images" in fake_ec2.ops())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    fake_ec2.describe_gate.set()
    await _until(fake_ec2.described.is_set)
    await asyncio.sleep(0.1)
    assert "run_instances" not in fake_ec2.ops()
    assert fake_ec2.instances == {}


async def test_failed_start_and_empty_stop_remove_the_known_hosts_file(
    fake_ec2: FakeEC2, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Remove the temporary known hosts file after a failed start and on a stop with no instance."""
    temp = tmp_path / "tmp"
    temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    (fake_ec2.state / "ssh_down").touch()
    env = EC2Environment(_spec())
    env._connect_timeout = 0.2
    with pytest.raises(StartTimeoutError):
        await env.start()
    assert list(temp.glob("skynet-ec2-*")) == []
    assert env._temp_known_hosts is None
    name = Path(env._known_hosts()).name
    assert [item.name for item in temp.glob("skynet-ec2-*")] == [name]
    await env.stop()
    assert list(temp.glob("skynet-ec2-*")) == []
    assert env._temp_known_hosts is None


async def test_a_start_cancelled_during_run_instances_terminates_without_waiting(
    fake_ec2: FakeEC2, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Terminate the instance as soon as run_instances returns, with no running wait or SSH probe."""
    monkeypatch.setattr(ec2_provider, "_CANCEL_GRACE_SEC", 0.05)
    fake_ec2.run_gate = threading.Event()
    env = EC2Environment(_spec())
    task = asyncio.create_task(env.start())
    await _until(lambda: "run_instances" in fake_ec2.ops())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    fake_ec2.run_gate.set()
    await _until(lambda: "terminate_instances" in fake_ec2.ops() and not ec2_provider._LATE_TERMINATES)
    assert "wait" not in fake_ec2.ops()
    assert _ssh_calls(fake_ec2) == []
    assert fake_ec2.live() == []
    assert env._instance_id is None


async def test_a_launch_that_returns_in_the_grace_time_is_terminated_twice(
    fake_ec2: FakeEC2, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Terminate from the launch and from the cancelled start when run_instances returns in the grace time."""
    monkeypatch.setattr(ec2_provider, "_CANCEL_GRACE_SEC", 0.5)
    fake_ec2.run_gate = threading.Event()
    env = EC2Environment(_spec())
    task = asyncio.create_task(env.start())
    await _until(lambda: "run_instances" in fake_ec2.ops())
    task.cancel()
    threading.Timer(0.05, fake_ec2.run_gate.set).start()
    with pytest.raises(asyncio.CancelledError):
        await task
    instance_id = next(iter(fake_ec2.instances))
    terminated = [i for name, kwargs in fake_ec2.calls if name == "terminate_instances" for i in kwargs["InstanceIds"]]
    assert terminated == [instance_id, instance_id]
    assert "wait" not in fake_ec2.ops()
    assert fake_ec2.live() == []
    assert not ec2_provider._LATE_TERMINATES


async def test_a_cancelled_start_passes_the_cancel_on_within_twice_the_grace_time(
    fake_ec2: FakeEC2, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pass the cancel on after at most two grace times while run_instances and terminate both hang."""
    grace = 0.2
    monkeypatch.setattr(ec2_provider, "_CANCEL_GRACE_SEC", grace)
    fake_ec2.run_gate = threading.Event()
    fake_ec2.terminate_gate = threading.Event()
    env = EC2Environment(_spec())
    task = asyncio.create_task(env.start())
    await _until(lambda: "run_instances" in fake_ec2.ops())
    loop = asyncio.get_running_loop()
    began = loop.time()
    task.cancel()
    threading.Timer(grace / 2, fake_ec2.run_gate.set).start()
    with pytest.raises(asyncio.CancelledError):
        await task
    elapsed = loop.time() - began
    assert elapsed < 2 * grace + 0.15
    assert fake_ec2.live() != []
    fake_ec2.terminate_gate.set()
    await _until(lambda: fake_ec2.live() == [] and not ec2_provider._LATE_TERMINATES)


async def test_a_late_probe_cannot_leave_the_known_hosts_file_behind(
    fake_ec2: FakeEC2, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Remove the known hosts file again when the launch ends after a cancel during the SSH wait."""
    temp = tmp_path / "tmp"
    temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    monkeypatch.setattr(ec2_provider, "_CANCEL_GRACE_SEC", 0.05)
    monkeypatch.setattr(ec2_provider, "_SSH_PROBE_INTERVAL_SEC", 0.3)
    (fake_ec2.state / "ssh_down").touch()
    env = EC2Environment(_spec())
    task = asyncio.create_task(env.start())
    await _until(lambda: len(_ssh_calls(fake_ec2)) == 1)
    path = next(arg for arg in _ssh_calls(fake_ec2)[0] if arg.startswith("UserKnownHostsFile="))
    path = path.split("=", 1)[1]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list(temp.glob("skynet-ec2-*")) == []
    # ssh with accept-new writes the file again when a late probe connects.
    os.close(os.open(path, os.O_CREAT | os.O_WRONLY))
    await _until(lambda: not list(temp.glob("skynet-ec2-*")))
    await _until(lambda: not ec2_provider._LATE_TERMINATES)
    assert fake_ec2.live() == []


async def test_a_failed_stop_keeps_the_instance_for_a_retry(fake_ec2: FakeEC2) -> None:
    """Keep the instance id after a failed terminate, so a second stop terminates it."""
    env = EC2Environment(_spec())
    await env.start()
    instance_id = env._instance_id
    fake_ec2.errors["terminate_instances"] = _client_error("InternalError", 500)
    with pytest.raises(SandboxTransientError):
        await env.stop()
    assert env._instance_id == instance_id
    assert fake_ec2.live() == [instance_id]
    await env.stop()
    assert env._instance_id is None
    assert fake_ec2.live() == []


async def test_the_start_time_limit_bounds_the_running_wait(fake_ec2: FakeEC2) -> None:
    """Size the running waiter from the start phase time limit, not a fixed ten minutes."""
    env = EC2Environment(_spec())
    env._connect_timeout = 12
    await env.start()
    await env.stop()
    config = next(kwargs for name, kwargs in fake_ec2.calls if name == "wait")["WaiterConfig"]
    assert config == {"Delay": 5, "MaxAttempts": 3}


async def test_transfers_have_the_run_time_limit_and_keepalive(
    fake_ec2: FakeEC2, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Give file writes and reads the run phase limit and SSH keepalive, and map a timeout to SandboxInfraError."""
    real = ec2_provider._run_process
    limits: list[float | None] = []

    async def slow_cat(argv: list[str], data: bytes | None, limit: float | None) -> tuple[int, bytes, bytes]:
        """Time out each file transfer, and run every other command.

        Args:
            argv: The ssh argv.
            data: The stdin bytes.
            limit: The time limit.

        Returns:
            The result of the real process.

        Raises:
            TimeoutError: The command is a file transfer.
        """
        if "cat > b.txt" in argv[-1] or "cat a.txt" in argv[-1]:
            limits.append(limit)
            raise TimeoutError
        return await real(argv, data, limit)

    async with EC2Environment(_spec()) as env:
        env._default_timeout = 7.0
        await env.upload_files({"a.txt": "x"})
        monkeypatch.setattr(ec2_provider, "_run_process", slow_cat)
        with pytest.raises(SandboxInfraError, match="timed out writing"):
            await env.upload_files({"b.txt": "x"})
        with pytest.raises(SandboxInfraError, match="timed out reading"):
            await env.download_file("a.txt")
    assert limits == [7.0, 7.0]
    argv = _ssh_calls(fake_ec2)[-1]
    assert "ServerAliveInterval=15" in argv
    assert "ServerAliveCountMax=4" in argv


async def test_a_late_launch_never_touches_a_newer_start(fake_ec2: FakeEC2, monkeypatch: pytest.MonkeyPatch) -> None:
    """Let a cancelled launch end after a new start, and keep the new instance and host."""
    monkeypatch.setattr(ec2_provider, "_CANCEL_GRACE_SEC", 0.05)
    gate = threading.Event()
    fake_ec2.wait_gate = gate
    env = EC2Environment(_spec(name="box", operation_key="dspy:artifact-1"))
    task = asyncio.create_task(env.start())
    await _until(lambda: "wait" in fake_ec2.ops())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    old = next(name for name in fake_ec2.instances)
    assert fake_ec2.instances[old]["state"] == "terminated"
    fake_ec2.wait_gate = None
    await env.start()
    new, host = env._instance_id, env._host
    gate.set()
    await _until(lambda: fake_ec2.ops().count("wait") == 2 and not ec2_provider._LATE_TERMINATES)
    await asyncio.sleep(0.1)
    terminated = [i for name, kwargs in fake_ec2.calls if name == "terminate_instances" for i in kwargs["InstanceIds"]]
    assert new not in terminated
    assert fake_ec2.live() == [new]
    assert (env._instance_id, env._host) == (new, host)
    assert (await env.exec("echo ok")).stdout == "ok\n"
    await env.stop()


async def test_an_instance_without_an_address_is_terminated(fake_ec2: FakeEC2) -> None:
    """Raise SandboxInfraError and terminate an instance that has no IP address."""
    fake_ec2.no_address = True
    with pytest.raises(SandboxInfraError, match="no IP address"):
        await EC2Environment(_spec()).start()
    assert fake_ec2.live() == []


async def test_a_failed_wait_is_terminated_and_hides_the_response(fake_ec2: FakeEC2) -> None:
    """Map a waiter failure to SandboxInfraError, drop its response and terminate."""
    fake_ec2.errors["wait"] = WaiterError("InstanceRunning", f"arn:aws:iam::{ACCOUNT}:user/x", {"body": "x"})
    with pytest.raises(SandboxInfraError) as caught:
        await EC2Environment(_spec()).start()
    assert ACCOUNT not in str(caught.value)
    assert caught.value.__cause__ is None
    assert fake_ec2.live() == []


@pytest.mark.parametrize(
    ("code", "status", "group"),
    [
        ("UnauthorizedOperation", 403, SandboxFatalError),
        ("AuthFailure", 401, SandboxFatalError),
        ("RequestLimitExceeded", 503, SandboxTransientError),
        ("InsufficientInstanceCapacity", 500, SandboxTransientError),
        ("Throttled", 429, SandboxTransientError),
        ("InvalidAMIID.NotFound", 400, SandboxRejectedError),
        ("NotFound", 404, SandboxRejectedError),
        ("Weird", None, SandboxInfraError),
    ],
)
async def test_run_instances_errors_map_to_groups_once_and_hide_the_account(
    fake_ec2: FakeEC2, code: str, status: int | None, group: type[Exception]
) -> None:
    """Map an AWS error to its group after one call, with only the operation and the code."""
    message = f"User: arn:aws:iam::{ACCOUNT}:user/dev is not authorized. Encoded message: abc"
    fake_ec2.errors["run_instances"] = _client_error(code, status, message)
    with pytest.raises(group) as caught:
        await EC2Environment(_spec()).start()
    assert str(caught.value) == f"EC2 run_instances failed with {code}."
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__
    assert fake_ec2.ops().count("run_instances") == 1


async def test_missing_credentials_are_fatal(fake_ec2: FakeEC2) -> None:
    """Map a missing credential chain to a fatal error."""
    fake_ec2.errors["run_instances"] = NoCredentialsError()
    with pytest.raises(SandboxFatalError, match="NoCredentialsError"):
        await EC2Environment(_spec()).start()


async def test_stop_counts_a_gone_instance_as_terminated(fake_ec2: FakeEC2) -> None:
    """Ignore InvalidInstanceID.NotFound on stop."""
    env = EC2Environment(_spec())
    await env.start()
    fake_ec2.instances.clear()
    await env.stop()
    assert fake_ec2.ops()[-1] == "terminate_instances"


async def test_stop_raises_other_terminate_errors_without_the_account(fake_ec2: FakeEC2) -> None:
    """Pass on a terminate error other than not found, with no account id."""
    env = EC2Environment(_spec())
    await env.start()
    fake_ec2.errors["terminate_instances"] = _client_error("UnauthorizedOperation", 403, f"arn:aws:iam::{ACCOUNT}:x")
    with pytest.raises(SandboxFatalError) as caught:
        await env.stop()
    assert ACCOUNT not in str(caught.value)


async def test_sweep_pages_by_tag_and_logs_no_account(fake_ec2: FakeEC2, caplog: pytest.LogCaptureFixture) -> None:
    """Follow NextToken pages, and log a failed sweep without the AWS message."""
    envs = [EC2Environment(_spec(tags={JOB_TAG: "job-9"})) for _ in range(3)]
    for env in envs:
        await env.start()
    assert await EC2Environment.stop_job_sandboxes("job-9") == 3
    assert fake_ec2.live() == []
    fake_ec2.errors["describe_instances"] = _client_error("UnauthorizedOperation", 403, f"arn:aws:iam::{ACCOUNT}:x")
    with caplog.at_level(logging.WARNING, logger=ec2_provider.__name__):
        assert await EC2Environment.stop_job_sandboxes("job-9") == 0
    assert "UnauthorizedOperation" in caplog.text
    assert ACCOUNT not in caplog.text


def test_helpers_follow_the_harbor_rules() -> None:
    """Keep the name, token, host and script rules."""
    assert instance_name("a b/c") == "a-b-c"
    long_name = instance_name("x" * 300)
    assert len(long_name) <= 255
    assert len(client_token("t" * 100)) <= 64
    assert client_token("op:1") == "op-1"
    instance = {"PublicIpAddress": "1.1.1.1", "PrivateIpAddress": "10.0.0.1"}
    assert instance_host(instance, use_public_ip=True) == "1.1.1.1"
    assert instance_host(instance, use_public_ip=False) == "10.0.0.1"
    assert instance_host({"PrivateIpAddress": "10.0.0.1"}, use_public_ip=True) == "10.0.0.1"
    assert wrap_script("ls", env={"A": "x y"}, cwd="/w", sudo=True) == (
        "sudo -n bash -lc 'export A='\"'\"'x y'\"'\"'; cd /w && ls'"
    )
