"""Tests for ``core.sandbox.vcr_copy``. A fake registry and API on httpx.MockTransport: no network."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from core.sandbox import vcr_copy
from core.sandbox.image_registry import ImageRegistryError
from core.sandbox.vcr_copy import VcrCopier, delete_copies

_SOURCE_HOST = "123456789012.dkr.ecr.us-east-2.amazonaws.com"
_SOURCE_NAME = "skynet-user-images/u-owner"
_TARGET_NAME = "team-slug/project-name/user-u-owner"


def _digest(body: bytes) -> str:
    """Return the sha256 digest of some bytes.

    Args:
        body: The bytes.

    Returns:
        ``sha256:<hex>``.
    """
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _basic(username: str, password: str) -> str:
    """Return the Basic Authorization header value.

    Args:
        username: The user name.
        password: The password.

    Returns:
        The header value.
    """
    return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()


async def _chunks(body: bytes) -> AsyncIterator[bytes]:
    """Yield a body in two chunks.

    Args:
        body: The body.

    Yields:
        The chunks.
    """
    yield body[:3]
    yield body[3:]


class FakeRegistries:
    """Serve a source registry, an object store, the Vercel API and VCR."""

    def __init__(self, *, existing: frozenset[str] = frozenset(), preparing: int = 0) -> None:
        """Build an image with one config and two layers.

        Args:
            existing: Blob digests that VCR already holds.
            preparing: How many image reads report ``preparing`` before ``ready``.
        """
        self.config = b'{"architecture":"amd64","os":"linux"}'
        self.layers = [b"layer-one", b"layer-two"]
        self.blobs = {_digest(b): b for b in [self.config, *self.layers]}
        self.manifest = json.dumps(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": {"digest": _digest(self.config), "size": len(self.config)},
                "layers": [{"digest": _digest(b), "size": len(b)} for b in self.layers],
            }
        ).encode()
        self.manifests: dict[str, tuple[str, bytes]] = {
            _digest(self.manifest): ("application/vnd.oci.image.manifest.v1+json", self.manifest)
        }
        self.pushed_blobs: dict[str, bytes] = {d: self.blobs[d] for d in existing}
        self.pushed_manifests: dict[str, tuple[str, bytes]] = {}
        self.preparing = preparing
        self.requests: list[httpx.Request] = []

    def add_index(self, children: list[dict[str, Any]]) -> str:
        """Add an index of platform images to the source registry.

        Args:
            children: The index's manifest descriptors.

        Returns:
            The index digest.
        """
        body = json.dumps({"schemaVersion": 2, "manifests": children}).encode()
        self.manifests[_digest(body)] = ("application/vnd.oci.image.index.v1+json", body)
        return _digest(body)

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Answer one request.

        Args:
            request: The request.

        Returns:
            The fake response.
        """
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        if host == "api.vercel.com":
            assert request.headers["Authorization"] == "Bearer vercel-token"
            assert request.url.params["teamId"] == "team_1"
            if path == "/v2/teams/team_1":
                return httpx.Response(200, json={"slug": "team-slug"})
            if path == "/v9/projects/prj_1":
                return httpx.Response(200, json={"name": "project-name"})
            if path == "/projects/prj_1/token":
                return httpx.Response(200, json={"token": "oidc-token"})
            if path == "/v1/vcr/repository/user-u-owner/images":
                assert request.url.params["projectId"] == "prj_1"
                status = "preparing" if self.preparing else "ready"
                self.preparing = max(0, self.preparing - 1)
                images = [{"manifestDigest": d, "status": status} for d in self.pushed_manifests]
                return httpx.Response(200, json={"images": images})
        if host == _SOURCE_HOST:
            assert request.headers["Authorization"] == _basic("AWS", "pull-token")
            kind, digest = path.removeprefix(f"/v2/{_SOURCE_NAME}/").split("/")
            if kind == "manifests" and digest in self.manifests:
                media_type, body = self.manifests[digest]
                return httpx.Response(200, content=body, headers={"Content-Type": media_type})
            if kind == "blobs" and digest in self.blobs:
                return httpx.Response(307, headers={"Location": f"https://bucket.s3.amazonaws.com/{digest}?sig=x"})
        if host == "bucket.s3.amazonaws.com":
            assert "Authorization" not in request.headers
            # An unread stream, as a real download is: bytes content is read at once.
            return httpx.Response(200, content=_chunks(self.blobs[path.lstrip("/")]))
        if host == vcr_copy.VCR_HOST:
            assert request.headers["Authorization"] == _basic("oidc", "oidc-token")
            rest = path.removeprefix(f"/v2/{_TARGET_NAME}/")
            if request.method == "HEAD":
                return httpx.Response(200 if rest.split("/")[-1] in self.pushed_blobs else 404)
            if request.method == "POST" and rest == "blobs/uploads/":
                return httpx.Response(202, headers={"Location": f"/v2/{_TARGET_NAME}/blobs/uploads/u1?state=s"})
            if request.method == "PUT" and rest.startswith("blobs/uploads/u1"):
                body = request.read()
                assert request.url.params["state"] == "s"
                assert _digest(body) == request.url.params["digest"]
                assert request.headers["Content-Length"] == str(len(body))
                self.pushed_blobs[request.url.params["digest"]] = body
                return httpx.Response(201)
            if request.method == "PUT" and rest.startswith("manifests/"):
                self.pushed_manifests[rest.split("/")[1]] = (request.headers["Content-Type"], request.read())
                return httpx.Response(201)
        return httpx.Response(599, text=f"unexpected {request.method} {request.url}")


