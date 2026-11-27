"""Daily-run correctness: new-only discover limits, crawl retries, tenant re-keying, platform
aliases, discovery-metadata fallback, multi-marker source search, and the Claude result cache.

Everything runs against a temp SQLite file and small fakes; nothing touches the network.
"""

from datetime import UTC, datetime, timedelta
from urllib.parse import unquote

import httpx
import pytest

from moh_leadgen import cli, pipeline
from moh_leadgen.discovery.base import DiscoverySource
from moh_leadgen.discovery.sourcesearch import PublicWWWDiscovery
from moh_leadgen.enrich import llm
from moh_leadgen.enrich.site_crawler import SiteSnapshot, _parse, crawl_site
from moh_leadgen.enrich.tech import canonical_platform
from moh_leadgen.http import classify_exception, classify_response
from moh_leadgen.models import Candidate, Lead
from moh_leadgen.scoring import score_lead
from moh_leadgen.store import LeadStore
from moh_leadgen.tools.competitor_platform import SOURCE_MARKERS, CompetitorPlatformTool

HOME = "<html><head><title>Glow Med Spa</title></head><body><h1>Glow Med Spa</h1><p>Welcome</p></body></html>"


@pytest.fixture
def store(tmp_path):
    return LeadStore(tmp_path / "leads.db")


@pytest.fixture
def offline(monkeypatch):
    """Stub DNS and RDAP so enrich_lead never leaves the process."""
    monkeypatch.setattr(pipeline.dns_info, "lookup", lambda domain: {"has_mx": True})
    monkeypatch.setattr(pipeline.rdap, "lookup", lambda domain: {})


def _crawl_returns(monkeypatch, snap: SiteSnapshot) -> None:
    monkeypatch.setattr(pipeline, "crawl_site", lambda domain, client: snap)


def _ok_snapshot(domain: str, final_url: str | None = None, html: str = HOME) -> SiteSnapshot:
    url = final_url or f"https://{domain}/"
    return SiteSnapshot(domain=domain, final_url=url, pages=[_parse(url, html)])


def _failed_snapshot(domain: str, kind: str) -> SiteSnapshot:
    return SiteSnapshot(domain=domain, final_url=f"https://{domain}/", error_kind=kind)


# --------------------------------------------------------------------------- 1. discover --limit


class _ListSource(DiscoverySource):
    name = "fake"

    def __init__(self, domains: list[str], via: str = "fake"):
        self.domains = domains
        self.via = via
        self.limits_seen: list[int | None] = []

    def discover(self, limit=None):
        self.limits_seen.append(limit)
        for d in self.domains[:limit]:
            yield Candidate(domain=d, source="competitor_platform", discovered_via=self.via)


class _FakeTool:
    def __init__(self, sources):
        self.sources = sources

    def discovery_sources(self):
        return self.sources

    def keep_candidate(self, domain):
        return True


def test_discover_limit_counts_new_domains_only(store, monkeypatch):
    known = [f"known{i}.example" for i in range(200)]
    for d in known:
        store.add_candidate(Candidate(domain=d, source="competitor_platform", discovered_via="seed"))
    src = _ListSource(known + ["new1.example", "new2.example", "new3.example", "new4.example"])
    monkeypatch.setattr(cli, "_store", lambda: store)
    monkeypatch.setattr(cli, "_tool", lambda name, seed, opts: _FakeTool([src]))

    cli.discover("competitor", limit=3, seed=None, opt=[])

    assert store.stats()["total"] == 203
    assert store.get("new3.example") is not None and store.get("new4.example") is None
    # the source got the safety scan cap, not the CLI's new-domain limit
    assert src.limits_seen == [cli.settings.discovery_scan_limit]


def test_scan_limit_can_be_set_per_source():
    src = _ListSource([])
    assert src.effective_scan_limit() == cli.settings.discovery_scan_limit
    src.scan_limit = 50
    assert src.effective_scan_limit() == 50


def test_known_domain_from_another_source_is_corroborated(store):
    cand = Candidate(domain="brand.example", source="competitor_platform", discovered_via="publicwww")
    assert store.add_candidate(cand) is True
    again = cand.model_copy(update={"discovered_via": "crtsh:rimo.health"})
    assert store.add_candidate(again) is False
    assert store.add_candidate(again) is False  # recorded once
    assert store.add_candidate(cand) is False  # its own source is not "corroboration"
    assert store.get("brand.example").also_found_via == ["crtsh:rimo.health"]


