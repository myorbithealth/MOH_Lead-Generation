"""Warm texting channel (SendBlue): consent gate, stop handling, reminders, inbound texts.

Never cold. A contact is texted only when contacts.sms_consent_at is set (captured on the Cal.com
booking form), the phone is E.164, the number has no sms_stop suppression, and it is between
08:00 and 21:00 in the contact's timezone. Every text sent is an `activities` row (kind sms_sent)
whose body starts with a tag such as "[sms:reminder_24h]"; the tag plus meeting_id is the
idempotency key, so the hourly job can run any number of times.

Inbound texts arrive as webhook_events(provider 'sendblue', event_type 'receive') written by the
portal's `sendblue-webhook` route, and go through the same classifier and routing as email replies.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..config import settings
from . import portal_io
from . import text_templates as tt
from .models import Classification, Disposition
from .sendblue import RateLimitExceeded, SendblueError, is_e164, message_id, normalize_us_e164

log = logging.getLogger(__name__)

DEFAULT_TZ = "America/New_York"
QUIET_START_HOUR = 8  # first hour texts may go out, local time
QUIET_END_HOUR = 21  # texts stop at 21:00 local time
COOLDOWN = timedelta(hours=24)
# Kinds that respect the 24 h cooldown. The confirmation answers a booking just made, the 1 h
# reminder always follows the 24 h one, and a stop confirmation must go out regardless.
COOLDOWN_KINDS = {tt.REMINDER_24H, tt.FOLLOW_UP}
ONE_HOUR_WINDOW = timedelta(minutes=90)  # hourly cron: the 1 h reminder goes 30 to 90 min before
MIN_LEAD_FOR_24H = timedelta(hours=6)  # inside this, skip the day-before text and wait for the 1 h one
CANCELLED_OUTCOMES = {"cancelled", "canceled", "rescheduled", "no_show"}
SMS_STOP = "sms_stop"
SUPPRESSION_SOURCE = "sendblue:inbound"

_STOP_WORDS = ("stop", "stopall", "unsubscribe", "cancel", "quit", "end", "opt out", "optout", "remove me")
_STOP_RE = re.compile(
    r"^\W*(" + "|".join(re.escape(w).replace(r"\ ", r"[\s-]*") for w in _STOP_WORDS) + r")\b", re.IGNORECASE
)
_TAG_RE = re.compile(r"^\[sms:(\w+)")

Classifier = Callable[[str, str | None, str | None, dict], tuple[Classification, str]]


# ---------------------------------------------------------------------- pure rules
def is_stop_text(body: str | None) -> bool:
    """Stop, unsubscribe, cancel, quit, end, opt out, remove me: the whole message or its first word.

    "Cancel" also matches "cancel my meeting": treating that as an opt-out is the safe side, and
    the rep still gets the classification and can reschedule by email.
    """
    return bool(body) and bool(_STOP_RE.match(body.strip()))


def _as_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def contact_tz(contact: dict) -> ZoneInfo:
    try:
        return ZoneInfo(contact.get("timezone") or DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TZ)


def can_text(
    contact: dict,
    *,
    kind: str = tt.REMINDER_24H,
    now: datetime | None = None,
    suppressed: bool | None = None,
    require_consent: bool = True,
) -> tuple[bool, str]:
    """(allowed, reason). `suppressed` is the sms_stop lookup for the phone (see phone_suppressed)."""
    now = now or datetime.now(UTC)
    if require_consent and not contact.get("sms_consent_at"):
        return False, "no_sms_consent"
    if not is_e164(contact.get("phone")):
        return False, "phone_not_e164"
    if suppressed or contact.get("sms_suppressed"):
        return False, "suppressed_sms_stop"
    last = _as_dt(contact.get("last_texted_at"))
    if kind in COOLDOWN_KINDS and last and now - last < COOLDOWN:
        return False, "texted_within_24h"
    local = now.astimezone(contact_tz(contact))
    if not QUIET_START_HOUR <= local.hour < QUIET_END_HOUR:
        return False, "quiet_hours"
    return True, "ok"


def due_template(meeting: dict, sent: set[str], now: datetime, window_hours: float = 26) -> str | None:
    """Which text (if any) a meeting needs now. At most one per meeting per run."""
    starts = _as_dt(meeting.get("starts_at"))
    if starts is None:
        return None
    if str(meeting.get("outcome") or meeting.get("status") or "").lower() in CANCELLED_OUTCOMES:
        return None
    delta = starts - now
    if delta <= timedelta(0):
        return None
    if delta <= ONE_HOUR_WINDOW:
        return tt.REMINDER_1H if tt.REMINDER_1H not in sent else None
    if tt.CONFIRMATION not in sent:
        return tt.CONFIRMATION
    if MIN_LEAD_FOR_24H < delta <= timedelta(hours=window_hours) and tt.REMINDER_24H not in sent:
        return tt.REMINDER_24H
    return None


def first_name(contact: dict) -> str | None:
    name = contact.get("first_name") or contact.get("full_name") or contact.get("name") or ""
    return name.split(" ")[0] or None


def tag_of(activity_body: str | None) -> str | None:
    m = _TAG_RE.match(activity_body or "")
    return m.group(1) if m else None


# ---------------------------------------------------------------------- portal helpers
def find_contact_by_phone(portal, phone: str | None) -> dict | None:
    if not phone:
        return None
    return portal_io.first(portal.select("contacts", {"phone": f"eq.{phone}"}, limit=1))


def phone_suppressed(portal, phone: str | None, now: datetime | None = None) -> bool:
    if not phone:
        return False
    now = now or datetime.now(UTC)
    for row in portal.select("suppressions", {"phone": f"eq.{phone}"}, limit=5):
        expires = _as_dt(row.get("expires_at"))
        if expires is None or expires > now:
            return True
    return False


def add_phone_suppression(portal, phone: str, source: str = SUPPRESSION_SOURCE) -> bool:
    """Permanent sms_stop row for the phone. True when the number was not already suppressed."""
    rows = portal.select("suppressions", {"phone": f"eq.{phone}"}, limit=5)
    if rows:
        row = rows[0]
        active = _as_dt(row.get("expires_at")) is None or _as_dt(row["expires_at"]) > datetime.now(UTC)
        if row.get("reason") != SMS_STOP or row.get("expires_at") is not None:
            portal.patch(
                "suppressions",
                {"id": f"eq.{row['id']}"},
                {"reason": SMS_STOP, "expires_at": None, "source": source},
            )
        return not active
    portal_io.insert_ignoring_duplicates(
        portal,
        "suppressions",
        {
            "phone": phone,
            "email": None,
            "domain": None,
            "reason": SMS_STOP,
            "source": source,
            "expires_at": None,
        },
    )
    return True


def sent_templates(portal, *, meeting_id: Any = None, contact_id: Any = None) -> list[str]:
    """Tags of sms_sent activities for a meeting (or a contact)."""
    filters: dict[str, Any] = {"kind": "eq.sms_sent"}
    if meeting_id is not None:
        filters["meeting_id"] = f"eq.{meeting_id}"
    elif contact_id is not None:
        filters["contact_id"] = f"eq.{contact_id}"
    else:
        return []
    return [t for t in (tag_of(a.get("body")) for a in portal.select_all("activities", filters)) if t]


def rep_name_for(portal, profile_id: str | None) -> str | None:
    from . import routing

    if profile_id:
        try:
            _, reps, _ = routing.load_config(portal)
        except Exception:  # noqa: BLE001
            reps = []
        for rep in [*reps, *routing.DEFAULT_REPS]:
            if rep.get("profile_id") == profile_id and rep.get("name"):
                return rep["name"]
    return settings.outreach_sender_name or None


# ---------------------------------------------------------------------- service
class TextService:
    """Sends and records texts. `client` is a SendblueClient (or a fake with send_message)."""

    def __init__(
        self,
        portal,
        client=None,
        *,
        now: Callable[[], datetime] | None = None,
        classifier: Classifier | None = None,
        echo: Callable[[str], None] | None = None,
    ):
        self.portal = portal
        self._client = client
        self.now = now or (lambda: datetime.now(UTC))
        self.classifier = classifier
        self.echo = echo or (lambda line: log.info(line))

    @property
    def client(self):
        if self._client is None:
            from .sendblue import SendblueClient

            self._client = SendblueClient()
        return self._client

    # -- one text
    def send(
        self,
        contact: dict | None,
        phone: str,
        template: str,
        body: str,
        *,
        meeting_id: Any = None,
        note: str | None = None,
    ) -> dict:
        contact = contact or {}
        if contact.get("last_texted_at") and hasattr(self.client, "limiter"):
            self.client.limiter.mark_known([phone])
        result = self.client.send_message(phone, body)
        handle = message_id(result)
        tag = f"[sms:{template}{f' {note}' if note else ''}{f' handle={handle}' if handle else ''}]"
        now = self.now().isoformat()
        self.portal.insert(
            "activities",
            {
                "kind": "sms_sent",
                "body": f"{tag} {body}",
                "contact_id": contact.get("id"),
                "company_id": contact.get("company_id"),
                "meeting_id": meeting_id,
            },
        )
        if contact.get("id"):
            self.portal.patch("contacts", {"id": f"eq.{contact['id']}"}, {"last_texted_at": now})
        return result

    # -- reminders
    def run_reminders(self, *, window_hours: float = 26, dry_run: bool = False) -> dict[str, int]:
        now = self.now()
        stats: dict[str, int] = {"meetings": 0, "sent": 0, "failed": 0}
        meetings = self.portal.select_all(
            "meetings", {"starts_at": f"gte.{now.isoformat()}"}, order="starts_at.asc"
        )
        for meeting in meetings:
            stats["meetings"] += 1
            sent = set(sent_templates(self.portal, meeting_id=meeting["id"]))
            template = due_template(meeting, sent, now, window_hours)
            if template is None:
                stats["nothing_due"] = stats.get("nothing_due", 0) + 1
                continue
            contact = portal_io.get_contact(self.portal, meeting.get("contact_id"))
            if contact is None:
                stats["no_contact"] = stats.get("no_contact", 0) + 1
                continue
            ok, reason = can_text(
                contact,
                kind=template,
                now=now,
                suppressed=phone_suppressed(self.portal, contact.get("phone"), now),
            )
            if not ok:
                stats[reason] = stats.get(reason, 0) + 1
                continue
            body = tt.render(
                template,
                first_name=first_name(contact),
                rep_name=rep_name_for(self.portal, meeting.get("claimed_by")),
                starts_at=_as_dt(meeting["starts_at"]),
                tz=str(contact_tz(contact)),
            )
            if dry_run:
                self.echo(f"[dry run] {template} -> {contact['phone']} (meeting {meeting['id']}): {body}")
                stats["would_send"] = stats.get("would_send", 0) + 1
                continue
            try:
                self.send(contact, contact["phone"], template, body, meeting_id=meeting["id"])
                stats["sent"] += 1
                stats[template] = stats.get(template, 0) + 1
            except RateLimitExceeded as exc:
                log.warning("stopping reminders: %s", exc)
                stats["rate_limited"] = 1
                break
            except SendblueError as exc:
                log.warning("reminder %s for meeting %s failed: %s", template, meeting["id"], exc)
                stats["failed"] += 1
        return stats

    # -- follow-ups to positive email replies
    def run_follow_ups(self, *, days: int = 7, dry_run: bool = False) -> dict[str, int]:
        now = self.now()
        stats: dict[str, int] = {"candidates": 0, "sent": 0, "failed": 0}
        rows = self.portal.select_all(
            "reply_classifications",
            {
                "disposition": "in.(interested,meeting_requested)",
                "created_at": f"gte.{(now - timedelta(days=days)).isoformat()}",
            },
        )
        for rc in rows:
            if not rc.get("contact_id") or self._is_text_event(rc.get("webhook_event_id")):
                continue
            stats["candidates"] += 1
            tags = sent_templates(self.portal, contact_id=rc["contact_id"])
            if tt.FOLLOW_UP in tags:
                stats["already_sent"] = stats.get("already_sent", 0) + 1
                continue
            contact = portal_io.get_contact(self.portal, rc["contact_id"])
            if contact is None:
                continue
            ok, reason = can_text(
                contact,
                kind=tt.FOLLOW_UP,
                now=now,
                suppressed=phone_suppressed(self.portal, contact.get("phone")),
            )
            if not ok:
                stats[reason] = stats.get(reason, 0) + 1
                continue
            body = tt.reply_follow_up(
                first_name(contact), rep_name_for(self.portal, rc.get("routed_to")), rc["disposition"]
            )
            if dry_run:
                self.echo(f"[dry run] follow_up -> {contact['phone']}: {body}")
                stats["would_send"] = stats.get("would_send", 0) + 1
                continue
            try:
                self.send(contact, contact["phone"], tt.FOLLOW_UP, body, note=f"rc={rc.get('id')}")
                stats["sent"] += 1
            except RateLimitExceeded as exc:
                log.warning("stopping follow-ups: %s", exc)
                stats["rate_limited"] = 1
                break
            except SendblueError as exc:
                log.warning("follow-up for contact %s failed: %s", rc["contact_id"], exc)
                stats["failed"] += 1
        return stats

    def _is_text_event(self, event_id: Any) -> bool:
        if not event_id:
            return False
        row = portal_io.first(self.portal.select("webhook_events", {"id": f"eq.{event_id}"}, limit=1))
        return bool(row) and row.get("provider") == "sendblue"

    # -- manual send
    def send_manual(self, to: str, body: str, *, consent_source: str | None = None) -> dict:
        phone = to if is_e164(to) else normalize_us_e164(to)
        if not phone:
            raise ValueError(f"not a US or E.164 phone number: {to!r}")
        if len(body) >= tt.MAX_CHARS:
            raise ValueError(f"keep texts under {tt.MAX_CHARS} characters")
        contact = find_contact_by_phone(self.portal, phone)
        if consent_source and contact and not contact.get("sms_consent_at"):
            values = {"sms_consent_at": self.now().isoformat(), "sms_consent_source": consent_source}
            self.portal.patch("contacts", {"id": f"eq.{contact['id']}"}, values)
            contact = {**contact, **values}
        ok, reason = can_text(
            {**(contact or {}), "phone": phone},
            kind=tt.MANUAL,
            now=self.now(),
            suppressed=phone_suppressed(self.portal, phone),
            require_consent=not consent_source,
        )
        if not ok:
            raise PermissionError(f"not sending: {reason}")
        note = f"consent={consent_source.replace(']', ')')[:80]}" if consent_source else None
        return self.send(contact, phone, tt.MANUAL, body, note=note)

    # -- inbound
    def run_inbound(self, limit: int = 200) -> dict[str, int]:
        stats: dict[str, int] = {"events": 0, "processed": 0, "failed": 0}
        events = self.portal.select(
            "webhook_events",
            {"provider": "eq.sendblue", "event_type": "eq.receive", "processed_at": "is.null"},
            order="received_at.asc",
            limit=limit,
        )
        for event in events:
            stats["events"] += 1
            try:
                outcome = self.process_inbound(event)
                if outcome == "skipped_no_key":
                    stats[outcome] = stats.get(outcome, 0) + 1
                    continue
                portal_io.mark_event(self.portal, event["id"])
                stats["processed"] += 1
                stats[outcome] = stats.get(outcome, 0) + 1
            except Exception as exc:
                log.exception("sendblue event %s failed", event.get("id"))
                stats["failed"] += 1
                try:
                    portal_io.mark_event(self.portal, event["id"], error=f"{type(exc).__name__}: {exc}")
                except Exception:
                    log.exception("could not record error on event %s", event.get("id"))
        return stats

    def _classifier(self) -> Classifier | None:
        if self.classifier is not None:
            return self.classifier
        if not settings.anthropic_key:
            return None
        from .classify import classify_reply_with_model

        return classify_reply_with_model

    def process_inbound(self, event: dict) -> str:
        from . import routing
        from .process_replies import classification_from_row, needs_rep

        payload = event.get("payload") or {}
        if payload.get("is_outbound"):
            return "outbound_ignored"
        text = str(payload.get("content") or "").strip()
        raw_from = payload.get("from_number") or payload.get("number")
        phone = raw_from if is_e164(raw_from) else normalize_us_e164(raw_from) or raw_from
        contact = find_contact_by_phone(self.portal, phone)
        lead_domain = (contact or {}).get("lead_domain")
        stop = is_stop_text(text) or payload.get("opted_out") is True

        existing = portal_io.classification_for_event(self.portal, event["id"])
        if existing:
            c, model = classification_from_row(existing), existing.get("model")
        elif stop:
            c = Classification(
                disposition=Disposition.unsubscribe,
                confidence="high",
                summary="Asked to stop receiving texts.",
            )
            model = "rule:stop_word"
        elif not text:
            c = Classification(
                disposition=Disposition.unclear, confidence="low", summary="Sent a text with no words."
            )
            model = "rule:empty_text"
        else:
            classifier = self._classifier()
            if classifier is None:
                log.warning("ANTHROPIC_API_KEY not set; leaving text event %s for the next run", event["id"])
                return "skipped_no_key"
            lead = portal_io.get_lead(self.portal, lead_domain) if lead_domain else None
            context = {
                k: (lead or {}).get(k) for k in ("company_name", "vertical", "segment") if (lead or {}).get(k)
            }
            c, model = classifier(
                text, "Text message", self._last_text(contact), {**context, "channel": "sms"}
            )

        lead = portal_io.get_lead(self.portal, lead_domain) if lead_domain else None
        assignment = None
        if needs_rep(c):
            assignment = routing.route(
                lead_domain, c.disposition.value, self.portal, contact_id=(contact or {}).get("id"), lead=lead
            )

        if not existing:
            rc = portal_io.insert_ignoring_duplicates(
                self.portal,
                "reply_classifications",
                {
                    "webhook_event_id": event["id"],
                    "contact_id": (contact or {}).get("id"),
                    "lead_domain": lead_domain,
                    "disposition": c.disposition.value,
                    "confidence": c.confidence,
                    "summary": c.summary,
                    "objection": c.objection.value,
                    "snooze_until": c.snooze_until.isoformat() if c.snooze_until else None,
                    "referral": c.referral.model_dump() if c.referral else None,
                    "requested_times": c.requested_times,
                    "draft_reply": c.draft_reply,
                    "contains_phi": c.contains_phi,
                    "model": model,
                    "routed_to": assignment.rep_id if assignment else None,
                },
            )
            existing = rc or portal_io.classification_for_event(self.portal, event["id"]) or {}
            self.portal.insert(
                "activities",
                {
                    "kind": "sms_received",
                    "body": "[sms:inbound] " + ("(redacted: health detail)" if c.contains_phi else text),
                    "contact_id": (contact or {}).get("id"),
                    "company_id": (contact or {}).get("company_id"),
                },
            )
        if contact:
            values: dict[str, Any] = {"last_replied_at": payload.get("date_sent") or portal_io.now_iso()}
            if assignment and assignment.rep_id:
                values["routed_to"] = assignment.rep_id
            self.portal.patch("contacts", {"id": f"eq.{contact['id']}"}, values)

        newly_stopped = (
            c.disposition == Disposition.unsubscribe
            and is_e164(phone)
            and add_phone_suppression(self.portal, phone)
        )
        if newly_stopped and not payload.get("opted_out"):
            try:
                self.send(contact, phone, tt.STOP_CONFIRM, tt.STOP_CONFIRMATION)
            except Exception:  # the suppression is what matters; never re-send
                log.warning("stop confirmation to %s failed", phone, exc_info=True)

        if assignment and assignment.rep_id:
            company = (lead or {}).get("company_name") or lead_domain or phone or "unknown"
            portal_io.insert_ignoring_duplicates(
                self.portal,
                "tasks",
                {
                    "title": f"Text reply ({c.disposition.value.replace('_', ' ')}): {company}"[:300],
                    "due_at": (self.now() + timedelta(minutes=60)).isoformat(),
                    "priority": "high"
                    if c.disposition.value in ("interested", "meeting_requested")
                    else "normal",
                    "assignee_id": assignment.rep_id,
                    "company_id": (contact or {}).get("company_id"),
                    "contact_id": (contact or {}).get("id"),
                    "lead_domain": lead_domain,
                    "source": f"sms:{event['id']}",
                },
            )
        return c.disposition.value

    def _last_text(self, contact: dict | None) -> str | None:
        if not contact or not contact.get("id"):
            return None
        rows = self.portal.select(
            "activities",
            {"kind": "eq.sms_sent", "contact_id": f"eq.{contact['id']}"},
            order="created_at.desc",
            limit=1,
        )
        body = (rows[0].get("body") if rows else None) or ""
        return re.sub(r"^\[sms:[^\]]*\]\s*", "", body) or None
