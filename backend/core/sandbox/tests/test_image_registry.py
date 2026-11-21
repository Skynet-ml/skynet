"""Tests for the ECR user-image registry, with a fake ECR client (SKY-888)."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Any

import pytest

from core.sandbox.image_registry import EcrRegistry, ImageRegistryError, require_digest

URI = "123456789012.dkr.ecr.us-east-2.amazonaws.com/skynet-staging-user-images"
DIGEST = "sha256:" + "a" * 64


class _ImageNotFoundError(Exception):
    """Stand in for botocore's ImageNotFoundException."""


class _Exceptions:
    """Expose the modeled exceptions like a boto3 client does."""

    ImageNotFoundException = _ImageNotFoundError


class FakeEcr:
    """Answer the two ECR calls that the registry makes."""

    exceptions = _Exceptions

    def __init__(self, images: dict[str, int] | None = None, token: str = "AWS:secret-token") -> None:
        """Hold the images by digest and the authorization token.

        Args:
            images: Image sizes by digest.
            token: The decoded authorization token.
        """
        self.images = images or {}
        self.token = token
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get_authorization_token(self, **kwargs: Any) -> dict[str, Any]:
        """Return one encoded token.

        Args:
            **kwargs: The request.

        Returns:
            The ECR response.
        """
        self.calls.append(("get_authorization_token", kwargs))
        expires = datetime(2026, 11, 21, tzinfo=UTC)
        encoded = base64.b64encode(self.token.encode()).decode()
        return {"authorizationData": [{"authorizationToken": encoded, "expiresAt": expires}]}

    def describe_images(self, **kwargs: Any) -> dict[str, Any]:
        """Return the image details or raise ImageNotFound.

        Args:
            **kwargs: The request.

        Returns:
            The ECR response.
        """
        self.calls.append(("describe_images", kwargs))
        digest = kwargs["imageIds"][0]["imageDigest"]
        if digest not in self.images:
            raise _ImageNotFoundError(digest)
        return {"imageDetails": [{"imageDigest": digest, "imageSizeInBytes": self.images[digest]}]}


def test_references_use_the_repository_uri() -> None:
    """Push, cache and pinned references name the one repository."""
    registry = EcrRegistry(FakeEcr(), URI)

    assert registry.push_reference("abc-1") == f"{URI}:abc-1"
    assert registry.cache_reference("user-7") == f"{URI}:cache-user-7"
    assert registry.pinned_reference(DIGEST) == f"{URI}@{DIGEST}"


def test_push_reference_refuses_cache_and_invalid_tags() -> None:
    """An image tag cannot take the cache prefix or a character ECR refuses."""
    registry = EcrRegistry(FakeEcr(), URI)

    with pytest.raises(ImageRegistryError, match="cache-"):
        registry.push_reference("cache-user-7")
    with pytest.raises(ImageRegistryError, match="valid image tag"):
        registry.push_reference("bad tag")
    with pytest.raises(ImageRegistryError, match="valid image tag"):
        registry.cache_reference("x" * 128)


def test_rejects_a_uri_that_is_not_ecr() -> None:
    """Only an ECR repository URI is accepted."""
    with pytest.raises(ImageRegistryError, match="ECR repository URI"):
        EcrRegistry(FakeEcr(), "docker.io/library/python")


def test_pull_credentials_decode_the_token_and_hide_the_password() -> None:
    """The token splits into user and password, and repr never shows the password."""
    fake = FakeEcr()
    credentials = EcrRegistry(fake, URI).pull_credentials()

    assert credentials.server == "123456789012.dkr.ecr.us-east-2.amazonaws.com"
    assert (credentials.username, credentials.password) == ("AWS", "secret-token")
    assert credentials.expires_at == datetime(2026, 11, 21, tzinfo=UTC)
    assert "secret-token" not in repr(credentials)
    assert fake.calls == [("get_authorization_token", {"registryIds": ["123456789012"]})]


def test_pull_credentials_refuse_a_malformed_token() -> None:
    """A token with no colon is an error, not a password."""
    with pytest.raises(ImageRegistryError, match="unknown form"):
        EcrRegistry(FakeEcr(token="no-colon"), URI).pull_credentials()


def test_check_digest_returns_the_pinned_image_and_size() -> None:
    """A held digest returns its pinned reference and the size ECR reports."""
    fake = FakeEcr({DIGEST: 1234})
    image = EcrRegistry(fake, URI).check_digest(DIGEST)

    assert (image.reference, image.digest, image.size_bytes) == (f"{URI}@{DIGEST}", DIGEST, 1234)
    assert fake.calls[0][1] == {
        "registryId": "123456789012",
        "repositoryName": "skynet-staging-user-images",
        "imageIds": [{"imageDigest": DIGEST}],
    }


def test_check_digest_fails_for_a_missing_or_malformed_digest() -> None:
    """A missing image and a non-digest value both fail with a clear error."""
    registry = EcrRegistry(FakeEcr(), URI)

    with pytest.raises(ImageRegistryError, match="has no image"):
        registry.check_digest(DIGEST)
    with pytest.raises(ImageRegistryError, match="not an image digest"):
        registry.check_digest("latest")
    with pytest.raises(ImageRegistryError, match="not an image digest"):
        require_digest("sha256:" + "A" * 64)
