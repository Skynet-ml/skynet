"""Builder interface for user images, with AWS CodeBuild and local Docker builders (SKY-888).

A power user can bring a Dockerfile for the candidate box or the metric box
(SKY-737). A builder builds it with its build context and returns the image
digest, the image size, an input hash, the full build log and a short tail
of that log for the user. A cloud run builds on CodeBuild and pushes to the
registry; a local run builds with the local Docker engine.

The input hash covers the Dockerfile bytes, every path and byte in the build
context and the build args, so a later part can reuse a finished image when
the hash matches.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import tempfile
import time
import uuid
import zipfile
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import IO, Any, Protocol

from core.config import settings

from .image_registry import ImageRegistry, ImageRegistryError

_HASH_VERSION = b"skynet-image-input-v1"
_BUILD_ARG_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LOG_TAIL_LINES = 40
_READ_CHUNK = 1024 * 1024
_DIGEST_VARIABLE = "IMAGE_DIGEST"
_METADATA_FILE = "/tmp/skynet-image-metadata.json"
LOCAL_IMAGE_NAME = "skynet-user-image"


class ImageBuildError(RuntimeError):
    """Report a failed image build, with the log the user may see."""

    def __init__(self, message: str, *, log: str = "", log_tail: str = "") -> None:
        """Keep the message and the build log.

        Args:
            message: What failed.
            log: The full build log, when there is one.
            log_tail: The last lines of the log, for the user.
        """
        super().__init__(message)
        self.log = log
        self.log_tail = log_tail


class ImageBuildTimeoutError(ImageBuildError):
    """Report a build that ran longer than the time limit."""


class ImageTooLargeError(ImageBuildError):
    """Report an image larger than the size limit."""


@dataclass(frozen=True)
class BuildRequest:
    """Describe one image build.

    ``dockerfile`` is a path relative to ``context_dir`` and must stay inside
    it. ``cache_scope`` names the owner of the registry build cache (for
    example one user). It has no default, so two owners never share a cache
    by accident.
    """

    context_dir: Path
    cache_scope: str
    dockerfile: str = "Dockerfile"
    build_args: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class BuildResult:
    """Describe a finished image build."""

    image: str
    digest: str
    size_bytes: int
    input_hash: str
    log: str
    log_tail: str


class ImageBuilder(Protocol):
    """Build a Dockerfile with its build context into an image."""

    def build(self, request: BuildRequest) -> BuildResult:
        """Build the image that the request describes.

        Args:
            request: The Dockerfile, the build context and the build args.

        Returns:
            The pinned image, its digest and size, the input hash and the log.

        Raises:
            ImageBuildError: The build failed, ran over the time limit or made
                an image over the size limit.
        """
        ...


@dataclass(frozen=True)
class BuildLimits:
    """Hold the service limits of one build."""

    timeout_seconds: float
    max_size_bytes: int

    @classmethod
    def from_settings(cls) -> BuildLimits:
        """Read the limits from the app settings.

        Returns:
            The configured time and size limits.
        """
        return cls(
            timeout_seconds=settings.image_build_timeout_minutes * 60.0,
            max_size_bytes=int(settings.image_build_max_size_gb * 1_000_000_000),
        )


def log_tail(log: str, lines: int = _LOG_TAIL_LINES) -> str:
    """Return the last lines of a build log.

    Args:
        log: The full log.
        lines: How many lines to keep.

    Returns:
        The last ``lines`` lines.
    """
    return "\n".join(log.rstrip("\n").splitlines()[-lines:])


def _check_request(request: BuildRequest) -> Path:
    """Validate the request and return the Dockerfile path.

    Args:
        request: The build request.

    Returns:
        The absolute Dockerfile path inside the context.

    Raises:
        ImageBuildError: The context or the Dockerfile is missing, the
            Dockerfile is outside the context, or a build arg name is invalid.
    """
    if not request.context_dir.is_dir():
        raise ImageBuildError(f"build context {request.context_dir} is not a directory")
    relative = PurePosixPath(request.dockerfile)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ImageBuildError(f"Dockerfile path {request.dockerfile!r} must stay inside the build context")
    path = request.context_dir.joinpath(*relative.parts)
    if path.is_symlink() or not path.is_file():
        raise ImageBuildError(f"Dockerfile {request.dockerfile!r} is not a regular file in the build context")
    for name in request.build_args:
        if not _BUILD_ARG_NAME.match(name):
            raise ImageBuildError(f"build arg name {name!r} is not valid")
    return path


def _context_entries(context_dir: Path) -> Iterator[tuple[str, Path]]:
    """Yield every file and symlink of the context, sorted by relative path.

    Symlinks are never followed, so a link cannot pull a file from outside the
    context into the hash or the uploaded source.

    Args:
        context_dir: The build context.

    Yields:
        The POSIX relative path and the absolute path of each entry.
    """
    entries: list[tuple[str, Path]] = []
    for root, dirs, files in os.walk(context_dir, followlinks=False):
        base = Path(root)
        for name in dirs + files:
            path = base / name
            if path.is_symlink() or path.is_file():
                entries.append((path.relative_to(context_dir).as_posix(), path))
    yield from sorted(entries)


def compute_input_hash(request: BuildRequest) -> str:
    """Hash the Dockerfile, the build context content and the build args.

    Every field is length-prefixed, so two different inputs never feed the
    same bytes to the hash. The order of the context walk and of the build
    args does not change the result.

    Args:
        request: The build request.

    Returns:
        ``sha256:<64 hex>`` over the build inputs.

    Raises:
        ImageBuildError: The request is invalid.
    """
    dockerfile = _check_request(request)
    digest = hashlib.sha256()

    def put(data: bytes) -> None:
        """Feed one length-prefixed field to the hash.

        Args:
            data: The field bytes.
        """
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)

    put(_HASH_VERSION)
    put(dockerfile.read_bytes())
    for relative, path in _context_entries(request.context_dir):
        put(relative.encode())
        if path.is_symlink():
            put(b"link")
            put(str(path.readlink()).encode())
            continue
        put(b"file")
        digest.update(path.stat().st_size.to_bytes(8, "big"))
        with path.open("rb") as handle:
            while chunk := handle.read(_READ_CHUNK):
                digest.update(chunk)
    for name in sorted(request.build_args):
        put(name.encode())
        put(request.build_args[name].encode())
    return f"sha256:{digest.hexdigest()}"


def _zip_context(context_dir: Path, target: IO[bytes]) -> None:
    """Write the build context as a zip archive, keeping symlinks as links.

    Args:
        context_dir: The build context.
        target: The open binary file to write.
    """
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative, path in _context_entries(context_dir):
            if path.is_symlink():
                info = zipfile.ZipInfo(relative)
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(info, str(path.readlink()))
            else:
                archive.write(path, relative)


def _hex(input_hash: str) -> str:
    """Return the hex part of a ``sha256:`` input hash.

    Args:
        input_hash: The input hash.

    Returns:
        The 64 hex characters.
    """
    return input_hash.split(":", 1)[1]


def codebuild_buildspec(*, image_ref: str, cache_ref: str, dockerfile: str, build_args: Mapping[str, str]) -> str:
    """Return the CodeBuild buildspec that builds and pushes one image.

    The buildspec is JSON, which YAML also reads, so no value needs YAML
    quoting. Every user value goes through ``shlex.quote``. Provenance and
    SBOM attestations stay off, so the pushed digest is a plain image manifest
    whose size ECR reports.

    Args:
        image_ref: The ``repository:tag`` to push.
        cache_ref: The ``repository:tag`` of the registry build cache.
        dockerfile: The Dockerfile path relative to the source root.
        build_args: The build args.

    Returns:
        The buildspec text.
    """
    server = image_ref.split("/", 1)[0]
    build = [
        "docker buildx build",
        "--progress plain",
        "--provenance=false",
        "--sbom=false",
        f"--file {shlex.quote(dockerfile)}",
        f"--tag {shlex.quote(image_ref)}",
        *(f"--build-arg {shlex.quote(f'{k}={v}')}" for k, v in sorted(build_args.items())),
        f"--cache-from {shlex.quote(f'type=registry,ref={cache_ref}')}",
        f"--cache-to {shlex.quote(f'type=registry,ref={cache_ref},mode=max,image-manifest=true,oci-mediatypes=true')}",
        f"--metadata-file {_METADATA_FILE}",
        "--push",
        ".",
    ]
    spec = {
        "version": "0.2",
        "env": {"exported-variables": [_DIGEST_VARIABLE]},
        "phases": {
            "pre_build": {
                "commands": [
                    f'aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin {shlex.quote(server)}',
                    "docker buildx create --use --driver docker-container",
                ]
            },
            "build": {
                "commands": [
                    " ".join(build),
                    f"export {_DIGEST_VARIABLE}=$(python3 -c "
                    + shlex.quote(f"import json; print(json.load(open({_METADATA_FILE!r}))['containerimage.digest'])")
                    + ")",
                ]
            },
        },
    }
    return json.dumps(spec, indent=2)


class CodeBuildImageBuilder:
    """Build user images on AWS CodeBuild and push them to the registry.

    The source is a zip of the build context in an S3 bucket: the builder
    uploads it, starts the CodeBuild project with an inline buildspec, polls
    the build until it ends or the time limit passes, reads the CloudWatch
    log, and checks the pushed digest and its size in the registry. The boto3
    clients are injected, so tests pass fakes.
    """

    def __init__(
        self,
        *,
        codebuild: Any,
        logs: Any,
        s3: Any,
        registry: ImageRegistry,
        project_name: str,
        source_bucket: str,
        source_prefix: str = "image-builds/",
        limits: BuildLimits | None = None,
        poll_seconds: float = 10.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Bind the builder to its clients, project, bucket and limits.

        Args:
            codebuild: A boto3 CodeBuild client.
            logs: A boto3 CloudWatch Logs client.
            s3: A boto3 S3 client.
            registry: The registry the build pushes to.
            project_name: The CodeBuild project name.
            source_bucket: The S3 bucket for the zipped build context.
            source_prefix: The key prefix in that bucket.
            limits: The time and size limits. ``None`` reads the settings.
            poll_seconds: The wait between two build status reads.
            sleep: The wait function (tests pass a fake).
            clock: The monotonic clock (tests pass a fake).
        """
        self._codebuild = codebuild
        self._logs = logs
        self._s3 = s3
        self._registry = registry
        self._project = project_name
        self._bucket = source_bucket
        self._prefix = source_prefix
        self._limits = limits or BuildLimits.from_settings()
        self._poll = poll_seconds
        self._sleep = sleep
        self._clock = clock

    def build(self, request: BuildRequest) -> BuildResult:
        """Build the image on CodeBuild and push it to the registry.

        Args:
            request: The Dockerfile, the build context and the build args.

        Returns:
            The pinned image, its digest and size, the input hash and the log.

        Raises:
            ImageBuildError: The build failed, gave no digest, or the registry
                does not hold the digest.
            ImageBuildTimeoutError: The build ran longer than the time limit.
            ImageTooLargeError: The pushed image is larger than the size limit.
        """
        input_hash = compute_input_hash(request)
        token = uuid.uuid4().hex[:12]
        # A unique suffix keeps two builds of the same input from colliding on
        # an immutable tag.
        try:
            image_ref = self._registry.push_reference(f"{_hex(input_hash)}-{token}")
            cache_ref = self._registry.cache_reference(request.cache_scope)
        except ImageRegistryError as exc:
            raise ImageBuildError(str(exc)) from exc
        key = f"{self._prefix}{_hex(input_hash)}/{token}.zip"
        with tempfile.TemporaryFile() as archive:
            _zip_context(request.context_dir, archive)
            archive.seek(0)
            self._s3.upload_fileobj(archive, self._bucket, key)

        timeout_minutes = max(5, int(self._limits.timeout_seconds // 60))
        started = self._codebuild.start_build(
            projectName=self._project,
            sourceTypeOverride="S3",
            sourceLocationOverride=f"{self._bucket}/{key}",
            buildspecOverride=codebuild_buildspec(
                image_ref=image_ref,
                cache_ref=cache_ref,
                dockerfile=PurePosixPath(request.dockerfile).as_posix(),
                build_args=request.build_args,
            ),
            timeoutInMinutesOverride=timeout_minutes,
        )
        build_id = started["build"]["id"]
        build = self._wait(build_id)
        log = self._read_log(build)
        tail = log_tail(log)
        status = build.get("buildStatus")
        if status == "TIMED_OUT":
            raise ImageBuildTimeoutError(self._timeout_message(), log=log, log_tail=tail)
        if status != "SUCCEEDED":
            raise ImageBuildError(f"image build {build_id} ended with status {status}", log=log, log_tail=tail)

        exported = {v.get("name"): v.get("value") for v in build.get("exportedEnvironmentVariables") or []}
        digest = exported.get(_DIGEST_VARIABLE) or ""
        try:
            image = self._registry.check_digest(digest)
        except ImageRegistryError as exc:
            raise ImageBuildError(
                f"image build {build_id} pushed no usable image: {exc}", log=log, log_tail=tail
            ) from exc
        if image.size_bytes > self._limits.max_size_bytes:
            raise ImageTooLargeError(_size_message(image.size_bytes, self._limits), log=log, log_tail=tail)
        return BuildResult(
            image=image.reference,
            digest=image.digest,
            size_bytes=image.size_bytes,
            input_hash=input_hash,
            log=log,
            log_tail=tail,
        )

    def _timeout_message(self) -> str:
        """Return the error text for a build over the time limit.

        Returns:
            The message with the limit in minutes.
        """
        return f"image build ran longer than the {self._limits.timeout_seconds / 60:g}-minute limit"

    def _wait(self, build_id: str) -> dict[str, Any]:
        """Poll the build until it ends, or stop it at the time limit.

        Args:
            build_id: The CodeBuild build id.

        Returns:
            The final build description.

        Raises:
            ImageBuildTimeoutError: The build was still running at the time limit.
        """
        deadline = self._clock() + self._limits.timeout_seconds
        while True:
            build = self._codebuild.batch_get_builds(ids=[build_id])["builds"][0]
            if build.get("buildComplete"):
                return build
            if self._clock() >= deadline:
                self._codebuild.stop_build(id=build_id)
                log = self._read_log(build)
                raise ImageBuildTimeoutError(self._timeout_message(), log=log, log_tail=log_tail(log))
            self._sleep(self._poll)

    def _read_log(self, build: Mapping[str, Any]) -> str:
        """Read the full CloudWatch log of a build.

        Args:
            build: The build description.

        Returns:
            Every log line, in order. Empty when the build has no log stream yet.
        """
        logs = build.get("logs") or {}
        group, stream = logs.get("groupName"), logs.get("streamName")
        if not group or not stream:
            return ""
        lines: list[str] = []
        kwargs: dict[str, Any] = {"logGroupName": group, "logStreamName": stream, "startFromHead": True}
        while True:
            page = self._logs.get_log_events(**kwargs)
            lines.extend(event["message"].rstrip("\n") for event in page.get("events", []))
            token = page.get("nextForwardToken")
            # CloudWatch returns the same forward token when no page is left.
            if not token or token == kwargs.get("nextToken"):
                return "\n".join(lines)
            kwargs["nextToken"] = token


def _size_message(size_bytes: int, limits: BuildLimits) -> str:
    """Return the error text for an image over the size limit.

    Args:
        size_bytes: The image size.
        limits: The build limits.

    Returns:
        The message with both sizes in GB.
    """
    return f"image is {size_bytes / 1e9:.2f} GB, over the {limits.max_size_bytes / 1e9:g} GB limit"


class LocalDockerImageBuilder:
    """Build user images with the local Docker engine, for local runs.

    The image stays in the local engine and is not pushed. ``image`` and
    ``digest`` are the local image id (``sha256:<64 hex>``), which Docker runs
    directly and which never moves.
    """

    def __init__(self, *, docker: str = "docker", limits: BuildLimits | None = None) -> None:
        """Bind the builder to a Docker CLI and the limits.

        Args:
            docker: The Docker CLI path.
            limits: The time and size limits. ``None`` reads the settings.
        """
        self._docker = docker
        self._limits = limits or BuildLimits.from_settings()

    def build(self, request: BuildRequest) -> BuildResult:
        """Build the image with ``docker build``.

        Args:
            request: The Dockerfile, the build context and the build args.

        Returns:
            The local image id, its size, the input hash and the log.

        Raises:
            ImageBuildError: Docker failed or reported no image id.
            ImageBuildTimeoutError: The build ran longer than the time limit.
            ImageTooLargeError: The image is larger than the size limit.
        """
        dockerfile = _check_request(request)
        input_hash = compute_input_hash(request)
        with tempfile.TemporaryDirectory() as scratch:
            iidfile = Path(scratch) / "iid"
            command = [
                self._docker,
                "build",
                "--progress=plain",
                "--file",
                str(dockerfile),
                "--iidfile",
                str(iidfile),
                "--tag",
                f"{LOCAL_IMAGE_NAME}:{_hex(input_hash)[:32]}",
                *(arg for k, v in sorted(request.build_args.items()) for arg in ("--build-arg", f"{k}={v}")),
                str(request.context_dir),
            ]
            try:
                done = subprocess.run(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=self._limits.timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                log = (exc.stdout or b"").decode(errors="replace")
                raise ImageBuildTimeoutError(
                    f"image build ran longer than the {self._limits.timeout_seconds / 60:g}-minute limit",
                    log=log,
                    log_tail=log_tail(log),
                ) from exc
            log = done.stdout.decode(errors="replace")
            tail = log_tail(log)
            image_id = iidfile.read_text().strip() if iidfile.exists() else ""
        if done.returncode != 0 or not image_id:
            raise ImageBuildError(f"docker build exited with code {done.returncode}", log=log, log_tail=tail)
        size_bytes = self._image_size(image_id)
        if size_bytes > self._limits.max_size_bytes:
            raise ImageTooLargeError(_size_message(size_bytes, self._limits), log=log, log_tail=tail)
        return BuildResult(
            image=image_id,
            digest=image_id,
            size_bytes=size_bytes,
            input_hash=input_hash,
            log=log,
            log_tail=tail,
        )

    def _image_size(self, image_id: str) -> int:
        """Return the size that Docker reports for a local image.

        Args:
            image_id: The local image id.

        Returns:
            The size in bytes.

        Raises:
            ImageBuildError: Docker cannot inspect the image.
        """
        done = subprocess.run(
            [self._docker, "image", "inspect", "--format", "{{.Size}}", image_id],
            capture_output=True,
            text=True,
            check=False,
        )
        if done.returncode != 0:
            raise ImageBuildError(f"docker image inspect failed: {done.stderr.strip()}")
        return int(done.stdout.strip())
