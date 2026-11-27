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
    # Portal proxy: the portal's `pipeline-db` endpoint forwards PostgREST requests using the
    # service role it holds server-side, so the pipeline only needs its own token (a row in
    # public.api_clients holds the token's SHA-256). Preferred over a service-role key.
    portal_proxy_url: str = field(default_factory=lambda: _env("LEADGEN_PORTAL_PROXY_URL"))
    portal_token: str = field(default_factory=lambda: _env("LEADGEN_PORTAL_TOKEN"))

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

    # Reply side (Builder REPLY): portal anon key for read-only views, Cal.com v2 API key, and the
    # Claude models for reply classification (cheap, every reply) and weekly variant proposals.
    supabase_anon_key: str = field(default_factory=lambda: _env("SUPABASE_ANON_KEY"))
    calcom_api_key: str = field(default_factory=lambda: _env("CALCOM_API_KEY"))
    classify_model: str = field(default_factory=lambda: _env("LEADGEN_CLASSIFY_MODEL", "claude-haiku-5-5"))
    classify_escalation_model: str = field(
        default_factory=lambda: _env("LEADGEN_CLASSIFY_ESCALATION_MODEL", "claude-sonnet-5-5")
    )
    learn_model: str = field(default_factory=lambda: _env("LEADGEN_LEARN_MODEL", "claude-opus-5-5"))

    # Enrolment side (Builder ENROL): Smartlead sending platform, the outreach gate and the
    # compliance footer. The footer values can also come from app_settings 'outreach' in the portal.
    smartlead_api_key: str = field(default_factory=lambda: _env("SMARTLEAD_API_KEY"))
    smartlead_base_url: str = field(
        default_factory=lambda: _env("SMARTLEAD_BASE_URL", "https://server.smartlead.ai/api/v1")
    )
    smartlead_webhook_url: str = field(default_factory=lambda: _env("SMARTLEAD_WEBHOOK_URL"))
    smartlead_webhook_secret: str = field(default_factory=lambda: _env("SMARTLEAD_WEBHOOK_SECRET"))
    # comma-separated Smartlead email account ids attached to every campaign
    smartlead_email_account_ids: str = field(default_factory=lambda: _env("SMARTLEAD_EMAIL_ACCOUNT_IDS"))
    outreach_min_score: int = field(default_factory=lambda: int(_env("LEADGEN_OUTREACH_MIN_SCORE", "50")))
    # email sends per day that enrolment may commit to (a 4-step contact costs 4)
    outreach_daily_cap: int = field(default_factory=lambda: int(_env("LEADGEN_OUTREACH_DAILY_CAP", "300")))
    outreach_timezone: str = field(
        default_factory=lambda: _env("LEADGEN_OUTREACH_TIMEZONE", "America/New_York")
    )
    outreach_sender_name: str = field(default_factory=lambda: _env("LEADGEN_OUTREACH_SENDER_NAME"))
    outreach_company_legal_name: str = field(
        default_factory=lambda: _env("LEADGEN_OUTREACH_COMPANY_LEGAL_NAME")
    )
    outreach_postal_address: str = field(default_factory=lambda: _env("LEADGEN_OUTREACH_POSTAL_ADDRESS"))
    personalize_model: str = field(
        default_factory=lambda: _env("LEADGEN_PERSONALIZE_MODEL", "claude-sonnet-5-5")
    )

    @property
    def has_search_provider(self) -> bool:
        return bool(self.serpapi_key or (self.google_cse_key and self.google_cse_cx) or self.bing_key)


settings = Settings()


def portal_http(
    url: str | None,
    key: str | None,
    *,
    allow_anon: bool = False,
) -> tuple[str, dict[str, str]]:
    """(base_url, headers) for the portal: the pipeline-db proxy when configured, else PostgREST.

    Proxy mode (LEADGEN_PORTAL_PROXY_URL + LEADGEN_PORTAL_TOKEN) wins unless an explicit url/key
    pair is passed. Direct mode keeps the apikey/Authorization pair PostgREST expects.
    """
    if not url and not key and settings.portal_proxy_url and settings.portal_token:
        base = settings.portal_proxy_url.rstrip("/")
        return base, {
            "Authorization": f"Bearer {settings.portal_token}",
            "Content-Type": "application/json",
        }
    base = (url or settings.supabase_url).rstrip("/")
    key = key or settings.supabase_key or (settings.supabase_anon_key if allow_anon else "")
    if not base or not key:
        raise RuntimeError(
            "Set LEADGEN_PORTAL_PROXY_URL + LEADGEN_PORTAL_TOKEN, or SUPABASE_URL + "
            "SUPABASE_SERVICE_ROLE_KEY, to reach the Sales Portal"
        )
    headers = {"apikey": key, "Content-Type": "application/json"}
    if not key.startswith("sb_"):  # legacy JWT keys also go in Authorization
        headers["Authorization"] = f"Bearer {key}"
    return f"{base}/rest/v1", headers
