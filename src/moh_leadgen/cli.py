"""`leadgen` command-line interface."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path

import typer
from rich import print as rprint
from rich.console import Console
from rich.table import Table

from .config import settings
from .http import PoliteClient
from .models import Lead
from .outreach.cli import outreach_app
from .pipeline import enrich_lead, schedule_retry
from .scoring import score_lead
from .store import LeadStore
from .tools import TOOLS

app = typer.Typer(help="MyOrbit Health lead generation", no_args_is_help=True, add_completion=False)
console = Console()
app.add_typer(outreach_app, name="outreach")


def _store() -> LeadStore:
    return LeadStore(settings.db_path)


def _tool(name: str, seed: Path | None, opts: list[str]):
    if name not in TOOLS:
        raise typer.BadParameter(f"unknown tool {name!r}; choose from {', '.join(TOOLS)}")
    options = dict(o.split("=", 1) for o in opts if "=" in o)
    return TOOLS[name](seed_file=seed, **options)


@app.callback()
def _setup(verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging")):
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


@app.command()
def tools():
    """List available lead-generation tools."""
    t = Table("name", "source", "description")
    for name, cls in TOOLS.items():
        t.add_row(name, cls.source, cls.description)
    console.print(t)


@app.command()
def discover(
    tool: str = typer.Argument(..., help="legitscript | competitor | local"),
    limit: int = typer.Option(0, help="Stop after this many NEW candidates (0 = no limit)"),
    seed: Path | None = typer.Option(None, help="Override the seed file"),
    opt: list[str] = typer.Option([], "--opt", help="Tool option key=value (e.g. platform=bask)"),
):
    """Find candidate domains and add them to the local store."""
    store = _store()
    t = _tool(tool, seed, opt)
    added = 0
    for src in t.discovery_sources():
        if not src.available():
            rprint(f"[yellow]skip[/] {src.name}: not configured (missing key or file)")
            continue
        n = 0
        # The source gets only a safety scan cap; --limit counts NEW domains, enforced below.
        for cand in src.discover(limit=src.effective_scan_limit()):
            if t.keep_candidate(cand.domain) and store.add_candidate(cand):
                n += 1
                added += 1
                if limit and added >= limit:
                    break
        rprint(f"[green]{src.name}[/]: {n} new candidate(s)")
        if limit and added >= limit:
            break
    rprint(f"[bold]{added}[/] new candidates stored in {settings.db_path}")
    return added


@app.command()
def enrich(
    tool: str | None = typer.Argument(None, help="Only leads from this tool (default: all)"),
    limit: int = typer.Option(25, help="How many leads to enrich this run"),
    llm: bool = typer.Option(False, help="Use Claude to extract decision-makers (needs ANTHROPIC_API_KEY)"),
    redo: bool = typer.Option(False, help="Re-enrich leads that were already enriched"),
    providers: bool = typer.Option(
        True, help="Use Apollo/Lusha/Hunter for decision-maker contacts when keys are set"
    ),
    domain: str | None = typer.Option(None, help="Enrich a single domain (added if missing)"),
    workers: int = typer.Option(4, help="Sites crawled in parallel (each host is still rate-limited)"),
    platform: str | None = typer.Option(
        None, help="Only candidates seeded for / fingerprinted as this platform (prefix match)"
    ),
    retry_failed: bool = typer.Option(
        False, "--retry-failed", help="Only re-crawl failed sites whose back-off (1d, 3d, 7d) has expired"
    ),
):
    """Crawl each lead's site and fill in owner, contacts, LegitScript evidence, tech stack, score."""
    store = _store()
    source = TOOLS[tool].source if tool else None
    if domain:
        lead = store.get(domain) or Lead(domain=domain, source=source or "manual", discovered_via="cli")
        leads = [lead]
    elif retry_failed:
        requeued = store.requeue_legacy_failures()
        if requeued:
            rprint(f"requeued {requeued} site(s) an older version had parked after an empty crawl")
        leads = list(store.iter(source=source, retry_due_only=True, limit=limit, platform=platform))
    else:
        leads = list(
            store.iter(source=source, enriched=None if redo else False, limit=limit, platform=platform)
        )
    if not leads:
        rprint("nothing to enrich")
        return 0
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _work(lead: Lead) -> Lead:
        client = PoliteClient()
        try:
            enrich_lead(lead, client, use_llm=llm, use_providers=providers)
        except Exception as exc:  # keep the batch going
            logging.getLogger(__name__).exception("enrich failed for %s", lead.domain)
            lead.errors.append(f"enrich: {exc}")
            schedule_retry(lead, "other")
            score_lead(lead)
        finally:
            client.close()
        return lead

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(_work, lead) for lead in leads]
        for fut in as_completed(futures):
            lead = save_enriched(store, fut.result())
            done += 1
            rprint(
                f"[{done}/{len(leads)}] {lead.domain}: score {lead.score:>3}  ls={lead.legitscript.status:<13} "
                f"vertical={lead.vertical or '-':<20} platform={lead.competitor_platform or '-'} "
                f"owner={lead.owner_name or '-'} email={lead.owner_email or lead.primary_email or '-'}"
                f"{' (' + lead.owner_email_status + ')' if lead.owner_email_status else ''} phone={lead.owner_phone or lead.primary_phone or '-'}"
            )
    return done


