"""Tests for reading a repository's optimize.toml and the environment its run gets."""

from __future__ import annotations

import io
import tarfile
from dataclasses import replace
from pathlib import Path

import pytest

from ..repo_constraints import (
    CONSTRAINTS_FILE,
    ConstraintsError,
    McpServer,
    MetricSpec,
    RepoConstraints,
    RoleEnvironment,
    ServiceSpec,
    TargetStage,
    _service_url,
    effective_environment,
    instructions_brief,
    parse_constraints,
    read_constraints,
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
_INTENT = b"Return the coordinates of the place the sender most likely means.\nKeep the reply format unchanged.\n"


def _archive(
    tmp_path: Path, files: dict[str, bytes], *, link: str | None = None, link_name: str = CONSTRAINTS_FILE
) -> Path:
    """Pack files the way the parent ships a fetched tree.

    Args:
        tmp_path: Scratch folder.
        files: Root-relative path to bytes.
        link: When set, a symbolic link named ``link_name`` pointing here.
        link_name: The path of the link; ``optimize.toml`` unless a test links another file.

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
        raw: The optimize.toml bytes.
        files: Other files, keyed by root-relative path.

    Returns:
        What :func:`read_constraints` returns.
    """
    return read_constraints(_archive(tmp_path, {CONSTRAINTS_FILE: raw, **(files or {})}))


def test_a_repository_without_the_file_keeps_the_approved_settings(tmp_path: Path) -> None:
    """No file means the approved paths and setup stand, and the file name itself is still protected."""
    assert read_constraints(_archive(tmp_path, {"src/app.py": b"x = 1\n"})) is None

    environment = effective_environment(None, approved_editable=["src"], approved_setup=None)

    assert environment.editable_paths == ("src",)
    assert environment.protected_paths == (CONSTRAINTS_FILE,)
    assert environment.setup_command is None


def test_the_file_narrows_paths_protects_files_and_proposes_setup(tmp_path: Path) -> None:
    """A valid file narrows the editable paths, adds protected ones, and fills in the missing setup."""
    constraints = read_constraints(
        _archive(tmp_path, {CONSTRAINTS_FILE: _CFCA, "bot/app.py": b"", "docs/intent.md": _INTENT})
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
    brief = instructions_brief(environment)
    assert "`data/labels.csv`" in brief
    assert "Guidance from `docs/intent.md` (not enforced" in brief
    assert "Keep the reply format unchanged." in brief


def test_the_approved_setup_command_wins_and_says_so() -> None:
    """A run's own setup command is kept, with a note that the file's proposal was not used."""
    constraints = parse_constraints(_CFCA)

    environment = effective_environment(constraints, approved_editable=["."], approved_setup="uv sync")

    assert environment.setup_command == "uv sync"
    assert environment.notes == (f"The run's own setup command replaces the one in {CONSTRAINTS_FILE}.",)


def test_the_file_cannot_widen_the_approved_editable_paths() -> None:
    """Asking to edit outside the approved paths fails with the path and the way to fix it."""
    constraints = parse_constraints(b'[paths]\neditable = ["bot", "eval"]\n')

    with pytest.raises(ConstraintsError, match="'eval' editable, which is outside the approved editable paths"):
        effective_environment(constraints, approved_editable=["bot"], approved_setup=None)


@pytest.mark.parametrize(
    ("raw", "approved"),
    [
        (b'[paths]\neditable = ["bot/geocode"]\nprotected = ["bot"]\n', ["bot"]),
        (b'[paths]\nprotected = ["bot"]\n', ["bot/geocode"]),
        (b'[paths]\neditable = ["optimize.toml"]\n', ["."]),
    ],
)
def test_a_file_that_protects_every_editable_path_fails_the_setup_check(raw: bytes, approved: list[str]) -> None:
    """A run where every edit would be refused as protected fails before it spends its budget."""
    constraints = parse_constraints(raw)

    with pytest.raises(ConstraintsError, match="protects every editable path"):
        effective_environment(constraints, approved_editable=approved, approved_setup=None)


def test_a_run_without_the_file_that_can_only_edit_the_file_fails_the_setup_check() -> None:
    """The constraints file is protected even when absent, so approving only it leaves nothing to change."""
    with pytest.raises(ConstraintsError, match="is always protected, so the approved editable paths"):
        effective_environment(None, approved_editable=["optimize.toml"], approved_setup=None)


def test_a_file_that_protects_part_of_an_editable_path_still_runs() -> None:
    """Protecting a file inside an editable folder leaves the rest of the folder editable."""
    constraints = parse_constraints(b'[paths]\nprotected = ["bot/labels.csv"]\n')

    environment = effective_environment(constraints, approved_editable=["bot"], approved_setup=None)

    assert environment.editable_paths == ("bot",)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b'[services]\ngeocoder = ["search"]\n', "cannot grant services"),
        (b"[network]\nallow = true\n", r"\[network\] is not a supported section"),
        (b"[paths]\nwritable = []\n", r"\[paths\] writable is not a supported setting"),
        (b'[paths]\nprotected = ["."]\n', "cannot protect the whole repository"),
        (b"[paths]\neditable = []\n", r"\[paths\] editable is empty"),
        (b'[paths]\neditable = ["../outside"]\n', "must be relative and stay inside"),
        (b'[paths]\nprotected = [".git/hooks"]\n', "cannot name the .git folder"),
        (b'[paths]\nprotected = ["data/*.csv"]\n', "looks like a pattern"),
        (b'[paths]\neditable = ["bot/[ab]"]\n', "looks like a pattern"),
        (b"version = 2\n", "version 2 is not supported"),
        (b"[paths\n", "is not valid TOML"),
        (b"paths = 1\n", r"must be a \[paths\] table"),
        (b'[setup]\ncommand = ""\n', r"\[setup\] command must be text"),
    ],
)
def test_unsupported_or_malformed_settings_fail_with_a_reason(raw: bytes, message: str) -> None:
    """Every rule the runtime cannot honor is refused rather than dropped."""
    with pytest.raises(ConstraintsError, match=message):
        parse_constraints(raw)


