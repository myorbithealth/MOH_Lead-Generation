"""Tool 3: local healthcare & wellness businesses (med spas, gyms, clinics, IV bars...).

Discovery across a city list - these businesses have customers and cash flow but no telehealth
program, which is the upsell. OpenStreetMap (free, no key) runs by default; Google Places Text
Search (New) adds much deeper coverage when GOOGLE_PLACES_API_KEY is set.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import httpx

from ..config import settings
from ..discovery import OverpassDiscovery, SeedFileDiscovery
from ..discovery.base import DiscoverySource
from ..domains import is_junk, normalize_domain
from ..models import Candidate
from .base import Tool

log = logging.getLogger(__name__)

DEFAULT_SEED = Path("seeds/local_business_seed_domains.txt")

DEFAULT_CATEGORIES = [
    "med spa",
    "medical spa",
    "IV hydration therapy",
    "weight loss clinic",
    "men's health clinic",
    "hormone therapy clinic",
    "anti-aging clinic",
    "wellness clinic",
    "functional medicine clinic",
    "concierge medicine",
    "urgent care",
    "primary care clinic",
    "chiropractor wellness center",
    "gym personal training studio",
    "crossfit gym",
    "physical therapy clinic",
    "addiction treatment center",
    "ketamine clinic",
]

DEFAULT_CITIES = [
    "Los Angeles, CA", "San Diego, CA", "San Francisco, CA", "Sacramento, CA", "Phoenix, AZ", "Scottsdale, AZ",
    "Las Vegas, NV", "Denver, CO", "Dallas, TX", "Houston, TX", "Austin, TX", "San Antonio, TX", "Miami, FL",
    "Fort Lauderdale, FL", "Tampa, FL", "Orlando, FL", "Jacksonville, FL", "Atlanta, GA", "Charlotte, NC",
    "Raleigh, NC", "Nashville, TN", "Chicago, IL", "Minneapolis, MN", "Columbus, OH", "Detroit, MI",
    "New York, NY", "Brooklyn, NY", "Boston, MA", "Philadelphia, PA", "Washington, DC", "Seattle, WA",
    "Portland, OR", "Salt Lake City, UT", "Kansas City, MO", "St. Louis, MO", "Indianapolis, IN",
]


class GooglePlacesDiscovery(DiscoverySource):
    name = "google_places"

    def __init__(self, categories: list[str], cities: list[str], source="local_business"):
        self.categories, self.cities, self.source = categories, cities, source

    def available(self) -> bool:
        return bool(settings.google_places_key)

    def discover(self, limit: int | None = None) -> Iterator[Candidate]:
        seen: set[str] = set()
        count = 0
        headers = {
            "X-Goog-Api-Key": settings.google_places_key,
            "X-Goog-FieldMask": "places.displayName,places.websiteUri,places.nationalPhoneNumber,places.formattedAddress,places.rating,places.userRatingCount,places.primaryType,nextPageToken",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=30, headers=headers) as client:
            for city in self.cities:
                for cat in self.categories:
                    token = None
                    for _ in range(3):  # up to 60 results per query
                        body: dict = {"textQuery": f"{cat} in {city}", "pageSize": 20}
                        if token:
                            body["pageToken"] = token
                        r = client.post("https://places.googleapis.com/v1/places:searchText", json=body)
                        if r.status_code != 200:
                            log.warning("places %s: %s", r.status_code, r.text[:200])
                            break
                        data = r.json()
                        for place in data.get("places", []):
                            site = place.get("websiteUri")
                            domain = normalize_domain(site) if site else None
                            if not domain or domain in seen or is_junk(domain):
                                continue
                            seen.add(domain)
                            yield Candidate(
                                domain=domain,
                                source=self.source,
                                discovered_via="google_places",
                                source_detail={
                                    "category": cat,
                                    "city": city,
                                    "place_name": (place.get("displayName") or {}).get("text"),
                                    "place_phone": place.get("nationalPhoneNumber"),
                                    "place_address": place.get("formattedAddress"),
                                    "rating": place.get("rating"),
                                    "reviews": place.get("userRatingCount"),
                                    "primary_type": place.get("primaryType"),
                                },
                            )
                            count += 1
                            if limit and count >= limit:
                                return
                        token = data.get("nextPageToken")
                        if not token:
                            break


class LocalBusinessTool(Tool):
    name = "local"
    source = "local_business"
    description = "Med spas, gyms, clinics and wellness businesses by city (Google Places)"

    def discovery_sources(self) -> list[DiscoverySource]:
        cats = [c.strip() for c in self.options.get("categories", "").split(",") if c.strip()] or DEFAULT_CATEGORIES
        cities = [c.strip() for c in self.options.get("cities", "").split(";") if c.strip()] or DEFAULT_CITIES
        osm_cats = [c.strip() for c in self.options.get("osm_categories", "").split(",") if c.strip()] or None
        return [
            SeedFileDiscovery(self.seed_file or DEFAULT_SEED, source=self.source),
            OverpassDiscovery(cities, osm_cats, source=self.source),
            GooglePlacesDiscovery(cats, cities, source=self.source),
        ]
