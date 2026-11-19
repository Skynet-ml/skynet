"""Scorer helpers for repository runs: score normalization, code loading and the remote adapter.

Every scorer normalizes to the engine contract ``(score, side_info)``,
carries a ``usage`` ledger the run bills (``None`` for remote scorers, whose
calls are billed by their endpoint outside Skynet Total) and a ``close()`` the
run calls when it is done. The helpers here are thin wrappers over
:mod:`.runner`, the stdlib-only module that executes scorer code in the box,
translating its errors into the worker's ``ServiceError``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

import httpx

from ....exceptions import ServiceError
from . import runner
from .protocol import RUN_FAILED_STATUS, STATUS_KEY, Candidate, SideInfo
from .remote_evaluator import RemoteEvaluatorClient
from .sandbox_scorer import ScorerUsage


class JobScorer(Protocol):
    """What a run needs from a scorer beyond scoring: its usage ledger and a way to shut it down."""

    usage: ScorerUsage | None

    def __call__(self, candidate: Candidate, case: Any = None) -> tuple[float | None, SideInfo]:
        """Score ``candidate`` on ``case``.

        Args:
            candidate: The version to score.
            case: The case to score it on, if the task has cases.

        Returns:
            The score (``None`` for a failed ``[run]`` entry) and side information.
        """
        ...

    def close(self) -> None:
        """Release whatever the scorer holds. Never raises."""
        ...


def normalize_score(raw: Any, *, own_keys: bool = False) -> tuple[float | None, SideInfo]:
    """Read a scorer's reply into ``(score, side_info)``.

    A relay reply is a document that Skynet built from a checked metric
    answer, so it may hold Skynet's own keys, such as ``status``; see
    :func:`runner.normalize_score` for the answer form. A reply with no
    score and the ``run_failed`` status is a failed ``[run]`` entry: it
    keeps no score (SKY-751).

    Args:
        raw: The scorer's reply.
        own_keys: Whether the reply comes from Skynet. Set it only for a
            document Skynet built; by default the reply is a metric answer
            and gets the full check.

    Returns:
        The float score, or ``None`` for a failed ``[run]`` entry, and side
        info carrying ``feedback`` (``""`` when none) and any named ``scores``.

    Raises:
        ServiceError: When the reply does not have the valid form.
    """
    try:
        if own_keys and isinstance(raw, dict) and raw.get("score") is None and raw.get(STATUS_KEY) == RUN_FAILED_STATUS:
            side_info = {key: value for key, value in raw.items() if key != "score"}
            side_info["feedback"] = runner.feedback(side_info.get("feedback"), "the failed run")
            if "scores" in side_info:
                side_info["scores"] = runner.named_scores(side_info["scores"])
            return None, side_info
        return runner.normalize_score(raw, own_keys=own_keys)
    except runner.ScorerError as exc:
        raise ServiceError(str(exc)) from exc


def load_scorer_from_code(code: str, *, helpers: dict[str, Any] | None = None) -> Callable[..., Any]:
    """Execute scorer source and return the callable it defines.

    Looks for ``score`` then ``metric``, then falls back to the single
    function the code defines.

    Args:
        code: User-authored python source.
        helpers: Names the scorer may import from ``skynet``; also bound in its namespace.

    Returns:
        The scorer callable.

    Raises:
        ServiceError: When the code fails to compile or load, or defines no
            unambiguous scorer function.
    """
    try:
        return runner.load_scorer_from_code(code, helpers=helpers)
    except runner.ScorerError as exc:
        raise ServiceError(str(exc)) from exc


class RemoteScorer:
    """Scorer that POSTs ``{"candidate", "case"}`` to a user-owned endpoint."""

    usage = None

    def __init__(
        self,
        url: str,
        *,
        secret: str | None,
        timeout_seconds: float,
        protected_route: dict[str, str] | None = None,
        label: str = "remote scorer",
        trusted_reply: bool = False,
    ) -> None:
        """Create a remote scorer.

        Args:
            url: Endpoint that returns ``{"score": ..., "feedback": ..., "scores": ...}``.
            secret: Shared secret sent as a bearer token, if any.
            timeout_seconds: Per-request timeout.
            protected_route: Parent-issued capability for a protected evaluator endpoint.
            label: Names the scorer in failure messages.
            trusted_reply: Whether the relay reply comes from Skynet's own
                repository scorer, which may add Skynet's keys, such as
                ``status``. A user's endpoint is never trusted.
        """
        self._url = url
        self._trusted_reply = trusted_reply
        self._secret = secret
        self._timeout_seconds = timeout_seconds
        self._relay = (
            RemoteEvaluatorClient(protected_route, timeout_seconds=timeout_seconds, label=label)
            if protected_route
            else None
        )

    def __call__(self, candidate: Candidate, case: Any = None) -> tuple[float | None, SideInfo]:
        """Score ``candidate`` on ``case`` via one HTTP request.

        Args:
            candidate: The version to score.
            case: The case to score it on, if the task has cases.

        Returns:
            The normalized score (``None`` for a failed ``[run]`` entry) and side information.

        Raises:
            ServiceError: When the request fails, returns an error status,
                a non-JSON body, or a body without a usable score.
        """
        if self._relay is not None:
            return normalize_score(self._relay(candidate, case), own_keys=self._trusted_reply)
        headers = {"Authorization": f"Bearer {self._secret}"} if self._secret else {}
        try:
            response = httpx.post(
                self._url,
                json={"candidate": candidate, "case": case},
                headers=headers,
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPError as exc:
            raise ServiceError(f"remote scorer request failed: {exc}") from exc
        except ValueError as exc:
            raise ServiceError("remote scorer returned a non-JSON body.") from exc
        # The endpoint's body is the metric answer itself, so it gets the full check.
        return normalize_score(body, own_keys=False)

    def close(self) -> None:
        """Nothing to release: each call is one stateless request."""