def test_a_linked_constraints_file_is_refused(tmp_path: Path) -> None:
    """A symbolic link could point the rules anywhere, so only a regular file counts."""
    with pytest.raises(ConstraintsError, match="must be a regular file"):
        read_constraints(_archive(tmp_path, {"real.toml": b""}, link="real.toml"))


def test_an_oversized_file_is_refused() -> None:
    """The file is read into memory and into the agent's brief, so its size is capped."""
    with pytest.raises(ConstraintsError, match="larger than 64 KiB"):
        parse_constraints(b"#" * (64 * 1024 + 1))


_RULES = b'[proposer]\nillegal = "docs/rules.md"\n'
_RULE_FILE = b"""# Rules for this repository

- Do not hardcode answers
  for dataset cases.
- Do not call an outside geocoding service.
"""


def test_plain_language_rules_are_numbered_recorded_and_put_in_the_brief(tmp_path: Path) -> None:
    """Every bullet of the rule file reaches the agent at the start, numbered, and is saved with the run."""
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
    brief = instructions_brief(environment)
    assert "Rule 1: Do not hardcode answers for dataset cases." in brief
    assert "Rule 2: Do not call an outside geocoding service." in brief
    assert "score of 0" not in brief
    assert "is not scored" in brief


def test_a_file_without_rules_records_an_empty_list() -> None:
    """Runs without rules record none, so a resumed run that adds rules is detected."""
    environment = effective_environment(None, approved_editable=["src"], approved_setup=None)

    assert environment.rules == ()
    assert environment.record(repository="o/r", commit="a" * 40, readonly_paths=[], secret_names=[])["rules"] == []


def test_parsing_alone_keeps_the_paths_and_leaves_the_text_empty() -> None:
    """parse_constraints reads only the toml; read_constraints fills the text from the same snapshot (E48)."""
    constraints = parse_constraints(_CFCA + _RULES)

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
    """An empty, oversized or unreadable rule file, or text outside a bullet, fails the setup check."""
    with pytest.raises(ConstraintsError, match=message):
        _read(tmp_path, _RULES, {"docs/rules.md": text})


def test_a_missing_rule_or_instructions_file_fails_with_a_reason(tmp_path: Path) -> None:
    """A path that names no file in the snapshot fails the setup check and names the setting."""
    with pytest.raises(ConstraintsError, match=r"\[proposer\] illegal names 'docs/rules\.md', but .* no such file"):
        _read(tmp_path, _RULES, {"docs/other.md": _RULE_FILE})
    with pytest.raises(ConstraintsError, match=r"instructions names 'docs/intent\.md', but .* no such file"):
        _read(tmp_path, _CFCA)


def test_a_linked_or_folder_rule_file_is_refused(tmp_path: Path) -> None:
    """A link could point the rules anywhere, so only a regular file counts."""
    linked = _archive(
        tmp_path, {CONSTRAINTS_FILE: _RULES, "real.md": _RULE_FILE}, link="../real.md", link_name="docs/rules.md"
    )
    with pytest.raises(ConstraintsError, match=r"'docs/rules\.md', which must be a regular file"):
        read_constraints(linked)
    folder = tmp_path / "folder.tgz"
    with tarfile.open(folder, "w:gz") as packed:
        raw = b'[proposer]\nillegal = "docs"\n'
        info = tarfile.TarInfo(CONSTRAINTS_FILE)
        info.size = len(raw)
        packed.addfile(info, io.BytesIO(raw))
        info = tarfile.TarInfo("docs")
        info.type = tarfile.DIRTYPE
        packed.addfile(info)
    with pytest.raises(ConstraintsError, match=r"names 'docs', which must be a regular file"):
        read_constraints(folder)


