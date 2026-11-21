"""Tests for the user-image builders and the input hash (SKY-888).

The CodeBuild builder runs against fake CodeBuild, CloudWatch Logs, S3 and
registry objects, so no test calls AWS. One test builds a real small image
with the local Docker engine and skips when Docker is not there.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any

import pytest

from core.sandbox.image_build import (
    BuildLimits,
    BuildRequest,
    CodeBuildImageBuilder,
    ImageBuildError,
    ImageBuildTimeoutError,
    ImageTooLargeError,
    LocalDockerImageBuilder,
    compute_input_hash,
    log_tail,
)
from core.sandbox.image_registry import ImageRegistryError, RegistryCredentials, RegistryImage

REPO = "123456789012.dkr.ecr.us-east-2.amazonaws.com/skynet-staging-user-images"
DIGEST = "sha256:" + "b" * 64
LIMITS = BuildLimits(timeout_seconds=3600.0, max_size_bytes=20_000_000_000)


def _context(tmp_path: Path) -> Path:
    """Write a small build context.

    Args:
        tmp_path: The pytest temporary directory.

    Returns:
        The context directory.
    """
    context = tmp_path / "ctx"
    (context / "src").mkdir(parents=True)
    (context / "Dockerfile").write_text("FROM scratch\nCOPY hello.txt /hello.txt\n")
    (context / "hello.txt").write_text("hello\n")
    (context / "src" / "a.py").write_text("print('a')\n")
    return context


class FakeRegistry:
    """Act as a registry that holds a set of digests."""

    def __init__(self, sizes: dict[str, int]) -> None:
        """Hold the image sizes by digest.

        Args:
            sizes: Image sizes by digest.
        """
        self.sizes = sizes

    def push_reference(self, tag: str) -> str:
        """Return the push reference.

        Args:
            tag: The tag.

        Returns:
            The reference.
        """
        return f"{REPO}:{tag}"

    def cache_reference(self, scope: str) -> str:
        """Return the cache reference.

        Args:
            scope: The cache scope.

        Returns:
            The reference.
        """
        return f"{REPO}:cache-{scope}"

    def pinned_reference(self, digest: str) -> str:
        """Return the pinned reference.

        Args:
            digest: The digest.

        Returns:
            The reference.
        """
        return f"{REPO}@{digest}"

    def pull_credentials(self) -> RegistryCredentials:
        """Return fixed fake credentials.

        Returns:
            The credentials.
        """
        return RegistryCredentials(server=REPO.split("/")[0], username="AWS", password="x")

    def check_digest(self, digest: str) -> RegistryImage:
        """Return the held image or fail.

        Args:
            digest: The digest.

        Returns:
            The image.
        """
        if digest not in self.sizes:
            raise ImageRegistryError(f"no image {digest}")
        return RegistryImage(reference=self.pinned_reference(digest), digest=digest, size_bytes=self.sizes[digest])


class FakeS3:
    """Keep uploaded objects in memory."""

    def __init__(self) -> None:
        """Start empty."""
        self.objects: dict[tuple[str, str], bytes] = {}

    def upload_fileobj(self, handle: Any, bucket: str, key: str) -> None:
        """Store the uploaded bytes.

        Args:
            handle: The open file.
            bucket: The bucket.
            key: The key.
        """
        self.objects[(bucket, key)] = handle.read()


class FakeCodeBuild:
    """Return a scripted list of build states, one for each status read."""

    def __init__(self, states: list[dict[str, Any]]) -> None:
        """Hold the build states.

        Args:
            states: The build descriptions to return, in order. The last one repeats.
        """
        self.states = states
        self.started: dict[str, Any] = {}
        self.stopped: list[str] = []
        self.reads = 0

    def start_build(self, **kwargs: Any) -> dict[str, Any]:
        """Record the start request.

        Args:
            **kwargs: The request.

        Returns:
            The build id.
        """
        self.started = kwargs
        return {"build": {"id": "proj:1"}}

    def batch_get_builds(self, ids: list[str]) -> dict[str, Any]:
        """Return the next scripted state.

        Args:
            ids: The build ids.

        Returns:
            The builds.
        """
        state = self.states[min(self.reads, len(self.states) - 1)]
        self.reads += 1
        return {"builds": [{"id": ids[0], **state}]}

    def stop_build(self, id: str) -> dict[str, Any]:
        """Record a stop.

        Args:
            id: The build id.

        Returns:
            An empty response.
        """
        self.stopped.append(id)
        return {}


class FakeLogs:
    """Return a log in two pages, then the same token, like CloudWatch."""

    def __init__(self, lines: list[str]) -> None:
        """Hold the log lines.

        Args:
            lines: The log lines.
        """
        self.lines = lines
        self.requests: list[dict[str, Any]] = []

    def get_log_events(self, **kwargs: Any) -> dict[str, Any]:
        """Return one page of events.

        Args:
            **kwargs: The request.

        Returns:
            The page.
        """
        self.requests.append(kwargs)
        half = len(self.lines) // 2
        token = kwargs.get("nextToken")
        if token is None:
            return {"events": [{"message": m + "\n"} for m in self.lines[:half]], "nextForwardToken": "f/1"}
        if token == "f/1":
            return {"events": [{"message": m + "\n"} for m in self.lines[half:]], "nextForwardToken": "f/2"}
        return {"events": [], "nextForwardToken": token}


class FakeClock:
    """Advance by a fixed step on each sleep."""

    def __init__(self) -> None:
        """Start at zero."""
        self.now = 0.0

    def __call__(self) -> float:
        """Return the time.

        Returns:
            The fake time.
        """
        return self.now

    def sleep(self, seconds: float) -> None:
        """Move the time forward.

        Args:
            seconds: The wait.
        """
        self.now += seconds


LOG_STREAM = {"logs": {"groupName": "/aws/codebuild/x", "streamName": "s1"}}


def _done(status: str = "SUCCEEDED", digest: str | None = DIGEST) -> dict[str, Any]:
    """Return a finished build description.

    Args:
        status: The build status.
        digest: The exported digest, or ``None`` for none.

    Returns:
        The build description.
    """
    exported = [{"name": "IMAGE_DIGEST", "value": digest}] if digest else []
    return {"buildComplete": True, "buildStatus": status, "exportedEnvironmentVariables": exported, **LOG_STREAM}


def _builder(
    codebuild: FakeCodeBuild,
    *,
    sizes: dict[str, int] | None = None,
    limits: BuildLimits = LIMITS,
    clock: FakeClock | None = None,
    lines: list[str] | None = None,
) -> tuple[CodeBuildImageBuilder, FakeS3]:
    """Build a CodeBuild builder on fakes.

    Args:
        codebuild: The fake CodeBuild client.
        sizes: Image sizes the fake registry holds.
        limits: The build limits.
        clock: The fake clock.
        lines: The build log lines.

    Returns:
        The builder and its fake S3.
    """
    clock = clock or FakeClock()
    s3 = FakeS3()
    builder = CodeBuildImageBuilder(
        codebuild=codebuild,
        logs=FakeLogs(lines if lines is not None else [f"line {i}" for i in range(100)]),
        s3=s3,
        registry=FakeRegistry({DIGEST: 5_000} if sizes is None else sizes),
        project_name="skynet-staging-image-build",
        source_bucket="skynet-staging-image-build-src",
        limits=limits,
        sleep=clock.sleep,
        clock=clock,
    )
    return builder, s3


def test_input_hash_is_deterministic_and_covers_every_input(tmp_path: Path) -> None:
    """The same inputs hash the same; a change to any input changes the hash."""
    context = _context(tmp_path)
    request = BuildRequest(context_dir=context, cache_scope="u1", build_args={"B": "2", "A": "1"})
    first = compute_input_hash(request)

    assert first.startswith("sha256:")
    assert len(first) == 71
    assert (
        compute_input_hash(BuildRequest(context_dir=context, cache_scope="u1", build_args={"A": "1", "B": "2"}))
        == first
    )

    assert (
        compute_input_hash(BuildRequest(context_dir=context, cache_scope="u1", build_args={"A": "1", "B": "3"}))
        != first
    )
    assert compute_input_hash(BuildRequest(context_dir=context, cache_scope="u1", build_args={"A": "1"})) != first

    (context / "src" / "a.py").write_text("print('b')\n")
    changed_content = compute_input_hash(request)
    assert changed_content != first

    (context / "src" / "a.py").rename(context / "src" / "b.py")
    assert compute_input_hash(request) != changed_content

    (context / "Dockerfile").write_text("FROM scratch\n")
    assert compute_input_hash(request) not in {first, changed_content}


def test_input_hash_does_not_depend_on_the_context_location(tmp_path: Path) -> None:
    """Two copies of one context in different folders hash the same."""
    one = _context(tmp_path / "one")
    two = _context(tmp_path / "two")

    assert compute_input_hash(BuildRequest(context_dir=one, cache_scope="u1")) == compute_input_hash(
        BuildRequest(context_dir=two, cache_scope="u1")
    )


def test_input_hash_uses_a_symlink_target_and_never_follows_it(tmp_path: Path) -> None:
    """A symlink hashes as its link text, so a file outside the context never enters."""
    context = _context(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("one")
    (context / "link").symlink_to(outside)
    before = compute_input_hash(BuildRequest(context_dir=context, cache_scope="u1"))

    outside.write_text("two")

    assert compute_input_hash(BuildRequest(context_dir=context, cache_scope="u1")) == before


@pytest.mark.parametrize("dockerfile", ["../Dockerfile", "/etc/Dockerfile", "missing/Dockerfile"])
def test_request_refuses_a_dockerfile_outside_the_context(tmp_path: Path, dockerfile: str) -> None:
    """The Dockerfile must be a regular file inside the context."""
    with pytest.raises(ImageBuildError, match="Dockerfile"):
        compute_input_hash(BuildRequest(context_dir=_context(tmp_path), cache_scope="u1", dockerfile=dockerfile))


def test_request_refuses_an_invalid_build_arg_name(tmp_path: Path) -> None:
    """A build arg name with shell characters is refused."""
    with pytest.raises(ImageBuildError, match="build arg"):
        compute_input_hash(BuildRequest(context_dir=_context(tmp_path), cache_scope="u1", build_args={"A;rm": "1"}))


def test_codebuild_build_returns_digest_size_hash_and_logs(tmp_path: Path) -> None:
    """A successful build returns the pinned image, its size, the hash, the full log and a tail."""
    context = _context(tmp_path)
    (context / "link").symlink_to("hello.txt")
    request = BuildRequest(context_dir=context, build_args={"PY": "3.12"}, cache_scope="user-7")
    codebuild = FakeCodeBuild([{"buildComplete": False, **LOG_STREAM}, _done()])
    builder, s3 = _builder(codebuild)

    result = builder.build(request)

    assert result.image == f"{REPO}@{DIGEST}"
    assert (result.digest, result.size_bytes) == (DIGEST, 5_000)
    assert result.input_hash == compute_input_hash(request)
    assert result.log.splitlines() == [f"line {i}" for i in range(100)]
    assert result.log_tail.splitlines() == [f"line {i}" for i in range(60, 100)]

    started = codebuild.started
    assert started["projectName"] == "skynet-staging-image-build"
    assert started["sourceTypeOverride"] == "S3"
    assert started["timeoutInMinutesOverride"] == 60
    [(bucket, key)] = s3.objects
    assert started["sourceLocationOverride"] == f"{bucket}/{key}"
    assert key.startswith(f"image-builds/{result.input_hash.removeprefix('sha256:')}/")

    with zipfile.ZipFile(io.BytesIO(s3.objects[(bucket, key)])) as archive:
        assert sorted(archive.namelist()) == ["Dockerfile", "hello.txt", "link", "src/a.py"]
        assert archive.read("link") == b"hello.txt"
        assert (archive.getinfo("link").external_attr >> 16) & 0o170000 == 0o120000

    spec = json.loads(started["buildspecOverride"])
    command = spec["phases"]["build"]["commands"][0]
    assert "docker buildx build" in command
    assert f"--cache-from type=registry,ref={REPO}:cache-user-7" in command
    assert (
        f"--cache-to type=registry,ref={REPO}:cache-user-7,mode=max,image-manifest=true,oci-mediatypes=true" in command
    )
    assert "--build-arg PY=3.12" in command
    assert "--push" in command
    assert spec["env"]["exported-variables"] == ["IMAGE_DIGEST"]


def test_buildspec_quotes_user_values(tmp_path: Path) -> None:
    """A build arg value with shell characters stays one quoted word."""
    context = _context(tmp_path)
    codebuild = FakeCodeBuild([_done()])
    builder, _ = _builder(codebuild)

    builder.build(BuildRequest(context_dir=context, cache_scope="u1", build_args={"X": "$(id); echo hi"}))

    command = json.loads(codebuild.started["buildspecOverride"])["phases"]["build"]["commands"][0]
    assert "--build-arg 'X=$(id); echo hi'" in command


def test_codebuild_stops_a_build_over_the_time_limit(tmp_path: Path) -> None:
    """A build still running at the limit is stopped and fails as a timeout."""
    codebuild = FakeCodeBuild([{"buildComplete": False, **LOG_STREAM}])
    limits = BuildLimits(timeout_seconds=600.0, max_size_bytes=LIMITS.max_size_bytes)
    builder, _ = _builder(codebuild, limits=limits, lines=["step 1", "step 2"])

    with pytest.raises(ImageBuildTimeoutError, match="10-minute limit") as caught:
        builder.build(BuildRequest(context_dir=_context(tmp_path), cache_scope="u1"))

    assert codebuild.stopped == ["proj:1"]
    assert caught.value.log_tail == "step 1\nstep 2"
    assert codebuild.started["timeoutInMinutesOverride"] == 10


def test_codebuild_reports_its_own_timeout_as_a_timeout(tmp_path: Path) -> None:
    """CodeBuild's TIMED_OUT status fails with the time limit error."""
    builder, _ = _builder(FakeCodeBuild([_done("TIMED_OUT", digest=None)]))

    with pytest.raises(ImageBuildTimeoutError, match="60-minute limit"):
        builder.build(BuildRequest(context_dir=_context(tmp_path), cache_scope="u1"))


