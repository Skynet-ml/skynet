"""Tests for the user-image builders and the input hash (SKY-888).

The CodeBuild builder runs against fake CodeBuild, CloudWatch Logs, S3 and
registry objects, so no test calls AWS. One test builds a real small image
with the local Docker engine and skips when Docker is not there.
"""

from __future__ import annotations

import base64
import io
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any

import pytest

from core.sandbox import image_build
from core.sandbox.image_build import (
    BUILDKIT_IMAGE,
    MAX_LOG_CHARS,
    BuildLimits,
    BuildRequest,
    CodeBuildImageBuilder,
    ImageBuildError,
    ImageBuildTimeoutError,
    ImageTooLargeError,
    LocalDockerImageBuilder,
    cap_log,
    compute_input_hash,
    log_tail,
)
from core.sandbox.image_registry import ImageRegistryError, RegistryCredentials, RegistryImage

SERVER = "123456789012.dkr.ecr.us-east-2.amazonaws.com"
PREFIX = "skynet/user-images-staging"
OWNER = "user-7"
REPO = f"{SERVER}/{PREFIX}/{OWNER}"
DIGEST = "sha256:" + "b" * 64
PUSH_ROLE = "arn:aws:iam::123456789012:role/skynet-staging-image-build-push"
LIMITS = BuildLimits(timeout_seconds=3600.0, max_size_bytes=20_000_000_000, max_context_bytes=500_000_000)


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
    """Act as an owner-scoped registry that holds a set of digests."""

    server = SERVER

    def __init__(self, sizes: dict[str, int]) -> None:
        """Hold the image sizes by digest.

        Args:
            sizes: Image sizes by digest.
        """
        self.sizes = sizes
        self.deleted: list[tuple[str, str]] = []
        self.delete_error: str | None = None

    def _repo(self, owner: str) -> str:
        """Return the owner's repository URI.

        Args:
            owner: The owner.

        Returns:
            The repository URI.
        """
        return f"{SERVER}/{PREFIX}/{owner}"

    def push_reference(self, owner: str, tag: str) -> str:
        """Return the push reference.

        Args:
            owner: The owner.
            tag: The tag.

        Returns:
            The reference.
        """
        return f"{self._repo(owner)}:{tag}"

    def cache_reference(self, owner: str, scope: str) -> str:
        """Return the cache reference.

        Args:
            owner: The owner.
            scope: The cache scope.

        Returns:
            The reference.
        """
        return f"{self._repo(owner)}:cache-{scope}"

    def pinned_reference(self, owner: str, digest: str) -> str:
        """Return the pinned reference.

        Args:
            owner: The owner.
            digest: The digest.

        Returns:
            The reference.
        """
        return f"{self._repo(owner)}@{digest}"

    def push_session_policy(self, owner: str) -> dict[str, Any]:
        """Return a session policy that names the owner.

        Args:
            owner: The owner.

        Returns:
            The policy.
        """
        return {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Resource": f"repository/{owner}"}]}

    def pull_credentials(self) -> RegistryCredentials:
        """Return fixed fake credentials.

        Returns:
            The credentials.
        """
        return RegistryCredentials(server=SERVER, username="AWS", password="x")

    def check_digest(self, owner: str, digest: str) -> RegistryImage:
        """Return the held image or fail.

        Args:
            owner: The owner.
            digest: The digest.

        Returns:
            The image.
        """
        if digest not in self.sizes:
            raise ImageRegistryError(f"no image {digest}")
        return RegistryImage(
            reference=self.pinned_reference(owner, digest), digest=digest, size_bytes=self.sizes[digest]
        )

    def delete_image(self, owner: str, digest: str) -> None:
        """Record a delete, or fail when told to.

        Args:
            owner: The owner.
            digest: The digest.
        """
        if self.delete_error:
            raise ImageRegistryError(self.delete_error)
        self.deleted.append((owner, digest))


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

    def __init__(self, lines: list[str], late: list[str] | None = None) -> None:
        """Hold the log lines.

        Args:
            lines: The log lines.
            late: Lines that arrive only on the second read at the end.
        """
        self.lines = lines
        self.late = late or []
        self.requests: list[dict[str, Any]] = []
        self.end_reads = 0

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
        if token == "f/2":
            self.end_reads += 1
            if self.end_reads == 2 and self.late:
                return {"events": [{"message": m + "\n"} for m in self.late], "nextForwardToken": "f/3"}
        return {"events": [], "nextForwardToken": token}


class FakeClock:
    """Advance by a fixed step on each sleep."""

    def __init__(self) -> None:
        """Start at zero."""
        self.now = 0.0
        self.sleeps: list[float] = []

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
        self.sleeps.append(seconds)
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
    late: list[str] | None = None,
    registry: FakeRegistry | None = None,
) -> tuple[CodeBuildImageBuilder, FakeS3]:
    """Build a CodeBuild builder on fakes.

    Args:
        codebuild: The fake CodeBuild client.
        sizes: Image sizes the fake registry holds.
        limits: The build limits.
        clock: The fake clock.
        lines: The build log lines.
        late: Log lines that arrive after the build ends.
        registry: The fake registry.

    Returns:
        The builder and its fake S3.
    """
    clock = clock or FakeClock()
    s3 = FakeS3()
    builder = CodeBuildImageBuilder(
        codebuild=codebuild,
        logs=FakeLogs(lines if lines is not None else [f"line {i}" for i in range(100)], late),
        s3=s3,
        registry=registry or FakeRegistry({DIGEST: 5_000} if sizes is None else sizes),  # type: ignore[arg-type]
        project_name="skynet-staging-image-build",
        source_bucket="skynet-staging-image-build-src",
        push_role_arn=PUSH_ROLE,
        limits=limits,
        sleep=clock.sleep,
        clock=clock,
    )
    return builder, s3


