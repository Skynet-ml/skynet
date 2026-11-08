"""Run pinned upstream agent engines in a managed sandbox."""

from __future__ import annotations

import base64
import io
import json
import logging
import math
import re
import shlex
import sys
import tarfile
import threading
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from importlib.metadata import distribution
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from .... import run_log
from ....billing.host_agent import HOST_AGENT_TOKEN_KEY
from ....billing.model_gateway import raise_gateway_stop
from ....billing.pricing import model_token_costs
from ....billing.runtime import UsagePendingError
from ....billing.usage_tags import CALLER_PROPOSER, KIND_MUTATION, USAGE_TAGS_HEADER, encode_tags, usage_scope
from ....config import Settings, app_env, settings, with_legacy_env
from ....exceptions import ServiceError
from ....models.blackbox import BLACKBOX_HARNESS_CODEX, BLACKBOX_HARNESS_PI, BlackboxProposer, BlackboxTarget
from ..budget_stop import BudgetReached
from . import harness_bridge, native_runner, repo_tree, sandbox_log
from .agent_eval import gateway_from_settings
from .feedback import emit_candidate, emit_case_scored, emit_scorer_feedback
from .harness import ENV_MODEL, GatewayConfig, build_launch, launch_payload, pi_editor_launch, pinned_harness_check
from .protocol import BudgetExhaustedError, EngineContext, EvalServer, Result, Task
from .runner import side_info_json_default
from .sandbox import (
    CommandResult,
    SandboxRuntime,
    SandboxSession,
    SandboxSpec,
    check_codex_requirements,
    current_sandbox_runtime,
    sandbox_runtime_from_settings,
    sandbox_unavailable_reason,
    unique_sandbox_name,
    write_codex_requirements,
)
from .upstream import (
    AUTORESEARCH_SOURCE,
    AUTORESEARCH_VERSION,
    AUTOSADDLER_REVISION,
    AUTOSADDLER_SOURCE,
    GEPA_REVISION,
    META_HARNESS_REVISION,
    META_HARNESS_SOURCE,
    SHINKA_SOURCE,
    SHINKA_VERSION,
)
from .upstream import GEPA_SOURCE as GEPA_PACKAGE_SOURCE

GEPA_SOURCE = "0632cdb5dcc052e690eab439e1b4a7e3e9cfe407"
# The guest must already carry the parent's exact Python patch version (checkpoint_compat identity),
# so the native floor only restates pyproject's requires-python. A stricter floor here contradicts
# any image built to match a host below it and fails every readiness check on that host.
PYTHON_FLOOR = (3, 11)
_RUNNER_FILE = "native_runner.py"
_INPUT_FILE = "native_input.json"
_RESULT_FILE = "native_result.json"
_ARTIFACT_FILE = "native_artifacts.tar.gz.b64"
_INSTALL_ALLOWANCE = 600.0
# Sent where the network edge injects the real gateway key; nothing reads its value.
_INJECTED_KEY_PLACEHOLDER = "app-managed"
_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
logger = logging.getLogger(__name__)

_RPC_PREFIX = "APP_NATIVE_RPC "
_PROGRESS_PREFIX = "APP_NATIVE_PROGRESS "
_LOG_PREFIX = "APP_NATIVE_LOG "
_UUID = re.compile(r"^[0-9a-f]{32}$")
# The only task fields a native box may receive; Task.context, valset and the
# test split stay with the parent so no proposer can read the metric.
NATIVE_TASK_FIELDS = frozenset({"name", "seed_candidate", "objective", "background", "train_set"})
# Error side info is cut to one line of this size before a proposer sees it.
_ERROR_CHARS = 500
NATIVE_ENGINES = frozenset(
    {"meta_harness", "autoresearch", "autosaddler", "shinka_evolve", "gepa_repo", "best_of_n_repo"}
)
# ``gepa_repo`` and ``best_of_n_repo`` exist only for repositories; the others take either kind of task.
REPO_ONLY_NATIVE_ENGINES = frozenset({"gepa_repo", "best_of_n_repo"})
_UPSTREAMS = {
    "meta_harness": (META_HARNESS_SOURCE, META_HARNESS_REVISION),
    "autoresearch": (AUTORESEARCH_SOURCE, AUTORESEARCH_VERSION),
    "gepa_repo": (GEPA_PACKAGE_SOURCE, GEPA_REVISION),
    "best_of_n_repo": (GEPA_PACKAGE_SOURCE, GEPA_REVISION),
    "autosaddler": (AUTOSADDLER_SOURCE, AUTOSADDLER_REVISION),
    "shinka_evolve": (SHINKA_SOURCE, SHINKA_VERSION),
}
# Engines that run in their own pinned interpreter instead of the vendored GEPA
# runner, and take a multi-part starting point.
_OWN_VENV_ENGINES = frozenset({"autosaddler", "shinka_evolve"})
_AUTOSADDLER_RUNNER_FILE = "autosaddler_runner.py"
_SHINKA_RUNNER_FILE = "shinka_runner.py"
_SHINKA_REQUIREMENTS_FILE = "shinka_requirements.txt"
_SHINKA_BUNDLE_FILE = "shinka_bundle.py"
# Each ShinkaEvolve model route's token reaches the guest in its own variable,
# named by position; the meta-notes model reads its own so its calls are told apart.
SHINKA_KEY_ENV = "APP_SHINKA_KEY_{index}"
SHINKA_META_KEY_ENV = "APP_SHINKA_META_KEY"
# The protected image bakes ShinkaEvolve into its own venv (backend/Dockerfile),
# because its pinned closure conflicts with the backend's own dependencies.
SHINKA_IMAGE_PYTHON = "/opt/shinka/venv/bin/python"
# Duplicate rejection's judge and embeddings read their own variables too.
SHINKA_NOVELTY_KEY_ENV = "APP_SHINKA_NOVELTY_KEY"
SHINKA_EMBED_KEY_ENV = "APP_SHINKA_EMBED_KEY"
_BRIDGE_FILE = "harness_bridge.py"
_ENGINES_FILE = "native_engines.py"
_REPO_TREE_FILE = "repo_tree.py"
_REPO_CHUNK_DIR = "repo-tree"
_PROMPTS_DIR = "upstream_prompts"
_AUTOSADDLER_PLUGIN_DIR = "autosaddler_plugin"
# Upstream AutoSaddler v2 declares Python 3.12+ (its usage dataclasses rely on
# 3.12 default semantics), so its guest carries its own interpreter and a
# fully pinned, dependency-free install of exactly what the v2 core imports.
AUTOSADDLER_PYTHON_FLOOR = (3, 12)
_AUTOSADDLER_PYTHON = "3.12.12"
_AUTOSADDLER_PINS = (
    f"autosaddler @ https://github.com/microsoft/AutoSaddler/archive/{AUTOSADDLER_REVISION}.tar.gz",
    "annotated-types==0.8.0",
    "anyio==4.15.1",
    "attrs==26.1.0",
    "cffi==2.1.1",
    "click==8.5.0",
    "cryptography==50.0.1",
    "h11==0.16.0",
    "httpcore2==2.13.0",
    "httpx2==2.13.0",
    "idna==3.19",
    "jsonschema==4.26.0",
    "jsonschema-specifications==2025.9.1",
    "mcp==2.2.0",
    "mcp-types==2.2.0",
    "opentelemetry-api==1.44.0",
    "pycparser==3.0",
    "pydantic==2.13.5",
    "pydantic_core==2.46.5",
    "PyJWT==2.14.0",
    "python-multipart==0.0.32",
    "PyYAML==6.0.3",
    "referencing==0.37.0",
    "rpds-py==2026.6.3",
    "sniffio==1.3.1",
    "sse-starlette==3.4.11",
    "starlette==1.6.0",
    "truststore==0.10.4",
    "typing_extensions==4.16.0",
    "typing-inspection==0.4.4",
    "uvicorn==0.53.0",
)


