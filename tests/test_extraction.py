from pathlib import Path

from moh_leadgen.domains import normalize_domain
from moh_leadgen.enrich.extract import (
    extract_emails,
    extract_jsonld,
    extract_people_heuristic,
    extract_phones,
    extract_socials,
    organization_from_jsonld,
    people_from_jsonld,
    pick_primary_email,
)
from moh_leadgen.enrich.legitscript import detect_legitscript, infer_vertical
from moh_leadgen.enrich.site_crawler import SiteSnapshot, _parse
from moh_leadgen.enrich.tech import fingerprint
from moh_leadgen.models import Lead, LegitScriptEvidence, Person
from moh_leadgen.scoring import score_lead

FIXTURE = Path(__file__).parent / "fixtures" / "certified_site.html"


def snapshot() -> SiteSnapshot:
    html = FIXTURE.read_text()
    snap = SiteSnapshot(domain="apexwellness.example", final_url="https://apexwellness.example/")
    snap.pages.append(_parse(snap.final_url, html))
    return snap


def test_seal_detection():
    ev = detect_legitscript(snapshot())
    assert ev.status == "seal_detected"
    assert ev.seal_ids == ["16209345"]
    assert "checker_keywords=apexwellness.example" in ev.verify_url


def test_mention_only():
    snap = SiteSnapshot(domain="x.example", final_url="https://x.example/")
    snap.pages.append(_parse(snap.final_url, "<html><body><p>We are LegitScript certified.</p></body></html>"))
    assert detect_legitscript(snap).status == "mentioned"
    snap2 = SiteSnapshot(domain="y.example", final_url="https://y.example/")
    snap2.pages.append(_parse(snap2.final_url, "<html><body><p>hello</p></body></html>"))
    assert detect_legitscript(snap2).status == "not_found"


def test_contacts():
    snap = snapshot()
    emails = extract_emails(snap, "apexwellness.example")
    assert emails == ["care@apexwellness.example"]
    assert pick_primary_email(emails) == "care@apexwellness.example"
    assert extract_phones(snap)[0] == "+15125550142"
    socials = extract_socials(snap)
    assert socials["facebook"].endswith("apexwellnessclinic")


def test_jsonld_and_people():
    snap = snapshot()
    objs = extract_jsonld(snap)
    org = organization_from_jsonld(objs)
    assert org["name"] == "Apex Wellness Clinic"
    assert org["city"] == "Austin" and org["state"] == "TX"
    ld_people = people_from_jsonld(objs, org)
    assert ld_people[0].name == "Maria Delgado" and ld_people[0].is_decision_maker
    assert ld_people[0].linkedin.endswith("maria-delgado-apex")
    heur = extract_people_heuristic(snap)
    names = {p.name: p for p in heur}
    assert "Maria Delgado" in names and names["Maria Delgado"].title == "Founder"
    assert any("Okafor" in n for n in names)


def test_vertical_and_tech():
    snap = snapshot()
    vertical, _ = infer_vertical(snap.all_text())
    assert vertical == "weight_loss_glp1"
    stack, competitor = fingerprint(snap.all_html())
    assert competitor == "Bask Health"
    assert "Stripe" in stack and "Google Ads" in stack


def test_scoring_orders_reasonably():
    strong = Lead(
        domain="strong.example", vertical="weight_loss_glp1", pages_crawled=["x"],
        legitscript=LegitScriptEvidence(status="seal_detected"), owner_name="A B", owner_title="CEO",
        owner_email="a@strong.example", primary_phone="+15125550142", competitor_platform="Bask Health", has_mx=True,
        people=[Person(name="A B", title="CEO", is_decision_maker=True)],
    )
    weak = Lead(domain="weak.example", pages_crawled=["x"], vertical="primary_care_clinic", has_mx=False)
    unreachable = Lead(domain="dead.example")
    assert score_lead(strong).score > score_lead(weak).score > score_lead(unreachable).score
    assert strong.score >= 80
    assert any("Bask" in r for r in strong.score_reasons)


def test_normalize_domain():
    assert normalize_domain("https://www.Example.com/path?x=1") == "example.com"
    assert normalize_domain("mailto:joe@sub.example.co.uk") == "example.co.uk"
    assert normalize_domain("not a domain") is None


def test_rimo_and_bask_fingerprints():
    rimo = '<img src="https://larp-cdn.com/pp/store/PDm2bRTVON8jDCq_NwyYQ/branding/rimo-mark.png">'
    assert fingerprint(rimo)[1] == "Rimo"
    bask = '<script src="https://checkout.bask.health/embed.js"></script>'
    assert fingerprint(bask)[1] == "Bask Health"
    assert fingerprint("<p>plain page</p>")[1] is None
