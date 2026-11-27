"""Enrolment: pick sendable leads, choose a variant, add them to Smartlead, record it everywhere.

Order of writes, chosen so a crash at any point is safe to re-run:
  1. Smartlead add_leads (re-adding an existing lead returns its id under existingLeads)
  2. local store: lead.outreach (campaign, Smartlead lead ids, variant, enrolled_at) plus the portal
     rows we still owe, kept in source_detail['outreach_portal_pending']
  3. portal: contacts, email_drafts (step 1, status queued), leads outreach columns, activities.
     Pending rows that failed are retried at the start of the next run.
The gate rejects any lead with outreach.enrolled_at, so a lead is never added twice.

Daily cap: `daily_cap` is the number of email sends per day enrolment may commit to. A named
contact costs 4 sends (4 steps), a role mailbox 2. 300/day therefore enrols about 75 new contacts a
day, which is what 300 sends/day sustains once every sequence is in flight.
"""

from __future__ import annotations

import json
import logging
import random
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..config import settings
from ..domains import normalize_domain, registrable
from ..enrich.email_hygiene import email_owners, normalize_lead_emails
from ..models import Lead
from ..store import LeadStore
from .gate import USE_DEFAULT_MX, Contact, Suppressions, sendable_contacts
from .models import FDA_SEGMENT, PERMANENT_SUPPRESSION_REASONS, Variant
from .personalize import build_custom_fields, get_opener
from .sequences import (
    CAMPAIGN_KEYS,
    CAMPAIGNS,
    GENERIC_CAMPAIGN,
    UNSUBSCRIBE_TEXT,
    WEBHOOK_NAME,
    as_email_html,
    assign_segment,
    campaign_variants,
    render,
    smartlead_sequences,
    variant_label,
    with_footer,
)
from .smartlead import WEBHOOK_EVENTS, SmartleadError

log = logging.getLogger(__name__)

STATE_FILE = "smartlead_campaigns.json"
CONTROL_FLOOR = 0.10
SUPPRESSION_REASONS = (
    "unsubscribe",
    "bounce",
    "complaint",
    "not_interested",
    "customer",
    "competitor",
    "manual",
)
# reasons that also go on Smartlead's account-wide block list (not_interested expires, so it does not)
PERMANENT_REASONS = PERMANENT_SUPPRESSION_REASONS
NOT_INTERESTED_DAYS = 180
PLACEHOLDER_ADDRESS = "TO BE CONFIRMED"
PENDING_KEY = "outreach_portal_pending"


# ------------------------------------------------------------------ config + state
@dataclass
class OutreachConfig:
    company_name_legal: str
    postal_address: str
    sender_name: str
    daily_cap: int
    reps: list[dict] = field(default_factory=list)

    def footer_problem(self) -> str | None:
        """CAN-SPAM needs a real postal address in every email; refuse to send without one."""
        if not self.postal_address or PLACEHOLDER_ADDRESS in self.postal_address.upper():
            return "postal address missing: set app_settings 'outreach'.postal_address or LEADGEN_OUTREACH_POSTAL_ADDRESS"
        return None


def _real(value: Any) -> str:
    text = str(value or "").strip()
    return "" if PLACEHOLDER_ADDRESS in text.upper() else text


def load_config(sink: Any | None) -> OutreachConfig:
    """Portal app_settings 'outreach' first (admins edit it there), env settings as the fallback."""
    value: dict = {}
    if sink is not None:
        try:
            value = sink.get_app_setting("outreach") or {}
        except RuntimeError as exc:
            log.warning("could not read app_settings 'outreach': %s", exc)
    return OutreachConfig(
        company_name_legal=_real(value.get("company_name"))
        or settings.outreach_company_legal_name
        or "MyOrbit Health",
        postal_address=_real(value.get("postal_address")) or settings.outreach_postal_address,
        sender_name=settings.outreach_sender_name,
        daily_cap=int(value.get("daily_cap") or settings.outreach_daily_cap),
        reps=list(value.get("reps") or []),
    )


def default_state_path() -> Path:
    """Next to the SQLite store, so the campaign ids travel with the data."""
    return Path(settings.db_path).resolve().parent / STATE_FILE


