"""Tests for the picked-folder check of the local folder routes (SKY-1007)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.local.picked_folders import PICKED_FOLDERS_FILE, is_picked


def _pick(state_dir: Path, *folders: Path | str) -> str:
    """Write the picked-folder list as the desktop app does.

    Args:
        state_dir: The local state folder.
        folders: The picked folders.

    Returns:
        The state folder as a string, for :func:`is_picked`.
    """
    state_dir.mkdir(exist_ok=True)
    (state_dir / PICKED_FOLDERS_FILE).write_text(json.dumps([str(f) for f in folders]), encoding="utf-8")
    return str(state_dir)


@pytest.fixture
def picked(tmp_path: Path) -> Path:
    """Make a picked folder with a subfolder.

    Args:
        tmp_path: Pytest's temporary directory.

    Returns:
        The picked folder.
    """
    folder = tmp_path / "picked"
    (folder / "sub" / "deep").mkdir(parents=True)
    return folder


def test_a_picked_folder_and_any_folder_below_it_pass(tmp_path: Path, picked: Path) -> None:
    """Accept the entry itself and every folder below it."""
    state = _pick(tmp_path / "state", picked)
    assert is_picked(picked, state)
    assert is_picked(str(picked / "sub"), state)
    assert is_picked(picked / "sub" / "deep", state)


def test_a_folder_off_the_list_is_refused(tmp_path: Path, picked: Path) -> None:
    """Refuse a sibling, a parent, a name that only shares a prefix and a relative path."""
    (tmp_path / "other").mkdir()
    (tmp_path / "picked-2").mkdir()
    state = _pick(tmp_path / "state", picked)
    for path in (tmp_path / "other", tmp_path, tmp_path / "picked-2", Path("/"), "picked", "picked/sub"):
        assert not is_picked(path, state), path


def test_dot_dot_out_of_a_picked_folder_is_refused(tmp_path: Path, picked: Path) -> None:
    """Compare after ``resolve()``, so ``..`` cannot leave the entry."""
    (tmp_path / "other").mkdir()
    state = _pick(tmp_path / "state", picked)
    assert not is_picked(f"{picked}/sub/../../other", state)
    assert is_picked(f"{picked}/sub/../sub/deep", state)


def test_a_link_that_resolves_outside_a_picked_folder_is_refused(tmp_path: Path, picked: Path) -> None:
    """Refuse a link below the entry whose target is outside it, and accept one that stays inside."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (picked / "escape").symlink_to(outside)
    (picked / "inside").symlink_to(picked / "sub")
    state = _pick(tmp_path / "state", picked)
    assert not is_picked(picked / "escape", state)
    assert is_picked(picked / "inside", state)


def test_a_link_to_a_picked_folder_passes_because_it_resolves_inside(tmp_path: Path, picked: Path) -> None:
    """Resolve the entries too, so a link and its target compare equal."""
    alias = tmp_path / "alias"
    alias.symlink_to(picked)
    assert is_picked(alias / "sub", _pick(tmp_path / "state", picked))
    assert is_picked(picked / "sub", _pick(tmp_path / "state2", alias))


@pytest.mark.parametrize("content", [None, "", "not json", '{"folders": []}', '[1, null, "relative"]'])
def test_a_missing_damaged_or_empty_list_refuses_every_path(tmp_path: Path, picked: Path, content: str | None) -> None:
    """Refuse every path when the list is missing, is not a list, or holds no usable entry."""
    state = tmp_path / "state"
    state.mkdir()
    if content is not None:
        (state / PICKED_FOLDERS_FILE).write_text(content, encoding="utf-8")
    assert not is_picked(picked, str(state))


def test_no_state_folder_or_a_bad_path_refuses(tmp_path: Path, picked: Path) -> None:
    """Refuse every path without a state folder, and a path that cannot resolve."""
    assert not is_picked(picked, "")
    state = _pick(tmp_path / "state", picked)
    assert not is_picked(f"{picked}/a\x00b", state)
