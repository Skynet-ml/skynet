"""Run the S3 connector against real S3 calls on the local AWS Emulator.

The connector signs its own requests and sends them through
``core.connectors.transport``. These tests check its SigV4 signing, its XML
parsing and its key quoting against an S3 API, with no mocks.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator

import httpx
import pytest

from core.api.errors import DomainError
from core.connectors import s3, transport
from core.connectors.vault import ConnectorSecret
from tests.aws_emulator.conftest import REGION, Emulator

try:
    import boto3
except ImportError:
    boto3 = None

CSV = b"name,score\nada,3\nlin,5\n"
# Spaces, plus signs and parentheses need exact quoting in the signed path.
ODD_KEY = "data/odd name+v1 (copy).csv"


def _fields(emulator: Emulator, **extra: str) -> dict[str, str]:
    """Build the connector form fields for the emulator.

    Args:
        emulator: The connection facts.
        **extra: Fields to add or replace.

    Returns:
        The fields that ``s3.verify_credentials`` reads.
    """
    return {
        "access_key_id": emulator.access_key_id,
        "secret_access_key": emulator.secret_access_key,
        "region": REGION,
        "endpoint_url": emulator.endpoint,
        **extra,
    }


def _secret(emulator: Emulator) -> ConnectorSecret:
    """Validate the emulator key pair and wrap it as a stored connector.

    Args:
        emulator: The connection facts.

    Returns:
        The secret that the browse, preview and import calls take.
    """
    credential = s3.verify_credentials(_fields(emulator))
    return ConnectorSecret(
        access_token=credential.secret,
        refresh_token=None,
        expires_at=None,
        auth_method=credential.auth_method,
        account_label=credential.account_label,
    )


@pytest.fixture(autouse=True)
def emulator_transport(aws_emulator: Emulator, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Send connector calls through a client that trusts only the emulator CA.

    Args:
        aws_emulator: The connection facts.
        monkeypatch: Replaces the shared transport client for this test.

    Yields:
        Nothing. The client closes after the test.
    """
    client = httpx.Client(follow_redirects=True, verify=aws_emulator.ssl_context())
    monkeypatch.setattr(transport, "CLIENT", client)
    yield
    client.close()


@pytest.fixture
def bucket() -> Iterator[str]:
    """Create a unique bucket with test objects, and delete it after the test.

    Yields:
        The bucket name.
    """
    name = f"skynet-test-{uuid.uuid4().hex[:12]}"
    client = boto3.client("s3")
    client.create_bucket(Bucket=name)
    try:
        for key in ("top.csv", "data/a.csv", "data/sub/b.jsonl", "data/notes.txt", ODD_KEY):
            body = b'{"x": 1}\n' if key.endswith(".jsonl") else CSV
            client.put_object(Bucket=name, Key=key, Body=body)
        yield name
    finally:
        for page in client.get_paginator("list_objects_v2").paginate(Bucket=name):
            for item in page.get("Contents", []):
                client.delete_object(Bucket=name, Key=item["Key"])
        client.delete_bucket(Bucket=name)


def test_verify_credentials_accepts_the_key_pair(aws_emulator: Emulator, bucket: str) -> None:
    """Check that a valid key pair, also with a pinned bucket, passes validation."""
    credential = s3.verify_credentials(_fields(aws_emulator))
    assert credential.auth_method == "credentials"
    assert credential.account_label == aws_emulator.access_key_id
    assert json.loads(credential.secret)["endpoint_url"] == aws_emulator.endpoint

    pinned = s3.verify_credentials(_fields(aws_emulator, bucket=bucket))
    assert pinned.account_label == bucket


def test_browse_lists_buckets_and_objects(aws_emulator: Emulator, bucket: str) -> None:
    """Check bucket listing, prefix listing, folders, filters and the search."""
    secret = _secret(aws_emulator)

    assert bucket in {entry.name for entry in s3.browse(secret, "", "")}

    root = s3.browse(secret, bucket, "")
    assert [(entry.kind, entry.name, entry.ref) for entry in root] == [
        ("folder", "data", f"{bucket}/data/"),
        ("file", "top.csv", f"{bucket}/top.csv"),
    ]
    assert root[1].size == len(CSV)

    data = s3.browse(secret, f"{bucket}/data/", "")
    # The .txt key has no supported extension, so the listing leaves it out.
    assert [(entry.kind, entry.name) for entry in data] == [
        ("folder", "sub"),
        ("file", "a.csv"),
        ("file", "odd name+v1 (copy).csv"),
    ]
    assert [entry.name for entry in s3.browse(secret, f"{bucket}/data/", "ODD")] == ["odd name+v1 (copy).csv"]


def test_get_object_in_full_and_ranged(aws_emulator: Emulator, bucket: str) -> None:
    """Check full and ranged downloads, also for a key that needs quoting."""
    secret = _secret(aws_emulator)
    config = json.loads(secret.access_token)

    for key in ("top.csv", ODD_KEY):
        fetch = s3._fetcher(config, bucket, key)
        assert fetch(None) == (CSV, False)
        assert fetch(10) == (CSV[:10], True)

    assert s3.preview(secret, f"{bucket}/{ODD_KEY}")["rows"] == [
        {"name": "ada", "score": "3"},
        {"name": "lin", "score": "5"},
    ]
    rows, _, filename = s3.import_ref(secret, f"{bucket}/{ODD_KEY}")
    assert [row["name"] for row in rows] == ["ada", "lin"]
    assert filename == "odd name+v1 (copy).csv"


def test_wrong_secret_is_rejected(aws_emulator: Emulator, bucket: str) -> None:
    """Check that a wrong secret key maps to the connector's rejected errors."""
    wrong = _fields(aws_emulator, secret_access_key="wrong-" + uuid.uuid4().hex)

    with pytest.raises(DomainError) as invalid:
        s3.verify_credentials(wrong)
    assert (invalid.value.code, invalid.value.status_code) == ("connectors.invalid_credentials", 400)

    secret = ConnectorSecret(json.dumps({**wrong, "bucket": ""}), None, None, "credentials")
    with pytest.raises(DomainError) as rejected:
        s3.browse(secret, bucket, "")
    assert (rejected.value.code, rejected.value.status_code) == ("connectors.rejected", 409)