def load_state(path: Path | None = None, sink: Any | None = None) -> dict:
    """Campaign ids from the local state file. When the file is missing (a fresh CI runner only
    restores leads.db), fall back to the ids sync-campaigns also wrote to app_settings 'outreach'."""
    path = path or default_state_path()
    if path.exists():
        return json.loads(path.read_text())
    state: dict = {"campaigns": {}}
    if sink is not None:
        try:
            ids = (sink.get_app_setting("outreach") or {}).get("campaigns") or {}
        except RuntimeError as exc:
            log.warning("could not read campaign ids from app_settings 'outreach': %s", exc)
            ids = {}
        state["campaigns"] = {key: {"id": int(cid)} for key, cid in ids.items() if cid}
    return state


def save_state(state: dict, path: Path | None = None) -> None:
    path = path or default_state_path()
    path.write_text(json.dumps(state, indent=2, sort_keys=True))


# ------------------------------------------------------------------ plan
@dataclass
class Candidate:
    lead: Lead
    segment: str
    campaign_key: str  # the segment, or 'generic' for a role mailbox
    contacts: list[Contact]
    tier: str


@dataclass
class Plan:
    candidates: list[Candidate] = field(default_factory=list)
    rejections: Counter = field(default_factory=Counter)
    by_segment: Counter = field(default_factory=Counter)


def build_suppressions(leads: list[Lead], sink: Any | None) -> Suppressions:
    """Portal suppressions plus what the local store knows (suppressed companies and emails)."""
    supp = Suppressions()
    if sink is not None:
        supp = Suppressions.from_rows(sink.fetch_suppressions())
    for lead in leads:
        if lead.outreach.suppressed_at:
            supp.add(domain=lead.domain)
        for email in lead.source_detail.get("suppressed_emails", []):
            supp.add(email=email)
    return supp


def plan_enrolment(
    store: LeadStore,
    sink: Any | None = None,
    *,
    segment: str | None = None,
    allow_generic: bool = False,
    min_score: int | None = None,
    mx_check: Any = USE_DEFAULT_MX,
) -> Plan:
    """Run every lead through the gate. Read-only: nothing is written anywhere.

    Stored emails are cleaned in memory first (normalize_lead_emails), and the email -> companies
    map is built over every lead so a template address shared by several companies is rejected
    (shared_contact_email). mx_check: gate.DEFAULT_MX_CHECK by default, None to skip DNS.
    """
    leads = list(store.iter())
    for lead in leads:
        normalize_lead_emails(lead)
    shared = email_owners(leads)
    statuses = sink.fetch_status([lead.domain for lead in leads]) if sink is not None else {}
    supp = build_suppressions(leads, sink)
    plan = Plan()
    for lead in leads:
        seg = assign_segment(lead)
        if segment and seg != segment:
            continue
        contacts, reason, tier = sendable_contacts(
            lead,
            statuses.get(lead.domain),
            supp,
            allow_generic=allow_generic,
            min_score=min_score,
            shared=shared,
            mx_check=mx_check,
        )
        skipped = set(lead.source_detail.get("smartlead_skipped", {}))
        contacts = [c for c in contacts if c.email not in skipped]
        if reason or not contacts:
            plan.rejections[reason or "smartlead_skipped"] += 1
            continue
        # A role mailbox normally gets the 2-step generic campaign, but its copy carries the price
        # pitch, so an FDA-list lead stays in its compliance-only campaign whatever the tier.
        key = GENERIC_CAMPAIGN if tier == "generic" and seg != FDA_SEGMENT else seg
        plan.candidates.append(Candidate(lead, seg, key, contacts, tier))
        plan.by_segment[key] += 1
    return plan


# ------------------------------------------------------------------ variants
def choose_variant(
    variants: list[Variant], rng: random.Random, control_floor: float = CONTROL_FLOOR
) -> Variant:
    """Pick the arm for one lead.

    Until any arm has sends, an equal random split. After that, Thompson sampling: one draw from
    each arm's Beta(alpha, beta), highest wins, with the control forced for `control_floor` of
    leads so the baseline keeps getting traffic.
    """
    live = [v for v in variants if v.status in ("active", "control")]
    if not live:
        raise ValueError("no active variants")
    if all(v.sends == 0 for v in live):
        return rng.choice(live)
    control = next((v for v in live if v.status == "control"), None)
    if control is not None and len(live) > 1 and rng.random() < control_floor:
        return control
    return max(live, key=lambda v: rng.betavariate(max(v.alpha, 1e-6), max(v.beta, 1e-6)))


