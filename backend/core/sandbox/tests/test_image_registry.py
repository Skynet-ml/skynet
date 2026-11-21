"""Tests for the ECR user-image registry, with a fake ECR client (SKY-888)."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from core.sandbox import image_registry as image_registry_module
from core.sandbox.image_registry import EcrRegistry, ImageRegistryError, image_owner, image_registry, require_digest

SERVER = "123456789012.dkr.ecr.us-east-2.amazonaws.com"
PREFIX = "skynet/user-images-staging"
URI = f"{SERVER}/{PREFIX}"
DIGEST = "sha256:" + "a" * 64
PULL_ROLE = "arn:aws:iam::123456789012:role/skynet-staging-image-build-pull"


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


class FakeSts:
    """Return scripted credentials for AssumeRole and record each call."""

    def __init__(self, expiration: datetime | None = None) -> None:
        """Hold the session expiry.

        Args:
            expiration: The ``Expiration`` of the returned credentials.
        """
        self.expiration = expiration or datetime(2026, 11, 20, 13, tzinfo=UTC)
        self.calls: list[dict[str, Any]] = []

    def assume_role(self, **kwargs: Any) -> dict[str, Any]:
        """Record the call and return session credentials.

        Args:
            **kwargs: The request.

        Returns:
            The STS response.
        """
        self.calls.append(kwargs)
        return {
            "Credentials": {
                "AccessKeyId": "ASIA-test",
                "SecretAccessKey": "test-secret",
                "SessionToken": "test-session",
                "Expiration": self.expiration,
            }
        }


def _registry(
    client: Any,
    uri: str,
    *,
    sts: FakeSts | None = None,
    ecr_for: Any = None,
) -> EcrRegistry:
    """Build a registry with a fake STS client and pull role.

    Args:
        client: The fake worker ECR client.
        uri: The repository prefix URI.
        sts: The fake STS client.
        ecr_for: The factory for the assumed-role ECR client. The default
            returns ``client``.

    Returns:
        The registry.
    """
    return EcrRegistry(
        client,
        uri,
        sts=sts or FakeSts(),
        pull_role_arn=PULL_ROLE,
        ecr_for=ecr_for or (lambda _credentials: client),
    )


def test_references_use_the_owner_repository() -> None:
    """Push, cache and pinned references name the owner's own repository."""
    registry = _registry(FakeEcr(), URI)

    assert registry.repository_name("user-7") == f"{PREFIX}/user-7"
    assert registry.push_reference("user-7", "abc-1") == f"{URI}/user-7:abc-1"
    assert registry.cache_reference("user-7", "default") == f"{URI}/user-7:cache-default"
    assert registry.pinned_reference("user-7", DIGEST) == f"{URI}/user-7@{DIGEST}"
    assert registry.repository_arn("user-7") == f"arn:aws:ecr:us-east-2:123456789012:repository/{PREFIX}/user-7"


@pytest.mark.parametrize("owner", ["", "User7", "a/b", "../x", "-x", "x" * 65])
def test_owner_must_be_one_repository_name_component(owner: str) -> None:
    """An owner can never name a path in another owner's repository."""
    with pytest.raises(ImageRegistryError, match="not a valid image owner"):
        _registry(FakeEcr(), URI).push_reference(owner, "abc")


def test_push_session_policy_allows_only_the_owner_repository() -> None:
    """The session policy limits every push action to the owner's repository."""
    registry = _registry(FakeEcr(), URI)

    policy = registry.push_session_policy("user-7")

    [login, push] = policy["Statement"]
    assert login == {"Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*"}
    assert push["Resource"] == registry.repository_arn("user-7")
    assert "ecr:PutImage" in push["Action"]
    assert "ecr:CreateRepository" in push["Action"]
    assert "ecr:BatchDeleteImage" not in push["Action"]


def test_push_reference_refuses_cache_and_invalid_tags() -> None:
    """An image tag cannot take the cache prefix or a character ECR refuses."""
    registry = _registry(FakeEcr(), URI)

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
        _registry(FakeEcr(), uri)


def test_pull_session_policy_allows_only_pulls_from_the_owner_repository() -> None:
    """The pull session policy allows the token and the 3 pull actions on one repository."""
    registry = _registry(FakeEcr(), URI)

    policy = registry.pull_session_policy("user-7")

    [login, pull] = policy["Statement"]
    assert login == {"Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*"}
    assert pull == {
        "Effect": "Allow",
        "Action": ["ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"],
        "Resource": f"arn:aws:ecr:us-east-2:123456789012:repository/{PREFIX}/user-7",
    }


