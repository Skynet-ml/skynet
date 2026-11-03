"""The unified contract: static signature checks and the sampled check in a local box."""

from __future__ import annotations

import csv
import io
import json
from typing import Any

import pytest

from core.models.unified import UnifiedRunRequest
from core.service_gateway.agents.code import _unified_inputs

from ...blackbox.harness import GatewayConfig
from ...blackbox.sandbox import LocalSubprocessRuntime
from ...blackbox.tests.mocks import FakeGateway
from ..contract import (
    contract_fingerprint,
    run_contract_check,
    static_contract_check,
    validate_unified_payload,
    verify_workspace,
    with_message_codes,
)
from ..service import unified_job_spec

RUN_CODE = "def run(artifact, input):\n    return artifact + ':' + input['q']\n"
BLACKBOX_METRIC = "def score(input, output, expected=None):\n    return float(output == expected), {'feedback': 'ok'}\n"
# The retired per-optimizer shapes, as saved runs from before the unified contract hold them.
LEGACY_FEEDBACK_METRIC = (
    "def metric(gold, pred, trace=None, pred_name=None, pred_trace=None):\n"
    "    return {'score': 1.0, 'feedback': 'fine'}\n"
)
LEGACY_BUILD_PROGRAM = "import dspy\ndef build_program():\n    return dspy.Predict('q -> a')\n"
# A DSPy program under the one contract: run calls the module-level module. It
# returns the module's instruction so the sampled check needs no model.
DSPY_RUN_CODE = (
    "import dspy\n"
    "program = dspy.Predict('q -> answer')\n"
    "def run(artifact, input):\n"
    "    return dspy.Prediction(answer=program.signature.instructions + ':' + input['q'])\n"
)
DSPY_METRIC = (
    "def score(input, output, expected=None):\n"
    "    return float(output.answer == expected), {'feedback': output.answer}\n"
)


def _payload(
    *,
    kind: str = "prompt",
    optimizer: str = "best_of_n",
    run_code: str = RUN_CODE,
    metric_code: str = BLACKBOX_METRIC,
    metric_model: str | None = None,
    files: dict[str, str] | None = None,
) -> UnifiedRunRequest:
    """Build a pairs-mode unified run with six inline cases."""
    workspace: dict[str, Any] = {"run_code": run_code, "metric_code": metric_code, "metric_timeout_seconds": 30}
    if files is not None:
        workspace["files"] = files
    return UnifiedRunRequest.model_validate(
        {
            "task": {"objective": "o", "artifact": {"kind": kind, "starting_version": "p"}},
            "data": {
                "mode": "pairs",
                "cases": [{"input": {"q": str(i)}, "expected": f"p:{i}"} for i in range(6)],
                "input_fields": ["q"],
            },
            "optimizer": {"choice": optimizer},
            "models": {
                "reflection_model": {"name": "openrouter/task"},
                "metric_model": {"name": metric_model} if metric_model else None,
            },
            "workspace": workspace,
        }
    )


def _statuses(response: Any) -> dict[str, str]:
    """Map each non-sample check key to its status."""
    return {check.key: check.status for check in response.checks if check.sample_index is None}


def _check(payload: UnifiedRunRequest, **kwargs: Any) -> Any:
    """Run the sampled check in a local box against a fake gateway."""
    with FakeGateway(reply="unused") as gw:
        return run_contract_check(
            payload,
            kwargs.pop("sample_count", 2),
            gateway=GatewayConfig(url=gw.url, api_key="k"),
            protected=False,
            runtime=LocalSubprocessRuntime(),
            **kwargs,
        )


def test_static_check_passes_a_matching_blackbox_workspace() -> None:
    """Run and score both fit, and no model is needed."""
    response = static_contract_check(_payload())

    assert response.ok
    assert response.samples == []
    assert _statuses(response) == {
        "workspace.run_signature": "passed",
        "metric.signature": "passed",
        "metric.model": "passed",
    }


def test_missing_run_fails_the_run_signature() -> None:
    """Run code without ``run`` fails before anything executes."""
    response = static_contract_check(_payload(run_code="def go(artifact, input):\n    return ''\n"))

    assert not response.ok
    assert _statuses(response)["workspace.run_signature"] == "failed"
    with pytest.raises(ValueError):
        validate_unified_payload(_payload(run_code="x = 1\n"))


