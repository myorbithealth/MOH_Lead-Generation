"""Reply side: classifier, routing, metrics, Cal.com signature, ingest, end-to-end reply sweep."""

from __future__ import annotations

import hashlib
import hmac
import json
import random
from datetime import date

import httpx
import pytest
from test_outreach_replies_fakes import FakeAnthropic, FakePortal, FakeSmartlead, response

from moh_leadgen.outreach import calcom, classify, ingest, metrics, routing, sequences
from moh_leadgen.outreach.models import Classification, Disposition, Objection, Variant
from moh_leadgen.outreach.portal_io import PortalClient
from moh_leadgen.outreach.process_replies import ReplyProcessor, extract_reply
from moh_leadgen.store import LeadStore


def _answer(**overrides):
    base = {
        "disposition": "interested",
        "confidence": "high",
        "summary": "Wants to hear more.",
        "snooze_until": None,
        "referral": None,
        "requested_times": [],
        "objection": "none",
        "draft_reply": "Happy to show you. Grab a slot: {{booking_link}}. {{sender_name}}",
        "contains_phi": False,
    }
    return {**base, **overrides}


# ---------------------------------------------------------------------- classifier
def test_schema_enum_matches_disposition():
    schema = Classification.json_schema()
    assert schema["properties"]["disposition"]["enum"] == [d.value for d in Disposition]
    assert schema["properties"]["objection"]["enum"] == [o.value for o in Objection]
    assert set(schema["required"]) == set(schema["properties"]) == set(Classification.model_fields)
    assert schema["additionalProperties"] is False


def test_classifier_happy_path_sends_strict_schema_and_caches_system():
    client = FakeAnthropic(response(_answer(disposition="MEETING_REQUESTED", confidence="High")))
    result, model = classify.classify_reply_with_model(
        "Sure, Tuesday 2pm works", "Quick question", "Hi there", {"vertical": "med_spa"}, client
    )
    assert result.disposition is Disposition.meeting_requested  # case-insensitive
    assert result.confidence == "high"
    assert model == "claude-haiku-5-5"
    call = client.calls[0]
    assert call["model"] == "claude-haiku-5-5"
    assert call["output_config"]["effort"] == "low"
    assert call["output_config"]["format"] == {"type": "json_schema", "schema": Classification.json_schema()}
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "Tuesday 2pm" in call["messages"][0]["content"]


def test_classifier_refusal_then_escalation_returns_unclear():
    client = FakeAnthropic(response(None, "refusal"), response(None, "max_tokens"))
    result = classify.classify_reply("text", None, None, {}, client)
    assert result.disposition is Disposition.unclear and result.confidence == "low"
    assert [c["model"] for c in client.calls] == ["claude-haiku-5-5", "claude-sonnet-5-5"]
    assert client.calls[1]["fallbacks"] == "default"


def test_classifier_escalates_once_on_low_confidence():
    client = FakeAnthropic(
        response(_answer(disposition="unclear", confidence="low", draft_reply=None)),
        response(
            _answer(
                disposition="maybe_later", confidence="medium", snooze_until="2027-03-01", draft_reply=None
            )
        ),
    )
    result, model = classify.classify_reply_with_model("ping me in March", None, None, {}, client)
    assert result.disposition is Disposition.maybe_later
    assert result.snooze_until == date(2027, 3, 1)
    assert model == "claude-sonnet-5-5"


def test_unsubscribe_rule_overrides_negative_but_not_quoted_footer():
    client = FakeAnthropic(response(_answer(disposition="not_interested", draft_reply=None)))
    assert (
        classify.classify_reply("No thanks. Remove me from your list.", None, None, {}, client).disposition
        is Disposition.unsubscribe
    )
    client = FakeAnthropic(response(_answer()))
    quoted = "Yes, let's talk!\n\nOn Mon, Jan 5, Sam wrote:\n> Reply STOP to unsubscribe"
    assert classify.classify_reply(quoted, None, None, {}, client).disposition is Disposition.interested