def save_enriched(store: LeadStore, lead: Lead) -> Lead:
    """Persist an enriched lead; a platform tenant that redirected to its brand moves to that domain."""
    store.save(lead)
    target = lead.source_detail.get("rekey_to")
    if not target or target == lead.domain:
        return lead
    renamed = store.rename(lead.domain, target)
    if renamed is None:
        return lead
    store.save(score_lead(renamed))
    rprint(f"re-keyed {lead.domain} -> {target}")
    return renamed


@app.command()
def score():
    """Re-score every lead with the current weights (no crawling)."""
    store = _store()
    n = 0
    for lead in store.iter():
        store.save(score_lead(lead))
        n += 1
    rprint(f"re-scored {n} lead(s)")
    return n


@app.command()
def sync(
    target: str = typer.Argument(..., help="portal | csv"),
    min_score: int = typer.Option(0, help="Only leads at or above this score"),
    tool: str | None = typer.Option(None, help="Only leads from this tool"),
    out: Path = typer.Option(Path("out/leads.csv"), help="CSV path (csv target)"),
    all_: bool = typer.Option(False, "--all", help="Include leads already synced"),
):
    """Push enriched leads to the Lovable Sales Portal or a CSV."""
    store = _store()
    source = TOOLS[tool].source if tool else None
    leads = list(
        store.iter(
            source=source, enriched=True, min_score=min_score, unsynced_only=(target == "portal" and not all_)
        )
    )
    if not leads:
        rprint("nothing to sync")
        return 0
    if target == "portal":
        from .sinks.supabase import SupabaseSink

        n = SupabaseSink().upsert(leads)
        store.mark_synced([lead.domain for lead in leads])
        rprint(f"[green]upserted {n} lead(s) into the Sales Portal[/]")
    elif target == "csv":
        from .sinks.csvsink import write_csv

        n = write_csv(leads, out)
        rprint(f"[green]wrote {n} lead(s) to {out}[/]")
    else:
        raise typer.BadParameter("target must be portal or csv")
    return n


@app.command()
def run(
    tool: str = typer.Argument(..., help="legitscript | competitor | local"),
    discover_limit: int = typer.Option(50, help="New candidates to discover"),
    enrich_limit: int = typer.Option(25, help="Leads to enrich"),
    llm: bool = typer.Option(False),
    push: bool = typer.Option(True, help="Sync to the Sales Portal when configured"),
    min_score: int = typer.Option(40, help="Minimum score to push"),
    seed: Path | None = typer.Option(None),
    opt: list[str] = typer.Option([], "--opt"),
):
    """discover -> enrich -> score -> sync, in one go."""
    discover(tool, limit=discover_limit, seed=seed, opt=opt)
    # Keywords for every option: a typer default left out here would arrive as an OptionInfo object.
    enrich(
        tool,
        limit=enrich_limit,
        llm=llm,
        redo=False,
        providers=True,
        domain=None,
        workers=4,
        platform=None,
        retry_failed=False,
    )
    if push and settings.supabase_url and settings.supabase_key:
        sync("portal", min_score, tool, Path("out/leads.csv"), False)
    else:
        sync("csv", min_score, tool, Path(f"out/{tool}_leads.csv"), True)


