"""Read a repository's ``ascent.toml`` with the OpenAscent parser and Skynet's limits.

The ``openascent`` package holds the one parser of the contract file, so a
file that ``ascent check`` accepts is a file that Skynet accepts, apart from
Skynet's own limits (SKY-760). This module holds no rule about the form of
the file. It keeps the public names that Skynet's callers import, and adds
only what needs Skynet:

- Skynet's runner limits, which :func:`parse_constraints` and
  :func:`read_constraints` give to the package.
- The Compose check of ``[sandbox] compose``: :func:`read_constraints` gives
  the package Skynet's Compose parser, so the file's services fill
  ``compose_services``.
- ``CONSTRAINTS_REQUIRED``, the text that a new run without a contract gets.

The file format and its errors: ``docs/repository-constraints.md`` and the
package's own documentation.
"""

from __future__ import annotations

from pathlib import Path

from openascent.contract import (
    _DEFAULT_EXPLOITS,
    _NAMED_SECTIONS,
    _ROLE_KEYS,
    _SECTIONS,
    _TARGET_KEYS,
    CONSTRAINTS_FILE,
    DEFAULT_METRIC_ENTRY,
    MAXIMIZE,
    MINIMIZE,
    ConstraintsError,
    McpServer,
    MetricSpec,
    RepoConstraints,
    RepoEnvironment,
    RoleEnvironment,
    RunnerLimits,
    ServiceSpec,
    TargetStage,
    effective_environment,
    instructions_brief,
    numbered_rules,
    read_constraints_file,
    rule_file_settings,
    scorer_only_paths,
)
from openascent.contract import parse_constraints as _parse_constraints
from openascent.contract import read_constraints as _read_constraints

from ....sandbox.capabilities import VERCEL_CAPABILITIES
from ....sandbox.services import parse_compose

__all__ = [
    "CONSTRAINTS_FILE",
    "CONSTRAINTS_REQUIRED",
    "DEFAULT_METRIC_ENTRY",
    "MAXIMIZE",
    "MINIMIZE",
    "_DEFAULT_EXPLOITS",
    "_NAMED_SECTIONS",
    "_ROLE_KEYS",
    "_SECTIONS",
    "_TARGET_KEYS",
    "ConstraintsError",
    "McpServer",
    "MetricSpec",
    "RepoConstraints",
    "RepoEnvironment",
    "RoleEnvironment",
    "ServiceSpec",
    "TargetStage",
    "effective_environment",
    "instructions_brief",
    "numbered_rules",
    "parse_constraints",
    "read_constraints",
    "read_constraints_file",
    "rule_file_settings",
    "scorer_only_paths",
]

# Every new run needs a contract (SKY-693); the setup screen, the MCP contract step and staging all say this.
CONSTRAINTS_REQUIRED = (
    "The run needs an ascent.toml contract. Add ascent.toml to the repository,"
    " or write it on the Metric screen or with repo_draft_set_contract."
)
# Every box of a run is a Vercel Sandbox, so its vCPU cap is the contract's cap.
# Skynet sets no maximum timeout: the scorer's supervisor enforces each one (SKY-635).
_LIMITS = RunnerLimits(max_vcpus=VERCEL_CAPABILITIES.max_vcpus, max_timeout=None)


def parse_constraints(raw: bytes, *, require_version: bool = True, legacy_forms: bool = False) -> RepoConstraints:
    """Check the bytes of a contract file with the package parser and Skynet's limits.

    Args:
        raw: The file's contents.
        require_version: Whether a file with no ``version`` fails. ``False``
            reads it as version 1, for a run that started before the key
            was required (SKY-690); a wrong ``version`` fails either way.
        legacy_forms: Whether the package also accepts the older forms that
            Skynet's own reader accepted, such as a path with a ``/`` at the
            end. Only the re-read of a run that started under that reader
            passes ``True`` (SKY-760); a new check keeps ``False``. A
            ``network`` entry must name a public host either way.

    Returns:
        The validated proposal.

    Raises:
        ConstraintsError: When the file is too large, not TOML, or holds an
            unknown, malformed, or unsupported setting.
    """
    return _parse_constraints(raw, require_version=require_version, limits=_LIMITS, legacy_forms=legacy_forms)


def read_constraints(
    archive: Path, *, require_version: bool = True, legacy_forms: bool = False
) -> RepoConstraints | None:
    """Read the contract file at the root of a packed snapshot, with the files it names.

    Args:
        archive: Packed tree from :func:`fetch_snapshot`.
        require_version: Passed to :func:`parse_constraints`.
        legacy_forms: Passed to :func:`parse_constraints`. It also lets the
            ``type`` of a server in a named ``.mcp.json`` file match in any case.

    Returns:
        The validated proposal, or ``None`` when the repository has no such file.

    Raises:
        ConstraintsError: When the file or a file it names is invalid, or when
            the ``[sandbox] compose`` file is not a compose file the box can run.
    """
    return _read_constraints(
        archive,
        require_version=require_version,
        limits=_LIMITS,
        compose_parser=parse_compose,
        legacy_forms=legacy_forms,
    )
