"""Guest side of a host-CLI proposer turn: ship the workspace out, apply the result.

In local mode a proposer can run on the user's own coding CLI (Codex or Claude
Code) instead of inside the sandbox. This shim is the proposer
command the sandbox runs. It packs the current workspace, sends it to the
parent's ``/v1/_host_agent`` capability with the token the parent minted for
this proposer, and writes the tree that comes back over the workspace. The parent runs the CLI in a copy,
never in the user's own folder.

The sandbox image may not have the backend on its import path, so the
launch ships this file's source and runs it with ``python3``. It therefore
imports only the standard library. ``python -m core.worker.host_agent_shim``
runs the same code.
"""

from __future__ import annotations

import base64
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

CAPABILITY_PATH = "/v1/_host_agent"
# The guest relay and the parent gateway both refuse larger request bodies.
MAX_REQUEST_BYTES = 32 * 1024 * 1024
# Launch-generated config (this shim included) and bridge session state stay
# in the sandbox; the host CLI never needs them.
EXCLUDED_TOP_LEVEL = frozenset({".app", ".app-bridge"})
# Project config the host CLIs load (settings, hooks, MCP servers, skills).
# It never leaves the sandbox, so guest code cannot run on the Mac through it.
AGENT_CONFIG_NAMES = frozenset({".claude", ".codex", ".factory", ".agents", ".mcp.json", "CLAUDE.local.md"})
# The rendered system context of each CLI's turn. The host CLIs run with their own
# project-file loading off, so the parent puts this file into the prompt instead.
INSTRUCTIONS_FILES = {"codex": "AGENTS.md", "claude": "CLAUDE.md"}
# Skill folders the engines write (meta-harness, AutoSaddler). They are agent
# config, so they stay here, but the prompt points the proposer at them.
SKILL_DIRECTORIES = (".claude/skills", ".agents/skills")
_SKILLS_NOTE = (
    "\n\n# Skills\n\nThe skill files below are not in your copy of the workspace. "
    "Their text follows; follow them as if they were written for you.\n\n"
)
# CLI login file names; the host turn must never send one back to the guest.
LOGIN_FILE_NAMES = frozenset({"auth.json", ".credentials.json", ".claude.json"})
# The Mac's home volume ignores case, so ".Claude" opens ".claude".
_EXCLUDED_TOP_FOLDED = frozenset(name.casefold() for name in EXCLUDED_TOP_LEVEL)
_EXCLUDED_NAMES_FOLDED = frozenset(name.casefold() for name in AGENT_CONFIG_NAMES | LOGIN_FILE_NAMES)
FAILED_CODE = "blackbox.host_agent_failed"
RATE_LIMITED_CODE = "subscription.rate_limited"
FAILURE_PREFIX = "APP_HOST_AGENT_ERROR "
_TIMEOUT_MARGIN_SECONDS = 120.0


def _env(name: str, default: str = "") -> str:
    """Read an ``APP_*`` variable, falling back to its ``SKYNET_*`` spelling.

    Args:
        name: Variable name with the ``APP_`` prefix.

    Returns:
        The value, or ``default`` when neither spelling is set.
    """
    return os.environ.get(name) or os.environ.get("SKYNET_" + name.removeprefix("APP_")) or default


def endpoint_url() -> str:
    """Return the capability URL behind the sandbox relay or the gateway.

    Returns:
        The absolute ``/v1/_host_agent`` URL.

    Raises:
        RuntimeError: When neither a relay nor a gateway URL is set.
    """
    base = _env("APP_BUDGET_RELAY_URL") or _env("APP_GATEWAY_URL")
    if not base:
        raise RuntimeError("No model gateway is configured for the host agent.")
    return base.rstrip("/").removesuffix("/v1") + CAPABILITY_PATH


def excluded_path(relative: Path) -> bool:
    """Tell whether a workspace path stays out of every host-agent archive.

    Args:
        relative: Path relative to the workspace root.

    Returns:
        True for launch config, bridge state, any CLI's project config and CLI
        login file names, in any letter case.
    """
    parts = [part.casefold() for part in relative.parts]
    return bool(parts) and (parts[0] in _EXCLUDED_TOP_FOLDED or any(part in _EXCLUDED_NAMES_FOLDED for part in parts))


