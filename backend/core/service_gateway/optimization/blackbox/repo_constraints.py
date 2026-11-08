"""Read a repository's ``optimize.toml`` and work out the environment its run gets.

The file travels with the repository, so anyone who can push to it wrote it.
It may only narrow what the customer approved: it can shrink the editable
paths, protect more files, propose a setup command when none was chosen, and
add instructions for the agent. It can never widen an editable path or hold a
secret value; asking for either fails the setup check with the reason.

Enforced: ``[paths] editable`` and ``[paths] protected`` (every version is
checked against them before it is scored or published), and ``[proposer]
illegal``, the path of a markdown file whose top-level bullets are
plain-language rules that judge models check during the run and on every
version before it is scored. Not enforced: the top-level ``instructions``
path, a text file the agent reads as guidance only. Rule text lives in those
files, not in the toml (E48), and every file they name is protected like
``optimize.toml`` itself, so no version can rewrite its own rules.

Parsed and validated here, but not enforced yet: ``[proposer]`` and
``[target.<name>]`` (mcp, network, services, and for a target its entry,
vcpus and timeout), ``[services.<name>]`` (a user's API: an https ``url``
and ``headers`` that name secrets only as ``${NAME}`` placeholders),
``[metric]`` (the files and exploit folder hidden from the agent) and
``[run]`` (the entry that starts the candidate for each case, and its
timeout; with no ``[run]``, nothing runs and the metric judges only the tree
and the patch), and ``[data] cases`` (the path of a JSONL file that holds
the run's cases, one ``{"input": ..., "expected": ...}`` object for each
line; it is hidden from the agent and the candidate like the metric
files). ``[run]`` and ``[metric]`` also take the role keys mcp,
services and illegal; they share one box, so network and vcpus fail. Every
role's mcp is the path of a standard ``.mcp.json`` file in the repository,
read from the same snapshot and checked here; a role's ``tools`` key fails
and points to mcp (E46). No box gets the proposer's or a target's MCP
servers yet: SKY-385 owns that wiring. A role gets only the services it lists and only the
servers in its own mcp file (E49, E55). Every secret goes to one host, and
the file names it only as ``${NAME}``; a raw secret value fails (E49, E52).
The worker's preflight gate (``core.worker.repo_gate``) fails a run that
sets a key its sandbox cannot enforce, before any sandbox time (SKY-352).
That includes every ``[run]`` key and the ``[metric]`` mcp, services and
illegal keys, until SKY-385 gives that box its environment.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
import socket
import tarfile
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

CONSTRAINTS_FILE = "optimize.toml"
_MAX_FILE_BYTES = 64 * 1024
_MAX_PATHS = 200
_MAX_INTENT_CHARS = 4_000
_MAX_RULE_FILE_BYTES = 32 * 1024
_MAX_CASES_FILE_BYTES = 20 * 1024 * 1024
# The same limit as the cases list of a run request.
_MAX_CASES = 200_000
_CASE_KEYS = frozenset({"input", "expected"})
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
_ROLE_KEYS = frozenset({"mcp", "network", "services", "illegal"})
_TARGET_KEYS = _ROLE_KEYS | {"entry", "vcpus", "timeout"}
_SECTIONS: dict[str, frozenset[str]] = {
    "paths": frozenset({"editable", "protected"}),
    "setup": frozenset({"command"}),
    "proposer": _ROLE_KEYS,
    "metric": frozenset({"files", "exploits", "reference", "mcp", "services", "illegal"}),
    "run": frozenset({"entry", "timeout", "mcp", "services", "illegal"}),
    "data": frozenset({"cases"}),
}
# Every role names its MCP servers in a .mcp.json file, not catalog tool names (E46).
_ROLE_SECTIONS = frozenset({"proposer", "run", "metric"})
_RUN_ROLE_SECTIONS = ("run", "metric")
# The candidate and the metric share one box today (E37), so neither can size
# or wall off its own box; these keys fail with that reason, not as unknown.
_SHARED_BOX_KEYS = frozenset({"network", "vcpus"})
_BULLET = re.compile(r"[-*+](?:[ \t]+(.*))?")
# Only an ATX heading counts; "#1 Do not ..." or "#tag" is text and must not be dropped as a heading.
_HEADING = re.compile(r"#{1,6}(?:[ \t]|$)")
# Each of these holds one table per name, so its keys are checked per table.
_NAMED_SECTIONS = frozenset({"target", "services"})
_TABLE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_SECRET_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,127}")
_HEADER_NAME = re.compile(r"[A-Za-z0-9-]{1,64}")
_PLACEHOLDER = re.compile(r"\$\{([^}]*)\}")
_MAX_URL_CHARS = 2_048
_MAX_HEADER_CHARS = 1_000
# Wildcard DNS names such as 169.254.169.254.nip.io resolve to the address they hold.
_DOTTED_IPV4 = re.compile(r"(^|\.)[0-9]{1,3}(\.[0-9]{1,3}){3}(\.|$)")
_HOST = re.compile(r"(\*\.)?[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*(:[0-9]{1,5})?")
_MCP_HTTP_TYPES = frozenset({"http", "streamable-http", "sse"})
_MCP_HTTP_KEYS = frozenset({"type", "url", "headers"})
# Only http servers are supported now (E64): a stdio server would run code and hold secrets in the box.
_MCP_STDIO_KEYS = frozenset({"command", "args", "env"})
# Known key formats: OpenAI/Stripe, GitHub, Slack, Google, AWS, GitLab, Hugging Face, and JWTs.
_SECRET_PREFIX = re.compile(r"(sk|pk|rk)[-_]|gh[pousr]_|github_pat_|xox[abposr]-|AIza|AKIA|ASIA|glpat-|hf_|eyJ")
_SECRET_TOKEN = re.compile(r"[A-Za-z0-9_\-+/.~]{24,}")
# Some providers issue UUID keys, which the dashed-slug rule below would let through.
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


class ConstraintsError(ValueError):
    """The repository's constraints file is invalid or asks for more than the run allows."""


