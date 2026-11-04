# Tool: `local`

**Goal:** med spas, gyms, IV bars, weight-loss / hormone / wellness clinics and urgent cares by
city - businesses with patients and cash flow but no telehealth program.

## Discovery

1. **OpenStreetMap** (free, default): each city is geocoded with Nominatim, then an Overpass query
   returns clinics, doctors, med spas / beauty, gyms, pharmacies, physio and weight-loss practices
   that publish a `website` tag (`discovery/overpass.py`, categories in `CATEGORY_FILTERS`).
2. **Google Places Text Search (New)** (optional, `GOOGLE_PLACES_API_KEY`): far deeper coverage
   across `DEFAULT_CATEGORIES × DEFAULT_CITIES` in `tools/local_business.py`.

```bash
leadgen discover local --opt "cities=Austin, TX;Dallas, TX" --opt "osm_categories=clinic,med_spa,gym"
leadgen discover local --opt "categories=med spa,IV hydration therapy" --opt "cities=Austin, TX"   # Places
leadgen enrich local --limit 100
```

Cost control (Places): each (category, city) pair is up to 3 requests (60 results); the defaults
are 18 categories × 36 cities ≈ 2k requests, so narrow with `--opt` for a test run.

## Scoring notes

Local businesses rarely carry a LegitScript seal, so they score on vertical fit, reachability and
paid-acquisition signals. Med spas, weight-loss and hormone clinics rank highest by default.