@dataclass(frozen=True)
class NativeOptions:
    """Bind an upstream proposer to the managed runtime and model gateway."""

    runtime: Literal["vercel", "docker"]
    model: str
    gateway: GatewayConfig = field(repr=False)
    max_token_cost: float | None
    proposer: BlackboxProposer = field(default_factory=BlackboxProposer)
    budget_route: dict[str, str] | None = field(default=None, repr=False)
    sandbox_runtime: SandboxRuntime | None = None
    # A repository run's packed tree: ``chunks`` (files on this machine),
    # ``editable_paths``, ``readonly_paths``, ``constrained`` (a
    # constraints file governs the run) and ``proposer_hosts`` (what the
    # proposer box may reach besides the relay).
    repo: dict[str, Any] | None = None
    # ShinkaEvolve's evolution settings, and its optimization models in bandit
    # order: each ``{"model", "url", "token"}`` names one scoped gateway route.
    shinka: dict[str, Any] | None = None
    shinka_models: tuple[dict[str, str], ...] = field(default=(), repr=False)
    # The embeddings route ShinkaEvolve's duplicate rejection uses, when it is on.
    shinka_embedding: dict[str, str] | None = field(default=None, repr=False)
    # The optimization model's effort level, which the proposer CLI applies.
    reasoning_effort: str | None = None
    usage_by_model: dict[str, dict[str, int]] = field(default_factory=dict)
    usage_lock: Any = field(default_factory=threading.Lock, repr=False, compare=False)

    @property
    def history(self) -> list[dict[str, Any]]:
        """Expose proposer tokens to the existing language-model usage reader.

        Returns:
            Usage entries with separate cache counters retained for settlement.
        """
        with self.usage_lock:
            return [{"model": model, "usage": dict(usage)} for model, usage in self.usage_by_model.items()]


def _runtime_allowed(runtime: str, settings: Settings) -> bool:
    """Check that ``runtime`` names an execution environment this deployment offers.

    Args:
        runtime: Requested execution environment.
        settings: Deployment configuration.

    Returns:
        True for Vercel, and for local Docker in local mode. Local mode opens
        its boxes in Docker whichever of the two the request names.
    """
    return runtime == "vercel" or (runtime == "docker" and settings.local_mode)


def native_runtime_unavailable_reason(runtime: str, settings: Settings) -> str | None:
    """Explain whether the selected native execution environment can launch.

    Args:
        runtime: Requested execution environment.
        settings: Deployment gateway and managed-sandbox configuration.

    Returns:
        An actionable unavailability reason, or ``None`` when checks pass.
    """
    if not _runtime_allowed(runtime, settings):
        return "Production optimizations run in the managed cloud sandbox."
    current = current_sandbox_runtime()
    if current is not None:
        return None
    # Local mode routes model calls through the run's own parent gateway
    # (the user's CLI subscriptions), never through a deployment proxy.
    if not settings.local_mode and gateway_from_settings(settings) is None:
        return "Native optimizers require a configured model gateway."
    return sandbox_unavailable_reason(settings)


def _source_archive() -> str:
    """Package the installed, verified upstream source without resolving new dependencies.

    Returns:
        Base64 tar archive containing the installed GEPA Python sources and its MIT license.

    Raises:
        ServiceError: When the worker was not built from the approved commit.
    """
    package = distribution("gepa")
    provenance = json.loads(package.read_text("direct_url.json") or "{}")
    if provenance.get("vcs_info", {}).get("commit_id") != GEPA_SOURCE:
        raise ServiceError(f"Native optimizers require GEPA commit {GEPA_SOURCE}.")
    root = Path(package.locate_file("gepa"))
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for source in sorted(root.rglob("*.py")):
            if source.is_symlink():
                raise ServiceError("The pinned GEPA source contains a symbolic link.")
            archive.add(source, arcname=str(Path("gepa") / source.relative_to(root)), recursive=False)
        license_file = next((f for f in package.files or () if f.name == "LICENSE"), None)
        if license_file is None:
            raise ServiceError("The pinned GEPA install is missing its LICENSE file.")
        archive.add(Path(package.locate_file(license_file)), arcname="gepa/LICENSE", recursive=False)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _runner_files(engine_id: str) -> dict[str, str]:
    """Collect the sandbox-side runner plus the pinned upstream prompts or plugin it drives.

    Args:
        engine_id: Native engine being launched.

    Returns:
        Relative file paths mapped to their text.
    """
    if engine_id == "shinka_evolve":
        runner = Path(native_runner.__file__).with_name(_SHINKA_RUNNER_FILE)
        return {
            _SHINKA_RUNNER_FILE: runner.read_text(encoding="utf-8"),
            _SHINKA_BUNDLE_FILE: runner.with_name(_SHINKA_BUNDLE_FILE).read_text(encoding="utf-8"),
            _BRIDGE_FILE: Path(harness_bridge.__file__).read_text(encoding="utf-8"),
            _SHINKA_REQUIREMENTS_FILE: runner.with_name(_SHINKA_REQUIREMENTS_FILE).read_text(encoding="utf-8"),
            _REPO_TREE_FILE: Path(repo_tree.__file__).read_text(encoding="utf-8"),
        }
    bridge = {_BRIDGE_FILE: Path(harness_bridge.__file__).read_text(encoding="utf-8")}
    if engine_id != "autosaddler":
        engines = Path(native_runner.__file__).with_name(_ENGINES_FILE)
        files = {
            **bridge,
            _RUNNER_FILE: Path(native_runner.__file__).read_text(encoding="utf-8"),
            _ENGINES_FILE: engines.read_text(encoding="utf-8"),
            _REPO_TREE_FILE: Path(repo_tree.__file__).read_text(encoding="utf-8"),
        }
        prompts_root = engines.with_name(_PROMPTS_DIR)
        for asset in sorted(path for path in prompts_root.rglob("*") if path.is_file()):
            files[f"{_PROMPTS_DIR}/{asset.relative_to(prompts_root).as_posix()}"] = asset.read_text(encoding="utf-8")
        return files
    runner = Path(native_runner.__file__).with_name(_AUTOSADDLER_RUNNER_FILE)
    plugin_root = runner.with_name(_AUTOSADDLER_PLUGIN_DIR)
    files = {
        **bridge,
        _AUTOSADDLER_RUNNER_FILE: runner.read_text(encoding="utf-8"),
        _REPO_TREE_FILE: Path(repo_tree.__file__).read_text(encoding="utf-8"),
    }
    for asset in sorted([*plugin_root.rglob("*.md"), plugin_root / "LICENSE"]):
        files[f"{_AUTOSADDLER_PLUGIN_DIR}/{asset.relative_to(plugin_root).as_posix()}"] = asset.read_text(
            encoding="utf-8"
        )
    return files


