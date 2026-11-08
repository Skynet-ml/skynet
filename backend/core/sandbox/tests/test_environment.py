"""Tests for the async sandbox interface, plus the reusable provider contract.

A new provider reuses the contract like this::

    from core.sandbox.tests.test_environment import SandboxEnvironmentContract

    class TestVercelSandboxContract(SandboxEnvironmentContract):
        provider_cls = VercelSandbox

Override ``make_spec`` when the provider needs other spec fields.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import ClassVar

import pytest

from core.sandbox import (
    JOB_TAG,
    VERCEL_CAPABILITIES,
    EnvironmentCapabilities,
    ExecResult,
    HealthcheckConfig,
    HealthcheckError,
    NetworkMode,
    NetworkPolicy,
    ResourceMode,
    ResourceRequest,
    SandboxEnvironment,
    SandboxSpec,
    SandboxSpecError,
    resolve_resources,
    validate_spec,
)
from core.sandbox import environment as environment_module

from .fake_provider import FAKE_CAPABILITIES, InMemorySandbox


class SandboxEnvironmentContract:
    """Check the behavior that every sandbox provider must have.

    Subclass it with a ``Test`` name and set ``provider_cls``.
    """

    provider_cls: ClassVar[type[SandboxEnvironment]]

    def make_spec(self, **overrides) -> SandboxSpec:
        """Return a small spec that the provider can open.

        Args:
            **overrides: Spec fields to replace.

        Returns:
            The spec.
        """
        return SandboxSpec(**{"lifetime_seconds": 300, "resources": ResourceRequest(cpus=1), **overrides})

    @pytest.fixture
    async def sandbox(self) -> AsyncIterator[SandboxEnvironment]:
        """Yield a started sandbox and stop it after the test."""
        async with self.provider_cls(self.make_spec()) as env:
            yield env

    async def test_exec_returns_stdout_and_exit_code(self, sandbox: SandboxEnvironment) -> None:
        """Return stdout and a zero exit code for a passing command."""
        result = await sandbox.exec("echo hello")
        assert result.ok
        assert result.exit_code == 0
        assert result.stdout.strip() == "hello"

    async def test_exec_reports_a_failing_exit_code(self, sandbox: SandboxEnvironment) -> None:
        """Return the exit code of a failing command."""
        result = await sandbox.exec("exit 3")
        assert not result.ok
        assert result.exit_code == 3

    async def test_exec_passes_env(self, sandbox: SandboxEnvironment) -> None:
        """Make per-command environment variables visible to the command."""
        result = await sandbox.exec("printenv SKYNET_CONTRACT", env={"SKYNET_CONTRACT": "yes"})
        assert result.stdout.strip() == "yes"

    async def test_exec_streams_output(self, sandbox: SandboxEnvironment) -> None:
        """Send stdout chunks to the output sink."""
        chunks: list[tuple[str, str]] = []
        await sandbox.exec("echo streamed", on_output=lambda stream, text: chunks.append((stream, text)))
        assert "streamed" in "".join(text for stream, text in chunks if stream == "stdout")

    async def test_upload_then_download_round_trips(self, sandbox: SandboxEnvironment) -> None:
        """Read back the exact text that an upload wrote."""
        await sandbox.upload_files({"/tmp/contract/a.txt": "alpha\nבטא\n"})
        assert await sandbox.download_file("/tmp/contract/a.txt") == "alpha\nבטא\n"
        assert (await sandbox.exec("cat /tmp/contract/a.txt")).stdout == "alpha\nבטא\n"

    async def test_download_missing_file_returns_none(self, sandbox: SandboxEnvironment) -> None:
        """Return None for a path that does not exist."""
        assert await sandbox.download_file("/tmp/contract/missing.txt") is None

    async def test_healthcheck_passes_for_a_ready_sandbox(self, sandbox: SandboxEnvironment) -> None:
        """Return when the healthcheck command succeeds."""
        await sandbox.healthcheck(HealthcheckConfig(command="true", interval_sec=0, start_interval_sec=0))

    async def test_healthcheck_raises_after_retries(self, sandbox: SandboxEnvironment) -> None:
        """Raise HealthcheckError when the command keeps failing."""
        with pytest.raises(HealthcheckError):
            await sandbox.healthcheck(HealthcheckConfig(command="false", interval_sec=0, retries=2))

    async def test_disable_network_when_supported(self, sandbox: SandboxEnvironment) -> None:
        """Switch to deny-all egress when the provider can change policy."""
        if not sandbox.capabilities.dynamic_network_policy:
            pytest.skip("The provider cannot change its network policy after start.")
        await sandbox.disable_network()
        assert sandbox.network_policy == NetworkPolicy(disabled=True)

    async def test_stop_is_idempotent(self) -> None:
        """Allow stop before start and more than once."""
        env = self.provider_cls(self.make_spec())
        await env.stop()
        async with env:
            pass
        await env.stop()

    async def test_stop_job_sandboxes_stops_only_tagged_sandboxes(self) -> None:
        """Stop the sandboxes of one job and leave other sandboxes running."""
        job_id = f"contract-{uuid.uuid4().hex}"
        tagged = [self.provider_cls(self.make_spec(tags={JOB_TAG: job_id})) for _ in range(2)]
        other = self.provider_cls(self.make_spec(tags={JOB_TAG: f"{job_id}-other"}))
        try:
            for env in (*tagged, other):
                await env.start()
            assert await self.provider_cls.stop_job_sandboxes(job_id) == 2
            assert (await other.exec("true")).ok
        finally:
            for env in (*tagged, other):
                await env.stop()

    def test_rejects_a_spec_above_the_vcpu_limit(self) -> None:
        """Reject a spec that asks for more vCPUs than the provider allows."""
        max_vcpus = self.provider_cls.capabilities.max_vcpus
        if max_vcpus is None:
            pytest.skip("The provider sets no vCPU limit.")
        with pytest.raises(SandboxSpecError):
            self.provider_cls(self.make_spec(resources=ResourceRequest(cpus=max_vcpus + 1)))


class TestInMemorySandboxContract(SandboxEnvironmentContract):
    """Run the provider contract against the in-memory fake."""

    provider_cls = InMemorySandbox


def _spec(**resources) -> SandboxSpec:
    """Return a spec with the given resource fields.

    Args:
        **resources: ``ResourceRequest`` fields.

    Returns:
        The spec.
    """
    return SandboxSpec(lifetime_seconds=60, resources=ResourceRequest(**resources))


@pytest.mark.parametrize("cpus", [1, 2, 4, 16, 32])
def test_vercel_accepts_one_or_even_vcpus_up_to_32(cpus: int) -> None:
    """Accept every vCPU count that Vercel allows."""
    validate_spec(_spec(cpus=cpus), VERCEL_CAPABILITIES)


@pytest.mark.parametrize("cpus", [3, 5, 31])
def test_vercel_rejects_odd_vcpus_above_one(cpus: int) -> None:
    """Reject an odd vCPU count above 1."""
    with pytest.raises(SandboxSpecError, match="even number"):
        validate_spec(_spec(cpus=cpus), VERCEL_CAPABILITIES)


@pytest.mark.parametrize("cpus", [33, 34, 64])
def test_vercel_rejects_more_than_32_vcpus(cpus: int) -> None:
    """Reject more than 32 vCPUs."""
    with pytest.raises(SandboxSpecError, match="at most 32 vCPUs"):
        validate_spec(_spec(cpus=cpus), VERCEL_CAPABILITIES)


@pytest.mark.parametrize("cpus", [0, -2, True, 2.0])
def test_rejects_non_positive_or_non_integer_vcpus(cpus: object) -> None:
    """Reject zero, negative, bool and float vCPU counts."""
    with pytest.raises(SandboxSpecError, match="whole, positive"):
        validate_spec(_spec(cpus=cpus), VERCEL_CAPABILITIES)


@pytest.mark.parametrize(
    ("resources", "message"),
    [
        ({"memory_mb": True}, "memory must be a whole"),
        ({"memory_mb": 1024.0}, "memory must be a whole"),
        ({"disk_mb": True}, "disk must be a whole"),
        ({"disk_mb": 1024.5}, "disk must be a whole"),
        ({"gpu": True}, "number of GPUs"),
        ({"gpu": 1.0}, "number of GPUs"),
        ({"gpu": -1}, "non-negative number of GPUs"),
    ],
)
def test_rejects_bool_float_or_negative_sizes(resources: dict, message: str) -> None:
    """Reject bool and float memory, disk and GPU sizes, and a negative GPU count."""
    with pytest.raises(SandboxSpecError, match=message):
        validate_spec(_spec(**resources), VERCEL_CAPABILITIES)


def test_vercel_rejects_memory_above_2048_mb_per_vcpu() -> None:
    """Reject memory above 2048 MB for each vCPU."""
    validate_spec(_spec(cpus=2, memory_mb=4096), VERCEL_CAPABILITIES)
    with pytest.raises(SandboxSpecError, match="2048 MB of memory per vCPU"):
        validate_spec(_spec(cpus=2, memory_mb=4097), VERCEL_CAPABILITIES)


def test_vercel_rejects_disk_above_32_gb() -> None:
    """Reject disk above 32 GB."""
    validate_spec(_spec(disk_mb=32 * 1024), VERCEL_CAPABILITIES)
    with pytest.raises(SandboxSpecError, match="MB of disk"):
        validate_spec(_spec(disk_mb=32 * 1024 + 1), VERCEL_CAPABILITIES)


def test_rejects_gpus_when_the_provider_has_none() -> None:
    """Reject a GPU request on a provider without GPUs."""
    with pytest.raises(SandboxSpecError, match="GPUs"):
        validate_spec(_spec(gpu=1), VERCEL_CAPABILITIES)


def test_ignore_mode_skips_the_size_checks() -> None:
    """Skip the vCPU and memory checks when the mode is IGNORE."""
    validate_spec(_spec(cpus=3, memory_mb=10**6, mode=ResourceMode.IGNORE), VERCEL_CAPABILITIES)


@pytest.mark.parametrize("lifetime", [1, 0.5, 300])
def test_accepts_a_finite_positive_lifetime(lifetime: float) -> None:
    """Accept an int or float lifetime above zero."""
    validate_spec(SandboxSpec(lifetime_seconds=lifetime), VERCEL_CAPABILITIES)


def test_resource_request_defaults_to_request_mode() -> None:
    """Use REQUEST as the default resource mode."""
    assert ResourceRequest().mode is ResourceMode.REQUEST


@pytest.mark.parametrize("lifetime", [0, -1, float("inf"), float("nan"), True, "60", None])
def test_rejects_a_lifetime_that_is_not_finite_and_positive(lifetime: object) -> None:
    """Reject zero, negative, infinite, bool and non-number lifetimes."""
    with pytest.raises(SandboxSpecError, match="lifetime"):
        validate_spec(SandboxSpec(lifetime_seconds=lifetime), VERCEL_CAPABILITIES)


@pytest.mark.parametrize(
    ("spec_fields", "message"),
    [
        ({"network_disabled": True, "allowed_hosts": ("a.example",)}, "no network"),
        ({"network_disabled": True, "inject_headers": {"a.example": {"x": "1"}}}, "no network"),
        ({"allowed_hosts": ("a.example",), "inject_headers": {"b.example": {"x": "1"}}}, "only for hosts"),
    ],
)
def test_rejects_contradictory_network_specs(spec_fields: dict, message: str) -> None:
    """Reject a spec whose egress policy contradicts its header injection."""
    with pytest.raises(SandboxSpecError, match=message):
        validate_spec(SandboxSpec(lifetime_seconds=60, **spec_fields), VERCEL_CAPABILITIES)


def test_open_egress_allows_header_injection() -> None:
    """Accept header injection when an empty allowlist leaves egress open."""
    validate_spec(SandboxSpec(lifetime_seconds=60, inject_headers={"a.example": {"x": "1"}}), VERCEL_CAPABILITIES)


def test_rejects_unsupported_network_features() -> None:
    """Reject header injection and network modes that the provider lacks."""
    caps = EnvironmentCapabilities(name="plain")
    with pytest.raises(SandboxSpecError, match="inject egress headers"):
        validate_spec(
            SandboxSpec(lifetime_seconds=60, allowed_hosts=("a.example",), inject_headers={"a.example": {"x": "1"}}),
            caps,
        )
    with pytest.raises(SandboxSpecError, match="deny_all"):
        validate_spec(SandboxSpec(lifetime_seconds=60, network_disabled=True), caps)
    with pytest.raises(SandboxSpecError, match="allowlist"):
        validate_spec(SandboxSpec(lifetime_seconds=60, allowed_hosts=("a.example",)), caps)


def test_resolve_resources_derives_vercel_memory() -> None:
    """Set memory to 2048 MB per vCPU when the request leaves it unset."""
    assert resolve_resources(ResourceRequest(cpus=4), VERCEL_CAPABILITIES).memory_mb == 8192
    assert resolve_resources(ResourceRequest(cpus=4), EnvironmentCapabilities(name="plain")).memory_mb is None


def test_resolve_resources_raises_a_smaller_vercel_memory_to_the_vcpu_size() -> None:
    """Replace a requested memory below 2048 MB per vCPU with the size Vercel bills."""
    request = ResourceRequest(cpus=4, memory_mb=1024)
    validate_spec(SandboxSpec(lifetime_seconds=60, resources=request), VERCEL_CAPABILITIES)
    assert resolve_resources(request, VERCEL_CAPABILITIES).memory_mb == 8192
    plain = EnvironmentCapabilities(name="plain")
    assert resolve_resources(request, plain).memory_mb == 1024


def test_provider_rejects_an_invalid_spec_at_construction() -> None:
    """Check the spec against the provider capabilities in the constructor."""
    with pytest.raises(SandboxSpecError, match="even number"):
        InMemorySandbox(_spec(cpus=3))


async def test_healthcheck_retries_until_success() -> None:
    """Return once the command passes before the retry limit."""
    async with InMemorySandbox(_spec()) as env:
        env.script("check", ExecResult(1), ExecResult(1), ExecResult(0))
        await env.healthcheck(HealthcheckConfig(command="check", interval_sec=0, retries=3))
        assert env.commands == ["check"] * 3


async def test_healthcheck_ignores_failures_in_the_start_period() -> None:
    """Do not count failures in the start period toward the retries."""
    async with InMemorySandbox(_spec()) as env:
        env.script("check", *[ExecResult(1)] * 5, ExecResult(0))
        config = HealthcheckConfig(command="check", start_period_sec=60, start_interval_sec=0, retries=1)
        await env.healthcheck(config)
        assert len(env.commands) == 6


async def test_healthcheck_start_period_uses_the_probe_start_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip a probe that starts in the start period, even when it ends after it."""
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(environment_module, "time", SimpleNamespace(monotonic=lambda: clock.now))

    class SlowProbe(InMemorySandbox):
        async def exec(self, command: str, **kwargs) -> ExecResult:
            """Fail every probe after it runs for its full timeout.

            Args:
                command: The shell command.
                **kwargs: The exec options.

            Returns:
                A failed result.
            """
            self.commands.append(command)
            clock.now += kwargs["timeout_sec"]
            return ExecResult(1, stderr="timed out")

    async with SlowProbe(_spec()) as env:
        config = HealthcheckConfig(
            command="check", start_period_sec=10, timeout_sec=30, retries=1, interval_sec=0, start_interval_sec=0
        )
        with pytest.raises(HealthcheckError, match="1 times"):
            await env.healthcheck(config)
        assert env.commands == ["check", "check"]


