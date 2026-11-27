"""FDA warning-letter leads: own segment, compliance-only copy, human review before the gate passes."""

from __future__ import annotations

import csv
import random

from test_outreach_enroll import SAMPLE_FIELDS, FakeSink, make_lead
from typer.testing import CliRunner

from moh_leadgen import cli
from moh_leadgen.config import settings
from moh_leadgen.discovery.seedfile import SeedFileDiscovery
from moh_leadgen.models import Candidate
from moh_leadgen.outreach import cli_enroll
from moh_leadgen.outreach import enroll as enroll_mod
from moh_leadgen.outreach.cli_enroll import review_rows, set_review
from moh_leadgen.outreach.gate import is_sendable, lead_rejection, sendable_contacts
from moh_leadgen.outreach.metrics import lint_variant
from moh_leadgen.outreach.models import SEGMENTS
from moh_leadgen.outreach.personalize import (
    _AI_TELLS,
    _ANGLE_HINTS,
    _valid_opener,
    get_opener,
    opener_facts,
    template_opener,
)
from moh_leadgen.outreach.sequences import (
    _PROOF,
    CAMPAIGN_KEYS,
    CAMPAIGNS,
    FOOTER,
    assign_segment,
    render,
    smartlead_sequences,
    subject_problems,
    with_footer,
    word_count,
)
from moh_leadgen.store import LeadStore

FDA_VIA = "seedfile:public_lists_2026-11-27.txt"
FORBIDDEN = ("fda", "warning", "enforcement", "beat")
runner = CliRunner()


def fda_lead(domain: str = "ronanrx.com", **overrides):
    return make_lead(domain, discovered_via=FDA_VIA, **overrides)


# ------------------------------------------------------------------ segment
def test_fda_segment_is_registered_and_gets_its_own_campaign():
    assert "fda_compliance" in SEGMENTS and "fda_compliance" in CAMPAIGN_KEYS
    assert CAMPAIGNS["fda_compliance"].name == "MOH outbound - fda_compliance"
    assert "fda_compliance" in _PROOF
    report = enroll_mod.sync_campaigns(None, None, dry_run=True, daily_cap=300)
    seqs = report["campaigns"]["fda_compliance"]["sequences"]
    assert [s["seq_delay_details"]["delay_in_days"] for s in seqs] == [0, 4, 6]


def test_fda_list_wins_over_every_other_rule():
    # weight-loss on a competitor platform would be glp1_competitor; the FDA list comes first
    assert assign_segment(make_lead()) == "glp1_competitor"
    assert assign_segment(fda_lead()) == "fda_compliance"
    assert assign_segment(fda_lead(vertical="med_spa", source="local_business")) == "fda_compliance"
    assert assign_segment(make_lead(source_detail={"fda_list": True})) == "fda_compliance"
    assert assign_segment(make_lead(also_found_via=[FDA_VIA])) == "fda_compliance"
    assert (
        assign_segment(make_lead(discovered_via="seedfile:legitscript_seed_domains.txt")) == "glp1_competitor"
    )


def test_store_flags_fda_seed_candidates(tmp_path):
    seed = tmp_path / "public_lists_2026-11-27.txt"
    seed.write_text("# comment\nronanrx.com\nmintmed.com\n")
    store = LeadStore(tmp_path / "leads.db")
    store.save(make_lead("mintmed.com", discovered_via="competitor:healthie"))  # already known
    for cand in SeedFileDiscovery(seed, "competitor_platform").discover():
        store.add_candidate(cand)
    assert store.get("ronanrx.com").source_detail["fda_list"] is True
    known = store.get("mintmed.com")
    assert known.source_detail["fda_list"] is True and FDA_VIA in known.also_found_via
    assert assign_segment(known) == "fda_compliance"
    store.add_candidate(Candidate(domain="other.com", source="manual", discovered_via="seedfile:x.txt"))
    assert "fda_list" not in store.get("other.com").source_detail


# ------------------------------------------------------------------ copy
def _rendered(campaign_key: str) -> list[tuple[int, str, str]]:
    lead = fda_lead()
    opener = template_opener(opener_facts(lead, "compliance_foundation"))
    fields = {**SAMPLE_FIELDS, "opener": opener}
    out = []
    for step in CAMPAIGNS[campaign_key].steps:
        for v in step.variants:
            out.append((step.step, render(v.subject, fields), render(v.body, fields)))
    return out