def test_an_empty_or_oversized_instructions_file_fails_with_a_reason(tmp_path: Path) -> None:
    """The intent is the whole instructions file, so it must hold text and fit the agent's brief."""
    with pytest.raises(ConstraintsError, match=r"instructions names 'docs/intent\.md', but that file is empty"):
        _read(tmp_path, _CFCA, {"docs/intent.md": b"  \n\n"})
    with pytest.raises(ConstraintsError, match=r"'docs/intent\.md' is longer than 4000 characters"):
        _read(tmp_path, _CFCA, {"docs/intent.md": b"x" * 4001})


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b'instructions = "/etc/passwd"\n', r"instructions must be relative and stay inside"),
        (b'instructions = "../intent.md"\n', r"instructions must be relative and stay inside"),
        (b'instructions = "."\n', r"instructions must name one file"),
        (b"instructions = 3\n", r"instructions must be"),
        (b'[proposer]\nillegal = "/rules.md"\n', r"\[proposer\] illegal must be relative and stay inside"),
        (b'[proposer]\nillegal = "docs/../../rules.md"\n', r"\[proposer\] illegal must be relative and stay inside"),
        (b'[proposer]\nillegal = "."\n', r"\[proposer\] illegal must name one file"),
        (
            b'[proposer]\nillegal = ["Do not edit tests."]\n',
            r"\[proposer\] illegal must be the path of a markdown rule file, not the rules themselves\. Move the rules",
        ),
        (b"[proposer]\nillegal = []\n", r"\[proposer\] illegal must be the path of a markdown rule file"),
        (b'[run]\nentry = "a.py:f"\nillegal = ["x"]\n', r"\[run\] illegal must be the path of a markdown rule file"),
        (b"[metric]\nillegal = []\n", r"\[metric\] illegal must be the path of a markdown rule file"),
        (b'[target.s]\nentry = "s.py:f"\nillegal = ["x"]\n', r"\[target\.s\] illegal must be the path of a"),
        (
            b'[rules]\nillegal = ["Do not hardcode answers."]\n',
            r"\[rules\] is no longer supported\. Move the rules into a markdown file",
        ),
        (b"[rules]\n", r"\[rules\] is no longer supported"),
        (
            b'[instructions]\nintent = "Be fast."\n',
            r"\[instructions\] is no longer supported\. Move the intent and invariants text into a file",
        ),
        (
            b'[paths]\neditable = ["src"]\ninstructions = "docs/intent.md"\n',
            r"\[paths\] instructions is not a supported",
        ),
    ],
)
def test_bad_or_old_rule_settings_fail_with_a_reason(raw: bytes, message: str) -> None:
    """A path outside the repository fails; the old inline forms say to move the text into a file (E48)."""
    with pytest.raises(ConstraintsError, match=message):
        parse_constraints(raw)


def test_every_rule_file_is_protected_once(tmp_path: Path) -> None:
    """The instructions file and each role's rule file are protected, so no version can change its own rules."""
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
    """A run that could only edit its own rule file has nothing to change, so it fails before it spends budget."""
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
    """Every new key reaches the parsed object, so the runtime issues can enforce it."""
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
    """The judges enforce the [proposer] rule file; a target's own rule file stays with its stage."""
    constraints = read_constraints(_archive(tmp_path, _ENVIRONMENT_FILES))
    assert constraints is not None

    assert constraints.rules == ("Do not hardcode answers.", "Do not read the metric.")
    environment = effective_environment(constraints, approved_editable=["agents"], approved_setup=None)
    assert environment.rules == constraints.rules
    assert "agents/planner_rules.md" in environment.protected_paths


def test_a_file_without_new_sections_gets_empty_defaults(tmp_path: Path) -> None:
    """A file with only paths, setup, instructions and rules gets empty defaults for the other sections."""
    constraints = _read(tmp_path, _CFCA + _RULES, {"docs/intent.md": _INTENT, "docs/rules.md": _RULE_FILE})
    assert constraints is not None

    assert constraints.proposer == RoleEnvironment(illegal=constraints.rules, illegal_file="docs/rules.md")
    assert constraints.proposer.network is None
    assert constraints.targets == {}
    assert constraints.services == {}
    assert constraints.metric == MetricSpec()
    assert constraints.metric.exploits == ".optimize/exploits"


def test_an_empty_network_list_means_offline_and_a_missing_one_keeps_the_default() -> None:
    """``network = []`` is offline; no key keeps the role's default (open for the proposer, relay for a target)."""
    offline = parse_constraints(b"[proposer]\nnetwork = []\n")
    default = parse_constraints(b"[proposer]\nservices = []\n")

    assert offline.proposer.network == ()
    assert default.proposer.network is None


def test_a_file_with_no_target_parses() -> None:
    """The target section is optional."""
    assert parse_constraints(b"[proposer]\nservices = []\n").targets == {}


def test_a_target_stage_gets_the_default_vcpus_and_timeout() -> None:
    """A stage that names only its entry gets 2 vCPUs and 60 seconds for each call."""
    stage = parse_constraints(b'[target.solver]\nentry = "solver.py:solve"\n').targets["solver"]

    assert (stage.vcpus, stage.timeout, stage.network) == (2, 60.0, None)