async def test_healthcheck_error_reports_the_last_stderr() -> None:
    """Put the failure count and the last stderr in the error."""
    async with InMemorySandbox(_spec()) as env:
        env.script("check", ExecResult(1), ExecResult(2, stderr="not ready\n"))
        with pytest.raises(HealthcheckError, match=r"2 times.*exit code 2.*not ready"):
            await env.healthcheck(HealthcheckConfig(command="check", interval_sec=0, retries=2))


async def test_healthcheck_counts_an_exec_error_as_a_failed_probe() -> None:
    """Count an exec exception as one failed probe and report its text."""

    class FlakyExec(InMemorySandbox):
        async def exec(self, command: str, **kwargs) -> ExecResult:
            """Raise a transport error on the first probe, then pass.

            Args:
                command: The shell command.
                **kwargs: The exec options.

            Returns:
                A passing result after the first probe.

            Raises:
                ConnectionError: On the first probe.
            """
            self.commands.append(command)
            if len(self.commands) == 1:
                raise ConnectionError("link reset")
            return ExecResult(0)

    class BrokenExec(InMemorySandbox):
        async def exec(self, command: str, **kwargs) -> ExecResult:
            """Raise a transport error on every probe.

            Args:
                command: The shell command.
                **kwargs: The exec options.

            Raises:
                ConnectionError: Always.
            """
            self.commands.append(command)
            raise ConnectionError("link reset")

    async with FlakyExec(_spec()) as env:
        await env.healthcheck(HealthcheckConfig(command="check", interval_sec=0, retries=2))
        assert env.commands == ["check", "check"]
    async with BrokenExec(_spec()) as env:
        with pytest.raises(HealthcheckError, match=r"2 times.*ConnectionError: link reset"):
            await env.healthcheck(HealthcheckConfig(command="check", interval_sec=0, retries=2))


