"""Load-test orchestrator: boot stack → run scenarios → emit reports.

Default flow:

    cd backend
    .venv/bin/python -m load_tests.run_all

Knobs (env or flag):
    --skip-stack       reuse an already-running stack (faster reruns)
    --no-down          keep the stack up after the run (debug failures)
    --scenarios=A,B    run a subset, in the listed order

The SKY-727 scenarios (reference_shapes, chaos_gate, swarm_10k) run in
process with a mock model and local or fake boxes. When only they are
selected, the orchestrator starts no stack. The swarm runs only when
named: its rate gate needs about 10,000 agents, and its default 1,000
agents always fail it:

    .venv/bin/python -m load_tests.run_all --scenarios=chaos_gate,swarm_10k --swarm-agents=10000

Reports land in ``load_tests/results/<timestamp>.{json,md}`` and the
console renders one block per scenario as it finishes so a long run does
not feel silent.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from pathlib import Path

# The in-process scenarios import ``core``, and litellm fetches its price map over HTTPS at import unless this
# is set. The Dockerfile sets the same value. It must come before the scenario imports.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

from .lib.metrics import ScenarioResult
from .lib.reporter import print_result, write_json_report, write_markdown_report
from .lib.warmup import warm_validation_cache
from .scenarios import (
    chaos_gate,
    dashboard_read,
    failure_injection,
    full_lifecycle,
    mixed_realistic,
    reference_shapes,
    submission_burst,
    swarm_10k,
)

_DEFAULT_COMPOSE_FILE = str(Path(__file__).resolve().parent / "docker-compose.loadtest.yml")
_DEFAULT_API_URL = "http://127.0.0.1:58000"
_DEFAULT_FRONTEND_URL = "http://127.0.0.1:53001"
_DEFAULT_MOCK_LM_URL = "http://mock-lm:9000/v1"
_DEFAULT_DB_URL = "postgresql://skynet:loadtest@127.0.0.1:55432/skynet_loadtest"

# Same value the compose file injects into every api replica; the harness
# must use it to mint tokens auth.py will accept.
_DEFAULT_BACKEND_SECRET = "loadtest-secret-do-not-use-in-production"

ScenarioFn = Callable[[str, str], Awaitable[ScenarioResult]]

_SCENARIOS: dict[str, ScenarioFn] = {
    "mixed_realistic": mixed_realistic.main,
    "submission_burst": submission_burst.main,
    "full_lifecycle": full_lifecycle.main,
    "dashboard_read": dashboard_read.main,
    "failure_injection": failure_injection.main,
    "reference_shapes": reference_shapes.main,
    "chaos_gate": chaos_gate.main,
    "swarm_10k": swarm_10k.main,
}

# These scenarios need no compose stack: they run the new path in process and spend no money (SKY-727).
_IN_PROCESS_SCENARIOS = frozenset({"reference_shapes", "chaos_gate", "swarm_10k"})
# The swarm runs only when named: below about 3,000 agents it cannot offer its target rate, so its default
# 1,000 agents would fail every default run.
_OPT_IN_SCENARIOS = frozenset({"swarm_10k"})
_DEFAULT_SCENARIOS = [name for name in _SCENARIOS if name not in _OPT_IN_SCENARIOS]

# Each flag sets the environment variable that its scenario reads, so the scenario also runs alone.
_SCENARIO_KNOBS = (
    (
        "--shape-rounds",
        "LOAD_TEST_SHAPE_ROUNDS",
        "model rounds of each shape run, for reference_shapes and for chaos_gate"
        " (default: 2 for reference_shapes, 4 for chaos_gate)",
    ),
    ("--box-image", "LOAD_TEST_BOX_IMAGE", "local Docker image id for the shape boxes (default: local subprocesses)"),
    ("--chaos-runs-per-shape", "LOAD_TEST_CHAOS_RUNS_PER_SHAPE", "chaos gate runs of each shape (default: 3)"),
    ("--chaos-seed", "LOAD_TEST_CHAOS_SEED", "chaos job random seed (default: 727)"),
    ("--swarm-agents", "LOAD_TEST_SWARM_AGENTS", "swarm agents (default: 1000; the capacity target is 10000)"),
    (
        "--swarm-steps-per-agent",
        "LOAD_TEST_SWARM_STEPS_PER_AGENT",
        f"swarm steps of each agent, at least {swarm_10k.MIN_STEPS_PER_AGENT} (default: 10)",
    ),
    (
        "--swarm-shards",
        "LOAD_TEST_SWARM_SHARDS",
        "step-log shards of the swarm's second pass, one OS process each"
        f" (default: one for each {swarm_10k.AGENTS_PER_SHARD} agents, 2 to {swarm_10k.MAX_SHARDS})",
    ),
    ("--swarm-db-url", "LOAD_TEST_SWARM_DB_URL", "throwaway Postgres URL for the swarm (default: SQLite files)"),
)


def _parse_args() -> argparse.Namespace:
    """Parse CLI flags for the orchestrator.

    Returns:
        The parsed :class:`argparse.Namespace` carrying every knob.
    """
    parser = argparse.ArgumentParser(description="Skynet load-test orchestrator")
    parser.add_argument(
        "--api-url",
        default=os.environ.get("LOAD_TEST_API_URL", _DEFAULT_API_URL),
        help="Backend base URL the scenarios drive (default: %(default)s)",
    )
    parser.add_argument(
        "--mock-lm-url",
        default=os.environ.get("LOAD_TEST_MOCK_LM_URL", _DEFAULT_MOCK_LM_URL),
        help="Mock LM base URL embedded in scenario payloads (default: %(default)s)",
    )
    parser.add_argument(
        "--frontend-url",
        default=os.environ.get("LOAD_TEST_FRONTEND_URL", _DEFAULT_FRONTEND_URL),
        help="Production Next.js server driven by mixed journeys (default: %(default)s)",
    )
    parser.add_argument(
        "--scenarios",
        default=",".join(_DEFAULT_SCENARIOS),
        help=(
            f"Comma-separated scenario list from: {', '.join(_SCENARIOS)}"
            f" (default: all but {', '.join(sorted(_OPT_IN_SCENARIOS))}, in this order)"
        ),
    )
    parser.add_argument(
        "--compose-file",
        default=os.environ.get("LOAD_TEST_COMPOSE_FILE", _DEFAULT_COMPOSE_FILE),
        help="Path to the load-test compose file",
    )
    parser.add_argument(
        "--compose-project",
        default=os.environ.get("LOAD_TEST_COMPOSE_PROJECT", "skynet-loadtest"),
        help="Compose project name (default: %(default)s)",
    )
    parser.add_argument(
        "--results-dir",
        default=os.environ.get("LOAD_TEST_RESULTS_DIR", str(Path(__file__).resolve().parent / "results")),
        help="Directory the orchestrator writes JSON + Markdown reports to",
    )
    parser.add_argument(
        "--no-build",
        action="store_true",
        help="Reuse cached images when bringing the stack up",
    )
    parser.add_argument(
        "--skip-stack",
        action="store_true",
        help="Assume the stack is already running; do not start/stop it",
    )
    parser.add_argument(
        "--no-down",
        action="store_true",
        help="Leave the stack running after the report (useful for triage)",
    )
    for flag, env_name, text in _SCENARIO_KNOBS:
        parser.add_argument(flag, dest=env_name, metavar="VALUE", default=None, help=f"{text}; sets {env_name}")
    return parser.parse_args()


def _compose_argv(args: argparse.Namespace, *cmd: str) -> list[str]:
    """Build a ``docker compose`` argv for the configured project + file.

    Args:
        args: Parsed orchestrator args.
        *cmd: Subcommand and flags.

    Returns:
        The argv list ready for :mod:`subprocess`.
    """
    return ["docker", "compose", "-p", args.compose_project, "-f", args.compose_file, *cmd]


def _run(argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` synchronously, streaming stdout/stderr through to the user.

    Args:
        argv: Command to invoke.
        check: When ``True`` raise on non-zero exit.

    Returns:
        The completed :class:`subprocess.CompletedProcess`.
    """
    return subprocess.run(
        argv,
        check=check,
        text=True,
    )


