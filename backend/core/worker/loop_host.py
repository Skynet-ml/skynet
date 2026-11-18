"""Host a repository run on the worker's engine loop (SKY-829).

A run whose engine has a row in :data:`LOOP_ROUTES` does not start a guest box for the whole job. The worker's
child process runs the job itself, and the job gets an :class:`EngineLoop` through a :class:`LoopBinding`:

- The engine's model calls go through the loop. Each call is one step that the run's lease pays for, so the
  model client here bills nothing itself. A managed call goes straight to OpenRouter with the worker's key and
  gives the provider's cost; a BYOK call goes to the user's provider with the user's key, at no Skynet fee.
- The engine's commands run in candidate boxes, and its metrics in new metric boxes, of the run's box route.
- Each call of the run's repository scorer is one ``evaluate`` step. The scorer stays in its own boxes in the
  parent, and the step commits the score and the feedback, so a resume replays it and does not pay again.

A worker that dies in the run's middle gives the job to a new generation. That generation opens the same step
log, and the engine replays each committed step in its order until it reaches the first new one.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import shutil
import tarfile
import tempfile
import traceback
from collections.abc import Callable, Mapping
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx
import litellm
from sqlalchemy.engine import Engine

from ..billing.budgets import BudgetError, BudgetService
from ..billing.model_dispatch import usage_charge
from ..billing.operation_pricing import ChargePolicy, UnpricedOperationError
from ..billing.runtime import UsagePendingError
from ..config import settings
from ..constants import TOKEN_SOURCE_BYOK, TOKEN_SOURCE_MANAGED
from ..exceptions import ServiceError
from ..sandbox.grants import resolve_grants
from ..sandbox.trees import tar_to_tree, tree_to_tar
from ..service_gateway.optimization.blackbox.protocol import (
    Candidate,
    EngineContext,
    ModelReply,
    RepoSeed,
    ScorerAbortError,
    ScorerFn,
    SideInfo,
    ToolDefinition,
    openai_tools,
    tool_calls_from_openai,
)
from ..service_gateway.optimization.blackbox.remote_sandbox import RemoteSandboxRuntime
from ..service_gateway.optimization.blackbox.repo_tree import REPO_SNAPSHOT_KEY, unpack_tree, version_patch
from ..service_gateway.optimization.blackbox.runner import side_info_json_default
from ..service_gateway.optimization.blackbox.sandbox_contract import SandboxRuntime, SandboxSpec
from ..service_gateway.optimization.blackbox.tree_sandbox import RuntimeBoxSource
from ..storage import get_job_store
from ..storage.base import ObjectStore
from ..storage.object_store import S3ObjectStore
from ..storage.tree_store import TreeStore
from .actors import User
from .engine_loop import EngineLoop, ModelCall, Prompt
from .failure_events import failure_event
from .step_recovery import open_step_log
from .subprocess_runner import run_service_in_subprocess, safe_queue_put

logger = logging.getLogger(__name__)

LOOP_HOST_KEY = "_loop_host"
# One lease is one ledger hold. A dollar keeps the ledger calls few, and a stopped run gives back what it held.
CHUNK_CENTS = 100
# The bound of one model call must fit in one lease. A reply that costs more stops the loop.
MODEL_MAX_CENTS = 100
# The longest that one command or metric box lives.
STEP_BOX_SECONDS = 900
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL_TIMEOUT_SECONDS = 600.0
_TREE_ID = re.compile(r"[0-9a-f]{64}")
_HTTP_STATUS = re.compile(r"\(HTTP (\d{3})\)")
# The engine loop stops on these; an evaluation must not commit them as a refused version.
_STOP_ERRORS = (ScorerAbortError, BudgetError, UsagePendingError, UnpricedOperationError)


class LeaseModelClient:
    """Call the run's proposer model with no budget hold: the loop's lease pays for each call."""

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        payload_token_source: str | None,
        managed_key: str,
        client: httpx.Client | None = None,
    ) -> None:
        """Bind the client to the run's resolved proposer model.

        Args:
            config: The run's ``reflection_model_config`` with its resolved credentials.
            payload_token_source: The run's own ``token_source``, for a model config that names none.
            managed_key: The worker's OpenRouter key, for a managed model.
            client: The HTTP client of managed calls; tests give a fake transport.

        Raises:
            ValueError: When the source is not managed or BYOK, the model has no key, or a BYOK call would owe a
                Skynet fee, which this client cannot measure.
        """
        source = config.get("token_source") or payload_token_source or TOKEN_SOURCE_MANAGED
        extra = dict(config.get("extra") or {})
        if source == TOKEN_SOURCE_MANAGED:
            key = managed_key
        elif source == TOKEN_SOURCE_BYOK:
            key = str(extra.get("api_key") or "")
            # LiteLLM gives no exact provider bill for a BYOK call, so only a free BYOK route can run here.
            if ChargePolicy("byok_model").byok_fee_fraction != 0:
                raise ValueError("A BYOK model with a Skynet fee cannot run on the engine loop.")
        else:
            raise ValueError(f"The engine loop cannot call a {source!r} model.")
        if not key:
            raise ValueError("The engine loop's model has no provider credential.")
        self._source = source
        self._key = key
        model = str(extra.get("model") or config.get("name") or "")
        # OpenRouter takes its own slug; LiteLLM needs the provider prefix to route a BYOK call.
        self._model = model.removeprefix("openrouter/") if source == TOKEN_SOURCE_MANAGED else model
        self._api_base = extra.get("api_base") or extra.get("base_url") or config.get("base_url")
        self._options = {name: config[name] for name in ("temperature", "max_tokens") if config.get(name) is not None}
        self._client = client or httpx.Client(timeout=MODEL_TIMEOUT_SECONDS)

    def __call__(self, prompt: Prompt, tools: list[ToolDefinition] | None) -> ModelReply:
        """Call the model once.

        Args:
            prompt: The prompt text, or the chat messages.
            tools: The tool definitions, or ``None`` for no tools.

        Returns:
            The reply, with the provider's cost in ledger cents; 0 for a BYOK call.

        Raises:
            ServiceError: When the provider fails or a managed reply gives no complete cost. The loop then records
                a failed call with no cost.
        """
        messages = [{"role": "user", "content": prompt}] if isinstance(prompt, str) else list(prompt)
        request: dict[str, Any] = {"model": self._model, "messages": messages, **self._options}
        if tools:
            request["tools"] = openai_tools(tools)
        if self._source == TOKEN_SOURCE_BYOK:
            response = litellm.completion(api_key=self._key, api_base=self._api_base, **request)
            message = response.choices[0].message.model_dump()
            return ModelReply(str(message.get("content") or ""), Decimal(0), tool_calls_from_openai(message))
        try:
            answer = self._client.post(
                OPENROUTER_CHAT_URL,
                headers={"Authorization": f"Bearer {self._key}"},
                json={**request, "usage": {"include": True}},
            )
            answer.raise_for_status()
            body = answer.json()
        except (httpx.HTTPError, ValueError) as error:
            raise ServiceError(f"The model call failed: {type(error).__name__}") from error
        usd = usage_charge(body.get("usage") or {})
        if usd is None:
            raise ServiceError("The model reply gave no complete cost.")
        message = body["choices"][0]["message"]
        return ModelReply(
            str(message.get("content") or ""),
            ChargePolicy("managed_model").convert(usd).total,
            tool_calls_from_openai(message),
        )


def tree_patch(trees: TreeStore, run_id: str, base_tree: str, candidate_tree: str) -> str:
    """Return a candidate tree as a git patch against the base tree, the form that the repository scorer takes.

    Args:
        trees: The run's tree store.
        run_id: The run that reads the trees.
        base_tree: The tree that the patch applies to.
        candidate_tree: The candidate's tree.

    Returns:
        The ``git diff --binary`` patch; empty when the trees hold the same files.

    Raises:
        LookupError: When the run cannot see one of the trees.
    """
    folder = Path(tempfile.mkdtemp(prefix="loop-patch-"))
    try:
        chunk = folder / "base.b64"
        chunk.write_text(base64.b64encode(tree_to_tar(trees, run_id, base_tree)).decode("ascii"), encoding="ascii")
        checkout = unpack_tree([chunk], folder / "checkout")
        for entry in checkout.iterdir():
            if entry.name == ".git":
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        with tarfile.open(fileobj=BytesIO(tree_to_tar(trees, run_id, candidate_tree))) as archive:
            archive.extractall(checkout, filter="data")
        return version_patch(checkout)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _retryable(error: ServiceError) -> bool:
    """Report whether a scorer error can pass on a new try, so no step may record it.

    Args:
        error: The scorer's error.

    Returns:
        True for a transport error, a server error, a pending usage or a rate limit.
    """
    if isinstance(error.__cause__, httpx.HTTPError):
        return True
    status = _HTTP_STATUS.search(str(error))
    return status is not None and (int(status.group(1)) >= 500 or int(status.group(1)) in {424, 429})


class _LoopBinding:
    """Give a job the engine loop's context and run its scorer calls as loop steps."""

    def __init__(self, loop: EngineLoop, trees: TreeStore, run_id: str, seed: RepoSeed) -> None:
        """Bind the job to the loop and the run's seed.

        Args:
            loop: The run's engine loop.
            trees: The run's tree store.
            run_id: The run.
            seed: The run's seed tree and path rules.
        """
        self._loop = loop
        self._trees = trees
        self._run_id = run_id
        self._seed = seed
        # The engine's trees grow from the visible seed, so a patch against it holds only the engine's changes.
        self._base_tree = loop.context(repo_seed=seed).repo_seed.tree_id

    def context(self, **fields: Any) -> EngineContext:
        """Return the loop's engine context with the run's seed.

        Args:
            **fields: The job's own context fields.

        Returns:
            The context.
        """
        return self._loop.context(repo_seed=self._seed, **fields)

    def finish(self) -> None:
        """Check that the run replayed every committed step of the loop."""
        self._loop.finish()

    def scorer(self, base: ScorerFn) -> ScorerFn:
        """Return a scorer whose each call is one ``evaluate`` step of the loop.

        Args:
            base: The job's repository scorer.

        Returns:
            The scorer. A version that the scorer refused raises the same error on a replay.
        """

        def score(candidate: Candidate, case: Any = None) -> tuple[float | None, SideInfo]:
            """Score one version on one case, or give back the committed score.

            Args:
                candidate: A patch, a tree id, or a one-file mapping of one of them.
                case: The case.

            Returns:
                The score and the side information.

            Raises:
                ServiceError: When the scorer refused the version.
            """
            result = self._loop.evaluate(
                {"candidate": candidate, "case": case}, lambda: self._score(base, candidate, case)
            )
            if "refused" in result:
                raise ServiceError(str(result["refused"]))
            return result["score"], result["side_info"]

        return score

    def _score(self, base: ScorerFn, candidate: Candidate, case: Any) -> dict[str, Any]:
        """Run the scorer once and give its result in the form that a step commits.

        Args:
            base: The job's repository scorer.
            candidate: The version.
            case: The case.

        Returns:
            ``{"score", "side_info"}``, or ``{"refused": <why>}`` when the scorer refused the version for good.
        """
        patch = self._patch(candidate)
        try:
            score, side_info = base(patch, case)
        except _STOP_ERRORS:
            raise
        except ServiceError as error:
            if _retryable(error):
                raise
            return {"refused": str(error)}
        except Exception as error:
            return {"refused": f"{type(error).__name__}: {error}"}
        # The replay gives the side info from JSON, so the live run uses the same JSON form.
        side_info = json.loads(json.dumps(side_info, default=side_info_json_default))
        return {"score": score, "side_info": side_info}

    def _patch(self, candidate: Candidate) -> str:
        """Turn the engine's version into the patch that the repository scorer takes.

        Args:
            candidate: A patch, a tree id, or a one-file mapping of one of them.

        Returns:
            The patch.
        """
        if isinstance(candidate, Mapping) and len(candidate) == 1:
            candidate = next(iter(candidate.values()))
        text = str(candidate)
        if _TREE_ID.fullmatch(text):
            return tree_patch(self._trees, self._run_id, self._base_tree, text)
        return text