def _copy(fake: FakeRegistries, digest: str, credentials: tuple[str, str] | None = ("AWS", "pull-token")) -> str:
    """Run one copy against the fake registries.

    Args:
        fake: The fake registries.
        digest: The source digest.
        credentials: The source pull credentials.

    Returns:
        The copy's reference.
    """
    copier = VcrCopier("vercel-token", "team_1", "prj_1", transport=httpx.MockTransport(fake.handler))
    return asyncio.run(copier(f"{_SOURCE_HOST}/{_SOURCE_NAME}@{digest}", credentials))


@pytest.fixture(autouse=True)
def _no_poll_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the readiness poll return at once.

    Args:
        monkeypatch: Pytest fixture.
    """
    monkeypatch.setattr(vcr_copy, "_POLL_SECONDS", 0.0)


def test_copy_pushes_blobs_and_manifest_and_keeps_the_digest() -> None:
    """Check that a copy pushes every blob and the exact manifest bytes by digest."""
    fake = FakeRegistries(preparing=2)
    digest = _digest(fake.manifest)
    assert _copy(fake, digest) == f"{vcr_copy.VCR_HOST}/{_TARGET_NAME}@{digest}"
    assert fake.pushed_blobs == fake.blobs
    assert fake.pushed_manifests == {digest: ("application/vnd.oci.image.manifest.v1+json", fake.manifest)}
    reads = [r for r in fake.requests if r.url.path.endswith("/images")]
    assert len(reads) == 3


def test_copy_skips_blobs_that_vcr_holds() -> None:
    """Check that a blob VCR already holds is neither read nor uploaded."""
    held = _digest(b"layer-one")
    fake = FakeRegistries(existing=frozenset({held}))
    _copy(fake, _digest(fake.manifest))
    assert not [r for r in fake.requests if r.url.path.endswith(f"/blobs/{held}") and r.method == "GET"]
    uploads = [r for r in fake.requests if r.method == "POST" and r.url.host == vcr_copy.VCR_HOST]
    assert len(uploads) == 2


def test_copy_of_an_index_copies_only_the_amd64_image() -> None:
    """Check that an index copies its linux/amd64 image and returns that digest."""
    fake = FakeRegistries()
    child = _digest(fake.manifest)
    index = fake.add_index(
        [
            {"digest": "sha256:" + "b" * 64, "platform": {"os": "linux", "architecture": "arm64"}},
            {"digest": child, "platform": {"os": "linux", "architecture": "amd64"}},
            {"digest": "sha256:" + "c" * 64, "platform": {"os": "unknown", "architecture": "unknown"}},
        ]
    )
    assert _copy(fake, index).endswith(f"@{child}")
    assert list(fake.pushed_manifests) == [child]


def test_copy_refuses_an_index_with_no_amd64_image() -> None:
    """Check that an index without a linux/amd64 image stops before any push."""
    fake = FakeRegistries()
    index = fake.add_index([{"digest": "sha256:" + "b" * 64, "platform": {"os": "linux", "architecture": "arm64"}}])
    with pytest.raises(ValueError, match="0 linux/amd64"):
        _copy(fake, index)
    assert not fake.pushed_blobs


def test_copy_refuses_a_manifest_that_does_not_match_its_digest() -> None:
    """Check that changed manifest bytes stop the copy."""
    fake = FakeRegistries()
    digest = _digest(fake.manifest)
    fake.manifests[digest] = ("application/vnd.oci.image.manifest.v1+json", fake.manifest + b" ")
    with pytest.raises(ValueError, match="does not match"):
        _copy(fake, digest)


def test_copy_refuses_a_reference_without_a_digest() -> None:
    """Check that a tag reference stops the copy before any request."""
    fake = FakeRegistries()
    copier = VcrCopier("vercel-token", "team_1", "prj_1", transport=httpx.MockTransport(fake.handler))
    with pytest.raises(ValueError, match="digest-pinned"):
        asyncio.run(copier(f"{_SOURCE_HOST}/{_SOURCE_NAME}:latest", None))
    assert not fake.requests


def test_copy_raises_on_a_refused_push() -> None:
    """Check that a VCR refusal raises an HTTP error."""
    fake = FakeRegistries()

    def refuse(request: httpx.Request) -> httpx.Response:
        """Refuse every VCR call.

        Args:
            request: The request.

        Returns:
            The fake response.
        """
        if request.url.host == vcr_copy.VCR_HOST:
            return httpx.Response(401)
        return fake.handler(request)

    copier = VcrCopier("vercel-token", "team_1", "prj_1", transport=httpx.MockTransport(refuse))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(copier(f"{_SOURCE_HOST}/{_SOURCE_NAME}@{_digest(fake.manifest)}", ("AWS", "pull-token")))


def test_repr_hides_the_token() -> None:
    """Check that the copier's representation does not show the Vercel token."""
    assert "vercel-token" not in repr(VcrCopier("vercel-token", "team_1", "prj_1"))


