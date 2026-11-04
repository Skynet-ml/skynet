"""Tests for the unified run's server-side held-out split."""

from __future__ import annotations

from typing import Any

import pytest

from core.models.common import SplitFractions
from core.models.unified import MIN_PAIRS_FOR_HELD_OUT, UnifiedCase, UnifiedRunRequest
from core.service_gateway.optimization.unified.split import (
    contract_samples,
    report_cases,
    selection_cases,
    split_cases,
    split_seed,
    task_fingerprint,
)


def _cases(n: int) -> list[UnifiedCase]:
    """Build ``n`` numbered cases.

    Args:
        n: How many cases.

    Returns:
        Cases whose input carries their index.
    """
    return [UnifiedCase(input={"i": i}, expected={"answer": str(i)}) for i in range(n)]


def _payload(**data_overrides: Any) -> UnifiedRunRequest:
    """Build a minimal valid pairs-mode unified request.

    Args:
        **data_overrides: Fields to replace in ``data``.

    Returns:
        The validated request.
    """
    data: dict[str, Any] = {"mode": "pairs", "cases": [case.model_dump() for case in _cases(10)]}
    data.update(data_overrides)
    return UnifiedRunRequest.model_validate(
        {
            "task": {"objective": "answer", "artifact": {"kind": "dspy_program", "starting_version": "Answer."}},
            "data": data,
            "models": {"reflection_model": {"name": "openai/gpt-4o-mini"}},
            "workspace": {"run_code": "def run(artifact, input):\n    return ''", "metric_code": "x = 1"},
        }
    )


def _ids(cases: list[UnifiedCase]) -> list[int]:
    """Read the case indices.

    Args:
        cases: Cases built by :func:`_cases`.

    Returns:
        Their ``input["i"]`` values.
    """
    return [case.input["i"] for case in cases]


def test_pairs_split_sizes_follow_the_fractions() -> None:
    """Twenty cases at 0.7/0.15/0.15 split 14/3/3 with test held out."""
    splits = split_cases(_cases(20), "pairs", SplitFractions(), shuffle=True, seed=3)
    assert (len(splits.train), len(splits.val), len(splits.test)) == (14, 3, 3)
    assert splits.counts.held_out is True
    assert splits.counts.selection_split == "val"
    assert splits.counts.report_split == "test"
    assert sorted(_ids(splits.train + splits.val + splits.test)) == list(range(20))


def test_split_is_deterministic_for_a_seed() -> None:
    """The same seed gives the same split; a different seed shuffles differently."""
    first = split_cases(_cases(20), "pairs", SplitFractions(), shuffle=True, seed=7)
    again = split_cases(_cases(20), "pairs", SplitFractions(), shuffle=True, seed=7)
    other = split_cases(_cases(20), "pairs", SplitFractions(), shuffle=True, seed=8)
    assert _ids(first.test) == _ids(again.test)
    assert _ids(first.train) == _ids(again.train)
    assert _ids(first.train + first.val + first.test) != _ids(other.train + other.val + other.test)


def test_no_shuffle_keeps_submitted_order() -> None:
    """Without shuffling the slices follow the submitted order."""
    splits = split_cases(_cases(20), "pairs", SplitFractions(), shuffle=False, seed=1)
    assert _ids(splits.train) == list(range(14))
    assert _ids(splits.test) == [17, 18, 19]


@pytest.mark.parametrize("n", list(range(2, MIN_PAIRS_FOR_HELD_OUT)))
def test_too_few_pairs_hold_nothing_out(n: int) -> None:
    """Below the held-out minimum, test is empty and reporting falls back to validation.

    Args:
        n: Case count below the minimum.
    """
    splits = split_cases(_cases(n), "pairs", SplitFractions(), shuffle=True, seed=1)
    assert splits.test == []
    assert splits.counts.held_out is False
    assert len(splits.train) + len(splits.val) == n
    assert splits.counts.report_split in {"val", "all"}
    assert report_cases(splits) == (splits.val or splits.train)


def test_single_mode_uses_the_one_case_everywhere() -> None:
    """Single mode trains, selects and reports on its one case."""
    splits = split_cases(_cases(1), "single", SplitFractions(), shuffle=True, seed=1)
    assert _ids(splits.train) == _ids(splits.val) == _ids(splits.test) == [0]
    assert splits.counts.selection_split == "all"
    assert splits.counts.report_split == "all"
    assert splits.counts.held_out is False


def test_empty_val_selects_on_train() -> None:
    """With no validation fraction, selection falls back to the train cases."""
    splits = split_cases(_cases(10), "pairs", SplitFractions(train=0.8, val=0.0, test=0.2), shuffle=False, seed=0)
    assert splits.val == []
    assert splits.counts.selection_split == "train"
    assert selection_cases(splits) == splits.train


def test_contract_samples_never_take_test_cases() -> None:
    """Contract samples come from train then validation, never test."""
    splits = split_cases(_cases(20), "pairs", SplitFractions(), shuffle=True, seed=2)
    samples = contract_samples(splits, 20)
    assert len(samples) == 17
    assert not set(_ids(samples)) & set(_ids(splits.test))
    assert _ids(contract_samples(splits, 3)) == _ids(splits.train[:3])


def test_fingerprint_tracks_cases_objective_and_artifact() -> None:
    """The fingerprint changes with the cases but not with the run name."""
    base = _payload()
    renamed = base.model_copy(update={"name": "renamed"})
    fewer = _payload(cases=[case.model_dump() for case in _cases(9)])
    assert task_fingerprint(base) == task_fingerprint(renamed)
    assert task_fingerprint(base) != task_fingerprint(fewer)
    assert len(task_fingerprint(base)) == 64


def test_split_seed_prefers_the_explicit_seed() -> None:
    """An explicit seed wins; otherwise the seed derives from the fingerprint."""
    base = _payload()
    assert split_seed(base.model_copy(update={"seed": 42})) == 42
    assert split_seed(base) == split_seed(_payload())
    assert 0 <= split_seed(base) < 2**31