def host_loop_job(
    payload: dict[str, Any],
    artifact_id: str,
    event_queue: Any,
    *,
    host: Mapping[str, Any],
    engine: Engine,
    objects: ObjectStore,
    runtime: SandboxRuntime,
    model: ModelCall,
    run_dir: Path | None = None,
) -> None:
    """Run one job on a new engine loop and send its events, as :func:`run_service_in_subprocess` does.

    Args:
        payload: The protected job payload, without the host fields.
        artifact_id: The id that the events name.
        event_queue: The queue back to the worker.
        host: The trusted host fields that the worker set: run, user, budget and generation.
        engine: Engine with the jobs, ledger and step log tables.
        objects: Where the step log's packs and trees are.
        runtime: Opens the loop's command and metric boxes.
        model: The loop's model client.
        run_dir: The loop's local folder; a new temporary folder when ``None``.
    """
    run_id = str(host["run_id"])
    try:
        step_log = open_step_log(engine, objects, str(host["worker_id"]))
    except BaseException as error:
        safe_queue_put(event_queue, failure_event(error, traceback_text=traceback.format_exc()))
        return
    loop: EngineLoop | None = None
    try:
        try:
            user = User(str(host["username"]), engine=engine, budgets=BudgetService(engine=engine))
            workspace = user.workspace()
            # The job's own budget pays for its run, also when the workspace points at another budget.
            workspace.budget_id = str(host["budget_id"])
            run = workspace.start_run(
                run_id,
                step_log=step_log,
                execution_generation=int(host["execution_generation"]),
                budget_generation=int(host["budget_generation"]),
                grant_list_id=resolve_grants((), {}).grant_list_id,
                chunk_cents=CHUNK_CENTS,
                lease_slices=1,
            )
            trees = TreeStore(step_log)
            snapshot = payload[REPO_SNAPSHOT_KEY]
            seed_tree = tar_to_tree(trees, run_id, Path(snapshot["archive"]).read_bytes())
            hidden = dict(snapshot.get("hidden_hashes") or {})
            seed = RepoSeed(
                tree_id=seed_tree,
                editable_paths=tuple(payload["target"]["repo"].get("editable_paths") or ()),
                readonly_paths=tuple(snapshot.get("readonly_paths") or ()),
                hidden_hashes=hidden,
            )
            descriptor = payload.get("_budget_gateway_descriptor") or {}
            # No operation key: the box route makes each open its own request, so no two steps share a box.
            spec = SandboxSpec(lifetime_seconds=STEP_BOX_SECONDS, image=descriptor.get("image"))
            loop = EngineLoop(
                run,
                engine=engine,
                trees=trees,
                candidates=RuntimeBoxSource(runtime, spec),
                metrics=RuntimeBoxSource(runtime, spec),
                model=model,
                model_max_cents=MODEL_MAX_CENTS,
                run_dir=run_dir or Path(tempfile.mkdtemp(prefix=f"engine-loop-{run_id[:8]}-")),
                hidden_hashes=hidden,
            )
            binding = _LoopBinding(loop, trees, run_id, seed)
        except BaseException as error:
            safe_queue_put(event_queue, failure_event(error, traceback_text=traceback.format_exc()))
            return
        run_service_in_subprocess(payload, artifact_id, event_queue, loop=binding)
    finally:
        _close(loop.close if loop is not None else None, "engine loop")
        _close(step_log.close, "step log")


