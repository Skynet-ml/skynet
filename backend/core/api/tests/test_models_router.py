"""Tests for the ``/models``, ``/models/discover`` and ``/models/call-holds`` endpoints."""

from __future__ import annotations

import urllib.error
from decimal import Decimal
from email.message import Message
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ...billing.operation_pricing import ChargePolicy
from ...billing.vercel_usage import sandbox_hold_cents
from ...local.cli_catalog import CLI_MODELS
from ...local.cli_probe import clear_probe_cache
from ...local.tests.test_cli_probe import CLAUDE_SUBSCRIPTION, _fake_cli
from ...local.tests.test_cli_probe import _settings as _probe_settings
from ...service_gateway.optimization.blackbox.runner import DEFAULT_MAX_TOKENS
from ...service_gateway.optimization.blackbox.sandbox import COVERAGE_SLICE_SECONDS
from .. import model_router
from ..model_catalog import CatalogModel, CatalogProvider, ModelCatalogResponse
from ..routers import models as models_router_module
from ..routers.models import _validate_discover_url, create_models_router
from .conftest import bypass_auth
from .test_local_mode import _local_submit_client, _run_payload


def _make_catalog(*model_ids: str) -> ModelCatalogResponse:
    """Build a synthetic ``ModelCatalogResponse`` from a list of model ids.

    Args:
        *model_ids: Slash-prefixed model ids to include in the catalog.

    Returns:
        A ``ModelCatalogResponse`` with one OpenAI provider and the given models.
    """
    models = [
        CatalogModel(value=mid, label=mid.split("/")[-1], provider=mid.split("/")[0] if "/" in mid else "openai")
        for mid in model_ids
    ]
    providers = [CatalogProvider(slug="openai", label="OpenAI", has_env_key=True)]
    return ModelCatalogResponse(providers=providers, models=models)


@pytest.fixture
def models_client() -> TestClient:
    """Build a ``TestClient`` exposing only the models router.

    Returns:
        A ``TestClient`` over a minimal FastAPI app.
    """
    app = FastAPI()
    app.include_router(create_models_router())
    bypass_auth(app)
    return TestClient(app, raise_server_exceptions=False)


def test_list_models_returns_200_with_catalog_shape(models_client: TestClient) -> None:
    """``/models`` returns the cached catalog with a ``models``/``providers`` shape."""
    fake_catalog = _make_catalog("openai/gpt-4o-mini", "anthropic/claude-3-5-haiku-20241022")

    with patch("core.api.routers.models.get_catalog_cached", return_value=fake_catalog):
        resp = models_client.get("/models")

    assert resp.status_code == 200
    body = resp.json()
    assert "models" in body
    assert "providers" in body
    assert len(body["models"]) == 2


def test_list_models_returns_model_fields(models_client: TestClient) -> None:
    """Each returned model carries ``value``, ``label``, and ``provider``."""
    fake_catalog = _make_catalog("openai/gpt-4o")

    with patch("core.api.routers.models.get_catalog_cached", return_value=fake_catalog):
        resp = models_client.get("/models")

    assert resp.status_code == 200
    model = resp.json()["models"][0]
    assert model["value"] == "openai/gpt-4o"
    assert "label" in model
    assert "provider" in model
    assert model["zero_retention"] is True