def test_bare_stop_is_unsubscribe_even_if_model_says_auto_reply():
    client = FakeAnthropic(response(_answer(disposition="auto_reply", draft_reply=None)))
    assert classify.classify_reply("STOP", None, None, {}, client).disposition is Disposition.unsubscribe


def test_parse_handles_garbage():
    assert classify.parse_classification("not json").disposition is Disposition.unclear
    bad_date = json.dumps(_answer(snooze_until="next spring"))
    assert classify.parse_classification(bad_date).disposition is Disposition.interested


# ---------------------------------------------------------------------- routing
@pytest.mark.parametrize(
    ("clause", "facts", "expected"),
    [
        ({"field": "score", "op": "gte", "value": 80}, {"score": 80}, True),
        ({"field": "score", "op": "gte", "value": 80}, {"score": 79}, False),
        ({"field": "score", "op": "lte", "value": 50}, {"score": 50}, True),
        ({"field": "score", "op": "gte", "value": 80}, {"score": None}, False),
        ({"field": "vertical", "op": "eq", "value": "MED_SPA"}, {"vertical": "med_spa"}, True),
        ({"field": "vertical", "op": "in", "value": ["a", "Med_Spa"]}, {"vertical": "med_spa"}, True),
        ({"field": "vertical", "op": "in", "value": "med_spa"}, {"vertical": "med_spa"}, False),
        ({"field": "competitor_platform", "op": "not_null"}, {"competitor_platform": ""}, False),
        ({"field": "competitor_platform", "op": "not_null"}, {"competitor_platform": "healthie"}, True),
        ({"field": "competitor_platform", "op": "is_null"}, {}, True),
        ({"field": "owner_id", "op": "is_null"}, {}, False),  # unknown field never matches
        ({"field": "score", "op": "between", "value": 1}, {"score": 5}, False),  # unknown op
    ],
)
def test_condition_ops(clause, facts, expected):
    assert routing.clause_matches(clause, facts) is expected


def test_empty_condition_matches():
    assert routing.rule_matches({"all": []}, {}) is True


def _stacy_reps():
    return [
        *routing.DEFAULT_REPS[:2],
        {
            "profile_id": "stacy-1",
            "name": "Stacy",
            "email": "stacy@x.com",
            "booking_link": "https://cal.com/stacy",
        },
    ]


def _rules(reps):
    by_name = {r["name"]: r.get("profile_id") for r in reps}
    return [{**r, "rep_id": by_name.get(r["rep_name"])} for r in routing.DEFAULT_RULES]


@pytest.mark.parametrize(
    ("facts", "rep", "rule"),
    [
        ({"disposition": "question_objection", "score": 10}, routing.SHUBH, "shubh_question_objection"),
        ({"disposition": "interested", "score": 85}, routing.SHUBH, "shubh_high_score"),
        (
            {"disposition": "interested", "legitscript_status": "seal_detected"},
            routing.SHUBH,
            "shubh_legitscript_seal",
        ),
        (
            {"disposition": "interested", "vertical": "weight_loss_glp1", "competitor_platform": "healthie"},
            routing.DIVANG,
            "divang_dtc_competitor",
        ),
        ({"disposition": "interested", "vertical": "med_spa"}, "stacy-1", "stacy_local_verticals"),
        ({"disposition": "interested", "source": "local_business"}, "stacy-1", "stacy_local_business"),
    ],
)
def test_default_rules(facts, rep, rule):
    reps = _stacy_reps()
    assert routing.evaluate(_rules(reps), reps, facts) == (rep, rule)


def test_rules_without_profile_are_skipped_and_fallback_balances():
    reps = routing.DEFAULT_REPS  # Stacy has no profile id: her rules fall through
    facts = {"disposition": "interested", "vertical": "med_spa"}
    rep, rule = routing.evaluate(_rules(reps), reps, facts, {routing.SHUBH: 4, routing.DIVANG: 1})
    assert (rep, rule) == (routing.DIVANG, "fallback_least_loaded")