def _harness_setup(harness: str, install_command: str | None, *, protected: bool) -> tuple[str, str]:
    """Return the install step and the version assertion for the proposer harness.

    Args:
        harness: Proposer harness identifier.
        install_command: Launch install command for a built-in or custom harness.
        protected: Whether the offline image must already carry the harness.

    Returns:
        ``(install, check)`` shell fragments, each ending in ``"; "`` or empty.
    """
    check = pinned_harness_check(harness)
    install = f"{install_command}; " if install_command and not protected else ""
    return install, (f"{check}; " if check else "")


def _bootstrap_command(
    runtime: str,
    *,
    protected: bool = False,
    engine_id: str = "meta_harness",
    harness: str = BLACKBOX_HARNESS_CODEX,
    install_command: str | None = None,
    shinka_agent: bool = False,
) -> str:
    """Build installation and preflight commands with immutable package versions.

    Args:
        runtime: Selected managed execution environment.
        protected: Require dependencies already present in the immutable offline image.
        engine_id: Native engine whose interpreter and packages are prepared.
        harness: Proposer harness the engine drives.
        install_command: Install step of the proposer harness.
        shinka_agent: ShinkaEvolve writes versions with Pi (its agent editor),
            so Pi and Node are prepared as for a Pi proposer.

    Returns:
        Shell command that prepares the isolated source and runtime.
    """
    own_venv = engine_id in _OWN_VENV_ENGINES
    shinka = engine_id == "shinka_evolve"
    # ShinkaEvolve calls its models directly unless its agent editor runs Pi.
    harnessless = shinka and not shinka_agent
    harness_install, harness_check = (
        ("", "") if harnessless else _harness_setup(harness, install_command, protected=protected)
    )
    floor = AUTOSADDLER_PYTHON_FLOOR if own_venv else PYTHON_FLOOR
    prepare = (
        "set -eu; mkdir -p .claude .cache .local native_vendor rpc; "
        "test -f .claude.json || printf '{}' > .claude.json; "
    )
    if not protected and own_venv:
        pins = f"-r {_SHINKA_REQUIREMENTS_FILE}" if shinka else " ".join(shlex.quote(pin) for pin in _AUTOSADDLER_PINS)
        prepare += (
            'export HOME="$PWD"; '
            'export PATH="$HOME/.local/bin:$PATH"; '
            "if ! command -v uv > /dev/null 2>&1; then "
            "python3 -m pip install --disable-pip-version-check --no-deps --user uv==0.9.13; fi; "
            f"uv venv --python {_AUTOSADDLER_PYTHON} native_venv; "
            f"uv pip install --python native_venv/bin/python --no-deps {pins}; "
            'printf "%s\\n" "$PWD/native_venv/bin/python" > native-python.txt; '
            + ("" if harnessless else "node -e 'if (+process.versions.node.split(\".\")[0] < 22) process.exit(1)'; ")
            + harness_install
        )
    elif not protected:
        prepare += (
            'export HOME="$PWD"; '
            'export PATH="$HOME/.local/bin:$PATH"; '
            f"if python3 -c 'import sys; sys.exit(sys.version_info < {PYTHON_FLOOR!r})' 2>/dev/null; then "
            "command -v python3 > native-python.txt; else "
            "python3 -m pip install --disable-pip-version-check --no-deps --user uv==0.9.13; "
            '"$HOME/.local/bin/uv" python install 3.11.9; '
            '"$HOME/.local/bin/uv" python find 3.11.9 > native-python.txt; fi; '
            "node -e 'if (+process.versions.node.split(\".\")[0] < 22) process.exit(1)'; " + harness_install
        )
    elif shinka:
        prepare += (
            f"if [ -x {SHINKA_IMAGE_PYTHON} ]; then printf '%s\\n' {SHINKA_IMAGE_PYTHON} > native-python.txt; "
            "else command -v python3 > native-python.txt; fi; "
        )
    else:
        prepare += "command -v python3 > native-python.txt; "
    extract = (
        "import base64,io,pathlib,tarfile; "
        "data=base64.b64decode(pathlib.Path('native_source.tar.gz.b64').read_text()); "
        "tarfile.open(fileobj=io.BytesIO(data),mode='r:gz').extractall('native_vendor',filter='data')"
    )
    if not own_venv:
        prepare += f'"$(cat native-python.txt)" -c {shlex.quote(extract)}; '
    prepare += (
        harness_check
        + '"$(cat native-python.txt)" -c '
        + shlex.quote(
            f"import sys; assert sys.version_info >= {floor!r}, "
            f"'Native optimizers need Python {'.'.join(map(str, floor))} or newer'"
        )
    )
    if engine_id == "autosaddler":
        prepare += '; "$(cat native-python.txt)" -c ' + shlex.quote("import autosaddler.v2.core.engine")
    if shinka:
        prepare += '; "$(cat native-python.txt)" -c ' + shlex.quote("import shinka.core.async_runner")
    if protected and not harnessless:
        prepare += "; node -e 'if (+process.versions.node.split(\".\")[0] < 22) process.exit(1)'"
    return prepare


def _failure_detail(result: CommandResult, timeout_seconds: float) -> str:
    """Summarize a failed readiness command so the setup check names what broke.

    Args:
        result: Completed command whose output would otherwise be discarded.
        timeout_seconds: Bound the command ran under.

    Returns:
        One sentence naming the timeout or the command's exit status and last output line.
    """
    if result.timed_out:
        return f"The check timed out after {timeout_seconds:.0f}s."
    lines = [line.strip() for line in f"{result.stdout}\n{result.stderr}".splitlines() if line.strip()]
    if not lines:
        return f"The check exited with status {result.exit_code} and no output."
    return f"The check exited with status {result.exit_code}: {lines[-1][:240]}"


def _selected_runtime(options: NativeOptions) -> SandboxRuntime:
    """Bind the selected proposer transport without falling back to an unisolated process.

    Args:
        options: Fixed runtime selection and scoped model route.

    Returns:
        The explicit runtime adapter or the matching deployment transport.
    """
    if options.sandbox_runtime is not None:
        return options.sandbox_runtime
    runtime = sandbox_runtime_from_settings(settings)
    if runtime is None:
        raise ServiceError(sandbox_unavailable_reason(settings) or "The sandbox runtime is unavailable.")
    return runtime