@pytest.mark.parametrize(
    ("kind", "optimizer", "run_code", "metric_code"),
    [
        ("dspy_program", "auto", DSPY_RUN_CODE, DSPY_METRIC),
        ("dspy_program", "dspy_gepa", DSPY_RUN_CODE, DSPY_METRIC),
        ("dspy_program", "dspy_miprov2", DSPY_RUN_CODE, DSPY_METRIC),
        ("dspy_program", "dspy_bootstrap_fewshot", DSPY_RUN_CODE, DSPY_METRIC),
        ("prompt", "gepa", RUN_CODE, BLACKBOX_METRIC),
        ("prompt", "best_of_n", RUN_CODE, BLACKBOX_METRIC),
    ],
)
def test_every_optimizer_checks_the_same_contract(kind: str, optimizer: str, run_code: str, metric_code: str) -> None:
    """run(artifact, input) plus score(input, output, expected=None) passes for every kind and optimizer.

    Args:
        kind: The artifact kind.
        optimizer: The optimizer the user picked.
        run_code: Run code written to the one contract.
        metric_code: Metric written to the one contract.
    """
    response = static_contract_check(
        _payload(kind=kind, optimizer=optimizer, run_code=run_code, metric_code=metric_code)
    )

    assert response.ok, response.checks
    validate_unified_payload(_payload(kind=kind, optimizer=optimizer, run_code=run_code, metric_code=metric_code))


def test_retired_shapes_ask_to_regenerate_the_code() -> None:
    """Code saved under build_program() / metric(gold, pred, ...) fails with a regenerate hint."""
    payload = _payload(
        kind="dspy_program", optimizer="dspy_gepa", run_code=LEGACY_BUILD_PROGRAM, metric_code=LEGACY_FEEDBACK_METRIC
    )
    response = static_contract_check(payload)

    details = {check.key: check.detail or "" for check in response.checks}
    assert not response.ok
    assert _statuses(response)["workspace.run_signature"] == "failed"
    assert _statuses(response)["metric.signature"] == "failed"
    assert "regenerate the run code" in details["workspace.run_signature"]
    assert "regenerate the metric" in details["metric.signature"]
    with pytest.raises(ValueError, match="regenerate the run code"):
        validate_unified_payload(payload)


def test_a_guest_holding_an_older_contract_never_reads_as_all_passed() -> None:
    """A static item the sandbox failed but the host passed stays visible as a failed run item."""
    payload = _payload(kind="dspy_program", optimizer="dspy_gepa", run_code=DSPY_RUN_CODE, metric_code=DSPY_METRIC)
    static = static_contract_check(payload)
    guest = static.model_copy(
        update={
            "ok": False,
            "checks": [
                static.checks[0].model_copy(
                    update={"status": "failed", "detail": "NameError: build_program is not defined"}
                ),
                *static.checks[1:],
            ],
        }
    )

    response = with_message_codes(guest, static)

    assert static.ok
    assert not response.ok
    failed = [check for check in response.checks if check.status == "failed"]
    assert [check.key for check in failed] == ["workspace.run"]
    assert failed[0].message_code == "contract.runner_outdated"
    assert "build_program is not defined" in (failed[0].detail or "")


def test_an_older_guest_message_never_reaches_the_client_text() -> None:
    """The guest's base-language message lands in detail only; the item's message comes from its code."""
    payload = _payload(kind="dspy_program", optimizer="dspy_gepa", run_code=DSPY_RUN_CODE, metric_code=DSPY_METRIC)
    static = static_contract_check(payload)
    guest_text = "קוד הריצה חייב להגדיר build_program() שמחזירה dspy.Module."
    guest = static.model_copy(
        update={
            "ok": False,
            "checks": [
                static.checks[0].model_copy(update={"status": "failed", "message": guest_text, "detail": None}),
                *static.checks[1:],
            ],
        }
    )

    failed = [check for check in with_message_codes(guest, static).checks if check.status == "failed"]

    assert failed[0].message_params == {}
    assert guest_text not in failed[0].message
    assert guest_text in (failed[0].detail or "")


