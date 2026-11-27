"""Reply-side commands: classify, route, report, learn."""

from __future__ import annotations

import json

import typer
from rich.console import Console
from rich.table import Table

from .cli import outreach_app

console = Console()


def _portal():
    from .portal_io import PortalClient

    try:
        return PortalClient()
    except RuntimeError as exc:
        raise typer.BadParameter(str(exc)) from exc


@outreach_app.command("classify")
def classify_cmd(
    limit: int = typer.Option(200, help="Most webhook events to process this run"),
    text: str | None = typer.Option(
        None, "--text", help="Classify this reply text only (dry run, prints JSON)"
    ),
    subject: str | None = typer.Option(None, help="With --text: the subject of our last email"),
    body: str | None = typer.Option(None, help="With --text: the body of our last email"),
):
    """Classify unprocessed Smartlead replies in the portal (or one --text reply as a dry run)."""
    from ..config import settings

    if not settings.anthropic_key:
        raise typer.BadParameter("ANTHROPIC_API_KEY is required to classify replies")
    if text is not None:
        from .classify import classify_reply_with_model

        result, model = classify_reply_with_model(text, subject, body, {})
        console.print_json(json.dumps({"model": model, **result.model_dump(mode="json")}))
        return
    from .process_replies import process_replies

    stats = process_replies(_portal(), limit)
    console.print(stats)


@outreach_app.command("route")
def route_cmd(
    domain: str = typer.Option(..., help="Lead domain"),
    disposition: str = typer.Option(..., help="A Disposition value, e.g. interested"),
    contact_id: str | None = typer.Option(None, help="Portal contact id for the booking link"),
    offline: bool = typer.Option(False, help="Use the built-in rules without calling the portal"),
):
    """Print which rep a reply would go to, and why."""
    from .models import Disposition
    from .routing import route

    try:
        Disposition(disposition.lower())
    except ValueError as exc:
        raise typer.BadParameter(f"unknown disposition {disposition!r}") from exc
    portal = None if offline else _portal()
    console.print_json(route(domain, disposition.lower(), portal, contact_id=contact_id).model_dump_json())


@outreach_app.command("report")
def report_cmd(
    days: int = typer.Option(30, help="Window in days (0 = all time, from the outreach_funnel view)"),
):
    """Funnel per segment / variant / step with positive rate and P(best)."""
    from .metrics import funnel, report_rows

    portal = _portal()
    names = {v["id"]: v["name"] for v in portal.select_all("variants", columns="id,name")}
    table = Table(title=f"Outreach funnel ({'all time' if not days else f'last {days} days'})")
    for col in (
        "segment",
        "variant",
        "step",
        "sends",
        "replies",
        "positive",
        "meetings",
        "held",
        "pos rate",
        "P(best)",
    ):
        table.add_column(col, justify="left" if col in ("segment", "variant") else "right")
    for r in report_rows(funnel(portal, days or None), names):
        table.add_row(
            str(r.get("segment") or "-"),
            str(r["variant"]),
            str(r.get("sequence_step") or "-"),
            str(r.get("sends") or 0),
            str(r.get("replies") or 0),
            str(r.get("positive_replies") or 0),
            str(r.get("meetings") or 0),
            str(r.get("held") or 0),
            f"{r['positive_rate']:.1%}",
            "-" if r["p_best"] is None else f"{r['p_best']:.2f}",
        )
    console.print(table)


@outreach_app.command("learn")
def learn_cmd(
    propose: int = typer.Option(3, help="New step-1 variants to propose per segment (0 = none)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show decisions and proposals; write nothing"),
    activate: bool = typer.Option(False, "--activate", help="Insert proposals as active instead of proposed"),
):
    """Weekly loop: retire/promote variants and ask Claude for new step-1 variants."""
    from .metrics import learn

    report = learn(_portal(), propose=propose, dry_run=dry_run, activate=activate)
    for vid, action, reason in report["decisions"]:
        if action != "keep":
            console.print(f"[bold]{action}[/bold] {vid}: {reason}")
    for row in report["proposed"]:
        console.print(f"[green]proposed[/green] {row['segment']}/{row['name']}: {row['hypothesis']}")
    for note in report["rejected"]:
        console.print(f"[yellow]rejected[/yellow] {note}")
    suffix = " (dry run, nothing written)" if dry_run else f"; {report['applied']} status change(s) applied"
    console.print(f"{len(report['proposed'])} proposal(s){suffix}")
