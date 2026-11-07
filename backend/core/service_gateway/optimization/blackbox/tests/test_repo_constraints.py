"""Tests for reading a repository's optimize.toml and the environment its run gets."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from ..repo_constraints import (
    CONSTRAINTS_FILE,
    ConstraintsError,
    MetricSpec,
    RoleEnvironment,
    ServiceSpec,
    TargetStage,
    effective_environment,
    instructions_brief,
    parse_constraints,
    read_constraints,
)

_CFCA = b"""
version = 1

[paths]
editable = ["bot/geocode"]
protected = ["bot/geocode/eval", "data/labels.csv"]

[setup]
command = "pip install -r requirements.txt"

[instructions]
intent = "Return the coordinates of the place the sender most likely means."
invariants = ["Keep the reply format unchanged."]
"""


def _archive(tmp_path: Path, files: dict[str, bytes], *, link: str | None = None) -> Path:
    """Pack files the way the parent ships a fetched tree.

    Args:
        tmp_path: Scratch folder.
        files: Root-relative path to bytes.
        link: When set, a symbolic link named ``optimize.toml`` pointing here.

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
            info = tarfile.TarInfo(CONSTRAINTS_FILE)
            info.type, info.linkname = tarfile.SYMTYPE, link
            packed.addfile(info)
    return archive


def test_a_repository_without_the_file_keeps_the_approved_settings(tmp_path: Path) -> None:
    """No file means the approved paths and setup stand, and the file name itself is still protected."""
    assert read_constraints(_archive(tmp_path, {"src/app.py": b"x = 1\n"})) is None

    environment = effective_environment(None, approved_editable=["src"], approved_setup=None)

    assert environment.editable_paths == ("src",)
    assert environment.protected_paths == (CONSTRAINTS_FILE,)
    assert environment.setup_command is None


def test_the_file_narrows_paths_protects_files_and_proposes_setup(tmp_path: Path) -> None:
    """A valid file narrows the editable paths, adds protected ones, and fills in the missing setup."""
    constraints = read_constraints(_archive(tmp_path, {CONSTRAINTS_FILE: _CFCA, "bot/app.py": b""}))
    assert constraints is not None

    environment = effective_environment(constraints, approved_editable=["bot"], approved_setup=None)

    assert environment.editable_paths == ("bot/geocode",)
    assert environment.protected_paths == (CONSTRAINTS_FILE, "bot/geocode/eval", "data/labels.csv")
    assert environment.setup_command == "pip install -r requirements.txt"
    record = environment.record(
        repository="cfca/bot", commit="a" * 40, readonly_paths=["vendor"], secret_names={"GEO_KEY": "v"}
    )
    assert record["constraints_file"] == {"path": CONSTRAINTS_FILE, "sha256": constraints.sha256}
    assert record["secret_names"] == ["GEO_KEY"]
    assert record["services"] == []
    brief = instructions_brief(environment)
    assert "`data/labels.csv`" in brief
    assert "not enforced" in brief
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


_RULES = b"""
[rules]
illegal = [
  "Do not hardcode answers for dataset cases.",
  "Do not call an outside geocoding service.",
]
"""


def test_plain_language_rules_are_numbered_recorded_and_put_in_the_brief() -> None:
    """Every rule reaches the agent at the start, numbered, and is saved with the run's environment."""
    constraints = parse_constraints(_RULES)

    environment = effective_environment(constraints, approved_editable=["bot"], approved_setup=None)

    assert environment.rules == (
        "Do not hardcode answers for dataset cases.",
        "Do not call an outside geocoding service.",
    )
    record = environment.record(repository="cfca/bot", commit="a" * 40, readonly_paths=[], secret_names=[])
    assert record["rules"] == list(environment.rules)
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


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"[rules]\nillegal = []\n", r"\[rules\] illegal must list 1 to 50 rules"),
        (b'[rules]\nillegal = "one"\n', r"\[rules\] illegal must list 1 to 50 rules"),
        (b'[rules]\nillegal = [""]\n', r"\[rules\] illegal must be text"),
        (b'[rules]\nallowed = ["x"]\n', r"\[rules\] allowed is not a supported setting"),
        (b"[rules]\nillegal = [" + b'"x",' * 51 + b"]\n", r"must list 1 to 50 rules"),
        (b'[rules]\nillegal = ["' + b"x" * 501 + b'"]\n', r"\[rules\] illegal must be text of 1 to 500"),
    ],
)
def test_malformed_rules_fail_with_a_reason(raw: bytes, message: str) -> None:
    """A rule list the judges cannot read is refused rather than dropped."""
    with pytest.raises(ConstraintsError, match=message):
        parse_constraints(raw)


