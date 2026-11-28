"""Read the root ``.env`` file of a local folder into secret names and values (SKY-994).

A local run imports this file into the user's saved secrets. Only the folder's
own root ``.env`` is read. The parser follows the common dotenv rules:
``export`` prefixes, single and double quotes, full-line and inline comments,
and blank lines. A line that it cannot use is skipped and reported by line
number and reason. The report never carries a value, and it carries a name
only when the name looks like a name, so a value on a broken line never
leaks into a response.

A value that runs over more lines without quotes (a PEM key pasted as is) has
body lines that can look like ``NAME=value``. Value text must never become a
name, so:

- An unquoted ``-----BEGIN`` value with no ``-----END`` on the same line skips
  every line up to its ``-----END`` line.
- A line with no ``=`` skips every line up to the next blank or comment line.
- A line whose value is only ``=`` is base64 padding, not a pair.
- After a long unquoted value made only of base64 characters, each next line
  made only of base64 characters and optional ``=`` padding continues it. The
  value and every such line are skipped. This rule also skips a long key
  followed by an empty ``OPT=`` line: a skip with a report is safer than a
  saved key with its tail cut off.

Those body lines are reported by number only.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from ..models.blackbox import REPO_SECRET_NAME_PATTERN
from .saved_secrets import MAX_SECRET_BYTES

ENV_FILE = ".env"
ENV_FILE_MAX_BYTES = 256 * 1024

_NAME = re.compile(REPO_SECRET_NAME_PATTERN)
# A rejected name is shown back only when it is short and plain, because the
# text before "=" on a broken line can be part of a value.
_SHOWN_NAME = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
_EXPORT = re.compile(r"^export\s+")
_PEM_BEGIN = "-----BEGIN"
_PEM_END = "-----END"
# A long unquoted value of base64 characters with no padding can be the first
# line of a wrapped key; a base64 line after it is then more of the same value.
_WRAPPED_HEAD = re.compile(r"^[A-Za-z0-9+/]{40,}$")
_CONTINUATION = re.compile(r"^[A-Za-z0-9+/]+=*$")
_DOUBLE_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\", "$": "$"}


class EnvFileError(ValueError):
    """Explain why a folder's ``.env`` file cannot be read.

    ``reason`` is one of ``missing``, ``unsafe``, ``too_large`` or ``unreadable``.
    """

    def __init__(self, reason: str) -> None:
        """Keep the reason code.

        Args:
            reason: Stable code for the failure.
        """
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class EnvSkip:
    """One ``.env`` line that the import does not use."""

    line: int
    name: str | None
    reason: str


@dataclass(frozen=True)
class EnvFile:
    """The usable pairs of a ``.env`` file and the lines it skipped."""

    values: dict[str, str]
    skipped: tuple[EnvSkip, ...]


def _shown(name: str) -> str | None:
    """Return a rejected name only when it is safe to show.

    Args:
        name: Text before ``=`` on a skipped line.

    Returns:
        The name, or None when it could be part of a value.
    """
    return name if _SHOWN_NAME.match(name) else None


def _quoted(lines: list[str], start: int, rest: str) -> tuple[str | None, int]:
    """Read a quoted value that can continue over more lines.

    Args:
        lines: All lines of the file.
        start: Index of the line where the value starts.
        rest: Text after the opening quote on that line.

    Returns:
        The value (None when the quote never closes) and the index of the
        line where the value ends.
    """
    quote = rest[0]
    text = rest[1:]
    index = start
    out: list[str] = []
    while True:
        pos = 0
        while pos < len(text):
            char = text[pos]
            if quote == '"' and char == "\\" and pos + 1 < len(text):
                nxt = text[pos + 1]
                out.append(_DOUBLE_ESCAPES.get(nxt, "\\" + nxt))
                pos += 2
                continue
            if char == quote:
                return "".join(out), index
            out.append(char)
            pos += 1
        index += 1
        if index >= len(lines):
            return None, index - 1
        out.append("\n")
        text = lines[index]


def parse_env(text: str) -> EnvFile:
    """Parse dotenv text into names and values.

    A later line with the same name wins, as in most dotenv loaders.

    Args:
        text: Content of the ``.env`` file.

    Returns:
        The usable pairs and the skipped lines.
    """
    lines = text.splitlines()
    values: dict[str, str] = {}
    skipped: list[EnvSkip] = []
    # The name and line number of the value on the line before, when that value
    # could be the head of a wrapped base64 key.
    head: tuple[str, int] | None = None
    index = 0
    while index < len(lines):
        number = index + 1
        line = lines[index].strip()
        before, head = head, None
        if not line or line.startswith("#"):
            index += 1
            continue
        if before and _CONTINUATION.match(line):
            head_name, head_number = before
            values.pop(head_name, None)
            skipped.append(EnvSkip(head_number, head_name, "unquoted_multiline"))
            while index < len(lines) and _CONTINUATION.match(lines[index].strip()):
                skipped.append(EnvSkip(index + 1, None, "unquoted_multiline"))
                index += 1
            continue
        line = _EXPORT.sub("", line, count=1)
        name, equals, raw = line.partition("=")
        name = name.strip()
        if equals and raw.strip() and not raw.strip().strip("="):
            skipped.append(EnvSkip(number, None, "invalid_line"))
            index += 1
            continue
        if not equals:
            # The line is likely the body of a broken value, and so may be the
            # lines after it; none of them is read as a name.
            if before:
                values.pop(before[0], None)
                skipped.append(EnvSkip(before[1], before[0], "unquoted_multiline"))
            while index < len(lines) and lines[index].strip() and not lines[index].strip().startswith("#"):
                skipped.append(EnvSkip(index + 1, None, "invalid_line"))
                index += 1
            continue
        spaced = raw[:1].isspace()
        raw = raw.lstrip()
        if raw.startswith(_PEM_BEGIN) and _PEM_END not in raw:
            skipped.append(EnvSkip(number, _shown(name), "unquoted_multiline"))
            index += 1
            while index < len(lines):
                body = lines[index].strip()
                skipped.append(EnvSkip(index + 1, None, "unquoted_multiline"))
                index += 1
                if body.startswith(_PEM_END):
                    break
            continue
        if raw[:1] in ("'", '"'):
            value, index = _quoted(lines, index, raw)
            if value is None:
                skipped.append(EnvSkip(number, _shown(name), "unclosed_quote"))
                break
        elif spaced and raw.startswith("#"):
            # ``NAME= # note`` is an empty value with a comment, as in common dotenv loaders.
            value = ""
        else:
            value = re.split(r"\s#", raw, maxsplit=1)[0].rstrip()
            if _NAME.match(name) and _WRAPPED_HEAD.match(value):
                head = (name, number)
        index += 1
        if not _NAME.match(name):
            skipped.append(EnvSkip(number, _shown(name), "invalid_name"))
        elif not value:
            skipped.append(EnvSkip(number, name, "empty_value"))
        elif len(value.encode("utf-8")) > MAX_SECRET_BYTES:
            skipped.append(EnvSkip(number, name, "too_long"))
        else:
            values[name] = value
    return EnvFile(values=values, skipped=tuple(skipped))


def read_env_file(folder: str) -> EnvFile:
    """Read and parse the root ``.env`` file of a local folder.

    The folder itself must be a real directory, not a symbolic link. The
    ``.env`` file may be a symbolic link only when its target stays inside
    the folder. Nothing below the root is read.

    Args:
        folder: Absolute path of the folder.

    Returns:
        The parsed file.

    Raises:
        EnvFileError: When the folder or file is missing, unsafe, too large
            or unreadable.
    """
    base = Path(folder)
    if not base.is_absolute():
        raise EnvFileError("unsafe")
    try:
        if base.is_symlink() or not base.is_dir():
            raise EnvFileError("missing")
        root = base.resolve(strict=True)
        path = base / ENV_FILE
        if not path.exists() and not path.is_symlink():
            raise EnvFileError("missing")
        target = path.resolve(strict=True)
    except EnvFileError:
        raise
    except (OSError, RuntimeError) as exc:
        raise EnvFileError("unreadable") from exc
    if root not in target.parents:
        raise EnvFileError("unsafe")
    try:
        # O_NOFOLLOW on the resolved path stops a swap to a new link between
        # the check above and the read.
        fd = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError as exc:
        raise EnvFileError("unreadable") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise EnvFileError("unsafe")
        if info.st_size > ENV_FILE_MAX_BYTES:
            raise EnvFileError("too_large")
        chunks: list[bytes] = []
        size = 0
        while size <= ENV_FILE_MAX_BYTES:
            chunk = os.read(fd, ENV_FILE_MAX_BYTES + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        data = b"".join(chunks)
    except OSError as exc:
        raise EnvFileError("unreadable") from exc
    finally:
        os.close(fd)
    if len(data) > ENV_FILE_MAX_BYTES:
        raise EnvFileError("too_large")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise EnvFileError("unreadable") from exc
    return parse_env(text)