def test_route_prefers_rpc_and_adds_tracking_params():
    portal = FakePortal(
        {
            "app_settings": [
                {
                    "key": "outreach",
                    "value": {
                        "reps": [
                            {
                                "profile_id": routing.DIVANG,
                                "name": "Divang",
                                "email": "d@x",
                                "booking_link": "https://cal.com/divang?x=1",
                            }
                        ]
                    },
                }
            ]
        },
        rpc_results={"assign_rep": routing.DIVANG},
    )
    a = routing.route("brand.com", "interested", portal, contact_id="c-9")
    assert a.rep_id == routing.DIVANG and a.rule == "rpc:assign_rep"
    assert a.booking_link == "https://cal.com/divang?x=1&lead_domain=brand.com&contact_id=c-9"
    assert portal.rpc_calls == [("assign_rep", {"p_lead_domain": "brand.com", "p_disposition": "interested"})]


def test_route_falls_back_to_python_when_rpc_fails():
    portal = FakePortal(
        {"leads": [{"domain": "brand.com", "score": 90}]}, rpc_results={"assign_rep": RuntimeError("404")}
    )
    a = routing.route("brand.com", "interested", portal)
    assert a.rep_id == routing.SHUBH and a.rule == "shubh_high_score"


# ---------------------------------------------------------------------- metrics
def _v(vid, sends, pos, status="active", segment="glp1_competitor", step=1, **kw):
    return Variant(
        id=vid,
        segment=segment,
        sequence_step=step,
        name=vid,
        sends=sends,
        positive_replies=pos,
        status=status,
        **kw,
    )


def test_beta_posteriors():
    post = metrics.beta_posteriors([_v("a", 100, 7), _v("new", 0, 0, alpha=3, beta=40)])
    assert post == {"a": (8.0, 94.0), "new": (3.0, 40.0)}


def test_p_best_sums_to_one_and_favours_winner():
    probs = metrics.p_best([_v("a", 500, 40), _v("b", 500, 5)], draws=4000, rng=random.Random(1))
    assert abs(sum(probs.values()) - 1) < 1e-9
    assert probs["a"] > 0.99


def test_decide_thresholds():
    arms = [
        _v("winner", 600, 40),
        _v("loser", 400, 1),
        _v("young_loser", 100, 0),
        _v("ctrl", 600, 3, status="control"),
        _v("other_slot", 50, 1, step=2),
    ]
    actions = {vid: action for vid, action, _ in metrics.decide(arms, draws=4000, rng=random.Random(2))}
    assert actions == {
        "winner": "promote",
        "loser": "retire",
        "young_loser": "keep",
        "ctrl": "keep",
        "other_slot": "keep",
    }


def test_promote_needs_enough_positives():
    arms = [_v("a", 600, 14), _v("b", 600, 0)]
    actions = {vid: a for vid, a, _ in metrics.decide(arms, draws=2000, rng=random.Random(3))}
    assert actions["a"] == "keep" and actions["b"] == "retire"


def test_thompson_control_floor():
    arms = [_v("hot", 500, 100), _v("ctrl", 500, 1, status="control"), _v("old", 500, 0, status="retired")]
    rng = random.Random(4)
    picks = [metrics.thompson_choose(arms, rng, control_floor=0.10).id for _ in range(5000)]
    share = picks.count("ctrl") / len(picks)
    assert 0.08 < share < 0.12
    assert "old" not in picks