def check_native_runtime(options: NativeOptions) -> dict[str, Any]:
    """Verify native dependencies inside the selected protected runtime without proposing a candidate.

    Args:
        options: Actual runtime selection with a scoped parent gateway capability.

    Returns:
        Confirmed immutable source, engine imports and runtime selection.

    Raises:
        ServiceError: When the managed runtime or its pinned dependencies cannot launch.
        UsagePendingError: When the readiness sandbox's usage still requires reconciliation.
    """
    if options.budget_route is None or not _runtime_allowed(options.runtime, settings):
        raise ServiceError("Native readiness requires the protected cloud sandbox.")
    source = _source_archive()
    runtime = _selected_runtime(options)
    lifetime = min(180.0, settings.vercel_sandbox_max_lifetime_seconds)
    session = runtime.open(
        SandboxSpec(
            lifetime_seconds=lifetime,
            network_disabled=True,
            name=unique_sandbox_name("app-native-readiness"),
            env={"PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"},
        )
    )
    started = time.monotonic()
    try:
        session.write_files(
            {
                **_runner_files("meta_harness"),
                **_runner_files("autosaddler"),
                **_runner_files("shinka_evolve"),
                "native_source.tar.gz.b64": source,
            }
        )
        install_timeout = min(60, lifetime - 1)
        installed = session.run(
            _bootstrap_command(options.runtime, protected=True, harness=options.proposer.harness),
            timeout_seconds=install_timeout,
        )
        if not installed.ok or installed.timed_out:
            raise ServiceError(
                "The selected native runtime lacks its required pinned offline dependencies. "
                + _failure_detail(installed, install_timeout)
            )
        probe = (
            "import importlib.util,json,os,subprocess,sys; "
            "import native_runner, autosaddler_runner, shinka_runner, native_engines; "
            "native_engines.check_assets(); "
            f"autosaddler=sys.version_info >= {AUTOSADDLER_PYTHON_FLOOR!r} "
            "and importlib.util.find_spec('autosaddler') is not None; "
            f"shinka_python={SHINKA_IMAGE_PYTHON!r} if os.access({SHINKA_IMAGE_PYTHON!r},os.X_OK) else sys.executable; "
            "shinka=subprocess.run([shinka_python,'-c',"
            + repr(
                "import importlib.util,sys; "
                f"sys.exit(0 if sys.version_info >= {AUTOSADDLER_PYTHON_FLOOR!r} "
                "and importlib.util.find_spec('shinka') else 1)"
            )
            + "],capture_output=True,timeout=20).returncode == 0; "
            "print(json.dumps({'ready':True,'autosaddler':autosaddler,'shinka':shinka}))"
        )
        remaining = lifetime - (time.monotonic() - started) - 1
        if remaining <= 0:
            raise ServiceError("The native readiness runtime expired during dependency checks.")
        probe_timeout = min(60, remaining)
        checked = session.run(
            'export HOME="$PWD"; export PYTHONPATH="$PWD/native_vendor"; '
            f'"$(cat native-python.txt)" -c {shlex.quote(probe)}',
            timeout_seconds=probe_timeout,
            env={"CI": "1"},
        )
        ready = next(
            (json.loads(line) for line in checked.stdout.splitlines() if line.strip().startswith('{"ready": true')),
            None,
        )
        if not checked.ok or checked.timed_out or ready is None:
            raise ServiceError(
                "The selected native runtime cannot launch the pinned upstream engine dependencies. "
                + _failure_detail(checked, probe_timeout)
            )
        return {
            "runtime": options.runtime,
            "gepa_source": GEPA_SOURCE,
            "meta_harness_source": META_HARNESS_REVISION,
            "autoresearch_version": AUTORESEARCH_VERSION,
            "autosaddler_source": AUTOSADDLER_REVISION,
            "autosaddler_ready": bool(ready.get("autosaddler")),
            "shinka_version": SHINKA_VERSION,
            "shinka_ready": bool(ready.get("shinka")),
        }
    finally:
        original_error = sys.exception()
        try:
            session.close()
        except UsagePendingError:
            if original_error is None:
                raise


