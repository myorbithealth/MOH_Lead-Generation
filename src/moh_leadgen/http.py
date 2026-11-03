"""A polite HTTP client: identifies itself, honours robots.txt, rate-limits per host, retries gently.

Everything the toolkit fetches goes through this so we never hammer a prospect's site.
"""

from __future__ import annotations

import logging
import time
import urllib.robotparser
from urllib.parse import urlsplit

import httpx

from .config import settings

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 3_000_000


class PoliteClient:
    def __init__(
        self,
        *,
        user_agent: str | None = None,
        min_delay: float | None = None,
        respect_robots: bool | None = None,
        timeout: float | None = None,
    ):
        self.user_agent = user_agent or settings.user_agent
        self.min_delay = settings.min_delay_seconds if min_delay is None else min_delay
        self.respect_robots = settings.respect_robots if respect_robots is None else respect_robots
        self._last_hit: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._client = httpx.Client(
            headers={
                "User-Agent": self.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.8",
            },
            follow_redirects=True,
            timeout=timeout or settings.request_timeout,
        )

    # ------------------------------------------------------------------ robots
    def _robots_for(self, url: str) -> urllib.robotparser.RobotFileParser | None:
        parts = urlsplit(url)
        host = parts.netloc.lower()
        if host in self._robots:
            return self._robots[host]
        rp = urllib.robotparser.RobotFileParser()
        robots_url = f"{parts.scheme}://{host}/robots.txt"
        try:
            resp = self._client.get(robots_url)
            if resp.status_code >= 400:
                self._robots[host] = None
                return None
            rp.parse(resp.text.splitlines())
            self._robots[host] = rp
        except httpx.HTTPError:
            self._robots[host] = None
        return self._robots[host]

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        rp = self._robots_for(url)
        if rp is None:
            return True
        return rp.can_fetch(self.user_agent, url) and rp.can_fetch("*", url)

    # ------------------------------------------------------------------ fetch
    def _throttle(self, url: str) -> None:
        host = urlsplit(url).netloc.lower()
        last = self._last_hit.get(host)
        if last is not None:
            wait = self.min_delay - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_hit[host] = time.monotonic()

    def get(self, url: str, *, retries: int = 2, **kwargs) -> httpx.Response | None:
        """GET a URL. Returns None when disallowed, unreachable, or persistently failing."""
        if not self.allowed(url):
            log.info("robots.txt disallows %s", url)
            return None
        backoff = 2.0
        for attempt in range(retries + 1):
            self._throttle(url)
            try:
                resp = self._client.get(url, **kwargs)
            except httpx.HTTPError as exc:
                log.debug("GET %s failed: %s", url, exc)
                if attempt == retries:
                    return None
                time.sleep(backoff)
                backoff *= 2
                continue
            if resp.status_code in (429, 502, 503, 504) and attempt < retries:
                retry_after = resp.headers.get("Retry-After")
                time.sleep(float(retry_after) if retry_after and retry_after.isdigit() else backoff)
                backoff *= 2
                continue
            if len(resp.content) > MAX_BODY_BYTES:
                log.debug("GET %s body too large (%d bytes)", url, len(resp.content))
                return None
            return resp
        return None

    def get_text(self, url: str) -> str | None:
        resp = self.get(url)
        if resp is None or resp.status_code >= 400:
            return None
        ctype = resp.headers.get("content-type", "")
        if "html" not in ctype and "text" not in ctype and "json" not in ctype and "xml" not in ctype:
            return None
        return resp.text

    def get_json(self, url: str, **kwargs):
        resp = self.get(url, **kwargs)
        if resp is None or resp.status_code >= 400:
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    def close(self) -> None:
        self._client.close()