RUNNING = {"buildComplete": False, "currentPhase": "BUILD", **LOG_STREAM}
QUEUED = {"buildComplete": False, "currentPhase": "QUEUED", **LOG_STREAM}


def _request(context: Path, **fields: Any) -> BuildRequest:
    """Return a build request for the test owner.

    Args:
        context: The build context.
        **fields: Other request fields.

    Returns:
        The request.
    """
    return BuildRequest(context_dir=context, owner=OWNER, **fields)


def _hash(request: BuildRequest, builder: str = "codebuild", platform: str = "linux/amd64") -> str:
    """Hash a request with the test limits.

    Args:
        request: The build request.
        builder: The builder kind.
        platform: The platform.

    Returns:
        The input hash.
    """
    return compute_input_hash(request, builder=builder, platform=platform, max_context_bytes=LIMITS.max_context_bytes)


def test_input_hash_is_deterministic_and_covers_every_input(tmp_path: Path) -> None:
    """The same inputs hash the same; a change to any input changes the hash."""
    context = _context(tmp_path)
    request = _request(context, build_args={"B": "2", "A": "1"})
    first = _hash(request)

    assert first.startswith("sha256:")
    assert len(first) == 71
    assert _hash(_request(context, build_args={"A": "1", "B": "2"})) == first
    assert _hash(_request(context, build_args={"A": "1", "B": "3"})) != first
    assert _hash(_request(context, build_args={"A": "1"})) != first

    (context / "src" / "a.py").write_text("print('b')\n")
    changed_content = _hash(request)
    assert changed_content != first

    (context / "src" / "a.py").rename(context / "src" / "b.py")
    assert _hash(request) != changed_content

    (context / "Dockerfile").write_text("FROM scratch\n")
    assert _hash(request) not in {first, changed_content}


def test_input_hash_covers_the_builder_and_the_platform(tmp_path: Path) -> None:
    """The same context hashes differently for another builder or platform."""
    request = _request(_context(tmp_path))

    hashes = {
        _hash(request),
        _hash(request, builder="local-docker"),
        _hash(request, platform="linux/arm64"),
        _hash(request, platform=""),
    }

    assert len(hashes) == 4


