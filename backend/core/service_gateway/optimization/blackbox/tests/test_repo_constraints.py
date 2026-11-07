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


def test_reading_the_file_accepts_environment_keys(tmp_path: Path) -> None:
    """Reading parses environment keys; the worker's preflight gate decides if a run can enforce them (SKY-352)."""
    assert read_constraints(_archive(tmp_path, {CONSTRAINTS_FILE: _ENVIRONMENT})) == parse_constraints(_ENVIRONMENT)


@pytest.mark.parametrize(
    "raw",
    [
        _CFCA + _RULES,
        b'[proposer]\nillegal = ["Do not edit tests."]\n',
        b"[proposer]\n[metric]\n",
    ],
)
def test_a_run_accepts_old_files_and_proposer_illegal(tmp_path: Path, raw: bytes) -> None:
    """Old files, ``[proposer] illegal`` (the judges enforce it) and empty sections still stage."""
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


_SERVICES = b'[services.maps]\nsecrets = ["MAPS_API_KEY"]\n[services.grader]\nsecrets = ["GRADER_KEY"]\n'


def test_run_and_metric_roles_default_to_no_mcp_and_the_run_services() -> None:
    """[run] gets no MCP file and no services; [metric] gets the [run] services (E44)."""
    plain = parse_constraints(b'[run]\nentry = "a.py:f"\n')
    inherited = parse_constraints(_SERVICES + b'[run]\nentry = "a.py:f"\nservices = ["maps"]\n')

    assert (plain.run_role, plain.metric_role) == (RoleEnvironment(), RoleEnvironment())
    assert inherited.run_role == RoleEnvironment(services=("maps",))
    assert inherited.metric_role == RoleEnvironment(services=("maps",))


def test_run_and_metric_accept_the_role_keys() -> None:
    """[run] and [metric] take mcp, services and illegal; [metric] keeps files, exploits and reference."""
    raw = _SERVICES + (
        b'[run]\nentry = "a.py:f"\nmcp = ".mcp.json"\nservices = ["maps"]\nillegal = ["No hard-coded answers."]\n'
        b'[metric]\nfiles = ["metric.py"]\nreference = "ref"\nservices = ["grader"]\nmcp = "eval/.mcp.json"\n'
        b'illegal = ["Never read the answers."]\n'
    )
    constraints = parse_constraints(raw)

    assert constraints.run_role == RoleEnvironment(
        mcp=".mcp.json", services=("maps",), illegal=("No hard-coded answers.",)
    )
    assert constraints.metric_role == RoleEnvironment(
        mcp="eval/.mcp.json", services=("grader",), illegal=("Never read the answers.",)
    )
    assert (constraints.metric.files, constraints.metric.reference) == (("metric.py",), "ref")


def test_metric_services_empty_list_gives_the_metric_no_services() -> None:
    """An explicit empty [metric] services overrides the [run] default."""
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
        (b"[metric]\nillegal = []\n", r"\[metric\] illegal must list 1 to"),
    ],
)
def test_bad_run_or_metric_role_keys_fail_with_a_reason(raw: bytes, message: str) -> None:
    """network and vcpus fail (one shared box, E37); tools points to mcp (E46); services must be declared."""
    with pytest.raises(ConstraintsError, match=message):
        parse_constraints(raw)


def test_an_mcp_file_in_the_snapshot_passes(tmp_path: Path) -> None:
    """read_constraints accepts an mcp path the snapshot holds, without reading its servers (E46)."""
    raw = b'[run]\nentry = "a.py:f"\nmcp = ".mcp.json"\n[metric]\nmcp = "eval/.mcp.json"\n'
    files = {CONSTRAINTS_FILE: raw, ".mcp.json": b"not json", "eval/.mcp.json": b"{}"}

    assert read_constraints(_archive(tmp_path, files)) == parse_constraints(raw)


@pytest.mark.parametrize("label", ["run", "metric"])
def test_a_missing_mcp_file_fails_with_a_reason(tmp_path: Path, label: str) -> None:
    """An mcp path that names no file in the snapshot fails the setup check (E46)."""
    entry = b'entry = "a.py:f"\n' if label == "run" else b""
    raw = b"[" + label.encode() + b"]\n" + entry + b'mcp = "tools/.mcp.json"\n'

    with pytest.raises(ConstraintsError, match=rf"\[{label}\] mcp names 'tools/\.mcp\.json', but .* no such file"):
        read_constraints(_archive(tmp_path, {CONSTRAINTS_FILE: raw, "tools/other.json": b"{}"}))


def test_a_service_with_its_own_url_and_one_secret_gets_the_bearer_header() -> None:
    """A user's own API takes an https url; with one secret and no headers, it sends Bearer ${SECRET} (E45)."""
    raw = b'[services.judge]\nurl = "https://llm.example.com/v1"\nsecrets = ["JUDGE_KEY"]\n'
    service = parse_constraints(raw).services["judge"]

    assert (service.url, service.headers) == ("https://llm.example.com/v1", None)
    assert service.request_headers == {"Authorization": "Bearer ${JUDGE_KEY}"}


def test_a_service_keeps_its_own_headers_with_placeholders() -> None:
    """headers values may use ${SECRET} placeholders for the service's own secrets, and replace the default."""
    raw = (
        b'[services.judge]\nurl = "https://llm.example.com:8443/v1"\nsecrets = ["JUDGE_KEY", "ORG"]\n'
        b'[services.judge.headers]\nx-api-key = "${JUDGE_KEY}"\nX-Org = "org-${ORG}"\n'
    )
    service = parse_constraints(raw).services["judge"]

    assert service.request_headers == {"x-api-key": "${JUDGE_KEY}", "X-Org": "org-${ORG}"}


def test_a_service_with_no_or_many_secrets_has_no_default_header() -> None:
    """The Bearer default needs exactly one secret."""
    raw = b'[services.a]\nurl = "https://a.example.com"\n[services.b]\nsecrets = ["K1", "K2"]\n'
    services = parse_constraints(raw).services

    assert (services["a"].request_headers, services["b"].request_headers) == ({}, {})


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
        "https://intranet/v1",
        "https://0x7f.1",
        "https://2130706433",
        "https://user:pw@llm.example.com",
        "https://llm.example.com:99999",
    ],
)
def test_a_bad_service_url_fails_with_a_reason(url: str) -> None:
    """url must be https on a public host name: no IP, no localhost, no credentials (SSRF guard, E45)."""
    raw = f'[services.judge]\nurl = "{url}"\n'.encode()

    with pytest.raises(ConstraintsError, match=r"\[services\.judge\] url "):
        parse_constraints(raw)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (
            b'[services.judge]\nsecrets = ["JUDGE_KEY"]\n[services.judge.headers]\nAuthorization = "Bearer ${OTHER}"\n',
            r"headers Authorization uses \$\{OTHER\}, but OTHER is not in its secrets",
        ),
        (b'[services.judge]\nheaders = "Bearer x"\n', r"\[services\.judge\] headers must be a table"),
        (b'[services.judge.headers]\n"bad name" = "x"\n', r"header name 'bad name' must use only"),
        (b"[services.judge.headers]\nX-N = 3\n", r"headers X-N must be one line"),
        (b'[services.judge.headers]\nX-N = "a\\r\\nb"\n', r"headers X-N must be one line"),
    ],
)
def test_bad_service_headers_fail_with_a_reason(raw: bytes, message: str) -> None:
    """Each placeholder must name one of the service's secrets, and each header is one line of text."""
    with pytest.raises(ConstraintsError, match=message):
        parse_constraints(raw)
