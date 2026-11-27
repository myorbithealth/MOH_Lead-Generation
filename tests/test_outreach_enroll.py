"""Enrolment: segments, copy rules, gate reasons, variant choice, personalisation, sync, enrol."""

from __future__ import annotations

import json
import random
from collections import Counter
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from moh_leadgen.models import Lead, Person
from moh_leadgen.outreach import enroll as enroll_mod
from moh_leadgen.outreach.gate import Suppressions, is_sendable, sendable_contacts
from moh_leadgen.outreach.models import Variant
from moh_leadgen.outreach.personalize import (
    build_custom_fields,
    first_name_from,
    get_opener,
    opener_facts,
)
from moh_leadgen.outreach.sequences import (
    CAMPAIGNS,
    FOOTER,
    all_variants,
    assign_segment,
    render,
    smartlead_sequences,
    subject_problems,
    word_count,
)
from moh_leadgen.outreach.smartlead import SmartleadClient
from moh_leadgen.store import LeadStore

NOW = datetime(2026, 11, 27, 15, 0, tzinfo=UTC)


def make_lead(domain: str = "acmehealth.com", **overrides) -> Lead:
    data = {
        "domain": domain,
        "website": f"https://{domain}",
        "company_name": "Acme Health",
        "source": "competitor_platform",
        "vertical": "weight_loss_glp1",
        "competitor_platform": "Healthie",
        "score": 70,
        "last_enriched_at": datetime(2026, 11, 1, tzinfo=UTC),
        "pages_crawled": [f"https://{domain}/", f"https://{domain}/glp-1-program"],
        "has_mx": True,
        "owner_name": "Dr. JANE Smith",
        "owner_email": f"jane@{domain}",
        "owner_email_status": "verified",
        "emails": [f"info@{domain}"],
        "city": "Austin",
        "state": "TX",
    }
    data.update(overrides)
    return Lead(**data)


# ------------------------------------------------------------------ segments + copy
@pytest.mark.parametrize(
    ("overrides", "segment"),
    [
        ({"vertical": "weight_loss_glp1"}, "glp1_competitor"),
        ({"vertical": "peptides_longevity", "competitor_platform": "OpenLoop"}, "glp1_competitor"),
        ({"vertical": "peptides_longevity", "competitor_platform": None}, "trt_peptides"),
        ({"vertical": "mens_health_trt", "competitor_platform": None}, "trt_peptides"),
        ({"vertical": "med_spa", "competitor_platform": None}, "med_spa_clinic"),
        ({"vertical": None, "source": "local_business", "competitor_platform": None}, "med_spa_clinic"),
        ({"vertical": "online_pharmacy"}, "pharmacy_general"),
        ({"vertical": "cbd"}, "pharmacy_general"),
        ({"vertical": "mental_health"}, "other"),
        ({"vertical": None, "competitor_platform": None}, "other"),
    ],
)
def test_assign_segment(overrides, segment):
    assert assign_segment(make_lead(**overrides)) == segment


SAMPLE_FIELDS = {
    "first_name": "Jane",
    "company_name": "Acme Health",
    "opener": " ".join(["word"] * 35),
    "platform": "Healthie",
    "vertical_phrase": "weight-loss programs",
    "booking_link": "https://cal.com/moh/demo?utm_source=smartlead",
    "sender_name": "Shubh",
}


def test_copy_follows_the_house_rules():
    for key, campaign in CAMPAIGNS.items():
        for step in campaign.steps:
            for variant in step.variants:
                text = render(variant.body, SAMPLE_FIELDS)
                limit = 150 if step.step == 1 else 90
                assert word_count(text) < limit, (key, step.step, variant.name)
                assert "{{booking_link}}" in variant.body, (key, variant.name)
                assert "{{" not in text, (key, variant.name)
                assert "$" not in text  # the price-beat guarantee is allowed; specific prices are not
                if step.step == 1:
                    assert subject_problems(variant.subject) == [], (key, variant.subject)
                else:
                    assert variant.subject == ""  # threaded follow-up
    assert "{{postal_address}}" in FOOTER and "{{company_name_legal}}" in FOOTER


