"""Tests for the scorer adapters: normalization, code loading and remote endpoints."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from core.exceptions import ServiceError

from .. import scorer as scorer_mod
from ..scorer import RemoteScorer, load_scorer_from_code, normalize_score


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"score": 0.75, "feedback": "f"}, (0.75, {"feedback": "f"})),
        ({"score": 0.75}, (0.75, {"feedback": ""})),
        ({"score": 0.75, "feedback": "f", "status": "x"}, (0.75, {"feedback": "f", "status": "x"})),
    ],
)
def test_normalize_score_reads_a_relay_reply_with_skynet_keys(raw: Any, expected: tuple[float, dict[str, Any]]) -> None:
    """A relay reply is Skynet's document: it may hold Skynet's own keys, and no feedback is ``""``.

    Args:
        raw: The relay reply.
        expected: The normalized pair.
    """
    assert normalize_score(raw, own_keys=True) == expected


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        ("0.5", "must be an object, not str"),
        (None, "must be an object, not NoneType"),
        (0.5, "must be an object, not a bare number"),
        ((0.5, "f"), "must be an object, not a pair"),
        ({"feedback": "no score"}, 'has no "score"'),
        ({"score": "high"}, "must be a finite number"),
    ],
)
def test_normalize_score_rejects_a_reply_that_is_not_an_answer_object(raw: Any, match: str) -> None:
    """A reply that is not an object with a finite score is an error.

    Args:
        raw: An unsupported reply.
        match: A part of the expected message.
    """
    with pytest.raises(ServiceError, match=match):
        normalize_score(raw)


def test_normalize_score_checks_every_key_of_a_metric_answer() -> None:
    """By default the reply is the metric answer itself, so Skynet's keys are unknown keys too."""
    with pytest.raises(ServiceError, match="unknown key `status`"):
        normalize_score({"score": None, "feedback": "x", "status": "run_failed"})
    with pytest.raises(ServiceError, match="unknown key `illegal_approach`"):
        normalize_score({"score": 0.5, "illegal_approach": True})
    with pytest.raises(ServiceError, match="Did you mean `feedback`"):
        normalize_score({"score": 0.5, "feedbak": "x"})


def test_normalize_score_keeps_no_score_for_a_failed_run_with_feedback() -> None:
    """The run-failed answer that Skynet builds passes the second read with its ``status`` (SKY-751, SKY-769)."""
    raw = {"score": None, "feedback": "Run failed: boom", "status": "run_failed"}

    assert normalize_score(raw, own_keys=True) == (None, {"feedback": "Run failed: boom", "status": "run_failed"})


@pytest.mark.parametrize("feedback", [None, "", "   "])
def test_normalize_score_keeps_a_failed_run_without_feedback(feedback: str | None) -> None:
    """A failed ``[run]`` entry with no feedback passes, with ``""`` as its feedback (SKY-769).

    Args:
        feedback: Missing or blank feedback.
    """
    raw: dict[str, Any] = {"score": None, "status": "run_failed"}
    if feedback is not None:
        raw["feedback"] = feedback

    assert normalize_score(raw, own_keys=True) == (None, {"status": "run_failed", "feedback": ""})


@pytest.mark.parametrize(
    ("scores", "match"),
    [
        ({"acc": {"score": "bad", "feedback": "x"}}, "of the named score 'acc' must be a finite number"),
        ({"acc": 0.5}, "the named score 'acc' is a bare number"),
        ({"acc": (1, "ok")}, "the named score 'acc' is a pair"),
        ("not a mapping", '"scores" must be an object that maps each name to a score object'),
    ],
)
def test_normalize_score_rejects_bad_named_scores_on_a_failed_run_like_a_scored_one(scores: Any, match: str) -> None:
    """A failed ``[run]`` entry validates its named scores like a scored result does (SKY-751).

    Args:
        scores: A malformed ``scores`` value.
        match: A part of the expected named-score message.
    """
    failed = {"score": None, "status": "run_failed", "feedback": "x", "scores": scores}
    scored = {"score": 0.5, "feedback": "x", "scores": scores}

    for raw in (failed, scored):
        with pytest.raises(ServiceError, match=match):
            normalize_score(raw, own_keys=True)


