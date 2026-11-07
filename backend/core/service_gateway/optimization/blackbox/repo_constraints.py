"""Read a repository's ``optimize.toml`` and work out the environment its run gets.

The file travels with the repository, so anyone who can push to it wrote it.
It may only narrow what the customer approved: it can shrink the editable
paths, protect more files, propose a setup command when none was chosen, and
add instructions for the agent. It can never widen an editable path or hold a
secret value; asking for either fails the setup check with the reason.

Enforced: ``[paths] editable`` and ``[paths] protected`` (every version is
checked against them before it is scored or published), and ``[rules]
illegal`` with its alias ``[proposer] illegal``, plain-language rules that
judge models check during the run and on every version before it is scored.
Not enforced: ``[instructions]``, which the agent reads as guidance only.

Parsed and validated here, but not enforced yet: ``[proposer]`` and
``[target.<name>]`` (tools, network, services, and for a target its entry,
vcpus and timeout), ``[services.<name>]`` (the secret names a service needs)
and ``[metric]`` (the files and exploit folder hidden from the agent). A
repository run that sets any of them fails the setup check until the
sandbox runtime enforces them (SKY-352).
"""

from __future__ import annotations

import hashlib
import math
import re
import tarfile
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

CONSTRAINTS_FILE = "optimize.toml"
_MAX_FILE_BYTES = 64 * 1024
_MAX_PATHS = 200
_MAX_INTENT_CHARS = 4_000
_MAX_INVARIANTS = 50
_MAX_INVARIANT_CHARS = 500
_MAX_COMMAND_CHARS = 300
_MAX_RULES = 50
_MAX_RULE_CHARS = 500
_MAX_NAMES = 50
_MAX_NAME_CHARS = 100
_MAX_HOSTS = 100
_MAX_TABLES = 20
_MAX_COMMAND_PARTS = 50
_DEFAULT_VCPUS = 2
_DEFAULT_TIMEOUT_SECONDS = 60.0
_DEFAULT_EXPLOITS = ".optimize/exploits"
_ROLE_KEYS = frozenset({"tools", "network", "services", "illegal"})
_TARGET_KEYS = _ROLE_KEYS | {"entry", "vcpus", "timeout"}
_SECTIONS: dict[str, frozenset[str]] = {
    "paths": frozenset({"editable", "protected"}),
    "setup": frozenset({"command"}),
    "instructions": frozenset({"intent", "invariants"}),
    "rules": frozenset({"illegal"}),
    "proposer": _ROLE_KEYS,
    "metric": frozenset({"files", "exploits", "reference"}),
}
# Each of these holds one table per name, so its keys are checked per table.
_NAMED_SECTIONS = frozenset({"target", "services"})
_TABLE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_SECRET_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,127}")
_HOST = re.compile(r"(\*\.)?[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*(:[0-9]{1,5})?")


class ConstraintsError(ValueError):
    """The repository's constraints file is invalid or asks for more than the run allows."""


@dataclass(frozen=True)
class RoleEnvironment:
    """The environment one role's box gets: the proposer or one target stage.

    ``network`` is ``None`` when the file sets no list, which keeps the role's
    default (open egress for the proposer, offline plus the relay for a
    target). An empty tuple means offline.
    """

    tools: tuple[str, ...] = ()
    network: tuple[str, ...] | None = None
    services: tuple[str, ...] = ()
    illegal: tuple[str, ...] = ()


@dataclass(frozen=True, kw_only=True)
class TargetStage(RoleEnvironment):
    """One ``[target.<name>]`` stage: its environment and how the runner starts it.

    ``entry`` is ``"path/to/file.py:function"`` for a Python function, or a
    command such as ``("node", "planner.js")``. ``timeout`` is in seconds for
    each ``skynet.call``.
    """

    entry: str | tuple[str, ...]
    vcpus: int = _DEFAULT_VCPUS
    timeout: float = _DEFAULT_TIMEOUT_SECONDS