_ENVIRONMENT = b"""
version = 1

[rules]
illegal = ["Do not hardcode answers."]

[proposer]
tools = ["bash", "maps-mcp"]
network = ["pypi.org", "files.pythonhosted.org"]
services = ["maps"]
illegal = ["Do not read the metric."]

[target.planner]
entry = "agents/planner.py:plan"
tools = ["search"]
network = []
services = ["maps"]
illegal = ["Do not call a paid model."]
vcpus = 4
timeout = 30

[target.router]
entry = ["node", "router.js"]

[services.maps]
secrets = ["GOOGLE_MAPS_API_KEY"]

[metric]
files = ["eval/metric.py", "eval/cases"]
reference = "eval/reference.json"
"""


def test_the_environment_sections_parse_into_typed_fields() -> None:
    """Every new key reaches the parsed object, so the runtime issues can enforce it."""
    constraints = parse_constraints(_ENVIRONMENT)

    assert constraints.proposer == RoleEnvironment(
        tools=("bash", "maps-mcp"),
        network=("pypi.org", "files.pythonhosted.org"),
        services=("maps",),
        illegal=("Do not hardcode answers.", "Do not read the metric."),
    )
    assert constraints.targets == {
        "planner": TargetStage(
            entry="agents/planner.py:plan",
            tools=("search",),
            network=(),
            services=("maps",),
            illegal=("Do not call a paid model.",),
            vcpus=4,
            timeout=30.0,
        ),
        "router": TargetStage(entry=("node", "router.js")),
    }
    assert constraints.services == {"maps": ServiceSpec(secrets=("GOOGLE_MAPS_API_KEY",))}
    assert constraints.metric == MetricSpec(
        files=("eval/metric.py", "eval/cases"), exploits=".optimize/exploits", reference="eval/reference.json"
    )


def test_rules_illegal_and_proposer_illegal_merge_and_keep_both_lists() -> None:
    """[rules] illegal is an alias for [proposer] illegal; the judges enforce both lists."""
    constraints = parse_constraints(_ENVIRONMENT)

    assert constraints.rules == ("Do not hardcode answers.", "Do not read the metric.")
    environment = effective_environment(constraints, approved_editable=["agents"], approved_setup=None)
    assert environment.rules == constraints.rules


def test_proposer_illegal_alone_feeds_the_judges() -> None:
    """A file that uses only the new name gets the same enforcement as [rules] illegal."""
    constraints = parse_constraints(b'[proposer]\nillegal = ["Do not edit tests."]\n')

    assert constraints.rules == ("Do not edit tests.",)


def test_an_old_file_parses_exactly_as_before() -> None:
    """Files written before the new sections keep their meaning and get empty defaults."""
    constraints = parse_constraints(_CFCA + _RULES)

    assert constraints.proposer == RoleEnvironment(illegal=constraints.rules)
    assert constraints.proposer.network is None
    assert constraints.targets == {}
    assert constraints.services == {}
    assert constraints.metric == MetricSpec()
    assert constraints.metric.exploits == ".optimize/exploits"


def test_an_empty_network_list_means_offline_and_a_missing_one_keeps_the_default() -> None:
    """``network = []`` is offline; no key keeps the role's default (open for the proposer, relay for a target)."""
    offline = parse_constraints(b"[proposer]\nnetwork = []\n")
    default = parse_constraints(b'[proposer]\ntools = ["bash"]\n')

    assert offline.proposer.network == ()
    assert default.proposer.network is None


