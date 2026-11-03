from __future__ import annotations

from pathlib import Path

from ..discovery.base import DiscoverySource
from ..models import LeadSource


class Tool:
    name: str = "base"
    source: LeadSource = "manual"
    description: str = ""

    def __init__(self, seed_file: Path | None = None, **options):
        self.seed_file = seed_file
        self.options = options

    def discovery_sources(self) -> list[DiscoverySource]:
        raise NotImplementedError

    def keep_candidate(self, domain: str) -> bool:
        """Last-chance filter before a candidate enters the store."""
        return True
