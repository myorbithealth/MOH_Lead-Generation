"""Domain normalisation and junk filtering shared across discovery sources."""

from __future__ import annotations

from urllib.parse import urlsplit

import tldextract

_extract = tldextract.TLDExtract(suffix_list_urls=(), fallback_to_snapshot=True)

# Domains that show up in search results / backlink lists but can never be a lead.
JUNK_DOMAINS = {
    "legitscript.com",
    "google.com",
    "facebook.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "linkedin.com",
    "youtube.com",
    "tiktok.com",
    "reddit.com",
    "wikipedia.org",
    "yelp.com",
    "bbb.org",
    "trustpilot.com",
    "crunchbase.com",
    "pinterest.com",
    "amazon.com",
    "apple.com",
    "medium.com",
    "prnewswire.com",
    "businesswire.com",
    "globenewswire.com",
    "nabp.pharmacy",
    "fda.gov",
    "nih.gov",
    "cdc.gov",
    "samhsa.gov",
    "glassdoor.com",
    "indeed.com",
    "zoominfo.com",
    "apollo.io",
    "shopify.com",
    "wordpress.com",
    "wix.com",
    "squarespace.com",
    "godaddy.com",
    "cloudflare.com",
    "webmd.com",
    "healthline.com",
    "drugs.com",
    "goodrx.com",
}

# Large, well-funded telehealth brands: real LegitScript merchants but poor fits for a
# white-label infrastructure pitch. They stay in the DB, just scored down.
ENTERPRISE_DOMAINS = {
    "hims.com",
    "forhers.com",
    "ro.co",
    "nurx.com",
    "lemonaidhealth.com",
    "teladoc.com",
    "amwell.com",
    "mdlive.com",
    "cvs.com",
    "walgreens.com",
    "riteaid.com",
    "goodrx.com",
    "weightwatchers.com",
    "noom.com",
    "calibrate.com",
    "foundhealth.com",
    "sesamecare.com",
    "plushcare.com",
    "zocdoc.com",
    "costplusdrugs.com",
    "healthwarehouse.com",
    "honeybeehealth.com",
    "blinkhealth.com",
    "amazon.com",
    "pillpack.com",
    "capsule.com",
    "alto.com",
    "truepill.com",
}


def normalize_domain(value: str) -> str | None:
    """Turn a URL/hostname/email-ish string into a bare registrable domain, or None."""
    if not value:
        return None
    value = value.strip().lower()
    if "@" in value:
        value = value.split("@", 1)[1]
    if "://" not in value:
        value = "http://" + value
    host = urlsplit(value).hostname or ""
    ext = _extract(host)
    if not ext.domain or not ext.suffix:
        return None
    return f"{ext.domain}.{ext.suffix}"


def registrable(host: str) -> str | None:
    return normalize_domain(host)


def is_junk(domain: str) -> bool:
    return domain in JUNK_DOMAINS


def is_enterprise(domain: str) -> bool:
    return domain in ENTERPRISE_DOMAINS
