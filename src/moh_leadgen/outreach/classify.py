"""Classify a prospect's reply into the shared Disposition taxonomy with Claude structured outputs.

Haiku 5.5 at low effort handles every reply (fractions of a cent). When it is unsure
(confidence low, or disposition unclear) the same request is re-run once on Sonnet 5.5.
The output is constrained to exactly `Classification.json_schema()`, the schema the portal's
Edge Function also sends, so both paths can only ever produce the same enum values.

The Anthropic client is injectable: tests pass a fake that returns canned JSON.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from typing import Any

from pydantic import ValidationError

from ..config import settings
from .models import Classification, Disposition, Objection

log = logging.getLogger(__name__)

MAX_REPLY_CHARS = 6000  # a reply plus a quoted thread; anything longer is signature/legal noise
FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM_PROMPT = """\
You classify replies to B2B cold emails sent by MyOrbit Health. MyOrbit Health sells white-label \
telehealth infrastructure (licensed providers, pharmacy fulfilment, compliance, checkout) to DTC \
telehealth brands, med spas, clinics and gyms. The person replying is a business owner or employee, \
never a patient. Return only the JSON object the schema asks for.

## Dispositions (pick exactly one)
- interested: wants to learn more or says yes, but has not asked for a specific meeting time.
- meeting_requested: asks to talk, proposes times, or asks for a calendar link.
- meeting_booked: says they already booked a slot or accepted an invite.
- question_objection: asks a question or raises a concern (price, compliance, current vendor) \
without saying no.
- maybe_later: not now, but leaves the door open ("next quarter", "after our launch").
- not_interested: a clear no, with no request to stop emailing.
- wrong_person_referral: says they are not the right person, and/or names someone else.
- out_of_office: automatic away/vacation/leave notice, usually with a return date.
- auto_reply: any other automatic message (ticket received, mailbox not monitored, generic \
autoresponder) that is not an out-of-office notice.
- unsubscribe: asks to stop, be removed, unsubscribe, or not be contacted again, in any wording.
- bounce: a delivery failure notice (mailbox does not exist, message rejected).
- unclear: none of the above can be decided from the text.

## Hard rules
1. A reply that asks to stop, remove, or unsubscribe is ALWAYS unsubscribe, even if it is polite or \
also says not interested. A reply of just "stop" is unsubscribe (our footer offers that). Text quoted \
from our own earlier email, including that footer, is not their request.
2. If the reply mentions any patient, any person's health condition, treatment, medication or other \
health detail, set contains_phi to true and keep that detail out of summary and draft_reply.
3. summary is one plain sentence about what the sender wants. No reasoning steps. No patient details.
4. snooze_until: for maybe_later use the date they name, or null; for out_of_office use the return \
date if given (the day after they say they are back), or null. Otherwise null. Format YYYY-MM-DD.
5. referral: only for wrong_person_referral, with whatever name/email/title they gave; else null.
6. requested_times: the times or windows they proposed, copied as written; else an empty list.
7. objection: the main concern if there is one, else none.
8. confidence: high when the text is unambiguous, medium when one reading is clearly more likely, \
low otherwise.

## Tricky cases, with examples
maybe_later vs not_interested:
- "Not a priority right now, maybe revisit in Q2." -> maybe_later (snooze_until in Q2).
- "We're all set, thanks." -> not_interested.
question_objection vs interested:
- "How is this different from OpenLoop? We already pay them." -> question_objection, \
objection already_have_platform.
- "Sounds interesting, tell me more." -> interested.
wrong_person_referral:
- "I don't handle this, try Dana Ruiz, our COO, dana@brand.com." -> wrong_person_referral with \
referral {name: Dana Ruiz, email: dana@brand.com, title: COO}.
- "Not my area." -> wrong_person_referral with referral null.
auto_reply vs out_of_office:
- "I'm out of the office until 3 March with limited access to email." -> out_of_office.
- "Thanks for contacting support, we received your ticket #4411." -> auto_reply.
unsubscribe:
- "Please take me off your list." -> unsubscribe.
- "Not interested. Stop emailing me." -> unsubscribe (rule 1).

