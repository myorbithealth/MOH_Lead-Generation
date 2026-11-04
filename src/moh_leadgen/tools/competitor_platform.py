"""Tool 2: businesses running on a competing telehealth platform (Bask Health, Rimo, Healthie...).

Signals we use ("reverse lookup"):
  * certificate transparency - tenants on platform subdomains (brand.platform.com)
  * search dorks             - "powered by <platform>", widget/script URLs in page source
  * seed lists               - case studies, customer logos, app-store listings
  * DNS CNAME / HTML fingerprints during enrichment confirm the platform and catch
    custom-domain tenants (checkout.brand.com -> platform CNAME).

Add a platform by appending to COMPETITORS; the fingerprint lives in enrich/tech.py.
"""

from __future__ import annotations

from pathlib import Path

from ..discovery import (
    CrtShDiscovery,
    NerdyDataDiscovery,
    PublicWWWDiscovery,
    SearchDiscovery,
    SeedFileDiscovery,
)
from ..discovery.base import DiscoverySource
from .base import Tool

DEFAULT_SEED = Path("seeds/competitor_platform_seed_domains.txt")

# Source-code markers unique to each platform's storefront HTML (what source-search engines look for).
SOURCE_MARKERS: dict[str, str] = {
    "Rimo": "larp-cdn.com",
    "Bask Health": "analytics.bask.health",
    "Healthie": "secure.gethealthie.com",
    "OpenLoop": "openloophealth.com",
    "Beluga Health": "belugahealth.com",
    "MD Integrations": "mdintegrations.com",
    "Qualiphy": "qualiphy.me",
}

# name -> (apex domains the platform serves tenants from, extra search dorks)
COMPETITORS: dict[str, tuple[list[str], list[str]]] = {
    "Bask Health": (
        ["bask.health"],
        ['"powered by bask"', '"analytics.bask.health"', '"checkout.bask.health"', '"bask.health" telehealth -site:bask.health'],
    ),
    "Rimo": (
        ["rimo.health", "rimo.co"],
        ['"larp-cdn.com"', '"powered by rimo" telehealth', '"rimo.health" -site:rimo.health -site:rimo.co'],
    ),
    "Healthie": (
        ["gethealthie.com"],
        ['"secure.gethealthie.com" -site:gethealthie.com', '"powered by healthie"'],
    ),
    "OpenLoop": (["openloophealth.com"], ['"openloop" telehealth partner -site:openloophealth.com']),
    "Beluga Health": (["belugahealth.com"], ['"beluga health" telehealth partner -site:belugahealth.com']),
    "MD Integrations": (["mdintegrations.com"], ['"mdintegrations" -site:mdintegrations.com']),
    "Qualiphy": (["qualiphy.me"], ['"qualiphy" "good faith exam" -site:qualiphy.me']),
    "Wheel": (["wheel.com"], ['"wheel" "virtual care" powered -site:wheel.com']),
}


class CompetitorPlatformTool(Tool):
    name = "competitor"
    source = "competitor_platform"
    description = "Brands already running on competing telehealth infrastructure"

    def discovery_sources(self) -> list[DiscoverySource]:
        only = self.options.get("platform")
        markers = {k: v for k, v in SOURCE_MARKERS.items() if not only or only.lower() in k.lower()}
        sources: list[DiscoverySource] = [
            SeedFileDiscovery(self.seed_file or DEFAULT_SEED, source=self.source),
            PublicWWWDiscovery(markers, source=self.source),
            NerdyDataDiscovery(markers, source=self.source),
        ]
        for name, (domains, dorks) in COMPETITORS.items():
            if only and only.lower() not in name.lower():
                continue
            for d in domains:
                sources.append(CrtShDiscovery(d, name, source=self.source))
            sources.append(SearchDiscovery(dorks, source=self.source, detail={"platform": name}))
        return sources
