"""Fetch a prospect's homepage plus the handful of pages where contact/owner info lives."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ..config import settings
from ..http import PoliteClient

log = logging.getLogger(__name__)

# Paths worth visiting, in priority order. Matched against the link path (case-insensitive).
INTERESTING_PATHS = [
    r"contact",
    r"about",
    r"our-story|our_story|story",
    r"team|our-team|meet-the-team|staff|providers|doctors|physicians",
    r"leadership|founder|founders|owner|management",
    r"locations?|clinics?",
    r"press|media|news",
    r"privacy|terms",
]
_PATH_RE = [re.compile(p, re.IGNORECASE) for p in INTERESTING_PATHS]


@dataclass
class Page:
    url: str
    html: str
    soup: BeautifulSoup
    text: str


@dataclass
class SiteSnapshot:
    domain: str
    final_url: str
    pages: list[Page] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def homepage(self) -> Page | None:
        return self.pages[0] if self.pages else None

    def all_text(self) -> str:
        return "\n\n".join(p.text for p in self.pages)

    def all_html(self) -> str:
        return "\n".join(p.html for p in self.pages)


def _to_text(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
        tag.decompose()
    text = soup.get_text("\n")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()


def _parse(url: str, html: str) -> Page:
    soup = BeautifulSoup(html, "lxml")
    text = _to_text(BeautifulSoup(html, "lxml"))
    return Page(url=url, html=html, soup=soup, text=text)


def _rank_links(base_url: str, soup: BeautifulSoup) -> list[str]:
    base_host = urlsplit(base_url).netloc.lower().removeprefix("www.")
    ranked: list[tuple[int, str]] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a["href"].strip())
        parts = urlsplit(href)
        if parts.scheme not in ("http", "https"):
            continue
        if parts.netloc.lower().removeprefix("www.") != base_host:
            continue
        clean = parts._replace(fragment="", query="").geturl()
        if clean in seen or clean.rstrip("/") == base_url.rstrip("/"):
            continue
        path = parts.path.lower()
        if re.search(r"\.(pdf|jpg|jpeg|png|gif|svg|zip|mp4|css|js)$", path):
            continue
        for rank, rx in enumerate(_PATH_RE):
            if rx.search(path) or rx.search(a.get_text(" ", strip=True).lower()):
                ranked.append((rank, clean))
                seen.add(clean)
                break
    ranked.sort()
    return [u for _, u in ranked]


def crawl_site(domain: str, client: PoliteClient, *, max_pages: int | None = None) -> SiteSnapshot:
    max_pages = max_pages or settings.max_pages_per_site
    snap = SiteSnapshot(domain=domain, final_url=f"https://{domain}/")

    resp = None
    for candidate in (f"https://{domain}/", f"https://www.{domain}/", f"http://{domain}/"):
        resp = client.get(candidate)
        if resp is not None and resp.status_code < 400 and resp.text:
            break
        resp = None
    if resp is None:
        snap.errors.append("homepage unreachable")
        return snap

    snap.final_url = str(resp.url)
    home = _parse(snap.final_url, resp.text)
    snap.pages.append(home)

    for url in _rank_links(snap.final_url, home.soup)[: max_pages - 1]:
        sub = client.get(url)
        if sub is None or sub.status_code >= 400:
            continue
        if "html" not in sub.headers.get("content-type", ""):
            continue
        snap.pages.append(_parse(str(sub.url), sub.text))
    log.info("%s: crawled %d page(s)", domain, len(snap.pages))
    return snap