@dataclass(frozen=True)
class ServiceSpec:
    """One ``[services.<name>]`` declaration: the names of the secrets it needs, never values."""

    secrets: tuple[str, ...] = ()


@dataclass(frozen=True)
class MetricSpec:
    """The ``[metric]`` section: what the agent must never see."""

    files: tuple[str, ...] = ()
    exploits: str = _DEFAULT_EXPLOITS
    reference: str | None = None


@dataclass(frozen=True)
class RepoConstraints:
    """What a repository's constraints file proposes, already validated.

    ``rules`` and ``proposer.illegal`` hold the same merged list: ``[rules]
    illegal`` first, then ``[proposer] illegal``.
    """

    sha256: str
    editable_paths: tuple[str, ...] | None = None
    protected_paths: tuple[str, ...] = ()
    setup_command: str | None = None
    intent: str | None = None
    invariants: tuple[str, ...] = ()
    rules: tuple[str, ...] = ()
    proposer: RoleEnvironment = RoleEnvironment()
    targets: Mapping[str, TargetStage] = field(default_factory=dict)
    services: Mapping[str, ServiceSpec] = field(default_factory=dict)
    metric: MetricSpec = MetricSpec()


@dataclass(frozen=True)
class RepoEnvironment:
    """The effective rules a repository run is held to."""

    editable_paths: tuple[str, ...]
    protected_paths: tuple[str, ...]
    setup_command: str | None
    constraints: RepoConstraints | None = None
    notes: tuple[str, ...] = ()

    @property
    def rules(self) -> tuple[str, ...]:
        """Return the plain-language rules the judges enforce, in file order."""
        return self.constraints.rules if self.constraints is not None else ()

    def record(
        self, *, repository: str, commit: str, readonly_paths: Sequence[str], secret_names: Iterable[str]
    ) -> dict[str, Any]:
        """Describe the environment as the JSON saved with the run and shown before it starts.

        Args:
            repository: ``owner/name``.
            commit: The pinned starting commit.
            readonly_paths: Submodules and Git LFS files.
            secret_names: Names of the variables the scorer receives; never values.

        Returns:
            A JSON-ready summary of what the run can and cannot do.
        """
        file = self.constraints
        return {
            "repository": repository,
            "commit": commit,
            "constraints_file": {"path": CONSTRAINTS_FILE, "sha256": file.sha256} if file else None,
            "editable_paths": list(self.editable_paths),
            "protected_paths": list(self.protected_paths),
            "readonly_paths": list(readonly_paths),
            "secret_names": sorted(secret_names),
            "services": [],
            "instructions": {"intent": file.intent, "invariants": list(file.invariants)}
            if file and (file.intent or file.invariants)
            else None,
            "rules": list(self.rules),
            "notes": list(self.notes),
        }


def _clean_path(raw: Any, setting: str) -> str:
    """Normalize one repository-relative path from the file.

    Args:
        raw: The value as parsed.
        setting: The setting it came from, such as ``[paths] editable``, named in errors.

    Returns:
        The ``/``-joined path without leading or trailing slashes; ``.`` for the whole tree.

    Raises:
        ConstraintsError: When the value is not a path inside the repository, or is a pattern.
    """
    if not isinstance(raw, str):
        raise ConstraintsError(f"{CONSTRAINTS_FILE}: every entry of {setting} must be a text path.")
    path = raw.strip().replace("\\", "/")
    if path in (".", "./"):
        return "."
    stripped = path.strip("/")
    if path.startswith("/") or not stripped or any(part in ("", ".", "..") for part in stripped.split("/")):
        raise ConstraintsError(
            f"{CONSTRAINTS_FILE}: '{raw}' in {setting} must be relative and stay inside the repository."
        )
    if stripped == ".git" or stripped.startswith(".git/"):
        raise ConstraintsError(f"{CONSTRAINTS_FILE}: {setting} cannot name the .git folder.")
    # Paths are compared literally, so a pattern would match no file and
    # protect nothing while still being listed as protected.
    if any(char in stripped for char in "*?["):
        raise ConstraintsError(
            f"{CONSTRAINTS_FILE}: '{raw}' in {setting} looks like a pattern. Patterns are not supported;"
            " name the files or folders."
        )
    return stripped


