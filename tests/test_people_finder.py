"""Hunter Email Finder pass: name splitting, finder calls, thresholds, skips, credits, CLI wiring."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from moh_leadgen import cli
from moh_leadgen.enrich.hunter import CreditBudget, HunterClient, TokenBucket
from moh_leadgen.enrich.people_pass import (
    finder_names,
    finder_pass,
    run_people_pass,
    split_person_name,
)
from moh_leadgen.models import Lead, Person
from moh_leadgen.store import LeadStore

NOW = datetime(2026, 11, 27, 12, tzinfo=UTC)


class FakeFinder:
    """MockTransport handler: Email Finder answers keyed by (domain, first, last), verifier verdicts."""

    def __init__(self, found: dict | None = None, verdicts: dict | None = None):
        self.found = found or {}
        self.verdicts = verdicts or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        p = request.url.params
        if request.url.path == "/v2/email-finder":
            hit = self.found.get((p["domain"], p["first_name"], p["last_name"]))
            if hit is None:
                return httpx.Response(200, json={"data": {"email": None, "score": None}})
            email, score = hit
            return httpx.Response(
                200,
                json={
                    "data": {
                        "email": email,
                        "score": score,
                        "position": "Founder",
                        "linkedin_url": None,
                        "phone_number": None,
                        "accept_all": False,
                        "verification": {"date": None, "status": None},
                    }
                },
            )
        if request.url.path == "/v2/email-verifier":
            status = self.verdicts.get(p["email"], "valid")
            return httpx.Response(200, json={"data": {"status": status, "score": 95, "accept_all": False}})
        if request.url.path == "/v2/domain-search":
            return httpx.Response(200, json={"data": {"emails": [], "pattern": None}})
        return httpx.Response(404)

    def finder_calls(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == "/v2/email-finder"]

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


def make_client(fake: FakeFinder, budget: int | None = None) -> HunterClient:
    return HunterClient(
        "test-key",
        budget=CreditBudget(budget),
        transport=httpx.MockTransport(fake),
        bucket=TokenBucket(rate=1000.0, sleep=lambda s: None),
        sleep=lambda s: None,
    )


def lead(domain: str, **kw) -> Lead:
    defaults = {
        "domain": domain,
        "source": "competitor_platform",
        "last_enriched_at": NOW - timedelta(days=3),
        "emails": [f"info@{domain}"],
        "primary_email": f"info@{domain}",
        "score": 60,
    }
    defaults.update(kw)
    return Lead(**defaults)


@pytest.fixture
def store(tmp_path):
    return LeadStore(tmp_path / "leads.db")


# ----------------------------------------------------------------------------- names
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Dr. Carol Ann Linebarger", ("Carol", "Linebarger")),
        ("Paige Erickson, APRN", ("Paige", "Erickson")),
        ("Jane Doe", ("Jane", "Doe")),
        ("JOHN SMITH MD", ("John", "Smith")),
        ("Mary O'Neil-Burke, FNP-C", ("Mary", "O'Neil-Burke")),
        ("Madonna", None),
        ("Dr. Madonna", None),
        ("Smith, MD", None),
        ("", None),
        (None, None),
        ("J. Smith", None),
        ("Glow Wellness Clinic", None),
        ("Team 4 Health", None),
    ],
)
def test_split_person_name(raw, expected):
    assert split_person_name(raw) == expected


def test_finder_names_owner_first_then_decision_makers_deduped():
    lead_ = lead(
        "a.com",
        owner_name="Dr. Carol Linebarger",
        people=[
            Person(name="Staff", title="Team"),
            Person(name="Bo Ray", title="Nurse"),
            Person(name="Ann Lee", title="Medical Director", is_decision_maker=True),
            Person(name="Carol Linebarger, MD", title="Owner"),
        ],
    )
    assert finder_names(lead_) == ["Dr. Carol Linebarger", "Ann Lee", "Bo Ray"]


# ----------------------------------------------------------------------------- the client call
def test_finder_call_params_header_and_credit():
    fake = FakeFinder(found={("acme.com", "Carol", "Linebarger"): ("carol@acme.com", 93)})
    client = make_client(fake)
    found = client.email_finder("acme.com", "Carol", "Linebarger")
    req = fake.requests[0]
    assert req.url.path == "/v2/email-finder"
    assert dict(req.url.params) == {"domain": "acme.com", "first_name": "Carol", "last_name": "Linebarger"}
    assert req.headers["X-API-KEY"] == "test-key" and "test-key" not in str(req.url)
    assert found.email == "carol@acme.com" and found.score == 93 and client.credits_used == 1
    assert client.email_finder("acme.com", "No", "Body").email is None
    assert client.credits_used == 1  # a miss costs nothing


# ----------------------------------------------------------------------------- the pass
def test_pass_sets_owner_fields_and_person_and_records_attempt(store):
    store.save(lead("acme.com", owner_name="Dr. Carol Ann Linebarger", owner_title="Owner"))
    fake = FakeFinder(found={("acme.com", "Carol", "Linebarger"): ("carol@acme.com", 88)})
    stats = finder_pass(store, make_client(fake), limit=10, max_credits=50, min_score=70, now=NOW, echo=None)
    got = store.get("acme.com")
    assert got.owner_email == "carol@acme.com" and got.owner_email_status == "verified"
    person = next(p for p in got.people if p.email == "carol@acme.com")
    assert person.name == "Dr. Carol Ann Linebarger" and person.confidence == 0.88
    assert person.email_status == "verified" and person.title == "Owner"
    entry = got.source_detail["hunter"]["finder"]["Dr. Carol Ann Linebarger"]
    assert entry["checked_at"] == NOW.isoformat() and entry["outcome"] == "verified"
    assert entry["score"] == 88 and entry["email"] == "carol@acme.com"
    assert "hunter:email-finder" in got.source_detail["contact_providers"]
    assert stats.tried == 1 and stats.found == 1 and stats.verified == 1 and stats.credits_used == 2
    assert fake.paths() == ["/v2/email-finder", "/v2/email-verifier"]
    assert "1 lead(s) tried" in stats.summary() and "2 credit(s)" in stats.summary()


def test_crawled_person_gets_email_without_replacing_named_owner(store):
    store.save(
        lead(
            "b.com",
            owner_name="Al Owner",
            people=[Person(name="Paige Erickson, APRN", title="Nurse Practitioner")],
        )
    )
    fake = FakeFinder(found={("b.com", "Paige", "Erickson"): ("paige@b.com", 90)})
    stats = finder_pass(store, make_client(fake), limit=10, max_credits=50, verify=False, now=NOW, echo=None)
    got = store.get("b.com")
    assert got.owner_name == "Al Owner" and got.owner_email is None
    paige = next(p for p in got.people if p.name.startswith("Paige"))
    assert paige.email == "paige@b.com" and paige.email_status == "provider"
    assert "/v2/email-verifier" not in fake.paths() and stats.credits_used == 1
    tried = [(r.url.params["first_name"], r.url.params["last_name"]) for r in fake.finder_calls()]
    assert tried == [("Al", "Owner"), ("Paige", "Erickson")]
    assert got.source_detail["hunter"]["finder"]["Al Owner"]["outcome"] == "not_found"


def test_score_threshold_drops_low_scores(store):
    store.save(lead("c.com", owner_name="Low Score"))
    fake = FakeFinder(found={("c.com", "Low", "Score"): ("low@c.com", 69)})
    stats = finder_pass(store, make_client(fake), limit=10, max_credits=50, min_score=70, now=NOW, echo=None)
    got = store.get("c.com")
    assert got.owner_email is None and not [p for p in got.people if p.email]
    assert got.source_detail["hunter"]["finder"]["Low Score"]["outcome"] == "low_score"
    assert stats.low_score == 1 and stats.found == 0
    assert "/v2/email-verifier" not in fake.paths()


def test_verifier_drop_keeps_lead_unchanged(store):
    store.save(lead("d.com", owner_name="Dee Drop"))
    fake = FakeFinder(found={("d.com", "Dee", "Drop"): ("dee@d.com", 95)}, verdicts={"dee@d.com": "invalid"})
    stats = finder_pass(store, make_client(fake), limit=10, max_credits=50, now=NOW, echo=None)
    got = store.get("d.com")
    assert got.owner_email is None
    assert got.source_detail["hunter"]["finder"]["Dee Drop"]["outcome"] == "dropped"
    assert stats.dropped == 1 and stats.found == 0 and stats.credits_used == 2


def test_names_tried_in_last_90_days_are_skipped(store):
    recent = {"hunter": {"finder": {"Ann Recent": {"checked_at": (NOW - timedelta(days=30)).isoformat()}}}}
    stale = {"hunter": {"finder": {"Sam Stale": {"checked_at": (NOW - timedelta(days=91)).isoformat()}}}}
    store.save(lead("recent.com", owner_name="Ann Recent", source_detail=recent))
    store.save(lead("stale.com", owner_name="Sam Stale", source_detail=stale))
    store.save(lead("noname.com", owner_name="Madonna"))  # single word: no plausible name
    store.save(lead("has.com", owner_name="Al Has", owner_email="al@has.com", owner_email_status="on_site"))
    fake = FakeFinder()
    stats = finder_pass(store, make_client(fake), limit=10, max_credits=50, now=NOW, echo=None)
    assert [r.url.params["domain"] for r in fake.finder_calls()] == ["stale.com"]
    assert stats.skipped_recent == 1 and stats.eligible == 2 and stats.scanned == 4


def test_stops_cleanly_at_max_credits(store):
    for i in range(5):
        store.save(lead(f"d{i}.com", owner_name=f"Xa Yb{'c' * i}", score=60 - i))
    fake = FakeFinder(found={(f"d{i}.com", "Xa", f"Yb{'c' * i}"): (f"xa@d{i}.com", 90) for i in range(5)})
    stats = finder_pass(store, make_client(fake), limit=10, max_credits=5, now=NOW, echo=None)
    # each lead spends 2 (find + verify); a third would need 4 + 2 > 5
    assert stats.credits_used == 4 and stats.found == 2
    assert stats.stopped and "credit cap of 5" in stats.stopped
    assert [r.url.params["domain"] for r in fake.finder_calls()] == ["d0.com", "d1.com"]
    assert "hunter" not in store.get("d2.com").source_detail


def test_dry_run_makes_no_calls(store):
    store.save(lead("acme.com", owner_name="Jane Doe"))
    fake = FakeFinder()
    lines: list[str] = []
    stats = finder_pass(store, make_client(fake), limit=10, max_credits=5, dry_run=True, echo=lines.append)
    assert fake.requests == [] and stats.domains == ["acme.com"] and stats.tried == 0
    assert store.get("acme.com").source_detail == {}
    assert lines and "dry run" in lines[0]


def test_domain_search_keeps_finder_history(store):
    history = {"hunter": {"finder": {"Jane Doe": {"checked_at": NOW.isoformat(), "outcome": "not_found"}}}}
    store.save(lead("acme.com", owner_name="Jane Doe", source_detail=history))
    run_people_pass(store, make_client(FakeFinder()), min_score=0, now=NOW)
    hunter = store.get("acme.com").source_detail["hunter"]
    assert hunter["checked_at"] == NOW.isoformat() and hunter["finder"]["Jane Doe"]["outcome"] == "not_found"


# ----------------------------------------------------------------------------- CLI
def test_cli_finder_flag_switches_pass(monkeypatch, tmp_path):
    from moh_leadgen.config import settings
    from moh_leadgen.enrich import people_pass

    monkeypatch.setattr(settings, "db_path", tmp_path / "leads.db")
    called = []
    monkeypatch.setattr(people_pass, "finder_pass", lambda *a, **k: called.append(("finder", k)) or _Stats())
    monkeypatch.setattr(
        people_pass, "run_people_pass", lambda *a, **k: called.append(("domain", k)) or _Stats()
    )
    cli.people(
        limit=7,
        source=None,
        min_score=40,
        dry_run=True,
        verify=True,
        max_credits=9,
        finder=True,
        min_finder_score=75,
    )
    cli.people(
        limit=7,
        source=None,
        min_score=40,
        dry_run=True,
        verify=True,
        max_credits=9,
        finder=False,
        min_finder_score=70,
    )
    assert [c[0] for c in called] == ["finder", "domain"]
    kw = called[0][1]
    assert (
        kw["limit"] == 7 and kw["max_credits"] == 9 and kw["min_score"] == 75 and kw["min_lead_score"] == 40
    )


class _Stats:
    def __init__(self):
        self.domains: list = []

    queued = eligible = scanned = skipped_recent = 0

    def summary(self) -> str:
        return "ok"


def test_daily_runs_domain_search_then_finder(monkeypatch, tmp_path):
    from datetime import date

    from moh_leadgen.config import settings

    monkeypatch.setattr(settings, "db_path", tmp_path / "leads.db")
    monkeypatch.setattr(settings, "hunter_key", "k")
    calls = []
    monkeypatch.setattr(cli, "people", lambda **kw: calls.append(kw) or _Stats())
    steps = cli._daily_steps(
        discover_limit=1,
        enrich_limit=1,
        retry_limit=1,
        workers=1,
        min_score=0,
        daily_cap=None,
        do_enroll=False,
        do_classify=False,
        today=date(2026, 11, 27),
    )
    rows = {n: s for n, s, _ in cli.run_steps([s for s in steps if s[0] == "people"])}
    assert rows["people"] == "ok"
    assert [c["finder"] for c in calls] == [False, True]
    assert calls[1]["limit"] == 100 and calls[1]["max_credits"] == 150
    assert calls[0]["limit"] == 150
