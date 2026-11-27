# MyOrbit Health outbound system plan

Prepared for Shubh, Divang and Stacy. Date: 27 November 2026.
All prices were checked on 2026-11-27 against the vendor pages listed in the sources appendix. Anything marked "unverified" has not been confirmed against a primary source or a live account.

---

## 1. Executive summary and the decision

### The recommendation

Buy the whole sending layer from one vendor and keep everything else we already have.

1. Smartlead Pro ($94/mo) sends, warms up, rotates mailboxes, runs the Day 0/3/7/14 timers, detects replies and bounces, and fires webhooks. Mailboxes are bought through Smartlead's own marketplace (SmartSenders), so nothing is set up in a Google or Microsoft admin console.
2. The Lovable/Supabase Sales Portal stays the system of record for status, contacts, meetings and deals. The repo pipeline stays the system of record for evidence.
3. We add exactly three things: an `outreach` package in the repo that gates and enrolls leads from the existing daily GitHub Actions run, three thin Supabase Edge Functions (Smartlead webhook with inline Claude classification, Cal.com booking webhook, Meta lead-ads webhook), and one additive database migration.
4. No Redis. No Bull. No always-on worker at stage 1. Smartlead holds the per-contact send timers, pg_cron holds the in-database timers, Edge Functions hold the webhooks. A persistent worker (Fly.io, $6.70/mo) is the stage-2 upgrade and has concrete triggers.

### The thing to understand first

"1,500 leads enrolled" is not 1,500 sendable contacts. The portal holds 1,471 leads. Only 50 of them (3.4%) have a direct owner email. 1,026 (70%) have only a generic mailbox such as info@ or support@. 395 (27%) have no email at all. Only 261 (18%) have an owner's name.

Contact enrichment is therefore the first bottleneck, before any sending platform matters. The first paid thing we turn on is Hunter, not Smartlead. A Hunter pass over all 1,471 domains runs on day 3. Its coverage number decides how fast we can send. If fewer than 30% of domains return a verified named email, stage 1 fills its 300/day with role mailboxes at a lower cadence and lower expected reply rate, and lead discovery moves up the priority list.

### What it costs and what it returns

| Item | Stage 1: 300 sends/day | Stage 2: 3,000 sends/day |
|---|---|---|
| Infrastructure per month | $613 | $1,540 |
| New leads enrolled per month (4-step sequence, 22 sending days) | 1,650 | 16,500 |
| Held demos per month, base case | 23 | 229 |
| Infrastructure cost per held demo, base case | $27 | $7 |
| Cost per held demo including $150 of founder time | $177 | $157 |
| Break-even demo-to-close rate against a $16,964 Satellite year-1 contract | 1.0% | 0.9% |
| Benchmark demo-to-close (SMB SaaS) | 25% to 32% | 25% to 32% |

Unit economics are positive in every scenario, including the pessimistic one (2.4% break-even). Money is not the constraint. Sendable contacts, lead supply and demo capacity are.

### Decisions needed from the team

1. Approve the vendor set: Smartlead Pro, SmartSenders mailboxes (Zapmail Google, pre-warmed), Hunter Growth, Cal.com Teams, Anthropic Claude API, Supabase Pro via Lovable Cloud, PublicWWW Pro. Total $613/mo at stage 1.
2. Approve six brand-variant .com domains for sending (never the main brand domain).
3. Approve the legal name and physical postal address that goes in every email footer.
4. Confirm the routing rules (section 6.6). They are a proposal.
5. Confirm that sending to monitored role mailboxes (info@, hello@, support@) is acceptable as a second tier. It is legal under CAN-SPAM. It will have lower reply rates.
6. Decide on Meta: who owns the Business Manager and Page, and whether a $1,500/mo test budget is approved for weeks 5 to 8.

### What changes from the way things worked before

- Email goes out from Smartlead, not from Gmail or SES. Reps approve replies in the portal, and the reply is sent from the Smartlead mailbox so the thread stays intact.
- Meetings are booked through Cal.com, not iClosed. Cal.com writes to each rep's Google Calendar.
- The weekly GitHub Actions job becomes daily and gets its secrets configured. It has effectively never run end to end.

---

## 2. What we have today and its limits

### 2.1 The pipeline

| Component | What it does | Status |
|---|---|---|
| `leadgen discover <tool>` | Finds candidate domains from seed files, PublicWWW source search, crt.sh certificate logs, search dorks, Wayback, Common Crawl, Overpass, Google Places | Works, but `--limit` counts yielded candidates per source, not new ones. With 809 seeded domains already stored, `discover --limit 150` adds about zero new domains. Reproduced. |
| `leadgen enrich` | Crawls up to 8 pages per site, extracts contacts and people, detects LegitScript seals, fingerprints the competitor platform, looks up DNS and RDAP, scores | Works. 609 of 2,545 enriched rows (24%) are unreachable sites marked enriched forever. No retry state. |
| `leadgen score` | Additive 0 to 100 score with reasons | Works. Weights are constants in code. No feedback from outcomes. |
| `leadgen sync portal` | Upserts pipeline-owned columns into `public.leads` | Works once `SUPABASE_SERVICE_ROLE_KEY` is set. Never writes status, notes or owner. |
| People data (Apollo, Lusha, Hunter adapters) | Finds decision makers | Code exists. No keys. `Hunter.verify()` is implemented but never called, so guessed emails never become verified. |
| GitHub Actions `leadgen.yml` | Weekly run, Mondays 06:00 UTC | Secrets never configured. `|| true` swallows failures. `leads.db` lives only in an Actions cache. |
| Portal (Lovable, Supabase project zrrndxdnopvgspyhrsxh) | Reps review leads, set status, promote to company + contact + deal | Works. `email_drafts`, `webhook_events`, `meetings` exist and are empty. |

### 2.2 What is in the portal today

| Field | Count | Share of 1,471 |
|---|---|---|
| Direct or owner email | 50 | 3.4% |
| Generic mailbox only (support@, info@, hello@) | 1,026 | 70% |
| No email at all | 395 | 27% |
| Phone number | 979 | 67% |
| Owner name | 261 | 18% |
| LegitScript seal on site | 391 | 27% |

| Vertical | Leads | Notes |
|---|---|---|
| weight_loss_glp1 | 603 | 256 with seal, average score 64 |
| mental_health | 211 | ICP fit to confirm |
| online_pharmacy | 122 | 73 with seal |
| peptides_longevity | 99 | |
| telehealth_general | 58 | |
| cbd | 52 | |
| womens_health | 44 | |
| mens_health_trt | 44 | 25 with seal, average score 65 |
| fitness_gym | 42 | |
| primary_care | 36 | |
| med_spa | 28 | |
| addiction | 19 | |
| none | 112 | |

### 2.3 The limits that matter

1. Discovery is starved by the `--limit` bug, not by sources. Fixing it is free and is the single biggest lever on lead supply.
2. Sendable contacts are almost zero. 50 direct emails. Everything downstream waits on this.
3. No feedback loop. Portal status never reaches the pipeline. There is no suppression list. Scoring cannot learn.
4. No outreach layer. Nothing sends, classifies, routes or books.
5. Evidence durability. `leads.db` sits in a GitHub Actions cache with 7-day eviction.
6. Only one contact per company reaches the CRM. `promote_lead` inserts one contact.
7. Duplicates and false penalties. crt.sh tenants are keyed by platform subdomain; 631 rows carry a redirect and a -15 penalty.

### 2.4 Scale limits

| Limit | Where it bites |
|---|---|
| SQLite single writer, no WAL | Any second process (continuous discover, a webhook processor) causes `database is locked` |
| Enrichment ceiling of 80 leads per tool per week | Config, not compute. 10x candidates means 35 to 60 crawl-hours, above the 6-hour Actions job cap unless sharded |
| Shared endpoints (rdap.org, crt.sh, Claude API) | No backoff or concurrency cap; 429s are dropped |
| PublicWWW 100 searches/day | No quota accounting in code |
| LLM cost | Opus for every site extraction, no caching |

---

## 3. Target architecture

### 3.1 Components and boundaries

| Layer | Runs where | Owns | Code change |
|---|---|---|---|
| Discovery, enrichment, scoring | GitHub Actions (existing `leadgen.yml`, now daily 06:17 UTC plus `workflow_dispatch`) | Evidence: crawl, fingerprints, LegitScript, DNS, people, score; `leads.db` (SQLite) | Fix `discover --limit`, `--workers 16`, WAL, retry state, upload `leads.db` as artifact every run, drop `|| true` |
| Portal sync | GitHub Actions to Supabase PostgREST | Pipeline-owned columns of `public.leads` | None |
| Enrollment gate | GitHub Actions, new `leadgen outreach enroll` step after `sync portal` | Reads `leads`, `contacts`, `suppressions`, `mailboxes`; pushes sendable contacts to a Smartlead campaign with custom fields | New `src/moh_leadgen/outreach/` (about 400 lines) |
| Personalization | GitHub Actions calling Claude Sonnet 5.5 | One opener sentence and a pitch angle per lead, plus rendered step subjects and bodies for the chosen variant | Reuse `enrich/llm.py` structured-output pattern |
| Sending, warm-up, rotation, sequence timers, unsubscribe link, bounce and reply detection | Smartlead (vendor) | Mailboxes, campaigns (one per segment), 4-step sequences, lead categories | None; configured via API |
| Reply ingestion and classification | Supabase Edge Function `smartlead-webhook` (Deno) | Verify shared secret, insert `webhook_events`, call Claude Haiku 5.5, write `reply_classifications`, set Smartlead category, pause lead, `assign_rep()`, insert `tasks` and `email_drafts(status='pending_approval')`, Slack notification | New (about 200 lines TypeScript) plus a 10-minute pg_cron retry |
| Booking | Cal.com Teams (vendor) to Edge Function `calcom-webhook` | Round-robin event types per segment; booking events to `meetings` | New (about 80 lines) |
| Meta lead ads | Meta to Edge Function `meta-leadgen` | Lead-ads webhook to `leads(source='meta_ads')`; audience sync and Conversions API from the daily Action | New (about 120 lines) plus `leadgen ingest` |
| In-database timers | Supabase pg_cron | Nightly posterior update, snooze release, suppression expiry, stuck-event alert, 10-minute webhook retry | Migration 0003 |
| System of record | Supabase (`leads.status`, `contacts`, `email_drafts`, `meetings`, `deals`, `tasks`) | Reps work in the Lovable portal; RLS unchanged | Migration 0003 (additive) |

### 3.2 Diagram

```
          GitHub Actions: daily 06:17 UTC + workflow_dispatch (inside $12/mo included compute)
  +-------------------------------------------------------------------------------------------+
  | leadgen discover -> enrich (--workers 16, Hunter find+verify) -> score -> sync portal      |
  |   -> outreach personalize (Sonnet 5.5)  -> outreach enroll (gate + Smartlead add-leads)   |
  |   -> outreach pull-stats (Smartlead per-variant counts -> variants)                       |
  |   -> weekly (Mon): outreach learn (retire/promote arms, Opus 5.5 proposes new arms)       |
  |   -> daily: meta audiences + CAPI flush (phase 3)                                          |
  +--------+----------------------------+--------------------------------+--------------------+
           | PostgREST (service key)    | Smartlead REST                 | Meta Marketing API
           v                            v                                v
  +------------------------+   +-----------------------------+   +--------------------------+
  | Supabase (portal proj) |   | Smartlead Pro               |   | Meta                     |
  | leads (status SoR)     |   |  18 -> 160 mailboxes        |   |  Custom Audiences        |
  | contacts, suppressions |   |  warm-up, rotation          |   |  Lead Ads (Instant Form) |
  | email_drafts (touches) |   |  one campaign per segment   |   |  Conversions API dataset |
  | reply_classifications  |   |  Day 0/3/7/14 timers        |   +-----------+--------------+
  | variants, rep_rules    |   |  categories, unsubscribe    |               | leadgen webhook
  | mailboxes, domains     |   +-------------+---------------+               |
  | meetings, tasks, deals |                 | webhooks (EMAIL_REPLY carries the body)
  | webhook_events (idem.) |                 v                               v
  | pg_cron: posteriors,   |   +---------------------------------------------------------+
  |   snooze, retry, SLA   |   | Supabase Edge Functions (Deno, thin)                    |
  +-----------+------------+   |  smartlead-webhook: verify -> webhook_events -> Haiku   |
              ^                |     classify -> Smartlead category/pause -> assign_rep  |
              |                |     -> tasks + email_drafts(pending_approval) -> Slack  |
              |                |  calcom-webhook: HMAC -> meetings upsert -> deal stage  |
              |                |  meta-leadgen: X-Hub-Signature-256 -> leads(meta_ads)   |
              |                +---------------------------------------------------------+
              |                                       ^
  +-----------+------------+                          | BOOKING_* webhooks
  | Lovable Sales Portal   |   +-------------------------------+
  | Shubh / Divang / Stacy |<--| Cal.com Teams (3 seats)       |
  | approve drafts, run    |   | round-robin per segment,      |
  | demos, set outcome     |   | per-rep links for routed leads|
  +------------------------+   +-------------------------------+
```

### 3.3 What runs where, in one sentence each

- Batch work (crawl, enrich, score, enroll, learn) runs once a day in GitHub Actions. Nothing time-critical lives there.
- Per-contact timing (send step 1 now, step 2 in three days, stop on reply) lives in Smartlead.
- Event handling (a reply, a booking, a Meta lead) lives in Edge Functions and fires within seconds.
- Database timers (nightly math, snooze release, retry sweeps) live in pg_cron.
- Humans work only in the portal: approve a drafted reply, run the demo, set the outcome.

