# Tool: `local`

**Goal:** med spas, gyms, IV bars, weight-loss / hormone / wellness clinics and urgent cares by
city - businesses with patients and cash flow but no telehealth program.

## Discovery

Google Places *Text Search (New)* across `DEFAULT_CATEGORIES × DEFAULT_CITIES`
(`tools/local_business.py`). Each place that has a website becomes a candidate; the Places name,
phone, address, rating and review count are kept in `source_detail`.

```bash
leadgen discover local --opt "categories=med spa,IV hydration therapy" --opt "cities=Austin, TX;Dallas, TX"
leadgen enrich local --limit 100
```

Cost control: each (category, city) pair is up to 3 requests (60 results). The defaults are
18 categories × 36 cities ≈ 2k requests; narrow with `--opt` for a test run.

## Scoring notes

Local businesses rarely carry a LegitScript seal, so they score on vertical fit, reachability and
paid-acquisition signals. Med spas, weight-loss and hormone clinics rank highest by default.
