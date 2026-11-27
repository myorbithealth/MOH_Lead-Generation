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
from ..enrich.tech import PLATFORM_ALIASES, canonical_platform
from .base import Tool

__all__ = ["COMPETITORS", "PLATFORM_ALIASES", "PLATFORM_OWNED", "SOURCE_MARKERS", "CompetitorPlatformTool"]

DEFAULT_SEED = Path("seeds/competitor_platform_seed_domains.txt")

# Domains owned by the platforms themselves (marketing, demo, staging, status). Never leads.
PLATFORM_OWNED = {
    "bask.health",
    "baskhealth.com",
    "mybaskhealth.com",
    "bask.host",
    "dev-bask.com",
    "staging-bask.com",
    "bask.blue",
    "bask.network",
    "bask.works",
    "bask.plus",
    "bask.fyi",
    "dorf.today",
    "rimo.co",
    "rimo.health",
    "rimohealth.com",
    "getrimo.com",
    "larp-cdn.com",
    "gethealthie.com",
    "openloophealth.com",
    "belugahealth.com",
    "belugacareapp.online",
    "yourbelugaapp.com",
    "mdintegrations.com",
    "qualiphy.me",
    "doctegrity.com",
    "doctegrity.vip",
    "doctegrityportal.com",
    "steadymd.com",
    "wheel.com",
    "techstars.com",
    "fusehealth.com",
    "fusehealth.fit",
    "tryfuse.co",
    "carevalidate.com",
    "telegramd.com",
    "legal-telegramd.com",
    "whitelabelmd.com",
    "karpahealth.com",
    "karpa.health",
    "withkarpa.com",
    "cuvo.co",
    "rxave.health",
    # Med spa / DPC / white-label telehealth vendors (docs/tools/competitor.md, verified 2026-11-27).
    # Tenant subdomains (brand.akutehealth.com, brand.joinasher.com, ...) are platform-hosted and
    # get re-keyed to the brand's own domain like brand.rimo.health.
    "akutehealth.com",
    "canvasmedical.com",
    "joinmoxie.com",
    "zenoti.com",
    "aestheticrecord.com",
    "myaestheticrecord.com",
    "welliemd.com",
    "wizlo.com",
    "wizlo.health",
    "joinasher.com",
    "asherhealth.com",
    "wellsync.com",
    "upscript.com",
    "upscripthealth.com",
    "upscripthealth.org",
    "hydreight.com",
    "vsdhone.com",
    # Vendor sites confirmed, but no tenant marker verified yet (no fingerprint): still never leads.
    "spakinect.com",
    "remedora.com",
    "leguprx.com",
    "nimbusrx.io",
    "nimbusrx.com",
    "altrohealth.com",
    "altroapp.com",
    "rxpnow.com",
    "outfitmd.com",
}

# Source-code markers unique to each platform's storefront HTML (what source-search engines look for).
# Several per platform: one string rarely finds every tenant (see docs/tools/competitor.md for how
# each was verified). Keys are canonical names from enrich.tech.PLATFORM_ALIASES.
SOURCE_MARKERS: dict[str, list[str]] = {
    "Rimo": ["larp-cdn.com", "rimo-health"],
    "Fuse Health": [".fusehealth.com"],
    "CareValidate": [
        "carevalidate.com",
        "care360-next",
        "careValidateApiUrl",
        "careglp",
        "primaryMedicationSubtitle1Text",
    ],
    "Telegra MD": ["telemdnow", "telegraApp-script.js"],
    "WhiteLabel MD": ["intake_placement iframe", "api.whitelabelmd.com"],
    # Most Karpa tenants fulfil through RxAve, which is the better search key.
    "Karpa": ["karpahealth.com", "rxave.health"],
    "Cuvo": ["Medstra Inc DBA Cuvo Health", "/images/products/tirzepatide-bottle.png"],
    "Bask Health": ["analytics.bask.health", "data-basky"],
    "Healthie": ["secure.gethealthie.com"],
    "OpenLoop": ["openloophealth.com"],
    "Beluga Health": ["belugahealth.com"],
    "MD Integrations": ["mdintegrations.com"],
    "Qualiphy": ["qualiphy.me"],
    "Aesthetic Record": ["myaestheticrecord.com/online-booking"],
    "Akute Health": ["akutehealth.com/appointments", "portal.akutehealth.com"],
    "Canvas Medical": ["canvas-pcp-assets"],
    "Moxie": ["app.joinmoxie.com/booking", "app.joinmoxie.com/online-store"],
    "Zenoti": ["zenoti.com/webstoreNew"],
    "WellieMD": ["WellieMD Questionnaire App"],
    "Wizlo": ["data-wz-brand"],
    "Asher": ["asher-exit-calendar", "asher-footer-cta"],
    "WellSync": ["wellsyncimagestorage", "wellsync-Iframe"],
    "UpScript": ["upscripthealth.org", "ush-prod-s3-sfwp-images-public"],
    "Hydreight": ["Hydreight"],
}


