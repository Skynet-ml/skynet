"""Offline checks for account.py: help text, target URLs and the forget rule.

Run: backend/.venv/bin/python -m pytest .agents/skills/skynet-test-accounts/test_account.py -q
No AWS login and no network are needed.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "account.py"
SPEC = importlib.util.spec_from_file_location("account", SCRIPT)
account = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(account)

HELP_CASES = ("register never succeeded", "the preview database is gone", "delete printed 'account delete: HTTP 200'")
SKILL_CASES = (
    "`register` never succeeded",
    "the preview database is gone",
    "`delete` printed `account delete: HTTP 200`",
)


def help_text(*args):
    """Return the ``--help`` output of account.py with its whitespace collapsed.

    Args:
        *args: Subcommand before ``--help``, or nothing.

    Returns:
        The help text. The call fails the test when the exit code is not 0.
    """
    out = subprocess.run([sys.executable, str(SCRIPT), *args, "--help"], capture_output=True, text=True, check=True)
    return " ".join(out.stdout.split())


@pytest.mark.parametrize("cmd", [(), ("create",), ("env",), ("delete",), ("forget",), ("list",)])
def test_every_command_prints_help(cmd):
    """Exit 0 for each command; argparse fails on a stray % in a help string."""
    assert "usage:" in help_text(*cmd)


def test_forget_help_names_all_cases():
    """Name the three cases in which the account no longer exists."""
    text = help_text("forget")
    for case in HELP_CASES:
        assert case in text


def test_skill_forget_rule_names_all_cases():
    """Keep the SKILL.md forget rule in step with the help text."""
    skill = " ".join((HERE / "SKILL.md").read_text().split())
    rule = skill[skill.index("`account.py forget <id>` removes only the secret") :]
    rule = rule[: rule.index("Never use `forget` while the account still exists")]
    for case in SKILL_CASES:
        assert case in rule


def test_targets_staging_and_preview():
    """Return the app and API hosts of each target."""
    assert account.targets("staging") == ("https://staging.skynetml.com", "https://api-staging.skynetml.com")
    assert account.targets("pr-12") == (
        "https://pr-12.staging.skynetml.com",
        "https://api-pr-12.staging.skynetml.com",
    )


@pytest.mark.parametrize("target", ["production", "pr-", "pr-12x", "pr-1.example.com", ""])
def test_other_targets_stop(target):
    """Stop before any call for production or a malformed preview name."""
    with pytest.raises(SystemExit):
        account.targets(target)


@pytest.mark.parametrize(
    ("stderr", "gone"),
    [
        ("(ResourceNotFoundException) ...", True),
        ("... it was marked for deletion.", True),
        ("(AccessDeniedException) ...", False),
        ("Unable to locate credentials", False),
    ],
)
def test_secret_gone(stderr, gone):
    """Treat only a missing or just-deleted secret as gone."""
    assert account.secret_gone(SimpleNamespace(stderr=stderr)) is gone