def skill_text(workspace: Path) -> str:
    """Render the workspace's skills as prompt text, so the host CLI reads them as data.

    Args:
        workspace: Directory the proposer works in.

    Returns:
        Each ``SKILL.md`` under the skill folders with its path, or "" when there is none.
    """
    root = workspace.resolve()
    sections = []
    for directory in SKILL_DIRECTORIES:
        for path in sorted((workspace / directory).glob("*/SKILL.md")):
            if path.is_file() and path.resolve().is_relative_to(root):
                text = path.read_text(encoding="utf-8", errors="replace")
                sections.append(f"--- {path.relative_to(workspace).as_posix()} ---\n{text}")
    return _SKILLS_NOTE + "\n".join(sections) if sections else ""


def pack_workspace(workspace: Path) -> tuple[bytes, set[str]]:
    """Archive the workspace as gzip tar, without following links.

    Args:
        workspace: Directory the proposer works in.

    Returns:
        The archive bytes and the relative paths of the regular files in it.
    """
    buffer = io.BytesIO()
    files: set[str] = set()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in sorted(workspace.rglob("*")):
            relative = path.relative_to(workspace)
            if excluded_path(relative):
                continue
            archive.add(path, arcname=relative.as_posix(), recursive=False)
            if path.is_file() and not path.is_symlink():
                files.add(relative.as_posix())
    return buffer.getvalue(), files


class ResultRefusedError(ValueError):
    """Raised for a returned tree the shim will not write into the workspace."""


def _checked_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    """Validate every returned member before anything is written.

    Args:
        archive: The returned archive.

    Returns:
        The members to apply, without launch files or agent config.

    Raises:
        ResultRefusedError: For an absolute or escaping path or link, a hard link,
            a FIFO or device, or a path through a returned link.
    """
    members = []
    links: set[str] = set()
    # The live workspace can still hold an old entry of another kind (a file where
    # a folder now goes), and realpath fails through it, so names are checked in an
    # empty folder. extractall(filter="data") checks the real tree again.
    with tempfile.TemporaryDirectory() as empty:
        for member in archive.getmembers():
            name = member.name.removeprefix("./")
            if not name or name == ".":
                continue
            if excluded_path(Path(name)):
                continue
            if Path(name).is_absolute():
                # data_filter would quietly make it relative; the parent never sends one.
                raise ResultRefusedError(f"The result has an absolute path: {name}")
            if member.islnk() or not (member.isreg() or member.isdir() or member.issym()):
                raise ResultRefusedError(f"The result has a special file: {name}")
            if any(name.startswith(f"{link}/") for link in links):
                raise ResultRefusedError(f"The result writes through a link: {name}")
            try:
                tarfile.data_filter(member, empty)
            except tarfile.FilterError as error:
                raise ResultRefusedError(f"The result has an unsafe entry: {error}") from error
            if member.issym():
                links.add(name.rstrip("/"))
            members.append(member)
    return members


def _remove(path: Path) -> None:
    """Delete a file, link or directory tree without following links.

    Args:
        path: Path to remove; a missing path is ignored.
    """
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif os.path.lexists(path):
        path.unlink()


def apply_result(workspace: Path, archive_bytes: bytes, shipped: set[str]) -> None:
    """Write the returned tree over the workspace, including deletions.

    Every member is checked first; a refused result leaves the workspace as it was.

    Args:
        workspace: Directory the proposer works in.
        archive_bytes: Gzip tar of the edited copy.
        shipped: Regular files that were sent, so files the CLI deleted are removed here too.

    Raises:
        ResultRefusedError: When the archive is unreadable or any member is unsafe.
    """
    try:
        archive = tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz")  # noqa: SIM115 - closed by the with below
    except (tarfile.TarError, OSError, EOFError) as error:
        raise ResultRefusedError(f"The result archive is invalid: {error}") from error
    with archive:
        members = _checked_members(archive)
        returned = {member.name.removeprefix("./") for member in members}
        for relative in shipped - returned:
            _remove(workspace / relative)
        for member in members:
            target = workspace / member.name.removeprefix("./")
            # A path the CLI turned into another kind (file, folder or link)
            # must not make extraction write through the old entry.
            same_kind = (member.isdir() and target.is_dir() and not target.is_symlink()) or (
                member.isreg() and target.is_file() and not target.is_symlink()
            )
            if os.path.lexists(target) and not same_kind:
                _remove(target)
        archive.extractall(workspace, members=members, filter="data")


