"""The async AWS EC2 sandbox provider, for dev and evaluation only.

Adapted from Harbor 0.25.0 (Apache-2.0), harbor/environments/ec2.py: the
``boto3`` EC2 client, the ``run_instances`` call with its client token,
tags, network interface, root volume and instance profile, the root device
lookup, the ``instance_running`` waiter, the host choice between the public
and private address, the instance name and client token rules, the SSH
command line, the ``bash -lc`` script wrapper with env, cwd and sudo, the
SSH readiness probe and the terminate on stop come from it. Changes:

- The provider implements the SBX-6 async contract. The factory refuses it
  in production (SKY-406 D1).
- The AWS calls never retry (SKY-406 D3, D10).
- AWS errors keep only the operation and the error code, so no response
  body or account identifier reaches an error or a log.
- The exec script and its env go over SSH stdin, so no credential is in an
  argv. An env name must be a shell identifier. A remote ``timeout`` ends
  the command at the exec limit.
- Each start sends a new client token, so a restart never gets back the
  instance of an earlier start.
- The instance shuts itself down and terminates at the end of the spec
  lifetime.
- The job sweep terminates the instances by tag.
- Files move over SSH stdin one file at a time.

Harbor's Docker Compose, Docker-in-Docker, Docker bootstrap, cloud-init,
attach mode, trial paths and directory transfer are left out.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import logging
import math
import os
import re
import shlex
import shutil
import tempfile
import uuid
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar

from ..capabilities import EnvironmentCapabilities, SandboxSpecError
from ..environment import JOB_TAG, ExecResult, OutputSink, SandboxEnvironment
from ..errors import (
    SandboxError,
    SandboxFatalError,
    SandboxInfraError,
    SandboxRejectedError,
    SandboxTransientError,
    StartTimeoutError,
)
from ..resources import ResourceMode, SandboxSpec
from ..telemetry import current_output_sink
from ..timeouts import Phase, PhaseTimeouts

try:
    import boto3
    from botocore.config import Config as BotoConfig
    from botocore.exceptions import (
        BotoCoreError,
        ClientError,
        NoCredentialsError,
        NoRegionError,
        PartialCredentialsError,
        ProfileNotFound,
        WaiterError,
    )
except ImportError:
    boto3 = None
    BotoConfig = None
    BotoCoreError = None
    ClientError = None
    NoCredentialsError = None
    NoRegionError = None
    PartialCredentialsError = None
    ProfileNotFound = None
    WaiterError = None

logger = logging.getLogger(__name__)

# EC2 has no egress policy that the provider sets, and the instance type sets
# the CPU and memory size, so the capabilities keep the defaults.
EC2_CAPABILITIES = EnvironmentCapabilities(name="ec2")

DEFAULT_INSTANCE_TYPE = "m7i-flex.large"
DEFAULT_ROOT_DEVICE = "/dev/xvda"
DEFAULT_SSH_CONNECT_TIMEOUT_SEC = 300.0
# One attempt for every AWS call (SKY-406 D3, D10). "standard" is the only
# mode that reads total_max_attempts.
NO_RETRY = {"total_max_attempts": 1, "mode": "standard"}
_RUNNING_WAITER_DELAY_SEC = 5
_SWEEP_STATES = ["pending", "running", "stopping", "stopped"]
_GONE_CODES = frozenset({"InvalidInstanceID.NotFound", "InvalidInstanceID.Malformed"})
_AUTH_CODES = frozenset(
    {
        "AuthFailure",
        "Blocked",
        "ExpiredToken",
        "InvalidClientTokenId",
        "OptInRequired",
        "SignatureDoesNotMatch",
        "UnauthorizedOperation",
        "UnrecognizedClientException",
    }
)
_TRANSIENT_CODES = frozenset(
    {
        "InsufficientInstanceCapacity",
        "InternalError",
        "RequestLimitExceeded",
        "ServiceUnavailable",
        "Throttling",
        "ThrottlingException",
        "Unavailable",
    }
)
_CODE_CHARS = re.compile(r"[^A-Za-z0-9.]")
_ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SSH_PROBE_TIMEOUT_SEC = 10.0
# ssh ends a session after this many silent seconds times the count, so a
# lost instance cannot hold a command open forever.
_SSH_ALIVE_INTERVAL_SEC = 15
_SSH_ALIVE_COUNT = 4
_SSH_PROBE_INTERVAL_SEC = 5.0
_TIMEOUT_EXIT_CODE = 124
# The seconds between TERM and KILL when the remote timeout ends a command.
_REMOTE_KILL_AFTER_SEC = 5
# ssh exits with 255 when it cannot reach the host.
_SSH_FAILED = 255
# How long a cancelled start or stop waits for an open AWS call before the cancel passes on.
_CANCEL_GRACE_SEC = 0.5
# The remote shell reads the whole script from stdin and runs it, so the
# command then sees an empty stdin.
_READ_STDIN_SCRIPT = 'eval "$(cat)"'

# The instances of this process, so that the job sweep can clear their state
# and find the regions that they run in.
_LIVE: set[EC2Environment] = set()
_LATE_TERMINATES: set[asyncio.Future[None]] = set()


@dataclasses.dataclass
class _LaunchState:
    """Hold the state of one launch, so a late launch never touches a newer start."""

    instance_id: str | None = None
    cancelled: bool = False
    known_hosts: str | None = None


class EC2Environment(SandboxEnvironment):
    """Run a sandbox as one EC2 instance and reach it over SSH."""

    capabilities: ClassVar[EnvironmentCapabilities] = EC2_CAPABILITIES

    def __init__(
        self,
        spec: SandboxSpec,
        *,
        ami_id: str | None = None,
        region: str | None = None,
        aws_profile: str | None = None,
        instance_type: str = DEFAULT_INSTANCE_TYPE,
        subnet_id: str | None = None,
        security_group_ids: tuple[str, ...] = (),
        key_name: str | None = None,
        ssh_key_path: str | None = None,
        ssh_user: str = "ubuntu",
        ssh_port: int = 22,
        use_public_ip: bool = True,
        root_volume_type: str = "gp3",
        root_device_name: str | None = None,
        iam_instance_profile: str | None = None,
        strict_host_key_checking: str = "accept-new",
        ssh_known_hosts_path: str | None = None,
        timeouts: PhaseTimeouts | None = None,
    ) -> None:
        """Create a stopped EC2 sandbox.

        The instance type sets the CPU and memory size, so the requested
        vCPU and memory sizes are not sent. The disk size sets the root
        volume size.

        Args:
            spec: The sandbox to open. ``spec.image`` may name the AMI.
            ami_id: The AMI to launch; ``None`` uses ``spec.image``.
            region: The AWS region, or ``None`` for the boto3 default.
            aws_profile: The AWS profile, or ``None`` for the boto3 default.
            instance_type: The EC2 instance type.
            subnet_id: The subnet, or ``None`` for the default VPC.
            security_group_ids: The security groups of the instance.
            key_name: The EC2 key pair that the instance accepts.
            ssh_key_path: The private key for SSH, or ``None`` for the SSH default.
            ssh_user: The SSH user of the AMI.
            ssh_port: The SSH port.
            use_public_ip: Connect to the public address when there is one.
            root_volume_type: The EBS type of the root volume.
            root_device_name: The root device; ``None`` reads it from the AMI.
            iam_instance_profile: An instance profile name or ARN.
            strict_host_key_checking: The SSH ``StrictHostKeyChecking`` value.
            ssh_known_hosts_path: The SSH known hosts file; ``None`` uses a
                temporary file for the life of the instance.
            timeouts: The phase timeouts. The start phase bounds the SSH
                readiness wait; the run phase is the default ``exec`` limit.

        Raises:
            SandboxSpecError: No AMI is set.
        """
        super().__init__(spec)
        ami = ami_id or spec.image
        if not ami or not ami.startswith("ami-"):
            raise SandboxSpecError("The ec2 sandbox needs an AMI id: set ami_id or an 'ami-' spec image.")
        self._ami_id = ami
        self._region = region
        self._aws_profile = aws_profile
        self._instance_type = instance_type
        self._subnet_id = subnet_id
        self._security_group_ids = tuple(security_group_ids)
        self._key_name = key_name
        self._ssh_key_path = ssh_key_path
        self._ssh_user = ssh_user
        self._ssh_port = ssh_port
        self._use_public_ip = use_public_ip
        self._root_volume_type = root_volume_type
        self._root_device_name = root_device_name
        self._iam_instance_profile = iam_instance_profile
        self._strict_host_key_checking = strict_host_key_checking
        self._known_hosts_path = ssh_known_hosts_path
        self._temp_known_hosts: str | None = None
        self._connect_timeout = (
            timeouts.seconds(Phase.START) if timeouts is not None else DEFAULT_SSH_CONNECT_TIMEOUT_SEC
        )
        self._default_timeout = timeouts.seconds(Phase.RUN) if timeouts is not None else None
        self._client: Any = None
        self._instance_id: str | None = None
        self._host: str | None = None

    @classmethod
    def preflight(cls) -> None:
        """Check that boto3 and the ssh client are present.

        Raises:
            SandboxFatalError: boto3 or ``ssh`` is missing.
        """
        _require_sdk()
        if shutil.which("ssh") is None:
            raise SandboxFatalError("The ec2 sandbox needs the ssh client on PATH.")

    async def start(self) -> None:
        """Launch the instance and wait for SSH. Do nothing when the instance already runs.

        A cancel before ``run_instances`` launches no instance. A cancel
        terminates the launched instance at once when its id is known.
        Otherwise the launch terminates it as soon as ``run_instances``
        returns. The cancel passes on after at most twice
        ``_CANCEL_GRACE_SEC``, also through repeat cancels: one grace time
        for the launch to end and one for the terminate. A launch that is
        still open after that ends in the background, and it only ever
        terminates its own instance. A failed start removes its temporary
        known hosts file.

        Raises:
            SandboxError: An AWS call failed, or the instance has no address.
            StartTimeoutError: SSH did not answer in time.
            asyncio.CancelledError: The task was cancelled during the launch.
        """
        if self._instance_id is not None:
            return
        _require_sdk()
        if self._client is None:
            with _translated("client setup"):
                self._client = ec2_client(self._aws_profile, self._region)
        client = self._client
        state = _LaunchState()
        launch = asyncio.ensure_future(self._launch(client, state))
        try:
            instance_id, host = await asyncio.shield(launch)
        except asyncio.CancelledError:
            # The launch stops at its next step and terminates its own
            # instance. An instance id that is known already ends now, so it
            # does not wait for the running state or SSH.
            state.cancelled = True
            try:
                if state.instance_id is None:
                    await _wait_through_cancels(launch, _CANCEL_GRACE_SEC)
                if not launch.done():
                    launch.add_done_callback(lambda done: _terminate_late_launch(client, done))
                if state.instance_id is not None:
                    await _terminate_bounded(client, state.instance_id)
            finally:
                self._remove_known_hosts()
            raise
        except BaseException:
            self._remove_known_hosts()
            raise
        self._instance_id = instance_id
        self._host = host
        _LIVE.add(self)

    async def stop(self) -> None:
        """Terminate the instance. An instance that is already gone counts as terminated.

        A cancel during the terminate waits for it for at most
        ``_CANCEL_GRACE_SEC``, also through repeat cancels, then passes the
        cancel on with the terminate still running in the background.

        The sandbox keeps the instance until the terminate succeeds, so a
        second stop tries again after a failed or unfinished terminate.
        With no instance, stop only removes a temporary known hosts file.

        Raises:
            SandboxError: The terminate failed for a reason other than a gone instance.
            asyncio.CancelledError: The task was cancelled during the terminate.
        """
        instance_id = self._instance_id
        if instance_id is None:
            self._remove_known_hosts()
            return
        terminate = asyncio.ensure_future(_terminate(self._client, instance_id))
        try:
            await asyncio.shield(terminate)
        except asyncio.CancelledError:
            await _wait_through_cancels(terminate, _CANCEL_GRACE_SEC)
            if not terminate.done():
                _keep_late_terminate(terminate)
            elif not terminate.cancelled() and terminate.exception() is not None:
                logger.warning(
                    "Could not terminate an ec2 instance during a cancelled stop: %s",
                    _safe_text(terminate.exception()),
                )
            elif not terminate.cancelled():
                self._clear()
            raise
        self._clear()

    async def exec(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout_sec: float | None = None,
        on_output: OutputSink | None = None,
        sudo: bool = False,
    ) -> ExecResult:
        """Run one shell command on the instance over SSH.

        The output arrives when the command ends, so the sink gets one
        stdout chunk and one stderr chunk at the end. The command reads an
        empty stdin.

        Args:
            command: The shell command.
            cwd: The working directory, or the SSH user's home.
            env: Extra environment variables.
            timeout_sec: After this long, kill the local ssh and return
                ``timed_out=True``. A remote ``timeout`` also ends the
                command. ``None`` uses the run phase timeout.
            on_output: Receives ``(stream, text)`` chunks; ``None`` uses the
                sink of the current task.
            sudo: Run the command as root instead of the SSH user.

        Returns:
            The exit code, the captured output and whether the timeout fired.

        Raises:
            RuntimeError: The instance is not running.
            SandboxRejectedError: An env name is not a shell identifier.
        """
        self._require_running()
        limit = timeout_sec if timeout_sec is not None else self._default_timeout
        sink = on_output or current_output_sink()
        # The env can hold credentials, so the script goes over stdin and
        # never into the argv of the local ssh or the remote shell.
        script = wrap_script(command, env={**self.spec.env, **(env or {})}, cwd=cwd, sudo=False)
        reader = _READ_STDIN_SCRIPT
        if limit is not None:
            # Killing the local ssh does not end the remote command, so the
            # instance ends it too. The extra second lets the local limit
            # fire first, so a timeout always reports timed_out=True.
            seconds = math.ceil(limit) + 1
            reader = f"timeout -k {_REMOTE_KILL_AFTER_SEC} {seconds} bash -lc {shlex.quote(reader)}"
        reader = wrap_script(reader, env={}, cwd=None, sudo=sudo)
        try:
            code, out, err = await _run_process(
                self._ssh_argv(reader, stdin=True, limit=limit), script.encode("utf-8"), limit
            )
        except TimeoutError:
            return ExecResult(exit_code=_TIMEOUT_EXIT_CODE, timed_out=True)
        stdout, stderr = out.decode("utf-8", errors="replace"), err.decode("utf-8", errors="replace")
        if sink is not None:
            if stdout:
                sink("stdout", stdout)
            if stderr:
                sink("stderr", stderr)
        return ExecResult(exit_code=code, stdout=stdout, stderr=stderr)

    async def upload_files(self, files: Mapping[str, str]) -> None:
        """Write UTF-8 text files on the instance and create their parent directories.

        Each file write has the run phase time limit, as ``exec`` has.

        Args:
            files: Maps each absolute sandbox path to its text content.

        Raises:
            RuntimeError: The instance is not running, or a directory or a
                file could not be written.
            SandboxInfraError: A file write ran past the time limit.
        """
        self._require_running()
        parents = sorted({str(PurePosixPath(path).parent) for path in files} - {"", ".", "/"})
        if parents:
            made = await self.exec("mkdir -p " + " ".join(shlex.quote(parent) for parent in parents), sudo=True)
            if not made.ok:
                raise RuntimeError(f"The ec2 sandbox could not create the upload directories: {made.stderr}")
        limit = self._default_timeout
        for path, text in files.items():
            script = wrap_script(f"cat > {shlex.quote(path)}", env={}, cwd=None, sudo=True)
            try:
                code, _, err = await _run_process(
                    self._ssh_argv(script, stdin=True, limit=limit), text.encode("utf-8"), limit
                )
            except TimeoutError:
                raise SandboxInfraError(f"The ec2 sandbox timed out writing {path}.") from None
            if code != 0:
                raise RuntimeError(f"The ec2 sandbox could not write {path}: {err.decode('utf-8', errors='replace')}")

    async def download_file(self, path: str) -> str | None:
        """Read one UTF-8 text file from the instance.

        Args:
            path: The absolute sandbox path.

        Returns:
            The exact file content, or ``None`` when the file does not exist.

        Raises:
            RuntimeError: The instance is not running.
            SandboxInfraError: SSH could not reach the instance, or the read
                failed or ran past the run phase time limit.
        """
        self._require_running()
        # A lost connection must not read as a missing file, so the probe
        # tells the ssh failure code apart from a failed test.
        probe = await self.exec(f"test -f {shlex.quote(path)}", sudo=True)
        if probe.timed_out or probe.exit_code == _SSH_FAILED:
            raise SandboxInfraError(f"The ec2 sandbox could not reach the instance: {probe.stderr}")
        if not probe.ok:
            return None
        script = wrap_script(f"cat {shlex.quote(path)}", env={}, cwd=None, sudo=True)
        limit = self._default_timeout
        try:
            code, out, err = await _run_process(self._ssh_argv(script, stdin=False, limit=limit), None, limit)
        except TimeoutError:
            raise SandboxInfraError(f"The ec2 sandbox timed out reading {path}.") from None
        if code != 0:
            raise SandboxInfraError(f"The ec2 sandbox could not read {path}: {err.decode('utf-8', errors='replace')}")
        return out.decode("utf-8")

    @classmethod
    async def stop_job_sandboxes(cls, job_id: str) -> int:
        """Terminate every instance that carries the job tag.

        The sweep reads the tag from AWS, so it also finds instances that
        other processes started. It looks in the default region and in the
        regions of the instances of this process.

        Args:
            job_id: The value of the ``JOB_TAG`` tag.

        Returns:
            The number of instances that the sweep terminated.
        """
        _require_sdk()
        targets: dict[tuple[str | None, str | None], Any] = {}
        for env in _LIVE:
            targets.setdefault((env._aws_profile, env._region), env._client)
        if (None, None) not in targets:
            targets[(None, None)] = None
        terminated: set[str] = set()
        for (profile, region), client in targets.items():
            try:
                with _translated("client setup"):
                    client = client or ec2_client(profile, region)
                ids = await _aws("describe_instances", _tagged_instances, client, job_id)
                if ids:
                    await _aws("terminate_instances", client.terminate_instances, InstanceIds=ids)
            except Exception as error:
                logger.warning(
                    "Could not terminate the ec2 instances of job %s in region %s: %s",
                    job_id,
                    region,
                    _safe_text(error),
                )
                continue
            terminated.update(ids)
        for env in [env for env in _LIVE if env._instance_id in terminated]:
            env._clear()
        return len(terminated)

    async def _launch(self, client: Any, state: _LaunchState) -> tuple[str, str]:
        """Launch one instance, wait until it runs and answers SSH.

        The launch keeps its instance and host in local state, so a launch
        that ends after a cancelled start never changes a newer start.

        Args:
            client: The EC2 client.
            state: The state of this launch, which the start marks as cancelled.

        Returns:
            The instance id and the SSH host.

        Raises:
            SandboxError: An AWS call failed, or the instance has no address.
            StartTimeoutError: SSH did not answer in time.
            asyncio.CancelledError: The start was cancelled during the launch.
        """
        kwargs = await _aws("describe_images", self._run_kwargs)
        # A cancel during the image lookup must not start a paid instance.
        _raise_if_cancelled(state)
        response = await _aws("run_instances", client.run_instances, **kwargs)
        instance_id = response["Instances"][0]["InstanceId"]
        state.instance_id = instance_id
        loop = asyncio.get_running_loop()
        # The start phase time limit bounds the running wait and the SSH wait together.
        deadline = loop.time() + self._connect_timeout
        try:
            _raise_if_cancelled(state)
            waiter = client.get_waiter("instance_running")
            attempts = max(1, math.ceil(self._connect_timeout / _RUNNING_WAITER_DELAY_SEC))
            config = {"Delay": _RUNNING_WAITER_DELAY_SEC, "MaxAttempts": attempts}
            await _aws("instance_running wait", waiter.wait, InstanceIds=[instance_id], WaiterConfig=config)
            _raise_if_cancelled(state)
            described = await _aws("describe_instances", client.describe_instances, InstanceIds=[instance_id])
            host = instance_host(described["Reservations"][0]["Instances"][0], use_public_ip=self._use_public_ip)
            if host is None:
                raise SandboxInfraError(f"The ec2 instance {instance_id} has no IP address.")
            await self._wait_for_ssh(host, state, deadline)
            _raise_if_cancelled(state)
        except BaseException:
            if state.cancelled and state.known_hosts not in (None, self._temp_known_hosts):
                # The start removed this file already, and a late probe with
                # accept-new could write it again.
                _remove_file(state.known_hosts)
            # A failed launch must not leave a paid instance behind.
            await _terminate_bounded(client, instance_id)
            raise
        return instance_id, host

    def _run_kwargs(self) -> dict[str, Any]:
        """Build the ``run_instances`` arguments from the spec and the settings.

        Returns:
            The keyword arguments.
        """
        spec = self.spec
        name = instance_name(spec.name or f"skynet-{uuid.uuid4().hex[:8]}")
        tags = aws_tags({"Name": name, **spec.tags})
        kwargs: dict[str, Any] = {
            "ImageId": self._ami_id,
            "InstanceType": self._instance_type,
            "MinCount": 1,
            "MaxCount": 1,
            # EC2 keeps a client token for about a day, and a stable box key
            # would return the terminated instance of an earlier start. The
            # call never retries, so a new token for each start loses nothing.
            "ClientToken": client_token(uuid.uuid4().hex),
            "TagSpecifications": [
                {"ResourceType": "instance", "Tags": tags},
                {"ResourceType": "volume", "Tags": tags},
            ],
            # The instance ends itself at the spec lifetime, so a lost worker
            # cannot leave it running.
            "UserData": f"#!/bin/sh\nshutdown -P +{max(1, math.ceil(spec.lifetime_seconds / 60))}\n",
            "InstanceInitiatedShutdownBehavior": "terminate",
        }
        if self._key_name:
            kwargs["KeyName"] = self._key_name
        if self._subnet_id:
            interface: dict[str, Any] = {
                "DeviceIndex": 0,
                "AssociatePublicIpAddress": self._use_public_ip,
                "SubnetId": self._subnet_id,
            }
            if self._security_group_ids:
                interface["Groups"] = list(self._security_group_ids)
            kwargs["NetworkInterfaces"] = [interface]
        elif self._security_group_ids:
            kwargs["SecurityGroupIds"] = list(self._security_group_ids)
        request = spec.resources
        if request.mode is not ResourceMode.IGNORE and request.disk_mb:
            kwargs["BlockDeviceMappings"] = [
                {
                    "DeviceName": self._root_device(),
                    "Ebs": {
                        "VolumeSize": math.ceil(request.disk_mb / 1024),
                        "VolumeType": self._root_volume_type,
                        "DeleteOnTermination": True,
                    },
                }
            ]
        if self._iam_instance_profile:
            key = "Arn" if self._iam_instance_profile.startswith("arn:") else "Name"
            kwargs["IamInstanceProfile"] = {key: self._iam_instance_profile}
        return kwargs

    def _root_device(self) -> str:
        """Return the root device name from the settings, the AMI or the default.

        Returns:
            The device name.
        """
        if self._root_device_name:
            return self._root_device_name
        images = self._client.describe_images(ImageIds=[self._ami_id]).get("Images") or []
        return (images[0].get("RootDeviceName") if images else None) or DEFAULT_ROOT_DEVICE

    async def _wait_for_ssh(self, host: str, state: _LaunchState, deadline: float) -> None:
        """Probe SSH with ``true`` until it answers or the start deadline passes.

        This is a readiness wait for a new host, not a retry of an AWS call.

        Args:
            host: The SSH host of the new instance.
            state: The state of the launch; a cancelled launch stops probing.
            deadline: The event loop time when the start phase ends.

        Raises:
            StartTimeoutError: SSH did not answer in time.
            asyncio.CancelledError: The start was cancelled during the wait.
        """
        loop = asyncio.get_running_loop()
        argv = self._ssh_argv(wrap_script("true", env={}, cwd=None, sudo=False), stdin=False, limit=None, host=host)
        state.known_hosts = self._temp_known_hosts
        last = b""
        while True:
            _raise_if_cancelled(state)
            remaining = deadline - loop.time()
            try:
                code, _, last = await _run_process(argv, None, max(0.0, min(_SSH_PROBE_TIMEOUT_SEC, remaining)))
            except TimeoutError:
                code = _SSH_FAILED
            if code == 0:
                return
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(_SSH_PROBE_INTERVAL_SEC, remaining))
        logger.warning("SSH to an ec2 instance did not answer: %s", last.decode("utf-8", errors="replace")[:500])
        raise StartTimeoutError(self._connect_timeout)

    def _ssh_argv(self, script: str, *, stdin: bool, limit: float | None, host: str | None = None) -> list[str]:
        """Build the ssh command line that runs a script on the instance.

        Args:
            script: The remote shell script.
            stdin: Send stdin to the remote command; otherwise pass ``-n``.
            limit: The command time limit, which caps the connect timeout.
            host: The SSH host; ``None`` uses the host of the running instance.

        Returns:
            The argv list.
        """
        connect = 30 if limit is None else min(30, max(1, math.ceil(limit)))
        argv = [
            "ssh",
            "-p",
            str(self._ssh_port),
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={connect}",
            "-o",
            f"StrictHostKeyChecking={self._strict_host_key_checking}",
            "-o",
            f"UserKnownHostsFile={self._known_hosts()}",
            "-o",
            f"ServerAliveInterval={_SSH_ALIVE_INTERVAL_SEC}",
            "-o",
            f"ServerAliveCountMax={_SSH_ALIVE_COUNT}",
        ]
        if not stdin:
            argv.append("-n")
        if self._ssh_key_path:
            argv += ["-i", self._ssh_key_path]
        argv += [f"{self._ssh_user}@{host or self._host}", "bash -lc " + shlex.quote(script)]
        return argv

    def _known_hosts(self) -> str:
        """Return the known hosts file, and make a temporary one when none is set.

        Returns:
            The file path.
        """
        if self._known_hosts_path:
            return self._known_hosts_path
        if self._temp_known_hosts is None:
            handle, self._temp_known_hosts = tempfile.mkstemp(prefix="skynet-ec2-", suffix=".known_hosts")
            os.close(handle)
        return self._temp_known_hosts

    def _clear(self) -> None:
        """Forget the instance and remove the temporary known hosts file."""
        self._instance_id = None
        self._host = None
        _LIVE.discard(self)
        self._remove_known_hosts()

    def _remove_known_hosts(self) -> None:
        """Remove the temporary known hosts file when there is one."""
        if self._temp_known_hosts is not None:
            with contextlib.suppress(OSError):
                Path(self._temp_known_hosts).unlink(missing_ok=True)
            self._temp_known_hosts = None

    def _require_running(self) -> None:
        """Raise when the instance is stopped.

        Raises:
            RuntimeError: The instance is not running.
        """
        if self._instance_id is None:
            raise RuntimeError("The ec2 sandbox is not running.")


def ec2_client(profile: str | None, region: str | None) -> Any:
    """Build an EC2 client that never retries a call.

    Args:
        profile: The AWS profile, or ``None`` for the default.
        region: The AWS region, or ``None`` for the default.

    Returns:
        The boto3 EC2 client.
    """
    session = boto3.Session(profile_name=profile, region_name=region)
    return session.client("ec2", config=BotoConfig(retries=dict(NO_RETRY)))


def wrap_script(command: str, *, env: Mapping[str, str], cwd: str | None, sudo: bool) -> str:
    """Add the env exports, the working directory and the root switch to a command.

    Args:
        command: The shell command.
        env: The environment variables to export.
        cwd: The working directory, or ``None``.
        sudo: Run the script as root.

    Returns:
        The remote script.

    Raises:
        SandboxRejectedError: An env name is not a shell identifier.
    """
    for key in env:
        # The names go into the script unquoted, so a bad name could inject shell code.
        if not _ENV_KEY.fullmatch(key):
            raise SandboxRejectedError(f"{key!r} is not a valid environment variable name.")
    script = command
    if cwd:
        script = f"cd {shlex.quote(cwd)} && {script}"
    if env:
        # Exports reach every part of a compound command, unlike a prefix.
        exports = " ".join(f"{key}={shlex.quote(value)}" for key, value in env.items())
        script = f"export {exports}; {script}"
    if sudo:
        script = f"sudo -n bash -lc {shlex.quote(script)}"
    return script


def instance_host(instance: Mapping[str, Any], *, use_public_ip: bool) -> str | None:
    """Pick the SSH address of an instance.

    Args:
        instance: One instance from ``describe_instances``.
        use_public_ip: Prefer the public address.

    Returns:
        The preferred address, else the other one, else ``None``.
    """
    public, private = instance.get("PublicIpAddress"), instance.get("PrivateIpAddress")
    return (public or private) if use_public_ip else (private or public)


def aws_tags(tags: Mapping[str, str]) -> list[dict[str, str]]:
    """Convert a tag mapping to the AWS tag list.

    Args:
        tags: The tags.

    Returns:
        The ``[{"Key", "Value"}]`` list.
    """
    return [{"Key": key, "Value": value} for key, value in tags.items()]


def instance_name(value: str) -> str:
    """Normalize a name to the characters and length that an EC2 Name tag takes.

    Args:
        value: The wanted name.

    Returns:
        The name, at most 255 characters long; a long name ends in a hash.
    """
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", value)
    if len(safe) <= 255:
        return safe
    return f"{safe[:242].rstrip('-._')}-{hashlib.sha256(value.encode()).hexdigest()[:12]}"


def client_token(value: str) -> str:
    """Normalize a value to an EC2 client token.

    Args:
        value: The idempotency key.

    Returns:
        The token, at most 64 characters long; a long token ends in a hash.
    """
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", value)
    if len(safe) <= 64:
        return safe
    return f"{safe[:51].rstrip('-_')}-{hashlib.sha256(value.encode()).hexdigest()[:12]}"


async def _run_process(argv: list[str], stdin: bytes | None, limit_sec: float | None) -> tuple[int, bytes, bytes]:
    """Run a local command and kill it when it runs past the time limit.

    Args:
        argv: The command.
        stdin: The bytes for stdin, or ``None`` for no stdin.
        limit_sec: The time limit in seconds, or ``None``.

    Returns:
        The exit code, stdout and stderr.

    Raises:
        TimeoutError: The command ran past the time limit.
    """
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(limit_sec):
            out, err = await process.communicate(stdin)
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise
    return process.returncode or 0, out, err


def _tagged_instances(client: Any, job_id: str) -> list[str]:
    """List the live instances that carry the job tag.

    Args:
        client: The EC2 client.
        job_id: The value of the ``JOB_TAG`` tag.

    Returns:
        The instance ids.
    """
    filters = [
        {"Name": f"tag:{JOB_TAG}", "Values": [job_id]},
        {"Name": "instance-state-name", "Values": _SWEEP_STATES},
    ]
    ids: list[str] = []
    token: str | None = None
    while True:
        page = client.describe_instances(Filters=filters, **({"NextToken": token} if token else {}))
        for reservation in page.get("Reservations", []):
            ids += [instance["InstanceId"] for instance in reservation.get("Instances", [])]
        token = page.get("NextToken")
        if not token:
            return ids


def _require_sdk() -> None:
    """Raise when boto3 is not installed.

    Raises:
        SandboxFatalError: The ``boto3`` package is missing.
    """
    if boto3 is None:
        raise SandboxFatalError("The ec2 sandbox needs the boto3 package: install the `ec2` extra.")


async def _aws(action: str, call: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run one blocking AWS call in a thread and translate its error.

    Args:
        action: The short name of the call, for the error message.
        call: The blocking function.
        *args: The positional arguments of the call.
        **kwargs: The keyword arguments of the call.

    Returns:
        The result of the call.

    Raises:
        SandboxError: The AWS call failed, in the matching error group.
    """
    with _translated(action):
        return await asyncio.to_thread(call, *args, **kwargs)


