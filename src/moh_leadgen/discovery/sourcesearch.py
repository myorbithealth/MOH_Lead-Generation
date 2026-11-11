"""Source-code search engines: find every site whose HTML contains a platform marker.

This is the most direct "who runs on competitor X" technique. Tenant storefronts on custom domains
look like any other Vercel/Cloudflare site at the DNS layer, but their HTML always loads the
platform's assets (e.g. Rimo tenants load `larp-cdn.com`). Source-code search engines index that.

* PublicWWW  - https://publicwww.com  (API key on paid plans; `export=csvu` returns one URL per line)
* NerdyData  - https://www.nerdydata.com (API key; JSON)

Both are optional; the tool skips them without keys.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import httpx

from ..config import settings
from ..domains import is_junk, normalize_domain
from ..models import Candidate, LeadSource
from .base import DiscoverySource

log = logging.getLogger(__name__)


class PublicWWWDiscovery(DiscoverySource):
    name = "publicwww"

    def __init__(self, markers: dict[str, str], source: LeadSource = "competitor_platform"):
        """markers: {platform name: source snippet to search for}"""
        self.markers = markers
        self.source = source

    def available(self) -> bool:
        return bool(settings.publicwww_key)

    def discover(self, limit: int | None = None) -> Iterator[Candidate]:
        seen: set[str] = set()
        count = 0
        with httpx.Client(timeout=120, follow_redirects=True) as client:
            for platform, marker in self.markers.items():
                url = f"https://publicwww.com/websites/%22{marker}%22/"
                try:
                    r = client.get(url, params={"export": "csvu", "key": settings.publicwww_key})
                except httpx.HTTPError as exc:
                    log.warning("publicwww failed for %s: %s", marker, exc)
                    continue
                if r.status_code != 200:
                    log.warning("publicwww %s for %s: %s", r.status_code, marker, r.text[:150])
                    continue
                for line in r.text.splitlines():
                    domain = normalize_domain(line.strip().split(",")[0])
                    if not domain or domain in seen or is_junk(domain):
                        continue
                    seen.add(domain)
                    yield Candidate(
                        domain=domain,
                        source=self.source,
                        discovered_via="publicwww",
                        source_detail={"platform": platform, "marker": marker},
                    )
                    count += 1
                    if limit and count >= limit:
                        return


class NerdyDataDiscovery(DiscoverySource):
    name = "nerdydata"

    def __init__(self, markers: dict[str, str], source: LeadSource = "competitor_platform"):
        self.markers = markers
        self.source = source

    def available(self) -> bool:
        return bool(settings.nerdydata_key)

    def discover(self, limit: int | None = None) -> Iterator[Candidate]:
        seen: set[str] = set()
        count = 0
        with httpx.Client(
            timeout=120, headers={"Authorization": f"Bearer {settings.nerdydata_key}"}
        ) as client:
            for platform, marker in self.markers.items():
                try:
                    r = client.get(
                        "https://api.nerdydata.com/v1/search", params={"query": marker, "limit": 1000}
                    )
                except httpx.HTTPError as exc:
                    log.warning("nerdydata failed for %s: %s", marker, exc)
                    continue
                if r.status_code != 200:
                    log.warning("nerdydata %s for %s", r.status_code, marker)
                    continue
                for row in r.json().get("results", r.json().get("data", [])):
                    domain = normalize_domain(row.get("domain") or row.get("url") or "")
                    if not domain or domain in seen or is_junk(domain):
                        continue
                    seen.add(domain)
                    yield Candidate(
                        domain=domain,
                        source=self.source,
                        discovered_via="nerdydata",
                        source_detail={"platform": platform, "marker": marker},
                    )
                    count += 1
                    if limit and count >= limit:
                        return
