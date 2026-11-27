"""The pipeline reaches the portal through the pipeline-db proxy when its token is configured."""

from moh_leadgen import config
from moh_leadgen.outreach.portal_io import PortalClient
from moh_leadgen.sinks.supabase import SupabaseSink


def test_proxy_mode_uses_token_and_no_rest_prefix(monkeypatch):
    monkeypatch.setattr(config.settings, "portal_proxy_url", "https://portal.example/api/public/pipeline-db/")
    monkeypatch.setattr(config.settings, "portal_token", "tok123")
    monkeypatch.setattr(config.settings, "supabase_url", "")
    monkeypatch.setattr(config.settings, "supabase_key", "")
    for cls in (SupabaseSink, PortalClient):
        c = cls()
        assert str(c.client.base_url).rstrip("/") == "https://portal.example/api/public/pipeline-db"
        assert c.client.headers["authorization"] == "Bearer tok123"
        assert "apikey" not in c.client.headers


def test_direct_mode_keeps_postgrest_headers(monkeypatch):
    monkeypatch.setattr(config.settings, "portal_proxy_url", "")
    monkeypatch.setattr(config.settings, "portal_token", "")
    c = SupabaseSink("https://x.supabase.co", "eyJ.legacy")
    assert str(c.client.base_url).rstrip("/") == "https://x.supabase.co/rest/v1"
    assert c.client.headers["apikey"] == "eyJ.legacy"
    assert c.client.headers["authorization"] == "Bearer eyJ.legacy"


def test_explicit_args_beat_proxy(monkeypatch):
    monkeypatch.setattr(config.settings, "portal_proxy_url", "https://portal.example/pipeline-db")
    monkeypatch.setattr(config.settings, "portal_token", "tok123")
    c = PortalClient("https://y.supabase.co", "sb_secret_abc")
    assert str(c.client.base_url).rstrip("/") == "https://y.supabase.co/rest/v1"
    assert "authorization" not in c.client.headers