@app.command()
def show(domain: str):
    """Print everything we know about one lead."""
    lead = _store().get(domain)
    if not lead:
        rprint(f"[red]{domain} not in store[/]")
        raise typer.Exit(1)
    console.print_json(json.dumps(lead.model_dump(mode="json"), indent=2, default=str))


@app.command()
def stats():
    """Counts by source, enrichment status and score bucket."""
    console.print_json(json.dumps(_store().stats(), indent=2))


@app.command()
def top(n: int = typer.Option(20), tool: str | None = typer.Option(None)):
    """Best leads first."""
    source = TOOLS[tool].source if tool else None
    t = Table("score", "domain", "company", "vertical", "LegitScript", "owner", "email", "phone", "platform")
    for lead in _store().iter(source=source, enriched=True, limit=n):
        t.add_row(
            str(lead.score),
            lead.domain,
            (lead.company_name or "")[:28],
            lead.vertical or "",
            lead.legitscript.status,
            f"{lead.owner_name or ''} {('(' + lead.owner_title + ')') if lead.owner_title else ''}"[:34],
            lead.owner_email or lead.primary_email or "",
            lead.primary_phone or "",
            lead.competitor_platform or "",
        )
    console.print(t)


@app.command()
def promote(domain: str, owner: str | None = typer.Option(None, help="Portal user id to own the records")):
    """Create company + contact + deal in the Sales Portal from a lead."""
    from .sinks.supabase import SupabaseSink

    result = SupabaseSink().promote(domain, owner)
    console.print_json(json.dumps(result, indent=2))


@app.command()
def ingest(
    path: Path = typer.Argument(..., exists=True, dir_okay=False, help="CSV or JSON export"),
    source: str = typer.Option(..., "--source", help="meta_ads | inbound"),
):
    """Add Meta Lead Ads exports or inbound form submissions to the store as leads."""
    from .outreach.ingest import ingest_file

    if source not in ("meta_ads", "inbound"):
        raise typer.BadParameter("--source must be meta_ads or inbound")
    stats = ingest_file(path, source, _store())
    rprint(f"ingest {source}: {stats}")
    return stats


class StepSkipped(Exception):
    """Raised by a daily step that has nothing to do (missing key, flag off). Not a failure."""


def run_steps(steps: list[tuple[str, Callable[[], object]]]) -> list[tuple[str, str, str]]:
    """Run each step in its own try/except so one failure never stops the rest.

    Returns (step, status, detail) rows where status is ok, skipped or failed.
    """
    log = logging.getLogger(__name__)
    rows: list[tuple[str, str, str]] = []
    for name, fn in steps:
        rprint(f"[bold cyan]== {name}[/]")
        try:
            detail = fn()
        except StepSkipped as why:
            rows.append((name, "skipped", str(why)))
        except Exception as exc:  # the daily run must reach every step
            log.exception("daily step %s failed", name)
            rows.append((name, "failed", f"{type(exc).__name__}: {exc}"[:200]))
        else:
            rows.append((name, "ok", "" if detail is None else str(detail)[:200]))
    return rows


def _require(condition: object, reason: str) -> None:
    if not condition:
        raise StepSkipped(reason)


def _portal_client():
    """PortalClient for the reply side; a missing Supabase setting means the step is skipped."""
    from .outreach.portal_io import PortalClient

    try:
        return PortalClient()
    except RuntimeError as exc:
        raise StepSkipped(str(exc)) from exc


