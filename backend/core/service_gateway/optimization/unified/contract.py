"""The unified workspace contract: does the code fit the one contract every optimizer runs?

Every artifact kind and every optimizer share one shape: the run code
defines ``run(artifact, input)`` and the metric defines ``score(input,
output, expected=None)`` returning ``(score, side_info)``. The static check
reads the source in a subprocess, never executing it in the API process: both
entry points must exist with a fitting arity, and a metric that calls
``llm`` needs a metric model. Code written for the retired per-optimizer
shapes (``build_program()``, ``metric(gold, pred, ...)``) fails with a detail
asking to regenerate it.
Extra workspace files (helpers ``run.py`` imports) must parse, and only the
black-box engines can run them: the DSPy optimizers load ``run.py`` alone.
The sandboxed check then runs the starting version through the code and the
metric on a few training/validation cases (never test) and reports the
user's exact error, user-code frames only, when anything does not fit.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ....i18n import t, t_locale
from ....models.unified import (
    DSPY_OPTIMIZERS,
    METRIC_FILE,
    METRIC_SIGNATURE,
    RUN_ENTRY,
    RUN_FILE,
    ContractCheckItem,
    ContractCheckResponse,
    ContractSample,
    UnifiedRunRequest,
    WorkspaceFileLint,
    resolve_optimizer,
)
from ...safe_exec import FunctionShape, WorkspaceCodeIntrospection, introspect_workspace_code
from ..blackbox.harness import GatewayConfig
from ..blackbox.sandbox_contract import SandboxRuntime
from ..blackbox.workspace_harness import DETAIL_MAX_CHARS, METRIC_ENTRY_NAME, RUN_ENTRY_NAME
from ..blackbox.workspace_scorer import WorkspaceProbe, build_workspace_scorer
from .metric_model import metric_declaration_unsafe, resolve_metric_model, scan_metric_llm
from .split import contract_samples, split_cases, split_seed

PREVIEW_CHARS = 500
SIGNATURE_KEY = "contract.metric_signature.blackbox"
SHAPE_KEY = "contract.shape.blackbox"
# The retired per-optimizer entry points; code that defines one instead of the
# current entry point was written before the contract was unified.
LEGACY_RUN_ENTRY = "build_program"
LEGACY_METRIC_ENTRY = "metric"
REGENERATE_RUN = (
    f"NameError: {RUN_ENTRY_NAME} is not defined. build_program() is the retired DSPy entry point: "
    f"regenerate the run code so it defines {RUN_ENTRY} and calls the module-level dspy.Module inside it."
)
REGENERATE_METRIC = (
    f"NameError: {METRIC_ENTRY_NAME} is not defined. metric(gold, pred, ...) is the retired DSPy metric: "
    f"regenerate the metric so it defines {METRIC_SIGNATURE} returning (score, side_info)."
)


def contract_fingerprint(payload: UnifiedRunRequest) -> str:
    """Hash everything a contract check's verdict depends on.

    Args:
        payload: The unified run.

    Returns:
        The sha256 hex digest of the canonical JSON of the artifact, the data
        (inline cases or the staged dataset id), data mode, input and expected
        fields, split and shuffle, resolved optimizer, both pieces of code, the
        task and metric models' names, the metric timeout and any extra files.
    """
    data = payload.data
    task_model = payload.models.task_model
    models = payload.models.model_copy(deep=True)
    if models.reflection_model.token_source is None:
        models.reflection_model.token_source = payload.token_source
    resolve_metric_model(models, payload.workspace.metric_code)
    metric_model = models.metric_model
    # The sampled check draws its cases through the split, so different data
    # or a different split can flip the verdict even with identical code.
    cases = None if data.cases is None else [case.model_dump(mode="json") for case in data.cases]
    body = {
        "artifact": payload.task.artifact.model_dump(mode="json"),
        "data_mode": data.mode,
        "cases": cases,
        "staged_dataset_id": data.staged_dataset_id if data.cases is None else None,
        "input_fields": list(data.input_fields),
        "expected_field": data.expected_field,
        "split": data.split.model_dump(mode="json"),
        "shuffle": data.shuffle,
        "optimizer_resolved": resolve_optimizer(payload.optimizer.choice, payload.task.artifact.kind),
        "run_code": payload.workspace.run_code,
        "metric_code": payload.workspace.metric_code,
        "task_model": task_model.name if task_model is not None else None,
        "metric_model": metric_model.name if metric_model is not None else None,
        "metric_timeout_seconds": payload.workspace.metric_timeout_seconds,
    }
    # Added only when present so fingerprints of single-file workspaces
    # stay what they were before extra files existed.
    if payload.workspace.files:
        body["files"] = dict(payload.workspace.files)
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _item(
    key: str,
    passed: bool,
    message_key: str,
    *,
    detail: str | None = None,
    sample_index: int | None = None,
    **params: Any,
) -> ContractCheckItem:
    """Build one check item, carrying its message code for the client's locale.

    Args:
        key: The contract check key.
        passed: Whether the check passed.
        message_key: i18n key of the failure message.
        detail: Exact error text for a failure.
        sample_index: The sample the item is about, if any.
        **params: Placeholders for the failure message.

    Returns:
        The item; a passing item carries the shared passed message.
    """
    code = "contract.passed" if passed else message_key
    code_params = {} if passed else params
    return ContractCheckItem(
        key=key,  # type: ignore[arg-type]
        status="passed" if passed else "failed",
        message=t(code, **code_params),
        message_code=code,
        message_params=code_params,
        sample_index=sample_index,
        detail=None if passed or detail is None else detail[-DETAIL_MAX_CHARS:],
    )


def _fits(shape: FunctionShape | None, slots: int, at_least: int) -> bool:
    """Return whether a function can be called with ``slots`` positional arguments.

    Args:
        shape: The function's declared arity; unknown arity always fits.
        slots: Positional arguments its caller passes at most.
        at_least: Positional parameters it must accept.

    Returns:
        True when the declared signature fits the call.
    """
    if shape is None:
        return False
    if shape.capacity is None or shape.required is None:
        return True
    return shape.capacity >= at_least and shape.required <= slots


def _run_item(code: WorkspaceCodeIntrospection) -> ContractCheckItem:
    """Check the run code defines ``run(artifact, input)``.

    Args:
        code: The run code's introspection.

    Returns:
        The ``workspace.run_signature`` item.
    """
    detail = code.syntax_error
    if detail is None and RUN_ENTRY_NAME not in code.functions and LEGACY_RUN_ENTRY in code.functions:
        detail = REGENERATE_RUN
    ok = code.syntax_error is None and _fits(code.functions.get(RUN_ENTRY_NAME), 2, 2)
    return _item("workspace.run_signature", ok, "contract.run_missing", detail=detail)


def _metric_items(payload: UnifiedRunRequest, code: WorkspaceCodeIntrospection) -> list[ContractCheckItem]:
    """Check ``score`` fits ``(input, output, expected=None)`` and has a model if it calls one.

    Args:
        payload: The unified run.
        code: The metric code's introspection.

    Returns:
        The ``metric.signature`` and ``metric.model`` items.
    """
    function = code.functions.get(METRIC_ENTRY_NAME)
    if code.syntax_error is not None:
        signature = _item("metric.signature", False, "contract.metric_missing", detail=code.syntax_error)
    elif function is None:
        detail = (
            REGENERATE_METRIC
            if LEGACY_METRIC_ENTRY in code.functions
            else f"NameError: {METRIC_ENTRY_NAME} is not defined"
        )
        signature = _item("metric.signature", False, "contract.metric_missing", detail=detail)
    else:
        # ``expected`` is optional: the harness passes it only when the function accepts it.
        signature = _item("metric.signature", _fits(function, 2, 2), SIGNATURE_KEY)
    models = payload.models.model_copy(deep=True)
    if models.reflection_model.token_source is None:
        models.reflection_model.token_source = payload.token_source
    error = resolve_metric_model(models, payload.workspace.metric_code)
    model = _item("metric.model", error is None, error or "contract.metric_model_declaration")
    return [signature, model]


def _files_item(payload: UnifiedRunRequest) -> ContractCheckItem | None:
    """Check the extra workspace files parse and the optimizer can run them.

    Args:
        payload: The unified run.

    Returns:
        The ``workspace.files`` item, or ``None`` when there are no extra files.
    """
    files = payload.workspace.files
    if not files:
        return None
    if resolve_optimizer(payload.optimizer.choice, payload.task.artifact.kind) in DSPY_OPTIMIZERS:
        return _item("workspace.files", False, "contract.files_unsupported", detail=", ".join(sorted(files)))
    for path in sorted(files):
        if not path.endswith(".py"):
            continue
        error = introspect_workspace_code(files[path]).syntax_error
        if error is not None:
            return _item(
                "workspace.files",
                False,
                "contract.file_invalid",
                detail=f"{path}: {error}",
                path=path,
                error=error,
            )
    return _item("workspace.files", True, "contract.file_invalid")


def _static_items(payload: UnifiedRunRequest) -> tuple[list[ContractCheckItem], bool]:
    """Run every source-level check.

    Args:
        payload: The unified run.

    Returns:
        The run signature, metric signature and metric model items (plus the
        extra files' item when there are any), and whether the metric code
        calls ``llm``.
    """
    run_code = introspect_workspace_code(payload.workspace.run_code)
    metric_code = introspect_workspace_code(payload.workspace.metric_code)
    items = [_run_item(run_code), *_metric_items(payload, metric_code)]
    files = _files_item(payload)
    if files is not None:
        items.append(files)
    return items, scan_metric_llm(payload.workspace.metric_code).uses_llm


# Pyflakes catches what fails at import or call time: undefined names and
# broken imports. Everything else it reports is a warning, never a blocker.
_RUFF_SELECT = "E9,F"
_RUFF_ERROR_CODES = frozenset({"E902", "E999", "F821", "F822", "F823"})
_RUFF_TIMEOUT_SECONDS = 10.0
_RUFF_MAX_FINDINGS = 20


# One file's verdict from :func:`verify_workspace`.
@dataclass
class FileVerification:
    path: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Return whether the file has no errors."""
        return not self.errors

    def as_dict(self) -> dict[str, Any]:
        """Return the verdict as plain data for an SSE event or an agent observation.

        Returns:
            ``{"path", "ok", "errors", "warnings"}``.
        """
        return {"path": self.path, "ok": self.ok, "errors": list(self.errors), "warnings": list(self.warnings)}


def _ruff_binary() -> str | None:
    """Return the ruff executable next to this interpreter or on ``PATH``, if any."""
    beside = Path(sys.executable).with_name("ruff")
    return str(beside) if beside.is_file() else shutil.which("ruff")


def ruff_findings(path: str, code: str) -> tuple[list[str], list[str]]:
    """Lint one Python file with ruff, reading it from stdin so nothing is written.

    Args:
        path: The file's workspace path, shown in each finding.
        code: The file's source.

    Returns:
        ``(errors, warnings)`` as ``"<path>:<line>:<col> <code> <message>"``
        lines; both empty when ruff is unavailable or fails.
    """
    binary = _ruff_binary()
    if binary is None:
        return [], []
    try:
        result = subprocess.run(
            [
                binary,
                "check",
                "--isolated",
                "--no-cache",
                "--select",
                _RUFF_SELECT,
                "--output-format",
                "json",
                "--stdin-filename",
                path,
                "-",
            ],
            input=code,
            capture_output=True,
            text=True,
            timeout=_RUFF_TIMEOUT_SECONDS,
            check=False,
        )
        findings = json.loads(result.stdout or "[]")
    except (OSError, subprocess.SubprocessError, ValueError):
        return [], []
    errors: list[str] = []
    warnings: list[str] = []
    for finding in findings[:_RUFF_MAX_FINDINGS] if isinstance(findings, list) else []:
        location = finding.get("location") or {}
        rule = str(finding.get("code") or "E999")
        line = f"{path}:{location.get('row', 0)}:{location.get('column', 0)} {rule} {finding.get('message', '')}"
        (errors if rule in _RUFF_ERROR_CODES else warnings).append(line)
    return errors, warnings


def _contract_errors(
    path: str, code: WorkspaceCodeIntrospection, *, metric_code: str, reflection_name: str | None = None
) -> list[str]:
    """Return the contract errors of ``run.py`` or ``metric.py``, in the agent's English.

    Args:
        path: :data:`RUN_FILE` or :data:`METRIC_FILE`.
        code: The file's introspection, which parsed.
        metric_code: Metric source to check for model declarations.
        reflection_name: Reflection model name, if known.

    Returns:
        One line per broken rule; empty when the file fits its contract.
    """
    if path == RUN_FILE:
        if _fits(code.functions.get(RUN_ENTRY_NAME), 2, 2):
            return []
        if RUN_ENTRY_NAME not in code.functions and LEGACY_RUN_ENTRY in code.functions:
            return [REGENERATE_RUN]
        return [f"{RUN_FILE} must define {RUN_ENTRY}."]
    errors: list[str] = []
    function = code.functions.get(METRIC_ENTRY_NAME)
    if function is None and LEGACY_METRIC_ENTRY in code.functions:
        errors.append(REGENERATE_METRIC)
    elif not _fits(function, 2, 2):
        errors.append(f"{METRIC_FILE} must define {METRIC_SIGNATURE} returning (score, side_info).")
    scan = scan_metric_llm(metric_code)
    if scan.empty_model:
        errors.append("llm() model= must not be empty or contain only whitespace.")
    elif metric_declaration_unsafe(scan, reflection_name):
        errors.append('llm() calls must name at most one literal model, e.g. model="openai/gpt-4.1-mini".')
    return errors


def workspace_lint(payload: UnifiedRunRequest) -> list[WorkspaceFileLint]:
    """Lint every Python file of the workspace that parses, each on its own.

    A file that does not parse is left out: its syntax error is already a
    check item.

    Args:
        payload: The unified run.

    Returns:
        ``run.py``, ``metric.py`` and then the extra Python files by path,
        each with its ruff findings.
    """
    workspace = payload.workspace
    sources = {RUN_FILE: workspace.run_code, METRIC_FILE: workspace.metric_code}
    sources.update((path, workspace.files[path]) for path in sorted(workspace.files) if path.endswith(".py"))
    lints: list[WorkspaceFileLint] = []
    for path, text in sources.items():
        if introspect_workspace_code(text).syntax_error is not None:
            continue
        errors, warnings = ruff_findings(path, text)
        lints.append(WorkspaceFileLint(path=path, errors=errors, warnings=warnings))
    return lints


def verify_workspace(
    run_code: str,
    metric_code: str,
    files: dict[str, str],
    *,
    optimizer_resolved: str,
    reflection_name: str | None = None,
) -> list[FileVerification]:
    """Check every workspace file on its own, without running any of it.

    Each Python file must parse and pass ruff's error rules; ``run.py`` and
    ``metric.py`` must also fit the contract :func:`static_contract_check`
    enforces, and extra files are refused for the DSPy optimizers. Text
    files are listed as passing.

    Args:
        run_code: ``run.py``.
        metric_code: ``metric.py``.
        files: Path to text of every extra file.
        optimizer_resolved: The optimizer the run resolves to.
        reflection_name: Reflection model name, if known.

    Returns:
        One verdict per file: ``run.py``, ``metric.py``, then the extra files by path.
    """
    sources = {RUN_FILE: run_code, METRIC_FILE: metric_code, **{path: files[path] for path in sorted(files)}}
    verdicts: list[FileVerification] = []
    for path, text in sources.items():
        verdict = FileVerification(path)
        verdicts.append(verdict)
        if path in files and optimizer_resolved in DSPY_OPTIMIZERS:
            verdict.errors.append(
                f"{path}: DSPy optimizers load only {RUN_FILE} and {METRIC_FILE}; move this code into {RUN_FILE}."
            )
        if not path.endswith(".py"):
            continue
        code = introspect_workspace_code(text)
        if code.syntax_error is not None:
            verdict.errors.append(f"{path}: {code.syntax_error}")
            continue
        if path in (RUN_FILE, METRIC_FILE):
            verdict.errors.extend(
                _contract_errors(path, code, metric_code=metric_code, reflection_name=reflection_name)
            )
        lint_errors, lint_warnings = ruff_findings(path, text)
        verdict.errors.extend(lint_errors)
        verdict.warnings.extend(lint_warnings)
    return verdicts


def _response(
    payload: UnifiedRunRequest,
    checks: list[ContractCheckItem],
    samples: list[ContractSample],
    *,
    uses_llm: bool,
) -> ContractCheckResponse:
    """Assemble a contract check response.

    Args:
        payload: The unified run.
        checks: Every check item.
        samples: The scored samples, if any.
        uses_llm: Whether the metric code calls ``llm``.

    Returns:
        The response; ``ok`` only when every item passed.
    """
    return ContractCheckResponse(
        ok=bool(checks) and all(check.status == "passed" for check in checks),
        optimizer_resolved=resolve_optimizer(payload.optimizer.choice, payload.task.artifact.kind),
        uses_llm=uses_llm,
        checks=checks,
        samples=samples,
        fingerprint=contract_fingerprint(payload),
    )


def _recover_error(message: str, code: str, number: int) -> str | None:
    """Read the ``{error}`` placeholder back out of a Hebrew-base message.

    Args:
        message: The rendered Hebrew-base message.
        code: The i18n key the message may have been rendered from.
        number: The sample's 1-based index.

    Returns:
        The error text, or ``None`` when ``message`` was not rendered from ``code``.
    """
    marker = "\x00"
    prefix, _, suffix = t(code, index=number, error=marker).partition(marker)
    if message.startswith(prefix) and message.endswith(suffix) and len(message) >= len(prefix) + len(suffix):
        return message[len(prefix) : len(message) - len(suffix)]
    return None


def _sample_code(item: ContractCheckItem) -> tuple[str, dict[str, str | int]]:
    """Work out the message code of a sample item the guest sent without one.

    Args:
        item: A sample item lacking ``message_code``.

    Returns:
        ``(message_code, message_params)``.
    """
    if item.status == "passed":
        return "contract.passed", {}
    if item.status == "skipped" or (item.key == "metric.shape" and item.message == t(SHAPE_KEY)):
        return SHAPE_KEY, {}
    number = (item.sample_index or 0) + 1
    code = "contract.run_error" if item.key == "workspace.run" else "contract.metric_error"
    error = _recover_error(item.message, code, number)
    if error is None:
        lines = [line for line in (item.detail or item.message).splitlines() if line.strip()]
        error = lines[-1].strip() if lines else ""
    return code, {"index": number, "error": error}


def with_message_codes(response: ContractCheckResponse, static: ContractCheckResponse) -> ContractCheckResponse:
    """Give every item of a sandboxed check the message code the client renders.

    The sandboxed check runs inside the pinned sandbox image, whose backend
    can predate ``message_code``; its items then carry only the Hebrew base
    text, which an English UI would show as is. The static items are
    replaced by the host's own, and sample items get their code back. A
    static item the guest failed although the host passed it means the image
    holds an older contract; it stays in the response as a failed
    ``workspace.run`` item asking for a rebuild, so a failed check never shows
    only passed items. The guest's own words go to ``detail`` only: they are
    in the image's base language, not the client's.

    Args:
        response: The contract check as the sandbox returned it.
        static: The host's static check of the same spec.

    Returns:
        The response with every item carrying ``message_code``.
    """
    samples = []
    for item in response.checks:
        if item.sample_index is None:
            if item.status == "failed" and static.ok:
                detail = f"{item.key}: {item.detail or item.message}"
                samples.append(_item("workspace.run", False, "contract.runner_outdated", detail=detail))
            continue
        if item.message_code is None:
            code, params = _sample_code(item)
            item = item.model_copy(update={"message_code": code, "message_params": params})
        samples.append(item)
    return response.model_copy(update={"checks": [*static.checks, *samples]})


def in_locale(response: ContractCheckResponse, locale: str | None) -> ContractCheckResponse:
    """Render each coded item's message in the caller's locale.

    Args:
        response: A contract check whose items carry Hebrew-base messages.
        locale: The caller's locale tag; None keeps the Hebrew base.

    Returns:
        The response with every coded message in ``locale``.
    """
    if not locale:
        return response
    checks = [
        item
        if item.message_code is None
        else item.model_copy(update={"message": t_locale(locale, item.message_code, **item.message_params)})
        for item in response.checks
    ]
    return response.model_copy(update={"checks": checks})


def validate_unified_payload(payload: UnifiedRunRequest) -> None:
    """Reject a unified run whose code cannot fit its optimizer, without running it.

    Args:
        payload: The unified run.

    Raises:
        ValueError: With the first failing check's localized message, plus the
            regenerate hint when the code was written for a retired shape.
    """
    checks, _ = _static_items(payload)
    for check in checks:
        if check.status == "failed":
            if check.detail in (REGENERATE_RUN, REGENERATE_METRIC):
                raise ValueError(f"{check.message} {check.detail}")
            raise ValueError(check.message)


def static_contract_check(payload: UnifiedRunRequest) -> ContractCheckResponse:
    """Check the code's signatures and metric model without a sandbox.

    Args:
        payload: The unified run.

    Returns:
        The static items and each Python file's lint, with no samples.
    """
    checks, uses_llm = _static_items(payload)
    response = _response(payload, checks, [], uses_llm=uses_llm)
    response.files = workspace_lint(payload)
    return response


def _preview(value: Any) -> str:
    """Render a value for a sample preview.

    Args:
        value: Plain data.

    Returns:
        Compact JSON, capped in length.
    """
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text[:PREVIEW_CHARS]


def _sample_items(index: int, probe: WorkspaceProbe) -> list[ContractCheckItem]:
    """Turn one sample's outcome into its run and metric items.

    Args:
        index: The sample's position, from 0.
        probe: The sample's outcome.

    Returns:
        The ``workspace.run`` and ``metric.shape`` items.
    """
    number = index + 1
    detail = probe.detail or probe.error
    if probe.stage == "run":
        return [
            _item(
                "workspace.run",
                False,
                "contract.run_error",
                detail=detail,
                sample_index=index,
                index=number,
                error=probe.error,
            ),
            ContractCheckItem(
                key="metric.shape",
                status="skipped",
                message=t(SHAPE_KEY),
                message_code=SHAPE_KEY,
                sample_index=index,
            ),
        ]
    run = _item("workspace.run", True, "contract.run_error", sample_index=index)
    if probe.stage == "shape":
        return [run, _item("metric.shape", False, SHAPE_KEY, detail=detail, sample_index=index)]
    if probe.error is not None or probe.score is None:
        return [
            run,
            _item(
                "metric.shape",
                False,
                "contract.metric_error",
                detail=detail,
                sample_index=index,
                index=number,
                error=probe.error or "ValueError: no score",
            ),
        ]
    return [run, _item("metric.shape", True, SHAPE_KEY, sample_index=index)]


def run_contract_check(
    payload: UnifiedRunRequest,
    sample_count: int,
    *,
    gateway: GatewayConfig | None,
    protected: bool,
    runtime: SandboxRuntime | None = None,
    job_id: str | None = None,
    lifetime_seconds: float | None = None,
    dependency_route: dict[str, str] | None = None,
    on_ready: Callable[[], None] | None = None,
) -> ContractCheckResponse:
    """Run the starting version through the code and metric on a few samples, in the sandbox.

    The samples come from train then validation, never test. The static
    checks run first; when they fail, nothing is executed. The box warms up
    before the samples fan out into it together.

    Args:
        payload: The unified run; its cases must be inline.
        sample_count: How many samples to score.
        gateway: The model gateway for managed models, if any.
        protected: Whether this runs under the protected profile.
        runtime: Where the box opens; the configured scorer runtime when unset.
        job_id: Names the box, when known.
        lifetime_seconds: Requested box lifetime; the configured ceiling when unset.
        dependency_route: The protected package relay pinned dependencies install through.
        on_ready: Called once the box is warm and scoring starts, for progress reporting.

    Returns:
        Every check item and the scored samples; ``ok`` only when all passed.

    Raises:
        ValueError: When the cases are not inline.
        ServiceError: When the sandbox could not run the harness at all.
    """
    checks, uses_llm = _static_items(payload)
    if any(check.status == "failed" for check in checks):
        return _response(payload, checks, [], uses_llm=uses_llm)
    if payload.data.cases is None:
        raise ValueError("The contract check needs the cases inline.")
    splits = split_cases(
        list(payload.data.cases),
        payload.data.mode,
        payload.data.split,
        shuffle=payload.data.shuffle,
        seed=split_seed(payload),
    )
    scorer = build_workspace_scorer(
        payload,
        gateway,
        protected=protected,
        runtime=runtime,
        job_id=job_id,
        lifetime_seconds=lifetime_seconds,
        dependency_route=dependency_route,
    )
    samples: list[ContractSample] = []
    try:
        cases = list(contract_samples(splits, sample_count))
        if cases:
            # No box opens for an empty draw: the scorer warms lazily otherwise.
            scorer.warm()
            if on_ready is not None:
                on_ready()
        probes = scorer.run_many(payload.task.artifact.starting_version, cases)
        for index, (case, probe) in enumerate(zip(cases, probes, strict=True)):
            checks.extend(_sample_items(index, probe))
            feedback = probe.side_info.get("feedback")
            samples.append(
                ContractSample(
                    index=index,
                    input_preview=_preview(case.input),
                    output_preview=probe.output_preview,
                    score=probe.score,
                    feedback_preview=feedback[:PREVIEW_CHARS] if isinstance(feedback, str) and feedback else None,
                    duration_ms=probe.duration_ms,
                    observation=probe.observation,
                )
            )
    finally:
        scorer.close()
    return _response(payload, checks, samples, uses_llm=uses_llm)
