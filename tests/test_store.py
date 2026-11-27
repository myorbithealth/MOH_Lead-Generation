from moh_leadgen.models import Candidate, Lead
from moh_leadgen.store import LeadStore


def test_store_roundtrip(tmp_path):
    store = LeadStore(tmp_path / "t.db")
    cand = Candidate(domain="a.example", source="legitscript", discovered_via="test")
    assert store.add_candidate(cand) is True
    assert store.add_candidate(cand) is False
    lead = store.get("a.example")
    assert isinstance(lead, Lead) and lead.source == "legitscript"
    assert [l.domain for l in store.iter(enriched=False)] == ["a.example"]
    lead.score = 70
    store.save(lead)
    assert next(store.iter(min_score=60)).domain == "a.example"
    assert store.stats()["total"] == 1
    assert store.stats()["unsynced"] == 1
    store.mark_synced(["a.example"])
    assert store.stats()["unsynced"] == 0


def test_rename_keeps_enrolment_and_suppression_of_brand_row(tmp_path):
    """A richer, just-crawled tenant re-keyed onto an enrolled brand must not reset its outreach."""
    from datetime import UTC, datetime

    from moh_leadgen.outreach.gate import lead_rejection

    store = LeadStore(tmp_path / "t.db")
    enrolled = datetime(2026, 11, 1, tzinfo=UTC)
    brand = Lead(domain="brand.com", website="https://brand.com", source="manual", status="enrolled")
    brand.outreach.enrolled_at = enrolled
    brand.outreach.enrolled_emails = ["ann@brand.com"]
    brand.outreach.smartlead_lead_ids = {"ann@brand.com": 11}
    brand.outreach.suppressed_at = enrolled
    brand.source_detail = {
        "suppressed_emails": ["ann@brand.com"],
        "smartlead_skipped": {"x@brand.com": "dup"},
    }
    store.save(brand)

    tenant = Lead(
        domain="brand.rimo.health",
        website="https://brand.rimo.health",
        source="competitor_platform",
        last_enriched_at=datetime(2026, 11, 26, tzinfo=UTC),
        pages_crawled=["/", "/about", "/pricing"],
        score=80,
    )
    tenant.outreach.smartlead_lead_ids = {"bob@brand.com": 22}
    tenant.source_detail = {"suppressed_emails": ["bob@brand.com"]}
    store.save(tenant)

    merged = store.rename("brand.rimo.health", "brand.com")
    assert merged is not None
    assert merged.status == "enrolled"
    assert merged.outreach.enrolled_at == enrolled
    assert merged.outreach.suppressed_at == enrolled
    assert merged.outreach.enrolled_emails == ["ann@brand.com"]
    assert merged.outreach.smartlead_lead_ids == {"ann@brand.com": 11, "bob@brand.com": 22}
    assert merged.source_detail["suppressed_emails"] == ["ann@brand.com", "bob@brand.com"]
    assert merged.source_detail["smartlead_skipped"] == {"x@brand.com": "dup"}
    assert merged.pages_crawled == ["/", "/about", "/pricing"]
    assert lead_rejection(merged) == "already_enrolled"
