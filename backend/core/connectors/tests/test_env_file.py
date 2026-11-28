"""Tests for reading a local folder's root ``.env`` file (SKY-994)."""

from __future__ import annotations

import os

import pytest

from core.connectors.env_file import ENV_FILE_MAX_BYTES, EnvFileError, EnvSkip, parse_env, read_env_file
from core.connectors.saved_secrets import MAX_SECRET_BYTES


def test_parse_reads_export_quotes_comments_and_blank_lines() -> None:
    """Parse the common dotenv forms into names and values."""
    text = (
        "# a comment\n"
        "\n"
        "PLAIN=one\n"
        "export EXPORTED=two\n"
        "SPACED = three  \n"
        "INLINE=four # a note\n"
        "HASHED=fi#ve\n"
        "SINGLE='six # not a comment \\n'\n"
        'DOUBLE="seven\\nline"\n'
        'MULTI="first\n'
        'second"\n'
        "   # an indented comment\n"
    )
    parsed = parse_env(text)
    assert parsed.values == {
        "PLAIN": "one",
        "EXPORTED": "two",
        "SPACED": "three",
        "INLINE": "four",
        "HASHED": "fi#ve",
        "SINGLE": "six # not a comment \\n",
        "DOUBLE": "seven\nline",
        "MULTI": "first\nsecond",
    }
    assert parsed.skipped == ()


def test_parse_skips_and_lists_bad_names_without_values() -> None:
    """List lines with a bad name, and never put a value in the list."""
    text = "1BAD=sentinel-a\nMY-KEY=sentinel-b\nab+/cd=sentinel-c\nEMPTY=\nno equals here\n\nGOOD=ok\n"
    parsed = parse_env(text)
    assert parsed.values == {"GOOD": "ok"}
    assert parsed.skipped == (
        EnvSkip(1, "1BAD", "invalid_name"),
        EnvSkip(2, "MY-KEY", "invalid_name"),
        EnvSkip(3, None, "invalid_name"),
        EnvSkip(4, "EMPTY", "empty_value"),
        EnvSkip(5, None, "invalid_line"),
    )
    assert "sentinel" not in repr(parsed.skipped)


def test_parse_keeps_a_multiline_key_out_of_the_names() -> None:
    """Read a quoted PEM body as one value, not as many names."""
    text = 'KEY="-----BEGIN KEY-----\nMIIEabc=\n-----END KEY-----"\nNEXT=1\n'
    parsed = parse_env(text)
    assert parsed.values == {"KEY": "-----BEGIN KEY-----\nMIIEabc=\n-----END KEY-----", "NEXT": "1"}
    assert parsed.skipped == ()


def test_parse_never_reads_an_unquoted_key_body_as_names() -> None:
    """Skip the body of an unquoted PEM value by line number only."""
    text = "KEY=-----BEGIN KEY-----\nMIIEabcdSENTINEL\nAbCdEfGh12==\nZx9Q=wSENTINEL\n-----END KEY-----\nNEXT=1\n"
    parsed = parse_env(text)
    assert parsed.values == {"NEXT": "1"}
    assert parsed.skipped == (
        EnvSkip(1, "KEY", "unquoted_multiline"),
        *(EnvSkip(line, None, "unquoted_multiline") for line in range(2, 6)),
    )


def test_parse_skips_a_broken_block_up_to_the_next_blank_line() -> None:
    """Skip the lines after a line with no ``=``, so a fragment with one ``=`` is no name."""
    text = "FIRST=1\nKEY=abc\ndefSENTINEL\nAbCdEfGh12=xySENTINEL\nQRst==\n\nLATER=2\n"
    parsed = parse_env(text)
    assert parsed.values == {"FIRST": "1", "KEY": "abc", "LATER": "2"}
    assert parsed.skipped == tuple(EnvSkip(line, None, "invalid_line") for line in (3, 4, 5))
    assert "SENTINEL" not in repr(parsed.skipped)
    assert "AbCdEfGh12" not in repr(parsed)


def test_parse_stops_at_an_unclosed_quote() -> None:
    """Skip the rest of the file when a quote never closes."""
    parsed = parse_env('FIRST=1\nOPEN="never closes\nLATER=2\n')
    assert parsed.values == {"FIRST": "1"}
    assert parsed.skipped == (EnvSkip(2, "OPEN", "unclosed_quote"),)


