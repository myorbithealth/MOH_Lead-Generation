from __future__ import annotations

import csv
from pathlib import Path

from ..models import Lead

COLUMNS = [
    "score",
    "domain",
    "company_name",
    "vertical",
    "legitscript_status",
    "owner_name",
    "owner_title",
    "owner_email",
    "owner_email_status",
    "owner_phone",
    "owner_linkedin",
    "primary_email",
    "primary_phone",
    "phones",
    "emails",
    "city",
    "state",
    "competitor_platform",
    "tech_stack",
    "website",
    "booking_link",
    "source",
    "discovered_via",
    "score_reasons",
]


def write_csv(leads: list[Lead], path: Path | str) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        for lead in leads:
            row = lead.to_portal_row()
            row["phones"] = "; ".join(lead.phones)
            row["emails"] = "; ".join(lead.emails)
            row["tech_stack"] = "; ".join(lead.tech_stack)
            row["score_reasons"] = " | ".join(lead.score_reasons)
            w.writerow({k: row.get(k, "") for k in COLUMNS})
    return len(leads)