def _bring_up(args: argparse.Namespace) -> None:
    """Boot the docker compose stack and wait until the LB is reachable.

    Args:
        args: Parsed orchestrator args.
    """
    up_args = ["up", "-d"]
    if not args.no_build:
        up_args.append("--build")
    print("[loadtest] starting stack:", " ".join(_compose_argv(args, *up_args)))
    _run(_compose_argv(args, *up_args))


def _wait_for_http(url: str, *, timeout_seconds: float = 240.0) -> None:
    """Block until ``url`` returns 2xx, or raise on timeout.

    Args:
        url: Health URL to poll.
        timeout_seconds: Total wall-clock budget before giving up.

    Raises:
        TimeoutError: When the URL never returns 2xx.
    """
    deadline = time.monotonic() + timeout_seconds
    last_error: str | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                if 200 <= response.status < 300:
                    return
                last_error = f"status={response.status}"
        except (TimeoutError, urllib.error.URLError, ConnectionError, OSError) as exc:
            last_error = repr(exc)
        time.sleep(2.0)
    raise TimeoutError(f"timed out waiting for {url}: {last_error}")


def _bring_down(args: argparse.Namespace) -> None:
    """Tear the stack down and remove the dedicated volume.

    Args:
        args: Parsed orchestrator args.
    """
    print("[loadtest] tearing down stack")
    _run(_compose_argv(args, "down", "-v"), check=False)


