"""Verify metric source never reaches an optimizer or candidate code: payloads, workspaces, feedback and boxes."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import posixpath
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from core.billing.vercel_usage import PACKAGE_REGISTRY_HOSTS
from core.exceptions import ServiceError

from .. import native_engines, native_runtime, runner
from ..native_runtime import NATIVE_TASK_FIELDS, assert_metric_absent, native_task_payload, run_native_engine
from ..protocol import EvalServer, Task
from ..repo_scorer import RepoScoreError, RepoScorer, _StageRelay
from ..repo_workspace import METRIC_DIR, METRIC_REPO_DIR, RepoWorkspace
from ..sandbox import AsyncBridgeRuntime, CommandResult, LocalSubprocessRuntime, SandboxSpec
from ..sandbox_scorer import RUNNER_FILE, SandboxPythonScorer
from .test_native_runtime import FakeRuntime, FakeSession, _context
from .test_repo_workspace import _archive

SENTINEL = "METRIC_SENTINEL_7f3a9c"
VAL_SENTINEL = "VALSET_SENTINEL_51d0"
METRIC_CODE = (
    "def score(input, output, expected=None):\n"
    f"    marker = '{SENTINEL}'\n"
    "    return float(output == expected), {'feedback': 'compared'}\n"
)
TRACEBACK_ERROR = (
    "Traceback (most recent call last):\n"
    '  File "<metric>", line 2, in score\n'
    f"    marker = '{SENTINEL}'\n"
    "ValueError: bad output"
)


def _task() -> Task:
    """Build a unified-like task whose private context holds the metric.

    Returns:
        A task with train cases, a validation set and the metric in context.
    """
    return Task(
        "seed",
        objective="Answer briefly",
        background="Support desk",
        cases=[{"input": {"q": "a"}, "expected": "A"}],
        valset=[{"input": {"q": VAL_SENTINEL}, "expected": "B"}],
        context={"metric_code": METRIC_CODE, "run_code": "def run(artifact, input):\n    return artifact\n"},
    )


def test_native_task_payload_holds_only_the_train_fields() -> None:
    """Ship seed, objective, background and train case inputs, never expected, context or validation cases."""
    task = _task()
    document = native_task_payload("meta_harness", task)

    assert set(document) == NATIVE_TASK_FIELDS
    assert document["train_set"] == [{"id": 0, "input": {"q": "a"}}]
    text = json.dumps(document)
    assert SENTINEL not in text
    assert VAL_SENTINEL not in text
    assert "metric_code" not in text


def test_native_payload_guard_refuses_a_document_with_the_metric() -> None:
    """Stop before writing a box file that would carry the metric source."""
    with pytest.raises(ServiceError, match="metric source"):
        assert_metric_absent(json.dumps({"leak": METRIC_CODE.strip()}), _task())
    assert_metric_absent(json.dumps({"fine": "seed"}), _task())


def test_error_side_info_is_one_line_without_source() -> None:
    """Reduce a scorer traceback to its ``Type: message`` line."""
    assert native_runtime.sanitize_side_info({"error": TRACEBACK_ERROR, "feedback": SENTINEL}) == {
        "error": "ValueError: bad output"
    }
    assert native_runtime.sanitize_side_info({"feedback": "ok"}) == {"feedback": "ok"}


def test_native_flow_never_writes_the_metric_into_the_box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Grep every file a native engine's box receives, RPC answers included, for the metric.

    Args:
        tmp_path: Artifact destination.
        monkeypatch: Replaces the pinned source archive.
    """
    monkeypatch.setattr(native_runtime, "_source_archive", lambda: "source")
    session = FakeSession()

    def score(candidate: str, example: Any) -> tuple[float, dict[str, Any]]:
        """Fail the way a broken metric does, with its source in the error."""
        return 0.0, {"error": TRACEBACK_ERROR}

    run_native_engine("meta_harness", _task(), EvalServer(score, max_evals=3), _context(tmp_path, FakeRuntime(session)))

    assert session.files
    for path, text in session.files.items():
        assert SENTINEL not in text, path
        assert VAL_SENTINEL not in text, path
    [answer] = [text for path, text in session.files.items() if path.startswith("rpc/")]
    assert json.loads(answer)["info"] == {"error": "ValueError: bad output"}


