"""Warm texting channel: SendBlue client, consent gate, templates, reminders, stop words, inbound."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from test_outreach_replies_fakes import FakePortal

from moh_leadgen.outreach import routing, texting
from moh_leadgen.outreach import text_templates as tt
from moh_leadgen.outreach.models import Classification, Disposition
from moh_leadgen.outreach.sendblue import (
    LineRateLimiter,
    RateLimitExceeded,
    SendblueClient,
    SendblueError,
    normalize_us_e164,
)

# 2026-11-27 17:00 UTC is 12:00 in New York, 09:00 in Los Angeles
NOW = datetime(2026, 11, 27, 17, 0, tzinfo=UTC)
PHONE = "+14155550134"


class FakeSendblue:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send_message(self, number, content, **kwargs):
        self.sent.append((number, content))
        return {"message_handle": f"h{len(self.sent)}", "status": "QUEUED"}


def _contact(**overrides):
    base = {
        "id": "c1",
        "first_name": "Dana",
        "phone": PHONE,
        "email": "dana@brand.com",
        "lead_domain": "brand.com",
        "company_id": "co1",
        "sms_consent_at": "2026-11-20T15:00:00+00:00",
        "sms_consent_source": "calcom_booking_form",
        "timezone": "America/New_York",
        "last_texted_at": None,
    }
    return {**base, **overrides}


def _client(handler, **kwargs) -> SendblueClient:
    clock = iter(range(1000))
    limiter = LineRateLimiter(clock=lambda: float(next(clock)), sleep=lambda s: None, now=lambda: NOW)
    return SendblueClient(
        "key-id", "secret", transport=httpx.MockTransport(handler), limiter=limiter, retry_delay=0, **kwargs
    )


# ---------------------------------------------------------------------- client
def test_client_headers_and_payload():
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json={"message_handle": "abc", "status": "QUEUED"})

    client = _client(handler, from_number="+18885550100")
    out = client.send_message("(415) 555-0134", "Hello", status_callback="https://x.test/cb")
    assert out["message_handle"] == "abc"
    req = seen[0]
    assert req.method == "POST"
    assert str(req.url) == "https://api.sendblue.com/api/send-message"
    assert req.headers["sb-api-key-id"] == "key-id"
    assert req.headers["sb-api-secret-key"] == "secret"
    assert json.loads(req.content) == {
        "number": PHONE,
        "content": "Hello",
        "from_number": "+18885550100",
        "status_callback": "https://x.test/cb",
    }


def test_client_retries_once_on_5xx_and_raises_on_4xx():
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        return httpx.Response(502 if calls["n"] == 1 else 200, json={"message_handle": "m"})

    assert _client(flaky).send_message(PHONE, "hi")["message_handle"] == "m"
    assert calls["n"] == 2

    def bad(request):
        return httpx.Response(400, text='{"error_code": 429, "error_message": "LINE_DAILY_LIMIT_REACHED"}')

    with pytest.raises(SendblueError) as info:
        _client(bad).send_message(PHONE, "hi")
    assert info.value.status == 400 and "LINE_DAILY_LIMIT_REACHED" in str(info.value)

    def always_500(request):
        return httpx.Response(500, text="boom")

    with pytest.raises(SendblueError):
        _client(always_500).send_message(PHONE, "hi")


def test_get_message_status_path():
    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/api/v2/messages/abc"
        return httpx.Response(200, json={"status": "DELIVERED"})

    assert _client(handler).get_message_status("abc")["status"] == "DELIVERED"


def test_client_requires_credentials(monkeypatch):
    monkeypatch.setattr(texting.settings, "sendblue_api_key_id", "")
    monkeypatch.setattr(texting.settings, "sendblue_api_secret", "")
    with pytest.raises(RuntimeError):
        SendblueClient()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("(415) 555-0134", PHONE),
        ("415.555.0134", PHONE),
        ("1-415-555-0134", PHONE),
        ("+1 415 555 0134", PHONE),
        ("+14155550134", PHONE),
        ("415-555-0134 x12", PHONE),
        ("555-0134", None),
        ("(015) 555-0134", None),
        ("415-155-0134", None),
        ("+44 20 7946 0958", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_us_e164(raw, expected):
    assert normalize_us_e164(raw) == expected


# ---------------------------------------------------------------------- rate limits
def test_burst_bucket_sleeps_after_ten_per_second():
    t = {"now": 0.0}
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        t["now"] += seconds

    limiter = LineRateLimiter(clock=lambda: t["now"], sleep=sleep, now=lambda: NOW)
    for _ in range(10):
        limiter.take()
    assert slept == []
    limiter.take()
    assert slept and slept[0] == pytest.approx(0.1)


def test_new_contact_caps_and_daily_reset():
    moment = {"now": NOW}
    limiter = LineRateLimiter(new_per_day=2, new_per_hour=None, now=lambda: moment["now"])
    limiter.mark_known(["+12125550000"])
    for n in ("+12125550001", "+12125550002"):
        limiter.check_new_contact(n)
        limiter.record(n)
    limiter.check_new_contact("+12125550000")  # known numbers never count
    limiter.check_new_contact("+12125550001")  # already texted today
    with pytest.raises(RateLimitExceeded):
        limiter.check_new_contact("+12125550003")
    moment["now"] = datetime(2026, 11, 28, 8, 1, tzinfo=UTC)  # 03:01 ET: new window
    limiter.check_new_contact("+12125550003")


def test_new_contacts_per_hour():
    limiter = LineRateLimiter(new_per_hour=1, now=lambda: NOW)
    limiter.record("+12125550001")
    with pytest.raises(RateLimitExceeded):
        limiter.check_new_contact("+12125550002")


# ---------------------------------------------------------------------- consent gate
def test_can_text_rules():
    assert texting.can_text(_contact(), now=NOW) == (True, "ok")
    assert texting.can_text(_contact(sms_consent_at=None), now=NOW) == (False, "no_sms_consent")
    assert texting.can_text(_contact(phone="(415) 555-0134"), now=NOW) == (False, "phone_not_e164")
    assert texting.can_text(_contact(), now=NOW, suppressed=True) == (False, "suppressed_sms_stop")
    recent = _contact(last_texted_at=(NOW - timedelta(hours=3)).isoformat())
    assert texting.can_text(recent, kind=tt.REMINDER_24H, now=NOW) == (False, "texted_within_24h")
    assert texting.can_text(recent, kind=tt.REMINDER_1H, now=NOW) == (True, "ok")
    old = _contact(last_texted_at=(NOW - timedelta(hours=25)).isoformat())
    assert texting.can_text(old, kind=tt.REMINDER_24H, now=NOW) == (True, "ok")


def test_can_text_quiet_hours_in_contact_timezone():
    early_la = datetime(2026, 11, 27, 15, 30, tzinfo=UTC)  # 07:30 Los Angeles, 10:30 New York
    assert texting.can_text(_contact(timezone="America/Los_Angeles"), now=early_la) == (False, "quiet_hours")
    assert texting.can_text(_contact(timezone=None), now=early_la) == (True, "ok")  # default New York
    late_ny = datetime(2026, 11, 28, 2, 0, tzinfo=UTC)  # 21:00 New York
    assert texting.can_text(_contact(), now=late_ny) == (False, "quiet_hours")
    eight_ny = datetime(2026, 11, 27, 13, 0, tzinfo=UTC)  # 08:00 New York
    assert texting.can_text(_contact(), now=eight_ny) == (True, "ok")
    assert texting.can_text(_contact(timezone="Not/AZone"), now=eight_ny) == (True, "ok")


# ---------------------------------------------------------------------- templates
@pytest.mark.parametrize("rep", ["Shubh", None, "A Rather Long Representative Name"])
def test_templates_follow_channel_rules(rep):
    starts = NOW + timedelta(days=3)
    texts = [
        tt.booking_confirmation("Dana", rep, starts, "America/New_York"),
        tt.reminder_24h("Dana", rep, starts, "America/Chicago"),
        tt.reminder_1h(None, rep, starts, None),
        tt.reply_follow_up("Dana", rep, "interested"),
        tt.reply_follow_up("Dana", rep, "meeting_requested"),
    ]
    for text in texts:
        assert len(text) < 300
        assert text.count("MyOrbit Health") == 1
        assert "http" not in text and "STOP" not in text
        assert chr(0x2014) not in text
    assert tt.OPT_OUT_LINE in texts[0]
    assert "Monday, November 30 at 12:00 PM EST" in texts[0]
    if rep == "Shubh":
        assert texts[0].startswith("Hi Dana, this is Shubh from MyOrbit Health.")


def test_check_rejects_links_and_boilerplate():
    with pytest.raises(ValueError):
        tt.check("Hi, MyOrbit Health here: https://cal.com/x")
    with pytest.raises(ValueError):
        tt.check("Hi from MyOrbit Health. Reply STOP to opt out")


# ---------------------------------------------------------------------- reminders
def _reminder_world(starts_at: datetime, **contact):
    portal = FakePortal(
        {
            "contacts": [_contact(**contact)],
            "meetings": [
                {
                    "id": "m1",
                    "contact_id": "c1",
                    "starts_at": starts_at.isoformat(),
                    "claimed_by": routing.SHUBH,
                }
            ],
        }
    )
    return portal


def _service(portal, client, now):
    return texting.TextService(portal, client, now=lambda: now)


def test_reminders_send_each_template_once():
    starts = NOW + timedelta(days=3)
    portal = _reminder_world(starts)
    client = FakeSendblue()

    stats = _service(portal, client, NOW).run_reminders()
    assert stats["sent"] == 1 and stats[tt.CONFIRMATION] == 1
    assert "Shubh" in client.sent[0][1]
    activity = portal.tables["activities"][0]
    assert activity["kind"] == "sms_sent" and activity["meeting_id"] == "m1"
    assert activity["body"].startswith("[sms:confirmation handle=h1] ")
    assert portal.tables["contacts"][0]["last_texted_at"] == NOW.isoformat()

    # same hour again: nothing new
    assert _service(portal, client, NOW).run_reminders()["sent"] == 0
    assert len(client.sent) == 1

    day_before = starts - timedelta(hours=25)
    assert _service(portal, client, day_before).run_reminders()[tt.REMINDER_24H] == 1
    assert _service(portal, client, day_before + timedelta(hours=1)).run_reminders()["sent"] == 0

    hour_before = starts - timedelta(minutes=70)
    assert _service(portal, client, hour_before).run_reminders()[tt.REMINDER_1H] == 1
    assert _service(portal, client, hour_before + timedelta(minutes=20)).run_reminders()["sent"] == 0
    assert [t for t in (texting.tag_of(a["body"]) for a in portal.tables["activities"])] == [
        tt.CONFIRMATION,
        tt.REMINDER_24H,
        tt.REMINDER_1H,
    ]


def test_reminders_respect_consent_suppression_and_dry_run():
    starts = NOW + timedelta(days=2)
    client = FakeSendblue()
    portal = _reminder_world(starts, sms_consent_at=None)
    assert _service(portal, client, NOW).run_reminders()["no_sms_consent"] == 1

    portal = _reminder_world(starts)
    portal.tables["suppressions"] = [{"id": 1, "phone": PHONE, "reason": "sms_stop", "expires_at": None}]
    assert _service(portal, client, NOW).run_reminders()["suppressed_sms_stop"] == 1

    portal = _reminder_world(starts)
    lines = []
    service = texting.TextService(portal, client, now=lambda: NOW, echo=lines.append)
    assert service.run_reminders(dry_run=True)["would_send"] == 1
    assert client.sent == [] and not portal.tables.get("activities") and lines

    portal = _reminder_world(starts)
    portal.tables["meetings"][0]["outcome"] = "cancelled"
    assert _service(portal, client, NOW).run_reminders()["sent"] == 0


def test_booking_inside_an_hour_gets_only_the_one_hour_text():
    sent: set[str] = set()
    meeting = {"starts_at": (NOW + timedelta(minutes=45)).isoformat()}
    assert texting.due_template(meeting, sent, NOW) == tt.REMINDER_1H
    meeting = {"starts_at": (NOW + timedelta(hours=3)).isoformat()}
    assert texting.due_template(meeting, sent, NOW) == tt.CONFIRMATION
    assert texting.due_template(meeting, {tt.CONFIRMATION}, NOW) is None  # too close for the 24 h text


def test_follow_up_to_positive_email_reply():
    portal = FakePortal(
        {
            "contacts": [_contact()],
            "webhook_events": [{"id": "e1", "provider": "smartlead"}],
            "reply_classifications": [
                {
                    "id": "rc1",
                    "webhook_event_id": "e1",
                    "contact_id": "c1",
                    "disposition": "interested",
                    "created_at": (NOW - timedelta(hours=2)).isoformat(),
                    "routed_to": routing.DIVANG,
                }
            ],
        }
    )
    client = FakeSendblue()
    assert _service(portal, client, NOW).run_follow_ups()["sent"] == 1
    assert "Divang" in client.sent[0][1]
    assert _service(portal, client, NOW + timedelta(days=2)).run_follow_ups()["already_sent"] == 1


def test_manual_send_needs_consent_unless_forced():
    portal = FakePortal({"contacts": [_contact(sms_consent_at=None)]})
    client = FakeSendblue()
    with pytest.raises(PermissionError):
        _service(portal, client, NOW).send_manual(PHONE, "Hi Dana, quick question.")
    _service(portal, client, NOW).send_manual("415-555-0134", "Hi Dana.", consent_source="phone call 11/27")
    assert client.sent == [(PHONE, "Hi Dana.")]
    assert portal.tables["contacts"][0]["sms_consent_source"] == "phone call 11/27"
    assert "consent=phone call 11/27" in portal.tables["activities"][0]["body"]


# ---------------------------------------------------------------------- stop words
@pytest.mark.parametrize(
    "body",
    [
        "STOP",
        "stop",
        " Stop. ",
        "Unsubscribe",
        "cancel",
        "QUIT",
        "end",
        "opt out",
        "Opt-out",
        "remove me",
        "Stop texting me",
        "Remove me from your list please",
    ],
)
def test_stop_words(body):
    assert texting.is_stop_text(body)


@pytest.mark.parametrize(
    "body",
    ["", None, "Can't stop thinking about it", "sounds good", "Weekend works", "Sendgrid?", "stopwatch"],
)
def test_not_stop_words(body):
    assert not texting.is_stop_text(body)


# ---------------------------------------------------------------------- inbound
def _inbound_world(*events):
    return FakePortal(
        {
            "contacts": [_contact()],
            "leads": [{"domain": "brand.com", "company_name": "Brand Co", "vertical": "med_spa"}],
            "webhook_events": [
                {
                    "id": f"ev{i}",
                    "provider": "sendblue",
                    "event_type": "receive",
                    "processed_at": None,
                    "received_at": f"2026-11-27T16:0{i}:00+00:00",
                    "payload": {
                        "content": text,
                        "from_number": PHONE,
                        "number": PHONE,
                        "is_outbound": False,
                        "message_handle": f"mh{i}",
                    },
                }
                for i, text in enumerate(events)
            ],
        },
        rpc_results={"assign_rep": routing.SHUBH},
    )


def test_inbound_stop_writes_suppression_and_confirms_once():
    portal = _inbound_world("STOP", "stop please")
    client = FakeSendblue()

    def classifier(*args):
        raise AssertionError("stop words never reach the classifier")

    stats = texting.TextService(portal, client, now=lambda: NOW, classifier=classifier).run_inbound()
    assert stats["processed"] == 2 and stats["unsubscribe"] == 2
    assert client.sent == [(PHONE, "Done, no more texts from me.")]
    sup = portal.tables["suppressions"]
    assert len(sup) == 1 and sup[0]["phone"] == PHONE and sup[0]["reason"] == "sms_stop"
    assert sup[0]["expires_at"] is None
    assert all(e["processed_at"] for e in portal.tables["webhook_events"])
    assert {r["disposition"] for r in portal.tables["reply_classifications"]} == {"unsubscribe"}
    assert not portal.tables.get("tasks")
    # once suppressed, the gate blocks every later text
    assert texting.phone_suppressed(portal, PHONE)


def test_inbound_interested_is_classified_routed_and_tasked():
    portal = _inbound_world("Yes, Tuesday 2pm works for a call")
    client = FakeSendblue()
    calls = []

    def classifier(text, subject, body, ctx):
        calls.append((text, subject, ctx))
        return (
            Classification(
                disposition=Disposition.meeting_requested,
                confidence="high",
                summary="Proposes Tuesday 2pm.",
                requested_times=["Tuesday 2pm"],
            ),
            "fake-model",
        )

    service = texting.TextService(portal, client, now=lambda: NOW, classifier=classifier)
    stats = service.run_inbound()
    assert stats == {"events": 1, "processed": 1, "failed": 0, "meeting_requested": 1}
    assert calls[0][0].startswith("Yes, Tuesday") and calls[0][2]["channel"] == "sms"
    rc = portal.tables["reply_classifications"][0]
    assert (
        rc["webhook_event_id"] == "ev0" and rc["model"] == "fake-model" and rc["routed_to"] == routing.SHUBH
    )
    task = portal.tables["tasks"][0]
    assert task["assignee_id"] == routing.SHUBH and task["source"] == "sms:ev0"
    assert task["priority"] == "high" and "Brand Co" in task["title"]
    assert portal.tables["contacts"][0]["routed_to"] == routing.SHUBH
    assert [a["kind"] for a in portal.tables["activities"]] == ["sms_received"]
    assert client.sent == [] and not portal.tables.get("suppressions")
    assert portal.rpc_calls[0] == (
        "assign_rep",
        {"p_lead_domain": "brand.com", "p_disposition": "meeting_requested"},
    )

    # a retry of the same event reuses the stored classification and does not duplicate the task
    portal.tables["webhook_events"][0]["processed_at"] = None
    assert service.run_inbound()["processed"] == 1
    assert len(calls) == 1 and len(portal.tables["tasks"]) == 1


def test_inbound_unsubscribe_disposition_also_suppresses():
    portal = _inbound_world("Please don't text me again")
    client = FakeSendblue()

    def classifier(*args):
        return Classification(
            disposition=Disposition.unsubscribe, confidence="high", summary="Wants no texts."
        ), "m"

    texting.TextService(portal, client, now=lambda: NOW, classifier=classifier).run_inbound()
    assert portal.tables["suppressions"][0]["reason"] == "sms_stop"
    assert client.sent == [(PHONE, tt.STOP_CONFIRMATION)]


def test_inbound_without_key_leaves_non_stop_events(monkeypatch):
    monkeypatch.setattr(texting.settings, "anthropic_key", "")
    portal = _inbound_world("Sounds good", "STOP")
    stats = texting.TextService(portal, FakeSendblue(), now=lambda: NOW).run_inbound()
    assert stats["skipped_no_key"] == 1 and stats["processed"] == 1
    events = {e["id"]: e for e in portal.tables["webhook_events"]}
    assert events["ev0"]["processed_at"] is None and events["ev1"]["processed_at"]
