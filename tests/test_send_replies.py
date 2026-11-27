"""Approved reply drafts go out through Smartlead's reply-email-thread, once each."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
from test_outreach_replies_fakes import FakePortal

from moh_leadgen.outreach.send_replies import body_to_html, latest_inbound, send_approved
from moh_leadgen.outreach.smartlead import SmartleadClient

NOW = datetime(2026, 11, 27, 18, 0, tzinfo=UTC)
REP = "rep-1"


def _draft(**overrides) -> dict:
    base = {
        "id": "d1",
        "to_email": "dana@brand.com",
        "subject": "Re: quick question",
        "body": "Hi Dana,\nHere is my calendar: https://cal.example/shubh\n\nShubh",
        "status": "approved",
        "approver_id": REP,
        "contact_id": "c1",
        "lead_domain": "brand.com",
        "direction": "outbound",
        "smartlead_campaign_id": 4707751,
        "smartlead_lead_id": 991,
        "smartlead_stats_id": "stats-abc",
        "provider_message_id": "<inbound-1@brand.com>",
        "sent_at": None,
        "created_at": "2026-11-27T10:00:00+00:00",
        "notes": None,
    }
    return {**base, **overrides}


def _portal(*drafts, reps=None) -> FakePortal:
    outreach = {
        "reps": reps or [{"profile_id": REP, "name": "Shubh", "booking_link": "https://cal.example/shubh"}]
    }
    return FakePortal(
        {
            "email_drafts": list(drafts),
            "contacts": [
                {
                    "id": "c1",
                    "first_name": "Dana",
                    "last_name": "Lee",
                    "company_id": "co1",
                    "email": "dana@brand.com",
                }
            ],
            "app_settings": [{"key": "outreach", "value": outreach}],
        }
    )


class Recorder:
    def __init__(self, reply_status=200, reply_json=None, history=None):
        self.requests: list[httpx.Request] = []
        self.reply_status = reply_status
        self.reply_json = reply_json if reply_json is not None else {"success": True, "message": "Reply sent"}
        self.history = history or {"messages": []}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/reply-email-thread"):
            return httpx.Response(self.reply_status, json=self.reply_json)
        if request.url.path.endswith("/message-history"):
            return httpx.Response(200, json=self.history)
        return httpx.Response(404, json={"error": "unexpected"})

    def replies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests if r.url.path.endswith("/reply-email-thread")]


def _smartlead(recorder: Recorder) -> SmartleadClient:
    return SmartleadClient(
        "test-key",
        "https://server.smartlead.test/api/v1",
        transport=httpx.MockTransport(recorder),
        sleep=lambda s: None,
    )


def test_happy_path_sends_patches_and_logs_activity():
    portal = _portal(_draft())
    rec = Recorder(reply_json={"success": True, "message_id": "<sent-1@mail.example>"})

    stats = send_approved(portal, _smartlead(rec), now=NOW)

    assert stats["sent"] == 1 and stats["failed"] == 0
    [req] = rec.requests
    assert req.method == "POST"
    assert req.url.path == "/api/v1/campaigns/4707751/reply-email-thread"
    assert req.url.params["api_key"] == "test-key"
    [body] = rec.replies()
    assert body == {
        "email_stats_id": "stats-abc",
        # the persona mailbox signs via Smartlead's signature, so the draft's sign-off line is dropped
        "email_body": "Hi Dana,<br>Here is my calendar: https://cal.example/shubh",
        "add_signature": True,
        "to_email": "dana@brand.com",
        "to_first_name": "Dana",
        "to_last_name": "Lee",
        "reply_message_id": "<inbound-1@brand.com>",
    }
    row = portal.tables["email_drafts"][0]
    assert row["status"] == "sent"
    assert row["sent_at"] == NOW.isoformat()
    assert row["provider_message_id"] == "<sent-1@mail.example>"
    [activity] = portal.tables["activities"]
    assert activity["kind"] == "reply_sent"
    assert activity["contact_id"] == "c1" and activity["company_id"] == "co1"
    assert activity["body"] == _draft()["body"][:200]


def test_missing_stats_id_uses_latest_inbound_from_history():
    history = {
        "history": [
            {"type": "SENT", "stats_id": "s-1", "message_id": "<out-1>", "time": "2026-11-20T10:00:00Z"},
            {"type": "REPLY", "stats_id": "s-1", "message_id": "<in-1>", "time": "2026-11-21T10:00:00Z"},
            {
                "type": "REPLY",
                "stats_id": "s-2",
                "message_id": "<in-2>",
                "time": "2026-11-26T10:00:00Z",
                "email_body": "<p>sounds good</p>",
            },
        ]
    }
    portal = _portal(_draft(smartlead_stats_id=None, provider_message_id=None))
    rec = Recorder(history=history)

    assert send_approved(portal, _smartlead(rec), now=NOW)["sent"] == 1

    assert rec.requests[0].url.path == "/api/v1/campaigns/4707751/leads/991/message-history"
    [body] = rec.replies()
    assert body["email_stats_id"] == "s-2"
    assert body["reply_message_id"] == "<in-2>"
    assert body["reply_email_time"] == "2026-11-26T10:00:00Z"
    assert body["reply_email_body"] == "<p>sounds good</p>"
    # no id came back from Smartlead: the column keeps what it had
    assert portal.tables["email_drafts"][0]["provider_message_id"] is None


def test_4xx_marks_send_failed_and_continues():
    portal = _portal(_draft(id="d1", send_error=None), _draft(id="d2", contact_id="c1"))
    rec = Recorder(reply_status=400, reply_json={"error": "invalid email_stats_id"})

    stats = send_approved(portal, _smartlead(rec), now=NOW)

    assert stats["failed"] == 2 and stats["sent"] == 0
    assert len(rec.replies()) == 2  # the first failure did not stop the run
    d1, d2 = portal.tables["email_drafts"]
    assert d1["status"] == "send_failed" and "invalid email_stats_id" in d1["send_error"]
    assert d2["status"] == "send_failed" and "invalid email_stats_id" in d2["notes"]
    assert d1["sent_at"] is None and not portal.tables.get("activities")


def test_5xx_leaves_draft_approved_for_next_run():
    portal = _portal(_draft())
    rec = Recorder(reply_status=503, reply_json={"error": "down"})

    stats = send_approved(portal, _smartlead(rec), now=NOW)

    assert stats["retry"] == 1
    row = portal.tables["email_drafts"][0]
    assert row["status"] == "approved" and row["sent_at"] is None


def test_dry_run_sends_and_writes_nothing():
    portal = _portal(_draft())
    rec = Recorder()

    stats = send_approved(portal, _smartlead(rec), dry_run=True, now=NOW)

    assert stats["would_send"] == ["d1"] and stats["sent"] == 0
    assert rec.replies() == []
    assert portal.tables["email_drafts"][0]["status"] == "approved"
    assert not portal.tables.get("activities")


def test_already_sent_and_other_rows_are_skipped():
    portal = _portal(
        _draft(id="sent", status="sent", sent_at="2026-11-27T09:00:00+00:00"),
        _draft(id="stamped", sent_at="2026-11-27T09:00:00+00:00"),  # approved but already has sent_at
        _draft(id="pending", status="pending_approval"),
        _draft(id="inbound", direction="inbound"),
    )
    rec = Recorder()

    stats = send_approved(portal, _smartlead(rec), now=NOW)

    assert stats["approved"] == 0 and rec.requests == []


def test_rerun_does_not_send_twice():
    portal = _portal(_draft())
    rec = Recorder()
    smartlead = _smartlead(rec)

    send_approved(portal, smartlead, now=NOW)
    second = send_approved(portal, smartlead, now=NOW)

    assert second["approved"] == 0 and len(rec.replies()) == 1
    assert len(portal.tables["activities"]) == 1


def test_row_changed_after_listing_is_skipped(monkeypatch):
    portal = _portal(_draft())
    rec = Recorder()
    real_select = portal.select

    def select(table, filters=None, **kwargs):
        rows = real_select(table, filters, **kwargs)
        if table == "email_drafts" and set(filters or {}) == {"id"}:  # the pre-send re-check
            portal.tables["email_drafts"][0]["status"] = "rejected"
            rows = real_select(table, filters, **kwargs)
        return rows

    monkeypatch.setattr(portal, "select", select)
    stats = send_approved(portal, _smartlead(rec), now=NOW)

    assert stats["skipped"] == 1 and rec.replies() == []


def test_placeholder_booking_link_filled_from_approver():
    portal = _portal(_draft(body="Grab a time: {{booking_link}}\n{{sender_name}}"))
    rec = Recorder()

    send_approved(portal, _smartlead(rec), now=NOW)

    [body] = rec.replies()
    assert (
        body["email_body"] == "Grab a time: https://cal.example/shubh?lead_domain=brand.com&amp;contact_id=c1"
    )
    assert "{{" not in portal.tables["activities"][0]["body"]


def test_placeholder_falls_back_to_shared_link(monkeypatch):
    monkeypatch.setattr("moh_leadgen.outreach.send_replies.settings.booking_url", "https://book.example/team")
    portal = _portal(_draft(body="Book: {{booking_link}}", approver_id="someone-else"))
    rec = Recorder()

    send_approved(portal, _smartlead(rec), now=NOW)

    assert rec.replies()[0]["email_body"].startswith("Book: https://book.example/team?lead_domain=brand.com")


def test_placeholder_without_any_link_fails_without_sending(monkeypatch):
    monkeypatch.setattr("moh_leadgen.outreach.send_replies.settings.booking_url", "")
    portal = _portal(_draft(body="Book: {{booking_link}}", approver_id=None))
    rec = Recorder()

    stats = send_approved(portal, _smartlead(rec), now=NOW)

    assert stats["failed"] == 1 and rec.requests == []
    row = portal.tables["email_drafts"][0]
    assert row["status"] == "send_failed" and "booking_link" in row["notes"]


def test_body_to_html_escapes_markup():
    assert body_to_html("a < b & c\r\nnext") == "a &lt; b &amp; c<br>next"


def test_latest_inbound_reads_documented_shape():
    history = {"messages": [{"id": "m1", "direction": "outbound"}, {"id": "m2", "direction": "inbound"}]}
    assert latest_inbound(history)["id"] == "m2"
    assert latest_inbound({"messages": []}) is None
