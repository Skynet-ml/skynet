"""Unit tests for the contract file's proposer command in the engine loop (SKY-816).

A fake command call stands in for the boxes, so each test sets one box result or failure and needs no
database, no tree store and no subprocess.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.exceptions import ServiceError
from core.models.blackbox import BLACKBOX_HARNESS_CUSTOM, BlackboxProposer
from core.sandbox.grants import Grant
from core.sandbox.trees import BoxFailureError, TreeRejectedError
from core.service_gateway.optimization.blackbox import loop_tools
from core.service_gateway.optimization.blackbox.protocol import FileTooLargeError, RepoSeed
from core.service_gateway.optimization.blackbox.sandbox_contract import CommandResult

_LAB = ".autoresearch"
_SETUP = ["sh", "-c", "mkdir deps"]


class _Commands:
    """Record each box command, its grants and its time limit, and give one result or failure for a command."""

    def __init__(self, outcomes: dict[str, CommandResult | Exception] | None = None) -> None:
        """Set the outcome of the commands that start with a key; every other command exits 0.

        Args:
            outcomes: The result or the error, keyed by the start of the command.
        """
        self.outcomes = outcomes or {}
        self.calls: list[tuple[str, tuple[Grant, ...]]] = []
        self.timeouts: list[tuple[str, float | None]] = []

    def __call__(
        self, tree: str, command: str, grants: tuple[Grant, ...] = (), *, timeout_seconds: float | None = None
    ) -> tuple[CommandResult, str]:
        """Run one fake box command.

        Args:
            tree: The tree the command starts from.
            command: The shell command.
            grants: The box's grants.
            timeout_seconds: The command's time limit; ``None`` keeps the box's limit.

        Returns:
            The result and one fixed tree id, so each box round trip gives the same tree.

        Raises:
            Exception: The error that the test set for the command.
        """
        del tree
        self.calls.append((command, grants))
        self.timeouts.append((command, timeout_seconds))
        for start, outcome in self.outcomes.items():
            if command.startswith(start):
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome, "after"
        return CommandResult(0), "after"

    def grants(self, part: str) -> list[tuple[Grant, ...]]:
        """Return the grants of each command that holds a text.

        Args:
            part: The text.

        Returns:
            The grants, in call order.
        """
        return [grants for command, grants in self.calls if part in command]


def _proposer(
    commands: _Commands, *, setup: list[str] | None = None, hosts: list[str] | None = None, gateway: str = ""
) -> loop_tools.FileProposer:
    """Build a proposer over the fake command call.

    Args:
        commands: The fake command call.
        setup: The ``[proposer] setup`` argv; none when ``None``.
        hosts: The proposer's hosts.
        gateway: The run's model gateway URL.

    Returns:
        The proposer.
    """
    ctx = SimpleNamespace(
        repo_seed=RepoSeed(tree_id="seed"),
        run_command=commands,
        read_file=lambda tree, path, limit=None: b"",
        list_files=lambda tree, path="", executable=False: [],
    )
    options = SimpleNamespace(
        model="m",
        proposer=BlackboxProposer(run_command="my-agent"),
        gateway=SimpleNamespace(url=gateway),
        repo={"file_proposer": True, "proposer_setup": setup or [], "proposer_hosts": hosts or []},
    )
    return loop_tools.FileProposer(ctx, options, _LAB)  # type: ignore[arg-type]


def test_the_setup_and_the_entry_get_the_proposer_hosts_without_the_model_gateway() -> None:
    """The gateway holds the managed key, so its host never gets a grant; the other hosts do, in both boxes."""
    commands = _Commands()
    proposer = _proposer(commands, setup=_SETUP, hosts=["pypi.org", "gw.example"], gateway="https://gw.example/v1")

    proposer.base("seed")
    proposer.run("start")

    assert commands.grants("mkdir deps") == [(Grant("pypi.org"),)]
    assert commands.grants("my-agent") == [(Grant("pypi.org"),)]
    # The lab removal and the check boxes get no network.
    assert all(
        grants == () for command, grants in commands.calls if "mkdir deps" not in command and "my-agent" not in command
    )


def test_an_entry_that_runs_out_of_time_gives_no_version() -> None:
    """A timed-out command keeps ``start`` and names the timeout, also when its exit code is 0."""
    proposer = _proposer(_Commands({"export APP_MODEL": CommandResult(0, timed_out=True)}))

    tree, failure = proposer.run("start")

    assert tree == "start"
    assert failure is not None
    assert "[proposer] entry command ran out of time" in failure


@pytest.mark.parametrize(
    ("outcomes", "message"),
    [
        (
            {"export APP_MODEL": BoxFailureError("s", "box died")},
            r"setup command .* could not run in a box\. Start a new run",
        ),
        (
            {"export APP_MODEL": TreeRejectedError("s", "hidden path")},
            r"setup command .* left files that the run refuses",
        ),
        ({"rm -rf --": BoxFailureError("s", "box died")}, r"check of the \[proposer\] setup .* could not run"),
        ({"true": BoxFailureError("s", "box died")}, r"check of the \[proposer\] setup .* could not run"),
        ({"true": TreeRejectedError("s", "refused")}, r"check of the \[proposer\] setup .* could not run"),
        ({"true": CommandResult(1)}, r"check of the \[proposer\] setup .* could not run"),
    ],
)
def test_a_box_failure_in_the_setup_or_its_check_gives_its_own_error(
    outcomes: dict[str, CommandResult | Exception], message: str
) -> None:
    """A box that fails is no proof that the setup changed a seed file, so the error says to try again.

    Args:
        outcomes: The box failure, keyed by the start of the command.
        message: The pattern of the error.
    """
    commands = _Commands(outcomes)
    proposer = _proposer(commands, setup=_SETUP)
    # The setup listing names one added path, so the check also runs the ``rm`` of the added paths.
    proposer._ctx.read_file = lambda tree, path, limit=None: b"./deps\0"  # type: ignore[attr-defined]

    with pytest.raises(ServiceError, match=message) as error:
        proposer.base("seed")

    assert "changed or removed files" not in str(error.value)


def test_a_setup_whose_check_tree_differs_from_the_seed_changed_a_seed_file() -> None:
    """When every box works and the trees differ, the error names the changed files, not the box."""
    commands = _Commands({"true": CommandResult(0)})
    proposer = _proposer(commands, setup=_SETUP)
    original = commands.__call__

    def differ(
        tree: str, command: str, grants: tuple[Grant, ...] = (), *, timeout_seconds: float | None = None
    ) -> tuple[CommandResult, str]:
        """Give the seed's round trip another tree than the setup's.

        Args:
            tree: The tree the command starts from.
            command: The shell command.
            grants: The box's grants.
            timeout_seconds: The command's time limit.

        Returns:
            The result and the tree.
        """
        result, after = original(tree, command, grants, timeout_seconds=timeout_seconds)
        return result, "seed-again" if command == "true" else after

    proposer._ctx.run_command = differ  # type: ignore[attr-defined]
    proposer._ctx.list_files = lambda tree, path="", executable=False: [] if executable else ["a.py", "b.py"]  # type: ignore[attr-defined]
    proposer._ctx.read_file = lambda tree, path, limit=None: b"new" if (tree, path) == ("after", "a.py") else b""  # type: ignore[attr-defined]

    with pytest.raises(ServiceError, match=r"changed or removed files of the repository: a\.py\. ") as error:
        proposer.base("seed")

    assert "b.py" not in str(error.value)


def test_the_setup_and_the_entry_get_thirty_minutes_and_the_other_boxes_keep_their_limit() -> None:
    """The docs give each proposer command 30 minutes; the lab removal and the check boxes keep the box limit."""
    commands = _Commands()
    proposer = _proposer(commands, setup=_SETUP)

    proposer.base("seed")
    proposer.run("start")

    proposer_limits = [
        limit for command, limit in commands.timeouts if "mkdir deps" in command or "my-agent" in command
    ]
    other_limits = {
        limit for command, limit in commands.timeouts if "mkdir deps" not in command and "my-agent" not in command
    }
    assert proposer_limits == [1800, 1800]
    assert other_limits == {None}


def test_a_seed_file_too_large_to_compare_counts_as_changed_only_when_its_size_changed() -> None:
    """The error reads each file with a byte cap; a file above the cap is compared by its size, not loaded."""
    ctx = SimpleNamespace(
        list_files=lambda tree, path="", executable=False: [] if executable else ["a.bin", "b.bin", "c.py"]
    )
    limits: list[int | None] = []

    def read_file(tree: str, path: str, limit: int | None = None) -> bytes:
        """Refuse the large files, and give the same bytes for every other file.

        Args:
            tree: The tree.
            path: The file.
            limit: The byte cap.

        Returns:
            The file bytes.

        Raises:
            FileTooLargeError: For the large files; only ``b.bin`` has another size after the setup.
        """
        limits.append(limit)
        if path.endswith(".bin"):
            size = 2 * loop_tools._COMPARED_BYTES + (tree == "after" and path == "b.bin")
            raise FileTooLargeError(path, size, loop_tools._COMPARED_BYTES)
        return b"same"

    ctx.read_file = read_file

    assert loop_tools._changed_files(ctx, "seed", "after") == (["b.bin"], False)  # type: ignore[arg-type]
    assert limits
    assert set(limits) == {loop_tools._COMPARED_BYTES}


def test_the_seed_change_error_compares_at_most_the_capped_number_of_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """A large repository reads only the first files, so the error path cannot read every file of it.

    Args:
        monkeypatch: Sets a small file cap.
    """
    monkeypatch.setattr(loop_tools, "_COMPARED_FILES", 3)
    reads: list[str] = []
    ctx = SimpleNamespace(
        list_files=lambda tree, path="", executable=False: [] if executable else [f"f{i}" for i in range(10)],
        read_file=lambda tree, path, limit=None: reads.append(path) or b"",
    )

    assert loop_tools._changed_files(ctx, "seed", "after") == ([], True)  # type: ignore[arg-type]
    assert sorted(set(reads)) == ["f0", "f1", "f2"]


def test_the_seed_change_error_names_at_most_the_capped_number_of_added_files() -> None:
    """A setup that adds many files names only the first ones, and no file is read to compare."""
    reads: list[str] = []
    ctx = SimpleNamespace(
        list_files=lambda tree, path="", executable=False: (
            [] if executable or tree == "seed" else [f"f{i:02}" for i in range(25)]
        ),
        read_file=lambda tree, path, limit=None: reads.append(path) or b"",
    )

    names = [f"f{i:02}" for i in range(20)]
    assert loop_tools._changed_files(ctx, "seed", "after") == (names, True)  # type: ignore[arg-type]
    assert reads == []


def test_the_seed_change_error_stops_reading_at_the_capped_number_of_changed_files() -> None:
    """When many common files differ, the compare stops at the name cap, so it reads two files for each name."""
    reads: list[str] = []
    ctx = SimpleNamespace(
        list_files=lambda tree, path="", executable=False: [] if executable else [f"f{i:02}" for i in range(25)],
        read_file=lambda tree, path, limit=None: reads.append(path) or tree.encode(),
    )

    names = [f"f{i:02}" for i in range(20)]
    assert loop_tools._changed_files(ctx, "seed", "after") == (names, True)  # type: ignore[arg-type]
    assert len(reads) == 40


def test_the_seed_change_error_names_a_changed_file_after_many_large_files_of_the_same_size() -> None:
    """Large files whose size stays the same do not fill the named files, so a later changed file is named."""
    paths = [f"big{i:02}" for i in range(25)] + ["z.py"]
    ctx = SimpleNamespace(list_files=lambda tree, path="", executable=False: [] if executable else paths)

    def read_file(tree: str, path: str, limit: int | None = None) -> bytes:
        """Refuse the large files with one size, and give ``z.py`` other bytes after the setup.

        Args:
            tree: The tree.
            path: The file.
            limit: The byte cap.

        Returns:
            The file bytes.

        Raises:
            FileTooLargeError: For the large files.
        """
        if path.startswith("big"):
            raise FileTooLargeError(path, 2 * loop_tools._COMPARED_BYTES, loop_tools._COMPARED_BYTES)
        return tree.encode()

    ctx.read_file = read_file

    assert loop_tools._changed_files(ctx, "seed", "after") == (["z.py"], False)  # type: ignore[arg-type]


def test_a_complete_list_of_changed_files_is_not_shortened() -> None:
    """When every common file is compared and few differ, the list is whole."""
    ctx = SimpleNamespace(
        list_files=lambda tree, path="", executable=False: [] if executable else ["a.py", "b.py"],
        read_file=lambda tree, path, limit=None: tree.encode() if path == "a.py" else b"",
    )

    assert loop_tools._changed_files(ctx, "seed", "after") == (["a.py"], False)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("changed", "says"),
    [
        ((["a.py"], True), r"of the repository: a\.py \(list shortened\)\. Those changes"),
        (([], False), r"of the repository\. The check names no file: the change is in a link"),
    ],
)
def test_the_seed_change_error_says_when_its_list_is_shortened_or_empty(
    changed: tuple[list[str], bool], says: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A capped list says that it is shortened, and an empty list gives a general sentence, not a bare error.

    Args:
        changed: What ``_changed_files`` gives.
        says: The pattern of the error.
        monkeypatch: Sets what ``_changed_files`` gives.
    """
    monkeypatch.setattr(loop_tools, "_changed_files", lambda ctx, seed, tree: changed)
    commands = _Commands()
    proposer = _proposer(commands, setup=_SETUP)
    original = commands.__call__

    def differ(
        tree: str, command: str, grants: tuple[Grant, ...] = (), *, timeout_seconds: float | None = None
    ) -> tuple[CommandResult, str]:
        """Give the seed's round trip another tree than the setup's.

        Args:
            tree: The tree the command starts from.
            command: The shell command.
            grants: The box's grants.
            timeout_seconds: The command's time limit.

        Returns:
            The result and the tree.
        """
        result, after = original(tree, command, grants, timeout_seconds=timeout_seconds)
        return result, "seed-again" if command == "true" else after

    proposer._ctx.run_command = differ  # type: ignore[attr-defined]

    with pytest.raises(ServiceError, match=says):
        proposer.base("seed")