def load_variants(sink: Any | None) -> dict[str, dict[int, list[Variant]]]:
    """{campaign key: {step: [variants]}}: portal rows when present, the built-in copy otherwise."""
    portal: list[Variant] = []
    if sink is not None:
        try:
            portal = [Variant.model_validate(row) for row in sink.fetch_variants()]
        except RuntimeError as exc:
            log.warning("could not read portal variants, using built-in copy: %s", exc)
    out: dict[str, dict[int, list[Variant]]] = {}
    for key in CAMPAIGN_KEYS:
        steps: dict[int, list[Variant]] = {}
        for v in [p for p in portal if p.segment == key]:
            steps.setdefault(v.sequence_step, []).append(v)
        # Built-in copy only for steps the portal does not cover with a live arm. Decide coverage
        # before adding anything, or the first built-in arm would shadow its siblings.
        covered = {
            step for step, arms in steps.items() if any(x.status in ("active", "control") for x in arms)
        }
        for v in campaign_variants(key):
            if v.sequence_step not in covered:
                steps.setdefault(v.sequence_step, []).append(v)
        out[key] = steps
    return out


# ------------------------------------------------------------------ payloads
CUSTOM_FIELD_KEYS = (
    "opener",
    "platform",
    "vertical_phrase",
    "sender_name",
    "booking_link",
    "company_name_legal",
    "postal_address",
    "segment",
)


def render_steps(chosen: dict[int, Variant], fields: dict[str, str]) -> dict[str, str]:
    """{step1_subject, step1_body, ..., stepN_body} rendered for one contact, footer included."""
    out: dict[str, str] = {}
    for step, variant in sorted(chosen.items()):
        if step == 1:
            out["step1_subject"] = render(variant.subject or "", fields)
        out[f"step{step}_body"] = as_email_html(render(with_footer(variant.body or ""), fields))
    return out


def smartlead_lead_payload(lead: Lead, contact: Contact, fields: dict[str, str], rendered: dict) -> dict:
    location = ", ".join(p for p in (lead.city, lead.state, lead.country) if p)
    custom = {k: fields[k] for k in CUSTOM_FIELD_KEYS}
    custom.update(rendered)
    payload = {
        "email": contact.email,
        "first_name": contact.first_name or "",
        "last_name": contact.last_name or "",
        "company_name": fields["company_name"],
        "website": lead.website or f"https://{lead.domain}",
        "location": location,
        "custom_fields": custom,
    }
    if contact.linkedin:
        payload["linkedin_profile"] = contact.linkedin
    return payload


def _plain(html_body: str) -> str:
    return html_body.replace("<br>", "\n").replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


# ------------------------------------------------------------------ enrol
@dataclass
class EnrolResult:
    dry_run: bool
    budget: int
    used_before: int
    enrolled: list[dict] = field(default_factory=list)
    skipped_cap: int = 0
    smartlead_skipped: int = 0
    portal_errors: int = 0
    openers: Counter = field(default_factory=Counter)
    plan: Plan | None = None

    @property
    def sends_committed(self) -> int:
        return sum(row["cost"] for row in self.enrolled)

    def summary(self) -> str:
        verb = "would enrol" if self.dry_run else "enrolled"
        leads = len({row["domain"] for row in self.enrolled})
        return (
            f"outreach enroll: {verb} {len(self.enrolled)} contacts at {leads} companies, "
            f"{self.sends_committed}/{self.budget} sends of today's budget ({self.used_before} used before), "
            f"{self.skipped_cap} over cap, {self.smartlead_skipped} skipped by Smartlead, "
            f"{self.portal_errors} portal errors, openers {dict(self.openers)}"
        )


def _campaign_key_of(lead: Lead) -> str:
    return lead.source_detail.get("outreach_campaign_key") or lead.outreach.segment or "other"


def sends_committed_on(store: LeadStore, day: datetime) -> int:
    """Sends already committed by contacts enrolled on `day` (UTC date)."""
    total = 0
    for lead in store.iter():
        when = lead.outreach.enrolled_at
        if when and when.astimezone(UTC).date() == day.astimezone(UTC).date():
            steps = CAMPAIGNS[_campaign_key_of(lead)].step_count if _campaign_key_of(lead) in CAMPAIGNS else 4
            total += steps * max(1, len(lead.outreach.enrolled_emails))
    return total


