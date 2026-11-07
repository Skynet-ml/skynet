"""Translate Skynet's execution contract without replacing upstream search logic."""

from __future__ import annotations

import json
import math
import secrets
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import dspy
from gepa.oa.budget import BudgetExhausted, BudgetTracker
from gepa.oa.engine import Result as UpstreamResult
from gepa.oa.eval_server import EvalServer as UpstreamEvalServer
from gepa.oa.task import Task as UpstreamTask

from ..budget_stop import BudgetReached
from .native_engines import AUTORESEARCH_VERSION, META_HARNESS_REVISION
from .protocol import BudgetExhaustedError, EngineContext, EvalServer, Result, Task, engine_score

GEPA_REVISION = "0632cdb5dcc052e690eab439e1b4a7e3e9cfe407"
GEPA_SOURCE = f"git+https://github.com/gepa-ai/gepa@{GEPA_REVISION}"
AUTOSADDLER_REVISION = "9df6d2e3e1d3946057243690bca28e136fa81179"
AUTOSADDLER_SOURCE = f"git+https://github.com/microsoft/AutoSaddler@{AUTOSADDLER_REVISION}"
SHINKA_VERSION = "0.0.7"
SHINKA_SOURCE = f"shinka-evolve=={SHINKA_VERSION}"
META_HARNESS_SOURCE = f"https://github.com/stanford-iris-lab/meta-harness@{META_HARNESS_REVISION}"
# AutoResearch is the app's own engine, so its provenance is a version, not an
# upstream pin. The label is stored with each run's engine provenance, so it keeps
# its pre-rename prefix.
AUTORESEARCH_SOURCE = f"skynet:autoresearch@{AUTORESEARCH_VERSION}"
AUTO_ENGINES = ("gepa", "autoresearch", "meta_harness", "autosaddler", "shinka_evolve")


def upstream_task(task: Task, name: str) -> UpstreamTask:
    """Translate a Skynet task into the upstream task shape.

    Args:
        task: Skynet's optimization task.
        name: Artifact grouping name.

    Returns:
        The equivalent upstream task; only the train cases go in, as its
        train set, because upstream merges train and val into one pool and
        the validation split must stay out of that pool for selection.
    """
    return UpstreamTask(
        name=name,
        seed_candidate=task.seed_candidate,
        objective=task.objective or "",
        background=task.background or "",
        train_set=task.cases or None,
    )


def upstream_server(
    task: Task, server: EvalServer, ctx: EngineContext, on_eval: Callable[[Any, Any, float | None], None] | None = None
) -> UpstreamEvalServer:
    """Keep upstream evaluations inside Skynet's run-wide scoring allowance.

    Args:
        task: Optimization inputs.
        server: Skynet's accounting and scorer boundary.
        ctx: Workspace and concurrency settings.
        on_eval: Called with the candidate, case and score (``None`` after a metric error) after each scorer run, if given.

    Returns:
        An unstarted upstream server for an in-process engine.
    """

    stops: list[BudgetReached] = []

    def evaluate(candidate: Any, example: Any = None) -> tuple[float, dict[str, Any]]:
        """Translate the admission stop into the exception upstream engines handle.

        Args:
            candidate: Proposed text or components.
            example: Optional dataset example.

        Returns:
            The score, :data:`UNSCORED` after a metric error, and feedback.
        """
        try:
            score, info = server.evaluate(candidate, example)
        except BudgetReached as exc:
            stops.append(exc)
            raise BudgetExhausted(str(exc)) from exc
        except BudgetExhaustedError as exc:
            raise BudgetExhausted(str(exc)) from exc
        if on_eval is not None:
            on_eval(candidate, example, score)
        return engine_score(score), info

    upstream = UpstreamEvalServer(
        upstream_task(task, Path(ctx.run_dir).name),
        evaluate,
        BudgetTracker(max_evals=server.remaining),
        max_concurrency=ctx.concurrency,
        output_dir=Path(ctx.run_dir),
    )
    upstream.platform_budget_stops = stops
    return upstream


def local_result(result: UpstreamResult, server: EvalServer) -> Result:
    """Keep the upstream aggregate incumbent and omit recursive ensemble metadata.

    Args:
        result: The engine's completed result.
        server: Run accounting for actual scorer calls.

    Returns:
        A JSON-safe platform result without re-ranking candidates.
    """
    metadata = finite_json(
        {key: value for key, value in result.metadata.items() if key not in {"all_results", "stage_results"}}
    )
    metadata["upstream_source"] = GEPA_SOURCE
    score = float(result.best_score)
    return Result(
        best_candidate=result.best_candidate,
        best_score=score if math.isfinite(score) else None,
        total_evals=server.used,
        metadata=metadata,
    )


def finite_json(value: Any) -> Any:
    """Keep unscored upstream sentinels valid in persisted JSON and progress events.

    Args:
        value: Upstream metadata or a progress payload.

    Returns:
        JSON-shaped values with non-finite scores represented as missing.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: finite_json(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [finite_json(item) for item in value]
    return value


@contextmanager
def reflection_endpoint(ctx: EngineContext) -> Iterator[dict[str, Any]]:
    """Route upstream Best-of-N through the existing metered reflection model.

    The upstream engine constructs its own LiteLLM client. A loopback transport
    preserves its prompts and sampling loop while retaining Skynet's configured
    model, credentials, usage accounting and DSPy cost callbacks.

    Args:
        ctx: The already configured and metered model callable.

    Yields:
        LiteLLM connection options valid only for this invocation.
    """
    token = secrets.token_urlsafe(32)
    failures: list[BaseException] = []
    model_context = dict(dspy.settings.copy())

    class Handler(BaseHTTPRequestHandler):
        """Expose only the completion route to an authenticated loopback client."""

        def do_POST(self) -> None:
            """Forward one completion and record errors for the parent thread."""
            if self.path != "/v1/chat/completions" or self.headers.get("Authorization") != f"Bearer {token}":
                self.send_error(403)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 16 * 1024 * 1024:
                    raise ValueError("Invalid completion request size")
                payload = json.loads(self.rfile.read(size))
                if ctx.check_budget is not None:
                    ctx.check_budget()
                with dspy.context(**model_context):
                    answer = ctx.reflection_lm(payload["messages"])
                if ctx.check_budget is not None:
                    ctx.check_budget()
                response = {
                    "id": "app-reflection",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "app-reflection",
                    "choices": [
                        {"index": 0, "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}
                    ],
                }
                body = json.dumps(response).encode()
                self.send_response(200)
            except BaseException as exc:
                failures.append(exc)
                body = json.dumps({"error": {"message": "The configured optimization model failed."}}).encode()
                self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            """Keep transport logs from disclosing request details.

            Args:
                format: HTTP server message format.
                *args: Message arguments.
            """

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield {
            "api_base": f"http://127.0.0.1:{httpd.server_port}/v1",
            "api_key": token,
            "num_retries": 0,
        }
        if failures:
            raise failures[0]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