# --------------------------------------------------------------------------- 2. retries


def test_failed_crawl_backs_off_then_parks(store, monkeypatch, offline):
    _crawl_returns(monkeypatch, _failed_snapshot("down.example", "timeout"))
    lead = Lead(domain="down.example", source="legitscript")
    expected_days = [1, 3, 7, None]
    for attempt, days in enumerate(expected_days, start=1):
        before = datetime.now(UTC)
        pipeline.enrich_lead(lead, client=None, use_providers=False)
        assert lead.last_enriched_at is None
        assert lead.attempts == attempt and lead.last_error_kind == "timeout"
        if days is None:
            assert lead.next_retry_at is None  # parked
        else:
            assert lead.next_retry_at - before >= timedelta(days=days)
            assert lead.next_retry_at - before < timedelta(days=days, minutes=1)
    store.save(lead)
    assert list(store.iter(enriched=False)) == []
    assert store.stats()["parked"] == 1


def test_successful_crawl_clears_retry_state(monkeypatch, offline):
    lead = Lead(domain="glow.example", source="local_business", attempts=2, last_error_kind="dns")
    lead.next_retry_at = datetime.now(UTC)
    _crawl_returns(monkeypatch, _ok_snapshot("glow.example"))
    pipeline.enrich_lead(lead, client=None, use_providers=False)
    assert lead.last_enriched_at is not None
    assert (lead.attempts, lead.next_retry_at, lead.last_error_kind) == (0, None, None)


def test_iter_returns_new_and_due_rows_and_retry_failed_selects_only_due(store):
    now = datetime(2026, 11, 27, 12, tzinfo=UTC)
    store.add_candidate(Candidate(domain="fresh.example", source="legitscript", discovered_via="t"))
    due = pipeline.schedule_retry(
        Lead(domain="due.example", source="legitscript"), "dns", now - timedelta(days=2)
    )
    waiting = pipeline.schedule_retry(Lead(domain="wait.example", source="legitscript"), "dns", now)
    for lead in (due, waiting):
        store.save(lead)

    assert {lead.domain for lead in store.iter(enriched=False, now=now)} == {"fresh.example", "due.example"}
    assert [lead.domain for lead in store.iter(retry_due_only=True, now=now)] == ["due.example"]


def test_retry_failed_requeues_rows_parked_by_older_versions(store):
    legacy = Lead(domain="old.example", source="legitscript", last_enriched_at=datetime.now(UTC))
    store.save(legacy)  # enriched, but nothing was ever crawled
    assert list(store.iter(retry_due_only=True)) == []
    assert store.requeue_legacy_failures() == 1
    assert [lead.domain for lead in store.iter(retry_due_only=True)] == ["old.example"]
    assert store.requeue_legacy_failures() == 0


def test_failure_classification():
    req = httpx.Request("GET", "https://x.example/")
    assert classify_exception(httpx.ConnectError("[Errno -2] Name or service not known")) == "dns"
    assert classify_exception(httpx.ReadTimeout("slow")) == "timeout"
    assert classify_exception(httpx.ConnectError("connection refused")) == "other"
    assert classify_response(httpx.Response(403, request=req)) == "blocked"
    assert classify_response(httpx.Response(429, request=req)) == "blocked"
    challenge = httpx.Response(503, text="<title>Just a moment...</title>", request=req)
    assert classify_response(challenge) == "blocked"
    assert classify_response(httpx.Response(404, request=req)) == "http_4xx"
    assert classify_response(httpx.Response(502, request=req)) == "http_5xx"
    assert classify_response(httpx.Response(200, text="ok", request=req)) is None


class _FakeClient:
    """Stands in for PoliteClient: returns canned responses (or None with a reason) per URL."""

    def __init__(self, answers: dict[str, httpx.Response | str]):
        self.answers = answers
        self.last_error_kind = None

    def get(self, url):
        answer = self.answers.get(url, "dns")
        if isinstance(answer, str):
            self.last_error_kind = answer
            return None
        self.last_error_kind = None
        return answer


def test_crawl_reports_the_most_telling_failure():
    req = httpx.Request("GET", "https://shy.example/")
    client = _FakeClient(
        {"https://shy.example/": "timeout", "http://shy.example/": httpx.Response(403, request=req)}
    )
    snap = crawl_site("shy.example", client)
    assert snap.pages == [] and snap.error_kind == "blocked"
    assert crawl_site("gone.example", _FakeClient({})).error_kind == "dns"