def _close(close: Callable[[], None] | None, label: str) -> None:
    """Close one part of the host and log a failure, so the other parts still close.

    Args:
        close: The close call, or ``None`` when the part never opened.
        label: Names the part in the log.
    """
    if close is None:
        return
    try:
        close()
    except Exception:
        logger.exception("the %s did not close", label)


def run_loop_job(payload: dict[str, Any], artifact_id: str, event_queue: Any, _start_method: str) -> None:
    """Run one job on the engine loop in the worker's child process; the spawn target of a loop-routed run.

    Args:
        payload: The protected job payload with the worker's host fields.
        artifact_id: The id that the events name.
        event_queue: The queue back to the worker.
        _start_method: The process start method; the same arguments as the guest-box target.
    """
    payload = dict(payload)
    host = payload.pop(LOOP_HOST_KEY)
    try:
        descriptor = payload["_budget_gateway_descriptor"]
        runtime = RemoteSandboxRuntime(descriptor["url"], descriptor["control_token"])
        managed_key = settings.openrouter_api_key.get_secret_value() if settings.openrouter_api_key else ""
        model = LeaseModelClient(
            host["reflection_model"], payload_token_source=host.get("token_source"), managed_key=managed_key
        )
        engine = get_job_store().engine
        objects = S3ObjectStore()
    except BaseException as error:
        safe_queue_put(event_queue, failure_event(error, traceback_text=traceback.format_exc()))
        return
    host_loop_job(
        payload,
        artifact_id,
        event_queue,
        host=host,
        engine=engine,
        objects=objects,
        runtime=runtime,
        model=model,
    )