def test_learn_dedupes_lints_and_inherits_half_prior():
    parent = {
        "id": "p1",
        "segment": "glp1_competitor",
        "sequence_step": 1,
        "name": "control_a",
        "status": "control",
        "body": "Hi {{first_name}}, we run the telehealth back office for brands on {{platform}}.",
        "subject": "telehealth back office",
        "sends": 0,
        "positive_replies": 0,
        "alpha": 6,
        "beta": 100,
    }
    portal = FakePortal({"variants": [parent], "reply_classifications": []})
    proposals = {
        "variants": [
            {
                "name": "speed",
                "angle": "speed",
                "subject": "launch in 3 weeks",
                "body": "Hi {{first_name}}, brands on {{platform}} launch new states in weeks with us. Worth a look?",
                "hypothesis": "speed beats price",
                "parent_id": "p1",
            },
            {
                "name": "dup",
                "angle": "x",
                "subject": "dup",
                "body": parent["body"] + "!",
                "hypothesis": "h",
                "parent_id": None,
            },
            {
                "name": "claim",
                "angle": "x",
                "subject": "Guaranteed results",
                "body": "We guarantee patients lose 20 lbs.",
                "hypothesis": "h",
                "parent_id": None,
            },
        ]
    }
    client = FakeAnthropic(response(proposals))
    report = metrics.learn(portal, propose=3, client=client, rng=random.Random(5))
    assert [r["name"] for r in report["proposed"]] == ["speed"]
    assert len(report["rejected"]) == 2
    new = next(v for v in portal.tables["variants"] if v["name"] == "speed")
    assert (new["status"], new["alpha"], new["beta"], new["parent_id"]) == ("proposed", 3.0, 50.0, "p1")
    assert new["id"] == sequences.variant_id("glp1_competitor", 1, "speed")
    assert client.calls[0]["model"] == "claude-opus-5-5"
    assert ("outreach_refresh_variants", {}) in portal.rpc_calls


def test_report_rows_positive_rate_and_p_best():
    rows = metrics.report_rows(
        [
            {"segment": "s", "variant_id": "a", "sequence_step": 1, "sends": 100, "positive_replies": 10},
            {"segment": "s", "variant_id": "b", "sequence_step": 1, "sends": 100, "positive_replies": 0},
        ],
        rng=random.Random(6),
    )
    assert rows[0]["positive_rate"] == 0.1 and rows[0]["p_best"] > 0.95


# ---------------------------------------------------------------------- Cal.com
def test_calcom_signature():
    body = b'{"triggerEvent":"BOOKING_CREATED"}'
    sig = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert calcom.verify_webhook_signature(body, sig, "s3cret")
    assert calcom.verify_webhook_signature(body, "sha256=" + sig.upper(), "s3cret")
    assert not calcom.verify_webhook_signature(body + b" ", sig, "s3cret")
    assert not calcom.verify_webhook_signature(body, "no-secret-provided", "s3cret")
    assert not calcom.verify_webhook_signature(body, sig, "")


def test_calcom_client_headers_and_cursor():
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        cursor = request.url.params.get("cursor")
        data = {
            "status": "success",
            "data": [{"uid": cursor or "first"}],
            "pagination": {"hasMore": cursor is None, "nextCursor": None if cursor else "c2"},
        }
        return httpx.Response(200, json=data)

    client = calcom.CalcomClient("cal_test", transport=httpx.MockTransport(handler))
    assert [b["uid"] for b in client.list_bookings(after="2026-11-01T00:00:00Z")] == ["first", "c2"]
    assert seen[0].headers["authorization"] == "Bearer cal_test"
    assert seen[0].headers["cal-api-version"] == calcom.BOOKINGS_VERSION
    assert seen[0].url.params["afterStart"] == "2026-11-01T00:00:00Z"


# ---------------------------------------------------------------------- portal_io
def test_portal_client_retries_once_on_5xx():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503 if len(calls) == 1 else 200, json=[{"id": 1}])

    portal = PortalClient(
        "https://x.supabase.co", "sb_secret_test", transport=httpx.MockTransport(handler), retry_delay=0
    )
    assert portal.select("leads", {"domain": "eq.a.com"}) == [{"id": 1}]
    assert len(calls) == 2 and calls[1].url.params["domain"] == "eq.a.com"


