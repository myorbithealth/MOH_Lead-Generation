"""`leadgen pull-portal`: paged reads of the portal, the row mapping, and the newer-locally rule."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
from typer.testing import CliRunner

from moh_leadgen import cli
from moh_leadgen.config import settings
from moh_leadgen.models import Lead, LegitScriptEvidence, Person
from moh_leadgen.sinks.portal_import import lead_from_portal_row, pull_portal
from moh_leadgen.sinks.supabase import SupabaseSink
from moh_leadgen.store import LeadStore

runner = CliRunner()
OLD = "2026-11-01T10:00:00+00:00"


def _portal_row(domain: str, **extra) -> dict:
    row = {
        "id": f"id-{domain}",
        "domain": domain,
        "website": f"https://{domain}",
        "company_name": domain.split(".")[0].title(),
        "source": "competitor_platform",
        "score": 55,
        "emails": [f"info@{domain}"],
        "people": [],
        "status": "new",
        "created_at": "2026-10-01T00:00:00+00:00",
        "updated_at": OLD,
    }
    row.update(extra)
    return row


class FakePortal:
    """Serves /leads and /contacts with PostgREST limit/offset/filter semantics."""

    def __init__(self, leads: list[dict], contacts: list[dict] | None = None):
        self.leads = sorted(leads, key=lambda r: r["domain"])
        self.contacts = contacts or []
        self.requests: list[tuple[str, dict[str, str]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = urlsplit(str(request.url)).path.rsplit("/", 1)[-1]
        params = {k: v[0] for k, v in parse_qs(urlsplit(str(request.url)).query).items()}
        self.requests.append((path, params))
        if path == "leads":
            rows = self.leads
            if "updated_at" in params:
                since = datetime.fromisoformat(params["updated_at"].removeprefix("gte."))
                rows = [r for r in rows if datetime.fromisoformat(r["updated_at"]) >= since]
        elif path == "contacts":
            wanted = {v.strip('"') for v in params["lead_domain"][4:-1].split(",")}
            rows = [c for c in self.contacts if c["lead_domain"] in wanted]
        else:
            return httpx.Response(404, json={"message": "no route"})
        offset, limit = int(params.get("offset", 0)), int(params.get("limit", 1000))
        return httpx.Response(200, json=rows[offset : offset + limit])


def _sink(portal: FakePortal) -> SupabaseSink:
    return SupabaseSink("https://x.supabase.co", "sb_secret_test", transport=httpx.MockTransport(portal))


# ---------------------------------------------------------------------- paging


def test_fetch_leads_pages_until_a_short_page():
    portal = FakePortal([_portal_row(f"site{i:02d}.com") for i in range(12)])
    rows = list(_sink(portal).fetch_leads(page_size=5))
    assert [r["domain"] for r in rows] == [f"site{i:02d}.com" for i in range(12)]
    pages = [p for path, p in portal.requests if path == "leads"]
    assert [p["offset"] for p in pages] == ["0", "5", "10"]
    assert all(p["order"] == "domain.asc" and p["select"] == "*" for p in pages)


def test_fetch_leads_exact_multiple_stops_on_empty_page():
    portal = FakePortal([_portal_row(f"site{i}.com") for i in range(4)])
    assert len(list(_sink(portal).fetch_leads(page_size=2))) == 4
    assert len([1 for path, _ in portal.requests if path == "leads"]) == 3


def test_fetch_leads_since_filters_on_updated_at_when_present():
    portal = FakePortal(
        [_portal_row("old.com"), _portal_row("new.com", updated_at="2026-11-20T00:00:00+00:00")]
    )
    rows = list(_sink(portal).fetch_leads(since=datetime(2026, 11, 15)))
    assert [r["domain"] for r in rows] == ["new.com"]
    assert portal.requests[-1][1]["updated_at"] == "gte.2026-11-15T00:00:00+00:00"


def test_fetch_leads_since_ignored_without_updated_at_column():
    row = _portal_row("a.com")
    del row["updated_at"]
    portal = FakePortal([row])

    rows = list(_sink(portal).fetch_leads(since=datetime(2026, 11, 15, tzinfo=UTC)))
    assert [r["domain"] for r in rows] == ["a.com"]
    assert "updated_at" not in portal.requests[-1][1]


def test_fetch_contacts_for_chunks_by_100_and_groups_by_domain():
    domains = [f"d{i}.com" for i in range(230)]
    contacts = [{"id": i, "email": f"a@{d}", "lead_domain": d} for i, d in enumerate(domains)]
    portal = FakePortal([], contacts)
    out = _sink(portal).fetch_contacts_for(domains)
    assert len(out) == 230 and out["d7.com"][0]["email"] == "a@d7.com"
    assert len([1 for path, _ in portal.requests if path == "contacts"]) == 3


# ---------------------------------------------------------------------- mapping


def _rich_lead() -> Lead:
    return Lead(
        domain="glowclinic.com",
        website="https://glowclinic.com",
        company_name="Glow Clinic",
        source="competitor_platform",
        discovered_via="publicwww:bask",
        source_detail={"platform": "bask"},
        vertical="glp1_telehealth",
        legitscript=LegitScriptEvidence(
            status="seal_detected", seal_ids=["12345"], verify_url="https://legitscript.com/x"
        ),
        people=[
            Person(name="Ana Ruiz", title="Founder", email="ana@glowclinic.com", email_status="verified"),
            Person(name="Bo Li", email="bo@glowclinic.com", email_status="guessed"),
        ],
        owner_name="Ana Ruiz",
        owner_title="Founder",
        owner_email="ana@glowclinic.com",
        owner_email_status="verified",
        emails=["info@glowclinic.com"],
        phones=["+15550001111"],
        primary_email="info@glowclinic.com",
        city="Austin",
        state="TX",
        tech_stack=["bask", "shopify"],
        competitor_platform="bask",
        has_mx=True,
        score=72,
        score_reasons=["glp1 vertical"],
        last_enriched_at=datetime(2026, 11, 2, tzinfo=UTC),
        pages_crawled=["https://glowclinic.com/", "https://glowclinic.com/about"],
    )


def test_round_trip_preserves_core_fields():
    original = _rich_lead()
    row = {
        **original.to_portal_row(),
        "status": "reviewed",
        "notes": "call Tuesday",
        "created_at": "2026-10-01T00:00:00+00:00",
        "updated_at": OLD,
        "segment": "glp1_competitor",
        "enrolled_at": "2026-11-03T12:00:00+00:00",
        "smartlead_campaign_id": 4242,
        "disposition": "interested",
    }
    lead = lead_from_portal_row(row, [])
    assert lead.domain == original.domain
    assert lead.company_name == "Glow Clinic"
    assert lead.owner_email == "ana@glowclinic.com"
    assert lead.score == 72
    assert lead.competitor_platform == "bask"
    assert {p.email for p in lead.people} == {"ana@glowclinic.com", "bo@glowclinic.com"}
    assert lead.legitscript.status == "seal_detected"
    assert lead.legitscript.seal_ids == ["12345"]
    assert lead.legitscript.verify_url == "https://legitscript.com/x"
    assert lead.source_detail["platform"] == "bask"
    assert lead.status == "reviewed" and lead.notes == "call Tuesday"
    assert lead.last_enriched_at == datetime(2026, 11, 2, tzinfo=UTC)
    assert lead.outreach.segment == "glp1_competitor"
    assert lead.outreach.smartlead_campaign_id == 4242
    assert lead.outreach.enrolled_at == datetime(2026, 11, 3, 12, tzinfo=UTC)
    assert lead.outreach.disposition == "interested"
    # the raw row is kept, minus the jsonb columns that are already Lead fields
    assert lead.source_detail["portal"]["notes"] == "call Tuesday"
    assert "people" not in lead.source_detail["portal"]
    assert "source_detail" not in lead.source_detail["portal"]
    # a crawled row stays crawled after the daily rescore
    assert lead.pages_crawled


def test_contacts_rows_merge_into_people_and_enrolment():
    row = _portal_row("acme.com", people=[{"name": "Ann Lee", "email": "ann@acme.com"}])
    contacts = [
        {"email": "Ann@acme.com", "email_status": "verified", "lead_domain": "acme.com"},
        {
            "email": "cto@acme.com",
            "first_name": "Cy",
            "last_name": "Tow",
            "title": "CTO",
            "email_status": "provider",
            "smartlead_lead_id": 991,
            "lead_domain": "acme.com",
        },
    ]
    lead = lead_from_portal_row(row, contacts)
    people = {p.email: p for p in lead.people}
    assert people["ann@acme.com"].email_status == "verified"
    assert people["cto@acme.com"].name == "Cy Tow" and people["cto@acme.com"].title == "CTO"
    assert lead.outreach.smartlead_lead_ids == {"cto@acme.com": 991}
    assert lead.outreach.enrolled_emails == ["cto@acme.com"]


def test_unknown_values_fall_back_and_uncrawled_rows_stay_candidates():
    row = _portal_row(
        "bare.com", source="spreadsheet", status="weird", score=0, emails=[], legitscript_status="??"
    )
    lead = lead_from_portal_row(row, [])
    assert lead.source == "manual"
    assert lead.status == "new"
    assert lead.legitscript.status == "unchecked"
    assert lead.last_enriched_at is None and lead.pages_crawled == []


def test_last_enriched_at_falls_back_to_updated_at():
    lead = lead_from_portal_row(_portal_row("a.com"), [])
    assert lead.last_enriched_at == datetime.fromisoformat(OLD)


def test_unreachable_site_gets_no_placeholder_page():
    lead = lead_from_portal_row(_portal_row("down.com", score_reasons=["site unreachable"]), [])
    assert lead.last_enriched_at is not None and lead.pages_crawled == []


# ---------------------------------------------------------------------- pull + newer-locally


def test_pull_inserts_updates_and_skips_newer_local_rows(tmp_path):
    store = LeadStore(tmp_path / "leads.db")
    store.save(Lead(domain="newer.com", company_name="Local Edit", score=10))  # updated_at = now
    store.save(Lead(domain="older.com", company_name="Stale", pages_crawled=["https://older.com/a"]))
    store.conn.execute(
        "update leads set updated_at=? where domain='older.com'", ("2026-10-01T00:00:00+00:00",)
    )
    store.conn.commit()

    portal = FakePortal(
        [
            _portal_row("newer.com", company_name="Portal Name"),
            _portal_row("older.com", company_name="Portal Name"),
            _portal_row("fresh.com"),
        ]
    )
    stats = pull_portal(store, _sink(portal))
    assert (stats.fetched, stats.inserted, stats.updated, stats.skipped_newer_locally) == (3, 1, 1, 1)
    assert store.get("newer.com").company_name == "Local Edit"
    older = store.get("older.com")
    assert older.company_name == "Portal Name"
    assert older.pages_crawled == ["https://older.com/a"]  # the real crawl survives the merge
    assert store.get("fresh.com").last_enriched_at is not None
    # pulled rows are not queued for the next portal sync
    unsynced = {lead.domain for lead in store.iter(unsynced_only=True)}
    assert "fresh.com" not in unsynced and "older.com" not in unsynced


def test_pull_dry_run_and_limit_write_nothing(tmp_path):
    store = LeadStore(tmp_path / "leads.db")
    portal = FakePortal([_portal_row(f"s{i}.com") for i in range(5)])
    stats = pull_portal(store, _sink(portal), limit=3, dry_run=True)
    assert stats.fetched == 3 and stats.inserted == 3
    assert store.count() == 0


def test_pull_portal_cli_prints_counts(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "db_path", tmp_path / "leads.db")
    portal = FakePortal([_portal_row("a.com"), _portal_row("b.com")])
    monkeypatch.setattr("moh_leadgen.sinks.supabase.SupabaseSink", lambda: _sink(portal))
    result = runner.invoke(cli.app, ["pull-portal", "--since", "2026-01-01"])
    assert result.exit_code == 0, result.output
    assert "fetched 2, inserted 2, updated 0, skipped-newer-locally 0" in result.output
    lead_pages = [p for path, p in portal.requests if path == "leads" and "offset" in p]
    assert lead_pages[0]["updated_at"] == "gte.2026-01-01T00:00:00+00:00"
    assert LeadStore(tmp_path / "leads.db").count() == 2


# ---------------------------------------------------------------------- daily gating


def _steps():
    return cli._daily_steps(
        discover_limit=1,
        enrich_limit=1,
        retry_limit=1,
        workers=1,
        min_score=0,
        daily_cap=None,
        do_enroll=False,
        do_classify=False,
        today=date(2026, 11, 24),
    )


def _configure(monkeypatch, tmp_path, *, portal: bool):
    monkeypatch.setattr(settings, "db_path", tmp_path / "leads.db")
    for name in ("supabase_url", "supabase_key", "smartlead_api_key", "anthropic_key", "hunter_key"):
        monkeypatch.setattr(settings, name, "")
    monkeypatch.setattr(settings, "portal_proxy_url", "https://portal.example/pipeline-db" if portal else "")
    monkeypatch.setattr(settings, "portal_token", "tok" if portal else "")


def _pull_step(monkeypatch):
    calls = []

    def fake_pull(**kwargs):
        calls.append(kwargs)
        from moh_leadgen.sinks.portal_import import PullStats

        return PullStats(fetched=7, inserted=7)

    monkeypatch.setattr(cli, "pull_portal", fake_pull)
    steps = _steps()
    assert steps[0][0] == "pull portal"
    return calls, cli.run_steps(steps[:1])[0]


def test_daily_pulls_portal_first_into_a_small_store(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, portal=True)
    calls, (_, status, detail) = _pull_step(monkeypatch)
    assert status == "ok" and "fetched 7" in detail
    assert calls == [{"since": None, "limit": 0, "dry_run": False}]


def test_daily_skips_pull_when_store_is_populated(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, portal=True)
    store = LeadStore(tmp_path / "leads.db")
    for i in range(cli.PULL_PORTAL_BELOW):
        store.save(Lead(domain=f"s{i}.com"))
    calls, (_, status, detail) = _pull_step(monkeypatch)
    assert status == "skipped" and "50 leads" in detail and calls == []


def test_daily_skips_pull_without_portal_settings(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path, portal=False)
    calls, (_, status, detail) = _pull_step(monkeypatch)
    assert status == "skipped" and "portal" in detail and calls == []


def test_newer_portal_row_beats_older_local_row(tmp_path):
    store = LeadStore(tmp_path / "leads.db")
    store.save(Lead(domain="x.com", status="new"))
    past = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    store.conn.execute("update leads set updated_at=? where domain='x.com'", (past,))
    store.conn.commit()
    future = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    portal = FakePortal([_portal_row("x.com", status="disqualified", updated_at=future)])
    stats = pull_portal(store, _sink(portal))
    assert stats.updated == 1 and store.get("x.com").status == "disqualified"
    assert json.loads(store.conn.execute("select data from leads").fetchone()[0])["domain"] == "x.com"