### 3.4 What we deliberately do not build at stage 1

- No persistent worker, no Redis, no BullMQ, no Celery, no pgmq. The stage-2 triggers are listed in section 4.
- No own SMTP or SES. The AWS Acceptable Use Policy bans unsolicited mass email, and SES pauses an account at 0.5% complaints (sources appendix).
- No self-provisioned Google Workspace or Microsoft 365. Google throttles newly paid accounts until the domain has paid $100 cumulatively, up to 75 days.
- No cold SMS or iMessage. The TCPA treats texts as calls with no B2B carve-out for cell phones. Texting is allowed only after documented consent on the booking form, for reminders.
- No separate CRM or sequencing database. `email_drafts`, `meetings`, `deals`, `activities` and `tasks` already exist in the portal.

---

## 4. Orchestration decision: cron, persistent runner, or Redis/Bull

### 4.1 The answer

Cron plus vendor timers plus webhooks. No queue and no always-on worker at stage 1.

"Can it run all the time?" Yes, in the way that matters. Replies, bookings and Meta leads are handled within seconds by Edge Functions. Sends go out on Smartlead's schedule every day. The batch job runs daily. There is no process anyone has to keep alive.

### 4.2 Why each piece

1. GitHub Actions is the batch clock. The existing `leadgen.yml` moves to `cron: "17 6 * * *"` plus `workflow_dispatch`, with `concurrency: { group: leadgen, cancel-in-progress: false }` so two runs cannot double-enroll. Steps in order: `discover` (new-only limit), `enrich --workers 16 --retry-failed`, `score`, `sync portal --min-score 30`, `outreach personalize`, `outreach enroll`, `outreach pull-stats`, `meta sync` (phase 3), and on Mondays `outreach learn`. A daily 45 to 60 minute run uses 1,350 to 1,800 of the roughly 2,000 Linux 2-core minutes that the $12/mo included compute buys. That is tight. We add a payment method with a $20 spending limit and, if the crawl is I/O bound, move it to the 1-core slim runner at $0.002/min. The minute 17 avoids the top-of-hour delays GitHub documents.
2. Smartlead is the sequence timer. Day 0/3/7/14, stop-on-reply, per-mailbox daily caps, warm-up and rotation are vendor features. The repo never schedules an individual send. This is what removes the need for Celery, BullMQ, Inngest or Temporal at this volume.
3. pg_cron is the in-database clock. Nightly: refresh the funnel view, then update variant posteriors. Nightly: release snoozed contacts into a re-enrollment queue. Every 10 minutes: retry unprocessed webhook events by calling the Edge Function through pg_net with a dedicated retry secret (not the service-role key). Every 15 minutes: raise one alert row (deduplicated) if events are stuck more than 30 minutes. All jobs are short and well under Supabase's guidance of 8 concurrent jobs of under 10 minutes.
4. Edge Functions are the only always-on compute. They are event-driven. The Smartlead function does the whole reply path inline. Supabase's limits are 400 s wall-clock and 2 s CPU per worker (isolate), with awaited I/O excluded from CPU time, plus a 150 s request idle timeout. One to two seconds of awaited Claude I/O per request fits. Idempotency lives in the schema: `webhook_events (provider, idempotency_key)` is unique. A failure leaves `processed_at` null and the 10-minute retry picks it up. We return 200 to Smartlead within its 60-second window before doing any slow work where possible, and rely on the retry for the rest.

### 4.3 Why not the alternatives now

| Option | Why not at stage 1 |
|---|---|
| Celery, RQ, Dramatiq, BullMQ (Python) on Redis | Adds Redis (about $10/mo on Upstash), a scheduler singleton (Celery Beat), and visibility-timeout tuning. Celery on Redis redelivers unacknowledged tasks after one hour and its own docs say long countdowns belong in a database. Multi-day sequence steps are exactly that anti-pattern. No capability gain over Smartlead timers plus pg_cron at this volume (about 0.05 messages per second at stage 2). |
| Inngest, Temporal, Hatchet | Correct stage-2 engines. Not two-week ships. |
| Trigger.dev | Tasks are TypeScript only. |
| Prefect, Dagster | Batch DAG tools. Wrong shape for webhook-driven sequences. |
| A persistent runner alone | Loses work on restart with no durable queue. The queue we would add is Postgres anyway. |

### 4.4 Stage-2 triggers and the upgrade path

Any one of these moves us to stage 2 orchestration:

- Sends above about 5,000/day, or more than about 10,000 contacts live in sequences.
- The daily Action exceeds 3 hours (crawl backlog).
- Edge Function classification failures above 2% per day, or an interested reply misses its 1-hour SLA because of a retry gap.
- pg_cron needs more than 8 concurrent or longer than 10-minute jobs.
- The Micro Postgres (1 GB, 60 direct connections) shows connection pressure. First response: Small compute at about $15/mo.

The upgrade is pre-wired, not a redesign. Migration 0003 includes an `AFTER INSERT` trigger stub on `webhook_events` that can call `pgmq.send`. Stage 2 enables Supabase Queues (pgmq), deploys the same Docker image that runs in GitHub Actions to one Fly.io machine (shared-cpu-1x 1 GB, $6.70/mo), and points it at the queues. Edge Functions then become insert-only.

### 4.5 Where the Lovable Cloud question sits

Everything above assumes the Lovable Cloud wrapper on project zrrndxdnopvgspyhrsxh exposes pg_cron, pg_net, Vault, Edge Function deployment from the CLI, and the service-role key. This is unverified. It is the first check on day 1. If any piece is missing, the fallback is to link the project to a direct Supabase Pro organization, or to create a sibling Supabase project for the outreach tables and sync status back through an RPC. Worst case, the pg_cron jobs run as steps in the daily Action and the webhook retry becomes a small 15-minute Action (which would cost about $17/mo of extra minutes).

---

## 5. Email infrastructure

### 5.1 Vendor choice

| Role | Vendor | Why |
|---|---|---|
| Sending control plane | Smartlead Pro | The team already knows it. Pro includes API and webhooks (confirmed on the pricing page and help center; Base does not). 90,000 sends/month and 30,000 stored contacts. Add up to 1,000 leads per call with up to 200 custom fields. Per-lead pause, resume, unsubscribe. Lead categories settable via API. 13 webhook events; EMAIL_REPLY carries the reply body. Rate limit 200 requests/min on the account key. |
| Mailboxes and domains | Smartlead SmartSenders, Zapmail pre-warmed Google (stage 1); Zapmail fresh Google plus Mailforge SMTP (stage 2) | Bought through Smartlead so they auto-link and DNS is handled. Pre-warmed boxes skip the 2 to 3 week warm-up so sends start in week 2. Mailforge is the second provider pool at stage 2 so one Google-side reputation event cannot stop all sending. |
| Fallback sequencer | Instantly Hypergrowth ($107/mo) | Only if Smartlead's SmartSenders API access is refused. Instantly's DFY mailbox order endpoint is fully public. |

Smartlead facts that shape the design:

- The SmartSenders ordering API requires emailing support@smartlead.ai for marketplace access. We ask on day 1. If it is not enabled within 48 hours, the one-time mailbox order is the only UI step in this plan, or we switch to Instantly.
- Pre-warmed Zapmail mailboxes: $9/mailbox/mo plus $18/domain/yr, .com only, up to 5 mailboxes per domain, cannot have mailboxes added later, expire after about 9 to 10 months, cannot be moved outside Smartlead.
- Smartlead's own Zapmail guidance says do not exceed about 20 sends/day per Google mailbox and stay under 50/day including warm-up. The "20 to 25/day" figure in earlier drafts is not vendor-prescribed. We plan at 20.
- EMAIL_REPLY payload field names differ across Smartlead's own docs (`reply_body`, `reply_message.html`, `reply.body`). The Edge Function reads all three and we validate against one live "Send Test" delivery on day 4. Smartlead retries failed webhooks at 1 m, 5 m, 15 m, 1 h, 6 h and disables the webhook after 5 failures (from the vendor's core webhooks page; unverified against a live account).

### 5.2 Domain and inbox math

