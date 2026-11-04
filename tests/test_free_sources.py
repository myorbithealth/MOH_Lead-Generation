from moh_leadgen.discovery.free_index import domains_from_urls
from moh_leadgen.discovery.overpass import build_query, parse_elements
from moh_leadgen.discovery.search import SearchHit


def test_verify_urls_to_domains():
    urls = [
        "https://www.legitscript.com/websites/?checker_keywords=henrymeds.com",
        "http://legitscript.com/websites/?checker_keywords=www.tryshed.com&foo=1",
        "https://www.legitscript.com/pharmacy/example-pharmacy.com/",
        "https://www.legitscript.com/websites/",               # no domain
        "https://www.legitscript.com/websites/?checker_keywords=legitscript.com",  # junk
        "https://www.legitscript.com/websites/?checker_keywords=HENRYMEDS.COM",     # dup
    ]
    out = list(domains_from_urls(urls))
    assert [d for d, _ in out] == ["henrymeds.com", "tryshed.com", "example-pharmacy.com"]


def test_overpass_query_and_parse():
    q = build_query((30.1, -97.9, 30.5, -97.5), ["clinic", "gym"])
    assert '["amenity"="clinic"]["website"]' in q and '["leisure"="fitness_centre"]["website"]' in q
    elements = [
        {"tags": {"name": "Austin Med Spa", "website": "https://www.austinmedspa-test.com/", "phone": "+1 512 555 0100", "shop": "beauty"}},
        {"tags": {"name": "No site"}},
        {"tags": {"name": "Dup", "website": "http://austinmedspa-test.com/about"}},
    ]
    cands = list(parse_elements(elements, "Austin, TX"))
    assert len(cands) == 1
    assert cands[0].domain == "austinmedspa-test.com"
    assert cands[0].source_detail["place_phone"] == "+1 512 555 0100"


def test_searchhit_defaults():
    assert SearchHit("https://x.example").snippet == ""
