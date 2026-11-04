"""Live golden-path smoke test for the Skynet backend.

One test. It hits a real model provider, a real PostgreSQL, and a real
running backend. It exists to answer one question: is the product alive?

Everything else that used to live in this file has been migrated to the
fixture-backed unit tier under `backend/core/**/tests/` — those tests run
in < 20 seconds with zero network dependencies and cover every branch,
error path, and integration boundary that used to be retested here.

What the test covers:
    - test_optimize_golden_path — POST /optimize → worker subprocess →
      optimizer → GET /optimizations/{id} reaches `success` with a best
      version and held-out scores.

Requires:
    - Model credentials in backend/.env (charged for real LLM calls)
    - Backend server running on localhost:8000
    - PostgreSQL running with skynet database

Run:
    cd backend && .venv/bin/python -m pytest tests/test_llm_integration.py -v
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

import pytest
import requests  # type: ignore[import-untyped]

from .conftest import backend_auth_headers, fund_account, requires_llm, requires_server, wait_for_terminal

BASE_URL = "http://localhost:8000"
DATASET_PATH = Path(__file__).resolve().parents[2] / "data" / "math_problems.json"
MODEL = {"name": "openai/gpt-4o-mini"}

# Submitted as user code and run in the sandbox, where ``skynet.llm`` calls the
# task model.
RUN_CODE = """from skynet import llm

def run(artifact, input):
    return llm(artifact + "\\n\\nProblem: " + input["question"] + "\\nFinal numeric answer only:")
"""

METRIC_CODE = """import re

def score(input, output, expected=None):
    numbers = re.findall(r"-?\\d+(?:\\.\\d+)?", str(output).replace(",", ""))
    got = numbers[-1] if numbers else ""
    want = str(expected).replace(",", "").strip()
    return float(got == want), {"feedback": f"expected {want}, got {got or 'no number'}"}
"""


def _load_dataset(rows: int = 12) -> list[dict]:
    """Read the math-problems fixture and return the first ``rows`` rows."""
    with DATASET_PATH.open() as f:
        return json.load(f)[:rows]


def _cleanup(job_id: str, username: str) -> None:
    """Best-effort delete the test job from the backend; swallow any error."""
    with contextlib.suppress(Exception):
        requests.delete(
            f"{BASE_URL}/optimizations/{job_id}",
            headers=backend_auth_headers(username),
            timeout=10,
        )


@pytest.mark.e2e
@pytest.mark.slow
@requires_llm
@requires_server
def test_optimize_golden_path() -> None:
    """End-to-end check: a prompt optimization through ``/optimize`` succeeds with a best version."""
    username = "e2e-golden-optimize"
    fund_account(username)
    payload = {
        "name": "golden path optimize smoke",
        "username": username,
        "task": {
            "objective": "Solve grade school math word problems.",
            "artifact": {"kind": "dspy_program", "starting_version": "Solve the math problem."},
        },
        "data": {
            "mode": "pairs",
            "cases": [{"input": {"question": row["question"]}, "expected": row["answer"]} for row in _load_dataset()],
            "input_fields": ["question"],
            "split": {"train": 0.5, "val": 0.25, "test": 0.25},
        },
        "optimizer": {"choice": "gepa"},
        "models": {"task_model": MODEL, "reflection_model": MODEL},
        "workspace": {"run_code": RUN_CODE, "metric_code": METRIC_CODE},
    }

    headers = backend_auth_headers(username)
    r = requests.post(f"{BASE_URL}/optimize", headers=headers, json=payload, timeout=60)
    assert r.status_code == 201, f"submit failed ({r.status_code}): {r.text}"
    job_id = r.json()["optimization_id"]

    try:
        final = wait_for_terminal(job_id, timeout=1500, username=username)
        assert final["status"] == "success", f"expected success, got {final['status']}: {final.get('message')}"

        result = final["blackbox_result"]
        assert result is not None, "completed job missing blackbox_result"
        assert result["best_candidate"], "run produced no best version"
        assert isinstance(result["baseline_score"], (int, float))
        assert isinstance(result["best_score"], (int, float))
        assert result["num_lm_calls"] > 0, "optimizer should have called the LM"
    finally:
        _cleanup(job_id, username)
