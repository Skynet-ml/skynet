"""Refuse a repository run whose ``optimize.toml`` asks for what its sandbox cannot give.

A setting the run ignores would look like protection the user does not get,
for example an offline proposer that still has open egress. The gate runs in
the trusted parent after the tree is fetched and before any sandbox opens or
any model is called, so a refused run costs nothing (SKY-352). The worker and
the wizard's setup check call the same function, so the setup check shows
the same failures the run would stop on.
"""

from __future__ import annotations

import posixpath
import tarfile
import tomllib
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..exceptions import ServiceError
from ..i18n_en import t_en
from ..i18n_keys import I18nKey
from ..service_gateway.optimization.blackbox.repo_constraints import parse_constraints, read_constraints_file

Place = Literal["local", "cloud"]

# The settings each place's sandbox runtime enforces. Staging strips the
# metric files and the exploit folder from the agent's tree in both places,
# and ``check_hidden`` proves it (SKY-353). The issue that enforces another
# setting adds its kind here, and the gate stops refusing it.
_METRIC_HIDING = frozenset({"metric.files", "metric.exploits"})
ENFORCED: dict[Place, frozenset[str]] = {"local": _METRIC_HIDING, "cloud": _METRIC_HIDING}

_UNENFORCED_CODE: dict[Place, I18nKey] = {
    "local": I18nKey.REPO_GATE_UNENFORCED_LOCAL,
    "cloud": I18nKey.REPO_GATE_UNENFORCED_CLOUD,
}


@dataclass(frozen=True)
class GateFailure:
    """One reason the run cannot start: the ``optimize.toml`` key, the place, and why."""

    key: str
    place: Place
    reason: str
    code: I18nKey
    params: dict[str, str] = field(default_factory=dict)


class RepoGateError(ServiceError):
    """The gate refused the run; ``failures`` lists every reason."""

    def __init__(self, failures: list[GateFailure]) -> None:
        """Carry every failure, and the first one's catalog code for a client that shows one message.

        Args:
            failures: The non-empty list from :func:`check_repository`.
        """
        first = failures[0]
        super().__init__(" ".join(item.reason for item in failures), code=first.code, params=first.params)
        self.failures = failures


def sandbox_place(local_mode: bool) -> Place:
    """Name the place a run's sandbox runs in.

    Args:
        local_mode: ``settings.local_mode``; local runs use Docker on this computer.

    Returns:
        ``"local"`` for local Docker, otherwise ``"cloud"``.
    """
    return "local" if local_mode else "cloud"


def check_repository(archive: Path, secret_names: Collection[str], *, place: Place) -> list[GateFailure]:
    """List every reason the repository's ``optimize.toml`` stops this run.

    Args:
        archive: The packed tree that ``stage_repository`` fetched.
        secret_names: Names of the secrets this run has; never values.
        place: Where the run's sandbox runs.

    Returns:
        The failures, in file order. An empty list means the run may start.
    """
    raw = read_constraints_file(archive)
    if raw is None:
        return []
    constraints = parse_constraints(raw)
    failures = [
        _failure(key, place, _UNENFORCED_CODE[place], key=key)
        for kind, key in _set_keys(raw, constraints.targets, constraints.services)
        if kind not in ENFORCED[place]
    ]
    for service, spec in constraints.services.items():
        failures += [
            _failure(
                f"[services.{service}] secrets", place, I18nKey.REPO_GATE_MISSING_SECRET, service=service, secret=name
            )
            for name in spec.secrets
            if name not in secret_names
        ]
    return failures


