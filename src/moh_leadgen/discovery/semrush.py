"""Semrush-powered discovery (requires SEMRUSH_API_KEY with API units).

Two angles:

* Backlinks: every site that embeds the LegitScript seal links to legitscript.com, so the
  referring domains of legitscript.com are, overwhelmingly, certified merchants.
* Advertisers: Google only lets LegitScript-certified advertisers run ads for telehealth,
  pharmacy and addiction-treatment keywords in the US, so domains bidding on those keywords
  are near-certain certification holders.
"""

from __future__ import annotations

import csv
import io
import logging
from collections.abc import Iterator

import httpx

from ..config import settings
from ..domains import is_junk, normalize_domain
from ..models import Candidate, LeadSource
from .base import DiscoverySource

log = logging.getLogger(__name__)

API = "https://api.semrush.com/"
ANALYTICS = "https://api.semrush.com/analytics/v1/"


def _rows(text: str) -> list[dict]:
    text = text.strip()
    if not text or text.startswith("ERROR"):
        if text:
            log.warning("semrush: %s", text[:200])
        return []
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    return list(reader)


class SemrushBacklinksDiscovery(DiscoverySource):
    name = "semrush_backlinks"

    def __init__(self, target: str = "legitscript.com", source: LeadSource = "legitscript", page_size: int = 500):
        self.target = target
        self.source = source
        self.page_size = page_size

    def available(self) -> bool:
        return bool(settings.semrush_key)

    def discover(self, limit: int | None = None) -> Iterator[Candidate]:
        seen: set[str] = set()
        count = 0
        offset = 0
        with httpx.Client(timeout=60) as client:
            while True:
                r = client.get(
                    ANALYTICS,
                    params={
                        "key": settings.semrush_key,
                        "type": "backlinks_refdomains",
                        "target": self.target,
                        "target_type": "root_domain",
                        "export_columns": "domain,domain_ascore,backlinks_num,first_seen,last_seen,country",
                        "display_limit": self.page_size,
                        "display_offset": offset,
                        "display_sort": "last_seen_desc",
                    },
                )
                rows = _rows(r.text)
                if not rows:
                    return
                for row in rows:
                    domain = normalize_domain(row.get("domain", ""))
                    if not domain or domain in seen or is_junk(domain):
                        continue
                    seen.add(domain)
                    yield Candidate(
                        domain=domain,
                        source=self.source,
                        discovered_via="semrush:backlinks_refdomains",
                        source_detail={
                            "target": self.target,
                            "authority_score": row.get("domain_ascore"),
                            "backlinks_to_target": row.get("backlinks_num"),
                            "last_seen": row.get("last_seen"),
                            "country": row.get("country"),
                        },
                    )
                    count += 1
                    if limit and count >= limit:
                        return
                if len(rows) < self.page_size:
                    return
                offset += self.page_size


class SemrushAdvertisersDiscovery(DiscoverySource):
    name = "semrush_advertisers"

    def __init__(self, keywords: list[str], source: LeadSource = "legitscript", database: str = "us", per_keyword: int = 100):
        self.keywords = keywords
        self.source = source
        self.database = database
        self.per_keyword = per_keyword

    def available(self) -> bool:
        return bool(settings.semrush_key)

    def discover(self, limit: int | None = None) -> Iterator[Candidate]:
        seen: set[str] = set()
        count = 0
        with httpx.Client(timeout=60) as client:
            for kw in self.keywords:
                r = client.get(
                    API,
                    params={
                        "key": settings.semrush_key,
                        "type": "phrase_adwords_historical",
                        "phrase": kw,
                        "database": self.database,
                        "export_columns": "Dn,Dt,Po,Ur,Tt,Ds",
                        "display_limit": self.per_keyword,
                    },
                )
                for row in _rows(r.text):
                    domain = normalize_domain(row.get("Dn") or row.get("Ur") or "")
                    if not domain or domain in seen or is_junk(domain):
                        continue
                    seen.add(domain)
                    yield Candidate(
                        domain=domain,
                        source=self.source,
                        discovered_via="semrush:phrase_adwords_historical",
                        source_detail={"keyword": kw, "ad_title": row.get("Tt"), "ad_url": row.get("Ur"), "position": row.get("Po")},
                    )
                    count += 1
                    if limit and count >= limit:
                        return