async def test_context_manager_stops_when_start_fails() -> None:
    """Stop the sandbox when start raises inside ``async with``."""

    class FailingStart(InMemorySandbox):
        stopped = False

        async def start(self) -> None:
            """Fail after the sandbox is half started."""
            await super().start()
            raise RuntimeError("boom")

        async def stop(self) -> None:
            """Record the stop."""
            self.stopped = True
            await super().stop()

    env = FailingStart(_spec())
    with pytest.raises(RuntimeError, match="boom"):
        async with env:
            pass
    assert env.stopped
    assert not env.running


async def test_context_manager_raises_the_start_error_when_stop_fails(caplog: pytest.LogCaptureFixture) -> None:
    """Raise the start error, not the stop error, and log the stop error."""

    class FailingStartAndStop(InMemorySandbox):
        async def start(self) -> None:
            """Fail after the sandbox is half started.

            Raises:
                RuntimeError: Always.
            """
            await super().start()
            raise RuntimeError("start boom")

    env = FailingStartAndStop(_spec())
    env.fail_stop = True
    with pytest.raises(RuntimeError, match="start boom"):
        async with env:
            pass
    assert "did not stop after its start failed" in caplog.text
    assert "fake stop failure" in caplog.text
    env.fail_stop = False
    await env.stop()


