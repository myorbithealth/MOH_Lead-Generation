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
    publicwww_key: str = field(default_factory=lambda: _env("PUBLICWWW_API_KEY"))
    nerdydata_key: str = field(default_factory=lambda: _env("NERDYDATA_API_KEY"))

    # Enrichment providers
    hunter_key: str = field(default_factory=lambda: _env("HUNTER_API_KEY"))
    apollo_key: str = field(default_factory=lambda: _env("APOLLO_API_KEY"))
    apollo_phone_webhook: str = field(default_factory=lambda: _env("APOLLO_PHONE_WEBHOOK_URL"))
    lusha_key: str = field(default_factory=lambda: _env("LUSHA_API_KEY"))
    anthropic_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    google_places_key: str = field(default_factory=lambda: _env("GOOGLE_PLACES_API_KEY"))

    # Claude models. Per-site extraction and search run on every lead, so they default to Sonnet
    # (half Opus' price); override per run with LEADGEN_LLM_MODEL / LEADGEN_SEARCH_MODEL.
    llm_model: str = field(default_factory=lambda: _env("LEADGEN_LLM_MODEL", "claude-sonnet-5-5"))
    search_model: str = field(default_factory=lambda: _env("LEADGEN_SEARCH_MODEL", "claude-sonnet-5-5"))

    # Discovery: a safety cap on how many candidates one source may yield per run (known ones
    # included), so a crt.sh or PublicWWW pull stays bounded even when nothing new turns up.
    discovery_scan_limit: int = field(
        default_factory=lambda: int(_env("LEADGEN_DISCOVERY_SCAN_LIMIT", "5000"))
    )

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