def _ensure_env(args: argparse.Namespace) -> None:
    """Set env vars the scenarios + DB inspector require if not already exported.

    Args:
        args: Parsed orchestrator args.
    """
    os.environ.setdefault("BACKEND_AUTH_SECRET", _DEFAULT_BACKEND_SECRET)
    os.environ.setdefault("LOAD_TEST_DB_URL", _DEFAULT_DB_URL)
    os.environ.setdefault("LOAD_TEST_API_URL", args.api_url)
    os.environ.setdefault("LOAD_TEST_FRONTEND_URL", args.frontend_url)
    os.environ.setdefault("LOAD_TEST_MOCK_LM_URL", args.mock_lm_url)
    os.environ.setdefault("LOAD_TEST_COMPOSE_FILE", args.compose_file)
    os.environ.setdefault("LOAD_TEST_COMPOSE_PROJECT", args.compose_project)
    for _, env_name, _ in _SCENARIO_KNOBS:
        value = getattr(args, env_name)
        if value is not None:
            os.environ[env_name] = value


async def _run_scenarios(names: list[str], api_url: str, mock_lm_url: str) -> list[ScenarioResult]:
    """Run each scenario serially and print its result as it finishes.

    Args:
        names: Scenario names to run (must exist in :data:`_SCENARIOS`).
        api_url: Backend base URL forwarded to each scenario.
        mock_lm_url: Mock LM URL forwarded to each scenario.

    Returns:
        The collected :class:`ScenarioResult` objects in run order.
    """
    results: list[ScenarioResult] = []
    for name in names:
        scenario = _SCENARIOS.get(name)
        if scenario is None:
            print(f"[loadtest] WARNING: unknown scenario '{name}', skipping")
            continue
        print(f"\n[loadtest] running scenario: {name}")
        result = await scenario(api_url, mock_lm_url)
        results.append(result)
    return results


def _write_reports(results: list[ScenarioResult], results_dir: Path) -> tuple[Path, Path]:
    """Write the JSON + Markdown reports and return both paths.

    Args:
        results: Scenario results collected during this run.
        results_dir: Directory the reports are written to.

    Returns:
        ``(json_path, markdown_path)`` so the orchestrator can echo them.
    """
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    json_path = write_json_report(results, results_dir / f"{timestamp}.json")
    md_path = write_markdown_report(results, results_dir / f"{timestamp}.md")
    return json_path, md_path


async def _async_main() -> int:
    """Orchestrator entrypoint coroutine.

    Returns:
        Process exit code: ``0`` when every SLO gate passes, otherwise ``1``.
    """
    args = _parse_args()
    _ensure_env(args)
    names = [name.strip() for name in args.scenarios.split(",") if name.strip()]
    needs_stack = any(name not in _IN_PROCESS_SCENARIOS for name in names)

    if needs_stack and not args.skip_stack:
        _bring_up(args)

    try:
        if needs_stack:
            _wait_for_http(f"{args.api_url}/health")
            _wait_for_http(f"{args.frontend_url}/login")
            warm = await warm_validation_cache(args.api_url)
            print(
                f"[loadtest] warmed validation cache: {warm['calls']} calls, "
                f"{warm['failures']} failures, {warm['elapsed_seconds']:.1f}s "
                f"(latencies_ms={[f'{ms:.0f}' for ms in warm['latencies_ms']]})"
            )
        results = await _run_scenarios(names, args.api_url, args.mock_lm_url)
    finally:
        if needs_stack and not args.skip_stack and not args.no_down:
            _bring_down(args)

    if not results:
        print("[loadtest] no scenarios ran; nothing to report")
        return 1

    for result in results:
        print_result(result)

    results_dir = Path(args.results_dir)
    json_path, md_path = _write_reports(results, results_dir)
    print(f"\n[loadtest] wrote {json_path}")
    print(f"[loadtest] wrote {md_path}")
    failed = [result for result in results if result.slo_passed is False]
    if failed:
        names = ", ".join(result.name for result in failed)
        print(f"[loadtest] RELEASE GATE FAILED: {names}")
        return 1
    return 0


def main() -> int:
    """Synchronous entrypoint used by ``python -m load_tests.run_all``.

    Returns:
        The orchestrator exit code.
    """
    try:
        return asyncio.run(_async_main())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
