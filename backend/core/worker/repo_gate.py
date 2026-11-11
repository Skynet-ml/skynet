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
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..billing.env_connectors import CONNECTORS, MANAGED_BINDING, host_allowed, managed_key_host
from ..exceptions import ServiceError
from ..i18n_en import t_en
from ..i18n_keys import I18nKey
from ..service_gateway.optimization.blackbox.repo_constraints import (
    RepoConstraints,
    read_constraints,
    read_constraints_file,
)
from ..service_gateway.optimization.blackbox.repo_tree import case_insensitive, fold_case, inside

Place = Literal["local", "cloud"]

# The settings each place's sandbox runtime enforces. Staging strips the
# metric files and the exploit folder from the agent's tree in both places,
# and ``check_hidden`` proves it (SKY-353). The issue that enforces another
# setting adds its kind here, and the gate stops refusing it. The broker
# grants the proposer's box its hosts and refuses any other (SKY-384). The run
# and metric boxes reach ``[services.<name>]`` and the servers of their own MCP
# file only through the stage relay, which checks the role and fills each
# secret by host (SKY-385). The scorer opens each ``[target.<name>]`` stage in
# its own offline box from its entry, timeout and vCPUs, and the judges read
# its rules (SKY-599).
_METRIC_HIDING = frozenset({"metric.files", "metric.exploits"})
_PROPOSER_NETWORK = frozenset({"proposer.network"})
_STAGE_SERVICES = frozenset({"services", "run.services", "metric.services", "run.mcp", "metric.mcp"})
_TARGET_STAGES = frozenset({"target.entry", "target.timeout", "target.vcpus", "target.illegal"})
_ENFORCED_EVERYWHERE = _METRIC_HIDING | _PROPOSER_NETWORK | _STAGE_SERVICES | _TARGET_STAGES
ENFORCED: dict[Place, frozenset[str]] = {"local": _ENFORCED_EVERYWHERE, "cloud": _ENFORCED_EVERYWHERE}

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


def check_repository(
    archive: Path,
    secret_names: Collection[str],
    *,
    place: Place,
    bindings: Mapping[str, str] | None = None,
    allowed_hosts: Mapping[str, Collection[str]] | None = None,
) -> list[GateFailure]:
    """List every reason the repository's ``optimize.toml`` stops this run.

    The role MCP files are read from the same snapshot as the toml (E49).

    Args:
        archive: The packed tree that ``stage_repository`` fetched.
        secret_names: Names of the secrets this run has; never values.
        place: Where the run's sandbox runs.
        bindings: Each ``${NAME}``'s binding, ``user`` or ``managed`` (E50);
            a name it does not hold binds to the user's secret.
        allowed_hosts: Each user secret's ``allowed_hosts`` (E52); a name it
            does not hold may go to no host.

    Returns:
        The failures, in file order. An empty list means the run may start.

    Raises:
        ConstraintsError: When the file or a role's MCP file is invalid.
    """
    raw = read_constraints_file(archive)
    constraints = read_constraints(archive) if raw is not None else None
    if raw is None or constraints is None:
        return []
    failures = [
        _failure(key, place, _UNENFORCED_CODE[place], key=key)
        for kind, key in _set_keys(raw, constraints.targets, constraints.services)
        if kind not in ENFORCED[place]
    ]
    return failures + _secret_failures(constraints, secret_names, bindings or {}, allowed_hosts or {}, place)


