"""Who may be emailed. Every rejection returns a short reason so `outreach plan` can count them.

Rules (docs/outbound-system-plan.md section 5.5 and 6.1):
  lead-level   not already enrolled; not a competitor platform's own domain; enriched with pages
               crawled; score >= settings.outreach_min_score; has_mx is not False; portal status
               new or reviewed; domain not suppressed.
  contact      has an email; email not suppressed; a personal email must be verified, provider or
               on_site; a role mailbox (info@, hello@, contact@, support@, admin@, office@) only
               with allow_generic=True, and then it goes to the separate 2-step 'generic' campaign.
  company      at most 2 named contacts, or 1 role mailbox.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..config import settings
from ..domains import registrable
from ..enrich.extract import is_generic_mailbox
from ..models import Lead
from ..tools.competitor_platform import PLATFORM_OWNED
from .personalize import first_name_from, last_name_from

SENDABLE_EMAIL_STATUSES = {"verified", "provider", "on_site"}
ALLOWED_GENERIC_LOCALPARTS = ("info", "hello", "contact", "support", "admin", "office")
SENDABLE_PORTAL_STATUSES = {"new", "reviewed"}
MAX_CONTACTS_PER_COMPANY = 2


@dataclass
class Suppressions:
    """Emails and domains we must not email (portal `suppressions` + local store flags)."""

    emails: set[str] = field(default_factory=set)
    domains: set[str] = field(default_factory=set)

    def add(self, email: str | None = None, domain: str | None = None) -> None:
        if email:
            self.emails.add(email.strip().lower())
        elif domain:
            self.domains.add(domain.strip().lower())

    def email_blocked(self, email: str) -> bool:
        return email.strip().lower() in self.emails

    def domain_blocked(self, domain: str) -> bool:
        domain = domain.strip().lower()
        return domain in self.domains or (registrable(domain) or domain) in self.domains

    @classmethod
    def from_rows(cls, rows: list[dict[str, Any]], now: datetime | None = None) -> Suppressions:
        """Build from portal rows {email, domain, expires_at}; expired rows are ignored."""
        now = now or datetime.now(UTC)
        out = cls()
        for row in rows:
            expires = row.get("expires_at")
            if expires and datetime.fromisoformat(str(expires)) <= now:
                continue
            out.add(row.get("email"), row.get("domain"))
        return out


@dataclass
class Contact:
    email: str
    first_name: str | None = None
    last_name: str | None = None
    title: str | None = None
    email_status: str | None = None
    is_generic: bool = False
    is_decision_maker: bool = False
    linkedin: str | None = None


def _generic_rank(email: str) -> int | None:
    """Position in ALLOWED_GENERIC_LOCALPARTS (lower is better), None if not an allowed role box."""
    local = email.split("@", 1)[0].lower()
    return ALLOWED_GENERIC_LOCALPARTS.index(local) if local in ALLOWED_GENERIC_LOCALPARTS else None


def candidate_contacts(lead: Lead) -> list[Contact]:
    """Every email we know for the lead, best first: owner, named people, then site emails."""
    seen: set[str] = set()
    out: list[Contact] = []

    def add(contact: Contact) -> None:
        key = contact.email.strip().lower()
        if key and key not in seen:
            seen.add(key)
            contact.email = key
            contact.is_generic = is_generic_mailbox(key)
            out.append(contact)

    if lead.owner_email:
        add(
            Contact(
                email=lead.owner_email,
                first_name=first_name_from(lead.owner_name),
                last_name=last_name_from(lead.owner_name),
                title=lead.owner_title,
                email_status=lead.owner_email_status,
                is_decision_maker=True,
                linkedin=lead.owner_linkedin,
            )
        )
    for person in sorted(lead.people, key=lambda p: (p.is_decision_maker, p.confidence), reverse=True):
        if person.email:
            add(
                Contact(
                    email=person.email,
                    first_name=first_name_from(person.name),
                    last_name=last_name_from(person.name),
                    title=person.title,
                    email_status=person.email_status,
                    is_decision_maker=person.is_decision_maker,
                    linkedin=person.linkedin,
                )
            )
    for email in [lead.primary_email, *lead.emails]:
        if email:
            add(Contact(email=email, email_status="on_site"))
    return out


def is_platform_owned(domain: str) -> bool:
    domain = domain.lower()
    return (registrable(domain) or domain) in PLATFORM_OWNED or any(
        domain.endswith("." + owned) for owned in PLATFORM_OWNED
    )


def lead_rejection(
    lead: Lead,
    portal_status: str | None = None,
    suppressions: Suppressions | None = None,
    *,
    min_score: int | None = None,
) -> str | None:
    """Why no contact at this company may be emailed, or None if the company passes."""
    threshold = settings.outreach_min_score if min_score is None else min_score
    status = portal_status or lead.status
    if lead.outreach.enrolled_at or lead.outreach.enrolled_emails:
        return "already_enrolled"
    if lead.outreach.suppressed_at:
        return "suppressed_local"
    if is_platform_owned(lead.domain):
        return "platform_owned_domain"
    if not lead.last_enriched_at or not lead.pages_crawled:
        return "not_enriched"
    if lead.score < threshold:
        return "low_score"
    if lead.has_mx is False:
        return "no_mx"
    if status not in SENDABLE_PORTAL_STATUSES:
        return f"portal_status_{status}"
    if suppressions and suppressions.domain_blocked(lead.domain):
        return "suppressed_domain"
    return None


def contact_rejection(
    contact: Contact, suppressions: Suppressions | None = None, *, allow_generic: bool = False
) -> str | None:
    """Why this one address may not be emailed, or None."""
    if suppressions and suppressions.email_blocked(contact.email):
        return "suppressed_email"
    if suppressions and suppressions.domain_blocked(contact.email.split("@", 1)[-1]):
        return "suppressed_domain"
    if contact.is_generic:
        if _generic_rank(contact.email) is None:
            return "role_mailbox_not_allowed"
        return None if allow_generic else "generic_mailbox"
    if (contact.email_status or "") not in SENDABLE_EMAIL_STATUSES:
        return "email_unverified"
    return None


def _find_contact(lead: Lead, email: str) -> Contact:
    key = email.strip().lower()
    for contact in candidate_contacts(lead):
        if contact.email == key:
            return contact
    return Contact(email=key, is_generic=is_generic_mailbox(key))


def is_sendable(
    lead: Lead,
    contact_email: str | None,
    portal_status: str | None = None,
    suppressions: Suppressions | None = None,
    *,
    allow_generic: bool = False,
    min_score: int | None = None,
) -> tuple[bool, str]:
    """(True, 'ok' | 'ok_generic') or (False, reason) for one address at one company."""
    reason = lead_rejection(lead, portal_status, suppressions, min_score=min_score)
    if reason:
        return False, reason
    if not contact_email:
        return False, "no_email"
    contact = _find_contact(lead, contact_email)
    reason = contact_rejection(contact, suppressions, allow_generic=allow_generic)
    if reason:
        return False, reason
    return True, "ok_generic" if contact.is_generic else "ok"


def sendable_contacts(
    lead: Lead,
    portal_status: str | None = None,
    suppressions: Suppressions | None = None,
    *,
    allow_generic: bool = False,
    min_score: int | None = None,
) -> tuple[list[Contact], str | None, str]:
    """(contacts, rejection_reason, tier) for one company.

    tier 'named': up to 2 verified personal addresses. tier 'generic': one allowed role mailbox,
    used only when no named contact passes and allow_generic is on. On rejection, contacts is
    empty and the reason is the most useful one we saw (named-contact reasons first).
    """
    reason = lead_rejection(lead, portal_status, suppressions, min_score=min_score)
    if reason:
        return [], reason, "none"
    candidates = candidate_contacts(lead)
    if not candidates:
        return [], "no_email", "none"
    named, generic, reasons = [], [], []
    for contact in candidates:
        why = contact_rejection(contact, suppressions, allow_generic=allow_generic)
        if why:
            reasons.append(why)
        elif contact.is_generic:
            generic.append(contact)
        else:
            named.append(contact)
    if named:
        return named[:MAX_CONTACTS_PER_COMPANY], None, "named"
    if generic:
        generic.sort(key=lambda c: _generic_rank(c.email) or 0)
        return generic[:1], None, "generic"
    priority = ["suppressed_email", "suppressed_domain", "email_unverified", "generic_mailbox"]
    reason = next((r for r in priority if r in reasons), reasons[0])
    return [], reason, "none"
