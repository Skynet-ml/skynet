"""B-L2: GEPA state → CandidateEvent extraction contract.

The trajectory feature surfaces accepted GEPA candidates as progress events.
These tests pin the dict-shape that ``extract_candidates_from_state`` reads
from ``gepa_state.bin`` and the JSON-safe metrics dict it forwards to the
progress callback. Drift in either direction silently breaks the frontend
genealogy tree, so the tests live in the unit suite.
"""

from __future__ import annotations

import dataclasses
import pickle
import tempfile
from pathlib import Path

import cloudpickle
import dspy
import pytest
from gepa.proposer.base import CandidateProposal
from gepa.proposer.reflective_mutation.reflective_mutation import (
    ReflectiveMutationProposer,
)

from core.constants import PROGRESS_CANDIDATE
from core.service_gateway.optimization.trajectory import (
    GEPA_STATE_FILENAME,
    CandidateEvent,
    RejectedEvent,
    TrajectoryWatcher,
    _current_proposal_iteration,
    _load_state,
    capture_proposal_prompts,
    current_proposal_iteration,
    extract_candidates_from_state,
    extract_rejected_from_trace,
    trajectory_watch,
)
from core.storage.run_call_store import PROGRAM_EVENT_KEY


def _minimal_state() -> dict:
    """Return a state dict mirroring two iterations of GEPA acceptance.

    Returns:
        State dict with a seed candidate plus one accepted child.
    """
    return {
        "program_candidates": [
            {"qa.predict": "Answer the question."},
            {"qa.predict": "Answer the question step by step."},
        ],
        "parent_program_for_candidate": [[None], [0]],
        "prog_candidate_val_subscores": [
            {"ex1": 0.5, "ex2": 0.75},
            {"ex1": 0.6, "ex2": 1.0},
        ],
        "num_metric_calls_by_discovery": [0, 4],
    }


class TestExtractCandidates:
    """Lock the shape and ordering of CandidateEvents emitted from a state dict."""

    def test_seed_candidate_has_no_parent(self) -> None:
        """Index-0 candidate yields ``parent_id=None`` and generation 0."""
        events = extract_candidates_from_state(_minimal_state(), last_seen_count=0)
        assert events[0].id == "0"
        assert events[0].parent_id is None
        assert events[0].generation == 0

    def test_child_candidate_carries_parent_string(self) -> None:
        """Index-1 candidate exposes ``parent_id="0"`` and generation 1."""
        events = extract_candidates_from_state(_minimal_state(), last_seen_count=0)
        assert events[1].id == "1"
        assert events[1].parent_id == "0"
        assert events[1].generation == 1

    def test_score_is_mean_of_per_example(self) -> None:
        """``score`` averages the per-example subscores."""
        events = extract_candidates_from_state(_minimal_state(), last_seen_count=0)
        assert events[1].score == 0.8

    def test_last_seen_count_filters_emitted_events(self) -> None:
        """Only candidates with index ``>= last_seen_count`` are returned."""
        events = extract_candidates_from_state(_minimal_state(), last_seen_count=1)
        assert len(events) == 1
        assert events[0].id == "1"

    def test_no_new_candidates_returns_empty_list(self) -> None:
        """When the watcher has seen everything, the next call yields ``[]``."""
        assert extract_candidates_from_state(_minimal_state(), last_seen_count=2) == []

    def test_metrics_dict_is_json_safe(self) -> None:
        """``to_metrics()`` returns plain dicts/lists, no tuples or dataclasses."""
        events = extract_candidates_from_state(_minimal_state(), last_seen_count=0)
        metrics = events[1].to_metrics()
        assert metrics == {
            "candidate_id": "1",
            "parent_id": "0",
            "parents_extra": [],
            "generation": 1,
            "score": 0.8,
            "per_example": [{"id": "ex1", "score": 0.6}, {"id": "ex2", "score": 1.0}],
            "prompt": {"qa.predict": "Answer the question step by step."},
            "discovered_at_evals": 4,
            "iteration": None,
        }

    def test_iteration_is_populated_from_trace(self) -> None:
        """Accepted candidates surface the iteration that accepted them."""
        state = _minimal_state()
        state["full_program_trace"] = [
            {"i": 0, "new_program_idx": 1, "selected_program_candidate": 0},
        ]
        events = extract_candidates_from_state(state, last_seen_count=0)
        assert events[0].iteration is None
        assert events[1].iteration == 0

    def test_merge_candidate_exposes_extra_parents(self) -> None:
        """Multi-parent rows surface the first as primary, the rest in ``parents_extra``."""
        state = _minimal_state()
        state["program_candidates"].append({"qa.predict": "Merged answer."})
        state["parent_program_for_candidate"].append([0, 1])
        state["prog_candidate_val_subscores"].append({"ex1": 1.0, "ex2": 1.0})
        state["num_metric_calls_by_discovery"].append(8)

        events = extract_candidates_from_state(state, last_seen_count=2)
        assert events[0].parent_id == "0"
        assert events[0].parents_extra == ("1",)
        assert events[0].to_metrics()["parents_extra"] == ["1"]