@contextlib.contextmanager
def _translated(action: str) -> Iterator[None]:
    """Turn a boto3 or botocore error into a sandbox error group, without a retry.

    The new error does not chain to the AWS error, because the AWS message
    and response can name the account, the caller ARN or the request.

    Args:
        action: The short name of the call, for the error message.

    Yields:
        Nothing.

    Raises:
        SandboxError: The mapped error.
    """
    try:
        yield
    except Exception as error:
        mapped = _aws_error(error, action)
        if mapped is None:
            raise
        raise mapped from None


def _aws_error(error: Exception, action: str) -> SandboxError | None:
    """Return the sandbox error for one AWS error, with only the operation and the code.

    Args:
        error: The error that the AWS call raised.
        action: The short name of the call.

    Returns:
        The sandbox error in the matching group, or ``None`` for an error
        that does not come from boto3 or botocore.
    """
    if ClientError is not None and isinstance(error, ClientError):
        response = error.response if isinstance(error.response, Mapping) else {}
        raw = str((response.get("Error") or {}).get("Code") or "Unknown")
        code = _CODE_CHARS.sub("", raw)[:64] or "Unknown"
        status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        message = f"EC2 {action} failed with {code}."
        if code in _AUTH_CODES or status in (401, 403):
            mapped: SandboxError = SandboxFatalError(message)
        elif code in _TRANSIENT_CODES or (isinstance(status, int) and (status == 429 or status >= 500)):
            mapped = SandboxTransientError(message)
        elif isinstance(status, int) and 400 <= status < 500:
            mapped = SandboxRejectedError(message)
        else:
            mapped = SandboxInfraError(message)
        mapped.aws_code = code
        return mapped
    if WaiterError is not None and isinstance(error, WaiterError):
        return SandboxInfraError(f"EC2 {action} ended before the instance ran.")
    if BotoCoreError is not None and isinstance(error, BotoCoreError):
        fatal = (NoCredentialsError, PartialCredentialsError, NoRegionError, ProfileNotFound)
        message = f"EC2 {action} failed: {type(error).__name__}."
        return SandboxFatalError(message) if isinstance(error, fatal) else SandboxTransientError(message)
    return None


