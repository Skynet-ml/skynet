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
from core.service_gateway.optimization.blackbox.protocol import RepoSeed
from core.service_gateway.optimization.blackbox.sandbox_contract import CommandResult

_LAB = ".autoresearch"
_SETUP = ["sh", "-c", "mkdir deps"]


class _Commands:
    """Record each box command and its grants, and give one result or failure for a command."""

    def __init__(self, outcomes: dict[str, CommandResult | Exception] | None = None) -> None:
        """Set the outcome of the commands that start with a key; every other command exits 0.

        Args:
            outcomes: The result or the error, keyed by the start of the command.
        """
        self.outcomes = outcomes or {}
        self.calls: list[tuple[str, tuple[Grant, ...]]] = []

    def __call__(self, tree: str, command: str, grants: tuple[Grant, ...] = ()) -> tuple[CommandResult, str]:
        """Run one fake box command.

        Args:
            tree: The tree the command starts from.
            command: The shell command.
            grants: The box's grants.

        Returns:
            The result and one fixed tree id, so each box round trip gives the same tree.

        Raises:
            Exception: The error that the test set for the command.
        """
        del tree
        self.calls.append((command, grants))
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
        ({f"rm -rf {_LAB}": BoxFailureError("s", "box died")}, r"check of the \[proposer\] setup .* could not run"),
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

    def differ(tree: str, command: str, grants: tuple[Grant, ...] = ()) -> tuple[CommandResult, str]:
        """Give the seed's round trip another tree than the setup's.

        Args:
            tree: The tree the command starts from.
            command: The shell command.
            grants: The box's grants.

        Returns:
            The result and the tree.
        """
        result, after = original(tree, command, grants)
        return result, "seed-again" if command == "true" else after

    proposer._ctx.run_command = differ  # type: ignore[attr-defined]
    proposer._ctx.list_files = lambda tree, path="", executable=False: [] if executable else ["a.py", "b.py"]  # type: ignore[attr-defined]
    proposer._ctx.read_file = lambda tree, path, limit=None: b"new" if (tree, path) == ("after", "a.py") else b""  # type: ignore[attr-defined]

    with pytest.raises(ServiceError, match=r"changed or removed files of the repository: a\.py\. ") as error:
        proposer.base("seed")

    assert "b.py" not in str(error.value)


def test_a_setup_name_with_a_new_line_is_one_path_of_the_listing() -> None:
    """The setup box lists NUL-separated names, so a name with a new line is removed whole, not as two paths."""
    commands = _Commands()
    proposer = _proposer(commands, setup=_SETUP)
    proposer._ctx.read_file = lambda tree, path, limit=None: b"./deps\0./a\nb\0./.autoresearch\0"  # type: ignore[attr-defined]

    proposer.base("seed")

    assert "find . -mindepth 1 -print0" in commands.calls[0][0]
    assert [command for command, _ in commands.calls if command.startswith("rm -rf --")] == ["rm -rf -- deps 'a\nb'"]


def test_a_setup_output_that_rm_cannot_remove_gives_its_own_error() -> None:
    """A failed ``rm`` is no box failure and no seed change, so the error names the removal and its output."""
    commands = _Commands({"rm -rf -- deps": CommandResult(1, stderr="rm: deps/x: Permission denied")})
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