def test_step1_has_three_angles_and_a_control_and_follow_ups_are_single():
    for segment in ("glp1_competitor", "trt_peptides", "med_spa_clinic", "pharmacy_general", "other"):
        steps = CAMPAIGNS[segment].steps
        assert [s.day for s in steps] == [0, 3, 7, 14]
        assert {v.name for v in steps[0].variants} == {
            "price_beat",
            "own_the_stack",
            "better_medicine",
            "control",
        }
        assert all(len(s.variants) == 1 for s in steps[1:])
    assert [s.day for s in CAMPAIGNS["generic"].steps] == [0, 5]


def test_variant_rows_have_stable_unique_ids():
    rows = all_variants()
    assert len({r.id for r in rows}) == len(rows)
    assert {r.id for r in rows} == {r.id for r in all_variants()}
    assert sum(1 for r in rows if r.status == "control") == 7  # one per campaign


def test_render_drops_the_name_but_keeps_the_greeting():
    assert render("Hi {{first_name}},\n\nx", {"first_name": ""}) == "Hi,\n\nx"
    assert render("Hi {{first_name}},", {"first_name": "Ana"}) == "Hi Ana,"


def test_smartlead_sequence_delays_are_gaps_between_steps():
    seqs = smartlead_sequences(CAMPAIGNS["other"])
    assert [s["seq_delay_details"]["delay_in_days"] for s in seqs] == [0, 3, 4, 7]
    assert seqs[0]["subject"] == "{{step1_subject}}"
    assert seqs[3]["email_body"] == "{{step4_body}}"


# ------------------------------------------------------------------ gate
def test_gate_accepts_a_verified_owner():
    assert is_sendable(make_lead(), "jane@acmehealth.com", "new") == (True, "ok")


@pytest.mark.parametrize(
    ("overrides", "email", "portal", "reason"),
    [
        ({"last_enriched_at": None}, "jane@acmehealth.com", "new", "not_enriched"),
        ({"pages_crawled": []}, "jane@acmehealth.com", "new", "not_enriched"),
        ({"score": 49}, "jane@acmehealth.com", "new", "low_score"),
        ({"has_mx": False}, "jane@acmehealth.com", "new", "no_mx"),
        ({}, "jane@acmehealth.com", "promoted", "portal_status_promoted"),
        ({"owner_email_status": "guessed"}, "jane@acmehealth.com", "new", "email_unverified"),
        ({}, "info@acmehealth.com", "new", "generic_mailbox"),
        ({"emails": ["sales@acmehealth.com"]}, "sales@acmehealth.com", "new", "role_mailbox_not_allowed"),
        ({"domain": "secure.gethealthie.com"}, "jane@acmehealth.com", "new", "platform_owned_domain"),
        ({}, None, "new", "no_email"),
    ],
)
def test_gate_rejection_reasons(overrides, email, portal, reason):
    assert is_sendable(make_lead(**overrides), email, portal) == (False, reason)


def test_gate_rejects_enrolled_and_suppressed():
    lead = make_lead()
    lead.outreach.enrolled_at = NOW
    assert is_sendable(lead, "jane@acmehealth.com", "new") == (False, "already_enrolled")
    supp = Suppressions()
    supp.add(email="JANE@acmehealth.com")
    assert is_sendable(make_lead(), "jane@acmehealth.com", "new", supp) == (False, "suppressed_email")
    supp.add(domain="acmehealth.com")
    assert is_sendable(make_lead(), "jane@acmehealth.com", "new", supp) == (False, "suppressed_domain")


def test_expired_suppressions_are_ignored():
    supp = Suppressions.from_rows(
        [
            {"email": "a@x.com", "domain": None, "expires_at": "2026-01-01T00:00:00+00:00"},
            {"email": None, "domain": "y.com", "expires_at": None},
        ],
        now=NOW,
    )
    assert not supp.email_blocked("a@x.com")
    assert supp.domain_blocked("y.com")


def test_generic_mailbox_only_with_allow_generic_and_only_one():
    lead = make_lead(owner_email=None, emails=["info@acmehealth.com", "hello@acmehealth.com"])
    assert sendable_contacts(lead, "new")[1] == "generic_mailbox"
    contacts, reason, tier = sendable_contacts(lead, "new", allow_generic=True)
    assert reason is None and tier == "generic"
    assert [c.email for c in contacts] == ["info@acmehealth.com"]