# --------------------------------------------------------------------------- 3. re-key tenants


def test_platform_tenant_redirect_rekeys_to_brand_domain(store, monkeypatch, offline):
    store.add_candidate(
        Candidate(
            domain="glow.rimo.health",
            source="competitor_platform",
            discovered_via="crtsh:rimo.health",
            source_detail={"platform": "Rimo"},
        )
    )
    # the brand domain was already known from another source, not yet crawled
    store.add_candidate(
        Candidate(domain="glowspa.com", source="competitor_platform", discovered_via="publicwww")
    )
    _crawl_returns(monkeypatch, _ok_snapshot("glow.rimo.health", "https://www.glowspa.com/"))

    lead = pipeline.enrich_lead(store.get("glow.rimo.health"), client=None, use_providers=False)
    assert lead.source_detail["rekey_to"] == "glowspa.com"
    assert not any("different brand" in r for r in lead.score_reasons)

    saved = cli.save_enriched(store, lead)
    assert saved.domain == "glowspa.com"
    assert store.get("glow.rimo.health") is None
    merged = store.get("glowspa.com")
    assert merged.last_enriched_at is not None and merged.company_name
    assert {merged.discovered_via, *merged.also_found_via} == {"crtsh:rimo.health", "publicwww"}
    assert merged.source_detail["rekeyed_from"] == "glow.rimo.health"
    assert not any("different brand" in r for r in merged.score_reasons)
    assert store.stats()["total"] == 1


def test_rekey_rules():
    tenant = Lead(domain="integration-glow.telegramd.com", discovered_via="crtsh:telegramd.com")
    assert pipeline.rekey_target(tenant, "glowspa.com") == "glowspa.com"
    assert pipeline.rekey_target(tenant, "telegramd.com") is None  # platform apex
    assert pipeline.rekey_target(tenant, "rimo.health") is None  # another platform's domain
    assert pipeline.rekey_target(Lead(domain="glowspa.com"), "other.com") is None  # not a tenant


def test_tenant_served_from_platform_apex_is_not_a_redirect(monkeypatch, offline):
    _crawl_returns(monkeypatch, _ok_snapshot("glow.rimo.health"))
    lead = Lead(domain="glow.rimo.health", source="competitor_platform", discovered_via="crtsh:rimo.health")
    pipeline.enrich_lead(lead, client=None, use_providers=False)
    assert "redirected_to" not in lead.source_detail and "rekey_to" not in lead.source_detail


def test_ordinary_redirect_to_another_brand_is_still_penalised():
    lead = Lead(domain="oldbrand.com", pages_crawled=["x"], source_detail={"redirected_to": "newowner.com"})
    assert any("different brand" in r for r in score_lead(lead).score_reasons)


# --------------------------------------------------------------------------- 4. platform aliases


@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("Karpa (RxAve fulfilment)", "Karpa"),
        ("RxAve", "Karpa"),
        ("rxave", "Karpa"),
        ("Beluga", "Beluga Health"),
        ("GetLabs/Steady", "SteadyMD"),
        ("MD", "MD Integrations"),
        ("Telegra", "Telegra MD"),
        ("WhiteLabel", "WhiteLabel MD"),
        ("Rimo", "Rimo"),
    ],
)
def test_canonical_platform(raw, canonical):
    assert canonical_platform(raw) == canonical


def test_aliases_applied_on_seed_and_fingerprint(store, monkeypatch, offline):
    store.add_candidate(
        Candidate(
            domain="trimrx.example",
            source="competitor_platform",
            discovered_via="seedfile:x",
            source_detail={"platform": "Karpa (RxAve fulfilment)"},
        )
    )
    assert store.get("trimrx.example").source_detail["platform"] == "Karpa"

    html = HOME.replace("</body>", '<script src="https://cdn.rxave.health/x.js"></script></body>')
    _crawl_returns(monkeypatch, _ok_snapshot("trimrx.example", html=html))
    lead = pipeline.enrich_lead(store.get("trimrx.example"), client=None, use_providers=False)
    assert lead.competitor_platform == "Karpa"


def test_iter_platform_matches_every_spelling(store):
    legacy = Lead(domain="legacy.example", source="competitor_platform", competitor_platform="RxAve")
    store.save(legacy)
    store.add_candidate(
        Candidate(
            domain="new.example",
            source="competitor_platform",
            discovered_via="t",
            source_detail={"platform": "Karpa"},
        )
    )
    found = {lead.domain for lead in store.iter(platform="rxave")}
    assert found == {"legacy.example", "new.example"}