def test_fda_copy_follows_the_house_rules():
    campaign = CAMPAIGNS["fda_compliance"]
    assert [s.day for s in campaign.steps] == [0, 4, 10]
    assert all(len(s.variants) == 1 for s in campaign.steps)
    assert campaign.steps[0].variants[0].subject == "prescribing and pharmacy setup"
    assert campaign.steps[0].variants[0].angle == "compliance_foundation"
    for step in campaign.steps:
        v = step.variants[0]
        text = render(v.body, SAMPLE_FIELDS)
        assert word_count(text) < (150 if step.step == 1 else 90), (step.step, v.name)
        assert "{{booking_link}}" in v.body
        assert "20 minutes" in text or "Twenty minutes" in text
        assert "{{" not in text and "$" not in text and "!" not in text and "—" not in text
        assert not _AI_TELLS.search(text), v.name
        assert text.rstrip().endswith(SAMPLE_FIELDS["sender_name"])
        if step.step == 1:
            assert subject_problems(v.subject) == []
            assert v.body.startswith("Hi {{first_name}},\n\n{{opener}}\n\n")
            assert lint_variant({"subject": v.subject, "body": v.body}) is None
        else:
            assert v.subject == ""
            assert word_count(text) < 70, v.name
    assert "{{postal_address}}" in FOOTER


def test_fda_step1_says_what_we_run_and_what_they_keep():
    body = CAMPAIGNS["fda_compliance"].steps[0].variants[0].body
    for phrase in (
        "physician network",
        "licensed prescribers in every state",
        "LegitScript-reviewed pharmacy partners",
        "proper intake",
        "MyOrbit Health runs exactly that under your brand",
        "storefront",
        "processor",
        "patient data",
    ):
        assert phrase in body, phrase


def test_fda_rendered_text_never_mentions_regulators_or_the_price_pitch():
    for _, subject, body in _rendered("fda_compliance"):
        text = with_footer(f"{subject}\n{body}").lower()
        for word in FORBIDDEN:
            assert word not in text, word
    # and the normal campaigns do carry the pitch, so the check is not vacuous
    assert any("beat" in body.lower() for _, _, body in _rendered("glp1_competitor"))


def test_fda_opener_angle_hint_and_validation():
    hint = _ANGLE_HINTS["compliance_foundation"]
    assert "LegitScript-reviewed" in hint and "without mentioning regulators or enforcement" in hint
    ok = (
        "Ronan sells weight-loss programs online with a Stripe checkout and runs paid ads. Prescribing "
        "and fulfilment likely sit with a vendor you don't control, and that's where programs wobble."
    )
    assert _valid_opener(ok, "compliance_foundation")
    flagged = ok.replace("wobble", "draw FDA warning letters")
    assert _valid_opener(flagged) and not _valid_opener(flagged, "compliance_foundation")
    opener, source = get_opener(fda_lead(), None, angle="compliance_foundation")
    assert source == "template" and "Ronan" not in opener
    assert not any(w in opener.lower() for w in FORBIDDEN)


# ------------------------------------------------------------------ gate
def test_gate_rejects_unreviewed_fda_leads_and_passes_approved_ones():
    lead = fda_lead()
    assert lead_rejection(lead) == "fda_list_not_reviewed"
    assert sendable_contacts(lead, mx_check=None)[1] == "fda_list_not_reviewed"
    assert is_sendable(lead, lead.owner_email, mx_check=None) == (False, "fda_list_not_reviewed")
    lead.source_detail["fda_review"] = "rejected"
    assert lead_rejection(lead) == "fda_list_not_reviewed"
    lead.source_detail["fda_review"] = "approved"
    contacts, reason, tier = sendable_contacts(lead, mx_check=None)
    assert reason is None and tier == "named" and contacts[0].email == "jane@ronanrx.com"
    # the other lead-level rules still apply first
    assert lead_rejection(fda_lead(score=5)) == "low_score"


