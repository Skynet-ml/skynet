"""Object stores for the agent step log packs (SKY-710).

:class:`LocalObjectStore` keeps each object as a file under one folder. Tests
and local development use it. :class:`S3ObjectStore` keeps them in the S3
bucket that ``SKYNET_TREE_BUCKET`` names. It gets its credentials from the
default boto3 chain; the code holds none.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from ..config import settings

try:
    import boto3
except ImportError:
    boto3 = None


class LocalObjectStore:
    """Objects as files under one root folder; the key is the relative path."""

    def __init__(self, root: str | Path) -> None:
        """Bind the store to a root folder, and create the folder when it is missing.

        Args:
            root: The folder that holds the objects.
        """
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        """Return the file path of a key.

        Args:
            key: The object key.

        Returns:
            The path under the root.

        Raises:
            ValueError: When the key points outside the root.
        """
        path = (self._root / key).resolve()
        if not path.is_relative_to(self._root.resolve()):
            raise ValueError(f"object key leaves the store root: {key!r}")
        return path

    def put(self, key: str, data: bytes) -> None:
        """Write one whole object; a reader sees the old file or the full new one.

        Args:
            key: The object key.
            data: The object bytes.
        """
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".put-")
        with os.fdopen(handle, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        Path(temporary).replace(path)

    def get(self, key: str, offset: int = 0, length: int | None = None) -> bytes:
        """Read a byte range of one object.

        Args:
            key: The object key.
            offset: The first byte to read.
            length: The number of bytes to read; ``None`` reads to the end.

        Returns:
            The bytes of the range.

        Raises:
            KeyError: When no object is filed under ``key``.
        """
        try:
            with self._path(key).open("rb") as file:
                file.seek(offset)
                return file.read() if length is None else file.read(length)
        except FileNotFoundError as exc:
            raise KeyError(key) from exc

    def list(self, prefix: str) -> list[str]:
        """Return the keys that start with ``prefix``, in key order.

        Args:
            prefix: The key prefix.

        Returns:
            The matching keys; half-written temporary files are not keys.
        """
        keys = (
            path.relative_to(self._root).as_posix()
            for path in self._root.rglob("*")
            if path.is_file() and not path.name.startswith(".put-")
        )
        return sorted(key for key in keys if key.startswith(prefix))


class S3ObjectStore:
    """Objects in one S3 bucket, S3 Standard class."""

    def __init__(self, bucket: str | None = None, client: Any = None) -> None:
        """Bind the store to a bucket.

        Args:
            bucket: The bucket name; ``None`` reads ``SKYNET_TREE_BUCKET``.
            client: A boto3 S3 client; ``None`` builds one from the default
                credential chain.

        Raises:
            ValueError: When no bucket is set.
            RuntimeError: When ``client`` is ``None`` and boto3 is not installed.
        """
        self._bucket = bucket or settings.tree_bucket
        if not self._bucket:
            raise ValueError("SKYNET_TREE_BUCKET is not set")
        if client is None:
            if boto3 is None:
                raise RuntimeError("boto3 is not installed")
            client = boto3.client("s3")
        self._client = client

    def put(self, key: str, data: bytes) -> None:
        """Write one whole object.

        Args:
            key: The object key.
            data: The object bytes.
        """
        self._client.put_object(Bucket=self._bucket, Key=key, Body=data, StorageClass="STANDARD")

    def get(self, key: str, offset: int = 0, length: int | None = None) -> bytes:
        """Read a byte range of one object with an HTTP range request.

        Args:
            key: The object key.
            offset: The first byte to read.
            length: The number of bytes to read; ``None`` reads to the end.

        Returns:
            The bytes of the range.

        Raises:
            KeyError: When no object is filed under ``key``.
        """
        if length == 0:
            return b""
        end = "" if length is None else str(offset + length - 1)
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key, Range=f"bytes={offset}-{end}")
        except self._client.exceptions.NoSuchKey as exc:
            raise KeyError(key) from exc
        return response["Body"].read()

    def list(self, prefix: str) -> list[str]:
        """Return the keys that start with ``prefix``, in key order.

        Args:
            prefix: The key prefix.

        Returns:
            The matching keys.
        """
        pages = self._client.get_paginator("list_objects_v2").paginate(Bucket=self._bucket, Prefix=prefix)
        return sorted(item["Key"] for page in pages for item in page.get("Contents", []))
