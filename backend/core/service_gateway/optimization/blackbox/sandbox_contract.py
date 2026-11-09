"""Sandbox contract shared by every runtime: specs, results, sessions and naming.

A leaf module, so a runtime module (``sandbox_runtime``) and the settings
factories in ``sandbox`` can both use it without importing each other.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from ....sandbox.services import ComposeService

# Slack past the in-sandbox timeout before the service kills the wrapper as well.
KILL_GRACE_SECONDS = 15.0
# Called with the stream name (``stdout``/``stderr``) and each new piece of output.
OutputSink = Callable[[str, str], None]
# Every sandbox carries this tag so stragglers from a crashed worker can be found and destroyed.
# Both tags are stored on live Vercel sandboxes and queried by the straggler sweep;
# renaming them would hide boxes created before the rename.
SANDBOX_TAG = {"skynet": "blackbox"}
JOB_TAG = "skynet_job"
_NAME_UNSAFE = re.compile(r"[^a-z0-9-]+")
_NAME_MAX_CHARS = 60
_NAME_SUFFIX_CHARS = 8


@dataclass(frozen=True)
class SandboxSpec:
    """What a session needs to open a sandbox."""

    lifetime_seconds: float
    env: Mapping[str, str] = field(default_factory=dict)
    name: str | None = None
    tags: Mapping[str, str] = field(default_factory=dict)
    # Container image; the runtime's default when unset.
    image: str | None = None
    # Host → headers the network edge adds to the box's requests to that host, so a
    # secret reaches a service without ever entering the box.
    inject_headers: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    network_disabled: bool = False
    # When set, the box reaches only these hosts; every other destination is denied.
    allowed_hosts: tuple[str, ...] = ()
    vcpus: int = 2
    operation_key: str | None = None
    # Holds code no optimizer may read (the metric): a local workspace opens
    # under a private root instead of beside proposer workspaces.
    private: bool = False
    # The repository's ``[sandbox] compose`` services; a runtime that cannot
    # run them refuses the spec instead of opening a box without them.
    services: tuple[ComposeService, ...] = ()
    # Start from this after-setup snapshot. A runtime without snapshots
    # ignores it and opens a normal box, so callers always check the tree.
    snapshot_id: str | None = None


@dataclass(frozen=True)
class CommandResult:
    """Outcome of one command inside a sandbox."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        """Return True when the command exited 0."""
        return self.exit_code == 0


class SandboxSession(Protocol):
    """One open sandbox: write files, run commands, read results, destroy."""

    def write_files(self, files: Mapping[str, str]) -> None:
        """Write text files at paths relative to the working directory, creating parents.

        Args:
            files: Relative path → content.
        """
        ...

    def run(
        self,
        command: str,
        *,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        on_output: OutputSink | None = None,
        sudo: bool = False,
    ) -> CommandResult:
        """Run ``command`` through ``bash --noprofile --norc -c`` in the working directory and wait for it.

        Args:
            command: Shell command line.
            env: Extra environment for this command.
            timeout_seconds: Kill the command after this long.
            on_output: Receives each new piece of output while the command runs.
            sudo: Run as root. Only the control plane may ask for it.

        Returns:
            Exit code, captured output and whether the timeout fired.
        """
        ...

    def read_file(self, path: str) -> str | None:
        """Return the text of ``path`` (relative to the working directory), or ``None`` when absent.

        Args:
            path: Relative file path.

        Returns:
            The file's text, or ``None``.
        """
        ...

    def close(self) -> None:
        """Destroy the sandbox and reconcile any protected usage."""
        ...


class SandboxRuntime(Protocol):
    """Creates sandboxes."""

    # Whether ``SandboxSpec.inject_headers`` is honoured; without a network edge the
    # caller has to hand the box its secrets some other way.
    injects_headers: bool

    def open(self, spec: SandboxSpec) -> SandboxSession:
        """Create a sandbox per ``spec``.

        Args:
            spec: Lifetime, environment, image, header injection and labels for the sandbox.

        Returns:
            The session over the new sandbox.
        """
        ...


def unique_sandbox_name(stem: str) -> str:
    """Return ``stem`` in a form Vercel accepts, with a random suffix that keeps every open apart.

    Vercel refuses to create a sandbox whose name is still taken in the
    project, and a job opens boxes from several holdout threads at once (one
    per case) and again on every retry, so a name derived from the job id
    alone collides. The suffix survives the length cap: trimming falls on the
    stem, never on the part that makes the name unique.

    Args:
        stem: The readable part, normally carrying the job id.

    Returns:
        ``<stem>-<8 hex chars>``: lower-case, ``[a-z0-9-]`` only, at most 60 characters.
    """
    safe = _NAME_UNSAFE.sub("-", stem.lower()).strip("-")
    head = safe[: _NAME_MAX_CHARS - _NAME_SUFFIX_CHARS - 1].rstrip("-")
    return f"{head}-{uuid.uuid4().hex[:_NAME_SUFFIX_CHARS]}"


def _pump(name: str, pipe: Any, captured: list[str], on_output: OutputSink | None) -> None:
    """Read ``pipe`` line by line until it closes, keeping every line and forwarding it.

    Args:
        name: The stream name handed to ``on_output``.
        pipe: The text pipe to drain.
        captured: Receives every line, for the command's captured output.
        on_output: Receives each line as it arrives, when set.
    """
    with pipe:
        for line in iter(pipe.readline, ""):
            captured.append(line)
            if on_output is not None:
                on_output(name, line)