def test_codebuild_failure_keeps_the_full_log_and_the_tail(tmp_path: Path) -> None:
    """A failed build raises with the full log apart from the short tail."""
    builder, _ = _builder(FakeCodeBuild([_done("FAILED", digest=None)]))

    with pytest.raises(ImageBuildError, match="FAILED") as caught:
        builder.build(BuildRequest(context_dir=_context(tmp_path), cache_scope="u1"))

    assert len(caught.value.log.splitlines()) == 100
    assert len(caught.value.log_tail.splitlines()) == 40


def test_codebuild_fails_when_the_digest_is_missing_from_the_registry(tmp_path: Path) -> None:
    """A success that names no held digest is still a failed build."""
    builder, _ = _builder(FakeCodeBuild([_done()]), sizes={})

    with pytest.raises(ImageBuildError, match="no usable image"):
        builder.build(BuildRequest(context_dir=_context(tmp_path), cache_scope="u1"))


def test_codebuild_fails_an_image_over_the_size_limit(tmp_path: Path) -> None:
    """An image over the size limit fails with a clear error."""
    builder, _ = _builder(FakeCodeBuild([_done()]), sizes={DIGEST: 21_000_000_000})

    with pytest.raises(ImageTooLargeError, match=r"21\.00 GB, over the 20 GB limit"):
        builder.build(BuildRequest(context_dir=_context(tmp_path), cache_scope="u1"))


