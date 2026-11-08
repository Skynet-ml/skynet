"""Serve the best version of a finished repository run through its ``serve.py``.

A repository opts into serving with a ``serve.py`` file at its root that
defines ``serve(input)``. This mirrors the scorer contract
``score(repo_path, case)``: the parent builds a secret-free payload and
stages the pinned tree, and the protected guest applies the best patch,
calls ``serve`` in a child process and returns its JSON output.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ....config import settings
from ....connectors.github_repo import RepoFetchError, fetch_snapshot, github_token
from ....constants import TOKEN_SOURCE_MANAGED
from ....models.common import ModelConfig
from ....worker.repo_staging import hidden_repo_paths
from . import runner
from .harness import ENV_MODEL
from .repo_constraints import read_constraints
from .repo_tree import REPO_SNAPSHOT_KEY, apply_patch, strip_archive, unpack_tree
from .sandbox_scorer import scorer_gateway

SERVE_FILE = "serve.py"
SERVE_FUNCTION = "serve"
INTERACTION_KIND = "repo_serve"
# The guest must answer inside the interaction's 300 s sandbox lifetime, with
# room left for unpacking the tree and returning the result.
SERVE_TIMEOUT_SECONDS = 240.0
_OUTPUT_KEY = "output"

# Runs in a fresh child process so serve.py never sees the guest's event
# nonce or writes into its framed stdout. The runner is loaded by path because
# it is stdlib-only and gives serve.py the same ``skynet.llm`` helper scorers get.
_BOOTSTRAP = """
import importlib.util, json, sys
runner_path, request_path, output_path, checkout = sys.argv[1:5]
spec = importlib.util.spec_from_file_location("app_serve_runner", runner_path)
runner = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = runner
spec.loader.exec_module(runner)
with open(request_path, encoding="utf-8") as stream:
    request = json.load(stream)
gateway = request.get("gateway")
llm = runner.missing_llm
if isinstance(gateway, dict):
    llm = runner.GatewayClient(
        str(gateway["url"]),
        str(gateway["model"]),
        api_key=runner.env_value(runner.ENV_API_KEY),
        temperature=gateway.get("temperature"),
        max_tokens=gateway.get("max_tokens"),
        reasoning_effort=gateway.get("reasoning_effort"),
        timeout_seconds=float(gateway.get("timeout_seconds") or 120.0),
        protected=bool(gateway.get("protected")),
        declared_model=gateway.get("declared_model"),
        subscription=bool(gateway.get("subscription")),
        gateway_reasoning=bool(gateway.get("gateway_reasoning")),
    )
