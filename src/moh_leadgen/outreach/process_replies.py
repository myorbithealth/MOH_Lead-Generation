"""Sweep unprocessed Smartlead EMAIL_REPLY webhook events: classify, record, route, act.

The portal's `smartlead-webhook` Edge Function normally does all of this inline. This sweep is the
retry path (it runs from `leadgen daily` and `leadgen outreach classify`): any event whose
processed_at is still null is handled here, and every step is safe to repeat:

  * an existing reply_classifications row for the event is reused (no second Claude call),
  * suppressions are checked before insert, tasks are unique on source 'reply:<id>',
  * the reply draft is looked up by reply_classification_id before it is created.

On any failure the event keeps processed_at null and gets `error`, and the sweep moves on.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from bs4 import BeautifulSoup

from ..config import settings
from . import portal_io, routing
from .ingest import FREEMAIL
from .models import (
    ACTIONABLE_DISPOSITIONS,
    Classification,
    Disposition,
    Objection,
    RepAssignment,
)

log = logging.getLogger(__name__)

# The portal spec says reply drafts are direction 'outbound'. Note they have no variant_id and no
# sequence_step, so the unique (smartlead_lead_id, sequence_step, direction) index never collides,
# but once sent they count as variant-less sends in outreach_funnel. 'reply' is allowed by the
# check constraint if the team prefers to keep them out of the funnel.
REPLY_DRAFT_DIRECTION = "outbound"
NOT_INTERESTED_SUPPRESSION_DAYS = 180
SNOOZE_DEFAULT_DAYS = {Disposition.maybe_later: 60, Disposition.out_of_office: 7}
DEFAULT_SLA_MINUTES = 60

# Smartlead lead categories by name (ids are fetched once per run: the docs disagree on defaults).
SMARTLEAD_CATEGORY = {
    Disposition.interested: "Interested",
    Disposition.meeting_requested: "Interested",
    Disposition.question_objection: "Interested",
    Disposition.meeting_booked: "Meeting Booked",
    Disposition.not_interested: "Not Interested",
    Disposition.unsubscribe: "Do Not Contact",
    Disposition.out_of_office: "Out of Office",
}
NO_PAUSE = {Disposition.auto_reply, Disposition.unclear, Disposition.bounce}

Classifier = Callable[[str, str | None, str | None, dict], tuple[Classification, str]]


# ---------------------------------------------------------------------- payload parsing
@dataclass
class ReplyFacts:
    lead_email: str | None
    reply_text: str
    subject: str | None
    campaign_id: int | None
    smartlead_lead_id: int | None
    stats_id: str | None
    sequence_number: int | None
    replied_at: str | None
    sent_subject: str | None = None
    sent_body: str | None = None


def html_to_text(html: str | None) -> str:
    if not html:
        return ""
    return BeautifulSoup(html, "lxml").get_text("\n").strip()


def _int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def extract_reply(payload: dict) -> ReplyFacts:
    """Read the three Smartlead payload shapes seen in their docs (see webhook notes in the plan)."""
    reply_message = payload.get("reply_message") or {}
    old_reply = payload.get("reply") or {}
    old_lead = payload.get("lead") or {}
    sent = payload.get("sent_message") or {}
    text = (
        reply_message.get("text")
        or old_reply.get("body")
        or html_to_text(reply_message.get("html"))
        or html_to_text(payload.get("reply_body"))
        or payload.get("preview_text")
        or ""
    )
    lead_email = (
        payload.get("sl_lead_email")
        or payload.get("lead_email")
        or old_lead.get("email")
        or payload.get("to_email")
    )
    return ReplyFacts(
        lead_email=(lead_email or "").strip().lower() or None,
        reply_text=str(text).strip(),
        subject=payload.get("subject")
        or old_reply.get("subject")
        or (payload.get("email") or {}).get("subject"),
        campaign_id=_int(payload.get("campaign_id")),
        smartlead_lead_id=_int(payload.get("sl_email_lead_id") or payload.get("lead_id")),
        stats_id=str(payload["stats_id"]) if payload.get("stats_id") else None,
        sequence_number=_int(payload.get("sequence_number")),
        replied_at=reply_message.get("time")
        or payload.get("time_replied")
        or old_reply.get("received_at")
        or payload.get("event_timestamp"),
        sent_subject=sent.get("subject"),
        sent_body=sent.get("text") or html_to_text(sent.get("html")) or None,
    )


def classification_from_row(row: dict) -> Classification:
    """Rebuild the Classification stored on a reply_classifications row (retry path)."""
    return Classification(
        disposition=Disposition(row["disposition"]),
        confidence=row.get("confidence") or "low",
        summary=row.get("summary") or "",
        snooze_until=row.get("snooze_until"),
        referral=row.get("referral"),
        requested_times=row.get("requested_times") or [],
        objection=row.get("objection") or Objection.none,
        draft_reply=row.get("draft_reply"),
        contains_phi=bool(row.get("contains_phi")),
    )


def fill_placeholders(text: str, assignment: RepAssignment | None) -> str:
    if assignment is None:
        return text
    if assignment.booking_link:
        text = text.replace("{{booking_link}}", assignment.booking_link)
    if assignment.rep_name and assignment.rep_name != "unassigned":
        text = text.replace("{{sender_name}}", assignment.rep_name)
    return text


def needs_rep(c: Classification) -> bool:
    """A human looks at positives, questions, referrals, unclear replies and unsure maybe-laters."""
    if c.disposition in ACTIONABLE_DISPOSITIONS or c.disposition == Disposition.unclear:
        return True
    return c.disposition == Disposition.maybe_later and c.confidence == "low"


def _reply_subject(subject: str | None) -> str:
    subject = (subject or "").strip() or "your note"
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


# ---------------------------------------------------------------------- processor
class ReplyProcessor:
    def __init__(
        self, portal, classifier: Classifier | None = None, smartlead=None, today: date | None = None
    ):
        self.portal = portal
        self.classifier = classifier or self._default_classifier
        self._smartlead = smartlead
        self._smartlead_loaded = smartlead is not None
        self._categories: dict[str, Any] | None = None
        self.today = today or date.today()
        self._outreach: dict | None = None

    # -- dependencies, created lazily so a dry run needs no keys
    @staticmethod
    def _default_classifier(text, subject, body, ctx):
        from .classify import classify_reply_with_model

        return classify_reply_with_model(text, subject, body, ctx)

    @property
    def smartlead(self):
        if not self._smartlead_loaded:
            self._smartlead_loaded = True
            try:
                from .smartlead import SmartleadClient  # written by the enrolment work

                self._smartlead = SmartleadClient()
            except (ImportError, RuntimeError) as exc:
                log.warning("Smartlead client unavailable, skipping pause/category: %s", exc)
        return self._smartlead

    @property
    def outreach_settings(self) -> dict:
        if self._outreach is None:
            try:
                self._outreach = portal_io.get_app_setting(self.portal, "outreach")
            except Exception as exc:  # noqa: BLE001
                log.warning("could not read app_settings.outreach: %s", exc)
                self._outreach = {}
        return self._outreach

    # -- the sweep
    def run(self, limit: int = 200) -> dict[str, int]:
        stats = {"events": 0, "processed": 0, "failed": 0}
        for event in portal_io.unprocessed_reply_events(self.portal, limit):
            stats["events"] += 1
            try:
                disposition = self.process_event(event)
                portal_io.mark_event(self.portal, event["id"])
                stats["processed"] += 1
                stats[disposition] = stats.get(disposition, 0) + 1
            except Exception as exc:
                log.exception("reply event %s failed", event.get("id"))
                stats["failed"] += 1
                try:
                    portal_io.mark_event(self.portal, event["id"], error=f"{type(exc).__name__}: {exc}")
                except Exception:
                    log.exception("could not record error on event %s", event.get("id"))
        return stats

    def process_event(self, event: dict) -> str:
        facts = extract_reply(event.get("payload") or {})
        contact, draft, lead_domain = self._resolve(facts)
        lead = portal_io.get_lead(self.portal, lead_domain) if lead_domain else None

        existing = portal_io.classification_for_event(self.portal, event["id"])
        if existing:
            c = classification_from_row(existing)
            rc = existing
            assignment = self._route(c, lead_domain, contact, lead) if needs_rep(c) else None
        else:
            c, model = self.classifier(
                facts.reply_text,
                (draft or {}).get("subject") or facts.sent_subject or facts.subject,
                (draft or {}).get("body") or facts.sent_body,
                self._lead_context(lead),
            )
            assignment = self._route(c, lead_domain, contact, lead) if needs_rep(c) else None
            rc = self._insert_classification(event, c, model, contact, draft, lead_domain, assignment)

        self._update_records(c, facts, contact, draft, lead_domain, lead, assignment)
        self._write_suppressions(c, facts, lead_domain)
        if assignment and assignment.rep_id:
            self._create_task(c, rc, contact, lead_domain, lead, assignment)
            if c.draft_reply:
                self._create_draft(c, rc, facts, contact, draft, lead_domain, assignment)
        self._update_smartlead(c, facts, contact)
        return c.disposition.value

    # -- lookups
    def _resolve(self, facts: ReplyFacts) -> tuple[dict | None, dict | None, str | None]:
        """(contact, the outbound draft replied to, lead domain)."""
        draft = portal_io.find_outbound_draft(
            self.portal,
            smartlead_lead_id=facts.smartlead_lead_id,
            sequence_step=facts.sequence_number,
            to_email=facts.lead_email,
        )
        contact = None
        if draft and draft.get("contact_id"):
            contact = portal_io.get_contact(self.portal, draft["contact_id"])
        if contact is None and facts.lead_email:
            contact = portal_io.find_contact_by_email(self.portal, facts.lead_email)
        if draft is None and contact is not None:
            draft = portal_io.find_outbound_draft(self.portal, contact_id=contact["id"])
        lead_domain = (contact or {}).get("lead_domain") or (draft or {}).get("lead_domain")
        if not lead_domain and facts.lead_email and "@" in facts.lead_email:
            domain = facts.lead_email.split("@", 1)[1]
            lead_domain = None if domain in FREEMAIL else domain
        return contact, draft, lead_domain

    @staticmethod
    def _lead_context(lead: dict | None) -> dict:
        lead = lead or {}
        keys = ("company_name", "vertical", "competitor_platform", "score", "segment", "legitscript_status")
        return {k: lead.get(k) for k in keys if lead.get(k) is not None}

    def _route(self, c: Classification, lead_domain, contact, lead) -> RepAssignment:
        return routing.route(
            lead_domain, c.disposition.value, self.portal, contact_id=(contact or {}).get("id"), lead=lead
        )

    # -- writes
    def _insert_classification(
        self, event, c: Classification, model, contact, draft, lead_domain, assignment
    ):
        row = {
            "webhook_event_id": event["id"],
            "contact_id": (contact or {}).get("id"),
            "lead_domain": lead_domain,
            "email_draft_id": (draft or {}).get("id"),
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
        }
        inserted = portal_io.insert_ignoring_duplicates(self.portal, "reply_classifications", row)
        return inserted or portal_io.classification_for_event(self.portal, event["id"]) or row

    def _snooze_until(self, c: Classification) -> date | None:
        if c.disposition not in SNOOZE_DEFAULT_DAYS:
            return None
        return c.snooze_until or self.today + timedelta(days=SNOOZE_DEFAULT_DAYS[c.disposition])

    def _update_records(self, c, facts, contact, draft, lead_domain, lead, assignment) -> None:
        now = portal_io.now_iso()
        if lead:
            values: dict[str, Any] = {"disposition": c.disposition.value}
            if c.disposition in (Disposition.not_interested, Disposition.unsubscribe):
                values |= {"suppressed_at": now, "suppression_reason": c.disposition.value}
            self.portal.patch("leads", {"domain": f"eq.{lead['domain']}"}, values)
        if contact:
            values = {"last_replied_at": facts.replied_at or now}
            if assignment and assignment.rep_id:
                values["routed_to"] = assignment.rep_id
            snooze = self._snooze_until(c)
            if snooze:
                values["snooze_until"] = snooze.isoformat()
            if c.disposition == Disposition.unsubscribe:
                values["unsubscribed_at"] = now
            if c.disposition == Disposition.bounce:
                values["bounced_at"] = now
            self.portal.patch("contacts", {"id": f"eq.{contact['id']}"}, values)
        if draft and not draft.get("replied_at"):
            self.portal.patch(
                "email_drafts", {"id": f"eq.{draft['id']}"}, {"replied_at": now, "status": "replied"}
            )

    def _write_suppressions(self, c: Classification, facts: ReplyFacts, lead_domain: str | None) -> None:
        email = facts.lead_email
        domain = lead_domain if lead_domain and lead_domain not in FREEMAIL else None
        source = "reply_classifier"
        if c.disposition == Disposition.not_interested and domain:
            expires = datetime.now(UTC) + timedelta(days=NOT_INTERESTED_SUPPRESSION_DAYS)
            portal_io.add_suppression(
                self.portal,
                email=None,
                domain=domain,
                reason="not_interested",
                source=source,
                expires_at=expires,
            )
        elif c.disposition == Disposition.unsubscribe:
            if email:
                portal_io.add_suppression(
                    self.portal, email=email, domain=domain, reason="unsubscribe", source=source
                )
            if domain:
                portal_io.add_suppression(
                    self.portal, email=None, domain=domain, reason="unsubscribe", source=source
                )
        elif c.disposition == Disposition.bounce and email:
            portal_io.add_suppression(self.portal, email=email, domain=domain, reason="bounce", source=source)

    def _create_task(self, c: Classification, rc: dict, contact, lead_domain, lead, assignment) -> None:
        sla = int(self.outreach_settings.get("reply_sla_minutes") or DEFAULT_SLA_MINUTES)
        company = (lead or {}).get("company_name") or lead_domain or (contact or {}).get("email") or "unknown"
        title = f"Reply ({c.disposition.value.replace('_', ' ')}): {company}"
        if c.referral and (c.referral.name or c.referral.email):
            title += f" - referral {c.referral.name or ''} {c.referral.email or ''}".rstrip()
        positive = c.disposition in (Disposition.meeting_requested, Disposition.interested)
        portal_io.insert_ignoring_duplicates(
            self.portal,
            "tasks",
            {
                "title": title[:300],
                "due_at": (datetime.now(UTC) + timedelta(minutes=sla)).isoformat(),
                "priority": "high" if positive else "normal",
                "assignee_id": assignment.rep_id,
                "company_id": (contact or {}).get("company_id"),
                "contact_id": (contact or {}).get("id"),
                "lead_domain": lead_domain,
                "source": f"reply:{rc.get('id') or rc.get('webhook_event_id')}",
            },
        )

    def _create_draft(self, c, rc: dict, facts: ReplyFacts, contact, draft, lead_domain, assignment) -> None:
        rc_id = rc.get("id")
        if rc_id and self.portal.select("email_drafts", {"reply_classification_id": f"eq.{rc_id}"}, limit=1):
            return
        to_email = facts.lead_email or (contact or {}).get("email")
        if not to_email:
            return
        self.portal.insert(
            "email_drafts",
            {
                "to_email": to_email,
                "subject": _reply_subject(facts.subject or (draft or {}).get("subject")),
                "body": fill_placeholders(c.draft_reply, assignment),
                "status": "pending_approval",
                "approver_id": assignment.rep_id,
                "contact_id": (contact or {}).get("id"),
                "lead_domain": lead_domain,
                "direction": REPLY_DRAFT_DIRECTION,
                "smartlead_campaign_id": facts.campaign_id or (contact or {}).get("smartlead_campaign_id"),
                "smartlead_lead_id": facts.smartlead_lead_id or (contact or {}).get("smartlead_lead_id"),
                "smartlead_stats_id": facts.stats_id or (draft or {}).get("smartlead_stats_id"),
                "reply_classification_id": rc_id,
            },
        )

    # -- Smartlead
    def _category_id(self, name: str):
        if self._categories is None:
            self._categories = {}
            try:
                for cat in self.smartlead.fetch_categories() or []:
                    self._categories[str(cat.get("name", "")).strip().lower()] = cat.get("id")
            except Exception as exc:  # noqa: BLE001
                log.warning("could not fetch Smartlead categories: %s", exc)
        return self._categories.get(name.lower())

    def _update_smartlead(self, c: Classification, facts: ReplyFacts, contact) -> None:
        if c.disposition in NO_PAUSE or self.smartlead is None:
            return
        campaign_id = facts.campaign_id or (contact or {}).get("smartlead_campaign_id")
        lead_id = facts.smartlead_lead_id or (contact or {}).get("smartlead_lead_id")
        if not campaign_id or not lead_id:
            log.info("no Smartlead campaign/lead id for %s; not pausing", facts.lead_email)
            return
        try:
            name = SMARTLEAD_CATEGORY.get(c.disposition)
            category_id = self._category_id(name) if name else None
            if category_id is not None:
                self.smartlead.set_lead_category(campaign_id, lead_id, category_id, pause=True)
            else:
                self.smartlead.pause_lead(campaign_id, lead_id)
            if c.disposition == Disposition.unsubscribe:
                self.smartlead.unsubscribe_lead_globally(lead_id)
            elif c.disposition == Disposition.not_interested:
                self.smartlead.unsubscribe_lead(campaign_id, lead_id)
        except Exception:
            # Opt-outs must reach Smartlead, so fail the event and let the next sweep retry. For the
            # rest, Smartlead's stop-on-reply already paused the lead; the portal rows are what count.
            if c.disposition in (Disposition.unsubscribe, Disposition.not_interested):
                raise
            log.warning("Smartlead update failed for lead %s", lead_id, exc_info=True)


def process_replies(
    portal, limit: int = 200, *, classifier: Classifier | None = None, smartlead=None
) -> dict:
    """Entry point for `leadgen daily` and `leadgen outreach classify`."""
    if classifier is None and not settings.anthropic_key:
        log.warning("ANTHROPIC_API_KEY not set; reply sweep skipped")
        return {"events": 0, "processed": 0, "failed": 0, "skipped_no_key": 1}
    return ReplyProcessor(portal, classifier=classifier, smartlead=smartlead).run(limit)
