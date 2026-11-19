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

from ..billing.budgets import BudgetService
from ..billing.byok_upstreams import resolve_upstream
from ..billing.data_policy import DEFAULT_DATA_POLICY, DataPolicy, apply_data_policy
from ..billing.mcp_broker import _pinned_address, _PinnedTransport
from ..billing.model_dispatch import data_policy_refusal, usage_charge
from ..billing.openrouter_quotes import fetch_endpoint_prices, price_text_request, resolve_model_slug
from ..billing.operation_pricing import ChargePolicy, UnpricedOperationError
from ..config import settings
from ..constants import TOKEN_SOURCE_BYOK, TOKEN_SOURCE_MANAGED
from ..exceptions import InfrastructureInterruptionError, ServiceError
from ..sandbox.grants import resolve_grants
from ..sandbox.trees import tar_to_tree, tree_to_tar
from ..service_gateway.optimization.blackbox.protocol import (
    Candidate,
    EngineContext,
    ModelReply,
    RepoSeed,
    ScorerFn,
    SideInfo,
    ToolDefinition,
    openai_tools,
    tool_calls_from_openai,
)
from ..service_gateway.optimization.blackbox.remote_sandbox import RemoteSandboxRuntime
from ..service_gateway.optimization.blackbox.repo_tree import (
    BASE_REF,
    REPO_SNAPSHOT_KEY,
    _git,
    unpack_tree,
    version_patch,
)
from ..service_gateway.optimization.blackbox.runner import DEFAULT_MAX_TOKENS, side_info_json_default
from ..service_gateway.optimization.blackbox.sandbox_contract import SandboxRuntime, SandboxSpec
from ..service_gateway.optimization.blackbox.tree_sandbox import RuntimeBoxSource
from ..storage import get_job_store
from ..storage.base import ObjectStore
from ..storage.object_store import S3ObjectStore
from ..storage.tree_store import TreeStore
from .actors import User
from .engine_loop import EngineLoop, ModelCall, ModelCallBilledError, Prompt
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
_RETRYABLE_STATUSES = frozenset({408, 424, 429})
# Only these answers say that the version itself is bad; any other failure says nothing of the version.
_REFUSED_STATUSES = frozenset({400, 422})


