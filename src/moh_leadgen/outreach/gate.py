"""Who may be emailed. Every rejection returns a short reason so `outreach plan` can count them.

Rules (docs/outbound-system-plan.md section 5.5 and 6.1). Reasons are stable snake_case strings:
  lead-level   already_enrolled; suppressed_local; platform_owned_domain (a competitor platform's own
               domain); not_enriched (no pages crawled); low_score (< settings.outreach_min_score);
               no_mx (lead.has_mx is False); portal_status_<status> (only new / reviewed pass);
               suppressed_domain; fda_list_not_reviewed (a lead in the 'fda_compliance' segment, i.e.
               from the FDA warning-letter seed list, until a person sets source_detail['fda_review']
               to 'approved' with `leadgen outreach review-approve`; checked after the other
               lead-level rules, so its count is the FDA-list leads waiting only on review).
  contact      every address is first cleaned (enrich.email_hygiene.clean_email: mailto:, %20,
               u003e / &gt; escapes, query strings) and classified person / role / junk:
               suppressed_email, suppressed_domain;
               junk_email           unparseable, placeholder (jane.smith@), hash-like, system box
                                    (noreply@, privacy@, registrar@) or a platform/registrar domain;
               shared_contact_email the same address is attached to more than one registrable
                                    company domain across the candidate leads (template copies);
               freemail_unverified  gmail/yahoo/outlook/... allowed as a person only when the local
                                    part matches the contact's name or the lead has no corporate
                                    email at all;
               generic_mailbox      a role box (info@, hello@, contact@, support@, admin@, office@)
                                    without allow_generic; with it, the separate 2-step 'generic'
                                    campaign;
               role_mailbox_not_allowed  any other role box (care@, rx@, health@, hola@ ...);
               email_unverified     a person address whose status is not verified/provider/on_site;
               no_mx                the address's domain provably has no MX (checked last, only for
                                    contacts that passed everything else; unknown = allowed).
  company      at most 2 named (person) contacts, or 1 role mailbox.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..config import settings
from ..domains import registrable
from ..enrich import email_hygiene
from ..enrich.email_hygiene import (
    classify_local_part,
    clean_email,
    is_cross_company_duplicate,
    is_freemail,
    matches_owner,
)
from ..enrich.extract import is_generic_mailbox
from ..models import Lead
from ..tools.competitor_platform import PLATFORM_OWNED
from .models import FDA_SEGMENT
from .personalize import first_name_from, last_name_from
from .sequences import assign_segment

MxCheck = Callable[[str], "bool | None"]
# The MX lookup used when a caller does not pass mx_check. Tests swap it for an offline fake.
DEFAULT_MX_CHECK: MxCheck | None = email_hygiene.has_mx
USE_DEFAULT_MX: Any = object()

SENDABLE_EMAIL_STATUSES = {"verified", "provider", "on_site"}
ALLOWED_GENERIC_LOCALPARTS = ("info", "hello", "contact", "support", "admin", "office")
SENDABLE_PORTAL_STATUSES = {"new", "reviewed"}
MAX_CONTACTS_PER_COMPANY = 2
FDA_REVIEW_APPROVED = "approved"


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
    kind: str = "person"  # person | role | junk (email_hygiene.classify_local_part)
    is_freemail: bool = False
    freemail_ok: bool = True  # a free-mail address we may treat as this company's contact


def _generic_rank(email: str) -> int | None:
    """Position in ALLOWED_GENERIC_LOCALPARTS (lower is better), None if not an allowed role box."""
    local = email.split("@", 1)[0].lower()
    return ALLOWED_GENERIC_LOCALPARTS.index(local) if local in ALLOWED_GENERIC_LOCALPARTS else None


def _has_corporate_email(lead: Lead) -> bool:
    """Any cleaned, non-junk address on a non-free-mail domain."""
    return any(
        not is_freemail(e) and classify_local_part(e, lead.owner_name, lead.domain) != "junk"
        for e in email_hygiene.lead_emails(lead)
    )


def person_name(name: str | None, lead: Lead) -> str | None:
    """`name` unless it is really the company (extractors sometimes file the brand as a person).

    'ClearPath Metabolic' on clearpathmetabolic.com is not an owner, so clearpath@ there must not
    pass as that person's mailbox; it is a brand box and is classified like one.
    """
    if not name:
        return None
    flat = re.sub(r"[^a-z0-9]", "", name.lower())
    if not flat:
        return None
    company = re.sub(r"[^a-z0-9]", "", (lead.company_name or "").lower())
    brand = email_hygiene._brand_label(lead.domain)
    if flat != company and not (len(brand) >= 4 and flat == brand):
        return name
    # The name is the company's. A solo practice named after its owner ("Barbara Baron" on
    # barbarabaron.com) is still a person; a brand ("ClearPath Metabolic", "Nutree") is not.
    tokens = [t for t in re.findall(r"[a-z]+", name.lower()) if t not in _NAME_TITLES]
    if len(tokens) == 2 and not any(t in _BUSINESS_WORDS for t in tokens):
        return name
    return None


_NAME_TITLES = {"dr", "md", "do", "np", "pa", "rn", "phd", "dnp", "aprn", "fnp"}
_BUSINESS_WORDS = {
    "metabolic",
    "health",
    "healthcare",
    "wellness",
    "clinic",
    "clinics",
    "medical",
    "medicine",
    "med",
    "spa",
    "medspa",
    "care",
    "rx",
    "pharmacy",
    "pharma",
    "telehealth",
    "telemed",
    "group",
    "labs",
    "lab",
    "center",
    "centre",
    "centers",
    "solutions",
    "partners",
    "inc",
    "llc",
    "co",
    "company",
    "nutrition",
    "weight",
    "loss",
    "aesthetics",
    "aesthetic",
    "clique",
    "peptides",
    "peptide",
    "hormone",
    "hormones",
    "therapy",
    "therapeutics",
    "direct",
    "online",
    "virtual",
    "institute",
    "associates",
    "services",
    "network",
    "studio",
    "collective",
    "project",
    "app",
    "hw",
    "mens",
    "womens",
    "men",
    "women",
    "family",
    "primary",
    "urgent",
    "total",
    "modern",
    "elite",
    "premier",
    "advanced",
    "integrative",
    "functional",
    "longevity",
    "vitality",
    "glow",
    "slim",
    "lean",
    "fit",
    "trt",
    "glp",
    "ketamine",
    "iv",
    "hydration",
    "beauty",
    "skin",
    "derm",
    "dermatology",
    "doctor",
    "doctors",
    "physicians",
    "md's",
    "telegra",
    "concierge",
    "renew",
    "restore",
    "revive",
    "thrive",
    "balance",
    "optimal",
    "optimum",
    "wellbeing",
    "well",
    "route",
    "strategies",
}


def candidate_contacts(lead: Lead) -> list[Contact]:
    """Every email we know for the lead, best first: owner, named people, then site emails.

    Addresses are cleaned on the fly (stored leads may predate email_hygiene); one that cannot be
    cleaned is kept as kind 'junk' so the gate reports junk_email rather than no_email.
    """
    seen: set[str] = set()
    out: list[Contact] = []
    corporate = _has_corporate_email(lead)
    owner_name = person_name(lead.owner_name, lead)

    def add(contact: Contact, name: str | None) -> None:
        raw = contact.email.strip().lower()
        cleaned = clean_email(raw)
        key = cleaned or raw
        if not key or key in seen:
            return
        seen.add(key)
        contact.email = key
        name = person_name(name, lead)
        if cleaned is None:
            contact.kind = "junk"
        else:
            owner_match = matches_owner(key, name) or matches_owner(key, owner_name)
            contact.kind = classify_local_part(
                key, name if matches_owner(key, name) else owner_name, lead.domain
            )
            if contact.kind == "person" and not owner_match and is_generic_mailbox(key):
                contact.kind = "role"  # the extractor's wider role pattern (patientsupport@, pr@ ...)
            contact.is_freemail = is_freemail(key)
            if contact.is_freemail:
                contact.freemail_ok = contact.kind != "junk" and (owner_match or not corporate)
        contact.is_generic = contact.kind == "role"
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
            ),
            lead.owner_name,
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
                ),
                person.name,
            )
    for email in [lead.primary_email, *lead.emails]:
        if email:
            add(Contact(email=email, email_status="on_site"), None)
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
    if assign_segment(lead) == FDA_SEGMENT and lead.source_detail.get("fda_review") != FDA_REVIEW_APPROVED:
        return "fda_list_not_reviewed"
    return None


def contact_rejection(
    contact: Contact,
    suppressions: Suppressions | None = None,
    *,
    allow_generic: bool = False,
    company_domain: str | None = None,
    shared: dict[str, set[str]] | None = None,
) -> str | None:
    """Why this one address may not be emailed, or None. (The MX check is separate: `mx_rejection`.)

    `shared` is email_hygiene.email_owners() over all candidate leads; with `company_domain` it
    rejects an address that also belongs to another company (shared_contact_email).
    """
    if suppressions and suppressions.email_blocked(contact.email):
        return "suppressed_email"
    if suppressions and suppressions.domain_blocked(contact.email.split("@", 1)[-1]):
        return "suppressed_domain"
    if contact.kind == "junk":
        return "junk_email"
    if shared and company_domain and is_cross_company_duplicate(contact.email, company_domain, shared):
        return "shared_contact_email"
    if contact.is_freemail and not contact.freemail_ok:
        return "freemail_unverified"
    if contact.is_generic:
        if _generic_rank(contact.email) is None:
            return "role_mailbox_not_allowed"
        return None if allow_generic else "generic_mailbox"
    if (contact.email_status or "") not in SENDABLE_EMAIL_STATUSES:
        return "email_unverified"
    return None


def mx_rejection(contact: Contact, lead: Lead, mx_check: MxCheck | None) -> str | None:
    """'no_mx' when the address's domain provably has no MX; None when it has one or is unknown.

    Skipped for free-mail domains and for the lead's own domain when enrichment already saw an MX.
    """
    if mx_check is None or contact.is_freemail:
        return None
    domain = contact.email.rsplit("@", 1)[-1]
    if lead.has_mx is True and (registrable(domain) or domain) == (registrable(lead.domain) or lead.domain):
        return None
    return "no_mx" if mx_check(domain) is False else None


def _resolve_mx_check(mx_check: Any) -> MxCheck | None:
    return DEFAULT_MX_CHECK if mx_check is USE_DEFAULT_MX else mx_check


def _find_contact(lead: Lead, email: str) -> Contact:
    key = clean_email(email) or email.strip().lower()
    for contact in candidate_contacts(lead):
        if contact.email == key:
            return contact
    probe = lead.model_copy(update={"emails": [*lead.emails, key]})
    return next(c for c in candidate_contacts(probe) if c.email == key)


def is_sendable(
    lead: Lead,
    contact_email: str | None,
    portal_status: str | None = None,
    suppressions: Suppressions | None = None,
    *,
    allow_generic: bool = False,
    min_score: int | None = None,
    shared: dict[str, set[str]] | None = None,
    mx_check: Any = USE_DEFAULT_MX,
) -> tuple[bool, str]:
    """(True, 'ok' | 'ok_generic') or (False, reason) for one address at one company."""
    reason = lead_rejection(lead, portal_status, suppressions, min_score=min_score)
    if reason:
        return False, reason
    if not contact_email:
        return False, "no_email"
    contact = _find_contact(lead, contact_email)
    reason = contact_rejection(
        contact, suppressions, allow_generic=allow_generic, company_domain=lead.domain, shared=shared
    ) or mx_rejection(contact, lead, _resolve_mx_check(mx_check))
    if reason:
        return False, reason
    return True, "ok_generic" if contact.is_generic else "ok"


REASON_PRIORITY = [
    "suppressed_email",
    "suppressed_domain",
    "shared_contact_email",
    "no_mx",
    "email_unverified",
    "freemail_unverified",
    "generic_mailbox",
    "role_mailbox_not_allowed",
    "junk_email",
]


def sendable_contacts(
    lead: Lead,
    portal_status: str | None = None,
    suppressions: Suppressions | None = None,
    *,
    allow_generic: bool = False,
    min_score: int | None = None,
    shared: dict[str, set[str]] | None = None,
    mx_check: Any = USE_DEFAULT_MX,
) -> tuple[list[Contact], str | None, str]:
    """(contacts, rejection_reason, tier) for one company.

    tier 'named': up to 2 addresses classified 'person' that pass every rule. tier 'generic': one
    allowed role mailbox, used only when no named contact passes and allow_generic is on. 'junk'
    addresses never pass. On rejection, contacts is empty and the reason is the most useful one we
    saw (REASON_PRIORITY). `shared` is email_hygiene.email_owners() over all candidate leads;
    `mx_check` defaults to DEFAULT_MX_CHECK (dnspython, cached) and None disables it.
    """
    reason = lead_rejection(lead, portal_status, suppressions, min_score=min_score)
    if reason:
        return [], reason, "none"
    candidates = candidate_contacts(lead)
    if not candidates:
        return [], "no_email", "none"
    check = _resolve_mx_check(mx_check)
    named, generic, reasons = [], [], []
    for contact in candidates:
        why = contact_rejection(
            contact, suppressions, allow_generic=allow_generic, company_domain=lead.domain, shared=shared
        )
        if why:
            reasons.append(why)
        elif contact.is_generic:
            generic.append(contact)
        else:
            named.append(contact)

    def mx_ok(pool: list[Contact], limit: int) -> list[Contact]:
        kept: list[Contact] = []
        for contact in pool:
            if len(kept) >= limit:
                break
            why = mx_rejection(contact, lead, check)
            if why:
                reasons.append(why)
            else:
                kept.append(contact)
        return kept

    named = mx_ok(named, MAX_CONTACTS_PER_COMPANY)
    if named:
        return named, None, "named"
    generic.sort(key=lambda c: _generic_rank(c.email) or 0)
    generic = mx_ok(generic, 1)
    if generic:
        return generic, None, "generic"
    reason = next((r for r in REASON_PRIORITY if r in reasons), reasons[0])
    return [], reason, "none"