def _secret_failures(
    constraints: RepoConstraints,
    secret_names: Collection[str],
    bindings: Mapping[str, str],
    allowed_hosts: Mapping[str, Collection[str]],
    place: Place,
) -> list[GateFailure]:
    """List each ``${NAME}`` that cannot be filled at run start (E50, E52, E53).

    A user-bound name needs a secret in the run, and the host that receives
    it must be on that secret's ``allowed_hosts``. A managed name needs a host
    in the connector catalog for which Skynet holds a key. Every MCP server
    is http now (E64), so every name has a host.

    Args:
        constraints: The file with each role's MCP servers filled.
        secret_names: Names of the secrets this run has; never values.
        bindings: Each name's binding; a missing name means ``user``.
        allowed_hosts: Each user secret's allowed hosts; a missing name allows none.
        place: Where the run's sandbox runs.

    Returns:
        One failure per name that cannot be filled, in file order.
    """
    uses: list[tuple[str, str, str, str | None]] = [
        (name, f"[services.{service}]", service, None)
        for service, spec in constraints.services.items()
        for name in spec.secrets
    ]
    uses += [
        (name, f"{label} mcp", server.name, role.mcp or "-")
        for label, role in constraints.mcp_roles.items()
        for server in role.mcp_servers
        for name in server.secrets
    ]
    hosts = constraints.secret_hosts
    failures: list[GateFailure] = []
    seen: set[str] = set()
    for name, setting, owner, mcp_path in uses:
        if name in seen:
            continue
        seen.add(name)
        if bindings.get(name) != MANAGED_BINDING:
            if name in secret_names:
                failure = _host_failure(name, hosts.get(name, ""), allowed_hosts.get(name, ()), setting, place)
                if failure is not None:
                    failures.append(failure)
                continue
            if mcp_path is None:
                failure = _failure(
                    f"{setting} secrets", place, I18nKey.REPO_GATE_MISSING_SECRET, service=owner, secret=name
                )
            else:
                failure = _failure(
                    f"{setting} secrets",
                    place,
                    I18nKey.REPO_GATE_MISSING_MCP_SECRET,
                    server=owner,
                    path=mcp_path,
                    secret=name,
                )
            failures.append(failure)
            continue
        host = hosts.get(name, "")
        if not managed_key_host(host):
            known = ", ".join(key for key in CONNECTORS if managed_key_host(key))
            failures.append(
                _failure(
                    f"{setting} secrets",
                    place,
                    I18nKey.REPO_GATE_MANAGED_SECRET_UNAVAILABLE,
                    secret=name,
                    host=host,
                    known=known or "-",
                )
            )
    return failures


def _host_failure(name: str, host: str, allowed: Collection[str], setting: str, place: Place) -> GateFailure | None:
    """Refuse a user secret that goes to a host its ``allowed_hosts`` does not list (E52).

    Args:
        name: The secret's name.
        host: The one host that receives the secret.
        allowed: The secret's allowed hosts.
        setting: The ``optimize.toml`` setting that uses the secret.
        place: Where the run's sandbox runs.

    Returns:
        The failure, or ``None`` when the host is allowed.
    """
    if host_allowed(host, allowed):
        return None
    if not allowed:
        return _failure(f"{setting} secrets", place, I18nKey.REPO_GATE_SECRET_HOST_UNLISTED, secret=name, host=host)
    return _failure(
        f"{setting} secrets",
        place,
        I18nKey.REPO_GATE_SECRET_HOST_NOT_ALLOWED,
        secret=name,
        host=host,
        allowed=", ".join(allowed),
    )