class LeaseModelClient:
    """Call the run's proposer model with no budget hold: the loop's lease pays for each call."""

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        payload_token_source: str | None,
        managed_key: str,
        data_policy: DataPolicy = DEFAULT_DATA_POLICY,
        client: httpx.Client | None = None,
    ) -> None:
        """Bind the client to the run's resolved proposer model.

        Args:
            config: The run's ``reflection_model_config`` with its resolved credentials.
            payload_token_source: The run's own ``token_source``, for a model config that names none.
            managed_key: The worker's OpenRouter key, for a managed model.
            data_policy: The owner's model data privacy setting, for a managed call.
            client: The HTTP client of managed calls; tests give a fake transport.

        Raises:
            ValueError: When the source is not managed or BYOK, the model has no key, a BYOK call would owe a
                Skynet fee, which this client cannot measure, the BYOK provider is not supported, or a custom
                endpoint is not a public HTTPS address.
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
        self._data_policy = data_policy
        self._owns_client = client is None
        model = str(extra.get("model") or config.get("name") or "")
        self._api_base = extra.get("api_base") or extra.get("base_url") or config.get("base_url")
        # A managed call's output limit comes from the price bound of each call, see _bounded.
        self._max_tokens = int(config.get("max_tokens") or DEFAULT_MAX_TOKENS)
        names = ("temperature",) if source == TOKEN_SOURCE_MANAGED else ("temperature", "max_tokens")
        self._options = {name: config[name] for name in names if config.get(name) is not None}
        if extra.get("reasoning_effort"):
            self._options["reasoning_effort"] = extra["reasoning_effort"]
        # Only a custom BYOK endpoint goes through this URL; the other BYOK providers go through LiteLLM.
        self._custom_url: str | None = None
        if source == TOKEN_SOURCE_MANAGED:
            self._client = client or httpx.Client(timeout=MODEL_TIMEOUT_SECONDS)
            try:
                # The price catalog names the model by its full slug.
                self._model = resolve_model_slug(model, client=self._client)
            except BaseException:
                self.close()
                raise
            return
        # LiteLLM needs the provider prefix to route a BYOK call.
        self._model = model
        upstream = resolve_upstream(config)
        if upstream.slug != "custom":
            self._client = client or httpx.Client(timeout=MODEL_TIMEOUT_SECONDS)
        else:
            # The user names this host, so each call goes to the one public address that the check accepted.
            address = _pinned_address(upstream.base_url, False, label="custom provider endpoint")
            self._owns_client = True
            self._client = httpx.Client(
                transport=_PinnedTransport(upstream.base_url, address),
                timeout=MODEL_TIMEOUT_SECONDS,
                follow_redirects=False,
                trust_env=False,
            )
            self._custom_url = f"{upstream.base_url}/chat/completions"
            self._model = model.removeprefix("openai/")

    def close(self) -> None:
        """Close the HTTP client that this model client opened; a client that the caller gave stays open."""
        if self._owns_client:
            self._client.close()

    def __call__(self, prompt: Prompt, tools: list[ToolDefinition] | None) -> ModelReply:
        """Call the model once.

        Args:
            prompt: The prompt text, or the chat messages.
            tools: The tool definitions, or ``None`` for no tools.

        Returns:
            The reply, with the provider's cost in ledger cents; 0 for a BYOK call.

        Raises:
            ServiceError: When a managed or custom endpoint call fails, or a managed reply gives no complete cost.
                The loop then records a failed call with no cost.
            ModelCallBilledError: When a managed reply that gave its cost cannot be read. The loop records a
                failed call with that cost.
            UnpricedOperationError: When no provider meets the owner's data policy, the model has no verified
                price, or the prompt alone can cost more than one call's bound. The loop stops.
            Exception: LiteLLM's own errors for a BYOK call; the loop records a failed call with no cost.
        """
        messages = [{"role": "user", "content": prompt}] if isinstance(prompt, str) else list(prompt)
        request: dict[str, Any] = {"model": self._model, "messages": messages, **self._options}
        if tools:
            request["tools"] = openai_tools(tools)
        if self._source == TOKEN_SOURCE_BYOK:
            return self._byok(request)
        try:
            catalog = fetch_endpoint_prices(self._model, client=self._client)
        except (httpx.HTTPError, json.JSONDecodeError) as error:
            raise ServiceError(f"The model price read failed: {type(error).__name__}") from error
        body = self._bounded(apply_data_policy(request, self._data_policy), catalog)
        try:
            answer = self._client.post(
                OPENROUTER_CHAT_URL,
                headers={"Authorization": f"Bearer {self._key}"},
                json={**body, "usage": {"include": True}},
            )
        except httpx.HTTPError as error:
            raise ServiceError(f"The model call failed: {type(error).__name__}") from error
        if answer.status_code == 404 and b"data policy" in answer.content.lower():
            # No provider meets the setting, so every later call fails too: the loop stops with the reason.
            refusal = json.loads(data_policy_refusal(self._model, self._data_policy))
            raise UnpricedOperationError(refusal["error"]["message"])
        try:
            answer.raise_for_status()
            body = answer.json()
        except (httpx.HTTPError, ValueError) as error:
            raise ServiceError(f"The model call failed: {type(error).__name__}") from error
        usd = usage_charge(body.get("usage") or {})
        if usd is None:
            raise ServiceError("The model reply gave no complete cost.")
        cents = ChargePolicy("managed_model").convert(usd).total
        try:
            message = body["choices"][0]["message"]
            return ModelReply(str(message.get("content") or ""), cents, tool_calls_from_openai(message))
        except (KeyError, IndexError, TypeError, AttributeError, ValueError) as error:
            # The provider billed the call, so the lease pays its cost even with no reply to read.
            raise ModelCallBilledError(cents) from error

    def _bounded(self, request: Mapping[str, Any], catalog: Mapping[str, Any]) -> dict[str, Any]:
        """Give the request the largest output limit whose price bound fits one call's bound.

        A call that costs more than :data:`MODEL_MAX_CENTS` stops the loop, so the bound of each call must fit.

        Args:
            request: The routed request, without an output limit.
            catalog: The model's current endpoint prices.

        Returns:
            The request with its output limit and the price caps that hold the provider to the bound.

        Raises:
            UnpricedOperationError: When the model has no verified price, or no output limit fits.
        """
        policy = ChargePolicy("managed_model")
        low, high = 1, self._max_tokens
        fitted: dict[str, Any] | None = None
        refusal: UnpricedOperationError | None = None
        # The run's own limit comes first, so a call that fits costs one price check.
        tokens = high
        while low <= high:
            try:
                priced = price_text_request({**request, "max_tokens": tokens}, catalog, policy)
            except UnpricedOperationError as error:
                priced, refusal = None, error
            if priced is not None and priced.quote.maximum.total <= MODEL_MAX_CENTS:
                fitted, low = priced.body, tokens + 1
            else:
                high = tokens - 1
            tokens = (low + high) // 2
        if fitted is None:
            raise refusal or UnpricedOperationError("The prompt alone can cost more than one model call may.")
        return fitted

    def _byok(self, request: dict[str, Any]) -> ModelReply:
        """Call the user's provider with the user's key.

        Args:
            request: The chat request.

        Returns:
            The reply, at no cost: the provider bills the user's key.

        Raises:
            ServiceError: When a custom endpoint call fails.
        """
        if self._custom_url is None:
            response = litellm.completion(api_key=self._key, api_base=self._api_base, **request)
            message = response.choices[0].message.model_dump()
        else:
            try:
                answer = self._client.post(
                    self._custom_url, headers={"Authorization": f"Bearer {self._key}"}, json=request
                )
                answer.raise_for_status()
                message = answer.json()["choices"][0]["message"]
            except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as error:
                raise ServiceError(f"The model call failed: {type(error).__name__}") from error
        return ModelReply(str(message.get("content") or ""), Decimal(0), tool_calls_from_openai(message))


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
        # Both tars carry the same mtime, and Linux reuses the freed inode, so a
        # same-size edit keeps the stat that git cached and ``git add`` skips it
        # (SKY-843). An index read from the tree has no stat, so git hashes each file.
        _git(["read-tree", BASE_REF], checkout)
        return version_patch(checkout)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _status(error: ServiceError) -> int | None:
    """Return the HTTP status of a scorer error.

    Args:
        error: The scorer's error.

    Returns:
        The status of the relay's answer, or ``None`` when no answer came.
    """
    cause = error.__cause__
    if isinstance(cause, httpx.HTTPStatusError):
        return cause.response.status_code
    found = _HTTP_STATUS.search(str(error))
    return int(found.group(1)) if found is not None else None


def _retryable(error: ServiceError) -> bool:
    """Report whether a scorer error can pass on a new try, so the run must requeue and not record it.

    Args:
        error: The scorer's error.

    Returns:
        True for a transport error, a server error, a timeout, a pending usage or a rate limit.
    """
    status = _status(error)
    if status is None:
        return isinstance(error.__cause__, httpx.HTTPError | OSError)
    return status >= 500 or status in _RETRYABLE_STATUSES


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

        Raises:
            InfrastructureInterruptionError: When the scorer failed in a way that can pass on a new try; the loop
                stops, and the run requeues.
            Exception: Any other scorer failure that is not a refusal; the loop stops.
        """
        patch = self._patch(candidate)
        try:
            score, side_info = base(patch, case)
        except ServiceError as error:
            if _status(error) in _REFUSED_STATUSES:
                return {"refused": str(error)}
            if _retryable(error):
                raise InfrastructureInterruptionError(f"The repository scorer failed: {error}") from error
            raise
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
    own_dir = run_dir is None
    folder = run_dir or Path(tempfile.mkdtemp(prefix=f"engine-loop-{run_id[:8]}-"))
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
                run_dir=folder,
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
        if own_dir:
            shutil.rmtree(folder, ignore_errors=True)


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
    model: LeaseModelClient | None = None
    try:
        try:
            descriptor = payload["_budget_gateway_descriptor"]
            runtime = RemoteSandboxRuntime(descriptor["url"], descriptor["control_token"])
            managed_key = settings.openrouter_api_key.get_secret_value() if settings.openrouter_api_key else ""
            model = LeaseModelClient(
                host["reflection_model"],
                payload_token_source=host.get("token_source"),
                managed_key=managed_key,
                data_policy=host.get("data_policy", DEFAULT_DATA_POLICY),
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
    finally:
        _close(model.close if model is not None else None, "model client")
