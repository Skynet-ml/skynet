"""Tests for the Hermes Agent harness: registry entry, launch, install pin, protected build and output parsing."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

from core.models.blackbox import BLACKBOX_HARNESSES

from ..harnesses import REGISTRY
from ..harnesses.hermes import (
    _WHEEL_FETCH,
    HERMES_VERSION,
    SPEC,
)

_FAKE_UV = """#!/bin/sh
{ printf 'uv'; for a in "$@"; do printf ' [%s]' "$a"; done; printf '\\n'; } >> "$FAKE_LOG"
mkdir -p "$HOME/.local/bin"
printf '#!/bin/sh\\necho "Hermes Agent v0.19.0 (2026.7.20)"\\n' > "$HOME/.local/bin/hermes"
chmod +x "$HOME/.local/bin/hermes"
"""


def _fetch(tmp_path: Path, sha: str) -> subprocess.CompletedProcess[str]:
    """Run the install's wheel fetch on a local file URL.

    Args:
        tmp_path: Test folder.
        sha: Expected sha256 digest.

    Returns:
        The finished process.
    """
    source = tmp_path / "source.whl"
    source.write_bytes(b"wheel bytes")
    return subprocess.run(
        [sys.executable, "-I", "-c", _WHEEL_FETCH, source.as_uri(), sha, str(tmp_path / "out.whl")],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def test_registry_lists_hermes_after_goose_with_its_pin_and_plain_parser() -> None:
    """Hermes registers after goose and before cline, with the version token its ``--version`` prints."""
    assert REGISTRY["hermes"] is SPEC
    assert SPEC.binary == "hermes"
    assert HERMES_VERSION == "0.19.0"
    assert SPEC.version == "v0.19.0"
    assert SPEC.output_format == "plain"
    assert SPEC.position == 65
    ids = list(REGISTRY)
    assert ids.index("goose") < ids.index("hermes") < ids.index("cline")
    assert BLACKBOX_HARNESSES.index("goose") < BLACKBOX_HARNESSES.index("hermes") < BLACKBOX_HARNESSES.index("cline")


def test_wheel_fetch_keeps_a_matching_file_and_rejects_a_mismatch(tmp_path: Path) -> None:
    """The fetch writes the wheel only when its sha256 matches the pin."""
    good = _fetch(tmp_path, hashlib.sha256(b"wheel bytes").hexdigest())
    assert good.returncode == 0
    assert (tmp_path / "out.whl").read_bytes() == b"wheel bytes"
    (tmp_path / "out.whl").unlink()

    bad = _fetch(tmp_path, "0" * 64)
    assert bad.returncode != 0
    assert "sha256 mismatch" in bad.stderr
    assert not (tmp_path / "out.whl").exists()


def test_spec_declares_only_the_skynet_effort_levels() -> None:
    """Submit sees the seven Skynet levels Hermes accepts; ``ultra`` is not one of them."""
    assert SPEC.supported_efforts == ("none", "minimal", "low", "medium", "high", "xhigh", "max")
    assert "ultra" not in SPEC.supported_efforts
