"""Host a repository run on the worker's engine loop (SKY-829).

Every run uses this host (SKY-728), so no run starts a guest box for the whole job. The worker's
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
import math
import re
import shutil
import tarfile
import tempfile
import time
import traceback
from collections.abc import Callable, Mapping
from dataclasses import replace
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
from ..billing.model_dispatch import data_policy_refusal, response_usage, usage_charge
from ..billing.openrouter_quotes import fetch_endpoint_prices, price_text_request, resolve_model_slug
from ..billing.operation_pricing import ChargePolicy, UnpricedOperationError
from ..config import settings
from ..constants import TOKEN_SOURCE_BYOK, TOKEN_SOURCE_MANAGED
from ..exceptions import InfrastructureInterruptionError, ServiceError
from ..sandbox.grants import Grant, resolve_grants
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
    REPO_SNAPSHOT_KEY,
    forget_file_stats,
    unpack_tree,
    version_patch,
)
from ..service_gateway.optimization.blackbox.runner import DEFAULT_MAX_TOKENS, side_info_json_default
from ..service_gateway.optimization.blackbox.sandbox_contract import CommandResult, SandboxRuntime, SandboxSpec
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
# The time in a command's box for the tree's unpack before the command and its pack after it.
_PACK_SECONDS = 300
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL_TIMEOUT_SECONDS = 600.0
# A rate limit (429) or a busy service (503) refuses the call before the model runs, and often clears in seconds, so
# a model call tries again on these answers, unless the body names a generation or reports usage: the model dispatch
# counts such an answer as billed (see model_dispatch's ``refused``). A 502 or 504 can come after the model ran and the provider billed it,
# and the lease pays one reply only once, so these answers are final, as all other error answers are.
_MODEL_RETRY_STATUSES = frozenset({429, 503})
# These errors come before the request reaches the provider, so the provider cannot bill the try. A read, write or
# protocol error can come after the provider got the request, so it gets no new try.
_MODEL_RETRY_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
# OpenRouter's free models allow some calls each minute, so the waits together pass one minute. With the default
# stall timeout of 1800 s, a refusal that comes back fast leaves room for all three waits and a last full try in the
# deadline below.
_MODEL_BACKOFF_SECONDS = (5.0, 15.0, 40.0)
_MODEL_RETRIES = len(_MODEL_BACKOFF_SECONDS)
_MODEL_RETRY_AFTER_CAP_SECONDS = 60.0
# An epoch reset above this value is in milliseconds, as OpenRouter sends it; below it, the value is in seconds.
_RESET_MILLISECONDS_ABOVE = 1e11
# The worker forwards no log of this module, so nothing resets the stall watchdog during a model step. A new try
# starts only when it can end, at its full timeout, within this fraction of job_stall_timeout_seconds. The rest of the
# window is for the price read before the first try.
_MODEL_RETRY_STALL_FRACTION = 0.5
_PROVIDER_MESSAGE_CHARS = 200
# Tests replace these, so a retry test does not wait.
_sleep = time.sleep
_clock = time.monotonic
_wall = time.time
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
        """Call the model.

        A managed or custom endpoint call gets a new try after a rate limit (429), a busy service (503) or a
        connection error, see :meth:`_post`.

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
        answer = self._post(OPENROUTER_CHAT_URL, {**body, "usage": {"include": True}})
        if answer.status_code == 404 and b"data policy" in answer.content.lower():
            # No provider meets the setting, so every later call fails too: the loop stops with the reason.
            refusal = json.loads(data_policy_refusal(self._model, self._data_policy))
            raise UnpricedOperationError(refusal["error"]["message"])
        try:
            answer.raise_for_status()
            body = answer.json()
        except (httpx.HTTPError, ValueError) as error:
            raise self._failed(error, answer) from error
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
            answer = self._post(self._custom_url, request)
            try:
                answer.raise_for_status()
                message = answer.json()["choices"][0]["message"]
            except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as error:
                raise self._failed(error, answer) from error
        return ModelReply(str(message.get("content") or ""), Decimal(0), tool_calls_from_openai(message))

    def _post(self, url: str, body: Mapping[str, Any]) -> httpx.Response:
        """Post one model call, and try it again after an answer that a short wait can clear.

        Only an answer that refuses the call before the model runs, or an error before the request reaches the
        provider, leads to a new try, so a new try never follows a call that the provider billed. A new try starts
        only when it can end, at its full timeout, within a fraction of the stall watchdog's window. The loop saves
        the call's step only after this returns, so a resume does not replay a failure that a new try clears.

        Args:
            url: The chat completions URL.
            body: The chat request.

        Returns:
            The first answer that is not a retryable status, or the last retryable answer.

        Raises:
            ServiceError: When the last try gets no answer, or a try fails with an error that gets no new try.
        """
        started = _clock()
        for attempt in range(_MODEL_RETRIES + 1):
            answer: httpx.Response | None = None
            try:
                answer = self._client.post(url, headers={"Authorization": f"Bearer {self._key}"}, json=body)
            except _MODEL_RETRY_ERRORS as error:
                failure: httpx.HTTPError = error
            except httpx.HTTPError as error:
                raise self._failed(error) from error
            else:
                if answer.status_code not in _MODEL_RETRY_STATUSES or _maybe_billed(answer):
                    return answer
            if attempt == _MODEL_RETRIES:
                break
            wait = _retry_wait(answer, attempt)
            if _clock() - started + wait + MODEL_TIMEOUT_SECONDS > _retry_deadline():
                break
            reason = f"HTTP {answer.status_code}" if answer is not None else type(failure).__name__
            logger.warning("The model call failed with %s; starting try %d in %.1f s.", reason, attempt + 2, wait)
            _sleep(wait)
        if answer is None:
            raise self._failed(failure) from failure
        return answer

    def _failed(self, error: Exception, answer: httpx.Response | None = None) -> ServiceError:
        """Describe a failed model call with its status and the provider's own short message.

        Args:
            error: The call's error.
            answer: The provider's answer, when one came back.

        Returns:
            The error for the loop to record.
        """
        text = f"The model call failed: {type(error).__name__}"
        if answer is None or not answer.is_error:
            return ServiceError(text)
        text += f" (HTTP {answer.status_code})"
        detail = provider_message(answer.content, self._key)
        if detail:
            text = f"{text}: {detail}"
        # The engine loop records only the error's class, so the worker process log is where the provider's reason
        # shows: the job log does not get this module's records.
        logger.warning("%s", text)
        return ServiceError(text)


