"""Run harnesses over the benchmark tasks and record graded attempts.

Example::

    python -m bench.run --harness pi codex --trials 2 --concurrency 3 --out results/main

Each attempt gets its own world server on its own port, so attempts never
share state. Harnesses run one after another; the OpenRouter key's billed usage
is read before and after each harness batch, which gives a ground-truth cost
per harness that does not depend on how each harness reports tokens.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from bench.harnesses.base import MODEL, Attempt
from bench.harnesses.cli import CLI_HARNESSES
from bench.harnesses.dspy_react import STABLE, run_dspy
from bench.language import is_hebrew
from bench.prompt import brief, user_message
from bench.task import Run, Task, grade, load_tasks

ROOT = Path(__file__).resolve().parent.parent
HARNESSES = [*CLI_HARNESSES, "dspy-reactv2", "dspy-react", STABLE]
_write_lock = threading.Lock()


def api_key() -> str:
    """Return the OpenRouter key from the environment or the backend's ``.env``."""
    key = os.environ.get("OPENROUTER_API_KEY") or dotenv_values(ROOT.parent.parent / "backend" / ".env").get(
        "OPENROUTER_API_KEY"
    )
    if not key:
        raise SystemExit("OPENROUTER_API_KEY is not set")
    return key


def billed_usage(key: str) -> float | None:
    """Return the key's lifetime billed usage in USD, or ``None`` if unavailable."""
    request = urllib.request.Request("https://openrouter.ai/api/v1/key", headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return float(json.load(response)["data"]["usage"])
    except Exception:
        return None


def settled_usage(key: str, interval: float = 30, max_wait: float = 240) -> float | None:
    """Return billed usage once OpenRouter's lagging counter stops moving.

    Args:
        key: The OpenRouter API key.
        interval: Seconds between two reads.
        max_wait: Give up waiting for a stable value after this many seconds.

    Returns:
        The last usage read, or ``None`` if unavailable.
    """
    time.sleep(interval)
    last = billed_usage(key)
    waited = interval
    while waited < max_wait:
        time.sleep(interval)
        waited += interval
        current = billed_usage(key)
        if current == last:
            break
        last = current
    return last


def free_port() -> int:
    """Return a TCP port that is free right now."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def fetch_state(port: int, timeout: float = 5) -> dict[str, Any]:
    """Fetch the world snapshot from an attempt's server."""
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/__state", timeout=timeout) as response:
        return json.load(response)


def start_server(task_id: str, port: int, log: Path) -> subprocess.Popen:
    """Start the world server for one attempt and wait until it answers.

    Args:
        task_id: The task whose world to serve.
        port: Port to listen on.
        log: File receiving the server's output.

    Returns:
        The running server process.

    Raises:
        RuntimeError: When the server does not come up within 30 seconds.
    """
    proc = subprocess.Popen(
        [sys.executable, "-m", "bench.server", "--task", task_id, "--port", str(port)],
        cwd=ROOT, stdout=log.open("w"), stderr=subprocess.STDOUT,
    )  # fmt: skip
    deadline = time.time() + 30
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        try:
            fetch_state(port, timeout=1)
            return proc
        except OSError:
            time.sleep(0.3)
    proc.kill()
    raise RuntimeError(f"world server for {task_id} did not start; see {log}")


def run_attempt(
    harness: str, task: Task, trial: int, out: Path, key: str, timeout: int, slot: int, pin_language: bool = False
) -> dict[str, Any]:
    """Run and grade one (harness, task, trial) attempt.

    Args:
        harness: Harness name.
        task: The task.
        trial: Trial index, for repeated runs.
        out: The run's output directory.
        key: OpenRouter API key.
        timeout: Seconds before the harness is killed.
        slot: Index of the concurrent worker running this attempt.
        pin_language: Also tell the text-mode DSPy loops which language to reply in.

    Returns:
        The result record written to ``results.jsonl``.
    """
    workdir = out / "attempts" / harness / f"{task.id}.t{trial}"
    workdir.mkdir(parents=True, exist_ok=True)
    # opencode keeps a SQLite database in its home, which locks when two attempts share it.
    home = out / "homes" / harness / f"slot{slot}" if harness == "opencode" else out / "homes" / harness
    home.mkdir(parents=True, exist_ok=True)
    port = free_port()
    started = time.time()
    try:
        server = start_server(task.id, port, workdir / "server.log")
    except RuntimeError as exc:
        # A world that cannot start is a benchmark bug, not a harness failure:
        # recording it as "infra" keeps it out of the harness's pass rate.
        return {"harness": harness, "task": task.id, "trial": trial, "infra_error": str(exc)}
    try:
        message = user_message(task)
        if harness in CLI_HARNESSES:
            attempt = CLI_HARNESSES[harness](message, brief(), port, workdir, home, key, timeout)
        elif pin_language and harness in ("dspy-reactv2", STABLE):
            pinned = {"reply_language": "Hebrew" if is_hebrew(task.prompt) else "English"}
            attempt = run_dspy(harness, message, brief(), port, workdir, key, timeout, pinned)
        else:
            attempt = run_dspy(harness, message, brief(), port, workdir, key, timeout)
        snapshot = fetch_state(port)
    except Exception as exc:
        attempt, snapshot = Attempt(error=f"{type(exc).__name__}: {exc}"[:300]), None
        with contextlib.suppress(OSError):
            snapshot = fetch_state(port)
    finally:
        server.kill()
        server.wait()
    seconds = time.time() - started
    # A harness that died before its first model call never attempted the task.
    if snapshot is None or (attempt.error and not attempt.llm_calls):
        return {"harness": harness, "task": task.id, "trial": trial, "infra_error": attempt.error}
    (workdir / "world.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=1))
    result = grade(task, Run(snapshot["state"], snapshot["initial"], snapshot["calls"], attempt.answer))
    calls = snapshot["calls"]
    return {
        "harness": harness, "task": task.id, "trial": trial, "category": task.category,
        "difficulty": task.difficulty, **result, **attempt.as_dict(), "seconds": round(seconds, 1),
        "tool_calls": len(calls), "failed_tool_calls": sum(1 for c in calls if not c["ok"]),
        "tools_used": [c["tool"] for c in calls],
    }  # fmt: skip


def main() -> None:
    """Parse arguments and run the requested harness × task × trial matrix."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--harness", nargs="+", choices=HARNESSES, default=HARNESSES)
    parser.add_argument("--tasks", nargs="*", help="task ids (default: all)")
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--timeout", type=int, default=420)
    parser.add_argument("--out", type=Path, default=ROOT / "results" / "latest")
    parser.add_argument(
        "--pin-language", action="store_true",
        help="pin the reply language for dspy-reactv2 and dspy-reactv2-stable, to compare them without language drift",
    )  # fmt: skip
    args = parser.parse_args()

    key = api_key()
    tasks = load_tasks()
    chosen = [tasks[t] for t in args.tasks] if args.tasks else list(tasks.values())
    # Each CLI runs from its own working directory, so every path handed to it must be absolute.
    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    results_path = args.out / "results.jsonl"
    done = set()
    if results_path.exists():
        for line in results_path.read_text().splitlines():
            record = json.loads(line)
            if "infra_error" not in record:
                done.add((record["harness"], record["task"], record["trial"]))

    for harness in args.harness:
        todo = [(t, n) for n in range(args.trials) for t in chosen if (harness, t.id, n) not in done]
        if not todo:
            print(f"{harness}: nothing to do")
            continue
        before = billed_usage(key)
        started = time.time()
        slots: queue.Queue[int] = queue.Queue()
        for index in range(args.concurrency):
            slots.put(index)

        def work(item: tuple[Task, int], harness: str = harness, slots: queue.Queue[int] = slots) -> None:
            """Run one attempt and append its record."""
            task, trial = item
            slot = slots.get()
            try:
                record = run_attempt(harness, task, trial, args.out, key, args.timeout, slot, args.pin_language)
            finally:
                slots.put(slot)
            with _write_lock:
                with results_path.open("a") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                mark = (
                    "INFRA" if "infra_error" in record else ("pass" if record["passed"] else f"{record['score']:.2f}")
                )
                print(f"{harness:13} {task.id:34} t{trial} {mark:5} {record.get('seconds', 0):6.1f}s", flush=True)

        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            list(pool.map(work, todo))
        after = settled_usage(key)
        batch = {
            "harness": harness, "model": MODEL, "attempts": len(todo), "wall_seconds": round(time.time() - started),
            "billed_usd": round(after - before, 6) if before is not None and after is not None else None,
        }  # fmt: skip
        with (args.out / "batches.jsonl").open("a") as handle:
            handle.write(json.dumps(batch) + "\n")
        print(json.dumps(batch), flush=True)


if __name__ == "__main__":
    main()
