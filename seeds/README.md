# Seed lists

| File | Used by | Format |
|---|---|---|
| `competitor_platform_seed_domains.txt` | `leadgen discover competitor` (default seed) | `domain, Platform` |
| `legitscript_seed_domains.txt` | `leadgen discover legitscript` | one domain per line |
| `local_business_seed_domains.txt` | local tool | one domain per line |
| `public_lists_2026-11-27.txt` | `leadgen discover competitor --seed <file>` | one domain per line |

`public_lists_2026-11-27.txt` holds 104 domains of US companies that sell prescription telehealth
programs (mostly compounded GLP-1 weight loss) direct to consumers. All of them come from free public
government records. Each domain is the website named in the letter itself, not a guess from the company name.
`tests/test_seed_lists.py` checks the format: bare lowercase domains, no duplicates, no platform vendors.

## Evidence (fetched 2026-11-27)

* FDA warning letter index. Search for "telehealth" to get the 113 CDER letters with the subject
  "False & Misleading Claims/Misbranded (Telehealth)":
  https://www.fda.gov/inspections-compliance-enforcement-and-criminal-investigations/compliance-actions-and-activities/warning-letters
* The JSON behind that table, which links each letter (this is what we read):
  `https://www.fda.gov/datatables/views/ajax?view_name=warning_letter_solr_index&view_display_id=warning_letter_solr_block&search_api_fulltext=telehealth&length=150` (plus the other `view_*` parameters from the index page)
* Batches: 2025-09-09 (about 58 letters, including https://www.fda.gov/inspections-compliance-enforcement-and-criminal-investigations/warning-letters/hims-hers-health-inc-dba-hims-716567-09092025),
  2026-02-20 (30 letters; press release https://www.fda.gov/news-events/press-announcements/fda-warns-30-telehealth-companies-against-illegal-marketing-compounded-glp-1s),
  2026-06-08 (25 letters) and 2026-10-30 (RonanRx).
* FDA guidance page for telehealth companies (context only): https://www.fda.gov/drugs/human-drug-compounding/fda-telehealth-companies-what-know-when-promoting-compounded-drugs

## Checked and not used

* The FDA letters that match "semaglutide" or "tirzepatide" but are not tagged telehealth went to compounders, 503B
  facilities, API makers, "research peptide" sellers and rogue internet pharmacies. None of them run a DTC telehealth program.
* West Virginia Board of Medicine interstate telehealth registrations
  (https://wvbom.wv.gov/public/search/InterstateTelehealthIssued.asp) list individual MDs and PAs only, with no
  companies or websites.
* The Alaska Telemedicine Business Registry is the only state business-level registry we found.
  Its "Business License Download" spreadsheet (see
  https://commerce.alaska.gov/web/cbpl/BusinessLicensing/TelemedicineBusinessRegistryFAQs) returned HTTP 403 to
  automated clients, so it needs a manual download.
* LegitScript does not publish a list of certified merchants. Its public checker
  (https://www.legitscript.com/websites/) looks up one domain at a time.
* The NABP accredited digital pharmacy list loads through JavaScript, so the fetch got no rows.