def _maybe_billed(answer: httpx.Response) -> bool:
    """Tell whether an error answer can follow a billed call, by the model dispatch's rule.

    Args:
        answer: The provider's answer.

    Returns:
        ``True`` when the body names a generation or reports usage, or when the body cannot be read.
    """
    try:
        identity, usage = response_usage(answer.content, answer.headers.get("content-type", ""))
    except ValueError:
        # An event stream that is not UTF-8 cannot be read, so it can hide a bill, and a new try could bill twice.
        return True
    return identity is not None or usage is not None


def _retry_deadline() -> float:
    """Return the time from a call's first try by which its last try must end.

    Returns:
        A fraction of ``job_stall_timeout_seconds``, or no limit when the stall watchdog is off.
    """
    stall = settings.job_stall_timeout_seconds
    return stall * _MODEL_RETRY_STALL_FRACTION if stall > 0 else float("inf")


def _retry_wait(answer: httpx.Response | None, attempt: int) -> float:
    """Return the wait before the next try of a model call.

    Args:
        answer: The retryable answer, or ``None`` when no answer came back.
        attempt: The index of the try that failed, from 0.

    Returns:
        The provider's numeric ``Retry-After``, or else the time to the rate limit reset in the body, up to their
        cap; or else the backoff of the try.
    """
    asked: float | None = None
    if answer is not None:
        try:
            asked = float(answer.headers["Retry-After"])
        except (KeyError, ValueError):
            asked = None
        if asked is None or not asked >= 0:
            asked = _rate_limit_reset_wait(answer.content)
    if asked is not None:
        return min(asked, _MODEL_RETRY_AFTER_CAP_SECONDS)
    return _MODEL_BACKOFF_SECONDS[attempt]


def _rate_limit_reset_wait(body: bytes) -> float | None:
    """Return the time until the rate limit resets, from OpenRouter's ``error.metadata.headers`` in the body.

    OpenRouter puts the upstream limit's headers in the 429 body, with ``X-RateLimit-Reset`` as epoch milliseconds,
    and often sends no ``Retry-After``.

    Args:
        body: The provider's error answer body.

    Returns:
        The seconds until the reset, or ``None`` when the body has no usable reset or the reset is past.
    """
    try:
        reset = float(json.loads(body)["error"]["metadata"]["headers"]["X-RateLimit-Reset"])
    except (ValueError, KeyError, TypeError):
        return None
    if not math.isfinite(reset):
        return None
    if reset > _RESET_MILLISECONDS_ABOVE:
        reset /= 1000
    wait = reset - _wall()
    return wait if wait > 0 else None