class _EvaluatorMailbox:
    """Translate child evaluator requests without exposing an inbound service."""

    def __init__(
        self,
        session: SandboxSession,
        server: EvalServer,
        nonce: str,
        progress_callback: Any = None,
        check_budget: Any = None,
        str_mode: bool = True,
        cases: Sequence[Any] = (),
    ) -> None:
        """Bind the transport to one parent evaluator.

        Args:
            session: Running process filesystem and command connection.
            server: Parent budgeted evaluation authority.
            nonce: Per-process framing token.
            progress_callback: Optional job trajectory sink.
            check_budget: Direct cumulative-spend guard, including on reader threads.
            str_mode: Whether the task admits only text candidates rather than named parts.
            cases: The full train cases, by position; the guest names a case only by its id.
        """
        self.str_mode = str_mode
        self.cases = [_plain_case(case) for case in cases]
        self.session = session
        self.server = server
        self.nonce = nonce
        self.progress_callback = progress_callback
        self.check_budget = check_budget
        self.error: BaseException | None = None
        self.owner: str | None = getattr(session, "log_owner", None)
        # Replaced once the run's environment is known, so its secrets are redacted.
        self.scrub = sandbox_log.Scrubber(())
        self._buffer = ""
        self._plain = sandbox_log.LineGroups(
            lambda text: sandbox_log.stream_line(text, owner=self.owner, scrub=self.scrub, label="stdout")
        )
        self._responses: dict[str, str] = {}
        self._lock = threading.Lock()

    def on_output(self, stream: str, text: str) -> None:
        """Process complete request lines while tolerating split output chunks.

        Args:
            stream: Process stdout or stderr.
            text: Newly available output.
        """
        if stream != "stdout":
            return
        with self._lock:
            self._buffer += text
            while "\n" in self._buffer:
                line, self._buffer = self._buffer.split("\n", 1)
                framed = (
                    f"{_RPC_PREFIX}{self.nonce} ",
                    f"{_PROGRESS_PREFIX}{self.nonce} ",
                    f"{_LOG_PREFIX}{self.nonce} ",
                )
                if not line.startswith(framed):
                    if line.strip():
                        self._plain.add(line)
                    continue
                self._plain.flush()
                if line.startswith(_LOG_PREFIX):
                    self._log(line.split(" ", 2)[2])
                    continue
                try:
                    if line.startswith(_RPC_PREFIX):
                        self._respond(json.loads(line.split(" ", 2)[2]))
                    else:
                        self._progress(json.loads(line.split(" ", 2)[2]))
                except Exception as exc:
                    # LocalSubprocessRuntime delivers output on a reader thread;
                    # raising there would abandon the child waiting for its reply.
                    self.error = self.error or exc
            self._plain.flush()

    def _log(self, body: str) -> None:
        """Relay one structured record from the child into the run log.

        A record the host cannot read is logged as plain output: a broken log
        line must never stop the run the way a broken evaluation request does.

        Args:
            body: The JSON after the framing prefix.
        """
        try:
            data = json.loads(body)
        except ValueError:
            sandbox_log.stream_line(body, owner=self.owner, scrub=self.scrub, label="stdout")
            return
        sandbox_log.forward(data, owner=self.owner, scrub=self.scrub)

    def _progress(self, event: dict[str, Any]) -> None:
        """Relay a child checkpoint: one scored case of a sweep, or a completed aggregate.

        An aggregate may name the version it came from (``parent_id``) and its
        depth (``generation``); engines that do not track lineage omit both.

        Args:
            event: Case score or candidate aggregate reported by the child.
        """
        score = event.get("score")
        candidate_id = event.get("candidate_id")
        if not _finite(score) or isinstance(candidate_id, bool) or not isinstance(candidate_id, int):
            return
        if event.get("event") == "case_scored":
            total = event.get("total")
            example_id = str(event.get("example_id", "?"))
            logger.info(
                "Candidate %s scored %.4f on case %s",
                candidate_id,
                float(score),
                example_id,
                extra=run_log.event_extra(
                    source="engine",
                    event="case.scored",
                    fields={"score": float(score), "total": total},
                    candidate=candidate_id,
                    case=example_id,
                ),
            )
            if isinstance(total, int) and not isinstance(total, bool):
                emit_case_scored(
                    self.progress_callback,
                    trial=candidate_id,
                    example_id=str(event.get("example_id", "?")),
                    score=float(score),
                    total=total,
                )
            return
        per_example = event.get("per_example")
        parent_id = event.get("parent_id")
        generation = event.get("generation")
        # The child fills a crashed case with its version's mean (SKY-367).
        # Final selection drops that version (SKY-350), so it shows no score.
        shown = None if self.server.metric_error(event.get("candidate")) is not None else float(score)
        fields: dict[str, Any] = {"score": shown, "total_evals": event.get("total_evals")}
        tool_calls = event.get("tool_calls")
        tool_note = ""
        if isinstance(tool_calls, int) and not isinstance(tool_calls, bool):
            cap_reached = event.get("tool_cap_reached") is True
            fields.update(tool_calls=tool_calls, tool_cap_reached=cap_reached)
            tool_note = f"; the agent used {tool_calls} tool call(s)" + (", hitting the cap" if cap_reached else "")
        logger.info(
            "Candidate %s scored %s after %s evaluations%s",
            candidate_id,
            "no score (a case crashed)" if shown is None else f"{shown:.4f}",
            event.get("total_evals"),
            tool_note,
            extra=run_log.event_extra(
                source="engine",
                event="candidate.scored",
                fields=fields,
                candidate=candidate_id,
            ),
        )
        emit_candidate(
            self.progress_callback,
            candidate_id=str(candidate_id),
            parent_id=_label(parent_id),
            generation=generation if isinstance(generation, int) and not isinstance(generation, bool) else 0,
            score=shown,
            per_example=[]
            if shown is None
            else [
                (str(example_id), float(value))
                for example_id, value in (per_example if isinstance(per_example, list) else [])
                if _finite(value)
            ],
            candidate=event["candidate"],
            discovered_at_evals=int(event["total_evals"]),
            iteration=None,
        )

    def _forward_feedback(self, request: dict[str, Any], score: Any, info: Any) -> None:
        """Send the scorer's notes on one evaluation to the candidate tree drawer.

        Native engines have no proposal iterations, so each note carries the
        id of the version it scored instead.

        Args:
            request: The evaluation request, naming the version and case.
            score: The score the scorer returned.
            info: What the scorer returned next to the score.
        """
        candidate_id = request.get("candidate_id")
        if not _finite(score) or not isinstance(info, dict):
            return
        if isinstance(candidate_id, bool) or not isinstance(candidate_id, int):
            return
        case = request.get("case")
        emit_scorer_feedback(
            self.progress_callback,
            example_id="?" if case is None else str(case),
            score=float(score),
            side_info=info,
            candidate_id=str(candidate_id),
        )

    def _respond(self, request: dict[str, Any]) -> None:
        """Evaluate a request once and persist its response.

        Args:
            request: Nonce-framed candidate, example and request identity.

        Raises:
            ServiceError: When a child sends an invalid request identity.
        """
        request_id = request.get("id", "")
        if not isinstance(request_id, str) or not _UUID.fullmatch(request_id):
            raise ServiceError("Native evaluator request has an invalid identity.")
        if request_id not in self._responses:
            if self.error is not None:
                response: dict[str, Any] = {"error": "Evaluation already stopped."}
            else:
                try:
                    candidate = request["candidate"]
                    if not _candidate_shape_ok(candidate, self.str_mode):
                        raise ServiceError("Native evaluator request carries an invalid candidate shape.")
                    if self.check_budget is not None:
                        self.check_budget()
                    with sandbox_log.event_scope(
                        candidate=_label(request.get("candidate_id")), case=_label(request.get("case"))
                    ):
                        score, info = self.server.evaluate(candidate, self._full_case(request.get("example")))
                    if self.check_budget is not None:
                        self.check_budget()
                    self._forward_feedback(request, score, info)
                    response = {"score": score, "info": sanitize_side_info(info)}
                except (Exception, BudgetReached) as exc:
                    self.error = exc
                    response = {"error": "The parent evaluator stopped this run."}
                    if isinstance(exc, BudgetReached):
                        response["stop_reason"] = "budget_reached"
            try:
                self._responses[request_id] = json.dumps(response, default=side_info_json_default, allow_nan=False)
            except (TypeError, ValueError) as exc:
                self.error = self.error or exc
                self._responses[request_id] = json.dumps({"error": "The parent evaluator returned invalid feedback."})
        self.session.write_files({f"rpc/{request_id}.json": self._responses[request_id]})

    def _full_case(self, example: Any) -> Any:
        """Restore the full train case that a guest example names.

        Args:
            example: The guest's example: ``None``, or a dict with the case's position as ``id``.

        Returns:
            ``None`` for no example, else the full case with ``expected``.

        Raises:
            ServiceError: When the example names no known case.
        """
        if example is None:
            return None
        index = example.get("id") if isinstance(example, dict) else None
        if type(index) is not int or not 0 <= index < len(self.cases):
            raise ServiceError("Native evaluator request names an unknown case.")
        return self.cases[index]


def _plain_case(case: Any) -> Any:
    """Turn one train case into plain JSON data for the native box.

    Args:
        case: A task case: a dict, or a model such as ``UnifiedCase``.

    Returns:
        The case as JSON-plain data.
    """
    dump = getattr(case, "model_dump", None)
    return dump(mode="json") if callable(dump) else case


def _guest_case(index: int, case: Any) -> dict[str, Any]:
    """Build the view of one train case that a guest may hold.

    The guest runs proposer and candidate code, so it gets only the case's
    position and its input. Only the parent's metric side gets ``expected``
    (E42, E61, E62). The input is the one ``case_document`` gives the parent's
    candidate: a dict with no ``input`` key is its own input without its
    ``expected`` key, and an empty ``input`` such as ``None`` is ``{}``.

    Args:
        index: The case's position in the task's cases.
        case: A task case: a dict, or a model such as ``UnifiedCase``.

    Returns:
        ``{"id": index, "input": ...}``, or ``{"id": index}`` for a case that is not a dict.
    """
    plain = _plain_case(case)
    if not isinstance(plain, dict):
        return {"id": index}
    if "input" in plain:
        return {"id": index, "input": plain["input"] or {}}
    return {"id": index, "input": {key: value for key, value in plain.items() if key != "expected"}}