def test_normalize_score_keeps_valid_named_scores_on_a_failed_run() -> None:
    """A failed ``[run]`` entry keeps its valid named scores in the normalized form (SKY-751)."""
    raw = {"score": None, "status": "run_failed", "feedback": "x", "scores": {"acc": {"score": 1, "feedback": "ok"}}}

    assert normalize_score(raw, own_keys=True) == (
        None,
        {"status": "run_failed", "feedback": "x", "scores": {"acc": {"score": 1.0, "feedback": "ok"}}},
    )


def test_load_scorer_prefers_score_then_metric_then_single_function() -> None:
    """Entrypoint lookup order: ``score``, ``metric``, the only function defined."""
    both = "def metric(c, x=None): return 0.0\ndef score(c, x=None): return 1.0\n"
    metric_only = "def metric(c, x=None): return 0.5\n"
    single = "import math\ndef judge(c, x=None): return 0.75\n"

    assert load_scorer_from_code(both)("x") == 1.0
    assert load_scorer_from_code(metric_only)("x") == 0.5
    assert load_scorer_from_code(single)("x") == 0.75


def test_load_scorer_ignores_imported_functions_when_picking_the_single_function() -> None:
    """Functions imported into the namespace do not count as the user's scorer."""
    code = "from math import sqrt\ndef judge(c, x=None): return 1.0\n"

    assert load_scorer_from_code(code)("x") == 1.0


@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("def !!!", "syntax error"),
        ("raise RuntimeError('boom')", "failed to load: RuntimeError: boom"),
        ("x = 1", "must define a function named 'score"),
        ("def a(c): return 1\ndef b(c): return 2\n", "must define a function named 'score"),
    ],
)
def test_load_scorer_reports_unusable_code(code: str, message: str) -> None:
    """Broken or ambiguous scorer code raises a ``ServiceError`` naming the problem.

    Args:
        code: The scorer source.
        message: Expected fragment of the error.
    """
    with pytest.raises(ServiceError, match=message):
        load_scorer_from_code(code)


class _FakeResponse:
    """Minimal stand-in for ``httpx.Response``."""

    def __init__(self, body: Any, *, status_error: bool = False, non_json: bool = False) -> None:
        """Create the fake.

        Args:
            body: What ``json()`` returns.
            status_error: Whether ``raise_for_status`` raises.
            non_json: Whether ``json()`` raises ``ValueError``.
        """
        self._body = body
        self._status_error = status_error
        self._non_json = non_json

    def raise_for_status(self) -> None:
        """Raise ``httpx.HTTPStatusError`` when configured to."""
        if self._status_error:
            request = httpx.Request("POST", "https://scorer.example/score")
            raise httpx.HTTPStatusError("500", request=request, response=httpx.Response(500, request=request))

    def json(self) -> Any:
        """Return the body, or raise when configured as non-JSON."""
        if self._non_json:
            raise ValueError("not json")
        return self._body


def test_remote_scorer_posts_candidate_and_case_with_bearer_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """The remote adapter sends the documented body and auth header and normalizes the reply."""
    captured: dict[str, Any] = {}

    def fake_post(url: str, *, json: Any, headers: dict[str, str], timeout: float) -> _FakeResponse:
        """Record the request and answer with a scored body."""
        captured.update(url=url, json=json, headers=headers, timeout=timeout)
        return _FakeResponse({"score": 0.9, "feedback": "fine"})

    monkeypatch.setattr(scorer_mod.httpx, "post", fake_post)
    scorer = RemoteScorer("https://scorer.example/score", secret="s3cret", timeout_seconds=7.5)

    assert scorer("hello", {"k": 1}) == (0.9, {"feedback": "fine"})
    assert captured == {
        "url": "https://scorer.example/score",
        "json": {"candidate": "hello", "case": {"k": 1}},
        "headers": {"Authorization": "Bearer s3cret"},
        "timeout": 7.5,
    }
    assert scorer.usage is None
    scorer.close()


