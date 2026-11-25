"""Fetch each model lab's icon from the LobeHub AI icon set, for the model picker.

Bundled brand marks go stale when a lab redraws its logo. LobeHub keeps a
maintained SVG set of AI brands, so this module reads its latest release from
a CDN and keeps the bytes in memory for a day. Each brand has one file for
both themes. A one-color mark carries ``data-tone="mono"``, so the frontend
paints it in the text color and it reads on light and dark backgrounds. The
frontend falls back to its bundled mark when a fetch fails.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

# The newest release of LobeHub's static SVG set. A redrawn logo reaches the
# picker without a Skynet deploy.
ICON_CDN = "https://cdn.jsdelivr.net/npm/@lobehub/icons-static-svg@latest/icons/"

# Provider slug -> LobeHub icon name, only where the two differ. Any other slug
# is looked up under its own name, so a new lab needs no entry here.
ICON_NAMES: dict[str, str] = {
    "google": "gemini",
    "moonshotai": "moonshot",
    "xiaomi": "xiaomimimo",
    "friendliai": "friendli",
    "gmi": "gmicloud",
}

# Labs that LobeHub lacks. Thinking Machines has no symbol logo; its favicon is
# a plain rounded square, drawn here in one color.
LOCAL_MARKS: dict[str, bytes] = {
    "thinkingmachines": (
        b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor">'
        b'<rect x="3" y="3" width="18" height="18" rx="3.5"/></svg>'
    ),
}

# OpenRouter vendor segments spelled differently from the brand slug. This
# mirrors ``BRAND_SLUGS`` in ``frontend/src/shared/lib/model-provider.ts``.
_BRAND_SLUGS: dict[str, str] = {
    "x-ai": "xai",
    "z-ai": "zai",
    "mistralai": "mistral",
    "meta-llama": "meta",
}

LOGO_TTL_SECONDS = 24 * 60 * 60
# A failed fetch retries sooner, so a short outage does not hide a logo all day.
_FAILURE_TTL_SECONDS = 60 * 60
_MAX_ICON_BYTES = 256 * 1024
_SLUG = re.compile(r"[a-z0-9-]{1,40}")
_SVG_OPEN = re.compile(rb"<svg\b")


def lab_slug(model_id: str) -> str:
    """Return a model's lab slug, the same way the frontend's ``modelProviderSlug`` does.

    Args:
        model_id: A routed model id, for example ``openrouter/meta-llama/llama-4``.

    Returns:
        The lab slug, for example ``meta``.
    """
    parts = (model_id.split("/")[2:] if model_id.startswith("subscription/") else model_id.split("/")) or [model_id]
    slug = parts[1] if parts[0] == "openrouter" and len(parts) > 2 else parts[0]
    return _BRAND_SLUGS.get(slug, slug)


@dataclass(frozen=True)
class LabLogo:
    """One fetched icon: its SVG bytes, and whether it is a one-color mark."""

    content: bytes
    mono: bool

    def svg(self) -> bytes:
        """Return the SVG to serve, with one-color marks tagged for the frontend.

        Returns:
            The SVG bytes; a one-color mark gets ``data-tone="mono"`` on its root.
        """
        if not self.mono:
            return self.content
        return _SVG_OPEN.sub(b'<svg data-tone="mono"', self.content, count=1)


def default_logo_client() -> httpx.Client:
    """Build the client that fetches icons from the CDN.

    Returns:
        A client with a short timeout that does not follow redirects.
    """
    return httpx.Client(timeout=5, follow_redirects=False, trust_env=False)


def _fetch_svg(client: httpx.Client, url: str) -> bytes | None:
    """GET one SVG and accept it only when it is small and really SVG.

    Args:
        client: The HTTP client.
        url: The icon URL.

    Returns:
        The SVG bytes, or None on an error status, a transport error, a body
        over the cap, or a body that is not SVG.
    """
    try:
        with client.stream("GET", url) as response:
            if response.status_code != 200:
                return None
            if response.headers.get("content-type", "").split(";")[0].strip().lower() != "image/svg+xml":
                return None
            body = b""
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > _MAX_ICON_BYTES:
                    return None
    except httpx.HTTPError:
        return None
    return body if _SVG_OPEN.search(body) else None


def fetch_lab_logo(client: httpx.Client, name: str) -> LabLogo | None:
    """Fetch a brand's full-color icon, or its one-color icon when it has none.

    Args:
        client: The HTTP client.
        name: The LobeHub icon name, for example ``meta``.

    Returns:
        The logo, or None when LobeHub has no icon under that name.
    """
    color = _fetch_svg(client, f"{ICON_CDN}{name}-color.svg")
    if color is not None:
        return LabLogo(content=color, mono=False)
    mono = _fetch_svg(client, f"{ICON_CDN}{name}.svg")
    return LabLogo(content=mono, mono=True) if mono is not None else None


class LabLogoCache:
    """Hold fetched logos in memory, one fetch per slug per day."""

    def __init__(self, make_client: Callable[[], httpx.Client] = default_logo_client) -> None:
        """Create an empty cache.

        Args:
            make_client: Factory for the fetch client; tests pass a mock transport.
        """
        self._make_client = make_client
        self._entries: dict[str, tuple[float, LabLogo | None]] = {}
        self._lock = threading.Lock()

    def get(self, slug: str) -> LabLogo | None:
        """Return the logo for ``slug``, fetching it when the cache is cold.

        Args:
            slug: The frontend provider slug.

        Returns:
            The logo, or None for an invalid slug or a failed fetch.
        """
        if not _SLUG.fullmatch(slug):
            return None
        if slug in LOCAL_MARKS:
            return LabLogo(content=LOCAL_MARKS[slug], mono=True)
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(slug)
        if entry is not None and entry[0] > now:
            return entry[1]
        with self._make_client() as client:
            logo = fetch_lab_logo(client, ICON_NAMES.get(slug, slug))
        ttl = LOGO_TTL_SECONDS if logo is not None else _FAILURE_TTL_SECONDS
        with self._lock:
            self._entries[slug] = (now + ttl, logo)
        return logo
