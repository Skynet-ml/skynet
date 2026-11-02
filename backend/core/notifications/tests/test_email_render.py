"""Tests for the shared email renderer (notification and account emails)."""

from __future__ import annotations

import importlib
import re
from html import escape as esc

import pytest

from ...brand import APP_NAME
from ...i18n import t_locale
from .. import email_render
from ..email_render import (
    ACCOUNT_EMAIL_KINDS,
    COLORS,
    EMAIL_LOCALE,
    EVENT_TYPES,
    TEXT_PAIRS,
    EmailContent,
    build_facts,
    render_account_email,
    render_email_html,
    render_email_text,
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
    return re.findall(r'<div class="sk-fact"[^>]*>', html)


@pytest.mark.parametrize("event_type", EVENT_TYPES)
def test_every_event_renders_html_and_text(event_type: str) -> None:
    """Each event type renders a headline, one button and a settings link, with no reason line.

    Args:
        event_type: Notification event under test.
    """
    subject, html, text = render_notification_email(event_type, "Support triage", ALL_FACTS, "/optimizations/abc")
    prefix = f"email.event.{event_type}"
    button = t_locale("en", f"{prefix}.button")
    reason = t_locale("en", f"{prefix}.reason")
    settings = t_locale("en", "email.settings_link")

    assert "Support triage" in subject
    assert html.startswith("<!doctype html>")
    assert html.count('class="sk-btn-a"') == 1
    assert button in html
    assert reason not in html
    assert settings in html
    assert "https://app.example/optimizations/abc" in html
    assert html.count('<meta name="color-scheme" content="dark">') == 1
    assert html.count('<meta name="supported-color-schemes" content="dark">') == 1
    assert ":root{color-scheme:dark;supported-color-schemes:dark}" in html
    assert "light" not in html.split("</head>")[0].split("<style>")[0]
    assert "Heebo" not in html
    assert text.startswith(f"{APP_NAME.upper()}\n")
    assert f"{button}: https://app.example/optimizations/abc" in text
    assert reason not in text
    assert settings in text
    assert "<" not in text


def test_emails_are_english_only() -> None:
    """Every app email renders in English, whatever the recipient's app locale."""
    assert EMAIL_LOCALE == "en"
    subject, html, _ = render_notification_email("stuck", "Run", None, "/x")
    account_subject, _, _ = render_account_email("sign_in", "482913")

    assert subject == t_locale("en", "email.event.stuck.headline", title="Run")
    assert account_subject == t_locale("en", "email.account.sign_in.subject")
    assert '<html lang="en" dir="ltr"' in html


def _luminance(color: str) -> float:
    """Return the WCAG relative luminance of a ``#rrggbb`` color.

    Args:
        color: Hex color.

    Returns:
        Luminance from 0 (black) to 1 (white).
    """
    channels = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    r, g, b = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


@pytest.mark.parametrize(("ink", "surface"), TEXT_PAIRS)
def test_text_colors_meet_wcag_aa(ink: str, surface: str) -> None:
    """Every text color of the table is at least 4.5:1 against the surface it sits on.

    Args:
        ink: Text color key.
        surface: Background color key.
    """
    light, dark = sorted((_luminance(COLORS[ink]), _luminance(COLORS[surface])), reverse=True)
    assert (light + 0.05) / (dark + 0.05) >= 4.5


def test_color_table_has_no_pure_black_or_white() -> None:
    """The authored colors avoid the exact values some clients invert."""
    assert all(re.fullmatch(r"#[0-9a-f]{6}", value) for value in COLORS.values())
    assert not {"#000000", "#ffffff"} & set(COLORS.values())


@pytest.mark.parametrize("event_type", EVENT_TYPES)
def test_emails_are_dark_and_color_locked(event_type: str) -> None:
    """Emails are dark only, with the per-client locks against forced recoloring.

    Args:
        event_type: Notification event under test.
    """
    _, html, _ = render_notification_email(event_type, "Run", ALL_FACTS, "/x")
    style = html[html.index("<style>body") : html.index("</style></head>")]

    assert "prefers-color-scheme" not in html
    assert "light dark" not in html
    assert f"[data-ogsb] .sk-bg{{background-color:{COLORS['bg']} !important}}" in style
    assert f"[data-ogsb] .sk-card{{background-color:{COLORS['card']} !important}}" in style
    assert f"[data-ogsb] .sk-btn{{background-color:{COLORS['btn']} !important}}" in style
    for cls, key in (("sk-ink", "ink"), ("sk-text", "text"), ("sk-muted", "muted"), ("sk-btn-a", "btn_ink")):
        assert f"[data-ogsc] .{cls}{{color:{COLORS[key]} !important}}" in style
    assert "[data-ogsc] img:hover,[data-ogsb] img:hover{background-color:transparent !important}" in style
    assert '<body class="body sk-bg"' in html
    assert '<div class="gmail-bg"><div class="gmail-screen"><div class="gmail-diff">' in html
    assert f'<v:fill type="tile" color="{COLORS["bg"]}"/>' in html
    assert "mso-style-textfill-type:gradient" in html
    for tag in re.findall(r"<(?:body|table|td)\b[^>]*>", html):
        if "background-color:" in tag:
            assert re.search(r'bgcolor="#[0-9a-f]{6}"', tag), tag
    authored = re.sub(r"<style>.*?</style>", "", html, flags=re.DOTALL)
    assert not re.search(r"#(?:000|fff)(?:fff|000)?\b", authored, flags=re.IGNORECASE)


def test_button_is_centered() -> None:
    """The call-to-action button sits in the center of the column."""
    _, html, _ = render_notification_email("finished", "Run", None, "/x")

    button_row = html[: html.index('class="sk-btn"')].rsplit("<tr>", 2)[-2]
    assert 'align="center"' in button_row
    assert "text-align:center" in button_row


def test_rtl_content_is_mirrored() -> None:
    """Content built with an RTL locale still sets RTL direction and right alignment."""
    html = render_email_html(EmailContent(locale="he", subject="S", headline="H"))

    assert '<html lang="he" dir="rtl"' in html
    assert '<body class="body sk-bg" dir="rtl"' in html
    assert "text-align:right" in html
    assert "text-align:left" not in html


def test_english_email_is_ltr() -> None:
    """English mail stays left to right with no RTL markers."""
    _, html, text = render_notification_email("finished", "Support triage", ALL_FACTS, "/x")

    assert '<html lang="en" dir="ltr"' in html
    assert "text-align:right" not in html
    assert chr(0x2066) not in text
    assert chr(0x2068) not in text


def test_finished_text_snapshot() -> None:
    """The finished email's plain-text body matches its snapshot."""
    subject, _, text = render_notification_email("finished", "Support triage", ALL_FACTS, "/optimizations/abc")

    assert subject == "Support triage finished"
    assert text == FINISHED_EN_TEXT


@pytest.mark.parametrize("count", [1, 2, 3, 4])
def test_fact_row_holds_one_to_four_facts(count: int) -> None:
    """The fact row holds any number of facts from one to four, content-sized.

    Args:
        count: How many facts to pass.
    """
    facts = dict(list({"score": 0.5, "delta": 0.1, "spent": 1.0, "duration": 60}.items())[:count])
    _, html, _ = render_notification_email("finished", "Run", facts, "/x")

    cells = _fact_cells(html)
    assert len(cells) == count
    # Fixed equal widths let a long value such as "$6.37 of $10.00" run into
    # the next column, so cells size to their content.
    assert not any("width=" in cell for cell in cells)


def test_no_facts_omits_the_row() -> None:
    """An email without facts has no fact row at all."""
    _, html, text = render_notification_email("shared", "Run", None, "/x")

    assert _fact_cells(html) == []
    assert "Score:" not in text


def test_event_drops_facts_it_does_not_show() -> None:
    """A failed run shows spend and duration but never a score."""
    _, html, text = render_notification_email("failed", "Run", ALL_FACTS, "/x")

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
        render_notification_email("nope", "Run", None, "/x")


def test_custom_detail_and_absolute_link() -> None:
    """A caller-supplied detail and absolute link are used as given."""
    _, html, text = render_notification_email(
        "failed", "Run", None, "https://elsewhere.example/r/1", detail="Out of memory."
    )

    assert "Out of memory." in text
    assert "https://elsewhere.example/r/1" in html


def test_html_escapes_user_text() -> None:
    """Run titles are escaped in the HTML body."""
    _, html, _ = render_notification_email("finished", "<script>x</script>", None, "/x")

    assert "<script>x</script>" not in html
    assert "&lt;script&gt;" in html


@pytest.mark.parametrize("kind", ACCOUNT_EMAIL_KINDS)
def test_account_emails_keep_their_content(kind: str) -> None:
    """Account emails show the code on the new layout, with no button or settings link.

    Args:
        kind: Account email kind under test.
    """
    subject, html, text = render_account_email(kind, "482913")
    locale = "en"

    assert subject == t_locale(locale, f"email.account.{kind}.subject")
    assert t_locale(locale, "email.account.enter_code") in html
    assert "482913" in html
    assert "482913" in text
    assert 'class="sk-btn-a"' not in html
    assert t_locale(locale, "email.settings_link") not in html
    ignore = t_locale(locale, f"email.account.{kind}.ignore")
    assert ignore in text
    assert html.count(esc(ignore)) == 1
    assert 'class="sk-rule"' not in html


def test_account_email_english_text_matches_the_original_wording() -> None:
    """The plain-text body keeps the exact wording the routers sent before."""
    _, _, text = render_account_email("password_reset", "123456")

    assert text == (
        f"Your {APP_NAME} password-reset code is 123456. It expires in 30 minutes.\n\n"
        "If you didn't ask to reset your password, you can ignore this email.\n"
    )


def test_account_email_subjects_name_the_app() -> None:
    """Every account email subject resolves {term.appName} to the brand constant."""
    for kind in ACCOUNT_EMAIL_KINDS:
        subject, html, text = render_account_email(kind, "482913")

        assert APP_NAME in subject
        assert "{term." not in subject + html + text


def test_render_email_html_without_optional_parts() -> None:
    """A bare content object still renders a complete document."""
    html = render_email_html(EmailContent(locale="en", subject="S", headline="H"))
    text = render_email_text(EmailContent(locale="en", subject="S", headline="H"))

    assert "<h1" in html
    assert 'class="sk-btn-a"' not in html
    assert 'class="sk-rule"' not in html
    assert "--" not in text


def test_frontend_url_falls_back_to_port_3000(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without FRONTEND_URL, links point at this checkout's frontend on :3000.

    Args:
        monkeypatch: Pytest fixture used to clear the environment variable.
    """
    monkeypatch.delenv("FRONTEND_URL", raising=False)
    try:
        module = importlib.reload(email_render)
        assert module.FRONTEND_URL == "http://localhost:3000"
        _, html, text = module.render_notification_email("finished", "Run", None, "/optimizations/abc")
        assert "http://localhost:3000/optimizations/abc" in html
        assert "localhost:3001" not in html
        assert "localhost:3001" not in text
    finally:
        monkeypatch.undo()
        importlib.reload(email_render)


def test_header_logo_and_stacked_mobile_facts() -> None:
    """The header shows the sized logo image, and narrow screens stack every fact on its own line."""
    _, html, _ = render_notification_email("finished", "Run", ALL_FACTS, "/x")

    assert re.search(
        rf'<img src="[^"]+/brand/skynet-email-lockup@2x\.png" width="\d+" height="\d+" alt="{APP_NAME}"', html
    )
    assert ".sk-fact{display:block!important;width:100%!important" in html
    assert all("display:inline-block" in cell for cell in _fact_cells(html))
