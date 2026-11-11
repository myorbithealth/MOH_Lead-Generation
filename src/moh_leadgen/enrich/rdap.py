"""Registration data via RDAP (the modern WHOIS). Registrant details are usually redacted,
but registrar + creation date are reliable and useful for scoring (business age)."""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)


def lookup(domain: str, timeout: float = 15) -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        with httpx.Client(
            timeout=timeout, follow_redirects=True, headers={"Accept": "application/rdap+json"}
        ) as client:
            r = client.get(f"https://rdap.org/domain/{domain}")
            if r.status_code != 200:
                return out
            data = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.debug("rdap %s: %s", domain, exc)
        return out

    for ev in data.get("events", []):
        if ev.get("eventAction") == "registration":
            out["created"] = ev.get("eventDate")
        elif ev.get("eventAction") == "expiration":
            out["expires"] = ev.get("eventDate")
    for ent in data.get("entities", []):
        roles = ent.get("roles", [])
        vcard = ent.get("vcardArray", [None, []])[1] if ent.get("vcardArray") else []
        fields: dict[str, str] = {}
        for item in vcard:
            if not isinstance(item, list) or len(item) < 4:
                continue
            key, val = item[0], item[3]
            if (
                key in ("fn", "org", "email", "tel")
                and isinstance(val, str)
                and val
                and "redacted" not in val.lower()
            ):
                fields[key] = val
        if "registrar" in roles:
            out["registrar"] = fields.get("fn") or fields.get("org")
        if "registrant" in roles and fields:
            out["registrant"] = fields
    out["status"] = data.get("status", [])
    return out
