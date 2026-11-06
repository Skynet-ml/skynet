"""Parent side of a host-CLI proposer turn, run on the user's own subscription.

In local mode a black-box proposer can be the user's own coding CLI (Codex
or Claude Code). The sandbox never reaches the CLI itself. Its shim
(:mod:`core.worker.host_agent_shim`) sends the workspace to the gateway's
``/v1/_host_agent`` capability. This module unpacks that archive into a fresh
copy under ``local_state_dir``, runs the CLI there with provider API keys
removed from its environment, and sends the edited tree back. The CLI never
runs in the user's original folder.

The guest is untrusted. The CLI and its permission come only from the run's
stored proposer settings, given to :func:`bind_host_agent` by the worker, and
only the token minted for that proposer reaches the capability. Agent config
in the copy (``.claude/``, ``.codex/``, ``.factory/``, MCP files) is removed
before the CLI starts, and a ``workspace_only`` turn runs under a macOS
``sandbox-exec`` profile that denies reads and writes in the user's home
except the turn's own directory and the CLI's install paths. A codex turn's
login copy holds only placeholders; its model calls go through a per-turn
loopback proxy (:mod:`core.billing.codex_auth_proxy`) that adds the real token.

The turn is recorded in the run's budget at zero cost: the user's
subscription pays, not the wallet.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import ctypes
import io
import json
import logging
import os
import platform
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.parse
import uuid
import weakref
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast

from ..config import Settings
from ..i18n import t_locale
from ..local.cli_catalog import CliProvider, cli_efforts, cli_model_arg, parse_subscription_model_id
from ..local.cli_isolation import CODEX_AUTH_FILE, isolated_cli
from ..local.cli_probe import cli_binary, codex_subscription_login, read_cli_limits, write_cli_limit
from ..worker.host_agent_shim import INSTRUCTIONS_FILES, excluded_path
from .cli_runners import limit_error
from .codex_auth_proxy import CodexAuthProxy, jwt_claims, placeholder_login, real_account
from .model_dispatch import ModelHTTPResult
from .model_gateway import ROUTE_KEY, ModelGateway
from .operation_pricing import ChargePolicy, operation_quote
from .runtime import PaidResult

logger = logging.getLogger(__name__)

HOST_AGENT_PATH = "/v1/_host_agent"
# Droid has no flag that skips the user's own config, hooks and MCP servers,
# so it cannot run isolated and is not a host proposer option.
HOST_CLIS: tuple[CliProvider, ...] = ("codex", "claude")
HostPermission = Literal["workspace_only", "full_access"]
HOST_PERMISSIONS: tuple[HostPermission, ...] = ("workspace_only", "full_access")
HOST_AGENT_TOKEN_KEY = "host_agent_token"
SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
# Agent sockets act outside the sandbox (the SSH agent signs with the user's keys).
RATE_LIMITED_CODE = "subscription.rate_limited"
FAILED_KEY = "blackbox.host_agent_failed"
NOT_STARTED_KEY = "_cli_not_started"
CLAUDE_WORKSPACE_TOOLS = ("Read", "Edit", "Write", "Glob", "Grep")
CODEX_SUBSCRIPTION_HINT = (
    "Codex is not signed in with a ChatGPT subscription in a login file. Sign in with ChatGPT, not an API key: "
    'set `cli_auth_credentials_store = "file"` in `~/.codex/config.toml`, then run `codex login`.'
)
CODEX_EXPIRED_HINT = "Your Codex login expired. Run `codex` once in a terminal to refresh it, then retry."
# The turn's login copy has no refresh token, so these mean an expired access token.
_CODEX_AUTH_FAILURE = re.compile(
    r"(?<![\w-])401(?![\w-])"
    r"|\b(?:auth\w*|login|log in|sign in|token)\W+(?:\w+\W+){0,3}?"
    r"(?:expired|invalid|revoked|refreshed|unauthori[sz]ed)\b"
    r"|\brefresh(?:ing)?[ _]token\b|\btoken refresh\b",
    re.IGNORECASE,
)
RATE_LIMITED_KEY = "blackbox.host_agent_rate_limited"
# The guest relay and the gateway refuse larger request bodies, so a bigger
# archive could never have arrived; the bound keeps a compressed bomb small too.
MAX_REQUEST_BYTES = 32 * 1024 * 1024
MAX_EXTRACTED_BYTES = 512 * 1024 * 1024
MAX_MEMBERS = 50_000
_TRANSCRIPT_TAIL_CHARS = 4000


@dataclass(frozen=True)
class HostAgentFailure:
    """A host turn that must end the run, with the user-facing reason."""

    code: str
    message: str
    cli: str
    reset_at: str | None = None


class HostAgentRequestError(ValueError):
    """Raised for a request the capability refuses before any CLI runs."""


_failures: weakref.WeakKeyDictionary[ModelGateway, HostAgentFailure] = weakref.WeakKeyDictionary()
_failures_lock = threading.Lock()


def host_cli_argv(
    cli: CliProvider,
    permission: str,
    binary: str,
    workdir: Path,
    model_arg: str | None,
    isolation_args: tuple[str, ...] = (),
    effort: str | None = None,
) -> list[str]:
    """Build the CLI command for one turn; the prompt goes on stdin.

    Args:
        cli: ``codex`` or ``claude``.
        permission: ``workspace_only`` runs under :func:`confine`, which keeps
            writes in the turn; ``full_access`` runs with no sandbox at all.
        binary: Resolved CLI executable.
        workdir: The copied workspace.
        model_arg: The CLI's own model name, or None for its default.
        isolation_args: Flags that keep the user's own config out.
        effort: An effort level from :func:`_effort_arg`, or None for the CLI's default.

    Returns:
        The argv list.

    Raises:
        HostAgentRequestError: When the CLI or permission is unknown.
    """
    if permission not in HOST_PERMISSIONS:
        raise HostAgentRequestError(f"Unknown host permission {permission!r}.")
    full = permission == "full_access"
    if cli == "codex":
        # Under workspace_only the macOS profile from confine() is the sandbox:
        # codex's own one cannot start inside it.
        mode = ["--dangerously-bypass-approvals-and-sandbox"] if full else ["--sandbox", "danger-full-access"]
        # The copy is not a git checkout, and codex refuses to run outside one
        # without this flag.
        argv = [binary, "exec", *isolation_args, "--json", *mode, "--skip-git-repo-check", "-C", str(workdir)]
    elif cli == "claude":
        mode = "bypassPermissions" if full else "acceptEdits"
        argv = [binary, *isolation_args, "-p", "--output-format", "json", "--permission-mode", mode]
        if not full:
            # The profile must let claude run /usr/bin/security for its own login, so any
            # tool that runs a command (Bash, Monitor, Task agents, Workflow) could read
            # decrypted Keychain items. This allow-list has only file tools.
            argv += ["--tools", ",".join(CLAUDE_WORKSPACE_TOOLS)]
    else:
        raise HostAgentRequestError(f"Unknown host CLI {cli!r}.")
    if model_arg:
        argv += ["--model", model_arg]
    if effort:
        # ``codex exec`` has no effort flag; its config key takes the same levels.
        argv += ["-c", f"model_reasoning_effort={json.dumps(effort)}"] if cli == "codex" else ["--effort", effort]
    return argv


def _catalog_slug(cli: CliProvider, model: str) -> str | None:
    """Find the catalog slug the CLI runs for the proposer's model id.

    Args:
        cli: The CLI that runs the turn.
        model: ``subscription/<cli>/<slug>``, a plain catalog slug, or empty.

    Returns:
        The slug, or None when the CLI has no model for it and runs its own default.
    """
    model = model.removeprefix("openrouter/")
    parsed = parse_subscription_model_id(model)
    slug = parsed[1] if parsed else model
    if not slug:
        return None
    try:
        cli_model_arg(cli, slug)
    except ValueError:
        return None
    return slug


def _model_arg(cli: CliProvider, model: str) -> str | None:
    """Map the proposer's model id to the CLI's ``--model`` value.

    Args:
        cli: The CLI that runs the turn.
        model: ``subscription/<cli>/<slug>``, a plain catalog slug, or empty.

    Returns:
        The CLI's model name, or None to let the CLI use its own default.
    """
    slug = _catalog_slug(cli, model)
    return cli_model_arg(cli, slug) if slug else None


def _effort_arg(cli: CliProvider, model: str, effort: str | None) -> str | None:
    """Map the run's effort to a level the CLI takes for the proposer's model.

    Like the CLI dispatcher, Max runs at the model's top level when the model
    has no Max, and any other level the model does not take is dropped.

    Args:
        cli: The CLI that runs the turn.
        model: ``subscription/<cli>/<slug>``, a plain catalog slug, or empty.
        effort: The reflection model's effort from the run settings, or None.

    Returns:
        A level from :data:`~core.local.cli_catalog.CLI_EFFORTS`, or None for the CLI's default.
    """
    slug = _catalog_slug(cli, model)
    levels = cli_efforts(cli, slug) if slug and effort else ()
    if effort == "max" and levels and "max" not in levels:
        return levels[-1]
    return effort if effort in levels else None


def _extract(archive_bytes: bytes, destination: Path) -> None:
    """Unpack an untrusted guest archive into ``destination`` only.

    Args:
        archive_bytes: Gzip tar from the sandbox.
        destination: Empty directory that becomes the CLI's workspace.

    Raises:
        HostAgentRequestError: When the archive is unreadable, too large, or escapes ``destination``.
    """
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
            members = archive.getmembers()
            if len(members) > MAX_MEMBERS or sum(member.size for member in members) > MAX_EXTRACTED_BYTES:
                raise HostAgentRequestError("The workspace is too large for a host agent turn.")
            archive.extractall(destination, filter="data")
    except (tarfile.TarError, OSError, EOFError) as error:
        raise HostAgentRequestError(f"The workspace archive is invalid: {error}") from error


def strip_agent_config(workdir: Path) -> None:
    """Remove launch files and every CLI's project config from the copy.

    Project settings, hooks and MCP servers would run on the Mac, outside the
    sandbox, so none may come from the guest.

    Args:
        workdir: The copied workspace, before the CLI starts.
    """
    for path in sorted(workdir.rglob("*"), key=lambda item: len(item.parts)):
        if not excluded_path(path.relative_to(workdir)) or not os.path.lexists(path):
            continue
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()


def _safe_link(workdir: Path, path: Path) -> bool:
    """Tell whether a link in the copy stays inside the copy.

    Args:
        workdir: The copied workspace.
        path: A symbolic link under ``workdir``.

    Returns:
        True for a relative link whose target resolves inside ``workdir``.
    """
    target = path.readlink()
    if target.is_absolute():
        return False
    resolved = os.path.normpath(path.parent / target)
    return os.path.commonpath([resolved, str(workdir)]) == str(workdir)


# Wrapped base64 (openssl base64, MIME, base64.encodebytes) splits a token copy
# across lines, and JSON output escapes those line breaks as "\n", so a copy is
# also looked for with both removed.
_LINE_BREAKS = re.compile(r"\s+|\\[nrt]")
_LINE_BREAK_BYTES = re.compile(rb"\s+|\\[nrt]")


def _encoded_forms(value: str) -> set[bytes]:
    """Return the byte forms in which a file can hold a token: plain, hex, percent and base64.

    A base64 copy depends on where the token starts in a 3-byte group, so
    each start gives one form: the characters that only token bytes decide.

    Args:
        value: A token value.

    Returns:
        The forms to look for.
    """
    raw = value.encode()
    forms = {
        raw,
        raw.hex().encode(),
        raw.hex().upper().encode(),
        urllib.parse.quote(value, safe="").encode(),
        "".join(f"%{byte:02X}" for byte in raw).encode(),
        "".join(f"%{byte:02x}" for byte in raw).encode(),
    }
    for shift in range(3):
        start, end = -(-8 * shift // 6), 8 * (shift + len(raw)) // 6
        for encode in (base64.b64encode, base64.urlsafe_b64encode):
            forms.add(encode(b"\0" * shift + raw)[start:end])
    return forms


def _redact(text: str, secret_values: set[str]) -> str:
    """Replace each login token in a CLI's output, in any form of :func:`_encoded_forms`.

    Args:
        text: Output that goes to the guest.
        secret_values: Token values of the CLI's login.

    Returns:
        The text with every token form replaced by ``[redacted]``; only ``[redacted]``
        when a form shows up only after line breaks are removed, since it cannot be
        cut out of the wrapped lines.
    """
    forms = sorted(
        {form.decode("utf-8", "replace") for value in secret_values for form in _encoded_forms(value)}, key=len
    )
    for form in reversed(forms):
        text = text.replace(form, "[redacted]")
    joined = _LINE_BREAKS.sub("", text)
    if any(form in joined for form in forms):
        return "[redacted]"
    return text


def _holds_secret(path: Path, forms: set[bytes]) -> bool:
    """Tell whether a file contains one of the CLI's login tokens in any form of :func:`_encoded_forms`.

    Args:
        path: A regular file in the copy.
        forms: The encoded forms of the login's token values.

    Returns:
        True when any form appears in the file's bytes, also with line breaks removed.
    """
    if not forms:
        return False
    try:
        data = path.read_bytes()
    except OSError:
        return True
    joined = _LINE_BREAK_BYTES.sub(b"", data)
    return any(form in data or form in joined for form in forms)


def _pack(workdir: Path, secret_values: set[str] | frozenset[str] = frozenset()) -> bytes:
    """Archive the edited copy without following links.

    Launch files, agent config, login files, files that hold a login token (plain or encoded),
    special files and links that leave the copy stay out, so the guest never
    receives a path into the Mac or the user's login.

    Args:
        workdir: The CLI's workspace after the turn.
        secret_values: Token values of the CLI's login.

    Returns:
        Gzip tar bytes.
    """
    forms = {form for value in secret_values for form in _encoded_forms(value)}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in sorted(workdir.rglob("*")):
            relative = path.relative_to(workdir)
            if excluded_path(relative):
                continue
            if path.is_symlink():
                if not _safe_link(workdir, path):
                    logger.warning("Host agent: dropped a link that leaves the copy")
                    continue
            elif path.is_file():
                if _holds_secret(path, forms):
                    logger.warning("Host agent: dropped a file that holds the CLI's login")
                    continue
            elif not path.is_dir():
                continue
            archive.add(path, arcname=relative.as_posix(), recursive=False)
    return buffer.getvalue()


def _real(path: Path | str) -> str:
    """Return the real path, as Seatbelt compares real paths only.

    Args:
        path: Any path.

    Returns:
        The absolute path with every link resolved.
    """
    return os.path.realpath(path)


def _install_root(path: Path | str, home: str) -> bool:
    """Tell whether a directory may be opened for reading inside the home.

    Args:
        path: A real directory path.
        home: The user's real home directory.

    Returns:
        False for the home itself, a directory that contains it, and a folder
        directly in it (``~/.cargo`` holds ``credentials.toml`` next to ``bin``).
    """
    path = str(path)
    if os.path.commonpath([path, home]) == path:
        return False
    if os.path.commonpath([path, home]) != home:
        return True
    return len(Path(path).relative_to(home).parts) >= 2


def tool_roots(binary: str, home: Path | str | None = None) -> list[str]:
    """Return the directories a CLI must read to start.

    Covers the command's own directory, its install tree and, for a script,
    its interpreter's install tree (for example a Node install for Codex).
    A root that would open the home, or a folder directly in it, is left out.

    Args:
        binary: Absolute path of the CLI command.
        home: The user's home directory; the process's home by default.

    Returns:
        Real directory paths.
    """
    real_home = _real(home if home is not None else Path.home())
    real = Path(_real(binary))
    roots = {_real(Path(binary).parent), str(real.parent)}
    parts = real.parts
    if "node_modules" in parts:
        index = parts.index("node_modules")
        prefix = parts[: index - 1] if index > 0 and parts[index - 1] == "lib" else parts[:index]
        roots.add(str(Path(*prefix)))
    else:
        # A release folder (codex keeps codex-resources beside bin/).
        roots.add(str(real.parent.parent))
    try:
        with real.open("rb") as handle:
            first = handle.readline(512)
    except OSError:
        first = b""
    if first.startswith(b"#!"):
        words = first[2:].decode("utf-8", "replace").split()
        interpreter = (
            words[1] if words and Path(words[0]).name == "env" and len(words) > 1 else words[0] if words else ""
        )
        found = shutil.which(interpreter) if interpreter else None
        if found:
            roots.add(str(Path(_real(found)).parent.parent))
    return sorted(root for root in roots if _install_root(root, real_home))


def confine(
    argv: list[str],
    *,
    home: Path | str,
    writable: list[str],
    readable: list[str],
    hidden: tuple[str, ...] = (),
) -> list[str]:
    """Wrap a command in a macOS sandbox that hides the user's home.

    The command may write only to ``writable`` (and devices). Inside ``home``
    and ``hidden`` it may also read only ``writable`` and ``readable``;
    everything else there is denied. File metadata stays readable so path
    lookups and ``getcwd`` work. It cannot connect to any Unix socket (the
    Docker daemon and the SSH agent act outside the sandbox), run
    ``launchctl``, create a launchd job, open an app through LaunchServices
    or send AppleEvents: each of these starts code outside the sandbox.
    Other reads outside ``home``
    and the TCP network stay open: the CLI needs system libraries and its
    provider. macOS refuses a nested sandbox under a profile with deny rules,
    so this profile is the CLI's only sandbox.

    Args:
        argv: The CLI command.
        home: The user's home directory.
        writable: Directories the turn owns (the copy, its temp space and home).
        readable: The CLI's install directories.
        hidden: More directories to hide, such as the shared temp folder.

    Returns:
        The ``sandbox-exec`` command.

    Raises:
        HostAgentRequestError: When ``sandbox-exec`` is not available.
    """
    if sys.platform != "darwin" or not SANDBOX_EXEC.exists():
        raise HostAgentRequestError("workspace_only host agents need macOS sandbox-exec.")
    params = {"HOME_DIR": _real(home)}
    hidden_rules = ['(subpath (param "HOME_DIR"))']
    for index, path in enumerate(hidden):
        params[f"H{index}"] = _real(path)
        hidden_rules.append(f'(subpath (param "H{index}"))')
    write_rules = []
    for index, path in enumerate(writable):
        params[f"W{index}"] = _real(path)
        write_rules.append(f'(subpath (param "W{index}"))')
    read_rules = []
    for index, path in enumerate(readable):
        params[f"R{index}"] = _real(path)
        read_rules.append(f'(subpath (param "R{index}"))')
    # Later rules win, so each allow below opens a hole in the denies above it.
    profile = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        f"(deny file-read-data file-read-xattr {' '.join(hidden_rules)})",
        '(allow file-write* (subpath "/dev"))',
        "(deny network-outbound (remote unix-socket))",
        # Name lookups go through this socket; without it no provider host resolves.
        '(allow network-outbound (remote unix-socket (path-literal "/private/var/run/mDNSResponder")))',
        '(deny process-exec (literal "/bin/launchctl"))',
        # `open` asks LaunchServices to start an app outside this sandbox. It needs both
        # services, so denying only one of them does not stop it.
        '(deny mach-lookup (global-name "com.apple.coreservices.launchservicesd")'
        ' (global-name "com.apple.coreservices.quarantine-resolver"))',
        "(deny job-creation)",
        "(deny appleevent-send)",
    ]
    if read_rules:
        profile.append(f"(allow file-read-data file-read-xattr {' '.join(read_rules)})")
    if write_rules:
        profile.append(f"(allow file-read-data file-read-xattr file-write* {' '.join(write_rules)})")
    defines = [item for key, value in params.items() for item in ("-D", f"{key}={value}")]
    return [str(SANDBOX_EXEC), "-p", "\n".join(profile), *defines, *argv]


TURN_MARKER_ENV = "SKYNET_HOST_TURN"
_PROC_PIDVNODEPATHINFO = 9
# struct proc_vnodepathinfo: the cwd's vnode_info (152 bytes), then its path.
_VNODE_PATH_INFO_SIZE = 2352
_CWD_PATH = slice(152, 152 + 1024)
_libproc: list[Any] = []
_sandbox: list[Any] = []
_SANDBOX_FILTER_PATH = 1


def _cwd(pid: int) -> str | None:
    """Return a process's working directory.

    Args:
        pid: The process id.

    Returns:
        The real path, or None when it cannot be read.
    """
    if sys.platform == "linux":
        try:
            return str(Path(f"/proc/{pid}/cwd").readlink())
        except OSError:
            return None
    if sys.platform != "darwin":
        return None
    if not _libproc:
        library = ctypes.CDLL("/usr/lib/libproc.dylib")
        library.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        _libproc.append(library)
    buffer = ctypes.create_string_buffer(_VNODE_PATH_INFO_SIZE)
    if _libproc[0].proc_pidinfo(pid, _PROC_PIDVNODEPATHINFO, 0, buffer, _VNODE_PATH_INFO_SIZE) != _VNODE_PATH_INFO_SIZE:
        return None
    return buffer.raw[_CWD_PATH].split(b"\0", 1)[0].decode("utf-8", "replace") or None


def _marked(marker: str) -> set[int]:
    """Find this user's processes whose environment holds the turn marker.

    macOS hides the environment of its own system binaries, so this finds
    only some processes. It adds to the other rules and never replaces them.

    Args:
        marker: The ``NAME=value`` marker of the turn.

    Returns:
        Process ids.
    """
    found = set()
    if sys.platform == "linux":
        needle = marker.encode()
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if needle in (entry / "environ").read_bytes().split(b"\0"):
                    found.add(int(entry.name))
            except OSError:
                continue
        return found
    listing = subprocess.run(
        ["/bin/ps", "-x", "-E", "-ww", "-o", "pid=,command="], capture_output=True, text=True, check=False
    ).stdout
    for line in listing.splitlines():
        fields = line.split()
        if fields and fields[0].isdigit() and marker in fields[1:]:
            found.add(int(fields[0]))
    return found


def _confined_to(pids: set[int], root: str, home: str) -> set[int]:
    """Find the processes that run under this turn's Seatbelt profile.

    A process cannot leave its sandbox, so this finds a turn process that
    left the tree, its group and the turn folder and cleared its environment.
    The turn's profile lets it write the turn's folder but not the folder
    around it or the home. A system service sandbox that may write the
    shared temp tree can also write the folder around the turn, so it does
    not match.

    Args:
        pids: Candidate process ids.
        root: The turn's real folder; it must exist.
        home: The user's real home directory.

    Returns:
        The process ids under this turn's profile; empty off macOS.
    """
    if sys.platform != "darwin":
        return set()
    if not _sandbox:
        library = ctypes.CDLL("/usr/lib/system/libsystem_sandbox.dylib")
        library.sandbox_check.restype = ctypes.c_int
        # Two runs' first sweeps can get here at once; one whole-list assignment
        # keeps the list at two items, where extend would leave four.
        _sandbox[:] = [library.sandbox_check, ctypes.c_int.in_dll(library, "SANDBOX_CHECK_NO_REPORT").value]
    check, no_report = _sandbox

    def allowed(pid: int, path: str) -> bool:
        """Ask the kernel whether ``pid`` may write ``path``, without a log entry.

        Args:
            pid: The process id.
            path: An existing real path.

        Returns:
            True when the write is allowed.
        """
        # sandbox_check takes the path as a variadic argument, which arm64 Darwin
        # passes on the stack: five dummies fill the remaining argument registers.
        padding = (ctypes.c_long(0),) * 5 if platform.machine() == "arm64" else ()
        filter_type = ctypes.c_int(_SANDBOX_FILTER_PATH | no_report)
        return check(ctypes.c_int(pid), b"file-write-data", filter_type, *padding, ctypes.c_char_p(path.encode())) == 0

    found = set()
    outside = (str(Path(root).parent), home)
    for pid in pids:
        confined = check(ctypes.c_int(pid), None, ctypes.c_int(0)) == 1
        if confined and allowed(pid, root) and not any(allowed(pid, path) for path in outside):
            found.add(pid)
    return found


def _process_table() -> list[tuple[int, int, int]]:
    """List this user's live processes with their parent and group.

    Returns:
        ``(pid, ppid, pgid)`` rows, without zombies.
    """
    rows = []
    if sys.platform == "linux":
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat_line = (entry / "stat").read_text()
            except OSError:
                continue
            # The command name in field 2 can hold spaces, so fields count from its ")".
            fields = stat_line.rsplit(")", 1)[-1].split()
            if fields[0] != "Z":
                rows.append((int(entry.name), int(fields[1]), int(fields[2])))
        return rows
    listing = subprocess.run(
        ["/bin/ps", "-x", "-o", "pid=,ppid=,pgid=,stat="], capture_output=True, text=True, check=False
    ).stdout
    for line in listing.splitlines():
        fields = line.split()
        if len(fields) == 4 and fields[0].isdigit() and not fields[3].startswith("Z"):
            rows.append((int(fields[0]), int(fields[1]), int(fields[2])))
    return rows


def _turn_processes(leader: int | None, root: Path, marker: str) -> set[int]:
    """Find every live process of one turn, also those that left its group or session.

    A process belongs to the turn when it is the leader or its descendant,
    when it works inside the turn's folder (an escaped tool command keeps the
    folder it started in), when its environment holds the turn marker, when
    it runs under the turn's Seatbelt profile, or when it shares a group with
    one that does.

    Args:
        leader: The CLI's own process while it is not reaped, else None.
        root: The turn's folder.
        marker: The ``NAME=value`` marker in the turn's environment.

    Returns:
        Process ids, without this process.
    """
    rows = _process_table()
    real_root = _real(root)
    live = {pid for pid, _, _ in rows}
    found = (set() if leader is None else {leader}) | (_marked(marker) & live)
    found |= _confined_to(live - {os.getpid()}, real_root, _real(Path.home()))
    for pid, _, _ in rows:
        cwd = _cwd(pid)
        if cwd is not None and (cwd == real_root or cwd.startswith(real_root + os.sep)):
            found.add(pid)
    children: dict[int, list[int]] = {}
    for pid, ppid, _ in rows:
        children.setdefault(ppid, []).append(pid)
    groups = {pgid for pid, _, pgid in rows if pid in found}
    while True:
        before = len(found)
        found |= {pid for pid, _, pgid in rows if pgid in groups}
        queue = list(found)
        while queue:
            for child in children.get(queue.pop(), []):
                if child not in found:
                    found.add(child)
                    queue.append(child)
        groups |= {pgid for pid, _, pgid in rows if pid in found}
        if len(found) == before:
            break
    own_group = os.getpgid(0)
    turn = {pid for pid, _, pgid in rows if pid in found and pgid != own_group}
    if leader is not None:
        turn.add(leader)
    return turn - {os.getpid()}


def _stop_turn(process: subprocess.Popen[str], root: Path, marker: str) -> None:
    """Kill every process of one turn and every group they lead, until none is left.

    A CLI runs each tool command in a new session (``setsid``), so killing the
    leader's group alone would leave those commands running on the Mac.

    Args:
        process: The CLI, started in its own session.
        root: The turn's folder.
        marker: The ``NAME=value`` marker in the turn's environment.
    """
    own_group = os.getpgid(0)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        # A group id is not reused while any member lives, so this is safe even
        # after the leader is reaped.
        os.killpg(process.pid, signal.SIGKILL)
    for _ in range(50):
        # A reaped leader's pid can belong to another process by now.
        pids = _turn_processes(process.pid if process.poll() is None else None, root, marker)
        if not pids:
            return
        for pid in pids:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                group = os.getpgid(pid)
                if group != own_group:
                    os.killpg(group, signal.SIGKILL)
                os.kill(pid, signal.SIGKILL)
        time.sleep(0.1)
    logger.warning("Host agent: some turn processes did not stop")


def run_group(
    argv: list[str], *, cwd: Path, env: dict[str, str], stdin: str, timeout: float, root: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a command in its own session and kill all its processes when it ends.

    A CLI starts tools and helpers of its own, some in new sessions, so a
    timeout that killed only the direct child or its group would leave them
    running on the Mac.

    Args:
        argv: The command.
        cwd: Working directory.
        env: Full environment.
        stdin: Text written to standard input.
        timeout: Seconds before every process of the turn is killed.
        root: The turn's folder; a process working inside it belongs to the
            turn. ``cwd`` by default.

    Returns:
        The finished process with its captured output.

    Raises:
        subprocess.TimeoutExpired: When the command ran longer than ``timeout``.
        OSError: When the command could not start.
    """
    root = root or cwd
    marker_value = uuid.uuid4().hex
    marker = f"{TURN_MARKER_ENV}={marker_value}"
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env={**env, TURN_MARKER_ENV: marker_value},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired:
        _stop_turn(process, root, marker)
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        raise
    finally:
        _stop_turn(process, root, marker)
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


