# Tool: `legitscript`

**Goal:** every business displaying the LegitScript certification seal, with the owner's name,
email and phone, ranked by fit for white-label telehealth.

## Why LegitScript merchants

Google, Meta, Microsoft and TikTok require LegitScript certification before a business can run
ads for telehealth, online pharmacy, addiction treatment and most compounded/GLP-1 offers. So a
certified merchant has (1) passed a real compliance review, (2) is spending on paid acquisition and
(3) is selling a regulated health product online - the exact profile that outgrows a DIY stack.

## Discovery

| Source | Needs | Notes |
|---|---|---|
| `seeds/legitscript_seed_domains.txt` | nothing | Append any list; verification happens later |
| Wayback Machine CDX (`discovery/free_index.py`) | nothing | Every archived `legitscript.com/websites/?checker_keywords=<domain>` URL - i.e. the verify link behind a seal. Thousands of domains, refreshed as the archive crawls. |
| Common Crawl index | nothing | Same URL pattern across the last few monthly crawls |
| Search dorks (`SEARCH_DORKS` in `tools/legitscript.py`) | `ANTHROPIC_API_KEY` (Claude web search) or SerpAPI / Google CSE / Bing | Pages that say "LegitScript certified" / embed the seal |
| Semrush `backlinks_refdomains` of legitscript.com | Semrush key + API units | Optional |
| Semrush `phrase_adwords_historical` on regulated keywords | Semrush key + API units | Optional |

## Verification

`enrich/legitscript.py` looks in the crawled HTML for:

* `https://static.legitscript.com/seals/<id>.(png|js)` - the seal asset (strong)
* `https://www.legitscript.com/websites/?checker_keywords=<domain>` - the verify link (strong)
* text such as "LegitScript certified" (weak → `mentioned`)

`seal_detected` leads get +25; `mentioned` +12. The verify URL is stored so a rep can confirm on
legitscript.com with one click.

## Running

```bash
leadgen discover legitscript --limit 100
leadgen enrich legitscript --limit 50 --llm
leadgen top --tool legitscript
leadgen sync portal --tool legitscript --min-score 40
```

## Tuning

* Verticals and their keywords: `VERTICALS` in `enrich/legitscript.py`.
* Weights: `VERTICAL_WEIGHTS` and `score_lead` in `scoring.py`.
* Enterprise brands to down-rank: `ENTERPRISE_DOMAINS` in `domains.py`.
