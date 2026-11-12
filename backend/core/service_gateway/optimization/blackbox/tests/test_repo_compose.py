"""Tests for the ``[sandbox] compose`` setting of a repository's optimize.toml."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from ..repo_constraints import CONSTRAINTS_FILE, ConstraintsError, ServiceSpec, parse_constraints, read_constraints

_COMPOSE = b"""
services:
  db:
    image: postgres:16
    command: postgres -D /data
  api:
    image: python:3.12
    command: python -m api
    depends_on: [db]
"""


def _toml(extra: str = "") -> bytes:
    """Return a small optimize.toml that names a compose file.

    Args:
        extra: More TOML text, added at the end.

    Returns:
        The file's bytes.
    """
    return f'version = 1\n\n[sandbox]\ncompose = "deploy/compose.yaml"\n{extra}'.encode()


def _archive(tmp_path: Path, files: dict[str, bytes]) -> Path:
    """Pack files the way the parent ships a fetched tree.

    Args:
        tmp_path: Scratch folder.
        files: Root-relative path to bytes.

    Returns:
        The archive.
    """
    archive = tmp_path / "tree.tgz"
    with tarfile.open(archive, "w:gz") as packed:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            packed.addfile(info, io.BytesIO(data))
    return archive


def test_parse_keeps_the_compose_path() -> None:
    """Keep the repository path that ``[sandbox] compose`` names."""
    constraints = parse_constraints(_toml())

    assert constraints.compose_file == "deploy/compose.yaml"
    assert constraints.compose_services == ()


def test_no_sandbox_section_means_no_compose() -> None:
    """Leave the compose fields empty when the file has no ``[sandbox]`` section."""
    constraints = parse_constraints(b"version = 1\n")

    assert constraints.compose_file is None
    assert constraints.compose_services == ()


def test_unknown_sandbox_key_fails() -> None:
    """Refuse a ``[sandbox]`` key that the runtime cannot obey."""
    with pytest.raises(ConstraintsError, match=r"\[sandbox\] image is not a supported setting"):
        parse_constraints(_toml('image = "ubuntu"\n'))


@pytest.mark.parametrize("path", ["/etc/compose.yaml", "../compose.yaml", ".", ""])
def test_compose_path_must_be_one_repository_file(path: str) -> None:
    """Refuse a compose path outside the repository or the whole repository.

    Args:
        path: The bad path.
    """
    with pytest.raises(ConstraintsError, match=r"\[sandbox\] compose"):
        parse_constraints(f'version = 1\n[sandbox]\ncompose = "{path}"\n'.encode())


def test_compose_path_must_be_text() -> None:
    """Refuse a compose setting that is not a text path."""
    with pytest.raises(ConstraintsError, match=r"\[sandbox\] compose"):
        parse_constraints(b"version = 1\n[sandbox]\ncompose = 3\n")


def test_read_fills_the_services_in_start_order(tmp_path: Path) -> None:
    """Read the compose file from the snapshot into services, dependencies first.

    Args:
        tmp_path: Scratch folder.
    """
    archive = _archive(tmp_path, {CONSTRAINTS_FILE: _toml(), "deploy/compose.yaml": _COMPOSE})

    constraints = read_constraints(archive)

    assert constraints is not None
    assert [service.name for service in constraints.compose_services] == ["db", "api"]
    assert constraints.compose_services[1].depends_on == ("db",)


def test_missing_compose_file_fails(tmp_path: Path) -> None:
    """Refuse a compose path that the snapshot does not hold.

    Args:
        tmp_path: Scratch folder.
    """
    archive = _archive(tmp_path, {CONSTRAINTS_FILE: _toml()})

    with pytest.raises(ConstraintsError, match=r"\[sandbox\] compose names 'deploy/compose.yaml'"):
        read_constraints(archive)


@pytest.mark.parametrize(
    "text",
    [b"services: [", b"version: '3'\n", b"services:\n  db:\n    image: postgres\n    depends_on: [cache]\n"],
)
def test_invalid_compose_file_fails_as_constraints_error(tmp_path: Path, text: bytes) -> None:
    """Report a compose file the box cannot run as a constraints problem.

    Args:
        tmp_path: Scratch folder.
        text: A compose file that is not valid YAML, has no services, or names an unknown dependency.
    """
    archive = _archive(tmp_path, {CONSTRAINTS_FILE: _toml(), "deploy/compose.yaml": text})

    with pytest.raises(ConstraintsError, match=r"\[sandbox\] compose"):
        read_constraints(archive)


def test_services_tables_keep_their_api_relay_meaning(tmp_path: Path) -> None:
    """Keep ``[services.<name>]`` as an API relay, apart from the compose services.

    Args:
        tmp_path: Scratch folder.
    """
    raw = _toml('\n[services.weather]\nurl = "https://api.weather.example"\n')
    archive = _archive(tmp_path, {CONSTRAINTS_FILE: raw, "deploy/compose.yaml": _COMPOSE})

    constraints = read_constraints(archive)

    assert constraints is not None
    assert isinstance(constraints.services["weather"], ServiceSpec)
    assert "weather" not in {service.name for service in constraints.compose_services}