async def test_stop_job_sandboxes_continues_after_a_failure() -> None:
    """Keep sweeping when one sandbox fails to stop."""
    job_id = f"sweep-{uuid.uuid4().hex}"
    broken, healthy = (InMemorySandbox(SandboxSpec(lifetime_seconds=60, tags={JOB_TAG: job_id})) for _ in range(2))
    await broken.start()
    await healthy.start()
    broken.fail_stop = True
    assert await InMemorySandbox.stop_job_sandboxes(job_id) == 1
    assert not healthy.running
    broken.fail_stop = False
    await broken.stop()


async def test_set_network_policy_applies_once_and_skips_no_change() -> None:
    """Apply a changed policy once and skip a policy that does not change."""
    async with InMemorySandbox(_spec()) as env:
        allowlist = NetworkPolicy(allowed_hosts=("a.example",))
        await env.set_network_policy(allowlist)
        await env.set_network_policy(allowlist)
        assert env.applied_policies == [allowlist]
        assert env.network_policy == allowlist


async def test_set_network_policy_keeps_header_injection_hosts() -> None:
    """Reject an allowlist that drops a host which receives injected headers."""
    spec = SandboxSpec(lifetime_seconds=60, allowed_hosts=("a.example",), inject_headers={"a.example": {"x": "1"}})
    async with InMemorySandbox(spec) as env:
        with pytest.raises(SandboxSpecError, match="injected headers"):
            await env.set_network_policy(NetworkPolicy(allowed_hosts=("b.example",)))
        await env.disable_network()
        assert env.network_policy.disabled