def test_sampled_check_runs_a_dspy_program_through_run(tmp_path: Any) -> None:
    """In a local box the DSPy program's run applies the candidate to its module and score reads the output.

    Args:
        tmp_path: Unused; keeps the box's working files out of the repo.
    """
    del tmp_path
    response = _check(
        _payload(kind="dspy_program", optimizer="dspy_gepa", run_code=DSPY_RUN_CODE, metric_code=DSPY_METRIC)
    )

    assert response.ok, [check.detail for check in response.checks if check.status != "passed"]
    assert [sample.score for sample in response.samples] == [1.0, 1.0]


def test_blackbox_score_with_one_argument_fails_the_signature() -> None:
    """``score`` must accept at least input and output."""
    response = static_contract_check(_payload(metric_code="def score(output):\n    return 1.0, {}\n"))

    assert _statuses(response)["metric.signature"] == "failed"


def test_metric_calling_llm_defaults_to_reflection_and_rejects_ambiguity() -> None:
    """The model is derived from code, not the submitted metric model."""
    metric = "from skynet import llm\ndef score(input, output):\n    return float(llm('j', output)), {}\n"

    without = static_contract_check(_payload(metric_code=metric))
    with_model = static_contract_check(_payload(metric_code=metric, metric_model="openrouter/judge"))
    ambiguous = static_contract_check(_payload(metric_code=metric.replace("llm('j', output)", "llm('j', model=x)")))

    assert _statuses(without)["metric.model"] == "passed"
    assert _statuses(with_model)["metric.model"] == "passed"
    assert _statuses(ambiguous)["metric.model"] == "failed"
    assert (
        next(item.message_code for item in ambiguous.checks if item.key == "metric.model")
        == "contract.metric_model_declaration"
    )
    assert without.uses_llm
    assert with_model.uses_llm
    assert not static_contract_check(_payload()).uses_llm


@pytest.mark.parametrize("expression", ["llm = 'judge'", "output.llm"])
def test_unrelated_llm_names_do_not_mark_metric_as_calling_helper(expression: str) -> None:
    """Ignore unrelated names in the model check and helper-use response."""
    metric = f"def score(input, output):\n    {expression}\n    return 1.0, {{}}\n"
    response = static_contract_check(_payload(metric_code=metric))
    assert _statuses(response)["metric.model"] == "passed"
    assert not response.uses_llm


@pytest.mark.parametrize("provider", ["openrouter", "openai"])
def test_metric_byok_declaration_accepts_provider(provider: str) -> None:
    """A literal model declaration does not depend on the reflection provider."""
    raw = _payload(
        metric_code="from skynet import llm\ndef score(input, output):\n    return llm(model='openai/a')\n"
    ).model_dump(mode="json")
    raw["models"]["reflection_model"].update(token_source="byok", byok_provider=provider)
    result = static_contract_check(UnifiedRunRequest.model_validate(raw))
    item = next(item for item in result.checks if item.key == "metric.model")
    assert item.status == "passed"


def test_fingerprint_tracks_the_code_and_the_data() -> None:
    """Editing code, cases, split, task model or timeout changes the fingerprint; the same inputs hash the same."""
    base = contract_fingerprint(_payload())

    assert contract_fingerprint(_payload()) == base
    assert contract_fingerprint(_payload(metric_code=BLACKBOX_METRIC + "\n# edit\n")) != base
    edits: list[dict[str, Any]] = [
        {"data": {"cases": [{"input": {"q": "x"}, "expected": "p:x"}] * 6}},
        {"data": {"cases": None, "staged_dataset_id": "ds-1"}},
        {"data": {"split": {"train": 0.5, "val": 0.25, "test": 0.25}}},
        {"data": {"shuffle": False}},
        {"models": {"task_model": {"name": "openrouter/other"}}},
        {"workspace": {"metric_timeout_seconds": 5}},
    ]
    for edit in edits:
        raw = _payload().model_dump(mode="json")
        for section, fields in edit.items():
            raw[section].update(fields)
        assert contract_fingerprint(UnifiedRunRequest.model_validate(raw)) != base


def test_sampled_check_passes_and_reports_each_sample() -> None:
    """A fitting workspace passes every sample and previews its output."""
    response = _check(_payload())

    assert response.ok
    assert [sample.index for sample in response.samples] == [0, 1]
    assert all(sample.score == 1.0 for sample in response.samples)
    assert {check.key for check in response.checks if check.sample_index == 0} == {"workspace.run", "metric.shape"}


