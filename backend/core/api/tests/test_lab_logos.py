"""Tests for fetching lab icons from the LobeHub icon set and the ``/models/logos`` route."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ..lab_logos import ICON_CDN, LabLogoCache, lab_slug
from ..routers import models as models_router
from ..routers.models import create_models_router

COLOR_SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><path fill="#0866FF" d="M0 0h1v1z"/></svg>'
MONO_SVG = b'<svg fill="currentColor" xmlns="http://www.w3.org/2000/svg"><path d="M0 0h1v1z"/></svg>'


CATALOG = SimpleNamespace(
    models=[
        SimpleNamespace(value=value)
        for value in ("openrouter/meta-llama/llama-4", "openrouter/moonshotai/kimi-k2", "mistral/mistral-large")
    ],
    providers=[SimpleNamespace(slug="openai")],
)


@pytest.fixture(autouse=True)
def _catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve a small fixed catalog, so the route knows meta, moonshotai, mistral and openai.

    Args:
        monkeypatch: Pytest's patch fixture.
    """
    monkeypatch.setattr(models_router, "get_catalog_cached", lambda: CATALOG)


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


@pytest.mark.parametrize("slug", ["Bad_Slug", "x" * 41, "not-a-catalog-lab"])
def test_invalid_slug_is_404_without_any_fetch(slug: str) -> None:
    """Fetch nothing for a malformed slug or a lab the catalog does not know."""

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


@pytest.mark.parametrize(
    ("model_id", "slug"),
    [
        ("openrouter/meta-llama/llama-4", "meta"),
        ("openrouter/x-ai/grok-4", "xai"),
        ("anthropic/claude-haiku-5-5", "anthropic"),
        ("subscription/codex/openai/gpt-5", "openai"),
        ("openrouter/auto", "openrouter"),
    ],
)
def test_lab_slug_matches_the_frontend_rule(model_id: str, slug: str) -> None:
    """Derive the same lab slug as the frontend's ``modelProviderSlug``."""
    assert lab_slug(model_id) == slug