def test_proposer_environment_drops_scorer_variables() -> None:
    """Keep the harness key and paths a proposer needs, drop scorer and nested-session variables."""
    env = native_engines.proposer_environment(
        {
            "PATH": "/bin",
            "APP_API_KEY": "route",
            "SKYNET_SCORER_DIR": "/private/scorer",
            "SKYNET_METRIC_PATH": "/private/metric.py",
            "CLAUDECODE": "1",
            "ANTHROPIC_API_KEY": "raw",
        }
    )

    assert env == {"PATH": "/bin", "APP_API_KEY": "route"}


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="Directory modes do not bind root.")
def test_local_proposer_workspace_cannot_find_scorer_files() -> None:
    """Search a proposer's whole reachable temp tree and environment for the scorer's metric."""
    scorer = SandboxPythonScorer(METRIC_CODE, runtime=LocalSubprocessRuntime(), gateway=None, timeout_seconds=30)
    proposer = LocalSubprocessRuntime().open(SandboxSpec(lifetime_seconds=60))
    try:
        scorer_box = scorer._open()
        scorer_dir = scorer_box.path
        assert (scorer_dir / runner.CODE_FILE).read_text(encoding="utf-8") == METRIC_CODE
        assert (scorer_dir / RUNNER_FILE).is_file()
        assert not scorer_dir.parent.samefile(proposer.path.parent)
        result = proposer.run(
            f"env; pwd; grep -rl {SENTINEL} {tempfile.gettempdir()} 2>/dev/null; true",
            timeout_seconds=60,
        )
        assert SENTINEL not in result.stdout
        assert str(scorer_dir) not in result.stdout
        assert str(scorer_dir.parent) not in result.stdout
    finally:
        proposer.close()
        scorer.close()


def test_scorer_calls_never_repeat_the_source() -> None:
    """Score through the real runner and confirm no call directory holds the metric."""
    scorer = SandboxPythonScorer(
        f"def score(candidate, case=None):\n    return 1.0, {{'feedback': 'ok {len(SENTINEL)}'}}  # {SENTINEL}\n",
        runtime=LocalSubprocessRuntime(),
        gateway=None,
        timeout_seconds=30,
    )
    try:
        assert scorer("x", {"input": "i"})[0] == 1.0
        assert scorer("y", {"input": "j"})[0] == 1.0
        calls = scorer._open().path / "calls"
        files = [path for path in calls.rglob("*") if path.is_file()]
        assert len(files) == 4
        for path in files:
            assert SENTINEL not in path.read_text(encoding="utf-8"), path
    finally:
        scorer.close()


# --- The candidate box C and the metric box M of a repository evaluation (SKY-398) ---

_HIDDEN = ("eval/metric.py",)
_METRIC_FILE = f"EXPECTED = '{SENTINEL}'\n"
_REQUIREMENTS = "tinypkg==1.0\n"
_TREE = {
    "src/app.py": "def run(value):\n    return {'doubled': value * 2}\n",
    "eval/metric.py": _METRIC_FILE,
    ".optimize/metric-requirements.txt": _REQUIREMENTS,
}
# The metric's own source carries the sentinel too, so a leak of the code shows as well as a leak of the file.
_REPO_METRIC = (
    "import os\n"
    "def score(repo_path, case, output):\n"
    f"    # {SENTINEL}\n"
    "    text = open(os.path.join(repo_path, 'eval/metric.py')).read()\n"
    f"    found = 'sentinel' if {SENTINEL!r} in text else 'missing'\n"
    "    return float(output['doubled']) / 10, found\n"
)