def provider_message(body: bytes, secret: str) -> str:
    """Return the provider's error message from an error answer, short, on one line and with no credential.

    Only ``error.message``, or ``error`` when it is a string, is read: other body text can echo the request, which
    holds the prompt.

    Args:
        body: The provider's error answer body.
        secret: The call's credential, which the message must never show.

    Returns:
        The message, or an empty string when the body has none.
    """
    try:
        error = json.loads(body).get("error")
    except (ValueError, AttributeError):
        return ""
    message = error.get("message") if isinstance(error, dict) else error
    if not isinstance(message, str):
        return ""
    if secret:
        message = message.replace(secret, "[key]")
    return " ".join(message.split())[:_PROVIDER_MESSAGE_CHARS]


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
        # Both tars carry the same mtime, so git's cached stats can hide edits (SKY-843).
        forget_file_stats(checkout)
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

    def __init__(
        self, loop: EngineLoop, trees: TreeStore, run_id: str, seed: RepoSeed, lifetime_seconds: float | None = None
    ) -> None:
        """Bind the job to the loop and the run's seed.

        Args:
            loop: The run's engine loop.
            trees: The run's tree store.
            run_id: The run.
            seed: The run's seed tree and path rules.
            lifetime_seconds: The longest box that the parent's broker opens for the run; ``None`` for no bound.
        """
        self._loop = loop
        self._lifetime = lifetime_seconds
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
            The context; a command with its own limit gets at most what its box can hold.
        """
        ctx = self._loop.context(repo_seed=self._seed, **fields)
        run = ctx.run_command
        assert run is not None

        def run_command(
            tree_id: str,
            command: str,
            grants: tuple[Grant, ...] = (),
            *,
            timeout_seconds: float | None = None,
            env: Mapping[str, str] | None = None,
        ) -> tuple[CommandResult, str]:
            """Run one loop command, with its limit cut so that it times out before its box dies.

            Args:
                tree_id: The tree the command starts from.
                command: The shell command.
                grants: The box's network grants.
                timeout_seconds: The command's limit; ``None`` keeps the sandbox's limit.
                env: Extra environment for this command only.

            Returns:
                The command's result and the tree id after it.
            """
            if timeout_seconds is not None:
                timeout_seconds = command_timeout_seconds(timeout_seconds, self._lifetime)
            return run(tree_id, command, grants, timeout_seconds=timeout_seconds, env=env)

        return replace(ctx, run_command=run_command)

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
            # The broker refuses a box that outlives the run's deadline, so a command box must fit in it too.
            lifetime = descriptor.get("lifetime_seconds")
            # No operation key: the box route makes each open its own request, so no two steps share a box.
            spec = SandboxSpec(lifetime_seconds=STEP_BOX_SECONDS, image=descriptor.get("image"))
            loop = EngineLoop(
                run,
                engine=engine,
                trees=trees,
                candidates=RuntimeBoxSource(
                    runtime, spec, command_lifetime=lambda timeout: command_box_seconds(timeout, lifetime)
                ),
                metrics=RuntimeBoxSource(runtime, spec),
                model=model,
                model_max_cents=MODEL_MAX_CENTS,
                run_dir=folder,
                hidden_hashes=hidden,
            )
            binding = _LoopBinding(loop, trees, run_id, seed, lifetime)
        except BaseException as error:
            safe_queue_put(event_queue, failure_event(error, traceback_text=traceback.format_exc()))
            return
        run_service_in_subprocess(payload, artifact_id, event_queue, loop=binding)
    finally:
        _close(loop.close if loop is not None else None, "engine loop")
        _close(step_log.close, "step log")
        if own_dir:
            shutil.rmtree(folder, ignore_errors=True)


def command_box_seconds(timeout_seconds: float, lifetime_seconds: float | None = None) -> float:
    """Return the lifetime of the box of a command with its own limit, such as a file proposer command (SKY-816).

    Args:
        timeout_seconds: The command's limit.
        lifetime_seconds: The longest box that the broker opens for the run, from its deadline; ``None`` for none.

    Returns:
        At least ``STEP_BOX_SECONDS``, and room for the command and the tree's unpack and pack, but never more
        than the configured sandbox lifetime or the run's box bound.
    """
    ceiling = settings.vercel_sandbox_max_lifetime_seconds
    if lifetime_seconds is not None:
        ceiling = min(ceiling, lifetime_seconds)
    return min(ceiling, max(STEP_BOX_SECONDS, timeout_seconds + _PACK_SECONDS))


def command_timeout_seconds(timeout_seconds: float, lifetime_seconds: float | None = None) -> float:
    """Return a command's limit cut to what its box holds after the tree's unpack and pack.

    Args:
        timeout_seconds: The command's own limit.
        lifetime_seconds: The longest box that the broker opens for the run; ``None`` for none.

    Returns:
        The limit, so that a long command times out with a result and does not die with its box.
    """
    room = command_box_seconds(timeout_seconds, lifetime_seconds) - _PACK_SECONDS
    return min(timeout_seconds, max(1.0, room))


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
