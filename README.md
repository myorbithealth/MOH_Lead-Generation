# MyOrbit Health — Lead Generation Toolkit

A set of lead-generation tools that feed the **MyOrbit Health Sales Portal** (Lovable). Every tool shares one pipeline, one data model and one `leads` table, so a
rep sees LegitScript merchants, competitor-platform tenants and local clinics side by side,
ranked by fit, each with an owner name, email, phone and a personalised booking link.

| Tool | What it finds | Discovery sources (★ = free, no key) |
|---|---|---|
| `legitscript` | Businesses displaying the **LegitScript certification seal** (telehealth, pharmacy, weight loss, TRT, peptides, med spas, addiction treatment) | ★ seed list, ★ Wayback Machine + Common Crawl indexes of LegitScript verify links, search dorks (Claude web search / SerpAPI / Google CSE / Bing), Semrush (optional) |
| `competitor` | Brands already running on a **competing telehealth platform** (Bask Health, Rimo, Healthie, OpenLoop, Beluga, MD Integrations, Qualiphy, Wheel) | ★ certificate transparency (crt.sh), ★ seed list, search dorks, ★ DNS CNAME + HTML fingerprints |
| `local` | **Med spas, gyms, clinics, IV bars, weight-loss and hormone clinics** by city | ★ OpenStreetMap (Nominatim + Overpass), ★ seed list, Google Places (optional) |

Shared enrichment for every candidate:

1. Polite crawl of the homepage + contact/about/team/leadership pages (robots.txt honoured, 1 request / 2 s per host, identifies itself).
2. Emails (mailto, inline, obfuscated), phones (E.164), address, socials, JSON-LD organisation data.
3. **People**: founder/owner/CEO/medical director from JSON-LD, heading structure, "founded by" patterns, LinkedIn links; optional Hunter.io and Claude extraction.
4. **LegitScript evidence**: seal image/script + verify URL found on the merchant's own site (`seal_detected`), text mention (`mentioned`) or `not_found`.
5. Tech + platform fingerprint (competitor telehealth stacks, commerce, ad pixels), DNS (MX, CNAME chain, reverse PTR), RDAP (registrar, domain age).
6. ICP **score 0–100** with human-readable reasons, and a personalised booking link (UTM-tagged).

## Quick start

```bash
uv venv && uv pip install -e ".[dev,llm]"      # or: pip install -e ".[dev,llm]"
cp .env.example .env                           # fill in the keys you have
source .venv/bin/activate

leadgen tools                                  # list tools
leadgen discover legitscript --limit 50        # candidates -> local SQLite (leads.db)
leadgen enrich legitscript --limit 25 --llm    # crawl + extract + score (drop --llm without an Anthropic key)
leadgen top                                    # best leads first
leadgen sync portal --min-score 40             # upsert into the Sales Portal `leads` table
leadgen sync csv --out out/leads.csv           # or just a spreadsheet
leadgen run legitscript                        # all of the above in one command
leadgen show henrymeds.com                     # everything we know about one lead
leadgen promote henrymeds.com                  # create company + contact + deal in the portal
```

### Zero-key mode

Nothing is required beyond Python. With no keys at all, the LegitScript tool pulls every domain
behind an archived LegitScript verify link (Wayback Machine + Common Crawl), the competitor tool
reads certificate-transparency logs, the local tool reads OpenStreetMap, and every candidate is
crawled, verified and scored directly. Owner *names* come from the sites themselves.

### One-key mode (recommended)

Add **`ANTHROPIC_API_KEY`** and two things switch on: Claude's web search runs the discovery
dorks (no SerpAPI/Bing needed) and Claude reads each crawled site to extract decision-makers,
company size and a pitch angle. This is the single key worth adding.

| Key | Unlocks |
|---|---|
| `ANTHROPIC_API_KEY` | search-dork discovery via Claude web search + AI extraction of owners/executives |
| `SUPABASE_SERVICE_ROLE_KEY` | `leadgen sync portal` (Lovable Cloud → Settings → API, secret key) |
| `APOLLO_API_KEY` / `LUSHA_API_KEY` / `HUNTER_API_KEY` | founders' real emails, mobiles and LinkedIn (see below) |
| `GOOGLE_PLACES_API_KEY` | much deeper `local` coverage than OpenStreetMap |
| `SERPAPI_KEY` / `GOOGLE_CSE_KEY`+`GOOGLE_CSE_CX` / `BING_SEARCH_KEY` | alternative search providers |
| `SEMRUSH_API_KEY` | optional extra discovery (needs API units) |

