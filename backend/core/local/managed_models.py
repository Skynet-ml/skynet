"""Route local agent inference through the signed-in Skynet account."""

from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from sqlalchemy.engine import Engine

from ..api.errors import DomainError
from ..models import ModelConfig
from . import hosted_link, hosted_proxy

HOSTED_INFERENCE_KEY = "_skynet_hosted_inference"
_MAX_REQUEST_BYTES = 16 * 1024 * 1024
_lock = threading.Lock()
_gateway: HostedModelGateway | None = None


class HostedModelGateway:
    """Keep account credentials behind scoped loopback model routes."""

    def __init__(self) -> None:
        """Start a loopback-only relay for local agent models."""
        self.routes: dict[str, tuple[Engine, str, str]] = {}
        self.tokens: dict[tuple[Engine, str, str], str] = {}
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            """Relay only the model bound to a registered token."""

            def log_message(self, format: str, *args: Any) -> None:
                """Keep model prompts and scoped tokens out of access logs."""

            def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler protocol method
                """Forward one completion through the account link with automatic token refresh."""
                token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                route = gateway.routes.get(token)
                if route is None or self.path != "/v1/chat/completions":
                    self.send_error(403)
                    return
                engine, username, model = route
                answer = None
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= _MAX_REQUEST_BYTES:
                        self.send_error(413)
                        return
                    raw = self.rfile.read(length)
                    body = json.loads(raw)
                    if not isinstance(body, dict) or body.get("model") != model:
                        self.send_error(422)
                        return
                    answer = hosted_proxy.forward(
                        engine,
                        username,
                        "POST",
                        "models/inference/v1/chat/completions",
                        "",
                        {"content-type": "application/json"},
                        raw,
                    )
                    self.send_response(answer.status)
                    self.send_header("Content-Type", answer.headers.get("content-type", "application/json"))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    for chunk in answer.chunks():
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except DomainError as error:
                    self.send_error(error.status_code, error.code)
                except (ValueError, TypeError):
                    self.send_error(422)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    if answer is not None:
                        answer.close()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True, name="hosted-model-relay").start()

    def config(self, engine: Engine, username: str, model: str) -> ModelConfig:
        """Register a model bound to the local user's hosted account.

        Args:
            engine: Local database containing the encrypted account link.
            username: Authenticated local user.
            model: Fully qualified OpenRouter model id.

        Returns:
            A config holding only a scoped loopback token, never the hosted credential.
        """
        slug = model.removeprefix("openrouter/")
        identity = (engine, username, slug)
        with _lock:
            token = self.tokens.get(identity)
            if token is None:
                token = secrets.token_urlsafe(32)
                self.tokens[identity] = token
                self.routes[token] = identity
        return ModelConfig(
            name=model,
            token_source="managed",
            base_url=f"http://127.0.0.1:{self.server.server_port}/v1",
            extra={"api_key": token, HOSTED_INFERENCE_KEY: True},
        )


def hosted_model_config(engine: Engine, username: str, model: str | None) -> ModelConfig | None:
    """Build managed inference only for a linked local account.

    Args:
        engine: Local database.
        username: Authenticated local user.
        model: Selected OpenRouter id, or None for the hosted catalog default.

    Returns:
        The managed config, or None when no account link exists.
    """
    global _gateway
    if hosted_link.link_status(engine, username) is None:
        return None
    if not model:
        answer = hosted_proxy.forward(engine, username, "GET", "models", "", {}, b"")
        try:
            data = json.loads(b"".join(answer.chunks()))
            rows = data.get("models", []) if answer.status == 200 else []
            rows = [row for row in rows if row.get("available") and str(row.get("value", "")).startswith("openrouter/")]
            chosen = next((row for row in rows if row.get("is_default")), None)
            chosen = chosen or next((row for row in rows if row.get("featured")), None) or (rows[0] if rows else None)
            model = chosen["value"] if chosen else None
        finally:
            answer.close()
        if not model:
            raise DomainError("models.unknown_model", status=422)
    with _lock:
        if _gateway is None:
            _gateway = HostedModelGateway()
        gateway = _gateway
    return gateway.config(engine, username, model)
