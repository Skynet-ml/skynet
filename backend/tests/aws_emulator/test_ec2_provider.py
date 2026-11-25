"""Run the EC2 sandbox provider against EC2 calls on the local AWS Emulator.

The emulator starts no real machine, so the tests replace only the SSH
wait. Every other call goes to the emulator's EC2 API through boto3.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest

from core.sandbox.environment import JOB_TAG
from core.sandbox.errors import SandboxRejectedError
from core.sandbox.providers import ec2
from core.sandbox.providers.ec2 import EC2Environment
from core.sandbox.resources import ResourceRequest, SandboxSpec
from tests.aws_emulator.conftest import Emulator

try:
    import boto3
except ImportError:
    boto3 = None

GONE = {"shutting-down", "terminated"}
# No image has this id. The emulator's RunInstances does not check that an
# image exists, so only DescribeImages refuses this id.
UNKNOWN_AMI = "ami-0123456789abcdef0"
# RunInstances refuses this id: it has too few hex digits.
MALFORMED_AMI = "ami-deadbeef"
# Every instance of a test carries this tag, also when its job tag differs.
# The fixture finalizer terminates the instances by it.
TEST_TAG = "skynet-emulator-test"


@pytest.fixture
def client(aws_emulator: Emulator) -> Any:
    """Build a boto3 EC2 client for the emulator.

    Args:
        aws_emulator: Makes sure that the client reaches the emulator.

    Returns:
        The client.
    """
    return boto3.client("ec2")


@pytest.fixture
def ami(client: Any) -> str:
    """Pick a real Amazon AMI from the emulator's image list.

    Args:
        client: The EC2 client.

    Returns:
        The AMI id.
    """
    images = client.describe_images(Owners=["amazon"])["Images"]
    if not images:
        pytest.fail("the emulator lists no Amazon AMI")
    return images[0]["ImageId"]


@pytest.fixture
def job_id(client: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Isolate the provider state, and terminate every instance of the test job.

    Args:
        client: The EC2 client.
        monkeypatch: Replaces the live set and the SSH wait for this test.

    Yields:
        A unique job id. ``_sandbox`` puts it in the test tag of each instance.
    """

    async def ssh_ready(*_args: Any) -> None:
        """Skip the SSH wait: the emulator starts no SSH server."""

    # A fresh live set, so the sweep sees only the sandboxes of this test.
    monkeypatch.setattr(ec2, "_LIVE", set())
    monkeypatch.setattr(EC2Environment, "_wait_for_ssh", ssh_ready)
    job = f"emulator-test-{uuid.uuid4().hex[:12]}"
    try:
        yield job
    finally:
        ids = _instance_ids(client, TEST_TAG, job)
        if ids:
            client.terminate_instances(InstanceIds=ids)


def _instance_ids(client: Any, tag: str, value: str) -> list[str]:
    """List the instances that are not terminated and carry one tag value.

    Args:
        client: The EC2 client.
        tag: The tag key.
        value: The tag value.

    Returns:
        The instance ids.
    """
    pages = client.get_paginator("describe_instances").paginate(
        Filters=[
            {"Name": f"tag:{tag}", "Values": [value]},
            {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]},
        ]
    )
    return [
        instance["InstanceId"]
        for page in pages
        for reservation in page["Reservations"]
        for instance in reservation["Instances"]
    ]


def _instance(client: Any, instance_id: str) -> dict[str, Any]:
    """Describe one instance.

    Args:
        client: The EC2 client.
        instance_id: The instance id.

    Returns:
        The instance description.
    """
    return client.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]


def _sandbox(ami: str, test_job: str, *, job: str | None = None, name: str | None = None) -> EC2Environment:
    """Build an EC2 sandbox for the emulator.

    Args:
        ami: The AMI id.
        test_job: The job id of the test, for the test tag.
        job: The value of the job tag, or ``None`` for ``test_job``.
        name: The sandbox name, or ``None`` for a generated one.

    Returns:
        The stopped sandbox.
    """
    tags = {JOB_TAG: job or test_job, TEST_TAG: test_job}
    spec = SandboxSpec(lifetime_seconds=300, resources=ResourceRequest(cpus=1), name=name, tags=tags)
    return EC2Environment(spec, ami_id=ami)