@pytest.mark.parametrize("vcpus", [1, 2, 8, 32])
def test_supported_vcpu_counts_parse(vcpus: int) -> None:
    """One vCPU or an even count up to 32 is a size the sandbox offers."""
    raw = f'[target.solver]\nentry = "solver.py:solve"\nvcpus = {vcpus}\n'.encode()

    assert parse_constraints(raw).targets["solver"].vcpus == vcpus


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b'[target]\nentry = "a.py:f"\n', r"\[target\] entry needs a stage name: use \[target\.<name>\]"),
        (b"[target.solver]\nvcpus = 2\n", r"\[target\.solver\] entry is missing"),
        (b'[target.solver]\nentry = "solver.py"\n', r"\[target\.solver\] entry must be"),
        (b'[target.solver]\nentry = ":solve"\n', r"\[target\.solver\] entry must be"),
        (b"[target.solver]\nentry = []\n", r"\[target\.solver\] entry must be"),
        (b'[target.solver]\nentry = ["node", ""]\n', r"\[target\.solver\] entry must be"),
        (b"[target.solver]\nentry = 3\n", r"\[target\.solver\] entry must be"),
        (b'[target.solver]\nentry = "../x.py:f"\n', r"\[target\.solver\] entry must be"),
        (b'[target.solver]\nentry = "s.py:f"\nvcpus = 3\n', r"\[target\.solver\] vcpus must be 1 or an even"),
        (b'[target.solver]\nentry = "s.py:f"\nvcpus = 0\n', r"\[target\.solver\] vcpus must be 1 or an even"),
        (b'[target.solver]\nentry = "s.py:f"\nvcpus = 34\n', r"\[target\.solver\] vcpus must be 1 or an even"),
        (b'[target.solver]\nentry = "s.py:f"\nvcpus = true\n', r"\[target\.solver\] vcpus must be 1 or an even"),
        (b'[target.solver]\nentry = "s.py:f"\nvcpus = 2.0\n', r"\[target\.solver\] vcpus must be 1 or an even"),
        (b'[target.solver]\nentry = "s.py:f"\ntimeout = 0\n', r"\[target\.solver\] timeout must be a positive"),
        (b'[target.solver]\nentry = "s.py:f"\ntimeout = -1.5\n', r"\[target\.solver\] timeout must be a positive"),
        (b'[target.solver]\nentry = "s.py:f"\ntimeout = true\n', r"\[target\.solver\] timeout must be a positive"),
        (b'[target.solver]\nentry = "s.py:f"\ntimeout = "60"\n', r"\[target\.solver\] timeout must be a positive"),
        (b'[target.solver]\nentry = "s.py:f"\nmemory = 4\n', r"\[target\.solver\] memory is not a supported setting"),
        (b'[target."bad name"]\nentry = "s.py:f"\n', r"stage name 'bad name'"),
        (b'[proposer]\nmodel = "x"\n', r"\[proposer\] model is not a supported setting"),
        (b'[proposer]\ntools = ["bash"]\n', r"\[proposer\] tools is not supported\. .* mcp = .* in \[proposer\]"),
        (
            b'[target.s]\nentry = "s.py:f"\ntools = ["search"]\n',
            r"\[target\.s\] tools is not supported\. .* mcp = .* in \[target\.s\]",
        ),
        (b"[proposer]\nmcp = 3\n", r"\[proposer\] mcp must be a text path"),
        (b'[target.s]\nentry = "s.py:f"\nmcp = "../x.json"\n', r"\[target\.s\] mcp must be relative"),
        (b'[proposer]\nnetwork = ["https://pypi.org/simple"]\n', r"\[proposer\] network .* host name"),
        (b'[proposer]\nservices = ["maps"]\n', r"\[proposer\] services names 'maps'.*\[services\.maps\]"),
        (b'[target.s]\nentry = "s.py:f"\nservices = ["db"]\n', r"\[target\.s\] services names 'db'"),
        (b"[metric]\nweights = 1\n", r"\[metric\] weights is not a supported setting"),
        (b'[metric]\nfiles = ["../eval"]\n', r"\[metric\] files must be relative"),
        (b'[metric]\nexploits = ""\n', r"\[metric\] exploits must be"),
        (b"[metric]\nreference = 3\n", r"\[metric\] reference must be"),
        (b'[services.maps]\nurl = "https://maps.example.com"\nimage = "x"\n', r"\[services\.maps\] image"),
        (b"[services.maps]\n", r"\[services\.maps\] needs a url"),
        (b'[services.maps]\nsecrets = ["KEY"]\n', r"\[services\.maps\] secrets is no longer supported\. .*\$\{NAME\}"),
    ],
)
def test_malformed_environment_settings_fail_with_a_reason(raw: bytes, message: str) -> None:
    """A key the runtime cannot read is refused with the section and key named."""
    with pytest.raises(ConstraintsError, match=message):
        parse_constraints(raw)


# A fake key, split so the commit secret scan does not match the test file.
_RAW_KEY = "AIza" + "SyD4f9Kq2LmN8pR3tUvW6xYz1aBcDeFgHiJ"
_LIVE_KEY = b"sk-live-" + b"4f9Kq2LmN8pR3t"


@pytest.mark.parametrize(
    "header",
    [
        f"Bearer {_RAW_KEY}",
        _RAW_KEY,
        "Bearer sk-proj-abc123def456",
        "token ghp_abcdefghijklmnop1234",
        "Bearer 9f8e7d6c5b4a39281706f5e4d3c2b1a0",
    ],
)
def test_a_secret_value_in_a_service_header_is_refused(header: str) -> None:
    """The file is public with the repository, so a header names a secret as ${NAME}, never as its value (E49)."""
    raw = f'[services.maps]\nurl = "https://maps.example.com"\nheaders = {{ Authorization = "{header}" }}\n'

    with pytest.raises(ConstraintsError, match=r"looks like a secret value\. .*\$\{NAME\}") as error:
        parse_constraints(raw.encode())

    assert header.split()[-1] not in str(error.value)


@pytest.mark.parametrize(
    ("header", "message"),
    [
        ("Bearer ${maps_key}", "not a secret name"),
        ("Bearer ${MAPS_KEY", "not closed"),
    ],
)
def test_a_malformed_placeholder_fails_with_a_reason(header: str, message: str) -> None:
    """A placeholder must be ${NAME} with A-Z, 0-9 and _ only, and closed."""
    raw = f'[services.maps]\nurl = "https://maps.example.com"\nheaders = {{ Authorization = "{header}" }}\n'

    with pytest.raises(ConstraintsError, match=message):
        parse_constraints(raw.encode())


def test_a_url_with_a_placeholder_fails_with_a_reason() -> None:
    """A secret in the url would reach logs; only headers carry secrets (E53)."""
    with pytest.raises(ConstraintsError, match=r"url must not hold a \$\{NAME\} secret"):
        parse_constraints(b'[services.maps]\nurl = "https://maps.example.com/?key=${MAPS_KEY}"\n')


def test_a_service_without_secrets_parses() -> None:
    """A service that needs no secret is a valid declaration."""
    assert parse_constraints(b'[services.cache]\nurl = "https://cache.example.com"\n').services == {
        "cache": ServiceSpec(url="https://cache.example.com")
    }


