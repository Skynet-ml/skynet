"""Email rendering: one calm, status-line layout for every app email.

Every email shares the same skeleton: a centered logo, a status label,
a one-line headline saying what happened, an optional row of facts (score,
change vs baseline, spend, duration), exactly one button, and, for notifications, a small notification-settings
link. Each email also gets a plain-text twin.

The HTML is built for real mail clients rather than browsers: table layout,
inline styles, a 600px card, and the app's dark colors only. The HTML asks
clients not to recolor it, and adds the known per-client locks against forced
dark-mode inversion. :class:`EmailContent` still accepts a locale, and RTL
locales get ``dir="rtl"`` and mirrored alignment and padding throughout.

The notification and account emails always render in English
(:data:`EMAIL_LOCALE`), whatever the recipient's app locale.

Public entry points:

* :func:`render_notification_email` for run and sharing events.
* :func:`render_account_email` for verification, password-reset and sign-in codes.
* :func:`render_email_html` / :func:`render_email_text` for callers that
  assemble their own :class:`EmailContent`.
"""

from __future__ import annotations

import html
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, NamedTuple

from ..brand import APP_NAME
from ..i18n import t_locale

FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000").rstrip("/")

# The ivory hoatzin mark and the wordmark as one opaque PNG, drawn from the
# app's dark-theme artwork at 2x on the card color: mail clients load neither
# web fonts nor inline SVG, and an opaque image cannot pick up a client's own
# background. The alt text carries the styles so the name still shows when
# images are blocked.
_LOGO_URL = f"{FRONTEND_URL}/brand/skynet-email-lockup@2x.png"
_LOGO_SIZE = (300, 60)
# The settings modal opens on the tab named by this query param on any page.
NOTIFICATION_SETTINGS_PATH = "/?settings=notifications"

# Product decision: every app email is English only, whatever the user's app
# locale. The email.* keys stay in the shared catalogs: Hebrew is the i18n base
# and must hold every key, and English reads from its overlay.
EMAIL_LOCALE = "en"

EventType = Literal["needs_input", "budget_near", "failed", "finished", "new_best", "stuck", "shared"]
EVENT_TYPES: tuple[EventType, ...] = ("needs_input", "budget_near", "failed", "finished", "new_best", "stuck", "shared")
AccountEmailKind = Literal["verification", "password_reset", "sign_in"]
ACCOUNT_EMAIL_KINDS: tuple[AccountEmailKind, ...] = ("verification", "password_reset", "sign_in")
_ACCOUNT_CODE_MINUTES: dict[str, int] = {"verification": 30, "password_reset": 30, "sign_in": 10}

Tone = Literal["good", "warn", "bad", "neutral"]
FactValue = float | int | str | None

# Display order of the fact row. ``limit`` is not a column of its own: it is
# folded into ``spent`` as "$8.10 of $10.00".
FACT_KEYS: tuple[str, ...] = ("score", "delta", "spent", "duration")
_FACT_INPUT_KEYS = frozenset((*FACT_KEYS, "limit"))
_EVENT_FACTS: dict[str, frozenset[str]] = {
    "needs_input": frozenset({"score", "spent", "duration"}),
    "budget_near": frozenset({"score", "spent", "duration"}),
    "failed": frozenset({"spent", "duration"}),
    "finished": frozenset({"score", "delta", "spent", "duration"}),
    "new_best": frozenset({"score", "delta", "spent", "duration"}),
    "stuck": frozenset({"score", "spent", "duration"}),
    "shared": frozenset({"score", "delta"}),
}
_EVENT_TONES: dict[str, Tone] = {
    "needs_input": "warn",
    "budget_near": "warn",
    "failed": "bad",
    "finished": "good",
    "new_best": "good",
    "stuck": "warn",
    "shared": "neutral",
}

_RTL_LANGUAGES = frozenset({"he", "ar", "fa", "ur", "yi"})
# Letter-spacing breaks Arabic joining and Indic conjuncts, and looks wrong on
# CJK, so tracking is reserved for the Latin and Cyrillic locales.
_TRACKED_LANGUAGES = frozenset({"en", "de", "es", "fr", "it", "pt", "ru", "uk", "tr"})

