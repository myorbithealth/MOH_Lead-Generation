"""Rebuild the local store from the Sales Portal: the reverse of Lead.to_portal_row.

A fresh GitHub Actions runner starts with an empty leads.db (the store lives only in an Actions
cache and is never committed, since it holds scraped personal data). The portal already holds
every lead the pipeline pushed, plus what reps and outreach added, so `leadgen pull-portal`
reads public.leads and public.contacts back into Lead rows.

What cannot come back: pages_crawled (no portal column), retry state and raw crawl errors. A row
that shows a crawl happened (score > 0, or emails present) gets pages_crawled = [its website]
unless its score_reasons say the site was unreachable, so the daily `score` step and the
enrolment gate treat it as crawled instead of docking 25 points for "site unreachable".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, get_args

from pydantic import ValidationError

from ..models import Lead, LeadSource, LeadStatus, LegitScriptEvidence, LegitScriptStatus, Person
from ..outreach.models import Disposition, OutreachState
from ..store import _STATUS_RANK, LeadStore, _is_empty, _merge_outreach, _merge_source_detail

log = logging.getLogger(__name__)

_SOURCES = set(get_args(LeadSource))
_STATUSES = set(get_args(LeadStatus))
_LS_STATUSES = set(get_args(LegitScriptStatus))

# jsonb columns already carried by a Lead field; left out of source_detail['portal'].
_JSONB_DUPLICATES = {
    "source_detail",
    "legitscript_evidence",
    "people",
    "emails",
    "phones",
    "socials",
    "tech_stack",
    "score_reasons",
    "pages_crawled",
}


def _ts(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _str_list(value: Any) -> list[str]:
    return [str(v) for v in value if v] if isinstance(value, list) else []


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _legitscript(row: dict[str, Any]) -> LegitScriptEvidence:
    evidence = row.get("legitscript_evidence") if isinstance(row.get("legitscript_evidence"), dict) else {}
    status = row.get("legitscript_status") or evidence.get("status") or "unchecked"
    data = {
        "status": status if status in _LS_STATUSES else "unchecked",
        "seal_ids": _str_list(evidence.get("seal_ids")),
        "verify_url": evidence.get("verify_url"),
        "snippets": _str_list(evidence.get("snippets")),
        "checked_at": evidence.get("checked_at"),
    }
    try:
        return LegitScriptEvidence.model_validate(data)
    except ValidationError:
        return LegitScriptEvidence(status=data["status"])


def _people(row: dict[str, Any], contacts: list[dict]) -> list[Person]:
    """The people jsonb, then portal contacts rows (filling gaps on a matching email)."""
    people: list[Person] = []
    for item in row.get("people") or []:
        if not isinstance(item, dict):
            continue
        try:
            people.append(Person.model_validate(item))
        except ValidationError:
            log.debug("skipping malformed person on %s: %r", row.get("domain"), item)
    by_email = {p.email.lower(): p for p in people if p.email}
    for c in contacts:
        email = (c.get("email") or "").strip().lower()
        if not email:
            continue
        name = " ".join(part for part in (c.get("first_name"), c.get("last_name")) if part).strip()
        found = by_email.get(email)
        if found is None:
            person = Person(
                name=name or email.split("@", 1)[0],
                title=c.get("title"),
                email=email,
                email_status=c.get("email_status"),
                phone=c.get("phone"),
                linkedin=c.get("linkedin"),
                is_decision_maker=bool(c.get("is_decision_maker")),
            )
            people.append(person)
            by_email[email] = person
            continue
        for attr, key in (
            ("email_status", "email_status"),
            ("title", "title"),
            ("phone", "phone"),
            ("linkedin", "linkedin"),
        ):
            if not getattr(found, attr) and c.get(key):
                setattr(found, attr, c[key])
        if c.get("is_decision_maker"):
            found.is_decision_maker = True
    return people


def _outreach(row: dict[str, Any], contacts: list[dict]) -> OutreachState:
    disposition = row.get("disposition")
    state = OutreachState(
        segment=row.get("segment"),
        smartlead_campaign_id=_int_or_none(row.get("smartlead_campaign_id")),
        enrolled_at=_ts(row.get("enrolled_at")),
        last_outreach_at=_ts(row.get("last_outreach_at")),
        disposition=disposition if disposition in Disposition._value2member_map_ else None,
        suppressed_at=_ts(row.get("suppressed_at")),
        suppression_reason=row.get("suppression_reason"),
    )
    # Contacts the enrolment wrote carry their Smartlead lead id: those emails were enrolled.
    for c in contacts:
        email = (c.get("email") or "").strip().lower()
        lead_id = _int_or_none(c.get("smartlead_lead_id"))
        if email and lead_id is not None:
            state.smartlead_lead_ids[email] = lead_id
            if email not in state.enrolled_emails:
                state.enrolled_emails.append(email)
    state.enrolled_emails.sort()
    return state


def lead_from_portal_row(row: dict[str, Any], contacts: list[dict] | None = None) -> Lead:
    """A Lead rebuilt from one public.leads row and its public.contacts rows."""
    contacts = contacts or []
    domain = row["domain"]
    detail = dict(row.get("source_detail") or {}) if isinstance(row.get("source_detail"), dict) else {}
    detail["portal"] = {k: v for k, v in row.items() if k not in _JSONB_DUPLICATES}
    source = row.get("source")
    status = row.get("status")
    score = _int_or_none(row.get("score")) or 0
    emails = _str_list(row.get("emails"))
    pages = _str_list(row.get("pages_crawled"))
    score_reasons = _str_list(row.get("score_reasons"))
    crawled = score > 0 or bool(pages) or bool(emails)
    website = row.get("website") or f"https://{domain}"
    if crawled and not pages and "site unreachable" not in score_reasons:
        pages = [website]
    lead = Lead(
        domain=domain,
        website=row.get("website"),
        company_name=row.get("company_name"),
        source=source if source in _SOURCES else "manual",
        discovered_via=row.get("discovered_via"),
        source_detail=detail,
        outreach=_outreach(row, contacts),
        vertical=row.get("vertical"),
        description=row.get("description"),
        legitscript=_legitscript(row),
        people=_people(row, contacts),
        owner_name=row.get("owner_name"),
        owner_title=row.get("owner_title"),
        owner_email=row.get("owner_email"),
        owner_email_status=row.get("owner_email_status"),
        owner_phone=row.get("owner_phone"),
        owner_linkedin=row.get("owner_linkedin"),
        email_pattern=row.get("email_pattern"),
        emails=emails,
        phones=_str_list(row.get("phones")),
        primary_email=row.get("primary_email"),
        primary_phone=row.get("primary_phone"),
        address=row.get("address"),
        city=row.get("city"),
        state=row.get("state"),
        country=row.get("country"),
        socials={str(k): str(v) for k, v in (row.get("socials") or {}).items() if v}
        if isinstance(row.get("socials"), dict)
        else {},
        tech_stack=_str_list(row.get("tech_stack")),
        competitor_platform=row.get("competitor_platform"),
        domain_created=row.get("domain_created"),
        has_mx=row.get("has_mx"),
        score=score,
        score_reasons=score_reasons,
        status=status if status in _STATUSES else "new",
        booking_link=row.get("booking_link"),
        notes=row.get("notes"),
        pages_crawled=pages,
    )
    created = _ts(row.get("created_at"))
    if created:
        lead.first_seen_at = created
    if crawled:
        lead.last_enriched_at = (
            _ts(row.get("last_enriched_at")) or _ts(row.get("updated_at")) or datetime.now(UTC)
        )
    return lead


def _merge_into_local(imported: Lead, local: Lead) -> Lead:
    """The portal row wins (it is newer); the local row fills gaps the portal cannot carry."""
    merged = imported.model_copy(deep=True)
    for name in Lead.model_fields:
        if name in ("domain", "source_detail", "outreach", "status"):
            continue
        if _is_empty(getattr(merged, name)) and not _is_empty(getattr(local, name)):
            setattr(merged, name, getattr(local, name))
    if local.pages_crawled and merged.pages_crawled == [merged.website or f"https://{merged.domain}"]:
        merged.pages_crawled = list(local.pages_crawled)  # the real crawl beats the placeholder
    merged.outreach = _merge_outreach(imported.outreach, local.outreach)
    merged.status = max(imported.status, local.status, key=lambda s: _STATUS_RANK.get(s, 0))
    _merge_source_detail(merged, local)
    merged.first_seen_at = min(imported.first_seen_at, local.first_seen_at)
    return merged


@dataclass
class PullStats:
    fetched: int = 0
    inserted: int = 0
    updated: int = 0
    skipped_newer_locally: int = 0
    domains: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"fetched {self.fetched}, inserted {self.inserted}, updated {self.updated}, "
            f"skipped-newer-locally {self.skipped_newer_locally}"
        )


def pull_portal(
    store: LeadStore,
    sink: Any,
    *,
    since: datetime | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    batch_size: int = 500,
) -> PullStats:
    """Write portal leads into the store without marking them for the next portal sync.

    A local row whose updated_at is newer than the portal row's is left alone. Saved rows are
    marked synced straight away so `sync portal` does not push them back unchanged.
    """
    stats = PullStats()
    batch: list[dict] = []

    def _flush() -> None:
        if not batch:
            return
        contacts = sink.fetch_contacts_for([r["domain"] for r in batch])
        saved: list[str] = []
        for row in batch:
            domain = row["domain"]
            local_ts = _ts(store.updated_at(domain))
            local = store.get(domain) if local_ts else None
            if local is not None:
                portal_ts = _ts(row.get("updated_at"))
                if portal_ts is None or local_ts > portal_ts:
                    stats.skipped_newer_locally += 1
                    continue
            lead = lead_from_portal_row(row, contacts.get(domain, []))
            if local is not None:
                lead = _merge_into_local(lead, local)
                stats.updated += 1
            else:
                stats.inserted += 1
            stats.domains.append(domain)
            if not dry_run:
                store.save(lead)
                saved.append(domain)
        if saved:
            store.mark_synced(saved)
        batch.clear()

    for row in sink.fetch_leads(since=since, page_size=batch_size):
        if not row.get("domain"):
            continue
        stats.fetched += 1
        batch.append(row)
        if len(batch) >= batch_size:
            _flush()
        if limit and stats.fetched >= limit:
            break
    _flush()
    return stats