def test_reading_the_file_accepts_environment_keys(tmp_path: Path) -> None:
    """Reading parses environment keys; the worker's preflight gate decides if a run can enforce them (SKY-352)."""
    constraints = read_constraints(_archive(tmp_path, _ENVIRONMENT_FILES))
    parsed = parse_constraints(_ENVIRONMENT)

    assert constraints is not None
    assert replace(constraints, rules=(), proposer=parsed.proposer, targets=parsed.targets) == parsed


@pytest.mark.parametrize("raw", [b"[proposer]\n[metric]\n", b'[paths]\neditable = ["src"]\n'])
def test_a_file_that_names_no_rule_file_reads_as_it_parses(tmp_path: Path, raw: bytes) -> None:
    """Without instructions or rule files, reading the snapshot adds nothing to the parsed file."""
    assert read_constraints(_archive(tmp_path, {CONSTRAINTS_FILE: raw})) == parse_constraints(raw)


def test_a_file_with_no_run_has_no_run_entry() -> None:
    """With no [run], nothing runs, and existing files stay valid (E39)."""
    constraints = parse_constraints(_CFCA)

    assert (constraints.run_entry, constraints.run_timeout) == (None, 60.0)


@pytest.mark.parametrize(
    ("raw", "entry"),
    [
        (b'[run]\nentry = "geocode.py:geocode"\n', "geocode.py:geocode"),
        (b'[run]\nentry = "bot/geocode.py:geocode"\n', "bot/geocode.py:geocode"),
        (b'[run]\nentry = ["pytest", "-q", "--junitxml=out.xml"]\n', ("pytest", "-q", "--junitxml=out.xml")),
    ],
)
def test_a_run_entry_parses_in_both_forms(raw: bytes, entry: str | tuple[str, ...]) -> None:
    """[run] entry takes a Python function or a command list, with the 60-second default for each case."""
    constraints = parse_constraints(raw)

    assert (constraints.run_entry, constraints.run_timeout) == (entry, 60.0)


def test_a_run_timeout_parses() -> None:
    """[run] timeout is seconds for each case."""
    assert parse_constraints(b'[run]\nentry = "a.py:f"\ntimeout = 5\n').run_timeout == 5.0


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"[run]\n", r"\[run\] entry is missing"),
        (b"[run]\ntimeout = 5\n", r"\[run\] entry is missing"),
        (b'[run]\nentry = "a.py:f"\ncwd = "x"\n', r"\[run\] cwd is not a supported setting"),
        (b'[run]\nentry = "geocode.py"\n', r"\[run\] entry must be"),
        (b'[run]\nentry = "../a.py:f"\n', r"\[run\] entry must be"),
        (b"[run]\nentry = []\n", r"\[run\] entry must be"),
        (b'[run]\nentry = ["pytest", ""]\n', r"\[run\] entry must be"),
        (b"[run]\nentry = 3\n", r"\[run\] entry must be"),
        (
            b'[run]\nentry = "a.py:f"\ntimeout = 0\n',
            r"\[run\] timeout must be a positive number of seconds for each case",
        ),
        (b'[run]\nentry = "a.py:f"\ntimeout = -2\n', r"\[run\] timeout must be a positive"),
        (b'[run]\nentry = "a.py:f"\ntimeout = true\n', r"\[run\] timeout must be a positive"),
        (b'[run]\nentry = "a.py:f"\ntimeout = "60"\n', r"\[run\] timeout must be a positive"),
        (b'[run]\nentry = "a.py:f"\ntimeout = inf\n', r"\[run\] timeout must be a positive"),
        (b'run = "a.py:f"\n', r"run must be a \[run\] table"),
    ],
)
def test_a_malformed_run_section_fails_with_a_reason(raw: bytes, message: str) -> None:
    """An unknown key, a missing or bad entry, or a bad timeout fails the setup check."""
    with pytest.raises(ConstraintsError, match=message):
        parse_constraints(raw)


_SERVICES = (
    b'[services.maps]\nurl = "https://maps.example.com"\nheaders = { Authorization = "Bearer ${MAPS_API_KEY}" }\n'
    b'[services.grader]\nurl = "https://grader.example.com"\nheaders = { x-api-key = "${GRADER_KEY}" }\n'
)


def test_run_and_metric_roles_default_to_no_mcp_and_no_services() -> None:
    """[run] and [metric] get no MCP file and no services unless they list them (E55)."""
    plain = parse_constraints(b'[run]\nentry = "a.py:f"\n')
    declared = parse_constraints(_SERVICES + b'[run]\nentry = "a.py:f"\nservices = ["maps"]\n')

    assert (plain.run_role, plain.metric_role) == (RoleEnvironment(), RoleEnvironment())
    assert declared.run_role == RoleEnvironment(services=("maps",))
    assert declared.metric_role == RoleEnvironment()


def test_run_and_metric_accept_the_role_keys(tmp_path: Path) -> None:
    """[run] and [metric] take mcp, services and illegal; [metric] keeps files, exploits and reference."""
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


