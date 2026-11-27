"""Text message templates for the warm SendBlue channel.

Rules every template follows (plan section 6.8):
  * plain sentences, first person from the rep, MyOrbit Health named exactly once,
  * no links (SendBlue blocks links, media, phone numbers and emails before a contact replies),
  * under 300 characters (the pre-reply cap on Blue Ocean lines),
  * no "Reply STOP" boilerplate: the opt-out is a natural sentence. Stop words are still
    honoured automatically by the inbound sweep (texting.is_stop_text).
"""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

MAX_CHARS = 300
COMPANY = "MyOrbit Health"
OPT_OUT_LINE = "If texts aren't useful, just say so and I'll stop."
STOP_CONFIRMATION = "Done, no more texts from me."

CONFIRMATION = "confirmation"
REMINDER_24H = "reminder_24h"
REMINDER_1H = "reminder_1h"
FOLLOW_UP = "follow_up"
STOP_CONFIRM = "stop_confirmation"
MANUAL = "manual"
TEMPLATES = (CONFIRMATION, REMINDER_24H, REMINDER_1H, FOLLOW_UP)

_LINK_RE = re.compile(r"(https?://|www\.|\b[\w-]+\.(com|io|co|net|org|health)\b)", re.IGNORECASE)


def _first(name: str | None) -> str:
    name = (name or "").strip().split(" ")[0]
    return f"Hi {name}" if name else "Hi"


def _rep(rep_name: str | None) -> str:
    rep = (rep_name or "").strip()
    return rep if rep and rep != "unassigned" else ""


def _intro(rep_name: str | None) -> str:
    rep = _rep(rep_name)
    return f"this is {rep} from {COMPANY}" if rep else f"this is your contact at {COMPANY}"


def format_when(starts_at: datetime, tz: str | None, *, with_day: bool = True) -> str:
    """'Tuesday, December 1 at 2:00 PM EST' in the contact's timezone (America/New_York default)."""
    local = starts_at.astimezone(ZoneInfo(tz or "America/New_York"))
    hour = local.strftime("%I").lstrip("0")
    clock = f"{hour}:{local:%M} {local:%p} {local:%Z}"
    if not with_day:
        return clock
    return f"{local:%A}, {local:%B} {local.day} at {clock}"


def check(text: str) -> str:
    """Raise if a rendered text breaks the channel rules; return it unchanged otherwise."""
    if len(text) >= MAX_CHARS:
        raise ValueError(f"text is {len(text)} characters, the limit is under {MAX_CHARS}")
    if _LINK_RE.search(text):
        raise ValueError("texts carry no links")
    if text.count(COMPANY) != 1:
        raise ValueError(f"name {COMPANY} exactly once")
    if "STOP" in text or "opt out" in text.lower():
        raise ValueError("no opt-out boilerplate; use the natural sentence")
    return text


def booking_confirmation(
    first_name: str | None, rep_name: str | None, starts_at: datetime, tz: str | None
) -> str:
    return check(
        f"{_first(first_name)}, {_intro(rep_name)}. You're booked with me for "
        f"{format_when(starts_at, tz)}. I'll send a short reminder the day before. {OPT_OUT_LINE}"
    )


def reminder_24h(first_name: str | None, rep_name: str | None, starts_at: datetime, tz: str | None) -> str:
    return check(
        f"{_first(first_name)}, {_intro(rep_name)}. Looking forward to our call on "
        f"{format_when(starts_at, tz)}. If the time no longer works, reply here and "
        f"I'll find another. {OPT_OUT_LINE}"
    )


def reminder_1h(first_name: str | None, rep_name: str | None, starts_at: datetime, tz: str | None) -> str:
    rep = _rep(rep_name)
    who = f"{rep} from {COMPANY}" if rep else f"your contact at {COMPANY}"
    return check(
        f"{_first(first_name)}, {who} here. We're on in about an hour, at "
        f"{format_when(starts_at, tz, with_day=False)}. Running late? Just text me."
    )


def reply_follow_up(first_name: str | None, rep_name: str | None, disposition: str) -> str:
    if disposition == "meeting_requested":
        ask = "What day and time suit you for a 20 minute call? I'll send the invite."
    else:
        ask = "Glad it caught your eye. Is a quick call this week easier than email?"
    return check(
        f"{_first(first_name)}, {_intro(rep_name)}, following up on your email. {ask} {OPT_OUT_LINE}"
    )


def render(
    template: str,
    *,
    first_name: str | None,
    rep_name: str | None,
    starts_at: datetime | None = None,
    tz: str | None = None,
    disposition: str | None = None,
) -> str:
    if template == CONFIRMATION:
        return booking_confirmation(first_name, rep_name, starts_at, tz)
    if template == REMINDER_24H:
        return reminder_24h(first_name, rep_name, starts_at, tz)
    if template == REMINDER_1H:
        return reminder_1h(first_name, rep_name, starts_at, tz)
    if template == FOLLOW_UP:
        return reply_follow_up(first_name, rep_name, disposition or "interested")
    raise ValueError(f"unknown template {template!r}")