def _trace_with_one_acceptance_one_rejection() -> list[dict]:
    """Return a ``full_program_trace`` with iter-0 accepted and iter-1 rejected.

    Returns:
        Trace list mirroring two GEPA iterations: the first proposal was
        accepted (carries ``new_program_idx``), the second was discarded.
    """
    return [
        {
            "selected_program_candidate": 0,
            "subsample_ids": [0, 1, 2],
            "subsample_scores": [0.0, 1.0, 0.5],
            "new_subsample_scores": [1.0, 1.0, 1.0],
            "new_program_idx": 1,
        },
        {
            "selected_program_candidate": 1,
            "subsample_ids": [3, 4, 5],
            "subsample_scores": [1.0, 1.0, 1.0],
            "new_subsample_scores": [0.0, 1.0, 1.0],
        },
    ]


class TestExtractRejected:
    """Lock the shape and ordering of RejectedEvents reconstructed from full_program_trace."""

    def test_skips_iterations_with_new_program_idx(self) -> None:
        """Accepted iterations (``new_program_idx`` set) must not produce rejections."""
        state = {"full_program_trace": _trace_with_one_acceptance_one_rejection()}
        events = extract_rejected_from_trace(state, last_seen_iteration=-1)
        assert len(events) == 1
        assert events[0].iteration == 1

    def test_rejection_carries_parent_and_scores(self) -> None:
        """A rejected iteration emits the parent id and mean scores."""
        state = {"full_program_trace": _trace_with_one_acceptance_one_rejection()}
        event = extract_rejected_from_trace(state, last_seen_iteration=-1)[0]
        assert event.rejection_id == "r1"
        assert event.parent_id == "1"
        assert event.parent_score == pytest.approx(1.0)
        assert event.proposal_score == pytest.approx((0.0 + 1.0 + 1.0) / 3)
        assert event.subsample_size == 3

    def test_last_seen_iteration_filters_out_old_rejections(self) -> None:
        """Re-polls after ``last_seen_iteration=N`` skip iterations ``<=N``."""
        state = {"full_program_trace": _trace_with_one_acceptance_one_rejection()}
        assert extract_rejected_from_trace(state, last_seen_iteration=1) == []

    def test_missing_subsample_scores_is_skipped(self) -> None:
        """Entries without ``new_subsample_scores`` (e.g. setup-only) are skipped."""
        state = {
            "full_program_trace": [
                {
                    "selected_program_candidate": 0,
                    "subsample_ids": [0],
                    "subsample_scores": [0.5],
                }
            ]
        }
        assert extract_rejected_from_trace(state, last_seen_iteration=-1) == []

    def test_metrics_dict_is_json_safe(self) -> None:
        """``to_metrics()`` returns plain numerics and strings only."""
        state = {"full_program_trace": _trace_with_one_acceptance_one_rejection()}
        metrics = extract_rejected_from_trace(state, last_seen_iteration=-1)[0].to_metrics()
        assert metrics == {
            "rejection_id": "r1",
            "iteration": 1,
            "parent_id": "1",
            "parent_score": 1.0,
            "proposal_score": pytest.approx((0.0 + 1.0 + 1.0) / 3),
            "subsample_size": 3,
            "proposal_prompt": {},
            "parent_prompt": {},
            "subsample_ids": ["3", "4", "5"],
            "per_example_parent": [
                {"id": "3", "score": 1.0},
                {"id": "4", "score": 1.0},
                {"id": "5", "score": 1.0},
            ],
            "per_example_proposal": [
                {"id": "3", "score": 0.0},
                {"id": "4", "score": 1.0},
                {"id": "5", "score": 1.0},
            ],
        }

    def test_prompts_are_surfaced_when_snapshots_are_present(self) -> None:
        """``capture_proposal_prompts`` writes snapshots that the extractor reads."""
        trace = _trace_with_one_acceptance_one_rejection()
        trace[1]["parent_prompt_snapshot"] = {"qa": "Answer the question."}
        trace[1]["proposed_prompt_snapshot"] = {"qa": "Answer step by step."}
        event = extract_rejected_from_trace({"full_program_trace": trace}, last_seen_iteration=-1)[0]
        assert event.parent_prompt == {"qa": "Answer the question."}
        assert event.proposal_prompt == {"qa": "Answer step by step."}

    def test_missing_trace_returns_empty_list(self) -> None:
        """A state without ``full_program_trace`` yields ``[]``."""
        assert extract_rejected_from_trace({}, last_seen_iteration=-1) == []