class _Recorded:
    """Run commands in a local box, log each open, write, run and close, and keep the files at close."""

    def __init__(self, runtime: _Recorder, inner: Any, spec: SandboxSpec) -> None:
        """Wrap a local box.

        Args:
            runtime: The recorder that keeps the ordered log.
            inner: The local box commands run in.
            spec: The spec the box opened with.
        """
        self._runtime = runtime
        self._inner = inner
        self.spec = spec
        # ``op:candidate-2`` names a candidate box; the number is the shared counter.
        self.kind = (spec.operation_key or "").rsplit(":", 1)[-1].rsplit("-", 1)[0]
        self.writes: dict[str, str] = {}
        self.files: dict[str, bytes] = {}
        self.closed = False
        runtime.log(self, "open")

    @property
    def path(self) -> Path:
        """Return the box's folder on the host."""
        return self._inner.path

    def write_files(self, files: dict[str, str]) -> None:
        """Record the files, then write them into the box."""
        self.writes.update(files)
        for name in files:
            self._runtime.log(self, "write", name)
        self._inner.write_files(files)

    def read_file(self, path: str) -> str | None:
        """Read from the box."""
        return self._inner.read_file(path)

    def run(self, command: str, **options: Any) -> CommandResult:
        """Record the command, and answer the metric setup's pip install without the network.

        Args:
            command: The shell command.
            **options: The run options.

        Returns:
            The command's result.
        """
        self._runtime.log(self, "run", command)
        if "pip install" in command:
            self._runtime.installs.append((command, self._inner.read_file(f"{METRIC_DIR}/requirements.txt")))
            command = f"rm -f {METRIC_DIR}/requirements.txt && touch {METRIC_DIR}/ready"
        return self._inner.run(command, **options)

    def close(self) -> None:
        """Keep the box's files, then end it; a candidate box first waits until the metric box is open."""
        if self.kind == "candidate":
            # The metric box opens in a thread; this pins the order the spec asks for, M open before C ends.
            self._runtime.metric_opened.wait(10)
            self._runtime.metric_opened.clear()
        if not self.closed:
            self.files = {
                str(item.relative_to(self.path)): item.read_bytes() for item in self.path.rglob("*") if item.is_file()
            }
            self.closed = True
            self._runtime.log(self, "close")
        self._inner.close()


class _Recorder:
    """Open recording boxes over the local runtime and keep one ordered log of what each box did."""

    def __init__(self) -> None:
        """Start with no box."""
        self._local = LocalSubprocessRuntime()
        self._lock = threading.Lock()
        self.boxes: list[_Recorded] = []
        self.events: list[tuple[str, str, str]] = []
        self.installs: list[tuple[str, str | None]] = []
        self.metric_opened = threading.Event()

    def log(self, box: _Recorded, action: str, detail: str = "") -> None:
        """Add one event to the log.

        Args:
            box: The box the event happened in.
            action: ``open``, ``write``, ``run`` or ``close``.
            detail: The file name or the command.
        """
        with self._lock:
            self.events.append((box.kind, action, detail))

    def open(self, spec: SandboxSpec) -> _Recorded:
        """Open one recording box.

        Args:
            spec: The box's spec.

        Returns:
            The box.
        """
        box = _Recorded(self, self._local.open(spec), spec)
        with self._lock:
            self.boxes.append(box)
        if box.kind == "metric":
            self.metric_opened.set()
        return box

    def of(self, kind: str) -> list[_Recorded]:
        """Return the boxes of one kind, in the order they opened.

        Args:
            kind: ``setup``, ``metric-setup``, ``candidate`` or ``metric``.

        Returns:
            The boxes.
        """
        return [box for box in self.boxes if box.kind == kind]

    def index(self, kind: str, action: str, detail: str = "") -> int:
        """Return the position of the first matching event in the log.

        Args:
            kind: The box kind.
            action: The event.
            detail: A prefix of the file name or the command.

        Returns:
            The event's position.
        """
        return next(
            number
            for number, event in enumerate(self.events)
            if event[:2] == (kind, action) and event[2].startswith(detail)
        )


def _repo_scorer(
    tmp_path: Path, runtime: Any, *, code: str = _REPO_METRIC, tree: dict[str, str] | None = None
) -> tuple[RepoScorer, Path]:
    """Build a repository scorer with a ``[run]`` entry, a metric file and a metric package list.

    Args:
        tmp_path: Scratch folder.
        runtime: Opens the boxes.
        code: The metric source.
        tree: Files that replace or add to the default tree.

    Returns:
        The scorer and the tree's archive.
    """
    archive = _archive(tmp_path, {**_TREE, **(tree or {})})
    scorer = RepoScorer(
        RepoWorkspace(
            runtime=runtime,
            spec=SandboxSpec(lifetime_seconds=120, allowed_hosts=("pypi.org",), operation_key="op"),
            archive=archive,
            editable_paths=["src"],
            readonly_paths=[],
            setup_command=None,
            secrets={},
            hidden_paths=_HIDDEN,
        ),
        code,
        timeout_seconds=60,
        hidden_paths=_HIDDEN,
        run_entry="src/app.py:run",
    )
    return scorer, archive


