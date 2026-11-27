"""Bring Meta Lead Ads exports and inbound form submissions into the local store as leads.

Each row becomes (or enriches) a Lead keyed by the company's domain, so these leads flow through
the same enrich -> score -> sync path as discovered ones. Attribution (utm_*, fbclid, leadgen_id)
is kept in source_detail["ingest"] so the portal can report meetings by first touch.

Accepts CSV or JSON (a list of objects). Column names are matched loosely (case, spaces, dashes).
"""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path
from typing import Literal

from ..domains import is_junk, normalize_domain
from ..models import Candidate, Person, utcnow

log = logging.getLogger(__name__)

IngestSource = Literal["meta_ads", "inbound"]

# Personal mailbox providers: their domain says nothing about the company.
FREEMAIL = {
    "gmail.com",
    "googlemail.com",
    "yahoo.com",
    "hotmail.com",
    "outlook.com",
    "live.com",
    "icloud.com",
    "me.com",
    "aol.com",
    "proton.me",
    "protonmail.com",
    "msn.com",
    "comcast.net",
    "ymail.com",
    "gmx.com",
}

# canonical name -> accepted column spellings (already normalised: lower case, underscores)
ALIASES: dict[str, tuple[str, ...]] = {
    "email": ("email", "email_address", "work_email", "business_email", "e_mail"),
    "phone": ("phone", "phone_number", "mobile", "mobile_phone", "work_phone", "telephone"),
    "company": ("company", "company_name", "business_name", "organization", "organisation", "clinic_name"),
    "website": ("website", "company_website", "url", "website_url", "domain", "site"),
    "full_name": ("full_name", "name", "contact_name"),
    "first_name": ("first_name", "firstname", "given_name"),
    "last_name": ("last_name", "lastname", "surname", "family_name"),
    "title": ("title", "job_title", "role", "position"),
    "leadgen_id": ("leadgen_id", "lead_id", "meta_lead_id", "id"),
    "fbclid": ("fbclid",),
    "platform": ("platform", "which_platform_do_you_run_on", "current_platform"),
    "city": ("city",),
    "state": ("state", "province", "region"),
    "created_time": ("created_time", "created_at", "submitted_at", "timestamp"),
}
UTM_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term")


def _norm_key(key: str) -> str:
    return "_".join(str(key).strip().lower().replace("-", " ").replace("?", " ").split())


def read_rows(path: Path | str) -> list[dict]:
    """CSV (header row) or JSON list of objects, chosen by file extension."""
    path = Path(path)
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):  # Graph API pages wrap rows in {"data": [...]}
            data = data.get("data") or []
        return [_flatten_field_data(row) for row in data if isinstance(row, dict)]
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def _flatten_field_data(row: dict) -> dict:
    """Meta's lead object keeps answers in field_data [{name, values}]; lift them to top-level keys."""
    out = {k: v for k, v in row.items() if k != "field_data"}
    for field in row.get("field_data") or []:
        values = field.get("values") or []
        if field.get("name") and values:
            out.setdefault(field["name"], values[0])
    return out


def map_row(row: dict) -> dict:
    """Canonical fields from a raw row; unknown columns are ignored, UTM columns kept as-is."""
    normalised = {_norm_key(k): (str(v).strip() if v is not None else "") for k, v in row.items()}
    out: dict[str, str] = {}
    for canonical, spellings in ALIASES.items():
        for spelling in spellings:
            if normalised.get(spelling):
                out[canonical] = normalised[spelling]
                break
    for key in UTM_KEYS:
        if normalised.get(key):
            out[key] = normalised[key]
    if "email" in out:
        out["email"] = out["email"].lower()
    if "full_name" not in out and (out.get("first_name") or out.get("last_name")):
        out["full_name"] = " ".join(p for p in (out.get("first_name"), out.get("last_name")) if p)
    return out


def domain_for(mapped: dict) -> str | None:
    """Company domain: the website if given, else the email domain unless it is a freemail."""
    if mapped.get("website"):
        domain = normalize_domain(mapped["website"])
        if domain:
            return domain
    if mapped.get("email") and "@" in mapped["email"]:
        domain = normalize_domain(mapped["email"])
        if domain and domain not in FREEMAIL:
            return domain
    return None


def _attribution(mapped: dict, source: str) -> dict:
    keep = ("leadgen_id", "fbclid", "platform", "created_time", *UTM_KEYS)
    detail = {k: mapped[k] for k in keep if mapped.get(k)}
    detail["source"] = source
    detail["ingested_at"] = utcnow().isoformat()
    return detail


def _merge_contact(lead, mapped: dict) -> None:
    """Add the form's person, email and phone to the lead without dropping crawled evidence."""
    email, phone, name = mapped.get("email"), mapped.get("phone"), mapped.get("full_name")
    if email and email not in lead.emails:
        lead.emails.append(email)
    if phone and phone not in lead.phones:
        lead.phones.append(phone)
    if not lead.primary_email and email:
        lead.primary_email = email
    if not lead.primary_phone and phone:
        lead.primary_phone = phone
    if name and not any(p.email and p.email == email for p in lead.people):
        # A person who filled in our own form gave us this address: "provided", not guessed.
        lead.people.append(
            Person(
                name=name,
                title=mapped.get("title"),
                email=email,
                email_status="provided",
                phone=phone,
                source_url=f"form:{mapped.get('leadgen_id') or 'inbound'}",
                confidence=0.9,
                is_decision_maker=True,
            )
        )
    if name and not lead.owner_name:
        lead.owner_name, lead.owner_title = name, mapped.get("title")
        lead.owner_email, lead.owner_email_status = email, "provided" if email else None
        lead.owner_phone = phone
    if mapped.get("company") and not lead.company_name:
        lead.company_name = mapped["company"]
    if mapped.get("city") and not lead.city:
        lead.city = mapped["city"]
    if mapped.get("state") and not lead.state:
        lead.state = mapped["state"]
    if mapped.get("platform") and not lead.competitor_platform:
        lead.source_detail.setdefault("platform_stated", mapped["platform"])


def ingest_rows(rows: list[dict], source: IngestSource, store) -> dict[str, int]:
    """Add each row as a lead (or enrich the existing one). Returns counts for the CLI to print."""
    if source not in ("meta_ads", "inbound"):
        raise ValueError("source must be 'meta_ads' or 'inbound'")
    stats = {"rows": len(rows), "new": 0, "updated": 0, "skipped": 0}
    via = "meta_lead_ads" if source == "meta_ads" else "inbound_form"
    for row in rows:
        mapped = map_row(row)
        domain = domain_for(mapped)
        if not domain or is_junk(domain):
            log.info(
                "ingest: no company domain for row %s; skipped",
                mapped.get("leadgen_id") or mapped.get("email"),
            )
            stats["skipped"] += 1
            continue
        attribution = _attribution(mapped, source)
        created = store.add_candidate(
            Candidate(domain=domain, source=source, discovered_via=via, source_detail={"ingest": attribution})
        )
        lead = store.get(domain)
        if not created:
            # Keep the original discovery source; record this touch alongside it.
            lead.source_detail.setdefault("ingest_history", []).append(attribution)
        _merge_contact(lead, mapped)
        store.save(lead)
        stats["new" if created else "updated"] += 1
    return stats


def ingest_file(path: Path | str, source: IngestSource, store) -> dict[str, int]:
    return ingest_rows(read_rows(path), source, store)
