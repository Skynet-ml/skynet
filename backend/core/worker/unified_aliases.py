"""Legacy role aliases for a unified payload's nested model configs."""

from __future__ import annotations

from typing import Any

from ..i18n_en import t_en
from ..service_gateway.optimization.unified.metric_model import resolve_metric_model_dict

# The BYOK and protected-route seams only walk the legacy top-level model
# keys; a unified payload nests its models under ``models``, so the worker and
# the setup check expose those same dict objects under the legacy keys
# (mutated in place by the seams) and drop the aliases before the payload
# crosses into the guest.
_UNIFIED_MODEL_ALIASES: tuple[tuple[str, str], ...] = (
    ("task_model", "task_model_config"),
    ("reflection_model", "reflection_model_config"),
)
_UNIFIED_ALIAS_KEYS = ("task_model_config", "reflection_model_config", "extra_reflection_model_configs", "scorer")


def alias_unified_model_roles(payload_dict: dict[str, Any], *, preserve_metric_model: bool = False) -> None:
    """Expose a unified payload's nested model configs under the legacy role keys.

    Args:
        payload_dict: The raw unified payload, mutated in place.
        preserve_metric_model: Keep a stored model when legacy code declares none.
    """
    models = payload_dict.get("models")
    if not isinstance(models, dict):
        return
    error = resolve_metric_model_dict(payload_dict, preserve_metric_model=preserve_metric_model)
    if error:
        raise ValueError(t_en(error))
    for nested_key, legacy_key in _UNIFIED_MODEL_ALIASES:
        if isinstance(models.get(nested_key), dict):
            payload_dict[legacy_key] = models[nested_key]
    extras = models.get("extra_reflection_models")
    if isinstance(extras, list):
        payload_dict["extra_reflection_model_configs"] = extras
    if isinstance(models.get("metric_model"), dict):
        payload_dict["scorer"] = {"kind": "python", "model": models["metric_model"]}
    else:
        payload_dict.pop("scorer", None)


def strip_unified_model_aliases(payload_dict: dict[str, Any]) -> None:
    """Drop the legacy role aliases added by :func:`alias_unified_model_roles`.

    Args:
        payload_dict: The raw unified payload, mutated in place.
    """
    for key in _UNIFIED_ALIAS_KEYS:
        payload_dict.pop(key, None)
