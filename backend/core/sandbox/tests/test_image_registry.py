"""Tests for the ECR user-image registry, with a fake ECR client (SKY-888)."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from typing import Any

import pytest

from core.sandbox.image_registry import EcrRegistry, ImageRegistryError, require_digest

SERVER = "123456789012.dkr.ecr.us-east-2.amazonaws.com"
PREFIX = "skynet/user-images-staging"
URI = f"{SERVER}/{PREFIX}"
DIGEST = "sha256:" + "a" * 64


class _ImageNotFoundError(Exception):
    """Stand in for botocore's ImageNotFoundException."""


class _RepositoryNotFoundError(Exception):
    """Stand in for botocore's RepositoryNotFoundException."""


class _Exceptions:
    """Expose the modeled exceptions like a boto3 client does."""

    ImageNotFoundException = _ImageNotFoundError
    RepositoryNotFoundException = _RepositoryNotFoundError


class FakeEcr:
    """Answer the ECR calls that the registry makes."""

    exceptions = _Exceptions

    def __init__(
        self,
        images: dict[str, Any] | None = None,
        token: str = "AWS:secret-token",
        repositories: set[str] | None = None,
        delete_failures: list[dict[str, str]] | None = None,
    ) -> None:
        """Hold the images by digest, the token and the repositories.

        Args:
            images: Image details by digest: a size, or a dict of extra fields.
            token: The decoded authorization token.
            repositories: The repository names that exist. ``None`` means all.
            delete_failures: The failures a delete returns.
        """
        self.images = images or {}
        self.token = token
        self.repositories = repositories
        self.delete_failures = delete_failures or []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _check_repository(self, name: str) -> None:
        """Raise RepositoryNotFound for a missing repository.

        Args:
            name: The repository name.
        """
        if self.repositories is not None and name not in self.repositories:
            raise _RepositoryNotFoundError(name)

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
        self._check_repository(kwargs["repositoryName"])
        digest = kwargs["imageIds"][0]["imageDigest"]
        if digest not in self.images:
            raise _ImageNotFoundError(digest)
        detail = self.images[digest]
        fields = detail if isinstance(detail, dict) else {"imageSizeInBytes": detail}
        return {"imageDetails": [{"imageDigest": digest, **fields}]}

    def batch_delete_image(self, **kwargs: Any) -> dict[str, Any]:
        """Record a delete and return the scripted failures.

        Args:
            **kwargs: The request.

        Returns:
            The ECR response.
        """
        self.calls.append(("batch_delete_image", kwargs))
        self._check_repository(kwargs["repositoryName"])
        return {"imageIds": [] if self.delete_failures else kwargs["imageIds"], "failures": self.delete_failures}


def test_references_use_the_owner_repository() -> None:
    """Push, cache and pinned references name the owner's own repository."""
    registry = EcrRegistry(FakeEcr(), URI)

    assert registry.repository_name("user-7") == f"{PREFIX}/user-7"
    assert registry.push_reference("user-7", "abc-1") == f"{URI}/user-7:abc-1"
    assert registry.cache_reference("user-7", "default") == f"{URI}/user-7:cache-default"
    assert registry.pinned_reference("user-7", DIGEST) == f"{URI}/user-7@{DIGEST}"
    assert registry.repository_arn("user-7") == f"arn:aws:ecr:us-east-2:123456789012:repository/{PREFIX}/user-7"


@pytest.mark.parametrize("owner", ["", "User7", "a/b", "../x", "-x", "x" * 65])
def test_owner_must_be_one_repository_name_component(owner: str) -> None:
    """An owner can never name a path in another owner's repository."""
    with pytest.raises(ImageRegistryError, match="not a valid image owner"):
        EcrRegistry(FakeEcr(), URI).push_reference(owner, "abc")


def test_push_session_policy_allows_only_the_owner_repository() -> None:
    """The session policy limits every push action to the owner's repository."""
    registry = EcrRegistry(FakeEcr(), URI)

    policy = registry.push_session_policy("user-7")

    [login, push] = policy["Statement"]
    assert login == {"Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*"}
    assert push["Resource"] == registry.repository_arn("user-7")
    assert "ecr:PutImage" in push["Action"]
    assert "ecr:CreateRepository" in push["Action"]
    assert "ecr:BatchDeleteImage" not in push["Action"]


