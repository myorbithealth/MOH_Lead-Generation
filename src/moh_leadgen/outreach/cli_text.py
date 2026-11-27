"""`leadgen outreach text ...`: the warm SendBlue texting channel (consent-gated, never cold)."""

from __future__ import annotations

import typer
from rich.console import Console

from .cli import outreach_app

console = Console()

text_app = typer.Typer(
    help="Warm texting via SendBlue: booking reminders, inbound texts, stop handling", no_args_is_help=True
)
outreach_app.add_typer(text_app, name="text")


def _service(*, need_client: bool):
    from ..config import settings
    from .portal_io import PortalClient
    from .texting import TextService

    try:
        portal = PortalClient()
    except RuntimeError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if need_client and not (settings.sendblue_api_key_id and settings.sendblue_api_secret):
        raise typer.BadParameter("SENDBLUE_API_KEY_ID and SENDBLUE_API_SECRET_KEY are required to send texts")
    return TextService(portal, echo=console.print)


@text_app.command("reminders")
def reminders_cmd(
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the texts that are due; send nothing"),
    window_hours: float = typer.Option(26, help="How far ahead the day-before reminder may go out"),
    follow_ups: bool = typer.Option(
        False, "--follow-ups", help="Also text consenting contacts who replied interested by email"
    ),
):
    """Booking confirmation, 24 h and 1 h reminders for upcoming meetings (each sent once)."""
    service = _service(need_client=not dry_run)
    console.print(service.run_reminders(window_hours=window_hours, dry_run=dry_run))
    if follow_ups:
        console.print(service.run_follow_ups(dry_run=dry_run))


@text_app.command("send")
def send_cmd(
    to: str = typer.Option(..., "--to", help="Phone number, E.164 (+1...)"),
    body: str = typer.Option(..., "--body", help="The text, under 300 characters"),
    force_consent_recorded: str | None = typer.Option(
        None,
        "--force-consent-recorded",
        help="Where consent was given (e.g. 'phone call 2026-11-27 with Shubh'); recorded on the contact",
    ),
):
    """Send one text by hand. Needs recorded SMS consent unless --force-consent-recorded names a source."""
    from .sendblue import SendblueError

    service = _service(need_client=True)
    try:
        result = service.send_manual(to, body, consent_source=force_consent_recorded)
    except (ValueError, PermissionError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    except SendblueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(result)


@text_app.command("inbound")
def inbound_cmd(limit: int = typer.Option(200, help="Most webhook events to process this run")):
    """Classify and route inbound texts; honour stop words (sms_stop suppression + one confirmation)."""
    service = _service(need_client=False)
    console.print(service.run_inbound(limit))