def test_competitor_tool_resolves_alias_in_platform_option():
    sources = CompetitorPlatformTool(platform="RxAve").discovery_sources()
    publicwww = next(s for s in sources if isinstance(s, PublicWWWDiscovery))
    assert list(publicwww.markers) == ["Karpa"]


# --------------------------------------------------------------------------- 5. metadata fallback


def test_places_metadata_fills_gaps_when_crawl_finds_nothing(monkeypatch, offline):
    detail = {
        "place_phone": "+1 512 555 0100",
        "place_name": "Austin Med Spa",
        "place_address": "1 Main St, Austin, TX",
    }
    lead = Lead(domain="austin.example", source="local_business", source_detail=detail)
    _crawl_returns(monkeypatch, _failed_snapshot("austin.example", "dns"))
    pipeline.enrich_lead(lead, client=None, use_providers=False)
    assert lead.primary_phone == "+1 512 555 0100" and "+1 512 555 0100" in lead.phones
    assert lead.company_name == "Austin Med Spa"
    assert lead.address == "1 Main St, Austin, TX"


def test_crawled_values_win_over_places_metadata(monkeypatch, offline):
    lead = Lead(domain="glow.example", source="local_business", source_detail={"place_name": "Places Name"})
    _crawl_returns(monkeypatch, _ok_snapshot("glow.example"))
    pipeline.enrich_lead(lead, client=None, use_providers=False)
    assert lead.company_name != "Places Name"


# --------------------------------------------------------------------------- 6. multi-marker search


def test_source_markers_are_lists():
    assert all(isinstance(v, list) and v for v in SOURCE_MARKERS.values())
    assert "rxave.health" in SOURCE_MARKERS["Karpa"] and "RxAve" not in SOURCE_MARKERS


def test_publicwww_searches_each_marker_and_dedupes(monkeypatch):
    monkeypatch.setattr("moh_leadgen.discovery.sourcesearch.settings.publicwww_key", "test-key")
    pages = {
        '"larp-cdn.com"': "https://www.alpha-spa.com/\nhttps://beta-rx.com/page\n",
        '"rimo-health"': "https://beta-rx.com/\nhttps://gamma-glp.com/\n",
        '"/images/products/tirzepatide-bottle.png"': "https://delta-trim.com/\n",
    }
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        raw = request.url.raw_path.decode()
        assert "%2F" in raw or "tirzepatide" not in raw  # slashes in a marker stay inside the path segment
        phrase = unquote(raw.removeprefix("/websites/").split("/")[0].split("?")[0])
        asked.append(phrase)
        return httpx.Response(200, text=pages.get(phrase, ""))

    markers = {"Rimo": ["larp-cdn.com", "rimo-health"], "Cuvo": ["/images/products/tirzepatide-bottle.png"]}
    src = PublicWWWDiscovery(markers, transport=httpx.MockTransport(handler))
    cands = list(src.discover())
    assert asked == list(pages)
    assert [c.domain for c in cands] == ["alpha-spa.com", "beta-rx.com", "gamma-glp.com", "delta-trim.com"]
    assert cands[2].source_detail == {"platform": "Rimo", "marker": "rimo-health"}
    assert cands[3].source_detail["platform"] == "Cuvo"


# --------------------------------------------------------------------------- 7 + 9


def test_store_uses_wal(store):
    assert store.conn.execute("pragma journal_mode").fetchone()[0] == "wal"


def test_llm_extraction_reuses_cache_for_same_corpus(monkeypatch):
    snap = _ok_snapshot("glow.example")
    digest = llm.corpus_hash(llm.build_corpus(snap))
    cached = {
        "corpus_sha256": digest,
        "summary": "Med spa",
        "people": [{"name": "Dana Lee", "title": "Founder", "evidence": "Founded by Dana"}],
    }

    def no_api():
        raise AssertionError("a cache hit must not call Claude")

    monkeypatch.setattr(llm, "available", no_api)
    people, meta = llm.extract(snap, cached=cached)
    assert [p.name for p in people] == ["Dana Lee"] and people[0].is_decision_maker
    assert meta is cached

    monkeypatch.setattr(llm, "available", lambda: False)
    assert llm.extract(snap, cached={**cached, "corpus_sha256": "stale"}) is None