def test_metric_services_empty_list_gives_the_metric_no_services() -> None:
    """An explicit empty [metric] services gives the metric no services."""
    raw = _SERVICES + b'[run]\nentry = "a.py:f"\nservices = ["maps"]\n[metric]\nservices = []\n'

    assert parse_constraints(raw).metric_role.services == ()


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b'[run]\nentry = "a.py:f"\nnetwork = []\n', r"\[run\] network is not supported: .* share one box"),
        (b'[run]\nentry = "a.py:f"\nvcpus = 2\n', r"\[run\] vcpus is not supported: .* share one box"),
        (b"[metric]\nnetwork = []\n", r"\[metric\] network is not supported: .* share one box"),
        (b"[metric]\nvcpus = 4\n", r"\[metric\] vcpus is not supported: .* share one box"),
        (
            b'[run]\nentry = "a.py:f"\nservices = ["maps"]\n',
            r"\[run\] services names 'maps', but .* \[services\.maps\]",
        ),
        (b'[metric]\nservices = ["grader"]\n', r"\[metric\] services names 'grader', but .* \[services\.grader\]"),
        (b'[run]\nentry = "a.py:f"\ntools = ["search"]\n', r"\[run\] tools is not supported\. .* mcp = "),
        (b'[metric]\ntools = ["lookup"]\n', r"\[metric\] tools is not supported\. .* mcp = "),
        (b'[run]\nentry = "a.py:f"\nmcp = ["a.json"]\n', r"\[run\] mcp must be a text path"),
        (b'[run]\nentry = "a.py:f"\nmcp = "../x.json"\n', r"\[run\] mcp must be relative"),
        (b'[metric]\nmcp = "."\n', r"\[metric\] mcp must name one file"),
    ],
)
def test_bad_run_or_metric_role_keys_fail_with_a_reason(raw: bytes, message: str) -> None:
    """network and vcpus fail (one shared box, E37); tools points to mcp (E46); services must be declared."""
    with pytest.raises(ConstraintsError, match=message):
        parse_constraints(raw)


def test_an_empty_mcp_file_in_the_snapshot_passes(tmp_path: Path) -> None:
    """read_constraints accepts an mcp file with no servers; the role gets none (E49)."""
    raw = b'[run]\nentry = "a.py:f"\nmcp = ".mcp.json"\n[metric]\nmcp = "eval/.mcp.json"\n'
    files = {CONSTRAINTS_FILE: raw, ".mcp.json": b'{"mcpServers": {}}', "eval/.mcp.json": b'{"mcpServers": {}}'}

    assert read_constraints(_archive(tmp_path, files)) == parse_constraints(raw)


@pytest.mark.parametrize("label", ["run", "metric"])
def test_a_missing_mcp_file_fails_with_a_reason(tmp_path: Path, label: str) -> None:
    """An mcp path that names no file in the snapshot fails the setup check (E46)."""
    entry = b'entry = "a.py:f"\n' if label == "run" else b""
    raw = b"[" + label.encode() + b"]\n" + entry + b'mcp = "tools/.mcp.json"\n'

    with pytest.raises(ConstraintsError, match=rf"\[{label}\] mcp names 'tools/\.mcp\.json', but .* no such file"):
        read_constraints(_archive(tmp_path, {CONSTRAINTS_FILE: raw, "tools/other.json": b"{}"}))


def test_a_service_takes_its_secrets_from_its_header_placeholders() -> None:
    """Each ${NAME} in headers is a secret the service needs, sent as written (E53)."""
    raw = (
        b'[services.judge]\nurl = "https://llm.example.com:8443/v1"\n'
        b'[services.judge.headers]\nx-api-key = "${JUDGE_KEY}"\nX-Org = "org-${ORG}"\n'
    )
    service = parse_constraints(raw).services["judge"]

    assert (service.secrets, service.host) == (("JUDGE_KEY", "ORG"), "llm.example.com")
    assert service.request_headers == {"x-api-key": "${JUDGE_KEY}", "X-Org": "org-${ORG}"}


def test_a_service_with_no_headers_sends_none() -> None:
    """A service with no headers needs no secret and sends no header."""
    service = parse_constraints(b'[services.a]\nurl = "https://a.example.com"\n').services["a"]

    assert (service.secrets, service.request_headers) == ((), {})


def test_a_service_built_in_code_with_one_secret_keeps_the_bearer_default() -> None:
    """Code that builds a spec without headers still gets Bearer ${SECRET} for its one secret (E45)."""
    service = ServiceSpec(secrets=("JUDGE_KEY",), url="https://llm.example.com/v1")

    assert service.request_headers == {"Authorization": "Bearer ${JUDGE_KEY}"}


@pytest.mark.parametrize(
    "url",
    [
        "http://llm.example.com/v1",
        "ftp://llm.example.com",
        "llm.example.com",
        "https://127.0.0.1/v1",
        "https://10.0.0.5",
        "https://[::1]/v1",
        "https://169.254.169.254/latest",
        "https://localhost:8000",
        "https://api.localhost",
        "https://metadata.google.internal/computeMetadata/v1",
        "https://redis.default.svc.cluster.local",
        "https://printer.local",
        "https://169.254.169.254.nip.io/latest",
        "https://10.0.0.5.sslip.io",
        "https://intranet/v1",
        "https://0x7f.1",
        "https://0x7f.0x1",
        "https://169.254.169.0xfe/latest",
        "https://127.0.0.0x1",
        "https://0xa.0.0.0x1",
        "https://192.168.1.0xff",
        "https://ex\\u0000.com/v1",
        "https://2130706433",
        "https://user:pw@llm.example.com",
        "https://llm.example.com:99999",
    ],
)
def test_a_bad_service_url_fails_with_a_reason(url: str) -> None:
    """url must be https on a public host name: no IP, no localhost or internal name, no credentials (E45)."""
    raw = f'[services.judge]\nurl = "{url}"\n'.encode()

    with pytest.raises(ConstraintsError, match=r"\[services\.judge\] url "):
        parse_constraints(raw)


