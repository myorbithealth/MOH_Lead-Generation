"""Hunter people pass: client (auth, params, credits, retries), selection, verification, the pass."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from moh_leadgen import cli
from moh_leadgen.enrich.hunter import (
    BudgetExhausted,
    CreditBudget,
    HunterClient,
    HunterStop,
    TokenBucket,
    VerifyResult,
    verification_outcome,
)
from moh_leadgen.enrich.people_pass import (
    has_person_email,
    run_people_pass,
    select_decision_makers,
    verify_guessed,
)
from moh_leadgen.models import Lead, Person
from moh_leadgen.store import LeadStore

NOW = datetime(2026, 11, 27, 12, tzinfo=UTC)


class FakeHunter:
    """A MockTransport handler serving Domain Search and the Email Verifier from dicts."""

    def __init__(self, searches: dict | None = None, verdicts: dict | None = None):
        self.searches = searches or {}
        self.verdicts = verdicts or {}
        self.requests: list[httpx.Request] = []
        self.queue: list[httpx.Response] = []  # responses served before the normal handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.queue:
            return self.queue.pop(0)
        params = request.url.params
        if request.url.path == "/v2/domain-search":
            body = self.searches.get(params["domain"], {"emails": [], "pattern": None})
            return httpx.Response(200, json={"data": body, "meta": {"results": len(body["emails"])}})
        if request.url.path == "/v2/email-verifier":
            status = self.verdicts.get(params["email"], "unknown")
            return httpx.Response(200, json={"data": {"status": status, "score": 91, "accept_all": False}})
        if request.url.path == "/v2/email-finder":
            return httpx.Response(200, json={"data": {"email": None}})
        return httpx.Response(404)

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


class NoWaitBucket(TokenBucket):
    def __init__(self):
        super().__init__(rate=1000.0, sleep=lambda s: None)


def make_client(fake: FakeHunter, budget: int | None = None, sleeps: list | None = None) -> HunterClient:
    return HunterClient(
        "test-key",
        budget=CreditBudget(budget),
        transport=httpx.MockTransport(fake),
        bucket=NoWaitBucket(),
        sleep=(sleeps.append if sleeps is not None else lambda s: None),
    )


def email_row(value, first, last, position=None, confidence=90, type_="personal"):
    return {
        "value": value,
        "type": type_,
        "confidence": confidence,
        "first_name": first,
        "last_name": last,
        "position": position,
        "linkedin": None,
        "phone_number": None,
    }


def enriched_lead(domain: str, **kw) -> Lead:
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


# ----------------------------------------------------------------------------- client
def test_domain_search_sends_key_header_and_params_and_counts_credits():
    fake = FakeHunter(searches={"acme.com": {"emails": [email_row("jane@acme.com", "Jane", "Doe")]}})
    client = make_client(fake)
    result = client.domain_search("acme.com")
    req = fake.requests[0]
    assert req.headers["X-API-KEY"] == "test-key"
    assert "api_key" not in req.url.params  # the key never goes in the URL
    assert dict(req.url.params) == {"domain": "acme.com", "type": "personal", "limit": "10"}
    assert result and result.emails[0]["value"] == "jane@acme.com"
    assert client.credits_used == 1
    client.domain_search("empty.com")  # no emails returned: Hunter does not bill it
    assert client.credits_used == 1


def test_verifier_counts_one_credit_and_returns_score():
    fake = FakeHunter(verdicts={"jane@acme.com": "valid"})
    client = make_client(fake)
    result = client.verify_email("jane@acme.com")
    assert result == VerifyResult(status="valid", score=91, accept_all=False)
    assert fake.requests[0].url.params["email"] == "jane@acme.com"
    assert client.credits_used == 1
    assert client.verify("jane@acme.com") == "valid"


def test_429_is_retried_once_after_retry_after():
    fake = FakeHunter(searches={"acme.com": {"emails": [email_row("jane@acme.com", "Jane", "Doe")]}})
    fake.queue.append(httpx.Response(429, headers={"Retry-After": "3"}, json={"errors": []}))
    sleeps: list[float] = []
    client = make_client(fake, sleeps=sleeps)
    assert client.domain_search("acme.com") is not None
    assert sleeps == [3.0]
    assert len(fake.requests) == 2
    assert client.credits_used == 1


def test_5xx_retried_once_then_gives_up_without_credit():
    fake = FakeHunter()
    fake.queue += [httpx.Response(503), httpx.Response(502)]
    client = make_client(fake)
    assert client.domain_search("acme.com") is None
    assert len(fake.requests) == 2
    assert client.credits_used == 0


def test_persistent_429_stops_the_run():
    fake = FakeHunter()
    fake.queue += [httpx.Response(429), httpx.Response(429)]
    with pytest.raises(HunterStop):
        make_client(fake).domain_search("acme.com")


def test_budget_refuses_call_before_it_is_made():
    fake = FakeHunter()
    client = make_client(fake, budget=0)
    with pytest.raises(BudgetExhausted):
        client.domain_search("acme.com")
    assert fake.requests == []


def test_token_bucket_waits_when_empty():
    t = [0.0]
    slept: list[float] = []

    def sleep(s):
        slept.append(s)
        t[0] += s

    bucket = TokenBucket(rate=10.0, clock=lambda: t[0], sleep=sleep)
    for _ in range(12):
        bucket.acquire()
    assert len(slept) == 2
    assert all(abs(s - 0.1) < 1e-9 for s in slept)


# ----------------------------------------------------------------------------- selection + mapping
def test_select_prefers_decision_maker_titles():
    rows = [
        email_row("sam@acme.com", "Sam", "Lee", "Nurse", 99),
        email_row("jane@acme.com", "Jane", "Doe", "Co-Founder & CEO", 70),
        email_row("bob@acme.com", "Bob", "Ray", "Head of Growth", 85),
        email_row("amy@acme.com", "Amy", "Fox", "Marketing Manager", 60),
        email_row("info@acme.com", "Info", "Desk", "Owner", 99),  # role box, never a person
    ]
    picked = select_decision_makers(rows, "acme.com")
    assert [p.email for p in picked] == ["bob@acme.com", "jane@acme.com"]
    assert all(p.email_status == "provider" and p.source_url == "hunter:domain-search" for p in picked)
    assert picked[0].confidence == 0.85 and picked[0].is_decision_maker


def test_select_falls_back_to_confidence_60_without_titles():
    rows = [
        email_row("sam@acme.com", "Sam", "Lee", "Nurse", 59),
        email_row("kim@acme.com", "Kim", "Ng", None, 75),
        email_row("noname@acme.com", None, None, None, 99),
    ]
    picked = select_decision_makers(rows, "acme.com")
    assert [p.email for p in picked] == ["kim@acme.com"]
    assert not picked[0].is_decision_maker


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("valid", ("verified", False)),
        ("accept_all", ("provider", True)),
        ("webmail", (None, False)),
        ("disposable", (None, False)),
        ("invalid", (None, False)),
        ("unknown", (None, False)),
    ],
)
def test_verification_outcome(status, expected):
    assert verification_outcome(VerifyResult(status=status)) == expected


def test_has_person_email_ignores_role_junk_and_guesses():
    lead = enriched_lead("acme.com", owner_email="info@acme.com", owner_email_status="on_site")
    assert not has_person_email(lead)
    lead.people = [Person(name="Jane Doe", email="jane.doe@acme.com", email_status="guessed")]
    assert not has_person_email(lead)
    lead.people[0].email_status = "provider"
    assert has_person_email(lead)


def test_verify_guessed_promotes_or_drops_owner_email():
    fake = FakeHunter(verdicts={"jane.doe@acme.com": "valid"})
    lead = enriched_lead(
        "acme.com",
        owner_name="Jane Doe",
        owner_email="jane.doe@acme.com",
        owner_email_status="guessed",
        people=[Person(name="Jane Doe", email="jane.doe@acme.com", email_status="guessed")],
        source_detail={"owner_email_guessed": True},
    )
    counts = verify_guessed(lead, make_client(fake))
    assert counts.verified == 1
    assert lead.owner_email_status == "verified" and lead.people[0].email_status == "verified"
    assert lead.people[0].verifier_score == 91
    assert "owner_email_guessed" not in lead.source_detail

    bad = enriched_lead(
        "bad.com", owner_name="Joe Bad", owner_email="joe.bad@bad.com", owner_email_status="guessed"
    )
    counts = verify_guessed(bad, make_client(FakeHunter(verdicts={"joe.bad@bad.com": "invalid"})))
    assert counts.dropped == 1
    assert bad.owner_email is None and bad.owner_email_status is None


def test_enrich_hook_skips_without_guesses():
    from moh_leadgen.enrich.people_pass import verify_guessed_with_hunter

    fake = FakeHunter()
    assert verify_guessed_with_hunter(enriched_lead("acme.com"), make_client(fake)) is None
    assert fake.requests == []


# ----------------------------------------------------------------------------- the pass
@pytest.fixture
def store(tmp_path):
    return LeadStore(tmp_path / "leads.db")


def _searches():
    return {
        "acme.com": {
            "pattern": "{first}",
            "accept_all": False,
            "emails": [
                email_row("jane@acme.com", "Jane", "Doe", "Founder", 92),
                email_row("bob@acme.com", "Bob", "Ray", "Medical Director", 80),
            ],
        },
        "named.com": {"pattern": None, "emails": [email_row("ann@named.com", "Ann", "Bee", "CEO", 90)]},
    }


def test_pass_fills_owner_only_when_empty_and_verifies(store):
    store.save(enriched_lead("acme.com"))
    store.save(enriched_lead("named.com", owner_name="Carl Owner", owner_title="Owner"))
    fake = FakeHunter(
        searches=_searches(),
        verdicts={"jane@acme.com": "valid", "bob@acme.com": "accept_all", "ann@named.com": "valid"},
    )
    client = make_client(fake, budget=300)
    stats = run_people_pass(store, client, limit=10, min_score=0, save=cli.save_enriched, now=NOW)

    acme = store.get("acme.com")
    assert acme.owner_name == "Jane Doe" and acme.owner_title == "Founder"
    assert acme.owner_email == "jane@acme.com" and acme.owner_email_status == "verified"
    assert acme.email_pattern == "{first}"
    bob = next(p for p in acme.people if p.name == "Bob Ray")
    assert bob.email_status == "provider" and bob.accept_all
    hunter = acme.source_detail["hunter"]
    assert hunter["results"] == 2 and hunter["credits_used"] == 3 and hunter["pattern"] == "{first}"
    assert hunter["checked_at"] == NOW.isoformat()

    named = store.get("named.com")
    assert named.owner_name == "Carl Owner" and named.owner_title == "Owner"
    assert named.owner_email is None  # a different person: the owner is never replaced
    assert any(p.email == "ann@named.com" and p.email_status == "verified" for p in named.people)

    assert stats.checked == 2 and stats.with_new_email == 2
    assert stats.verified == 2 and stats.dropped == 0 and stats.credits_used == 5
    assert stats.hit_rate == 100.0
    # saved leads are marked for the portal sync
    assert {lead.domain for lead in store.iter(unsynced_only=True)} >= {"acme.com", "named.com"}


def test_pass_drops_unverifiable_candidates(store):
    store.save(enriched_lead("acme.com"))
    fake = FakeHunter(searches=_searches(), verdicts={"jane@acme.com": "webmail"})
    stats = run_people_pass(store, make_client(fake), min_score=0, now=NOW)
    acme = store.get("acme.com")
    assert acme.owner_email is None and acme.owner_name is None
    assert not [p for p in acme.people if p.email]
    assert stats.dropped == 2 and stats.with_new_email == 0
    assert acme.source_detail["hunter"]["kept"] == []


def test_no_verify_keeps_provider_status_without_verifier_calls(store):
    store.save(enriched_lead("acme.com"))
    fake = FakeHunter(searches=_searches())
    stats = run_people_pass(store, make_client(fake), min_score=0, verify=False, now=NOW)
    assert "/v2/email-verifier" not in fake.paths()
    assert store.get("acme.com").owner_email_status == "provider"
    assert stats.credits_used == 1


def test_pass_skips_leads_checked_recently_or_with_person_email(store):
    store.save(
        enriched_lead(
            "recent.com", source_detail={"hunter": {"checked_at": (NOW - timedelta(days=10)).isoformat()}}
        )
    )
    store.save(
        enriched_lead(
            "stale.com", source_detail={"hunter": {"checked_at": (NOW - timedelta(days=91)).isoformat()}}
        )
    )
    store.save(
        enriched_lead("has.com", owner_name="Al Has", owner_email="al@has.com", owner_email_status="on_site")
    )
    fake = FakeHunter()
    stats = run_people_pass(store, make_client(fake), min_score=0, now=NOW)
    searched = [r.url.params["domain"] for r in fake.requests]
    assert searched == ["stale.com"]
    assert stats.skipped_recent == 1 and stats.eligible == 2 and stats.scanned == 3


def test_pass_stops_cleanly_when_budget_is_spent(store):
    for i in range(5):
        store.save(enriched_lead(f"d{i}.com", score=60 - i))
    fake = FakeHunter(
        searches={
            f"d{i}.com": {"emails": [email_row(f"x{i}@d{i}.com", "Xa", f"Yb{i}", "Owner")]} for i in range(5)
        },
        verdicts={f"x{i}@d{i}.com": "valid" for i in range(5)},
    )
    client = make_client(fake, budget=6)  # 3 per lead worst case; each spends 2, so 4 + 3 > 6 stops
    stats = run_people_pass(store, client, min_score=0, now=NOW)
    assert stats.checked == 2 and stats.credits_used == 4
    assert stats.stopped and "budget" in stats.stopped
    assert [r.url.params.get("domain") for r in fake.requests if r.url.path.endswith("domain-search")] == [
        "d0.com",
        "d1.com",
    ]
    assert "hunter" not in store.get("d2.com").source_detail


def test_dry_run_makes_no_calls(store):
    store.save(enriched_lead("acme.com"))
    stats = run_people_pass(store, None, min_score=0, dry_run=True, now=NOW)
    assert stats.domains == ["acme.com"] and stats.checked == 0


def test_daily_includes_people_step_gated_on_key(monkeypatch, tmp_path):
    from datetime import date

    from moh_leadgen.config import settings

    monkeypatch.setattr(settings, "db_path", tmp_path / "leads.db")
    monkeypatch.setattr(settings, "hunter_key", "")
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
    names = [name for name, _ in steps]
    assert names.index("people") == names.index("enrich --retry-failed") + 1
    rows = {n: s for n, s, _ in cli.run_steps([s for s in steps if s[0] == "people"])}
    assert rows["people"] == "skipped"

    calls = []
    monkeypatch.setattr(settings, "hunter_key", "k")
    monkeypatch.setattr(
        cli, "people", lambda **kw: calls.append(kw) or type("S", (), {"summary": lambda s: "ok"})()
    )
    rows = {n: s for n, s, _ in cli.run_steps([s for s in steps if s[0] == "people"])}
    assert rows["people"] == "ok" and calls[0]["limit"] == 150