def _score_once(tmp_path: Path) -> tuple[_Recorder, dict[str, Any]]:
    """Score the starting commit once on input 2 and close the scorer.

    Args:
        tmp_path: Scratch folder.

    Returns:
        The recorder and the result.
    """
    runtime = _Recorder()
    scorer, _ = _repo_scorer(tmp_path, runtime)
    try:
        result = scorer.score("", {"input": 2})
    finally:
        scorer.close()
    return runtime, result


def _unpacked(chunks: list[str]) -> dict[str, bytes]:
    """Decode base64 tar.gz parts the way a box does and return the files they hold.

    Args:
        chunks: The parts, in order; each is base64 on its own.

    Returns:
        Each member's name, without ``./``, and its bytes; folders map to ``b""``.
        The root, the local box's ``tmp`` and macOS ``._`` files are left out.
    """
    data = b"".join(base64.b64decode(chunk) for chunk in chunks)
    members: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(data)) as packed:
        for member in packed:
            name = member.name.removeprefix("./")
            # The local box's own TMPDIR, and the AppleDouble files of macOS tar; neither exists in a real box.
            if name in (".", "tmp") or posixpath.basename(name).startswith("._"):
                continue
            handle = packed.extractfile(member) if member.isreg() else None
            members[name] = handle.read() if handle is not None else b""
    return members


def _state_writes(box: _Recorded) -> dict[str, bytes]:
    """Return the files of the saved state the parent wrote into a box.

    Args:
        box: The box.

    Returns:
        The state's files; empty when the box opened without a state.
    """
    parts = [box.writes[name] for name in sorted(box.writes) if name.startswith(".app-state/state.")]
    return _unpacked(parts) if parts else {}