@pytest.mark.parametrize(
    "outcome",
    [BoxFailureError("s", "box died"), TreeRejectedError("s", "refused"), CommandResult(1)],
    ids=["box-failure", "refused-tree", "exit-1"],
)
def test_a_failed_lab_removal_for_a_parent_stops_the_run_and_caches_nothing(
    outcome: CommandResult | Exception,
) -> None:
    """A parent's session needs the setup's output, so a failed lab removal raises, and no plain parent is cached.

    Args:
        outcome: The result or the error of the lab removal box.
    """
    commands = _Commands()
    proposer = _proposer(commands, setup=_SETUP)
    proposer._ctx.read_file = lambda tree, path, limit=None: b"./deps\0"  # type: ignore[attr-defined]
    proposer.base("seed")
    version = proposer.version("start", "last", "seed")
    commands.outcomes[f"rm -rf {_LAB}"] = outcome

    with pytest.raises(
        ServiceError, match=r"check of the \[proposer\] setup .* could not run in a box\. Start a new run"
    ):
        proposer.base(version)  # type: ignore[arg-type]

    assert version not in proposer._full


def test_a_version_removes_the_lab_and_the_setup_output_in_one_box() -> None:
    """A session's version needs one ``rm`` box; the lab comes off the last tree only when the version is a parent."""
    commands = _Commands()
    proposer = _proposer(commands, setup=_SETUP)
    proposer._ctx.read_file = lambda tree, path, limit=None: b"./deps\0"  # type: ignore[attr-defined]
    proposer.base("seed")
    before = len(commands.calls)

    version = proposer.version("start", "last", "seed")

    assert [command for command, _ in commands.calls[before:]] == [f"rm -rf -- {_LAB} deps"]
    proposer.base(version)  # type: ignore[arg-type]
    assert [command for command, _ in commands.calls[before + 1 :]] == [f"rm -rf {_LAB}"]