def _paths(section: Mapping[str, Any], key: str, section_name: str = "paths") -> tuple[str, ...] | None:
    """Read one list of paths from a section.

    Args:
        section: The parsed table.
        key: The setting, such as ``editable`` or ``files``.
        section_name: The table's name, used in errors.

    Returns:
        The distinct cleaned paths, or ``None`` when the key is absent.

    Raises:
        ConstraintsError: When the value is not a list of paths.
    """
    if key not in section:
        return None
    setting = f"[{section_name}] {key}"
    values = section[key]
    if not isinstance(values, list) or len(values) > _MAX_PATHS:
        raise ConstraintsError(f"{CONSTRAINTS_FILE}: {setting} must be a list of at most {_MAX_PATHS} paths.")
    return tuple(dict.fromkeys(_clean_path(value, setting) for value in values))


def _text(value: Any, setting: str, limit: int) -> str:
    """Read one bounded text setting.

    Args:
        value: The value as parsed.
        setting: The setting's name, used in errors.
        limit: Most characters allowed.

    Returns:
        The stripped text.

    Raises:
        ConstraintsError: When the value is not text, is blank, or is too long.
    """
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ConstraintsError(f"{CONSTRAINTS_FILE}: {setting} must be text of 1 to {limit} characters.")
    return value.strip()


def _illegal(section: Mapping[str, Any], label: str) -> tuple[str, ...]:
    """Read an optional ``illegal`` list of plain-language rules.

    Args:
        section: The parsed table.
        label: The table as the file names it, such as ``[proposer]``, used in errors.

    Returns:
        The stripped rules in file order; empty when the key is absent.

    Raises:
        ConstraintsError: When the value is not a list of 1 to the maximum rules of bounded text.
    """
    if "illegal" not in section:
        return ()
    illegal = section["illegal"]
    if not isinstance(illegal, list) or not 1 <= len(illegal) <= _MAX_RULES:
        raise ConstraintsError(f"{CONSTRAINTS_FILE}: {label} illegal must list 1 to {_MAX_RULES} rules.")
    return tuple(_text(item, f"{label} illegal", _MAX_RULE_CHARS) for item in illegal)


def _names(section: Mapping[str, Any], key: str, label: str) -> tuple[str, ...]:
    """Read an optional list of short names, such as tools or services.

    Args:
        section: The parsed table.
        key: The setting.
        label: The table as the file names it, used in errors.

    Returns:
        The distinct stripped names in file order; empty when the key is absent.

    Raises:
        ConstraintsError: When the value is not a bounded list of bounded text.
    """
    values = section.get(key, [])
    if not isinstance(values, list) or len(values) > _MAX_NAMES:
        raise ConstraintsError(f"{CONSTRAINTS_FILE}: {label} {key} must be a list of at most {_MAX_NAMES} names.")
    return tuple(dict.fromkeys(_text(value, f"{label} {key}", _MAX_NAME_CHARS) for value in values))


def _network(section: Mapping[str, Any], label: str) -> tuple[str, ...] | None:
    """Read an optional list of allowed hosts.

    Args:
        section: The parsed table.
        label: The table as the file names it, used in errors.

    Returns:
        The distinct hosts, ``()`` for offline, or ``None`` when the key is absent.

    Raises:
        ConstraintsError: When the value is not a list of host names.
    """
    if "network" not in section:
        return None
    hosts = section["network"]
    if not isinstance(hosts, list) or len(hosts) > _MAX_HOSTS:
        raise ConstraintsError(
            f"{CONSTRAINTS_FILE}: {label} network must be a list of at most {_MAX_HOSTS} host names."
        )
    for host in hosts:
        if not isinstance(host, str) or len(host) > 253 or not _HOST.fullmatch(host):
            raise ConstraintsError(
                f"{CONSTRAINTS_FILE}: {label} network entry {str(host)[:100]!r} must be a host name such as"
                " api.example.com, not a URL. Use network = [] for no network."
            )
    return tuple(dict.fromkeys(host.lower() for host in hosts))


