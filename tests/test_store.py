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