Planning constants: 22 sending days per month; 3 mailboxes per domain; 4 steps per sequence; 20 sends per mailbox per day on Google (Smartlead guidance), 20 on Mailforge SMTP (Smartlead's SMTP guidance is 20 cold plus 20 warm-up); about 15% headroom.

| | Stage 1: 300/day | Stage 2: 3,000/day |
|---|---|---|
| Mailboxes | 18 pre-warmed Zapmail Google | 100 fresh Zapmail Google + 60 Mailforge SMTP = 160 |
| Domains | 6 .com brand variants | 34 (Google) + 20 (Mailforge) = 54 |
| Capacity at 20/mailbox/day | 360 (20% headroom) | 3,200 (7% headroom); 3,700 if Google boxes prove 25/day for 4 weeks |
| New leads enrolled per day (sends / 4 steps) | 75 | 750 |
| New leads per month | 1,650 | 16,500 |
| Monthly cost (mailboxes + domains amortized) | 18 x $9 + 6 x $18/12 = $171 | 100 x $4.50 + 34 x $13/12 = $487; 60 x $3 + 20 x $14/12 = $203; total $690 |

Each sending domain is a separate registrable .com (for example getmyorbit.com style, no hyphens or numbers, real first names on mailboxes). Google counts subdomains toward the primary domain's 5,000/day bulk threshold, so subdomains would not help. Every sending domain 301-redirects to the main site.

Scale by adding domains and mailboxes in cohorts, never by raising per-mailbox caps. Fresh mailboxes need 2 to 3 weeks of warm-up, so stage 2 cohorts of about 30 mailboxes are ordered every 2 to 3 weeks starting in week 3.

### 5.3 Warm-up and ramp schedule

Pre-warmed mailboxes (stage 1), per mailbox, enforced by the `mailboxes.daily_cap` column and pushed to Smartlead:

| Sending day | Cap per mailbox | Total with 18 mailboxes | Gate to advance |
|---|---|---|---|
| 1 to 2 | 5 | 90 | Bounce <= 1%, no EMAIL_ACCOUNT_DISCONNECTED |
| 3 to 5 | 10 | 180 | Bounce <= 1%, complaints < 0.1% |
| 6 to 9 | 15 | 270 | Same |
| 10 onward | 20 | 360 | Same; seed placement >= 80% once weekly tests run |

Fresh mailboxes (stage 2 cohorts): days 1 to 14 warm-up only at Smartlead's warm-up setting (25/day warm-up, no campaigns); days 15 to 18 send 5, 8, 10, 12; days 19 to 28 send 15, 18, 20; from day 29 cap 20; raise to 25 only after 4 clean weeks. Warm-up stays on permanently.

Total sends therefore reach 300/day in week 3 of sending, not week 2. First sends happen in week 2.

### 5.4 Guardrails enforced automatically

All thresholds live in `app_settings` and `mailboxes`. The daily Action and the Edge Function enforce them. Smartlead's own bounce auto-pause is a second line of defense.

| Signal | Threshold | Automatic action |
|---|---|---|
| Hard bounce rate, rolling 200 sends per campaign | > 2% | Pause campaign in Smartlead, re-verify the list with Hunter, alert |
| Hard bounce rate per mailbox, 7 days | > 3% | Pause mailbox (`mailboxes.status='paused'`), alert |
| Google Postmaster user-reported spam per domain | >= 0.1% | Halve the domain's caps |
| Google Postmaster user-reported spam per domain | >= 0.3% | Stop all sends from the domain for 7 days |
| Seed placement test (weekly, stage 2) | < 80% inbox | Pause domain until 48 hours clean |
| EMAIL_ACCOUNT_DISCONNECTED webhook | any | Pause mailbox row, alert, exclude from cap sum |
| Unsubscribe rate per variant | > 2x the control arm | Retire the variant |
| Smartlead `campaign_bounce_threshold` webhook | fires | Written to `webhook_events`; campaign stays paused until a human resumes |
| Enrollment cap | sum of active `mailboxes.daily_cap` / 4 | Enforced in SQL before any Smartlead call; the vendor's 429 is never the limiter |

Resume rules: a paused mailbox or domain resumes only after 48 hours of stable metrics, at the previous cap halved.

### 5.5 Compliance checklist (enforced by a linter before every enrollment)

CAN-SPAM has no B2B exception and liability does not transfer to the vendor ($53,088 per violating email). Google and Yahoo require one-click unsubscribe headers and unsubscribes honored within 48 hours; Microsoft hard-rejects unauthenticated bulk mail (550 5.7.515). Gmail stepped up enforcement in November 2025.

| Requirement | How it is met |
|---|---|
| Truthful headers and subject | Templates linted; no fake "Re:" |
| Identification as an ad | B2B pitch framing in body |
| Physical postal address | Footer field in `app_settings`; linter rejects a draft without it |
| Working opt-out for 30+ days | Smartlead unsubscribe link plus the visible sentence; `List-Unsubscribe` and `List-Unsubscribe-Post` headers (Smartlead's RFC 8058 header support is unverified; if absent, the Edge Function `unsubscribe` endpoint URL is used) |
| Opt-outs honored within 10 business days (legal) and 48 hours (Google) | Honored within minutes: LEAD_UNSUBSCRIBED webhook and classifier `unsubscribe` both write `suppressions` and call Smartlead global unsubscribe |
| SPF, DKIM (2048-bit), DMARC (p=none minimum, aligned) per domain | DNS preflight job; a failing domain is quarantined |
| Spam rate < 0.1% recommended, never >= 0.3% | Postmaster Tools pull plus the guardrail table |
| Suppression before every send | `is_suppressed(email, domain)` checked in the enrollment gate |
| Frequency caps | 4 touches per sequence, one active sequence per company, 2 contacts per company, 90-day cooldown before re-enrollment, no weekends, 7am to 6pm recipient local time |
| No PHI | Pitch infrastructure, never treatments; classifier `contains_phi` triggers redaction before storage |
| Audit trail | Sends, unsubscribes and suppressions retained 3 years in `email_drafts` and `suppressions` |

---

## 6. Outreach design

### 6.1 Sendable tiers and the enrichment pass

| Tier | Definition | Today | After week 1 (estimate, unverified) | Treatment |
|---|---|---|---|---|
| A | Named decision maker with a verified, provider or on-site email | 50 | 450 to 650 | 4-step personalized sequence |
| B | Monitored role mailbox (info@, hello@, support@) | 1,026 | about 1,000 | 2-step sequence (Day 0, Day 5), separate campaign and metrics, 20% of daily volume |
| C | No email | 395 | 150 to 250 after Hunter | High-score leads with a business phone: manual B2B call list for the reps; otherwise parked |

The week-1 Hunter pass over all 1,471 domains:

| Step | Hunter endpoint | Credits | Expected |
|---|---|---|---|
| Domain Search on every domain | 1 credit per domain that returns 1 to 10 emails, 0 if none | <= 1,471 | Named emails for the share of domains Hunter indexes (assumption 30% to 45%; measure on day 3) |
| Email Finder for the 261 owner names without a hit | 1 credit only when found; result is auto-verified | <= 261 | Promotes guessed emails to verified (+12 score, sendable) |
| Verifier on on-site and pattern-derived emails | 0.5 credit each | about 500 | Only verified or accept-all at reduced cap are sent |
| Total | | about 2,200 of Growth's 10,000/month | |

Apollo stays optional. Apollo's pricing page says API access is on Custom plans while its docs list Basic limits. We do not budget it until an in-account API test passes.

### 6.2 Sequence design

One Smartlead campaign per segment: `glp1_competitor`, `trt_peptides`, `pharmacy_telehealth`, `med_spa_clinic_gym`, `role_mailbox` (Tier B). Templates in Smartlead are placeholders only: `{{step1_subject}}`, `{{step1_body}}` through `{{step4_body}}`. The repo renders the actual text for the variant assigned to that lead into the lead's custom fields at enrollment. This gives per-lead variant assignment with one campaign per segment, not one campaign per arm.

| Step | Day | Purpose | Length |
|---|---|---|---|
| 1 | 0 | Opener: one personalized observation, one sentence of what MyOrbit does, one question | 60 to 90 words |
| 2 | 3 | Proof: one relevant outcome or compliance point (LegitScript, pharmacy fulfilment, checkout), same thread | 50 to 70 words |
| 3 | 7 | Angle switch: the arm's alternative pitch (speed to launch, cost versus current platform, compliance) | 50 to 70 words |
| 4 | 14 | Close the loop: short, offers the booking link, says we will stop | 30 to 50 words |

Tier B gets steps 1 and 4 only, addressed to the business, not a person.

Stop rules: any reply pauses the sequence (Smartlead stop-on-reply). Out-of-office pauses until `snooze_until` then resumes at the same step.

### 6.3 Personalization inputs from the lead record

| Input | Source column | Use |
|---|---|---|
| Owner first name, title | `leads.owner_name`, `owner_title` | Greeting; Tier A only |
| Company name | `leads.company_name` | Everywhere |
| Competitor platform | `leads.competitor_platform` (normalized by `PLATFORM_ALIASES`) | Displacement angle; never named negatively |
| Vertical | `leads.vertical` | Segment selection, examples used |
| LegitScript status | `leads.legitscript_status` | Compliance angle (seal holders) or certification angle (none) |
| Pitch angle, size, multi-location | `leads.source_detail.ai.pitch_angle`, `estimated_size`, `is_multi_location` | Opener and routing |
| Tech stack signals | `leads.tech_stack` (Stripe, Shopify, GoHighLevel, ad pixels) | DIY-stack angle |
| Booking link | `leads.booking_link` plus per-rep Cal.com link with `cid` | Step 4 and all drafted replies |
| Opener sentence | Generated by Claude Sonnet 5.5 from the crawled text, cached in `source_detail.ai.opener` by content hash | Step 1 |

Rules for generated text: no medical claims, no patient stories, no condition words in subject lines, B2B framing, footer always present.

### 6.4 Reply classification

Model: `claude-haiku-5-5` with `output_config.effort: "low"` and `output_config.format: {type: "json_schema", schema: ...}`. Structured outputs are stable with no beta header. The system prompt (taxonomy, routing rules, redaction rule, 12 examples) is cached with `cache_control`. Input: our last sent subject and body, the reply text from the webhook, lead vertical, platform and score. Escalate to `claude-sonnet-5-5` when confidence is `low` or disposition is `unclear`; if still unclear, open a task for the triage owner. Enum casing is not guaranteed by the API, so values are lowercase and compared case-insensitively. Check `stop_reason` for `refusal` and `max_tokens`; both route to `unclear`. Cost: about $0.0002 per reply.

Disposition enum (Postgres type `public.disposition`):

`interested | meeting_requested | meeting_booked | question_objection | maybe_later | not_interested | wrong_person_referral | out_of_office | auto_reply | unsubscribe | bounce | unclear`

Smartlead category IDs are fetched at runtime from `GET /api/v1/leads/fetch-categories` because Smartlead's docs disagree on the defaults. Mapping: interested, meeting_requested, question_objection to Interested; meeting_booked to Meeting Booked; not_interested to Not Interested; unsubscribe to Do Not Contact; out_of_office to Out of Office; the rest to custom categories created once via API.

JSON schema:

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": ["disposition", "confidence", "summary", "objection", "snooze_until",
               "referral", "requested_times", "draft_reply", "contains_phi", "compliance_flag"],
  "properties": {
    "disposition": {"type": "string", "enum": ["interested", "meeting_requested", "meeting_booked",
      "question_objection", "maybe_later", "not_interested", "wrong_person_referral",
      "out_of_office", "auto_reply", "unsubscribe", "bounce", "unclear"]},
    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
    "summary": {"type": "string", "description": "One sentence. No reasoning steps. No patient details."},
    "objection": {"type": "string", "enum": ["none", "price", "timing", "already_have_platform",
      "compliance_or_legitscript", "not_decision_maker", "bad_experience_with_vendors", "other"]},
    "snooze_until": {"anyOf": [{"type": "string", "format": "date"}, {"type": "null"}]},
    "referral": {"anyOf": [
      {"type": "object", "additionalProperties": false, "required": ["name", "email", "title"],
       "properties": {
         "name": {"anyOf": [{"type": "string"}, {"type": "null"}]},
         "email": {"anyOf": [{"type": "string", "format": "email"}, {"type": "null"}]},
         "title": {"anyOf": [{"type": "string"}, {"type": "null"}]}}},
      {"type": "null"}]},
    "requested_times": {"type": "array", "items": {"type": "string"}},
    "draft_reply": {"anyOf": [{"type": "string"}, {"type": "null"}],
      "description": "Only for interested, meeting_requested, question_objection, wrong_person_referral. B2B tone. Includes {{booking_link}}."},
    "contains_phi": {"type": "boolean", "description": "True if any patient or health detail appears. Raw text is then redacted before storage."},
    "compliance_flag": {"type": "boolean", "description": "True if the reply asks us to stop, alleges spam, or mentions legal action."}
  }
}
```

Deterministic paths run regardless of the model: Smartlead's LEAD_UNSUBSCRIBED and EMAIL_BOUNCE events write `suppressions` directly. A weekly reconciliation checks that every unsubscribe event has a suppression row within 48 hours.

### 6.5 Actions per disposition

| Disposition | Smartlead | Portal | Human | SLA |
|---|---|---|---|---|
| meeting_requested | Category Interested, pause lead | `assign_rep`, `promote_lead`, draft with 3 slots from Cal.com availability in `email_drafts(status='pending_approval')`, `tasks`, Slack | Assigned rep approves draft | 30 min |
| interested | Same | Same, draft includes the rep's Cal.com link with `cid` | Assigned rep | 1 hour |
| question_objection | Category Information Request, pause | Same; `objection` stored on the deal | Assigned rep | 4 business hours |
| meeting_booked | Category Meeting Booked, pause | Cal.com webhook is the source of truth; if no booking arrives in 24 h, task | Booking organizer | n/a |
| maybe_later | Pause | `contacts.snooze_until` (default +60 days), soft suppression until then, re-enroll into `nurture` campaign after | Low-priority task only if confidence low | 1 business day |
| wrong_person_referral | Unsubscribe original contact from campaign | Upsert referral into `contacts`; enroll next run if Hunter verifies the email; thank-you draft | Rep only if referral has no email | Next run |
| out_of_office | Pause until `snooze_until` (default +7 days), then resume | Excluded from reply-rate metrics | None | n/a |
| auto_reply | None | Excluded from metrics | None | n/a |
| not_interested | Category Not Interested, unsubscribe from campaign | `suppressions(domain, reason='not_interested', expires_at=now()+180 days)` | None; monthly digest | n/a |
| unsubscribe | Global unsubscribe | `suppressions(email and domain, permanent)`, `contacts.unsubscribed_at`, `leads.status='unsubscribed'`, removed from Meta audiences | None | Minutes |
| bounce | (EMAIL_BOUNCE webhook) | `contacts.bounced_at`, suppress email, try the next sendable contact at the company once | None | Minutes |
| unclear | None | Sonnet re-check; else task to triage owner | Triage owner (Shubh by default) | 4 business hours |

Approved drafts are sent through Smartlead's reply-from-inbox endpoint from the mailbox that sent the sequence, so the thread stays intact. Never from a rep's personal Gmail. No human-facing reply is ever auto-sent.

### 6.6 Routing rules (data in `rep_rules`, a proposal to confirm on day 1)

Rules are evaluated in priority order. All conditions inside one rule must match (AND). The first matching rule wins.

| Priority | Rule | Rep |
|---|---|---|
| 0 | Existing relationship: `leads.owner_id` set, or an open deal owner, or a prior meeting `claimed_by` for the company | That rep |
| 10 | disposition is question_objection, or score >= 80, or LegitScript seal_detected, or `is_multi_location` | Shubh |
| 20 | vertical in (weight_loss_glp1, mens_health_trt, peptides_longevity, online_pharmacy, telehealth_general) and `competitor_platform` is set | Divang |
| 30 | vertical in (med_spa, fitness_gym, primary_care, dental, womens_health) or source = local_business | Stacy |
| 100 | Fallback: fewest open tasks plus upcoming meetings in the last 7 days, weighted by `profiles.routing_weight` | Load balanced |

Every assignment writes a `routing_log` row (rule id, reason) so reassignments are auditable. Assignment is serialized with `pg_advisory_xact_lock` so two webhooks cannot rotate at once.

### 6.7 Booking flow

1. Cal.com Teams: one round-robin event type per segment created via `POST /v2/teams/{teamId}/event-types` with `schedulingType: "roundRobin"`, hosts with `priority` and `weight`, and `isRRWeightsEnabled: true` (weights are ignored without it). Proposed weights: Shubh 50, Divang 100, Stacy 100. Per-rep personal links are used in drafted replies; the shared link is for cold traffic.
2. Every booking link carries `?cid=<contacts.cid>&utm_source=email&utm_campaign=<segment>&utm_content=<step>&name=&email=`. The `cid` is an opaque token on every email link and a hidden field on the booking page.
3. One webhook per team event type via `POST /v2/teams/{teamId}/event-types/{eventTypeId}/webhooks` with triggers BOOKING_CREATED, BOOKING_RESCHEDULED, BOOKING_CANCELLED, BOOKING_NO_SHOW_UPDATED, MEETING_ENDED, BOOKING_REASSIGNED and a secret. (The user-level `POST /v2/webhooks` also works but is user-scoped.)
4. Edge Function `calcom-webhook` verifies `x-cal-signature-256` (HMAC-SHA256 over the raw body; encoding verified empirically on day 5), inserts `webhook_events(provider='calcom', idempotency_key=payload.uid||':'||triggerEvent)`, upserts `meetings` (contact resolved by `cid`, then lower(invitee_email), then `meta_lead_id`), sets `deals.stage`, sets Smartlead category Meeting Booked, and sends the Conversions API `meeting_booked` event (phase 3).
5. MEETING_ENDED fires at the scheduled end time, not on attendance. It creates a task for the rep to set `meetings.outcome` (held, no_show, qualified, closed_won, lost). BOOKING_NO_SHOW_UPDATED sets `no_show` and enrolls a one-step `no_show` re-engagement.
6. Same-day and next-day slots stay open in the event types. Show rates fall with lead time.

---

## 7. The self-improving loop

### 7.1 What is measured

Per enrolled lead, not per email. Per `variant_id`, step, segment, source, email tier, mailbox pool and week:

| Stage | Metric | Source |
|---|---|---|
| Delivery | sent, delivered, bounced, unsubscribed, complaint proxy (Postmaster) | Smartlead EMAIL_SENT, EMAIL_BOUNCE, LEAD_UNSUBSCRIBED into `email_drafts` |
| Engagement | opened (recorded, never a reward: Apple and Gmail proxies), clicked (`cid` links), replied | EMAIL_OPEN, EMAIL_LINK_CLICK, EMAIL_REPLY |
| Quality | positive_reply = interested or meeting_requested or meeting_booked | `reply_classifications` |
| Outcome | booked, held, no_show, closed_won, year-1 value | `meetings.outcome`, `deals.stage` |
| Cost | infrastructure per segment and per channel, Claude tokens per step | `monthly_costs`, `job_runs` |
| Model | classifier override rate, routing reassignments, reply latency per rep | `reply_classifications.human_override`, `routing_log`, `tasks` |

The `outreach_funnel` materialized view and the `cost_per_meeting` view are refreshed nightly and shown in the portal.

### 7.2 Experiment design

Why not classic A/B tests: at 1,650 leads/month, detecting a 2x lift on a 0.75% meeting rate needs about 3,100 leads per arm. Not feasible. Decisions are made on positive-reply rate, and held meetings are the tiebreaker.

Arms live in `variants` with a `slot` (step1_subject, step1_body, step2_body, step3_body, step4_body) and a segment. Launch arms for step 1: competitor-displacement angle, compliance and LegitScript angle, speed-to-launch angle, plus a control.

| Phase | Weeks | Allocation | Rule |
|---|---|---|---|
| Equal split | 2 to 4 | Round-robin across arms | Collect at least 100 to 200 sends per arm before any decision |
| Thompson sampling | 5 onward | One Beta(1 + positives, 1 + sends - positives) draw per active arm per lead; the max wins | Control arm keeps at least 10% of traffic |
| Retire | ongoing | | P(best) < 5% after >= 300 sends, or unsubscribe rate > 2x control, or bounce > 2% |
| Promote to control | ongoing | | P(best) > 90% with >= 500 sends and >= 15 positive replies |

Variants are rendered into Smartlead custom fields at enrollment, so the arm chosen on day 0 is what step 4 sends on day 14. Posteriors therefore lag by up to two weeks per lead. That is acceptable for batched Thompson sampling.

### 7.3 How the system updates itself

1. Copy (weekly, Monday). `outreach learn` calls `claude-opus-5-5` with structured output. Input: top and bottom arms with posteriors, 20 redacted reply excerpts classified interested or question_objection, 20 not_interested, the objection histogram, and the compliance rules. Output: two or three new arms per slot, each with a `hypothesis` and `parent_id`. The job deduplicates near-duplicates, lints footer and claims, inserts them with half the parent's alpha and beta, and they take effect at the next enrollment. Lineage shows which angles compound.
2. Targeting (monthly). `scoring_weights` is recomputed from positive-reply and held rates per feature (vertical, platform, source, seal status, email tier, corroboration count). Each weight is `base_weight x multiplier` with the multiplier clamped to 0.5 to 2.0 and a minimum sample of 100 enrollments. `leadgen score` reads the table; the portal shows it so humans can veto. A simple `predicted_meeting_p` orders the enrollment queue.
3. Enrollment policy. Daily cap, steps per sequence, send window and tier mix are `app_settings` rows. The learn job may only propose changes to them as a task for Shubh. Nothing that touches deliverability changes without a human.
4. Classifier (weekly). If the human override rate for any disposition exceeds 10%, the overridden cases become few-shot examples, `prompt_version` is bumped, and the last 200 replies are re-run offline before the new prompt is promoted.
5. Deliverability caps (daily). The guardrail table in section 5.4.
6. Routing (monthly). Show rate and close rate per rep feed proposed `routing_weight` changes; Shubh decides.

### 7.4 The weekly routine (Monday, about 30 minutes of human time)

The routine is a checklist run by a Claude Code session against the database and the vendor APIs. Output: a Slack digest, a `weekly_reports` row, and a short list of proposed changes that need Shubh's approval.

1. Health: unprocessed `webhook_events`, Edge Function error rate, Smartlead disconnects, DNS preflight per domain, Postmaster spam rate, bounce by mailbox, Actions minutes used.
2. Funnel: trailing 7 and 28 days by segment and tier; flag any metric more than 30% off the base case.
3. Variants: apply retire and promote rules; run the Opus proposal; show lineage.
4. Supply: sendable Tier A and Tier B inventory versus next week's planned enrollments. Inventory gate: at least 2 weeks of sendable, unsuppressed leads at the current rate before any mailbox cohort or cap increase. If short, raise discovery effort and hold volume; never widen the ICP silently.
5. Reply ops: SLA misses, pending drafts older than 24 hours, override rate.
6. Compliance: every unsubscribe event has a suppression row; footer lint over active variants; PHI redaction audit of 20 random stored replies.
7. Meta (phase 3): audience sizes versus the 100-matched lookalike floor, cost per booked demo by channel, pause any ad set that spent $600 without a booked meeting.
8. Economics: `cost_per_meeting` by segment and channel; prune segments below break-even for two consecutive months.

Success metric for the loop itself: held demos per 1,000 enrolled leads, reported weekly next to cost per held demo.

---

## 8. Getting more leads

Target: grow the portal from 1,471 to about 5,000 qualified leads in two months at under $300/mo of incremental discovery spend, and raise Tier A coverage from 3.4% to above 40% of enrolled leads.

### 8.1 Ranked sources

| Rank | Source | Expected net-new US leads | Cost | When | Notes |
|---|---|---|---|---|---|
| 1 | Fix `discover --limit` to count new domains only | Unblocks every source below | $0 | Week 1 | cli.py lines 65 to 78 and every source count yielded, not new |
| 2 | Hunter pass over the 1,471 existing leads | 0 new leads, 400 to 600 new sendable contacts | Inside Hunter Growth $149 | Week 1 | The biggest lever for the first meeting |
| 3 | crt.sh re-runs for platforms that host tenants on subdomains | 500 to 900 | $0 | Week 1 | `%.fusehealth.com` (407 names, about 67 brand slugs versus 24 in portal), `%.affiliates.commerce.telegramd.com`, `%.belugahealth.com` (111), `%.akutehealth.com` (228, 158 clinic slugs, new platform), `%.med.cuvo.co`, `%.carevalidate.com`, `%.canvasmedical.com` (974, sample DTC share), openloophealth.com (retry with 5 to 10 s pauses). Re-key tenants to the brand domain on redirect; store `not_before` as a daily watermark |
| 4 | FDA warning-letter recipients | 50 to 80 named DTC brands | $0 | Week 1 | About 50 to 55 GLP-1 telehealth companies in September 2025, about 25 more in June 2026 |
| 5 | PublicWWW multi-marker signatures | 1,500 to 3,000 med spas plus 300 to 600 small-platform brands | PublicWWW Pro $109 (already planned) | Weeks 2 to 4 | `SOURCE_MARKERS` becomes a list per platform with the verified markers in docs/tools/competitor.md plus med-spa embeds (quidget.qualiphy.me, moxie-suite-embeds, book.joinmoxie.com, client. and link.aestheticrecord.com, dashboard.boulevard.io) and footers of about 20 small white-label vendors (Remedora, UpScript, WellSync, Wizlo, LegUp Rx, NimbusRX, Altro, RxPNow, Asher, OutfitMD, WellieMD, ClinicX). Quota accounting in code: about 70 of 100 daily searches, rotated across platforms. Record corroborating sources in `also_found_via` instead of dropping duplicates (+3 to +5 score per source). Re-authorize the PublicWWW token first |
| 6 | LegitScript tool on GitHub runners | Thousands of seal holders (unverified) | $0 | Weeks 2 to 4 | Wayback CDX and Common Crawl are blocked only in the development sandbox |
| 7 | Google Ads Transparency Center via SerpApi | 200 to 600 advertisers actively buying GLP-1, TRT, peptide, hair and ED traffic | SerpApi Starter $25/mo if the tier exists; Developer $75/mo otherwise (tier list conflicts between vendor pages; confirm) | Weeks 5 to 8 | Also unlocks the repo's 15 LegitScript search dorks. Meta and TikTok ad-library APIs do not return US commercial ads |
| 8 | Exa Websets or Parallel FindAll semantic discovery | 500 to 1,500 | About $80 to $300 one-off per quarter | Weeks 5 to 8 | "US DTC telehealth companies selling compounded GLP-1, TRT, peptides online"; dedupe against the store |
| 9 | Google Places for med spa, weight-loss and hormone clinics x 36 cities | 5,000 to 15,000 raw businesses, low intent | Inside the 5,000 free Text Search requests/month | Stage 2 | Store only `place_id` and website URL; names and addresses come from the site crawl (Places policy) |
| 10 | Platform aliases and DIY-stack ICP | Better segmentation, +scoring | $0 | Week 2 | Karpa/RxAve, Beluga/Beluga Health, SteadyMD/GetLabs, MD/MD Integrations collapse to one name. Sites with a GLP-1 or TRT vertical plus Stripe or Shopify plus ad pixels and no platform get a scoring bonus |

Not buying: BuiltWith ($295 to $995/mo), Wappalyzer Business ($450/mo), Clay Growth ($205+/mo). None filters telehealth platforms. Apollo only after an API test. Yelp and Outscraper are excluded for lead lists on their terms.

### 8.2 New platforms to fingerprint

Add to `enrich/tech.py` and `tools/competitor_platform.py`: Akute Health (`[a-z0-9-]+\.akutehealth\.com`), Canvas Medical (after a 50-domain sample), Qualiphy (`quidget.qualiphy.me`), Moxie (`moxie-suite-embeds`, `book.joinmoxie.com`), Aesthetic Record (`client.` and `link.aestheticrecord.com`), Zenoti webstore links, Boulevard (`dashboard.boulevard.io`, `blvd.app`), Spakinect booking links, and the small white-label vendors above. Hydreight/VSDHOne (1,100+ licenses claimed) has no confirmed HTML marker; it needs 10 to 20 known brands to build a signature.

### 8.3 Agentic crawlers: where they help and where they do not

They help on three jobs the polite httpx crawler cannot do:

1. JavaScript-rendered tenant storefronts (Next.js or React shells on Bask, Rimo and many Vercel tenants). Fallback fetcher: Crawl4AI, free and self-hosted, invoked only when httpx returns a shell. It needs about 4 GB of RAM, so it runs on a stage-2 machine, not on a 1 GB box.
2. Competitor customer and case-study pages (Wheel, OpenLoop, MD Integrations, Bask, SteadyMD, Fuse `portals.json`, Healthie directory). A `CustomerPageDiscovery` source with Browser Use Cloud ($0.006 per step) for interactive pages such as Strive Pharmacy's provider map and Florida's out-of-state telehealth registry search.
3. A platform-scout routine: crawl vendor comparison pages, propose fingerprint candidates for a new platform, validate on at least 10 tenants and 10 non-tenants, and open a pull request. Human-reviewed, never auto-merged.

They do not help as a discovery engine on their own, and they are not used against Meta or TikTok ad libraries (Meta's developer terms prohibit non-API programmatic access). Firecrawl ($99 per 100,000 pages) is a stage-2 option once a measured backlog of JS shells justifies it.

### 8.4 Market size

Roughly 2,500 to 5,000 active US DTC prescription telehealth storefronts, 10,500 to 19,000 med spas, 2,300 to 5,000 TRT clinics, 3,172 DPC practices. Stage 2 consumes 16,500 new leads per month and would exhaust the core DTC niche within months. Stage 2 therefore requires the med-spa and clinic segments to be on, accepts lower reply rates there, and uses 90-day re-enrollment cycles. Treat 3,000/day as burst capacity; the likely steady state is 1,000 to 1,500/day.

---

## 9. Meta ads in union with email

Meta's job is (a) a second lead source into the same enrich, score and route path, and (b) cheap retargeting of clinics that already engaged with email, with CRM outcomes fed back. It is phase 3 (weeks 5 to 8) because its audiences need email engagement and pixel traffic to exist first.

### 9.1 Setup

- One Meta app with the Marketing API product, a Business Manager system-user token, app in Live mode (webhooks need it). The Limited access tier is automatic and sufficient for one ad account: 300 + 40 x active ads `ads_management` calls per hour.
- One-time UI steps: accept Custom Audience Terms on the ad account, accept Lead Ads terms on the Page, check Events Manager data-source categories for a Meta-assigned health and wellness label (request B2B software review if present; Core Setup would strip URL parameters).
- App Review: Meta's Lead Ads guide states that retrieving lead data requires App Review for `leads_retrieval` and `pages_manage_ads` plus Business Verification, and that leads cannot be retrieved in Development mode except those submitted by app role holders. Other Meta pages suggest a Business-type app with Standard Access can read its own Page's leads in Live mode. Treat App Review and Business Verification as likely required and budget 1 to 2 weeks for them; verify empirically with a role-user token on day 1 of phase 3.

### 9.2 Audiences

| Audience | Built from | Use | Match expectation |
|---|---|---|---|
| Clinic owners, outbound list | All `contacts` with hashed EMAIL, PHONE, FN, LN, CT, ST, ZIP, COUNTRY | Suggestion input for Advantage+ only | Low: 70% generic mailboxes, 67% landlines |
| Engaged: replied or clicked | `reply_classifications`, `email_drafts.clicked_at`, `contacts.last_replied_at` | Warm retargeting | The audience that works |
| Booked or customer | `meetings`, `deals` | Exclusion only | n/a |
| Website visitors, email-click subdomain | Pixel rule on a dedicated subdomain (go.<brand>) that all outreach links use | Warm retargeting; immune to Core Setup URL stripping | Grows with every click |
| Lead-form opened or dropped off | Instant Form engagement, 90-day max | Recover drop-offs | n/a |

Uploads are SHA-256 hashed, at most 10,000 per call, synced daily from the Action. Suppressed and unsubscribed contacts are removed from every audience in the same job that updates `suppressions`. Names are B2B only (no condition words; Meta flags condition-named audiences with status 471 since 2 September 2025). No lookalikes until a seed has at least 100 matched US members.

### 9.3 Campaigns and lead capture

- Ad set 1, warm retargeting: engaged + email-click + site visitors, `advantage_audience=0`, exclusions booked and customers, $10 to $20/day.
- Ad set 2, prospecting: Advantage+ audience with the lists as suggestions, exclusions only, $30 to $50/day.
- Image creative first (B2B Facebook CPL $136 image versus $225 video). Copy is B2B infrastructure ("launch compliant telehealth in weeks"), never treatment outcomes.
- Instant Form via `POST /{PAGE_ID}/leadgen_forms` with `is_optimized_for_quality=true`: FULL_NAME, EMAIL, PHONE, CUSTOM "Which platform do you run on?" mirroring `competitor_platform`, CUSTOM "Monthly patient volume", DATE_TIME demo slot. Business questions only.
- Edge Function `meta-leadgen`: answer `hub.challenge`, verify `X-Hub-Signature-256`, insert `webhook_events(provider='meta', idempotency_key=leadgen_id)`, return 200, fetch `GET /{leadgen_id}?fields=created_time,id,ad_id,form_id,field_data` (the webhook payload has no answers), upsert `leads(source='meta_ads')` and `contacts`, open a task with a 30-minute SLA. Nightly reconciliation from `GET /{form_id}/leads` with a `time_created` filter.
- Meta leads then get the same enrichment, score and routing, and after 48 hours without a booking a 2-step consent-based follow-up sequence.

### 9.4 Attribution and feedback

Attribution lives in Supabase, not in Meta. `contacts` and `meetings` carry first_touch_source, last_touch_source, utm fields, fbclid, fbc, fbp, meta_lead_id and the email step. Ads use `url_tags` macros `{{campaign.name}}`, `{{ad.name}}`, `{{adset.id}}`. The portal reports meetings by first touch, last touch and assisted (both channels within 30 days).

Conversions API from Supabase: website events deduplicated with the pixel by shared `event_id` (ViewContent on the booking page, Lead on form submit, Schedule on slot confirmed) and CRM stage events `lead_received`, `replied_interested`, `meeting_booked`, `demo_held`, `closed_won` with `action_source='system_generated'`, `custom_data.event_source='crm'`, `user_data.lead_id` for Instant Form leads or hashed email and phone plus fbc for website leads, within 7 days. Run the Leads goal until about 200 leads/month and 60% coverage, then switch to Conversion Leads optimizing for `meeting_booked`.

### 9.5 Budget expectation

$1,500/mo test budget. B2B Facebook CPL benchmark $145 (median $179, retargeting $120). Expect 8 to 15 Instant Form leads and, as an inference, 2 to 5 booked demos at $300 to $800 each in months 1 to 3. Compare against email's cost per held demo in the same portal view and scale the cheaper channel. Pause any ad set that spends $600 without a booked meeting.

---

## 10. Unit economics

### 10.1 Infrastructure cost per month

| Line | Stage 1: 300/day | Stage 2: 3,000/day | Source (checked 2026-11-27) |
|---|---|---|---|
| Smartlead plan | Pro $94 ($78 annual) | Unlimited Smart $174 (unlimited contacts) | https://www.smartlead.ai/pricing |
| Mailboxes and domains via SmartSenders | 18 pre-warmed Google x $9 + 6 domains x $18/yr = $171 | 100 fresh Google x $4.50 + 34 domains x $13/yr = $487 | https://helpcenter.smartlead.ai/en/articles/266-smartsenders-pricing |
| Mailforge SMTP pool (stage 2) | $0 | 60 slots x $3 + 20 domains x $14/yr = $203 | https://www.mailforge.ai/pricing |
| Hunter | Growth $149 (10,000 credits; $104 annual) | Scale $299 (25,000 credits; $209 annual) | https://hunter.io/pricing |
| Cal.com Teams, 3 seats | $36 (annual billing, $432/yr; $48 monthly) | $36 | https://cal.com/pricing |
| Anthropic Claude API | about $24 (1,650 Sonnet 5.5 openers at $0.0112, 132 Haiku 5.5 classifications, weekly Opus 5.5) | about $100 with the Batch API (16,500 openers at $0.0056; about $192 realtime) | https://platform.claude.com/docs/en/about-claude/pricing |
| Supabase Pro (via Lovable Cloud; billing equivalence unverified) | $25 | $40 (Small compute) | https://supabase.com/pricing |
| GitHub Actions | $5 overage allowance (daily 45 to 60 min run uses 1,350 to 1,800 of about 2,000 included minutes) | $10 | https://docs.github.com/en/billing/managing-billing-for-your-products/about-billing-for-github-actions |
| Fly.io worker (only if stage-2 triggers fire) | $0 | $7 (shared-cpu-1x 1 GB $6.70) | https://docs.fly.io/about/pricing |
| PublicWWW Pro | $109 | $109 | https://publicwww.com/prices.html |
| SerpApi | $0 | $75 Developer ($25 if a Starter tier exists; conflicting vendor pages) | https://serpapi.com/pricing |
| Meta Marketing API, Slack webhooks | $0 | $0 | https://developers.facebook.com/docs/marketing-api/overview/rate-limiting |
| Total infrastructure | $613 | $1,540 | |

Excluded: Meta ad spend ($1,500/mo test), human time (modelled below at $150 per held demo), one-off Exa or Parallel runs ($80 to $300 per quarter), Apollo (optional, $49/seat/mo, API unverified).

### 10.2 Volume assumptions

22 sending days per month. 4 steps per lead. 300 sends/day = 6,600 sends = 1,650 new leads per month. 3,000 sends/day = 66,000 sends = 16,500 new leads per month. Stage 2 presupposes about 10x today's lead supply and the med-spa and clinic segments.

### 10.3 Funnel assumptions (per enrolled Tier A lead)

| Case | reply per lead | positive share | positive to booked | show rate | lead to held | Basis |
|---|---|---|---|---|---|---|
| Pessimistic | 2% | 30% | 40% | 60% | 0.14% | Belkins agency data (7.5M emails): 0.45% per email, founders 0.57%; SDR cold-booked show 60% |
| Base | 8% | 45% | 55% | 70% | 1.39% | Hunter 3-message sequences 6.8%, Woodpecker 3 to 5 follow-ups 8.3%; positive share 40 to 60% (Cleverly); show between SDR-booked 60% and self-booked 93.5% |
| Optimistic | 15% | 55% | 65% | 80% | 4.29% | Saleshandy under-200-prospect campaigns 15 to 20%; self-booked no-show 6.5% (RevenueHero) |

Label the base case honestly: it sits closer to vendor-platform data than to agency-scale data. The one large agency dataset implies about 2% per 4-step lead, which is our pessimistic case. Tier B (role mailboxes) will pull toward the pessimistic case. Replace these parameters with observed values after the first 500 replies and 30 meetings.

### 10.4 Output and cost per meeting

| Stage | Case | Leads per month | Replies | Positive | Booked | Held | Infra per booked | Infra per held | Per held incl. $150 founder time | Break-even close rate vs $16,964 |
|---|---|---|---|---|---|---|---|---|---|---|
| 300/day ($613) | Pessimistic | 1,650 | 33 | 10 | 4.0 | 2.4 | $155 | $258 | $408 | 2.4% |
| 300/day ($613) | Base | 1,650 | 132 | 59 | 32.7 | 22.9 | $19 | $27 | $177 | 1.0% |
| 300/day ($613) | Optimistic | 1,650 | 248 | 136 | 88.5 | 70.8 | $7 | $9 | $159 | 0.9% |
| 3,000/day ($1,540) | Pessimistic | 16,500 | 330 | 99 | 39.6 | 23.8 | $39 | $65 | $215 | 1.3% |
| 3,000/day ($1,540) | Base | 16,500 | 1,320 | 594 | 326.7 | 228.7 | $5 | $7 | $157 | 0.9% |
| 3,000/day ($1,540) | Optimistic | 16,500 | 2,475 | 1,361 | 884.8 | 707.9 | $2 | $2 | $152 | 0.9% |

Cross-check on booked meetings per 1,000 emails: the base case gives 5.0, between agency reality (Belkins about 0.16) and vendor top quartile (Saleshandy 20 to 30).

The stage-2 base case implies about 229 held demos per month for three people, roughly 10 per business day. That is a capacity problem before it is a cost problem. Decide on an appointment setter or a fourth closer before passing about 1,000 sends per day.

### 10.5 Value per meeting

MyOrbit public pricing (https://www.myorbithealth.com/pricing, checked 2026-11-27):

| Tier | Monthly | Onboarding | Year-1 contract value | LTV revenue at 4% monthly churn (25 months) | LTV at an assumed 70% gross margin |
|---|---|---|---|---|---|
| Satellite | $997 | $5,000 | $16,964 | $29,925 | $20,948 |
| Orbit | $1,997 | $10,000 | $33,964 | $59,925 | $41,948 |
| Constellation | $2,997 | $20,000 | $55,964 | $94,925 | $66,448 |

Churn of 3% to 5% per month is the SMB SaaS benchmark (Optifai, 939 companies). Demo-to-close benchmark: 25% B2B, 32% SMB. At a 25% close rate a held Satellite demo is worth about $4,241 of year-1 revenue. Anything under about $1,000 per held demo (Meta retargeting, pre-warmed inboxes, an appointment setter) is positive expected value.

### 10.6 Assumptions called out

- Hunter coverage of 30% to 45% is a guess until day 3.
- Reply rates for role mailboxes are unknown; assume one third of Tier A until measured.
- 70% gross margin and 4% churn are placeholders for MyOrbit's own cohort data.
- Founder time of $150 per held demo assumes about 1.5 hours including prep and follow-up.
- Pre-warmed mailboxes at 20/day; the vendor does not publish a cap, only guidance.
- SerpApi tier pricing conflicts between pages.
- Lovable Cloud billing may differ from Supabase Pro's $25.

---

## 11. Rollout plan

### Week 1: accounts, schema, plumbing, enrichment. No sends.

Day 1

- Check Lovable Cloud on project zrrndxdnopvgspyhrsxh: pg_cron, pg_net, Vault, Edge Function deploy from the CLI, service-role key. Decide the fallback (section 4.5) if anything is missing.
- Open Smartlead Pro. Confirm in-app that API and webhooks are enabled. Email support@smartlead.ai to enable SmartSenders marketplace API access. Order 18 pre-warmed Zapmail Google mailboxes on 6 .com brand-variant domains as soon as access is granted (3 per domain).
- Open Hunter Growth, Cal.com Teams (3 seats, annual), Anthropic API key. Confirm PublicWWW Pro and re-authorize its token; rotate the old token.
- Add repo secrets: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, HUNTER_API_KEY, ANTHROPIC_API_KEY, SMARTLEAD_API_KEY, PUBLICWWW_API_KEY, CALCOM_API_KEY, SLACK_WEBHOOK_URL. Add a GitHub payment method with a $20 spending limit.
- Collect from the team: legal name and postal address for footers, the six domain names, the three reps' Google Calendar accounts, routing rule confirmation.

Day 2

- Apply `supabase/migrations/0003_outreach.sql` (section 12). Seed `rep_rules`, `variants` (3 step-1 arms plus control per segment), `scoring_weights` (defaults equal to today's constants), `app_settings` (daily cap, footer, send window), `sending_domains` and `mailboxes`.
- Repo pull request 1: `discover --limit` counts new domains only (with the 200-known/1-new regression test); `enrich --retry-failed` with `crawl_attempts` and `next_crawl_at`; WAL and `timeout=30` in `LeadStore`; call `Hunter.verify()` on guessed emails; apply `email_pattern` to every person; `LeadSource += meta_ads, inbound`; `LeadStatus += enrolled, replied, unsubscribed`; `PLATFORM_ALIASES`; re-key crt.sh tenants on redirect and drop the -15 penalty for `crtsh:` sources; workflow goes daily with a concurrency group, uploads `leads.db` as an artifact, drops `|| true`, writes a step summary.

Day 3

- Run the Hunter pass over all 1,471 portal leads (Domain Search, Email Finder for owner names, Verifier). Record Tier A coverage. Decision point: above 30% proceed as planned; below 30% start Tier B at 40% of volume instead of 20% and move discovery expansion forward a week.
- Re-run crt.sh for Fuse, Telegra MD, Beluga, Akute Health, Cuvo, CareValidate, Canvas Medical, OpenLoop. Ingest the FDA warning-letter seed file. Enrich with `--workers 16`. Sync to the portal.

Day 4

- Create Smartlead campaigns via API: `glp1_competitor`, `trt_peptides`, `pharmacy_telehealth`, `med_spa_clinic_gym`, `role_mailbox`. Placeholder templates `{{stepN_subject}}` and `{{stepN_body}}`, Day 0/3/7/14 (Tier B: Day 0/5), stop on reply, footer with postal address and unsubscribe sentence, `ignore_global_block_list=false`, `ignore_unsubscribe_list=false`, `ignore_duplicate_leads_in_other_campaign=false`.
- Deploy Edge Function `smartlead-webhook`. Register Smartlead webhooks for EMAIL_SENT, EMAIL_OPEN, EMAIL_LINK_CLICK, EMAIL_REPLY, EMAIL_BOUNCE, LEAD_UNSUBSCRIBED, LEAD_CATEGORY_UPDATED, EMAIL_ACCOUNT_DISCONNECTED. Fire Smartlead's "Send Test" and fix the reply-body parser against the live payload shape. Fetch category IDs and store the mapping.

Day 5

- Cal.com: create the team, three round-robin event types with host weights and `isRRWeightsEnabled: true`, per-rep links, one webhook per event type to the deployed `calcom-webhook` Edge Function. Make a dummy booking and confirm the `meetings` row, then cancel it.
- Deploy the pg_cron jobs. Confirm the 10-minute webhook retry works by inserting a fake unprocessed event.
- Portal views (a Lovable prompt, no repo code): Replies inbox with draft approval, My tasks with SLA timers, Meetings with outcome buttons, Outreach funnel by variant and segment, Deliverability by domain, Suppressions.
- Go/no-go checklist: DNS preflight passes on all 6 domains, footer linter passes, suppression check wired, classification round-trip tested, booking round-trip tested.

### Week 2: first sends and the first meeting

Day 6

- `leadgen outreach enroll` goes live in the daily Action. Gate: enriched, score >= 50, contact email status in (verified, provider, on_site), `has_mx` not false, portal status in (new, reviewed), not suppressed, at most 2 contacts per company, one active campaign per domain. Tier A only today. Sonnet 5.5 openers rendered realtime (75 leads take under 5 minutes).
- Mailbox cap 5. About 90 sends.

Day 7

- Review bounces per mailbox. First replies classified end to end. Reps approve the first drafts in the portal; approved replies go out through Smartlead's reply-from-inbox endpoint.

Day 8

- Cap 10 if bounce <= 1% and complaints < 0.1%. About 180 sends. Tier B campaign starts at 20% of volume.

Day 9

- Cap 15. About 270 sends. Check SLA timers; confirm Slack notifications reach the right rep.

Day 10

- Cap 20. Capacity 360, target 300. First weekly routine (section 7.4). Check Actions minutes used. Confirm every LEAD_UNSUBSCRIBED has a suppression row. First demo booked is expected in week 2 or 3 at the base case (about 33 booked per month at steady state).

### Weeks 3 to 6: learning loop and lead supply

- `outreach pull-stats` copies Smartlead per-variant counts nightly. Retire and promote rules go live. First Opus proposal run in week 5. Equal allocation until each arm has 100 to 200 sends.
- `scoring_weights` feedback term on from week 6 with the 0.5 to 2.0 clamp.
- Discovery expansion: multi-marker PublicWWW signatures, med-spa embeds, LegitScript on runners, `also_found_via` corroboration, `leadgen ingest` for CSV seeds. Target 3,000+ portal leads by end of week 6.
- Order the second mailbox cohort (30 fresh Zapmail Google on 10 domains) in week 3 so it is warm by week 6. Inventory gate applies before it sends.
- Stage-2 readiness check: Edge Function error rate, Postgres connections, Action runtime.

### Weeks 5 to 8: Meta in union with email

- Meta app, system user, Live mode, the three UI acceptances, App Review and Business Verification submission if the role-user test fails.
- Dedicated outreach-link subdomain with the pixel. Edge Function `meta-leadgen`. Daily audience sync. Instant Form plus the two ad sets. Conversions API events. Portal attribution view.

### Weeks 7 to 14: ramp toward 3,000/day, gated

- Cohorts of about 30 mailboxes every 2 to 3 weeks, split between Zapmail Google and Mailforge SMTP. Smartlead to Unlimited Smart when stored contacts approach 30,000. Hunter to Scale.
- Thompson sampling across arms. Per-mailbox cap toward 25 on Google only after 4 clean weeks.
- Lead supply at 16,500 new leads per month: SerpApi sweeps, quarterly Exa or Parallel run, med-spa and clinic segments on, Google Places (place_id only), Crawl4AI fallback fetcher on a 4 GB machine.
- If a stage-2 trigger fires: enable Supabase Queues (pgmq) and one Fly.io machine running the same Docker image; move classification to a queue consumer; Small compute for Postgres.
- Monthly unit-economics review in the portal.

---

## 12. Data-model changes (SQL sketches)

Additive and re-runnable. Applied with the service role. Column names on `tasks` and `profiles` (`assignee_id`, `completed_at`, `is_rep`, `routing_weight`) are not in this repo's migrations and must be checked against the live portal before this is applied. `public.is_approved(uuid)` matches the signature used in `0001_leads.sql`.

```sql
-- supabase/migrations/0003_outreach.sql

