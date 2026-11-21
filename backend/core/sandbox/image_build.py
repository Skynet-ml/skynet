"""Builder interface for user images, with AWS CodeBuild and local Docker builders (SKY-888).

A power user can bring a Dockerfile for the candidate box or the metric box
(SKY-737). A builder builds it with its build context and returns the image
digest, the image size, an input hash, the build log and a short tail of
that log for the user. A cloud run builds on CodeBuild and pushes to the
owner's registry repository; a local run builds with the local Docker engine.

The input hash covers the builder kind and platform, the Dockerfile path and
bytes, every path, mode and byte in the build context, and the build args, so
a later part can reuse a finished image when the hash matches.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import math
import os
import re
import shlex
import signal
import stat
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import IO, Any, Protocol

from core.config import settings

from .image_registry import EcrRegistry, ImageRegistryError

try:
    import boto3
except ImportError:
    boto3 = None

try:
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError:
    BotoCoreError = None
    ClientError = None

# Without botocore the names above are None, and except takes no None entry.
_AWS_ERRORS = tuple(kind for kind in (BotoCoreError, ClientError) if kind is not None)

logger = logging.getLogger(__name__)

_HASH_VERSION = b"skynet-image-input-v2"
_BUILD_ARG_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LOG_TAIL_LINES = 40
# Keeps a runaway build log from filling the worker's memory or the database.
MAX_LOG_CHARS = 2_000_000
_READ_CHUNK = 1024 * 1024
_DIGEST_VARIABLE = "IMAGE_DIGEST"
_METADATA_FILE = "/tmp/skynet-image-metadata.json"
# CodeBuild accepts 5 to 2160 minutes; the settings allow at most 480.
_MIN_TIMEOUT_MINUTES = 5
MAX_TIMEOUT_MINUTES = 480
_QUEUE_TIMEOUT_MINUTES = 60
_QUEUED_PHASES = frozenset({"SUBMITTED", "QUEUED"})
# CloudWatch can deliver the last log events a few seconds after the build ends.
_LOG_SETTLE_SECONDS = 5.0
# CodeBuild writes its own agent lines with this prefix, and marks each phase.
_CODEBUILD_LINE = "[Container] "
# Anchored, so a command line that holds the words (in a build arg) is no marker.
_CODEBUILD_PHASE = re.compile(r"\[Container\] \S+ \S+ Entering phase (\w+)")
# The buildspec is JSON read as YAML, with non-ASCII text kept as is. YAML
# refuses raw control characters and noncharacters, and reads U+2028/U+2029
# as line breaks, so user text that goes into it must not hold them.
_UNSAFE_TEXT = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029\ufeff\ufffe\uffff]")
# The user log keeps only the build phase: the other phases run Skynet's own
# setup (firewall rules, the credential helper with its role and policy).
_USER_LOG_PHASE = "BUILD"
# After a kill, the reader thread ends when the pipe closes.
_READER_JOIN_SECONDS = 5.0
LOCAL_IMAGE_NAME = "skynet-user-image"
CODEBUILD_PLATFORM = "linux/amd64"
# BuildKit from the ECR Public mirror of moby/buildkit:buildx-stable-1, pinned
# by index digest, so no build pulls an unpinned image from Docker Hub.
BUILDKIT_IMAGE = (
    "public.ecr.aws/vend/moby/buildkit:buildx-stable-1"
    "@sha256:c5f8862f52499773af85bc07695b2379ef08e8002352b0df2c4864c91d78dabe"
)
_CREDENTIAL_HELPER = "skynet-ecr"
_CREDENTIAL_HELPER_PATH = f"/usr/local/bin/docker-credential-{_CREDENTIAL_HELPER}"
# A Docker credential helper that runs in the CodeBuild container. Each call
# assumes the push role again with the owner's session policy, so BuildKit
# gets fresh credentials past the one-hour limit of a chained role session,
# and those credentials reach only the owner's repository.
_HELPER_SOURCE = """#!/usr/bin/env python3
import json, os, subprocess, sys

ROLE, POLICY, SERVER = {role!r}, {policy!r}, {server!r}

if sys.argv[1:] != ["get"]:
    sys.exit(0)
if sys.stdin.read().strip().removeprefix("https://").rstrip("/") != SERVER:
    print("credentials not found in native keychain")
    sys.exit(1)
