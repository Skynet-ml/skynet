"""Registry interface for user images, and its AWS ECR implementation (SKY-888).

A builder pushes each user image to a neutral OCI registry. The registry
gives the builder its push and cache references, gives a provider the
credentials it needs to pull, and confirms that a digest exists before a run
pins it. v1 uses one ECR repository for each owner in Skynet's account
(SKY-737).
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import urllib.request
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from core.config import settings

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

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
# ECR tags allow at most 128 characters from this set.
_TAG = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
# One ECR repository path component, so an owner can never name a path below
# another owner's repository.
_OWNER = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
_OWNER_MAX = 64
_ECR_PREFIX_URI = re.compile(
    r"^(?P<registry>\d{12})\.dkr\.ecr\.(?P<region>[a-z0-9-]+)\.amazonaws\.com/(?P<prefix>[a-z0-9]+(?:[._/-][a-z0-9]+)*)$"
)
# The Terraform lifecycle rule and the tag-mutability exclusion key on this
# prefix: cache tags move and expire on their own schedule.
CACHE_TAG_PREFIX = "cache-"
_PUSH_ACTIONS = (
    "ecr:BatchCheckLayerAvailability",
    "ecr:BatchGetImage",
    "ecr:CompleteLayerUpload",
    "ecr:CreateRepository",
    "ecr:GetDownloadUrlForLayer",
    "ecr:InitiateLayerUpload",
    "ecr:PutImage",
    "ecr:UploadLayerPart",
)
_PULL_ACTIONS = (
    "ecr:BatchCheckLayerAvailability",
    "ecr:BatchGetImage",
    "ecr:GetDownloadUrlForLayer",
)
# The worker task role assumes the pull role (role chaining), so AWS caps the
# session at one hour.
_PULL_SESSION_SECONDS = 3600
_IMAGE_MANIFEST_TYPES = (
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
)
_IMAGE_INDEX_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
)
# An image config is a few kilobytes; the cap stops a huge blob before it is parsed.
_CONFIG_MAX_BYTES = 1 << 20
_CONFIG_FETCH_SECONDS = 30.0
# buildx adds one attestation manifest for each platform image of an index.
_INDEX_MAX_IMAGES = 32
# A user image can be up to 20 GB (SKY-737), so its copy into a provider's own
# registry gets a long limit. The provider ends a longer copy, and the run's
# reservation for the copy covers this limit (SKY-891).
COPY_TIMEOUT_SECONDS = 3600.0
# Copies a source image, with optional (username, password) pull credentials,
# into a provider's own registry (Vercel) and returns the copy's
# ``name@sha256:<digest>``. It is here, a leaf module, so the provider and the
# bridge share one definition without an import cycle.
ImageCopier = Callable[[str, tuple[str, str] | None], Awaitable[str]]


class ImageRegistryError(RuntimeError):
    """Report a registry reference, credential or digest problem."""


@dataclass(frozen=True)
class RegistryCredentials:
    """Hold short-lived credentials to pull from or push to a registry."""

    server: str
    username: str
    password: str
    expires_at: datetime | None = None

    def __repr__(self) -> str:
        """Return a representation that hides the password.

        Returns:
            The server, user name and expiry, with no secret.
        """
        return (
            f"RegistryCredentials(server={self.server!r}, username={self.username!r}, expires_at={self.expires_at!r})"
        )


@dataclass(frozen=True)
class RegistryImage:
    """Describe one image that the registry holds."""

    reference: str
    digest: str
    size_bytes: int


class ImageRegistry(Protocol):
    """Name the registry operations that a builder and a provider need.

    Each owner (for example one user) has its own repository, so one owner's
    build credentials never reach another owner's images or build cache.
    """

    def repository_uri(self, owner: str) -> str:
        """Return the URI of the owner's repository, with no tag or digest.

        Args:
            owner: The owner of the repository.

        Returns:
            ``<server>/<repository>``.
        """
        ...

    def push_reference(self, owner: str, tag: str) -> str:
        """Return the ``repository:tag`` reference a builder pushes to.

        Args:
            owner: The owner of the repository.
            tag: The image tag.

        Returns:
            The full push reference.
        """
        ...

    def cache_reference(self, owner: str, scope: str) -> str:
        """Return the ``repository:tag`` reference for a build cache.

        Args:
            owner: The owner of the repository.
            scope: The cache name inside the owner's repository.

        Returns:
            The full cache reference.
        """
        ...

    def pinned_reference(self, owner: str, digest: str) -> str:
        """Return the ``repository@digest`` reference that pins one image.

        Args:
            owner: The owner of the repository.
            digest: The image digest, ``sha256:<64 hex>``.

        Returns:
            The pinned reference.
        """
        ...

    def pull_credentials(self, owner: str) -> RegistryCredentials:
        """Return credentials that pull only from the owner's repository.

        Args:
            owner: The owner of the repository.

        Returns:
            The short-lived credentials.
        """
        ...

    def check_digest(self, owner: str, digest: str) -> RegistryImage:
        """Confirm that the owner's repository holds an image with this digest.

        Args:
            owner: The owner of the repository.
            digest: The image digest, ``sha256:<64 hex>``.

        Returns:
            The pinned reference, the digest and the size the registry reports.

        Raises:
            ImageRegistryError: The digest is malformed, the image is not
                there, or the registry reports no size.
        """
        ...

    def image_users(self, owner: str, digest: str) -> tuple[str, ...]:
        """Return the config ``User`` of the image, or of each platform image of an index.

        Args:
            owner: The owner of the repository.
            digest: The image digest, ``sha256:<64 hex>``.

        Returns:
            One ``User`` value for each platform image, ``""`` when the config sets none.

        Raises:
            ImageRegistryError: The registry cannot give the image's config.
        """
        ...

    def delete_image(self, owner: str, digest: str) -> None:
        """Delete one image from the owner's repository.

        Args:
            owner: The owner of the repository.
            digest: The image digest, ``sha256:<64 hex>``.

        Raises:
            ImageRegistryError: The registry refused the delete.
        """
        ...

    def delete_cache(self, owner: str, scope: str) -> None:
        """Delete the build cache manifest of one scope from the owner's repository.

        Args:
            owner: The owner of the repository.
            scope: The cache name inside the owner's repository.

        Raises:
            ImageRegistryError: The registry refused the delete.
        """
        ...

    def delete_repository(self, owner: str) -> None:
        """Delete the owner's whole repository, with every image and tag in it.

        A repository that does not exist counts as deleted.

        Args:
            owner: The owner of the repository.

        Raises:
            ImageRegistryError: The registry refused the delete.
        """
        ...


def require_digest(digest: str) -> str:
    """Return the digest when it is ``sha256:<64 lowercase hex>``.

    Args:
        digest: The value to check.

    Returns:
        The same digest.

    Raises:
        ImageRegistryError: The value is not a sha256 digest.
    """
    if not _DIGEST.fullmatch(digest):
        raise ImageRegistryError(f"not an image digest: {digest!r}")
    return digest


def require_owner(owner: str) -> str:
    """Return the owner when it is one valid ECR repository path component.

    Args:
        owner: The value to check.

    Returns:
        The same owner.

    Raises:
        ImageRegistryError: The value is empty, too long, or has a character
            or a ``/`` that a single repository name component refuses.
    """
    if len(owner) > _OWNER_MAX or not _OWNER.fullmatch(owner):
        raise ImageRegistryError(f"not a valid image owner: {owner!r}")
    return owner


def _require_tag(tag: str) -> str:
    """Return the tag when ECR accepts it.

    Args:
        tag: The value to check.

    Returns:
        The same tag.

    Raises:
        ImageRegistryError: The tag has a character or length ECR refuses.
    """
    if not _TAG.fullmatch(tag):
        raise ImageRegistryError(f"not a valid image tag: {tag!r}")
    return tag


def _aws_error_kind(exc: BaseException) -> str:
    """Name an AWS error by its type and code only, because its message can echo request details.

    Args:
        exc: The boto3 or botocore error.

    Returns:
        The type name, with the AWS error code when there is one.
    """
    code = getattr(exc, "response", {}).get("Error", {}).get("Code")
    return f"{type(exc).__name__} {code}" if code else type(exc).__name__


def _fetch_url(url: str) -> bytes:
    """Read the bytes at an ECR layer download URL, up to one byte past the config cap.

    Args:
        url: The presigned HTTPS URL that ECR returns.

    Returns:
        The bytes, cut one byte past ``_CONFIG_MAX_BYTES``, so the caller sees a larger blob.

    Raises:
        ImageRegistryError: The URL is not HTTPS.
        OSError: The download failed.
    """
    if not url.startswith("https://"):
        raise ImageRegistryError("ECR returned a layer download URL that is not HTTPS")
    with urllib.request.urlopen(url, timeout=_CONFIG_FETCH_SECONDS) as response:
        return response.read(_CONFIG_MAX_BYTES + 1)


def image_owner(username: str) -> str:
    """Name the registry repository of one user's images.

    Args:
        username: The user who owns the images.

    Returns:
        A short lowercase name that the registry accepts and that hides the username.
    """
    return "u" + hashlib.sha256(username.encode("utf-8")).hexdigest()[:16]


def image_registry(config: Any = settings, *, pull: bool = True) -> EcrRegistry | None:
    """Return the user image registry of this deployment (SKY-891).

    Args:
        config: The app settings.
        pull: Whether the caller needs pull credentials. A caller that only
            deletes images uses the worker's own ECR client, so it needs no pull role.

    Returns:
        An ECR registry with the worker's AWS credentials when the repository
        prefix is set and, when ``pull`` is true, the pull role too; ``None``
        otherwise, for example in local mode, where local Docker keeps the images.

    Raises:
        ImageRegistryError: The settings are set but boto3 is missing, or the
            prefix is not an ECR repository prefix URI.
    """
    prefix = config.user_images_repository_prefix_uri
    role = config.image_pull_role_arn
    if not prefix or (pull and not role):
        return None
    if boto3 is None:
        raise ImageRegistryError("the user image registry needs boto3")
    match = _ECR_PREFIX_URI.fullmatch(prefix)
    if match is None:
        raise ImageRegistryError(f"not an ECR repository prefix URI: {prefix!r}")
    region = match["region"]

    def ecr_for(credentials: Mapping[str, Any]) -> Any:
        """Make an ECR client from assumed-role credentials.

        Args:
            credentials: The ``Credentials`` that STS ``AssumeRole`` returns.

        Returns:
            A boto3 ECR client in the registry's Region.
        """
        return boto3.client(
            "ecr",
            region_name=region,
            aws_access_key_id=credentials["AccessKeyId"],
            aws_secret_access_key=credentials["SecretAccessKey"],
            aws_session_token=credentials["SessionToken"],
        )

    return EcrRegistry(
        boto3.client("ecr", region_name=region),
        prefix,
        # Only pull credentials use STS, so a caller that only deletes makes no STS client.
        sts=boto3.client("sts", region_name=region) if pull else None,
        pull_role_arn=role,
        ecr_for=ecr_for,
    )


class EcrRegistry:
    """Store user images in one ECR repository for each owner.

    The repositories are ``<prefix>/<owner>``. ECR creates each one on its
    first push from the repository creation template in Terraform. The boto3
    clients are injected, so tests pass fakes and the caller chooses the
    credentials and the Region.

    Pull credentials never come from the worker's own role, which reaches
    every owner's repository. The registry assumes the pull role with a
    session policy for one owner, so a box that gets the token can pull only
    that owner's images.
    """

    def __init__(
        self,
        client: Any,
        repository_prefix_uri: str,
        *,
        sts: Any | None,
        pull_role_arn: str | None,
        ecr_for: Callable[[Mapping[str, Any]], Any],
        fetch: Callable[[str], bytes] | None = None,
    ) -> None:
        """Bind the registry to one repository prefix and its pull role.

        Args:
            client: A boto3 ECR client (or a fake with the same methods).
            repository_prefix_uri: ``<account>.dkr.ecr.<region>.amazonaws.com/<prefix>``.
            sts: A boto3 STS client, or ``None`` for a registry that only
                deletes images.
            pull_role_arn: The role to assume, with an owner session policy,
                for pull credentials and image configs. ``None`` or an empty
                string makes a registry that only deletes images: each call
                that needs the pull role raises ``ImageRegistryError``.
            ecr_for: Make an ECR client from the ``Credentials`` that STS
                ``AssumeRole`` returns.
            fetch: Read the bytes at an ECR layer download URL. ``None`` uses
                an HTTPS GET with a time limit.

        Raises:
            ImageRegistryError: The URI is not an ECR repository prefix URI.
        """
        match = _ECR_PREFIX_URI.fullmatch(repository_prefix_uri)
        if match is None:
            raise ImageRegistryError(f"not an ECR repository prefix URI: {repository_prefix_uri!r}")
        self._client = client
        self._sts = sts
        self._pull_role = pull_role_arn
        self._ecr_for = ecr_for
        self._fetch = fetch or _fetch_url
        self.registry_id = match["registry"]
        self.region = match["region"]
        self.prefix = match["prefix"]
        self.server = repository_prefix_uri.split("/", 1)[0]

    def repository_name(self, owner: str) -> str:
        """Return the name of the owner's repository.

        Args:
            owner: The owner of the repository.

        Returns:
            ``<prefix>/<owner>``.

        Raises:
            ImageRegistryError: The owner is not valid.
        """
        return f"{self.prefix}/{require_owner(owner)}"

    def repository_arn(self, owner: str) -> str:
        """Return the ARN of the owner's repository.

        Args:
            owner: The owner of the repository.

        Returns:
            The repository ARN.

        Raises:
            ImageRegistryError: The owner is not valid.
        """
        return f"arn:aws:ecr:{self.region}:{self.registry_id}:repository/{self.repository_name(owner)}"

    def push_session_policy(self, owner: str) -> dict[str, Any]:
        """Return the IAM session policy that limits a build to one owner.

        The build assumes the push role with this policy, so its registry
        credentials reach only the owner's repository.

        Args:
            owner: The owner of the repository.

        Returns:
            The policy document.

        Raises:
            ImageRegistryError: The owner is not valid.
        """
        return {
            "Version": "2012-10-17",
            "Statement": [
                # GetAuthorizationToken has no resource-level permission.
                {"Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*"},
                {"Effect": "Allow", "Action": list(_PUSH_ACTIONS), "Resource": self.repository_arn(owner)},
            ],
        }

    def pull_session_policy(self, owner: str) -> dict[str, Any]:
        """Return the IAM session policy that limits pull credentials to one owner.

        Args:
            owner: The owner of the repository.

        Returns:
            The policy document.

        Raises:
            ImageRegistryError: The owner is not valid.
        """
        return {
            "Version": "2012-10-17",
            "Statement": [
                {"Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*"},
                {"Effect": "Allow", "Action": list(_PULL_ACTIONS), "Resource": self.repository_arn(owner)},
            ],
        }

    def _uri(self, owner: str) -> str:
        """Return the URI of the owner's repository.

        Args:
            owner: The owner of the repository.

        Returns:
            ``<server>/<prefix>/<owner>``.
        """
        return f"{self.server}/{self.repository_name(owner)}"

    def repository_uri(self, owner: str) -> str:
        """Return the URI of the owner's repository, with no tag or digest.

        Args:
            owner: The owner of the repository.

        Returns:
            ``<server>/<prefix>/<owner>``.

        Raises:
            ImageRegistryError: The owner is not valid.
        """
        return self._uri(owner)

    def push_reference(self, owner: str, tag: str) -> str:
        """Return the ``repository:tag`` reference a builder pushes to.

        Args:
            owner: The owner of the repository.
            tag: The image tag. It must not start with the cache prefix.

        Returns:
            The full push reference.

        Raises:
            ImageRegistryError: The owner or the tag is invalid, or the tag
                collides with the cache tags.
        """
        if _require_tag(tag).startswith(CACHE_TAG_PREFIX):
            raise ImageRegistryError(f"image tags must not start with {CACHE_TAG_PREFIX!r}: {tag!r}")
        return f"{self._uri(owner)}:{tag}"

    def cache_reference(self, owner: str, scope: str) -> str:
        """Return the ``repository:cache-<scope>`` reference for a build cache.

        Args:
            owner: The owner of the repository.
            scope: The cache name inside the owner's repository.

        Returns:
            The full cache reference.

        Raises:
            ImageRegistryError: The owner is invalid or the scope makes an invalid tag.
        """
        return f"{self._uri(owner)}:{_require_tag(CACHE_TAG_PREFIX + scope)}"

    def pinned_reference(self, owner: str, digest: str) -> str:
        """Return the ``repository@digest`` reference that pins one image.

        Args:
            owner: The owner of the repository.
            digest: The image digest.

        Returns:
            The pinned reference.

        Raises:
            ImageRegistryError: The owner or the digest is malformed.
        """
        return f"{self._uri(owner)}@{require_digest(digest)}"

    def pull_credentials(self, owner: str) -> RegistryCredentials:
        """Return an ECR token that pulls only from the owner's repository.

        The token comes from the pull role, assumed with the owner's session
        policy. The expiry is the earlier of the token and the session expiry.

        Args:
            owner: The owner of the repository.

        Returns:
            The user name, password, server and expiry of the token.

        Raises:
            ImageRegistryError: The registry has no pull role, the owner is
                invalid, or ECR returns no token or a token in an unknown form.
        """
        session = self._pull_session(owner)
        response = self._ecr_for(session).get_authorization_token(registryIds=[self.registry_id])
        data = response.get("authorizationData") or []
        if not data:
            raise ImageRegistryError("ECR returned no authorization token")
        token = data[0]
        username, sep, password = base64.b64decode(token["authorizationToken"]).decode().partition(":")
        if not sep:
            raise ImageRegistryError("ECR returned an authorization token in an unknown form")
        expiries = [e for e in (token.get("expiresAt"), session.get("Expiration")) if e is not None]
        return RegistryCredentials(
            server=self.server,
            username=username,
            password=password,
            expires_at=min(expiries) if expiries else None,
        )

    def _pull_session(self, owner: str) -> Mapping[str, Any]:
        """Assume the pull role with the owner's session policy.

        Args:
            owner: The owner of the repository.

        Returns:
            The ``Credentials`` that STS ``AssumeRole`` returns.

        Raises:
            ImageRegistryError: The registry has no pull role, or the owner is invalid.
        """
        if not self._pull_role or self._sts is None:
            raise ImageRegistryError("the user image registry has no pull role")
        policy = self.pull_session_policy(owner)
        return self._sts.assume_role(
            RoleArn=self._pull_role,
            RoleSessionName="skynet-image-pull",
            Policy=json.dumps(policy, separators=(",", ":")),
            DurationSeconds=_PULL_SESSION_SECONDS,
        )["Credentials"]

    def image_users(self, owner: str, digest: str) -> tuple[str, ...]:
        """Return the config ``User`` of the image, or of each platform image of an index (SKY-891).

        The calls use the pull role with the owner's session policy, so they
        read only the owner's repository. An index skips its attestation
        manifests and the entries with an ``unknown`` platform, which hold no
        runnable image. Each config's bytes must match its digest.

        Args:
            owner: The owner of the repository.
            digest: The image digest.

        Returns:
            One ``User`` value for each platform image, ``""`` when the config sets none.

        Raises:
            ImageRegistryError: The digest is malformed, the registry has no
                pull role, the AWS call or the download failed, or the
                manifest or config is not in a known form.
        """
        name = self.repository_name(owner)
        require_digest(digest)
        try:
            ecr = self._ecr_for(self._pull_session(owner))
            media_type, manifest = self._manifest(ecr, name, digest)
            if media_type in _IMAGE_INDEX_TYPES:
                children = [
                    child["digest"]
                    for child in manifest.get("manifests") or []
                    if isinstance(child, dict)
                    and (child.get("annotations") or {}).get("vnd.docker.reference.type") != "attestation-manifest"
                    and (child.get("platform") or {}).get("os") != "unknown"
                ]
                if not children or len(children) > _INDEX_MAX_IMAGES:
                    raise ImageRegistryError(f"{name} image {digest} is an index with {len(children)} platform images")
                images = [self._manifest(ecr, name, require_digest(str(child))) for child in children]
            else:
                images = [(media_type, manifest)]
            return tuple(self._config_user(ecr, name, *image) for image in images)
        except _AWS_ERRORS as exc:
            raise ImageRegistryError(
                f"cannot read the config of {name} image {digest}: {_aws_error_kind(exc)}"
            ) from None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ImageRegistryError(f"cannot read the config of {name} image {digest}: {type(exc).__name__}") from None

    def _manifest(self, ecr: Any, name: str, digest: str) -> tuple[str, dict[str, Any]]:
        """Read one manifest of the repository by its digest.

        Args:
            ecr: An ECR client with the owner's pull session.
            name: The repository name.
            digest: The manifest digest.

        Returns:
            The manifest's media type and its JSON object.

        Raises:
            ImageRegistryError: ECR does not hold the manifest, or it is not a JSON object.
        """
        response = ecr.batch_get_image(
            registryId=self.registry_id,
            repositoryName=name,
            imageIds=[{"imageDigest": digest}],
            acceptedMediaTypes=[*_IMAGE_MANIFEST_TYPES, *_IMAGE_INDEX_TYPES],
        )
        images = [i for i in response.get("images") or [] if (i.get("imageId") or {}).get("imageDigest") == digest]
        if not images:
            raise ImageRegistryError(f"{name} has no image {digest}")
        manifest = json.loads(images[0]["imageManifest"])
        if not isinstance(manifest, dict):
            raise ImageRegistryError(f"{name} image {digest} has a manifest in an unknown form")
        return str(images[0].get("imageManifestMediaType") or manifest.get("mediaType") or ""), manifest

    def _config_user(self, ecr: Any, name: str, media_type: str, manifest: Mapping[str, Any]) -> str:
        """Read the ``User`` of one platform image's config.

        Args:
            ecr: An ECR client with the owner's pull session.
            name: The repository name.
            media_type: The manifest's media type.
            manifest: The image manifest.

        Returns:
            The config ``User``, or ``""`` when the config sets none.

        Raises:
            ImageRegistryError: The manifest is not an image manifest, or the
                config is too large, does not match its digest, or has a ``User`` that is not a string.
        """
        if media_type not in _IMAGE_MANIFEST_TYPES:
            raise ImageRegistryError(f"{name} has a manifest of the unknown type {media_type!r}")
        config_digest = require_digest(str(manifest["config"]["digest"]))
        url = ecr.get_download_url_for_layer(
            registryId=self.registry_id, repositoryName=name, layerDigest=config_digest
        )["downloadUrl"]
        blob = self._fetch(url)
        if len(blob) > _CONFIG_MAX_BYTES:
            raise ImageRegistryError(f"{name} config {config_digest} is larger than {_CONFIG_MAX_BYTES} bytes")
        if "sha256:" + hashlib.sha256(blob).hexdigest() != config_digest:
            raise ImageRegistryError(f"{name} config {config_digest} does not match its digest")
        config = json.loads(blob)
        user = ((config.get("config") if isinstance(config, dict) else None) or {}).get("User") or ""
        if not isinstance(user, str):
            raise ImageRegistryError(f"{name} config {config_digest} has a User that is not a string")
        return user

    def check_digest(self, owner: str, digest: str) -> RegistryImage:
        """Confirm that the owner's repository holds an image with this digest.

        Args:
            owner: The owner of the repository.
            digest: The image digest.

        Returns:
            The pinned reference, the digest and the compressed size ECR reports.

        Raises:
            ImageRegistryError: The digest is malformed, ECR does not hold it,
                or ECR reports no size for it.
        """
        name = self.repository_name(owner)
        require_digest(digest)
        missing = (self._client.exceptions.ImageNotFoundException, self._client.exceptions.RepositoryNotFoundException)
        try:
            response = self._client.describe_images(
                registryId=self.registry_id,
                repositoryName=name,
                imageIds=[{"imageDigest": digest}],
            )
        except missing as exc:
            raise ImageRegistryError(f"{name} has no image {digest}") from exc
        details = [d for d in response.get("imageDetails", []) if d.get("imageDigest") == digest]
        if not details:
            raise ImageRegistryError(f"{name} has no image {digest}")
        size = details[0].get("imageSizeInBytes")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ImageRegistryError(f"{name} reports no size for image {digest}")
        return RegistryImage(reference=self.pinned_reference(owner, digest), digest=digest, size_bytes=size)

    def delete_image(self, owner: str, digest: str) -> None:
        """Delete one image from the owner's repository.

        An image or a repository that is already gone counts as deleted.

        Args:
            owner: The owner of the repository.
            digest: The image digest.

        Raises:
            ImageRegistryError: The digest is malformed or ECR refused the delete.
        """
        self._batch_delete(owner, {"imageDigest": require_digest(digest)})

    def delete_cache(self, owner: str, scope: str) -> None:
        """Delete the build cache manifest of one scope from the owner's repository.

        The tag is the manifest's only tag, so ECR deletes the manifest at
        once and then removes the layers that no other image uses.

        Args:
            owner: The owner of the repository.
            scope: The cache name inside the owner's repository.

        Raises:
            ImageRegistryError: The scope makes an invalid tag or ECR refused the delete.
        """
        self._batch_delete(owner, {"imageTag": _require_tag(CACHE_TAG_PREFIX + scope)})

    def delete_repository(self, owner: str) -> None:
        """Delete the owner's whole repository, with every image and tag in it.

        ``force`` deletes a repository that still holds images. It also takes
        the images and cache tags of a build that pushed but never recorded a
        row. ECR creates the repository again on the owner's next push.

        Args:
            owner: The owner of the repository.

        Raises:
            ImageRegistryError: The owner is invalid, or the AWS call failed.
        """
        name = self.repository_name(owner)
        try:
            self._client.delete_repository(registryId=self.registry_id, repositoryName=name, force=True)
        except self._client.exceptions.RepositoryNotFoundException:
            return
        except _AWS_ERRORS as exc:
            raise ImageRegistryError(f"cannot delete the repository {name}: {_aws_error_kind(exc)}") from None

    def _batch_delete(self, owner: str, image_id: Mapping[str, str]) -> None:
        """Delete one image id from the owner's repository.

        Args:
            owner: The owner of the repository.
            image_id: The ECR image id (a digest or a tag).

        Raises:
            ImageRegistryError: The owner is invalid, ECR refused the delete,
                or the AWS call failed.
        """
        name = self.repository_name(owner)
        try:
            response = self._client.batch_delete_image(
                registryId=self.registry_id,
                repositoryName=name,
                imageIds=[dict(image_id)],
            )
        except self._client.exceptions.RepositoryNotFoundException:
            return
        except _AWS_ERRORS as exc:
            raise ImageRegistryError(f"cannot delete from {name}: {_aws_error_kind(exc)}") from None
        failures = [f for f in response.get("failures", []) if f.get("failureCode") != "ImageNotFound"]
        if failures:
            reason = failures[0].get("failureReason") or failures[0].get("failureCode")
            label = next(iter(image_id.values()))
            raise ImageRegistryError(f"cannot delete image {label} from {name}: {reason}")
