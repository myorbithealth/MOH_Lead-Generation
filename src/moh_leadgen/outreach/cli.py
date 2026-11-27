"""`leadgen outreach ...` command group. Commands live in cli_enroll.py and cli_replies.py."""

from __future__ import annotations

import typer

outreach_app = typer.Typer(
    help="Outbound: enrol leads into Smartlead, classify replies, route, learn", no_args_is_help=True
)

# register commands (each module decorates functions with @outreach_app.command())
from . import cli_enroll, cli_replies, cli_text  # noqa: F401