def test_limits_come_from_the_settings() -> None:
    """The default limits are 60 minutes and 20 GB, read from the settings."""
    limits = BuildLimits.from_settings()

    assert limits == BuildLimits(timeout_seconds=3600.0, max_size_bytes=20_000_000_000)


def test_log_tail_keeps_the_last_lines() -> None:
    """The tail keeps only the last lines of the log."""
    assert log_tail("a\nb\nc\n", lines=2) == "b\nc"


def test_local_builder_reports_a_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A local build over the time limit fails as a timeout, with its log so far."""

    def _slow(*args: Any, **kwargs: Any) -> None:
        """Raise the timeout that subprocess raises.

        Args:
            *args: The command.
            **kwargs: The options.
        """
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"], output=b"#1 building\n")

    monkeypatch.setattr(subprocess, "run", _slow)
    builder = LocalDockerImageBuilder(limits=BuildLimits(timeout_seconds=1.0, max_size_bytes=10))

    with pytest.raises(ImageBuildTimeoutError) as caught:
        builder.build(BuildRequest(context_dir=_context(tmp_path), cache_scope="u1"))

    assert caught.value.log_tail == "#1 building"


def _docker_ready() -> bool:
    """Say whether a Docker engine answers.

    Returns:
        True when ``docker info`` succeeds.
    """
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.mark.skipif(not _docker_ready(), reason="needs a running Docker engine")
def test_local_docker_builds_a_real_small_image(tmp_path: Path) -> None:
    """Local Docker builds a FROM scratch image and reports its id, size and log."""
    context = _context(tmp_path)
    builder = LocalDockerImageBuilder(limits=LIMITS)

    result = builder.build(BuildRequest(context_dir=context, cache_scope="u1", build_args={"UNUSED": "1"}))

    assert result.digest.startswith("sha256:")
    assert result.image == result.digest
    assert 0 < result.size_bytes < 1_000_000
    assert result.input_hash == compute_input_hash(
        BuildRequest(context_dir=context, cache_scope="u1", build_args={"UNUSED": "1"})
    )
    assert result.log

    with pytest.raises(ImageTooLargeError):
        LocalDockerImageBuilder(limits=BuildLimits(timeout_seconds=600.0, max_size_bytes=1)).build(
            BuildRequest(context_dir=context, cache_scope="u1")
        )
    subprocess.run(["docker", "image", "rm", "-f", result.digest], capture_output=True, check=False)


def test_local_docker_failure_keeps_the_log(tmp_path: Path) -> None:
    """A docker CLI that fails gives an error with its output."""
    context = _context(tmp_path)
    fake = tmp_path / "docker"
    fake.write_text("#!/bin/sh\necho 'ERROR: failed to solve'\nexit 1\n")
    fake.chmod(0o755)

    with pytest.raises(ImageBuildError, match="exited with code 1") as caught:
        LocalDockerImageBuilder(docker=os.fspath(fake), limits=LIMITS).build(
            BuildRequest(context_dir=context, cache_scope="u1")
        )

    assert "failed to solve" in caught.value.log_tail