def test_each_sample_carries_the_trusted_observation() -> None:
    """The Run check shows each sample's measured time and cost, as the run itself sees them."""
    response = _check(_payload())

    for sample in response.samples:
        assert sample.observation is not None
        assert sample.observation["timing"]["task_ms"] is not None
        assert sample.observation["timing"]["evaluation_ms"] >= sample.observation["timing"]["task_ms"]
        assert sample.observation["cost"]["status"] == "none"


def test_sampled_check_fires_on_ready_once_after_the_box_warms() -> None:
    """The progress hook fires exactly once, between the warm-up and the samples."""
    ready: list[str] = []
    response = _check(_payload(), on_ready=lambda: ready.append("ready"))

    assert response.ok
    assert ready == ["ready"]


def test_sampled_check_never_fires_on_ready_without_samples() -> None:
    """A spec whose cases all land in the held-out test split opens no box and fires no hook."""
    payload = UnifiedRunRequest.model_validate(
        {
            "task": {"objective": "o", "artifact": {"kind": "prompt", "starting_version": "p"}},
            "data": {
                "mode": "pairs",
                "cases": [{"input": {"q": str(i)}, "expected": f"p:{i}"} for i in range(6)],
                "input_fields": ["q"],
                "split": {"train": 0.0, "val": 0.0, "test": 1.0},
            },
            "optimizer": {"choice": "best_of_n"},
            "models": {"reflection_model": {"name": "openrouter/task"}},
            "workspace": {"run_code": RUN_CODE, "metric_code": BLACKBOX_METRIC},
        }
    )
    ready: list[str] = []
    response = _check(payload, on_ready=lambda: ready.append("ready"))

    assert response.samples == []
    assert ready == []


def test_blackbox_score_returning_a_bare_float_fails_the_shape() -> None:
    """A score function returning only a number fails ``metric.shape`` with the blackbox message."""
    response = _check(_payload(metric_code="def score(input, output):\n    return 1.0\n"))

    shape = [check for check in response.checks if check.key == "metric.shape"]
    assert not response.ok
    assert shape
    assert shape[0].status == "failed"


def test_run_crash_reports_the_users_error_and_skips_the_metric() -> None:
    """The exact error reaches the item, without the harness's own frames."""
    run_code = "def run(artifact, input):\n    raise KeyError('missing_field')\n"
    response = _check(_payload(run_code=run_code), sample_count=1)

    run = next(check for check in response.checks if check.key == "workspace.run")
    shape = next(check for check in response.checks if check.key == "metric.shape")
    assert run.status == "failed"
    assert "missing_field" in (run.detail or "")
    assert "workspace_harness" not in (run.detail or "")
    assert shape.status == "skipped"


def test_static_failure_never_opens_a_box() -> None:
    """When the signatures do not fit, nothing runs and no samples come back."""

    class _Refuse(LocalSubprocessRuntime):
        """A runtime that must never be asked for a box."""

        def open(self, spec: Any) -> Any:
            """Fail the test if a box is opened."""
            raise AssertionError("opened a box")

    response = run_contract_check(_payload(run_code="x = 1\n"), 2, gateway=None, protected=False, runtime=_Refuse())

    assert not response.ok
    assert response.samples == []


def test_check_items_carry_their_message_code_for_the_client_locale() -> None:
    """The client renders each item from its code, so an English UI never shows the Hebrew base."""
    response = static_contract_check(_payload(metric_code="x = 1\n"))

    codes = {check.key: (check.message_code, check.message_params) for check in response.checks}
    assert codes["workspace.run_signature"] == ("contract.passed", {})
    assert codes["metric.signature"] == ("contract.metric_missing", {})


def test_a_sample_run_error_carries_its_params() -> None:
    """A failing sample's message code keeps the placeholders the client fills in."""
    response = _check(_payload(run_code="def run(artifact, input):\n    raise ValueError('boom')\n"))

    failed = next(check for check in response.checks if check.key == "workspace.run" and check.status == "failed")
    assert failed.message_code == "contract.run_error"
    assert failed.message_params["index"] == 1
    assert "boom" in str(failed.message_params["error"])


REVIEWS_CSV = (
    "review,product,rating,sentiment\n"
    "Great sound,Headphones,5,positive\n"
    "Broke in a week,Kettle,1,negative\n"
    "Does the job,Lamp,3,neutral\n"
)