@pytest.fixture(autouse=True)
def _free_relay_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let each test's runner bind a free port, so parallel workers never share the fixed one.

    Args:
        monkeypatch: Restores the fixed port after the test.
    """
    monkeypatch.setattr(runner, "RELAY_PORT", 0)


def test_candidate_and_metric_boxes_are_separate_and_only_the_metric_box_gets_the_metric(tmp_path: Path) -> None:
    """C and M are two sessions; no write to any other box, and no file in it, holds a metric file name or byte."""
    runtime, result = _score_once(tmp_path)

    assert result["score"] == pytest.approx(0.4)
    assert result["feedback"] == "sentinel"
    [candidate] = runtime.of("candidate")
    [metric] = runtime.of("metric")
    assert candidate is not metric
    assert candidate.path != metric.path
    marker = SENTINEL.encode()
    for box in runtime.boxes:
        if box is metric:
            continue
        for name, text in box.writes.items():
            assert "metric.py" not in name, (box.kind, name)
            assert SENTINEL not in text, (box.kind, name)
            assert "metric.py" not in text, (box.kind, name)
        for name, data in {**box.files, **_state_writes(box)}.items():
            assert not name.endswith("eval/metric.py"), (box.kind, name)
            assert marker not in data, (box.kind, name)
    assert marker in metric.files[f"{METRIC_REPO_DIR}/eval/metric.py"]


def test_the_metric_box_gets_no_repository_file(tmp_path: Path) -> None:
    """M gets only the metric state, the metric files, the runner and its call folder."""
    runtime, _ = _score_once(tmp_path)
    [metric] = runtime.of("metric")

    assert metric.spec.private
    for name in metric.writes:
        assert name.startswith((".app-state/state.", f"{METRIC_DIR}/")), name
    assert set(_state_writes(metric)) == {METRIC_DIR, f"{METRIC_DIR}/ready"}
    [packed] = [text for name, text in metric.writes.items() if name.startswith(f"{METRIC_DIR}/metric-")]
    assert set(_unpacked([packed])) - {"eval"} == {"eval/metric.py"}
    assert metric.files, "the metric box kept no file"
    for name in metric.files:
        assert name.startswith(f"{METRIC_DIR}/"), name
    repo_files = {name for name in metric.files if name.startswith(f"{METRIC_REPO_DIR}/")}
    assert repo_files == {f"{METRIC_REPO_DIR}/eval/metric.py"}


def test_the_candidate_box_ends_before_the_metric_files_go_into_the_open_metric_box(tmp_path: Path) -> None:
    """M opens while C runs; the metric files go into M only after C ended."""
    runtime, _ = _score_once(tmp_path)

    metric_open = runtime.index("metric", "open")
    candidate_run = runtime.index("candidate", "run", "cd ")
    candidate_close = runtime.index("candidate", "close")
    metric_files = runtime.index("metric", "write", f"{METRIC_DIR}/metric-")
    assert metric_open < candidate_close < metric_files
    assert candidate_run < candidate_close
    assert runtime.index("metric", "run", "cd ") > metric_files


class _Inbox:
    """Record the answers a relay writes into its box."""

    def __init__(self) -> None:
        """Start with no answer."""
        self.files: dict[str, str] = {}

    def write_files(self, files: dict[str, str]) -> None:
        """Record the answers."""
        self.files.update(files)


@pytest.mark.parametrize(("role", "other"), [("run", "metric"), ("metric", "run")])
@pytest.mark.parametrize("kind", ["stage", "service", "mcp"])
def test_the_relay_refuses_a_frame_for_the_other_role(role: str, other: str, kind: str) -> None:
    """C's relay refuses a metric frame and M's relay refuses a run frame, with a 403 answer."""
    inbox = _Inbox()
    relay = _StageRelay(
        {},
        {},
        response_dir="calls/000001/stages",
        max_concurrency=1,
        env={},
        role=role,
        box=inbox,  # type: ignore[arg-type]
    )
    identity = uuid.uuid4().hex
    try:
        relay.output("stdout", relay._prefix + json.dumps({"id": identity, "role": other, kind: "judge"}) + "\n")
        deadline = time.monotonic() + 5
        while not inbox.files and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        relay.close()
    assert json.loads(inbox.files[f"calls/000001/stages/{identity}.json"]) == {
        "status": 403,
        "error": f"This box runs the [{role}] role; a frame for '{other}' is refused here.",
    }


_RUN_FAILS = {"src/app.py": "def run(value):\n    raise ValueError('candidate broke')\n"}
_METRIC_FAILS = "def score(repo_path, case, output):\n    raise RuntimeError('metric broke')\n"


@pytest.mark.parametrize(
    ("case", "options"),
    [("success", {}), ("run fails", {"tree": _RUN_FAILS}), ("metric fails", {"code": _METRIC_FAILS})],
)
def test_each_box_has_its_own_hold_and_every_box_closes(tmp_path: Path, case: str, options: dict[str, Any]) -> None:
    """Each box opens with its own operation key, so it reserves its own hold, and every box closes."""
    runtime = _Recorder()
    scorer, _ = _repo_scorer(tmp_path, runtime, **options)
    try:
        if case == "metric fails":
            with pytest.raises(RepoScoreError, match="raised an error"):
                scorer.score("", {"input": 2})
        else:
            result = scorer.score("", {"input": 2})
            assert (result["score"] == 0.0) == (case == "run fails")
    finally:
        scorer.close()

    keys = [box.spec.operation_key for box in runtime.boxes]
    assert len(set(keys)) == len(keys)
    assert all(key is not None and key.startswith("op:") for key in keys)
    assert [box.kind for box in runtime.boxes] == ["setup", "metric-setup", "candidate", "metric"]
    assert all(box.closed for box in runtime.boxes)
    [metric] = runtime.of("metric")
    # After a failed [run] entry the metric box closes without the metric files.
    assert any(name.startswith(f"{METRIC_DIR}/metric-") for name in metric.writes) == (case != "run fails")


def test_the_metric_setup_installs_only_the_declared_packages_and_keeps_no_repository_file(tmp_path: Path) -> None:
    """The metric setup box is private, gets only the package list, and its saved state holds no repository file."""
    runtime, _ = _score_once(tmp_path)
    [setup] = runtime.of("metric-setup")

    assert setup.spec.private
    assert setup.spec.allowed_hosts == PACKAGE_REGISTRY_HOSTS
    assert set(setup.writes) == {f"{METRIC_DIR}/requirements.txt"}
    [(command, requirements)] = runtime.installs
    assert requirements == _REQUIREMENTS
    assert f"-r {METRIC_DIR}/requirements.txt" in command
    assert set(setup.files) <= {f"{METRIC_DIR}/ready"} | {
        name for name in setup.files if name.startswith(".app-state/")
    }
    # The saved metric state, as the parent writes it into the metric box.
    [metric] = runtime.of("metric")
    assert set(_state_writes(metric)) == {METRIC_DIR, f"{METRIC_DIR}/ready"}


# --- Local Docker boxes (``pytest -m docker``) ---

_IMAGE = "skynet-local-sandbox:0.1.0-fa7455ae46e8-1794372282390"
_DOCKER_UP = (
    shutil.which("docker") is not None
    and subprocess.run(["docker", "image", "inspect", _IMAGE], capture_output=True, check=False).returncode == 0
)
_docker = pytest.mark.skipif(not _DOCKER_UP, reason=f"Docker or the local {_IMAGE} image is missing.")


def _docker_scorer(
    tmp_path: Path,
    runtime: Any,
    tag: str,
    entry: str,
    code: str,
    *,
    setup: str | None = None,
    files: dict[str, str] | None = None,
) -> RepoScorer:
    """Build a repository scorer whose boxes are local Docker containers labelled with ``tag``.

    Args:
        tmp_path: Scratch folder.
        runtime: Opens the boxes.
        tag: The ``skynet_test`` label of every container.
        entry: The source of ``src/app.py``, whose ``run`` is the ``[run]`` entry.
        code: The metric source.
        setup: The setup command.
        files: More files for the tree.

    Returns:
        The scorer.
    """
    archive = _archive(tmp_path, {"src/app.py": entry, "eval/metric.py": _METRIC_FILE, **(files or {})})
    return RepoScorer(
        RepoWorkspace(
            runtime=runtime,
            spec=SandboxSpec(
                lifetime_seconds=900, network_disabled=True, tags={"skynet_test": tag}, operation_key="op"
            ),
            archive=archive,
            editable_paths=["src"],
            readonly_paths=[],
            setup_command=setup,
            secrets={},
            hidden_paths=_HIDDEN,
        ),
        code,
        timeout_seconds=300,
        hidden_paths=_HIDDEN,
        run_entry="src/app.py:run",
        run_timeout=180,
    )


def _containers(tag: str) -> list[str]:
    """Return the ids of the containers labelled with ``tag``, running or not, after they had time to go.

    Args:
        tag: The ``skynet_test`` label.

    Returns:
        The container ids still there after up to 60 s.
    """
    deadline = time.monotonic() + 60
    while True:
        listed = subprocess.run(
            ["docker", "ps", "-a", "-q", "--filter", f"label=skynet_test={tag}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        if not listed or time.monotonic() > deadline:
            return listed
        time.sleep(1)


_PTH_ENTRY = """
import os
import site
import sysconfig

