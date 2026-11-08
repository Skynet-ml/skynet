"""The Check's live sandbox transcript: redacted, bounded, phased and per sample."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.billing.model_mailbox import ModelMailbox
from core.service_gateway.optimization.blackbox import check_terminal
from core.service_gateway.optimization.blackbox.check_terminal import CheckTerminalRecorder
from core.service_gateway.optimization.blackbox.runner import EVENT_PREFIX
from core.service_gateway.optimization.blackbox.sandbox import CommandResult, LocalSubprocessSession, SandboxSpec
from core.service_gateway.optimization.blackbox.workspace_harness import REPLY_PREFIX
from core.service_gateway.optimization.blackbox.workspace_scorer import WorkspaceProbe, WorkspaceScorer
from core.storage.preflights import preflight_document

PROVIDER_KEY = "sk-or-v1-provider-key-0123456789"
GATEWAY_TOKEN = "scoped-gateway-token-abcdef987654"
ENV_VALUE = "env-only-value-5551234"


def _text(recorder: CheckTerminalRecorder) -> str:
    """Return every stored and emitted byte of the transcript as one string."""
    return json.dumps(recorder.snapshot())


def test_no_secret_reaches_the_transcript_live_or_stored() -> None:
    """Provider keys, the scoped gateway token and env values are redacted before they leave the server."""
    emitted: list[dict[str, Any]] = []
    recorder = CheckTerminalRecorder(emit=emitted.append)
    recorder.redact(
        check_terminal.secret_values(
            {
                "models": {"reflection_model": {"extra": {"_skynet_route": {"url": "u", "token": GATEWAY_TOKEN}}}},
                "provider": {"api_key": PROVIDER_KEY},
            }
        )
    )
    session = LocalSubprocessSession(SandboxSpec(lifetime_seconds=60))
    command = (
        f'echo "key={PROVIDER_KEY}"; echo "token {GATEWAY_TOKEN}" >&2; '
        f'echo "env $SECRET_VALUE"; echo "{REPLY_PREFIX}{{\\"token\\": 1}}"'
    )
    try:
        with check_terminal.recording(recorder):
            recorder.set_phase("sandbox")
            result = session.run(command, env={"SECRET_VALUE": ENV_VALUE})
    finally:
        session.close()

    assert result.ok
    stored = _text(recorder)
    live = json.dumps(emitted)
    for secret in (PROVIDER_KEY, GATEWAY_TOKEN, ENV_VALUE):
        assert secret not in stored
        assert secret not in live
    lines = [event["text"] for event in recorder.snapshot()["events"] if event["kind"] == "output"]
    assert sorted(lines) == ["env [redacted]", "key=[redacted]", "token [redacted]"]
    assert not any(REPLY_PREFIX.strip() in line for line in lines)


def test_a_secret_split_across_chunks_is_still_redacted() -> None:
    """Output is matched per complete line, so a chunk boundary cannot cut a secret in two."""
    recorder = CheckTerminalRecorder()
    recorder.redact([GATEWAY_TOKEN])
    command_id = recorder.command_started("python3 app_workspace_harness.py --env", scrub=lambda text: text)
    half = len(GATEWAY_TOKEN) // 2
    recorder.command_output(command_id, "stderr", f"Bearer {GATEWAY_TOKEN[:half]}")
    recorder.command_output(command_id, "stderr", f"{GATEWAY_TOKEN[half:]}\ntail")
    recorder.command_finished(command_id, exit_code=1)

    outputs = [event["text"] for event in recorder.snapshot()["events"] if event["kind"] == "output"]
    assert outputs == ["Bearer [redacted]", "tail"]
    assert GATEWAY_TOKEN not in _text(recorder)


def test_commands_carry_their_phase_exit_and_duration_and_log_frames_show_their_message() -> None:
    """Each command opens, streams and closes under its row; ``log()`` frames show as plain messages."""
    recorder = CheckTerminalRecorder()
    command_id = recorder.command_started("pip install x", scrub=lambda text: text)
    recorder.set_phase("evaluator")
    frame = EVENT_PREFIX + json.dumps({"message": "scored 0.5", "level": "INFO"})
    recorder.command_output(command_id, "stderr", f"{frame}\n")
    recorder.command_finished(command_id, exit_code=0)

    command, output, finish = recorder.snapshot()["events"]
    assert (command["kind"], command["phase"], command["label"]) == ("command", "budget", "pip install x")
    assert (output["stream"], output["text"], output["phase"]) == ("log", "scored 0.5", "budget")
    assert (finish["kind"], finish["exit_code"]) == ("exit", 0)
    assert finish["duration_ms"] >= 0


def test_each_phase_has_a_bounded_output_budget() -> None:
    """A chatty phase stops at its budget with one truncation note; long lines are clipped."""
    recorder = CheckTerminalRecorder()
    recorder.set_phase("sandbox")
    command_id = recorder.command_started("yes", scrub=lambda text: text)
    line = "x" * (check_terminal.LINE_MAX_CHARS * 2)
    recorder.command_output(command_id, "stdout", (line + "\n") * 200)
    recorder.command_finished(command_id, exit_code=0)

    snapshot = recorder.snapshot()
    outputs = [event["text"] for event in snapshot["events"] if event["kind"] == "output"]
    assert all(len(text) <= check_terminal.LINE_MAX_CHARS + 1 for text in outputs)
    assert sum(len(text) for text in outputs) <= check_terminal.PHASE_MAX_OUTPUT_CHARS
    assert [event["note"] for event in snapshot["events"] if event["kind"] == "note"] == ["output_truncated"]
    assert snapshot["truncated"] == ["sandbox"]


def test_the_event_cap_never_drops_sample_or_exit_events() -> None:
    """A flood of short lines spends the output cap, but sample and exit events still arrive."""
    recorder = CheckTerminalRecorder()
    recorder.sample_started(0, 1)
    command_id = recorder.command_started("seq 5000", scrub=lambda text: text, sample=0)
    recorder.command_output(command_id, "stdout", "1\n" * (check_terminal.MAX_EVENTS + 1000))
    recorder.command_finished(command_id, exit_code=0)
    recorder.sample_finished(0, 1, score=1.0)

    events = recorder.snapshot()["events"]
    assert sum(event["kind"] == "output" for event in events) == check_terminal.MAX_EVENTS
    assert [event["kind"] for event in events[-2:]] == ["exit", "sample"]
    assert events[-1]["state"] == "done"
    assert recorder.snapshot()["truncated"] == ["evaluator"]


def test_no_output_follows_the_truncation_note() -> None:
    """A shorter line that would still fit the phase budget is dropped once the phase is truncated."""
    recorder = CheckTerminalRecorder()
    recorder.set_phase("sandbox")
    command_id = recorder.command_started("chatty", scrub=lambda text: text)
    filler = "x" * (check_terminal.LINE_MAX_CHARS - 10)
    lines = check_terminal.PHASE_MAX_OUTPUT_CHARS // len(filler)
    recorder.command_output(command_id, "stdout", (filler + "\n") * lines)
    recorder.command_output(command_id, "stdout", "y" * check_terminal.LINE_MAX_CHARS + "\nafter\n")
    recorder.command_finished(command_id, exit_code=0)

    kinds = [event.get("note") or event.get("text") for event in recorder.snapshot()["events"]]
    assert "output_truncated" in kinds
    assert "after" not in kinds


def test_a_line_without_a_newline_is_held_only_to_its_budget() -> None:
    """Output that never ends its line (a progress bar) keeps a bounded buffer and is still clipped."""
    recorder = CheckTerminalRecorder()
    command_id = recorder.command_started("progress", scrub=lambda text: text)
    for _ in range(200):
        recorder.command_output(command_id, "stdout", "#" * 1000)
    assert len(recorder._commands[command_id].pending["stdout"]) <= check_terminal.PENDING_MAX_CHARS
    recorder.command_finished(command_id, exit_code=0)

    outputs = [event["text"] for event in recorder.snapshot()["events"] if event["kind"] == "output"]
    assert outputs == ["#" * check_terminal.LINE_MAX_CHARS + "…"]


def test_run_many_reports_each_sample_and_pins_its_commands_to_it() -> None:
    """Concurrent samples report their own start and outcome, and their commands carry the sample index."""
    recorder = CheckTerminalRecorder()
    gate = threading.Barrier(3)

    def probe(candidate: Any, case: Any) -> WorkspaceProbe:
        """Run one fake harness command, then fail the second case the way a raising scorer does."""
        gate.wait(timeout=5)
        current = check_terminal.current_recorder()
        assert current is recorder
        command_id = current.command_started(
            "python3 harness", scrub=lambda text: text, sample=check_terminal.current_sample()
        )
        current.command_output(command_id, "stdout", f"case {case}\n")
        current.command_finished(command_id, exit_code=0)
        if case == 1:
            return WorkspaceProbe(score=None, side_info={}, error="ValueError: bad metric", stage="metric")
        return WorkspaceProbe(score=0.5, side_info={}, duration_ms=12)

    scorer = object.__new__(WorkspaceScorer)
    scorer._lock = threading.Lock()
    scorer._extend = lambda candidate: None
    scorer._open = lambda: None
    scorer._probe = probe
    with check_terminal.recording(recorder):
        probes = scorer.run_many("p", [0, 1, 2])

    assert [probe.score for probe in probes] == [0.5, None, 0.5]
    events = recorder.snapshot()["events"]
    finished = {event["sample"]: event for event in events if event["kind"] == "sample" and event["state"] != "running"}
    assert (finished[1]["state"], finished[1]["error"]) == ("failed", "ValueError: bad metric")
    assert (finished[0]["state"], finished[0]["score"], finished[0]["total"]) == ("done", 0.5, 3)
    outputs = {event["sample"]: event["text"] for event in events if event["kind"] == "output"}
    assert outputs == {0: "case 0", 1: "case 1", 2: "case 2"}
    assert {event["phase"] for event in events} == {"evaluator"}


def test_stored_evidence_keeps_the_transcript_for_a_reused_check() -> None:
    """The public evidence document carries the stored transcript, so a deduplicated check can replay it."""
    transcript = {"version": 1, "events": [{"t": 0, "phase": "budget", "kind": "note", "note": "budget_ready"}]}
    row = SimpleNamespace(
        id="p1",
        fingerprint="f",
        status="succeeded",
        result={"checks": [{"key": "metric.contract", "status": "succeeded"}], "terminal": transcript},
    )

    assert preflight_document(row)["terminal"] == transcript


def test_a_command_without_a_recorder_records_nothing() -> None:
    """Outside a check, sandboxed commands run exactly as before."""
    assert check_terminal.current_recorder() is None
    check_terminal.note("box_opening")
    result = CommandResult(exit_code=0, stdout="", stderr="")
    assert result.ok


def test_model_mailbox_plumbing_never_reaches_the_transcript() -> None:
    """A mailbox-wrapped command shows the command it wraps, and its nonce frames are hidden."""
    nonce = "0123456789abcdef0123456789abcdef"
    wrapped = (
        f"python3 -I .app-model-{nonce}/proxy.py .app-model-{nonce}/config.json "
        "bash --noprofile --norc -c 'python3 app_workspace_harness.py --run'"
    )
    recorder = CheckTerminalRecorder()
    command_id = recorder.command_started(wrapped, scrub=lambda text: text)
    frame = f'APP_MODEL_{nonce}:{{"headers": {{"authorization": "Bearer x"}}}}'
    recorder.command_output(command_id, "stdout", f"{frame}\nscore 1.0\n")
    recorder.command_finished(command_id, exit_code=0)

    events = recorder.snapshot()["events"]
    assert events[0]["label"] == "python3 app_workspace_harness.py --run"
    assert [event["text"] for event in events if event["kind"] == "output"] == ["score 1.0"]
    assert "APP_MODEL_" not in _text(recorder)


def test_a_planted_shell_profile_never_runs_through_the_model_mailbox_wrapper() -> None:
    """The mailbox wrapper runs its command with no profile, and the transcript still shows that command (E38)."""
    session = LocalSubprocessSession(SandboxSpec(lifetime_seconds=60))
    commands: list[str] = []
    run = session.run

    def recording_run(command: str, **kwargs: Any) -> CommandResult:
        """Record the wrapped command line, then run it."""
        commands.append(command)
        return run(command, **kwargs)

    session.run = recording_run  # type: ignore[method-assign]
    try:
        for name in (".bash_profile", ".bash_login", ".profile", ".bashrc"):
            (session.path / name).write_text("echo ran >> profile-ran\n")

        result = ModelMailbox(lambda *args: None).run(session, "printf ok", env={"SSH_CLIENT": "10.0.0.1 1 22"})

        assert result.stdout == "ok"
        assert not (session.path / "profile-ran").exists()
        recorder = CheckTerminalRecorder()
        recorder.command_started(commands[0], scrub=lambda text: text)
        assert recorder.snapshot()["events"][0]["label"] == "printf ok"
    finally:
        session.close()
    assert ".app-model-" not in _text(recorder)


def test_a_planted_user_site_pth_never_runs_in_the_model_mailbox_wrapper(tmp_path: Path) -> None:
    """The wrapper's python3 skips user site-packages, so a .pth that a version wrote there never runs (E38)."""
    base = getattr(sys, "_base_executable", sys.executable)
    probe = subprocess.run(
        [base, "-c", "import site; print(site.ENABLE_USER_SITE)"], capture_output=True, text=True, check=False
    )
    if probe.stdout.strip() != "True":
        pytest.skip("no interpreter with user site-packages on this host")
    # The test venv turns user site-packages off; a box python3 has them on.
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "python3").symlink_to(base)
    session = LocalSubprocessSession(SandboxSpec(lifetime_seconds=60))
    try:
        user_site = subprocess.run(
            [base, "-c", "import site; print(site.getusersitepackages())"],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "HOME": str(session.path)},
        ).stdout.strip()
        assert user_site.startswith(str(session.path))
        Path(user_site).mkdir(parents=True)
        marker = session.path / "pth-ran"
        (Path(user_site) / "plant.pth").write_text(f"import pathlib; pathlib.Path({str(marker)!r}).touch()\n")

        path = os.pathsep.join([str(shim), "/usr/bin", "/bin"])
        result = ModelMailbox(lambda *args: None).run(session, "printf ok", env={"PATH": path})

        assert result.stdout == "ok"
        assert not marker.exists()
    finally:
        session.close()