@dataclass
class _Item:
    cand: Candidate
    chosen: dict[int, Variant]
    payloads: list[dict]
    cost: int


def enroll(
    store: LeadStore,
    client: Any | None,
    sink: Any | None,
    *,
    daily_cap: int | None = None,
    segment: str | None = None,
    allow_generic: bool = False,
    dry_run: bool = False,
    rng: random.Random | None = None,
    now: datetime | None = None,
    anthropic_client: Any | None = None,
    state_path: Path | None = None,
    batch_size: int = 100,
) -> EnrolResult:
    """Gate -> variant -> Smartlead -> store -> portal. Idempotent; see the module docstring."""
    now = now or datetime.now(UTC)
    rng = rng or random.Random()
    config = load_config(sink)
    cap = config.daily_cap if daily_cap is None else daily_cap
    state = load_state(state_path, sink)
    if not dry_run:
        if client is None:
            raise RuntimeError("SMARTLEAD_API_KEY is required to enrol (use --dry-run to preview)")
        if config.footer_problem():
            raise RuntimeError(config.footer_problem())
        if sink is not None:
            retry_pending_portal_rows(store, sink)

    used = sends_committed_on(store, now)
    result = EnrolResult(dry_run=dry_run, budget=max(0, cap - used), used_before=used)
    plan = plan_enrolment(store, sink, segment=segment, allow_generic=allow_generic)
    result.plan = plan
    variants = load_variants(sink)
    budget = result.budget

    items_by_key: dict[str, list[_Item]] = {}
    for cand in plan.candidates:
        campaign = CAMPAIGNS[cand.campaign_key]
        cost = campaign.step_count * len(cand.contacts)
        if cost > budget:
            result.skipped_cap += 1
            continue
        if not dry_run and cand.campaign_key not in state["campaigns"]:
            raise RuntimeError(
                f"no Smartlead campaign for '{cand.campaign_key}': run `leadgen outreach sync-campaigns`"
            )
        budget -= cost
        steps = variants[cand.campaign_key]
        chosen = {step: choose_variant(steps[step], rng) for step in sorted(steps)}
        # Claude openers only for named contacts in a real run; role mailboxes get the template.
        opener, source = get_opener(
            cand.lead,
            anthropic_client,
            use_model=cand.tier == "named" and not dry_run,
            angle=chosen[1].angle,
        )
        result.openers[source] += 1
        payloads = []
        for contact in cand.contacts:
            fields = build_custom_fields(
                cand.lead,
                segment=cand.segment,
                opener=opener,
                utm_content=variant_label(1, chosen[1].name),
                first_name=contact.first_name,
                company_name_legal=config.company_name_legal,
                postal_address=config.postal_address,
                sender_name=config.sender_name,
            )
            payloads.append(smartlead_lead_payload(cand.lead, contact, fields, render_steps(chosen, fields)))
        items_by_key.setdefault(cand.campaign_key, []).append(_Item(cand, chosen, payloads, cost))

    for key, items in items_by_key.items():
        if dry_run:
            for item in items:
                _note_enrolled(result, item, key, None, {})
            continue
        campaign_id = int(state["campaigns"][key]["id"])
        response = client.add_leads(
            campaign_id, [p for item in items for p in item.payloads], batch_size=batch_size
        )
        for item in items:
            ids = {
                p["email"]: response["email_to_lead_id"][p["email"]]
                for p in item.payloads
                if p["email"] in response["email_to_lead_id"]
            }
            _record(store, item, key, campaign_id, ids, response.get("skipped", {}), now)
            if not ids:
                result.smartlead_skipped += len(item.payloads)
                continue
            _note_enrolled(result, item, key, campaign_id, ids)
            if sink is not None and not _push_pending(store, sink, item.cand.lead.domain):
                result.portal_errors += 1
    log.info(result.summary())
    return result


def _note_enrolled(result: EnrolResult, item: _Item, key: str, campaign_id: int | None, ids: dict) -> None:
    per_contact = item.cost // max(1, len(item.payloads))
    for p in item.payloads:
        if campaign_id is not None and p["email"] not in ids:
            continue
        result.enrolled.append(
            {
                "domain": item.cand.lead.domain,
                "email": p["email"],
                "campaign": key,
                "variant": item.chosen[1].name,
                "score": item.cand.lead.score,
                "smartlead_lead_id": ids.get(p["email"]),
                "cost": per_contact,
            }
        )