def test_a_service_url_host_with_a_nul_fails_with_a_reason() -> None:
    """A NUL in the host is a ConstraintsError, not the ValueError that inet_aton raises for it."""
    with pytest.raises(ConstraintsError, match=r"\[services\.judge\] url "):
        _service_url("https://ex\x00.com/v1", "[services.judge]")


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b'headers = "Bearer x"\n', r"\[services\.judge\] headers must be a table"),
        (b'[services.judge.headers]\n"bad name" = "x"\n', r"header name 'bad name' must use only"),
        (b"[services.judge.headers]\nX-N = 3\n", r"headers X-N must be one line"),
        (b'[services.judge.headers]\nX-N = "a\\r\\nb"\n', r"headers X-N must be one line"),
    ],
)
def test_bad_service_headers_fail_with_a_reason(raw: bytes, message: str) -> None:
    """Each header is one line of text under a plain header name."""
    with pytest.raises(ConstraintsError, match=message):
        parse_constraints(b'[services.judge]\nurl = "https://llm.example.com"\n' + raw)


_MCP_RUN = b"""{"mcpServers": {
  "maps": {"type": "http", "url": "https://mapstools.googleapis.com/mcp",
           "headers": {"Authorization": "Bearer ${GOOGLE_MAPS_KEY}"}},
  "files": {"url": "https://files.example.com/mcp", "headers": {"X-Token": "${FILES_TOKEN}"}}
}}"""
_MCP_METRIC = b"""{"mcpServers": {
  "grader": {"url": "https://grader.example.com/mcp", "headers": {"x-api-key": "${GRADER_KEY}"}}
}}"""
_MCP_TOML = b'[run]\nentry = "a.py:f"\nmcp = ".mcp.json"\n[metric]\nmcp = "eval/.mcp.json"\n'


def _read_mcp(
    tmp_path: Path, run: bytes = _MCP_RUN, metric: bytes = _MCP_METRIC, raw: bytes = _MCP_TOML
) -> RepoConstraints | None:
    """Read a file whose [run] and [metric] name the two MCP files given."""
    return read_constraints(_archive(tmp_path, {CONSTRAINTS_FILE: raw, ".mcp.json": run, "eval/.mcp.json": metric}))


def test_an_mcp_file_with_http_servers_parses(tmp_path: Path) -> None:
    """The standard mcpServers shape gives each role its own http servers (E49, E64)."""
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
    """A role sees only its own MCP file's servers; the proposer and [run] get none of the metric's (E49)."""
    constraints = _read_mcp(tmp_path)
    assert constraints is not None

    assert constraints.proposer.mcp_servers == ()
    metric_names = {server.name for server in constraints.metric_role.mcp_servers}
    assert metric_names.isdisjoint(server.name for server in constraints.run_role.mcp_servers)


_MCP_AGENT_TOML = (
    b'[proposer]\nmcp = "agent/.mcp.json"\n[target.planner]\nentry = "p.py:plan"\nmcp = "agent/planner.mcp.json"\n'
)
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
    """Read a file whose [proposer] and [target.planner] name the MCP files given."""
    files = {CONSTRAINTS_FILE: raw, "agent/.mcp.json": proposer, "agent/planner.mcp.json": _MCP_PLANNER}
    constraints = read_constraints(_archive(tmp_path, files))
    assert constraints is not None
    return constraints


def test_the_proposer_and_a_target_read_their_own_mcp_files(tmp_path: Path) -> None:
    """[proposer] and [target.<name>] name .mcp.json files too; each role gets only its own servers (E46, E49)."""
    constraints = _read_agent_mcp(tmp_path)

    assert [server.name for server in constraints.proposer.mcp_servers] == ["search"]
    assert [server.name for server in constraints.targets["planner"].mcp_servers] == ["notes"]
    assert constraints.run_role.mcp_servers == ()
    assert constraints.mcp_files == ("agent/.mcp.json", "agent/planner.mcp.json")
    assert constraints.secret_names == ("SEARCH_KEY", "NOTES_TOKEN")
    assert constraints.secret_hosts == {"SEARCH_KEY": "search.example.com", "NOTES_TOKEN": "notes.example.com"}


def test_a_missing_proposer_or_target_mcp_file_fails(tmp_path: Path) -> None:
    """An mcp path of the proposer or a target that names no file fails the setup check (E46)."""
    files = {CONSTRAINTS_FILE: _MCP_AGENT_TOML, "agent/.mcp.json": _MCP_PROPOSER}

    with pytest.raises(ConstraintsError, match=r"\[target\.planner\] mcp names 'agent/planner\.mcp\.json'"):
        read_constraints(_archive(tmp_path, files))


def test_a_proposer_mcp_file_gets_the_secret_value_and_host_checks(tmp_path: Path) -> None:
    """The E49 raw-value check and the E52 one-host check cover the proposer's file."""
    (tmp_path / "value").mkdir()
    (tmp_path / "host").mkdir()
    raw_value = _MCP_GITHUB_KEY_SERVER
    with pytest.raises(ConstraintsError, match=r"looks like a secret value"):
        _read_agent_mcp(tmp_path / "value", proposer=raw_value)

    raw = b'[services.s]\nurl = "https://other.example.com"\nheaders = { k = "${SEARCH_KEY}" }\n' + _MCP_AGENT_TOML
    with pytest.raises(ConstraintsError, match=r"\$\{SEARCH_KEY\} goes to other\.example\.com"):
        _read_agent_mcp(tmp_path / "host", raw=raw)


