"""Connect the opt-in AWS Emulator tests to a local emulator, and refuse real AWS.

Start the emulator with ``scripts/aws-emulator.sh start``. Read
``docs/agents/aws-emulator.md`` for the rules.

The tests skip when no emulator answers. ``AWS_EMULATOR_REQUIRED=1`` turns
each skip into a failure, so ``just test-aws-emulator`` cannot pass silently.
Every test goes through the ``aws_emulator`` fixture. It stops the test
unless the endpoint is ``https://127.0.0.1:<port>`` and STS returns the
emulator account ``000000000000``.
"""

from __future__ import annotations

import configparser
import os
import ssl
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

try:
    import boto3
except ImportError:
    boto3 = None

PORT = os.environ.get("AWS_EMULATOR_PORT", "9297")
ENDPOINT = f"https://127.0.0.1:{PORT}"
EMULATOR_ACCOUNT = "000000000000"
REGION = "us-east-1"
FILES = Path.home() / ".aws" / "emulator" / PORT

# Each of these can send a call to a real AWS account.
REAL_CREDENTIAL_VARS = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_ROLE_ARN",
    "AWS_ROLE_SESSION_NAME",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN",
)


@dataclass(frozen=True)
class Emulator:
    """The connection facts of one running emulator.

    The key pair has ``repr=False``, so a failed assertion never prints it.
    """

    endpoint: str
    ca_bundle: Path
    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)

    def ssl_context(self) -> ssl.SSLContext:
        """Build an SSL context that trusts only the emulator's CA.

        Returns:
            A verifying context for httpx. It never turns verification off.
        """
        return ssl.create_default_context(cafile=str(self.ca_bundle))


def _unavailable(reason: str) -> None:
    """Skip the test, or fail it when the run requires the emulator.

    Args:
        reason: Why the emulator is not usable.
    """
    if os.environ.get("AWS_EMULATOR_REQUIRED") == "1":
        pytest.fail(f"AWS_EMULATOR_REQUIRED=1, but {reason}")
    pytest.skip(reason)


def _read_credentials(path: Path) -> tuple[str, str]:
    """Read the emulator's default key pair without printing it.

    Args:
        path: The emulator's ``emulator_credentials`` file.

    Returns:
        ``(access_key_id, secret_access_key)``, or empty strings when absent.
    """
    parser = configparser.ConfigParser()
    parser.read(path)
    return (
        parser.get("default", "aws_access_key_id", fallback=""),
        parser.get("default", "aws_secret_access_key", fallback=""),
    )


def _healthy(ca_bundle: Path) -> bool:
    """Ask the emulator's admin health endpoint if it is ready.

    Args:
        ca_bundle: The emulator's CA certificate.

    Returns:
        True when the endpoint answers ``ok``.
    """
    try:
        context = ssl.create_default_context(cafile=str(ca_bundle))
        response = httpx.get(f"{ENDPOINT}/__admin/health", verify=context, timeout=2)
    except (httpx.HTTPError, ssl.SSLError, OSError):
        return False
    return response.status_code == 200 and response.text.strip() == "ok"


@pytest.fixture(autouse=True)
def aws_emulator(monkeypatch: pytest.MonkeyPatch) -> Emulator:
    """Point AWS clients at the local emulator, then check that they reach it.

    Args:
        monkeypatch: Sets the connect variables for this test only.

    Returns:
        The connection facts of the emulator.
    """
    if boto3 is None:
        _unavailable("boto3 is not installed")
    credentials, config, ca_bundle = FILES / "emulator_credentials", FILES / "config", FILES / "ca.pem"
    if not all(path.is_file() for path in (credentials, config, ca_bundle)):
        _unavailable(f"no AWS Emulator files in {FILES}; run scripts/aws-emulator.sh start")
    if not _healthy(ca_bundle):
        _unavailable(f"no AWS Emulator answers on {ENDPOINT}")

    for name in REAL_CREDENTIAL_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_ENDPOINT_URL", ENDPOINT)
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(credentials))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
    # The config file names a CA path inside the container; this one is on the host.
    monkeypatch.setenv("AWS_CA_BUNDLE", str(ca_bundle))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)

    # The guard checks what a client really uses, not only the constants above.
    sts = boto3.Session(profile_name="default").client("sts")
    if os.environ["AWS_ENDPOINT_URL"] != ENDPOINT or sts.meta.endpoint_url != ENDPOINT:
        pytest.fail(f"refused: the AWS endpoint is {sts.meta.endpoint_url}, not {ENDPOINT}")
    account = sts.get_caller_identity()["Account"]
    if account != EMULATOR_ACCOUNT:
        pytest.fail(f"refused: STS returned account {account}, not the emulator account {EMULATOR_ACCOUNT}")

    access_key_id, secret_access_key = _read_credentials(credentials)
    if not access_key_id or not secret_access_key:
        pytest.fail(f"no default key pair in {credentials}")
    return Emulator(ENDPOINT, ca_bundle, access_key_id, secret_access_key)
