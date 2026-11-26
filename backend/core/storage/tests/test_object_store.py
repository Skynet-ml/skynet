"""Tests for the choice of the step log pack store (SKY-977)."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.storage import object_store
from core.storage.object_store import LocalObjectStore, S3ObjectStore, pack_store


def test_a_local_build_with_no_bucket_keeps_packs_under_its_state_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The Mac app has no bucket, so its packs go to a folder under the local state.

    Args:
        monkeypatch: The test's patcher.
        tmp_path: The local state folder.
    """
    monkeypatch.setattr(object_store.settings, "tree_bucket", None)
    monkeypatch.setattr(object_store.settings, "local_mode", True)
    monkeypatch.setattr(object_store.settings, "local_state_dir", str(tmp_path))

    store = pack_store()

    assert isinstance(store, LocalObjectStore)
    store.put("packs/a", b"x")
    assert (tmp_path / "objects" / "packs" / "a").read_bytes() == b"x"


def test_a_bucket_wins_over_the_local_folder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A build with a bucket keeps its packs in S3, also in local mode.

    Args:
        monkeypatch: The test's patcher.
        tmp_path: The local state folder.
    """
    monkeypatch.setattr(object_store.settings, "tree_bucket", "packs-bucket")
    monkeypatch.setattr(object_store.settings, "local_mode", True)
    monkeypatch.setattr(object_store.settings, "local_state_dir", str(tmp_path))
    monkeypatch.setattr(object_store, "boto3", type("Boto", (), {"client": staticmethod(lambda name: object())}))

    assert isinstance(pack_store(), S3ObjectStore)


@pytest.mark.parametrize(("local_mode", "state_dir"), [(False, "state"), (True, "")], ids=["hosted", "no-state-dir"])
def test_a_build_with_no_bucket_and_no_local_folder_has_no_store(
    monkeypatch: pytest.MonkeyPatch, local_mode: bool, state_dir: str
) -> None:
    """A hosted build with no bucket has no store, so the engine loop refuses to start.

    Args:
        monkeypatch: The test's patcher.
        local_mode: Whether the build is the Mac app.
        state_dir: The local state folder setting.
    """
    monkeypatch.setattr(object_store.settings, "tree_bucket", None)
    monkeypatch.setattr(object_store.settings, "local_mode", local_mode)
    monkeypatch.setattr(object_store.settings, "local_state_dir", state_dir)

    assert pack_store() is None


def test_the_local_store_deletes_a_file_and_ignores_a_missing_one(tmp_path: Path) -> None:
    """Delete a local pack file; a second delete of the same key is not an error, as in S3.

    Args:
        tmp_path: The store root.
    """
    store = LocalObjectStore(tmp_path)
    store.put("packs/a", b"x")

    store.delete("packs/a")
    store.delete("packs/a")

    assert store.list("packs/") == []
    with pytest.raises(ValueError):
        store.delete("../outside")
