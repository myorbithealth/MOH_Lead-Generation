"""Enrolment-side commands: plan, enroll, sync-campaigns, pull-stats, suppress."""

from __future__ import annotations

import json
import random

import typer
from rich.console import Console
from rich.table import Table

from ..config import settings
from ..store import LeadStore
from .cli import outreach_app

console = Console()


def _store() -> LeadStore:
    return LeadStore(settings.db_path)


def _sink():
    """The portal sink when SUPABASE_URL and the service key are set, else None (local only)."""
    if not (settings.supabase_url and settings.supabase_key):
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