# ---------------------------------------------------------------------- ingest
def test_ingest_maps_columns_and_keeps_attribution(tmp_path):
    csv_path = tmp_path / "meta.csv"
    csv_path.write_text(
        "Full Name,Email,Phone Number,Company Name,Website,leadgen_id,fbclid,utm_campaign\n"
        "Dana Ruiz,Dana@BrandRx.com,+1 555 0100,BrandRx,https://www.brandrx.com/,L123,FB1,glp1_q1\n"
        "Solo Person,solo@gmail.com,,,,L124,,\n",
        encoding="utf-8",
    )
    store = LeadStore(tmp_path / "leads.db")
    stats = ingest.ingest_file(csv_path, "meta_ads", store)
    assert stats == {"rows": 2, "new": 1, "updated": 0, "skipped": 1}
    lead = store.get("brandrx.com")
    assert lead.source == "meta_ads" and lead.company_name == "BrandRx"
    assert lead.primary_email == "dana@brandrx.com" and lead.owner_name == "Dana Ruiz"
    assert lead.source_detail["ingest"]["utm_campaign"] == "glp1_q1"
    assert lead.source_detail["ingest"]["fbclid"] == "FB1"
    assert lead.source_detail["ingest"]["leadgen_id"] == "L123"


def test_ingest_json_field_data_and_existing_lead(tmp_path):
    store = LeadStore(tmp_path / "leads.db")
    ingest.ingest_rows([{"email": "a@clinic.com", "first_name": "Ann", "last_name": "Lee"}], "inbound", store)
    path = tmp_path / "leads.json"
    path.write_text(
        json.dumps(
            {
                "data": [
                    {
                        "id": "L9",
                        "field_data": [
                            {"name": "email", "values": ["bob@clinic.com"]},
                            {"name": "full_name", "values": ["Bob Ng"]},
                        ],
                    }
                ]
            }
        )
    )
    assert ingest.ingest_file(path, "meta_ads", store)["updated"] == 1
    lead = store.get("clinic.com")
    assert lead.source == "inbound"
    assert {p.name for p in lead.people} == {"Ann Lee", "Bob Ng"}
    assert lead.source_detail["ingest_history"][0]["leadgen_id"] == "L9"


# ---------------------------------------------------------------------- end to end
def _portal_for_reply(reply_text="Yes please, what does onboarding cost?"):
    return FakePortal(
        {
            "webhook_events": [
                {
                    "id": "ev1",
                    "provider": "smartlead",
                    "event_type": "EMAIL_REPLY",
                    "processed_at": None,
                    "payload": {
                        "event_type": "EMAIL_REPLY",
                        "to_email": "dana@brandrx.com",
                        "subject": "Re: launch",
                        "campaign_id": 77,
                        "sequence_number": 1,
                        "sl_email_lead_id": 555,
                        "stats_id": "st-1",
                        "reply_message": {"text": reply_text, "time": "2026-11-27T10:00:00Z"},
                    },
                }
            ],
            "email_drafts": [
                {
                    "id": "d1",
                    "direction": "outbound",
                    "smartlead_lead_id": 555,
                    "sequence_step": 1,
                    "contact_id": "c1",
                    "lead_domain": "brandrx.com",
                    "subject": "launch",
                    "body": "Hi Dana",
                    "sent_at": "2026-11-20T10:00:00Z",
                    "replied_at": None,
                    "to_email": "dana@brandrx.com",
                }
            ],
            "contacts": [
                {
                    "id": "c1",
                    "email": "Dana@BrandRx.com",
                    "lead_domain": "brandrx.com",
                    "company_id": "co1",
                    "smartlead_lead_id": 555,
                    "smartlead_campaign_id": 77,
                }
            ],
            "leads": [
                {
                    "domain": "brandrx.com",
                    "company_name": "BrandRx",
                    "score": 50,
                    "vertical": "weight_loss_glp1",
                    "competitor_platform": "healthie",
                }
            ],
            "app_settings": [
                {
                    "key": "outreach",
                    "value": {
                        "reply_sla_minutes": 30,
                        "reps": [
                            {
                                "profile_id": routing.SHUBH,
                                "name": "Shubh",
                                "email": "s@x",
                                "booking_link": "https://cal.com/s",
                            },
                            {
                                "profile_id": routing.DIVANG,
                                "name": "Divang",
                                "email": "d@x",
                                "booking_link": "https://cal.com/d",
                            },
                        ],
                    },
                }
            ],
            "rep_rules": [],
            "tasks": [],
            "reply_classifications": [],
            "suppressions": [],
        },
        rpc_results={"assign_rep": RuntimeError("no rpc in test")},
    )