def test_input_hash_covers_file_and_folder_modes(tmp_path: Path) -> None:
    """COPY keeps modes, so a mode change is a new input."""
    context = _context(tmp_path)
    request = _request(context)
    before = _hash(request)

    (context / "hello.txt").chmod(0o755)
    file_mode = _hash(request)
    (context / "src").chmod(0o700)

    assert len({before, file_mode, _hash(request)}) == 3


def test_input_hash_covers_the_dockerfile_path(tmp_path: Path) -> None:
    """Two Dockerfile paths with the same bytes hash differently (``<Dockerfile>.dockerignore``)."""
    context = _context(tmp_path)
    (context / "other.Dockerfile").write_text((context / "Dockerfile").read_text())

    assert _hash(_request(context)) != _hash(_request(context, dockerfile="other.Dockerfile"))
    assert _hash(_request(context)) == _hash(_request(context, dockerfile="./Dockerfile"))


def test_input_hash_covers_an_empty_folder(tmp_path: Path) -> None:
    """An empty folder is part of the context, so adding one changes the hash."""
    context = _context(tmp_path)
    before = _hash(_request(context))

    (context / "empty").mkdir()

    assert _hash(_request(context)) != before


def test_input_hash_does_not_depend_on_the_context_location(tmp_path: Path) -> None:
    """Two copies of one context in different folders hash the same."""
    one = _context(tmp_path / "one")
    two = _context(tmp_path / "two")

    assert _hash(_request(one)) == _hash(_request(two))


