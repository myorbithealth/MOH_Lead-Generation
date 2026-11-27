"""Fingerprints for med spa, DPC and white-label telehealth platforms.

Each snippet is copied from a live tenant page fetched on 2026-11-27 (tenant URL in the comment and
in docs/tools/competitor.md), trimmed to the element that carries the marker.
"""

import pytest

from moh_leadgen.enrich.tech import COMPETITOR_PLATFORMS, FINGERPRINTS, canonical_platform, fingerprint
from moh_leadgen.tools.competitor_platform import COMPETITORS, PLATFORM_OWNED, SOURCE_MARKERS

SNIPPETS: dict[str, list[str]] = {
    "Akute Health": [
        # cardonadpc.com
        (
            '<a data-item-label="true" data-testid="linkElement" '
            'href="https://cardonadpc.akutehealth.com/appointments" target="_blank">Book</a>'
        ),
        # coastlinedpc.com
        (
            '<a class="dmButtonLink" href="https://coastlinedpc.akutehealth.com/appointments" '
            'data-element-type="dButtonLinkId">Book</a>'
        ),
        # bgmenshealth.akutehealth.com (tenant app shell)
        '<link rel="manifest" href="/manifest.json"/><title>Akute Health</title>',
    ],
    "Canvas Medical": [
        # fellahealth.canvasmedical.com/app/
        (
            '<title>Fella Portal</title><link href="https://s3-us-west-2.amazonaws.com/canvas-pcp-assets/'
            'fonts/488830/FC462E716C3760D7A.css?sri" rel="stylesheet">'
        ),
        # synapticure.canvasmedical.com/app/
        '"customerLogoUrl": "https://canvas-medical.s3-us-west-2.amazonaws.com/images/Synapticure/logo.png"',
    ],
    "Moxie": [
        # toxandpout.com
        '<a class="et_pb_button" href="https://app.joinmoxie.com/booking/tox-and-pout" target="_blank">Book Now</a>',
        # paradisemedspa.com
        '<a href="https://app.joinmoxie.com/online-store/paradise-medspa-wellness/memberships">Memberships</a>',
        # etoileaestheticssandiego.com
        '"buttonLink": "https://app.joinmoxie.com/packages/etoile-aesthetics-sd"',
    ],
    "WellieMD": [
        # intake.virelomd.com, intake.kinmeds.com, intake.elaramd.com (identical shells)
        '<meta name="description" content="WellieMD Questionnaire App"/><title>WellieMD Questionnaire</title>',
    ],
    "Wizlo": [
        # portal.sexymd.com, portal.lasara.com, patients.waybettermd.com (CNAME portal2.wizlo.com)
        '<body data-wz-brand="" class="__variable_d8ebd1 bg-secondary antialiased">',
        '{"className":"wz-auth-card flex w-full flex-col","data-wz-component":"card"}',
    ],
    "Asher": [
        # ivluxeinfusion.joinasher.com, peakvitality.joinasher.com
        "<style>#asher-exit-calendar { display: none; } #asher-footer-cta .footer-button { color: #fff; }</style>",
    ],
    "WellSync": [
        # portal.boomrx.com (custom-domain tenant)
        '<link rel="icon" href="https://wellsyncimagestorage.blob.core.windows.net/carehub/boomrx/favicon.png"/>',
        # weightloss.wellsync.com
        "iFrameResize({ log: false }, '#wellsync-Iframe');",
        # vitaminshoppe.wellsync.com (Whole Health Rx by Vitamin Shoppe)
        "let cta_url = 'https://vitaminshoppe.wellsync.com/glp-1-intake' + marketing_qs;",
    ],
    "UpScript": [
        # getafrezza.com
        '"breadcrumb":{"@id":"https://prodsfwpgetafrezza.upscripthealth.org/home-page/#breadcrumb"}',
        # geteversensecgm.com
        '<img src="https://ush-prod-s3-sfwp-images-public.s3.us-west-2.amazonaws.com/wp-content/uploads/x.png">',
    ],
    "Hydreight": [
        # thefountainplace.squarespace.com (IV clinic on the Hydreight network)
        '<a href="/hydreight-app">Hydreight® App</a>',
    ],
    "Aesthetic Record": [
        # rplusmedspa.com, expressmedspas.com
        (
            '<a href="https://rplusmedspa.myaestheticrecord.com/online-booking/book-appointment" '
            'target="_blank" class="nav-link">Book</a>'
        ),
    ],
}


@pytest.mark.parametrize(
    ("platform", "html"),
    [(platform, html) for platform, snippets in SNIPPETS.items() for html in snippets],
)
def test_tenant_snippet_fingerprints_as_competitor(platform, html):
    found, competitor = fingerprint(html)
    assert platform in found
    assert competitor == platform


def test_zenoti_detected_but_not_a_competitor():
    # everbody.com and alchemy43.com
    for html in [
        '<li><a href="https://amp.zenoti.com/webstoreNew/services/09654253-4607-4dbb-9a2e-8d0ae9112f1d">Book</a>',
        '<a href="https://alchemy43.zenoti.com/webstoreNew/services?_ga=2.1">Book a class</a>',
    ]:
        found, competitor = fingerprint(html)
        assert "Zenoti" in found
        assert competitor is None
    assert "Zenoti" not in COMPETITOR_PLATFORMS


def test_cname_chain_identifies_custom_domain_tenants():
    # patients.waybettermd.com -> portal2.wizlo.com; book.ohmyolivia.com -> VSDHOne booking widget
    assert fingerprint("<html></html>", ["portal2.wizlo.com."])[1] == "Wizlo"
    assert fingerprint("<html></html>", ["vsdigital-bookingwidget-prod.azurewebsites.net."])[1] == "Hydreight"
    assert fingerprint("<html></html>", ["hydreight-bookingwidget-prod.azurewebsites.net."])[1] == "Hydreight"


def test_plain_med_spa_page_is_not_fingerprinted():
    found, competitor = fingerprint('<a href="/book">Book now</a><p>Botox, filler and GLP-1 weight loss</p>')
    assert competitor is None
    assert not set(found) & set(SNIPPETS)


def test_new_platforms_are_wired_into_the_competitor_tool():
    for platform in [*SNIPPETS, "Zenoti"]:
        assert platform in FINGERPRINTS
        assert SOURCE_MARKERS[platform], platform
        assert platform in COMPETITORS
    assert canonical_platform("VSDHOne") == "Hydreight"
    assert canonical_platform("canvas") == "Canvas Medical"


def test_vendor_domains_are_platform_owned():
    expected = {
        "akutehealth.com",
        "canvasmedical.com",
        "joinmoxie.com",
        "zenoti.com",
        "aestheticrecord.com",
        "myaestheticrecord.com",
        "welliemd.com",
        "wizlo.com",
        "joinasher.com",
        "wellsync.com",
        "upscripthealth.com",
        "upscripthealth.org",
        "hydreight.com",
        "vsdhone.com",
        "spakinect.com",
        "remedora.com",
        "leguprx.com",
        "nimbusrx.io",
        "altrohealth.com",
        "rxpnow.com",
        "outfitmd.com",
        "qualiphy.me",
    }
    assert expected <= PLATFORM_OWNED


def test_crtsh_apexes_are_owned_so_tenant_subdomains_rekey():
    for domains, _dorks in COMPETITORS.values():
        for apex in domains:
            assert apex in PLATFORM_OWNED, apex
