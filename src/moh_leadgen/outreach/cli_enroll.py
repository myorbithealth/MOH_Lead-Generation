"""Enrolment-side commands: plan, enroll, sync-campaigns, pull-stats, suppress, review-list/approve."""

from __future__ import annotations

import csv
import json
import random
from datetime import UTC, datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from ..config import settings
from ..domains import normalize_domain
from ..models import Lead
from ..store import LeadStore
from .cli import outreach_app
from .models import FDA_SEGMENT

console = Console()


def _store() -> LeadStore:
    return LeadStore(settings.db_path)


def _sink():
    """The portal sink when the pipeline-db proxy token or the service key is set, else None.

    Without it every outreach command runs local-only and its portal rows wait in the store.
    """
    proxy = settings.portal_proxy_url and settings.portal_token
    direct = settings.supabase_url and settings.supabase_key
    if not (proxy or direct):
        return None
    from ..sinks.supabase import SupabaseSink

    return SupabaseSink()


def _smartlead(required: bool):
    if not settings.smartlead_api_key:
        if required:
            raise typer.BadParameter("SMARTLEAD_API_KEY is not set (use --dry-run to preview)")
        return None
    from .smartlead import SmartleadClient

    return SmartleadClient()


@outreach_app.command("plan")
def plan_cmd(
    segment: str | None = typer.Option(None, help="Only this segment"),
    allow_generic: bool = typer.Option(False, "--allow-generic", help="Include info@/hello@ mailboxes"),
    min_score: int | None = typer.Option(None, help="Override LEADGEN_OUTREACH_MIN_SCORE"),
    top: int = typer.Option(20, help="How many top candidates to list"),
):
    """Dry run of the gate: sendable counts by segment, rejections by reason, top candidates."""
    from .enroll import plan_enrolment

    plan = plan_enrolment(
        _store(), _sink(), segment=segment, allow_generic=allow_generic, min_score=min_score
    )
    by_segment = Table("campaign", "companies", "contacts")
    for key, n in sorted(plan.by_segment.items()):
        contacts = sum(len(c.contacts) for c in plan.candidates if c.campaign_key == key)
        by_segment.add_row(key, str(n), str(contacts))
    console.print(by_segment)
    reasons = Table("rejection reason", "companies")
    for reason, n in plan.rejections.most_common():
        reasons.add_row(reason, str(n))
    console.print(reasons)
    best = Table("score", "domain", "campaign", "tier", "contacts")
    for cand in plan.candidates[:top]:
        best.add_row(
            str(cand.lead.score),
            cand.lead.domain,
            cand.campaign_key,
            cand.tier,
            ", ".join(c.email for c in cand.contacts),
        )
    console.print(best)


@outreach_app.command("sync-campaigns")
def sync_campaigns_cmd(
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the payloads, call nothing"),
    segment: str | None = typer.Option(None, help="Only this campaign key (a segment or 'generic')"),
    start: bool = typer.Option(False, "--start", help="Also set each campaign to START"),
):
    """Create/update one Smartlead campaign per segment (+ 'generic') and record the ids."""
    from .enroll import sync_campaigns
    from .sequences import CAMPAIGN_KEYS

    keys = (segment,) if segment else CAMPAIGN_KEYS
    if segment and segment not in CAMPAIGN_KEYS:
        raise typer.BadParameter(f"unknown campaign {segment!r}; choose from {', '.join(CAMPAIGN_KEYS)}")
    report = sync_campaigns(_smartlead(not dry_run), _sink(), dry_run=dry_run, keys=keys, start=start)
    console.print_json(json.dumps(report, default=str))


