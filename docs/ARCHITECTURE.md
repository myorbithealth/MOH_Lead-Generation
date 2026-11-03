# Architecture

```
            discovery sources                     shared pipeline                          sinks
 ┌──────────────────────────────┐   ┌────────────────────────────────────────┐   ┌───────────────────────┐
 │ seed files                   │   │ crawl (robots-aware, rate-limited)     │   │ Sales Portal (Supabase│
 │ search dorks (SerpAPI/CSE/   │ → │ extract: emails, phones, address,      │ → │   public.leads upsert)│
 │   Bing)                      │   │   socials, JSON-LD, people             │   │ CSV                   │
 │ Semrush backlinks + ads      │   │ LegitScript seal evidence              │   └───────────────────────┘
 │ crt.sh (competitor tenants)  │   │ tech / competitor fingerprint          │              │
 │ Google Places (local biz)    │   │ DNS + RDAP                             │              ▼
 └──────────────────────────────┘   │ Hunter.io + Claude (optional)          │   promote_lead(domain)
                 │                  │ score + booking link                   │   → company + contact + deal
                 ▼                  └────────────────────────────────────────┘
          Candidate(domain, source)             ▲        │
                 │                              │        ▼
                 └────────── local SQLite store (leads.db) ──────────┘
```

## Data model

`Lead` (`src/moh_leadgen/models.py`) is the single record type. Discovery produces a `Candidate`
(domain + where it came from); the pipeline turns it into a `Lead`; sinks serialise the same object.
`Lead.to_portal_row()` is the exact shape of `public.leads` in the Sales Portal.

## Why a local SQLite store

* Discovery and enrichment run at different cadences (discovery is cheap and bursty; enrichment is
  slow because we are polite to each host). The store is the queue between them.
* Re-scoring is instant and offline. Re-syncing is idempotent (`upsert on domain`).
* The portal stays the system of record for *status*; the store is the system of record for
  *evidence*. `SupabaseSink.fetch_status` can pull rep decisions back when needed.

## Adding a tool

1. Create `tools/<name>.py` with a `Tool` subclass: set `name`, `source`, and return discovery
   sources from `discovery_sources()`. Reuse `SeedFileDiscovery` + `SearchDiscovery` and add any
   bespoke source (see `local_business.py` for a Places API example).
2. Register it in `tools/__init__.py`.
3. If the tool needs a new `source` value, add it to `LeadSource` in `models.py`.
4. Optional: add scoring signals in `scoring.py`, fingerprints in `enrich/tech.py`.

## Adding a discovery provider

Subclass `DiscoverySource`, yield `Candidate`s, and return `False` from `available()` when the
provider's key is missing so the CLI skips it cleanly.

## Scoring

`scoring.py` is intentionally simple additive weights with reasons. Change the numbers and run
`leadgen score` - every lead is re-ranked without crawling anything.

## Politeness and compliance

* `http.PoliteClient` reads `robots.txt`, waits `LEADGEN_MIN_DELAY_SECONDS` between requests to the
  same host, retries 429/5xx with backoff, and refuses bodies over 3 MB.
* LegitScript's lookup form is reCAPTCHA-protected and is never automated. Evidence comes from the
  merchant's own site plus a verify link for a human to click.
* Contact data collected is business contact data published by the business. Outreach must follow
  CAN-SPAM / TCPA; keep a suppression list.
