# Tool: `competitor`

**Goal:** brands already paying a competing telehealth platform (Bask Health, Rimo, Healthie,
OpenLoop, Beluga Health, MD Integrations, Qualiphy, Wheel), and med spas / DPC clinics on
Moxie, Akute, Canvas, Zenoti, Aesthetic Record or a white-label telehealth vendor (WellieMD, Wizlo,
Asher, WellSync, UpScript, Hydreight/VSDHOne). They have validated the model; the
pitch is a better/cheaper/more complete stack.

## What was verified (Nov 2026)

* **Rimo** (rimo.co, legacy rimo.health): tenant storefronts live on `<brand>.rimo.health` and on
  custom domains; every storefront loads assets from **`larp-cdn.com`** and
  `/pp/store/<id>/branding/` paths. That string is unique to Rimo, so a source-code search for
  `larp-cdn.com` lists Rimo's customers directly.
* **Bask Health** (bask.health): tenant apps are served from Bask's `*.bask.health` services and
  carry Bask's server-side tagging (`analytics.bask.health`) and `data-basky-*` attributes.
  Bask's IPs are Vercel's shared edge, so reverse-IP lookups return every Vercel site, not Bask
  customers; use the HTML markers instead.
* Plain reverse DNS (PTR records) identifies the hosting provider only. It never lists tenants.

* **WhiteLabel MD**: tenants embed an intake iframe (`intake_placement`) and a `tracking_start()`
  snippet, and post answers to `api.whitelabelmd.com`. The iframe marker alone finds ~130 sites.
* **Telegra MD**: WordPress tenants load the `telemdnow` plugin stylesheet; Shopify tenants load
  `integration-<brand>.telegramd.com/telegraApp-script.js`; certificate logs list every integration
  as `integration-<brand>.telegramd.com` or `<brand>.affiliates.commerce.telegramd.com`.
* **Karpa**: tenants sit on custom domains and link only to karpahealth.com; most also use RxAve
  (`rxave.health`) for fulfilment, which is the better search key.
* **CareValidate**: tenants call `api.care360-next.carevalidate.com` (`careValidateApiUrl`); the
  storefront template leaks `careglp` (CareGLP, their GLP-1 product) and theme variables such as
  `primaryMedicationSubtitle1Text`, and many tenants check out through one shared Stripe
  publishable key (search the key itself to list them; it is not committed here).
* **Cuvo**: tenant storefronts are `<brand>.med.cuvo.co` behind a wildcard certificate. Custom
  domains carry the legal footer "technology platform ... provided by Medstra Inc DBA Cuvo Health"
  and the template asset `/images/products/tirzepatide-bottle.png`; the broad template class
  `href\:\/telehealth-consent` + "tirzepatide" finds ~380 sites that the Medstra fingerprint then
  verifies.
* **Rimo**: the newer embed uses the `rimo-health` token; `larp-cdn.com` catches the legacy store.
* Lesson: when a platform's domain string finds few sites, build a cluster from the known tenants
  and run PublicWWW `build_signatures`; a shared snippet usually finds 5-10x more.

## Med spa, DPC and white-label telehealth platforms (verified 2026-11-27)

Each row was checked by fetching the live tenant page on 2026-11-27 and copying the string from its
HTML (or, where noted, its DNS CNAME). Tenant subdomains came from crt.sh
(`https://crt.sh/?q=%25.<vendor-domain>&output=json`). Fingerprints are in `enrich/tech.py`,
search keys in `SOURCE_MARKERS`, crt.sh apexes in `COMPETITORS`; vendor domains are in
`PLATFORM_OWNED`.

