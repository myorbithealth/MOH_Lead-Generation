"""Send reply drafts a rep approved in the Sales Portal, through Smartlead.

The portal's Approve button only sets email_drafts.status = 'approved'; it holds no Smartlead key.
This hourly sweep (`leadgen outreach send-approved`) sends each approved outbound draft as a reply
in the lead's existing Smartlead thread, so it goes out from the mailbox that started it:

  POST /campaigns/{campaign_id}/reply-email-thread
  https://api.smartlead.ai/api-reference/campaigns/reply-email-thread

Outcomes per row:
  * sent: status 'sent', sent_at, provider_message_id when Smartlead returns one, plus an
    activities row (kind 'reply_sent').
  * Smartlead 4xx, or ids that cannot be resolved: status 'send_failed' with the reason in
    `send_error` when the row has that column, else appended to `notes`.
  * Smartlead 5xx / 429 / network error: left 'approved' for the next run.

A row is sent at most once: it is re-read right before the call and skipped unless it is still
approved with sent_at null, and the success patch is conditional on the same state.
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from ..config import settings
from . import portal_io
from .routing import with_tracking
from .smartlead import SmartleadError

log = logging.getLogger(__name__)

BOOKING_PLACEHOLDER = "{{booking_link}}"
SENDER_PLACEHOLDER = "{{sender_name}}"
ACTIVITY_BODY_CHARS = 200
# Replies go out from the persona mailbox that started the thread, so Smartlead's mailbox signature
# signs them and the draft's {{sender_name}} line is dropped. app_settings.outreach.reply_add_signature
# = false restores the old behaviour (rep name in the body, no signature).
DEFAULT_ADD_SIGNATURE = True


class SendFailed(Exception):
    """A permanent problem with this draft: mark it send_failed and move on."""


def body_to_html(text: str) -> str:
    """Plain text to the HTML Smartlead sends: escaped, line breaks kept."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    return html.escape(text, quote=False).replace("\n", "<br>")


def _int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _is_inbound(message: dict) -> bool:
    kind = str(message.get("type") or message.get("direction") or "").lower()
    return kind in ("reply", "inbound", "received")


def _message_time(message: dict) -> str:
    return str(message.get("time") or message.get("received_at") or message.get("sent_at") or "")


def latest_inbound(history: Any) -> dict | None:
    """The newest reply from the lead in a message-history response, or None."""
    if isinstance(history, dict):
        messages = history.get("messages") or history.get("history") or []
    else:
        messages = history or []
    inbound = [m for m in messages if isinstance(m, dict) and _is_inbound(m)]
    return max(inbound, key=_message_time) if inbound else None


def sent_message_id(result: Any) -> str | None:
    """Smartlead documents only {success, message}; use a message id if one ever comes back."""
    if not isinstance(result, dict):
        return None
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    for source in (result, data):
        for key in ("message_id", "messageId", "email_message_id"):
            if source.get(key):
                return str(source[key])
    return None


