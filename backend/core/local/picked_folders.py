"""Check a local folder path against the folders the user picked in the desktop app (SKY-1007).

The desktop main process writes every folder that the user picks or creates
to ``<local state dir>/picked-folders.json``, a JSON list of absolute paths.
The web page cannot write that file, so the local folder routes read only a
folder on the list or below an entry.
"""

from __future__ import annotations

import json
from pathlib import Path

PICKED_FOLDERS_FILE = "picked-folders.json"


def _picked_roots(state_dir: str) -> list[Path]:
    """Read the picked folders, resolved.

    Args:
        state_dir: The desktop app's local state folder.

    Returns:
        The resolved absolute entries. A missing or damaged list gives none.
    """
    try:
        entries = json.loads((Path(state_dir) / PICKED_FOLDERS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(entries, list):
        return []
    roots: list[Path] = []
    for entry in entries:
        if not isinstance(entry, str) or not Path(entry).is_absolute():
            continue
        try:
            roots.append(Path(entry).resolve())
        except (OSError, ValueError, RuntimeError):
            continue
    return roots


def is_picked(path: str | Path, state_dir: str) -> bool:
    """Tell whether a folder is a picked folder or below one, after both are resolved.

    Args:
        path: The folder's absolute path.
        state_dir: The desktop app's local state folder; empty refuses every path.

    Returns:
        True when the resolved path is a picked folder or inside one.
    """
    if not state_dir or not Path(path).is_absolute():
        return False
    try:
        resolved = Path(path).resolve()
    except (OSError, ValueError, RuntimeError):
        return False
    return any(resolved.is_relative_to(root) for root in _picked_roots(state_dir))