def test_a_multi_column_csv_gives_the_agent_and_the_check_the_same_input_fields() -> None:
    """Run code written against the fields the agent is shown passes the sampled check.

    The wizard ignored ``product`` and ``rating``; the seed once still showed
    them, so the agent read ``input["product"]`` and every sample failed with
    ``KeyError``.
    """
    rows = list(csv.DictReader(io.StringIO(REVIEWS_CSV)))
    input_fields = ["review"]
    cases = [{"input": {f: row[f] for f in input_fields}, "expected": row["sentiment"]} for row in rows]
    seen = _unified_inputs(
        {
            "task": {"objective": "o", "artifact": {"kind": "prompt", "starting_version": "p"}},
            "input_fields": input_fields,
            "expected_field": "sentiment",
            "sample_cases": [{"input": dict(row), "expected": row["sentiment"]} for row in rows],
        }
    )
    agent_fields = sorted({key for case in json.loads(seen["sample_cases"]) for key in case["input"]})
    reads = " + ".join(f"input[{field!r}]" for field in agent_fields)
    raw = _payload().model_dump(mode="json")
    raw["data"].update({"cases": cases * 2, "input_fields": input_fields})
    raw["workspace"]["run_code"] = f"def run(artifact, input):\n    return {reads}\n"

    response = _check(UnifiedRunRequest.model_validate(raw), sample_count=3)

    assert seen["case_fields"] == agent_fields == input_fields
    assert [check.status for check in response.checks if check.key == "workspace.run"] == ["passed"] * 3


@pytest.mark.parametrize(
    ("run_code", "metric_code"),
    [
        (RUN_CODE, BLACKBOX_METRIC),
        ("def run(artifact, input):\n    return input['missing']\n", BLACKBOX_METRIC),
        (RUN_CODE, "def score(input, output, expected=None):\n    return 1.0\n"),
        (RUN_CODE, "def score(input, output, expected=None):\n    raise ValueError('bad')\n"),
    ],
)
def test_every_item_and_sample_result_carries_a_message_code(run_code: str, metric_code: str) -> None:
    """Passing, failing and skipped items alike render from their code in the client's locale."""
    response = _check(_payload(run_code=run_code, metric_code=metric_code), sample_count=2)

    assert response.samples
    assert all(check.message_code for check in response.checks)
    assert {check.sample_index for check in response.checks if check.sample_index is not None} == {0, 1}


HELPER_RUN = "from helpers import join\n\ndef run(artifact, input):\n    return join(artifact, input['q'])\n"
HELPER = "def join(a, b):\n    return a + ':' + b\n"


def test_old_payload_without_files_loads_and_keeps_its_fingerprint() -> None:
    """A workspace with only run_code / metric_code loads with no files and hashes as before."""
    payload = _payload()
    assert payload.workspace.files == {}
    assert contract_fingerprint(payload) == contract_fingerprint(_payload(files={}))
    assert contract_fingerprint(_payload(files={"helpers.py": HELPER})) != contract_fingerprint(payload)


@pytest.mark.parametrize(
    "path",
    [
        "/abs.py",
        "../up.py",
        "a/../b.py",
        "a\\b.py",
        "run.py",
        "metric.py",
        ".hidden.py",
        "x.exe",
        "a//b.py",
        "skynet/x.py",
        "lib\n/x.py",
        "x.py\n",
    ],
)
def test_unsafe_file_paths_are_refused(path: str) -> None:
    """Absolute, parent, hidden, fixed, reserved and non-text paths never validate."""
    with pytest.raises(ValueError):
        _payload(files={path: "x = 1\n"})


def test_file_limits_are_enforced() -> None:
    """Too many files, or a file that is also a folder, are refused."""
    with pytest.raises(ValueError):
        _payload(files={f"f{i}.py": "" for i in range(21)})
    with pytest.raises(ValueError):
        _payload(files={"lib.py": "", "lib.py/x.py": ""})
    assert _payload(files={"lib/parse.py": "", "data/rules.json": "{}"}).workspace.files


