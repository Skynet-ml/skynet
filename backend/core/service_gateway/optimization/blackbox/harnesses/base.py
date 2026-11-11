"""Shared types and helpers every harness module builds its launch from."""

from __future__ import annotations

import shlex
from collections.abc import Callable
from dataclasses import dataclass, field

from .....config import with_legacy_env

PROVIDER = "app"
PROMPT_FILE = "task/PROMPT.md"
ANSWER_FILE = "output/answer.txt"
ENV_MODEL = "APP_MODEL"
ENV_GATEWAY_URL = "APP_GATEWAY_URL"
ENV_API_KEY = "APP_API_KEY"
ENV_PROMPT_FILE = "APP_PROMPT_FILE"
ENV_ANSWER_FILE = "APP_ANSWER_FILE"

Usage = dict[str, int]
OutputParser = Callable[[str], tuple[str | None, Usage]]

_PROMPT_ARG = f'"$(cat "${ENV_PROMPT_FILE}")"'
# The bridge sets these only when the run hides scoring-side paths: Pi and
# Prime load the refusal extension even with --no-extensions, and Codex reads
# the refusal hook from its home directory.
_GUARD_PI_ARG = '${APP_GUARD_PI:+-e "$APP_GUARD_PI"} '
_GUARD_CODEX_COPY = '[ -z "$APP_GUARD_CODEX" ] || cp "$APP_GUARD_CODEX" .app/codex/hooks.json; '


# A run's temp folder holds the harness's state outside the workspace, and an
# EXIT trap removes it. A SIGKILL at the turn timeout skips that trap, and one
# box runs many proposer turns, so each run first removes the folders of dead
# runs, as the Fabric run script does. A live run holds an flock on its
# ``owner.lock`` through file descriptor 9, which the harness inherits, and the
# kernel drops the lock when every holder dies, so a concurrent run's folder
# stays. The lock file gets its final name only after it is locked, so a sweep
# never sees an unlocked lock of a live run. A run killed before the rename
# leaves a folder with no lock; the sweep removes such a folder once it is
# older than ``STALE_SECONDS``, far longer than that short window.
_TEMP_ROOT_SWEEP = """\
import fcntl
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

STALE_SECONDS = 600
prefix = sys.argv[1]
for old in Path(tempfile.gettempdir()).glob(prefix + "*"):
    if old.is_symlink():
        continue
    try:
        fd = os.open(old / "owner.lock", os.O_RDONLY)
    except FileNotFoundError:
        try:
            stale = time.time() - old.stat().st_mtime > STALE_SECONDS
        except OSError:
            continue
        if stale and old.is_dir():
            shutil.rmtree(old, ignore_errors=True)
        continue
    except OSError:
        continue
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        continue
    else:
        shutil.rmtree(old, ignore_errors=True)
    finally:
        os.close(fd)
print(tempfile.mkdtemp(prefix=prefix))
"""
# The lock goes on the shell's descriptor 9, not on one this process opens, so
# it outlives this process.
_TEMP_ROOT_LOCK = (
    "import fcntl, os, sys; fcntl.flock(9, fcntl.LOCK_EX); "
    'os.rename(os.path.join(sys.argv[1], "owner.tmp"), os.path.join(sys.argv[1], "owner.lock"))'
)


@dataclass(frozen=True)
class GatewayConfig:
    """Where a sandboxed harness sends its model calls."""

    url: str
    api_key: str


@dataclass(frozen=True)
class HarnessLaunch:
    """Everything a sandbox needs to run one harness on one case."""

    instructions_file: str
    run_command: str
    parse_output: OutputParser
    install_command: str | None = None
    files: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)


def _npm_install(binary: str, package: str, version: str) -> str:
    """Return a command that installs ``package`` globally unless ``binary`` is already on PATH.

    Args:
        binary: Executable the package provides.
        package: npm package name.
        version: Exact release required by the runtime profile.

    Returns:
        The shell command.
    """
    return f"{_version_check(binary, version)} || npm install -g {package}@{version}"


def _version_check(binary: str, version: str) -> str:
    """Check the required CLI release without installing packages or invoking a model.

    Args:
        binary: Built-in harness executable name.
        version: Exact release recorded in the deployment lockfile.

    Returns:
        A shell command that fails when the pinned executable is unavailable.
    """
    pattern = "(^| )" + version.replace(".", "[.]") + "( |$)"
    return f"{binary} --version 2>/dev/null | grep -Eq {shlex.quote(pattern)}"


def _base_env(model: str, gateway: GatewayConfig) -> dict[str, str]:
    """Return the environment every harness gets.

    Args:
        model: Target model id as the gateway knows it.
        gateway: Gateway URL and key.

    Returns:
        Environment variables for the sandbox.
    """
    return with_legacy_env(
        {
            ENV_MODEL: model,
            ENV_GATEWAY_URL: gateway.url,
            ENV_API_KEY: gateway.api_key,
            ENV_PROMPT_FILE: PROMPT_FILE,
            ENV_ANSWER_FILE: ANSWER_FILE,
            "CI": "1",
            "NO_COLOR": "1",
        }
    )


def _temp_root(var: str, name: str) -> str:
    """Return shell that makes a fresh locked temp folder for one run and removes the folders of killed runs.

    The folder lives in the box's temp directory, outside the workspace. An
    EXIT trap removes it when the run's shell exits, and the next run's sweep
    removes it when a SIGKILL skipped the trap. The box image does not pin
    python3, so a box without it gets a plain ``mktemp`` folder with the
    same prefix and trap, and no lock or sweep, as before SKY-594.

    Args:
        var: Environment variable that gets the folder's path.
        name: Harness name in the folder prefix ``app-<name>-``.

    Returns:
        The shell command. Chain the run after it with ``&&``.
    """
    trap = f"trap 'rm -rf \"${var}\"' EXIT"
    return (
        "if command -v python3 >/dev/null 2>&1; then "
        f'{var}="$(python3 -I -c {shlex.quote(_TEMP_ROOT_SWEEP)} app-{name}-)" && export {var} && {trap} && '
        f'exec 9>"${var}/owner.tmp" && '
        f'python3 -I -c {shlex.quote(_TEMP_ROOT_LOCK)} "${var}"; '
        f'else {var}="$(mktemp -d "${{TMPDIR:-/tmp}}/app-{name}-XXXXXXXX")" && export {var} && {trap}; fi'
    )