def test_at_most_two_named_contacts_per_company():
    people = [
        Person(name=f"Person {i}", email=f"p{i}@acmehealth.com", email_status="verified") for i in range(4)
    ]
    contacts, _reason, tier = sendable_contacts(make_lead(people=people), "new")
    assert tier == "named" and len(contacts) == 2
    assert contacts[0].email == "jane@acmehealth.com"


# ------------------------------------------------------------------ variants
def _arms(sends=0):
    names = ["a", "b", "c", "control"]
    return [
        Variant(
            id=n,
            segment="other",
            sequence_step=1,
            name=n,
            status="control" if n == "control" else "active",
            sends=sends,
        )
        for n in names
    ]


def test_equal_split_before_any_data():
    rng = random.Random(1)
    picks = Counter(enroll_mod.choose_variant(_arms(), rng).name for _ in range(4000))
    assert all(900 < picks[n] < 1100 for n in ("a", "b", "c", "control"))


def test_thompson_prefers_the_winner_but_keeps_the_control_floor():
    arms = _arms(sends=300)
    arms[0].alpha, arms[0].beta = 30, 270  # 10% positive
    for arm in arms[1:]:
        arm.alpha, arm.beta = 3, 297  # 1%
    rng = random.Random(7)
    picks = Counter(enroll_mod.choose_variant(arms, rng).name for _ in range(3000))
    assert picks["a"] > 0.8 * 3000
    assert picks["control"] >= 0.09 * 3000


def test_retired_variants_are_never_chosen():
    arms = _arms()
    for arm in arms[:3]:
        arm.status = "retired"
    assert enroll_mod.choose_variant(arms, random.Random(0)).name == "control"


# ------------------------------------------------------------------ personalisation
def test_first_name_parsing():
    assert first_name_from("Dr. JANE Smith") == "Jane"
    assert first_name_from("john o'neil") == "John"
    assert first_name_from("J. Smith") is None
    assert first_name_from("Acme Health LLC Team Members Inc") is None
    assert first_name_from("") is None
    assert first_name_from("Mary-Kate Olsen") == "Mary-Kate"


def test_custom_fields_from_a_lead(monkeypatch):
    monkeypatch.setattr("moh_leadgen.config.settings.booking_url", "https://cal.com/moh/demo")
    lead = make_lead()
    opener, source = get_opener(lead, None)
    assert source == "template"
    fields = build_custom_fields(
        lead,
        segment="glp1_competitor",
        opener=opener,
        utm_content="s1_control",
        first_name="Jane",
        company_name_legal="MyOrbit Health Inc.",
        postal_address="1 Main St, Austin, TX 78701",
    )
    assert fields["platform"] == "Healthie"
    assert fields["vertical_phrase"] == "weight-loss programs"
    assert fields["booking_link"].startswith("https://cal.com/moh/demo?utm_source=smartlead")
    assert "utm_campaign=glp1_competitor" in fields["booking_link"]
    assert fields["postal_address"] == "1 Main St, Austin, TX 78701"
    assert "Healthie" in opener or "LegitScript" in opener


def test_rep_booking_link_wins():
    rep = SimpleNamespace(rep_name="Divang", booking_link="https://cal.com/divang?x=1")
    fields = build_custom_fields(make_lead(), rep, segment="other", opener="o")
    assert fields["sender_name"] == "Divang"
    assert fields["booking_link"].startswith("https://cal.com/divang?x=1&utm_source=smartlead")


class FakeAnthropic:
    """Mimics client.beta.messages.create returning a structured-output message."""

    def __init__(
        self,
        text=(
            '{"opener": "Acme Health runs its GLP-1 program on Healthie, so you already pay a platform '
            'fee, a consult charge per medication and a cut of every order. We would change all three."}'
        ),
        stop="end_turn",
    ):
        self.calls = []
        self.text = text
        self.stop = stop
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(stop_reason=self.stop, content=[SimpleNamespace(type="text", text=self.text)])