-- 1. Disposition enum shared by the classifier and the portal
do $$ begin
  if not exists (select 1 from pg_type where typname = 'disposition') then
    create type public.disposition as enum (
      'interested','meeting_requested','meeting_booked','question_objection','maybe_later',
      'not_interested','wrong_person_referral','out_of_office','auto_reply','unsubscribe','bounce','unclear');
  end if;
end $$;

-- 2. leads: outreach state (pipeline still never writes status, notes or owner_id)
alter table public.leads
  add column if not exists segment               text,
  add column if not exists disposition           public.disposition,
  add column if not exists enrolled_at           timestamptz,
  add column if not exists last_outreach_at      timestamptz,
  add column if not exists next_eligible_at      timestamptz,   -- 90-day cooldown, snoozes
  add column if not exists suppressed_at         timestamptz,
  add column if not exists suppression_reason    text,
  add column if not exists smartlead_campaign_id bigint,
  add column if not exists crawl_attempts        integer not null default 0,
  add column if not exists next_crawl_at         timestamptz,   -- 1d, 3d, 7d backoff, then park
  add column if not exists also_found_via        jsonb not null default '[]'::jsonb,
  add column if not exists predicted_meeting_p   numeric(6,5),
  add column if not exists first_touch_source    text,          -- email | meta_ads | inbound
  add column if not exists meta_lead_id          text;
