"""Resolve a black-box scorer's metric model before its credentials are bound."""

from __future__ import annotations

from typing import Any

from ..i18n_en import t_en
from ..service_gateway.optimization.unified.metric_model import resolve_blackbox_metric_model_dict


def alias_blackbox_metric_model(payload_dict: dict[str, Any]) -> None:
    """Resolve a black-box scorer before its model credentials are bound.

    Args:
        payload_dict: The raw black-box payload, mutated in place.

    Raises:
        ValueError: When the metric declaration is invalid.
    """
    error = resolve_blackbox_metric_model_dict(payload_dict)
    if error:
        raise ValueError(t_en(error))