@dataclass(frozen=True)
class McpServer:
    """One http server in a role's ``.mcp.json`` file (E49, E64).

    Header values hold secrets only as ``${NAME}`` placeholders, which the
    trusted parent resolves; the file never holds a value.
    """

    name: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def host(self) -> str:
        """Return the lowercase host the server's calls go to."""
        return _host_of(self.url)

    @property
    def secrets(self) -> tuple[str, ...]:
        """Return the secret names the server's headers use, in file order."""
        return _placeholders(*self.headers.values())

    def record(self) -> dict[str, Any]:
        """Describe the server for the run's disclosure, without secret values.

        Returns:
            The name, the type, the host and the secret names.
        """
        return {"name": self.name, "type": "http", "host": self.host, "secrets": list(self.secrets)}


@dataclass(frozen=True)
class RoleEnvironment:
    """The environment one role's box gets: the proposer or one target stage.

    ``network`` is ``None`` when the file sets no list, which keeps the role's
    default (open egress for the proposer, offline plus the relay for a
    target). An empty tuple means offline.
    """

    network: tuple[str, ...] | None = None
    services: tuple[str, ...] = ()
    illegal: tuple[str, ...] = ()
    # The repository path of the role's .mcp.json file (E46).
    mcp: str | None = None
    # The repository path the file names in illegal; read_constraints fills illegal from it (E48).
    illegal_file: str | None = None
    # The servers of the mcp file; read_constraints fills them from the same snapshot (E49).
    mcp_servers: tuple[McpServer, ...] = ()


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

    # The ${NAME} placeholders of headers, in file order (E53).
    secrets: tuple[str, ...] = ()
    # The parser always sets it (E53); only specs built in code leave it None.
    url: str | None = None
    # As written in the file, with ${SECRET} placeholders; None when the file sets none.
    headers: Mapping[str, str] | None = None

    @property
    def host(self) -> str | None:
        """Return the lowercase host the service's calls go to, or ``None`` when it has no url."""
        return _host_of(self.url) if self.url is not None else None

    @property
    def request_headers(self) -> dict[str, str]:
        """Return the headers to send, with ``${SECRET}`` placeholders still unresolved.

        Returns:
            The file's headers, or ``Authorization: Bearer ${SECRET}`` when it
            sets none and the service has exactly one secret (E45).
        """
        if self.headers is not None:
            return dict(self.headers)
        if len(self.secrets) == 1:
            return {"Authorization": f"Bearer ${{{self.secrets[0]}}}"}
        return {}


@dataclass(frozen=True)
class MetricSpec:
    """The ``[metric]`` section: what the agent must never see."""

    files: tuple[str, ...] = ()
    exploits: str = _DEFAULT_EXPLOITS
    reference: str | None = None


