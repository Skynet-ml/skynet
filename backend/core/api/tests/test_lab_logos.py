"""Tests for fetching lab icons from the LobeHub icon set and the ``/models/logos`` route."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from .. import lab_logos
from ..lab_logos import ICON_CDN, LabLogoCache
from ..routers.models import create_models_router

COLOR_SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><path fill="#0866FF" d="M0 0h1v1z"/></svg>'
MONO_SVG = b'<svg fill="currentColor" xmlns="http://www.w3.org/2000/svg"><path d="M0 0h1v1z"/></svg>'


def _svg(body: bytes) -> httpx.Response:
    """Build a CDN response that carries an SVG body.

    Args:
        body: The SVG bytes.

    Returns:
        The response.
    """
    return httpx.Response(200, content=body, headers={"content-type": "image/svg+xml; charset=utf-8"})


def _client_factory(handler: Callable[[httpx.Request], httpx.Response]) -> Callable[[], httpx.Client]:
    """Build a client factory over a mock transport.

    Args:
        handler: Maps each request to its canned response.

    Returns:
        A factory for clients that never touch the network.
    """
    return lambda: httpx.Client(transport=httpx.MockTransport(handler))


def _app(cache: LabLogoCache) -> TestClient:
    """Build a test client over a models router that uses ``cache``.

    Args:
        cache: The logo cache under test.

    Returns:
        The test client.
    """
    app = FastAPI()
    app.include_router(create_models_router(logo_cache=cache))
    return TestClient(app)


def test_color_icon_is_served_as_is_with_safe_headers_and_cached() -> None:
    """Serve the full-color icon unchanged, and fetch it once."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return _svg(COLOR_SVG)

    client = _app(LabLogoCache(_client_factory(handler)))
    response = client.get("/models/logos/meta")
    assert response.status_code == 200
    assert response.content == COLOR_SVG
    assert response.headers["content-type"] == "image/svg+xml"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "public, max-age=86400"
    assert response.headers["vary"] == "Origin", "a copy cached without CORS headers must not answer the page's fetch"
    assert client.get("/models/logos/meta").content == COLOR_SVG
    assert seen == [f"{ICON_CDN}meta-color.svg"], "a second request within a day reuses the cached icon"


def test_mono_icon_is_tagged_for_the_text_color() -> None:
    """Fall back to the one-color icon and tag it, so the page paints it in the text color."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(404) if request.url.path.endswith("-color.svg") else _svg(MONO_SVG)

    body = _app(LabLogoCache(_client_factory(handler))).get("/models/logos/moonshotai").content
    assert seen == ["moonshot-color.svg", "moonshot.svg"], "the slug maps onto its LobeHub icon name"
    assert body.startswith(b'<svg data-tone="mono" fill="currentColor"')


def test_thinking_machines_uses_the_local_mark_without_a_fetch() -> None:
    """Draw Thinking Machines from its local one-color mark."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected fetch of {request.url}")

    body = _app(LabLogoCache(_client_factory(handler))).get("/models/logos/thinkingmachines").content
    assert body.startswith(b'<svg data-tone="mono"')
    assert b"<rect" in body


@pytest.mark.parametrize("slug", ["Bad_Slug", "x" * 41])
def test_invalid_slug_is_404_without_any_fetch(slug: str) -> None:
    """Fetch nothing for a slug outside the allowed shape."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected fetch of {request.url}")

    assert _app(LabLogoCache(_client_factory(handler))).get(f"/models/logos/{slug}").status_code == 404


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, html="<html>not an icon</html>"),
        httpx.Response(200, content=b"<svg>" + b"x" * (300 * 1024), headers={"content-type": "image/svg+xml"}),
        httpx.Response(200, content=b"not svg", headers={"content-type": "image/svg+xml"}),
        httpx.Response(500),
    ],
)
def test_bad_icon_responses_are_404_and_cached(response: httpx.Response) -> None:
    """Return 404 for a non-SVG, oversized or failed icon, and do not refetch."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return response

    client = _app(LabLogoCache(_client_factory(handler)))
    assert client.get("/models/logos/mistral").status_code == 404
    assert len(calls) == 2
    assert client.get("/models/logos/mistral").status_code == 404
    assert len(calls) == 2


def test_full_cache_fetches_no_new_slug_until_an_entry_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cap the cache, so random slugs cannot grow memory or the CDN calls without a bound."""
    monkeypatch.setattr(lab_logos, "_MAX_ENTRIES", 2)
    clock = [1000.0]
    monkeypatch.setattr(lab_logos.time, "monotonic", lambda: clock[0])
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return _svg(COLOR_SVG)

    cache = LabLogoCache(_client_factory(handler))
    assert cache.get("meta") is not None
    assert cache.get("mistral") is not None
    assert cache.get("stepfun") is None, "a full cache refuses a new slug"
    assert cache.get("meta") is not None, "a cached slug still serves"
    assert len(calls) == 2
    clock[0] += lab_logos.LOGO_TTL_SECONDS + 1
    assert cache.get("stepfun") is not None, "expired entries make room again"
    assert len(calls) == 3
