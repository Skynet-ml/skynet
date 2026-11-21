"""Registry interface for user images, and its AWS ECR implementation (SKY-888).

A builder pushes each user image to a neutral OCI registry. The registry
gives the builder its push and cache references, gives a provider the
credentials it needs to pull, and confirms that a digest exists before a run
pins it. v1 uses one ECR repository in Skynet's account (SKY-737).
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
_ECR_URI = re.compile(
    r"^(?P<registry>\d{12})\.dkr\.ecr\.(?P<region>[a-z0-9-]+)\.amazonaws\.com/(?P<repository>[a-z0-9._/-]+)$"
)
# The Terraform lifecycle rule keys on this prefix: cache tags move and
# expire on their own schedule.
CACHE_TAG_PREFIX = "cache-"


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
    """Name the registry operations that a builder and a provider need."""

    def push_reference(self, tag: str) -> str:
        """Return the ``repository:tag`` reference a builder pushes to.

        Args:
            tag: The image tag.

        Returns:
            The full push reference.
        """
        ...

    def cache_reference(self, scope: str) -> str:
        """Return the ``repository:tag`` reference for a build cache scope.

        Args:
            scope: The cache owner, for example one user. Scopes never share a cache.

        Returns:
            The full cache reference.
        """
        ...

    def pinned_reference(self, digest: str) -> str:
        """Return the ``repository@digest`` reference that pins one image.

        Args:
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

    def check_digest(self, digest: str) -> RegistryImage:
        """Confirm that the registry holds an image with this digest.

        Args:
            digest: The image digest, ``sha256:<64 hex>``.

        Returns:
            The pinned reference, the digest and the size the registry reports.

        Raises:
            ImageRegistryError: The digest is malformed or the image is not there.
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
    """Store user images in one ECR repository.

    The boto3 ECR client is injected, so tests pass a fake and the caller
    chooses the credentials and the Region.
    """

    def __init__(self, client: Any, repository_uri: str) -> None:
        """Bind the registry to one repository.

        Args:
            client: A boto3 ECR client (or a fake with the same methods).
            repository_uri: ``<account>.dkr.ecr.<region>.amazonaws.com/<name>``.

        Raises:
            ImageRegistryError: The URI is not an ECR repository URI.
        """
        match = _ECR_URI.match(repository_uri)
        if match is None:
            raise ImageRegistryError(f"not an ECR repository URI: {repository_uri!r}")
        self._client = client
        self.repository_uri = repository_uri
        self.registry_id = match["registry"]
        self.repository_name = match["repository"]
        self.server = repository_uri.split("/", 1)[0]

    def push_reference(self, tag: str) -> str:
        """Return the ``repository:tag`` reference a builder pushes to.

        Args:
            tag: The image tag. It must not start with the cache prefix.

        Returns:
            The full push reference.

        Raises:
            ImageRegistryError: The tag is invalid or collides with the cache tags.
        """
        if _require_tag(tag).startswith(CACHE_TAG_PREFIX):
            raise ImageRegistryError(f"image tags must not start with {CACHE_TAG_PREFIX!r}: {tag!r}")
        return f"{self.repository_uri}:{tag}"

    def cache_reference(self, scope: str) -> str:
        """Return the ``repository:cache-<scope>`` reference for a build cache.

        Args:
            scope: The cache owner, for example one user.

        Returns:
            The full cache reference.

        Raises:
            ImageRegistryError: The scope makes an invalid tag.
        """
        return f"{self.repository_uri}:{_require_tag(CACHE_TAG_PREFIX + scope)}"

    def pinned_reference(self, digest: str) -> str:
        """Return the ``repository@digest`` reference that pins one image.

        Args:
            digest: The image digest.

        Returns:
            The pinned reference.

        Raises:
            ImageRegistryError: The digest is malformed.
        """
        return f"{self.repository_uri}@{require_digest(digest)}"

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

    def check_digest(self, digest: str) -> RegistryImage:
        """Confirm that the repository holds an image with this digest.

        Args:
            digest: The image digest.

        Returns:
            The pinned reference, the digest and the compressed size ECR reports.

        Raises:
            ImageRegistryError: The digest is malformed or ECR does not hold it.
        """
        require_digest(digest)
        try:
            response = self._client.describe_images(
                registryId=self.registry_id,
                repositoryName=self.repository_name,
                imageIds=[{"imageDigest": digest}],
            )
        except self._client.exceptions.ImageNotFoundException as exc:
            raise ImageRegistryError(f"{self.repository_uri} has no image {digest}") from exc
        details = [d for d in response.get("imageDetails", []) if d.get("imageDigest") == digest]
        if not details:
            raise ImageRegistryError(f"{self.repository_uri} has no image {digest}")
        return RegistryImage(
            reference=self.pinned_reference(digest),
            digest=digest,
            size_bytes=int(details[0].get("imageSizeInBytes", 0)),
        )