def test_input_hash_uses_a_symlink_target_and_never_follows_it(tmp_path: Path) -> None:
    """A symlink hashes as its link text, so a file outside the context never enters."""
    context = _context(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("one")
    (context / "link").symlink_to(outside)
    before = _hash(_request(context))

    outside.write_text("two")

    assert _hash(_request(context)) == before


def test_input_hash_refuses_a_context_over_the_size_limit(tmp_path: Path) -> None:
    """A context larger than the limit fails before any upload."""
    context = _context(tmp_path)
    (context / "big.bin").write_bytes(b"x" * 2000)

    with pytest.raises(ImageBuildError, match="larger than"):
        compute_input_hash(_request(context), builder="codebuild", platform="", max_context_bytes=1000)


def test_input_hash_fails_when_a_folder_cannot_be_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A walk error fails the build, so no folder drops out of the hash in silence."""
    context = _context(tmp_path)
    real_walk = os.walk

    def _walk(top: Any, **kwargs: Any) -> Any:
        """Report one unreadable folder, then walk.

        Args:
            top: The root.
            **kwargs: The walk options.

        Returns:
            The real walk.
        """
        kwargs["onerror"](PermissionError(13, "Permission denied", str(top)))
        return real_walk(top, **kwargs)

    monkeypatch.setattr(image_build.os, "walk", _walk)

    with pytest.raises(ImageBuildError, match="cannot read the build context"):
        _hash(_request(context))


def test_input_hash_refuses_a_name_that_is_not_utf8(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A file name that is not valid UTF-8 fails with a clear error."""
    context = _context(tmp_path)
    real_walk = os.walk

    def _walk(top: Any, **kwargs: Any) -> Any:
        """Add a name with an undecodable byte to the root folder.

        Args:
            top: The root.
            **kwargs: The walk options.

        Yields:
            The real walk entries, with one bad name in the root.
        """
        for root, dirs, files in real_walk(top, **kwargs):
            yield root, dirs, [*files, "bad\udcff.txt"] if Path(root) == Path(top) else files

    monkeypatch.setattr(image_build.os, "walk", _walk)

    with pytest.raises(ImageBuildError, match="not valid UTF-8"):
        _hash(_request(context))


@pytest.mark.parametrize("dockerfile", ["../Dockerfile", "/etc/Dockerfile", "missing/Dockerfile", "src"])
def test_request_refuses_a_dockerfile_outside_the_context(tmp_path: Path, dockerfile: str) -> None:
    """The Dockerfile must be a regular file inside the context."""
    with pytest.raises(ImageBuildError, match="Dockerfile"):
        _hash(_request(_context(tmp_path), dockerfile=dockerfile))


def test_request_refuses_a_dockerfile_under_a_symlinked_folder(tmp_path: Path) -> None:
    """A symlink in any part of the Dockerfile path is refused, so it cannot read outside files."""
    context = _context(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "Dockerfile").write_text("FROM scratch\n")
    (context / "a").symlink_to(outside)
    (context / "inner").symlink_to(context / "src")

    with pytest.raises(ImageBuildError, match="symlink"):
        _hash(_request(context, dockerfile="a/Dockerfile"))
    with pytest.raises(ImageBuildError, match="symlink"):
        _hash(_request(context, dockerfile="inner/a.py"))


def test_request_refuses_a_symlinked_dockerfile(tmp_path: Path) -> None:
    """A Dockerfile that is itself a symlink is refused."""
    context = _context(tmp_path)
    (context / "Linked").symlink_to(context / "Dockerfile")

    with pytest.raises(ImageBuildError, match="symlink"):
        _hash(_request(context, dockerfile="Linked"))


def test_request_refuses_an_invalid_build_arg_name(tmp_path: Path) -> None:
    """A build arg name with shell characters is refused."""
    with pytest.raises(ImageBuildError, match="build arg"):
        _hash(_request(_context(tmp_path), build_args={"A;rm": "1"}))


def test_codebuild_build_returns_digest_size_hash_and_logs(tmp_path: Path) -> None:
    """A successful build returns the pinned image, its size, the hash, the full log and a tail."""
    context = _context(tmp_path)
    (context / "link").symlink_to("hello.txt")
    (context / "empty").mkdir()
    request = _request(context, build_args={"PY": "3.12"})
    codebuild = FakeCodeBuild([RUNNING, _done()])
    builder, s3 = _builder(codebuild)

    result = builder.build(request)

    assert result.image == f"{REPO}@{DIGEST}"
    assert (result.digest, result.size_bytes) == (DIGEST, 5_000)
    assert result.input_hash == _hash(request)
    assert result.log.splitlines() == [f"line {i}" for i in range(100)]
    assert result.log_tail.splitlines() == [f"line {i}" for i in range(60, 100)]

    started = codebuild.started
    assert started["projectName"] == "skynet-staging-image-build"
    assert started["sourceTypeOverride"] == "S3"
    assert started["timeoutInMinutesOverride"] == 60
    assert started["queuedTimeoutInMinutesOverride"] == 60
    [(bucket, key)] = s3.objects
    assert started["sourceLocationOverride"] == f"{bucket}/{key}"
    assert key.startswith(f"image-builds/{OWNER}/{result.input_hash.removeprefix('sha256:')}/")

    with zipfile.ZipFile(io.BytesIO(s3.objects[(bucket, key)])) as archive:
        assert sorted(archive.namelist()) == ["Dockerfile", "empty/", "hello.txt", "link", "src/", "src/a.py"]
        assert archive.read("link") == b"hello.txt"
        assert (archive.getinfo("link").external_attr >> 16) & 0o170000 == 0o120000

    spec = json.loads(started["buildspecOverride"])
    command = spec["phases"]["build"]["commands"][0]
    assert "docker buildx build" in command
    assert "--platform linux/amd64" in command
    assert f"--tag {REPO}:" in command
    assert f"--cache-from type=registry,ref={REPO}:cache-default" in command
    assert (
        f"--cache-to type=registry,ref={REPO}:cache-default,mode=max,image-manifest=true,oci-mediatypes=true" in command
    )
    assert "--build-arg PY=3.12" in command
    assert "--push" in command
    assert spec["env"]["exported-variables"] == ["IMAGE_DIGEST"]


def test_buildspec_blocks_link_local_and_scopes_the_push_credentials(tmp_path: Path) -> None:
    """Pre-build blocks the metadata range, pins BuildKit and logs in only for the owner."""
    codebuild = FakeCodeBuild([_done()])
    builder, _ = _builder(codebuild)

    builder.build(_request(_context(tmp_path)))

    commands = json.loads(codebuild.started["buildspecOverride"])["phases"]["pre_build"]["commands"]
    assert commands[0] == "iptables -I DOCKER-USER -d 169.254.0.0/16 -j DROP"
    assert all("169.254.169.253/32" in c and "--dport 53 -j ACCEPT" in c for c in commands[1:3])
    assert commands[-1] == (f"docker buildx create --use --driver docker-container --driver-opt image={BUILDKIT_IMAGE}")
    assert "@sha256:" in BUILDKIT_IMAGE
    assert not any("docker login" in c for c in commands)

    [helper_command] = [c for c in commands if "base64 -d" in c]
    helper = base64.b64decode(helper_command.split()[1]).decode()
    compile(helper, "helper", "exec")
    assert PUSH_ROLE in helper
    assert f"repository/{OWNER}" in helper
    assert repr(SERVER) in helper
    [config_command] = [c for c in commands if "config.json" in c]
    assert json.dumps({"credHelpers": {SERVER: "skynet-ecr"}}) in config_command


def test_buildspec_quotes_user_values(tmp_path: Path) -> None:
    """A build arg value with shell characters stays one quoted word."""
    codebuild = FakeCodeBuild([_done()])
    builder, _ = _builder(codebuild)

    builder.build(_request(_context(tmp_path), build_args={"X": "$(id); echo hi"}))

    command = json.loads(codebuild.started["buildspecOverride"])["phases"]["build"]["commands"][0]
    assert "--build-arg 'X=$(id); echo hi'" in command


def test_codebuild_stops_a_build_over_the_time_limit(tmp_path: Path) -> None:
    """A build still running at the limit is stopped and fails as a timeout."""
    codebuild = FakeCodeBuild([RUNNING])
    limits = BuildLimits(timeout_seconds=600.0, max_size_bytes=LIMITS.max_size_bytes, max_context_bytes=10**9)
    builder, _ = _builder(codebuild, limits=limits, lines=["step 1", "step 2"])

    with pytest.raises(ImageBuildTimeoutError, match="10-minute limit") as caught:
        builder.build(_request(_context(tmp_path)))

    assert codebuild.stopped == ["proj:1"]
    assert caught.value.log_tail == "step 1\nstep 2"
    assert codebuild.started["timeoutInMinutesOverride"] == 10


def test_codebuild_time_limit_counts_from_the_end_of_the_queue(tmp_path: Path) -> None:
    """Queue time does not count toward the run time limit."""
    clock = FakeClock()
    codebuild = FakeCodeBuild([QUEUED] * 30 + [RUNNING])
    limits = BuildLimits(timeout_seconds=600.0, max_size_bytes=LIMITS.max_size_bytes, max_context_bytes=10**9)
    builder, _ = _builder(codebuild, limits=limits, clock=clock)

    with pytest.raises(ImageBuildTimeoutError, match="10-minute limit"):
        builder.build(_request(_context(tmp_path)))

    assert clock.now == 300.0 + 600.0
    assert codebuild.stopped == ["proj:1"]


def test_codebuild_stops_a_build_queued_too_long(tmp_path: Path) -> None:
    """A build that never leaves the queue stops at the queue limit."""
    clock = FakeClock()
    codebuild = FakeCodeBuild([QUEUED])
    builder, _ = _builder(codebuild, clock=clock)

    with pytest.raises(ImageBuildTimeoutError, match="60-minute queue limit"):
        builder.build(_request(_context(tmp_path)))

    assert clock.now == 3600.0
    assert codebuild.stopped == ["proj:1"]


def test_codebuild_reports_its_own_timeout_as_a_timeout(tmp_path: Path) -> None:
    """CodeBuild's TIMED_OUT status fails with the time limit error."""
    builder, _ = _builder(FakeCodeBuild([_done("TIMED_OUT", digest=None)]))

    with pytest.raises(ImageBuildTimeoutError, match="60-minute limit"):
        builder.build(_request(_context(tmp_path)))


def test_codebuild_failure_keeps_the_full_log_and_the_tail(tmp_path: Path) -> None:
    """A failed build raises with the full log apart from the short tail."""
    builder, _ = _builder(FakeCodeBuild([_done("FAILED", digest=None)]))

    with pytest.raises(ImageBuildError, match="FAILED") as caught:
        builder.build(_request(_context(tmp_path)))

    assert len(caught.value.log.splitlines()) == 100
    assert len(caught.value.log_tail.splitlines()) == 40


def test_codebuild_reads_log_lines_that_arrive_after_the_end(tmp_path: Path) -> None:
    """The builder waits a short time and reads once more, for late CloudWatch events."""
    clock = FakeClock()
    builder, _ = _builder(FakeCodeBuild([_done("FAILED", digest=None)]), clock=clock, lines=["a", "b"], late=["c"])

    with pytest.raises(ImageBuildError) as caught:
        builder.build(_request(_context(tmp_path)))

    assert caught.value.log.splitlines() == ["a", "b", "c"]
    assert clock.sleeps == [5.0]


def test_codebuild_caps_a_long_log(tmp_path: Path) -> None:
    """A huge log keeps its head and tail with a cut marker."""
    lines = ["x" * 999 for _ in range(4000)]
    builder, _ = _builder(FakeCodeBuild([_done("FAILED", digest=None)]), lines=lines)

    with pytest.raises(ImageBuildError) as caught:
        builder.build(_request(_context(tmp_path)))

    assert len(caught.value.log) <= MAX_LOG_CHARS + 100
    assert "log lines cut ...]" in caught.value.log


def test_codebuild_fails_when_the_digest_is_missing_from_the_registry(tmp_path: Path) -> None:
    """A success that names no held digest is still a failed build."""
    builder, _ = _builder(FakeCodeBuild([_done()]), sizes={})

    with pytest.raises(ImageBuildError, match="no usable image"):
        builder.build(_request(_context(tmp_path)))


def test_codebuild_deletes_an_image_over_the_size_limit(tmp_path: Path) -> None:
    """An image over the size limit fails with a clear error and leaves the registry."""
    registry = FakeRegistry({DIGEST: 21_000_000_000})
    builder, _ = _builder(FakeCodeBuild([_done()]), registry=registry)

    with pytest.raises(ImageTooLargeError, match=r"21\.00 GB, over the 20 GB limit$"):
        builder.build(_request(_context(tmp_path)))

    assert registry.deleted == [(OWNER, DIGEST)]


def test_codebuild_reports_a_failed_delete_of_an_over_size_image(tmp_path: Path) -> None:
    """When the delete fails, the error says that the image stays."""
    registry = FakeRegistry({DIGEST: 21_000_000_000})
    registry.delete_error = "denied"
    builder, _ = _builder(FakeCodeBuild([_done()]), registry=registry)

    with pytest.raises(ImageTooLargeError, match="stays in the registry"):
        builder.build(_request(_context(tmp_path)))


def test_codebuild_refuses_an_invalid_owner(tmp_path: Path) -> None:
    """A registry error for the owner fails the build before any upload."""
    registry = FakeRegistry({})

    def _refuse(owner: str, tag: str) -> str:
        """Refuse every owner.

        Args:
            owner: The owner.
            tag: The tag.

        Returns:
            Never.
        """
        raise ImageRegistryError(f"not a valid image owner: {owner!r}")

    registry.push_reference = _refuse  # type: ignore[method-assign]
    builder, s3 = _builder(FakeCodeBuild([_done()]), registry=registry)

    with pytest.raises(ImageBuildError, match="not a valid image owner"):
        builder.build(_request(_context(tmp_path)))
    assert s3.objects == {}


def test_limits_come_from_the_settings() -> None:
    """The default limits are 60 minutes, 20 GB and a 500 MB context, read from the settings."""
    assert BuildLimits.from_settings() == LIMITS


@pytest.mark.parametrize("seconds", [0.0, -1.0, 480 * 60 + 1.0])
def test_limits_refuse_a_time_limit_out_of_range(seconds: float) -> None:
    """The time limit must be over 0 and at most 480 minutes."""
    with pytest.raises(ValueError, match="480 minutes"):
        BuildLimits(timeout_seconds=seconds, max_size_bytes=1, max_context_bytes=1)


def test_log_tail_keeps_the_last_lines() -> None:
    """The tail keeps only the last lines of the log."""
    assert log_tail("a\nb\nc\n", lines=2) == "b\nc"


def test_cap_log_keeps_the_head_and_the_tail() -> None:
    """A log over the limit keeps its first and last lines and marks the cut."""
    log = "\n".join(f"line {i:03}" for i in range(100))

    capped = cap_log(log, limit=100)

    lines = capped.splitlines()
    assert lines[0] == "line 000"
    assert lines[-1] == "line 099"
    assert any(line.startswith("[... ") and line.endswith(" log lines cut ...]") for line in lines)
    assert len(capped) <= 100 + 40
    assert cap_log("short", limit=100) == "short"


def test_cap_log_cuts_one_very_long_line() -> None:
    """A single line longer than the limit cannot pass the cap."""
    assert len(cap_log("x" * 1000, limit=100)) <= 100


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
    builder = LocalDockerImageBuilder(
        limits=BuildLimits(timeout_seconds=1.0, max_size_bytes=10, max_context_bytes=10**9)
    )

    with pytest.raises(ImageBuildTimeoutError) as caught:
        builder.build(_request(_context(tmp_path)))

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

    result = builder.build(_request(context, build_args={"UNUSED": "1"}))

    assert result.digest.startswith("sha256:")
    assert result.image == result.digest
    assert 0 < result.size_bytes < 1_000_000
    assert result.input_hash == _hash(
        _request(context, build_args={"UNUSED": "1"}), builder="local-docker", platform=""
    )
    assert result.log

    small = BuildLimits(timeout_seconds=600.0, max_size_bytes=1, max_context_bytes=10**9)
    with pytest.raises(ImageTooLargeError):
        LocalDockerImageBuilder(limits=small).build(_request(context))
    subprocess.run(["docker", "image", "rm", "-f", result.digest], capture_output=True, check=False)


def test_local_docker_failure_keeps_the_log(tmp_path: Path) -> None:
    """A docker CLI that fails gives an error with its output."""
    context = _context(tmp_path)
    fake = tmp_path / "docker"
    fake.write_text("#!/bin/sh\necho 'ERROR: failed to solve'\nexit 1\n")
    fake.chmod(0o755)

    with pytest.raises(ImageBuildError, match="exited with code 1") as caught:
        LocalDockerImageBuilder(docker=os.fspath(fake), limits=LIMITS).build(_request(context))

    assert "failed to solve" in caught.value.log_tail


def test_local_docker_caps_its_log_and_passes_the_platform(tmp_path: Path) -> None:
    """The local log is capped, and the platform goes to docker build."""
    context = _context(tmp_path)
    fake = tmp_path / "docker"
    fake.write_text('#!/bin/sh\necho "$@"\nyes line | head -n 400000\nexit 1\n')
    fake.chmod(0o755)

    builder = LocalDockerImageBuilder(docker=os.fspath(fake), limits=LIMITS, platform="linux/amd64")
    with pytest.raises(ImageBuildError) as caught:
        builder.build(_request(context))

    assert len(caught.value.log) <= MAX_LOG_CHARS + 100
    assert "--platform linux/amd64" in caught.value.log.splitlines()[0]
    assert "log lines cut ...]" in caught.value.log