creds = json.loads(subprocess.check_output([
    "aws", "sts", "assume-role", "--role-arn", ROLE, "--role-session-name", "skynet-image-build",
    "--policy", POLICY, "--duration-seconds", "3600", "--output", "json",
]))["Credentials"]
env = dict(
    os.environ,
    AWS_ACCESS_KEY_ID=creds["AccessKeyId"],
    AWS_SECRET_ACCESS_KEY=creds["SecretAccessKey"],
    AWS_SESSION_TOKEN=creds["SessionToken"],
)
secret = subprocess.check_output(
    ["aws", "ecr", "get-login-password", "--region", os.environ["AWS_REGION"]], env=env, text=True
).strip()
print(json.dumps({{"ServerURL": SERVER, "Username": "AWS", "Secret": secret}}))
"""


class ImageBuildError(RuntimeError):
    """Report a failed image build, with the log the user may see."""

    def __init__(self, message: str, *, log: str = "", log_tail: str = "") -> None:
        """Keep the message and the build log.

        Args:
            message: What failed.
            log: The build log, when there is one.
            log_tail: The last lines of the log, for the user.
        """
        super().__init__(message)
        self.log = log
        self.log_tail = log_tail


class ImageBuildTimeoutError(ImageBuildError):
    """Report a build that ran or waited longer than its time limit."""


class ImageTooLargeError(ImageBuildError):
    """Report an image larger than the size limit."""


# Every build of one owner shares this cache. It is in the owner's repository, so account deletion
# removes it with the repository (SKY-891).
DEFAULT_CACHE_SCOPE = "default"


@dataclass(frozen=True)
class BuildRequest:
    """Describe one image build.

    ``dockerfile`` is a path relative to ``context_dir`` and must stay inside
    it, with no symlink in any part. ``owner`` names the registry repository
    that holds the image and its build cache (for example one user). It has
    no default, so two owners never share a repository by accident.
    ``cache_scope`` names a build cache inside that repository.

    Build args never carry secrets: they go into the input hash, the build
    log, the image history and the CodeBuild build record. A secret needs a
    BuildKit ``--secret`` mount, which this builder does not offer yet.
    """

    context_dir: Path
    owner: str
    dockerfile: str = "Dockerfile"
    build_args: Mapping[str, str] = field(default_factory=dict)
    cache_scope: str = DEFAULT_CACHE_SCOPE


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
    max_context_bytes: int

    def __post_init__(self) -> None:
        """Refuse a limit that CodeBuild or the settings do not allow.

        Raises:
            ValueError: A limit is not positive, or the time limit is over 480 minutes.
        """
        if not 0 < self.timeout_seconds <= MAX_TIMEOUT_MINUTES * 60:
            raise ValueError(f"build time limit must be over 0 and at most {MAX_TIMEOUT_MINUTES} minutes")
        if self.max_size_bytes <= 0 or self.max_context_bytes <= 0:
            raise ValueError("build size limits must be over 0")

    @classmethod
    def from_settings(cls) -> BuildLimits:
        """Read the limits from the app settings.

        Returns:
            The configured time, image size and context size limits.
        """
        return cls(
            timeout_seconds=settings.image_build_timeout_minutes * 60.0,
            max_size_bytes=int(settings.image_build_max_size_gb * 1_000_000_000),
            max_context_bytes=settings.image_build_max_context_mb * 1_000_000,
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


class _CappedLog:
    """Collect log lines, keeping the head and the tail inside a size limit."""

    def __init__(self, limit: int = MAX_LOG_CHARS) -> None:
        """Start an empty log.

        Args:
            limit: The most characters to keep, split between head and tail.
        """
        self._half = max(1, limit // 2)
        self._head: list[str] = []
        self._head_size = 0
        self._tail: deque[str] = deque()
        self._tail_size = 0
        self._cut = 0
        # The local builder adds lines from a reader thread.
        self._lock = threading.Lock()

    def add(self, line: str) -> None:
        """Add one line, cutting lines from the middle when the log is full.

        Args:
            line: The line, with no newline.
        """
        line = line[: self._half]
        with self._lock:
            self._add(line)

    def _add(self, line: str) -> None:
        """Add one cut line, with the lock held.

        Args:
            line: The line, at most half the limit long.
        """
        if not self._tail and self._head_size + len(line) + 1 <= self._half:
            self._head.append(line)
            self._head_size += len(line) + 1
            return
        self._tail.append(line)
        self._tail_size += len(line) + 1
        while self._tail_size > self._half:
            self._tail_size -= len(self._tail.popleft()) + 1
            self._cut += 1

    def text(self) -> str:
        """Return the kept log, with a marker where lines were cut.

        Returns:
            The head, the cut marker when lines were cut, and the tail.
        """
        with self._lock:
            marker = [f"[... {self._cut} log lines cut ...]"] if self._cut else []
            return "\n".join([*self._head, *marker, *self._tail])


@dataclass(frozen=True)
class _Dockerfile:
    """Hold the checked Dockerfile path of a request."""

    path: Path
    relative: str


def _check_request(request: BuildRequest) -> _Dockerfile:
    """Validate the request and return the checked Dockerfile path.

    Args:
        request: The build request.

    Returns:
        The resolved Dockerfile path inside the context, and its normalized
        POSIX path relative to the context.

    Raises:
        ImageBuildError: The context or the Dockerfile is missing, any part
            of the Dockerfile path is a symlink, the Dockerfile is outside
            the context, a build arg name is invalid, or the Dockerfile path or
            a build arg value is not valid UTF-8 or holds a control character
            or a character the buildspec cannot carry.
    """
    if not request.context_dir.is_dir():
        raise ImageBuildError(f"build context {request.context_dir} is not a directory")
    context = request.context_dir.resolve(strict=True)
    # Check text before any file call, which raises a raw error on a lone surrogate.
    _check_text(request.dockerfile, "Dockerfile path")
    for name, value in request.build_args.items():
        _check_text(value, f"build arg {name} value")
    relative = PurePosixPath(request.dockerfile)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ImageBuildError(f"Dockerfile path {request.dockerfile!r} must stay inside the build context")
    # buildx reads "--file -" as stdin, and a leading "-" reads as an option.
    if relative.parts[0].startswith("-"):
        raise ImageBuildError(f"Dockerfile path {request.dockerfile!r} must not start with '-'")
    path = context
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise ImageBuildError(f"Dockerfile path {request.dockerfile!r} must not pass through a symlink")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ImageBuildError(f"Dockerfile {request.dockerfile!r} is not in the build context") from exc
    if not resolved.is_relative_to(context) or not resolved.is_file():
        raise ImageBuildError(f"Dockerfile {request.dockerfile!r} is not a regular file in the build context")
    for name in request.build_args:
        if not _BUILD_ARG_NAME.fullmatch(name):
            raise ImageBuildError(f"build arg name {name!r} is not valid")
    return _Dockerfile(path=resolved, relative=relative.as_posix())


@dataclass(frozen=True)
class _Entry:
    """Describe one path of the build context."""

    relative: str
    path: Path
    info: os.stat_result


def _walk_error(exc: OSError) -> None:
    """Fail the build when the context walk cannot read a folder.

    Args:
        exc: The error from ``os.walk``.

    Raises:
        ImageBuildError: Always.
    """
    raise ImageBuildError(f"cannot read the build context: {exc}") from exc


def _utf8(text: str, what: str) -> bytes:
    """Encode a build input as UTF-8.

    Args:
        text: The input, for example a path as ``os`` decoded it.
        what: What the text is, for the error.

    Returns:
        The UTF-8 bytes.

    Raises:
        ImageBuildError: The text is not valid UTF-8 (it holds a lone surrogate).
    """
    try:
        return text.encode()
    except UnicodeEncodeError as exc:
        raise ImageBuildError(f"{what} {text!r} is not valid UTF-8") from exc


def _check_text(text: str, what: str) -> None:
    """Refuse text that the buildspec or the docker command cannot carry.

    Args:
        text: The Dockerfile path or a build arg value.
        what: What the text is, for the error.

    Raises:
        ImageBuildError: The text is not valid UTF-8, or holds a control
            character (NUL too) or a character YAML does not accept.
    """
    _utf8(text, what)
    if _UNSAFE_TEXT.search(text):
        raise ImageBuildError(f"{what} {text!r} holds a control character or a character the buildspec cannot carry")


def _context_entries(context_dir: Path, max_bytes: int) -> list[_Entry]:
    """Return every folder, file and symlink of the context, sorted by path.

    Symlinks are never followed, so a link cannot pull a file from outside
    the context into the hash or the uploaded source. Folders are entries,
    so an empty folder is part of the hash and the upload.

    Args:
        context_dir: The build context.
        max_bytes: The most file bytes the context may hold.

    Returns:
        The entries, sorted by POSIX relative path.

    Raises:
        ImageBuildError: A folder cannot be read, a name is not valid UTF-8,
            or the files are larger than ``max_bytes``.
    """
    root_dir = context_dir.resolve(strict=True)
    entries: list[_Entry] = []
    total = 0
    for root, dirs, files in os.walk(root_dir, onerror=_walk_error, followlinks=False):
        base = Path(root)
        for name in dirs + files:
            path = base / name
            relative = path.relative_to(root_dir).as_posix()
            _utf8(relative, "build context path")
            try:
                info = path.lstat()
            except OSError as exc:
                raise ImageBuildError(f"cannot read the build context: {exc}") from exc
            if not (stat.S_ISLNK(info.st_mode) or stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                continue
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
                if total > max_bytes:
                    raise ImageBuildError(f"build context is larger than the {max_bytes / 1e6:g} MB limit")
            entries.append(_Entry(relative=relative, path=path, info=info))
    return sorted(entries, key=lambda entry: entry.relative)


def compute_input_hash(
    request: BuildRequest,
    *,
    builder: str,
    platform: str,
    max_context_bytes: int | None = None,
) -> str:
    """Hash the builder, the Dockerfile, the build context and the build args.

    Every field is length-prefixed, so two different inputs never feed the
    same bytes to the hash. The order of the context walk and of the build
    args does not change the result. The hash covers the Dockerfile path,
    because ``<Dockerfile>.dockerignore`` changes the context, and the file
    and folder modes, because ``COPY`` keeps them.

    Args:
        request: The build request.
        builder: The builder kind, for example ``codebuild``.
        platform: The target platform, or ``""`` for the engine default.
        max_context_bytes: The context size limit. ``None`` reads the settings.

    Returns:
        ``sha256:<64 hex>`` over the build inputs.

    Raises:
        ImageBuildError: The request is invalid, or the context is too large
            or cannot be read.
    """
    dockerfile = _check_request(request)
    if max_context_bytes is None:
        max_context_bytes = BuildLimits.from_settings().max_context_bytes
    try:
        return _hash_inputs(
            request, dockerfile, builder=builder, platform=platform, max_context_bytes=max_context_bytes
        )
    except OSError as exc:
        raise ImageBuildError(f"cannot read the build context: {exc}") from exc


def _hash_inputs(
    request: BuildRequest,
    dockerfile: _Dockerfile,
    *,
    builder: str,
    platform: str,
    max_context_bytes: int,
) -> str:
    """Hash the checked build inputs.

    Args:
        request: The build request.
        dockerfile: The checked Dockerfile path.
        builder: The builder kind.
        platform: The target platform.
        max_context_bytes: The context size limit.

    Returns:
        ``sha256:<64 hex>`` over the build inputs.

    Raises:
        OSError: A context file cannot be read.
    """
    digest = hashlib.sha256()

    def put(data: bytes) -> None:
        """Feed one length-prefixed field to the hash.

        Args:
            data: The field bytes.
        """
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)

    put(_HASH_VERSION)
    put(builder.encode())
    put(platform.encode())
    put(_utf8(dockerfile.relative, "Dockerfile path"))
    put(dockerfile.path.read_bytes())
    for entry in _context_entries(request.context_dir, max_context_bytes):
        put(entry.relative.encode())
        mode = entry.info.st_mode
        if stat.S_ISLNK(mode):
            put(b"link")
            put(_utf8(str(entry.path.readlink()), "build context symlink target"))
            continue
        put(b"dir" if stat.S_ISDIR(mode) else b"file")
        put((mode & 0o7777).to_bytes(2, "big"))
        if stat.S_ISDIR(mode):
            continue
        digest.update(entry.info.st_size.to_bytes(8, "big"))
        with entry.path.open("rb") as handle:
            while chunk := handle.read(_READ_CHUNK):
                digest.update(chunk)
    for name in sorted(request.build_args):
        put(name.encode())
        put(_utf8(request.build_args[name], f"build arg {name} value"))
    return f"sha256:{digest.hexdigest()}"


def _zip_context(context_dir: Path, target: IO[bytes], max_bytes: int) -> None:
    """Write the build context as a zip archive, keeping symlinks as links.

    Args:
        context_dir: The build context.
        target: The open binary file to write.
        max_bytes: The most file bytes the context may hold.

    Raises:
        ImageBuildError: The context cannot be read or is too large.
    """
    # strict_timestamps=False stores a pre-1980 mtime as 1980 instead of raising.
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, strict_timestamps=False) as archive:
        for entry in _context_entries(context_dir, max_bytes):
            try:
                if stat.S_ISLNK(entry.info.st_mode):
                    info = zipfile.ZipInfo(entry.relative)
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                    archive.writestr(info, str(entry.path.readlink()))
                else:
                    archive.write(entry.path, entry.relative)
            except OSError as exc:
                raise ImageBuildError(f"cannot read the build context: {exc}") from exc


def _hex(input_hash: str) -> str:
    """Return the hex part of a ``sha256:`` input hash.

    Args:
        input_hash: The input hash.

    Returns:
        The 64 hex characters.
    """
    return input_hash.split(":", 1)[1]


def codebuild_buildspec(
    *,
    image_ref: str,
    cache_ref: str,
    dockerfile: str,
    build_args: Mapping[str, str],
    push_role_arn: str,
    session_policy: Mapping[str, Any],
) -> str:
    """Return the CodeBuild buildspec that builds and pushes one image.

    The buildspec is JSON, which YAML also reads, so no value needs YAML
    quoting. Non-ASCII text stays as is, because YAML refuses the surrogate
    pair escapes that JSON writes for characters above U+FFFF. Every user
    value goes through ``shlex.quote``. Provenance and SBOM attestations stay
    off, so the pushed digest is a plain image manifest whose size ECR reports.

    Before BuildKit starts, the buildspec blocks the link-local range for
    containers, so a ``RUN`` step cannot reach the CodeBuild credentials or
    metadata endpoints. Only the Amazon DNS resolver stays open. The registry
    credentials come from a credential helper limited to the owner's
    repository, so the build cannot read or write another owner's images.

    Args:
        image_ref: The ``repository:tag`` to push.
        cache_ref: The ``repository:tag`` of the registry build cache.
        dockerfile: The Dockerfile path relative to the source root.
        build_args: The build args.
        push_role_arn: The IAM role that the credential helper assumes.
        session_policy: The session policy that limits it to the owner.

    Returns:
        The buildspec text.
    """
    server = image_ref.split("/", 1)[0]
    helper = _HELPER_SOURCE.format(
        role=push_role_arn,
        policy=json.dumps(session_policy, separators=(",", ":")),
        server=server,
    )
    docker_config = json.dumps({"credHelpers": {server: _CREDENTIAL_HELPER}})
    build = [
        "docker buildx build",
        "--progress plain",
        f"--platform {CODEBUILD_PLATFORM}",
        "--provenance=false",
        "--sbom=false",
        f"--file {shlex.quote('./' + dockerfile)}",
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
                    # -I puts each rule first, so the DNS rules end above the drop.
                    "iptables -I DOCKER-USER -d 169.254.0.0/16 -j DROP",
                    "iptables -I DOCKER-USER -d 169.254.169.253/32 -p udp --dport 53 -j ACCEPT",
                    "iptables -I DOCKER-USER -d 169.254.169.253/32 -p tcp --dport 53 -j ACCEPT",
                    # DOCKER-USER sees only forwarded traffic. BuildKit runs on the
                    # default bridge, and its traffic to the build host's own
                    # addresses (172.17.0.1, or an endpoint the host DNATs to
                    # itself) goes through INPUT. buildx reaches buildkitd through
                    # docker exec, so the build needs only DNS from the host.
                    # Replies to host-started connections are not NEW, so they pass.
                    "iptables -I INPUT -i docker0 -m conntrack --ctstate NEW -j DROP",
                    "iptables -I INPUT -i docker0 -p udp --dport 53 -j ACCEPT",
                    "iptables -I INPUT -i docker0 -p tcp --dport 53 -j ACCEPT",
                    f"echo {base64.b64encode(helper.encode()).decode()} | base64 -d > {_CREDENTIAL_HELPER_PATH}",
                    f"chmod 755 {_CREDENTIAL_HELPER_PATH}",
                    f"mkdir -p ~/.docker && echo {shlex.quote(docker_config)} > ~/.docker/config.json",
                    f"docker buildx create --use --driver docker-container --driver-opt image={BUILDKIT_IMAGE}",
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
    return json.dumps(spec, indent=2, ensure_ascii=False)


class CodeBuildImageBuilder:
    """Build user images on AWS CodeBuild and push them to the owner's repository.

    The source is a zip of the build context in an S3 bucket: the builder
    uploads it, starts the CodeBuild project with an inline buildspec, polls
    the build until it ends or a time limit passes, reads the CloudWatch log,
    and checks the pushed digest and its size in the registry. The boto3
    clients are injected, so tests pass fakes.
    """

    kind = "codebuild"

    def __init__(
        self,
        *,
        codebuild: Any,
        logs: Any,
        s3: Any,
        registry: EcrRegistry,
        project_name: str,
        source_bucket: str,
        push_role_arn: str,
        source_prefix: str = "image-builds/",
        limits: BuildLimits | None = None,
        poll_seconds: float = 10.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Bind the builder to its clients, project, bucket, role and limits.

        Args:
            codebuild: A boto3 CodeBuild client.
            logs: A boto3 CloudWatch Logs client.
            s3: A boto3 S3 client.
            registry: The registry the build pushes to.
            project_name: The CodeBuild project name.
            source_bucket: The S3 bucket for the zipped build context.
            push_role_arn: The role a build assumes, with an owner session
                policy, to push to the owner's repository.
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
        self._push_role = push_role_arn
        self._prefix = source_prefix
        self._limits = limits or BuildLimits.from_settings()
        self._poll = poll_seconds
        self._sleep = sleep
        self._clock = clock

    def build(self, request: BuildRequest) -> BuildResult:
        """Build the image on CodeBuild and push it to the owner's repository.

        Args:
            request: The Dockerfile, the build context and the build args.

        Returns:
            The pinned image, its digest and size, the input hash and the log.

        Raises:
            ImageBuildError: The request or the context is invalid, the build
                failed, gave no digest, or the registry does not hold it.
            ImageBuildTimeoutError: The build ran longer than the time limit,
                or waited longer than the queue limit.
            ImageTooLargeError: The pushed image is larger than the size limit.
        """
        dockerfile = _check_request(request)
        input_hash = compute_input_hash(
            request,
            builder=self.kind,
            platform=CODEBUILD_PLATFORM,
            max_context_bytes=self._limits.max_context_bytes,
        )
        token = uuid.uuid4().hex[:12]
        # A unique suffix keeps two builds of the same input from colliding on
        # an immutable tag.
        try:
            image_ref = self._registry.push_reference(request.owner, f"{_hex(input_hash)}-{token}")
            cache_ref = self._registry.cache_reference(request.owner, request.cache_scope)
            session_policy = self._registry.push_session_policy(request.owner)
        except ImageRegistryError as exc:
            raise ImageBuildError(str(exc)) from exc
        key = f"{self._prefix}{request.owner}/{_hex(input_hash)}/{token}.zip"
        with tempfile.TemporaryFile() as archive:
            _zip_context(request.context_dir, archive, self._limits.max_context_bytes)
            archive.seek(0)
            try:
                self._s3.upload_fileobj(archive, self._bucket, key)
            except _AWS_ERRORS as exc:
                raise ImageBuildError(f"cannot upload the build context ({_aws_error(exc)})") from exc

        timeout_minutes = max(_MIN_TIMEOUT_MINUTES, math.ceil(self._limits.timeout_seconds / 60))
        try:
            started = self._codebuild.start_build(
                projectName=self._project,
                sourceTypeOverride="S3",
                sourceLocationOverride=f"{self._bucket}/{key}",
                buildspecOverride=codebuild_buildspec(
                    image_ref=image_ref,
                    cache_ref=cache_ref,
                    dockerfile=dockerfile.relative,
                    build_args=request.build_args,
                    push_role_arn=self._push_role,
                    session_policy=session_policy,
                ),
                timeoutInMinutesOverride=timeout_minutes,
                queuedTimeoutInMinutesOverride=_QUEUE_TIMEOUT_MINUTES,
            )
        except _AWS_ERRORS as exc:
            raise ImageBuildError(f"cannot start the image build ({_aws_error(exc)})") from exc
        build_id = started["build"]["id"]
        # A build the worker cannot follow must not run on and push, so every
        # failure here stops it. _wait stops the build before its own errors.
        try:
            build = self._wait(build_id)
            log = self._read_log(build, settle=True)
        except ImageBuildError:
            raise
        except Exception as exc:
            self._stop(build_id)
            raise ImageBuildError(f"lost track of image build {build_id} ({_aws_error(exc)})") from exc
        except BaseException:
            self._stop(build_id)
            raise
        tail = log_tail(log)
        status = build.get("buildStatus")
        if status == "TIMED_OUT":
            raise ImageBuildTimeoutError(self._timeout_message(), log=log, log_tail=tail)
        if status != "SUCCEEDED":
            raise ImageBuildError(f"image build {build_id} ended with status {status}", log=log, log_tail=tail)

        exported = {v.get("name"): v.get("value") for v in build.get("exportedEnvironmentVariables") or []}
        digest = exported.get(_DIGEST_VARIABLE) or ""
        try:
            image = self._registry.check_digest(request.owner, digest)
        except ImageRegistryError as exc:
            raise ImageBuildError(
                f"image build {build_id} pushed no usable image: {exc}", log=log, log_tail=tail
            ) from exc
        if image.size_bytes > self._limits.max_size_bytes:
            message = _size_message(image.size_bytes, self._limits)
            try:
                self._registry.delete_image(request.owner, image.digest)
            except ImageRegistryError as exc:
                message += f"; the image stays in the registry until its lifecycle rule deletes it ({exc})"
            # mode=max exports every layer to the cache before the size check,
            # so the cache would keep the over-size layers for 14 days.
            try:
                self._registry.delete_cache(request.owner, request.cache_scope)
            except ImageRegistryError as exc:
                message += f"; the build cache stays in the registry until its lifecycle rule deletes it ({exc})"
            raise ImageTooLargeError(message, log=log, log_tail=tail)
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
        """Poll the build until it ends, or stop it at a time limit.

        The time limit counts from the first poll that sees the build out of
        the queue. Queue time has its own limit.

        Args:
            build_id: The CodeBuild build id.

        Returns:
            The final build description.

        Raises:
            ImageBuildTimeoutError: The build was still running at the time
                limit, or still queued at the queue limit.
            ImageBuildError: CodeBuild does not know the build.
            BotoCoreError: A status read failed (or ClientError).
        """
        queue_deadline = self._clock() + _QUEUE_TIMEOUT_MINUTES * 60
        run_deadline: float | None = None
        while True:
            builds = self._codebuild.batch_get_builds(ids=[build_id]).get("builds") or []
            if not builds:
                self._stop(build_id)
                raise ImageBuildError(f"CodeBuild does not know image build {build_id}")
            build = builds[0]
            if build.get("buildComplete"):
                return build
            now = self._clock()
            if run_deadline is None and build.get("currentPhase") not in _QUEUED_PHASES:
                run_deadline = now + self._limits.timeout_seconds
            if run_deadline is None and now >= queue_deadline:
                self._stop(build_id)
                raise ImageBuildTimeoutError(
                    f"image build waited longer than the {_QUEUE_TIMEOUT_MINUTES}-minute queue limit"
                )
            if run_deadline is not None and now >= run_deadline:
                self._stop(build_id)
                log = ""
                # A failed log read must not hide the timeout.
                with contextlib.suppress(*_AWS_ERRORS):
                    log = self._read_log(build)
                raise ImageBuildTimeoutError(self._timeout_message(), log=log, log_tail=log_tail(log))
            self._sleep(self._poll)

    def _stop(self, build_id: str) -> None:
        """Stop a build, best effort.

        Args:
            build_id: The CodeBuild build id.
        """
        # The caller already reports a failure; a failed stop must not hide it.
        with contextlib.suppress(*_AWS_ERRORS):
            self._codebuild.stop_build(id=build_id)

    def _user_line(self, line: str) -> str:
        """Hide the push role, the registry host and the account id in a log line.

        Args:
            line: One build-phase log line.

        Returns:
            The line with those values replaced by placeholders.
        """
        server = self._registry.server
        account = server.split(".", 1)[0]
        line = line.replace(self._push_role, "<push-role>").replace(server, "<registry>")
        return line.replace(account, "<account>") if account.isdigit() else line

    def _read_log(self, build: Mapping[str, Any], *, settle: bool = False) -> str:
        """Read the build-phase output from the CloudWatch log, inside the log size limit.

        The user log drops CodeBuild's own lines (which echo each command
        Skynet runs) and the output of every phase but the build phase, and
        hides the push role, the registry host and the account id.

        Args:
            build: The build description.
            settle: Wait a short time at the end and read once more, for
                events CloudWatch delivers after the build ends.

        Returns:
            The log lines in order, with the middle cut when the log is too
            long. Empty when the build has no log stream yet.

        Raises:
            BotoCoreError: A log read failed (or ClientError).
        """
        logs = build.get("logs") or {}
        group, stream = logs.get("groupName"), logs.get("streamName")
        if not group or not stream:
            return ""
        capped = _CappedLog()
        phase = ""
        kwargs: dict[str, Any] = {"logGroupName": group, "logStreamName": stream, "startFromHead": True}
        while True:
            page = self._logs.get_log_events(**kwargs)
            for event in page.get("events", []):
                line = event["message"].rstrip("\n")
                if line.startswith(_CODEBUILD_LINE):
                    if found := _CODEBUILD_PHASE.fullmatch(line):
                        phase = found.group(1)
                elif phase == _USER_LOG_PHASE:
                    capped.add(self._user_line(line))
            token = page.get("nextForwardToken")
            # CloudWatch returns the same forward token when no page is left.
            if token and token != kwargs.get("nextToken"):
                kwargs["nextToken"] = token
                continue
            if not settle or not token:
                return capped.text()
            settle = False
            self._sleep(_LOG_SETTLE_SECONDS)


def codebuild_builder(registry: EcrRegistry | None, config: Any = settings) -> CodeBuildImageBuilder | None:
    """Return the CodeBuild builder of this deployment, or ``None`` when it is not set up (SKY-891).

    Args:
        registry: The user image registry the builds push to, or ``None``.
        config: The app settings.

    Returns:
        A builder with the worker's AWS credentials in the registry's Region
        when the registry, the project, the source bucket and the push role
        are all set; ``None`` otherwise.

    Raises:
        ImageBuildError: The settings are set but boto3 is missing.
    """
    project = config.image_build_project
    bucket = config.image_build_source_bucket
    push_role = config.image_build_push_role_arn
    if registry is None or not project or not bucket or not push_role:
        return None
    if boto3 is None:
        raise ImageBuildError("the CodeBuild image builder needs boto3")
    region = registry.region
    return CodeBuildImageBuilder(
        codebuild=boto3.client("codebuild", region_name=region),
        logs=boto3.client("logs", region_name=region),
        s3=boto3.client("s3", region_name=region),
        registry=registry,
        project_name=project,
        source_bucket=bucket,
        push_role_arn=push_role,
    )


def _aws_error(exc: BaseException) -> str:
    """Return a short, safe name for a boto3 or botocore error.

    Args:
        exc: The error.

    Returns:
        The AWS error code of a client error, else the error class name.
    """
    response = getattr(exc, "response", None)
    code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
    return code or type(exc).__name__


def _read_capped(stream: IO[bytes], capped: _CappedLog) -> None:
    """Read a process output stream into a capped log, a bounded piece at a time.

    Args:
        stream: The binary output stream.
        capped: The log that keeps the head and the tail.
    """
    while piece := stream.readline(_READ_CHUNK):
        capped.add(piece.decode(errors="replace").rstrip("\n"))


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    """Kill every process in docker's process group.

    An empty group raises ProcessLookupError. macOS raises PermissionError
    when the group holds only zombies. Both mean that nothing is left to kill.

    Args:
        process: The docker process, which leads its own process group.
    """
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)


def _release_output(process: subprocess.Popen[bytes], reader: threading.Thread) -> None:
    """Join the output reader for a bounded time, and detach the pipe if it still reads.

    The builder kills docker's group before this call on every path, so only
    a process outside the group (one that called setsid) can still hold the
    pipe. Closing the pipe would then block until that process exits, so the
    builder leaves the pipe to the daemon reader, which ends at EOF.

    Args:
        process: The docker process, already waited for.
        reader: The thread that reads its output.
    """
    reader.join(_READER_JOIN_SECONDS)
    if reader.is_alive():
        logger.warning("docker process %s left a process that holds its output open; the log may be short", process.pid)
        process.stdout = None


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

    kind = "local-docker"

    def __init__(
        self,
        *,
        docker: str = "docker",
        limits: BuildLimits | None = None,
        platform: str | None = None,
    ) -> None:
        """Bind the builder to a Docker CLI, the limits and a platform.

        Args:
            docker: The Docker CLI path.
            limits: The time and size limits. ``None`` reads the settings.
            platform: The target platform, for example ``linux/amd64``.
                ``None`` uses the engine default.
        """
        self._docker = docker
        self._limits = limits or BuildLimits.from_settings()
        self._platform = platform

    def build(self, request: BuildRequest) -> BuildResult:
        """Build the image with ``docker build``.

        Args:
            request: The Dockerfile, the build context and the build args.

        Returns:
            The local image id, its size, the input hash and the log.

        Raises:
            ImageBuildError: The request or the context is invalid, or Docker
                failed or reported no image id.
            ImageBuildTimeoutError: The build ran longer than the time limit.
            ImageTooLargeError: The image is larger than the size limit.
        """
        dockerfile = _check_request(request)
        input_hash = compute_input_hash(
            request,
            builder=self.kind,
            platform=self._platform or "",
            max_context_bytes=self._limits.max_context_bytes,
        )
        with tempfile.TemporaryDirectory() as scratch:
            iidfile = Path(scratch) / "iid"
            command = [
                self._docker,
                "build",
                "--progress=plain",
                *(("--platform", self._platform) if self._platform else ()),
                "--file",
                str(dockerfile.path),
                "--iidfile",
                str(iidfile),
                "--tag",
                f"{LOCAL_IMAGE_NAME}:{_hex(input_hash)[:32]}",
                *(arg for k, v in sorted(request.build_args.items()) for arg in ("--build-arg", f"{k}={v}")),
                str(request.context_dir),
            ]
            capped = _CappedLog()
            # A thread reads the output while it streams, so the log never
            # holds more than the cap and the pipe never fills.
            # A new session puts docker and its children in one process group,
            # so a timeout or an interrupt kills them all and the pipe closes.
            # The session also keeps the terminal's Ctrl-C from docker, so the
            # builder must kill the group itself on every way out.
            with subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True
            ) as process:
                reader = threading.Thread(target=_read_capped, args=(process.stdout, capped), daemon=True)
                reader.start()
                try:
                    returncode = process.wait(timeout=self._limits.timeout_seconds)
                except BaseException as exc:
                    _kill_group(process)
                    process.wait()
                    _release_output(process, reader)
                    if not isinstance(exc, subprocess.TimeoutExpired):
                        raise
                    log = capped.text()
                    raise ImageBuildTimeoutError(
                        f"image build ran longer than the {self._limits.timeout_seconds / 60:g}-minute limit",
                        log=log,
                        log_tail=log_tail(log),
                    ) from exc
                # A child that docker left in its group can hold the pipe open
                # after a normal exit, so kill the group here too.
                _kill_group(process)
                _release_output(process, reader)
            log = capped.text()
            tail = log_tail(log)
            image_id = iidfile.read_text().strip() if iidfile.exists() else ""
        if returncode != 0 or not image_id:
            raise ImageBuildError(f"docker build exited with code {returncode}", log=log, log_tail=tail)
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