## draft_reply
Write draft_reply only for interested, meeting_requested, question_objection and \
wrong_person_referral; otherwise null. A human reviews it before it is sent. Two to four sentences, \
plain text, B2B tone. Answer their question directly if they asked one. Propose a short demo using \
the literal placeholder {{booking_link}}. Sign off as {{sender_name}}. Never make medical or \
treatment claims, never promise outcomes, never mention patients. For wrong_person_referral, thank \
them and ask for (or confirm) the right contact.
"""

UNSUBSCRIBE_RE = re.compile(
    r"\b(unsubscribe|remove me|take me off|stop (emailing|contacting|sending)|do not (email|contact)|"
    r"don'?t (email|contact) me)\b",
    re.IGNORECASE,
)
# Our footer says 'Reply "stop" and we will not email you again', so a bare "stop" is an opt-out.
BARE_STOP_RE = re.compile(r"^\W*(stop|unsubscribe|remove)(\W+(please|thanks|thank you))?\W*$", re.IGNORECASE)


def _default_client():
    import anthropic

    return anthropic.Anthropic(api_key=settings.anthropic_key or None)


def _unclear(summary: str) -> Classification:
    return Classification(disposition=Disposition.unclear, confidence="low", summary=summary)


def build_user_message(
    reply_text: str, our_last_subject: str | None, our_last_body: str | None, lead_context: dict, today: date
) -> str:
    """Everything that changes per reply goes here, after the cached system prompt."""
    context = json.dumps(lead_context or {}, sort_keys=True, default=str)
    return (
        f"Today is {today.isoformat()}.\n\n"
        f"Lead context: {context}\n\n"
        f"Our last email subject: {our_last_subject or '(unknown)'}\n"
        f"Our last email body:\n{(our_last_body or '(unknown)')[:3000]}\n\n"
        f"Their reply:\n{reply_text[:MAX_REPLY_CHARS]}"
    )


def _lower_enum(value: Any, allowed: set[str], default: str) -> str:
    """Enum casing is not guaranteed by the API, so match case-insensitively."""
    text = str(value or "").strip().lower()
    return text if text in allowed else default


def parse_classification(raw: str) -> Classification:
    """JSON text -> Classification; anything malformed degrades to unclear/low."""
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return _unclear("Classifier returned unparseable output")
    if not isinstance(data, dict):
        return _unclear("Classifier returned unparseable output")
    data["disposition"] = _lower_enum(data.get("disposition"), {d.value for d in Disposition}, "unclear")
    data["confidence"] = _lower_enum(data.get("confidence"), {"low", "medium", "high"}, "low")
    data["objection"] = _lower_enum(data.get("objection"), {o.value for o in Objection}, "other")
    if not data.get("snooze_until"):
        data["snooze_until"] = None
    try:
        return Classification.model_validate(data)
    except ValidationError:
        # Most often a malformed snooze date: keep the rest rather than lose the reply.
        data["snooze_until"] = None
        try:
            return Classification.model_validate(data)
        except ValidationError:
            return _unclear("Classifier output failed validation")


def _create(client, model: str, system: list[dict], user: str):
    """Haiku has no server-side refusal fallback; Sonnet/Opus opt into it (beta)."""
    kwargs = {
        "model": model,
        "max_tokens": 2048,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "output_config": {
            "effort": "low",
            "format": {"type": "json_schema", "schema": Classification.json_schema()},
        },
    }
    if "haiku" in model:
        return client.messages.create(**kwargs)
    return client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)


def _log_usage(model: str, response) -> None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    log.info(
        "classify %s usage: input=%s output=%s cache_read=%s cache_write=%s",
        model,
        getattr(usage, "input_tokens", None),
        getattr(usage, "output_tokens", None),
        getattr(usage, "cache_read_input_tokens", None),
        getattr(usage, "cache_creation_input_tokens", None),
    )


def _ask(client, model: str, user: str) -> Classification:
    system = [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}]
    response = _create(client, model, system, user)
    _log_usage(model, response)
    if response.stop_reason == "refusal":
        return _unclear("Classifier declined this reply")
    if response.stop_reason == "max_tokens":
        return _unclear("Classifier output was cut off")
    text = next((b.text for b in response.content if getattr(b, "type", None) == "text"), "")
    return parse_classification(text)


def needs_escalation(c: Classification) -> bool:
    return c.confidence == "low" or c.disposition == Disposition.unclear


QUOTE_START_RE = re.compile(
    r"^(on .{0,200}wrote:|-{2,}\s*original message|from:\s|sent from my )", re.IGNORECASE | re.MULTILINE
)
# Only negative-ish answers are overridden: a positive reply that quotes our own footer (which
# says "unsubscribe") must never be turned into a suppression.
_OVERRIDABLE = {Disposition.not_interested, Disposition.maybe_later, Disposition.unclear}


def strip_quoted(text: str) -> str:
    """The new part of a reply: drop '>' lines and everything after the quoted-thread marker."""
    text = text or ""
    match = QUOTE_START_RE.search(text)
    if match:
        text = text[: match.start()]
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(">")).strip()


def _apply_hard_rules(c: Classification, reply_text: str) -> Classification:
    """Belt and braces for the compliance rule: an explicit stop request is always unsubscribe."""
    new_text = strip_quoted(reply_text)
    explicit = BARE_STOP_RE.match(new_text) or (
        c.disposition in _OVERRIDABLE and UNSUBSCRIBE_RE.search(new_text)
    )
    if explicit and c.disposition != Disposition.unsubscribe:
        return c.model_copy(update={"disposition": Disposition.unsubscribe, "draft_reply": None})
    return c


def classify_reply_with_model(
    reply_text: str,
    our_last_subject: str | None,
    our_last_body: str | None,
    lead_context: dict,
    client=None,
    today: date | None = None,
) -> tuple[Classification, str]:
    """Classify and also return which model produced the answer (stored on reply_classifications)."""
    client = client or _default_client()
    user = build_user_message(
        reply_text, our_last_subject, our_last_body, lead_context, today or date.today()
    )
    model = settings.classify_model
    result = _ask(client, model, user)
    if needs_escalation(result):
        model = settings.classify_escalation_model
        log.info("escalating reply classification to %s", model)
        result = _ask(client, model, user)
    return _apply_hard_rules(result, reply_text), model


def classify_reply(
    reply_text: str,
    our_last_subject: str | None,
    our_last_body: str | None,
    lead_context: dict,
    client=None,
) -> Classification:
    return classify_reply_with_model(reply_text, our_last_subject, our_last_body, lead_context, client)[0]
