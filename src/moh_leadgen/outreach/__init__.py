"""Outbound layer: enrol sendable leads into Smartlead, classify replies, route to reps, learn.

Modules (each owned by one piece of work, see docs/outbound-system-plan.md):
  models.py      shared enums/schemas (Disposition, Classification, OutreachState)
  smartlead.py   Smartlead REST client (campaigns, sequences, leads, categories, webhooks, stats)
  sequences.py   segment -> campaign/sequence definitions (steps, variants, footer)
  personalize.py per-lead variables + cached Claude opener
  gate.py        sendability + suppression rules
  enroll.py      pick leads, build Smartlead payloads, record enrolment in store + portal
  classify.py    Claude structured-output reply classifier (strict enum)
  routing.py     disposition + lead facts -> rep (rep_rules), booking link
  calcom.py      Cal.com v2 client (round-robin event types, bookings)
  metrics.py     funnel per variant/segment, Beta posteriors, retire/promote, variant proposals
  cli.py         `leadgen outreach ...` sub-commands (cli_enroll.py, cli_replies.py)
"""
