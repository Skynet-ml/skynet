"""Tests for the shared email renderer (notification and account emails)."""

from __future__ import annotations

import importlib
import re

import pytest

from ...brand import APP_NAME
from ...i18n import locale_chain, t_locale
from .. import email_render
from ..email_render import (
    ACCOUNT_EMAIL_KINDS,
    EVENT_TYPES,
    EmailContent,
    build_facts,
    render_account_email,
    render_email_html,
    render_notification_email,
)

ALL_FACTS = {"score": 0.8125, "delta": 0.031, "spent": 3.2, "limit": 10, "duration": 4380}

FINISHED_EN_TEXT = (
    f"{APP_NAME.upper()}\n"
    "\n"
    "Support triage finished\n"
    "\n"
    "Score: 0.812\n"
    "Vs. baseline: +0.031\n"
    "Spent: $3.20 of $10.00\n"
    "Duration: 1h 13m\n"
    "\n"
    "Open optimization: https://app.example/optimizations/abc\n"
    "\n"
    "--\n"
    "You get emails when an optimization finishes.\n"
    "Notification settings: https://app.example/?settings=notifications\n"
)


@pytest.fixture(autouse=True)
def _frontend_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin link resolution to a known host so snapshots are deterministic.

    Args:
        monkeypatch: Pytest fixture used to swap the module-level base URL.
    """
    monkeypatch.setattr(email_render, "FRONTEND_URL", "https://app.example")


def _fact_cells(html: str) -> list[str]:
    """Return the opening tags of the fact cells in ``html``."""
    return re.findall(r'<td class="sk-fact"[^>]*>', html)


@pytest.mark.parametrize("locale", ["en", "he"])
@pytest.mark.parametrize("event_type", EVENT_TYPES)
def test_every_event_renders_html_and_text(event_type: str, locale: str) -> None:
    """Each event type renders a headline, one button, a reason and settings link in HTML and text.

    Args:
        event_type: Notification event under test.
        locale: Recipient locale under test.
    """
    subject, html, text = render_notification_email(
        event_type, locale, "Support triage", ALL_FACTS, "/optimizations/abc"
    )
    prefix = f"email.event.{event_type}"
    button = t_locale(locale, f"{prefix}.button")
    reason = t_locale(locale, f"{prefix}.reason")
    settings = t_locale(locale, "email.settings_link")

    assert "Support triage" in subject
    assert html.startswith("<!doctype html>")
    assert html.count('class="sk-btn-a"') == 1
    assert button in html
    assert reason in html
    assert settings in html
    assert "https://app.example/optimizations/abc" in html
    assert '<meta name="color-scheme" content="light dark">' in html
    assert "prefers-color-scheme:dark" in html
    assert "Heebo" not in html
    assert text.startswith(f"{APP_NAME.upper()}\n")
    assert f"{button}: https://app.example/optimizations/abc" in text
    assert reason in text
    assert settings in text
    assert "<" not in text


@pytest.mark.parametrize("event_type", EVENT_TYPES)
def test_hebrew_email_is_rtl(event_type: str) -> None:
    """Hebrew mail sets RTL direction, right alignment and a Hebrew-capable font.

    Args:
        event_type: Notification event under test.
    """
    _, html, _ = render_notification_email(event_type, "he", "Support triage", ALL_FACTS, "/x")

    assert '<html lang="he" dir="rtl"' in html
    assert '<body class="sk-bg" dir="rtl"' in html
    assert "text-align:right" in html
    assert "text-align:left" not in html
    assert "'Noto Sans Hebrew'" in html
    assert '<span dir="auto">Support triage</span>' in html


def test_english_email_is_ltr() -> None:
    """English mail stays left to right with no RTL markers."""
    _, html, text = render_notification_email("finished", "en", "Support triage", ALL_FACTS, "/x")

    assert '<html lang="en" dir="ltr"' in html
    assert "text-align:right" not in html
    assert chr(0x2066) not in text
    assert chr(0x2068) not in text


def test_finished_text_snapshot() -> None:
    """The finished email's plain-text body matches its snapshot."""
    subject, _, text = render_notification_email("finished", "en", "Support triage", ALL_FACTS, "/optimizations/abc")

    assert subject == "Support triage finished"
    assert text == FINISHED_EN_TEXT


def test_hebrew_numbers_are_isolated_not_the_whole_value() -> None:
    """Only the dollar amounts are pinned LTR, so the Hebrew "of" keeps its place."""
    _, html, text = render_notification_email("budget_near", "he", "Run", {"spent": 8.1, "limit": 10}, "/x")

    assert '<span dir="ltr">$8.10</span> מתוך <span dir="ltr">$10.00</span>' in html
    assert f"{chr(0x2066)}$8.10{chr(0x2069)} מתוך" in text


@pytest.mark.parametrize("count", [1, 2, 3, 4])
def test_fact_row_holds_one_to_four_facts(count: int) -> None:
    """The fact row holds any number of facts from one to four, content-sized.

    Args:
        count: How many facts to pass.
    """
    facts = dict(list({"score": 0.5, "delta": 0.1, "spent": 1.0, "duration": 60}.items())[:count])
    _, html, _ = render_notification_email("finished", "en", "Run", facts, "/x")

    cells = _fact_cells(html)
    assert len(cells) == count
    # Fixed equal widths let a long value such as "$6.37 of $10.00" run into
    # the next column, so cells size to their content.
    assert not any("width=" in cell for cell in cells)


