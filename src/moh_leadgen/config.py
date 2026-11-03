"""Runtime settings, loaded from the environment (and a local .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    # Lovable Sales Portal (Supabase)
    supabase_url: str = field(default_factory=lambda: _env("SUPABASE_URL"))
    supabase_key: str = field(default_factory=lambda: _env("SUPABASE_SERVICE_ROLE_KEY"))

    # Meeting booking page handed to prospects (Google Calendar appointment schedule, Calendly, ...)
    booking_url: str = field(default_factory=lambda: _env("LEADGEN_BOOKING_URL"))

    # Discovery providers
    serpapi_key: str = field(default_factory=lambda: _env("SERPAPI_KEY"))
    google_cse_key: str = field(default_factory=lambda: _env("GOOGLE_CSE_KEY"))
    google_cse_cx: str = field(default_factory=lambda: _env("GOOGLE_CSE_CX"))
    bing_key: str = field(default_factory=lambda: _env("BING_SEARCH_KEY"))
    semrush_key: str = field(default_factory=lambda: _env("SEMRUSH_API_KEY"))

    # Enrichment providers
    hunter_key: str = field(default_factory=lambda: _env("HUNTER_API_KEY"))
    anthropic_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    google_places_key: str = field(default_factory=lambda: _env("GOOGLE_PLACES_API_KEY"))

    # Crawler
    db_path: Path = field(default_factory=lambda: Path(_env("LEADGEN_DB", "./leads.db")))
    user_agent: str = field(
        default_factory=lambda: _env(
            "LEADGEN_USER_AGENT", "MyOrbitHealth-LeadResearch/0.1 (+https://myorbithealth.com)"
        )
    )
    min_delay_seconds: float = field(default_factory=lambda: float(_env("LEADGEN_MIN_DELAY_SECONDS", "2.0")))
    max_pages_per_site: int = field(default_factory=lambda: int(_env("LEADGEN_MAX_PAGES_PER_SITE", "8")))
    respect_robots: bool = field(default_factory=lambda: _env_bool("LEADGEN_RESPECT_ROBOTS", True))
    request_timeout: float = field(default_factory=lambda: float(_env("LEADGEN_TIMEOUT_SECONDS", "20")))

    @property
    def has_search_provider(self) -> bool:
        return bool(self.serpapi_key or (self.google_cse_key and self.google_cse_cx) or self.bing_key)


settings = Settings()