def test_claude_opener_is_schema_restricted_and_cached():
    lead = make_lead()
    fake = FakeAnthropic()
    text, source = get_opener(lead, fake)
    assert source == "claude" and "Healthie" in text
    call = fake.calls[0]
    assert call["model"] == "claude-sonnet-5-5"
    fmt = call["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["schema"]["additionalProperties"] is False
    assert fmt["schema"]["required"] == ["opener"]
    # same facts -> cache hit, no second call
    assert get_opener(lead, fake) == (text, "cache")
    assert len(fake.calls) == 1
    # facts change -> new call
    lead.city = "Dallas"
    get_opener(lead, fake)
    assert len(fake.calls) == 2


@pytest.mark.parametrize(
    ("text", "stop"),
    [
        ('{"opener": "x"}', "refusal"),
        ('{"opener": "x"}', "max_tokens"),
        ('{"opener": "' + "word " * 130 + '"}', "end_turn"),
        ("not json", "end_turn"),
    ],
)
def test_bad_claude_output_falls_back_to_the_template(text, stop):
    lead = make_lead()
    opener, source = get_opener(lead, FakeAnthropic(text, stop))
    assert source == "template"
    assert "opener" not in lead.source_detail.get("ai", {})
    assert opener


def test_opener_facts_reference_concrete_details():
    facts = opener_facts(make_lead(legitscript={"status": "seal_detected"}))
    assert facts["platform"] == "Healthie"
    assert facts["legitscript"] == "displays a LegitScript certification seal"
    assert facts["city"] == "Austin, TX"
    assert facts["pages"] == "glp-1-program"


# ------------------------------------------------------------------ fakes for sync / enroll
class FakeSink:
    def __init__(self, statuses=None):
        self.statuses = statuses or {}
        self.suppressions: list[dict] = []
        self.contacts: dict[str, dict] = {}
        self.drafts: list[dict] = []
        self.lead_updates: list[tuple[str, dict]] = []
        self.activities: list[dict] = []
        self.variants: list[dict] = []
        self.settings = {
            "outreach": {
                "postal_address": "1 Main St, Austin, TX 78701",
                "company_name": "MyOrbit Health Inc.",
            }
        }
        self.rpcs: list[str] = []
        self.draft_updates: list[tuple[str, dict]] = []

    def fetch_status(self, domains):
        return {d: self.statuses.get(d, "new") for d in domains}

    def fetch_suppressions(self):
        return self.suppressions

    def upsert_suppressions(self, rows):
        self.suppressions += rows
        return len(rows)

    def upsert_contacts(self, rows):
        for row in rows:
            self.contacts.setdefault(row["email"], {"id": f"c-{len(self.contacts) + 1}", **row})
        return {r["email"]: self.contacts[r["email"]]["id"] for r in rows}

    def fetch_contacts_by_email(self, emails):
        return {e: self.contacts[e] for e in emails if e in self.contacts}

    def insert_email_drafts(self, rows):
        have = {(d["smartlead_lead_id"], d["sequence_step"]) for d in self.drafts}
        new = [r for r in rows if (r["smartlead_lead_id"], r["sequence_step"]) not in have]
        for r in new:
            self.drafts.append({"id": f"d-{len(self.drafts) + 1}", **r})
        return len(new)

    def fetch_email_drafts(self, lead_ids):
        return [d for d in self.drafts if d["smartlead_lead_id"] in set(lead_ids)]

    def update_email_draft(self, draft_id, fields):
        self.draft_updates.append((draft_id, fields))
        next(d for d in self.drafts if d["id"] == draft_id).update(fields)

    def update_lead_outreach(self, domain, fields):
        self.lead_updates.append((domain, fields))

    def insert_activity(self, row):
        self.activities.append(row)

    def fetch_variants(self, segment=None):
        return [v for v in self.variants if segment is None or v["segment"] == segment]

    def upsert_variants(self, rows):
        have = {(v["segment"], v["sequence_step"], v["name"]) for v in self.variants}
        self.variants += [r for r in rows if (r["segment"], r["sequence_step"], r["name"]) not in have]
        return {(r["segment"], r["sequence_step"], r["name"]): r["id"] for r in rows}

    def get_app_setting(self, key):
        return self.settings.get(key)

    def set_app_setting(self, key, value):
        self.settings[key] = value

    def rpc(self, name, args=None):
        self.rpcs.append(name)


class FakeSmartlead:
    """A tiny in-memory Smartlead behind httpx.MockTransport, so the real client code runs."""

    def __init__(self):
        self.campaigns: dict[int, dict] = {}
        self.leads: dict[int, dict[str, int]] = {}
        self.requests: list[tuple[str, str, dict | None]] = []
        self.stats: dict[int, list[dict]] = {}
        self.next_lead_id = 1000
        self.return_map = True  # the undocumented emailToLeadIdMap; real Smartlead may omit it

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        path = request.url.path.removeprefix("/api/v1")
        self.requests.append((request.method, path, body))
        parts = path.strip("/").split("/")
        if path == "/campaigns/" and request.method == "GET":
            return httpx.Response(200, json=list(self.campaigns.values()))
        if path == "/campaigns/create":
            cid = 100 + len(self.campaigns)
            self.campaigns[cid] = {"id": cid, "name": body["name"]}
            return httpx.Response(200, json={"ok": True, **self.campaigns[cid]})
        if len(parts) == 3 and parts[2] == "sequences" and request.method == "GET":
            return httpx.Response(200, json=[])
        if len(parts) == 3 and parts[2] == "webhooks" and request.method == "GET":
            return httpx.Response(200, json={"success": True, "data": []})
        if len(parts) == 3 and parts[2] == "leads" and request.method == "POST":
            cid = int(parts[1])
            existing = self.leads.setdefault(cid, {})
            new, old = {}, {}
            for lead in body["lead_list"]:
                if lead["email"] in existing:
                    old[lead["email"]] = existing[lead["email"]]
                else:
                    self.next_lead_id += 1
                    existing[lead["email"]] = new[lead["email"]] = self.next_lead_id
            body = {"ok": True, "upload_count": len(new), "already_added_to_campaign": len(old)}
            if self.return_map:
                body["emailToLeadIdMap"] = {"newlyAddedLeads": new, "existingLeads": old}
            return httpx.Response(200, json=body)
        if path == "/leads/" and request.method == "GET":
            email = request.url.params["email"]
            for cid, leads in self.leads.items():
                if email in leads:
                    return httpx.Response(
                        200,
                        json={
                            "id": leads[email],
                            "email": email,
                            "lead_campaign_data": [{"campaign_id": cid}],
                        },
                    )
            return httpx.Response(200, json={})
        if len(parts) == 3 and parts[2] == "statistics":
            rows = self.stats.get(int(parts[1]), [])
            offset = int(request.url.params.get("offset", 0))
            limit = int(request.url.params.get("limit", 100))
            return httpx.Response(200, json={"total_stats": len(rows), "data": rows[offset : offset + limit]})
        return httpx.Response(200, json={"ok": True})

    def client(self) -> SmartleadClient:
        return SmartleadClient(
            "k",
            "https://server.smartlead.ai/api/v1",
            transport=httpx.MockTransport(self.handler),
            sleep=lambda s: None,
        )

    def calls(self, method: str, suffix: str) -> list[dict | None]:
        return [b for m, p, b in self.requests if m == method and p.endswith(suffix)]


@pytest.fixture
def store(tmp_path):
    s = LeadStore(tmp_path / "leads.db")
    s.save(make_lead("acmehealth.com"))
    s.save(
        make_lead(
            "betaclinic.com",
            company_name="Beta Clinic",
            vertical="med_spa",
            competitor_platform=None,
            score=65,
            owner_name="Sam Lee",
            owner_email="sam@betaclinic.com",
        )
    )
    s.save(make_lead("lowscore.com", score=10))
    s.save(make_lead("infoonly.com", owner_email=None, emails=["info@infoonly.com"], score=60))
    return s


@pytest.fixture
def settings_env(monkeypatch):
    monkeypatch.setattr(
        "moh_leadgen.config.settings.smartlead_webhook_url",
        "https://p.supabase.co/functions/v1/smartlead-webhook",
    )
    monkeypatch.setattr("moh_leadgen.config.settings.smartlead_webhook_secret", "whsec")
    monkeypatch.setattr("moh_leadgen.config.settings.smartlead_email_account_ids", "11, 12")
    monkeypatch.setattr("moh_leadgen.config.settings.booking_url", "https://cal.com/moh/demo")


# ------------------------------------------------------------------ sync
def test_sync_campaigns_dry_run_payload_shape(settings_env):
    report = enroll_mod.sync_campaigns(None, None, dry_run=True, daily_cap=300)
    assert set(report["campaigns"]) == {
        "glp1_competitor",
        "trt_peptides",
        "med_spa_clinic",
        "pharmacy_general",
        "other",
        "fda_compliance",
        "generic",
    }
    p = report["campaigns"]["glp1_competitor"]
    assert p["name"] == "MOH outbound - glp1_competitor"
    assert [s["seq_number"] for s in p["sequences"]] == [1, 2, 3, 4]
    assert p["schedule"]["max_new_leads_per_day"] == 75
    assert p["schedule"]["days"] == [1, 2, 3, 4, 5]
    assert p["settings"]["unsubscribe_text"]
    assert p["email_account_ids"] == [11, 12]
    assert "EMAIL_REPLY" in p["webhook"]["event_types"]
    assert report["campaigns"]["generic"]["schedule"]["max_new_leads_per_day"] == 150


def test_sync_campaigns_creates_once_and_records_ids(settings_env, tmp_path):
    fake, sink = FakeSmartlead(), FakeSink()
    state_path = tmp_path / "state.json"
    client = fake.client()
    enroll_mod.sync_campaigns(client, sink, state_path=state_path, daily_cap=300)
    enroll_mod.sync_campaigns(client, sink, state_path=state_path, daily_cap=300)

    assert len(fake.campaigns) == 7  # second run found them, created nothing
    state = json.loads(state_path.read_text())
    assert state["campaigns"]["other"]["id"] in fake.campaigns
    assert "s1_control" in state["campaigns"]["other"]["variants"]
    webhook = fake.calls("POST", "/webhooks")[0]
    assert webhook["webhook_url"].endswith("?token=whsec")
    assert fake.calls("POST", "/email-accounts")[0] == {"email_account_ids": [11, 12]}
    schedule = fake.calls("POST", "/schedule")[0]
    assert schedule["timezone"] and schedule["max_new_leads_per_day"] == 75
    assert sink.settings["outreach"]["campaigns"]["generic"] in fake.campaigns
    assert sink.settings["outreach"]["postal_address"]  # merged, not replaced
    labels = {v["smartlead_variant_label"] for v in sink.variants}
    assert "s1_price_beat" in labels and len(sink.variants) == len(all_variants())


# ------------------------------------------------------------------ enroll
def _synced(tmp_path, settings_env):
    fake, sink = FakeSmartlead(), FakeSink()
    state_path = tmp_path / "state.json"
    enroll_mod.sync_campaigns(fake.client(), sink, state_path=state_path, daily_cap=300)
    return fake, sink, state_path


def test_plan_counts_by_segment_and_reason(store):
    plan = enroll_mod.plan_enrolment(store, FakeSink())
    assert plan.by_segment == Counter({"glp1_competitor": 1, "med_spa_clinic": 1})
    assert plan.rejections == Counter({"low_score": 1, "generic_mailbox": 1})
    plan = enroll_mod.plan_enrolment(store, FakeSink(), allow_generic=True)
    assert plan.by_segment["generic"] == 1


def test_enroll_is_idempotent_and_writes_store_and_portal(store, tmp_path, settings_env):
    fake, sink, state_path = _synced(tmp_path, settings_env)
    client = fake.client()
    first = enroll_mod.enroll(
        store, client, sink, daily_cap=300, rng=random.Random(1), now=NOW, state_path=state_path
    )
    assert len(first.enrolled) == 2
    added = fake.calls("POST", "/leads")
    assert sum(len(b["lead_list"]) for b in added) == 2
    payload = added[0]["lead_list"][0]
    cf = payload["custom_fields"]
    assert payload["first_name"] in ("Jane", "Sam")
    assert {"step1_subject", "step1_body", "step4_body", "opener", "booking_link", "postal_address"} <= set(
        cf
    )
    assert "1 Main St" in cf["step1_body"] and "{{" not in cf["step1_body"]

    lead = store.get("acmehealth.com")
    assert lead.outreach.enrolled_at == NOW
    assert lead.outreach.smartlead_lead_ids["jane@acmehealth.com"] > 1000
    assert lead.outreach.segment == "glp1_competitor"
    assert lead.status == "enrolled"
    assert "outreach_portal_pending" not in lead.source_detail

    assert len(sink.drafts) == 2 and all(
        d["status"] == "queued" and d["sequence_step"] == 1 for d in sink.drafts
    )
    assert all(d["variant_id"] and d["contact_id"] for d in sink.drafts)
    assert {d for d, _ in sink.lead_updates} == {"acmehealth.com", "betaclinic.com"}
    assert len(sink.activities) == 2

    second = enroll_mod.enroll(
        store, client, sink, daily_cap=300, rng=random.Random(1), now=NOW, state_path=state_path
    )
    assert second.enrolled == []
    assert len(fake.calls("POST", "/leads")) == len(added)  # nothing re-added
    assert len(sink.drafts) == 2


def test_enroll_records_leads_when_smartlead_returns_no_id_map(store, tmp_path, settings_env):
    """The documented add-leads response has no lead ids; enrolment must still be recorded."""
    fake, sink, state_path = _synced(tmp_path, settings_env)
    fake.return_map = False
    result = enroll_mod.enroll(
        store, fake.client(), sink, daily_cap=300, rng=random.Random(1), now=NOW, state_path=state_path
    )
    assert len(result.enrolled) == 2 and result.smartlead_skipped == 0
    lead = store.get("acmehealth.com")
    assert lead.outreach.enrolled_at == NOW
    assert lead.outreach.smartlead_lead_ids["jane@acmehealth.com"] > 1000
    assert "smartlead_skipped" not in lead.source_detail
    assert len(sink.drafts) == 2


def test_daily_cap_counts_sends_across_runs(store, tmp_path, settings_env):
    fake, sink, state_path = _synced(tmp_path, settings_env)
    client = fake.client()
    first = enroll_mod.enroll(
        store, client, sink, daily_cap=4, rng=random.Random(1), now=NOW, state_path=state_path
    )
    assert len(first.enrolled) == 1 and first.skipped_cap == 1  # one 4-step contact fits
    again = enroll_mod.enroll(
        store, client, sink, daily_cap=4, rng=random.Random(1), now=NOW, state_path=state_path
    )
    assert again.enrolled == [] and again.used_before == 4


def test_dry_run_writes_nothing(store, tmp_path, settings_env):
    fake, sink, state_path = _synced(tmp_path, settings_env)
    before = len(fake.requests)
    result = enroll_mod.enroll(
        store, None, sink, daily_cap=300, dry_run=True, rng=random.Random(1), now=NOW, state_path=state_path
    )
    assert len(result.enrolled) == 2
    assert len(fake.requests) == before
    assert store.get("acmehealth.com").outreach.enrolled_at is None
    assert sink.drafts == []


def test_enroll_refuses_without_a_postal_address(store, tmp_path, settings_env, monkeypatch):
    fake, sink, state_path = _synced(tmp_path, settings_env)
    sink.settings["outreach"]["postal_address"] = "TO BE CONFIRMED"
    monkeypatch.setattr(
        "moh_leadgen.config.settings.outreach_postal_address", ""
    )  # a local .env must not leak in
    with pytest.raises(RuntimeError, match="postal address"):
        enroll_mod.enroll(store, fake.client(), sink, daily_cap=300, now=NOW, state_path=state_path)


def test_portal_failure_is_retried_next_run(store, tmp_path, settings_env):
    fake, sink, state_path = _synced(tmp_path, settings_env)

    def broken(rows):
        raise RuntimeError("supabase down")

    good = sink.upsert_contacts
    sink.upsert_contacts = broken
    result = enroll_mod.enroll(
        store, fake.client(), sink, daily_cap=300, rng=random.Random(1), now=NOW, state_path=state_path
    )
    assert result.portal_errors == 2 and sink.drafts == []
    assert "outreach_portal_pending" in store.get("acmehealth.com").source_detail
    sink.upsert_contacts = good
    enroll_mod.enroll(
        store, fake.client(), sink, daily_cap=300, rng=random.Random(1), now=NOW, state_path=state_path
    )
    assert len(sink.drafts) == 2
    assert "outreach_portal_pending" not in store.get("acmehealth.com").source_detail


def test_pull_stats_updates_drafts_and_refreshes_variants(store, tmp_path, settings_env):
    fake, sink, state_path = _synced(tmp_path, settings_env)
    client = fake.client()
    enroll_mod.enroll(
        store, client, sink, daily_cap=300, rng=random.Random(1), now=NOW, state_path=state_path
    )
    lead = store.get("acmehealth.com")
    cid = lead.outreach.smartlead_campaign_id
    fake.stats[cid] = [
        {
            "lead_email": "jane@acmehealth.com",
            "sequence_number": 1,
            "sent_time": "2026-11-28T14:00:00+00:00",
            "is_opened": True,
            "open_time": "2026-11-28T15:00:00+00:00",
            "is_replied": False,
            "is_bounced": False,
        },
        {
            "lead_email": "jane@acmehealth.com",
            "sequence_number": 2,
            "sent_time": "2026-12-01T14:00:00+00:00",
            "is_opened": False,
            "is_replied": True,
            "reply_time": "2026-12-01T16:00:00+00:00",
            "is_bounced": False,
        },
    ]
    counts = enroll_mod.pull_stats(store, client, sink, state_path=state_path)
    assert counts["updated"] == 1 and counts["inserted"] == 1
    step1 = next(d for d in sink.drafts if d["to_email"] == "jane@acmehealth.com" and d["sequence_step"] == 1)
    assert step1["status"] == "opened" and step1["opened_at"].startswith("2026-11-28T15")
    step2 = next(d for d in sink.drafts if d["sequence_step"] == 2)
    assert step2["status"] == "replied" and step2["variant_id"]
    assert sink.rpcs == ["outreach_refresh_variants"]
    assert store.get("acmehealth.com").outreach.last_outreach_at.isoformat().startswith("2026-12-01")
    # re-running changes nothing
    assert enroll_mod.pull_stats(store, client, sink, state_path=state_path)["updated"] == 0


def test_pull_stats_ignores_reply_drafts(store, tmp_path, settings_env):
    """Reply drafts are direction 'outbound' with a smartlead_lead_id but no sequence_step."""
    fake, sink, state_path = _synced(tmp_path, settings_env)
    client = fake.client()
    enroll_mod.enroll(
        store, client, sink, daily_cap=300, rng=random.Random(1), now=NOW, state_path=state_path
    )
    lead = store.get("acmehealth.com")
    lead_id = lead.outreach.smartlead_lead_ids["jane@acmehealth.com"]
    sink.drafts.append(
        {
            "id": "reply-draft",
            "smartlead_lead_id": lead_id,
            "sequence_step": None,
            "direction": "outbound",
            "status": "pending_approval",
            "to_email": "jane@acmehealth.com",
        }
    )
    fake.stats[lead.outreach.smartlead_campaign_id] = [
        {"lead_email": "jane@acmehealth.com", "sequence_number": 1, "sent_time": "2026-11-28T14:00:00+00:00"}
    ]
    counts = enroll_mod.pull_stats(store, client, sink, state_path=state_path)
    assert counts["updated"] == 1 and counts["inserted"] == 0
    assert next(d for d in sink.drafts if d["id"] == "reply-draft")["status"] == "pending_approval"


def test_suppress_domain_everywhere(store, tmp_path, settings_env):
    fake, sink, state_path = _synced(tmp_path, settings_env)
    client = fake.client()
    enroll_mod.enroll(
        store, client, sink, daily_cap=300, rng=random.Random(1), now=NOW, state_path=state_path
    )
    out = enroll_mod.suppress(
        "AcmeHealth.com", "not_interested", store=store, sink=sink, client=client, now=NOW
    )
    assert out["smartlead_unsubscribed"] == 1
    assert sink.suppressions[0]["domain"] == "acmehealth.com" and sink.suppressions[0]["expires_at"]
    assert store.get("acmehealth.com").outreach.suppressed_at == NOW
    assert fake.calls("POST", "/add-domain-block-list") == []  # not_interested expires; no global block
    enroll_mod.suppress("x@betaclinic.com", "unsubscribe", store=store, sink=sink, client=client, now=NOW)
    assert fake.calls("POST", "/add-domain-block-list") == [{"domain_block_list": ["x@betaclinic.com"]}]
    with pytest.raises(ValueError):
        enroll_mod.suppress("a@b.com", "spam", store=store)