def test_pull_credentials_come_from_the_pull_role_scoped_to_the_owner() -> None:
    """The token comes from an ECR client on the assumed pull role, never the worker client."""
    worker = FakeEcr(token="AWS:worker-token")
    pull = FakeEcr()
    sts = FakeSts()
    seen: list[Any] = []

    def _ecr_for(credentials: Any) -> FakeEcr:
        """Record the session credentials and return the pull client.

        Args:
            credentials: The assumed-role credentials.

        Returns:
            The pull client.
        """
        seen.append(credentials)
        return pull

    credentials = _registry(worker, URI, sts=sts, ecr_for=_ecr_for).pull_credentials("user-7")

    [call] = sts.calls
    assert call["RoleArn"] == PULL_ROLE
    assert call["DurationSeconds"] == 3600
    assert json.loads(call["Policy"]) == _registry(worker, URI).pull_session_policy("user-7")
    assert seen[0]["SessionToken"] == "test-session"
    assert worker.calls == []
    assert pull.calls == [("get_authorization_token", {"registryIds": ["123456789012"]})]
    assert credentials.server == SERVER
    assert (credentials.username, credentials.password) == ("AWS", "secret-token")
    assert "secret-token" not in repr(credentials)


def test_pull_credentials_expire_with_the_earlier_of_token_and_session() -> None:
    """The session ends before the 12-hour token, so the credentials expire with the session."""
    session_end = datetime(2026, 11, 20, 13, tzinfo=UTC)
    credentials = _registry(FakeEcr(), URI, sts=FakeSts(session_end)).pull_credentials("u1")

    assert credentials.expires_at == session_end

    late = datetime(2026, 11, 22, tzinfo=UTC)
    assert _registry(FakeEcr(), URI, sts=FakeSts(late)).pull_credentials("u1").expires_at == datetime(
        2026, 11, 21, tzinfo=UTC
    )


def test_pull_credentials_refuse_an_invalid_owner_before_assuming_the_role() -> None:
    """An invalid owner fails before any STS call."""
    sts = FakeSts()

    with pytest.raises(ImageRegistryError, match="not a valid image owner"):
        _registry(FakeEcr(), URI, sts=sts).pull_credentials("../x")
    assert sts.calls == []


def test_pull_credentials_refuse_a_malformed_token() -> None:
    """A token with no colon is an error, not a password."""
    with pytest.raises(ImageRegistryError, match="unknown form"):
        _registry(FakeEcr(token="no-colon"), URI).pull_credentials("u1")


def test_check_digest_returns_the_pinned_image_and_size() -> None:
    """A held digest returns its pinned reference and the size ECR reports."""
    fake = FakeEcr({DIGEST: 1234})
    image = _registry(fake, URI).check_digest("user-7", DIGEST)

    assert (image.reference, image.digest, image.size_bytes) == (f"{URI}/user-7@{DIGEST}", DIGEST, 1234)
    assert fake.calls[0][1] == {
        "registryId": "123456789012",
        "repositoryName": f"{PREFIX}/user-7",
        "imageIds": [{"imageDigest": DIGEST}],
    }


def test_check_digest_fails_for_a_missing_or_malformed_digest() -> None:
    """A missing image, a missing repository and a non-digest value fail clearly."""
    registry = _registry(FakeEcr(), URI)

    with pytest.raises(ImageRegistryError, match="has no image"):
        registry.check_digest("u1", DIGEST)
    with pytest.raises(ImageRegistryError, match="has no image"):
        _registry(FakeEcr({DIGEST: 1}, repositories=set()), URI).check_digest("u1", DIGEST)
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
        _registry(FakeEcr({DIGEST: detail}), URI).check_digest("u1", DIGEST)


def test_delete_image_deletes_one_digest_from_the_owner_repository() -> None:
    """A delete names the owner's repository and the digest."""
    fake = FakeEcr()

    _registry(fake, URI).delete_image("user-7", DIGEST)

    assert fake.calls == [
        (
            "batch_delete_image",
            {"registryId": "123456789012", "repositoryName": f"{PREFIX}/user-7", "imageIds": [{"imageDigest": DIGEST}]},
        )
    ]