-- status values now also: enrolled | replied | unsubscribed (text column, no constraint)
-- source values now also: meta_ads | inbound | fda_warning_letter | ads_transparency
create index if not exists leads_enroll_gate_idx on public.leads (score desc)
  where suppressed_at is null and enrolled_at is null and status in ('new','reviewed');
create unique index if not exists leads_meta_lead_id_uq on public.leads (meta_lead_id) where meta_lead_id is not null;

-- 3. contacts: one row per sendable person
alter table public.contacts
  add column if not exists lead_domain        text references public.leads(domain) on update cascade on delete set null,
  add column if not exists email_status       text,            -- verified | provider | on_site | guessed | role | referred
  add column if not exists email_tier         text,            -- A | B
  add column if not exists is_decision_maker  boolean,
  add column if not exists linkedin           text,
  add column if not exists cid                text not null default encode(gen_random_bytes(8), 'hex'),
  add column if not exists smartlead_lead_id  bigint,
  add column if not exists routed_to          uuid references public.profiles(id),
  add column if not exists snooze_until       date,
  add column if not exists unsubscribed_at    timestamptz,
  add column if not exists bounced_at         timestamptz,
  add column if not exists utm                jsonb not null default '{}'::jsonb,  -- first/last touch, utm_*, fbclid, fbc, fbp
  add column if not exists meta_lead_id       text;
