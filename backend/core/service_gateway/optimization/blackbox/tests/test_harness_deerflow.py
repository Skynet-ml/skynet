"""Tests for the DeerFlow harness: gateway-only launch, pins, protected mode and output parsing."""

from __future__ import annotations

import json

from ..harness_bridge import parse_pi_output

# Synthetic stdout of one runner turn: DeerFlow log noise around the single
# Pi-shaped event the runner prints. No real model, key or account produced it.
_RECORDED_STDOUT = "\n".join(
    [
        "INFO deerflow.config: loaded config.yaml",
        "not json at all",
        json.dumps(
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Fixed the off-by-one in pager.py; tests pass."}],
                    "usage": {"input": 2210, "output": 388},
                },
            }
        ),
        "",
    ]
)


def test_parser_reads_the_recorded_runner_output() -> None:
    """Turn the recorded runner stdout into the answer and token counts."""
    assert parse_pi_output(_RECORDED_STDOUT) == (
        "Fixed the off-by-one in pager.py; tests pass.",
        {"input_tokens": 2210, "output_tokens": 388},
    )