class TestRejectedEvent:
    """Lock immutability invariants on the dataclass."""

    def test_is_frozen(self) -> None:
        """``RejectedEvent`` is immutable like ``CandidateEvent``."""
        event = RejectedEvent(
            rejection_id="r0",
            iteration=0,
            parent_id="0",
            parent_score=0.0,
            proposal_score=0.0,
            subsample_size=1,
            proposal_prompt={},
            parent_prompt={},
            subsample_ids=(),
            per_example_parent=(),
            per_example_proposal=(),
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            event.iteration = 99  # type: ignore[misc]


class TestCandidateEvent:
    """Lock the dataclass invariants the consumer relies on."""

    def test_is_frozen(self) -> None:
        """``CandidateEvent`` is immutable so producers can't mutate after dispatch."""
        event = CandidateEvent(
            id="0",
            parent_id=None,
            parents_extra=(),
            generation=0,
            score=0.0,
            per_example=(),
            prompt={},
            discovered_at_evals=0,
            iteration=None,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            event.id = "999"  # type: ignore[misc]


class TestLoadState:
    """Both cloudpickle and stdlib pickle payloads round-trip; partials return None."""

    def test_loads_cloudpickle_payload(self) -> None:
        """Files written via ``cloudpickle.dump`` are decoded into the state dict."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gepa_state.bin"
            with path.open("wb") as fh:
                cloudpickle.dump(_minimal_state(), fh)
            loaded = _load_state(path)
            assert isinstance(loaded, dict)
            assert loaded["program_candidates"] == _minimal_state()["program_candidates"]

    def test_loads_stdlib_pickle_payload(self) -> None:
        """Stdlib pickle is the fallback when cloudpickle.loads fails."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gepa_state.bin"
            with path.open("wb") as fh:
                pickle.dump(_minimal_state(), fh)
            loaded = _load_state(path)
            assert isinstance(loaded, dict)

    def test_truncated_file_returns_none(self) -> None:
        """Mid-write race produces a non-deserialisable file → ``None`` (no raise)."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gepa_state.bin"
            with path.open("wb") as fh:
                fh.write(b"not-a-pickle")
            assert _load_state(path) is None

    def test_missing_file_returns_none(self) -> None:
        """Pre-first-iteration polls don't raise when the file is absent."""
        assert _load_state("/tmp/__definitely_missing_gepa_state__.bin") is None


class TestTrajectoryWatchNoop:
    """When either input is missing, ``trajectory_watch`` is a pure no-op."""

    def test_skips_when_log_dir_none(self) -> None:
        """Non-GEPA optimizers pass ``None`` log_dir; the watcher must not start."""
        events: list[tuple] = []

        def callback(event, metrics):
            """Record each forwarded ``(event, metrics)`` pair."""
            events.append((event, metrics))

        with trajectory_watch(None, callback):
            pass
        assert events == []

    def test_skips_when_callback_none(self) -> None:
        """Runs without a progress callback have nothing to forward to."""
        with tempfile.TemporaryDirectory() as tmp, trajectory_watch(tmp, None):
            pass


class TestCaptureProposalPrompts:
    """The propose() wrapper writes prompt snapshots onto the latest trace entry."""

    def test_noop_for_non_gepa_optimizer(self) -> None:
        """Non-GEPA optimizers don't have a reflective proposer — context is a no-op."""
        with capture_proposal_prompts("bootstrap_fewshot"):
            pass

    def test_writes_snapshots_after_proposer_returns(self) -> None:
        """A captured ``propose`` call leaves parent + proposed prompt in the trace."""

        class _FakeState:
            """Minimal stand-in for GEPAState that exposes the fields we touch."""

            def __init__(self) -> None:
                """Seed candidate and trace fields the wrapped propose call reads."""
                self.program_candidates = [{"qa": "Answer the question."}]
                self.full_program_trace: list[dict] = [{"i": 0, "selected_program_candidate": 0}]

        captured_state = _FakeState()
        proposed_text = {"qa": "Answer in detail."}

        def fake_original(self: object, state: object) -> CandidateProposal:
            """Pretend to be the original propose method, returning the proposed candidate."""
            return CandidateProposal(
                candidate=proposed_text,
                parent_program_ids=[0],
                subsample_indices=[0],
                subsample_scores_before=[0.5],
                subsample_scores_after=[0.4],
                tag="reflective_mutation",
            )

        original = ReflectiveMutationProposer.propose
        ReflectiveMutationProposer.propose = fake_original  # type: ignore[method-assign]
        try:
            with capture_proposal_prompts("gepa"):
                ReflectiveMutationProposer.propose(None, captured_state)  # type: ignore[arg-type]
        finally:
            ReflectiveMutationProposer.propose = original  # type: ignore[method-assign]

        entry = captured_state.full_program_trace[-1]
        assert entry["parent_prompt_snapshot"] == {"qa": "Answer the question."}
        assert entry["proposed_prompt_snapshot"] == {"qa": "Answer in detail."}

    def test_restores_original_propose_after_context_exits(self) -> None:
        """Wrapper must unhook itself even when the inner block raises."""
        original = ReflectiveMutationProposer.propose
        with capture_proposal_prompts("gepa"):
            assert ReflectiveMutationProposer.propose is not original
        assert ReflectiveMutationProposer.propose is original

    def test_iteration_contextvar_is_set_to_state_i_during_propose(self) -> None:
        """Inside the wrapped propose ``_current_proposal_iteration`` matches ``state.i``."""

        class _FakeState:
            """Stand-in for GEPAState carrying only the fields wrapped_propose reads."""

            def __init__(self) -> None:
                """Seed iteration counter plus the candidate and trace lists."""
                self.i = 11
                self.program_candidates = [{"qa": "p"}]
                self.full_program_trace: list[dict] = [{"i": 11, "selected_program_candidate": 0}]

        seen: dict[str, int | None] = {"value": -1}

        def fake_original(self: object, state: object) -> CandidateProposal | None:
            """Record the contextvar visible to a metric call running inside propose."""
            seen["value"] = _current_proposal_iteration.get()
            return None

        original = ReflectiveMutationProposer.propose
        ReflectiveMutationProposer.propose = fake_original  # type: ignore[method-assign]
        try:
            with capture_proposal_prompts("gepa"):
                ReflectiveMutationProposer.propose(None, _FakeState())  # type: ignore[arg-type]
        finally:
            ReflectiveMutationProposer.propose = original  # type: ignore[method-assign]

        assert seen["value"] == 11
        assert _current_proposal_iteration.get() is None

    def test_iteration_reaches_dspy_worker_threads(self) -> None:
        """Calls DSPy's parallel evaluator makes inside propose see the iteration too."""

        class _FakeState:
            """Stand-in for GEPAState with an iteration and no trace."""

            i = 4
            program_candidates: list[dict] = []
            full_program_trace: list[dict] = []

        seen: list[int | None] = []

        def fake_original(self: object, state: object) -> CandidateProposal | None:
            """Read the iteration from worker threads, as a parent-minibatch evaluation would."""
            executor = dspy.utils.parallelizer.ParallelExecutor(num_threads=2, disable_progress_bar=True)
            seen.extend(executor.execute(lambda _: current_proposal_iteration(), [1, 2]))
            return None

        original = ReflectiveMutationProposer.propose
        ReflectiveMutationProposer.propose = fake_original  # type: ignore[method-assign]
        try:
            with capture_proposal_prompts("gepa"):
                ReflectiveMutationProposer.propose(None, _FakeState())  # type: ignore[arg-type]
        finally:
            ReflectiveMutationProposer.propose = original  # type: ignore[method-assign]

        assert seen == [4, 4]
        assert current_proposal_iteration() is None


class TestResumeBaseline:
    """On resume, the watcher must not re-emit candidates the restored state holds."""

    @staticmethod
    def _dump_state(run_dir: str, state: dict) -> None:
        """Write ``state`` to ``<run_dir>/gepa_state.bin`` as GEPA would."""
        with (Path(run_dir) / GEPA_STATE_FILENAME).open("wb") as fh:
            cloudpickle.dump(state, fh)

    def test_no_state_file_is_a_noop(self) -> None:
        """A fresh run (no seeded state) leaves the cursor at zero."""
        with tempfile.TemporaryDirectory() as run_dir:
            watcher = TrajectoryWatcher(run_dir, lambda _m, _x: None)
            watcher._prime_resume_baseline()
            assert watcher._last_count == 0

    def test_prime_skips_seeded_candidates_then_emits_only_new(self) -> None:
        """Priming swallows the seeded candidates; only post-resume ones emit."""
        emitted: list[tuple[str, dict]] = []
        with tempfile.TemporaryDirectory() as run_dir:
            self._dump_state(run_dir, _minimal_state())  # 2 prior-segment candidates
            watcher = TrajectoryWatcher(run_dir, lambda m, x: emitted.append((m, x)))

            watcher._prime_resume_baseline()
            assert watcher._last_count == 2

            # A forced tick over the unchanged state must emit nothing.
            watcher._tick(force=True)
            assert [m for m, _ in emitted if m == PROGRESS_CANDIDATE] == []

            # GEPA appends a candidate after resume → only that one is emitted.
            state = _minimal_state()
            state["program_candidates"].append({"qa.predict": "Third."})
            state["parent_program_for_candidate"].append([1])
            state["prog_candidate_val_subscores"].append({"ex1": 0.9, "ex2": 0.9})
            state["num_metric_calls_by_discovery"].append(8)
            self._dump_state(run_dir, state)

            watcher._tick(force=True)
            candidates = [x for m, x in emitted if m == PROGRESS_CANDIDATE]
            assert len(candidates) == 1
            assert candidates[0]["candidate_id"] == "2"
            # The run record keys a program event on its iteration, not on the program index.
            assert candidates[0][PROGRAM_EVENT_KEY] is True