def _role(section: Mapping[str, Any], label: str, services: Mapping[str, ServiceSpec]) -> dict[str, Any]:
    """Read the settings every role shares: tools, network, services and illegal.

    Args:
        section: The parsed ``[proposer]`` or ``[target.<name>]`` table.
        label: The table as the file names it, used in errors.
        services: The services the file declares.

    Returns:
        Keyword arguments for :class:`RoleEnvironment`.

    Raises:
        ConstraintsError: When a setting is malformed or names an undeclared service.
    """
    named = _names(section, "services", label)
    for name in named:
        if name not in services:
            raise ConstraintsError(
                f"{CONSTRAINTS_FILE}: {label} services names '{name}', but the file has no [services.{name}] table."
            )
    return {
        "tools": _names(section, "tools", label),
        "network": _network(section, label),
        "services": named,
        "illegal": _illegal(section, label),
    }


def _entry(value: Any, label: str) -> str | tuple[str, ...]:
    """Read how the runner starts a target stage.

    Args:
        value: The ``entry`` value as parsed.
        label: The stage's table, used in errors.

    Returns:
        ``"path.py:function"`` with a cleaned path, or the command as a tuple.

    Raises:
        ConstraintsError: When the value is neither form.
    """
    problem = ConstraintsError(
        f'{CONSTRAINTS_FILE}: {label} entry must be "path/to/file.py:function" for a Python function,'
        ' or a command list such as ["node", "planner.js"].'
    )
    if isinstance(value, str):
        path, _, function = value.rpartition(":")
        if not path.endswith(".py") or not function.isidentifier():
            raise problem
        try:
            return f"{_clean_path(path, f'{label} entry')}:{function}"
        except ConstraintsError as error:
            raise problem from error
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= _MAX_COMMAND_PARTS
        or not all(isinstance(part, str) and part.strip() and len(part) <= _MAX_COMMAND_CHARS for part in value)
    ):
        raise problem
    return tuple(value)


def _target(section: Any, name: str, services: Mapping[str, ServiceSpec]) -> TargetStage:
    """Read one ``[target.<name>]`` stage.

    Args:
        section: The value under the stage's name.
        name: The stage's name.
        services: The services the file declares.

    Returns:
        The validated stage.

    Raises:
        ConstraintsError: When the stage is not a named table, or a setting is missing, unknown or malformed.
    """
    if not isinstance(section, dict):
        raise ConstraintsError(
            f"{CONSTRAINTS_FILE}: [target] {name} needs a stage name: use [target.<name>] for each stage,"
            ' for example [target.planner] with entry = "agents/planner.py:plan".'
        )
    if not _TABLE_NAME.fullmatch(name):
        raise ConstraintsError(
            f"{CONSTRAINTS_FILE}: the [target] stage name {name[:100]!r} must start with a letter or digit"
            " and use only letters, digits, '-' and '_' (at most 64)."
        )
    label = f"[target.{name}]"
    unknown = sorted(set(section) - _TARGET_KEYS)
    if unknown:
        raise ConstraintsError(f"{CONSTRAINTS_FILE}: {label} {unknown[0]} is not a supported setting.")
    if "entry" not in section:
        raise ConstraintsError(
            f'{CONSTRAINTS_FILE}: {label} entry is missing. Name the function ("path/to/file.py:function")'
            ' or the command (["node", "planner.js"]) that runs the stage.'
        )
    vcpus = section.get("vcpus", _DEFAULT_VCPUS)
    # bool is an int in Python, so ``vcpus = true`` would otherwise read as 1.
    if (
        not isinstance(vcpus, int)
        or isinstance(vcpus, bool)
        or not (vcpus == 1 or (vcpus % 2 == 0 and 2 <= vcpus <= 32))
    ):
        raise ConstraintsError(f"{CONSTRAINTS_FILE}: {label} vcpus must be 1 or an even number from 2 to 32.")
    timeout = section.get("timeout", _DEFAULT_TIMEOUT_SECONDS)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
        raise ConstraintsError(
            f"{CONSTRAINTS_FILE}: {label} timeout must be a positive number of seconds for each skynet.call."
        )
    return TargetStage(
        **_role(section, label, services),
        entry=_entry(section["entry"], label),
        vcpus=vcpus,
        timeout=float(timeout),
    )