def test_a_setup_name_with_a_new_line_is_one_path_of_the_listing() -> None:
    """The setup box lists NUL-separated names, so a name with a new line is removed whole, not as two paths."""
    commands = _Commands()
    proposer = _proposer(commands, setup=_SETUP)
    proposer._ctx.read_file = lambda tree, path, limit=None: b"./deps\0./a\nb\0./.autoresearch\0"  # type: ignore[attr-defined]

    proposer.base("seed")

    assert "find . -mindepth 1 -print0" in commands.calls[0][0]
    assert [command for command, _ in commands.calls if command.startswith("rm -rf --")] == [
        f"rm -rf -- {_LAB} deps 'a\nb'"
    ]


def test_a_setup_output_that_rm_cannot_remove_gives_its_own_error() -> None:
    """A failed ``rm`` is no box failure and no seed change, so the error names the removal and its output."""
    commands = _Commands({f"rm -rf -- {_LAB} deps": CommandResult(1, stderr="rm: deps/x: Permission denied")})
    proposer = _proposer(commands, setup=_SETUP)
    proposer._ctx.read_file = lambda tree, path, limit=None: b"./deps\0"  # type: ignore[attr-defined]

    with pytest.raises(ServiceError) as error:
        proposer.base("seed")

    message = str(error.value)
    assert "removal of the files that [proposer] setup added failed with exit code 1" in message
    assert "Permission denied" in message
    assert "Start a new run" not in message
    assert "changed or removed files" not in message