def native_task_payload(engine_id: str, task: Task) -> dict[str, Any]:
    """Build the task document a native box receives, from the train cases only.

    Args:
        engine_id: The native engine, used as the task name.
        task: The engine's task.

    Returns:
        Exactly the fields in ``NATIVE_TASK_FIELDS``.

    Raises:
        ServiceError: When the document would hold any other field.
    """
    document = {
        "name": engine_id,
        "seed_candidate": task.seed_candidate,
        "objective": task.objective or "",
        "background": task.background or "",
        # The guest builds an upstream task, whose train and val sets
        # form one pool; the cases go in once, as its train set.
        "train_set": [_guest_case(index, case) for index, case in enumerate(task.cases)] or None,
    }
    if set(document) != NATIVE_TASK_FIELDS:
        raise ServiceError("The native optimizer task carries fields it must not.")
    return document


def assert_metric_absent(serialized: str, task: Task) -> None:
    """Refuse to ship a native document that carries the task's metric source.

    Args:
        serialized: The document as it would be written into the box.
        task: The engine's task, whose private context holds the metric.

    Raises:
        ServiceError: When the metric source appears in the document.
    """
    metric = str((getattr(task, "context", None) or {}).get("metric_code") or "").strip()
    # Documents are JSON, so the source appears with its newlines escaped.
    if metric and (metric in serialized or json.dumps(metric)[1:-1] in serialized):
        raise ServiceError("The native optimizer payload would expose the metric source.")


def sanitize_side_info(info: Any) -> Any:
    """Reduce an error result to one ``Type: message`` line before a proposer sees it.

    Scorer errors can carry tracebacks or source lines from the metric; the
    proposer must learn only that a version failed and why, in one line.

    Args:
        info: Side information the parent scorer returned.

    Returns:
        ``{"error": "<line>"}`` for an error result, else ``info`` unchanged.
    """
    if not isinstance(info, dict) or "error" not in info:
        return info
    lines = [line.strip() for line in str(info.get("error") or "").splitlines() if line.strip()]
    kept = next((line for line in reversed(lines) if not line.startswith(("File ", "Traceback"))), "")
    return {"error": kept[:_ERROR_CHARS] or "Evaluation failed."}


def _label(value: Any) -> str | None:
    """Read a child-sent candidate or case label as short text.

    Args:
        value: The label as the child sent it.

    Returns:
        A number or text label, or ``None`` for anything else.
    """
    if isinstance(value, bool) or not isinstance(value, int | str):
        return None
    return run_log.fit(str(value), run_log.CANDIDATE_CHARS)


def _finite(value: Any) -> bool:
    """Whether a child-reported score is a finite number.

    Args:
        value: Score field of a progress line.

    Returns:
        ``True`` for finite ints and floats, ``False`` for anything else.
    """
    return isinstance(value, float | int) and not isinstance(value, bool) and math.isfinite(value)


def _candidate_shape_ok(candidate: Any, str_mode: bool) -> bool:
    """Check that a child candidate matches the task's text or named-parts shape.

    Args:
        candidate: Candidate value received from the child.
        str_mode: Whether the task admits only text candidates.

    Returns:
        ``True`` when the parent evaluator may score the candidate.
    """
    if str_mode:
        return isinstance(candidate, str)
    return (
        isinstance(candidate, dict)
        and bool(candidate)
        and all(isinstance(name, str) and isinstance(text, str) for name, text in candidate.items())
    )


def _restore_artifacts(session: SandboxSession, destination: Path) -> None:
    """Copy bounded regular-file artifacts out before destroying the runtime.

    Args:
        session: Completed process filesystem.
        destination: Job-owned destination for upstream raw histories.

    Raises:
        ServiceError: When the archive escapes its destination or exceeds its bound.
    """
    encoded = session.read_file(_ARTIFACT_FILE)
    if not encoded:
        return
    destination.mkdir(parents=True, exist_ok=True)
    total = 0
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(encoded)), mode="r:gz") as archive:
        for member in archive:
            target = (destination / member.name).resolve()
            if not target.is_relative_to(destination.resolve()) or not member.isfile():
                raise ServiceError("Native artifact archive contains an unsafe path.")
            total += member.size
            if total > _MAX_ARTIFACT_BYTES:
                raise ServiceError("Native artifacts exceed the 64 MiB transfer limit.")
            source = archive.extractfile(member)
            if source is not None:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read())


def _record_usage(options: NativeOptions, usage: dict[str, Any]) -> None:
    """Accumulate proposer tokens even when a completed child reports failure.

    Args:
        options: Run-scoped usage ledger.
        usage: Per-model token counts recovered from native CLI artifacts.
    """
    with options.usage_lock:
        for model, counts in usage.items():
            if not isinstance(counts, dict):
                continue
            destination = options.usage_by_model.setdefault(model, {})
            for name, value in counts.items():
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    destination[name] = destination.get(name, 0) + value


def _shinka_editor(options: NativeOptions, relay: str | None) -> dict[str, Any]:
    """Describe the Pi launch ShinkaEvolve's agent editor runs on each optimization model.

    Pi talks to the same scoped route the bandit's model would, so its calls
    are billed with the run, and tags them as mutation calls the way upstream's
    own mutation calls are tagged.

    Args:
        options: The run's ShinkaEvolve routes, proposer settings and protection.
        relay: The sandbox-local gateway relay, when the run has one.

    Returns:
        ``{"max_tool_calls", "launches"}``; ``launches`` follows ``shinka_models``
        order, each a serialized launch with its ``model``, ``key_env`` and ``price``.
    """
    headers = {USAGE_TAGS_HEADER: encode_tags({"caller": CALLER_PROPOSER, "kind": KIND_MUTATION})}
    launches = []
    for index, route in enumerate(options.shinka_models):
        launch = pi_editor_launch(
            str(route["model"]),
            GatewayConfig(url=relay or route["url"], api_key=harness_bridge.KEY_TOKEN),
            headers=headers,
            protected=options.budget_route is not None,
            # Only the reflection model, first in ``shinka_models``, may carry an effort.
            reasoning_effort=options.reasoning_effort if index == 0 else None,
        )
        input_price, output_price = model_token_costs(route["model"])
        launches.append(
            {
                **launch_payload(launch),
                "harness": BLACKBOX_HARNESS_PI,
                "model": launch.env[ENV_MODEL],
                "key_env": SHINKA_KEY_ENV.format(index=index),
                "price": {"input": input_price, "output": output_price},
            }
        )
    return {"max_tool_calls": options.proposer.max_tool_calls, "launches": launches}


def _require_codex_requirements(runtime: SandboxRuntime, session: SandboxSession) -> None:
    """Make sure the root-owned Codex requirements file is in place before Codex starts.

    A contained session runs inside an outer box whose parent already wrote the
    file, and it cannot run as root, so it only reads the file back.

    Args:
        runtime: The runtime that opened ``session``.
        session: The proposer's box.

    Raises:
        ServiceError: When the file cannot be written or does not match.
    """
    if getattr(runtime, "contained", False):
        check_codex_requirements(session)
        return
    writer = getattr(session, "write_codex_requirements", None)
    if writer is not None:
        writer()
    else:
        write_codex_requirements(session)


