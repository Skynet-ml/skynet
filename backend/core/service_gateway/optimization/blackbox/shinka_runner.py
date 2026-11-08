"""Sandbox-side runner that drives the pinned upstream ShinkaEvolve package.

This file is copied verbatim into an isolated sandbox and runs in the pinned
``shinka-evolve`` interpreter. It imports no Skynet application code; the
parent owns the scorer and its cases. Upstream's loop (islands, parent
selection, diff/full/crossover mutations, meta notes and the model bandit)
runs unchanged. Skynet contributes only:

* the starting version, written as one Markdown program whose evolvable text
  sits between upstream's ``EVOLVE-BLOCK`` markers (one marked block per named
  part, so the parts split back out losslessly);
* an ``evaluate.py`` that hands each program to this process over a file
  queue, where it is scored through the parent-owned budget over the same
  filesystem mailbox the other native engines use;
* model routing: every optimization model is an OpenAI-compatible
  ``local/...`` model on the run's gateway, and every call carries usage tags
  so the run's usage splits into mutation, meta-note, novelty-judge and
  embedding calls;
* for a repository target, a program that is a bundle of the files a version
  changes, with the prompts, edit application and novelty input replaced by
  ``shinka_bundle``'s file-aware versions;
* optionally, an agent editor: each new version is written by a Pi coding
  agent (file tools only) editing the parent's files in a scratch directory,
  instead of upstream's single model call answering with a patch.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import difflib
import importlib.util
import io
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import threading
import time
import traceback
import uuid
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

try:
    import shinka.core.async_runner as shinka_async_runner
    import shinka.embed.embedding as shinka_embedding
    import shinka.llm.llm as shinka_llm
    import shinka.llm.query as shinka_query
    from shinka.core import EvolutionConfig, ShinkaEvolveRunner
    from shinka.database import DatabaseConfig
    from shinka.edit import summarize_diff
    from shinka.launch import LocalJobConfig
    from shinka.local_openai_config import parse_local_openai_model
    from shinka.prompts import FULL_SYS_FORMATS
except ImportError:  # Upstream needs Python 3.12; parent-side tests still import the helpers.
    shinka_async_runner = shinka_embedding = shinka_llm = shinka_query = EvolutionConfig = ShinkaEvolveRunner = None
    DatabaseConfig = LocalJobConfig = parse_local_openai_model = summarize_diff = None
    FULL_SYS_FORMATS = ()

try:
    from . import harness_bridge, repo_tree, shinka_bundle
except ImportError:  # In the sandbox this file runs as a script beside its sibling modules.
    _sibling_modules = {}
    for _sibling in ("harness_bridge", "repo_tree", "shinka_bundle"):
        _spec = importlib.util.spec_from_file_location(_sibling, Path(__file__).with_name(f"{_sibling}.py"))
        assert _spec is not None
        assert _spec.loader is not None
        _sibling_modules[_sibling] = importlib.util.module_from_spec(_spec)
        sys.modules[_sibling] = _sibling_modules[_sibling]
        _spec.loader.exec_module(_sibling_modules[_sibling])
    harness_bridge = _sibling_modules["harness_bridge"]
    repo_tree = _sibling_modules["repo_tree"]
    shinka_bundle = _sibling_modules["shinka_bundle"]

_RPC_PREFIX = "APP_NATIVE_RPC "
_PROGRESS_PREFIX = "APP_NATIVE_PROGRESS "
_LOG_PREFIX = "APP_NATIVE_LOG "
_MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
_TOKEN_NAMES = ("prompt_tokens", "completion_tokens")
_LANGUAGE = "markdown"
_LANGUAGE_EXT = "md"
_BLOCK_START = "<!-- EVOLVE-BLOCK-START -->"
_BLOCK_END = "<!-- EVOLVE-BLOCK-END -->"
_PART_PREFIX = "<!-- APP-PART "
_PART_SUFFIX = " -->"
_PART_HEADER = re.compile(r"^<!-- APP-PART (.+) -->$")
_GENERATION = re.compile(r"(?:^|/)gen_(\d+)/")
_FEEDBACK_CHARS = 8000
# Mirrors usage_tags in the parent; this file runs without the backend's code.
_USAGE_TAGS_HEADER = "x-app-usage-tags"
_CALLER_PROPOSER = "proposer"
_KIND_MUTATION = "mutation"
_KIND_META_NOTES = "meta_notes"
_KIND_NOVELTY_JUDGE = "novelty_judge"
_KIND_EMBEDDING = "embedding"
_AGENT_PROGRAM_FILE = "program.md"
# Where the launch writes Pi's config inside a scratch directory; never part of a version.
_AGENT_CONFIG_DIR = ".app"
_AGENT_KEY_ENV = re.compile(r"[?&]api_key_env=([A-Za-z0-9_]+)$")
_AGENT_NAME = re.compile(r"^\W*NAME\W*:[\s*_]*(.+?)\s*$", re.MULTILINE | re.IGNORECASE)
_AGENT_DESCRIPTION = re.compile(r"^\W*DESCRIPTION\W*:[\s*_]*(.+)", re.MULTILINE | re.IGNORECASE | re.DOTALL)
_AGENT_FINISH = (
    "When you are done editing, reply with one line `NAME: <short_lowercase_name_with_underscores>` "
    "followed by one line `DESCRIPTION: <what you changed and why it should score higher>`."
)
_AGENT_TEXT_TASK = (
    f"# Task\nThe current program is the file `{_AGENT_PROGRAM_FILE}` in your working directory. Improve it so "
    "it scores higher by editing that file in place with your tools. Change only the text between the "
    "EVOLVE-BLOCK markers, and keep every marker and every APP-PART line exactly as it is. " + _AGENT_FINISH
)
_AGENT_REPO_TASK = (
    "# Task\nYour working directory is the repository as the current version holds it. Improve it so it "
    "scores higher by editing files with your tools; read any file you need first. Only changes to the text "
    "files you may change are kept, and deleting a file is not. " + _AGENT_FINISH
)
_TASK_SYSTEM_MESSAGE = (
    "You are an expert at improving text artifacts such as prompts, instructions and agent "
    "configurations. The program is a Markdown document; only the text between the "
    "EVOLVE-BLOCK markers is evolved. Keep every marker and every APP-PART line exactly as "
    "they are, and change only the text inside the blocks so that it scores higher."
)
_EVALUATE_TEMPLATE = '''"""Hand one ShinkaEvolve program to the Skynet runner and record its score."""

import argparse
import json
import os
import time
import uuid
from pathlib import Path

QUEUE = Path({queue!r})
TIMEOUT = {timeout!r}


def main():
    """Queue the program, wait for its score and write upstream's result files."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--program_path", required=True)
    parser.add_argument("--results_dir", required=True)
    args, _ = parser.parse_known_args()
    request_id = uuid.uuid4().hex
    pending = QUEUE / "requests" / (request_id + ".tmp")
    pending.write_text(json.dumps({{"program_path": os.path.abspath(args.program_path)}}), encoding="utf-8")
    pending.rename(pending.with_suffix(".json"))
    answer = QUEUE / "responses" / (request_id + ".json")
    deadline = time.monotonic() + TIMEOUT
    response = {{"correct": False, "error": "The Skynet scorer did not answer.", "metrics": {{}}}}
    while time.monotonic() < deadline and not (QUEUE / "stop").exists():
        if answer.exists():
            try:
                response = json.loads(answer.read_text(encoding="utf-8"))
                break
            except (OSError, ValueError):
                pass
        time.sleep(0.05)
    results = Path(args.results_dir)
    results.mkdir(parents=True, exist_ok=True)
    correct = {{"correct": bool(response.get("correct")), "error": response.get("error")}}
    (results / "correct.json").write_text(json.dumps(correct), encoding="utf-8")
    (results / "metrics.json").write_text(json.dumps(response.get("metrics") or {{}}), encoding="utf-8")


if __name__ == "__main__":
    main()