# The one color table of every email: the app's dark-theme tokens
# (frontend/src/app/globals.css, ``.dark``). Values are inlined, so Outlook
# desktop, which drops <style>, gets the same colors as every other client.
# No pure black or white: some clients invert exactly those values.
COLORS: dict[str, str] = {
    "bg": "#1c1814",  # --bg
    "card": "#2c2621",  # --card
    "ink": "#f3ede6",  # --text-1
    "text": "#cdbfb1",  # --text-2
    "muted": "#a89a8c",  # --muted-foreground
    "rule": "#483d34",  # --border
    "panel": "#382f28",  # --surface-3
    "btn": "#b4521f",  # --primary-fill
    "btn_ink": "#faf8f5",
    "good": "#6cc0a8",  # --success
    "warn": "#e0a84a",  # --warning
    "bad": "#ef8a6a",  # --danger
    "neutral": "#a89a8c",  # --muted-foreground
}
# Each text color and the surface it sits on; the contrast test holds every
# pair to WCAG AA (4.5:1).
TEXT_PAIRS: tuple[tuple[str, str], ...] = (
    *((ink, "card") for ink in ("ink", "text", "muted", "good", "warn", "bad", "neutral")),
    ("ink", "panel"),
    ("btn_ink", "btn"),
)

# Outlook.com and the Outlook apps mark recolored elements with data-ogsb
# (background) and data-ogsc (text); these rules put the table's colors back.
_BG_CLASSES: dict[str, str] = {
    "sk-bg": "bg",
    "sk-card": "card",
    "sk-panel": "panel",
    "sk-btn": "btn",
    **{f"sk-dot-{tone}": tone for tone in ("good", "warn", "bad", "neutral")},
}
_INK_CLASSES: dict[str, str] = {
    "sk-ink": "ink",
    "sk-text": "text",
    "sk-muted": "muted",
    "sk-btn-a": "btn_ink",
    **{f"sk-{tone}": tone for tone in ("good", "warn", "bad", "neutral")},
}

# The app's faces load from Google Fonts: Onest for text, Bricolage Grotesque
# for the headline and wordmark. Gmail and Outlook ignore web fonts and fall
# through to the system faces, and every stack continues into faces covering
# the locale's script.
_FONTS_URL = (
    "https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,600;12..96,700"
    "&family=Onest:wght@400;500;600;700&family=JetBrains+Mono:wght@500&display=swap"
)
_DISPLAY_LEAD = "'Bricolage Grotesque',"
_FONT_LEAD = "'Onest',-apple-system,BlinkMacSystemFont,'Segoe UI'"
_FONT_TAIL = "Roboto,'Helvetica Neue',Arial,sans-serif"
_SCRIPT_FONTS: dict[str, str] = {
    "he": "'Noto Sans Hebrew','Arial Hebrew'",
    "ar": "'Noto Sans Arabic','Geeza Pro',Tahoma",
    "fa": "'Vazirmatn','Noto Sans Arabic','Geeza Pro',Tahoma",
    "hi": "'Noto Sans Devanagari','Kohinoor Devanagari','Nirmala UI'",
    "ja": "'Hiragino Sans','Hiragino Kaku Gothic ProN','Yu Gothic',Meiryo,'Noto Sans JP'",
    "ko": "'Apple SD Gothic Neo','Malgun Gothic','Noto Sans KR'",
    "zh": "'PingFang SC','Microsoft YaHei','Noto Sans SC'",
    "yue": "'PingFang HK','Microsoft JhengHei','Noto Sans HK'",
}
_MONO = "'JetBrains Mono',ui-monospace,'SF Mono',SFMono-Regular,Menlo,Consolas,'Liberation Mono',monospace"

# Bidi isolates built via chr() to keep raw control characters out of the
# source (ruff PLE2502). FSI wraps the user-named run title so a Latin title
# inside a Hebrew sentence (or the reverse) cannot reorder its neighbours;
# LRI pins numbers like "-0.031" and "$3.20" left-to-right inside RTL text.
_FSI = chr(0x2068)
_LRI = chr(0x2066)
_PDI = chr(0x2069)
_MINUS = chr(0x2212)


class RenderedEmail(NamedTuple):
    """A rendered email, unpackable as ``subject, html, text``."""

    subject: str
    html: str
    text: str


