"""Split a run's cases into train, validation and test, server side.

This is the one place the held-out split is decided. Engines receive the
train cases and the validation cases; selection (the winner and the
regression guard) runs on validation, falling back to train when there is
none; the reported scores are measured on test, falling back to validation.
Test cases never reach an engine or a proposer.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from ....models.blackbox import UnifiedSplitCounts
from ....models.common import SplitFractions
from ....models.unified import MIN_PAIRS_FOR_HELD_OUT, DataMode, UnifiedCase
from ...datasets.split_counts import split_counts


@dataclass(frozen=True)
class CaseSplits:
    """A run's cases split for search (train), selection (val) and reporting (test)."""

    train: list[UnifiedCase]
    val: list[UnifiedCase]
    test: list[UnifiedCase]
    counts: UnifiedSplitCounts


def split_cases(
    cases: list[UnifiedCase],
    mode: DataMode,
    fractions: SplitFractions,
    *,
    shuffle: bool,
    seed: int,
) -> CaseSplits:
    """Split ``cases`` deterministically for search, selection and reporting.

    Single mode (one case) uses that case everywhere and holds nothing out.
    In pairs mode the counts follow ``fractions`` (largest remainder, as the
    wizard shows them); below :data:`MIN_PAIRS_FOR_HELD_OUT` cases there is no
    test split and the train and validation fractions share every case.

    Args:
        cases: The run's cases, in submitted order.
        mode: ``"single"`` or ``"pairs"``.
        fractions: Train/val/test fractions summing to 1.0.
        shuffle: Whether to shuffle (with ``seed``) before slicing.
        seed: RNG seed for the shuffle.

    Returns:
        The three splits and their counts.
    """
    ordered = list(cases)
    if mode == "single" or len(ordered) <= 1:
        return CaseSplits(
            train=ordered,
            val=list(ordered),
            test=list(ordered),
            counts=UnifiedSplitCounts(
                train=len(ordered),
                val=len(ordered),
                test=len(ordered),
                held_out=False,
                selection_split="all",
                report_split="all",
            ),
        )
    if shuffle:
        random.Random(seed).shuffle(ordered)
    if len(ordered) < MIN_PAIRS_FOR_HELD_OUT:
        searchable = fractions.train + fractions.val
        fractions = (
            SplitFractions(train=fractions.train / searchable, val=fractions.val / searchable, test=0.0)
            if searchable > 0
            else SplitFractions(train=1.0, val=0.0, test=0.0)
        )
    counts = split_counts(len(ordered), fractions)
    val_end = counts.train + counts.val
    train, val, test = ordered[: counts.train], ordered[counts.train : val_end], ordered[val_end:]
    # Engines search on train; a split with no train fraction searches on
    # its validation cases and then selects on them too.
    if not train:
        train, val = val, []
    return CaseSplits(
        train=train,
        val=val,
        test=test,
        counts=UnifiedSplitCounts(
            train=len(train),
            val=len(val),
            test=len(test),
            held_out=bool(test),
            selection_split="val" if val else "train",
            report_split="test" if test else ("val" if val else "all"),
        ),
    )


def selection_cases(splits: CaseSplits) -> list[UnifiedCase]:
    """Return the cases the winner and the regression guard are chosen on.

    Args:
        splits: The run's splits.

    Returns:
        Validation cases, or the train cases when there are none.
    """
    return splits.val or splits.train


def report_cases(splits: CaseSplits) -> list[UnifiedCase]:
    """Return the cases the final scores are reported on.

    Args:
        splits: The run's splits.

    Returns:
        Test cases, else validation cases, else the train cases.
    """
    return splits.test or splits.val or splits.train