def test_plan_counts_fda_rejections_and_never_uses_the_generic_campaign(tmp_path):
    store = LeadStore(tmp_path / "leads.db")
    store.save(fda_lead("ronanrx.com"))
    store.save(fda_lead("mintmed.com", owner_email=None, emails=["info@mintmed.com"]))
    store.save(make_lead("acmehealth.com"))
    plan = enroll_mod.plan_enrolment(store, FakeSink(), allow_generic=True, mx_check=None)
    assert plan.rejections["fda_list_not_reviewed"] == 2
    assert [c.lead.domain for c in plan.candidates] == ["acmehealth.com"]

    set_review(store, ["ronanrx.com", "mintmed.com"], "approved")
    plan = enroll_mod.plan_enrolment(store, FakeSink(), allow_generic=True, mx_check=None)
    keys = {c.lead.domain: c.campaign_key for c in plan.candidates}
    assert keys == {
        "acmehealth.com": "glp1_competitor",
        "ronanrx.com": "fda_compliance",
        "mintmed.com": "fda_compliance",
    }

    result = enroll_mod.enroll(
        store, None, FakeSink(), daily_cap=300, allow_generic=True, dry_run=True, rng=random.Random(1)
    )
    fda_rows = [r for r in result.enrolled if r["campaign"] == "fda_compliance"]
    assert {r["variant"] for r in fda_rows} == {"compliance_foundation"}
    assert sum(r["cost"] for r in fda_rows) == 6  # 3 steps x 2 contacts


# ------------------------------------------------------------------ review tooling
def _review_store(tmp_path, monkeypatch) -> LeadStore:
    monkeypatch.setattr(settings, "db_path", tmp_path / "leads.db")
    monkeypatch.setattr(cli_enroll.console, "width", 250)  # no column truncation in the captured output
    store = LeadStore(tmp_path / "leads.db")
    store.save(fda_lead("ronanrx.com", score=80))
    store.save(fda_lead("mintmed.com", score=60, company_name="Mint Med"))
    store.save(make_lead("acmehealth.com"))
    return store


def test_review_list_prints_and_writes_csv(tmp_path, monkeypatch):
    _review_store(tmp_path, monkeypatch)
    out = tmp_path / "review.csv"
    result = runner.invoke(cli.app, ["outreach", "review-list", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert "ronanrx.com" in result.output and "acmehealth.com" not in result.output
    assert "2 leads in fda_compliance" in result.output
    rows = list(csv.DictReader(out.open()))
    assert [r["domain"] for r in rows] == ["ronanrx.com", "mintmed.com"]
    assert rows[1] == {
        "domain": "mintmed.com",
        "company": "Mint Med",
        "vertical": "weight_loss_glp1",
        "score": "60",
        "owner": "Dr. JANE Smith",
        "email": "jane@mintmed.com",
        "discovered_via": FDA_VIA,
        "fda_review": "pending",
    }


def test_review_approve_persists_and_unblocks_the_gate(tmp_path, monkeypatch):
    _review_store(tmp_path, monkeypatch)
    result = runner.invoke(
        cli.app, ["outreach", "review-approve", "RonanRx.com", "nosuch.com", "acmehealth.com"]
    )
    assert result.exit_code == 0, result.output
    assert "approved 1" in result.output and "nosuch.com" in result.output
    store = LeadStore(tmp_path / "leads.db")
    approved = store.get("ronanrx.com")
    assert approved.source_detail["fda_review"] == "approved" and approved.source_detail["fda_review_at"]
    assert lead_rejection(approved) is None
    assert "fda_review" not in store.get("acmehealth.com").source_detail

    listing = tmp_path / "reject.txt"
    listing.write_text("# not a fit\nmintmed.com\nronanrx.com  # changed our mind\n")
    result = runner.invoke(
        cli.app, ["outreach", "review-approve", "--all-from-file", str(listing), "--reject"]
    )
    assert result.exit_code == 0, result.output
    assert "rejected 2" in result.output
    assert {r["domain"]: r["fda_review"] for r in review_rows(LeadStore(tmp_path / "leads.db"))} == {
        "ronanrx.com": "rejected",
        "mintmed.com": "rejected",
    }
    assert lead_rejection(LeadStore(tmp_path / "leads.db").get("ronanrx.com")) == "fda_list_not_reviewed"


def test_review_approve_needs_a_domain(tmp_path, monkeypatch):
    _review_store(tmp_path, monkeypatch)
    result = runner.invoke(cli.app, ["outreach", "review-approve"])
    assert result.exit_code != 0


def test_smartlead_sequences_cover_three_steps():
    seqs = smartlead_sequences(CAMPAIGNS["fda_compliance"])
    assert [s["email_body"] for s in seqs] == ["{{step1_body}}", "{{step2_body}}", "{{step3_body}}"]