def _post(url: str, token: str, body: bytes, timeout: float) -> tuple[int, dict[str, Any]]:
    """POST one JSON request and decode the JSON answer, error statuses included.

    Args:
        url: Capability URL.
        token: The proposer's route token.
        body: Encoded request.
        timeout: Socket timeout in seconds.

    Returns:
        HTTP status and decoded body (empty when the body is not JSON).
    """
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    try:
        document = json.loads(raw or b"{}")
    except ValueError:
        document = {}
    return status, document if isinstance(document, dict) else {}


def _fail(code: str, message: str, reset_at: str | None = None) -> int:
    """Report a failed turn on stderr in a form the parent and the user can read.

    Args:
        code: Stable failure key.
        message: Human-readable detail.
        reset_at: When a usage cap ends, if known.

    Returns:
        The shim's exit status.
    """
    detail = {"code": code, "message": message, "reset_at": reset_at}
    print(FAILURE_PREFIX + json.dumps(detail), file=sys.stderr, flush=True)
    return 3 if code == RATE_LIMITED_CODE else 2


def reply_refusals(document: dict[str, Any]) -> list[dict[str, str]]:
    """Keep only the tool and the path kind of each refusal the parent reports.

    Args:
        document: The parent's reply.

    Returns:
        One ``{"tool", "kind"}`` entry per well-formed refusal.
    """
    refusals = document.get("refusals")
    if not isinstance(refusals, list):
        return []
    return [
        {"tool": entry["tool"], "kind": entry["kind"]}
        for entry in refusals
        if isinstance(entry, dict) and isinstance(entry.get("tool"), str) and isinstance(entry.get("kind"), str)
    ]


def main() -> int:
    """Run one host-CLI proposer turn for the current workspace.

    Returns:
        0 when the edited tree was applied, otherwise a non-zero status.
    """
    workspace = Path.cwd()
    prompt_file = _env("APP_PROMPT_FILE")
    prompt = Path(prompt_file).read_text(encoding="utf-8") if prompt_file else ""
    prompt += skill_text(workspace)
    archive, shipped = pack_workspace(workspace)
    # The parent takes the CLI and its permission from the run settings only.
    body = json.dumps(
        {
            "model": _env("APP_MODEL"),
            "prompt": prompt,
            "workspace_tar_b64": base64.b64encode(archive).decode("ascii"),
        }
    ).encode()
    if len(body) > MAX_REQUEST_BYTES:
        return _fail(
            FAILED_CODE,
            f"The workspace is too large for a host agent turn ({len(body)} bytes, limit {MAX_REQUEST_BYTES}).",
        )
    timeout = float(_env("APP_HOST_AGENT_TIMEOUT", "3600")) + _TIMEOUT_MARGIN_SECONDS
    try:
        status, document = _post(endpoint_url(), _env("APP_HOST_AGENT_TOKEN"), body, timeout)
    except (OSError, RuntimeError) as error:
        return _fail(FAILED_CODE, f"The host agent could not be reached: {error}")
    error = document.get("error") if isinstance(document.get("error"), dict) else {}
    if status != 200 or document.get("status") != "ok":
        code = str(error.get("code") or FAILED_CODE)
        message = str(error.get("message") or document.get("transcript_tail") or f"HTTP {status}")
        return _fail(code, message, error.get("reset_at"))
    try:
        apply_result(workspace, base64.b64decode(str(document.get("result_tar_b64") or "")), shipped)
    except (ResultRefusedError, ValueError) as error:
        return _fail(FAILED_CODE, f"The host agent's result was refused: {error}")
    usage = document.get("usage") or {}
    print(
        json.dumps(
            {
                "type": "host_agent.result",
                "text": str(document.get("transcript_tail") or ""),
                "usage": {
                    "input_tokens": int(usage.get("prompt_tokens") or 0),
                    "output_tokens": int(usage.get("completion_tokens") or 0),
                },
                "refusals": reply_refusals(document),
            }
        ),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