@dataclass(frozen=True)
class EmailContent:
    """Everything one email shows, already localized.

    Attributes:
        locale: Recipient locale tag; drives ``lang``, direction and fonts.
        subject: Mail subject line (also the document ``<title>``).
        headline: One line saying what happened.
        status: Optional ``(label, tone)`` status line above the headline.
        detail: Optional supporting sentence under the headline.
        facts: Ordered ``(label, value)`` pairs for the fact row (0-4).
        code: Optional one-time code shown large in place of the facts.
        note: Optional line under the code (e.g. its expiry).
        button: Optional ``(label, url)`` for the single call to action.
        settings: Optional ``(label, url)`` link to notification settings.
        preheader: Inbox preview text; defaults to the detail or facts.
        text_body: Plain-text body override; built from the parts when ``None``.
    """

    locale: str | None
    subject: str
    headline: str
    status: tuple[str, Tone] | None = None
    detail: str | None = None
    facts: tuple[tuple[str, str], ...] = ()
    code: str | None = None
    note: str | None = None
    button: tuple[str, str] | None = None
    settings: tuple[str, str] | None = None
    preheader: str | None = None
    text_body: str | None = None


def absolute_url(link: str) -> str:
    """Resolve an app path against ``FRONTEND_URL``; absolute URLs pass through.

    Args:
        link: Absolute ``http(s)`` URL, or an app path such as ``/optimizations/abc``.

    Returns:
        A URL the recipient's mail client can open.
    """
    if link.startswith(("http://", "https://", "mailto:")):
        return link
    return f"{FRONTEND_URL}/{link.lstrip('/')}"


def _language(locale: str | None) -> str:
    """Return the primary language subtag of ``locale`` (Hebrew when unset)."""
    return (locale or "he").split("-", 1)[0].lower()


def is_rtl(locale: str | None) -> bool:
    """Return whether ``locale`` is written right to left.

    Args:
        locale: Locale tag such as ``"he"`` or ``"en-GB"``; ``None`` is Hebrew.

    Returns:
        ``True`` for Hebrew, Arabic, Persian, Urdu and Yiddish.
    """
    return _language(locale) in _RTL_LANGUAGES


def _font_stack(locale: str | None) -> str:
    """Return the text font stack for ``locale``'s script."""
    script = _SCRIPT_FONTS.get(_language(locale))
    middle = f",{script}," if script else ","
    return f"{_FONT_LEAD}{middle}{_FONT_TAIL}"


def _ltr(value: str) -> str:
    """Wrap a number-like token in an LTR isolate so it keeps its order in RTL text."""
    return f"{_LRI}{value}{_PDI}"


def _strip_isolates(text: str) -> str:
    """Remove bidi isolate characters (for subjects and ``<title>``)."""
    return text.translate({ord(c): None for c in (_FSI, _LRI, _PDI)})


def _bidi_html(text: str) -> str:
    """Escape ``text`` and turn its bidi isolates into ``dir`` spans.

    Some mail clients draw raw isolate characters as boxes, while an element
    with a ``dir`` attribute isolates its contents in every engine that
    renders HTML email.

    Args:
        text: Plain text that may contain FSI/LRI ... PDI isolates.

    Returns:
        HTML-safe markup.
    """
    return (
        html.escape(text).replace(_FSI, '<span dir="auto">').replace(_LRI, '<span dir="ltr">').replace(_PDI, "</span>")
    )


def _format_number(value: float) -> str:
    """Render a score compactly (``0.8125`` -> ``0.812``, ``73.54`` -> ``73.5``)."""
    if float(value).is_integer() and abs(value) >= 1:
        return f"{int(value)}"
    return f"{value:.3g}" if abs(value) < 1 else f"{value:.1f}"


def _format_delta(value: float) -> str:
    """Render a signed change vs baseline with a true minus sign."""
    if value == 0:
        return "0"
    sign = "+" if value > 0 else _MINUS
    return _ltr(f"{sign}{_format_number(abs(value))}")


def _format_dollars(value: float) -> str:
    """Render a dollar amount with cents (``3.2`` -> ``$3.20``)."""
    return _ltr(f"${value:,.2f}")


