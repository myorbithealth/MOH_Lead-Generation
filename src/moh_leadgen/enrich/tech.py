"""Fingerprint the platforms a site is built on - especially competing telehealth infrastructure.

Knowing a prospect already pays a competitor (Bask, Rimo, Healthie, OpenLoop, ...) is the
strongest buying signal we have: they've validated the model and can be switched.
"""

from __future__ import annotations

import re

# name -> (is_competitor, [regexes matched against raw HTML + resolved CNAME chain])
FINGERPRINTS: dict[str, tuple[bool, list[str]]] = {
    # --- telehealth / DTC-care infrastructure (MyOrbit Health competitors) ---
    # Verified 2026-11: Rimo storefronts load assets from larp-cdn.com and /pp/store/<id>/branding/ paths,
    # and tenants live on <brand>.rimo.health; Bask serves tenants from its *.bask.health apps.
    "Rimo": (
        True,
        [
            r"larp-cdn\.com",
            r"/pp/store/[A-Za-z0-9_\-]+/",
            r"[a-z0-9\-]+\.rimo\.health",
            r"rimo\.co/",
            r"rimo-(?:mark|wordmark)",
        ],
    ),
    "Bask Health": (
        True,
        [
            r"(?:checkout|app|api|analytics|cdn|pg)\.bask\.health",
            r"data-basky",
            r"baskhealth\.com",
            r"bask-?(?:checkout|widget|embed)",
            r"powered by bask",
        ],
    ),
    # Verified 2026-11 on live tenant sites:
    "Fuse Health": (True, [r"[a-z0-9\-]+\.fusehealth\.com", r"fusehealth\.com/programs/"]),
    "CareValidate": (
        True,
        [
            r"care360-next",
            r"careValidateApiUrl",
            r"[a-z0-9\-]+\.carevalidate\.com",
            r"careglp",
            r"primaryMedicationSubtitle1Text",
        ],
    ),
    "Telegra MD": (
        True,
        [
            r"[a-z0-9\-]+\.telegramd\.com",
            r"telegramd\.js",
            r"window\.telegraCustomer",
            r"telegraApp-script",
            r"support@telegramd\.com",
            r"telemdnow",
        ],
    ),
    "WhiteLabel MD": (
        True,
        [
            r"wlmd-intake",
            r"wlmdSetCookie",
            r"wlmd_cfg",
            r"(?:app|api|forms|widgets)\.whitelabelmd\.com",
            r"@whitelabelmd\.com",
            r"intake_placement",
            r"function tracking_start\(\)",
        ],
    ),
    "Karpa": (True, [r"karpahealth\.com", r"withkarpa\.com", r"karpa\.health"]),
    "RxAve": (True, [r"rxave\.health"]),
    "Cuvo": (
        True,
        [
            r"app\.cuvo\.co",
            r"api\.cuvo\.co",
            r"[a-z0-9\-]+\.cuvo\.co",
            r"powered by cuvo",
            r"Medstra,? Inc",
            r"DBA Cuvo Health",
            r"Cuvo Health",
        ],
    ),
    "Healthie": (True, [r"gethealthie\.com", r"healthie\.(?:io|com)", r"secure\.gethealthie"]),
    "OpenLoop": (True, [r"openloophealth\.com", r"openloop\.(?:health|com)"]),
    "Wheel": (True, [r"wheel\.com", r"wheelhealth"]),
    "Beluga Health": (True, [r"belugahealth\.com"]),
    "MD Integrations": (True, [r"mdintegrations\.com", r"mdi-?api"]),
    "Doctegrity": (True, [r"doctegrity\.com"]),
    "Qualiphy": (True, [r"qualiphy\.(?:me|com)"]),
    "GetLabs/Steady": (True, [r"steadymd\.com"]),
    "SteadyMD": (True, [r"steadymd\.com"]),
    "Hint Health": (True, [r"hint\.com", r"hintclinical"]),
    "Tebra": (True, [r"tebra\.com", r"patientpop\.com", r"kareo\.com"]),
    "Jane App": (True, [r"janeapp\.com"]),
    "SimplePractice": (True, [r"simplepractice\.com"]),
    "IntakeQ": (True, [r"intakeq\.com"]),
    "Zocdoc": (False, [r"zocdoc\.com"]),
    "Mindbody": (False, [r"mindbodyonline\.com", r"widgets\.mindbody"]),
    "Vagaro": (False, [r"vagaro\.com"]),
    "Boulevard": (False, [r"blvd\.co", r"joinblvd\.com"]),
    "Aesthetic Record": (True, [r"aestheticrecord\.com"]),
    "RepeatMD": (False, [r"repeatmd\.com"]),
    "Medplum": (True, [r"medplum\.com"]),
    "Elation": (True, [r"elationhealth\.com", r"elationemr"]),
    "Athena": (True, [r"athenahealth\.com"]),
    "DrChrono": (True, [r"drchrono\.com"]),
    "Practice Fusion": (True, [r"practicefusion\.com"]),
    "Klara": (False, [r"klara\.com"]),
    "Spruce": (False, [r"sprucehealth\.com"]),
    "Doxy.me": (False, [r"doxy\.me"]),
    "Honeybee Health": (False, [r"honeybeehealth\.com"]),
    "Truepill": (False, [r"truepill\.com"]),
    "Empower Pharmacy": (False, [r"empowerpharmacy\.com"]),
    "Strive Pharmacy": (False, [r"strivepharmacy\.com"]),
    "Hallandale Pharmacy": (False, [r"hallandalerx\.com"]),
    # --- med spa / DPC / white-label telehealth platforms (verified 2026-11-27 on live tenant pages;
    # tenant URLs and the exact strings are listed in docs/tools/competitor.md) ---
    # Akute: DPC and men's-health clinics book through <clinic>.akutehealth.com/appointments.
    "Akute Health": (True, [r"[a-z0-9\-]+\.akutehealth\.com", r"<title>Akute Health</title>"]),
    # Canvas: tenant patient portals at <tenant>.canvasmedical.com/app/ load canvas-pcp-assets.
    "Canvas Medical": (
        True,
        [r"[a-z0-9\-]+\.canvasmedical\.com", r"canvas-pcp-assets", r"canvas-medical\.s3"],
    ),
    # Moxie (med spa EMR + GFE + medical director): Book buttons go to app.joinmoxie.com/booking/<slug>.
    "Moxie": (True, [r"app\.joinmoxie\.com/(?:booking|packages|online-store)/", r"joinmoxie\.com"]),
    # Zenoti (booking/POS, like Boulevard): webstore links to <brand>.zenoti.com/webstoreNew/.
    "Zenoti": (False, [r"[a-z0-9\-]+\.zenoti\.com/webstore", r"zenoti\.com"]),
    # WellieMD: tenant intake apps (intake.<brand>.com) are titled "WellieMD Questionnaire".
    "WellieMD": (True, [r"WellieMD Questionnaire", r"[a-z0-9\-]+\.welliemd\.com"]),
    # Wizlo: tenant portals (portal.<brand>.com, CNAME portal.wizlo.com) carry data-wz-* attributes.
    "Wizlo": (
        True,
        [r"data-wz-brand", r"data-wz-component", r"[a-z0-9\-]+\.wizlo\.com", r"wizlo-prod-portal"],
    ),
    # Asher: tenant GLP-1 landing pages on <brand>.joinasher.com.
    "Asher": (True, [r"[a-z0-9\-]+\.joinasher\.com", r"asher-exit-calendar", r"asher-footer-cta"]),
    # WellSync: <brand>.wellsync.com programs and custom-domain portals branded from wellsyncimagestorage.
    "WellSync": (
        True,
        [
            r"wellsyncimagestorage\.blob\.core\.windows\.net",
            r"[a-z0-9\-]+\.wellsync\.com",
            r"wellsync-Iframe",
        ],
    ),
    # UpScript: partner storefronts are WordPress sites on <env>sfwp<brand>.upscripthealth.org.
    "UpScript": (True, [r"upscripthealth\.(?:org|com)", r"ush-(?:prod|qa|uat|dev)-s3-sfwp"]),
    # Hydreight / VSDHOne: IV and GLP-1 network; tenant booking widgets CNAME to *-bookingwidget-prod.
    "Hydreight": (True, [r"hydreight", r"vsdhone\.com", r"(?:vsdigital|hydreight)-bookingwidget"]),
    # --- commerce / CMS / marketing stack (useful for sizing & outreach) ---
    "Shopify": (False, [r"cdn\.shopify\.com", r"myshopify\.com", r"Shopify\.theme"]),
    "WooCommerce": (False, [r"woocommerce", r"wc-ajax"]),
    "WordPress": (False, [r"wp-content/", r"wp-includes/"]),
    "Webflow": (False, [r"webflow\.(?:com|io)", r"data-wf-site"]),
    "Wix": (False, [r"wixstatic\.com", r"wix\.com", r"parastorage\.com"]),
    "Squarespace": (False, [r"squarespace\.com", r"sqsp\.net"]),
    "GoHighLevel": (False, [r"leadconnectorhq\.com", r"gohighlevel", r"msgsndr\.com"]),
    "HubSpot": (False, [r"hs-scripts\.com", r"hubspot\.com", r"hsforms\.net"]),
    "Klaviyo": (False, [r"klaviyo\.com"]),
    "Stripe": (False, [r"js\.stripe\.com", r"checkout\.stripe\.com"]),
    "Calendly": (False, [r"calendly\.com"]),
    "Intercom": (False, [r"intercom(?:cdn|\.io)"]),
    "Podium": (False, [r"podium\.com"]),
    "Google Ads": (False, [r"googleads\.g\.doubleclick|AW-\d{9,}|googleadservices\.com"]),
    "Meta Pixel": (False, [r"connect\.facebook\.net/[a-z_]+/fbevents\.js"]),
    "TikTok Pixel": (False, [r"analytics\.tiktok\.com"]),
    "Segment": (False, [r"cdn\.segment\.com"]),
    "Cloudflare": (False, [r"cloudflare"]),
    "Vercel": (False, [r"vercel\.app|vercel-insights"]),
    "Next.js": (False, [r"__NEXT_DATA__|/_next/static"]),
    "React": (False, [r"react(?:-dom)?(?:\.production)?\.min\.js|data-reactroot"]),
}
_COMPILED = {
    name: (comp, [re.compile(p, re.IGNORECASE) for p in pats]) for name, (comp, pats) in FINGERPRINTS.items()
}