async def test_set_network_policy_allows_open_egress_with_header_injection() -> None:
    """Accept an open-egress policy on a sandbox that injects headers."""
    spec = SandboxSpec(lifetime_seconds=60, allowed_hosts=("a.example",), inject_headers={"a.example": {"x": "1"}})
    async with InMemorySandbox(spec) as env:
        await env.set_network_policy(NetworkPolicy())
        assert env.network_policy == NetworkPolicy()


async def test_set_network_policy_needs_dynamic_policy_support() -> None:
    """Reject a policy change on a provider without dynamic policy support."""

    class StaticPolicy(InMemorySandbox):
        capabilities = EnvironmentCapabilities(name="static", network_modes=frozenset(NetworkMode))

    async with StaticPolicy(_spec()) as env:
        with pytest.raises(SandboxSpecError, match="cannot change"):
            await env.disable_network()


async def test_disable_network_applies_after_a_cancelled_apply() -> None:
    """Send deny-all to the provider after a cancelled apply left egress open."""

    class SlowApply(InMemorySandbox):
        async def _apply_network_policy(self, policy: NetworkPolicy) -> None:
            """Record the policy as the provider does, then hang.

            Args:
                policy: The checked policy.
            """
            self.applied_policies.append(policy)
            if not policy.disabled:
                await asyncio.sleep(10)

    async with SlowApply(SandboxSpec(lifetime_seconds=60, network_disabled=True)) as env:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(env.set_network_policy(NetworkPolicy()), 0.01)
        assert env.network_policy is None
        await env.disable_network()
        assert env.applied_policies == [NetworkPolicy(), NetworkPolicy(disabled=True)]
        assert env.network_policy == NetworkPolicy(disabled=True)