def _format_duration(seconds: float, locale: str | None) -> str:
    """Render a duration in seconds as the two largest localized units.

    Args:
        seconds: Elapsed wall-clock seconds.
        locale: Recipient locale for the unit labels.

    Returns:
        ``"1h 12m"``, ``"4m 05s"`` or ``"42s"`` in the recipient's language.
    """
    total = max(0, round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return t_locale(locale, "email.duration.hours", h=hours, m=f"{minutes:02d}")
    if minutes:
        return t_locale(locale, "email.duration.minutes", m=minutes, s=f"{secs:02d}")
    return t_locale(locale, "email.duration.seconds", s=secs)


def _fact_value(key: str, value: float | int | str, locale: str | None, limit: FactValue) -> str:
    """Format one fact value; strings are trusted as already formatted.

    Args:
        key: One of :data:`FACT_KEYS`.
        value: Raw number, or a preformatted string.
        locale: Recipient locale (durations and "of" phrasing).
        limit: Optional spending limit in dollars, folded into ``spent``.

    Returns:
        The display string for the fact row.
    """
    if isinstance(value, str):
        text = value
    elif key == "score":
        text = _format_number(value)
    elif key == "delta":
        text = _format_delta(value)
    elif key == "spent":
        text = _format_dollars(value)
    else:
        text = _format_duration(value, locale)
    if key == "spent" and limit is not None:
        limit_text = limit if isinstance(limit, str) else _format_dollars(limit)
        text = t_locale(locale, "email.spent.of_limit", spent=text, limit=limit_text)
    return text


def event_fact_keys(event_type: str) -> frozenset[str]:
    """Return the fact keys a notification about ``event_type`` shows.

    Args:
        event_type: A notification event type.

    Returns:
        The subset of :data:`FACT_KEYS` the email (and Slack) fact line shows.
    """
    return _EVENT_FACTS.get(event_type, frozenset(FACT_KEYS))


def build_facts(
    facts: Mapping[str, FactValue] | None,
    locale: str | None,
    allowed: frozenset[str] = frozenset(FACT_KEYS),
) -> tuple[tuple[str, str], ...]:
    """Turn a raw facts mapping into ordered, localized ``(label, value)`` pairs.

    Args:
        facts: Mapping with any of ``score``, ``delta`` (vs baseline),
            ``spent`` (dollars), ``limit`` (dollars, shown with ``spent``) and
            ``duration`` (seconds). Values may be numbers or preformatted
            strings; ``None`` values are skipped.
        locale: Recipient locale for labels and units.
        allowed: Fact keys this email shows; others are dropped.

    Returns:
        Up to four pairs in :data:`FACT_KEYS` order.

    Raises:
        ValueError: When ``facts`` holds a key this renderer does not know.
    """
    facts = facts or {}
    unknown = set(facts) - _FACT_INPUT_KEYS
    if unknown:
        raise ValueError(f"Unknown email fact keys: {sorted(unknown)}")
    pairs = []
    for key in FACT_KEYS:
        value = facts.get(key)
        if value is None or key not in allowed:
            continue
        label = t_locale(locale, f"email.fact.{key}")
        pairs.append((label, _fact_value(key, value, locale, facts.get("limit"))))
    return tuple(pairs)


def _style_block(tracked: bool) -> str:
    """Return the ``<style>`` contents: dark-mode color locks and responsive rules.

    Args:
        tracked: Whether the locale takes letter-spacing (Latin/Cyrillic).

    Returns:
        CSS text. Every rule is an enhancement; the email reads correctly
        without it.
    """
    tracking = ".sk-status{letter-spacing:0.08em}" if tracked else ""
    locks = "".join(
        f"[data-ogsb] .{cls}{{background-color:{COLORS[key]} !important}}" for cls, key in _BG_CLASSES.items()
    )
    locks += "".join(f"[data-ogsc] .{cls}{{color:{COLORS[key]} !important}}" for cls, key in _INK_CLASSES.items())
    locks += f"[data-ogsc] .sk-rule{{border-color:{COLORS['rule']} !important}}"
    return (
        ":root{color-scheme:dark;supported-color-schemes:dark}"
        "body{margin:0!important;padding:0!important;width:100%!important;-webkit-text-size-adjust:100%;"
        "-ms-text-size-adjust:100%}"
        "table{border-collapse:collapse;mso-table-lspace:0;mso-table-rspace:0}"
        "a{text-decoration:none}"
        ".sk-btn-a:hover{opacity:0.88}"
        f"{tracking}{locks}"
        "[data-ogsc] img:hover,[data-ogsb] img:hover{background-color:transparent !important}"
        # Gmail apps invert dark emails. Only Gmail puts a <u> before the
        # body, so these rules match there alone: the blend layers undo the
        # inversion of the text, and the gradient keeps the background.
        f"u + .body .gmail-bg{{background-image:linear-gradient({COLORS['bg']},{COLORS['bg']})}}"
        "u + .body .gmail-screen{background:#000;mix-blend-mode:screen}"
        "u + .body .gmail-diff{background:#000;mix-blend-mode:difference}"
        "@media (max-width:520px){"
        ".sk-outer{padding:12px 8px!important}"
        ".sk-pad{padding:24px 20px 28px!important}"
        ".sk-fact{display:block!important;width:100%!important;box-sizing:border-box;padding:12px 0!important}"
        f".sk-fact+.sk-fact{{border-top:1px solid {COLORS['rule']}}}"
        ".sk-fact-v{font-size:16px!important;line-height:24px!important;white-space:normal!important}"
        ".sk-h1{font-size:20px!important;line-height:28px!important}"
        ".sk-code{font-size:28px!important;letter-spacing:6px!important}"
        "}"
    )


def _mso_color(value: str) -> str:
    """Escape a hex color for an Outlook text-fill stop list (``#abc`` -> ``\\#ABC``)."""
    return "\\" + value.upper()


def _facts_row(facts: tuple[tuple[str, str], ...], font: str, rtl: bool) -> str:
    """Render the facts as inline blocks that wrap onto new lines when narrow.

    Args:
        facts: Ordered ``(label, value)`` pairs (1-4).
        font: Text font stack for the labels.
        rtl: Whether to mirror alignment and the gap between facts.

    Returns:
        A ``<tr>`` holding the facts, or ``""`` without facts.
    """
    if not facts:
        return ""
    start = "right" if rtl else "left"
    gap = "16px 0 16px 28px" if rtl else "16px 28px 16px 0"
    # Inline blocks, not table cells: a client that ignores the mobile media
    # query (the Gmail app with non-Google accounts) wraps a fact that does
    # not fit onto its own line instead of squeezing or overlapping it. The
    # media query then stacks every fact on its own full-width line. Outlook
    # desktop ignores inline-block, so a ghost table keeps its facts in a row.
    blocks = '<!--[if mso]></td><td valign="top"><![endif]-->'.join(
        f'<div class="sk-fact" style="display:inline-block;vertical-align:top;padding:{gap};text-align:{start};">'
        f'<div class="sk-muted" style="font-family:{font};font-size:12px;line-height:16px;color:{COLORS["muted"]};'
        f'text-align:{start};white-space:nowrap;">{html.escape(label)}</div>'
        f'<div class="sk-ink sk-fact-v" style="font-family:{_MONO};font-size:18px;line-height:26px;font-weight:500;'
        f'color:{COLORS["ink"]};padding-top:4px;text-align:{start};white-space:nowrap;">{_bidi_html(value)}</div>'
        "</div>"
        for label, value in facts
    )
    return (
        '<tr><td style="padding-top:24px;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" class="sk-rule" '
        f'style="width:100%;border-top:1px solid {COLORS["rule"]};border-bottom:1px solid {COLORS["rule"]};">'
        f'<tr><td style="text-align:{start};font-size:0;line-height:0;">'
        '<!--[if mso]><table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
        '<td valign="top"><![endif]-->'
        f"{blocks}"
        "<!--[if mso]></td></tr></table><![endif]-->"
        "</td></tr></table></td></tr>"
    )


def _code_rows(code: str, note: str | None, font: str, start: str) -> str:
    """Render a one-time code between rules, with an optional note beneath.

    Args:
        code: The code to show.
        note: Optional line under the code (its expiry).
        font: Text font stack for the note.
        start: ``"left"`` or ``"right"`` for the reading direction.

    Returns:
        Table rows for the code block.
    """
    note_row = (
        f'<tr><td class="sk-muted" style="padding-top:12px;font-family:{font};font-size:14px;line-height:20px;'
        f'color:{COLORS["muted"]};text-align:{start};">{html.escape(note)}</td></tr>'
        if note
        else ""
    )
    # The code sits on a tinted panel, centred, so it reads as the one thing
    # to copy; the bgcolor attribute keeps the panel in Outlook desktop.
    return (
        '<tr><td style="padding-top:24px;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        f'<tr><td class="sk-panel" bgcolor="{COLORS["panel"]}" align="center" '
        f'style="border-radius:10px;background-color:{COLORS["panel"]};padding:22px 16px;">'
        f'<span class="sk-ink sk-code" dir="ltr" style="font-family:{_MONO};font-size:34px;line-height:40px;'
        f'font-weight:500;letter-spacing:8px;color:{COLORS["ink"]};">'
        f"{html.escape(code)}</span></td></tr></table></td></tr>{note_row}"
    )


def _button_row(label: str, url: str, font: str) -> str:
    """Render the single call-to-action as a centered table-cell button.

    The colored cell (not the link) carries the background so Outlook desktop,
    which ignores padding on ``<a>``, still draws a solid button.

    Args:
        label: Button text.
        url: Destination URL.
        font: Text font stack.

    Returns:
        A ``<tr>`` holding the button.
    """
    href = html.escape(url, quote=True)
    return (
        '<tr><td align="center" style="padding-top:28px;text-align:center;">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" align="center"><tr>'
        f'<td class="sk-btn" bgcolor="{COLORS["btn"]}" style="border-radius:6px;background-color:{COLORS["btn"]};'
        'mso-padding-alt:12px 22px;">'
        f'<a class="sk-btn-a" href="{href}" target="_blank" style="display:inline-block;padding:12px 22px;'
        f"font-family:{font};font-size:15px;line-height:20px;font-weight:600;color:{COLORS['btn_ink']};"
        f'text-decoration:none;border-radius:6px;">{html.escape(label)}</a>'
        "</td></tr></table></td></tr>"
    )


def render_email_html(content: EmailContent) -> str:
    """Render :class:`EmailContent` as a self-contained HTML email document.

    Args:
        content: Localized parts of the email.

    Returns:
        An HTML document suitable for the ``text/html`` alternative.
    """
    rtl = is_rtl(content.locale)
    direction = "rtl" if rtl else "ltr"
    start = "right" if rtl else "left"
    font = _font_stack(content.locale)
    tracked = _language(content.locale) in _TRACKED_LANGUAGES
    lang = html.escape(content.locale or "he", quote=True)
    esc = html.escape

    preheader_text = content.preheader or content.detail or " · ".join(f"{k} {v}" for k, v in content.facts)
    preheader = (
        f'<div style="display:none;max-height:0;max-width:0;overflow:hidden;opacity:0;mso-hide:all;'
        f'font-size:1px;line-height:1px;color:{COLORS["bg"]};">{esc(_strip_isolates(preheader_text))}'
        f"{'&#8204;&nbsp;' * 40}</div>"
        if preheader_text
        else ""
    )
    status_row = ""
    if content.status:
        label, tone = content.status
        gap = "padding-right:8px;" if rtl else "padding-left:8px;"
        status_row = (
            f'<tr><td style="padding-bottom:10px;text-align:{start};">'
            f'<span class="sk-dot-{tone}" style="display:inline-block;width:7px;height:7px;border-radius:1px;'
            f'background-color:{COLORS[tone]};vertical-align:middle;font-size:0;line-height:0;">&nbsp;</span>'
            f'<span class="sk-{tone} sk-status" style="{gap}font-family:{font};font-size:12px;line-height:16px;'
            f'font-weight:600;text-transform:uppercase;color:{COLORS[tone]};vertical-align:middle;">'
            f"{esc(label)}</span></td></tr>"
        )
    headline_tracking = "letter-spacing:-0.01em;" if tracked else ""
    detail_row = (
        f'<tr><td class="sk-text" style="padding-top:8px;font-family:{font};font-size:15px;line-height:22px;'
        f'color:{COLORS["text"]};text-align:{start};">{esc(content.detail)}</td></tr>'
        if content.detail
        else ""
    )
    body_rows = (
        _code_rows(content.code, content.note, font, start) if content.code else _facts_row(content.facts, font, rtl)
    )
    button_row = _button_row(*content.button, font) if content.button else ""
    settings_row = ""
    if content.settings:
        settings_label, settings_url = content.settings
        settings_row = (
            f'<tr><td style="padding-top:40px;">'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" class="sk-rule" '
            f'style="width:100%;border-top:1px solid {COLORS["rule"]};"><tr>'
            f'<td class="sk-muted" style="padding-top:16px;font-family:{font};font-size:12px;line-height:18px;'
            f'color:{COLORS["muted"]};text-align:{start};">'
            f'<a class="sk-muted" href="{esc(settings_url, quote=True)}" target="_blank" '
            f'style="color:{COLORS["muted"]};text-decoration:underline;">{esc(settings_label)}</a></td>'
            "</tr></table></td></tr>"
        )
    home = esc(FRONTEND_URL, quote=True)

    return (
        "<!doctype html>"
        f'<html lang="{lang}" dir="{direction}" xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office">'
        '<head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="X-UA-Compatible" content="IE=edge">'
        # The email is dark in every client and mode. Declaring "dark" tells
        # Apple Mail, iOS Mail and Chromium-based clients that it supports
        # dark already, so they leave it as authored instead of recoloring.
        '<meta name="color-scheme" content="dark">'
        '<meta name="supported-color-schemes" content="dark">'
        '<meta name="format-detection" content="telephone=no,date=no,address=no,email=no">'
        f"<title>{esc(_strip_isolates(content.subject))}</title>"
        # Outlook desktop renders Times New Roman when the first family in a
        # stack is a font it does not have, so it gets a plain stack instead.
        "<!--[if mso]><style>body,table,td,div,p,a,span,h1{font-family:'Segoe UI',Arial,sans-serif!important}"
        "</style><![endif]-->"
        # New Outlook for Windows recolors text in dark mode but leaves a
        # gradient text fill alone, so the button label keeps its color.
        "<!--[if gte mso 16]><style>.sk-btn-a{mso-style-textfill-type:gradient;"
        f'mso-style-textfill-fill-gradientfill-stoplist:"0 {_mso_color(COLORS["btn_ink"])} 0 100000\\,'
        f'100000 {_mso_color(COLORS["btn_ink"])} 0 100000"}}</style><![endif]-->'
        f'<link href="{esc(_FONTS_URL, quote=True)}" rel="stylesheet">'
        f"<style>{_style_block(tracked)}</style>"
        "</head>"
        f'<body class="body sk-bg" dir="{direction}" bgcolor="{COLORS["bg"]}" '
        f'style="margin:0;padding:0;background-color:{COLORS["bg"]};">'
        # Outlook desktop paints the window behind the body only from VML.
        '<!--[if gte mso 9]><v:background xmlns:v="urn:schemas-microsoft-com:vml" fill="t">'
        f'<v:fill type="tile" color="{COLORS["bg"]}"/></v:background><![endif]-->'
        '<div class="gmail-bg"><div class="gmail-screen"><div class="gmail-diff">'
        f"{preheader}"
        f'<table role="presentation" class="sk-bg" width="100%" cellpadding="0" cellspacing="0" border="0" '
        f'dir="{direction}" bgcolor="{COLORS["bg"]}" style="width:100%;background-color:{COLORS["bg"]};">'
        '<tr><td class="sk-outer" align="center" style="padding:32px 16px;">'
        '<!--[if mso]><table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0">'
        "<tr><td><![endif]-->"
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" dir="{direction}" '
        # Separate borders: a collapsed border ignores border-radius and draws square corners.
        'style="width:100%;max-width:600px;border-collapse:separate;">'
        f'<tr><td class="sk-card sk-pad" bgcolor="{COLORS["card"]}" style="padding:28px 32px 36px;'
        f'background-color:{COLORS["card"]};border:1px solid {COLORS["rule"]};border-radius:12px;text-align:{start};">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="width:100%;">'
        '<tr><td align="center" style="padding-bottom:20px;text-align:center;">'
        f'<a href="{home}" target="_blank" style="text-decoration:none;">'
        f'<img src="{esc(_LOGO_URL, quote=True)}" width="{_LOGO_SIZE[0]}" height="{_LOGO_SIZE[1]}" '
        f'alt="{esc(APP_NAME, quote=True)}" '
        f'style="display:block;margin:0 auto;border:0;outline:none;width:{_LOGO_SIZE[0]}px;height:{_LOGO_SIZE[1]}px;'
        f"font-family:{_DISPLAY_LEAD}{font};font-size:18px;line-height:60px;font-weight:700;color:{COLORS['ink']};"
        'text-align:center;"></a></td></tr>'
        f"{status_row}"
        f'<tr><td style="text-align:{start};">'
        f'<h1 class="sk-ink sk-h1" style="margin:0;font-family:{_DISPLAY_LEAD}{font};'
        f"font-size:22px;line-height:30px;font-weight:600;{headline_tracking}color:{COLORS['ink']};"
        f'text-align:{start};">{_bidi_html(content.headline)}</h1></td></tr>'
        f"{detail_row}{body_rows}{button_row}{settings_row}"
        "</table></td></tr></table>"
        "<!--[if mso]></td></tr></table><![endif]-->"
        "</td></tr></table></div></div></div></body></html>"
    )


def render_email_text(content: EmailContent) -> str:
    """Render :class:`EmailContent` as the plain-text alternative.

    Args:
        content: Localized parts of the email.

    Returns:
        A plain-text body with the same information as the HTML version.
    """
    if content.text_body is not None:
        return content.text_body
    rtl = is_rtl(content.locale)
    iso = _ltr if rtl else (lambda value: value)
    lines = [APP_NAME.upper(), "", content.headline]
    if content.detail:
        lines.append(content.detail)
    if content.code:
        lines += ["", iso(content.code)]
        if content.note:
            lines.append(content.note)
    if content.facts:
        lines.append("")
        lines += [f"{label}: {value}" for label, value in content.facts]
    if content.button:
        label, url = content.button
        lines += ["", f"{label}: {url}"]
    if content.settings:
        label, url = content.settings
        lines += ["", "--", f"{label}: {url}"]
    body = "\n".join(lines) + "\n"
    # Isolates only matter when RTL text surrounds the numbers; in LTR bodies
    # they are noise that a few plain-text clients draw as boxes.
    return body if rtl else _strip_isolates(body)


def render_notification_email(
    event_type: str,
    title: str,
    facts: Mapping[str, FactValue] | None,
    link: str,
    *,
    detail: str | None = None,
    settings_url: str | None = None,
) -> RenderedEmail:
    """Render a run or sharing notification in English.

    Args:
        event_type: One of :data:`EVENT_TYPES` (``needs_input``,
            ``budget_near``, ``failed``, ``finished``, ``new_best``,
            ``stuck``, ``shared``).
        title: The run's display name, inserted into the headline.
        facts: Optional ``score`` / ``delta`` / ``spent`` (+ ``limit``) /
            ``duration`` values; see :func:`build_facts`. Only the facts the
            event shows, and that are not ``None``, are rendered.
        link: Button target: an absolute URL or an app path such as
            ``/optimizations/<id>`` (resolved against ``FRONTEND_URL``).
        detail: Optional sentence under the headline (an error summary, the
            question a paused run asks, who shared the run).
        settings_url: Notification-settings link; ``None`` uses the app's
            settings route under ``FRONTEND_URL``.

    Returns:
        ``(subject, html, text)`` as a :class:`RenderedEmail`.

    Raises:
        ValueError: On an unknown ``event_type`` or fact key.
    """
    if event_type not in EVENT_TYPES:
        raise ValueError(f"Unknown notification event type: {event_type!r}")
    prefix = f"email.event.{event_type}"
    headline = t_locale(EMAIL_LOCALE, f"{prefix}.headline", title=f"{_FSI}{title}{_PDI}")
    content = EmailContent(
        locale=EMAIL_LOCALE,
        subject=_strip_isolates(headline),
        headline=headline,
        status=(t_locale(EMAIL_LOCALE, f"{prefix}.status"), _EVENT_TONES[event_type]),
        detail=detail,
        facts=build_facts(facts, EMAIL_LOCALE, _EVENT_FACTS[event_type]),
        button=(t_locale(EMAIL_LOCALE, f"{prefix}.button"), absolute_url(link)),
        settings=(
            t_locale(EMAIL_LOCALE, "email.settings_link"),
            absolute_url(settings_url or NOTIFICATION_SETTINGS_PATH),
        ),
    )
    return RenderedEmail(content.subject, render_email_html(content), render_email_text(content))


def render_account_email(kind: str, code: str, *, minutes: int | None = None) -> RenderedEmail:
    """Render an account-security code email in English on the shared layout.

    The plain-text body keeps the original wording ("Your Skynet confirmation
    code is 123456. It expires in 30 minutes." plus the ignore line). These
    emails have no button and no settings link: they cannot be switched off.

    Args:
        kind: One of :data:`ACCOUNT_EMAIL_KINDS` (``verification``,
            ``password_reset``, ``sign_in``).
        code: The one-time code.
        minutes: Code lifetime; defaults to the kind's standard (30 or 10).

    Returns:
        ``(subject, html, text)`` as a :class:`RenderedEmail`.

    Raises:
        ValueError: On an unknown ``kind``.
    """
    if kind not in ACCOUNT_EMAIL_KINDS:
        raise ValueError(f"Unknown account email kind: {kind!r}")
    prefix = f"email.account.{kind}"
    lifetime = minutes if minutes is not None else _ACCOUNT_CODE_MINUTES[kind]
    subject = t_locale(EMAIL_LOCALE, f"{prefix}.subject")
    intro = t_locale(EMAIL_LOCALE, f"{prefix}.intro", code=code)
    expires = t_locale(EMAIL_LOCALE, "email.account.expires", minutes=lifetime)
    ignore = t_locale(EMAIL_LOCALE, f"{prefix}.ignore")
    content = EmailContent(
        locale=EMAIL_LOCALE,
        subject=subject,
        headline=subject,
        detail=t_locale(EMAIL_LOCALE, "email.account.enter_code"),
        status=(t_locale(EMAIL_LOCALE, "email.account.status"), "neutral"),
        code=code,
        # The ignore line is a safety instruction, so it stays in the body
        # text rather than in small print.
        note=f"{expires} {ignore}",
        preheader=f"{intro} {expires}",
        text_body=f"{intro} {expires}\n\n{ignore}\n",
    )
    return RenderedEmail(subject, render_email_html(content), render_email_text(content))