### Hands-off: GitHub Actions

`.github/workflows/leadgen.yml` runs every Monday (and on demand from the Actions tab): discover →
enrich → score → CSV artifact → sync to the Sales Portal. To turn it on, add repository secrets
`SUPABASE_SERVICE_ROLE_KEY` and (recommended) `ANTHROPIC_API_KEY`; optionally `HUNTER_API_KEY`,
`GOOGLE_PLACES_API_KEY`, and the `LEADGEN_BOOKING_URL` variable. Nothing else to configure.

## Real decision-maker contacts: which software you need

Websites only publish role mailboxes (info@, support@). Founders' direct emails, mobile numbers
and LinkedIn profiles come from licensed B2B contact databases, so the pipeline plugs into three.
Add any one key and `leadgen enrich` fills `owner_email`, `owner_phone`, `owner_linkedin` and the
`people` list automatically; each email carries a status (`verified`, `provider`, `on_site`, `guessed`).

| Provider | What it gives | Plan to get | Env var |
|---|---|---|---|
| **Apollo.io** (recommended first) | People search by company domain + title → names, titles, **LinkedIn URLs**; enrichment → **verified work email**; mobile numbers via async webhook | Free plan includes API access with monthly credits; Basic plan for volume | `APOLLO_API_KEY` (+ `APOLLO_PHONE_WEBHOOK_URL` for mobiles) |
| **Lusha** | **Direct dials / mobiles** and work emails by name + domain, synchronous | Free tier (few credits) → Pro | `LUSHA_API_KEY` |
| **Hunter.io** | Email finder (name + domain → email + confidence), email verifier, company email pattern | Free 25 searches/month → Starter | `HUNTER_API_KEY` |

Recommended setup: **Apollo for people + LinkedIn + email, Lusha for phones.** If you only buy one,
buy Apollo. ZoomInfo, Clay, Kaspr and RocketReach do the same job; add an adapter in
`enrich/people_data.py` if you prefer one of them.

Without any provider the pipeline still (a) attaches personal mailboxes found on the site to the
matching person, (b) infers the company's email pattern from them and (c) proposes the owner's
address from that pattern, flagged `guessed` and scored lower. Verify guessed addresses before
sending (Hunter's verifier is wired in).

## How it connects to the Sales Portal

* `supabase/migrations/0001_leads.sql` adds a `public.leads` table (RLS: approved users read/update,
  managers delete) and a `promote_lead(domain)` function that creates the company, contact, deal and
  an activity note using the portal's existing tables. The migration has been applied to the SALES
  PORTAL project.
* `leadgen sync portal` upserts on `domain`, so re-running is safe and updates scores/contacts in place.
* Reps work the lead in the portal (status: new → reviewed → contacted → meeting_booked → promoted);
  `promote` is exposed as an RPC the portal UI can call.
* `booking_link` is your booking page (`LEADGEN_BOOKING_URL`) with UTM parameters identifying the
  lead, so a booked meeting can be traced back to the source tool and domain.

## Layout

```
src/moh_leadgen/
  cli.py            leadgen command
  config.py         settings from .env
  models.py         Candidate / Person / Lead
  store.py          local SQLite working set
  http.py           polite, robots-aware HTTP client
  pipeline.py       shared enrichment chain
  scoring.py        ICP score + reasons (tune weights here)
  discovery/        seedfile, search (SerpAPI/Google CSE/Bing), semrush, crtsh
  enrich/           site_crawler, extract, legitscript, tech, dns_info, rdap, hunter, llm
  sinks/            supabase (portal), csv
  tools/            legitscript, competitor_platform, local_business
seeds/              starter domain lists (safe to append exports to)
supabase/migrations 0001_leads.sql
docs/               architecture + per-tool notes
```

## Compliance notes

* The crawler only reads public pages, honours `robots.txt`, rate-limits per host and sends a
  descriptive User-Agent. LegitScript's lookup page is reCAPTCHA-gated; we never automate it —
  verification uses the seal the merchant chose to publish, plus a verify link for a human check.
* Contact data is business contact data for B2B outreach. Email outreach must follow CAN-SPAM
  (identify yourself, honour opt-outs); calls/texts must follow TCPA. Scrub against your
  do-not-contact list before sequences.
* Guessed emails (from a Hunter pattern) are flagged `owner_email_guessed` and score lower.

See `docs/ARCHITECTURE.md` for the data flow and `docs/tools/*.md` for each tool.
