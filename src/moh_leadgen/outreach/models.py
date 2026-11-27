"""Shared outreach types. Keep this file dependency-free (pydantic + stdlib only).

The Disposition values are the contract between: the Claude classifier (strict JSON schema enum),
the Postgres enum `public.disposition` in the Sales Portal, Smartlead lead categories, and routing.
Lowercase snake_case so casing drift can never break a match.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

Segment = Literal["glp1_competitor", "trt_peptides", "med_spa_clinic", "pharmacy_general", "other"]
SEGMENTS: tuple[str, ...] = ("glp1_competitor", "trt_peptides", "med_spa_clinic", "pharmacy_general", "other")


class Disposition(StrEnum):
    interested = "interested"
    meeting_requested = "meeting_requested"
    meeting_booked = "meeting_booked"
    question_objection = "question_objection"
    maybe_later = "maybe_later"
    not_interested = "not_interested"
    wrong_person_referral = "wrong_person_referral"
    out_of_office = "out_of_office"
    auto_reply = "auto_reply"
    unsubscribe = "unsubscribe"
    bounce = "bounce"
    unclear = "unclear"


POSITIVE_DISPOSITIONS = {Disposition.interested, Disposition.meeting_requested, Disposition.meeting_booked}
# dispositions that need a human within the reply SLA
ACTIONABLE_DISPOSITIONS = POSITIVE_DISPOSITIONS | {
    Disposition.question_objection,
    Disposition.wrong_person_referral,
}
# dispositions that suppress further sends to the contact (and, for unsubscribe/complaint, the domain)
SUPPRESSING_DISPOSITIONS = {Disposition.not_interested, Disposition.unsubscribe, Disposition.bounce}


# Suppression reasons that never expire. A not_interested row expires (180 days); these do not.
PERMANENT_SUPPRESSION_REASONS = frozenset(
    {"unsubscribe", "bounce", "complaint", "customer", "competitor", "manual"}
)


def _as_datetime(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def suppression_update(
    existing: dict[str, Any], reason: str, expires_at: datetime | None, now: datetime
) -> dict[str, Any] | None:
    """Fields to PATCH onto an existing suppressions row for the same email/domain, or None.

    There is one row per email (and per domain) because of the unique indexes, so a stronger
    suppression has to upgrade that row rather than be dropped as a duplicate: an unsubscribe
    after a not_interested must become permanent, and an expired row must be re-armed.
    """
    old_expiry = _as_datetime(existing.get("expires_at"))
    if old_expiry is None:
        return None  # already permanent
    if reason in PERMANENT_SUPPRESSION_REASONS:
        return {"reason": reason, "expires_at": None}
    if old_expiry <= now:
        return {"reason": reason, "expires_at": expires_at.isoformat() if expires_at else None}
    return None  # still active and the new one is no stronger


class Objection(StrEnum):
    none = "none"
    price = "price"
    timing = "timing"
    already_have_platform = "already_have_platform"
    compliance_or_legitscript = "compliance_or_legitscript"
    not_decision_maker = "not_decision_maker"
    bad_experience_with_vendors = "bad_experience_with_vendors"
    other = "other"


class Referral(BaseModel):
    name: str | None = None
    email: str | None = None
    title: str | None = None


class Classification(BaseModel):
    """Exactly what the classifier returns. `json_schema()` is the strict schema sent to Claude."""

    disposition: Disposition
    confidence: Literal["low", "medium", "high"]
    summary: str = Field(description="One sentence, no reasoning chain")
    snooze_until: date | None = Field(default=None, description="For maybe_later / out_of_office")
    referral: Referral | None = None
    requested_times: list[str] = Field(default_factory=list)
    objection: Objection = Objection.none
    draft_reply: str | None = Field(
        default=None,
        description="Only for interested, meeting_requested, question_objection, wrong_person_referral; "
        "use the literal placeholder {{booking_link}} for the rep's calendar link",
    )
    contains_phi: bool = Field(
        default=False, description="True if the reply mentions any patient or health detail"
    )

    @classmethod
    def json_schema(cls) -> dict[str, Any]:
        """Strict schema for Claude structured outputs: closed objects, every key required."""
        return {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "disposition",
                "confidence",
                "summary",
                "snooze_until",
                "referral",
                "requested_times",
                "objection",
                "draft_reply",
                "contains_phi",
            ],
            "properties": {
                "disposition": {"type": "string", "enum": [d.value for d in Disposition]},
                "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                "summary": {"type": "string"},
                "snooze_until": {"type": ["string", "null"], "description": "ISO date YYYY-MM-DD or null"},
                "referral": {
                    "type": ["object", "null"],
                    "additionalProperties": False,
                    "required": ["name", "email", "title"],
                    "properties": {
                        "name": {"type": ["string", "null"]},
                        "email": {"type": ["string", "null"]},
                        "title": {"type": ["string", "null"]},
                    },
                },
                "requested_times": {"type": "array", "items": {"type": "string"}},
                "objection": {"type": "string", "enum": [o.value for o in Objection]},
                "draft_reply": {"type": ["string", "null"]},
                "contains_phi": {"type": "boolean"},
            },
        }


class OutreachState(BaseModel):
    """Per-lead outreach bookkeeping stored inside Lead (and mirrored to portal columns)."""

    segment: str | None = None
    smartlead_campaign_id: int | None = None
    smartlead_lead_ids: dict[str, int] = Field(default_factory=dict)  # email -> Smartlead lead id
    variant_id: str | None = None
    enrolled_at: datetime | None = None
    enrolled_emails: list[str] = Field(default_factory=list)
    last_outreach_at: datetime | None = None
    disposition: Disposition | None = None
    suppressed_at: datetime | None = None
    suppression_reason: str | None = None


class RepAssignment(BaseModel):
    rep_id: str | None = None  # portal profiles.id (uuid) when known
    rep_name: str
    rep_email: str | None = None
    booking_link: str | None = None
    rule: str  # which rule fired (for the audit trail)


class Variant(BaseModel):
    id: str
    segment: str
    sequence_step: int
    name: str
    angle: str | None = None
    subject: str | None = None
    body: str | None = None
    hypothesis: str | None = None
    parent_id: str | None = None
    status: Literal["active", "control", "retired", "proposed"] = "active"
    smartlead_campaign_id: int | None = None
    sends: int = 0
    opens: int = 0
    replies: int = 0
    positive_replies: int = 0
    meetings: int = 0
    alpha: float = 1.0
    beta: float = 1.0
