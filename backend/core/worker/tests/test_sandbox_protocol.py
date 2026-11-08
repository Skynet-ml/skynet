"""The sandbox protocol moves whenever the request shape the image validates moves."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel

from core.models.blackbox import BlackboxRunRequest
from core.models.unified import UnifiedRunRequest
from core.worker.sandbox_protocol import SANDBOX_PROTOCOL

# The image validates the request with its own copy of these models and
# silently drops a field it does not know: an image built before
# ``workspace.files`` existed ran every sample without the helper files and
# failed with ``ModuleNotFoundError``. Changing either shape therefore needs a
# protocol bump (so stale images are refused) and a new entry here.
REQUEST_SHAPES: dict[int, dict[str, str]] = {
    4: {"unified": "5245d5cd8aff52e8", "blackbox": "0ad0fe1fd5d08de1"},
    5: {"unified": "cbafddbba3b3a5ca", "blackbox": "bf4f97fca0d2c4be"},
    6: {"unified": "e5e53442de1fbb18", "blackbox": "818f7a80756c22a5"},
    7: {"unified": "e5e53442de1fbb18", "blackbox": "1f83ea232646c16c"},
    8: {"unified": "e5e53442de1fbb18", "blackbox": "a14c37c4d43ab71b"},
    9: {"unified": "e5e53442de1fbb18", "blackbox": "bdadc8fef05b52a2"},
    10: {"unified": "e5e53442de1fbb18", "blackbox": "bd713b1948891fa7"},
}


def _shape(node: Any) -> Any:
    """Drop the schema's prose so only fields, types and constraints remain.

    Args:
        node: A JSON-schema node.

    Returns:
        The node without descriptions, titles or examples.
    """
    if isinstance(node, dict):
        return {key: _shape(value) for key, value in node.items() if key not in ("description", "title", "examples")}
    if isinstance(node, list):
        return [_shape(value) for value in node]
    return node


def _digest(model: type[BaseModel]) -> str:
    """Return a short digest of a request model's validation shape.

    Args:
        model: The request model the image validates.

    Returns:
        The first 16 hex characters of the shape's SHA-256.
    """
    text = json.dumps(_shape(model.model_json_schema()), sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def test_request_shape_changes_bump_the_sandbox_protocol() -> None:
    """The current request shapes are the ones recorded for the current protocol."""
    current = {"unified": _digest(UnifiedRunRequest), "blackbox": _digest(BlackboxRunRequest)}

    assert REQUEST_SHAPES.get(SANDBOX_PROTOCOL) == current, (
        "The sandbox request shape changed: bump SANDBOX_PROTOCOL and record "
        f"{current} for it, so images built before the change are refused instead of dropping fields."
    )