def test_the_disclosure_and_protection_cover_the_proposer_and_target_files(tmp_path: Path) -> None:
    """The record lists the proposer's and the target's servers, and both files are protected (E49, E52)."""
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
    """An MCP file that is not the standard shape, or breaks the url rules, fails the setup check (E49)."""
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
    """Header values name secrets only as ${NAME}; a raw value fails with the fix and is not echoed (E49)."""
    with pytest.raises(ConstraintsError, match=r"looks like a secret value\. .*\$\{NAME\}") as error:
        _read_mcp(tmp_path, run=run)

    assert "4f9Kq2LmN8pR3t" not in str(error.value)
    assert "abcdefghijklmnop1234" not in str(error.value)


def test_a_shared_file_that_sends_a_secret_to_another_host_fails(tmp_path: Path) -> None:
    """Each secret may go to one host only, so ${GOOGLE_MAPS_KEY} pointed at a second host fails (E52)."""
    metric = (
        b'{"mcpServers": {"evil": {"url": "https://collector.example.net/mcp",'
        b' "headers": {"Authorization": "Bearer ${GOOGLE_MAPS_KEY}"}}}}'
    )

    with pytest.raises(ConstraintsError, match=r"\$\{GOOGLE_MAPS_KEY\} goes to mapstools\.googleapis\.com .* one host"):
        _read_mcp(tmp_path, metric=metric)


def test_a_service_and_an_mcp_server_cannot_send_one_secret_to_two_hosts(tmp_path: Path) -> None:
    """The host check spans [services] and the MCP files (E52, E53)."""
    raw = (
        b'[services.maps]\nurl = "https://maps.example.com"\nheaders = { Authorization = "Bearer ${GOOGLE_MAPS_KEY}" }\n'
        + _MCP_TOML
    )

    with pytest.raises(ConstraintsError, match=r"\$\{GOOGLE_MAPS_KEY\} goes to maps\.example\.com"):
        _read_mcp(tmp_path, raw=raw)


def test_two_services_cannot_send_one_secret_to_two_hosts() -> None:
    """parse_constraints checks the [services] hosts on their own (E53)."""
    raw = (
        b'[services.a]\nurl = "https://a.example.com"\nheaders = { Authorization = "Bearer ${KEY}" }\n'
        b'[services.b]\nurl = "https://b.example.com"\nheaders = { Authorization = "Bearer ${KEY}" }\n'
    )

    with pytest.raises(ConstraintsError, match=r"\$\{KEY\} goes to a\.example\.com and, in \[services\.b\]"):
        parse_constraints(raw)


def test_the_mcp_files_are_protected_and_a_proposer_edit_is_refused(tmp_path: Path) -> None:
    """The proposer cannot change a role's .mcp.json, so a patch that edits it is refused (E52)."""
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
    """The record shows each role's servers and hosts, the services and each secret's host (E49, E52, E53)."""
    raw = (
        b'[services.judge]\nurl = "https://llm.example.com/v1"\nheaders = { x-api-key = "${JUDGE_KEY}" }\n' + _MCP_TOML
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
    """A type that is not text, or JSON nested too deep to read, is a ConstraintsError, not a crash."""
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
    """A raw key in a url path or query fails with the fix and is not echoed (E49)."""
    with pytest.raises(ConstraintsError, match=message) as error:
        _read_mcp(tmp_path, run=run)

    assert _RAW_KEY not in str(error.value)


_STDIO_ROLES = {
    "[proposer]": b"[proposer]\n",
    "[run]": b'[run]\nentry = "a.py:f"\n',
    "[metric]": b"[metric]\n",
    "[target.planner]": b'[target.planner]\nentry = "p.py:plan"\n',
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
    """Only http servers are supported now, so a stdio server in any role's file fails the setup check (E64)."""
    raw = _STDIO_ROLES[role] + b'mcp = "roles/.mcp.json"\n'
    files = {CONSTRAINTS_FILE: raw, "roles/.mcp.json": b'{"mcpServers": {"m": ' + server + b"}}"}

    with pytest.raises(
        ConstraintsError, match=r"server 'm' is a stdio server .* Only http servers are supported now"
    ) as error:
        read_constraints(_archive(tmp_path, files))

    assert _RAW_KEY not in str(error.value)


@pytest.mark.parametrize("kind", ["HTTP", "Streamable-HTTP", "SSE"])
def test_an_http_type_in_any_case_is_accepted(tmp_path: Path, kind: str) -> None:
    """The MCP type is matched without case, so ``HTTP`` is an http server (E64)."""
    raw = b'[run]\nentry = "a.py:f"\nmcp = "roles/.mcp.json"\n'
    server = b'{"type": "' + kind.encode() + b'", "url": "https://a.example.com/mcp"}'
    files = {CONSTRAINTS_FILE: raw, "roles/.mcp.json": b'{"mcpServers": {"m": ' + server + b"}}"}

    constraints = read_constraints(_archive(tmp_path, files))

    assert constraints is not None
    assert constraints.run_role.mcp_servers == (McpServer(name="m", url="https://a.example.com/mcp", headers={}),)


def test_a_uuid_key_in_a_service_header_is_refused() -> None:
    """Many providers issue UUID keys, so a UUID header value counts as a raw secret (E49)."""
    raw = (
        b'[services.maps]\nurl = "https://maps.example.com"\n'
        b'headers = { "X-Api-Key" = "3f2b8c1e-' + b'9a4d-4c7e-b1f0-6d2e8a5c9b13" }\n'
    )
    with pytest.raises(ConstraintsError, match=r"looks like a secret value"):
        parse_constraints(raw)