def test_process_replies_end_to_end_question():
    portal = _portal_for_reply()
    sl = FakeSmartlead()
    seen = []

    def classifier(text, subject, body, ctx):
        seen.append((text, subject, body, ctx))
        return Classification.model_validate(
            _answer(disposition="question_objection", objection="price")
        ), "fake"

    stats = ReplyProcessor(portal, classifier=classifier, smartlead=sl).run()
    assert stats["processed"] == 1 and stats["question_objection"] == 1
    assert seen[0][1:3] == ("launch", "Hi Dana") and seen[0][3]["vertical"] == "weight_loss_glp1"

    rc = portal.tables["reply_classifications"][0]
    assert (rc["disposition"], rc["email_draft_id"], rc["contact_id"], rc["routed_to"]) == (
        "question_objection",
        "d1",
        "c1",
        routing.SHUBH,
    )  # question_objection -> Shubh rule
    assert portal.tables["leads"][0]["disposition"] == "question_objection"
    assert portal.tables["contacts"][0]["last_replied_at"] == "2026-11-27T10:00:00Z"
    assert portal.tables["email_drafts"][0]["status"] == "replied"
    task = portal.tables["tasks"][0]
    assert task["assignee_id"] == routing.SHUBH and task["source"] == f"reply:{rc['id']}"
    assert "BrandRx" in task["title"] and "question objection" in task["title"]
    draft = portal.tables["email_drafts"][1]
    assert draft["status"] == "pending_approval" and draft["approver_id"] == routing.SHUBH
    assert draft["subject"] == "Re: launch" and "{{booking_link}}" not in draft["body"]
    assert (
        "https://cal.com/s?lead_domain=brandrx.com&contact_id=c1" in draft["body"]
        and "Shubh" in draft["body"]
    )
    assert (draft["smartlead_lead_id"], draft["smartlead_stats_id"]) == (555, "st-1")
    assert ("category", 77, 555, 1, True) in sl.calls
    assert portal.tables["webhook_events"][0]["processed_at"] is not None
    assert portal.tables["suppressions"] == []

    # A second sweep over the same event (e.g. processed_at reset) duplicates nothing.
    portal.tables["webhook_events"][0]["processed_at"] = None
    ReplyProcessor(
        portal, classifier=lambda *a: pytest.fail("must reuse stored classification"), smartlead=sl
    ).run()
    assert len(portal.tables["tasks"]) == 1 and len(portal.tables["email_drafts"]) == 2


def test_process_replies_unsubscribe_suppresses_and_failures_are_recorded():
    portal = _portal_for_reply("Please remove me")
    sl = FakeSmartlead()
    unsub = Classification.model_validate(_answer(disposition="unsubscribe", draft_reply=None))
    ReplyProcessor(portal, classifier=lambda *a: (unsub, "fake"), smartlead=sl).run()
    reasons = sorted((s["email"] or "", s["domain"], s["reason"]) for s in portal.tables["suppressions"])
    assert reasons == [("", "brandrx.com", "unsubscribe"), ("dana@brandrx.com", "brandrx.com", "unsubscribe")]
    assert portal.tables["contacts"][0]["unsubscribed_at"]
    assert portal.tables["tasks"] == []
    assert ("category", 77, 555, 3, True) in sl.calls
    assert ("unsubscribe_globally", 555) in sl.calls

    portal = _portal_for_reply()

    def broken(*a):
        raise RuntimeError("anthropic down")

    stats = ReplyProcessor(portal, classifier=broken, smartlead=sl).run()
    event = portal.tables["webhook_events"][0]
    assert stats["failed"] == 1 and event["processed_at"] is None and "anthropic down" in event["error"]