def _matches_platform(name: str, only: str | None) -> bool:
    """`--opt platform=...` filter: substring match on the canonical name, aliases resolved."""
    if not only:
        return True
    wanted = (canonical_platform(only) or only).lower()
    return wanted in name.lower()


# name -> (apex domains the platform serves tenants from, extra search dorks)
COMPETITORS: dict[str, tuple[list[str], list[str]]] = {
    "Bask Health": (
        ["bask.health"],
        [
            '"powered by bask"',
            '"analytics.bask.health"',
            '"checkout.bask.health"',
            '"bask.health" telehealth -site:bask.health',
        ],
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
    "Fuse Health": (["fusehealth.com"], ['"fusehealth.com" -site:fusehealth.com']),
    "CareValidate": (
        ["carevalidate.com"],
        ['"care360-next"', '"careglp"', '"primaryMedicationSubtitle1Text"'],
    ),
    "Telegra MD": (["telegramd.com"], ['"telegramd.com" -site:telegramd.com', '"telemdnow"']),
    "WhiteLabel MD": (
        ["whitelabelmd.com"],
        ['"api.whitelabelmd.com/answers"', '"intake_placement iframe"', '"wlmd-intake"'],
    ),
    "Karpa": (["karpahealth.com"], ['"karpahealth.com" -site:karpahealth.com', '"rxave.health"']),
    "Cuvo": (
        ["cuvo.co"],
        [
            '"Medstra Inc DBA Cuvo Health"',
            '"/images/products/tirzepatide-bottle.png"',
            '"Cuvo Health" -site:cuvo.co',
        ],
    ),
    "Wheel": (["wheel.com"], ['"wheel" "virtual care" powered -site:wheel.com']),
    # Med spa / DPC / white-label telehealth platforms (verified 2026-11-27, see the docs).
    "Aesthetic Record": (["myaestheticrecord.com"], ['"myaestheticrecord.com/online-booking"']),
    "Akute Health": (
        ["akutehealth.com"],
        ['"akutehealth.com/appointments"', '"portal.akutehealth.com" -site:akutehealth.com'],
    ),
    "Canvas Medical": (["canvasmedical.com"], ['"canvasmedical.com/app" -site:canvasmedical.com']),
    # Moxie tenants are booking paths (app.joinmoxie.com/booking/<slug>), not subdomains.
    "Moxie": ([], ['"app.joinmoxie.com/booking"', '"app.joinmoxie.com/online-store"']),
    "Zenoti": (["zenoti.com"], ['"zenoti.com/webstoreNew" medspa']),
    "WellieMD": (["welliemd.com"], ['"WellieMD Questionnaire"']),
    "Wizlo": (["wizlo.com"], ['"data-wz-brand"', '"wizlo" telehealth -site:wizlo.com']),
    "Asher": (["joinasher.com"], ['"joinasher.com" -site:joinasher.com']),
    "WellSync": (["wellsync.com"], ['"wellsyncimagestorage"', '"powered by wellsync"']),
    "UpScript": (["upscripthealth.org"], ['"upscripthealth.org" -site:upscripthealth.com']),
    "Hydreight": ([], ['"Hydreight app" IV therapy -site:hydreight.com', '"VSDHOne" -site:vsdhone.com']),
}


class CompetitorPlatformTool(Tool):
    name = "competitor"
    source = "competitor_platform"
    description = "Brands already running on competing telehealth infrastructure"

    def keep_candidate(self, domain: str) -> bool:
        return domain not in PLATFORM_OWNED

    def discovery_sources(self) -> list[DiscoverySource]:
        only = self.options.get("platform")
        markers = {k: v for k, v in SOURCE_MARKERS.items() if _matches_platform(k, only)}
        sources: list[DiscoverySource] = [
            SeedFileDiscovery(self.seed_file or DEFAULT_SEED, source=self.source),
            PublicWWWDiscovery(markers, source=self.source),
            NerdyDataDiscovery(markers, source=self.source),
        ]
        for name, (domains, dorks) in COMPETITORS.items():
            if not _matches_platform(name, only):
                continue
            for d in domains:
                sources.append(CrtShDiscovery(d, name, source=self.source))
            sources.append(SearchDiscovery(dorks, source=self.source, detail={"platform": name}))
        return sources
