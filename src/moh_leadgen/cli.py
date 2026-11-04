"""`leadgen` command-line interface."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import typer
from rich import print as rprint
from rich.console import Console
from rich.table import Table

from .config import settings
from .http import PoliteClient
from .models import Lead
from .pipeline import enrich_lead
from .scoring import score_lead
from .store import LeadStore
from .tools import TOOLS

app = typer.Typer(help="MyOrbit Health lead generation", no_args_is_help=True, add_completion=False)
console = Console()


def _store() -> LeadStore:
    return LeadStore(settings.db_path)


def _tool(name: str, seed: Path | None, opts: list[str]):
    if name not in TOOLS:
        raise typer.BadParameter(f"unknown tool {name!r}; choose from {', '.join(TOOLS)}")
    options = dict(o.split("=", 1) for o in opts if "=" in o)
    return TOOLS[name](seed_file=seed, **options)


@app.callback()
def _setup(verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging")):
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
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
        for cand in src.discover(limit=limit or None):
            if t.keep_candidate(cand.domain) and store.add_candidate(cand):
                n += 1
                added += 1
                if limit and added >= limit:
                    break
        rprint(f"[green]{src.name}[/]: {n} new candidate(s)")
        if limit and added >= limit:
            break
    rprint(f"[bold]{added}[/] new candidates stored in {settings.db_path}")


@app.command()
def enrich(
    tool: str | None = typer.Argument(None, help="Only leads from this tool (default: all)"),
    limit: int = typer.Option(25, help="How many leads to enrich this run"),
    llm: bool = typer.Option(False, help="Use Claude to extract decision-makers (needs ANTHROPIC_API_KEY)"),
    redo: bool = typer.Option(False, help="Re-enrich leads that were already enriched"),
    providers: bool = typer.Option(True, help="Use Apollo/Lusha/Hunter for decision-maker contacts when keys are set"),
    domain: str | None = typer.Option(None, help="Enrich a single domain (added if missing)"),
    workers: int = typer.Option(4, help="Sites crawled in parallel (each host is still rate-limited)"),
):
    """Crawl each lead's site and fill in owner, contacts, LegitScript evidence, tech stack, score."""
    store = _store()
    source = TOOLS[tool].source if tool else None
    if domain:
        lead = store.get(domain) or Lead(domain=domain, source=source or "manual", discovered_via="cli")
        leads = [lead]
    else:
        leads = list(store.iter(source=source, enriched=None if redo else False, limit=limit))
    if not leads:
        rprint("nothing to enrich")
        return
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _work(lead: Lead) -> Lead:
        client = PoliteClient()
        try:
            enrich_lead(lead, client, use_llm=llm, use_providers=providers)
        except Exception as exc:  # keep the batch going
            logging.getLogger(__name__).exception("enrich failed for %s", lead.domain)
            lead.errors.append(f"enrich: {exc}")
            score_lead(lead)
        finally:
            client.close()
        return lead

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(_work, lead) for lead in leads]
        for fut in as_completed(futures):
            lead = fut.result()
            store.save(lead)
            done += 1
            rprint(
                f"[{done}/{len(leads)}] {lead.domain}: score {lead.score:>3}  ls={lead.legitscript.status:<13} "
                f"vertical={lead.vertical or '-':<20} platform={lead.competitor_platform or '-'} "
                f"owner={lead.owner_name or '-'} email={lead.owner_email or lead.primary_email or '-'}"
                f"{' (' + lead.owner_email_status + ')' if lead.owner_email_status else ''} phone={lead.owner_phone or lead.primary_phone or '-'}"
            )


@app.command()
def score():
    """Re-score every lead with the current weights (no crawling)."""
    store = _store()
    n = 0
    for lead in store.iter():
        store.save(score_lead(lead))
        n += 1
    rprint(f"re-scored {n} lead(s)")


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
    leads = list(store.iter(source=source, enriched=True, min_score=min_score, unsynced_only=(target == "portal" and not all_)))
    if not leads:
        rprint("nothing to sync")
        return
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
    discover(tool, discover_limit, seed, opt)
    enrich(tool, enrich_limit, llm, False, True, None, 4)
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
            str(lead.score), lead.domain, (lead.company_name or "")[:28], lead.vertical or "", lead.legitscript.status,
            f"{lead.owner_name or ''} {('(' + lead.owner_title + ')') if lead.owner_title else ''}"[:34],
            lead.owner_email or lead.primary_email or "", lead.primary_phone or "", lead.competitor_platform or "",
        )
    console.print(t)


@app.command()
def promote(domain: str, owner: str | None = typer.Option(None, help="Portal user id to own the records")):
    """Create company + contact + deal in the Sales Portal from a lead."""
    from .sinks.supabase import SupabaseSink

    result = SupabaseSink().promote(domain, owner)
    console.print_json(json.dumps(result, indent=2))


if __name__ == "__main__":
    app()