def check_hidden(
    original_archive: Path,
    agent_archive: Path,
    hidden_paths: Collection[str],
    *,
    place: Place,
    optional_paths: Collection[str] = (),
    labels: Mapping[str, str] | None = None,
) -> list[GateFailure]:
    """List every hidden file the agent's tree still shows.

    A file fails when the agent's tree holds it under a hidden path, when the
    full tree has a symbolic link under a hidden path whose target outside
    the hidden paths the agent can still read, or when a hidden path lies
    below a symbolic link. A hidden path that matches no file also fails,
    unless it is optional. On a disk that ignores case, paths compare
    without case, as in the strip.

    Args:
        original_archive: The full tree that ``stage_repository`` fetched.
        agent_archive: The stripped tree the proposer's workspace receives.
        hidden_paths: Every scoring-side path, from ``hidden_repo_paths``.
        place: Where the run's sandbox runs.
        optional_paths: Hidden paths that may match no file, such as the
            exploit folder.
        labels: The ``optimize.toml`` setting that names each hidden path;
            a path it does not hold is labelled ``[metric] files``.

    Returns:
        One failure per visible path, labelled with the setting of the hidden
        path that covers it; an empty list means every hidden path is hidden.
    """
    fold = fold_case(case_insensitive(agent_archive.parent))
    hidden = [fold(posixpath.normpath(path)) for path in hidden_paths]
    label_of = {fold(posixpath.normpath(path)): label for path, label in (labels or {}).items()}
    with tarfile.open(agent_archive) as packed:
        visible = [fold(posixpath.normpath(name)) for name in packed.getnames()]
    with tarfile.open(original_archive) as packed:
        members = packed.getmembers()
    names = [fold(posixpath.normpath(member.name)) for member in members]
    links = [member for member in members if member.issym()]
    link_names = [fold(posixpath.normpath(member.name)) for member in links]
    targets = [
        (target, root)
        for member, name in zip(links, link_names, strict=True)
        for root in hidden
        if inside(name, [root])
        and not inside(
            target := fold(posixpath.normpath(posixpath.join(posixpath.dirname(name), member.linkname))),
            hidden,
        )
    ]
    optional = {fold(posixpath.normpath(path)) for path in optional_paths}
    # A misspelled metric path hides nothing, so the agent could read the
    # real metric; the run stops instead of guessing (SKY-353).
    leaked = [
        (path, path) for path in hidden if path not in optional and not any(inside(name, [path]) for name in names)
    ]
    # A hidden path below a linked folder lives at the link's target, which the
    # strip cannot see by name, so the run stops instead of guessing.
    leaked += [(path, path) for path in hidden if any(path.startswith(link + "/") for link in link_names)]
    leaked += [(path, root) for path in visible for root in hidden if inside(path, [root])]
    leaked += [(path, root) for path, root in targets if any(inside(name, [path]) for name in visible)]
    failures: dict[str, GateFailure] = {}
    for path, root in leaked:
        label = label_of.get(root, "[metric] files")
        failures.setdefault(path, _failure(label, place, _UNENFORCED_CODE[place], key=f"{label}: {path}"))
    return list(failures.values())


def _set_keys(raw: bytes, targets: Collection[str], services: Collection[str]) -> list[tuple[str, str]]:
    """List the environment settings the file writes, as ``(kind, label)`` pairs.

    Presence is read from the file, not the parsed values: a written empty list
    (``services = []``) means "none", which differs from the default an absent key
    gives, and a written default (``exploits = ".optimize/exploits"``) still
    asks for a folder to be hidden. ``[proposer] illegal`` is not listed: the
    judges enforce it in every place. Every ``[run]`` and ``[target.<name>]``
    key and the ``[metric]`` role keys are listed. ``ENFORCED`` decides which of them still stop the
    run: a key whose runtime does not exist yet stops here instead of failing
    later in the sandbox.

    Args:
        raw: The file's contents, already accepted by ``parse_constraints``.
        targets: Names of the file's ``[target.<name>]`` stages.
        services: Names of the file's ``[services.<name>]`` declarations.

    Returns:
        Each setting's kind, for example ``"proposer.mcp"``, and its label, for example ``"[proposer] mcp"``.
    """
    document: dict[str, Any] = tomllib.loads(raw.decode("utf-8"))
    proposer, metric, run = document.get("proposer", {}), document.get("metric", {}), document.get("run", {})
    keys = [(f"proposer.{key}", f"[proposer] {key}") for key in ("mcp", "network", "services") if key in proposer]
    stages = document.get("target", {})
    target_keys = ("entry", "timeout", "vcpus", "illegal", "mcp", "network", "services")
    # A stage box is offline except for the relay, so ``network = []`` asks
    # for what the box already gives; only a host list stops the run.
    keys += [
        (f"target.{key}", f"[target.{name}] {key}")
        for name in targets
        for key in target_keys
        if key in stages[name] and not (key == "network" and stages[name][key] == [])
    ]
    keys += [("services", f"[services.{name}]") for name in services]
    keys += [(f"run.{key}", f"[run] {key}") for key in ("entry", "timeout", "mcp", "services", "illegal") if key in run]
    metric_keys = ("files", "exploits", "reference", "mcp", "services", "illegal")
    keys += [(f"metric.{key}", f"[metric] {key}") for key in metric_keys if key in metric]
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