def _service(section: Any, name: str) -> ServiceSpec:
    """Read one ``[services.<name>]`` declaration.

    The file is public with the repository, so it may name secrets but must
    never hold one. Error messages never repeat a refused value.

    Args:
        section: The value under the service's name.
        name: The service's name.

    Returns:
        The validated declaration.

    Raises:
        ConstraintsError: When the declaration is not a table, holds a value
            other than secret names, or has an unknown key.
    """
    if not isinstance(section, dict):
        raise ConstraintsError(
            f"{CONSTRAINTS_FILE}: [services] {name[:100]} must be a [services.<name>] table. A repository file"
            " cannot grant services with a value; declare the service and list only secret names in it."
        )
    if not _TABLE_NAME.fullmatch(name):
        raise ConstraintsError(
            f"{CONSTRAINTS_FILE}: the service name {name[:100]!r} must start with a letter or digit"
            " and use only letters, digits, '-' and '_' (at most 64)."
        )
    label = f"[services.{name}]"
    unknown = sorted(set(section) - {"secrets"})
    if unknown:
        raise ConstraintsError(
            f"{CONSTRAINTS_FILE}: {label} {unknown[0][:100]} is not a supported setting. The file may hold"
            " only secret names, in secrets = [...]; never put a secret value in it."
        )
    secrets = section.get("secrets", [])
    if not isinstance(secrets, list) or len(secrets) > _MAX_NAMES:
        raise ConstraintsError(
            f"{CONSTRAINTS_FILE}: {label} secrets must be a list of at most {_MAX_NAMES} names. The file may"
            " hold only secret names, never values."
        )
    for index, secret in enumerate(secrets, start=1):
        if not isinstance(secret, str) or not _SECRET_NAME.fullmatch(secret):
            raise ConstraintsError(
                f"{CONSTRAINTS_FILE}: {label} secrets entry {index} is not a secret name. The file may hold"
                " only secret names (A-Z, 0-9 and _, such as MAPS_API_KEY), never values."
            )
    return ServiceSpec(secrets=tuple(dict.fromkeys(secrets)))


def _named_tables(document: Mapping[str, Any], section_name: str) -> Mapping[str, Any]:
    """Return the tables of a section that holds one table per name.

    Args:
        document: The parsed file.
        section_name: ``target`` or ``services``.

    Returns:
        The tables by name; empty when the section is absent.

    Raises:
        ConstraintsError: When the section holds too many tables.
    """
    tables = document.get(section_name, {})
    if len(tables) > _MAX_TABLES:
        raise ConstraintsError(f"{CONSTRAINTS_FILE}: [{section_name}] may hold at most {_MAX_TABLES} tables.")
    return tables


def _metric_path(section: Mapping[str, Any], key: str) -> str | None:
    """Read one optional path from ``[metric]``.

    Args:
        section: The parsed ``[metric]`` table.
        key: ``exploits`` or ``reference``.

    Returns:
        The cleaned path, or ``None`` when the key is absent.

    Raises:
        ConstraintsError: When the value is not a path inside the repository.
    """
    if key not in section:
        return None
    if not isinstance(section[key], str):
        raise ConstraintsError(f"{CONSTRAINTS_FILE}: [metric] {key} must be a text path.")
    return _clean_path(section[key], f"[metric] {key}")