PTH = "import os; open(os.path.expanduser('~/PTH_RAN'), 'a').write('x')\\n"


def plant():
    folders = [site.getusersitepackages(), *site.getsitepackages(), sysconfig.get_paths()['purelib'],
               '/usr/local/lib/python3.12/site-packages']
    planted, refused = [], []
    for folder in dict.fromkeys(folders):
        try:
            os.makedirs(folder, exist_ok=True)
            with open(os.path.join(folder, 'evil_skynet.pth'), 'w') as handle:
                handle.write(PTH)
            planted.append(folder)
        except OSError:
            refused.append(folder)
    return planted, refused


def run(value):
    planted, refused = plant()
    # The image's venv turns the user site off, so load each folder the way site does; this proves the files run.
    for folder in planted:
        site.addsitedir(folder)
    return {'planted': planted, 'refused': refused, 'ran_in_c': os.path.exists(os.path.expanduser('~/PTH_RAN'))}


if __name__ == '__main__':
    plant()
"""

_PTH_METRIC = """
import glob
import json
import os
import site
import sys


def score(repo_path, case, output):
    folders = [site.getusersitepackages(), *site.getsitepackages(), *sys.path, '/usr/local/lib/python3.12/site-packages']
    found = sorted({path for folder in folders for path in glob.glob(os.path.join(folder, 'evil_skynet.pth'))})
    ran = os.path.exists(os.path.expanduser('~/PTH_RAN'))
    return (0.0 if found or ran else 1.0), json.dumps({'found': found, 'ran': ran, 'c': output})