| Platform (marketing domain) | Live tenants checked | Exact marker found |
|---|---|---|
| **Akute Health** (akutehealth.com) | cardonadpc.com, coastlinedpc.com; tenant apps bgmenshealth / fountaintrt / cascobaydpc `.akutehealth.com` | `https://cardonadpc.akutehealth.com/appointments`, `https://coastlinedpc.akutehealth.com/appointments`, `https://portal.akutehealth.com/login`; tenant app shell `<title>Akute Health</title>`. crt.sh lists ~228 `<clinic>.akutehealth.com` names (mostly DPC and TRT/men's health) |
| **Canvas Medical** (canvasmedical.com) | fellahealth.canvasmedical.com/app/ ("Fella Portal"), synapticure.canvasmedical.com/app/ | `s3-us-west-2.amazonaws.com/canvas-pcp-assets/`, `"customerLogoUrl": "https://canvas-medical.s3...`. Tenant marketing sites (fellahealth.com, synapticure.com) carry no Canvas string; the crt.sh tenant list is the discovery path |
| **Moxie** (joinmoxie.com) | toxandpout.com, churchwellaesthetics.com, citrusmedspasandiego.com, etoileaestheticssandiego.com, paradisemedspa.com, goodjujuaesthetics.com | `https://app.joinmoxie.com/booking/<slug>`, `/packages/<slug>`, `/online-store/<slug>/memberships`. `book.joinmoxie.com` is a Bitly branded short domain and `moxie-suite-embeds.joinmoxie.com` is Moxie's own Webflow site; neither appeared on a tenant page |
| **Zenoti** (zenoti.com; booking/POS, not a competitor) | everbody.com, alchemy43.com | `https://amp.zenoti.com/webstoreNew/services/<id>`, `https://alchemy43.zenoti.com/webstoreNew/services` |
| **Aesthetic Record** (aestheticrecord.com; existing fingerprint unchanged) | rplusmedspa.com, expressmedspas.com | `https://<spa>.myaestheticrecord.com/online-booking/book-appointment`. The `client.` and `link.aestheticrecord.com` hosts exist in crt.sh but were not seen on a tenant page |
| **WellieMD** (welliemd.com) | intake.virelomd.com, intake.kinmeds.com, intake.elaramd.com, intake.timbermeds.com, intake.thymerx.com, intake.ignitemeds.com | `<meta name="description" content="WellieMD Questionnaire App"/><title>WellieMD Questionnaire</title>`. The brand homepages (virelomd.com, kinmeds.com) do not name WellieMD; fetch `intake.<brand>` |
| **Wizlo** (wizlo.com) | portal.sexymd.com, portal.lasara.com, portal.myleaderhealth.com, patients.waybettermd.com | `<body data-wz-brand="" ...>`, `"data-wz-component"`; CNAMEs `portal2.wizlo.com` (sexymd, waybettermd), `portal.wizlo.com` (lasara, myleaderhealth), `wizlo-prod-portal-centralus-001-...azurewebsites.net` (acemd, arsynl). crt.sh also lists `portal.<brand>.wizlo.com` |
| **Asher** (joinasher.com) | ivluxeinfusion, bombshellbeauty, corewellness, peakvitality, integritypeptides `.joinasher.com` | `#asher-exit-calendar`, `#asher-footer-cta` (tenant GLP-1 landing pages, e.g. "8-Week Semaglutide Program for $195 \| ivLuxeInfusion") |
| **WellSync** (wellsync.com) | portal.boomrx.com (custom domain), vitaminshoppe.wellsync.com ("Whole Health Rx by Vitamin Shoppe"), joya.portal.wellsync.com | `https://wellsyncimagestorage.blob.core.windows.net/carehub/boomrx/favicon.png`, `'https://vitaminshoppe.wellsync.com/glp-1-intake'`, `#wellsync-Iframe` (weightloss.wellsync.com) |
| **UpScript** (upscripthealth.com, consumer site upscript.com) | getafrezza.com, geteversensecgm.com | `https://prodsfwpgetafrezza.upscripthealth.org/`, `https://prodsfwpgeteversensecgm.upscripthealth.org/`, `ush-prod-s3-sfwp-images-public.s3.us-west-2.amazonaws.com` |
| **Hydreight / VSDHOne** (hydreight.com, vsdhone.com) | book.ohmyolivia.com, The Fountain Place (thefountainplace.squarespace.com) | book.ohmyolivia.com CNAME `vsdigital-bookingwidget-prod.azurewebsites.net` (same app as book.vsdhone.com; booking.hydreight.com is `hydreight-bookingwidget-prod`), so it is a CNAME match only; The Fountain Place: "Hydreight® App" page and `/hydreight-app` |

Not verified (vendor domain confirmed and added to `PLATFORM_OWNED`, but no tenant page with a
vendor string was found, so no fingerprint was added):

* **Spakinect** (spakinect.com): GFEs are started from the clinic's EMR (OptiMantra, Aesthetic
  Record marketplace); no tenant page embeds a Spakinect host.
* **Qualiphy Quidget** (quidget.qualiphy.me): a WordPress/WooCommerce checkout plugin; no tenant
  page with `quidget` was found. The existing `qualiphy.me` fingerprint is unchanged.
* **Remedora** (remedora.com; app shell loads `assets.remedora.com`), **LegUp Rx** (leguprx.com),
  **NimbusRX** (nimbusrx.io), **Altro** (altrohealth.com, partner app altroapp.com), **RxPNow**
  (rxpnow.com), **OutfitMD** (outfitmd.com): crt.sh shows no tenant subdomains and search found no
  "powered by" tenant. A source-code search (PublicWWW) for `assets.remedora.com` is the next step.
* **ClinicX**: clinicx.io is an EHR/RCM company; no white-label telehealth ClinicX was confirmed.

## "Reverse lookup" techniques

1. **Certificate transparency (crt.sh)** - platforms that host tenants on subdomains
   (`brand.platform.com`) leak every tenant through issued certificates. Free, no key.
2. **Search dorks** - "powered by <platform>", the platform's widget/script hostnames quoted in
   page source, case-study pages.
3. **Fingerprints during enrichment** - `enrich/tech.py` matches script/CSS/iframe hostnames and
   the DNS CNAME chain (`checkout.brand.com → platform CNAME`). This also catches custom-domain
   tenants discovered by any other tool, which is why every lead gets `competitor_platform`.
4. **Source-code search engines** (`discovery/sourcesearch.py`) - PublicWWW or NerdyData index
   the HTML of hundreds of millions of sites; searching for the markers above returns the tenant
   list in one call. This is the tool to buy for this job (PublicWWW from ~$49/month; BuiltWith
   "sites using technology X" is the enterprise alternative).
5. **Reverse IP / ASN** - only useful for platforms on dedicated IPs; Bask and Rimo are not.

## Running

```bash
leadgen discover competitor --opt platform=bask
leadgen enrich competitor --limit 50
leadgen top --tool competitor
```

Add a platform: append to `COMPETITORS` in `tools/competitor_platform.py` and a fingerprint row
in `enrich/tech.py`.

## PublicWWW inside Claude Code

`.mcp.json` at the repo root registers PublicWWW's MCP server, so any Claude Code session in this
repo can run source searches, save tenant lists as clusters, extract contacts from their pages and
run `build_signatures` to discover new platform markers. Export `PUBLICWWW_TOKEN` (the API token
from https://publicwww.com/profile/api) in your shell before starting Claude Code; the token is
never written to the repo. The Python pipeline uses the same token via `PUBLICWWW_API_KEY`.