def test_delete_image_ignores_an_image_or_repository_that_is_gone() -> None:
    """An image or a repository that is already gone counts as deleted."""
    _registry(FakeEcr(delete_failures=[{"failureCode": "ImageNotFound"}]), URI).delete_image("u1", DIGEST)
    _registry(FakeEcr(repositories=set()), URI).delete_image("u1", DIGEST)


def test_delete_image_raises_on_another_failure() -> None:
    """A refused delete raises with the ECR reason."""
    fake = FakeEcr(delete_failures=[{"failureCode": "KmsError", "failureReason": "no key"}])

    with pytest.raises(ImageRegistryError, match="no key"):
        _registry(fake, URI).delete_image("u1", DIGEST)


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (
            ClientError({"Error": {"Code": "AccessDeniedException", "Message": "secret-detail"}}, "BatchDeleteImage"),
            "ClientError AccessDeniedException",
        ),
        (
            ClientError({"Error": {"Code": "ThrottlingException", "Message": "secret-detail"}}, "BatchDeleteImage"),
            "ClientError ThrottlingException",
        ),
        (EndpointConnectionError(endpoint_url="https://secret-detail.example"), "EndpointConnectionError"),
    ],
)
def test_a_failed_delete_call_names_only_the_error_type_and_code(error: Exception, kind: str) -> None:
    """Map an AWS error of a delete to ImageRegistryError, so account deletion answers 503 (SKY-891).

    Args:
        error: The error the ECR call raises.
        kind: The error type and code the message names.
    """
    fake = FakeEcr()

    def refuse(**kwargs: Any) -> dict[str, Any]:
        """Fail the delete.

        Args:
            **kwargs: The request.

        Raises:
            Exception: Always, the scripted AWS error.
        """
        raise error

    fake.batch_delete_image = refuse  # type: ignore[method-assign]

    for delete in (
        lambda: _registry(fake, URI).delete_image("u1", DIGEST),
        lambda: _registry(fake, URI).delete_cache("u1", "default"),
    ):
        with pytest.raises(ImageRegistryError) as caught:
            delete()
        assert kind in str(caught.value)
        assert "secret-detail" not in str(caught.value)
        assert caught.value.__cause__ is None


def test_delete_cache_deletes_the_cache_tag_from_the_owner_repository() -> None:
    """A cache delete names the owner's repository and the cache tag."""
    fake = FakeEcr()

    _registry(fake, URI).delete_cache("user-7", "default")

    assert fake.calls == [
        (
            "batch_delete_image",
            {
                "registryId": "123456789012",
                "repositoryName": f"{PREFIX}/user-7",
                "imageIds": [{"imageTag": "cache-default"}],
            },
        )
    ]


def test_delete_cache_ignores_a_missing_cache_and_raises_on_another_failure() -> None:
    """A cache that is gone counts as deleted; a refused delete raises with the reason."""
    _registry(FakeEcr(delete_failures=[{"failureCode": "ImageNotFound"}]), URI).delete_cache("u1", "default")
    _registry(FakeEcr(repositories=set()), URI).delete_cache("u1", "default")

    fake = FakeEcr(delete_failures=[{"failureCode": "KmsError", "failureReason": "no key"}])
    with pytest.raises(ImageRegistryError, match=r"cache-default.*no key"):
        _registry(fake, URI).delete_cache("u1", "default")
    with pytest.raises(ImageRegistryError, match="valid image tag"):
        _registry(FakeEcr(), URI).delete_cache("u1", "x" * 128)


def test_validation_refuses_a_trailing_newline() -> None:
    """An owner, digest, tag or URI that ends in a newline is refused."""
    registry = _registry(FakeEcr(), URI)

    with pytest.raises(ImageRegistryError, match="not a valid image owner"):
        registry.push_reference("alice\n", "abc")
    with pytest.raises(ImageRegistryError, match="not an image digest"):
        require_digest(DIGEST + "\n")
    with pytest.raises(ImageRegistryError, match="valid image tag"):
        registry.push_reference("alice", "abc\n")
    with pytest.raises(ImageRegistryError, match="ECR repository prefix URI"):
        _registry(FakeEcr(), URI + "\n")


