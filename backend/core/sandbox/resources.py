"""Resource requests and the sandbox spec that a provider validates and opens.

Adapted from Harbor (harbor-framework/harbor), Apache-2.0: ``ResourceMode``
comes from ``harbor/models/trial/config.py``. Changes: the per-resource
mode fields collapse into one ``mode`` on a frozen dataclass, and the spec
fields come from Skynet's blackbox ``SandboxSpec`` with ``vcpus`` moved into
``ResourceRequest``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum


class ResourceMode(StrEnum):
    """Say how a provider applies a requested resource size."""

    AUTO = "auto"
    LIMIT = "limit"
    REQUEST = "request"
    GUARANTEE = "guarantee"
    IGNORE = "ignore"


@dataclass(frozen=True)
class ResourceRequest:
    """Describe the compute size that a sandbox asks for.

    ``memory_mb`` and ``disk_mb`` stay ``None`` when the provider default
    applies. ``IGNORE`` sends no size, so validation skips the size checks.
    """

    cpus: int = 2
    memory_mb: int | None = None
    disk_mb: int | None = None
    gpu: int = 0
    mode: ResourceMode = ResourceMode.REQUEST


@dataclass(frozen=True)
class SandboxSpec:
    """Describe one sandbox before a provider opens it.

    ``inject_headers`` maps an allowed host to the headers that the provider
    adds to egress requests for that host.
    """

    lifetime_seconds: float
    resources: ResourceRequest = field(default_factory=ResourceRequest)
    env: Mapping[str, str] = field(default_factory=dict)
    name: str | None = None
    tags: Mapping[str, str] = field(default_factory=dict)
    image: str | None = None
    inject_headers: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    network_disabled: bool = False
    allowed_hosts: tuple[str, ...] = ()
    operation_key: str | None = None
    private: bool = False