class ApprovedSender:
    def __init__(
        self,
        portal,
        smartlead,
        *,
        dry_run: bool = False,
        now: Callable[[], datetime] | datetime | None = None,
    ):
        self.portal = portal
        self.smartlead = smartlead
        self.dry_run = dry_run
        if isinstance(now, datetime):
            fixed = now
            self.now = lambda: fixed
        else:
            self.now = now or (lambda: datetime.now(UTC))
        self._outreach: dict | None = None

    @property
    def outreach(self) -> dict:
        if self._outreach is None:
            try:
                self._outreach = portal_io.get_app_setting(self.portal, "outreach")
            except Exception as exc:  # noqa: BLE001 - a missing setting only loses the rep link
                log.warning("could not read app_settings.outreach: %s", exc)
                self._outreach = {}
        return self._outreach

    # ------------------------------------------------------------------ the sweep
    def pending(self, limit: int) -> list[dict]:
        return self.portal.select(
            "email_drafts",
            {"status": "eq.approved", "direction": "eq.outbound", "sent_at": "is.null"},
            order="created_at.asc",
            limit=limit,
        )

    def run(self, limit: int = 100) -> dict:
        stats: dict[str, Any] = {"approved": 0, "sent": 0, "failed": 0, "retry": 0, "skipped": 0}
        if self.dry_run:
            stats["would_send"] = []
        for row in self.pending(limit):
            stats["approved"] += 1
            try:
                outcome = self.send_one(row)
            except Exception:
                log.exception("approved draft %s could not be processed", row.get("id"))
                outcome = "retry"
            if self.dry_run and outcome == "would_send":
                stats["would_send"].append(row.get("id"))
            else:
                stats[outcome] = stats.get(outcome, 0) + 1
        return stats

    def send_one(self, row: dict) -> str:
        draft_id = row["id"]
        contact = portal_io.get_contact(self.portal, row["contact_id"]) if row.get("contact_id") else None
        try:
            payload, campaign_id, text = self.build(row, contact)
        except SendFailed as exc:
            if not self.dry_run:
                self._mark_failed(row, str(exc))
            log.warning("draft %s not sendable: %s", draft_id, exc)
            return "failed"
        except SmartleadError as exc:  # the message-history lookup
            return self._smartlead_failure(row, exc)
        if self.dry_run:
            log.info("dry run: would reply to %s in campaign %s", row.get("to_email"), campaign_id)
            return "would_send"

        current = portal_io.first(self.portal.select("email_drafts", {"id": f"eq.{draft_id}"}, limit=1))
        if not current or current.get("status") != "approved" or current.get("sent_at"):
            log.info("draft %s changed since it was listed; skipping", draft_id)
            return "skipped"

        try:
            result = self.smartlead.reply_to_thread(campaign_id, **payload)
        except SmartleadError as exc:
            return self._smartlead_failure(row, exc)

        sent_at = self.now().isoformat()
        values: dict[str, Any] = {"status": "sent", "sent_at": sent_at}
        message_id = sent_message_id(result)
        if message_id:
            values["provider_message_id"] = message_id
        self.portal.patch(
            "email_drafts", {"id": f"eq.{draft_id}", "status": "eq.approved", "sent_at": "is.null"}, values
        )
        self.portal.insert(
            "activities",
            {
                "kind": "reply_sent",
                "contact_id": row.get("contact_id") or (contact or {}).get("id"),
                "company_id": row.get("company_id") or (contact or {}).get("company_id"),
                "body": text[:ACTIVITY_BODY_CHARS],
            },
        )
        return "sent"

    # ------------------------------------------------------------------ building the call
    def build(self, row: dict, contact: dict | None) -> tuple[dict, int, str]:
        """(reply-email-thread body, campaign id, plain text). SendFailed when it cannot be sent."""
        contact = contact or {}
        campaign_id = _int(row.get("smartlead_campaign_id")) or _int(contact.get("smartlead_campaign_id"))
        lead_id = _int(row.get("smartlead_lead_id")) or _int(contact.get("smartlead_lead_id"))
        stats_id = row.get("smartlead_stats_id") or row.get("email_stats_id")
        reply_message_id = row.get("provider_message_id")
        inbound: dict | None = None
        if not campaign_id:
            raise SendFailed("no Smartlead campaign id on the draft or its contact")
        if not stats_id and not lead_id:
            raise SendFailed("no Smartlead stats id and no lead id to look it up")
        if lead_id and (not stats_id or not reply_message_id):
            inbound = self._latest_inbound(campaign_id, lead_id, required=not stats_id)
        if inbound:
            stats_id = stats_id or inbound.get("stats_id") or inbound.get("email_stats_id")
            reply_message_id = reply_message_id or inbound.get("message_id") or inbound.get("id")
        if not stats_id:
            raise SendFailed("no Smartlead stats id: the lead's message history has no reply")

        text = self.fill_body(row)
        payload: dict[str, Any] = {
            "email_stats_id": str(stats_id),
            "email_body": body_to_html(text),
            "add_signature": self.add_signature,
        }
        optional = {
            "to_email": row.get("to_email"),
            "to_first_name": contact.get("first_name"),
            "to_last_name": contact.get("last_name"),
            "reply_message_id": str(reply_message_id) if reply_message_id else None,
            "reply_email_time": (inbound or {}).get("time"),
            "reply_email_body": (inbound or {}).get("email_body"),
        }
        payload.update({k: v for k, v in optional.items() if v})
        return payload, campaign_id, text

    def _latest_inbound(self, campaign_id: int, lead_id: int, *, required: bool) -> dict | None:
        try:
            return latest_inbound(self.smartlead.message_history(campaign_id, lead_id))
        except SmartleadError as exc:
            if required:
                raise
            log.info("message history for lead %s unavailable (%s); sending without it", lead_id, exc)
            return None

    def fill_body(self, row: dict) -> str:
        """The approved text; a {{booking_link}} the router could not fill is filled now."""
        text = row.get("body") or ""
        if not text.strip():
            raise SendFailed("the draft body is empty")
        rep = self._rep(row.get("approver_id"))
        if BOOKING_PLACEHOLDER in text:
            link = rep.get("booking_link") or self.outreach.get("booking_link") or settings.booking_url
            link = with_tracking(link, row.get("lead_domain"), row.get("contact_id"))
            if not link:
                raise SendFailed("{{booking_link}} is still in the body and no booking link is configured")
            text = text.replace(BOOKING_PLACEHOLDER, link)
        if SENDER_PLACEHOLDER in text:
            if self.add_signature:
                # The sending mailbox (a persona such as grant@) signs through Smartlead's signature,
                # so the draft's own sign-off line goes.
                text = re.sub(r"[ \t]*" + re.escape(SENDER_PLACEHOLDER) + r"[ \t]*", "", text).rstrip()
            else:
                name = rep.get("name") or settings.outreach_sender_name
                if not name:
                    raise SendFailed("{{sender_name}} is still in the body and no sender name is configured")
                text = text.replace(SENDER_PLACEHOLDER, name)
        return text

    @property
    def add_signature(self) -> bool:
        return bool(self.outreach.get("reply_add_signature", DEFAULT_ADD_SIGNATURE))

    def _rep(self, approver_id: str | None) -> dict:
        if not approver_id:
            return {}
        reps = self.outreach.get("reps") or []
        return next((r for r in reps if str(r.get("profile_id")) == str(approver_id)), {})

    # ------------------------------------------------------------------ failure
    def _smartlead_failure(self, row: dict, exc: SmartleadError) -> str:
        """4xx (not 429) is permanent: mark send_failed. 429, 5xx and network errors retry."""
        if exc.status is not None and 400 <= exc.status < 500 and exc.status != 429:
            if not self.dry_run:
                self._mark_failed(row, str(exc))
            return "failed"
        log.warning("draft %s: Smartlead unavailable, will retry next run: %s", row.get("id"), exc)
        return "retry"

    def _mark_failed(self, row: dict, error: str) -> None:
        error = error[:1000]
        values: dict[str, Any] = {"status": "send_failed"}
        if "send_error" in row:
            values["send_error"] = error
        else:
            stamp = self.now().strftime("%Y-%m-%d %H:%M UTC")
            note = f"[send failed {stamp}] {error}"
            values["notes"] = f"{row['notes']}\n{note}" if row.get("notes") else note
        try:
            self.portal.patch("email_drafts", {"id": f"eq.{row['id']}", "status": "eq.approved"}, values)
        except Exception:
            log.exception("could not mark draft %s send_failed", row.get("id"))


def send_approved(
    portal,
    smartlead,
    *,
    limit: int = 100,
    dry_run: bool = False,
    now: Callable[[], datetime] | datetime | None = None,
) -> dict:
    """Send every approved outbound reply draft (oldest first). Returns counts per outcome."""
    return ApprovedSender(portal, smartlead, dry_run=dry_run, now=now).run(limit)