COMPETITOR_PLATFORMS = [name for name, (comp, _) in FINGERPRINTS.items() if comp]

# One name per platform. Seed files, fingerprints and older store rows used several spellings for
# the same company, which split the portal's platform counts (e.g. "Beluga" 33 vs "Beluga Health").
PLATFORM_ALIASES: dict[str, str] = {
    "Karpa (RxAve fulfilment)": "Karpa",
    "RxAve": "Karpa",
    "Beluga": "Beluga Health",
    "GetLabs/Steady": "SteadyMD",
    "MD": "MD Integrations",
    "Telegra": "Telegra MD",
    "WhiteLabel": "WhiteLabel MD",
    "VSDHOne": "Hydreight",
    "Hydreight / VSDHOne": "Hydreight",
    "Canvas": "Canvas Medical",
    "Akute": "Akute Health",
}
_ALIASES_LOWER = {alias.lower(): canonical for alias, canonical in PLATFORM_ALIASES.items()}


def canonical_platform(name: str | None) -> str | None:
    """Map any known spelling of a platform to its one canonical name (case-insensitive)."""
    if not name:
        return name
    cleaned = name.strip()
    return _ALIASES_LOWER.get(cleaned.lower(), cleaned)


def platform_spellings(name: str) -> list[str]:
    """Every stored spelling that means the same platform as `name` (canonical first)."""
    canonical = canonical_platform(name) or name
    aliases = [alias for alias, target in PLATFORM_ALIASES.items() if target == canonical]
    return [canonical, *aliases]


def fingerprint(html: str, cname_chain: list[str] | None = None) -> tuple[list[str], str | None]:
    haystack = html + "\n" + "\n".join(cname_chain or [])
    found = [name for name, (_, pats) in _COMPILED.items() if any(p.search(haystack) for p in pats)]
    competitor = next((n for n in found if _COMPILED[n][0]), None)
    return found, competitor