def test_parse_lets_a_later_line_win_and_skips_a_too_long_value() -> None:
    """Keep the last value of a repeated name and skip a value over the store limit."""
    text = f"NAME=old\nNAME=new\nHUGE={'x' * (MAX_SECRET_BYTES + 1)}\n"
    parsed = parse_env(text)
    assert parsed.values == {"NAME": "new"}
    assert parsed.skipped == (EnvSkip(3, "HUGE", "too_long"),)


def test_read_parses_the_root_env_file(tmp_path) -> None:
    """Read the folder's root ``.env`` and nothing below it."""
    (tmp_path / ".env").write_text("TOP=1\r\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / ".env").write_text("DEEP=1\n", encoding="utf-8")
    assert read_env_file(str(tmp_path)).values == {"TOP": "1"}


def test_read_reports_a_missing_file_or_folder(tmp_path) -> None:
    """Report ``missing`` when there is no folder or no ``.env``."""
    with pytest.raises(EnvFileError) as no_file:
        read_env_file(str(tmp_path))
    assert no_file.value.reason == "missing"
    with pytest.raises(EnvFileError) as no_folder:
        read_env_file(str(tmp_path / "gone"))
    assert no_folder.value.reason == "missing"


def test_read_refuses_a_relative_path() -> None:
    """Refuse a folder path that is not absolute."""
    with pytest.raises(EnvFileError) as caught:
        read_env_file("relative/folder")
    assert caught.value.reason == "unsafe"


def test_read_refuses_a_link_that_leaves_the_folder(tmp_path) -> None:
    """Refuse a ``.env`` link whose target is outside the folder."""
    outside = tmp_path / "outside.env"
    outside.write_text("STOLEN=1\n", encoding="utf-8")
    folder = tmp_path / "project"
    folder.mkdir()
    (folder / ".env").symlink_to(outside)
    with pytest.raises(EnvFileError) as caught:
        read_env_file(str(folder))
    assert caught.value.reason == "unsafe"


def test_read_follows_a_link_that_stays_inside_the_folder(tmp_path) -> None:
    """Allow a ``.env`` link to a file inside the folder."""
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "local.env").write_text("INSIDE=1\n", encoding="utf-8")
    (tmp_path / ".env").symlink_to(tmp_path / "config" / "local.env")
    assert read_env_file(str(tmp_path)).values == {"INSIDE": "1"}


def test_read_refuses_a_folder_that_is_a_link(tmp_path) -> None:
    """Refuse a folder path that is itself a symbolic link."""
    real = tmp_path / "real"
    real.mkdir()
    (real / ".env").write_text("A=1\n", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(EnvFileError) as caught:
        read_env_file(str(link))
    assert caught.value.reason == "missing"


def test_read_refuses_a_folder_named_env(tmp_path) -> None:
    """Refuse a ``.env`` that is a folder, not a file."""
    (tmp_path / ".env").mkdir()
    with pytest.raises(EnvFileError) as caught:
        read_env_file(str(tmp_path))
    assert caught.value.reason in {"unsafe", "unreadable"}


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs named pipes")
def test_read_refuses_a_named_pipe_without_blocking(tmp_path) -> None:
    """Refuse a ``.env`` named pipe and return at once."""
    os.mkfifo(tmp_path / ".env")
    with pytest.raises(EnvFileError) as caught:
        read_env_file(str(tmp_path))
    assert caught.value.reason == "unsafe"


def test_read_refuses_a_file_over_the_size_limit(tmp_path) -> None:
    """Report ``too_large`` for a ``.env`` over the limit."""
    (tmp_path / ".env").write_bytes(b"A=" + b"x" * ENV_FILE_MAX_BYTES)
    with pytest.raises(EnvFileError) as caught:
        read_env_file(str(tmp_path))
    assert caught.value.reason == "too_large"


def test_read_refuses_a_file_that_is_not_utf8(tmp_path) -> None:
    """Report ``unreadable`` for a ``.env`` that is not UTF-8."""
    (tmp_path / ".env").write_bytes(b"A=\xff\xfe\n")
    with pytest.raises(EnvFileError) as caught:
        read_env_file(str(tmp_path))
    assert caught.value.reason == "unreadable"