def _metric(section: Mapping[str, Any]) -> MetricSpec:
    """Read the ``[metric]`` section.

    Args:
        section: The parsed ``[metric]`` table; empty when the file has none.

    Returns:
        The validated section, with the default exploit folder when none is set.

    Raises:
        ConstraintsError: When a path is malformed or names the whole repository.
    """
    files = _paths(section, "files", "metric") or ()
    exploits = _metric_path(section, "exploits") or _DEFAULT_EXPLOITS
    reference = _metric_path(section, "reference")
    # The runtime hides these paths from the agent, so "." would hide the whole repository.
    if "." in (*files, exploits, reference):
        raise ConstraintsError(f"{CONSTRAINTS_FILE}: [metric] cannot name the whole repository.")
    return MetricSpec(files=files, exploits=exploits, reference=reference)


def parse_constraints(raw: bytes) -> RepoConstraints:
    """Validate the bytes of a constraints file.

    Unknown sections and keys fail rather than being ignored, so a rule the
    runtime cannot enforce is never silently dropped.

    Args:
        raw: The file's contents.

    Returns:
        The validated proposal.

    Raises:
        ConstraintsError: When the file is too large, not TOML, or holds an
            unknown, malformed, or unsupported setting.
    """
    if len(raw) > _MAX_FILE_BYTES:
        raise ConstraintsError(f"{CONSTRAINTS_FILE} is larger than {_MAX_FILE_BYTES // 1024} KiB.")
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ConstraintsError(f"{CONSTRAINTS_FILE} is not valid TOML: {error}") from error
    version = document.pop("version", 1)
    if version != 1:
        raise ConstraintsError(f"{CONSTRAINTS_FILE}: version {version!r} is not supported; use version = 1.")
    for name, section in document.items():
        if name not in _SECTIONS and name not in _NAMED_SECTIONS:
            raise ConstraintsError(f"{CONSTRAINTS_FILE}: [{name}] is not a supported section.")
        if not isinstance(section, dict):
            raise ConstraintsError(f"{CONSTRAINTS_FILE}: {name} must be a [{name}] table.")
        if name in _NAMED_SECTIONS:
            continue
        unknown = sorted(set(section) - _SECTIONS[name])
        if unknown:
            raise ConstraintsError(f"{CONSTRAINTS_FILE}: [{name}] {unknown[0]} is not a supported setting.")
    paths = document.get("paths", {})
    setup = document.get("setup", {})
    instructions = document.get("instructions", {})
    protected = _paths(paths, "protected") or ()
    if "." in protected:
        raise ConstraintsError(f"{CONSTRAINTS_FILE}: [paths] protected cannot protect the whole repository.")
    editable = _paths(paths, "editable")
    if editable == ():
        raise ConstraintsError(
            f"{CONSTRAINTS_FILE}: [paths] editable is empty, so no version could change anything."
            " List at least one path, or remove the setting to keep the approved ones."
        )
    invariants = instructions.get("invariants", [])
    if not isinstance(invariants, list) or len(invariants) > _MAX_INVARIANTS:
        raise ConstraintsError(
            f"{CONSTRAINTS_FILE}: [instructions] invariants must be a list of at most {_MAX_INVARIANTS} items."
        )
    services = {name: _service(table, name) for name, table in _named_tables(document, "services").items()}
    targets = {name: _target(table, name, services) for name, table in _named_tables(document, "target").items()}
    proposer = _role(document.get("proposer", {}), "[proposer]", services)
    # [rules] illegal is the older name for [proposer] illegal; a file with both keeps both lists.
    rules = (*_illegal(document.get("rules", {}), "[rules]"), *proposer["illegal"])
    proposer["illegal"] = rules
    return RepoConstraints(
        sha256=hashlib.sha256(raw).hexdigest(),
        editable_paths=editable,
        protected_paths=protected,
        setup_command=_text(setup["command"], "[setup] command", _MAX_COMMAND_CHARS) if "command" in setup else None,
        intent=_text(instructions["intent"], "[instructions] intent", _MAX_INTENT_CHARS)
        if "intent" in instructions
        else None,
        invariants=tuple(_text(item, "[instructions] invariants", _MAX_INVARIANT_CHARS) for item in invariants),
        rules=rules,
        proposer=RoleEnvironment(**proposer),
        targets=targets,
        services=services,
        metric=_metric(document.get("metric", {})),
    )