"""


@pytest.mark.docker
@_docker
def test_a_planted_pth_file_does_not_run_in_the_metric_box(tmp_path: Path) -> None:
    """Setup and the version plant .pth files in every site folder they can; none of them reaches M."""
    tag = uuid.uuid4().hex
    scorer = _docker_scorer(
        tmp_path, AsyncBridgeRuntime("docker", image=_IMAGE), tag, _PTH_ENTRY, _PTH_METRIC, setup="python3 src/app.py"
    )
    try:
        result = scorer.score("", {"input": 1})
    finally:
        scorer.close()

    seen = json.loads(result["feedback"])
    assert seen["c"]["ran_in_c"] is True, seen
    assert any(folder.endswith("site-packages") and "/.local/" in folder for folder in seen["c"]["planted"]), seen
    # The image's /app/.venv and system site folders are read-only, so the plant there fails in C already.
    assert any(folder.startswith("/app/.venv") for folder in seen["c"]["planted"] + seen["c"]["refused"]), seen
    assert seen["found"] == [], seen
    assert seen["ran"] is False, seen
    assert result["score"] == 1.0
    assert _containers(tag) == []


_DAEMON_ENTRY = """
import os
import subprocess
import time

LOOP = ("while true; do find /workspace /tmp -name metric.py 2>/dev/null >> $HOME/daemon_seen.txt;"
        " echo tick >> $HOME/daemon_ticks.txt; sleep 0.5; done # skynet-daemon-marker")