_CONFIG = SimpleNamespace(vercel_token=SecretStr("vercel-token"), vercel_team_id="team_1", vercel_project_id="prj_1")


@pytest.mark.parametrize("status", [200, 404])
def test_delete_copies_deletes_the_owner_repository(status: int) -> None:
    """Check that the delete names the owner's repository, and that a missing one counts as deleted.

    Args:
        status: The Vercel API answer.
    """
    requests: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        """Record the delete.

        Args:
            request: The request.

        Returns:
            The fake response.
        """
        requests.append(request)
        return httpx.Response(status)

    delete_copies("u-owner", _CONFIG, transport=httpx.MockTransport(answer))
    (request,) = requests
    assert request.method == "DELETE"
    assert request.url.path == "/v1/vcr/repository/user-u-owner"
    assert dict(request.url.params) == {"teamId": "team_1", "projectId": "prj_1"}
    assert request.headers["Authorization"] == "Bearer vercel-token"


def test_delete_copies_raises_on_a_refused_delete() -> None:
    """Check that a Vercel refusal raises a registry error."""
    transport = httpx.MockTransport(lambda request: httpx.Response(500))
    with pytest.raises(ImageRegistryError, match="status 500"):
        delete_copies("u-owner", _CONFIG, transport=transport)


def test_delete_copies_hides_the_request_on_a_network_failure() -> None:
    """Check that a network failure raises a registry error that names no team or project."""

    def fail(request: httpx.Request) -> httpx.Response:
        """Fail the connection.

        Args:
            request: The request.

        Raises:
            httpx.ConnectError: Always.
        """
        raise httpx.ConnectError(f"cannot reach {request.url}")

    with pytest.raises(ImageRegistryError) as caught:
        delete_copies("u-owner", _CONFIG, transport=httpx.MockTransport(fail))
    assert "team_1" not in str(caught.value)
    assert "prj_1" not in str(caught.value)
    assert caught.value.__cause__ is None


def test_delete_copies_makes_no_call_without_vercel_settings() -> None:
    """Check that a deployment without the Vercel settings sends no request."""

    def refuse(request: httpx.Request) -> httpx.Response:
        """Fail the test on any request.

        Args:
            request: The request.

        Raises:
            AssertionError: Always.
        """
        raise AssertionError(request.url)

    config = SimpleNamespace(vercel_token=None, vercel_team_id="team_1", vercel_project_id="prj_1")
    delete_copies("u-owner", config, transport=httpx.MockTransport(refuse))
