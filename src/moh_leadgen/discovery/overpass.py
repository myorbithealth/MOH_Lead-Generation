"""Key-free local-business discovery from OpenStreetMap (Nominatim geocoding + Overpass API).

Coverage is thinner than Google Places but costs nothing. Only businesses that publish a
`website` tag are returned, since the website is what the pipeline enriches.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import httpx

from ..domains import is_junk, normalize_domain
from ..models import Candidate, LeadSource
from .base import DiscoverySource

log = logging.getLogger(__name__)

NOMINATIM = "https://nominatim.openstreetmap.org/search"
OVERPASS = "https://overpass-api.de/api/interpreter"
UA = "MyOrbitHealth-LeadResearch/0.1 (+https://myorbithealth.com)"

# OSM tag filters per category we care about.
CATEGORY_FILTERS: dict[str, list[str]] = {
    "clinic": ['["amenity"="clinic"]', '["healthcare"="clinic"]', '["amenity"="doctors"]', '["healthcare"="doctor"]'],
    "med_spa": ['["shop"="beauty"]["beauty"~"skin_care|laser|aesthetic|medical"]', '["healthcare"="alternative"]', '["shop"="massage"]'],
    "gym": ['["leisure"="fitness_centre"]', '["amenity"="gym"]'],
    "pharmacy": ['["amenity"="pharmacy"]'],
    "weight_loss": ['["healthcare:speciality"~"nutrition|weight|bariatric|endocrin"]'],
    "physio": ['["healthcare"="physiotherapist"]', '["amenity"="physiotherapist"]'],
    "dentist": ['["amenity"="dentist"]'],
}


def geocode(city: str, client: httpx.Client) -> tuple[float, float, float, float] | None:
    r = client.get(NOMINATIM, params={"q": city, "format": "json", "limit": 1})
    if r.status_code != 200 or not r.json():
        return None
    s, n, w, e = (float(x) for x in r.json()[0]["boundingbox"])
    return s, w, n, e


def build_query(bbox: tuple[float, float, float, float], categories: list[str], limit: int = 500) -> str:
    s, w, n, e = bbox
    parts = []
    for cat in categories:
        for f in CATEGORY_FILTERS.get(cat, []):
            parts.append(f'nwr({s},{w},{n},{e}){f}["website"];')
    return f"[out:json][timeout:90];({''.join(parts)});out tags {limit};"


def parse_elements(elements: list[dict], city: str) -> Iterator[Candidate]:
    seen: set[str] = set()
    for el in elements:
        t = el.get("tags", {})
        site = t.get("website") or t.get("contact:website")
        domain = normalize_domain(site) if site else None
        if not domain or domain in seen or is_junk(domain):
            continue
        seen.add(domain)
        yield Candidate(
            domain=domain,
            source="local_business",
            discovered_via="openstreetmap",
            source_detail={
                "city": city,
                "place_name": t.get("name"),
                "place_phone": t.get("phone") or t.get("contact:phone"),
                "osm_tags": {k: v for k, v in t.items() if k in ("amenity", "healthcare", "shop", "leisure", "healthcare:speciality", "beauty")},
                "place_address": " ".join(filter(None, (t.get("addr:housenumber"), t.get("addr:street"), t.get("addr:city"), t.get("addr:state"), t.get("addr:postcode")))) or None,
            },
        )


class OverpassDiscovery(DiscoverySource):
    name = "openstreetmap"

    def __init__(self, cities: list[str], categories: list[str] | None = None, source: LeadSource = "local_business"):
        self.cities = cities
        self.categories = categories or list(CATEGORY_FILTERS)
        self.source = source

    def discover(self, limit: int | None = None) -> Iterator[Candidate]:
        count = 0
        with httpx.Client(timeout=120, headers={"User-Agent": UA}) as client:
            for city in self.cities:
                try:
                    bbox = geocode(city, client)
                    if not bbox:
                        log.warning("could not geocode %s", city)
                        continue
                    r = client.post(OVERPASS, data={"data": build_query(bbox, self.categories)})
                    if r.status_code != 200:
                        log.warning("overpass %s for %s", r.status_code, city)
                        continue
                    elements = r.json().get("elements", [])
                except (httpx.HTTPError, ValueError) as exc:
                    log.warning("openstreetmap failed for %s: %s", city, exc)
                    continue
                for cand in parse_elements(elements, city):
                    yield cand
                    count += 1
                    if limit and count >= limit:
                        return
