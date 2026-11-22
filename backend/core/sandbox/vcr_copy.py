"""Copy a user image from its OCI registry into Vercel's registry (SKY-898).

Vercel Sandbox starts a box only from an image in the project's Vercel
Container Registry (VCR). The worker runs with no Docker daemon, so this copier
moves the image with plain OCI Distribution calls: it reads the manifest and
blobs from the source registry and pushes them to VCR by digest. The manifest
bytes do not change, so the copy keeps the source digest.

VCR takes HTTP Basic auth with the user ``oidc`` and a project OIDC token. The
Vercel CLI gets the same token from ``POST /projects/<id>/token`` in
``vercel vcr login``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from typing import Any
from urllib.parse import urljoin

import httpx

from .image_registry import _IMAGE_INDEX_TYPES, _IMAGE_MANIFEST_TYPES, require_digest

VCR_HOST = "vcr.vercel.com"
_API = "https://api.vercel.com"
_REFERENCE = re.compile(
    r"^(?P<host>[a-z0-9.-]+(?::\d+)?)/(?P<name>[a-z0-9]+(?:[._/-][a-z0-9]+)*)@(?P<digest>sha256:[0-9a-f]{64})$"
)
_ACCEPT = ", ".join((*_IMAGE_MANIFEST_TYPES, *_IMAGE_INDEX_TYPES))
# A manifest is a few kilobytes; the cap stops a huge body before it is parsed.
_MANIFEST_MAX_BYTES = 4 << 20
# A layer of a 20 GB image can stream for many minutes, so only the connect
# and pool waits are short. The provider ends the whole copy at COPY_TIMEOUT.
_TIMEOUT = httpx.Timeout(300.0, connect=30.0, pool=30.0)
# VCR keeps a new image in ``preparing`` while it processes it; a box starts
# only from a ``ready`` or ``unoptimized`` image.
_PREPARING = "preparing"
_POLL_SECONDS = 5.0
# Vercel Sandbox runs linux/amd64 boxes.
_PLATFORM = {"os": "linux", "architecture": "amd64"}


class VcrCopier:
    """Copy one digest-pinned image into the Vercel project's registry.

    Each copy reads the team slug and project name, and gets a new project
    OIDC token. A copy is rare, because the provider reuses each copy.
    """

    def __init__(
        self, token: str, team_id: str, project_id: str, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        """Store the Vercel scope of the copies.

        Args:
            token: The Vercel access token of the control plane.
            team_id: The Vercel team that owns the project.
            project_id: The Vercel project whose registry gets the copies.
            transport: The HTTP transport; tests pass a mock one.
        """
        self._token = token
        self._team_id = team_id
        self._project_id = project_id
        self._transport = transport

    def __repr__(self) -> str:
        """Return a representation without the token.

        Returns:
            The team and project of the copier.
        """
        return f"VcrCopier(team_id={self._team_id!r}, project_id={self._project_id!r})"

    async def __call__(self, source_image: str, pull_credentials: tuple[str, str] | None) -> str:
        """Copy the source image into VCR and return the copy's reference.

        An index copies only its linux/amd64 image, because a box runs only that
        platform. The copy then has the digest of that platform image.

        Args:
            source_image: ``<host>/<name>@sha256:<digest>`` in the source registry.
            pull_credentials: The (username, password) that read the source image.

        Returns:
            ``vcr.vercel.com/<team>/<project>/user-<owner>@sha256:<digest>``.

        Raises:
            ValueError: The reference, a manifest or the index is not in a known form.
            httpx.HTTPError: A registry or Vercel API call failed.
        """
        match = _REFERENCE.fullmatch(source_image)
        if match is None:
            raise ValueError("The source image is not a digest-pinned reference.")
        source_auth = httpx.BasicAuth(*pull_credentials) if pull_credentials else None
        source = f"https://{match['host']}/v2/{match['name']}"
        async with httpx.AsyncClient(transport=self._transport, timeout=_TIMEOUT, follow_redirects=True) as http:
            team, project, oidc = await self._scope(http)
            repository = "user-" + match["name"].rsplit("/", 1)[-1]
            target_name = f"{team}/{project}/{repository}"
            target = f"https://{VCR_HOST}/v2/{target_name}"
            target_auth = httpx.BasicAuth("oidc", oidc)
            digest = match["digest"]
            media_type, body = await _manifest(http, source, digest, source_auth)
            if media_type in _IMAGE_INDEX_TYPES:
                digest = _platform_image(json.loads(body))
                media_type, body = await _manifest(http, source, digest, source_auth)
            if media_type not in _IMAGE_MANIFEST_TYPES:
                raise ValueError("The source image has a manifest of an unknown type.")
            manifest = json.loads(body)
            for blob in (manifest["config"], *manifest["layers"]):
                await _copy_blob(http, source, source_auth, target, target_auth, blob)
            response = await http.put(
                f"{target}/manifests/{digest}", content=body, headers={"Content-Type": media_type}, auth=target_auth
            )
            response.raise_for_status()
            await self._wait_ready(http, repository, digest)
        return f"{VCR_HOST}/{target_name}@{digest}"

    def _api(self, http: httpx.AsyncClient, method: str, path: str, **kwargs: Any) -> Any:
        """Start one Vercel API call in the copier's team.

        Args:
            http: The HTTP client.
            method: The HTTP method.
            path: The API path.
            **kwargs: Other request arguments.

        Returns:
            The awaitable response.
        """
        params = {"teamId": self._team_id, **kwargs.pop("params", {})}
        headers = {"Authorization": f"Bearer {self._token}"}
        return http.request(method, _API + path, params=params, headers=headers, **kwargs)

    async def _scope(self, http: httpx.AsyncClient) -> tuple[str, str, str]:
        """Read the registry path of the project and get a push token.

        Args:
            http: The HTTP client.

        Returns:
            The team slug, the project name and the project OIDC token.

        Raises:
            httpx.HTTPError: A Vercel API call failed.
            KeyError: A response has no expected field.
        """
        team, project, token = await asyncio.gather(
            self._api(http, "GET", f"/v2/teams/{self._team_id}"),
            self._api(http, "GET", f"/v9/projects/{self._project_id}"),
            self._api(http, "POST", f"/projects/{self._project_id}/token", json={"source": "skynet"}),
        )
        for response in (team, project, token):
            response.raise_for_status()
        return str(team.json()["slug"]), str(project.json()["name"]), str(token.json()["token"])

    async def _wait_ready(self, http: httpx.AsyncClient, repository: str, digest: str) -> None:
        """Wait until VCR lists the pushed image and no longer prepares it.

        The provider ends the copy at COPY_TIMEOUT, so the wait has no limit of its own.

        Args:
            http: The HTTP client.
            repository: The VCR repository name.
            digest: The pushed manifest digest.

        Raises:
            httpx.HTTPError: A Vercel API call failed.
        """
        while True:
            response = await self._api(
                http, "GET", f"/v1/vcr/repository/{repository}/images", params={"projectId": self._project_id}
            )
            response.raise_for_status()
            images = [i for i in response.json().get("images") or [] if i.get("manifestDigest") == digest]
            if images and images[0].get("status") != _PREPARING:
                return
            await asyncio.sleep(_POLL_SECONDS)


async def _manifest(
    http: httpx.AsyncClient, repository: str, digest: str, auth: httpx.Auth | None
) -> tuple[str, bytes]:
    """Read one manifest by digest and check its bytes.

    Args:
        http: The HTTP client.
        repository: The ``https://<host>/v2/<name>`` base of the repository.
        digest: The manifest digest.
        auth: The registry auth.

    Returns:
        The manifest's media type and its exact bytes.

    Raises:
        ValueError: The manifest is too large or does not match its digest.
        httpx.HTTPError: The registry call failed.
    """
    kwargs: dict[str, Any] = {"headers": {"Accept": _ACCEPT}}
    if auth is not None:
        kwargs["auth"] = auth
    response = await http.get(f"{repository}/manifests/{digest}", **kwargs)
    response.raise_for_status()
    body = response.content
    if len(body) > _MANIFEST_MAX_BYTES:
        raise ValueError("The source manifest is too large.")
    if "sha256:" + hashlib.sha256(body).hexdigest() != digest:
        raise ValueError("The source manifest does not match its digest.")
    media_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip()
    return media_type or str(json.loads(body).get("mediaType") or ""), body


def _platform_image(index: Any) -> str:
    """Return the digest of the linux/amd64 image of an index.

    Args:
        index: The index JSON.

    Returns:
        The digest of the platform image.

    Raises:
        ValueError: The index has no linux/amd64 image, or more than one.
    """
    children = index.get("manifests") if isinstance(index, dict) else None
    matches = [
        child["digest"]
        for child in children or []
        if isinstance(child, dict)
        and isinstance(child.get("platform"), dict)
        and all(child["platform"].get(key) == value for key, value in _PLATFORM.items())
    ]
    if len(matches) != 1:
        raise ValueError(f"The source index has {len(matches)} linux/amd64 images, not one.")
    return require_digest(str(matches[0]))


async def _copy_blob(
    http: httpx.AsyncClient,
    source: str,
    source_auth: httpx.Auth | None,
    target: str,
    target_auth: httpx.Auth,
    descriptor: Any,
) -> None:
    """Stream one blob from the source repository into VCR, unless VCR has it.

    The source registry can redirect the download to object storage; httpx
    drops the Authorization header on a redirect to another origin.

    Args:
        http: The HTTP client.
        source: The ``https://<host>/v2/<name>`` base of the source repository.
        source_auth: The source registry auth.
        target: The ``https://vcr.vercel.com/v2/<name>`` base of the copy.
        target_auth: The VCR auth.
        descriptor: The blob's descriptor in the manifest.

    Raises:
        ValueError: The descriptor has no valid digest or size.
        httpx.HTTPError: A registry call failed.
    """
    digest = require_digest(str(descriptor["digest"]))
    size = descriptor["size"]
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise ValueError("A source blob has no valid size.")
    found = await http.head(f"{target}/blobs/{digest}", auth=target_auth)
    if found.status_code == httpx.codes.OK:
        return
    started = await http.post(f"{target}/blobs/uploads/", auth=target_auth)
    started.raise_for_status()
    location = urljoin(f"{target}/blobs/uploads/", started.headers["Location"])
    upload = f"{location}{'&' if '?' in location else '?'}digest={digest}"
    kwargs: dict[str, Any] = {} if source_auth is None else {"auth": source_auth}
    async with http.stream("GET", f"{source}/blobs/{digest}", **kwargs) as blob:
        blob.raise_for_status()
        response = await http.put(
            upload,
            content=blob.aiter_raw(),
            headers={"Content-Type": "application/octet-stream", "Content-Length": str(size)},
            auth=target_auth,
        )
    response.raise_for_status()