def test_no_facts_omits_the_row() -> None:
    """An email without facts has no fact row at all."""
    _, html, text = render_notification_email("shared", "en", "Run", None, "/x")

    assert _fact_cells(html) == []
    assert "Score:" not in text


def test_event_drops_facts_it_does_not_show() -> None:
    """A failed run shows spend and duration but never a score."""
    _, html, text = render_notification_email("failed", "en", "Run", ALL_FACTS, "/x")

    assert len(_fact_cells(html)) == 2
    assert "Score:" not in text
    assert "Spent:" in text


def test_build_facts_rejects_unknown_keys() -> None:
    """A misspelt fact key fails loudly instead of disappearing."""
    with pytest.raises(ValueError, match="scroe"):
        build_facts({"scroe": 1}, "en")


def test_unknown_event_type_raises() -> None:
    """An unknown event type is a programming error."""
    with pytest.raises(ValueError, match="nope"):
        render_notification_email("nope", "en", "Run", None, "/x")


def test_custom_reason_detail_and_absolute_link() -> None:
    """A caller-supplied reason, detail and absolute link are used as given."""
    _, html, text = render_notification_email(
        "failed", "en", "Run", None, "https://elsewhere.example/r/1", "Custom reason.", detail="Out of memory."
    )

    assert "Custom reason." in html
    assert "Out of memory." in text
    assert "https://elsewhere.example/r/1" in html


def test_html_escapes_user_text() -> None:
    """Run titles are escaped in the HTML body."""
    _, html, _ = render_notification_email("finished", "en", "<script>x</script>", None, "/x")

    assert "<script>x</script>" not in html
    assert "&lt;script&gt;" in html


@pytest.mark.parametrize("kind", ACCOUNT_EMAIL_KINDS)
@pytest.mark.parametrize("locale", ["en", "he"])
def test_account_emails_keep_their_content(kind: str, locale: str) -> None:
    """Account emails show the code on the new layout, with no button or settings link.

    Args:
        kind: Account email kind under test.
        locale: Recipient locale under test.
    """
    subject, html, text = render_account_email(kind, locale, "482913")

    assert subject == t_locale(locale, f"email.account.{kind}.subject")
    assert "482913" in html
    assert "482913" in text
    assert 'class="sk-btn-a"' not in html
    assert t_locale(locale, "email.settings_link") not in html
    assert t_locale(locale, f"email.account.{kind}.ignore") in text


def test_account_email_english_text_matches_the_original_wording() -> None:
    """The plain-text body keeps the exact wording the routers sent before."""
    _, _, text = render_account_email("password_reset", "en", "123456")

    assert text == (
        f"Your {APP_NAME} password-reset code is 123456. It expires in 30 minutes.\n\n"
        "If you didn't ask to reset your password, you can ignore this email.\n"
    )


@pytest.mark.parametrize("locale", ["en", "he", "ja", "fr-CA", "pt-BR"])
def test_account_email_subjects_name_the_app(locale: str) -> None:
    """Every account email subject resolves {term.appName} to the brand constant.

    Args:
        locale: Recipient locale under test.
    """
    for kind in ACCOUNT_EMAIL_KINDS:
        subject, html, text = render_account_email(kind, locale, "482913")

        assert APP_NAME in subject
        assert "{term." not in subject + html + text


def test_render_email_html_without_optional_parts() -> None:
    """A bare content object still renders a complete document."""
    html = render_email_html(EmailContent(locale="en", subject="S", headline="H", footer="F"))

    assert "<h1" in html
    assert "F</td>" in html
    assert 'class="sk-btn-a"' not in html


def test_frontend_url_falls_back_to_port_3000(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without FRONTEND_URL, links point at this checkout's frontend on :3000.

    Args:
        monkeypatch: Pytest fixture used to clear the environment variable.
    """
    monkeypatch.delenv("FRONTEND_URL", raising=False)
    try:
        module = importlib.reload(email_render)
        assert module.FRONTEND_URL == "http://localhost:3000"
        _, html, text = module.render_notification_email("finished", "en", "Run", None, "/optimizations/abc")
        assert "http://localhost:3000/optimizations/abc" in html
        assert "localhost:3001" not in html
        assert "localhost:3001" not in text
    finally:
        monkeypatch.undo()
        importlib.reload(email_render)


def test_locale_chain_falls_back_through_english_to_hebrew() -> None:
    """Regional locales fall back to their language, then English, then Hebrew."""
    assert locale_chain("pt-BR") == ["pt-BR", "pt", "en", "he"]
    assert locale_chain("he") == ["he"]
    assert locale_chain(None) == ["he"]


def test_regional_locale_uses_language_copy() -> None:
    """A Brazilian recipient gets the Portuguese copy rather than English."""
    subject, _, _ = render_notification_email("stuck", "pt-BR", "Run", None, "/x")

    assert subject == "Run deixou de melhorar"
