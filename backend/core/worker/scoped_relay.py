"""Build the parent-side dispatcher for sandbox-scoped relay capabilities."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import httpx

from ..billing.env_connectors import CONNECTOR_ROUTES_KEY
from ..billing.host_agent import HOST_AGENT_PATH, HOST_AGENT_TOKEN_KEY
from ..billing.model_dispatch import ModelHTTPResult
from ..config import settings
from ..models.blackbox import BLACKBOX_TARGET_REPO
from ..service_gateway.optimization.blackbox.repo_scorer import SETUP_ALLOWANCE_SECONDS


def model_forwarder(payload: dict[str, Any]) -> Any:
    """Restrict the supervisor relay to explicitly issued parent capabilities.

    Args:
        payload: Request carrying opaque model, tool, and evaluator routes.

    Returns:
        A callback compatible with the parent-owned model mailbox protocol.
    """
    routes: dict[str, tuple[str, frozenset[str]]] = {}
    model_paths = frozenset(
        {
            "/v1/messages",
            "/v1/chat/completions",
            "/v1/responses",
            "/v1/_budget/state",
        }
    )

    def register(route: Any, permitted: frozenset[str]) -> None:
        """Register a trusted loopback descriptor for its allowed paths.

        Args:
            route: Opaque route descriptor placed by the trusted parent.
            permitted: Exact HTTP paths authorized for its token.

        Raises:
            ValueError: When the descriptor does not point to the parent loopback gateway.
        """
        if not isinstance(route, dict):
            return
        origin = urlsplit(route["url"])
        if (
            origin.scheme != "http"
            or origin.hostname not in {"127.0.0.1", "localhost", "::1"}
            or origin.username
            or origin.password
            or origin.path.rstrip("/") != "/v1"
            or origin.query
            or origin.fragment
        ):
            raise ValueError("Protected model routes must use the parent's loopback gateway.")
        routes[route["token"]] = (f"{origin.scheme}://{origin.netloc}", permitted)

    configs = [payload.get(key) for key in ("model_config", "reflection_model_config", "task_model_config")]
    scorer = payload.get("scorer")
    if isinstance(scorer, dict):
        configs.append(scorer.get("model"))
    for key in ("generation_models", "reflection_models", "extra_reflection_model_configs"):
        configs.extend(payload.get(key) or [])
    for config in configs:
        if isinstance(config, dict) and isinstance(config.get("extra"), dict):
            route = config["extra"].get("_skynet_budget_route")
            register(route, model_paths)
            if isinstance(route, dict):
                register(route.get("embedding"), frozenset({"/v1/embeddings", "/v1/_budget/state"}))
                if route.get(HOST_AGENT_TOKEN_KEY):
                    # Only a host proposer's own token reaches the host agent, and only there.
                    register({**route, "token": route[HOST_AGENT_TOKEN_KEY]}, frozenset({HOST_AGENT_PATH}))
    register(payload.get("_skynet_target_route"), model_paths)
    register(payload.get("_skynet_tools_route"), frozenset({"/v1/_mcp", "/v1/_budget/state"}))
    connector_routes = payload.get(CONNECTOR_ROUTES_KEY)
    if isinstance(connector_routes, dict):
        for route in connector_routes.values():
            register(route, frozenset({"/v1/_mcp", "/v1/_budget/state"}))
    register(payload.get("_skynet_packages_route"), frozenset({"/v1/_packages"}))
    register(payload.get("_skynet_evaluator_route"), frozenset({"/v1/_evaluator", "/v1/_budget/state"}))
    target = payload.get("target")
    evaluator_timeout = 630.0
    if isinstance(target, dict) and target.get("kind") == BLACKBOX_TARGET_REPO:
        # A repository score also prepares the version and opens its stage boxes, so the
        # guest waits up to its setup allowance longer; the parent must not cut it off first.
        evaluator_timeout += SETUP_ALLOWANCE_SECONDS

    def dispatch(token: str, path: str, body: dict[str, Any], headers: dict[str, str]) -> ModelHTTPResult:
        """Forward one request through its exact scoped capability.

        Args:
            token: Opaque capability token issued by the parent.
            path: Requested relay path.
            body: JSON request body.
            headers: Protocol headers safe to forward.

        Returns:
            The upstream status, content type, and response bytes.

        Raises:
            ValueError: When the token or requested path is outside its scope.
        """
        if token not in routes or path not in routes[token][1]:
            raise ValueError("The guest requested an unknown metered model capability.")
        timeout = evaluator_timeout if path == "/v1/_evaluator" else 600
        if path == HOST_AGENT_PATH:
            # One host CLI turn may run for its whole configured limit.
            timeout = settings.host_agent_timeout_seconds + 30
        with httpx.Client(trust_env=False, timeout=timeout) as client:
            response = client.request(
                "GET" if path.endswith("/_budget/state") else "POST",
                routes[token][0] + path,
                headers={**headers, "Authorization": f"Bearer {token}"},
                json=body,
            )
        return ModelHTTPResult(
            response.status_code,
            response.headers.get("Content-Type", "application/json"),
            response.content,
        )

    return dispatch
