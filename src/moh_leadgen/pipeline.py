"""The shared enrichment pipeline: Candidate/Lead in, fully populated Lead out.

Every tool runs the same chain so leads from different sources are comparable:
crawl -> extract contacts/people -> LegitScript evidence -> platform fingerprint
-> DNS/RDAP -> optional Hunter + Claude -> score -> booking link.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from .config import settings
from .domains import normalize_domain
from .enrich import dns_info, extract, llm, people_data, rdap
from .enrich.legitscript import detect_legitscript, infer_vertical
from .enrich.site_crawler import crawl_site
from .enrich.tech import fingerprint
from .http import PoliteClient
from .models import Lead, Person
from .scoring import score_lead

log = logging.getLogger(__name__)


def _merge_people(*groups: list[Person]) -> list[Person]:
    merged: dict[str, Person] = {}
    for group in groups:
        for p in group:
            key = p.name.lower().strip()
            cur = merged.get(key)
            if cur is None:
                merged[key] = p.model_copy()
                continue
            cur.title = cur.title or p.title
            cur.email = cur.email or p.email
            cur.email_status = cur.email_status or p.email_status
            cur.phone = cur.phone or p.phone
            cur.phone_type = cur.phone_type or p.phone_type
            cur.linkedin = cur.linkedin or p.linkedin
            cur.confidence = max(cur.confidence, p.confidence)
            cur.is_decision_maker = cur.is_decision_maker or p.is_decision_maker
    return sorted(merged.values(), key=lambda p: (p.is_decision_maker, p.confidence), reverse=True)


def booking_link(lead: Lead) -> str | None:
    """Personalised booking URL (UTM-tagged so a booked meeting can be traced back to the lead)."""
    base = settings.booking_url
    if not base:
        return None
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}utm_source=leadgen&utm_medium={lead.source}&utm_campaign={lead.vertical or 'general'}&utm_content={lead.domain}"


def enrich_lead(
    lead: Lead, client: PoliteClient, *, use_llm: bool = False, use_providers: bool = True
) -> Lead:
    lead.errors = []
    snap = crawl_site(lead.domain, client)
    lead.errors.extend(snap.errors)
    lead.website = snap.final_url
    lead.pages_crawled = [p.url for p in snap.pages]
    final_domain = normalize_domain(snap.final_url)
    if final_domain and final_domain != lead.domain:
        lead.source_detail["redirected_to"] = final_domain

    if snap.pages:
        objs = extract.extract_jsonld(snap)
        org = extract.organization_from_jsonld(objs)
        lead.company_name = extract.extract_company_name(snap, org, lead.domain)
        lead.description = extract.extract_description(snap, org)

        lead.emails = extract.extract_emails(snap, lead.domain)
        if org.get("email") and org["email"].lower() not in lead.emails:
            lead.emails.insert(0, org["email"].lower())
        lead.phones = extract.extract_phones(snap)
        if org.get("telephone"):
            lead.phones = extract.extract_phones(snap)  # already includes JSON-LD text; keep order
        lead.primary_email = extract.pick_primary_email(lead.emails)
        lead.primary_phone = lead.phones[0] if lead.phones else None

        if org.get("street") or org.get("city"):
            lead.address = ", ".join(str(org[k]) for k in ("street", "city", "state", "postal") if org.get(k))
            lead.city, lead.state = org.get("city"), org.get("state")
            lead.country = org.get("country")
        else:
            lead.address = extract.extract_address_text(snap)
        lead.socials = extract.extract_socials(snap)
        for url in org.get("sameAs", []) or []:
            if not isinstance(url, str):
                continue
            for host, key in extract.SOCIAL_HOSTS.items():
                if host in url and key not in lead.socials:
                    lead.socials[key] = url

        lead.legitscript = detect_legitscript(snap)
        lead.vertical, vertical_scores = infer_vertical(snap.all_text())
        lead.source_detail["vertical_signals"] = vertical_scores

        heur_people = extract.extract_people_heuristic(snap)
        ld_people = extract.people_from_jsonld(objs, org)
        groups = [ld_people, heur_people]

        if use_llm and llm.available():
            result = llm.extract(snap)
            if result:
                ai_people, meta = result
                groups.append(ai_people)
                lead.source_detail["ai"] = meta
                lead.company_name = meta.get("company_name") or lead.company_name
                lead.description = meta.get("summary") or lead.description
                lead.vertical = meta.get("vertical") or lead.vertical

        lead.people = _merge_people(*groups)

        # 1. Personal mailboxes published on the site -> attach to the matching person.
        personal = extract.personal_mailboxes(lead.emails)
        for p in lead.people:
            first, last = (p.name.replace("Dr.", "").split() + ["", ""])[:2]
            for e in personal:
                local = e.split("@")[0].lower()
                if first and (local.startswith(first.lower()) or (last and last.lower() in local)):
                    p.email, p.email_status = p.email or e, p.email_status or "on_site"
        lead.email_pattern = extract.infer_email_pattern(lead.emails, lead.people)

        # 2. Licensed people-data providers (Apollo / Lusha / Hunter) for the top decision-makers.
        if use_providers:
            lead.people, providers = people_data.enrich_decision_makers(lead.people, lead.domain)
            if providers:
                lead.source_detail["contact_providers"] = providers

        best = lead.best_person()
        if best and (best.is_decision_maker or best.confidence >= 0.6):
            lead.owner_name, lead.owner_title = best.name, best.title
            lead.owner_linkedin, lead.owner_phone = best.linkedin, best.phone
            if best.email:
                lead.owner_email, lead.owner_email_status = best.email, best.email_status or "provider"
            elif lead.email_pattern:
                guess = extract.apply_email_pattern(lead.email_pattern, best.name, lead.domain)
                if guess:
                    lead.owner_email, lead.owner_email_status = guess, "guessed"
                    best.email, best.email_status = guess, "guessed"
        if lead.owner_email_status == "guessed":
            lead.source_detail["owner_email_guessed"] = True

    dns_facts = dns_info.lookup(lead.domain)
    lead.has_mx = dns_facts.get("has_mx")
    lead.source_detail["dns"] = {
        k: dns_facts.get(k) for k in ("mx", "cname_chain", "ptr", "mail_provider") if dns_facts.get(k)
    }

    html = snap.all_html() if snap.pages else ""
    lead.tech_stack, lead.competitor_platform = fingerprint(html, dns_facts.get("cname_chain"))
    if lead.source == "competitor_platform" and not lead.competitor_platform:
        lead.competitor_platform = lead.source_detail.get("platform")

    reg = rdap.lookup(lead.domain)
    lead.domain_created = reg.get("created")
    lead.registrar = reg.get("registrar")
    if reg.get("registrant"):
        lead.source_detail["registrant"] = reg["registrant"]
        r_email = reg["registrant"].get("email")
        if r_email and r_email.lower() not in lead.emails:
            lead.emails.append(r_email.lower())

    lead.last_enriched_at = datetime.now(UTC)
    score_lead(lead)
    lead.booking_link = booking_link(lead)
    return lead
