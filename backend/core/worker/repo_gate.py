"""Refuse a repository run whose ``optimize.toml`` asks for what its sandbox cannot give.

A setting the run ignores would look like protection the user does not get,
for example an offline proposer that still has open egress. The gate runs in
the trusted parent after the tree is fetched and before any sandbox opens or
any model is called, so a refused run costs nothing (SKY-352). The worker and
the wizard's setup check call the same function, so the setup check shows
the same failures the run would stop on.
"""

from __future__ import annotations

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

# The settings each place's sandbox runtime enforces. Both are empty: no
# runtime wires these keys into the proposer or target boxes yet. The issue
# that enforces a setting adds its kind here, and the gate stops refusing it.
ENFORCED: dict[Place, frozenset[str]] = {"local": frozenset(), "cloud": frozenset()}

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