def _daily_steps(
    *,
    discover_limit: int,
    enrich_limit: int,
    retry_limit: int,
    workers: int,
    min_score: int,
    daily_cap: int | None,
    do_enroll: bool,
    do_classify: bool,
    today: date,
) -> list[tuple[str, Callable[[], object]]]:
    """The ordered list of steps `leadgen daily` runs. Each is a small closure so it can fail alone."""
    has_portal = bool(settings.supabase_url and settings.supabase_key)
    llm = bool(settings.anthropic_key)

    def _discover(name: str) -> Callable[[], object]:
        return lambda: f"{discover(name, limit=discover_limit, seed=None, opt=[])} new"

    def _enrich(retry: bool) -> object:
        n = enrich(
            None,
            limit=retry_limit if retry else enrich_limit,
            llm=llm,
            redo=False,
            providers=True,
            domain=None,
            workers=workers,
            platform=None,
            retry_failed=retry,
        )
        return f"{n} lead(s)"

    def _sync() -> object:
        _require(has_portal, "SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set")
        return f"{sync('portal', min_score, None, Path('out/leads.csv'), False)} upserted"

    def _smartlead():
        from .outreach.smartlead import SmartleadClient

        _require(settings.smartlead_api_key, "SMARTLEAD_API_KEY not set")
        return SmartleadClient()

    def _sink():
        from .sinks.supabase import SupabaseSink

        return SupabaseSink() if has_portal else None

    def _enroll() -> object:
        from .outreach.enroll import enroll as run_enroll
        from .outreach.personalize import default_client

        _require(do_enroll, "--no-enroll")
        result = run_enroll(
            _store(), _smartlead(), _sink(), daily_cap=daily_cap, anthropic_client=default_client()
        )
        return result.summary()

    def _classify() -> object:
        from .outreach.process_replies import process_replies

        _require(do_classify, "--no-classify")
        _require(settings.anthropic_key, "ANTHROPIC_API_KEY not set")
        return process_replies(_portal_client(), limit=200)

    def _pull_stats() -> object:
        from .outreach.enroll import pull_stats

        return pull_stats(_store(), _smartlead(), _sink())

    def _learn() -> object:
        from .outreach.metrics import learn

        _require(today.weekday() == 0, "Mondays only")
        _require(settings.anthropic_key, "ANTHROPIC_API_KEY not set")
        report = learn(_portal_client(), propose=3, dry_run=False)
        return f"{len(report['proposed'])} proposed, {report['applied']} applied"

    steps: list[tuple[str, Callable[[], object]]] = [(f"discover {t}", _discover(t)) for t in TOOLS]
    steps += [
        ("enrich", lambda: _enrich(False)),
        ("enrich --retry-failed", lambda: _enrich(True)),
        ("score", lambda: f"{score()} lead(s)"),
        ("sync portal", _sync),
        ("outreach enroll", _enroll),
        ("outreach classify", _classify),
        ("outreach pull-stats", _pull_stats),
        ("outreach learn", _learn),
    ]
    return steps


@app.command()
def daily(
    discover_limit: int = typer.Option(100, help="New candidates per discovery tool"),
    enrich_limit: int = typer.Option(200, help="Leads to enrich (never-tried rows)"),
    retry_limit: int = typer.Option(100, help="Failed crawls to retry once their back-off expires"),
    workers: int = typer.Option(16, help="Sites crawled in parallel"),
    min_score: int = typer.Option(30, help="Minimum score pushed to the portal"),
    daily_cap: int | None = typer.Option(
        None, help="Email sends enrolment may commit to today (default LEADGEN_OUTREACH_DAILY_CAP, 300)"
    ),
    enroll_: bool = typer.Option(True, "--enroll/--no-enroll", help="Enrol new leads into Smartlead"),
    classify_: bool = typer.Option(True, "--classify/--no-classify", help="Classify new replies"),
):
    """Run the whole day: discover, enrich, score, sync, enrol, classify, pull stats (and learn on Mondays).

    Steps whose keys are missing are skipped. Exit code 1 if any step failed.
    """
    steps = _daily_steps(
        discover_limit=discover_limit,
        enrich_limit=enrich_limit,
        retry_limit=retry_limit,
        workers=workers,
        min_score=min_score,
        daily_cap=daily_cap,
        do_enroll=enroll_,
        do_classify=classify_,
        today=datetime.now(UTC).date(),
    )
    rows = run_steps(steps)
    table = Table("step", "status", "detail")
    colours = {"ok": "green", "skipped": "yellow", "failed": "red"}
    for name, status, detail in rows:
        table.add_row(name, f"[{colours[status]}]{status}[/]", detail)
    console.print(table)
    if any(status == "failed" for _, status, _ in rows):
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