create unique index if not exists contacts_email_lower_uq on public.contacts (lower(email)) where email is not null;
create unique index if not exists contacts_cid_uq on public.contacts (cid);
create index if not exists contacts_lead_domain_idx on public.contacts (lead_domain);

-- 4. Suppressions, checked before every enrollment and every Meta audience add
create table if not exists public.suppressions (
  id          bigserial primary key,
  email       text,
  domain      text,
  reason      text not null check (reason in ('unsubscribe','bounce','complaint','not_interested','customer','competitor','manual')),
  source      text not null,          -- smartlead:LEAD_UNSUBSCRIBED | classifier | portal | promote_lead
  expires_at  timestamptz,            -- null = permanent
  created_by  uuid references public.profiles(id),
  created_at  timestamptz not null default now(),
  check (email is not null or domain is not null)
);
create unique index if not exists suppressions_email_uq  on public.suppressions (lower(email))  where email is not null;
create unique index if not exists suppressions_domain_uq on public.suppressions (lower(domain)) where email is null and domain is not null;
alter table public.suppressions enable row level security;
create policy "suppressions read"  on public.suppressions for select to authenticated using (public.is_approved(auth.uid()));
create policy "suppressions write" on public.suppressions for insert to authenticated with check (public.is_approved(auth.uid()));

create or replace function public.is_suppressed(_email text, _domain text) returns boolean
language sql stable as $$
  select exists (
    select 1 from public.suppressions s
    where (lower(s.email) = lower(_email) or (s.email is null and lower(s.domain) = lower(_domain)))
      and (s.expires_at is null or s.expires_at > now()));
$$;

-- 5. Sending infrastructure as data (caps and ramps are pushed to Smartlead, never trusted to the vendor)
create table if not exists public.sending_domains (
  domain        text primary key,
  vendor        text not null check (vendor in ('zapmail_google','mailforge','other')),
  bought_at     date,
  redirect_to   text,
  spf_ok        boolean, dkim_ok boolean, dmarc_policy text, dmarc_aligned boolean, dns_checked_at timestamptz,
  postmaster_spam_rate numeric,
  status        text not null default 'warming' check (status in ('warming','active','throttled','paused','retired')),
  paused_until  timestamptz
);
create table if not exists public.mailboxes (
  id                   uuid primary key default gen_random_uuid(),
  email                text unique not null,
  sending_domain       text not null references public.sending_domains(domain),
  smartlead_account_id bigint,
  pre_warmed           boolean not null default false,
  sending_day          integer not null default 0,           -- days since first cold send
  daily_cap            integer not null default 5,
  target_cap           integer not null default 20,
  bounce_7d            numeric, complaint_7d numeric, placement_pct numeric,
  status               text not null default 'warming' check (status in ('warming','active','throttled','paused','disconnected')),
  updated_at           timestamptz not null default now()
);

-- 6. Variants (arms) and their posteriors
create table if not exists public.variants (
  id                    uuid primary key default gen_random_uuid(),
  segment               text not null,
  slot                  text not null check (slot in ('step1_subject','step1_body','step2_body','step3_body','step4_body','meta_primary_text')),
  name                  text not null,
  content               text not null,
  hypothesis            text,
  parent_id             uuid references public.variants(id),
  status                text not null default 'active' check (status in ('active','control','retired','proposed')),
  traffic_floor         numeric not null default 0,            -- 0.10 for control arms
  prompt_version        text,
  sends integer not null default 0, bounces integer not null default 0, replies integer not null default 0,
  positive integer not null default 0, booked integer not null default 0, held integer not null default 0, unsubs integer not null default 0,
  alpha numeric not null default 1, beta numeric not null default 1,   -- Beta posterior on positive reply
  created_at timestamptz not null default now(), retired_at timestamptz
);

-- 7. email_drafts: one row per touch
alter table public.email_drafts
  add column if not exists lead_domain          text references public.leads(domain) on update cascade,
  add column if not exists contact_id           uuid references public.contacts(id),
  add column if not exists subject_variant_id   uuid references public.variants(id),
  add column if not exists body_variant_id      uuid references public.variants(id),
  add column if not exists provider             text not null default 'smartlead',
  add column if not exists provider_message_id  text,            -- gmail_message_id stays for legacy rows
  add column if not exists smartlead_campaign_id bigint,
  add column if not exists idempotency_key      text,            -- generated and stored before any vendor call
  add column if not exists opened_at            timestamptz,
  add column if not exists clicked_at           timestamptz,
  add column if not exists bounced_at           timestamptz;
-- status lifecycle: draft -> pending_approval -> approved -> queued -> sent -> delivered -> opened -> replied | bounced | unsubscribed
create unique index if not exists email_drafts_touch_uq on public.email_drafts (contact_id, smartlead_campaign_id, sequence_step)
  where contact_id is not null and smartlead_campaign_id is not null;
create unique index if not exists email_drafts_provider_msg_uq on public.email_drafts (provider, provider_message_id)
  where provider_message_id is not null;

-- 8. webhook_events: idempotent landing zone for Smartlead, Cal.com and Meta
alter table public.webhook_events
  add column if not exists received_at  timestamptz not null default now(),
  add column if not exists processed_at timestamptz,
  add column if not exists attempts     smallint not null default 0,
  add column if not exists error        text;
create unique index if not exists webhook_events_idem_uq on public.webhook_events (provider, idempotency_key);
create index if not exists webhook_events_unprocessed_idx on public.webhook_events (received_at) where processed_at is null;
-- Stage-2 ramp, disabled until pgmq is enabled:
-- create trigger webhook_events_enqueue after insert on public.webhook_events
--   for each row execute function public.webhook_events_enqueue();

-- 9. Reply classifications (feedback dataset; raw text redacted when contains_phi)
create table if not exists public.reply_classifications (
  id               uuid primary key default gen_random_uuid(),
  webhook_event_id uuid references public.webhook_events(id),    -- verify webhook_events.id type in the live portal
  lead_domain      text references public.leads(domain) on update cascade,
  contact_id       uuid references public.contacts(id),
  email_draft_id   uuid references public.email_drafts(id),
  disposition      public.disposition not null,
  confidence       text not null check (confidence in ('low','medium','high')),
  objection        text,
  summary          text,
  snooze_until     date,
  referral         jsonb,
  draft_reply      text,
  raw_reply        text,
  contains_phi     boolean not null default false,
  compliance_flag  boolean not null default false,
  model            text not null,                  -- claude-haiku-5-5 | claude-sonnet-5-5
  prompt_version   text not null,
  routed_to        uuid references public.profiles(id),
  human_override   public.disposition,
  overridden_by    uuid references public.profiles(id),
  overridden_at    timestamptz,
  created_at       timestamptz not null default now()
);
create index if not exists reply_classifications_lead_idx on public.reply_classifications (lead_domain, created_at desc);

