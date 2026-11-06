"""Pin the names the worker and the pinned sandbox image's backend copy exchange.

The DSPy guest runs the image's own ``/app`` copy of the backend, and a resumed
run keeps the image it first opened, so a newer worker talks to older guest
code (and the reverse while an image re-pin rolls out). Each name here crosses
that boundary; changing one breaks every run on an older image.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from core.billing import guest_model_proxy
from core.billing.model_gateway import ROUTE_KEY
from core.billing.usage_tags import LEGACY_USAGE_TAGS_HEADER, USAGE_TAGS_HEADER, with_tags_header
from core.worker import isolated_runner, vercel_dspy
from core.worker.sandbox_protocol import SANDBOX_PROTOCOL


def test_guest_event_framing_and_protocol_keep_their_image_names() -> None:
    """The guest frames events with the prefix older images print and the worker parses."""
    assert isolated_runner.EVENT_PREFIX == "SKYNET_JOB_EVENT "
    assert vercel_dspy.EVENT_PREFIX == isolated_runner.EVENT_PREFIX
    assert SANDBOX_PROTOCOL == 6


def test_payload_route_keys_keep_their_image_names() -> None:
    """Older guests pop these request keys by name."""
    assert ROUTE_KEY == "_skynet_budget_route"
    source = Path(isolated_runner.__file__).read_text(encoding="utf-8")
    for key in ("_skynet_tools_route", "_skynet_packages_route"):
        assert f'"{key}"' in source


def test_usage_tags_travel_under_both_header_names() -> None:
    """An older proxy forwards, and an older gateway reads, only the legacy header."""
    headers = with_tags_header({}, {"stage": "training"})
    assert headers[LEGACY_USAGE_TAGS_HEADER] == headers[USAGE_TAGS_HEADER]
    assert LEGACY_USAGE_TAGS_HEADER == "x-skynet-usage-tags"


def test_model_proxy_hands_its_command_both_relay_variable_names(tmp_path: Path) -> None:
    """The shipped proxy sets the legacy relay names that older guest code reads."""
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"prefix": "P:", "response_dir": str(tmp_path / "responses"), "timeout_seconds": 5}))
    probe = (
        "import json, os; print(json.dumps({name: os.environ.get(name) for name in "
        "('APP_BUDGET_RELAY_URL', 'SKYNET_BUDGET_RELAY_URL', 'APP_GATEWAY_URL', 'SKYNET_GATEWAY_URL')}))"
    )
    done = subprocess.run(
        [sys.executable, guest_model_proxy.__file__, str(config), sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    seen = json.loads(done.stdout.strip().splitlines()[-1])
    assert all(value and value.startswith("http://127.0.0.1:") for value in seen.values()), seen
    assert seen["APP_BUDGET_RELAY_URL"] == seen["SKYNET_BUDGET_RELAY_URL"]