def run(value):
    subprocess.Popen(['setsid', 'sh', '-c', LOOP], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    ticks = os.path.expanduser('~/daemon_ticks.txt')
    deadline = time.monotonic() + 60
    while not os.path.exists(ticks) and time.monotonic() < deadline:
        time.sleep(0.2)
    seen = os.path.expanduser('~/daemon_seen.txt')
    return {'ticks': os.path.exists(ticks), 'seen': open(seen).read() if os.path.exists(seen) else ''}
"""

_DAEMON_METRIC = """
import json
import os


def score(repo_path, case, output):
    daemons = []
    for pid in os.listdir('/proc'):
        if not pid.isdigit():
            continue
        try:
            with open(f'/proc/{pid}/cmdline', 'rb') as handle:
                command = handle.read()
        except OSError:
            continue
        if b'skynet-daemon-' + b'marker' in command:
            daemons.append(pid)
    left = [name for name in ('daemon_seen.txt', 'daemon_ticks.txt') if os.path.exists(os.path.expanduser('~/' + name))]
    return (0.0 if daemons or left else 1.0), json.dumps({'daemons': daemons, 'left': left, 'c': output})
"""


@pytest.mark.docker
@_docker
def test_a_setsid_daemon_ends_with_the_candidate_box_and_never_sees_the_metric(tmp_path: Path) -> None:
    """A daemon the version starts with setsid finds no metric file in C, and is gone with C."""
    tag = uuid.uuid4().hex
    scorer = _docker_scorer(tmp_path, AsyncBridgeRuntime("docker", image=_IMAGE), tag, _DAEMON_ENTRY, _DAEMON_METRIC)
    try:
        result = scorer.score("", {"input": 1})
    finally:
        scorer.close()

    seen = json.loads(result["feedback"])
    assert seen["c"] == {"ticks": True, "seen": ""}, seen
    assert seen["daemons"] == [], seen
    assert seen["left"] == [], seen
    assert result["score"] == 1.0
    assert _containers(tag) == []


_ROOT_ATTACK = (
    "id -u; mkdir -p .app-metric/repo/eval && echo 'def score(*a): return 1.0' > .app-metric/repo/eval/metric.py;"
    " echo forged > /tmp/root_forged; echo forged > /workspace/root_forged; true"
)

_ROOT_METRIC = """
import hashlib
import json
import os


def score(repo_path, case, output):
    digest = hashlib.sha256(open(os.path.join(repo_path, 'eval/metric.py'), 'rb').read()).hexdigest()
    forged = [path for path in ('/tmp/root_forged', '/workspace/root_forged') if os.path.exists(path)]
    honest = digest == {digest!r} and not forged
    return (0.25 if honest else 0.0), json.dumps({{'digest': digest, 'forged': forged}})
"""


class _RootFirst:
    """Open Docker boxes, and run a root command in each candidate box before its ``[run]`` entry."""

    def __init__(self, inner: Any) -> None:
        """Wrap a runtime.

        Args:
            inner: The Docker runtime.
        """
        self._inner = inner
        self.attacks: list[CommandResult] = []

    def open(self, spec: SandboxSpec) -> Any:
        """Open a box; a candidate box runs the root command first.

        Args:
            spec: The box's spec.

        Returns:
            The box.
        """
        session = self._inner.open(spec)
        if ":candidate-" not in (spec.operation_key or ""):
            return session
        attacks = self.attacks

        class _Rooted:
            """Delegate to the box, and run the root command before the candidate runner."""

            def __getattr__(self, name: str) -> Any:
                """Return the box's own attribute."""
                return getattr(session, name)

            def run(self, command: str, **options: Any) -> CommandResult:
                """Run the root command once before the ``[run]`` entry, then the command.

                Args:
                    command: The shell command.
                    **options: The run options.

                Returns:
                    The command's result.
                """
                if runner.CANDIDATE_FLAG in command and not attacks:
                    attacks.append(session.run(_ROOT_ATTACK, sudo=True, timeout_seconds=120))
                return session.run(command, **options)

        return _Rooted()


@pytest.mark.docker
@_docker
def test_root_in_the_candidate_box_cannot_change_the_metric_box_or_the_score(tmp_path: Path) -> None:
    """A root command in C cannot reach M's files, so M checks its own metric file and gives the honest score."""
    tag = uuid.uuid4().hex
    digest = hashlib.sha256(_METRIC_FILE.encode()).hexdigest()
    runtime = _RootFirst(AsyncBridgeRuntime("docker", image=_IMAGE))
    scorer = _docker_scorer(tmp_path, runtime, tag, _TREE["src/app.py"], _ROOT_METRIC.format(digest=digest))
    try:
        result = scorer.score("", {"input": 1})
    finally:
        scorer.close()

    [attack] = runtime.attacks
    assert attack.stdout.splitlines()[0] == "0", attack
    assert json.loads(result["feedback"]) == {"digest": digest, "forged": []}
    assert result["score"] == 0.25
    assert _containers(tag) == []


_PACKAGE_METRIC = """
import json

import inflection


def score(repo_path, case, output):
    return 1.0, json.dumps({'file': inflection.__file__, 'word': inflection.camelize('metric_box')})
"""


@pytest.mark.docker
@_docker
def test_a_declared_metric_package_imports_in_the_metric_box(tmp_path: Path) -> None:
    """The metric setup box installs a real package from the registry, and M imports it from the metric state.

    The repository has no ``[setup]`` command. The image's python3 is a venv without a user site, so the
    install goes into the metric state's own folder, which the runner puts on ``sys.path``.
    """
    tag = uuid.uuid4().hex
    scorer = _docker_scorer(
        tmp_path,
        AsyncBridgeRuntime("docker", image=_IMAGE),
        tag,
        _TREE["src/app.py"],
        _PACKAGE_METRIC,
        files={".optimize/metric-requirements.txt": "inflection==0.5.1\n"},
    )
    try:
        result = scorer.score("", {"input": 1})
    finally:
        scorer.close()

    seen = json.loads(result["feedback"])
    assert seen["word"] == "MetricBox", seen
    assert f"/{METRIC_DIR}/site/inflection" in seen["file"], seen
    assert result["score"] == 1.0
    assert _containers(tag) == []