def test_list_models_serializes_per_data_center_entries(models_client: TestClient) -> None:
    """A multi-DC provider round-trips one entry per data center via ``GET /models``."""
    models = [
        CatalogModel(value="openai/gpt-4o", label="gpt-4o", provider="openai"),
        CatalogModel(
            value="openai/gpt-4o",
            label="gpt-4o",
            provider="openai",
            data_center="EU",
        ),
    ]
    providers = [
        CatalogProvider(
            slug="openai",
            label="OpenAI",
            default_base_url="https://api.openai.com/v1",
            has_env_key=True,
        ),
        CatalogProvider(
            slug="openai",
            label="OpenAI",
            data_center="EU",
            default_base_url="https://eu.api.openai.com/v1",
            has_env_key=True,
        ),
    ]
    fake_catalog = ModelCatalogResponse(providers=providers, models=models)

    with patch("core.api.routers.models.get_catalog_cached", return_value=fake_catalog):
        resp = models_client.get("/models")

    assert resp.status_code == 200
    body = resp.json()
    dcs = sorted((p.get("data_center") or "") for p in body["providers"] if p["slug"] == "openai")
    assert dcs == ["", "EU"]
    regional = next(p for p in body["providers"] if p["slug"] == "openai" and p.get("data_center") == "EU")
    assert regional["default_base_url"] == "https://eu.api.openai.com/v1"
    assert any(m.get("data_center") == "EU" for m in body["models"])


def test_list_models_empty_catalog_returns_empty_lists(models_client: TestClient) -> None:
    """An empty catalog round-trips as empty ``models`` and ``providers`` lists."""
    fake_catalog = ModelCatalogResponse(providers=[], models=[])

    with patch("core.api.routers.models.get_catalog_cached", return_value=fake_catalog):
        resp = models_client.get("/models")

    assert resp.status_code == 200
    body = resp.json()
    assert body["models"] == []
    assert body["providers"] == []