sys.modules[runner.HELPER_MODULE_NAME] = runner.helper_module(
    {runner.LLM_HELPER_NAME: llm, runner.IMAGE_HELPER_NAME: runner.Image, runner.LOG_HELPER_NAME: runner.log}
)
sys.path.insert(0, checkout)
result = {"output": None, "error": None}
try:
    spec = importlib.util.spec_from_file_location("serve", checkout + "/serve.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["serve"] = module
    spec.loader.exec_module(module)
    result["output"] = json.loads(json.dumps(module.serve(request["input"])))
except BaseException as exc:
    result["error"] = f"{type(exc).__name__}: {exc}"
with open(output_path, "w", encoding="utf-8") as stream:
    json.dump(result, stream)
"""


def serve_payload(
    job_data: dict[str, Any],
    *,
    patch: str,
    inputs: dict[str, Any],
    optimization_id: str,
) -> dict[str, Any]:
    """Build the secret-free interaction payload for one repository serve call.

    The payload carries no temporary paths, so a replay with the same key and
    inputs keeps the same request fingerprint.

    Args:
        job_data: Stored repository run, with a pinned commit.
        patch: The version to serve; empty serves the starting code.
        inputs: The caller's input, passed to ``serve`` as one argument.
        optimization_id: The run being served.

    Returns:
        Payload for :func:`core.api.protected_interaction.run_protected_interaction`.
    """
    stored = job_data["payload"]
    repo = stored["target"]["repo"]
    payload: dict[str, Any] = {
        "repo": {"repository": repo["repository"], "commit": repo["commit"]},
        "patch": patch,
        "inputs": inputs,
        "token_source": stored.get("token_source") or TOKEN_SOURCE_MANAGED,
        "_interaction": {"kind": INTERACTION_KIND, "optimization_id": optimization_id},
    }
    scorer = stored.get("scorer")
    model = scorer.get("model") if isinstance(scorer, dict) else None
    if isinstance(model, dict):
        # ``model_config`` is the key the protected gateway rewrites into a
        # metered route; a repository run's only model is its scorer model.
        payload["model_config"] = model
    return payload


@contextmanager
def staged_snapshot(payload: dict[str, Any], *, engine: Any, owner: str | None) -> Iterator[dict[str, Any]]:
    """Fetch the pinned tree in the parent and attach it, without the scoring side, for the sandbox upload.

    The serving guest runs repository code, so it gets the same stripped tree
    as the agent and the candidate (E62): one hidden list for every box.

    Args:
        payload: Resolved interaction payload from :func:`serve_payload`.
        engine: SQLAlchemy engine holding the owner's GitHub connection, or ``None``.
        owner: The run's owner, whose connection reads a private repository.

    Yields:
        The payload with the stripped packed tree under ``REPO_SNAPSHOT_KEY``.

    Raises:
        RepoFetchError: When the repository cannot be fetched at its commit.
        ConstraintsError: When the commit's ``optimize.toml`` is invalid, so the hidden list is unknown.
    """
    token = None
    if engine is not None and owner:
        try:
            token = github_token(engine, owner)
        except RepoFetchError:
            token = None
    workdir = Path(tempfile.mkdtemp(prefix="repo-serve-"))
    try:
        repo = payload["repo"]
        snapshot = fetch_snapshot(repo["repository"], repo["commit"], token, workdir)
        hidden = hidden_repo_paths(read_constraints(snapshot.archive))
        archive = strip_archive(snapshot.archive, hidden, workdir / "serve-tree.tgz")
        yield {**payload, REPO_SNAPSHOT_KEY: {"archive": str(archive), "commit": snapshot.commit}}
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def check_serve_contract(checkout: Path) -> None:
    """Require a root ``serve.py`` that defines ``serve(input)``.

    Args:
        checkout: The repository tree with the served version applied.

    Raises:
        ValueError: When the file is missing, does not parse, or has no
            top-level ``serve`` function that takes one argument.
    """
    path = checkout / SERVE_FILE
    if not path.is_file():
        raise ValueError(f"The repository has no {SERVE_FILE} at its root. Add one that defines serve(input).")
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=SERVE_FILE)
    except (SyntaxError, UnicodeDecodeError) as error:
        raise ValueError(f"{SERVE_FILE} does not parse: {error}") from None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == SERVE_FUNCTION:
            arguments = node.args
            if arguments.posonlyargs or arguments.args or arguments.vararg:
                return
            raise ValueError(f"{SERVE_FILE} must define serve(input) with one argument.")
    raise ValueError(f"{SERVE_FILE} must define a top-level function serve(input).")


def _gateway(payload: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, str], str]:
    """Resolve the ``llm`` route serve.py gets, as the sandbox scorer does.

    Args:
        payload: Guest payload whose ``model_config`` carries the protected route.

    Returns:
        ``(runner gateway section, child environment additions, model name)``;
        no gateway and no additions when the run has no model.
    """
    config = payload.get("model_config")
    if not isinstance(config, dict):
        return None, {}, ""
    model = ModelConfig.model_validate(config)
    gateway = scorer_gateway(model, settings)
    env = {ENV_MODEL: gateway.model}
    if gateway.api_key:
        env[runner.ENV_API_KEY] = gateway.api_key
    return gateway.runner_payload(), env, model.name


def _call_serve(checkout: Path, workdir: Path, value: Any, payload: dict[str, Any]) -> tuple[Any, str]:
    """Run ``serve(value)`` in a child process inside the checkout.

    Args:
        checkout: Tree with the served version applied.
        workdir: Private folder for the request and output files.
        value: The caller's input.
        payload: Guest payload with the optional model route.

    Returns:
        ``(serve output, model name)``.

    Raises:
        RuntimeError: When serve.py fails, times out, or returns no JSON data.
    """
    gateway, env_additions, model_name = _gateway(payload)
    request_path = workdir / "request.json"
    output_path = workdir / "output.json"
    request_path.write_text(json.dumps({"input": value, "gateway": gateway}), encoding="utf-8")
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                _BOOTSTRAP,
                str(Path(runner.__file__)),
                str(request_path),
                str(output_path),
                str(checkout),
            ],
            cwd=checkout,
            env={**os.environ, **env_additions},
            capture_output=True,
            text=True,
            timeout=SERVE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"serve() did not return within {int(SERVE_TIMEOUT_SECONDS)} seconds.") from None
    if not output_path.is_file():
        detail = (completed.stderr or completed.stdout).strip()[-1000:]
        raise RuntimeError(f"serve.py stopped before it returned a result. {detail}".strip())
    result = json.loads(output_path.read_text(encoding="utf-8"))
    if result.get("error"):
        raise RuntimeError(f"serve() failed: {result['error']}")
    return result.get(_OUTPUT_KEY), model_name


def run_repo_serve(payload: dict[str, Any], descriptor: dict[str, Any]) -> dict[str, Any]:
    """Serve one input through the best version of a repository, inside the guest.

    Args:
        payload: Guest payload with the uploaded tree chunks, patch, inputs and route.
        descriptor: Interaction identity with the run id.

    Returns:
        A ``ServeResponse``-shaped result.

    Raises:
        ValueError: When the tree is missing or serve.py breaks the contract.
    """
    snapshot = payload.get(REPO_SNAPSHOT_KEY)
    if not isinstance(snapshot, dict) or not snapshot.get("chunks"):
        raise ValueError("The repository tree did not reach the sandbox.")
    inputs = dict(payload["inputs"])
    with tempfile.TemporaryDirectory(prefix="repo-serve-") as root:
        workdir = Path(root)
        checkout = unpack_tree(snapshot["chunks"], workdir / "repo")
        apply_patch(checkout, str(payload.get("patch") or ""))
        check_serve_contract(checkout)
        output, model_name = _call_serve(checkout, workdir, inputs, payload)
    outputs = output if isinstance(output, dict) else {_OUTPUT_KEY: output}
    return {
        "optimization_id": descriptor["optimization_id"],
        "outputs": outputs,
        "input_fields": list(inputs),
        "output_fields": list(outputs),
        "model_used": model_name,
        "node_traces": None,
    }