def check_hidden(
    original_archive: Path,
    agent_archive: Path,
    hidden_paths: Collection[str],
    *,
    place: Place,
    optional_paths: Collection[str] = (),
) -> list[GateFailure]:
    """List every metric file the agent's tree still shows.

    A file fails when the agent's tree holds it under a hidden path, when the
    full tree has a symbolic link under a hidden path whose target outside
    the hidden paths the agent can still read, or when a hidden path lies
    below a symbolic link. A hidden path that matches no file also fails,
    unless it is optional.

    Args:
        original_archive: The full tree that ``stage_repository`` fetched.
        agent_archive: The stripped tree the proposer's workspace receives.
        hidden_paths: The ``[metric] files`` entries and the exploit folder.
        place: Where the run's sandbox runs.
        optional_paths: Hidden paths that may match no file, such as the
            exploit folder.

    Returns:
        One failure per visible path; an empty list means the metric is hidden.
    """
    hidden = [posixpath.normpath(path) for path in hidden_paths]
    with tarfile.open(agent_archive) as packed:
        visible = [posixpath.normpath(name) for name in packed.getnames()]
    with tarfile.open(original_archive) as packed:
        members = packed.getmembers()
    names = [posixpath.normpath(member.name) for member in members]
    links = [member for member in members if member.issym()]
    targets = [
        target
        for member in links
        if _covered(posixpath.normpath(member.name), hidden)
        and not _covered(
            target := posixpath.normpath(
                posixpath.join(posixpath.dirname(posixpath.normpath(member.name)), member.linkname)
            ),
            hidden,
        )
    ]
    optional = {posixpath.normpath(path) for path in optional_paths}
    # A misspelled metric path hides nothing, so the agent could read the
    # real metric; the run stops instead of guessing (SKY-353).
    leaked = [path for path in hidden if path not in optional and not any(_covered(name, [path]) for name in names)]
    # A hidden path below a linked folder lives at the link's target, which the
    # strip cannot see by name, so the run stops instead of guessing.
    leaked += [path for path in hidden if any(path.startswith(posixpath.normpath(link.name) + "/") for link in links)]
    leaked += [path for path in visible if _covered(path, hidden)]
    leaked += [path for path in targets if any(_covered(name, [path]) for name in visible)]
    return [
        _failure("[metric] files", place, _UNENFORCED_CODE[place], key=f"[metric] files: {path}")
        for path in dict.fromkeys(leaked)
    ]


def _covered(path: str, roots: Collection[str]) -> bool:
    """Report whether ``path`` is one of ``roots`` or lies below one.

    Args:
        path: Normalized repository-relative path.
        roots: Normalized repository-relative files or folders.

    Returns:
        Whether a root covers the path.
    """
    return any(root == "." or path == root or path.startswith(root + "/") for root in roots)


def _set_keys(raw: bytes, targets: Collection[str], services: Collection[str]) -> list[tuple[str, str]]:
    """List the environment settings the file writes, as ``(kind, label)`` pairs.

    Presence is read from the file, not the parsed values: a written empty list
    (``tools = []``) means "none", which differs from the default an absent key
    gives, and a written default (``exploits = ".optimize/exploits"``) still
    asks for a folder to be hidden. ``[proposer] illegal`` is not listed: the
    judges enforce it in every place.

    Args:
        raw: The file's contents, already accepted by ``parse_constraints``.
        targets: Names of the file's ``[target.<name>]`` stages.
        services: Names of the file's ``[services.<name>]`` declarations.

    Returns:
        Each setting's kind, for example ``"proposer.tools"``, and its label, for example ``"[proposer] tools"``.
    """
    document: dict[str, Any] = tomllib.loads(raw.decode("utf-8"))
    proposer, metric = document.get("proposer", {}), document.get("metric", {})
    keys = [(f"proposer.{key}", f"[proposer] {key}") for key in ("tools", "network", "services") if key in proposer]
    keys += [("target", f"[target.{name}]") for name in targets]
    keys += [("services", f"[services.{name}]") for name in services]
    keys += [(f"metric.{key}", f"[metric] {key}") for key in ("files", "exploits", "reference") if key in metric]
    return keys


def _failure(setting: str, place: Place, code: I18nKey, **params: str) -> GateFailure:
    """Build one failure with its English reason and catalog code.

    Args:
        setting: The ``optimize.toml`` setting that fails.
        place: Where the run's sandbox runs.
        code: Catalog key of the message.
        **params: The message's placeholder values.

    Returns:
        The failure.
    """
    return GateFailure(key=setting, place=place, reason=t_en(code, **params), code=code, params=params)
