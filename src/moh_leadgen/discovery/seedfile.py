from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from ..domains import is_junk, normalize_domain
from ..models import Candidate, LeadSource
from .base import DiscoverySource


class SeedFileDiscovery(DiscoverySource):
    """Reads domains or URLs from a text/CSV file (one per line, '#' comments allowed)."""

    name = "seedfile"

    def __init__(self, path: Path | str, source: LeadSource, detail: dict | None = None):
        self.path = Path(path)
        self.source = source
        self.detail = detail or {}

    def available(self) -> bool:
        return self.path.exists()

    def discover(self, limit: int | None = None) -> Iterator[Candidate]:
        seen: set[str] = set()
        count = 0
        for raw in self.path.read_text(encoding="utf-8").splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = [t.strip() for t in line.split(",")]
            token = parts[0]
            domain = normalize_domain(token)
            if not domain or domain in seen or is_junk(domain):
                continue
            seen.add(domain)
            detail = dict(self.detail)
            if len(parts) > 1 and parts[1]:
                detail["platform"] = parts[1]
            yield Candidate(
                domain=domain,
                source=self.source,
                discovered_via=f"seedfile:{self.path.name}",
                source_detail=detail,
            )
            count += 1
            if limit and count >= limit:
                return