def read_constraints(archive: Path) -> RepoConstraints | None:
    """Read the constraints file at the root of a packed snapshot.

    Args:
        archive: Packed tree from :func:`fetch_snapshot`.

    Returns:
        The validated proposal, or ``None`` when the repository has no such file.

    Raises:
        ConstraintsError: When the file is not a regular file or is invalid.
    """
    with tarfile.open(archive, "r:gz") as packed:
        member = next((m for m in packed.getmembers() if str(PurePosixPath(m.name)) == CONSTRAINTS_FILE), None)
        if member is None:
            return None
        if not member.isfile():
            raise ConstraintsError(f"{CONSTRAINTS_FILE} must be a regular file, not a link or folder.")
        if member.size > _MAX_FILE_BYTES:
            raise ConstraintsError(f"{CONSTRAINTS_FILE} is larger than {_MAX_FILE_BYTES // 1024} KiB.")
        handle = packed.extractfile(member)
        raw = handle.read() if handle is not None else b""
    constraints = parse_constraints(raw)
    unenforced = _unenforced_keys(raw, constraints)
    if unenforced:
        raise ConstraintsError(
            f"{CONSTRAINTS_FILE} sets {', '.join(unenforced)}. Repository runs cannot enforce "
            "these settings yet (SKY-352). Remove them to run."
        )
    return constraints


def _unenforced_keys(raw: bytes, constraints: RepoConstraints) -> list[str]:
    """List the environment settings a repository run cannot enforce yet.

    A setting the run ignores would look like protection the user does not get,
    for example an offline proposer that still has open egress, so the run path
    refuses it instead. SKY-352 removes this check when the sandbox runtime
    enforces these keys. ``[proposer] illegal`` is not listed: the judges enforce it.

    Presence is read from the file, not the parsed values: a written empty list
    (``tools = []``) means "none", which differs from the default an absent key
    gives, and a written default (``exploits = ".optimize/exploits"``) still
    asks for a folder to be hidden.

    Args:
        raw: The file's contents, already accepted by :func:`parse_constraints`.
        constraints: The parsed file.

    Returns:
        The settings the file sets, as ``[section] key`` labels.
    """
    document = tomllib.loads(raw.decode("utf-8"))
    proposer, metric = document.get("proposer", {}), document.get("metric", {})
    keys = [f"[proposer] {key}" for key in ("tools", "network", "services") if key in proposer]
    keys += [f"[target.{name}]" for name in constraints.targets]
    keys += [f"[services.{name}]" for name in constraints.services]
    keys += [f"[metric] {key}" for key in ("files", "exploits", "reference") if key in metric]
    return keys


def _inside(path: str, roots: Iterable[str]) -> bool:
    """Report whether ``path`` is one of ``roots`` or lies below one.

    Args:
        path: Repository-relative path; ``.`` is the whole tree.
        roots: Repository-relative files or folders; ``.`` is the whole tree.

    Returns:
        Whether a root covers the path.
    """
    return any(root == "." or path == root or path.startswith(root.rstrip("/") + "/") for root in roots)


