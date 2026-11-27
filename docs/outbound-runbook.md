# Outbound runbook

Operational steps for the outbound system. The design and the reasons behind it are in
[outbound-system-plan.md](outbound-system-plan.md). Nothing here sends email until a person
starts the Smartlead campaigns.

## 1. Secrets and variables

GitHub repository secrets (Settings, Secrets and variables, Actions, Secrets):

| Secret | Used by |
| --- | --- |
| `SUPABASE_SERVICE_ROLE_KEY` | portal sync, enrolment rows, reply sweep |
| `SMARTLEAD_API_KEY` | enrol, pull-stats, reply actions |
| `SMARTLEAD_WEBHOOK_SECRET` | shared secret the webhook URL carries as `?token=` |
| `SMARTLEAD_WEBHOOK_URL` | the deployed `smartlead-webhook` Edge Function URL (a variable also works) |
| `CALCOM_API_KEY` | Cal.com v2 (event types, bookings) |
| `HUNTER_API_KEY` and/or `APOLLO_API_KEY` | decision-maker emails |
| `ANTHROPIC_API_KEY` | extraction, personalisation, reply classification, weekly learn |

GitHub repository variables (same page, Variables tab):

| Variable | Value |
| --- | --- |
| `SUPABASE_ANON_KEY` | the portal's anon key (public by design) |
| `SUPABASE_URL` | only if it differs from the default in the workflow |
| `SMARTLEAD_EMAIL_ACCOUNT_IDS` | comma-separated Smartlead mailbox ids for the campaigns |
| `LEADGEN_OUTREACH_POSTAL_ADDRESS` | CAN-SPAM postal address (or set it in the portal, see below) |
| `LEADGEN_OUTREACH_COMPANY_LEGAL_NAME`, `LEADGEN_OUTREACH_SENDER_NAME` | footer and signature |
| `LEADGEN_BOOKING_URL` | shared booking page used in first-touch emails |

Supabase Edge Function secrets (`supabase secrets set NAME=value`, or the dashboard):
`ANTHROPIC_API_KEY`, `SMARTLEAD_API_KEY`, `SMARTLEAD_WEBHOOK_SECRET`, `CALCOM_WEBHOOK_SECRET`.

Never paste a key into an issue, a commit, a workflow file or a chat. Rotate any key that was.

## 2. Before the first send

1. Apply `supabase/migrations/0003_outreach.sql` to the portal.
2. In the portal, set `app_settings` key `outreach`: `postal_address` (enrolment refuses to run
   while it still says `TO BE CONFIRMED`), and `reps` with each rep's profile id and booking link.
3. Deploy the Edge Functions and register the Smartlead and Cal.com webhooks against them.

## 3. First-run order

Run these locally (with the same env vars) or through the workflow, in this order:

```
leadgen outreach sync-campaigns --dry-run   # check the payloads
leadgen outreach sync-campaigns             # creates campaigns in DRAFTED, writes ids to the portal
leadgen outreach plan                       # sendable counts by segment, rejection reasons
leadgen outreach enroll --dry-run           # who would be enrolled, which variant
leadgen outreach enroll --daily-cap 60      # first real enrolment (about 15 contacts)
```

Then start the campaigns in Smartlead (or `sync-campaigns --start`). Starting is always a human
step. Ramp the cap over two to three weeks: 60, 120, 200, 300 sends a day, raising it only while
bounces stay under 2% and spam complaints are zero. `--daily-cap` counts sends, not contacts: a
named contact costs 4 sends, a role mailbox 2.

## 4. Schedules

| Workflow | When | What |
| --- | --- | --- |
| `leadgen.yml` | daily 06:00 UTC, and on dispatch | `leadgen daily`: discover, enrich, retry failed crawls, score, sync portal, enroll, classify, pull-stats; on Mondays also `outreach learn --propose 3` |
| `outreach-replies.yml` | every 6 hours, and on dispatch | `leadgen outreach classify --limit 500`, then `leadgen outreach pull-stats` |

Each step of `leadgen daily` is skipped when its keys are missing and isolated from the others.
The run ends with a table of ok / skipped / failed steps (also in the job summary) and exits 1 if
any step failed. The Edge Function handles replies within seconds; the 6-hourly sweep only picks up
events it could not finish.

## 5. Operating it (people and agents)

From the Actions tab, or with `gh workflow run`:

```
gh workflow run leadgen.yml -f enroll=false                 # everything except new enrolments
gh workflow run leadgen.yml -f daily_cap=120                # today's send budget
gh workflow run leadgen.yml -f tool=competitor -f discover_limit=300   # one tool only, no outreach
gh workflow run outreach-replies.yml -f limit=200
```

Useful CLI commands:

```
leadgen daily --no-enroll --no-classify   # pipeline only
leadgen ingest export.csv --source meta_ads   # or --source inbound
leadgen outreach suppress someone@example.com --reason unsubscribe
leadgen outreach report --days 7
leadgen outreach learn --dry-run
leadgen outreach classify --text "please remove me"   # test the classifier on one reply
```

An agent should prefer `workflow_dispatch` (it uses the repository's secrets and the shared
`leads.db` cache) and read the job summary table. Do not run `sync-campaigns --start` or raise
the cap without a person agreeing.

## 6. What each disposition does

A human sees every task and approves every drafted reply before it is sent.

| Disposition | Portal effect | Smartlead |
| --- | --- | --- |
| `interested` | routed to a rep, high-priority task, draft reply for approval | category Interested, lead paused |
| `meeting_requested` | as interested, draft offers the rep's booking link | category Interested, paused |
| `meeting_booked` | routed, task | category Meeting Booked, paused |
| `question_objection` | routed, task, draft answer | category Interested, paused |
| `wrong_person_referral` | routed, task naming the referral | paused |
| `maybe_later` | contact snoozed 60 days (or the date they gave); a task only when confidence is low | paused |
| `out_of_office` | contact snoozed 7 days (or their return date) | category Out of Office, paused |
| `not_interested` | lead marked suppressed, domain suppressed for 180 days | category Not Interested, unsubscribed from the campaign |
| `unsubscribe` | email and domain suppressed permanently, contact marked unsubscribed | category Do Not Contact, unsubscribed globally |
| `bounce` | email suppressed, contact marked bounced | left as is |
| `auto_reply` | recorded only | left as is |
| `unclear` | routed to a rep for a look | left as is |

A bare "stop" reply is always treated as `unsubscribe`, because the email footer offers it.
Freemail domains (gmail.com and similar) are never suppressed as a whole domain.
