"""Registry interface for user images, and its AWS ECR implementation (SKY-888).

A builder pushes each user image to a neutral OCI registry. The registry
gives the builder its push and cache references, gives a provider the
credentials it needs to pull, and confirms that a digest exists before a run
pins it. v1 uses one ECR repository for each owner in Skynet's account
(SKY-737).
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

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

    def pull_credentials(self) -> RegistryCredentials:
        """Return credentials to pull from the registry.

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

    def delete_image(self, owner: str, digest: str) -> None:
        """Delete one image from the owner's repository.

        Args:
            owner: The owner of the repository.
            digest: The image digest, ``sha256:<64 hex>``.

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
    if not _DIGEST.match(digest):
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
    if len(owner) > _OWNER_MAX or not _OWNER.match(owner):
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
    if not _TAG.match(tag):
        raise ImageRegistryError(f"not a valid image tag: {tag!r}")
    return tag


class EcrRegistry:
    """Store user images in one ECR repository for each owner.

    The repositories are ``<prefix>/<owner>``. ECR creates each one on its
    first push from the repository creation template in Terraform. The boto3
    ECR client is injected, so tests pass a fake and the caller chooses the
    credentials and the Region.
    """

    def __init__(self, client: Any, repository_prefix_uri: str) -> None:
        """Bind the registry to one repository prefix.

        Args:
            client: A boto3 ECR client (or a fake with the same methods).
            repository_prefix_uri: ``<account>.dkr.ecr.<region>.amazonaws.com/<prefix>``.

        Raises:
            ImageRegistryError: The URI is not an ECR repository prefix URI.
        """
        match = _ECR_PREFIX_URI.match(repository_prefix_uri)
        if match is None:
            raise ImageRegistryError(f"not an ECR repository prefix URI: {repository_prefix_uri!r}")
        self._client = client
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

    def _uri(self, owner: str) -> str:
        """Return the URI of the owner's repository.

        Args:
            owner: The owner of the repository.

        Returns:
            ``<server>/<prefix>/<owner>``.
        """
        return f"{self.server}/{self.repository_name(owner)}"

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

    def pull_credentials(self) -> RegistryCredentials:
        """Return an ECR authorization token as registry credentials.

        Returns:
            The user name, password, server and expiry of the token.

        Raises:
            ImageRegistryError: ECR returns no token or a token in an unknown form.
        """
        response = self._client.get_authorization_token(registryIds=[self.registry_id])
        data = response.get("authorizationData") or []
        if not data:
            raise ImageRegistryError("ECR returned no authorization token")
        token = data[0]
        username, sep, password = base64.b64decode(token["authorizationToken"]).decode().partition(":")
        if not sep:
            raise ImageRegistryError("ECR returned an authorization token in an unknown form")
        return RegistryCredentials(
            server=self.server,
            username=username,
            password=password,
            expires_at=token.get("expiresAt"),
        )

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
        name = self.repository_name(owner)
        require_digest(digest)
        try:
            response = self._client.batch_delete_image(
                registryId=self.registry_id,
                repositoryName=name,
                imageIds=[{"imageDigest": digest}],
            )
        except self._client.exceptions.RepositoryNotFoundException:
            return
        failures = [f for f in response.get("failures", []) if f.get("failureCode") != "ImageNotFound"]
        if failures:
            reason = failures[0].get("failureReason") or failures[0].get("failureCode")
            raise ImageRegistryError(f"cannot delete image {digest} from {name}: {reason}")