def test_a_file_with_no_target_parses() -> None:
    """The target section is optional."""
    assert parse_constraints(b'[proposer]\ntools = ["bash"]\n').targets == {}


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
        (b'[proposer]\ntools = "bash"\n', r"\[proposer\] tools must be a list"),
        (b'[proposer]\nnetwork = ["https://pypi.org/simple"]\n', r"\[proposer\] network .* host name"),
        (b"[proposer]\nillegal = []\n", r"\[proposer\] illegal must list 1 to 50 rules"),
        (b'[proposer]\nservices = ["maps"]\n', r"\[proposer\] services names 'maps'.*\[services\.maps\]"),
        (b'[target.s]\nentry = "s.py:f"\nservices = ["db"]\n', r"\[target\.s\] services names 'db'"),
        (b"[metric]\nweights = 1\n", r"\[metric\] weights is not a supported setting"),
        (b'[metric]\nfiles = ["../eval"]\n', r"\[metric\] files must be relative"),
        (b'[metric]\nexploits = ""\n', r"\[metric\] exploits must be"),
        (b"[metric]\nreference = 3\n", r"\[metric\] reference must be"),
        (b'[services.maps]\nsecrets = ["GOOGLE_MAPS_API_KEY"]\nimage = "x"\n', r"\[services\.maps\] image"),
        (b'[services.maps]\nsecrets = "KEY"\n', r"\[services\.maps\] secrets must be a list"),
    ],
)
def test_malformed_environment_settings_fail_with_a_reason(raw: bytes, message: str) -> None:
    """A key the runtime cannot read is refused with the section and key named."""
    with pytest.raises(ConstraintsError, match=message):
        parse_constraints(raw)


@pytest.mark.parametrize(
    "raw",
    [
        b'[services.maps]\nsecrets = ["AIzaSyD-not-a-name-1234"]\n',
        b'[services.maps]\nsecrets = ["GOOGLE_MAPS_API_KEY=abc123"]\n',
        b'[services.maps]\nGOOGLE_MAPS_API_KEY = "abc123"\n',
        b'[services.maps]\nsecrets = { GOOGLE_MAPS_API_KEY = "abc123" }\n',
    ],
)
def test_a_secret_value_in_the_file_is_refused(raw: bytes) -> None:
    """The file is public with the repository, so it may name secrets but never hold one."""
    with pytest.raises(ConstraintsError, match="only secret names") as error:
        parse_constraints(raw)

    assert "abc123" not in str(error.value)


def test_a_service_without_secrets_parses() -> None:
    """A service that needs no secret is a valid declaration."""
    assert parse_constraints(b"[services.cache]\n").services == {"cache": ServiceSpec()}


@pytest.mark.parametrize(
    ("raw", "key"),
    [
        (b'[proposer]\ntools = ["bash"]\n', "[proposer] tools"),
        (b"[proposer]\nnetwork = []\n", "[proposer] network"),
        (b'[proposer]\nnetwork = ["pypi.org"]\n', "[proposer] network"),
        (b'[services.maps]\n[proposer]\nservices = ["maps"]\n', "[proposer] services"),
        (b'[target.planner]\nentry = "agents/planner.py:plan"\n', "[target.planner]"),
        (b"[services.cache]\n", "[services.cache]"),
        (b'[metric]\nfiles = ["eval/scorer.py"]\n', "[metric] files"),
        (b'[metric]\nexploits = "eval/exploits"\n', "[metric] exploits"),
        (b'[metric]\nreference = "eval/reference.json"\n', "[metric] reference"),
    ],
)
def test_a_run_refuses_environment_keys_it_cannot_enforce_yet(tmp_path: Path, raw: bytes, key: str) -> None:
    """Until SKY-352, a repository run fails on these keys instead of silently ignoring them."""
    parse_constraints(raw)

    with pytest.raises(ConstraintsError, match="cannot enforce") as error:
        read_constraints(_archive(tmp_path, {CONSTRAINTS_FILE: raw}))

    assert key in str(error.value)
    assert "SKY-352" in str(error.value)


def test_a_run_names_every_unenforced_key(tmp_path: Path) -> None:
    """One message lists all the keys, so the user can fix the file in one pass."""
    with pytest.raises(ConstraintsError) as error:
        read_constraints(_archive(tmp_path, {CONSTRAINTS_FILE: _ENVIRONMENT}))

    message = str(error.value)
    for key in ("[proposer] network", "[target.planner]", "[target.router]", "[services.maps]", "[metric] files"):
        assert key in message


@pytest.mark.parametrize(
    "raw",
    [
        _CFCA + _RULES,
        b'[proposer]\nillegal = ["Do not edit tests."]\n',
        b'[metric]\nexploits = ".optimize/exploits"\n',
        b"[proposer]\n[metric]\n",
    ],
)
def test_a_run_accepts_old_files_and_proposer_illegal(tmp_path: Path, raw: bytes) -> None:
    """Old files and keys the run already enforces still stage."""
    assert read_constraints(_archive(tmp_path, {CONSTRAINTS_FILE: raw})) == parse_constraints(raw)