def _record(
    store: LeadStore,
    item: _Item,
    key: str,
    campaign_id: int,
    ids: dict[str, int],
    skipped: dict[str, str],
    now: datetime,
) -> None:
    """Write the enrolment into the store, plus the portal rows still to push."""
    lead = item.cand.lead
    for p in item.payloads:
        if p["email"] not in ids:
            lead.source_detail.setdefault("smartlead_skipped", {})[p["email"]] = skipped.get(
                p["email"], "not_added"
            )
    if not ids:
        store.save(lead)
        return
    state = lead.outreach
    state.segment = item.cand.segment
    state.smartlead_campaign_id = campaign_id
    state.smartlead_lead_ids.update(ids)
    state.variant_id = item.chosen[1].id
    state.enrolled_at = now
    state.enrolled_emails = sorted(ids)
    lead.status = "enrolled"
    lead.source_detail["outreach_campaign_key"] = key
    lead.source_detail["outreach_variants"] = {str(step): v.id for step, v in item.chosen.items()}
    lead.source_detail[PENDING_KEY] = _portal_rows(item, campaign_id, ids, now)
    store.save(lead)


def _portal_rows(item: _Item, campaign_id: int, ids: dict[str, int], now: datetime) -> dict:
    lead = item.cand.lead
    contacts = {c.email: c for c in item.cand.contacts}
    rows: dict[str, Any] = {"contacts": [], "drafts": [], "activities": []}
    for p in item.payloads:
        email = p["email"]
        if email not in ids:
            continue
        c = contacts[email]
        rows["contacts"].append(
            {
                "email": email,
                "first_name": c.first_name or email.split("@", 1)[0],
                "last_name": c.last_name,
                "title": c.title,
                "lead_domain": lead.domain,
                "email_status": c.email_status,
                "is_decision_maker": c.is_decision_maker,
                "linkedin": c.linkedin,
                "smartlead_lead_id": ids[email],
                "smartlead_campaign_id": campaign_id,
            }
        )
        rows["drafts"].append(
            {
                "to_email": email,
                "subject": p["custom_fields"]["step1_subject"],
                "body": _plain(p["custom_fields"]["step1_body"]),
                "status": "queued",
                "sequence_step": 1,
                "direction": "outbound",
                "lead_domain": lead.domain,
                "variant_id": item.chosen[1].id,
                "smartlead_campaign_id": campaign_id,
                "smartlead_lead_id": ids[email],
            }
        )
        rows["activities"].append(
            {
                "kind": "outreach_enrolled",
                "body": f"Enrolled {email} in Smartlead campaign {item.cand.campaign_key} "
                f"(variant {item.chosen[1].name})",
                "email": email,
            }
        )
    rows["lead"] = {
        "segment": item.cand.segment,
        "enrolled_at": now.isoformat(),
        "smartlead_campaign_id": campaign_id,
        "first_touch_source": "smartlead",
    }
    return rows


def _push_pending(store: LeadStore, sink: Any, domain: str) -> bool:
    """Push one lead's pending portal rows. Returns False (rows kept for the next run) on failure."""
    lead = store.get(domain)
    if lead is None or PENDING_KEY not in lead.source_detail:
        return True
    rows = lead.source_detail[PENDING_KEY]
    try:
        contact_ids = sink.upsert_contacts(rows["contacts"])
        sink.insert_email_drafts(
            [{**d, "contact_id": contact_ids.get(d["to_email"])} for d in rows["drafts"]]
        )
        sink.update_lead_outreach(domain, rows["lead"])
        for activity in rows["activities"]:
            email = activity["email"]
            sink.insert_activity(
                {"kind": activity["kind"], "body": activity["body"], "contact_id": contact_ids.get(email)}
            )
    except RuntimeError as exc:
        log.warning("portal write for %s failed, will retry next run: %s", domain, exc)
        return False
    lead.source_detail.pop(PENDING_KEY, None)
    store.save(lead)
    return True


def retry_pending_portal_rows(store: LeadStore, sink: Any) -> int:
    """Push portal rows that an earlier run could not write. Returns how many leads succeeded."""
    pending = [lead.domain for lead in store.iter() if PENDING_KEY in lead.source_detail]
    return sum(1 for domain in pending if _push_pending(store, sink, domain))