def test_push_reference_refuses_cache_and_invalid_tags() -> None:
    """An image tag cannot take the cache prefix or a character ECR refuses."""
    registry = EcrRegistry(FakeEcr(), URI)

    with pytest.raises(ImageRegistryError, match="cache-"):
        registry.push_reference("u1", "cache-user-7")
    with pytest.raises(ImageRegistryError, match="valid image tag"):
        registry.push_reference("u1", "bad tag")
    with pytest.raises(ImageRegistryError, match="valid image tag"):
        registry.cache_reference("u1", "x" * 128)


@pytest.mark.parametrize("uri", ["docker.io/library/python", SERVER, f"{SERVER}/Bad"])
def test_rejects_a_uri_that_is_not_an_ecr_prefix(uri: str) -> None:
    """Only an ECR repository prefix URI is accepted."""
    with pytest.raises(ImageRegistryError, match="ECR repository prefix URI"):
        EcrRegistry(FakeEcr(), uri)


def test_pull_credentials_decode_the_token_and_hide_the_password() -> None:
    """The token splits into user and password, and repr never shows the password."""
    fake = FakeEcr()
    credentials = EcrRegistry(fake, URI).pull_credentials()

    assert credentials.server == SERVER
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
    image = EcrRegistry(fake, URI).check_digest("user-7", DIGEST)

    assert (image.reference, image.digest, image.size_bytes) == (f"{URI}/user-7@{DIGEST}", DIGEST, 1234)
    assert fake.calls[0][1] == {
        "registryId": "123456789012",
        "repositoryName": f"{PREFIX}/user-7",
        "imageIds": [{"imageDigest": DIGEST}],
    }


def test_check_digest_fails_for_a_missing_or_malformed_digest() -> None:
    """A missing image, a missing repository and a non-digest value fail clearly."""
    registry = EcrRegistry(FakeEcr(), URI)

    with pytest.raises(ImageRegistryError, match="has no image"):
        registry.check_digest("u1", DIGEST)
    with pytest.raises(ImageRegistryError, match="has no image"):
        EcrRegistry(FakeEcr({DIGEST: 1}, repositories=set()), URI).check_digest("u1", DIGEST)
    with pytest.raises(ImageRegistryError, match="not an image digest"):
        registry.check_digest("u1", "latest")
    with pytest.raises(ImageRegistryError, match="not an image digest"):
        require_digest("sha256:" + "A" * 64)


@pytest.mark.parametrize(
    "detail", [{}, {"imageSizeInBytes": None}, {"imageSizeInBytes": True}, {"imageSizeInBytes": -1}]
)
def test_check_digest_refuses_an_image_with_no_size(detail: dict[str, Any]) -> None:
    """A missing or bad size is an error, so the size limit never sees 0 by mistake."""
    with pytest.raises(ImageRegistryError, match="reports no size"):
        EcrRegistry(FakeEcr({DIGEST: detail}), URI).check_digest("u1", DIGEST)


def test_delete_image_deletes_one_digest_from_the_owner_repository() -> None:
    """A delete names the owner's repository and the digest."""
    fake = FakeEcr()

    EcrRegistry(fake, URI).delete_image("user-7", DIGEST)

    assert fake.calls == [
        (
            "batch_delete_image",
            {"registryId": "123456789012", "repositoryName": f"{PREFIX}/user-7", "imageIds": [{"imageDigest": DIGEST}]},
        )
    ]


def test_delete_image_ignores_an_image_or_repository_that_is_gone() -> None:
    """An image or a repository that is already gone counts as deleted."""
    EcrRegistry(FakeEcr(delete_failures=[{"failureCode": "ImageNotFound"}]), URI).delete_image("u1", DIGEST)
    EcrRegistry(FakeEcr(repositories=set()), URI).delete_image("u1", DIGEST)


def test_delete_image_raises_on_another_failure() -> None:
    """A refused delete raises with the ECR reason."""
    fake = FakeEcr(delete_failures=[{"failureCode": "KmsError", "failureReason": "no key"}])

    with pytest.raises(ImageRegistryError, match="no key"):
        EcrRegistry(fake, URI).delete_image("u1", DIGEST)