@pytest.mark.parametrize(
    ("file_proposer", "harness", "says", "never"),
    [
        (True, BLACKBOX_HARNESS_CUSTOM, "runs the [proposer] entry command of the contract file", "built-in"),
        (False, "codex", "in the worker engine loop runs only its built-in proposer", "[proposer] entry"),
    ],
)
def test_the_proposer_refusal_says_which_proposer_the_engine_runs(
    file_proposer: bool, harness: str, says: str, never: str
) -> None:
    """With the file's command, the engine runs that command, so the refusal does not say "built-in proposer".

    Args:
        file_proposer: Whether the file's command runs.
        harness: The proposer harness that the engine accepts in that case.
        says: The text that the message holds.
        never: The text that the message does not hold.
    """
    options = SimpleNamespace(
        proposer=BlackboxProposer(
            harness=harness, run_command="my-agent" if file_proposer else None, install_command="npm ci"
        ),
        budget_route=None,
        reasoning_effort=None,
    )

    with pytest.raises(ServiceError) as error:
        loop_tools.refuse_unsupported_proposer(options, "AutoResearch", file_proposer=file_proposer)  # type: ignore[arg-type]

    assert f"AutoResearch {says}, so it cannot apply these settings: install_command." in str(error.value)
    assert never not in str(error.value)
