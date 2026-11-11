"""Run a network-isolated command while the trusted parent brokers model requests."""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import secrets
import shlex
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import guest_model_proxy
from .budgets import BudgetError, BudgetInsufficientError
from .model_dispatch import MODEL_ATTEMPT_HEADER, ModelHTTPResult
from .operation_pricing import UnpricedOperationError
from .runtime import ProviderFailedError, UsagePendingError
from .signals import BudgetReached
from .usage_tags import current_tags, with_tags_header

logger = logging.getLogger(__name__)


class ModelMailbox:
    """Bridge guest model requests without granting the guest network or billing authority."""

    def __init__(self, dispatch: Callable[..., ModelHTTPResult], *, concurrency: int = 8) -> None:
        """Bind the supervisor's authenticated, single-attempt model dispatch.

        Args:
            dispatch: Trusted callback accepting a scoped token, path, body, and protocol headers.
            concurrency: Maximum concurrently serviced guest model requests.
        """
        self._dispatch = dispatch
        self._concurrency = concurrency

    def run(
        self,
        session: Any,
        command: str,
        *,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        on_output: Callable[[str, str], None] | None = None,
    ) -> Any:
        """Wrap one sandbox command and service its model mailbox alongside output.

        Args:
            session: Exact sandbox session whose file and command APIs the parent owns.
            command: Original command executed without changing its algorithm.
            env: Guest environment containing only scoped route credentials.
            timeout_seconds: Command limit already covered by the sandbox reservation.
            on_output: Existing progress or upstream evaluator mailbox callback.

        Returns:
            The sandbox's original command result.
        """
        nonce = secrets.token_hex(16)
        # Captured here: requests are answered on pool threads, where the caller's scope is gone.
        host_tags = current_tags()
        directory = f".app-model-{nonce}"
        prefix = f"APP_MODEL_{nonce}:"
        source_path = f"{directory}/proxy.py"
        config_path = f"{directory}/config.json"
        response_directory = f"{directory}/responses"
        session.write_files(
            {
                source_path: Path(guest_model_proxy.__file__).read_text(encoding="utf-8"),
                config_path: json.dumps(
                    {"prefix": prefix, "response_dir": response_directory, "timeout_seconds": timeout_seconds or 600}
                ),
            }
        )
        pending = {"stdout": "", "stderr": ""}
        visible: dict[str, list[str]] = {"stdout": [], "stderr": []}
        seen: set[str] = set()
        guard = threading.Lock()
        errors: list[BaseException] = []
        # MCP event streams whose box reader left, and a flag for the end of the command.
        abandoned: set[str] = set()
        finished = threading.Event()

        def respond(document: dict[str, Any]) -> None:
            """Write one trusted response while preserving a paid attempt on transport failure."""
            try:
                headers = with_tags_header(
                    {key.lower(): value for key, value in document.get("headers", {}).items()}, host_tags
                )
                headers[MODEL_ATTEMPT_HEADER] = document["id"]
                token = headers.get("authorization", "").removeprefix("Bearer ") or headers.get("x-api-key", "")
                body = base64.b64decode(document["body"])
                # An MCP server takes the client's bytes as they are; only a model request is one JSON object.
                if not str(document["path"]).startswith(guest_model_proxy.MCP_PATH):
                    body = json.loads(body)
                response = self._dispatch(token, document["path"], body, headers)
            except BudgetReached as error:
                response = failure(402, "budget_reached", str(error))
            except BudgetInsufficientError as error:
                response = failure(402, "budget_insufficient", str(error))
            except ProviderFailedError as error:
                response = failure(502, "provider_failed", str(error))
            except (BudgetError, UsagePendingError):
                response = failure(424, "usage_pending", "Previous model usage is awaiting confirmation.")
            except (UnpricedOperationError, ValueError, TypeError) as error:
                response = failure(422, "unpriced_operation", str(error))
            except BaseException as error:
                errors.append(error)
                response = failure(502, "provider_unavailable", "The model transport was interrupted.")
            answer: dict[str, Any] = {
                "status": response.status,
                "content_type": response.content_type,
                "body": base64.b64encode(response.body).decode(),
                "headers": [list(pair) for pair in response.headers],
            }
            if response.stream is None:
                session.write_files({f"{response_directory}/{document['id']}.json": json.dumps(answer)})
                return
            try:
                session.write_files(
                    {f"{response_directory}/{document['id']}.json": json.dumps({**answer, "more": True})}
                )
            except BaseException:
                response.stream.close()
                raise
            # An event stream can stay open for the whole command, so it gets its own thread, not a pool worker.
            threading.Thread(
                target=pump,
                args=(document["id"], response.stream),
                daemon=True,
                name="sandbox-mcp-stream",
            ).start()

        def pump(identity: str, stream: Any) -> None:
            """Write each part of an MCP event stream as its own response file, then an end part.

            Part ``n`` goes to ``<id>.<n>.json``. The last part has ``"more": false``.
            The stream stops with no last part when the box's reader left or the
            command ended, so a server that keeps it alive with pings cannot hold
            its place.

            Args:
                identity: The request's id.
                stream: The relay's event stream.
            """
            base = f"{response_directory}/{identity}"
            part = 0
            try:
                for chunk in stream:
                    if identity in abandoned or finished.is_set():
                        return
                    part += 1
                    session.write_files(
                        {f"{base}.{part}.json": json.dumps({"body": base64.b64encode(chunk).decode(), "more": True})}
                    )
            except Exception as error:
                # Only the type: the relay already recorded the call, and a message could hold a body.
                logger.warning("An MCP event stream ended early in the sandbox mailbox: %s", type(error).__name__)
            finally:
                stream.close()
            # A box that went away cannot take the end part; the guest proxy is gone with it.
            with contextlib.suppress(Exception):
                session.write_files({f"{base}.{part + 1}.json": json.dumps({"more": False})})

        def failure(status: int, code: str, message: str) -> ModelHTTPResult:
            """Return an admission error understood by both supported model protocols."""
            return ModelHTTPResult(
                status,
                "application/json",
                json.dumps(
                    {
                        "error": {
                            "type": code,
                            "code": code,
                            "message": message,
                        }
                    }
                ).encode(),
            )

        with ThreadPoolExecutor(max_workers=self._concurrency, thread_name_prefix="sandbox-model") as executor:
            futures = []

            def output(stream: str, data: str) -> None:
                """Consume nonce-bound model frames and forward ordinary output in order."""
                with guard:
                    pending[stream] = pending.get(stream, "") + data
                    while "\n" in pending[stream]:
                        line, pending[stream] = pending[stream].split("\n", 1)
                        if stream == "stdout" and line.startswith(prefix):
                            document = json.loads(line[len(prefix) :])
                            identity = document.get("id", "")
                            if (
                                not isinstance(identity, str)
                                or len(identity) != 32
                                or any(char not in "0123456789abcdef" for char in identity)
                            ):
                                raise ValueError("Invalid model mailbox identity.")
                            if identity not in seen:
                                seen.add(identity)
                                if "stop" in document:
                                    abandoned.add(str(document["stop"]))
                                else:
                                    futures.append(executor.submit(respond, document))
                        else:
                            visible.setdefault(stream, []).append(line + "\n")
                            if on_output is not None:
                                on_output(stream, line + "\n")

            # A version can write ~/.bash_profile, ~/.profile or a .pth file in user
            # site-packages under $HOME. A login shell runs the first two, and a
            # python3 without -I runs the .pth (E38). The proxy needs only the stdlib.
            wrapped = (
                f"python3 -I {shlex.quote(source_path)} {shlex.quote(config_path)}"
                f" bash --noprofile --norc -c {shlex.quote(command)}"
            )
            try:
                result = session.run(wrapped, env=env, timeout_seconds=timeout_seconds, on_output=output)
            finally:
                finished.set()
            for future in futures:
                future.result()
            for stream, data in pending.items():
                if data and not (stream == "stdout" and data.startswith(prefix)):
                    visible.setdefault(stream, []).append(data)
                    if on_output is not None:
                        on_output(stream, data)
            if errors:
                raise errors[0]
            return replace(result, stdout="".join(visible["stdout"]), stderr="".join(visible["stderr"]))