@outreach_app.command("enroll")
def enroll_cmd(
    daily_cap: int | None = typer.Option(
        None, help="Email sends per day enrolment may commit to (4 per named contact, 2 per role mailbox)"
    ),
    segment: str | None = typer.Option(None, help="Only this segment"),
    allow_generic: bool = typer.Option(False, "--allow-generic", help="Include info@/hello@ mailboxes"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show who would be enrolled; write nothing"),
    seed: int | None = typer.Option(None, help="Seed the variant sampler (reproducible runs)"),
):
    """Gate -> variant -> Smartlead add_leads -> store + portal. Safe to re-run."""
    from .enroll import enroll
    from .personalize import default_client

    result = enroll(
        _store(),
        _smartlead(not dry_run),
        _sink(),
        daily_cap=daily_cap,
        segment=segment,
        allow_generic=allow_generic,
        dry_run=dry_run,
        rng=random.Random(seed),
        anthropic_client=None if dry_run else default_client(),
    )
    if dry_run:
        table = Table("score", "domain", "email", "campaign", "variant")
        for row in result.enrolled[:50]:
            table.add_row(str(row["score"]), row["domain"], row["email"], row["campaign"], row["variant"])
        console.print(table)
    console.print(result.summary())


@outreach_app.command("pull-stats")
def pull_stats_cmd():
    """Smartlead statistics -> portal email_drafts statuses and variant counters."""
    from .enroll import pull_stats

    counts = pull_stats(_store(), _smartlead(True), _sink())
    console.print(f"outreach pull-stats: {counts}")


@outreach_app.command("suppress")
def suppress_cmd(
    value: str = typer.Argument(..., help="An email address or a domain"),
    reason: str = typer.Option(
        "manual", help="unsubscribe|bounce|complaint|not_interested|customer|competitor|manual"
    ),
):
    """Never email this address/domain again (portal suppressions, local store, Smartlead)."""
    from .enroll import suppress

    try:
        out = suppress(value, reason, store=_store(), sink=_sink(), client=_smartlead(False))
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    console.print(f"outreach suppress: {out}")


# ------------------------------------------------------------------ human review (FDA-list leads)
REVIEW_COLUMNS = (
    "domain",
    "company",
    "vertical",
    "score",
    "owner",
    "email",
    "discovered_via",
    "fda_review",
)


def _review_row(lead: Lead) -> dict[str, str]:
    email = lead.owner_email or lead.primary_email or next(iter(lead.emails), None)
    return {
        "domain": lead.domain,
        "company": lead.company_name or "",
        "vertical": lead.vertical or "",
        "score": str(lead.score),
        "owner": lead.owner_name or "",
        "email": email or "",
        "discovered_via": lead.discovered_via or "",
        "fda_review": str(lead.source_detail.get("fda_review") or "pending"),
    }


def review_rows(store: LeadStore, segment: str = FDA_SEGMENT) -> list[dict[str, str]]:
    """One row per lead in `segment`, best score first, with its current fda_review value."""
    from .sequences import assign_segment

    leads = [lead for lead in store.iter() if assign_segment(lead) == segment]
    leads.sort(key=lambda lead: (-lead.score, lead.domain))
    return [_review_row(lead) for lead in leads]


def set_review(store: LeadStore, domains: list[str], decision: str) -> dict[str, list[str]]:
    """Set source_detail['fda_review'] = decision on each FDA-list lead and save it.

    Returns {'updated': [...], 'not_found': [...], 'not_fda_list': [...]}; leads outside the
    fda_compliance segment are left untouched (they need no review).
    """
    from .sequences import assign_segment

    out: dict[str, list[str]] = {"updated": [], "not_found": [], "not_fda_list": []}
    now = datetime.now(UTC).isoformat()
    for raw in domains:
        domain = normalize_domain(raw) or raw.strip().lower()
        lead = store.get(domain)
        if lead is None:
            out["not_found"].append(domain)
            continue
        if assign_segment(lead) != FDA_SEGMENT:
            out["not_fda_list"].append(domain)
            continue
        lead.source_detail["fda_review"] = decision
        lead.source_detail["fda_review_at"] = now
        store.save(lead)
        out["updated"].append(domain)
    return out


def _domains_from_file(path: Path) -> list[str]:
    """One domain per line; '#' starts a comment (the seeds/ format)."""
    lines = (raw.split("#", 1)[0].strip() for raw in path.read_text(encoding="utf-8").splitlines())
    return [line.split(",", 1)[0].strip() for line in lines if line]


@outreach_app.command("review-list")
def review_list_cmd(
    segment: str = typer.Option(FDA_SEGMENT, help="Segment to review"),
    out: Path | None = typer.Option(None, "--out", help="Also write the rows to this CSV file"),
):
    """Every lead in the segment with its fda_review value (pending / approved / rejected)."""
    rows = review_rows(_store(), segment)
    table = Table(*REVIEW_COLUMNS)
    for row in rows:
        table.add_row(*(row[c] for c in REVIEW_COLUMNS))
    console.print(table)
    if out is not None:
        with out.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=REVIEW_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
    counts = {k: sum(1 for r in rows if r["fda_review"] == k) for k in ("pending", "approved", "rejected")}
    console.print(f"outreach review-list: {len(rows)} leads in {segment}, {counts}")


@outreach_app.command("review-approve")
def review_approve_cmd(
    domains: list[str] = typer.Argument(None, help="Domains to approve (or reject)"),
    all_from_file: Path | None = typer.Option(
        None, "--all-from-file", help="Also every domain in this file (one per line, '#' comments)"
    ),
    reject: bool = typer.Option(False, "--reject", help="Mark them rejected instead of approved"),
):
    """Record the human review of FDA-list leads: approved leads pass the gate, rejected never do."""
    targets = list(domains or [])
    if all_from_file is not None:
        targets += _domains_from_file(all_from_file)
    if not targets:
        raise typer.BadParameter("give at least one domain or --all-from-file")
    decision = "rejected" if reject else "approved"
    result = set_review(_store(), targets, decision)
    console.print(
        f"outreach review-approve: {decision} {len(result['updated'])}, "
        f"not found {result['not_found']}, not on the FDA list {result['not_fda_list']}"
    )
