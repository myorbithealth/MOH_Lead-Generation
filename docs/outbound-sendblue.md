# SendBlue texting channel (warm only)

Plan section 6.8. Texting is the warm channel and never the cold one: no text goes to anyone who has not ticked the SMS consent box on the Cal.com booking form (or whose consent a rep recorded by hand with a source).

## What it does

| Piece | Where | What |
|---|---|---|
| Client | `src/moh_leadgen/outreach/sendblue.py` | `SendblueClient.send_message`, `get_message_status`, US E.164 normalisation, retry once on 5xx, raise on 4xx with the body, per-line rate limiter |
| Consent gate | `texting.can_text(contact)` | Needs `sms_consent_at`, an E.164 `phone`, no `sms_stop` suppression for the phone, no text in the last 24 h (day-before reminder and follow-ups), and local time 08:00 to 21:00 in `contacts.timezone` (default America/New_York) |
| Templates | `src/moh_leadgen/outreach/text_templates.py` | Booking confirmation, reminder the day before, reminder 1 h before, follow-up to an `interested` or `meeting_requested` email reply. First person from the rep, MyOrbit Health named once, no links, under 300 characters, opt-out as a plain sentence ("If texts aren't useful, just say so and I'll stop.") |
| Stop handling | `texting.is_stop_text` | stop, unsubscribe, cancel, quit, end, opt out, remove me (whole message or first word, any case), plus a classifier `unsubscribe`: writes a permanent `suppressions` row with reason `sms_stop` for the phone and sends one text, "Done, no more texts from me." |
| Commands | `leadgen outreach text ...` | `reminders [--dry-run] [--window-hours 26] [--follow-ups]`, `send --to +1... --body "..." [--force-consent-recorded "<source>"]`, `inbound [--limit 200]` |

Every text sent is an `activities` row, kind `sms_sent`, `meeting_id` set for reminders, body starting with a tag such as `[sms:reminder_24h handle=<message_handle>]`. The tag plus the meeting is the idempotency key, so the reminder job can run every hour without sending anything twice. `contacts.last_texted_at` is set on each send. Inbound texts become an `sms_received` activity, a `reply_classifications` row (same schema and classifier as email replies; stop words skip Claude and are stored with model `rule:stop_word`), a rep chosen by `routing.route` (portal RPC `assign_rep` first), and a task with source `sms:<webhook_event_id>`.

Reminder timing per meeting, at most one text per run: confirmation as soon as a future meeting with a consenting contact is seen; the day-before text when the meeting is 6 to 26 hours away (skipped if a text went out in the last 24 h, for example a same-day booking confirmation); the 1 h text 30 to 90 minutes before. Meetings with outcome `cancelled`, `rescheduled` or `no_show` get nothing. Quiet hours only delay a text to the next hourly run.

## Consent on the Cal.com booking form

Add two booking questions to every event type:

1. "Mobile number" (phone field, optional).
2. A checkbox, unticked by default, with exactly this text:

   > Text me a confirmation and reminders about this call at the number above. About 3 texts per booking, plus replies if I write back. Msg and data rates may apply. I can text "stop" at any time.

The `calcom-webhook` function stores, on the matched contact: `phone` in E.164, `sms_consent_at` = booking created time when the box is ticked, `sms_consent_source` = `calcom_booking_form:<event type slug>:<booking uid>`, and `timezone` = the attendee timezone from the booking payload. Untick means `sms_consent_at` stays null. Keep the checkbox text above in the repo history: it is the consent record's wording.

## Portal migration to request

Ask the portal (Lovable) for these additive changes. They are not in this repo's migrations folder on purpose.

```
contacts:      sms_consent_at timestamptz, sms_consent_source text, timezone text, last_texted_at timestamptz
suppressions:  phone text (E.164); allow reason 'sms_stop' in the reason check;
               relax the row check to (email is not null or domain is not null or phone is not null);
               unique index on phone where phone is not null
webhook_events: no change (provider 'sendblue' rows use the existing unique (provider, idempotency_key))
activities:    kinds 'sms_sent' and 'sms_received' (if kind has a check constraint)
```