# ------------------------------------------------------------------ sync campaigns
def account_ids_from_settings() -> list[int]:
    raw = settings.smartlead_email_account_ids
    return [int(x) for x in raw.replace(" ", "").split(",") if x]


def campaign_payload(key: str, daily_cap: int, account_ids: list[int]) -> dict:
    """Everything sync-campaigns sends for one campaign (also what --dry-run prints)."""
    campaign = CAMPAIGNS[key]
    return {
        "name": campaign.name,
        "sequences": smartlead_sequences(campaign),
        "settings": {
            "stop_on_reply": True,
            "track_opens": True,
            "track_clicks": False,  # rewritten links hurt cold-email deliverability
            "unsubscribe_text": UNSUBSCRIBE_TEXT,
            "plain_text": True,
        },
        "schedule": {
            "timezone": settings.outreach_timezone,
            "days": [1, 2, 3, 4, 5],
            "start_hour": "08:00",
            "end_hour": "17:00",
            "min_minutes_between": 10,
            "max_new_leads_per_day": max(1, daily_cap // max(1, campaign.step_count)),
        },
        "email_account_ids": account_ids,
        "webhook": {
            "name": WEBHOOK_NAME,
            "url": settings.smartlead_webhook_url,
            "event_types": WEBHOOK_EVENTS,
        },
    }


def _merge_sequence_ids(payload: list[dict], existing: list[dict]) -> list[dict]:
    """Reuse Smartlead's ids for steps that already exist, so a re-sync updates instead of appending."""
    ids = {int(s.get("seq_number", 0)): s.get("id") for s in existing}
    return [{**seq, "id": ids.get(seq["seq_number"])} for seq in payload]


def sync_campaigns(
    client: Any | None,
    sink: Any | None,
    *,
    dry_run: bool = False,
    keys: tuple[str, ...] = CAMPAIGN_KEYS,
    daily_cap: int | None = None,
    start: bool = False,
    state_path: Path | None = None,
) -> dict:
    """Make sure each campaign exists in Smartlead with our sequences, settings, schedule, mailboxes
    and webhook; record campaign ids and variant ids locally and in the portal."""
    cap = daily_cap or load_config(sink).daily_cap
    accounts = account_ids_from_settings()
    report: dict[str, Any] = {"dry_run": dry_run, "campaigns": {}}
    if dry_run:
        for key in keys:
            report["campaigns"][key] = campaign_payload(key, cap, accounts)
        return report
    if client is None:
        raise RuntimeError("SMARTLEAD_API_KEY is required for sync-campaigns (use --dry-run to preview)")
    state = load_state(state_path)
    by_name = {c.get("name"): c for c in client.list_campaigns()}
    for key in keys:
        payload = campaign_payload(key, cap, accounts)
        known = state["campaigns"].get(key, {}).get("id") or (by_name.get(payload["name"]) or {}).get("id")
        campaign_id = int(known or client.create_campaign(payload["name"])["id"])
        try:
            sequences = _merge_sequence_ids(payload["sequences"], client.get_sequences(campaign_id))
            client.save_sequences(campaign_id, sequences)
        except SmartleadError as exc:  # refused while ACTIVE: the placeholders rarely change anyway
            log.warning("sequences for %s not updated: %s", key, exc)
        client.update_settings(campaign_id, **payload["settings"])
        client.update_schedule(campaign_id, **payload["schedule"])
        if accounts:
            client.add_email_accounts(campaign_id, accounts)
        if settings.smartlead_webhook_url:
            client.upsert_webhook(
                campaign_id,
                WEBHOOK_NAME,
                settings.smartlead_webhook_url,
                WEBHOOK_EVENTS,
                settings.smartlead_webhook_secret,
            )
        else:
            log.warning("SMARTLEAD_WEBHOOK_URL not set: campaign %s has no webhook", key)
        if start:
            client.update_status(campaign_id, "START")
        rows = []
        for v in campaign_variants(key, campaign_id):
            row = v.model_dump(exclude={"sends", "opens", "replies", "positive_replies", "meetings"})
            row["smartlead_variant_label"] = variant_label(v.sequence_step, v.name)
            rows.append(row)
        ids = sink.upsert_variants(rows) if sink is not None else {}
        state["campaigns"][key] = {
            "id": campaign_id,
            "name": payload["name"],
            "variants": {
                row["smartlead_variant_label"]: ids.get((key, row["sequence_step"], row["name"]), row["id"])
                for row in rows
            },
        }
        report["campaigns"][key] = {"id": campaign_id, "name": payload["name"], "variants": len(rows)}
    state["synced_at"] = datetime.now(UTC).isoformat()
    save_state(state, state_path)
    if sink is not None:
        current = sink.get_app_setting("outreach") or {}
        current["campaigns"] = {k: v["id"] for k, v in state["campaigns"].items()}
        sink.set_app_setting("outreach", current)
    log.info("outreach sync-campaigns: %s", {k: v["id"] for k, v in report["campaigns"].items()})
    return report


# ------------------------------------------------------------------ pull stats
_STATUS_RANK = {
    "queued": 0,
    "sent": 1,
    "delivered": 2,
    "opened": 3,
    "replied": 5,
    "bounced": 6,
    "unsubscribed": 7,
}


def _stat_update(row: dict) -> dict:
    """email_drafts fields from one Smartlead statistics row."""
    sent = row.get("sent_time")
    update = {"sent_at": sent, "smartlead_stats_id": row.get("stats_id") or row.get("email_stats_id")}
    status = "sent" if sent else "queued"
    if row.get("is_opened"):
        update["opened_at"] = row.get("open_time") or sent
        status = "opened"
    if row.get("is_clicked"):
        update["clicked_at"] = row.get("click_time") or sent
    if row.get("is_replied"):
        update["replied_at"] = row.get("reply_time") or sent
        status = "replied"
    if row.get("is_bounced"):
        update["bounced_at"] = row.get("bounce_time") or sent
        status = "bounced"
    if row.get("is_unsubscribed"):
        status = "unsubscribed"
    update["status"] = status
    return {k: v for k, v in update.items() if v is not None}


def _changes(existing: dict, update: dict) -> dict:
    """Only fill empty timestamps and only move status forward (webhooks may be ahead of stats)."""
    out = {k: v for k, v in update.items() if k not in ("status",) and not existing.get(k)}
    old, new = existing.get("status") or "queued", update.get("status", "queued")
    if _STATUS_RANK.get(new, 0) > _STATUS_RANK.get(old, 0):
        out["status"] = new
    return out


def pull_stats(
    store: LeadStore, client: Any, sink: Any | None, *, state_path: Path | None = None
) -> dict[str, int]:
    """Smartlead per-email statistics -> email_drafts rows (status, sent/opened/replied/bounced
    times) -> portal variant counters (outreach_refresh_variants) and leads.last_outreach_at."""
    state = load_state(state_path, sink)
    index: dict[str, Lead] = {}
    for lead in store.iter():
        for email in lead.outreach.smartlead_lead_ids:
            index[email.lower()] = lead
    stats: dict[tuple[int, int], tuple[Lead, str, dict, dict]] = {}
    for info in state["campaigns"].values():
        for row in client.iter_campaign_statistics(int(info["id"])):
            email = str(row.get("lead_email") or "").lower()
            lead = index.get(email)
            if lead is None or not row.get("sequence_number"):
                continue
            lead_id = int(lead.outreach.smartlead_lead_ids[email])
            stats[(lead_id, int(row["sequence_number"]))] = (lead, email, row, _stat_update(row))
    counts = {"rows": len(stats), "updated": 0, "inserted": 0, "leads_touched": 0}
    if sink is None or not stats:
        return counts
    # Reply drafts share smartlead_lead_id but have no sequence_step: they are not sequence sends.
    existing = {
        (int(d["smartlead_lead_id"]), int(d["sequence_step"])): d
        for d in sink.fetch_email_drafts([lid for lid, _ in stats])
        if d.get("sequence_step") is not None
    }
    contact_ids = {
        e: c["id"] for e, c in sink.fetch_contacts_by_email([v[1] for v in stats.values()]).items()
    }
    new_rows = []
    last_sent: dict[str, str] = {}
    for (lead_id, step), (lead, email, row, update) in stats.items():
        if update.get("sent_at"):
            last_sent[lead.domain] = max(last_sent.get(lead.domain, ""), update["sent_at"])
        found = existing.get((lead_id, step))
        if found:
            changes = _changes(found, update)
            if changes:
                sink.update_email_draft(found["id"], changes)
                counts["updated"] += 1
            continue
        variant = (lead.source_detail.get("outreach_variants") or {}).get(str(step))
        new_rows.append(
            {
                **update,
                "to_email": email,
                "subject": row.get("email_subject") or f"(step {step})",
                "body": row.get("email_message") or "",
                "sequence_step": step,
                "direction": "outbound",
                "lead_domain": lead.domain,
                "variant_id": variant,
                "contact_id": contact_ids.get(email),
                "smartlead_campaign_id": lead.outreach.smartlead_campaign_id,
                "smartlead_lead_id": lead_id,
            }
        )
    counts["inserted"] = sink.insert_email_drafts(new_rows)
    for domain, sent in last_sent.items():
        lead = store.get(domain)
        when = datetime.fromisoformat(sent)
        when = when if when.tzinfo else when.replace(tzinfo=UTC)
        if lead and (lead.outreach.last_outreach_at is None or when > lead.outreach.last_outreach_at):
            lead.outreach.last_outreach_at = when
            store.save(lead)
            sink.update_lead_outreach(domain, {"last_outreach_at": sent})
            counts["leads_touched"] += 1
    sink.rpc("outreach_refresh_variants")
    log.info("outreach pull-stats: %s", counts)
    return counts


# ------------------------------------------------------------------ suppress
def suppress(
    value: str,
    reason: str,
    *,
    store: LeadStore,
    sink: Any | None = None,
    client: Any | None = None,
    now: datetime | None = None,
    source: str = "cli",
) -> dict[str, Any]:
    """Stop emailing an address or a whole domain: portal suppressions, local store, Smartlead."""
    if reason not in SUPPRESSION_REASONS:
        raise ValueError(f"reason must be one of {', '.join(SUPPRESSION_REASONS)}")
    now = now or datetime.now(UTC)
    value = value.strip().lower()
    is_email = "@" in value
    domain = normalize_domain(value.split("@", 1)[1] if is_email else value)
    if not domain:
        raise ValueError(f"not an email or domain: {value}")
    row = {
        "email": value if is_email else None,
        "domain": None if is_email else domain,
        "reason": reason,
        "source": source,
        "expires_at": (now + timedelta(days=NOT_INTERESTED_DAYS)).isoformat()
        if reason == "not_interested"
        else None,
    }
    out: dict[str, Any] = {"value": value, "portal": 0, "leads": 0, "smartlead_unsubscribed": 0}
    if sink is not None:
        out["portal"] = sink.upsert_suppressions([row])
    for lead_domain in {domain, registrable(domain) or domain}:
        lead = store.get(lead_domain)
        if lead is None:
            continue
        out["leads"] += 1
        if is_email:
            emails = set(lead.source_detail.get("suppressed_emails", [])) | {value}
            lead.source_detail["suppressed_emails"] = sorted(emails)
            targets = [value]
        else:
            lead.outreach.suppressed_at = now
            lead.outreach.suppression_reason = reason
            targets = list(lead.outreach.smartlead_lead_ids)
        store.save(lead)
        if client is not None and lead.outreach.smartlead_campaign_id:
            for email in targets:
                lead_id = lead.outreach.smartlead_lead_ids.get(email)
                if lead_id:
                    try:
                        client.unsubscribe_lead(lead.outreach.smartlead_campaign_id, lead_id)
                        out["smartlead_unsubscribed"] += 1
                    except SmartleadError as exc:
                        log.warning("smartlead unsubscribe failed for %s: %s", email, exc)
    if client is not None and reason in PERMANENT_REASONS:
        client.add_domains_to_block_list([value])
        out["blocked"] = True
    log.info("outreach suppress: %s", out)
    return out


# ------------------------------------------------------------------ daily
def daily(
    store: LeadStore,
    client: Any,
    sink: Any | None,
    *,
    daily_cap: int | None = None,
    anthropic_client: Any | None = None,
    allow_generic: bool = False,
) -> dict[str, Any]:
    """What `leadgen daily` should run for outreach: stats first (so posteriors are fresh), then enrol."""
    stats = pull_stats(store, client, sink)
    result = enroll(
        store,
        client,
        sink,
        daily_cap=daily_cap,
        anthropic_client=anthropic_client,
        allow_generic=allow_generic,
    )
    return {"stats": stats, "enroll": result.summary()}