def _json_documents(stdout: str) -> list[dict[str, Any]]:
    """Read every JSON object a CLI printed, whole-output or one per line.

    Args:
        stdout: Captured CLI output.

    Returns:
        Decoded objects in output order.
    """
    try:
        whole = json.loads(stdout)
    except ValueError:
        whole = None
    if isinstance(whole, dict):
        return [whole]
    documents = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            documents.append(value)
    return documents


def parse_cli_output(cli: CliProvider, stdout: str) -> tuple[str, dict[str, Any], str | None]:
    """Pull the final text, token usage and any error out of a CLI's JSON output.

    Args:
        cli: The CLI that printed ``stdout``.
        stdout: Captured CLI output.

    Returns:
        ``(text, usage, error)``. ``usage`` has ``prompt_tokens``,
        ``completion_tokens`` and ``model_ms``; ``error`` is the CLI's own
        failure message, or None.
    """
    text, error = "", None
    prompt_tokens = completion_tokens = 0
    model_ms: int | None = None
    documents = _json_documents(stdout)
    if cli == "codex":
        # Codex prints an "error" event for a stream retry ("Reconnecting... 1/5")
        # and goes on; only an error that no completed turn follows is a failure.
        for event in documents:
            kind = event.get("type")
            item = event.get("item") or {}
            if kind == "item.completed" and item.get("type") == "agent_message":
                text = str(item.get("text") or "")
            elif kind == "turn.completed":
                usage = event.get("usage") or {}
                prompt_tokens += int(usage.get("input_tokens") or 0)
                completion_tokens += int(usage.get("output_tokens") or 0)
                error = None
            elif kind == "turn.failed":
                error = str((event.get("error") or {}).get("message") or "The turn failed.")
            elif kind == "error":
                error = str(event.get("message") or "The CLI reported an error.")
    else:
        result = next((doc for doc in reversed(documents) if doc.get("type") == "result"), None)
        if result is None and documents:
            result = documents[-1]
        result = result or {}
        text = str(result.get("result") or "")
        usage = result.get("usage") or {}
        prompt_tokens = int(usage.get("input_tokens") or 0) + int(usage.get("cache_read_input_tokens") or 0)
        prompt_tokens += int(usage.get("cache_creation_input_tokens") or 0)
        completion_tokens = int(usage.get("output_tokens") or 0)
        duration = result.get("duration_api_ms", result.get("duration_ms"))
        model_ms = int(duration) if isinstance(duration, int | float) else None
        if result.get("is_error") or result.get("subtype", "success") != "success":
            error = text or str(result.get("subtype") or "The CLI reported an error.")
    return text, {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "model_ms": model_ms}, error