def test_maybe_later_snoozes_without_task():
    portal = _portal_for_reply("Circle back next year")
    later = Classification.model_validate(
        _answer(disposition="maybe_later", confidence="medium", draft_reply=None)
    )
    ReplyProcessor(
        portal, classifier=lambda *a: (later, "fake"), smartlead=FakeSmartlead(), today=date(2026, 11, 27)
    ).run()
    assert portal.tables["contacts"][0]["snooze_until"] == "2027-01-26"
    assert portal.tables["tasks"] == []


def test_out_of_office_resumes_the_sequence_after_the_snooze():
    """Plan 6.5: OOO pauses until snooze_until, then resumes; a bare pause would end the sequence."""
    portal = _portal_for_reply("Out until Dec 3")
    sl = FakeSmartlead()
    ooo = Classification.model_validate(
        _answer(disposition="out_of_office", draft_reply=None, snooze_until="2026-12-03")
    )
    ReplyProcessor(portal, classifier=lambda *a: (ooo, "fake"), smartlead=sl, today=date(2026, 11, 27)).run()
    assert ("category", 77, 555, 4, False) in sl.calls
    assert ("resume", 77, 555, 6) in sl.calls
    assert not any(call[0] == "pause" for call in sl.calls)
    assert portal.tables["contacts"][0]["snooze_until"] == "2026-12-03"


def test_extract_reply_old_shape():
    facts = extract_reply(
        {
            "event": "EMAIL_REPLIED",
            "campaign_id": "9",
            "lead_id": 3,
            "lead": {"email": "X@Y.com"},
            "reply": {"body": "hi", "subject": "Re: a"},
        }
    )
    assert (facts.lead_email, facts.reply_text, facts.campaign_id, facts.smartlead_lead_id) == (
        "x@y.com",
        "hi",
        9,
        3,
    )


def test_add_suppression_upgrades_expiring_and_expired_rows():
    """An unsubscribe after a not_interested must become permanent, not be skipped as a dup."""
    from datetime import UTC, datetime, timedelta

    from moh_leadgen.outreach import portal_io

    future = (datetime.now(UTC) + timedelta(days=100)).isoformat()
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    portal = FakePortal(
        {
            "suppressions": [
                {
                    "id": 1,
                    "email": "jane@acme.com",
                    "domain": "acme.com",
                    "reason": "not_interested",
                    "expires_at": future,
                },
                {"id": 2, "email": None, "domain": "old.com", "reason": "not_interested", "expires_at": past},
                {
                    "id": 3,
                    "email": "perm@acme.com",
                    "domain": "acme.com",
                    "reason": "bounce",
                    "expires_at": None,
                },
            ]
        }
    )
    portal_io.add_suppression(
        portal, email="jane@acme.com", domain="acme.com", reason="unsubscribe", source="reply"
    )
    portal_io.add_suppression(
        portal,
        email=None,
        domain="old.com",
        reason="not_interested",
        source="reply",
        expires_at=datetime.now(UTC) + timedelta(days=180),
    )
    portal_io.add_suppression(
        portal, email="perm@acme.com", domain="acme.com", reason="not_interested", source="reply"
    )
    portal_io.add_suppression(
        portal, email="jane@acme.com", domain="acme.com", reason="not_interested", source="x"
    )
    rows = {r["id"]: r for r in portal.tables["suppressions"]}
    assert len(rows) == 3
    assert rows[1]["reason"] == "unsubscribe" and rows[1]["expires_at"] is None
    assert rows[2]["expires_at"] > datetime.now(UTC).isoformat()
    assert rows[3]["reason"] == "bounce" and rows[3]["expires_at"] is None