'''


class EvaluationStopped(BaseException):
    """Abort the run immediately; upstream retry handling must not see this."""


class BudgetStopped(EvaluationStopped):
    """Stop admission without converting an unperformed evaluation into feedback."""


class EvaluatorMailbox:
    """Send evaluation requests to the parent and await matching response files."""

    def __init__(self, nonce: str, timeout_seconds: float) -> None:
        """Create a process-scoped evaluator transport.

        Args:
            nonce: Parent-generated request framing token.
            timeout_seconds: Maximum time any evaluation can wait for a response.
        """
        self.nonce = nonce
        self.timeout_seconds = timeout_seconds
        self.error: EvaluationStopped | None = None
        self.stopped = threading.Event()
        self.total_evals = 0
        self._write_lock = threading.Lock()

    def evaluate(
        self, candidate: str | dict[str, str], example: Any = None, *, candidate_id: int | None = None
    ) -> tuple[float, dict[str, Any]]:
        """Evaluate only through the parent-owned budget and scorer.

        Args:
            candidate: Text candidate or named component mapping.
            example: Visible case, or ``None`` for a run without cases.
            candidate_id: Version number the parent files the scorer's logs under.

        Returns:
            Parent score and feedback.

        Raises:
            EvaluationStopped: When the parent rejects evaluation or does not reply.
        """
        if self.stopped.is_set():
            raise self.error or EvaluationStopped("The parent evaluator has stopped this run.")
        request_id = uuid.uuid4().hex
        self.emit(
            _RPC_PREFIX, {"id": request_id, "candidate": candidate, "example": example, "candidate_id": candidate_id}
        )
        response_path = Path("rpc") / f"{request_id}.json"
        deadline = time.monotonic() + self.timeout_seconds
        while not self.stopped.is_set() and time.monotonic() < deadline:
            if response_path.exists():
                try:
                    response = json.loads(response_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    # Both runtime filesystems can expose a new file before its
                    # contents have finished landing.
                    time.sleep(0.05)
                    continue
                if "error" in response:
                    kind = BudgetStopped if response.get("stop_reason") == "budget_reached" else EvaluationStopped
                    self.error = kind(str(response["error"]))
                    self.stopped.set()
                    raise self.error
                with self._write_lock:
                    self.total_evals += 1
                return float(response["score"]), dict(response.get("info") or {})
            time.sleep(0.05)
        if self.stopped.is_set():
            # The supervisor stopped the run and records why; claiming a reply
            # timeout here would mask that reason.
            raise self.error or EvaluationStopped("The parent evaluator has stopped this run.")
        self.error = self.error or EvaluationStopped("Native evaluator response timed out.")
        self.stopped.set()
        raise self.error

    def emit(self, prefix: str, payload: dict[str, Any]) -> None:
        """Write one framed event atomically across evaluator threads.

        Args:
            prefix: Request or progress event family.
            payload: Event fields to serialize.
        """
        with self._write_lock:
            print(f"{prefix}{self.nonce} {json.dumps(payload, default=str, allow_nan=False)}", flush=True)


def rule_blocker(mailbox: EvaluatorMailbox) -> Callable[[str], None]:
    """Build the callback that sends a path-rule block to the parent's run log.

    Args:
        mailbox: Parent connection.

    Returns:
        A function taking the block's reason.
    """

    def block(reason: str) -> None:
        """Send one ``rule.block`` run event.

        Args:
            reason: Why the path rules refused the version.
        """
        mailbox.emit(_LOG_PREFIX, repo_tree.rule_block_record(reason))

    return block


def refusal_logger(mailbox: EvaluatorMailbox) -> Callable[[dict[str, str]], None]:
    """Build the callback that sends a hidden-path refusal to the parent's run log.

    Args:
        mailbox: Parent connection.

    Returns:
        A function taking one ``{"tool", "kind"}`` refusal.
    """

    def refuse(refusal: dict[str, str]) -> None:
        """Send one ``proposer.refusal`` run event.

        Args:
            refusal: The refused tool and the hidden path kind.
        """
        mailbox.emit(_LOG_PREFIX, harness_bridge.refusal_record(refusal))

    return refuse


def visible_examples(cases: Sequence[Any] | None) -> list[Any]:
    """Return the examples every version is scored on, one ``None`` when there are no cases.

    Args:
        cases: The run's cases, if any.

    Returns:
        The examples to score, never empty.
    """
    return list(cases or []) or [None]


def _part_header(name: str) -> str:
    """Render the line that names one part of a multi-part program.

    Args:
        name: The part's name.

    Returns:
        A Markdown comment line; ``>`` is escaped so no name can close the comment early.
    """
    return _PART_PREFIX + json.dumps(name).replace(">", "\\u003e") + _PART_SUFFIX


def pack_program(seed: Any) -> str:
    """Write the starting version as a Markdown program with evolvable blocks.

    A text version is one marked block. Named parts become one marked block
    each, preceded by a line naming the part, in the version's own order.

    Args:
        seed: Seed text or named parts from the task.

    Returns:
        The program upstream evolves.

    Raises:
        ValueError: When there is no seed, or its text already contains the markers.
    """
    if isinstance(seed, str):
        parts: list[tuple[str | None, str]] = [(None, seed)]
    elif isinstance(seed, dict) and seed:
        parts = [(str(name), str(text)) for name, text in seed.items()]
    else:
        raise ValueError("ShinkaEvolve requires a seed candidate: a version text or named parts.")
    blocks = []
    for name, text in parts:
        if "EVOLVE-BLOCK-" in text or _PART_PREFIX.strip() in text:
            raise ValueError("The starting version already contains ShinkaEvolve block markers.")
        block = f"{_BLOCK_START}\n{text}\n{_BLOCK_END}"
        blocks.append(block if name is None else f"{_part_header(name)}\n{block}")
    return "\n\n".join(blocks) + "\n"


def unpack_program(program: str, part_names: Sequence[str] | None) -> str | dict[str, str]:
    """Split an evolved program back into the shape of the starting version.

    Args:
        program: Program text upstream wrote.
        part_names: The starting version's part names in order, or ``None`` for a text version.

    Returns:
        The version text, or its named parts.

    Raises:
        ValueError: When the program lost, reordered or duplicated its markers.
    """
    blocks: list[tuple[str | None, str]] = []
    pending_name: str | None = None
    lines = program.split("\n")
    index = 0
    while index < len(lines):
        line = lines[index]
        header = _PART_HEADER.match(line.strip())
        if header:
            try:
                pending_name = str(json.loads(header.group(1)))
            except ValueError as exc:
                raise ValueError("A part name line is no longer valid.") from exc
        elif line.strip() == _BLOCK_START:
            end = next((at for at in range(index + 1, len(lines)) if lines[at].strip() == _BLOCK_END), None)
            if end is None:
                raise ValueError("An evolvable block lost its end marker.")
            blocks.append((pending_name, "\n".join(lines[index + 1 : end])))
            pending_name = None
            index = end
        index += 1
    if part_names is None:
        if len(blocks) != 1:
            raise ValueError("The program must keep exactly one evolvable block.")
        return blocks[0][1]
    if [name for name, _ in blocks] != list(part_names):
        raise ValueError("The program must keep one evolvable block per part, named and ordered as before.")
    return {str(name): text for name, text in blocks}


def generation_of(program_path: str) -> int | None:
    """Read upstream's generation number from a program's path.

    Args:
        program_path: Path upstream evaluates, under its ``gen_<n>`` directory.

    Returns:
        The generation number, or ``None`` when the path does not carry one.
    """
    match = _GENERATION.search(program_path.replace("\\", "/"))
    return int(match.group(1)) if match else None


def local_model_name(model: str, url: str, key_env: str) -> str:
    """Name a gateway route the way upstream addresses an OpenAI-compatible endpoint.

    Args:
        model: Model name the gateway route serves.
        url: Gateway base URL, ending in ``/v1``.
        key_env: Variable holding the route's token.

    Returns:
        A ``local/<model>@<url>?api_key_env=<VAR>`` model name.

    Raises:
        ValueError: When the model name cannot be expressed in that form.
    """
    if "@" in model or not model:
        raise ValueError(f"The model name {model!r} cannot be routed through ShinkaEvolve.")
    separator = "&" if "?" in url else "?"
    return f"local/{model}@{url}{separator}api_key_env={key_env}"


def generation_budget(max_evals: int, examples: int, max_iterations: int | None) -> int:
    """Count how many programs upstream may create, the starting version included.

    Args:
        max_evals: Scorer runs the parent admits.
        examples: Scorer runs one program costs.
        max_iterations: Optional cap on new versions after the starting one.

    Returns:
        Upstream's ``num_generations``.

    Raises:
        ValueError: When the budget cannot score even the starting version.
    """
    affordable = max_evals // max(1, examples)
    if affordable < 1:
        raise ValueError("The evaluation budget cannot score even the starting version once.")
    if max_iterations is not None:
        return max(1, min(affordable, int(max_iterations) + 1))
    return affordable


def build_config(
    payload: Mapping[str, Any], results_dir: str, examples: int, *, repo: bool = False
) -> dict[str, dict[str, Any]]:
    """Translate the parent's payload into upstream's configuration fields.

    Args:
        payload: Parent configuration carrying ``shinka`` settings and ``shinka_models`` routes.
        results_dir: Directory upstream writes its programs and database to.
        examples: Scorer runs one program costs.
        repo: Whether versions are repository bundles rather than marked text.

    Returns:
        Keyword arguments for ``EvolutionConfig`` (``evolution``), ``DatabaseConfig``
        (``database``) and ``ShinkaEvolveRunner`` (``runner``).

    Raises:
        ValueError: When no optimization model is configured.
    """
    settings = dict(payload.get("shinka") or {})
    routes = list(payload.get("shinka_models") or [])
    if not routes:
        raise ValueError("ShinkaEvolve needs at least one optimization model.")
    models = [local_model_name(str(route["model"]), str(route["url"]), str(route["key_env"])) for route in routes]
    meta_notes = bool(settings.get("meta_notes", True))
    meta_env = payload.get("shinka_meta_key_env")
    meta_model = (
        local_model_name(str(routes[0]["model"]), str(routes[0]["url"]), str(meta_env))
        if meta_notes and meta_env
        else None
    )
    embedding = payload.get("shinka_embedding")
    novelty_env = payload.get("shinka_novelty_key_env")
    novelty = bool(settings.get("novelty")) and bool(embedding) and bool(novelty_env)
    embedding_model = (
        local_model_name(str(embedding["model"]), str(embedding["url"]), str(embedding["key_env"])) if novelty else None
    )
    novelty_model = (
        local_model_name(str(routes[0]["model"]), str(routes[0]["url"]), str(novelty_env)) if novelty else None
    )
    task = payload.get("task") or {}
    objective = str(task.get("objective") or "").strip()
    background = str(task.get("background") or "").strip()
    system_message = shinka_bundle.REPO_SYSTEM_MESSAGE if repo else _TASK_SYSTEM_MESSAGE
    if objective:
        system_message += f"\n\nObjective:\n{objective}"
    if background:
        system_message += f"\n\nBackground:\n{background}"
    max_iterations = payload.get("max_iterations")
    if settings.get("editor") == "agent":
        # The agent edits files itself, so the prompt it gets is built from a
        # full-rewrite prompt with the answer format stripped.
        patch_types, patch_type_probs = ["full"], [1.0]
    else:
        patch_types = ["diff", "full", "cross"]
        patch_type_probs = [
            float(settings.get("patch_diff", 0.6)),
            float(settings.get("patch_full", 0.3)),
            float(settings.get("patch_cross", 0.1)),
        ]
    evolution = {
        "task_sys_msg": system_message,
        "patch_types": patch_types,
        "patch_type_probs": patch_type_probs,
        "num_generations": generation_budget(int(payload["max_evals"]), examples, max_iterations),
        "max_patch_resamples": int(settings.get("max_patch_resamples", 3)),
        # A repository answer that only opens files spends an attempt; the
        # extra rounds keep those reads from eating the edit attempts.
        "max_patch_attempts": int(settings.get("max_patch_attempts", 1)) + (shinka_bundle.OPEN_ROUNDS if repo else 0),
        "job_type": "local",
        "language": _LANGUAGE,
        "llm_models": models,
        "llm_dynamic_selection": "ucb",
        # Gateway models carry no upstream catalog price, so the bandit must
        # not weigh cost it cannot see.
        "llm_dynamic_selection_kwargs": {"cost_aware_coef": 0.0},
        "meta_rec_interval": int(settings.get("meta_rec_interval", 10)) if meta_model else None,
        "meta_llm_models": [meta_model] if meta_model else None,
        "meta_max_recommendations": int(settings.get("meta_max_recommendations", 5)),
        "embedding_model": embedding_model,
        "results_dir": results_dir,
        "max_novelty_attempts": int(settings.get("max_novelty_attempts", 3)),
        "code_embed_sim_threshold": float(settings.get("code_embed_sim_threshold", 0.99)),
        "novelty_llm_models": [novelty_model] if novelty_model else None,
        "use_text_feedback": bool(settings.get("use_text_feedback", True)),
    }
    database = {
        # Upstream always files programs here, whatever path it is given.
        "db_path": str(Path(results_dir) / "programs.sqlite"),
        "num_islands": int(settings.get("num_islands", 2)),
        "archive_size": int(settings.get("archive_size", 40)),
        "elite_selection_ratio": float(settings.get("elite_selection_ratio", 0.3)),
        "num_archive_inspirations": int(settings.get("num_archive_inspirations", 1)),
        "num_top_k_inspirations": int(settings.get("num_top_k_inspirations", 1)),
        "migration_interval": int(settings.get("migration_interval", 10)),
        "migration_rate": float(settings.get("migration_rate", 0.0)),
        "parent_selection_strategy": str(settings.get("parent_selection", "weighted")),
        "exploitation_alpha": float(settings.get("exploitation_alpha", 1.0)),
        "exploitation_ratio": float(settings.get("exploitation_ratio", 0.2)),
        "parent_selection_lambda": float(settings.get("parent_selection_lambda", 10.0)),
        "num_beams": int(settings.get("num_beams", 5)),
    }
    runner = {
        "max_evaluation_jobs": int(settings.get("max_parallel_evaluations", 2)),
        "max_proposal_jobs": int(settings.get("max_parallel_proposals", 2)),
        "verbose": False,
        "banner_style": "minimal",
    }
    return {"evolution": evolution, "database": database, "runner": runner}


def usage_kind(model_name: str, meta_key_env: str | None, novelty_key_env: str | None = None) -> str:
    """Tell what a model call is for from the model name upstream used.

    The meta-notes and novelty-judge models read their tokens from their own
    variables, so their names differ from every mutation model's even when
    all are the same model.

    Args:
        model_name: Upstream ``local/...`` model name.
        meta_key_env: Variable the meta-notes model reads its token from.
        novelty_key_env: Variable the novelty-judge model reads its token from.

    Returns:
        ``meta_notes``, ``novelty_judge`` or ``mutation``.
    """
    if meta_key_env and model_name.endswith(f"api_key_env={meta_key_env}"):
        return _KIND_META_NOTES
    if novelty_key_env and model_name.endswith(f"api_key_env={novelty_key_env}"):
        return _KIND_NOVELTY_JUDGE
    return _KIND_MUTATION


def usage_header(kind: str) -> dict[str, str]:
    """Build the header the gateway reads a call's usage tags from.

    Args:
        kind: What the call is for.

    Returns:
        One header carrying the caller and kind tags.
    """
    tags = {"caller": _CALLER_PROPOSER, "kind": kind}
    return {_USAGE_TAGS_HEADER: json.dumps(tags, separators=(",", ":"), sort_keys=True)}


class UsageLedger:
    """Count tokens per model across every upstream model call."""

    def __init__(self, meta_key_env: str | None, novelty_key_env: str | None = None) -> None:
        """Start an empty ledger.

        Args:
            meta_key_env: Variable the meta-notes model reads its token from.
            novelty_key_env: Variable the novelty-judge model reads its token from.
        """
        self.meta_key_env = meta_key_env
        self.novelty_key_env = novelty_key_env
        self.usage_by_model: dict[str, dict[str, int]] = {}
        self._lock = threading.Lock()

    def record(self, model_name: str, result: Any) -> None:
        """Fold one upstream query result into the per-model counters.

        Args:
            model_name: Upstream model name the call used.
            result: Upstream query result carrying token counts.
        """
        resolved = parse_local_openai_model(model_name) if parse_local_openai_model is not None else None
        model = resolved.api_model_name if resolved is not None else model_name
        with self._lock:
            current = self.usage_by_model.setdefault(model, dict.fromkeys(_TOKEN_NAMES, 0))
            current["prompt_tokens"] += int(getattr(result, "input_tokens", 0) or 0)
            current["completion_tokens"] += int(getattr(result, "output_tokens", 0) or 0)
            current["total_tokens"] = sum(current[name] for name in _TOKEN_NAMES)

    def install(self) -> None:
        """Wrap upstream's model entry points so every call is tagged and counted."""
        original_client = shinka_query.get_client_llm
        original_async_client = shinka_query.get_async_client_llm
        original_query = shinka_llm.query
        original_query_async = shinka_llm.query_async
        original_embed_client = shinka_embedding.get_client_embed
        original_async_embed_client = shinka_embedding.get_async_client_embed
        meta_key_env = self.meta_key_env
        novelty_key_env = self.novelty_key_env

        def tagged(factory: Callable[..., Any]) -> Callable[..., Any]:
            """Add the usage-tags header to every client a factory builds.

            Args:
                factory: Upstream sync or async client factory.

            Returns:
                The wrapped factory.
            """

            def build(model_name: str, *args: Any, **kwargs: Any) -> Any:
                """Build upstream's client, then tag its requests.

                Args:
                    model_name: Upstream model name.
                    *args: Upstream positional arguments.
                    **kwargs: Upstream keyword arguments.

                Returns:
                    Upstream's ``(client, model, provider)`` with the client tagged.
                """
                client, api_model, provider = factory(model_name, *args, **kwargs)
                if provider == "local_openai" and client is not None and hasattr(client, "with_options"):
                    kind = usage_kind(model_name, meta_key_env, novelty_key_env)
                    client = client.with_options(default_headers=usage_header(kind))
                return client, api_model, provider

            return build

        def tagged_embed(factory: Callable[..., Any]) -> Callable[..., Any]:
            """Add the embedding usage tag to every embeddings client a factory builds.

            Args:
                factory: Upstream sync or async embeddings client factory.

            Returns:
                The wrapped factory.
            """

            def build(model_name: str, *args: Any, **kwargs: Any) -> Any:
                """Build upstream's embeddings client, then tag its requests.

                Args:
                    model_name: Upstream embedding model name.
                    *args: Upstream positional arguments.
                    **kwargs: Upstream keyword arguments.

                Returns:
                    Upstream's ``(client, model)`` with the client tagged.
                """
                client, api_model = factory(model_name, *args, **kwargs)
                if client is not None and hasattr(client, "with_options"):
                    client = client.with_options(default_headers=usage_header(_KIND_EMBEDDING))
                return client, api_model

            return build

        def counted(model_name: str, *args: Any, **kwargs: Any) -> Any:
            """Run one upstream query and count its tokens.

            Args:
                model_name: Upstream model name.
                *args: Upstream positional arguments.
                **kwargs: Upstream keyword arguments.

            Returns:
                Upstream's query result.
            """
            result = original_query(model_name, *args, **kwargs)
            self.record(model_name, result)
            return result

        async def counted_async(model_name: str, *args: Any, **kwargs: Any) -> Any:
            """Run one upstream async query and count its tokens.

            Args:
                model_name: Upstream model name.
                *args: Upstream positional arguments.
                **kwargs: Upstream keyword arguments.

            Returns:
                Upstream's query result.
            """
            result = await original_query_async(model_name, *args, **kwargs)
            self.record(model_name, result)
            return result

        shinka_query.get_client_llm = tagged(original_client)
        shinka_query.get_async_client_llm = tagged(original_async_client)
        shinka_llm.query = counted
        shinka_llm.query_async = counted_async
        shinka_embedding.get_client_embed = tagged_embed(original_embed_client)
        shinka_embedding.get_async_client_embed = tagged_embed(original_async_embed_client)


def install_repo_mode(bundle: Any, runner: Any) -> None:
    """Make upstream evolve repository bundles instead of marked text.

    Upstream's loop, selection and bookkeeping stay as they are; only the
    prompt builders, the edit applier and the novelty input change.

    Args:
        bundle: ``shinka_bundle.RepoBundle`` bound to the run's checkout.
        runner: The constructed upstream ``ShinkaEvolveRunner``.
    """
    sampler = runner.prompt_sampler
    original_embedding = shinka_async_runner.get_code_embedding_async

    def sample(
        parent: Any,
        archive_inspirations: Sequence[Any],
        top_k_inspirations: Sequence[Any],
        meta_recommendations: str | None = None,
    ) -> tuple[str, str, str]:
        """Build one mutation prompt for a bundle version.

        Args:
            parent: Version being mutated.
            archive_inspirations: Archive versions upstream picked.
            top_k_inspirations: Best versions upstream picked.
            meta_recommendations: Upstream's meta notes.

        Returns:
            Upstream's ``(system, user, patch_type)``.
        """
        inspirations = [*archive_inspirations, *top_k_inspirations]
        kind = shinka_bundle.choose_patch_type(sampler.patch_types, sampler.patch_type_probs, bool(inspirations))
        system, user = bundle.prompt(
            kind,
            parent,
            system_message=sampler.task_sys_msg or shinka_bundle.REPO_SYSTEM_MESSAGE,
            inspirations=inspirations,
            partner=inspirations[0] if kind == "cross" else None,
            meta_recommendations=meta_recommendations,
            use_text_feedback=bool(getattr(sampler, "use_text_feedback", True)),
        )
        return system, user, kind

    def sample_fix(incorrect_program: Any, ancestor_inspirations: Sequence[Any] | None = None) -> tuple[str, str, str]:
        """Build the prompt that repairs a bundle version that failed to score.

        Args:
            incorrect_program: Version that failed.
            ancestor_inspirations: Its ancestors.

        Returns:
            Upstream's ``(system, user, "fix")``.
        """
        system, user = bundle.prompt(
            "fix",
            incorrect_program,
            system_message=sampler.task_sys_msg or shinka_bundle.REPO_SYSTEM_MESSAGE,
            inspirations=list(ancestor_inspirations or ()),
        )
        return system, user, "fix"

    async def apply_bundle_patch(
        original_str: str,
        patch_str: str,
        patch_dir: str,
        language: str = _LANGUAGE,
        patch_type: str = "diff",
        verbose: bool = False,
    ) -> tuple[Any, ...]:
        """Apply a model answer to a bundle version, whatever patch type upstream asked for.

        Args:
            original_str: Parent bundle text.
            patch_str: Model answer.
            patch_dir: Generation folder.
            language: Upstream's language; unused, the answer names its files.
            patch_type: Upstream's patch type; unused, each FILE block says how it edits.
            verbose: Upstream's logging flag; unused.

        Returns:
            Upstream's ``(code, applied, output_path, error, patch_text, patch_path)``.
        """
        return await asyncio.to_thread(bundle.write_patch, original_str, patch_str, Path(patch_dir))

    def write_changes(program: Path) -> Path | None:
        """Write a version's changes beside it as the text novelty embeds.

        Args:
            program: The version's bundle file.

        Returns:
            The diff file, or ``None`` when the version changes nothing or cannot be read.
        """
        try:
            changes = bundle.changes_text(shinka_bundle.decode_bundle(program.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return None
        if not changes:
            return None
        target = program.with_name("changes.diff")
        target.write_text(changes, encoding="utf-8")
        return target

    async def embed_changes(exec_fname: str, embedding_client: Any, max_chars: int = 10000) -> tuple[Any, float]:
        """Embed only what a version changes, so shared untouched code cannot make versions look alike.

        Args:
            exec_fname: The version's bundle file.
            embedding_client: Upstream's embeddings client.
            max_chars: Upstream's input cap.

        Returns:
            Upstream's ``(embedding, cost)``; no embedding for a version without changes.
        """
        target = await asyncio.to_thread(write_changes, Path(exec_fname))
        if target is None:
            return None, 0.0
        return await original_embedding(str(target), embedding_client, max_chars)

    sampler.sample = sample
    sampler.sample_fix = sample_fix
    shinka_async_runner.apply_patch_async = apply_bundle_patch
    shinka_async_runner.get_code_embedding_async = embed_changes


def agent_prompt(system: str, user: str, *, repo: bool) -> str:
    """Turn a mutation prompt into the task a coding agent gets.

    Upstream's text prompt ends its system message with a full-rewrite answer
    format and its user message with a task asking for that answer; both are
    replaced by an instruction to edit the files in place. A repository
    prompt built with the ``agent`` kind carries no answer format already.

    Args:
        system: The sampled system message.
        user: The sampled user message.
        repo: Whether the version is a repository bundle.

    Returns:
        One prompt: the system context, the version context, then the task.
    """
    if not repo:
        cut = min((at for at in (system.find(fmt) for fmt in FULL_SYS_FORMATS if fmt) if at >= 0), default=-1)
        if cut >= 0:
            system = system[:cut]
        task_at = user.rfind("\n# Task\n")
        if task_at >= 0:
            user = user[:task_at]
    task = _AGENT_REPO_TASK if repo else _AGENT_TEXT_TASK
    return "\n\n".join(part.strip() for part in (system, user, task) if part.strip()) + "\n"


def parse_agent_answer(text: str | None) -> tuple[str | None, str | None]:
    """Read the edit name and description an agent finished with.

    Args:
        text: The agent's final message.

    Returns:
        ``(name, description)``; either is ``None`` when the agent left it out.
    """
    if not text:
        return None, None
    name = _AGENT_NAME.search(text)
    description = _AGENT_DESCRIPTION.search(text)
    return (
        name.group(1).strip().strip("`*_ ") if name else None,
        description.group(1).strip() if description else None,
    )


def _git(cwd: Path, *arguments: str) -> str:
    """Run git in a directory and fail loudly.

    Args:
        cwd: Directory to run in.
        *arguments: Arguments after ``git``.

    Returns:
        Standard output.

    Raises:
        RuntimeError: When git exits non-zero.
    """
    result = subprocess.run(["git", *arguments], cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"git {arguments[0]} failed: {(result.stderr or result.stdout).strip()[-500:]}")
    return result.stdout


class AgentEditor:
    """Write a new version by letting a Pi coding agent edit the parent's files."""

    def __init__(
        self,
        config: Mapping[str, Any],
        workdir: Path,
        *,
        ledger: UsageLedger,
        deadline: float,
        part_names: Sequence[str] | None = None,
        bundle: Any = None,
    ) -> None:
        """Bind the editor to the run.

        Args:
            config: Payload ``shinka_editor``: ``launches`` (one serialized Pi
                launch per optimization model, each naming its ``key_env``)
                and ``max_tool_calls``.
            workdir: Run folder the scratch directories go under.
            ledger: Usage ledger the sessions' tokens are counted in.
            deadline: ``time.monotonic()`` value no session may run past.
            part_names: The starting version's part names, or ``None`` for a text version.
            bundle: ``shinka_bundle.RepoBundle`` for a repository target. Its
                ``hidden_paths`` guard each session, and its ``on_refusal``
                receives the session's refusals.
        """
        self.launches = {str(launch["key_env"]): dict(launch) for launch in config.get("launches") or ()}
        max_tool_calls = config.get("max_tool_calls")
        self.max_tool_calls = int(max_tool_calls) if max_tool_calls else None
        self.workdir = workdir
        self.ledger = ledger
        self.deadline = deadline
        self.part_names = part_names
        self.bundle = bundle
        self._git_lock = threading.Lock()
        self._tool_lock = threading.Lock()
        self.tool_calls_by_generation: dict[int, dict[str, Any]] = {}

    def record_tool_calls(self, generation: int, report: Mapping[str, Any]) -> None:
        """Add one session's tool calls to the version upstream files under a generation.

        Upstream may retry a generation (novelty or resample attempts), so
        every session spent on it counts towards that version.

        Args:
            generation: Generation the session wrote for.
            report: The session report from :meth:`edit`.
        """
        with self._tool_lock:
            entry = self.tool_calls_by_generation.setdefault(
                generation, {"tool_calls": 0, "tool_cap_reached": False, "agent_sessions": 0}
            )
            entry["tool_calls"] += int(report.get("tool_calls") or 0)
            entry["tool_cap_reached"] = entry["tool_cap_reached"] or bool(report.get("tool_cap_reached"))
            entry["agent_sessions"] += 1

    def tool_calls_for(self, generation: int) -> dict[str, Any] | None:
        """Return the tool calls spent writing a generation, if the agent wrote it.

        Args:
            generation: Upstream generation number.

        Returns:
            ``tool_calls``, ``tool_cap_reached`` and ``agent_sessions``, or ``None``.
        """
        with self._tool_lock:
            entry = self.tool_calls_by_generation.get(generation)
            return dict(entry) if entry is not None else None

    def launch_for(self, model_name: str) -> dict[str, Any]:
        """Find the Pi launch routed like an upstream model.

        Args:
            model_name: Upstream ``local/...`` model name the bandit picked.

        Returns:
            The serialized launch.

        Raises:
            ValueError: When no launch reads the model's key variable.
        """
        match = _AGENT_KEY_ENV.search(model_name)
        launch = self.launches.get(match.group(1)) if match else None
        if launch is None:
            raise ValueError(f"No agent editor launch is configured for {model_name!r}.")
        return launch

    def prompt(
        self,
        sampler: Any,
        parent: Any,
        archive_inspirations: Sequence[Any],
        top_k_inspirations: Sequence[Any],
        meta_recommendations: str | None,
    ) -> str:
        """Build the agent's task from the same context upstream's mutation prompt holds.

        Args:
            sampler: Upstream's prompt sampler.
            parent: Version being mutated.
            archive_inspirations: Archive versions upstream picked.
            top_k_inspirations: Best versions upstream picked.
            meta_recommendations: Upstream's meta notes.

        Returns:
            The prompt.
        """
        if self.bundle is not None:
            system, user = self.bundle.prompt(
                "agent",
                parent,
                system_message=sampler.task_sys_msg or shinka_bundle.REPO_SYSTEM_MESSAGE,
                inspirations=[*archive_inspirations, *top_k_inspirations],
                meta_recommendations=meta_recommendations,
                use_text_feedback=bool(getattr(sampler, "use_text_feedback", True)),
            )
            return agent_prompt(system, user, repo=True)
        system, user, _ = sampler.sample(
            parent=parent,
            archive_inspirations=list(archive_inspirations),
            top_k_inspirations=list(top_k_inspirations),
            meta_recommendations=meta_recommendations,
        )
        return agent_prompt(system, user, repo=False)

    def edit(self, parent_code: str, prompt: str, model_name: str, patch_dir: Path) -> dict[str, Any]:
        """Run one agent session on a copy of the parent and read back the version it leaves.

        Args:
            parent_code: The parent's program text or bundle.
            prompt: The agent's task.
            model_name: Upstream model name the bandit picked.
            patch_dir: Upstream's generation folder; the new version is written there.

        Returns:
            ``cost``, ``tool_calls``, ``tool_cap_reached``, ``name``,
            ``description``, and either ``code``, ``patch``, ``patch_path`` and
            ``changed`` for a new version or ``error`` when there is none.
        """
        launch = self.launch_for(model_name)
        scratch = uuid.uuid4().hex[:12]
        workspace = self.workdir / "agent-edits" / scratch
        session_dir = self.workdir / "agent-sessions" / scratch
        self._prepare(parent_code, workspace)
        hidden = list(self.bundle.hidden_paths) if self.bundle is not None else []
        try:
            outcome = harness_bridge.run_session(
                {
                    **launch,
                    "instructions_file": None,
                    "max_tool_calls": self.max_tool_calls,
                    **({"hidden_paths": hidden} if hidden else {}),
                },
                workspace=workspace,
                prompt=prompt,
                model=str(launch["model"]),
                session_dir=session_dir,
                timeout_seconds=max(1.0, self.deadline - time.monotonic()),
                api_key=os.environ.get(str(launch["key_env"]), ""),
            )
            self.ledger.record(
                model_name,
                SimpleNamespace(
                    input_tokens=outcome.usage.get("input_tokens", 0),
                    output_tokens=outcome.usage.get("output_tokens", 0),
                ),
            )
            if self.bundle is not None and self.bundle.on_refusal is not None:
                for refusal in outcome.refusals:
                    self.bundle.on_refusal(refusal)
            name, description = parse_agent_answer(outcome.text)
            report: dict[str, Any] = {
                "cost": outcome.cost_usd,
                "tool_calls": outcome.tool_calls,
                "tool_cap_reached": outcome.tool_cap_reached,
                "name": name,
                "description": description,
            }
            if outcome.failed:
                report["error"] = "The agent session failed: " + harness_bridge.failure_detail(outcome, limit=1000)
                return report
            try:
                code, patch, changed = self._collect(parent_code, workspace)
            except ValueError as exc:
                report["error"] = str(exc)
                return report
        finally:
            self._discard(workspace)
        patch_dir.mkdir(parents=True, exist_ok=True)
        (patch_dir / f"original.{_LANGUAGE_EXT}").write_text(parent_code, encoding="utf-8")
        (patch_dir / f"main.{_LANGUAGE_EXT}").write_text(code, encoding="utf-8")
        patch_path = patch_dir / "edit.diff"
        patch_path.write_text(patch, encoding="utf-8")
        report.update(code=code, patch=patch, patch_path=patch_path, changed=changed)
        return report

    def _prepare(self, parent_code: str, workspace: Path) -> None:
        """Lay the parent version out as real files in a fresh scratch directory.

        Args:
            parent_code: The parent's program text or bundle.
            workspace: Scratch directory to create.
        """
        workspace.parent.mkdir(parents=True, exist_ok=True)
        if self.bundle is None:
            workspace.mkdir()
            (workspace / _AGENT_PROGRAM_FILE).write_text(parent_code, encoding="utf-8")
            return
        with self._git_lock:
            _git(self.bundle.checkout, "worktree", "add", "--quiet", "--detach", str(workspace), self.bundle.start)
        for path, text in shinka_bundle.decode_bundle(parent_code).items():
            target = workspace / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")

    def _collect(self, parent_code: str, workspace: Path) -> tuple[str, str, int]:
        """Read the version the agent left back into a program.

        Args:
            parent_code: The parent's program text or bundle.
            workspace: The session's scratch directory.

        Returns:
            ``(code, unified_diff, changed_files)``.

        Raises:
            ValueError: When the agent changed nothing, broke the program's
                structure, or, under a constraints file, changed a path the
                rules forbid.
        """
        if self.bundle is None:
            try:
                edited = (workspace / _AGENT_PROGRAM_FILE).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise ValueError(f"The agent left no readable {_AGENT_PROGRAM_FILE}: {exc}") from exc
            code = pack_program(unpack_program(edited, self.part_names))
            if code == parent_code:
                raise ValueError("The agent left the program unchanged.")
            original = f"original.{_LANGUAGE_EXT}"
            patch = "".join(
                difflib.unified_diff(
                    parent_code.splitlines(keepends=True),
                    code.splitlines(keepends=True),
                    fromfile=f"a/{original}",
                    tofile=f"b/{original}",
                )
            )
            return code, patch, 1
        bundle = self.bundle
        parent = shinka_bundle.decode_bundle(parent_code)
        _git(workspace, "add", "--all", "--", ".", f":(exclude){_AGENT_CONFIG_DIR}")
        listed = _git(workspace, "diff", "--cached", "--name-only", "-z", "--no-renames", "--diff-filter=d", "HEAD")
        files: dict[str, str] = {}
        for path in filter(None, listed.split("\0")):
            problem = bundle.rule_problem(path)
            if problem is not None:
                # Without a constraints file the path is dropped silently, as before.
                if bundle.on_block is not None:
                    raise ValueError(bundle.refusal(path, problem))
                continue
            data = (workspace / path).read_bytes()
            if b"\0" in data or len(data) > shinka_bundle.MAX_FILE_BYTES:
                continue
            try:
                files[path] = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
        files = bundle.normalize(files)
        if files == bundle.normalize(parent):
            raise ValueError("The agent left every file as it was.")
        touched = sorted(set(parent) | set(files))
        patch = "".join(
            shinka_bundle.file_diff(path, bundle.current(parent, path), bundle.current(files, path)) for path in touched
        )
        changed = sum(1 for path in touched if bundle.current(parent, path) != bundle.current(files, path))
        return shinka_bundle.encode_bundle(files), patch, changed

    def _discard(self, workspace: Path) -> None:
        """Remove a session's scratch directory.

        Args:
            workspace: The scratch directory.
        """
        if self.bundle is not None:
            with self._git_lock, contextlib.suppress(RuntimeError):
                _git(self.bundle.checkout, "worktree", "remove", "--force", str(workspace))
        shutil.rmtree(workspace, ignore_errors=True)
        if self.bundle is not None:
            with self._git_lock, contextlib.suppress(RuntimeError):
                _git(self.bundle.checkout, "worktree", "prune")


def install_agent_editor(editor: AgentEditor, runner: Any) -> None:
    """Make upstream write every new version with the agent editor.

    Only the patch step changes: upstream still picks the parent,
    inspirations and model (its bandit), and files, scores and selects the
    version the agent leaves exactly as it would one model call's patch.
    Repairs of a failed version (upstream's fix mode) stay single calls.

    Args:
        editor: The run's agent editor.
        runner: The constructed upstream ``ShinkaEvolveRunner``.
    """

    async def run_patch(
        parent_program: Any,
        archive_programs: list[Any],
        top_k_programs: list[Any],
        generation: int,
        meta_recs: str | None = None,
        novelty_attempt: int = 1,
        resample_attempt: int = 1,
        model_sample_probs: list[float] | None = None,
        model_posterior: list[float] | None = None,
    ) -> tuple[str | None, dict[str, Any], bool]:
        """Stand in for upstream's ``_run_patch_async`` with one agent session.

        Args:
            parent_program: Version being mutated.
            archive_programs: Archive versions upstream picked.
            top_k_programs: Best versions upstream picked.
            generation: Generation the new version is filed under.
            meta_recs: Upstream's meta notes.
            novelty_attempt: Upstream's novelty attempt number.
            resample_attempt: Upstream's resample attempt number.
            model_sample_probs: The bandit's model probabilities for this generation.
            model_posterior: The bandit's posterior; unused, as upstream only logs it.

        Returns:
            Upstream's ``(patch_text, meta_patch_data, success)``.
        """
        llm_kwargs = runner.llm.get_kwargs(model_sample_probs=model_sample_probs)
        model_name = str(llm_kwargs.get("model_name", "unknown"))
        selection = getattr(runner, "llm_selection", None)
        if selection is not None:
            selection.update_submitted(model_name)
        try:
            prompt = editor.prompt(runner.prompt_sampler, parent_program, archive_programs, top_k_programs, meta_recs)
            report = await asyncio.to_thread(
                editor.edit,
                str(parent_program.code),
                prompt,
                model_name,
                Path(runner.results_dir) / f"gen_{generation}",
            )
        except Exception as exc:
            report = {"cost": 0.0, "error": f"{type(exc).__name__}: {exc}"}
        editor.record_tool_calls(generation, report)
        if selection is not None:
            selection.update_cost(arm=model_name, cost=report["cost"])
        meta: dict[str, Any] = {
            "api_costs": report["cost"],
            "patch_type": "agent",
            "patch_name": report.get("name"),
            "patch_description": report.get("description"),
            "novelty_attempt": novelty_attempt,
            "resample_attempt": resample_attempt,
            "patch_attempt": 1,
            "system_prompt_id": None,
            **llm_kwargs,
            "tool_calls": report.get("tool_calls", 0),
            "tool_cap_reached": report.get("tool_cap_reached", False),
            "llm_result": None,
        }
        if report.get("code") is None:
            meta["error_attempt"] = "The agent editor produced no new version"
            meta["last_error_msg"] = report.get("error")
            return None, meta, False
        diff_summary: dict[str, Any] = {}
        if summarize_diff is not None:
            diff_summary = summarize_diff(str(report["patch_path"]))
            diff_summary = diff_summary.get(f"original.{_LANGUAGE_EXT}", diff_summary)
        meta.update(num_applied=report["changed"], error_attempt=None, diff_summary=diff_summary)
        return report["patch"], meta, True

    runner._run_patch_async = run_patch


def _feedback_text(info: Mapping[str, Any]) -> str:
    """Flatten one case's scorer feedback into text for the mutation prompt.

    Args:
        info: Side info the parent scorer returned.

    Returns:
        The feedback text and any named scores, one per line.
    """
    lines = []
    feedback = info.get("feedback")
    if isinstance(feedback, str) and feedback.strip():
        lines.append(feedback.strip())
    elif feedback is not None:
        lines.append(json.dumps(_json_finite(feedback), default=str))
    for name, entry in _named_scores(info).items():
        lines.append(f"{name}: {entry['score']:.4g}" + (f" ({entry['feedback']})" if entry["feedback"] else ""))
    return "\n".join(lines)


def _named_scores(info: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Read the scorer's named scores from its side info.

    Args:
        info: Side info the parent scorer returned.

    Returns:
        ``{name: {"score", "feedback"}}`` for every finite named score.
    """
    named: dict[str, dict[str, Any]] = {}
    scores = info.get("scores")
    if not isinstance(scores, dict):
        return named
    for name, entry in scores.items():
        value = entry.get("score") if isinstance(entry, dict) else entry
        if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value):
            feedback = entry.get("feedback") if isinstance(entry, dict) else None
            named[str(name)] = {"score": float(value), "feedback": str(feedback or "")}
    return named


def summarize_scores(results: Sequence[tuple[float, Mapping[str, Any]]]) -> dict[str, Any]:
    """Turn per-case scores into upstream's metrics document.

    Args:
        results: Score and side info per case, in case order.

    Returns:
        ``combined_score`` (the mean), ``public`` (mean named scores),
        ``private`` (per-case scores) and ``text_feedback``.
    """
    scores = [score for score, _ in results]
    named: dict[str, list[float]] = {}
    texts = []
    for index, (score, info) in enumerate(results):
        for name, entry in _named_scores(info).items():
            named.setdefault(name, []).append(entry["score"])
        text = _feedback_text(info)
        label = f"Case {index}" if len(results) > 1 else "Result"
        texts.append(f"{label} scored {score:.4g}" + (f":\n{text}" if text else "."))
    feedback = "\n\n".join(texts)
    if len(feedback) > _FEEDBACK_CHARS:
        feedback = feedback[:_FEEDBACK_CHARS] + "\n[feedback truncated]"
    return {
        "combined_score": sum(scores) / len(scores),
        "public": {name: sum(values) / len(values) for name, values in named.items()},
        "private": {f"case_{index}": score for index, score in enumerate(scores)},
        "text_feedback": feedback,
    }


class ProgramScorer:
    """Score the programs upstream's ``evaluate.py`` queues, through the parent scorer."""

    def __init__(
        self,
        *,
        mailbox: EvaluatorMailbox,
        queue: Path,
        examples: Sequence[Any],
        part_names: Sequence[str] | None,
        max_concurrency: int,
        stop_at_score: float | None,
        bundle: Any = None,
    ) -> None:
        """Bind the scorer to the parent transport and the evaluation queue.

        Args:
            mailbox: Parent evaluation transport.
            queue: Directory holding ``requests``, ``responses`` and the ``stop`` marker.
            examples: Cases every program is scored on.
            part_names: The starting version's part names, or ``None`` for a text version.
            max_concurrency: Parallel scorer requests the parent admits.
            stop_at_score: Score that ends the search early.
            bundle: ``shinka_bundle.RepoBundle`` for a repository target, whose
                programs are materialized into a patch before scoring.
        """
        self.mailbox = mailbox
        self.bundle = bundle
        self.queue = queue
        self.examples = list(examples)
        self.part_names = part_names
        self.stop_at_score = stop_at_score
        self.target_reached = threading.Event()
        self.scored: dict[int, dict[str, Any]] = {}
        self.best: dict[str, Any] | None = None
        self._lock = threading.Lock()
        self._seen: set[str] = set()
        self._cases = ThreadPoolExecutor(max_workers=max(1, max_concurrency))
        self._requests = ThreadPoolExecutor(max_workers=16)
        for name in ("requests", "responses"):
            (queue / name).mkdir(parents=True, exist_ok=True)

    def poll(self) -> None:
        """Start scoring every newly queued program."""
        for path in sorted((self.queue / "requests").glob("*.json")):
            if path.name in self._seen:
                continue
            self._seen.add(path.name)
            self._requests.submit(self._handle, path)

    def stop(self) -> None:
        """Release every waiting ``evaluate.py`` and stop taking new programs."""
        (self.queue / "stop").write_text("stop", encoding="utf-8")
        self._requests.shutdown(wait=False, cancel_futures=True)
        self._cases.shutdown(wait=False, cancel_futures=True)

    def _handle(self, path: Path) -> None:
        """Score one queued program and answer its ``evaluate.py``.

        Args:
            path: The queued request file.
        """
        try:
            request = json.loads(path.read_text(encoding="utf-8"))
            response = self.score_program(str(request["program_path"]))
        except (Exception, EvaluationStopped) as exc:
            response = {"correct": False, "error": f"{type(exc).__name__}: {exc}", "metrics": {}}
        answer = self.queue / "responses" / path.name
        pending = answer.with_suffix(".tmp")
        pending.write_text(json.dumps(_json_finite(response), allow_nan=False), encoding="utf-8")
        pending.rename(answer)

    def score_program(self, program_path: str) -> dict[str, Any]:
        """Score one program on every case and report it as a version.

        Args:
            program_path: Program file upstream wrote.

        Returns:
            The ``correct``/``error``/``metrics`` answer for ``evaluate.py``.
        """
        generation = generation_of(program_path)
        if generation is None:
            return {"correct": False, "error": "The program is outside upstream's generation layout.", "metrics": {}}
        try:
            text = Path(program_path).read_text(encoding="utf-8")
            if self.bundle is not None:
                value = self.bundle.materialize(shinka_bundle.decode_bundle(text))
            else:
                value = unpack_program(text, self.part_names)
        except ValueError as exc:
            return {
                "correct": False,
                "error": str(exc),
                "metrics": {"combined_score": 0.0, "text_feedback": f"Rejected without scoring: {exc}"},
            }
        futures = [
            self._cases.submit(self.mailbox.evaluate, value, example, candidate_id=generation)
            for example in self.examples
        ]
        results = []
        for index, future in enumerate(futures):
            score, info = future.result()
            results.append((score, info))
            if math.isfinite(score):
                self.mailbox.emit(
                    _PROGRESS_PREFIX,
                    {
                        "event": "case_scored",
                        "candidate_id": generation,
                        "example_id": str(index),
                        "score": score,
                        "total": len(self.examples),
                    },
                )
        if not all(math.isfinite(score) for score, _ in results):
            return {"correct": False, "error": "The scorer returned a non-numeric score.", "metrics": {}}
        metrics = summarize_scores(results)
        with self._lock:
            self.scored[generation] = {
                "candidate": value,
                "score": metrics["combined_score"],
                "per_example": [(str(index), score) for index, (score, _) in enumerate(results)],
                "total_evals": self.mailbox.total_evals,
            }
            if self.best is None or metrics["combined_score"] > self.best["best_score"]:
                self.best = {"best_candidate": value, "best_score": metrics["combined_score"], "generation": generation}
        if self.stop_at_score is not None and metrics["combined_score"] >= self.stop_at_score:
            self.target_reached.set()
        return {"correct": True, "error": None, "metrics": metrics}


class LineageReporter:
    """Report each scored version once upstream has filed it with its parent."""

    def __init__(
        self, mailbox: EvaluatorMailbox, scorer: ProgramScorer, db_path: Path, editor: AgentEditor | None = None
    ) -> None:
        """Bind the reporter to the scorer's results and upstream's database.

        Args:
            mailbox: Parent transport progress is written to.
            scorer: Scorer holding every scored version.
            db_path: Upstream's program database.
            editor: The agent editor, whose tool calls each version reports.
        """
        self.mailbox = mailbox
        self.editor = editor
        self.scorer = scorer
        self.db_path = db_path
        self.generation_by_id: dict[str, int] = {}
        self.parent_by_generation: dict[int, int | None] = {}
        self.reported: set[int] = set()

    def poll(self, *, final: bool = False) -> None:
        """Report every scored version whose lineage is known.

        Args:
            final: Also report versions upstream never filed, without a parent.
        """
        self._read_lineage()
        with self.scorer._lock:
            pending = sorted(set(self.scorer.scored) - self.reported)
        for generation in pending:
            if generation not in self.parent_by_generation and not final and generation != 0:
                continue
            self.reported.add(generation)
            parent = self.parent_by_generation.get(generation)
            tool_calls = self.editor.tool_calls_for(generation) if self.editor is not None else None
            self.mailbox.emit(
                _PROGRESS_PREFIX,
                {
                    **self.scorer.scored[generation],
                    "candidate_id": generation,
                    "parent_id": parent,
                    "generation": self.depth(generation),
                    **(tool_calls or {}),
                },
            )

    def depth(self, generation: int) -> int:
        """Count how many versions separate one from the starting version.

        Args:
            generation: Upstream generation number of the version.

        Returns:
            Its lineage depth; the starting version is zero.
        """
        steps = 0
        current: int | None = generation
        visited = set()
        while current is not None and current not in visited:
            visited.add(current)
            current = self.parent_by_generation.get(current)
            if current is not None:
                steps += 1
        return steps

    def _read_lineage(self) -> None:
        """Read every program's generation and parent from upstream's database."""
        if not self.db_path.exists():
            return
        try:
            with contextlib.closing(sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=1.0)) as db:
                rows = db.execute("SELECT id, generation, parent_id FROM programs").fetchall()
        except sqlite3.Error:
            return
        for program_id, generation, _ in rows:
            self.generation_by_id[str(program_id)] = int(generation)
        for _, generation, parent_id in rows:
            parent = self.generation_by_id.get(str(parent_id)) if parent_id else None
            # Migration copies a program onto another island under a new id but
            # the same generation; the first filing names the true parent.
            self.parent_by_generation.setdefault(int(generation), parent if parent != generation else None)


def _archive_artifacts(paths: list[tuple[str, Path]]) -> None:
    """Preserve regular run files within a bounded artifact archive.

    Args:
        paths: Artifact prefixes and corresponding process-local directories.

    Raises:
        RuntimeError: When raw artifacts exceed the transfer limit.
    """
    buffer = io.BytesIO()
    total = 0
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for prefix, root in paths:
            if not root.exists():
                continue
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.is_symlink():
                    continue
                total += path.stat().st_size
                if total > _MAX_ARTIFACT_BYTES:
                    raise RuntimeError("Native artifacts exceed the 64 MiB transfer limit.")
                archive.add(path, arcname=str(Path(prefix) / path.relative_to(root)), recursive=False)
    Path("native_artifacts.tar.gz.b64").write_text(base64.b64encode(buffer.getvalue()).decode("ascii"))


def _json_finite(value: Any) -> Any:
    """Replace unsupported nonfinite scores while preserving metadata.

    Args:
        value: Result field or nested metadata value.

    Returns:
        A JSON-safe value with nonfinite floats represented by ``None``.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(key): _json_finite(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_finite(item) for item in value]
    return value


def execute(payload: dict[str, Any]) -> dict[str, Any]:
    """Run the pinned upstream engine once and describe the outcome.

    Args:
        payload: Parent configuration, task, model routes and evaluator framing token.

    Returns:
        Result envelope, or a failure envelope retaining available usage.

    Raises:
        ValueError: When the payload targets another engine or leaks test examples.
        RuntimeError: When the pinned upstream package is not importable.
    """
    if payload.get("engine_id") != "shinka_evolve":
        raise ValueError("Unsupported native optimizer.")
    task = payload.get("task", {})
    if task.get("test_set") is not None:
        raise ValueError("Held-out examples must not enter the native optimizer.")
    if ShinkaEvolveRunner is None:
        raise RuntimeError("The pinned upstream shinka-evolve package is not importable in this runtime.")
    timeout = float(payload["timeout_seconds"])
    seed = task.get("seed_candidate")
    part_names = [str(name) for name in seed] if isinstance(seed, dict) else None
    examples = visible_examples(task.get("train_set"))
    workdir = Path("shinka-run").resolve()
    results_dir = workdir / "results"
    queue = workdir / "eval-queue"
    results_dir.mkdir(parents=True, exist_ok=True)
    repo = payload.get("repo")
    mailbox = EvaluatorMailbox(payload["nonce"], timeout)
    bundle = (
        shinka_bundle.RepoBundle.prepare(
            repo, seed, workdir / "repo-checkout", on_block=rule_blocker(mailbox), on_refusal=refusal_logger(mailbox)
        )
        if repo
        else None
    )
    if bundle is not None and not isinstance(seed, str):
        seed = ""
    config = build_config(payload, str(results_dir), len(examples), repo=bundle is not None)
    scorer = ProgramScorer(
        mailbox=mailbox,
        queue=queue,
        examples=examples,
        part_names=part_names,
        max_concurrency=int(payload.get("max_concurrency", 1)),
        stop_at_score=payload.get("stop_at_score"),
        bundle=bundle,
    )
    ledger = UsageLedger(payload.get("shinka_meta_key_env"), payload.get("shinka_novelty_key_env"))
    ledger.install()
    runner = ShinkaEvolveRunner(
        evo_config=EvolutionConfig(**config["evolution"]),
        job_config=LocalJobConfig(python_executable=sys.executable),
        db_config=DatabaseConfig(**config["database"]),
        init_program_str=shinka_bundle.encode_bundle({}) if bundle is not None else pack_program(seed),
        evaluate_str=_EVALUATE_TEMPLATE.format(queue=str(queue), timeout=timeout),
        **config["runner"],
    )
    if bundle is not None:
        install_repo_mode(bundle, runner)
    editor = None
    if payload.get("shinka_editor"):
        editor = AgentEditor(
            payload["shinka_editor"],
            workdir,
            ledger=ledger,
            deadline=time.monotonic() + timeout,
            part_names=part_names,
            bundle=bundle,
        )
        install_agent_editor(editor, runner)
    reporter = LineageReporter(mailbox, scorer, Path(config["database"]["db_path"]), editor)
    document: dict[str, Any] = {}
    finished = threading.Event()

    def optimize() -> None:
        """Run upstream on a supervised thread so evaluator failures cannot spawn retries."""
        try:
            runner.run()
        except (Exception, EvaluationStopped) as exc:
            # The parent keeps stderr, so the traceback survives with the envelope.
            traceback.print_exc()
            document["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            finished.set()

    worker = threading.Thread(target=optimize, daemon=True)
    worker.start()
    deadline = time.monotonic() + max(0.1, timeout - min(10.0, timeout * 0.1))
    while not finished.wait(0.1):
        scorer.poll()
        reporter.poll()
        if scorer.target_reached.is_set():
            document["metadata"] = {"stop_reason": "target_reached"}
            break
        if mailbox.stopped.is_set() or time.monotonic() >= deadline:
            document["error"] = str(mailbox.error or "Native optimizer exceeded its runtime limit.")
            mailbox.stopped.set()
            break
    scorer.stop()
    worker.join(timeout=1.0)
    reporter.poll(final=True)
    if mailbox.error is not None:
        document["error"] = str(mailbox.error)
    best = scorer.best
    incumbent = (
        {"best_candidate": best["best_candidate"], "best_score": best["best_score"]}
        if best is not None
        else {"best_candidate": seed, "best_score": None}
    )
    if isinstance(mailbox.error, BudgetStopped):
        document.pop("error", None)
        document["stop_reason"] = "budget_reached"
        document.update(incumbent)
    elif document.get("error"):
        document["interrupted_incumbent"] = {**incumbent, "total_evals": mailbox.total_evals}
    elif best is None:
        document["error"] = "ShinkaEvolve finished without scoring any version."
    else:
        document.update(incumbent)
    document["metadata"] = {
        **document.get("metadata", {}),
        "best_generation": best["generation"] if best is not None else None,
        "scored_versions": len(scorer.scored),
        "num_generations": config["evolution"]["num_generations"],
    }
    document["total_evals"] = mailbox.total_evals
    document["usage_by_model"] = ledger.usage_by_model
    # The gateway meters and bills every call itself; these counts only
    # attribute tokens to models, so a failed call cannot leave them short.
    document["usage_complete"] = True
    try:
        _archive_artifacts([("upstream", results_dir)])
    except Exception as exc:
        document["error"] = f"Could not preserve native artifacts: {exc}"
    return _json_finite(document)


def main() -> int:
    """Run one native engine invocation from its JSON input file.

    Returns:
        Zero for a completed run, one for a persisted failure envelope.
    """
    try:
        payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        result = execute(payload)
    except Exception as exc:
        result = {"error": f"{type(exc).__name__}: {exc}", "usage_by_model": {}, "usage_complete": False}
    Path("native_result.json").write_text(json.dumps(result, default=str, allow_nan=False), encoding="utf-8")
    # Upstream's abandoned loop runs on a daemon thread; exiting here ends it.
    os._exit(1 if result.get("error") else 0)


if __name__ == "__main__":
    raise SystemExit(main())