-- 10. Routing rules, assignment function, routing log
alter table public.profiles add column if not exists routing_weight numeric not null default 1.0;  -- verify column set in live portal
create table if not exists public.rep_rules (
  id        serial primary key,
  priority  integer not null,
  name      text not null,
  predicate jsonb not null,   -- all keys must match: {"dispositions":[...],"min_score":80,"legitscript":[...],"verticals":[...],"sources":[...],"requires_platform":true}
  rep_id    uuid references public.profiles(id),               -- null = load balanced across is_rep profiles
  active    boolean not null default true
);
create table if not exists public.routing_log (
  id          bigserial primary key,
  lead_domain text, contact_id uuid, disposition public.disposition,
  rep_id      uuid references public.profiles(id), rule_id integer references public.rep_rules(id),
  reason      text, created_at timestamptz not null default now()
);
create or replace function public.assign_rep(_lead_domain text, _disposition public.disposition)
returns uuid language plpgsql security definer set search_path = public as $$
declare l public.leads%rowtype; r record; rep uuid; rule integer; why text;
begin
  perform pg_advisory_xact_lock(hashtext('assign_rep'));
  select * into l from public.leads where domain = _lead_domain;
  -- rule 0: existing relationship wins
  rep := coalesce(l.owner_id,
           (select d.owner_id from public.deals d where d.id = l.deal_id),                        -- verify deals.owner_id exists
           (select m.claimed_by from public.meetings m where m.lead_domain = _lead_domain order by m.starts_at desc limit 1));
  if rep is not null then why := 'existing_relationship'; end if;
  if rep is null then
    for r in select * from public.rep_rules where active order by priority loop
      if (r.predicate->'dispositions' is null or r.predicate->'dispositions' ? _disposition::text)
         and (r.predicate->>'min_score' is null or l.score >= (r.predicate->>'min_score')::int)
         and (r.predicate->'legitscript' is null or r.predicate->'legitscript' ? l.legitscript_status)
         and (r.predicate->'verticals' is null or r.predicate->'verticals' ? coalesce(l.vertical,''))
         and (r.predicate->'sources' is null or r.predicate->'sources' ? l.source)
         and (coalesce((r.predicate->>'requires_platform')::boolean, false) = false or l.competitor_platform is not null)
      then
        rule := r.id; why := r.name;
        if r.rep_id is not null then rep := r.rep_id;
        else
          select p.id into rep from public.profiles p
            left join public.tasks t on t.assignee_id = p.id and t.completed_at is null            -- verify tasks columns
            left join public.meetings m on m.claimed_by = p.id and m.starts_at > now() - interval '7 days'
           where p.is_rep                                                                           -- verify profiles.is_rep
           group by p.id, p.routing_weight
           order by (count(distinct t.id) + count(distinct m.id)) / p.routing_weight asc, random() limit 1;
        end if;
        exit;
      end if;
    end loop;
  end if;
  insert into public.routing_log(lead_domain, disposition, rep_id, rule_id, reason) values (_lead_domain, _disposition, rep, rule, why);
  return rep;
end $$;
grant execute on function public.assign_rep(text, public.disposition) to service_role, authenticated;

-- 11. meetings: attribution and outcome plumbing (Cal.com webhook writes these)
alter table public.meetings
  add column if not exists lead_domain          text references public.leads(domain) on update cascade,
  add column if not exists contact_id           uuid references public.contacts(id),
  add column if not exists variant_id           uuid references public.variants(id),
  add column if not exists source_channel       text,          -- email | meta_ads | inbound | referral
  add column if not exists calcom_booking_uid   text,
  add column if not exists calcom_event_type_id bigint,
  add column if not exists attribution          jsonb not null default '{}'::jsonb,  -- first_touch, last_touch, assisted, utm, variant ids
  add column if not exists no_show              boolean,
  add column if not exists ended_at             timestamptz;
-- outcome values: scheduled | held | no_show | rescheduled | cancelled | qualified | closed_won | lost
create unique index if not exists meetings_calcom_uid_uq on public.meetings (calcom_booking_uid) where calcom_booking_uid is not null;

-- 12. Scoring weights read by `leadgen score`; defaults equal to the constants in scoring.py
create table if not exists public.scoring_weights (
  feature      text primary key,            -- 'seal_detected' | 'vertical:weight_loss_glp1' | 'platform:Healthie' | 'email_tier:A'
  base_weight  numeric not null,
  multiplier   numeric not null default 1.0 check (multiplier between 0.5 and 2.0),
  positive_rate numeric, held_rate numeric, n integer,
  source       text not null default 'default' check (source in ('default','human','feedback')),
  updated_at   timestamptz not null default now()
);

-- 13. Re-enrollment queue drained by the daily Action; alerts table for deduplicated operational alerts
create table if not exists public.reenroll_queue (
  id bigserial primary key, contact_id uuid not null references public.contacts(id), reason text not null,
  due_at timestamptz not null, taken_at timestamptz
);
create table if not exists public.alerts (
  key text not null, day date not null default current_date, body text, created_at timestamptz not null default now(),
  primary key (key, day)
);

-- 14. Costs and job runs for the unit-economics view
create table if not exists public.monthly_costs (
  month date not null, channel text not null check (channel in ('email','meta','discovery','shared')),
  line_item text not null, amount_usd numeric(10,2) not null, source_url text, primary key (month, channel, line_item)
);
create table if not exists public.job_runs (
  id bigserial primary key, job text not null, started_at timestamptz not null default now(), finished_at timestamptz,
  status text check (status in ('ok','error')), error text, cost_usd numeric(10,6), tokens_in integer, tokens_out integer, meta jsonb
);

-- 15. Funnel view and cost view
create materialized view if not exists public.outreach_funnel as
select l.segment, d.body_variant_id as variant_id, d.sequence_step,
       count(*) filter (where d.sent_at is not null)                      as sent,
       count(*) filter (where d.bounced_at is not null)                   as bounced,
       count(*) filter (where d.replied_at is not null)                   as replied,
       count(distinct rc.contact_id) filter (where rc.disposition in ('interested','meeting_requested','meeting_booked')) as positive,
       count(distinct m.id)                                               as booked,
       count(distinct m.id) filter (where m.outcome in ('held','qualified','closed_won')) as held
from public.email_drafts d
join public.leads l on l.domain = d.lead_domain
left join public.reply_classifications rc on rc.email_draft_id = d.id
left join public.meetings m on m.contact_id = d.contact_id and m.starts_at > d.sent_at
group by 1,2,3;
create unique index if not exists outreach_funnel_uq on public.outreach_funnel (segment, variant_id, sequence_step);

create or replace view public.cost_per_meeting as
select date_trunc('month', m.starts_at)::date as month, m.source_channel,
       count(*) as booked,
       count(*) filter (where m.outcome in ('held','qualified','closed_won')) as held,
       (select sum(c.amount_usd) from public.monthly_costs c
         where c.month = date_trunc('month', m.starts_at)::date and c.channel in (m.source_channel, 'shared')) as cost_usd
from public.meetings m group by 1, 2;

-- 16. pg_cron jobs (SQL only, except the webhook retry which uses pg_net with a dedicated retry secret from Vault)
select cron.schedule('outreach_posteriors', '0 2 * * *', $$
  refresh materialized view concurrently public.outreach_funnel;
  update public.variants v
     set alpha = 1 + f.positive, beta = 1 + greatest(f.sent - f.positive, 0),
         sends = f.sent, bounces = f.bounced, replies = f.replied, positive = f.positive, booked = f.booked, held = f.held
    from (select variant_id, sum(sent) sent, sum(bounced) bounced, sum(replied) replied, sum(positive) positive,
                 sum(booked) booked, sum(held) held
            from public.outreach_funnel group by variant_id) f
   where f.variant_id = v.id; $$);

select cron.schedule('outreach_snooze_release', '0 3 * * *', $$
  insert into public.reenroll_queue(contact_id, reason, due_at)
  select id, 'snooze_release', now() from public.contacts c
   where c.snooze_until is not null and c.snooze_until <= current_date and c.unsubscribed_at is null
     and not exists (select 1 from public.reenroll_queue q where q.contact_id = c.id and q.taken_at is null); $$);

select cron.schedule('suppressions_expire', '30 3 * * *', $$
  delete from public.suppressions where expires_at is not null and expires_at < now(); $$);

select cron.schedule('outreach_stuck_webhooks', '*/15 * * * *', $$
  insert into public.alerts(key, body)
  select 'stuck_webhooks', 'Unprocessed webhook events older than 30 minutes: ' || count(*)
    from public.webhook_events where processed_at is null and received_at < now() - interval '30 minutes'
  having count(*) > 0
  on conflict (key, day) do update set body = excluded.body; $$);

-- Webhook retry every 10 minutes. Calls the Edge Function in retry mode with a dedicated secret.
-- Caveat: pg_net leaves the header in net.http_request_queue until sent; use a retry-only secret, never the service-role key.
select cron.schedule('outreach_webhook_retry', '*/10 * * * *', $$
  select net.http_post(
    url := (select decrypted_secret from vault.decrypted_secrets where name = 'edge_functions_url') || '/smartlead-webhook?mode=retry',
    headers := jsonb_build_object('Content-Type', 'application/json',
                                  'x-retry-secret', (select decrypted_secret from vault.decrypted_secrets where name = 'webhook_retry_secret')),
    body := '{"limit": 20}'::jsonb)
  where exists (select 1 from public.webhook_events where processed_at is null and attempts < 10); $$);
