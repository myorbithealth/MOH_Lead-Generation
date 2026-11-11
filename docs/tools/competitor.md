# Tool: `competitor`

**Goal:** brands already paying a competing telehealth platform (Bask Health, Rimo, Healthie,
OpenLoop, Beluga Health, MD Integrations, Qualiphy, Wheel). They have validated the model; the
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
* **CareValidate**: tenants call `api.care360-next.carevalidate.com` (`careValidateApiUrl`).
* **Cuvo**: tenant storefronts are `<brand>.med.cuvo.co` behind a wildcard certificate; no embed
  marker leaks to custom domains, so discovery relies on "Cuvo Health" mentions.
* Lesson: when a platform's domain string finds few sites, build a cluster from the known tenants
  and run PublicWWW `build_signatures`; a shared snippet usually finds 5-10x more.

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
