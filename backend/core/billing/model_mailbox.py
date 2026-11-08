"""Run a network-isolated command while the trusted parent brokers model requests."""

from __future__ import annotations

import base64
import json
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

        def respond(document: dict[str, Any]) -> None:
            """Write one trusted response while preserving a paid attempt on transport failure."""
            try:
                headers = with_tags_header(
                    {key.lower(): value for key, value in document.get("headers", {}).items()}, host_tags
                )
                headers[MODEL_ATTEMPT_HEADER] = document["id"]
                token = headers.get("authorization", "").removeprefix("Bearer ") or headers.get("x-api-key", "")
                response = self._dispatch(
                    token, document["path"], json.loads(base64.b64decode(document["body"])), headers
                )
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
            session.write_files(
                {
                    f"{response_directory}/{document['id']}.json": json.dumps(
                        {
                            "status": response.status,
                            "content_type": response.content_type,
                            "body": base64.b64encode(response.body).decode(),
                        }
                    )
                }
            )

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
            result = session.run(wrapped, env=env, timeout_seconds=timeout_seconds, on_output=output)
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