async def test_start_tags_the_instance_and_stop_terminates_it(client: Any, ami: str, job_id: str) -> None:
    """Check the running state, the tags, a repeat start and the terminate."""
    sandbox = _sandbox(ami, job_id, name="emulator-check")
    await sandbox.start()
    instance_id = sandbox._instance_id
    assert instance_id is not None
    assert sandbox in ec2._LIVE

    instance = _instance(client, instance_id)
    assert instance["State"]["Name"] == "running"
    assert instance["ImageId"] == ami
    tags = {tag["Key"]: tag["Value"] for tag in instance["Tags"]}
    assert tags["Name"] == "emulator-check"
    assert tags[JOB_TAG] == job_id
    assert len(tags[ec2._LAUNCH_TAG]) == 32

    await sandbox.start()
    assert sandbox._instance_id == instance_id
    assert _instance_ids(client, JOB_TAG, job_id) == [instance_id]

    await sandbox.stop()
    assert sandbox._instance_id is None
    assert _instance(client, instance_id)["State"]["Name"] in GONE


async def test_repeat_client_token_launches_one_instance(client: Any, ami: str, job_id: str) -> None:
    """Check that a repeat ``run_instances`` with one client token gives one instance.

    Each start makes a new token, so the test calls the launch helper twice
    with the same arguments.
    """
    sandbox = _sandbox(ami, job_id)
    launch_id = uuid.uuid4().hex
    kwargs = sandbox._run_kwargs(launch_id)

    first = ec2._run_instances(client, ec2._LaunchState(launch_id=launch_id), kwargs)
    second = ec2._run_instances(client, ec2._LaunchState(launch_id=launch_id), kwargs)

    instance_id = first["Instances"][0]["InstanceId"]
    assert second["Instances"][0]["InstanceId"] == instance_id
    assert _instance_ids(client, ec2._LAUNCH_TAG, launch_id) == [instance_id]


async def test_stop_job_sandboxes_sweeps_by_job_tag(client: Any, ami: str, job_id: str) -> None:
    """Check that the job sweep terminates the job's instances and only those."""
    sandboxes = [_sandbox(ami, job_id), _sandbox(ami, job_id)]
    other = _sandbox(ami, job_id, job=f"{job_id}-other")
    for sandbox in (*sandboxes, other):
        await sandbox.start()
    ids = {sandbox._instance_id for sandbox in sandboxes}
    other_id = other._instance_id
    try:
        assert await EC2Environment.stop_job_sandboxes(job_id) == 2
        assert all(sandbox._instance_id is None for sandbox in sandboxes)
        assert list(ec2._LIVE) == [other]
        assert {_instance(client, instance_id)["State"]["Name"] for instance_id in ids} <= GONE
        assert _instance(client, other_id)["State"]["Name"] == "running"
    finally:
        await other.stop()


@pytest.mark.parametrize(
    ("ami_id", "disk_mb", "failure"),
    [
        # A disk size makes the provider read the root device from the image.
        (UNKNOWN_AMI, 8192, "EC2 describe_images failed with InvalidAMIID.NotFound."),
        (MALFORMED_AMI, None, "EC2 run_instances failed with InvalidAMIID.Malformed."),
    ],
    ids=["unknown", "malformed"],
)
async def test_bad_ami_is_rejected(client: Any, job_id: str, ami_id: str, disk_mb: int | None, failure: str) -> None:
    """Check that a bad AMI maps to a rejected error and launches nothing."""
    spec = SandboxSpec(
        lifetime_seconds=300,
        resources=ResourceRequest(cpus=1, disk_mb=disk_mb),
        tags={JOB_TAG: job_id, TEST_TAG: job_id},
    )
    sandbox = EC2Environment(spec, ami_id=ami_id)

    with pytest.raises(SandboxRejectedError) as rejected:
        await sandbox.start()
    assert str(rejected.value) == failure

    assert sandbox._instance_id is None
    assert sandbox._lost_launches == {}
    assert _instance_ids(client, JOB_TAG, job_id) == []