def test_static_check_reports_extra_files() -> None:
    """A parsing helper passes; a broken one names its file; DSPy optimizers refuse extra files."""
    assert (
        _statuses(static_contract_check(_payload(run_code=HELPER_RUN, files={"helpers.py": HELPER})))["workspace.files"]
        == "passed"
    )
    broken = static_contract_check(_payload(files={"helpers.py": "def (:\n"}))
    item = next(check for check in broken.checks if check.key == "workspace.files")
    assert item.status == "failed"
    assert "helpers.py" in (item.detail or "")
    dspy = static_contract_check(
        _payload(
            kind="dspy_program",
            optimizer="dspy_gepa",
            run_code=DSPY_RUN_CODE,
            metric_code=DSPY_METRIC,
            files={"helpers.py": HELPER},
        )
    )
    assert _statuses(dspy)["workspace.files"] == "failed"


def test_sampled_check_runs_run_code_that_imports_a_helper_file() -> None:
    """run.py imports helpers.py from the workspace and the box scores it."""
    response = _check(_payload(run_code=HELPER_RUN, files={"helpers.py": HELPER}))

    assert response.ok, [(c.key, c.detail) for c in response.checks]
    assert all(sample.score == 1.0 for sample in response.samples)


PARSE = "def clean(text):\n    return text.strip()\n"


@pytest.mark.parametrize(
    ("run_import", "files"),
    [
        ("from lib.parse import clean", {"lib/parse.py": PARSE}),
        ("from lib.parse import clean", {"lib/__init__.py": "", "lib/parse.py": PARSE}),
        ("from parse import clean", {"parse.py": PARSE}),
    ],
)
def test_sampled_check_imports_helpers_from_folders_and_the_root(run_import: str, files: dict[str, str]) -> None:
    """A helper in a folder or at the workspace root imports in every sample, as in a real run."""
    run_code = f"{run_import}\n\ndef run(artifact, input):\n    return clean(artifact + ':' + input['q'])\n"
    response = _check(_payload(run_code=run_code, files=files))

    assert response.ok, [(c.key, c.message, c.detail) for c in response.checks]
    assert all(sample.score == 1.0 for sample in response.samples)


def test_guest_round_trip_keeps_the_workspace_files() -> None:
    """The JSON the image validates still carries every extra file the run and check write."""
    payload = _payload(files={"lib/parse.py": PARSE})
    guest = UnifiedRunRequest.model_validate(json.loads(payload.model_dump_json(by_alias=True)))

    assert guest.workspace.files == {"lib/parse.py": PARSE}
    assert unified_job_spec(guest, seed=0, task_lm=None).context["files"] == {"lib/parse.py": PARSE}


def test_metric_cannot_import_candidate_files() -> None:
    """The metric runs apart from the run code's files, so importing one fails."""
    metric = "from helpers import join\n\ndef score(input, output, expected=None):\n    return 1.0, {}\n"
    response = _check(_payload(run_code=HELPER_RUN, metric_code=metric, files={"helpers.py": HELPER}), sample_count=1)

    assert not response.ok


def test_verify_workspace_gives_each_file_its_own_verdict() -> None:
    """Lint and contract failures land on the file they come from."""
    verdicts = {
        v.path: v
        for v in verify_workspace(
            "from helpers import join\n\ndef go(artifact, input):\n    return join(artifact)\n",
            BLACKBOX_METRIC,
            {"helpers.py": "def join(a):\n    return a + missing\n", "notes.md": "# notes"},
            metric_has_model=False,
            optimizer_resolved="best_of_n",
        )
    }
    assert list(verdicts) == ["run.py", "metric.py", "helpers.py", "notes.md"]
    assert not verdicts["run.py"].ok
    assert any("run(artifact, input)" in error for error in verdicts["run.py"].errors)
    assert verdicts["metric.py"].ok
    assert not verdicts["helpers.py"].ok
    assert "F821" in verdicts["helpers.py"].errors[0]
    assert verdicts["notes.md"].ok


def test_static_check_lints_each_python_file_on_its_own() -> None:
    """Each parsing Python file gets its own lint; a file that does not parse is left to its check item."""
    response = static_contract_check(
        _payload(
            run_code=HELPER_RUN,
            files={"helpers.py": "def join(a, b):\n    return a + missing\n", "broken.py": "def (:\n", "notes.md": "x"},
        )
    )
    lints = {lint.path: lint for lint in response.files}
    assert list(lints) == ["run.py", "metric.py", "helpers.py"]
    assert lints["run.py"].errors == []
    assert any("F821" in error for error in lints["helpers.py"].errors)
