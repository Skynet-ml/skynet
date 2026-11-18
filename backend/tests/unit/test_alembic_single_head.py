"""Check that the Alembic revision tree has exactly one head (SKY-819)."""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

_BACKEND = Path(__file__).resolve().parents[2]


def test_the_migration_tree_has_one_head() -> None:
    """Fail when two revisions share a parent, because ``alembic upgrade head`` then stops."""
    config = Config(str(_BACKEND / "alembic.ini"))
    config.set_main_option("script_location", str(_BACKEND / "alembic"))

    heads = ScriptDirectory.from_config(config).get_heads()

    assert len(heads) == 1, f"Alembic has {len(heads)} heads: {sorted(heads)}. Add a merge revision."
