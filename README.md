# MyOrbit Health — Lead Generation Toolkit

A set of lead-generation tools that feed the **MyOrbit Health Sales Portal** (Lovable). Every tool shares one pipeline, one data model and one `leads` table, so a
rep sees LegitScript merchants, competitor-platform tenants and local clinics side by side,
ranked by fit, each with an owner name, email, phone and a personalised booking link.

| Tool | What it finds | Discovery sources |
|---|---|---|
| `legitscript` | Businesses displaying the **LegitScript certification seal** (telehealth, pharmacy, weight loss, TRT, peptides, med spas, addiction treatment) | seed list, search-engine dorks, Semrush backlinks of legitscript.com, Semrush advertisers on regulated keywords |
| `competitor` | Brands already running on a **competing telehealth platform** (Bask Health, Rimo, Healthie, OpenLoop, Beluga, MD Integrations, Qualiphy, Wheel) | certificate transparency (crt.sh), search dorks, seed list, DNS CNAME + HTML fingerprints |
| `local` | **Med spas, gyms, clinics, IV bars, weight-loss and hormone clinics** by city | Google Places text search, seed list |

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

Nothing is required beyond Python: with no API keys the LegitScript tool runs from the seed list
and verifies/enriches each site directly. Keys unlock more discovery and better owner data:

| Key | Unlocks |
|---|---|
| `SERPAPI_KEY` / `GOOGLE_CSE_KEY`+`GOOGLE_CSE_CX` / `BING_SEARCH_KEY` | search-dork discovery (any one) |
| `SEMRUSH_API_KEY` | backlinks of legitscript.com + advertisers on regulated keywords (needs API units) |
| `GOOGLE_PLACES_API_KEY` | `local` tool discovery |
| `HUNTER_API_KEY` | named mailboxes + email pattern |
| `ANTHROPIC_API_KEY` | Claude reads the crawled pages and extracts decision-makers, size, pitch angle |
| `SUPABASE_SERVICE_ROLE_KEY` | sync to the Sales Portal |
| `LEADGEN_BOOKING_URL` | personalised booking links on every lead |

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