def test_discover_models_happy_path_returns_model_list(models_client: TestClient) -> None:
    """Discovery successfully parses ids out of a Llama-stack ``/data`` payload."""
    fake_response_body = b'{"data": [{"id": "llama-3"}, {"id": "mistral-7b"}]}'

    class _FakeResp:
        """Context-manager stand-in for ``urllib.request.urlopen``."""

        @staticmethod
        def read():
            """Return the canned response body."""
            return fake_response_body

        def __enter__(self):
            """Enter the context manager."""
            return self

        def __exit__(self, *args):
            """Exit the context manager."""

    with patch("core.api.routers.models._open_pinned", return_value=_FakeResp()):
        resp = models_client.post("/models/discover", json={"base_url": "http://localhost:11434"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["error"] is None
    assert "llama-3" in body["models"]
    assert "mistral-7b" in body["models"]


def test_discover_models_invalid_body_returns_422(models_client: TestClient) -> None:
    """The discover endpoint requires ``base_url`` in the body."""
    resp = models_client.post("/models/discover", json={})

    assert resp.status_code == 422


def test_discover_models_url_error_returns_200_with_error(models_client: TestClient) -> None:
    """Connection errors surface as a 200 with an error message and no models."""
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")):
        resp = models_client.post("/models/discover", json={"base_url": "http://nowhere.invalid"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["models"] == []
    assert body["error"] is not None


def test_discover_models_404_fallback_exhausted_returns_error(models_client: TestClient) -> None:
    """A 404 from every fallback path returns a 200 with an error message."""
    http_error = urllib.error.HTTPError(
        url="http://localhost:8080",
        code=404,
        msg="Not Found",
        hdrs=Message(),
        fp=None,
    )
    with patch("core.api.routers.models._open_pinned", side_effect=http_error):
        resp = models_client.post("/models/discover", json={"base_url": "http://localhost:8080"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["models"] == []
    assert body["error"] is not None


def test_validate_discover_url_pins_resolved_ip_for_loopback() -> None:
    """A resolvable host returns a pinned IP and no error."""
    _, pinned_ip, error = _validate_discover_url("http://localhost:11434")
    assert error is None
    assert pinned_ip in {"127.0.0.1", "::1"}


def test_validate_discover_url_blocks_metadata_ip() -> None:
    """The cloud metadata service address is refused with no pinned IP."""
    _, pinned_ip, error = _validate_discover_url("http://169.254.169.254/latest")
    assert pinned_ip is None
    assert error is not None


_PRICED_MODEL = "openai/gpt-test"
_ENDPOINTS = {
    "data": {
        "id": _PRICED_MODEL,
        "endpoints": [
            {
                "tag": "openai",
                "provider_name": "OpenAI",
                "context_length": 1000,
                "pricing": {"prompt": "0.000001", "completion": "0.000002", "input_cache_write_1h": "0.000003"},
            }
        ],
    }
}


def _holds_client(seen: list[str]) -> TestClient:
    """Build a models router whose OpenRouter reads hit a mock catalog.

    Args:
        seen: Collects each requested catalog path.

    Returns:
        A ``TestClient`` over the models router with auth bypassed.
    """

    def provider(request: httpx.Request) -> httpx.Response:
        """Serve the one priced model and refuse the rest."""
        seen.append(request.url.path)
        if request.url.path == f"/api/v1/models/{_PRICED_MODEL}/endpoints":
            return httpx.Response(200, json=_ENDPOINTS)
        return httpx.Response(404, json={"error": "not found"})

    app = FastAPI()
    app.include_router(create_models_router(http_client=lambda: httpx.Client(transport=httpx.MockTransport(provider))))
    bypass_auth(app)
    return TestClient(app, raise_server_exceptions=False)


def test_call_holds_price_one_call_like_the_gateway_reservation() -> None:
    """The hold covers the stand-in body's byte bound at the base input rate plus the llm() output cap."""
    client = _holds_client([])
    resp = client.post(
        "/models/call-holds",
        json={
            "models": [
                {"name": f"openrouter/{_PRICED_MODEL}", "token_source": "managed"},
                {"name": _PRICED_MODEL, "token_source": "byok"},
            ]
        },
    )

    assert resp.status_code == 200
    # The stand-in body plus the 2,048-token wrapper allowance outgrows the 1,000-token window, and
    # no breakpoint asks for the one-hour cache, so the window prices at the base prompt rate.
    provider_usd = (1000 * Decimal("0.000001") + DEFAULT_MAX_TOKENS * Decimal("0.000002")) * Decimal("1.05")
    managed, byok = resp.json()["holds"]
    assert managed["cents"] == float(ChargePolicy("managed_model").convert(provider_usd).total)
    assert byok["cents"] == float(ChargePolicy("byok_model").convert(provider_usd).total)
    assert managed["cents"] > 0
    assert byok["cents"] == 0


def test_call_holds_leave_an_unpriced_model_unknown() -> None:
    """A model the catalog cannot price reports no hold instead of failing the request."""
    resp = _holds_client([]).post("/models/call-holds", json={"models": [{"name": "acme/missing"}]})

    assert resp.status_code == 200
    assert resp.json()["holds"] == [{"name": "acme/missing", "token_source": "managed", "cents": None}]


def test_call_holds_reuse_a_recent_price() -> None:
    """Asking again for the same model does not re-read the provider catalog."""
    seen: list[str] = []
    client = _holds_client(seen)
    for _ in range(2):
        client.post("/models/call-holds", json={"models": [{"name": _PRICED_MODEL}]})

    assert seen == [f"/api/v1/models/{_PRICED_MODEL}/endpoints"]


def test_call_holds_report_the_boxes_open_before_the_first_call() -> None:
    """The boxes' hold is two deny-all boxes, each funded for its first coverage slice."""
    resp = _holds_client([]).post("/models/call-holds", json={"models": []})

    assert resp.status_code == 200
    slice_cents = sandbox_hold_cents(lifetime_ms=COVERAGE_SLICE_SECONDS * 1000, vcpus=2)
    assert resp.json()["box_hold_cents"] == float(2 * slice_cents)
    assert resp.json()["box_hold_cents"] > 0


def _local_agent_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Build a local-mode models router: a signed-in claude and a signed-out codex.

    Args:
        tmp_path: Where the fake CLIs and the state folder go.
        monkeypatch: Turns local mode on for the router and the model router.

    Returns:
        A ``TestClient`` over the models router with auth bypassed.
    """
    claude = _fake_cli(tmp_path, "claude", version="2.1.306 (Claude Code)", login_out=CLAUDE_SUBSCRIPTION)
    codex = _fake_cli(tmp_path, "codex", version="codex-cli 0.163.0", login_exit=1, login_out="Not logged in")
    local = _probe_settings(tmp_path, claude_bin=str(claude), codex_bin=str(codex)).model_copy(
        update={"local_mode": True}
    )
    monkeypatch.setattr(models_router_module, "settings", local)
    monkeypatch.setattr(model_router, "settings", local)
    clear_probe_cache()
    app = FastAPI()
    app.include_router(create_models_router())
    bypass_auth(app)
    return TestClient(app, raise_server_exceptions=False)


def test_agent_models_in_local_mode_are_the_signed_in_subscriptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """List only the signed-in CLIs' subscription ids in local mode, never the managed catalog."""
    client = _local_agent_client(tmp_path, monkeypatch)
    managed = _make_catalog("openai/gpt-4o-mini")
    with patch("core.api.routers.models.get_catalog_cached", return_value=managed):
        body = client.get("/models/for-agent").json()
    names = [row["name"] for row in body["models"]]
    assert names == [f"subscription/claude/{slug}" for slug in sorted(CLI_MODELS["claude"])]
    assert body["models"][0]["provider"] == "anthropic"
    assert all(model_router.route_menu_model(name) == name for name in names)


def test_agent_models_in_local_mode_filter_by_query(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply the name filter to the subscription ids."""
    client = _local_agent_client(tmp_path, monkeypatch)
    body = client.get("/models/for-agent", params={"query": "HAIKU-4.5"}).json()
    assert [row["name"] for row in body["models"]] == ["subscription/claude/anthropic/claude-haiku-4.5"]


def test_agent_models_in_hosted_mode_are_the_catalog(models_client: TestClient) -> None:
    """Keep the managed catalog for a hosted agent."""
    fake_catalog = _make_catalog("openai/gpt-4o-mini", "anthropic/claude-3-5-haiku-20241022")
    with patch("core.api.routers.models.get_catalog_cached", return_value=fake_catalog):
        body = models_client.get("/models/for-agent", params={"query": "gpt"}).json()
    assert [row["name"] for row in body["models"]] == ["openai/gpt-4o-mini"]


@pytest.mark.parametrize("sent_source", [{}, {"token_source": "managed"}])
def test_listed_agent_model_submits_as_a_subscription_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sent_source: dict[str, str]
) -> None:
    """Accept a local agent model id from ``/models/for-agent`` as a subscription model on ``/blackbox/run``.

    The agent wizard copies only the listed ``name`` into the run; the web
    form adds ``token_source: managed``. Neither sends a ``cli_provider``.
    """
    name = _local_agent_client(tmp_path, monkeypatch).get("/models/for-agent").json()["models"][0]["name"]
    client, store = _local_submit_client(monkeypatch, local_mode=True)

    resp = client.post("/blackbox/run", json=_run_payload(reflection_model_config={"name": name, **sent_source}))

    assert resp.status_code == 201, resp.text
    payload = store._jobs[resp.json()["optimization_id"]]["payload"]
    assert payload["token_source"] == "subscription"
    reflection = payload["reflection_model_config"]
    assert (reflection["name"], reflection["token_source"], reflection["cli_provider"]) == (
        name,
        "subscription",
        "claude",
    )


def test_hosted_submit_ignores_the_subscription_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a hosted ``/blackbox/run`` blind to a ``subscription/<cli>/`` name.

    The name stays a managed model, so the empty wallet refuses it with 402,
    as before; it never becomes a subscription run.
    """
    client, store = _local_submit_client(monkeypatch, local_mode=False)

    resp = client.post(
        "/blackbox/run",
        json=_run_payload(reflection_model_config={"name": "subscription/claude/anthropic/claude-haiku-4.5"}),
    )

    assert resp.status_code == 402
    assert resp.json()["code"] == "billing.insufficient_funds"
    assert store.created_ids() == []