def _recorded_cap(settings: Settings, cli: str) -> datetime | None:
    """Return when a cap recorded for ``cli`` ends, while it is still in force.

    Args:
        settings: Settings with ``local_state_dir``.
        cli: The CLI about to run.

    Returns:
        The reset time, or None when no cap is in force.
    """
    try:
        stamp = (read_cli_limits(settings).get(cli) or {}).get("reset_at")
        reset = datetime.fromisoformat(str(stamp)) if stamp else None
    except (OSError, ValueError):
        return None
    if reset is None:
        return None
    reset = reset if reset.tzinfo else reset.replace(tzinfo=UTC)
    return reset if reset > datetime.now(UTC) else None


def _workspace_root(settings: Settings) -> Path:
    """Return the directory that holds every host-agent copy.

    Args:
        settings: Settings with ``local_state_dir``.

    Returns:
        ``<local_state_dir>/workspaces``.

    Raises:
        HostAgentRequestError: When local mode or its state directory is not configured.
    """
    if not settings.local_mode or not settings.local_state_dir:
        raise HostAgentRequestError("Host agents need local mode with a local state directory.")
    return Path(settings.local_state_dir).expanduser().resolve() / "workspaces"


def _error(code: str, message: str, *, reset_at: str | None = None, transcript: str = "") -> dict[str, Any]:
    """Build an error response in the capability's contract shape.

    Args:
        code: Stable failure code.
        message: Human-readable detail.
        reset_at: ISO time a usage cap ends, if any.
        transcript: Tail of the CLI's output.

    Returns:
        The response document.
    """
    return {
        "status": "error",
        "result_tar_b64": "",
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0, "model_ms": None},
        "transcript_tail": transcript[-_TRANSCRIPT_TAIL_CHARS:],
        "error": {"code": code, "reset_at": reset_at, "message": message},
    }