def test_remote_scorer_omits_auth_header_without_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """No secret means no ``Authorization`` header."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        scorer_mod.httpx,
        "post",
        lambda url, **kw: captured.update(kw) or _FakeResponse({"score": 0.5, "feedback": "f"}),
    )

    assert RemoteScorer("https://scorer.example", secret=None, timeout_seconds=1)("x") == (0.5, {"feedback": "f"})
    assert captured["headers"] == {}


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (_FakeResponse(None, status_error=True), "remote scorer request failed"),
        (_FakeResponse(None, non_json=True), "non-JSON body"),
        (_FakeResponse({"feedback": "no score"}), 'has no "score"'),
        (_FakeResponse(0.5), "must be an object, not a bare number"),
        (_FakeResponse({"score": 0.5, "predicted": 41}), "unknown key `predicted`"),
        (_FakeResponse({"score": None, "status": "run_failed"}), "unknown key `status`"),
    ],
)
def test_remote_scorer_reports_bad_replies(
    monkeypatch: pytest.MonkeyPatch, response: _FakeResponse, message: str
) -> None:
    """HTTP errors, non-JSON bodies and wrong metric answers all become ``ServiceError``.

    Args:
        monkeypatch: Pytest fixture.
        response: The canned reply.
        message: Expected fragment of the error.
    """
    monkeypatch.setattr(scorer_mod.httpx, "post", lambda *a, **kw: response)

    with pytest.raises(ServiceError, match=message):
        RemoteScorer("https://scorer.example", secret=None, timeout_seconds=1)("x")


def test_remote_scorer_wraps_transport_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Connection failures surface as ``ServiceError`` too."""

    def fail(*args: Any, **kwargs: Any) -> None:
        """Simulate a dead endpoint."""
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(scorer_mod.httpx, "post", fail)

    with pytest.raises(ServiceError, match="connection refused"):
        RemoteScorer("https://scorer.example", secret=None, timeout_seconds=1)("x")


def _relay_scorer(reply: dict[str, Any], *, trusted_reply: bool) -> RemoteScorer:
    """Build a protected remote scorer whose relay returns ``reply``.

    Args:
        reply: What the parent relay returns.
        trusted_reply: Whether the scorer is Skynet's own repository scorer.

    Returns:
        The scorer, with its relay replaced by a fake.
    """
    scorer = RemoteScorer(
        "https://scorer.example",
        secret=None,
        timeout_seconds=1,
        protected_route={"url": "http://127.0.0.1:9/v1", "token": "t"},
        trusted_reply=trusted_reply,
    )
    scorer._relay = lambda candidate, case: reply  # type: ignore[assignment]
    return scorer


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        ({"score": 0.5, "predicted": 41}, "unknown key `predicted`"),
        ({"score": None, "feedback": "x", "status": "run_failed"}, "unknown key `status`"),
    ],
)
def test_protected_relay_checks_a_user_endpoint_reply_like_a_metric_answer(reply: dict[str, Any], message: str) -> None:
    """A relayed reply from a user's endpoint is a metric answer: Skynet's keys are unknown keys (SKY-769).

    Args:
        reply: The endpoint's reply that the relay returns.
        message: Expected fragment of the error.
    """
    with pytest.raises(ServiceError, match=message):
        _relay_scorer(reply, trusted_reply=False)("x")


def test_trusted_relay_keeps_skynet_keys_of_the_repository_scorer() -> None:
    """Skynet's own repository scorer may reply with a failed ``[run]`` entry and its ``status`` (SKY-751)."""
    reply = {"score": None, "feedback": "Run failed: boom", "status": "run_failed"}

    assert _relay_scorer(reply, trusted_reply=True)("x") == (
        None,
        {"feedback": "Run failed: boom", "status": "run_failed"},
    )