def effective_environment(
    constraints: RepoConstraints | None,
    *,
    approved_editable: Sequence[str],
    approved_setup: str | None,
) -> RepoEnvironment:
    """Combine the customer's approved settings with what the repository proposes.

    The constraints file itself is always protected, so no version can change
    the rules it is scored under.

    Args:
        constraints: The repository's proposal, or ``None`` when it has none.
        approved_editable: Editable paths from the submitted run.
        approved_setup: Setup command from the submitted run, if any.

    Returns:
        The rules the run is held to.

    Raises:
        ConstraintsError: When the file proposes an editable path outside the approved ones,
            or when the protected paths (always including the file itself) cover every editable path.
    """
    if constraints is None:
        editable = tuple(approved_editable)
        if all(_inside(path, (CONSTRAINTS_FILE,)) for path in editable):
            raise ConstraintsError(
                f"{CONSTRAINTS_FILE} is always protected, so the approved editable paths ({', '.join(editable)})"
                " leave nothing a version can change. Approve a path other than the constraints file."
            )
        return RepoEnvironment(
            editable_paths=editable, protected_paths=(CONSTRAINTS_FILE,), setup_command=approved_setup
        )
    editable = tuple(approved_editable)
    if constraints.editable_paths is not None:
        wider = [path for path in constraints.editable_paths if not _inside(path, approved_editable)]
        if wider:
            approved = ", ".join(approved_editable)
            raise ConstraintsError(
                f"{CONSTRAINTS_FILE} makes '{wider[0]}' editable, which is outside the approved editable"
                f" paths ({approved}). A repository file can only narrow them; widen them in the run's settings."
            )
        editable = constraints.editable_paths
    protected = tuple(dict.fromkeys((CONSTRAINTS_FILE, *constraints.protected_paths)))
    # Protected paths win over editable ones when a patch is checked, so this
    # run could only spend its budget on versions that are all refused.
    if all(_inside(path, protected) for path in editable):
        raise ConstraintsError(
            f"{CONSTRAINTS_FILE} protects every editable path ({', '.join(editable)}), so no version could"
            " change anything. Protect less, or make a path outside [paths] protected editable."
        )
    notes: list[str] = []
    setup = approved_setup
    if (approved_setup or "").strip():
        if constraints.setup_command and constraints.setup_command != approved_setup.strip():
            notes.append(f"The run's own setup command replaces the one in {CONSTRAINTS_FILE}.")
    else:
        setup = constraints.setup_command
    return RepoEnvironment(
        editable_paths=editable,
        protected_paths=protected,
        setup_command=setup,
        constraints=constraints,
        notes=tuple(notes),
    )


def instructions_brief(environment: RepoEnvironment) -> str:
    """Write the repository's rules as text the optimization agent reads.

    Args:
        environment: The effective rules.

    Returns:
        Markdown naming the protected paths, every numbered plain-language
        rule, and the file's instructions, marked as guidance where the
        runtime does not enforce them.
    """
    lines = [
        f"## Repository rules ({CONSTRAINTS_FILE})",
        "",
        "Protected, every change to them is rejected before scoring: "
        + ", ".join(f"`{path}`" for path in environment.protected_paths)
        + ".",
    ]
    if environment.rules:
        lines += [
            "",
            "Illegal approaches. A judge model checks your work against these rules during the run, and"
            " checks every version before it is scored. A version that breaks a rule is not scored. It is"
            " rejected, and the run history keeps the judge's reason.",
            "",
        ]
        lines += [f"- {label}: {rule}" for label, rule in numbered_rules(environment.rules)]
    file = environment.constraints
    if file is not None and (file.intent or file.invariants):
        lines += ["", "Guidance from the repository (not enforced; the scorer decides):"]
        if file.intent:
            lines += ["", file.intent]
        lines += [f"- {item}" for item in file.invariants]
    return "\n".join(lines)


def numbered_rules(rules: Sequence[str]) -> list[tuple[str, str]]:
    """Name each plain-language rule the way the agent, judges and events refer to it.

    Args:
        rules: The rules in file order.

    Returns:
        ``("Rule 1", text)`` pairs, numbered from 1.
    """
    return [(f"Rule {index}", rule) for index, rule in enumerate(rules, start=1)]
