"""Tests for the NeMo Fabric harness: registry entry, pinned install, gateway-only config, protected env and parsing."""

from __future__ import annotations

from core.models.blackbox import BLACKBOX_HARNESS_FABRIC

from ..harnesses import REGISTRY

# Stand-in for uv: ``--version`` answers as the pinned release does, ``venv``
# makes a real stdlib venv, and ``pip install`` copies the fake Fabric package
# into it and logs the requested pins. No network.
_FAKE_UV = """#!/bin/sh
if [ "$1" = "--version" ]; then echo "uv 0.13.7 (0123abcd 2026-01-01)"; exit 0; fi
printf '%s\\n' "$*" >> "$FAKE_UV_LOG"
if [ "$1" = "venv" ]; then exec "$REAL_PYTHON" -m venv --without-pip "$4"; fi
if [ "$1" = "pip" ]; then
  exec "$4" -c \\
    'import shutil, sys, sysconfig; '\\
'shutil.copytree(sys.argv[1], sysconfig.get_paths()["purelib"], dirs_exist_ok=True)' \\
    "$FAKE_PACKAGE"
fi
exit 2
"""


def test_spec_declares_the_six_codex_efforts_for_the_submit_check() -> None:
    """Declare exactly the Codex efforts on the spec, so submit refuses any other effort before paid work."""
    assert REGISTRY[BLACKBOX_HARNESS_FABRIC].supported_efforts == ("none", "minimal", "low", "medium", "high", "xhigh")