```

Repo-side changes that are not SQL: `Lead.to_portal_row()` stays unchanged (never writes status, disposition or enrolled_at). New outreach writes go through `SupabaseSink.upsert_contacts()`, `insert_email_draft()`, `fetch_enrollable()`, `fetch_status()` (exists today, now called) and `write_suppression()`.

---

## 13. Risks and open questions

### 13.1 Risks and mitigations

| Risk | Mitigation |
|---|---|
| Lovable Cloud may not expose pg_cron, pg_net, Vault, CLI Edge Function deployment or the service-role key (unverified) | Day-1 check. Fallback: link the project to a direct Supabase Pro organization, or a sibling Supabase project with status synced back through an RPC. Worst case the pg_cron jobs run as Action steps. |
| Smartlead SmartSenders API access needs support enablement; pre-warmed inventory may be out of stock | Ask on day 1. Fallback: Instantly Hypergrowth ($107/mo) with its public DFY mailbox order endpoint; or accept the mailbox order as the one UI step. |
| Smartlead's EMAIL_REPLY payload field name is inconsistent across its own docs | Parser reads `reply_body`, `reply_message.html`, `reply_message.text` and `reply.body`; validated on day 4 against a live test delivery. |
| Sendable contacts are scarce: 50 direct emails today. If Hunter coverage comes in under 30%, stage 1 cannot fill 300/day with named decision makers | Measure on day 3. Tier B role mailboxes at a lower cadence. Discovery expansion moved forward. Apollo only after an API test. |
| Pre-warmed mailboxes are the only cohort ready in week 2; a bad first week burns them | Verified emails only, catch-all addresses at reduced cap, bounce auto-pause at 2%, second cohort ordered in week 3. Pre-warmed boxes expire in 9 to 10 months, so cohorts must be rotated. |
| GitHub Actions minutes: a daily 45 to 60 minute run uses 1,350 to 1,800 of about 2,000 included minutes; without a payment method runs are blocked when the allowance is exhausted | Payment method with a $20 spending limit; slim 1-core runner for I/O-bound steps; a public repository would make standard-runner minutes free but exposes the code. |
| A dropped reply classification waits for the retry | 10-minute pg_cron retry via pg_net; alert if anything is stuck 30 minutes; the daily Action sweeps as a last resort. |
| Stage 2 needs 16,500 new leads per month against a core niche of 2,500 to 5,000 DTC storefronts | Treat 3,000/day as burst capacity. Turn on med-spa and clinic segments. 90-day re-enrollment cycles. Inventory gate before every cohort. |
| Healthcare adjacency: Meta may classify the domain as health and wellness; Gmail enforcement tightened in November 2025 | B2B-only creative, audience names and form questions; dedicated outreach-link subdomain; DMARC aligned; one-click unsubscribe headers; spam rate watched in Postmaster Tools. |
| Meta App Review and Business Verification may be required to read leads | Budget 1 to 2 weeks in phase 3; test with a role-user token first. |
| CAN-SPAM liability ($53,088 per email; no B2B exception; vendor does not shift liability) | Footer and unsubscribe linter blocks enrollment; suppression check before every send; unsubscribe honored within minutes; 3-year retention. |
| Three-person demo capacity: base case 23 held demos per month at stage 1 and 229 at stage 2 | Daily enrollment cap in `app_settings`; Cal.com host weights; decide on an appointment setter before about 1,000 sends/day. |
| Classifier errors are compliance errors (a missed unsubscribe keeps sending) | Unsubscribe and bounce handled deterministically from Smartlead events in addition to the model; weekly reconciliation; Sonnet escalation on low confidence; override logging. |
| Reply bodies may contain patient details | `contains_phi` flag redacts before storage; no PHI in Meta custom_data or audiences; HIPAA business-associate status is not triggered by B2B prospecting (45 CFR 160.103). |
| One provider pool at scale is a single reputation event away from zero sends | Stage 2 splits Zapmail Google and Mailforge SMTP; cohorts; caps never above 20 on Google until proven. |
| Open-tracking noise (Apple Mail Privacy Protection, Gmail proxies) | Opens are excluded from the bandit reward. |
| Hunter Scale (25,000 credits) is undersized at stage 2 if two contacts per company are enriched | Domain Search returns up to 10 emails per credit; monitor credit burn monthly; annual Scale or a second provider if needed. |
| Secret hygiene: a PublicWWW token sits in plaintext in a scratch script | Rotate it on day 1; secrets only in GitHub, Supabase and Vault. |

### 13.2 Open questions for the team

1. Does Lovable Cloud expose pg_cron, pg_net, Vault, CLI Edge Function deploys and the service-role key on project zrrndxdnopvgspyhrsxh? If not, do you accept a direct Supabase Pro organization or a sibling project?
2. If Smartlead does not enable the SmartSenders API within 48 hours, do you accept one UI step for the mailbox order, or switch to Instantly Hypergrowth?
3. What legal name and physical postal address go in every footer?
4. Approve the six sending domain names (brand variants, .com, no hyphens or numbers).
5. Confirm or change the routing rules in section 6.6 and the Cal.com host weights (Shubh 50, Divang 100, Stacy 100). Who is the triage owner for unclear replies?
6. Is sending to monitored role mailboxes (info@, hello@, support@) acceptable as Tier B, at 20% of volume?
7. Is mental health (211 leads) inside the ICP?
8. Do you already pay for an Apollo seat? If yes, can you confirm in the account that API calls work on that plan?
9. Annual or monthly billing? Annual saves about $16/mo on Smartlead ($78 versus $94), $45/mo on Hunter ($104 versus $149), and Cal.com's $12 seat price requires annual billing ($432 up front).
10. Who owns the Meta Business Manager and Page? Is a $1,500/mo Meta test budget approved for weeks 5 to 8? Are you willing to go through App Review and Business Verification?
11. Is the repository private? If so, a GitHub payment method and spending limit are needed for Actions.
12. Do the reps use Google Meet or Cal Video for demos? Cal Video enables automatic no-show detection; Google Meet requires the rep to mark no-shows.
13. Demo capacity: how many held demos per week can each of you run before quality drops?

---

## 14. Sources appendix

All checked 2026-11-27.

Sending and mailboxes
- Smartlead pricing: https://www.smartlead.ai/pricing and https://www.smartlead.ai/pricing.md
- Smartlead API getting started and plan gating: https://helpcenter.smartlead.ai/en/articles/68-how-to-get-started-with-the-smartlead-api
- Smartlead rate limits: https://api.smartlead.ai/guides/rate-limits
- Smartlead webhook events: https://api.smartlead.ai/api-reference/webhooks/events
- Smartlead webhook core page (retry schedule, EMAIL_REPLIED shape): https://api.smartlead.ai/core/webhooks
- Smartlead test webhook sample payloads: https://helpcenter.smartlead.ai/en/articles/403-how-to-test-a-smartlead-webhook-with-sample-payloads
- Smartlead lead categories: https://api.smartlead.ai/api-reference/leads/categories
- Smartlead bounce auto-pause webhook: https://helpcenter.smartlead.ai/en/articles/210-bounce-autopause-and-webhook
- SmartSenders pricing: https://helpcenter.smartlead.ai/en/articles/266-smartsenders-pricing
- SmartSenders FAQ (Zapmail daily guidance, 5 mailboxes per domain, expiry): https://helpcenter.smartlead.ai/en/articles/256-smartsenders-faqs-and-troubleshooting-everything-you-need-to-know
- SmartSenders walkthrough: https://helpcenter.smartlead.ai/en/articles/255-smartsenders-step-by-step-walkthrough
- SmartSenders place-order API (support-enabled): https://api.smartlead.ai/api-reference/smart-senders/place-order
- Zapmail pre-warmed guide: https://help.zapmail.ai/en/articles/13358418-pre-warmed-mailboxes-in-zapmail-complete-guide
- Mailforge pricing: https://www.mailforge.ai/pricing
- Instantly pricing (fallback): https://instantly.ai/pricing.md
- Instantly DFY order API (fallback): https://developer.instantly.ai/api-reference/dfyemailaccountorder/place-a-dfy-email-account-order.md
- Instantly account limits (30/day planning constant): https://help.instantly.ai/en/articles/6248612-account-and-campaign-limits
- Instantly slow ramp plan: https://instantly.ai/blog/slow-ramp-plan-for-new-cold-email-domains-deliverability/
- lemlist domain guidance: https://www.lemlist.com/blog/cold-email-domains
- Google Workspace sending limits and the $100 throttle: https://knowledge.workspace.google.com/admin/gmail/gmail-sending-limits-in-google-workspace
- Exchange Online limits: https://learn.microsoft.com/en-us/office365/servicedescriptions/exchange-online-service-description/exchange-online-limits
- AWS Acceptable Use Policy: https://aws.amazon.com/aup/
- SES enforcement FAQ: https://docs.aws.amazon.com/ses/latest/dg/faqs-enforcement.html

Enrichment and discovery
- Hunter pricing: https://hunter.io/pricing
- Hunter Free plan includes API: https://help.hunter.io/en/articles/11060999-what-s-included-in-hunter-s-free-plan
- Hunter API docs: https://hunter.io/api-documentation/v2
- Hunter State of Cold Email: https://hunter.io/the-state-of-cold-email
- Apollo rate limits and plan ambiguity: https://docs.apollo.io/reference/rate-limits
- PublicWWW prices: https://publicwww.com/prices.html
- SerpApi Google Ads Transparency Center: https://serpapi.com/google-ads-transparency-center-api and https://serpapi.com/pricing
- Exa pricing: https://exa.ai/pricing
- Parallel pricing: https://docs.parallel.ai/resources/pricing
- Firecrawl pricing: https://www.firecrawl.dev/pricing
- Browser Use pricing: https://browser-use.com/pricing.md
- Crawl4AI: https://pypi.org/project/Crawl4AI/
- crt.sh queries: https://crt.sh/?q=%25.fusehealth.com&output=json, https://crt.sh/?q=%25.telegramd.com&output=json, https://crt.sh/?q=%25.belugahealth.com&output=json, https://crt.sh/?q=%25.canvasmedical.com&output=json
- Akute Health customers: https://www.akutehealth.com/customers
- Karpa platform comparison: https://karpahealth.com/compare/best-glp1-peptide-platforms/
- FDA warning letters coverage: https://www.pharmacytimes.com/view/fda-and-novo-nordisk-warned-of-glp-1-telehealth-compounding-take-down-what-s-next-
- Google Places policies: https://developers.google.com/maps/documentation/places/web-service/policies
- Meta Ad Library API limitations: https://developers.facebook.com/docs/graph-api/reference/ads_archive/
- BuiltWith plans: https://builtwith.com/plans
- Wappalyzer pricing: https://www.wappalyzer.com/pricing/
- Market size references: https://www.ibisworld.com/united-states/number-of-businesses/telehealth-services/5775/ and https://www.americanmedspa.org/

Booking, classification, loop
- Cal.com pricing: https://cal.com/pricing
- Cal.com create team event type (roundRobin, weights): https://cal.com/docs/api-reference/v2/teams-event-types/create-an-event-type
- Cal.com create webhook: https://cal.com/docs/api-reference/v2/webhooks/create-a-webhook
- Cal.com team event type webhooks: https://cal.com/docs/api-reference/v2/teams-event-types-webhooks/create-a-webhook-for-a-team-event-type
- Cal.com webhook signatures: https://cal.com/help/webhooks
- Anthropic pricing: https://platform.claude.com/docs/en/about-claude/pricing
- Anthropic structured outputs: https://platform.claude.com/docs/en/build-with-claude/structured-outputs
- Anthropic models overview: https://platform.claude.com/docs/en/models/overview
- Anthropic batch processing: https://platform.claude.com/docs/en/build-with-claude/batch-processing
- HubSpot owner rotation (pattern reference): https://knowledge.hubspot.com/workflows/assign-and-rotate-record-owners-using-workflows
- Slack rate limits: https://docs.slack.dev/apis/web-api/rate-limits
- A/B sample size: https://www.evanmiller.org/ab-testing/sample-size.html
- Batched Thompson sampling (Mao et al.): https://arxiv.org/abs/1908.06256
- Test and Roll (Feit and Berman): https://arxiv.org/abs/1811.00457
- LLM subject lines, generate then select (Nextdoor): https://arxiv.org/abs/2312.12457

Orchestration and platform
- Supabase pricing: https://supabase.com/pricing
- Supabase Edge Function limits: https://supabase.com/docs/guides/functions/limits
- Supabase Edge Function CPU limits: https://supabase.com/docs/guides/troubleshooting/edge-function-cpu-limits
- Supabase Cron: https://supabase.com/docs/guides/cron
- Supabase scheduling functions with pg_net and Vault: https://supabase.com/docs/guides/functions/schedule-functions
- Supabase Queues (stage 2): https://supabase.com/docs/guides/queues/api
- Supabase compute tiers: https://supabase.com/docs/guides/platform/compute-and-disk
- GitHub Actions billing: https://docs.github.com/en/billing/managing-billing-for-your-products/about-billing-for-github-actions
- GitHub Actions limits: https://docs.github.com/en/actions/reference/limits
- GitHub Actions schedule event: https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows
- GitHub Actions concurrency: https://docs.github.com/en/actions/writing-workflows/choosing-what-your-workflow-does/control-the-concurrency-of-workflows-and-jobs
- Fly.io pricing: https://docs.fly.io/about/pricing
- Celery Redis broker caveats: https://docs.celeryq.dev/en/stable/getting-started/backends-and-brokers/redis.html
- Upstash Redis pricing: https://upstash.com/pricing/redis

Meta
- Marketing API rate limiting: https://developers.facebook.com/docs/marketing-api/overview/rate-limiting
- Marketing API authorization and access tiers: https://developers.facebook.com/docs/marketing-api/overview/authorization
- Lead Ads guide (App Review and Business Verification): https://developers.facebook.com/docs/marketing-api/guides/lead-ads/
- Lead Ads retrieving: https://developers.facebook.com/docs/marketing-api/guides/lead-ads/retrieving
- Lead Ads webhooks: https://developers.facebook.com/documentation/ads-commerce/marketing-api/guides/lead-ads/quickstart/webhooks-integration.md
- Graph API webhooks: https://developers.facebook.com/docs/graph-api/webhooks/
- Custom Audiences: https://developers.facebook.com/docs/marketing-api/audiences/guides/custom-audiences
- Lookalike audiences: https://developers.facebook.com/documentation/ads-commerce/marketing-api/audiences/guides/lookalike-audiences
- Conversions API for CRM: https://developers.facebook.com/documentation/ads-commerce/conversions-api/guides/conversions-api-crm-for-platforms
- Advantage+ audience API change: https://developers.secure.facebook.com/blog/post/2025/06/13/marketing-api-changes-to-advantage-plus-audience-behaviors/
- Health and wellness ad standard: https://transparency.meta.com/policies/ad-standards/restricted-goods-services/health-wellness/
- B2B benchmarks (Metadata.io 2025): https://metadata.io/b2b-advertising-benchmarks

Compliance and benchmarks
- FTC CAN-SPAM compliance guide: https://www.ftc.gov/business-guidance/resources/can-spam-act-compliance-guide-business
- Google Email sender guidelines: https://support.google.com/mail/answer/81126?hl=en
- Google Email sender guidelines FAQ: https://support.google.com/a/answer/14229414
- Yahoo sender best practices: https://senders.yahooinc.com/best-practices/
- Microsoft high-volume sender requirements: https://techcommunity.microsoft.com/blog/microsoftdefenderforoffice365blog/strengthening-email-ecosystem-outlook%E2%80%99s-new-requirements-for-high%E2%80%90volume-senders/4399730
- Microsoft 550 5.7.515: https://support.microsoft.com/en-us/outlook/fix-ndr-error-550-5-7-515-in-outlook-com
- TCPA rules, 47 CFR 64.1200: https://www.law.cornell.edu/cfr/text/47/64.1200
- TSR B2B exemption, 16 CFR 310.6: https://www.law.cornell.edu/cfr/text/16/310.6
- HIPAA definitions, 45 CFR 160.103: https://www.law.cornell.edu/cfr/text/45/160.103
- Sendblue limits (why no cold texting): https://docs.sendblue.com/limits
- Belkins reply rates: https://belkins.io/blog/cold-email-response-rates
- Belkins follow-up statistics: https://belkins.io/blog/sales-follow-up-statistics
- Woodpecker statistics: https://woodpecker.co/blog/cold-email-statistics/
- Saleshandy statistics: https://www.saleshandy.com/blog/cold-email-statistics/
- Cleverly benchmarks: https://www.cleverly.co/blog/cold-email-benchmarks-by-industry
- RevenueHero no-show benchmark: https://revenuehero.io/reports/no-show-benchmark-december-02-2024
- Demo no-show statistics: https://www.gethyper.ai/blog/demo-no-show-statistics
- Demo-to-close benchmark: https://optif.ai/learn/questions/demo-to-close-conversion-rate/
- SaaS churn benchmark: https://optif.ai/learn/questions/b2b-saas-churn-rate-benchmark/
- MyOrbit Health pricing: https://www.myorbithealth.com/pricing
