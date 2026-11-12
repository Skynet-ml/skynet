"""Tests for the usage-tags header patch in ``core.service_gateway.optimization.timing``."""

from __future__ import annotations

import contextvars

import dspy.clients.lm as dspy_lm

from core.billing.usage_tags import USAGE_TAGS_HEADER, header_tags, usage_scope
from core.service_gateway.optimization.timing import STAGE_TRAINING


def test_timed_calls_carry_stage_usage_tags() -> None:
    """Tag the headers DSPy builds inside a usage scope, and only inside it."""
    contextvars.Context().run(_assert_scoped_call_tagged)


def _assert_scoped_call_tagged() -> None:
    """Check tagging from a context no earlier test has left tags open in."""
    with usage_scope(stage=STAGE_TRAINING):
        headers = dspy_lm._add_dspy_identifier_to_headers({"X-Other": "1"})
    assert headers["X-Other"] == "1"
    assert header_tags(headers) == {"stage": STAGE_TRAINING}
    assert USAGE_TAGS_HEADER not in dspy_lm._add_dspy_identifier_to_headers(None)


def test_untimed_lms_are_not_tagged() -> None:
    """Leave calls outside a usage scope untagged."""
    contextvars.Context().run(_assert_unscoped_call_untagged)


def _assert_unscoped_call_untagged() -> None:
    """Check that a call with no usage scope gets no tags header, from a clean context."""
    headers = dspy_lm._add_dspy_identifier_to_headers(None)
    assert USAGE_TAGS_HEADER not in headers