def run_native_engine(engine_id: str, task: Task, server: EvalServer, ctx: EngineContext) -> Result:
    """Execute an unchanged upstream agent engine inside the managed sandbox.

    Args:
        engine_id: ``meta_harness``, ``autoresearch``, ``autosaddler`` or ``shinka_evolve``.
        task: Seed and visible training/validation examples.
        server: Skynet evaluator and shared evaluation budget.
        ctx: Run context containing native execution options.

    Returns:
        Upstream incumbent, aggregate score, usage and artifact provenance.

    Raises:
        ServiceError: When configuration, installation or the native engine fails.
        Exception: The original parent evaluator exception, without retrying it.
    """
    options = ctx.native_options
    if options is None or not _runtime_allowed(options.runtime, settings):
        raise ServiceError("Native optimizers require the managed cloud sandbox.")
    if engine_id not in NATIVE_ENGINES:
        raise ServiceError("Unsupported native optimizer.")
    own_venv = engine_id in _OWN_VENV_ENGINES
    shinka = engine_id == "shinka_evolve"
    if not own_venv and not task.str_mode:
        raise ServiceError("Native agent engines require a single text candidate.")
    if options.repo is None and engine_id in REPO_ONLY_NATIVE_ENGINES:
        raise ServiceError("This agent proposer needs a repository checkout.")
    if shinka and not options.shinka_models:
        raise ServiceError("ShinkaEvolve needs at least one optimization model route.")
    if own_venv and options.repo is None and task.seed_candidate is None:
        # A blank run patches an empty starting text rather than refusing.
        task = replace(task, seed_candidate="")
    if server.remaining <= 0:
        raise BudgetExhaustedError("The native optimizer evaluation budget is exhausted.")
    if options.max_token_cost is not None and (
        not math.isfinite(options.max_token_cost) or options.max_token_cost <= 0
    ):
        raise ServiceError("Native optimizers require a positive proposer cost limit.")
    if not options.gateway.url or not options.gateway.api_key:
        raise ServiceError("Native optimizers require a configured model gateway.")
    runtime = _selected_runtime(options)
    nonce = uuid.uuid4().hex
    artifacts_dir = Path(ctx.run_dir) / f"{engine_id}-native-{nonce[:8]}"
    gateway_host = urlsplit(options.gateway.url).hostname
    # ShinkaEvolve picks among several models, each on its own route token, so
    # one header added at the network edge cannot authorize its calls; its
    # tokens travel in the environment instead and are scrubbed from its logs.
    headers = (
        {gateway_host: {"Authorization": f"Bearer {options.gateway.api_key}"}}
        if runtime.injects_headers and gateway_host and not shinka
        else {}
    )
    source = None if own_venv else _source_archive()
    upstream_source, upstream_revision = _UPSTREAMS[engine_id]
    runner_file = _SHINKA_RUNNER_FILE if shinka else _AUTOSADDLER_RUNNER_FILE if own_venv else _RUNNER_FILE
    relay = app_env("APP_BUDGET_RELAY_URL")
    proposer = options.proposer
    editor = _shinka_editor(options, relay) if shinka and (options.shinka or {}).get("editor") == "agent" else None
    launch = build_launch(
        BlackboxTarget(
            kind="agent",
            harness=proposer.harness,
            model=options.model,
            install_command=proposer.install_command,
            run_command=proposer.run_command,
        ),
        GatewayConfig(url=options.gateway.url, api_key=harness_bridge.KEY_TOKEN),
        protected=options.budget_route is not None,
        host_cli=proposer.host_cli,
        host_agent_token=(options.budget_route or {}).get(HOST_AGENT_TOKEN_KEY),
        reasoning_effort=options.reasoning_effort,
    )
    input_price, output_price = model_token_costs(options.model)
    # Runs carry no wall-clock cap of their own: the budget and evaluation
    # limits end them, and only the sandbox's platform lifetime bounds the box.
    lifetime = settings.vercel_sandbox_max_lifetime_seconds
    network: dict[str, Any] = {}
    if options.repo is not None:
        # A repository's proposer reaches only its registries and listed hosts
        # (E57). A box that calls the gateway directly needs its host too; a
        # protected box reaches the model through the parent's mailbox.
        hosts = set(options.repo.get("proposer_hosts") or ())
        if runtime.injects_headers and gateway_host:
            hosts.add(gateway_host)
        network = {"allowed_hosts": tuple(sorted(hosts)), "network_disabled": not hosts}
    spec = SandboxSpec(
        lifetime_seconds=lifetime,
        env={"PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"},
        name=unique_sandbox_name(f"app-{engine_id}"),
        inject_headers=headers,
        **network,
    )
    session = runtime.open(spec)
    final_result: Result | None = None
    opened = time.monotonic()
    mailbox = _EvaluatorMailbox(
        session,
        server,
        nonce,
        getattr(ctx, "progress_callback", None),
        getattr(ctx, "check_budget", None),
        str_mode=task.str_mode,
        cases=task.cases,
    )
    try:
        payload = {
            "nonce": nonce,
            "source": upstream_revision,
            "engine_id": engine_id,
            "model": options.model,
            "sandbox": False,
            "max_token_cost": options.max_token_cost,
            "max_evals": server.remaining,
            "max_concurrency": ctx.concurrency,
            "max_iterations": ctx.max_iterations,
            "stop_at_score": ctx.stop_at_score,
            "timeout_seconds": lifetime,
            "proposer": {
                **launch_payload(launch),
                "harness": proposer.harness,
                "model": options.model,
                "price": {"input": input_price, "output": output_price},
                "max_candidates_per_iter": proposer.max_candidates_per_iter,
                "ralph": proposer.ralph,
                "max_no_eval_seconds": proposer.max_no_eval_seconds,
                "max_tool_calls": proposer.max_tool_calls,
            },
            "task": native_task_payload(engine_id, task),
        }
        if shinka:
            payload["shinka"] = dict(options.shinka or {})
            payload["shinka_models"] = [
                {
                    "model": route["model"],
                    "url": relay or route["url"],
                    "key_env": SHINKA_KEY_ENV.format(index=index),
                }
                for index, route in enumerate(options.shinka_models)
            ]
            payload["shinka_meta_key_env"] = SHINKA_META_KEY_ENV
            if options.shinka_embedding is not None:
                payload["shinka_embedding"] = {
                    "model": options.shinka_embedding["model"],
                    "url": relay or options.shinka_embedding["url"],
                    "key_env": SHINKA_EMBED_KEY_ENV,
                }
                payload["shinka_novelty_key_env"] = SHINKA_NOVELTY_KEY_ENV
            if editor is not None:
                payload["shinka_editor"] = editor
        if options.repo is not None:
            chunks = []
            # One upload per chunk: each is close to the per-request size cap.
            for index, chunk in enumerate(options.repo["chunks"]):
                chunks.append(f"{_REPO_CHUNK_DIR}/tree.{index:04d}.b64")
                session.write_files({chunks[-1]: Path(chunk).read_text(encoding="ascii")})
            payload["repo"] = {
                "chunks": chunks,
                "editable_paths": list(options.repo["editable_paths"]),
                "readonly_paths": list(options.repo["readonly_paths"]),
                "constrained": bool(options.repo.get("constrained")),
            }
        document_text = json.dumps(payload, default=side_info_json_default)
        assert_metric_absent(document_text, task)
        session.write_files(
            {
                **_runner_files(engine_id),
                _INPUT_FILE: document_text,
                **({} if source is None else {"native_source.tar.gz.b64": source}),
            }
        )
        installed = session.run(
            _bootstrap_command(
                options.runtime,
                protected=options.budget_route is not None,
                engine_id=engine_id,
                harness=BLACKBOX_HARNESS_PI if editor else proposer.harness,
                install_command=editor["launches"][0]["install_command"] if editor else launch.install_command,
                shinka_agent=editor is not None,
            ),
            timeout_seconds=min(_INSTALL_ALLOWANCE, lifetime - 1.0),
        )
        if not installed.ok or installed.timed_out:
            detail = (installed.stderr or installed.stdout)[-2000:]
            raise ServiceError(f"Native optimizer runtime setup failed: {detail}")
        # A host_cli proposer runs the host-agent shim, not Codex in this box, so it
        # needs no requirements file (the parent's _starts_codex skips it too).
        if not editor and proposer.harness == BLACKBOX_HARNESS_CODEX and proposer.host_cli is None:
            _require_codex_requirements(runtime, session)
        env = {
            "ANTHROPIC_BASE_URL": (relay or options.gateway.url).removesuffix("/v1"),
            "ANTHROPIC_AUTH_TOKEN": _INJECTED_KEY_PLACEHOLDER if headers else options.gateway.api_key,
            harness_bridge.KEY_ENV: _INJECTED_KEY_PLACEHOLDER if headers else options.gateway.api_key,
            "DISABLE_AUTOUPDATER": "1",
            "CI": "1",
            "NO_COLOR": "1",
            **(with_legacy_env({"APP_BUDGET_RELAY_URL": relay}) if relay else {}),
        }
        if shinka:
            env.update(
                {
                    SHINKA_KEY_ENV.format(index=index): route["token"]
                    for index, route in enumerate(options.shinka_models)
                }
            )
            env[SHINKA_META_KEY_ENV] = options.shinka_models[0]["token"]
            if options.shinka_embedding is not None:
                env[SHINKA_NOVELTY_KEY_ENV] = options.shinka_models[0]["token"]
                env[SHINKA_EMBED_KEY_ENV] = options.shinka_embedding["token"]
            # Upstream prices calls from a bundled catalog unless told not to;
            # the gateway, not the guest, prices and bills every call.
            env["SHINKA_PRICING_MODE"] = "offline"
        command = (
            'export HOME="$PWD"; export PATH="$HOME/.local/bin:$PATH"; '
            'export PYTHONPATH="$PWD/native_vendor"; '
            f'exec "$(cat native-python.txt)" {runner_file} {_INPUT_FILE}'
        )
        timeout = lifetime - (time.monotonic() - opened) - 1.0
        if timeout <= 0:
            raise ServiceError("Native optimizer runtime expired while preparing dependencies.")
        payload["timeout_seconds"] = timeout
        document_text = json.dumps(payload, default=side_info_json_default)
        assert_metric_absent(document_text, task)
        session.write_files({_INPUT_FILE: document_text})
        session_env = getattr(session, "_env", None)
        mailbox.scrub = sandbox_log.Scrubber(
            [
                *env.values(),
                options.gateway.api_key,
                *(session_env.values() if isinstance(session_env, dict) else ()),
            ]
        )
        # The proposer CLI shares the optimization route with GEPA reflection in auto mode.
        with usage_scope(caller=CALLER_PROPOSER):
            completed = session.run(command, env=env, timeout_seconds=timeout, on_output=mailbox.on_output)
        text = session.read_file(_RESULT_FILE)
        document = json.loads(text) if text else {}
        usage = document.get("usage_by_model", {})
        _record_usage(options, usage)
        artifact_error: Exception | None = None
        try:
            _restore_artifacts(session, artifacts_dir)
        except Exception as exc:
            artifact_error = exc
        if mailbox.error is not None:
            if isinstance(mailbox.error, BudgetReached) and document.get("best_score") is not None:
                mailbox.error.result = Result(
                    best_candidate=document["best_candidate"],
                    best_score=document["best_score"],
                    total_evals=document.get("total_evals", 0),
                    metadata={
                        **document.get("metadata", {}),
                        "upstream_source": upstream_source,
                        "native_artifacts_dir": str(artifacts_dir),
                        "native_usage_by_model": usage,
                    },
                )
                mailbox.error.evidence.update(
                    selection_scope="validation",
                    final_evaluation_completed=False,
                    final_evaluation_reason="budget_reached",
                )
            raise mailbox.error
        if artifact_error is not None:
            raise artifact_error
        if completed.timed_out:
            raise ServiceError(f"Native optimizer exceeded its runtime limit: the sandbox's {lifetime:.0f}s lifetime.")
        if options.budget_route is not None:
            try:
                raise_gateway_stop(options.budget_route)
            except BudgetReached as stop:
                incumbent = (
                    document if document.get("best_score") is not None else document.get("interrupted_incumbent", {})
                )
                if incumbent.get("best_score") is not None:
                    stop.result = Result(
                        best_candidate=incumbent["best_candidate"],
                        best_score=incumbent["best_score"],
                        total_evals=incumbent.get("total_evals", 0),
                        metadata={
                            **incumbent.get("metadata", {}),
                            "native_artifacts_dir": str(artifacts_dir),
                            "native_usage_by_model": usage,
                        },
                    )
                stop.evidence.update(final_evaluation_completed=False, final_evaluation_reason="budget_reached")
                raise
        if not completed.ok or document.get("error") or not document:
            detail = str(document.get("error") or completed.stderr[-2000:] or "No result was produced.")
            raise ServiceError(f"Native optimizer failed: {detail.replace(options.gateway.api_key, '[redacted]')}")
        if not document.get("usage_complete", False):
            raise ServiceError("Native optimizer usage could not be reconciled from its CLI artifacts.")
        if document.get("best_score") is None and (
            task.seed_candidate is None or document.get("best_candidate") != task.seed_candidate
        ):
            raise ServiceError("The upstream optimizer stopped before producing a fully evaluated candidate.")
        metadata = dict(document.get("metadata", {}))
        metadata.update(
            {
                "upstream_source": upstream_source,
                "upstream_revision": upstream_revision,
                "runtime": options.runtime,
                "native_artifacts_dir": str(artifacts_dir),
                "native_usage_by_model": usage,
                "native_usage_complete": document.get("usage_complete", False),
            }
        )
        final_result = Result(
            best_candidate=document["best_candidate"],
            best_score=document.get("best_score"),
            total_evals=document.get("total_evals", 0),
            metadata=metadata,
        )
        return final_result
    finally:
        active_error = sys.exception()
        try:
            session.close()
        except UsagePendingError:
            if final_result is not None:
                final_result.metadata["runtime_usage_pending"] = True
            elif active_error is None:
                raise
