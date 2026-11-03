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

from ..discovery import CrtShDiscovery, SearchDiscovery, SeedFileDiscovery
from ..discovery.base import DiscoverySource
from .base import Tool

DEFAULT_SEED = Path("seeds/competitor_platform_seed_domains.txt")

# name -> (apex domains the platform serves tenants from, extra search dorks)
COMPETITORS: dict[str, tuple[list[str], list[str]]] = {
    "Bask Health": (
        ["bask.health", "baskhealth.com"],
        ['"powered by bask"', '"bask.health" telehealth -site:bask.health', '"baskhealth.com" -site:baskhealth.com'],
    ),
    "Rimo": (
        ["rimo.health", "rimohealth.com"],
        ['"powered by rimo" telehealth', '"rimo.health" -site:rimo.health'],
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
        sources: list[DiscoverySource] = [SeedFileDiscovery(self.seed_file or DEFAULT_SEED, source=self.source)]
        for name, (domains, dorks) in COMPETITORS.items():
            if only and only.lower() not in name.lower():
                continue
            for d in domains:
                sources.append(CrtShDiscovery(d, name, source=self.source))
            sources.append(SearchDiscovery(dorks, source=self.source, detail={"platform": name}))
        return sources