async def test_disable_network_applies_after_a_failed_apply() -> None:
    """Send deny-all to the provider after an apply raised past the change."""

    class FailingApply(InMemorySandbox):
        async def _apply_network_policy(self, policy: NetworkPolicy) -> None:
            """Record the policy as the provider does, then fail the confirm.

            Args:
                policy: The checked policy.

            Raises:
                ConnectionError: For every policy that opens egress.
            """
            self.applied_policies.append(policy)
            if not policy.disabled:
                raise ConnectionError("confirm lost")

    async with FailingApply(SandboxSpec(lifetime_seconds=60, network_disabled=True)) as env:
        with pytest.raises(ConnectionError, match="confirm lost"):
            await env.set_network_policy(NetworkPolicy())
        assert env.network_policy is None
        await env.disable_network()
        assert env.applied_policies == [NetworkPolicy(), NetworkPolicy(disabled=True)]
        assert env.network_policy == NetworkPolicy(disabled=True)


async def test_concurrent_policy_changes_keep_the_cache_in_step_with_the_provider() -> None:
    """Keep the cache on the last applied policy when calls overlap."""

    class SlowDisable(InMemorySandbox):
        async def _apply_network_policy(self, policy: NetworkPolicy) -> None:
            """Record the policy as the provider does, then confirm deny-all late.

            Args:
                policy: The checked policy.
            """
            self.applied_policies.append(policy)
            await asyncio.sleep(0.05 if policy.disabled else 0.01)

    async with SlowDisable(_spec()) as env:
        await asyncio.gather(env.disable_network(), env.set_network_policy(NetworkPolicy()))
        assert env.network_policy == env.applied_policies[-1]
        await env.disable_network()
        assert env.applied_policies[-1] == NetworkPolicy(disabled=True)
        assert env.network_policy == NetworkPolicy(disabled=True)


def test_fake_capabilities_match_vercel_limits() -> None:
    """Keep the fake on the Vercel size limits so the contract checks them."""
    assert FAKE_CAPABILITIES.max_vcpus == VERCEL_CAPABILITIES.max_vcpus
    assert FAKE_CAPABILITIES.memory_mb_per_vcpu == VERCEL_CAPABILITIES.memory_mb_per_vcpu