def _raise_if_cancelled(state: _LaunchState) -> None:
    """Stop a launch whose start was cancelled.

    Args:
        state: The state of the launch.

    Raises:
        asyncio.CancelledError: The start was cancelled.
    """
    if state.cancelled:
        raise asyncio.CancelledError


def _safe_text(error: BaseException | None) -> str:
    """Describe an error for a log line without AWS response text.

    Args:
        error: The error, or ``None``.

    Returns:
        The message of a sandbox error, else the error type name.
    """
    if isinstance(error, SandboxError):
        return str(error)
    return type(error).__name__


def _remove_file(path: str) -> None:
    """Remove a file, and ignore a file that is gone or cannot be removed.

    Args:
        path: The file path.
    """
    with contextlib.suppress(OSError):
        Path(path).unlink(missing_ok=True)


async def _terminate(client: Any, instance_id: str) -> None:
    """Terminate one instance, and ignore an instance that is already gone.

    Args:
        client: The EC2 client.
        instance_id: The instance id.

    Raises:
        SandboxError: The terminate failed for another reason.
    """
    try:
        await _aws("terminate_instances", client.terminate_instances, InstanceIds=[instance_id])
    except SandboxError as error:
        if getattr(error, "aws_code", None) not in _GONE_CODES:
            raise