## `sendblue-webhook` route (portal Edge Function)

- `POST /functions/v1/sendblue-webhook?token=<SENDBLUE_WEBHOOK_SECRET>`. Register this URL in the SendBlue dashboard for the `receive` type (optionally `outbound` for delivery status).
- Verify: compare the `token` query parameter with the `SENDBLUE_WEBHOOK_SECRET` function secret in constant time; 401 otherwise. SendBlue's docs say a per-webhook `secret` can be configured and is included in the request headers, but they do not name the header and describe no HMAC, so the query token is the check we rely on. When the header name is confirmed with SendBlue, check it as well.
- Insert `webhook_events(provider='sendblue', event_type=<payload.type, or 'receive' when absent>, idempotency_key=<payload.message_handle>, payload=<raw JSON>)`. On a unique violation return 200 (SendBlue may deliver twice). Return 200 quickly: SendBlue waits 45 s and retries up to 3 times on 5xx.
- Optional and recommended: run the stop-word check inline (port `is_stop_text`) so an opt-out is honoured within seconds. The hourly `inbound` sweep is the floor and does the rest.

## Schedule

`.github/workflows/outreach-replies.yml` has an hourly schedule (`17 * * * *`) that runs only the two text steps, `leadgen outreach text inbound` then `leadgen outreach text reminders --window-hours 26`, each gated on `SENDBLUE_API_KEY_ID` (and the portal key). The 6-hourly schedule still runs classify and pull-stats, plus the text steps. Secrets: `SENDBLUE_API_KEY_ID`, `SENDBLUE_API_SECRET_KEY`; variable: `SENDBLUE_FROM_NUMBER`. Classifying non-stop inbound texts needs `ANTHROPIC_API_KEY`; without it those events wait for the next run, and stop words are still honoured.

## SendBlue API facts and limits (checked 2026-11-27)

- Send: `POST https://api.sendblue.com/api/send-message`, headers `sb-api-key-id` and `sb-api-secret-key`, JSON `number` (E.164), `content`, `from_number`, `media_url`, `status_callback`. Message id: `message_handle`. Statuses: REGISTERED, PENDING, DECLINED, QUEUED, ACCEPTED, SENT, DELIVERED, READ, ERROR. https://docs.sendblue.com/getting-started/sending-messages
- Get one message: `GET /api/v2/messages/:message_id`. https://docs.sendblue.com/api-v2/messages
- Webhooks: types include `receive` and `outbound`; payload has `content`, `from_number`, `number`, `to_number`, `message_handle`, `is_outbound`, `opted_out`, `date_sent`, `service`. https://docs.sendblue.com/getting-started/webhooks
- Limits per line (https://docs.sendblue.com/limits):
  - Burst: 10 messages per second.
  - Blue Ocean: 50 new contacts per line per day (3 AM ET to 3 AM ET), 15 new contacts per rolling hour. Before a contact replies: at most 300 characters per message and at most 6 SENT or DELIVERED messages; media, links, phone numbers and emails are blocked.
  - AI Agent line (our plan, inbound-first, rolling 24 h): 1,000 inbound contacts per day per line, 200 follow-up contacts per day per line, replies inside 24 h unlimited.
  - 150 consecutive outbound messages per line without a reply.
  - A 429 means the request rate was exceeded; line capacity declines return HTTP 400 with `error_code` 429 (`LINE_HOURLY_LIMIT_REACHED`, `LINE_DAILY_LIMIT_REACHED`).

The client enforces the burst cap and the stricter Blue Ocean new-contact caps (50 per day, 15 per hour) by default; numbers already texted (`last_texted_at` set) do not count as new. On an AI Agent line these can be raised in `LineRateLimiter`.
