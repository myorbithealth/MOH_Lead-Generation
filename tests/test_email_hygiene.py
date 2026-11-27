"""Email hygiene: cleaning, person/role/junk classification, shared addresses, free-mail, MX."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from types import SimpleNamespace

import dns.resolver
import pytest

from moh_leadgen.enrich.email_hygiene import (
    classify_local_part,
    clean_email,
    email_owners,
    has_mx,
    is_cross_company_duplicate,
    normalize_lead_emails,
)
from moh_leadgen.enrich.extract import extract_emails, organization_from_jsonld
from moh_leadgen.enrich.site_crawler import SiteSnapshot, _parse
from moh_leadgen.models import Lead, Person
from moh_leadgen.outreach import enroll as enroll_mod
from moh_leadgen.outreach.gate import is_sendable, sendable_contacts
from moh_leadgen.store import LeadStore


def make_lead(domain: str = "acmehealth.com", **overrides) -> Lead:
    data = {
        "domain": domain,
        "website": f"https://{domain}",
        "company_name": "Acme Health",
        "vertical": "weight_loss_glp1",
        "competitor_platform": "Healthie",
        "score": 70,
        "last_enriched_at": datetime(2026, 11, 1, tzinfo=UTC),
        "pages_crawled": [f"https://{domain}/"],
        "has_mx": True,
        "owner_name": "Dr. Jane Smith",
        "owner_email": f"jane@{domain}",
        "owner_email_status": "verified",
        "emails": [],
    }
    data.update(overrides)
    return Lead(**data)


# ------------------------------------------------------------------ clean_email
@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("u003eprivacy@nutree.com", "privacy@nutree.com"),
        ("\\u003econtact@acme.com", "contact@acme.com"),
        ("u003cu003econtact@acme.com", "contact@acme.com"),
        ("&gt;hello@acme.com", "hello@acme.com"),
        (">hello@acme.com<", "hello@acme.com"),
        ("%20support@healthyumeds.com", "support@healthyumeds.com"),
        ("%20partnerships@acme.com", "partnerships@acme.com"),
        ("mailto:support@norseclinics.com", "support@norseclinics.com"),
        ("MAILTO:Jane.Doe@Acme.COM?subject=hi", "jane.doe@acme.com"),
        ("mailto:jane%40acme.com", "jane@acme.com"),
        ("  Jane@Acme.com.  ", "jane@acme.com"),
        ("jane@acme.com);", "jane@acme.com"),
        ("care@apexwellness.example", "care@apexwellness.example"),
    ],
)
def test_clean_email_strips_escapes(raw, clean):
    assert clean_email(raw) == clean


@pytest.mark.parametrize(
    "raw",
    [None, "", "jane", "@acme.com", "jane@localhost", "jane@@acme.com", "a..b@acme.com", "logo@2x.png", 42],
)
def test_clean_email_rejects_garbage(raw):
    assert clean_email(raw) is None


# ------------------------------------------------------------------ classification
@pytest.mark.parametrize(
    "email",
    [
        "privacy@nutree.com",
        "registrar@squarespace.com",
        "anyone@domainsbyproxy.com",
        "admin@shopify.com",
        "c00afd5e7b3d4234889a1f@tehr.ai",
        "bcdfghjklmnpqrstvwxzbcd@acme.com",
        "jane.smith@gmail.com",
        "john.doe@acme.com",
        "test@acme.com",
        "yourname@acme.com",
        "noreply@acme.com",
        "mailer-daemon@acme.com",
        "postmaster@acme.com",
        "wordpress@acme.com",
    ],
)
def test_junk_locals(email):
    assert classify_local_part(email) == "junk"


@pytest.mark.parametrize(
    "local",
    ["health", "rx", "hola", "clientcare", "heythere", "hello", "care", "info", "support", "partnerships"],
)
def test_role_locals(local):
    assert classify_local_part(f"{local}@acme.com") == "role"


@pytest.mark.parametrize("email", ["jane@acme.com", "mark.owens@acme.com", "drkim@acme.com", "p0@acme.com"])
def test_person_locals(email):
    assert classify_local_part(email) == "person"


@pytest.mark.parametrize("local", ["mark", "mark.owens", "mowens", "markowens", "marko", "owens"])
def test_owner_match_is_person(local):
    assert classify_local_part(f"{local}@acme.com", "Dr. Mark Owens, MD") == "person"


def test_owner_match_beats_placeholder_but_not_a_bare_role_word():
    assert classify_local_part("jane.smith@acme.com", "Jane Smith") == "person"
    assert classify_local_part("care@acme.com", "Lisa Care") == "role"
    assert classify_local_part("noreply@acme.com", "Noreply Bot") == "junk"


# ------------------------------------------------------------------ cross-company
def test_is_cross_company_duplicate():
    seen = {"dev@gmail.com": {"a.com", "b.com"}, "jane@a.com": {"a.com"}}
    assert is_cross_company_duplicate("dev@gmail.com", "a.com", seen)
    assert is_cross_company_duplicate("dev@gmail.com", "www.c.com", seen)
    assert not is_cross_company_duplicate("jane@a.com", "www.a.com", seen)
    assert not is_cross_company_duplicate("new@x.com", "x.com", seen)


def test_email_owners_uses_registrable_domains_and_cleaned_addresses():
    leads = [
        make_lead("a.com", emails=["%20johanwestin00@gmail.com"]),
        make_lead("www.a.com", emails=["johanwestin00@gmail.com"]),
        make_lead("b.com", emails=["mailto:JohanWestin00@gmail.com"]),
    ]
    owners = email_owners(leads)
    assert owners["johanwestin00@gmail.com"] == {"a.com", "b.com"}


# ------------------------------------------------------------------ normaliser
def test_normalize_lead_emails_in_place():
    lead = make_lead(
        owner_email="mailto:Jane@acmehealth.com",
        primary_email="u003econtact@acmehealth.com",
        emails=["%20support@acmehealth.com", "support@acmehealth.com", "not-an-email"],
        people=[Person(name="Sam Lee", email="&gt;sam@acmehealth.com"), Person(name="No Mail")],
    )
    assert normalize_lead_emails(lead) is True
    assert lead.owner_email == "jane@acmehealth.com"
    assert lead.primary_email == "contact@acmehealth.com"
    assert lead.emails == ["support@acmehealth.com"]
    assert lead.people[0].email == "sam@acmehealth.com" and lead.people[1].email is None
    assert normalize_lead_emails(lead) is False


# ------------------------------------------------------------------ extraction
def _snap(html: str, domain: str = "acme.com") -> SiteSnapshot:
    snap = SiteSnapshot(domain=domain, final_url=f"https://{domain}/")
    snap.pages.append(_parse(snap.final_url, html))
    return snap


def test_extract_emails_stores_clean_forms():
    html = (
        '<html><body><a href="mailto:%20Support@acme.com?subject=Hi">x</a>'
        '<script>var d = {"e":"\\u003einfo@acme.com"};</script>'
        "<p>u003ehello@acme.com and &gt;team@acme.com</p></body></html>"
    )
    emails = extract_emails(_snap(html), "acme.com")
    assert set(emails) == {"support@acme.com", "info@acme.com", "hello@acme.com", "team@acme.com"}
    assert not any(e.startswith(("u003e", "%20", "mailto:")) for e in emails)


def test_jsonld_email_is_cleaned():
    org = organization_from_jsonld([{"@type": "MedicalClinic", "email": "mailto:Info@Acme.com"}])
    assert org["email"] == "info@acme.com"


# ------------------------------------------------------------------ gate
def test_gate_rejects_junk_and_role_boxes_counted_as_named():
    lead = make_lead(owner_email="u003eprivacy@acmehealth.com")
    assert sendable_contacts(lead, "new")[1] == "junk_email"
    for local in ("health", "rx", "hola", "clientcare", "heythere", "care"):
        lead = make_lead(owner_email=None, emails=[f"{local}@acmehealth.com"])
        contacts, reason, tier = sendable_contacts(lead, "new", allow_generic=True)
        assert (contacts, reason, tier) == ([], "role_mailbox_not_allowed", "none"), local
    lead = make_lead(owner_email=None, emails=["%20hello@acmehealth.com"])
    assert sendable_contacts(lead, "new")[1] == "generic_mailbox"
    contacts, reason, tier = sendable_contacts(lead, "new", allow_generic=True)
    assert tier == "generic" and [c.email for c in contacts] == ["hello@acmehealth.com"]


def test_gate_cleans_stored_escaped_owner_email():
    lead = make_lead(owner_email="mailto:jane@acmehealth.com")
    contacts, _reason, tier = sendable_contacts(lead, "new")
    assert tier == "named" and [c.email for c in contacts] == ["jane@acmehealth.com"]


def test_gate_rejects_platform_and_hash_addresses():
    lead = make_lead(owner_email="registrar@squarespace.com")
    assert is_sendable(lead, "registrar@squarespace.com", "new") == (False, "junk_email")
    lead = make_lead(owner_email="c00afd5e7b3d423488aa@tehr.ai")
    assert is_sendable(lead, "c00afd5e7b3d423488aa@tehr.ai", "new") == (False, "junk_email")


def test_shared_contact_email_rejected_across_companies():
    leads = [
        make_lead(d, owner_name="Johan Westin", owner_email="johanwestin00@gmail.com")
        for d in ("a.com", "b.com")
    ]
    shared = email_owners(leads)
    for lead in leads:
        assert sendable_contacts(lead, "new", shared=shared)[1] == "shared_contact_email"
    alone = email_owners(leads[:1])
    assert sendable_contacts(leads[0], "new", shared=alone)[2] == "named"


def test_freemail_rules():
    # matches the owner's name: fine even next to a corporate mailbox
    lead = make_lead(
        owner_name="Mark Owens", owner_email="markowens@gmail.com", emails=["info@acmehealth.com"]
    )
    assert sendable_contacts(lead, "new")[2] == "named"
    # placeholder gmail is junk whatever else is on the lead
    lead = make_lead(owner_name=None, owner_email="jane.smith@gmail.com")
    assert sendable_contacts(lead, "new")[1] == "junk_email"
    # unmatched gmail with a corporate address on the lead
    lead = make_lead(
        owner_name="Mark Owens", owner_email="coolguy88@gmail.com", emails=["info@acmehealth.com"]
    )
    assert is_sendable(lead, "coolguy88@gmail.com", "new") == (False, "freemail_unverified")
    # unmatched gmail but the lead has no corporate email at all
    lead = make_lead(owner_name="Mark Owens", owner_email="coolguy88@gmail.com")
    assert is_sendable(lead, "coolguy88@gmail.com", "new") == (True, "ok")


def test_mx_check_runs_last_and_only_rejects_on_false():
    lead = make_lead(owner_email="jane@acme-mail.net", has_mx=None)
    calls: list[str] = []

    def no_mx(domain):
        calls.append(domain)
        return False

    assert sendable_contacts(lead, "new", mx_check=no_mx)[1] == "no_mx"
    assert calls == ["acme-mail.net"]
    assert sendable_contacts(lead, "new", mx_check=lambda d: None)[2] == "named"
    assert sendable_contacts(lead, "new", mx_check=None)[2] == "named"
    # the lead's own domain with a known MX and free-mail never trigger a lookup
    calls.clear()
    sendable_contacts(make_lead(), "new", mx_check=no_mx)
    sendable_contacts(make_lead(owner_email="janesmith@gmail.com"), "new", mx_check=no_mx)
    assert calls == []
    # a candidate that already failed another rule is never looked up
    sendable_contacts(make_lead(owner_email="noreply@acme-mail.net", has_mx=None), "new", mx_check=no_mx)
    assert calls == []


def test_plan_reports_new_reasons(tmp_path):
    store = LeadStore(tmp_path / "leads.db")
    store.save(make_lead("good.com"))
    store.save(make_lead("junk.com", owner_email="u003eprivacy@junk.com"))
    store.save(make_lead("one.com", owner_name="Ann Lee", owner_email="johanwestin00@gmail.com"))
    store.save(make_lead("two.com", owner_name="Bo Kim", owner_email="johanwestin00@gmail.com"))
    store.save(make_lead("nomx.com", owner_email="jane@nomx-mail.com"))
    plan = enroll_mod.plan_enrolment(store, None, mx_check=lambda d: d != "nomx-mail.com")
    assert [c.lead.domain for c in plan.candidates] == ["good.com"]
    assert plan.rejections == Counter({"shared_contact_email": 2, "junk_email": 1, "no_mx": 1})


# ------------------------------------------------------------------ has_mx
class FakeResolver:
    def __init__(self, answers):
        self.answers = answers
        self.calls = 0

    def resolve(self, name, rdtype, lifetime=None):
        self.calls += 1
        assert rdtype == "MX" and lifetime == 3.0
        result = self.answers[name]
        if isinstance(result, Exception):
            raise result
        return [SimpleNamespace(exchange=host) for host in result]


def test_has_mx_with_injected_resolver():
    resolver = FakeResolver(
        {
            "good.com": ["mx1.good.com."],
            "nullmx.com": ["."],
            "gone.com": dns.resolver.NXDOMAIN(),
            "nomx.com": dns.resolver.NoAnswer(),
            "slow.com": dns.exception.Timeout(),
        }
    )
    assert has_mx("good.com", resolver) is True
    assert has_mx("nullmx.com", resolver) is False
    assert has_mx("gone.com", resolver) is False
    assert has_mx("nomx.com", resolver) is False
    assert has_mx("slow.com", resolver) is None
    assert has_mx("nodot", resolver) is False


def test_has_mx_caches(monkeypatch):
    resolver = FakeResolver({"good.com": ["mx.good.com."]})
    cache: dict = {}
    assert has_mx("Good.com.", resolver, cache=cache) is True
    assert has_mx("good.com", resolver, cache=cache) is True
    assert resolver.calls == 1
    # the default (dnspython) path uses the module cache
    monkeypatch.setattr("moh_leadgen.enrich.email_hygiene._default_resolver", lambda: resolver)
    monkeypatch.setattr("moh_leadgen.enrich.email_hygiene._MX_CACHE", {})
    assert has_mx("good.com") is True and has_mx("good.com") is True
    assert resolver.calls == 2


def test_has_mx_unknown_when_resolver_cannot_answer_control_domain(monkeypatch):
    """A sandbox whose DNS answers nothing for gmail.com must not reject real domains."""
    from moh_leadgen.enrich import email_hygiene as eh

    class _NoAnswerResolver:
        def resolve(self, name, rdtype, lifetime=None):
            import dns.resolver

            raise dns.resolver.NoAnswer()

    monkeypatch.setattr(eh, "_default_resolver", lambda: _NoAnswerResolver())
    monkeypatch.setattr(eh, "_MX_TRUST", {})
    monkeypatch.setattr(eh, "_MX_CACHE", {})
    assert eh.has_mx("luvohealth.com") is None
