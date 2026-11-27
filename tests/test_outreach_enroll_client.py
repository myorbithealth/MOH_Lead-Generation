"""Smartlead client and portal sink helpers, against httpx.MockTransport (no network)."""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from moh_leadgen.outreach.smartlead import SmartleadClient, SmartleadError, TokenBucket, with_query_token
from moh_leadgen.sinks.supabase import SupabaseSink


def _client(handler, sleeps=None):
    sleeps = sleeps if sleeps is not None else []
    return SmartleadClient(
        "test-key",
        "https://server.smartlead.ai/api/v1",
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
    )


def test_api_key_is_a_query_param_and_paths_match_the_reference():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "id": 7, "name": "x"})

    client = _client(handler)
    client.create_campaign("MOH outbound - other")
    client.set_lead_category(7, 99, 3, pause=True)
    client.add_domains_to_block_list(["spam.example"])

    assert [r.url.path for r in seen] == [
        "/api/v1/campaigns/create",
        "/api/v1/campaigns/7/leads/99/category",
        "/api/v1/leads/add-domain-block-list",
    ]
    assert all(r.url.params["api_key"] == "test-key" for r in seen)
    assert "authorization" not in {k.lower() for k in seen[0].headers}
    assert json.loads(seen[1].content) == {"category_id": 3, "pause_lead": True}
    assert json.loads(seen[2].content) == {"domain_block_list": ["spam.example"]}


def test_add_leads_batches_and_collects_lead_ids():
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        emails = [lead["email"] for lead in body["lead_list"]]
        return httpx.Response(
            200,
            json={
                "ok": True,
                "upload_count": len(emails),
                "emailToLeadIdMap": {"newlyAddedLeads": {e: i + 1 for i, e in enumerate(emails)}},
            },
        )

    leads = [{"email": f"p{i}@example.com"} for i in range(250)]
    out = _client(handler).add_leads(42, leads, batch_size=100)

    assert [len(b["lead_list"]) for b in bodies] == [100, 100, 50]
    assert bodies[0]["settings"]["return_lead_ids"] is True
    assert bodies[0]["settings"]["ignore_unsubscribe_list"] is False
    assert out["upload_count"] == 250
    assert len(out["email_to_lead_id"]) == 250


def test_add_leads_never_sends_more_than_the_server_max():
    sizes = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":  # the per-email id lookup
            return httpx.Response(200, json={})
        sizes.append(len(json.loads(request.content)["lead_list"]))
        return httpx.Response(200, json={"ok": True, "upload_count": sizes[-1]})

    _client(handler).add_leads(1, [{"email": f"{i}@x.com"} for i in range(1500)], batch_size=5000)
    assert sizes == [1000, 500]


def test_add_leads_raises_when_lead_limit_is_exhausted():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "is_lead_limit_exhausted": True, "error": "limit"})

    with pytest.raises(SmartleadError):
        _client(handler).add_leads(1, [{"email": "a@x.com"}])


def test_add_leads_looks_up_ids_when_the_response_has_no_map():
    """The documented add-leads body has counters only; ids come from GET /leads/?email=."""
    lookups = []
    known = {
        "new@x.com": {"id": 501, "lead_campaign_data": [{"campaign_id": 42}]},
        "other@x.com": {"id": 502, "lead_campaign_data": [{"campaign_id": 7}]},
        "unsub@x.com": {"id": 503, "is_unsubscribed": True, "lead_campaign_data": []},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            email = request.url.params["email"]
            lookups.append(email)
            return httpx.Response(200, json=known.get(email, {}))
        return httpx.Response(
            200, json={"ok": True, "upload_count": 1, "total_leads": 4, "skipped_in_other_campaign_count": 1}
        )

    emails = ["new@x.com", "other@x.com", "unsub@x.com", "gone@x.com"]
    out = _client(handler).add_leads(42, [{"email": e} for e in emails])
    assert lookups == emails
    assert out["email_to_lead_id"] == {"new@x.com": 501}
    assert out["skipped"] == {
        "other@x.com": "in_other_campaign:7",
        "unsub@x.com": "unsubscribed",
        "gone@x.com": "not_added",
    }


def test_add_leads_raises_when_limit_exhausted_even_if_ok():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "upload_count": 0, "is_lead_limit_exhausted": True})

    with pytest.raises(SmartleadError):
        _client(handler).add_leads(1, [{"email": "a@x.com"}])