def test_repository_uri_names_the_owner_repository_with_no_tag() -> None:
    """The repository URI is the prefix of each pinned image of the owner (SKY-891)."""
    registry = _registry(FakeEcr(), URI)

    assert registry.repository_uri("user-7") == f"{URI}/user-7"
    assert registry.pinned_reference("user-7", DIGEST).startswith(registry.repository_uri("user-7") + "@")
    with pytest.raises(ImageRegistryError):
        registry.repository_uri("../x")


def test_image_owner_is_a_stable_repository_name_that_hides_the_username() -> None:
    """Each user gets one valid repository name that does not show the username (SKY-891)."""
    owner = image_owner("owner@example.com")

    assert owner == image_owner("owner@example.com")
    assert owner != image_owner("other@example.com")
    assert "owner" not in owner
    assert _registry(FakeEcr(), URI).repository_name(owner) == f"{PREFIX}/{owner}"


@pytest.mark.parametrize(
    "values",
    [
        {"user_images_repository_prefix_uri": None, "image_pull_role_arn": PULL_ROLE},
        {"user_images_repository_prefix_uri": URI, "image_pull_role_arn": None},
        {"user_images_repository_prefix_uri": None, "image_pull_role_arn": None},
    ],
)
def test_image_registry_is_off_until_both_settings_are_set(values: dict[str, Any]) -> None:
    """A deployment with no registry settings, such as local mode, gets no registry (SKY-891).

    Args:
        values: The registry settings.
    """
    assert image_registry(SimpleNamespace(**values)) is None


class _FakeBoto3:
    """Record each client that the registry factory makes."""

    def __init__(self) -> None:
        """Start with no clients."""
        self.clients: list[tuple[str, dict[str, Any]]] = []

    def client(self, name: str, **options: Any) -> Any:
        """Make a fake client.

        Args:
            name: The AWS service.
            **options: The client options.

        Returns:
            A fake client of that service.
        """
        self.clients.append((name, options))
        return FakeSts() if name == "sts" else FakeEcr()


def test_image_registry_builds_the_ecr_registry_in_the_prefix_region(monkeypatch: pytest.MonkeyPatch) -> None:
    """The registry uses the worker's clients in the Region of its prefix (SKY-891).

    Args:
        monkeypatch: Pytest fixture.
    """
    fake = _FakeBoto3()
    monkeypatch.setattr(image_registry_module, "boto3", fake)

    registry = image_registry(SimpleNamespace(user_images_repository_prefix_uri=URI, image_pull_role_arn=PULL_ROLE))

    assert isinstance(registry, EcrRegistry)
    assert registry.repository_uri("user-7") == f"{URI}/user-7"
    assert fake.clients == [("ecr", {"region_name": "us-east-2"}), ("sts", {"region_name": "us-east-2"})]


def test_image_registry_refuses_a_bad_prefix_or_a_missing_boto3(monkeypatch: pytest.MonkeyPatch) -> None:
    """A set but unusable registry setting fails loudly, never as a run with no registry (SKY-891).

    Args:
        monkeypatch: Pytest fixture.
    """
    monkeypatch.setattr(image_registry_module, "boto3", _FakeBoto3())
    with pytest.raises(ImageRegistryError, match="not an ECR repository prefix URI"):
        image_registry(
            SimpleNamespace(user_images_repository_prefix_uri="registry.example/x", image_pull_role_arn=PULL_ROLE)
        )

    monkeypatch.setattr(image_registry_module, "boto3", None)
    with pytest.raises(ImageRegistryError, match="needs boto3"):
        image_registry(SimpleNamespace(user_images_repository_prefix_uri=URI, image_pull_role_arn=PULL_ROLE))


def test_a_delete_only_registry_needs_no_pull_role(monkeypatch: pytest.MonkeyPatch) -> None:
    """Account deletion gets a registry from the prefix alone, and it refuses to pull (SKY-891).

    Args:
        monkeypatch: Pytest fixture.
    """
    monkeypatch.setattr(image_registry_module, "boto3", _FakeBoto3())
    config = SimpleNamespace(user_images_repository_prefix_uri=URI, image_pull_role_arn="")

    assert image_registry(config) is None
    registry = image_registry(config, pull=False)

    assert isinstance(registry, EcrRegistry)
    with pytest.raises(ImageRegistryError, match="no pull role"):
        registry.pull_credentials("user-7")