@dataclass(frozen=True)
class RepoConstraints:
    """What a repository's constraints file proposes, already validated.

    ``rules`` and ``proposer.illegal`` hold the same list: the top-level
    bullets of the ``[proposer] illegal`` file. :func:`parse_constraints`
    reads only the toml, so it leaves ``intent`` and every role's ``illegal``
    empty; :func:`read_constraints` fills them from the files in the snapshot.
    """

    sha256: str
    editable_paths: tuple[str, ...] | None = None
    protected_paths: tuple[str, ...] = ()
    setup_command: str | None = None
    # None means the file has no [run] section, so no candidate runs (E39).
    run_entry: str | tuple[str, ...] | None = None
    run_timeout: float = _DEFAULT_TIMEOUT_SECONDS
    # network is always None for these two roles: they share one box (E37).
    # Each role gets only the services it lists and only its own MCP file (E55).
    run_role: RoleEnvironment = RoleEnvironment()
    metric_role: RoleEnvironment = RoleEnvironment()
    instructions_file: str | None = None
    intent: str | None = None
    rules: tuple[str, ...] = ()
    proposer: RoleEnvironment = RoleEnvironment()
    targets: Mapping[str, TargetStage] = field(default_factory=dict)
    services: Mapping[str, ServiceSpec] = field(default_factory=dict)
    metric: MetricSpec = MetricSpec()
    # [data] cases: the only source of a repository run's cases (E58). parse_constraints
    # keeps the path; read_constraints fills cases from the file in the snapshot.
    cases_file: str | None = None
    cases: tuple[dict[str, Any], ...] = ()

    @property
    def rule_files(self) -> tuple[str, ...]:
        """Return the distinct repository paths of the instructions and rule files the toml names."""
        return tuple(dict.fromkeys(path for _, path in _rule_file_settings(self)))

    @property
    def mcp_files(self) -> tuple[str, ...]:
        """Return the distinct repository paths of the ``.mcp.json`` files that the roles name."""
        return tuple(dict.fromkeys(role.mcp for role in self.mcp_roles.values() if role.mcp is not None))

    @property
    def mcp_roles(self) -> dict[str, RoleEnvironment]:
        """Return every role, which may name an MCP file, by the label the file uses for it."""
        targets = {f"[target.{name}]": stage for name, stage in self.targets.items()}
        return {"[proposer]": self.proposer, "[run]": self.run_role, "[metric]": self.metric_role, **targets}

    @property
    def secret_hosts(self) -> dict[str, str]:
        """Return the one host each secret may go to, by secret name (E52).

        Returns:
            Each secret that a ``[services]`` header or an MCP server's
            header uses, mapped to the host of that url.
        """
        hosts = {name: spec.host for spec in self.services.values() for name in spec.secrets if spec.host}
        for role in self.mcp_roles.values():
            hosts |= {name: server.host for server in role.mcp_servers for name in server.secrets}
        return hosts

    @property
    def secret_names(self) -> tuple[str, ...]:
        """Return every secret name the file and its MCP files use, in file order."""
        names = [name for spec in self.services.values() for name in spec.secrets]
        names += [name for role in self.mcp_roles.values() for server in role.mcp_servers for name in server.secrets]
        return tuple(dict.fromkeys(names))


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
        services = file.services if file else {}
        roles = file.mcp_roles if file else {}
        return {
            "repository": repository,
            "commit": commit,
            "constraints_file": {"path": CONSTRAINTS_FILE, "sha256": file.sha256} if file else None,
            "editable_paths": list(self.editable_paths),
            "protected_paths": list(self.protected_paths),
            "readonly_paths": list(readonly_paths),
            "secret_names": sorted(secret_names),
            "services": [
                {"name": name, "host": spec.host, "secrets": list(spec.secrets)} for name, spec in services.items()
            ],
            "mcp_servers": {
                label: [server.record() for server in role.mcp_servers]
                for label, role in roles.items()
                if role.mcp_servers
            },
            "secret_hosts": file.secret_hosts if file else {},
            "instructions": {"path": file.instructions_file, "intent": file.intent} if file and file.intent else None,
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


def _one_file(raw: Any, setting: str) -> str:
    """Read the repository path of one file, such as an ``.mcp.json`` or a rule file.

    Args:
        raw: The value as parsed.
        setting: The setting it came from, such as ``[run] mcp``, named in errors.

    Returns:
        The cleaned repository-relative path.

    Raises:
        ConstraintsError: When the value is not a path to one file inside the repository.
    """
    path = _clean_path(raw, setting)
    if path == ".":
        raise ConstraintsError(f"{CONSTRAINTS_FILE}: {setting} must name one file, not the whole repository.")
    return path


def _illegal_file(section: Mapping[str, Any], label: str) -> str | None:
    """Read the optional ``illegal`` path: the markdown file that holds a role's rules (E48).

    Args:
        section: The parsed table.
        label: The table as the file names it, such as ``[proposer]``, used in errors.

    Returns:
        The repository path of the rule file, or ``None`` when the key is absent.

    Raises:
        ConstraintsError: When the value is an inline list of rules, or not a path to one file in the repository.
    """
    if "illegal" not in section:
        return None
    if not isinstance(section["illegal"], str):
        raise ConstraintsError(
            f"{CONSTRAINTS_FILE}: {label} illegal must be the path of a markdown rule file, not the rules"
            " themselves. Move the rules into a file in the repository, one top-level '- ' bullet for each rule,"
            f' and set illegal = "<path>" in {label}.'
        )
    return _one_file(section["illegal"], f"{label} illegal")


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
    """Read the settings every role shares: mcp, network, services and illegal.

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
        "mcp": _mcp(section, label),
        "network": _network(section, label),
        "services": named,
        "illegal_file": _illegal_file(section, label),
    }


def _tools_moved(label: str) -> ConstraintsError:
    """Build the error for a role's ``tools`` key, which names catalog tools instead of an MCP file (E46).

    Args:
        label: The role's table as the file names it.

    Returns:
        The error, with the fix that points to ``mcp``.
    """
    return ConstraintsError(
        f"{CONSTRAINTS_FILE}: {label} tools is not supported. List the MCP servers in a .mcp.json"
        f' file in the repository and set mcp = "<path>" in {label} instead.'
    )


def _mcp(section: Mapping[str, Any], label: str) -> str | None:
    """Read the optional ``mcp`` path of a role.

    Args:
        section: The parsed table.
        label: The table as the file names it, used in errors.

    Returns:
        The repository-relative path of the ``.mcp.json`` file, or ``None`` when the key is absent.

    Raises:
        ConstraintsError: When the value is not a path to one file inside the repository.
    """
    if "mcp" not in section:
        return None
    return _one_file(section["mcp"], f"{label} mcp")


def _entry(value: Any, label: str) -> str | tuple[str, ...]:
    """Read how the runner starts a target stage or the ``[run]`` candidate.

    Args:
        value: The ``entry`` value as parsed.
        label: The entry's table, used in errors.

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
    if "tools" in section:
        raise _tools_moved(label)
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
    return TargetStage(
        **_role(section, label, services),
        entry=_entry(section["entry"], label),
        vcpus=vcpus,
        timeout=_timeout(section, label, "skynet.call"),
    )


def _timeout(section: Mapping[str, Any], label: str, unit: str) -> float:
    """Read an optional ``timeout`` in seconds.

    Args:
        section: The parsed table.
        label: The table as the file names it, used in errors.
        unit: What one timeout covers, such as ``skynet.call``, named in errors.

    Returns:
        The timeout, or the 60-second default when the key is absent.

    Raises:
        ConstraintsError: When the value is not a positive finite number.
    """
    timeout = section.get("timeout", _DEFAULT_TIMEOUT_SECONDS)
    # bool is an int in Python, so ``timeout = true`` would otherwise read as 1.
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
        raise ConstraintsError(
            f"{CONSTRAINTS_FILE}: {label} timeout must be a positive number of seconds for each {unit}."
        )
    return float(timeout)


def _run(section: Mapping[str, Any] | None) -> tuple[str | tuple[str, ...] | None, float]:
    """Read the ``[run]`` section: how Skynet starts the candidate for each case (E39).

    Args:
        section: The parsed ``[run]`` table, or ``None`` when the file has none.

    Returns:
        The entry, or ``None`` when the file has no ``[run]``, and the timeout in seconds for each case.

    Raises:
        ConstraintsError: When ``[run]`` has no entry, or the entry or timeout is malformed.
    """
    if section is None:
        return None, _DEFAULT_TIMEOUT_SECONDS
    if "entry" not in section:
        raise ConstraintsError(
            f'{CONSTRAINTS_FILE}: [run] entry is missing. Name the function ("path/to/file.py:function")'
            ' or the command (["pytest", "-q"]) that runs the candidate for each case.'
        )
    return _entry(section["entry"], "[run]"), _timeout(section, "[run]", "case")


def _service(section: Any, name: str) -> ServiceSpec:
    """Read one ``[services.<name>]`` declaration.

    The file is public with the repository, so it may name secrets but must
    never hold one. A header names each secret as ``${NAME}`` (E53). Error
    messages never repeat a refused value.

    Args:
        section: The value under the service's name.
        name: The service's name.

    Returns:
        The validated declaration.

    Raises:
        ConstraintsError: When the declaration is not a table, has no url, has
            an unknown key or the removed ``secrets`` key, or a header holds a
            raw secret value.
    """
    if not isinstance(section, dict):
        raise ConstraintsError(
            f"{CONSTRAINTS_FILE}: [services] {name[:100]} must be a [services.<name>] table with a url and"
            " headers. A repository file cannot grant services with a value."
        )
    if not _TABLE_NAME.fullmatch(name):
        raise ConstraintsError(
            f"{CONSTRAINTS_FILE}: the service name {name[:100]!r} must start with a letter or digit"
            " and use only letters, digits, '-' and '_' (at most 64)."
        )
    label = f"[services.{name}]"
    # The older form listed names in secrets = [...] (E53); name the move instead of "not supported".
    if "secrets" in section:
        raise ConstraintsError(
            f"{CONSTRAINTS_FILE}: {label} secrets is no longer supported. Remove it and write each secret as"
            ' ${NAME} in headers, such as Authorization = "Bearer ${API_KEY}".'
        )
    unknown = sorted(set(section) - {"url", "headers"})
    if unknown:
        raise ConstraintsError(
            f"{CONSTRAINTS_FILE}: {label} {unknown[0][:100]} is not a supported setting. A service has only a"
            " url and headers; write each secret as ${NAME} in headers, never as a value."
        )
    if "url" not in section:
        raise ConstraintsError(f'{CONSTRAINTS_FILE}: {label} needs a url, such as url = "https://api.example.com".')
    headers = _service_headers(section["headers"], label) if "headers" in section else None
    return ServiceSpec(
        secrets=_placeholders(*(headers or {}).values()),
        url=_service_url(section["url"], label),
        headers=headers,
    )


def _service_url(value: Any, label: str, prefix: str = CONSTRAINTS_FILE) -> str:
    """Read a service's or an http MCP server's ``url``: an https address on a public host name (E45, E49).

    This is a parse-time check of the written text only. It refuses the
    obvious internal names early, but a public name can still resolve to a
    private address. The relay that calls the url (SKY-374) must resolve the
    host and refuse private, loopback and link-local addresses at connect
    time; this check does not stop SSRF on its own.

    Args:
        value: The ``url`` value as parsed.
        label: The service's table or the MCP server, used in errors.
        prefix: The file the error names first.

    Returns:
        The url as written.

    Raises:
        ConstraintsError: When the value is not https, holds credentials, a
            placeholder or what looks like a raw secret, or names an IP address, ``localhost``, an
            ``.internal`` or ``.local`` name, a name that holds a dotted IPv4
            address, or a host without a domain.
    """
    if not isinstance(value, str) or len(value) > _MAX_URL_CHARS or not value.startswith("https://"):
        raise ConstraintsError(f"{prefix}: {label} url must be an https:// address.")
    # A secret in the url would reach logs and the box; only headers carry secrets.
    if "${" in value:
        raise ConstraintsError(f"{prefix}: {label} url must not hold a ${{NAME}} secret; put it in headers.")
    try:
        parts = urlsplit(value)
        parts.port  # noqa: B018 - reading it raises ValueError for a bad port
    except ValueError as error:
        raise ConstraintsError(f"{prefix}: {label} url is not a valid address.") from error
    if _looks_secret(" ".join(re.split(r"[/?&#=]+", f"{parts.path}?{parts.query}"))):
        raise ConstraintsError(
            f"{prefix}: {label} url holds what looks like a secret value. Put the secret in headers as ${{NAME}}."
        )
    if parts.username is not None or parts.password is not None:
        raise ConstraintsError(
            f"{prefix}: {label} url must not hold credentials; write the secret as ${{NAME}} in headers."
        )
    host = (parts.hostname or "").rstrip(".")
    try:
        ipaddress.ip_address(host)
        is_ip = True
    except ValueError:
        is_ip = False
    if not is_ip:
        # inet_aton reads the IPv4 forms that ip_address refuses, such as 0x7f.0x1 or
        # 169.254.169.0xfe, the way an HTTP client resolves them.
        try:
            socket.inet_aton(host)
            is_ip = True
        except (OSError, ValueError):  # ValueError: a NUL in the host, which TOML's \u0000 allows
            pass
    # A last label with no letter also catches IP forms that ip_address refuses, such as 0x7f.1.
    last = host.rsplit(".", 1)[-1]
    if (
        is_ip
        or host == "localhost"
        or host.endswith((".localhost", ".internal", ".local"))
        or _DOTTED_IPV4.search(host)
        or "." not in host
        or not _HOST.fullmatch(host)
        or not any(char.isalpha() for char in last)
    ):
        raise ConstraintsError(
            f"{prefix}: {label} url must name a public host such as api.example.com, not an IP address or localhost."
        )
    return value


def _service_headers(value: Any, label: str, prefix: str = CONSTRAINTS_FILE) -> dict[str, str]:
    """Read a ``headers`` table, whose values name secrets only as ``${NAME}`` placeholders (E45, E53).

    Args:
        value: The ``headers`` value as parsed.
        label: The service's table or the MCP server, used in errors.
        prefix: The file the error names first.

    Returns:
        The headers as written.

    Raises:
        ConstraintsError: When the value is not a table of header names to
            one line of text, a placeholder is malformed, or a value holds
            what looks like a raw secret.
    """
    if not isinstance(value, dict) or len(value) > _MAX_NAMES:
        raise ConstraintsError(
            f"{prefix}: {label} headers must be a table of at most {_MAX_NAMES} header names to text."
        )
    for header, text in value.items():
        if not _HEADER_NAME.fullmatch(header):
            raise ConstraintsError(
                f"{prefix}: {label} header name {header[:100]!r} must use only letters, digits and '-'."
            )
        if not isinstance(text, str) or len(text) > _MAX_HEADER_CHARS or "\r" in text or "\n" in text:
            raise ConstraintsError(
                f"{prefix}: {label} headers {header} must be one line of at most {_MAX_HEADER_CHARS} characters."
            )
        _check_value(text, f"{label} headers {header}", prefix)
    return dict(value)


def _check_value(text: str, where: str, prefix: str) -> None:
    """Check that a header value names secrets only as ``${NAME}`` placeholders (E49).

    Args:
        text: The value as written.
        where: The setting that holds the value, used in errors.
        prefix: The file the error names first.

    Raises:
        ConstraintsError: When a placeholder is not a secret name, a ``${`` is
            not closed, or the text outside the placeholders looks like a raw
            secret. The message never repeats the value.
    """
    for name in _PLACEHOLDER.findall(text):
        if not _SECRET_NAME.fullmatch(name):
            raise ConstraintsError(
                f"{prefix}: {where} uses a placeholder that is not a secret name. Write ${{NAME}} with"
                " A-Z, 0-9 and _ only, such as ${MAPS_API_KEY}."
            )
    rest = _PLACEHOLDER.sub(" ", text)
    if "${" in rest:
        raise ConstraintsError(f"{prefix}: {where} has a ${{ that is not closed. Write each secret as ${{NAME}}.")
    if _looks_secret(rest):
        raise ConstraintsError(
            f"{prefix}: {where} holds what looks like a secret value. The file is public with the repository."
            ' Save the value as a Skynet secret and write ${NAME} here instead, such as "Bearer ${API_KEY}".'
        )


def _looks_secret(text: str) -> bool:
    """Report whether text outside the placeholders looks like a raw key or token.

    A known key prefix counts, and so does a long run of letters and digits
    that is not a path or a dashed slug. It is a guard against a pasted key,
    not proof that a value is safe.

    Args:
        text: The value with its placeholders removed.

    Returns:
        Whether a part of the text looks like a secret.
    """
    for token in re.split(r"[\s,;=\"']+", text):
        if (len(token) >= 12 and _SECRET_PREFIX.match(token)) or _UUID.fullmatch(token):
            return True
        if (
            _SECRET_TOKEN.fullmatch(token)
            and not token.startswith(("/", "./", "../", "~/"))
            and token.count("-") < 3
            and sum(char.isdigit() for char in token) >= 4
            and sum(char.isalpha() for char in token) >= 4
        ):
            return True
    return False


def _placeholders(*texts: str) -> tuple[str, ...]:
    """Return the secret names the texts use as ``${NAME}``, in order and without repeats.

    Args:
        *texts: Header values.

    Returns:
        The names.
    """
    return tuple(dict.fromkeys(name for text in texts for name in _PLACEHOLDER.findall(text)))


def _host_of(url: str) -> str:
    """Return the lowercase host of an accepted url, without a trailing dot.

    Args:
        url: A url :func:`_service_url` accepted.

    Returns:
        The host name.
    """
    return (urlsplit(url).hostname or "").rstrip(".").lower()


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


def _cases_file(section: Mapping[str, Any] | None) -> str | None:
    """Read the ``[data]`` section: the path of the run's JSONL cases file (E58).

    Args:
        section: The parsed ``[data]`` table, or ``None`` when the file has none.

    Returns:
        The repository path of the cases file, or ``None`` when there is no ``[data]``.

    Raises:
        ConstraintsError: When ``[data]`` has no ``cases`` path, or the value is not a path to one file.
    """
    if section is None:
        return None
    if "cases" not in section:
        raise ConstraintsError(
            f'{CONSTRAINTS_FILE}: [data] must set cases = "<path>" to a JSONL file of cases, or be removed.'
        )
    if not isinstance(section["cases"], str):
        raise ConstraintsError(f"{CONSTRAINTS_FILE}: [data] cases must be the path of one JSONL file.")
    return _one_file(section["cases"], "[data] cases")


def _refuse_visible_cases(cases_file: str | None, rule_files: list[tuple[str, str | None]]) -> None:
    """Refuse a cases file that is also the instructions or a rule file (E61).

    The brief quotes those files to the agent, so a shared path would show it
    every ``expected`` answer.

    Args:
        cases_file: The ``[data] cases`` path, or ``None``.
        rule_files: Each text file setting and its path, or ``None`` when unset.

    Raises:
        ConstraintsError: When the cases path equals one of those paths.
    """
    if cases_file is None:
        return
    for setting, path in rule_files:
        if path is not None and cases_file == path:
            raise ConstraintsError(
                f"{CONSTRAINTS_FILE}: [data] cases and {setting} name the same file '{path}'. The agent reads"
                " the instructions and rule files, so the cases must be in a file of their own."
            )


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
    instructions = document.pop("instructions", None)
    # The older forms held the text in the toml (E48); name the move instead of "not supported".
    if isinstance(instructions, dict):
        raise ConstraintsError(
            f"{CONSTRAINTS_FILE}: [instructions] is no longer supported. Move the intent and invariants text into"
            ' a file in the repository and set instructions = "<path>" at the top of the file, before any section.'
        )
    if "rules" in document:
        raise ConstraintsError(
            f"{CONSTRAINTS_FILE}: [rules] is no longer supported. Move the rules into a markdown file in the"
            " repository, one top-level '- ' bullet for each rule, and set illegal = \"<path>\" in [proposer]."
        )
    for name, section in document.items():
        if name not in _SECTIONS and name not in _NAMED_SECTIONS:
            raise ConstraintsError(f"{CONSTRAINTS_FILE}: [{name}] is not a supported section.")
        if not isinstance(section, dict):
            raise ConstraintsError(f"{CONSTRAINTS_FILE}: {name} must be a [{name}] table.")
        if name in _NAMED_SECTIONS:
            continue
        shared = sorted(set(section) & _SHARED_BOX_KEYS) if name in _RUN_ROLE_SECTIONS else []
        if name in _ROLE_SECTIONS and "tools" in section:
            raise _tools_moved(f"[{name}]")
        if shared:
            raise ConstraintsError(
                f"{CONSTRAINTS_FILE}: [{name}] {shared[0]} is not supported: the candidate and the metric"
                " share one box, so neither can set its own network or vcpus. Remove the setting."
            )
        unknown = sorted(set(section) - _SECTIONS[name])
        # The split comes from the run seed (E59), so a split key would be ignored, not obeyed.
        if name == "data" and "split" in unknown:
            raise ConstraintsError(
                f"{CONSTRAINTS_FILE}: [data] split is not supported. The run shuffles the cases with its seed"
                " and splits them 70/15/15. Remove the setting."
            )
        if unknown:
            raise ConstraintsError(f"{CONSTRAINTS_FILE}: [{name}] {unknown[0]} is not a supported setting.")
    paths = document.get("paths", {})
    setup = document.get("setup", {})
    protected = _paths(paths, "protected") or ()
    if "." in protected:
        raise ConstraintsError(f"{CONSTRAINTS_FILE}: [paths] protected cannot protect the whole repository.")
    editable = _paths(paths, "editable")
    if editable == ():
        raise ConstraintsError(
            f"{CONSTRAINTS_FILE}: [paths] editable is empty, so no version could change anything."
            " List at least one path, or remove the setting to keep the approved ones."
        )
    services = {name: _service(table, name) for name, table in _named_tables(document, "services").items()}
    targets = {name: _target(table, name, services) for name, table in _named_tables(document, "target").items()}
    proposer = _role(document.get("proposer", {}), "[proposer]", services)
    run_entry, run_timeout = _run(document.get("run"))
    run_section = document.get("run", {})
    run_role = _role(run_section, "[run]", services)
    metric_section = document.get("metric", {})
    metric_role = _role(metric_section, "[metric]", services)
    _check_secret_receivers(services, {})
    instructions_file = None if instructions is None else _one_file(instructions, "instructions")
    cases_file = _cases_file(document.get("data"))
    rule_files = [("instructions", instructions_file)]
    rule_files += [
        (label, role["illegal_file"])
        for label, role in (
            ("[proposer] illegal", proposer),
            ("[run] illegal", run_role),
            ("[metric] illegal", metric_role),
        )
    ]
    rule_files += [(f"[target.{name}] illegal", stage.illegal_file) for name, stage in targets.items()]
    _refuse_visible_cases(cases_file, rule_files)
    return RepoConstraints(
        sha256=hashlib.sha256(raw).hexdigest(),
        editable_paths=editable,
        protected_paths=protected,
        setup_command=_text(setup["command"], "[setup] command", _MAX_COMMAND_CHARS) if "command" in setup else None,
        run_entry=run_entry,
        run_timeout=run_timeout,
        run_role=RoleEnvironment(**run_role),
        metric_role=RoleEnvironment(**metric_role),
        instructions_file=instructions_file,
        proposer=RoleEnvironment(**proposer),
        targets=targets,
        services=services,
        metric=_metric(metric_section),
        cases_file=cases_file,
    )


def read_constraints(archive: Path) -> RepoConstraints | None:
    """Read the constraints file at the root of a packed snapshot.

    Args:
        archive: Packed tree from :func:`fetch_snapshot`.

    Returns:
        The validated proposal, or ``None`` when the repository has no such file.

    Raises:
        ConstraintsError: When the file is not a regular file or is invalid, when
            a named MCP file is missing or not a valid ``mcpServers`` file, when
            one secret goes to two hosts, when a named instructions or rule
            file is missing, not a regular text file, too large, empty, or not
            a list of top-level bullets, or when the ``[data] cases`` file is
            missing, too large, empty, or has a line that is not one case.
    """
    raw = read_constraints_file(archive)
    if raw is None:
        return None
    constraints = _load_mcp_files(parse_constraints(raw), archive)
    _check_secret_receivers(constraints.services, constraints.mcp_roles)
    return _load_cases(_load_rule_files(constraints, archive), archive)


def _load_cases(constraints: RepoConstraints, archive: Path) -> RepoConstraints:
    """Fill the run's cases from the ``[data] cases`` JSONL file (E58).

    Args:
        constraints: The parsed proposal.
        archive: Packed tree from :func:`fetch_snapshot`, the same snapshot the toml came from.

    Returns:
        The proposal with ``cases`` filled, unchanged when there is no ``[data]``.

    Raises:
        ConstraintsError: When the file is missing, is a link or folder, is too
            large, is not UTF-8, holds no case or too many, or has a line that
            is not a JSON object with ``input`` and an optional ``expected``.
    """
    path = constraints.cases_file
    if path is None:
        return constraints
    setting = "[data] cases"
    with tarfile.open(archive, "r:gz") as packed:
        member = next((m for m in packed.getmembers() if str(PurePosixPath(m.name)) == path), None)
        if member is None:
            raise ConstraintsError(
                f"{CONSTRAINTS_FILE}: {setting} names '{path}', but the repository has no such file."
            )
        if not member.isfile():
            raise ConstraintsError(
                f"{CONSTRAINTS_FILE}: {setting} names '{path}', which must be a regular file, not a link or folder."
            )
        if member.size > _MAX_CASES_FILE_BYTES:
            raise ConstraintsError(
                f"{CONSTRAINTS_FILE}: {setting} names '{path}', which is larger than"
                f" {_MAX_CASES_FILE_BYTES // (1024 * 1024)} MiB."
            )
        handle = packed.extractfile(member)
        data = handle.read() if handle is not None else b""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ConstraintsError(f"{CONSTRAINTS_FILE}: {setting} names '{path}', which is not UTF-8 text.") from exc
    cases: list[dict[str, Any]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            case = json.loads(line)
        except ValueError:
            case = None
        if not isinstance(case, dict) or "input" not in case or set(case) - _CASE_KEYS:
            raise ConstraintsError(
                f"{CONSTRAINTS_FILE}: line {number} of '{path}' ({setting}) is not a case. Write each case on"
                ' one line as a JSON object {"input": ..., "expected": ...}; "expected" is optional.'
            )
        cases.append(case)
        if len(cases) > _MAX_CASES:
            raise ConstraintsError(f"{CONSTRAINTS_FILE}: '{path}' ({setting}) holds more than {_MAX_CASES} cases.")
    if not cases:
        raise ConstraintsError(f"{CONSTRAINTS_FILE}: {setting} names '{path}', which holds no cases.")
    return replace(constraints, cases=tuple(cases))


def _rule_file_settings(constraints: RepoConstraints) -> list[tuple[str, str]]:
    """List each setting that names an instructions or rule file, with the path it names.

    Args:
        constraints: The parsed proposal.

    Returns:
        ``(setting, path)`` pairs in file order, such as ``("[proposer] illegal", "docs/rules.md")``.
    """
    roles = [
        ("[proposer] illegal", constraints.proposer),
        ("[run] illegal", constraints.run_role),
        ("[metric] illegal", constraints.metric_role),
        *((f"[target.{name}] illegal", stage) for name, stage in constraints.targets.items()),
    ]
    settings = [("instructions", constraints.instructions_file)]
    settings += [(setting, role.illegal_file) for setting, role in roles]
    return [(setting, path) for setting, path in settings if path is not None]


def _load_rule_files(constraints: RepoConstraints, archive: Path) -> RepoConstraints:
    """Fill the intent and every role's rules from the files the toml names (E48).

    Args:
        constraints: The parsed proposal, with paths but no rule text.
        archive: Packed tree from :func:`fetch_snapshot`, the same snapshot the toml came from.

    Returns:
        The proposal with ``intent``, ``rules`` and each role's ``illegal`` filled.

    Raises:
        ConstraintsError: When a named file is missing, not a regular text file,
            too large, empty, or not a list of top-level bullets.
    """
    settings = _rule_file_settings(constraints)
    if not settings:
        return constraints
    texts: dict[str, str] = {}
    with tarfile.open(archive, "r:gz") as packed:
        members = {str(PurePosixPath(m.name)): m for m in packed.getmembers()}
        for setting, path in settings:
            if path not in texts:
                texts[path] = _read_text_file(packed, members.get(path), setting, path)

    def rules_of(role: RoleEnvironment, setting: str) -> RoleEnvironment:
        """Return the role with its ``illegal`` rules read from its rule file."""
        if role.illegal_file is None:
            return role
        return replace(role, illegal=_split_rules(texts[role.illegal_file], setting, role.illegal_file))

    intent = None
    if constraints.instructions_file is not None:
        intent = texts[constraints.instructions_file].strip()
        if not intent:
            raise ConstraintsError(
                f"{CONSTRAINTS_FILE}: instructions names '{constraints.instructions_file}', but that file is empty."
            )
        if len(intent) > _MAX_INTENT_CHARS:
            raise ConstraintsError(
                f"{CONSTRAINTS_FILE}: the instructions file '{constraints.instructions_file}' is longer than"
                f" {_MAX_INTENT_CHARS} characters."
            )
    proposer = rules_of(constraints.proposer, "[proposer] illegal")
    return replace(
        constraints,
        intent=intent,
        rules=proposer.illegal,
        proposer=proposer,
        run_role=rules_of(constraints.run_role, "[run] illegal"),
        metric_role=rules_of(constraints.metric_role, "[metric] illegal"),
        targets={name: rules_of(stage, f"[target.{name}] illegal") for name, stage in constraints.targets.items()},
    )


def _read_text_file(packed: tarfile.TarFile, member: tarfile.TarInfo | None, setting: str, path: str) -> str:
    """Read one instructions, rule or MCP file from the snapshot as text.

    Args:
        packed: The open snapshot.
        member: The file's entry, or ``None`` when the snapshot has no such path.
        setting: The setting that names the file, such as ``[proposer] illegal``, named in errors.
        path: The repository path the setting names.

    Returns:
        The file's text.

    Raises:
        ConstraintsError: When the file is missing, is a link or folder, is too large, or is not UTF-8 text.
    """
    if member is None:
        raise ConstraintsError(f"{CONSTRAINTS_FILE}: {setting} names '{path}', but the repository has no such file.")
    if not member.isfile():
        raise ConstraintsError(
            f"{CONSTRAINTS_FILE}: {setting} names '{path}', which must be a regular file, not a link or folder."
        )
    if member.size > _MAX_RULE_FILE_BYTES:
        raise ConstraintsError(
            f"{CONSTRAINTS_FILE}: {setting} names '{path}', which is larger than {_MAX_RULE_FILE_BYTES // 1024} KiB."
        )
    handle = packed.extractfile(member)
    try:
        # utf-8-sig drops a leading byte-order mark, which editors on Windows add.
        return (handle.read() if handle is not None else b"").decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ConstraintsError(f"{CONSTRAINTS_FILE}: {setting} names '{path}', which is not UTF-8 text.") from exc


def _split_rules(text: str, setting: str, path: str) -> tuple[str, ...]:
    """Split a markdown rule file into its rules: one rule for each top-level bullet.

    Indented lines, and lines that directly follow a bullet with no blank line
    between, continue that bullet. Headings and blank lines between bullets
    are allowed; any other text outside a bullet is refused, so a rule cannot
    be lost by being written as a paragraph.

    Args:
        text: The file's text.
        setting: The setting that names the file, named in errors.
        path: The file's repository path, named in errors.

    Returns:
        The rules in file order, each joined onto one line.

    Raises:
        ConstraintsError: When the file holds no bullet, more than the maximum
            bullets, a bullet that is empty or too long, or text outside a bullet.
    """
    rules: list[list[str]] = []
    open_rule = False
    after_blank = False
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            after_blank = True
            continue
        bullet = _BULLET.fullmatch(line.rstrip())
        if bullet is not None:
            rules.append([(bullet.group(1) or "").strip()])
            open_rule = True
        elif _HEADING.match(line):
            open_rule = False
        elif open_rule and (line[0] in " \t" or not after_blank):
            rules[-1].append(line.strip())
        else:
            raise ConstraintsError(
                f"{CONSTRAINTS_FILE}: line {number} of '{path}' ({setting}) is not part of a top-level bullet."
                " Write each rule as a line that starts with '- '."
            )
        after_blank = False
    if not rules:
        raise ConstraintsError(
            f"{CONSTRAINTS_FILE}: {setting} names '{path}', which holds no rules. Write each rule as a line"
            " that starts with '- '."
        )
    if len(rules) > _MAX_RULES:
        raise ConstraintsError(
            f"{CONSTRAINTS_FILE}: '{path}' ({setting}) holds {len(rules)} rules; the most is {_MAX_RULES}."
        )
    joined = [" ".join(part for part in parts if part) for parts in rules]
    for number, rule in enumerate(joined, start=1):
        if not rule or len(rule) > _MAX_RULE_CHARS:
            raise ConstraintsError(
                f"{CONSTRAINTS_FILE}: rule {number} in '{path}' ({setting}) must be text of 1 to"
                f" {_MAX_RULE_CHARS} characters."
            )
    return tuple(joined)


def _load_mcp_files(constraints: RepoConstraints, archive: Path) -> RepoConstraints:
    """Fill each role with the servers of the MCP file it names (E49).

    The files come from the same snapshot as the toml, so the servers a run
    gets are the ones the setup check showed.

    Args:
        constraints: The parsed proposal.
        archive: Packed tree from :func:`fetch_snapshot`.

    Returns:
        The proposal with each role's ``mcp_servers`` filled.

    Raises:
        ConstraintsError: When a named file is missing, not a regular text
            file, too large, or not a valid ``mcpServers`` file.
    """
    wanted = {label: role for label, role in constraints.mcp_roles.items() if role.mcp is not None}
    if not wanted:
        return constraints
    servers: dict[str, tuple[McpServer, ...]] = {}
    with tarfile.open(archive, "r:gz") as packed:
        members = {str(PurePosixPath(m.name)): m for m in packed.getmembers()}
        for label, role in wanted.items():
            path = role.mcp or ""
            text = _read_text_file(packed, members.get(path), f"{label} mcp", path)
            servers[label] = _mcp_servers(text, f"{label} mcp", path)
    return replace(
        constraints,
        proposer=replace(constraints.proposer, mcp_servers=servers.get("[proposer]", ())),
        run_role=replace(constraints.run_role, mcp_servers=servers.get("[run]", ())),
        metric_role=replace(constraints.metric_role, mcp_servers=servers.get("[metric]", ())),
        targets={
            name: replace(stage, mcp_servers=servers.get(f"[target.{name}]", ()))
            for name, stage in constraints.targets.items()
        },
    )


def _mcp_servers(text: str, setting: str, path: str) -> tuple[McpServer, ...]:
    """Read a standard ``.mcp.json`` file: ``{"mcpServers": {<name>: <server>}}`` (E49).

    Each server is an http server (``type`` http, streamable-http or sse, or a
    ``url`` with no type) with a url and headers; the relay calls it. A stdio
    server (``command``, ``args``, ``env``) fails: only http servers are
    supported now (E64). Secrets appear only as ``${NAME}`` in header values.

    Args:
        text: The file's text.
        setting: The setting that names the file, such as ``[metric] mcp``.
        path: The file's repository path.

    Returns:
        The servers in file order.

    Raises:
        ConstraintsError: When the file is not that shape, a server has an
            unknown type or key, or a value breaks the url or secret rules.
    """
    prefix = f"{CONSTRAINTS_FILE}: {setting} file '{path}'"
    try:
        document = json.loads(text)
    except (ValueError, RecursionError) as error:
        raise ConstraintsError(f"{prefix} is not valid JSON.") from error
    if not isinstance(document, dict) or set(document) != {"mcpServers"}:
        raise ConstraintsError(f'{prefix} must be one object with only "mcpServers": {{"<name>": {{...}}}}.')
    entries = document["mcpServers"]
    if not isinstance(entries, dict) or len(entries) > _MAX_TABLES:
        raise ConstraintsError(f"{prefix}: mcpServers must be an object of at most {_MAX_TABLES} servers.")
    return tuple(_mcp_server(name, entry, prefix) for name, entry in entries.items())


def _mcp_server(name: str, entry: Any, prefix: str) -> McpServer:
    """Read one server of an ``.mcp.json`` file.

    Args:
        name: The server's name.
        entry: The server's object.
        prefix: The file, named first in errors.

    Returns:
        The validated server.

    Raises:
        ConstraintsError: When the name, type, a key or a value is invalid.
    """
    if not _TABLE_NAME.fullmatch(name):
        raise ConstraintsError(
            f"{prefix}: the server name {name[:100]!r} must start with a letter or digit and use only letters,"
            " digits, '-' and '_' (at most 64)."
        )
    label = f"server '{name}'"
    if not isinstance(entry, dict):
        raise ConstraintsError(f"{prefix}: {label} must be an object.")
    # The type is matched without case, so "STDIO" gets the E64 message and "HTTP" passes.
    kind = entry.get("type", "http")
    if isinstance(kind, str):
        kind = kind.lower()
    if kind == "stdio" or _MCP_STDIO_KEYS & set(entry):
        raise ConstraintsError(
            f"{prefix}: {label} is a stdio server (command, args, env). Only http servers are supported now;"
            ' give it a "url" and put each secret in "headers" as ${NAME}.'
        )
    if not isinstance(kind, str) or kind not in _MCP_HTTP_TYPES:
        raise ConstraintsError(f"{prefix}: {label} type must be http, streamable-http or sse.")
    unknown = sorted(set(entry) - _MCP_HTTP_KEYS)
    if unknown:
        raise ConstraintsError(f"{prefix}: {label} {unknown[0][:100]} is not a supported setting for an http server.")
    if "url" not in entry:
        raise ConstraintsError(f"{prefix}: {label} needs a url.")
    headers = _service_headers(entry["headers"], label, prefix) if "headers" in entry else {}
    return McpServer(name=name, url=_service_url(entry["url"], label, prefix), headers=headers)


def _check_secret_receivers(services: Mapping[str, ServiceSpec], roles: Mapping[str, RoleEnvironment]) -> None:
    """Check that each secret goes to one host only (E52, E53 rule 3).

    The relay sends a secret only to the host it is bound to, so a file that
    sends ``${GOOGLE_MAPS_KEY}`` to a second host could leak the key there.

    Args:
        services: The file's ``[services]`` declarations.
        roles: Every role with its MCP servers, by label: ``[proposer]``,
            ``[run]``, ``[metric]`` and each ``[target.<name>]``.

    Raises:
        ConstraintsError: When one secret name goes to two different hosts.
    """
    places: dict[str, str] = {}
    uses = [
        (name, spec.host or "", f"[services.{service}]") for service, spec in services.items() for name in spec.secrets
    ]
    for label, role in roles.items():
        for server in role.mcp_servers:
            uses += [(name, server.host, f"{label} mcp server '{server.name}'") for name in server.secrets]
    for name, where, setting in uses:
        first = places.setdefault(name, where)
        if first != where:
            raise ConstraintsError(
                f"{CONSTRAINTS_FILE}: ${{{name}}} goes to {first} and, in {setting}, to {where}. A secret may go to"
                " one host only; use a separate secret for each host."
            )


def read_constraints_file(archive: Path) -> bytes | None:
    """Read the raw bytes of the constraints file at the root of a packed snapshot.

    Args:
        archive: Packed tree from :func:`fetch_snapshot`.

    Returns:
        The file's contents, not yet validated, or ``None`` when the repository has no such file.

    Raises:
        ConstraintsError: When the file is not a regular file or is too large.
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
        return handle.read() if handle is not None else b""


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

    The constraints file and every instructions, rule or MCP file it names
    are always protected, so no version can change the rules it is scored under.

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
    protected = tuple(
        dict.fromkeys((CONSTRAINTS_FILE, *constraints.rule_files, *constraints.mcp_files, *constraints.protected_paths))
    )
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
    if file is not None and file.intent:
        lines += ["", f"Guidance from `{file.instructions_file}` (not enforced; the scorer decides):", "", file.intent]
    return "\n".join(lines)


def numbered_rules(rules: Sequence[str]) -> list[tuple[str, str]]:
    """Name each plain-language rule the way the agent, judges and events refer to it.

    Args:
        rules: The rules in file order.

    Returns:
        ``("Rule 1", text)`` pairs, numbered from 1.
    """
    return [(f"Rule {index}", rule) for index, rule in enumerate(rules, start=1)]
