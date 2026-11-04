"""Core data models shared by every lead-generation tool."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

LeadSource = Literal["legitscript", "competitor_platform", "local_business", "manual"]
LeadStatus = Literal["new", "reviewed", "contacted", "meeting_booked", "promoted", "disqualified"]
LegitScriptStatus = Literal["seal_detected", "mentioned", "not_found", "unchecked"]


def utcnow() -> datetime:
    return datetime.now(UTC)


class Candidate(BaseModel):
    """A domain that *might* be a lead. Produced by discovery sources."""

    domain: str
    source: LeadSource
    discovered_via: str
    source_detail: dict[str, Any] = Field(default_factory=dict)


class Person(BaseModel):
    """A named human associated with the business (owner, founder, medical director...)."""

    name: str
    title: str | None = None
    email: str | None = None
    email_status: str | None = None  # verified | provider | on_site | guessed
    phone: str | None = None
    phone_type: str | None = None  # mobile | work | office
    linkedin: str | None = None
    source_url: str | None = None
    confidence: float = 0.5
    is_decision_maker: bool = False


class LegitScriptEvidence(BaseModel):
    status: LegitScriptStatus = "unchecked"
    seal_ids: list[str] = Field(default_factory=list)
    verify_url: str | None = None
    snippets: list[str] = Field(default_factory=list)
    checked_at: datetime | None = None


class Lead(BaseModel):
    domain: str
    website: str | None = None
    company_name: str | None = None
    source: LeadSource = "manual"
    discovered_via: str | None = None
    source_detail: dict[str, Any] = Field(default_factory=dict)

    vertical: str | None = None
    description: str | None = None

    legitscript: LegitScriptEvidence = Field(default_factory=LegitScriptEvidence)

    people: list[Person] = Field(default_factory=list)
    owner_name: str | None = None
    owner_title: str | None = None
    owner_email: str | None = None
    owner_email_status: str | None = None  # verified | provider | on_site | guessed
    owner_phone: str | None = None
    owner_linkedin: str | None = None
    email_pattern: str | None = None  # e.g. {first}.{last}

    emails: list[str] = Field(default_factory=list)
    phones: list[str] = Field(default_factory=list)
    primary_email: str | None = None
    primary_phone: str | None = None

    address: str | None = None
    city: str | None = None
    state: str | None = None
    country: str | None = None

    socials: dict[str, str] = Field(default_factory=dict)
    tech_stack: list[str] = Field(default_factory=list)
    competitor_platform: str | None = None

    domain_created: str | None = None
    registrar: str | None = None
    has_mx: bool | None = None

    score: int = 0
    score_reasons: list[str] = Field(default_factory=list)
    status: LeadStatus = "new"
    booking_link: str | None = None
    notes: str | None = None

    pages_crawled: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    first_seen_at: datetime = Field(default_factory=utcnow)
    last_enriched_at: datetime | None = None

    # ------------------------------------------------------------------ helpers
    def best_person(self) -> Person | None:
        if not self.people:
            return None
        ranked = sorted(self.people, key=lambda p: (p.is_decision_maker, p.confidence), reverse=True)
        return ranked[0]

    def to_portal_row(self) -> dict[str, Any]:
        """Shape used by the Lovable Sales Portal `leads` table."""
        return {
            "domain": self.domain,
            "website": self.website,
            "company_name": self.company_name,
            "source": self.source,
            "discovered_via": self.discovered_via,
            "source_detail": self.source_detail,
            "vertical": self.vertical,
            "description": self.description,
            "legitscript_status": self.legitscript.status,
            "legitscript_evidence": self.legitscript.model_dump(mode="json"),
            "owner_name": self.owner_name,
            "owner_title": self.owner_title,
            "owner_email": self.owner_email,
            "owner_email_status": self.owner_email_status,
            "owner_phone": self.owner_phone,
            "owner_linkedin": self.owner_linkedin,
            "email_pattern": self.email_pattern,
            "people": [p.model_dump(mode="json") for p in self.people],
            "emails": self.emails,
            "phones": self.phones,
            "primary_email": self.primary_email,
            "primary_phone": self.primary_phone,
            "address": self.address,
            "city": self.city,
            "state": self.state,
            "country": self.country,
            "socials": self.socials,
            "tech_stack": self.tech_stack,
            "competitor_platform": self.competitor_platform,
            "domain_created": self.domain_created,
            "has_mx": self.has_mx,
            "score": self.score,
            "score_reasons": self.score_reasons,
            "booking_link": self.booking_link,
            "last_enriched_at": self.last_enriched_at.isoformat() if self.last_enriched_at else None,
        }