def test_429_waits_for_retry_after_then_succeeds():
    calls = {"n": 0}
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "2"}, json={"message": "slow down"})
        return httpx.Response(200, json=[{"id": 1, "name": "c"}])

    campaigns = _client(handler, sleeps).list_campaigns()
    assert campaigns == [{"id": 1, "name": "c"}]
    assert calls["n"] == 2
    assert 2.0 in sleeps


def test_5xx_is_retried_three_times_then_raises():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, text="down")

    with pytest.raises(SmartleadError) as err:
        _client(handler).analytics(5)
    assert calls["n"] == 3
    assert err.value.status == 503


def test_4xx_is_not_retried():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, json={"error": "bad"})

    with pytest.raises(SmartleadError):
        _client(handler).pause_lead(1, 2)
    assert calls["n"] == 1


def test_list_leads_pages_until_a_short_page():
    offsets = []

    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        offsets.append(offset)
        n = 100 if offset < 200 else 7
        return httpx.Response(200, json={"total": 207, "leads": [{"id": offset + i} for i in range(n)]})

    leads = _client(handler).list_leads(9)
    assert offsets == [0, 100, 200]
    assert len(leads) == 207


def test_upsert_webhook_reuses_existing_id_and_carries_the_secret_as_token():
    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"success": True, "data": [{"id": 456, "name": "hook"}]})
        posted.append(json.loads(request.content))
        return httpx.Response(200, json={"success": True, "data": {"id": 456}})

    _client(handler).upsert_webhook(
        3, "hook", "https://x.supabase.co/functions/v1/smartlead-webhook", ["EMAIL_REPLY"], "s3cret"
    )
    assert posted[0]["id"] == 456
    assert posted[0]["event_types"] == ["EMAIL_REPLY"]
    assert posted[0]["webhook_url"].endswith("?token=s3cret")


def test_with_query_token_replaces_an_existing_token():
    assert with_query_token("https://h/p?a=1&token=old", "new") == "https://h/p?a=1&token=new"
    assert with_query_token("https://h/p", "") == "https://h/p"


def test_token_bucket_sleeps_when_empty():
    now = [0.0]
    sleeps = []
    bucket = TokenBucket(per_minute=2, clock=lambda: now[0], sleep=sleeps.append)
    bucket.acquire()
    bucket.acquire()
    bucket.acquire()
    assert sleeps and sleeps[0] == pytest.approx(30.0)


def test_api_key_is_masked_in_logs(caplog):
    logger = logging.getLogger("moh_leadgen.outreach.smartlead")
    with caplog.at_level(logging.INFO, logger="moh_leadgen.outreach.smartlead"):
        logger.info("GET %s", "https://server.smartlead.ai/api/v1/campaigns/?api_key=abc123&x=1")
    assert "abc123" not in caplog.text
    assert "api_key=***" in caplog.text


# ------------------------------------------------------------------ sink
def _sink(handler):
    return SupabaseSink(
        "https://p.supabase.co", "service-key", transport=httpx.MockTransport(handler), retry_delay=0
    )