async def _terminate_bounded(client: Any, instance_id: str) -> None:
    """Terminate an instance of a failed or cancelled launch, and wait at most ``_CANCEL_GRACE_SEC``.

    Args:
        client: The EC2 client.
        instance_id: The instance id.
    """
    terminate = asyncio.ensure_future(_terminate(client, instance_id))
    await _wait_through_cancels(terminate, _CANCEL_GRACE_SEC)
    if not terminate.done():
        _keep_late_terminate(terminate)
    elif not terminate.cancelled() and terminate.exception() is not None:
        # The first error must still reach the caller, so this one goes to the log.
        logger.error(
            "Could not terminate an ec2 instance after a failed or cancelled start: %s",
            _safe_text(terminate.exception()),
        )


async def _wait_through_cancels(task: asyncio.Future[Any], grace_sec: float) -> None:
    """Wait until the task ends or the grace time passes, and absorb each cancel meanwhile.

    The task itself is never cancelled, because AWS may already have done
    the work on its side.

    Args:
        task: The task to wait for.
        grace_sec: The longest wait in seconds.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + grace_sec
    while not task.done():
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            await asyncio.wait({task}, timeout=remaining)
        except asyncio.CancelledError:
            # The caller re-raises its own cancel after this wait.
            continue


def _terminate_late_launch(client: Any, launch: asyncio.Future[tuple[str, str]]) -> None:
    """Terminate the instance of a launch that ended after its start was cancelled.

    A done callback must never raise, so every error goes to the log.

    Args:
        client: The EC2 client.
        launch: The finished launch task.
    """
    try:
        if launch.cancelled() or launch.exception() is not None:
            return
        terminate = asyncio.ensure_future(_terminate(client, launch.result()[0]))
    except Exception as error:
        logger.warning(
            "Could not terminate an ec2 instance that arrived after a cancelled start: %s", _safe_text(error)
        )
        return
    _keep_late_terminate(terminate)


def _keep_late_terminate(terminate: asyncio.Future[None]) -> None:
    """Keep a background terminate alive until it ends, and log its error then.

    Args:
        terminate: The running terminate task.
    """
    # The loop holds tasks only by weak reference, so keep this one until it ends.
    _LATE_TERMINATES.add(terminate)
    terminate.add_done_callback(_finish_late_terminate)


def _finish_late_terminate(terminate: asyncio.Future[None]) -> None:
    """Forget a finished late terminate and log its error.

    Args:
        terminate: The finished terminate task.
    """
    _LATE_TERMINATES.discard(terminate)
    if not terminate.cancelled() and terminate.exception() is not None:
        logger.error(
            "Could not terminate an ec2 instance after a cancelled start or stop: %s",
            _safe_text(terminate.exception()),
        )
