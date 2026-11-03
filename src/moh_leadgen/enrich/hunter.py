"""Hunter.io domain search (optional). Returns named mailboxes with positions when available."""

from __future__ import annotations

import logging

import httpx

from ..config import settings
from ..models import Person
from .extract import DECISION_RE

log = logging.getLogger(__name__)


def domain_search(domain: str) -> tuple[list[Person], list[str], str | None]:
    """Returns (people, generic emails, detected pattern like '{first}.{last}')."""
    if not settings.hunter_key:
        return [], [], None
    try:
        with httpx.Client(timeout=30) as client:
            r = client.get(
                "https://api.hunter.io/v2/domain-search",
                params={"domain": domain, "api_key": settings.hunter_key, "limit": 25},
            )
            if r.status_code != 200:
                log.warning("hunter %s: %s", r.status_code, r.text[:200])
                return [], [], None
            data = r.json().get("data", {})
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("hunter failed for %s: %s", domain, exc)
        return [], [], None

    people: list[Person] = []
    generic: list[str] = []
    for e in data.get("emails", []):
        if e.get("type") == "generic" or not (e.get("first_name") and e.get("last_name")):
            if e.get("value"):
                generic.append(e["value"].lower())
            continue
        title = e.get("position")
        people.append(
            Person(
                name=f"{e['first_name']} {e['last_name']}",
                title=title,
                email=e.get("value", "").lower() or None,
                linkedin=e.get("linkedin"),
                phone=e.get("phone_number"),
                source_url="hunter.io",
                confidence=min(1.0, (e.get("confidence") or 50) / 100),
                is_decision_maker=bool(title and DECISION_RE.search(title)),
            )
        )
    return people, generic, data.get("pattern")