def test_upsert_contacts_patches_existing_and_inserts_new():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request))
        if request.method == "GET":
            return httpx.Response(200, json=[{"id": "c1", "email": "old@a.com"}])
        if request.method == "PATCH":
            return httpx.Response(204)
        body = json.loads(request.content)
        return httpx.Response(201, json=[{"id": "c2", "email": body[0]["email"]}])

    ids = _sink(handler).upsert_contacts(
        [
            {"email": "OLD@a.com", "first_name": "Rep Edited", "smartlead_lead_id": 1},
            {"email": "new@a.com", "first_name": "New", "smartlead_lead_id": 2},
        ]
    )
    assert ids == {"old@a.com": "c1", "new@a.com": "c2"}
    methods = [c[0] for c in calls]
    assert methods == ["GET", "PATCH", "POST"]
    patch_body = json.loads(calls[1][2].content)
    assert "first_name" not in patch_body  # reps own names in the portal
    assert patch_body["smartlead_lead_id"] == 1
    assert calls[0][2].headers["authorization"] == "Bearer service-key"


def test_insert_email_drafts_skips_rows_already_present():
    inserted = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[{"smartlead_lead_id": 1, "sequence_step": 1}])
        inserted.extend(json.loads(request.content))
        return httpx.Response(201)

    n = _sink(handler).insert_email_drafts(
        [{"smartlead_lead_id": 1, "sequence_step": 1}, {"smartlead_lead_id": 2, "sequence_step": 1}]
    )
    assert n == 1
    assert inserted == [{"smartlead_lead_id": 2, "sequence_step": 1}]


def test_fetch_email_drafts_leaves_out_reply_drafts():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        return httpx.Response(200, json=[])

    _sink(handler).fetch_email_drafts([55])
    assert seen[0]["sequence_step"] == "not.is.null"
    assert seen[0]["direction"] == "eq.outbound"


def test_upsert_suppressions_upgrades_an_expiring_row_to_permanent():
    patches, inserted = [], []
    future = "2099-01-01T00:00:00+00:00"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 9,
                        "email": "jane@acme.com",
                        "domain": None,
                        "reason": "not_interested",
                        "expires_at": future,
                    },
                    {"id": 10, "email": None, "domain": "perm.com", "reason": "manual", "expires_at": None},
                ],
            )
        if request.method == "PATCH":
            patches.append((request.url.params["id"], json.loads(request.content)))
            return httpx.Response(204)
        inserted.extend(json.loads(request.content))
        return httpx.Response(201)

    n = _sink(handler).upsert_suppressions(
        [
            {"email": "Jane@acme.com", "reason": "unsubscribe", "source": "cli", "expires_at": None},
            {"domain": "perm.com", "reason": "not_interested", "source": "cli", "expires_at": future},
            {"domain": "new.com", "reason": "bounce", "source": "cli", "expires_at": None},
        ]
    )
    assert n == 2
    assert patches == [("eq.9", {"reason": "unsubscribe", "expires_at": None, "source": "cli"})]
    assert [r["domain"] for r in inserted] == ["new.com"]


def test_sink_retries_once_on_5xx():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(502)
        return httpx.Response(200, json=[{"value": {"daily_cap": 300}}])

    assert _sink(handler).get_app_setting("outreach") == {"daily_cap": 300}
    assert calls["n"] == 2


def test_upsert_variants_keeps_existing_ids_and_counters():
    patched, inserted = [], []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "portal-id",
                        "segment": "other",
                        "sequence_step": 1,
                        "name": "control",
                        "body": "old",
                        "sends": 40,
                    }
                ],
            )
        if request.method == "PATCH":
            patched.append(json.loads(request.content))
            return httpx.Response(204)
        inserted.extend(json.loads(request.content))
        return httpx.Response(201)

    ids = _sink(handler).upsert_variants(
        [
            {"id": "new-1", "segment": "other", "sequence_step": 1, "name": "control", "body": "new"},
            {"id": "new-2", "segment": "other", "sequence_step": 2, "name": "proof", "body": "b"},
        ]
    )
    assert ids[("other", 1, "control")] == "portal-id"
    assert patched == [{"body": "new"}]
    assert [r["id"] for r in inserted] == ["new-2"]
