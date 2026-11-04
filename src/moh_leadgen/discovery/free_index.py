"""Key-free LegitScript discovery from public web-archive indexes.

Every certified merchant links to `legitscript.com/websites/?checker_keywords=<their domain>` from
the seal. Web archives crawl those outbound links, so their URL indexes are a free, continuously
updated list of seal holders:

* Wayback Machine CDX API   - https://web.archive.org/cdx/search/cdx
* Common Crawl index API    - https://index.commoncrawl.org/<crawl>-index

Neither needs an account. Both are rate-limited; we page politely and stop early at `limit`.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator

import httpx

from ..domains import is_junk, normalize_domain
from ..models import Candidate, LeadSource
from .base import DiscoverySource

log = logging.getLogger(__name__)

CHECKER_RE = re.compile(r"checker_keywords=([A-Za-z0-9.\-]+)", re.IGNORECASE)
LEGACY_RE = re.compile(r"legitscript\.com/(?:pharmacy|websites)/([A-Za-z0-9.\-]+\.[A-Za-z]{2,})/?(?:$|[?#])", re.IGNORECASE)

WAYBACK_CDX = "https://web.archive.org/cdx/search/cdx"
CC_COLLINFO = "https://index.commoncrawl.org/collinfo.json"


def domains_from_urls(urls: Iterator[str] | list[str]) -> Iterator[tuple[str, str]]:
    """Yield (domain, matched_url) for every LegitScript verify URL."""
    seen: set[str] = set()
    for url in urls:
        m = CHECKER_RE.search(url) or LEGACY_RE.search(url)
        if not m:
            continue
        domain = normalize_domain(m.group(1))
        if not domain or domain in seen or is_junk(domain):
            continue
        seen.add(domain)
        yield domain, url


class WaybackDiscovery(DiscoverySource):
    name = "wayback_cdx"

    def __init__(self, source: LeadSource = "legitscript", page_size: int = 5000, max_pages: int = 20):
        self.source, self.page_size, self.max_pages = source, page_size, max_pages

    def _urls(self) -> Iterator[str]:
        patterns = ("legitscript.com/websites/*", "legitscript.com/pharmacy/*")
        with httpx.Client(timeout=120, headers={"User-Agent": "MyOrbitHealth-LeadResearch/0.1"}) as client:
            for pattern in patterns:
                for page in range(self.max_pages):
                    try:
                        r = client.get(
                            WAYBACK_CDX,
                            params={
                                "url": pattern, "output": "json", "fl": "original", "collapse": "urlkey",
                                "limit": self.page_size, "page": page, "filter": "statuscode:200",
                            },
                        )
                    except httpx.HTTPError as exc:
                        log.warning("wayback cdx failed: %s", exc)
                        return
                    if r.status_code != 200 or not r.text.strip():
                        break
                    try:
                        rows = r.json()
                    except ValueError:
                        break
                    body = rows[1:] if rows and rows[0] == ["original"] else rows
                    if not body:
                        break
                    for row in body:
                        yield row[0]
                    if len(body) < self.page_size:
                        break

    def discover(self, limit: int | None = None) -> Iterator[Candidate]:
        for n, (domain, url) in enumerate(domains_from_urls(self._urls()), 1):
            yield Candidate(domain=domain, source=self.source, discovered_via="wayback_cdx", source_detail={"verify_url": url})
            if limit and n >= limit:
                return


class CommonCrawlDiscovery(DiscoverySource):
    name = "commoncrawl"

    def __init__(self, source: LeadSource = "legitscript", crawls: int = 4):
        self.source, self.crawls = source, crawls

    def _urls(self) -> Iterator[str]:
        with httpx.Client(timeout=120, headers={"User-Agent": "MyOrbitHealth-LeadResearch/0.1"}) as client:
            try:
                collections = client.get(CC_COLLINFO).json()
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("commoncrawl collinfo failed: %s", exc)
                return
            for coll in collections[: self.crawls]:
                api = coll.get("cdx-api")
                if not api:
                    continue
                for pattern in ("legitscript.com/websites/*", "legitscript.com/pharmacy/*"):
                    try:
                        r = client.get(api, params={"url": pattern, "output": "json", "fl": "url", "filter": "=status:200"})
                    except httpx.HTTPError as exc:
                        log.warning("commoncrawl %s failed: %s", coll.get("id"), exc)
                        continue
                    if r.status_code != 200:
                        continue
                    for line in r.text.splitlines():
                        try:
                            yield json.loads(line)["url"]
                        except (ValueError, KeyError):
                            continue

    def discover(self, limit: int | None = None) -> Iterator[Candidate]:
        for n, (domain, url) in enumerate(domains_from_urls(self._urls()), 1):
            yield Candidate(domain=domain, source=self.source, discovered_via="commoncrawl", source_detail={"verify_url": url})
            if limit and n >= limit:
                return
