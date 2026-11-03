# Tool: `competitor`

**Goal:** brands already paying a competing telehealth platform (Bask Health, Rimo, Healthie,
OpenLoop, Beluga Health, MD Integrations, Qualiphy, Wheel). They have validated the model; the
pitch is a better/cheaper/more complete stack.

## "Reverse lookup" techniques

1. **Certificate transparency (crt.sh)** - platforms that host tenants on subdomains
   (`brand.platform.com`) leak every tenant through issued certificates. Free, no key.
2. **Search dorks** - "powered by <platform>", the platform's widget/script hostnames quoted in
   page source, case-study pages.
3. **Fingerprints during enrichment** - `enrich/tech.py` matches script/CSS/iframe hostnames and
   the DNS CNAME chain (`checkout.brand.com → platform CNAME`). This also catches custom-domain
   tenants discovered by any other tool, which is why every lead gets `competitor_platform`.
4. **Reverse IP / ASN** - add a provider (SecurityTrails, Shodan, BuiltWith) as a
   `DiscoverySource` if you have a key; the hook is `discovery/base.py`.

## Running

```bash
leadgen discover competitor --opt platform=bask
leadgen enrich competitor --limit 50
leadgen top --tool competitor
```

Add a platform: append to `COMPETITORS` in `tools/competitor_platform.py` and a fingerprint row
in `enrich/tech.py`.
