"""Search-engine discovery.

Supports three providers (whichever key is configured): SerpAPI, Google Programmable Search
(Custom Search JSON API) and Bing Web Search. Each tool supplies its own list of "dorks" -
queries that surface the sites we want, e.g. pages that embed the LegitScript seal.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass

import httpx

from ..config import settings
from ..domains import is_junk, normalize_domain
from ..models import Candidate, LeadSource
from .base import DiscoverySource

log = logging.getLogger(__name__)


@dataclass
class SearchHit:
    url: str
    title: str = ""
    snippet: str = ""


class SearchProvider:
    name = "none"

    def search(self, query: str, *, max_results: int = 30) -> list[SearchHit]:
        raise NotImplementedError


class SerpApiProvider(SearchProvider):
    name = "serpapi"

    def __init__(self, key: str):
        self.key = key

    def search(self, query: str, *, max_results: int = 30) -> list[SearchHit]:
        hits: list[SearchHit] = []
        start = 0
        with httpx.Client(timeout=30) as client:
            while len(hits) < max_results:
                r = client.get(
                    "https://serpapi.com/search.json",
                    params={"engine": "google", "q": query, "num": 20, "start": start, "api_key": self.key, "gl": "us", "hl": "en"},
                )
                if r.status_code != 200:
                    log.warning("serpapi %s: %s", r.status_code, r.text[:200])
                    break
                organic = r.json().get("organic_results") or []
                if not organic:
                    break
                hits.extend(SearchHit(o.get("link", ""), o.get("title", ""), o.get("snippet", "")) for o in organic)
                start += len(organic)
        return hits[:max_results]


class GoogleCseProvider(SearchProvider):
    name = "google_cse"

    def __init__(self, key: str, cx: str):
        self.key, self.cx = key, cx

    def search(self, query: str, *, max_results: int = 30) -> list[SearchHit]:
        hits: list[SearchHit] = []
        start = 1
        with httpx.Client(timeout=30) as client:
            while len(hits) < max_results and start <= 91:
                r = client.get(
                    "https://www.googleapis.com/customsearch/v1",
                    params={"key": self.key, "cx": self.cx, "q": query, "num": 10, "start": start},
                )
                if r.status_code != 200:
                    log.warning("google cse %s: %s", r.status_code, r.text[:200])
                    break
                items = r.json().get("items") or []
                if not items:
                    break
                hits.extend(SearchHit(i.get("link", ""), i.get("title", ""), i.get("snippet", "")) for i in items)
                start += 10
        return hits[:max_results]


class BingProvider(SearchProvider):
    name = "bing"

    def __init__(self, key: str):
        self.key = key

    def search(self, query: str, *, max_results: int = 30) -> list[SearchHit]:
        hits: list[SearchHit] = []
        offset = 0
        with httpx.Client(timeout=30, headers={"Ocp-Apim-Subscription-Key": self.key}) as client:
            while len(hits) < max_results:
                r = client.get(
                    "https://api.bing.microsoft.com/v7.0/search",
                    params={"q": query, "count": 50, "offset": offset, "mkt": "en-US"},
                )
                if r.status_code != 200:
                    log.warning("bing %s: %s", r.status_code, r.text[:200])
                    break
                pages = (r.json().get("webPages") or {}).get("value") or []
                if not pages:
                    break
                hits.extend(SearchHit(p.get("url", ""), p.get("name", ""), p.get("snippet", "")) for p in pages)
                offset += len(pages)
        return hits[:max_results]


def build_search_provider() -> SearchProvider | None:
    if settings.serpapi_key:
        return SerpApiProvider(settings.serpapi_key)
    if settings.google_cse_key and settings.google_cse_cx:
        return GoogleCseProvider(settings.google_cse_key, settings.google_cse_cx)
    if settings.bing_key:
        return BingProvider(settings.bing_key)
    return None


class SearchDiscovery(DiscoverySource):
    name = "search"

    def __init__(
        self,
        queries: list[str],
        source: LeadSource,
        provider: SearchProvider | None = None,
        per_query: int = 30,
        detail: dict | None = None,
    ):
        self.queries = queries
        self.source = source
        self.provider = provider or build_search_provider()
        self.per_query = per_query
        self.detail = detail or {}

    def available(self) -> bool:
        return self.provider is not None

    def discover(self, limit: int | None = None) -> Iterator[Candidate]:
        assert self.provider is not None
        seen: set[str] = set()
        count = 0
        for q in self.queries:
            try:
                hits = self.provider.search(q, max_results=self.per_query)
            except httpx.HTTPError as exc:
                log.warning("search failed for %r: %s", q, exc)
                continue
            for hit in hits:
                domain = normalize_domain(hit.url)
                if not domain or domain in seen or is_junk(domain):
                    continue
                seen.add(domain)
                yield Candidate(
                    domain=domain,
                    source=self.source,
                    discovered_via=f"search:{self.provider.name}",
                    source_detail={**self.detail, "query": q, "title": hit.title, "snippet": hit.snippet[:300], "url": hit.url},
                )
                count += 1
                if limit and count >= limit:
                    return
