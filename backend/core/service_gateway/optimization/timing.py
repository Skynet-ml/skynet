"""Run stage names and the usage tags that DSPy sends with each LM request.

The stage constants name the baseline, training and evaluation stages of a
run. The module also patches DSPy's header builder, so every LM request
carries the usage tags that the calling ``usage_scope`` sets.
"""

from typing import Any

import dspy.clients.lm as dspy_lm

from ...billing.usage_tags import current_tags, with_tags_header

# Stage names used as keys in the ``stage_summary`` output and in the
# wire-level ``LMActivity`` payload. Kept stable — the frontend matches on
# these exact strings.
STAGE_BASELINE = "baseline"
STAGE_TRAINING = "training"
STAGE_EVALUATION = "evaluation"

_dspy_identifier = dspy_lm._add_dspy_identifier_to_headers


def _identifier_with_usage_tags(headers: dict[str, Any] | None = None) -> dict[str, Any]:
    """Add the calling context's usage tags to the headers DSPy sends with every LM request.

    Args:
        headers: Headers the LM call already carries.

    Returns:
        DSPy's headers plus the usage tags header when any tag is active.
    """
    merged = _dspy_identifier(headers)
    tags = current_tags()
    return with_tags_header(merged, tags) if tags else merged


# DSPy builds every completion, text and responses request's headers here,
# after the cache key is taken, so tagging cannot split cache entries.
dspy_lm._add_dspy_identifier_to_headers = _identifier_with_usage_tags