def _not_started(document: dict[str, Any]) -> dict[str, Any]:
    """Mark an error response as one that never started the CLI.

    Args:
        document: A response from :func:`_error`.

    Returns:
        The same response with :data:`NOT_STARTED_KEY` set; the proposer
        route removes the key before the guest sees the response.
    """
    return {**document, NOT_STARTED_KEY: True}


def _login_paths(cli: CliProvider) -> list[str]:
    """Return the paths in the user's home a CLI must read for its subscription login.

    Codex gets its login copy in the turn's own home, so it needs nothing
    here. Claude Code reads its login from the login Keychain with
    ``/usr/bin/security``. The profile cannot tell claude from its children,
    so any process of a claude turn that can run ``security`` can read
    decrypted Keychain items (the claude login, other apps' tokens).
    :func:`host_cli_argv` gives a workspace_only claude turn only file tools
    (no Bash, Monitor, Task or Workflow) as the mitigation. The Keychain files stay read-only, because the Keychain
    service writes a refreshed login itself.
    ``~/.claude.json`` (the user's own MCP servers, which the user's later
    sessions start outside any sandbox) and ``~/.claude`` (other sessions'
    transcripts, the user's own instructions) stay hidden.

    Args:
        cli: The CLI of the turn.

    Returns:
        Paths the turn may read under the user's home.
    """
    if cli != "claude":
        return []
    return [str(Path.home() / "Library" / "Keychains")]


def _turn_home(cli: CliProvider, env: dict[str, str], turn: Path) -> CodexAuthProxy | None:
    """Give the CLI a home inside the turn, so no change it makes reaches the user's.

    The turn's tool commands can read and write the codex login copy and open
    TCP connections, so the copy holds only placeholders
    (:func:`placeholder_login`): no real access, refresh or id token, account
    id or e-mail. The returned proxy holds the real access token outside the
    turn and puts it on codex's model calls. A changed copy never goes back
    over the user's login. Claude Code gets an empty home whose ``Library``
    links the real one, so it still finds its Keychain login.

    Args:
        cli: The CLI of the turn.
        env: The CLI's environment from :func:`isolated_cli`; updated in place.
        turn: The turn's folder.

    Returns:
        For codex, the turn's auth proxy, not yet started; else None.

    Raises:
        HostAgentRequestError: When codex has no ChatGPT subscription login,
            or its access token has already expired.
    """
    home = turn / "home"
    home.mkdir()
    env["HOME"] = str(home)
    if cli == "codex":
        codex_home = turn / "codex"
        codex_home.mkdir()
        raw = _read_no_follow(Path(env["CODEX_HOME"]) / CODEX_AUTH_FILE)
        env["CODEX_HOME"] = str(codex_home)
        try:
            document = json.loads(raw) if raw is not None else None
        except ValueError:
            document = None
        if not codex_subscription_login(document):
            # An API-key login bills the user's API account, and a host turn must
            # cost nothing beyond the subscription.
            raise HostAgentRequestError(CODEX_SUBSCRIPTION_HINT)
        assert isinstance(document, dict)
        access_token = str(document["tokens"]["access_token"])
        expires = jwt_claims(access_token).get("exp")
        # Neither the turn nor the proxy has the refresh token, so an expired login cannot recover.
        if isinstance(expires, int) and expires <= time.time() + 60:
            raise HostAgentRequestError(CODEX_EXPIRED_HINT)
        copy, placeholder = placeholder_login(document)
        descriptor = os.open(codex_home / CODEX_AUTH_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            json.dump(copy, handle)
        return CodexAuthProxy(access_token=access_token, account_id=real_account(document), placeholder=placeholder)
    if cli == "claude":
        library = Path.home() / "Library"
        if library.is_dir():
            (home / "Library").symlink_to(library)
    return None


def _login_secrets(raw: bytes) -> tuple[str | None, set[str]]:
    """Read the account and the token values from a codex login file.

    Args:
        raw: The ``auth.json`` bytes.

    Returns:
        The account id (None when the file has none) and every token value.
    """
    try:
        document = json.loads(raw)
    except ValueError:
        return None, set()
    if not isinstance(document, dict):
        return None, set()
    tokens = document.get("tokens") if isinstance(document.get("tokens"), dict) else {}
    values = {value for value in [*tokens.values(), document.get("OPENAI_API_KEY")] if isinstance(value, str)}
    account = tokens.get("account_id")
    return (account if isinstance(account, str) and account else None), {value for value in values if len(value) >= 16}


def _read_no_follow(path: Path) -> bytes | None:
    """Read a regular file without following a link.

    Args:
        path: The file.

    Returns:
        Its bytes, or None for a link, a missing file or another kind.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    with os.fdopen(descriptor, "rb") as handle:
        return handle.read() if stat.S_ISREG(os.fstat(handle.fileno()).st_mode) else None


def _command(binary: str, env: dict[str, str]) -> str:
    """Resolve the CLI command to an absolute path.

    Args:
        binary: The configured command name or path.
        env: The CLI's environment, for its ``PATH``.

    Returns:
        The absolute path.

    Raises:
        HostAgentRequestError: When the command is not installed.
    """
    found = shutil.which(binary, path=env.get("PATH"))
    if found is None:
        raise HostAgentRequestError(f"The CLI command {binary!r} was not found.")
    return str(Path(found).absolute())


def run_host_agent(
    body: dict,
    settings: Settings,
    *,
    cli: CliProvider,
    permission: HostPermission,
    job: str = "run",
    reasoning_effort: str | None = None,
) -> dict:
    """Run one host-CLI turn on a copy of the guest workspace.

    Args:
        body: ``{model, prompt, workspace_tar_b64}`` from the guest. Any
            ``cli`` or ``permission`` it names is ignored.
        settings: Local-mode settings (CLI paths, state dir, timeout).
        cli: The CLI from the run's stored proposer settings.
        permission: The permission from the run's stored proposer settings.
        job: Folder name that groups this run's copies.
        reasoning_effort: The reflection model's effort from the run's stored settings, or None.

    Returns:
        ``{status, result_tar_b64, usage, transcript_tail, error?}``.
    """
    if cli not in HOST_CLIS:
        return _not_started(_error(FAILED_KEY, f"Unknown host CLI {cli!r}."))
    if permission not in HOST_PERMISSIONS:
        return _not_started(_error(FAILED_KEY, f"Unknown host permission {permission!r}."))
    try:
        root = _workspace_root(settings)
        archive = base64.b64decode(str(body.get("workspace_tar_b64") or ""), validate=True)
    except (HostAgentRequestError, binascii.Error) as error:
        return _not_started(_error(FAILED_KEY, str(error)))
    capped_until = _recorded_cap(settings, cli)
    if capped_until is not None:
        message = f"{cli} is capped until {capped_until.isoformat()}"
        return _not_started(_error(RATE_LIMITED_CODE, message, reset_at=capped_until.isoformat()))
    safe_job = "".join(char for char in job if char.isalnum() or char in "-_") or "run"
    turn = root / safe_job / uuid.uuid4().hex
    workdir = turn / "workspace"
    workdir.mkdir(parents=True)
    prompt = str(body.get("prompt") or "")
    try:
        _extract(archive, workdir)
        strip_agent_config(workdir)
        # The isolation flags stop the CLI from loading project files, this one included.
        instructions = _read_no_follow(workdir / INSTRUCTIONS_FILES[cli])
        if instructions:
            prompt = f"{instructions.decode('utf-8', errors='replace')}\n\n{prompt}"
        turn_tmp = turn / "tmp"
        turn_tmp.mkdir()
        with isolated_cli(cli) as isolation, contextlib.ExitStack() as turn_services:
            env = dict(isolation.env)
            # Each turn gets its own temp folder; the shared one holds other apps' files.
            # Claude Code ignores TMPDIR and uses /tmp/claude-<uid> unless CLAUDE_CODE_TMPDIR is set.
            env.update({name: str(turn_tmp) for name in ("TMPDIR", "TMP", "TEMP", "CLAUDE_CODE_TMPDIR")})
            login = _read_no_follow(Path(env["CODEX_HOME"]) / CODEX_AUTH_FILE) if cli == "codex" else None
            _, secret_values = _login_secrets(login or b"")
            proxy = _turn_home(cli, env, turn)
            isolation_args = isolation.args
            if proxy is not None:
                turn_services.enter_context(proxy)
                isolation_args = (*isolation_args, *proxy.config_args())
                ca_file = turn / "codex-proxy-ca.pem"
                ca_file.write_bytes(proxy.ca_pem)
                env["CODEX_CA_CERTIFICATE"] = str(ca_file)
                secret_values.update(_login_secrets((turn / "codex" / CODEX_AUTH_FILE).read_bytes())[1])
            binary = _command(cli_binary(cli, settings), env)
            model = str(body.get("model") or "")
            argv = host_cli_argv(
                cli,
                permission,
                binary,
                workdir,
                _model_arg(cli, model),
                isolation_args,
                _effort_arg(cli, model, reasoning_effort),
            )
            if permission == "workspace_only":
                argv = confine(
                    argv,
                    home=Path.home(),
                    writable=[str(turn)],
                    readable=[*tool_roots(binary), *_login_paths(cli)],
                    hidden=(tempfile.gettempdir(), "/private/tmp"),
                )
            started = time.monotonic()
            try:
                completed = run_group(
                    argv, cwd=workdir, env=env, stdin=prompt, timeout=settings.host_agent_timeout_seconds, root=turn
                )
            except subprocess.TimeoutExpired:
                return _error(FAILED_KEY, f"The {cli} turn ran longer than {settings.host_agent_timeout_seconds:.0f}s.")
            except OSError as error:
                return _not_started(_error(FAILED_KEY, f"The {cli} CLI could not start: {error}"))
        elapsed_ms = int((time.monotonic() - started) * 1000)
        # The transcript and the error text go to the guest like the files do.
        stdout, stderr = _redact(completed.stdout, secret_values), _redact(completed.stderr, secret_values)
        text, usage, cli_error = parse_cli_output(cli, stdout)
        transcript = text or stdout or stderr
        if completed.returncode != 0 and cli_error is None:
            cli_error = (stderr or stdout).strip()[-2000:] or f"exit status {completed.returncode}"
        if cli_error is not None:
            capped = limit_error(cli, f"{cli_error}\n{stderr}")
            if capped is not None:
                reset_at = capped.reset_at.isoformat() if capped.reset_at else None
                if capped.reset_at is not None:
                    try:
                        write_cli_limit(settings, cli, capped.reset_at, capped.message)
                    except (OSError, ValueError):
                        logger.warning("Host agent: could not record the %s usage cap", cli, exc_info=True)
                return _error(RATE_LIMITED_CODE, capped.message, reset_at=reset_at, transcript=transcript)
            detail = cli_error[:2000]
            if cli == "codex" and _CODEX_AUTH_FAILURE.search(cli_error):
                detail = f"{CODEX_EXPIRED_HINT} ({detail})"
            return _error(FAILED_KEY, detail, transcript=transcript)
        return {
            "status": "ok",
            "result_tar_b64": base64.b64encode(_pack(workdir, secret_values)).decode("ascii"),
            "usage": {**usage, "model_ms": usage["model_ms"] or elapsed_ms, "cost": 0.0},
            "transcript_tail": transcript[-_TRANSCRIPT_TAIL_CHARS:],
        }
    except HostAgentRequestError as error:
        # Every HostAgentRequestError comes from a check that runs before the CLI starts.
        return _not_started(_error(FAILED_KEY, str(error)))
    finally:
        shutil.rmtree(turn, ignore_errors=True)


def host_proposer(payload: dict[str, Any]) -> tuple[CliProvider, HostPermission] | None:
    """Read the host CLI and permission from a run's stored proposer settings.

    Args:
        payload: The stored run payload (black-box ``proposer`` or unified
            ``optimizer.proposer``).

    Returns:
        ``(cli, permission)``, or None when the proposer runs in the sandbox.
    """
    optimizer = payload.get("optimizer")
    proposer = payload.get("proposer") or (optimizer.get("proposer") if isinstance(optimizer, dict) else None)
    if not isinstance(proposer, dict) or proposer.get("host_cli") is None:
        return None
    permission = proposer.get("host_permission") or "workspace_only"
    return cast(CliProvider, proposer["host_cli"]), cast(HostPermission, permission)


def _proposer_model_extra(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the ``extra`` settings of a run's proposer model.

    Args:
        payload: The stored run payload (black-box ``reflection_model_config`` or
            unified ``models.reflection_model``).

    Returns:
        The model's ``extra`` mapping, or an empty one when the payload has none.
    """
    config = payload.get("reflection_model_config")
    if not isinstance(config, dict) and isinstance(payload.get("models"), dict):
        config = payload["models"].get("reflection_model")
    extra = config.get("extra") if isinstance(config, dict) else None
    return extra if isinstance(extra, dict) else {}


def host_reasoning_effort(payload: dict[str, Any]) -> str | None:
    """Read the proposer model's effort from a run's stored settings.

    Args:
        payload: The stored run payload.

    Returns:
        The effort, or None when the run chose none.
    """
    effort = _proposer_model_extra(payload).get("reasoning_effort")
    return effort if isinstance(effort, str) and effort else None


def attach_host_agent_token(payload: dict[str, Any], token: str) -> None:
    """Hand the host proposer's token to the guest beside its proposer route.

    Args:
        payload: The protected payload, mutated in place.
        token: The token :func:`bind_host_agent` minted.

    Raises:
        ValueError: When the payload has no proposer route.
    """
    route = _proposer_model_extra(payload).get(ROUTE_KEY)
    if not isinstance(route, dict):
        raise ValueError("A host CLI proposer needs its proposer model route.")  # noqa: TRY004 - a bad payload, not a bad argument type
    route[HOST_AGENT_TOKEN_KEY] = token


def host_agent_failure(gateway: ModelGateway) -> HostAgentFailure | None:
    """Return the host turn failure that must end this gateway's run, if any.

    Args:
        gateway: The run's parent gateway.

    Returns:
        The first recorded failure, or None.
    """
    with _failures_lock:
        return _failures.get(gateway)


def failure_message(failure: HostAgentFailure) -> str:
    """Render the run's failure message for a host turn.

    Args:
        failure: The recorded failure.

    Returns:
        English text naming the CLI and, for a usage cap, its reset time.
    """
    if failure.code == RATE_LIMITED_CODE:
        return t_locale("en", RATE_LIMITED_KEY, cli=failure.cli, reset_at=failure.reset_at or "unknown")
    return t_locale("en", FAILED_KEY, cli=failure.cli, detail=failure.message)


def _bearer(headers: dict[str, str]) -> str:
    """Return the caller's token from its forwarded headers.

    Args:
        headers: Lower-cased request headers.

    Returns:
        The bearer or ``x-api-key`` token, or an empty string.
    """
    return headers.get("authorization", "").removeprefix("Bearer ") or headers.get("x-api-key", "")


def _reply(status: int, document: dict[str, Any]) -> ModelHTTPResult:
    """Encode a capability answer.

    Args:
        status: HTTP status.
        document: Response document.

    Returns:
        The JSON response.
    """
    return ModelHTTPResult(status, "application/json", json.dumps(document).encode())


def bind_host_agent(
    gateway: ModelGateway, settings: Settings, *, cli: str, permission: str, reasoning_effort: str | None = None
) -> str:
    """Serve ``/v1/_host_agent`` for one run whose proposer is a host CLI.

    The worker calls this only for a run whose stored proposer names a host
    CLI, with that CLI and permission. The guest cannot choose either. Only
    the token returned here reaches the capability; a model route token is
    refused. Each turn is recorded through the run's budget at zero cost. A
    usage cap is remembered on the gateway so the worker can end the run with
    ``blackbox.host_agent_rate_limited``.

    Args:
        gateway: The run's parent gateway.
        settings: Local-mode settings.
        cli: The proposer's host CLI from the run settings.
        permission: The proposer's host permission from the run settings.
        reasoning_effort: The proposer model's effort from the run settings, or None.

    Returns:
        The token the host proposer's shim must present.

    Raises:
        ValueError: Outside local mode, or for an unknown CLI or permission.
    """
    if not settings.local_mode:
        raise ValueError("Host agents are available only in local mode.")
    if cli not in HOST_CLIS or permission not in HOST_PERMISSIONS:
        raise ValueError(f"Unknown host proposer {cli!r} with permission {permission!r}.")
    trusted_cli = cast(CliProvider, cli)
    trusted_permission = cast(HostPermission, permission)
    token = secrets.token_urlsafe(32)
    runtime = gateway.runtime
    policy = ChargePolicy("subscription_model")

    def handle(raw: bytes, headers: dict[str, str]) -> ModelHTTPResult:
        """Run one turn under the run's budget and answer in the contract shape.

        Args:
            raw: Request body bytes.
            headers: Forwarded protocol headers.

        Returns:
            The JSON response; 403 for any other token, 429 for a usage cap,
            413 for an oversized workspace.
        """
        if not secrets.compare_digest(_bearer(headers), token):
            return _reply(403, _error("forbidden", "This capability needs the host proposer's token."))
        if len(raw) > MAX_REQUEST_BYTES:
            return _reply(413, _error(FAILED_KEY, "The workspace is too large for a host agent turn."))
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return _reply(400, _error(FAILED_KEY, "The host agent request is not a JSON object."))
        request = {"cli": trusted_cli, "model": body.get("model"), "permission": trusted_permission}
        # The bare slug the CLI ran, as a CLI dispatcher records it; None for the CLI's default.
        model = _catalog_slug(trusted_cli, str(body.get("model") or ""))
        quote = operation_quote(
            {**request, "path": HOST_AGENT_PATH, "nonce": uuid.uuid4().hex},
            Decimal(0),
            policy,
            {"source": "host_agent", "cli": trusted_cli, "model": model},
        )

        def dispatch() -> PaidResult[dict[str, Any]]:
            """Run the CLI; the subscription pays, so the settled cost is zero."""
            document = run_host_agent(
                body,
                settings,
                cli=trusted_cli,
                permission=trusted_permission,
                job=str(runtime.budget_id),
                reasoning_effort=reasoning_effort,
            )
            started = not document.pop(NOT_STARTED_KEY, False)
            usage = document["usage"]
            evidence = {
                "source": "host_agent",
                "cli": trusted_cli,
                "status": document["status"],
                "prompt_tokens": usage["prompt_tokens"],
                "completion_tokens": usage["completion_tokens"],
                "model_ms": usage["model_ms"],
                # The run's Usage tab reads these three, as for a CLI dispatcher call.
                "model": model,
                "usage": {"prompt_tokens": usage["prompt_tokens"], "completion_tokens": usage["completion_tokens"]},
                "latency_ms": usage["model_ms"],
            }
            if not started:
                # The local usage panel must not count a turn that no CLI ran (SKY-341).
                evidence["dispatched"] = False
            return PaidResult(document, Decimal(0), evidence)

        document = runtime.execute(
            quote,
            policy,
            dispatch,
            operation_key=f"host_agent:{uuid.uuid4().hex}",
            cost_kind="model",
            role="proposer",
        )
        error = document.get("error") or {}
        status = 200
        if document["status"] != "ok":
            status = 429 if error.get("code") == RATE_LIMITED_CODE else 502
            failure = HostAgentFailure(
                code=str(error.get("code")),
                message=str(error.get("message") or ""),
                cli=trusted_cli,
                reset_at=error.get("reset_at"),
            )
            if failure.code == RATE_LIMITED_CODE:
                with _failures_lock:
                    _failures.setdefault(gateway, failure)
        return _reply(status, document)

    gateway.bind_capability(HOST_AGENT_PATH, handle, token=token)
    return token
