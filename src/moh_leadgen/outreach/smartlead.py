"""Smartlead REST client (https://api.smartlead.ai/reference).

Paths, payload keys and response shapes were copied from the endpoint reference pages on
2026-11-27. Where the reference pages and the guides disagree, the reference page wins and the
choice is noted on the method.

Authentication is only the `api_key` query parameter, so the key appears in every URL. httpx logs
request URLs at INFO, so this module installs a log filter that masks the key.

Rate limits: 200 requests/minute per account, shared by every endpoint. A token bucket keeps us at
180/minute so the vendor's 429 is a backstop, not the limiter. 429 and 5xx are retried 3 times.
"""

from __future__ import annotations

import logging
import random
import re
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from ..config import settings

log = logging.getLogger(__name__)

MAX_LEADS_PER_CALL = 1000  # add-leads rejects larger arrays with 400 and imports nothing
DEFAULT_LEAD_BATCH = 100
PAGE_SIZE = 100  # leads and email-accounts listings cap `limit` at 100

# Events from the "Webhook Events Reference" page (events.md). The older guide names
# (EMAIL_REPLIED, LEAD_REPLIED, ...) are not used.
WEBHOOK_EVENTS = [
    "EMAIL_SENT",
    "EMAIL_OPEN",
    "EMAIL_LINK_CLICK",
    "EMAIL_REPLY",
    "EMAIL_BOUNCE",
    "LEAD_UNSUBSCRIBED",
    "LEAD_CATEGORY_UPDATED",
]

_KEY_RE = re.compile(r"(api_key=)[^&\s'\"]+")


class _MaskApiKey(logging.Filter):
    """Replace api_key=<value> with api_key=*** in any log record (httpx logs full URLs)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args:
            record.args = tuple(_KEY_RE.sub(r"\1***", str(a)) for a in record.args)
        record.msg = _KEY_RE.sub(r"\1***", str(record.msg))
        return True


for _name in ("httpx", "httpcore", __name__):
    logging.getLogger(_name).addFilter(_MaskApiKey())


class SmartleadError(RuntimeError):
    """A Smartlead call failed after retries, or returned a 4xx we should not retry."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class TokenBucket:
    """At most `per_minute` calls in any rolling minute, refilled continuously."""

    def __init__(
        self,
        per_minute: int = 180,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.capacity = float(per_minute)
        self.tokens = float(per_minute)
        self.rate = per_minute / 60.0
        self.clock = clock
        self.sleep = sleep
        self.updated = clock()
        self.lock = threading.Lock()

    def acquire(self) -> None:
        with self.lock:
            now = self.clock()
            self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
            self.updated = now
            if self.tokens < 1:
                wait = (1 - self.tokens) / self.rate
                self.sleep(wait)
                self.tokens = 1.0
                self.updated = self.clock()
            self.tokens -= 1


def with_query_token(url: str, token: str) -> str:
    """Append ?token=<secret> to a webhook URL.

    Smartlead's campaign webhook endpoint has no field for a signing secret, so the Edge Function
    accepts the shared secret as a query parameter (it also accepts payload.secret_key or an
    X-Smartlead-Signature HMAC when Smartlead sends one).
    """
    if not token:
        return url
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "token"] + [("token", token)]
    return urlunsplit(parts._replace(query=urlencode(query)))


def _chunks(items: list, size: int) -> Iterator[list]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


class SmartleadClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        per_minute: int = 180,
        max_tries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = 60,
    ):
        self.api_key = api_key if api_key is not None else settings.smartlead_api_key
        if not self.api_key:
            raise RuntimeError("SMARTLEAD_API_KEY is required for Smartlead calls")
        self.base_url = (base_url or settings.smartlead_base_url).rstrip("/")
        self.max_tries = max_tries
        self.sleep = sleep
        self.bucket = TokenBucket(per_minute, sleep=sleep)
        self.http = httpx.Client(base_url=self.base_url, transport=transport, timeout=timeout)

    # ------------------------------------------------------------------ transport
    def _retry_wait(self, response: httpx.Response | None, attempt: int) -> float:
        """Seconds to wait before the next try.

        A 429 from the account limiter carries Retry-After (always 60, the window length) and
        X-RateLimit-Reset (when the window really ends); the shorter of the two is enough.
        Endpoint-specific 429s carry neither, so they wait the documented 60 s window.
        """
        if response is not None and response.status_code == 429:
            waits = []
            retry_after = response.headers.get("Retry-After")
            if retry_after and retry_after.replace(".", "", 1).isdigit():
                waits.append(float(retry_after))
            reset = response.headers.get("X-RateLimit-Reset")
            if reset and reset.isdigit():
                waits.append(max(0.0, float(reset) - time.time()))
            return min(waits) if waits else 60.0
        return (2**attempt) + random.uniform(0, 1)

    def request(self, method: str, path: str, *, params: dict | None = None, json: Any = None) -> Any:
        """One API call with rate limiting and retries. Returns the decoded JSON body."""
        query = {k: v for k, v in (params or {}).items() if v is not None}
        query["api_key"] = self.api_key
        last: str = ""
        for attempt in range(self.max_tries):
            self.bucket.acquire()
            response: httpx.Response | None = None
            try:
                response = self.http.request(method, path, params=query, json=json)
            except httpx.TransportError as exc:
                last = f"{type(exc).__name__}"
            else:
                if response.status_code < 400:
                    return response.json() if response.content else {}
                last = f"{response.status_code}: {response.text[:300]}"
                if response.status_code != 429 and response.status_code < 500:
                    raise SmartleadError(f"smartlead {method} {path} -> {last}", response.status_code)
            if attempt + 1 < self.max_tries:
                wait = self._retry_wait(response, attempt)
                log.info("smartlead %s %s failed (%s); retrying in %.1fs", method, path, last[:80], wait)
                self.sleep(wait)
        status = response.status_code if response is not None else None
        raise SmartleadError(f"smartlead {method} {path} failed after {self.max_tries} tries: {last}", status)

    # ------------------------------------------------------------------ campaigns
    def list_campaigns(self) -> list[dict]:
        """GET /campaigns/ - a bare JSON array, newest first."""
        data = self.request("GET", "/campaigns/")
        return data if isinstance(data, list) else data.get("data", [])

    def create_campaign(self, name: str, client_id: int | None = None) -> dict:
        """POST /campaigns/create {name, client_id} -> {ok, id, name, created_at}. Starts DRAFTED."""
        return self.request("POST", "/campaigns/create", json={"name": name, "client_id": client_id})

    def save_sequences(self, campaign_id: int, sequences: list[dict]) -> dict:
        """POST /campaigns/{id}/sequences {sequences: [...]}.

        Each sequence: {id (null creates), seq_number, seq_delay_details: {delay_in_days}, subject,
        email_body} or, for A/B steps, {variant_distribution_type, seq_variants: [{subject,
        email_body, variant_label, ...}]}. delay_in_days is the gap from the previous step.
        Smartlead refuses changes while the campaign is ACTIVE.
        """
        return self.request("POST", f"/campaigns/{campaign_id}/sequences", json={"sequences": sequences})

    def get_sequences(self, campaign_id: int) -> list[dict]:
        data = self.request("GET", f"/campaigns/{campaign_id}/sequences")
        return data if isinstance(data, list) else data.get("data", [])

    def update_settings(
        self,
        campaign_id: int,
        *,
        stop_on_reply: bool = True,
        track_opens: bool = True,
        track_clicks: bool = False,
        unsubscribe_text: str = "",
        plain_text: bool = True,
        follow_up_percentage: int = 100,
        name: str | None = None,
    ) -> dict:
        """POST /campaigns/{id}/settings.

        track_settings lists what NOT to track (empty = track everything). The reference page
        lists stop_lead_settings values CLICK_ON_A_LINK / OPEN_AN_EMAIL, while its overview and
        the older docs use REPLY_TO_AN_EMAIL; stop-on-reply is Smartlead's default either way.
        """
        track: list[str] = []
        if not track_opens:
            track.append("DONT_TRACK_EMAIL_OPEN")
        if not track_clicks:
            track.append("DONT_TRACK_LINK_CLICK")
        payload: dict[str, Any] = {
            "track_settings": track,
            "unsubscribe_text": unsubscribe_text,
            "send_as_plain_text": plain_text,
            "follow_up_percentage": follow_up_percentage,
            "add_unsubscribe_tag": True,
            "auto_pause_domain_leads_on_reply": True,
            "out_of_office_detection_settings": {
                "ignoreOOOasReply": True,
                # Smartlead rejects both flags together; we categorise OOO and let our own
                # classifier snooze/resume the lead (process_replies), so no vendor auto-reactivation.
                "autoReactivateOOO": False,
                "autoCategorizeOOO": True,
            },
        }
        if stop_on_reply:
            payload["stop_lead_settings"] = "REPLY_TO_AN_EMAIL"
        if name:
            payload["name"] = name
        return self.request("POST", f"/campaigns/{campaign_id}/settings", json=payload)

    def update_schedule(
        self,
        campaign_id: int,
        *,
        timezone: str,
        days: list[int],
        start_hour: str,
        end_hour: str,
        min_minutes_between: int,
        max_new_leads_per_day: int,
    ) -> dict:
        """POST /campaigns/{id}/schedule.

        Flat body as in the campaign-setup guide (days_of_the_week, max_new_leads_per_day). The
        reference page shows the same keys wrapped in {"schedule": {...}} with "days" and no
        daily cap; unverified which one the server accepts.
        """
        payload = {
            "timezone": timezone,
            "days_of_the_week": days,
            "start_hour": start_hour,
            "end_hour": end_hour,
            "min_time_btw_emails": min_minutes_between,
            "max_new_leads_per_day": max_new_leads_per_day,
        }
        return self.request("POST", f"/campaigns/{campaign_id}/schedule", json=payload)

    def update_status(self, campaign_id: int, status: str) -> dict:
        """POST /campaigns/{id}/status {status: START | PAUSED | STOPPED}."""
        return self.request("POST", f"/campaigns/{campaign_id}/status", json={"status": status})

    # ------------------------------------------------------------------ email accounts
    def list_email_accounts(self) -> list[dict]:
        """GET /email-accounts/ paged by offset/limit (max 100)."""
        out: list[dict] = []
        offset = 0
        while True:
            page = self.request("GET", "/email-accounts/", params={"offset": offset, "limit": PAGE_SIZE})
            rows = page if isinstance(page, list) else page.get("data", [])
            out.extend(rows)
            if len(rows) < PAGE_SIZE:
                return out
            offset += PAGE_SIZE

    def add_email_accounts(self, campaign_id: int, account_ids: list[int]) -> dict:
        """POST /campaigns/{id}/email-accounts {email_account_ids}. Does not check mailbox health."""
        return self.request(
            "POST", f"/campaigns/{campaign_id}/email-accounts", json={"email_account_ids": account_ids}
        )

    def campaign_email_accounts(self, campaign_id: int) -> list[dict]:
        data = self.request("GET", f"/campaigns/{campaign_id}/email-accounts")
        return data if isinstance(data, list) else data.get("data", [])

    # ------------------------------------------------------------------ leads
    def add_leads(
        self,
        campaign_id: int,
        leads: list[dict],
        settings_: dict | None = None,
        batch_size: int = DEFAULT_LEAD_BATCH,
    ) -> dict:
        """POST /campaigns/{id}/leads {lead_list, settings} in batches (the server max is 1,000).

        Lead keys: email, first_name, last_name, company_name, phone_number, website, location,
        linkedin_profile, company_url, custom_fields{}.

        The documented 200 body only has counters (upload_count, already_added_to_campaign,
        block_count, ..., is_lead_limit_exhausted), no lead ids. return_lead_ids and its
        emailToLeadIdMap are undocumented, so the map is used when present and any email it
        does not cover is looked up with GET /leads/?email= (see _resolve_lead_ids). Without
        that, leads Smartlead is emailing would be recorded as "not added".

        A 200 can still report a problem (ok=false, or the lead limit is exhausted); that raises.
        Returns {"upload_count", "email_to_lead_id", "skipped", "responses"}.
        """
        size = max(1, min(batch_size, MAX_LEADS_PER_CALL))
        opts = {
            "ignore_global_block_list": False,
            "ignore_unsubscribe_list": False,
            "ignore_duplicate_leads_in_other_campaign": False,
            "ignore_community_bounce_list": False,
            "return_lead_ids": True,  # undocumented; harmless if ignored
            **(settings_ or {}),
        }
        result: dict[str, Any] = {"upload_count": 0, "email_to_lead_id": {}, "skipped": {}, "responses": []}
        for batch in _chunks(leads, size):
            data = self.request(
                "POST", f"/campaigns/{campaign_id}/leads", json={"lead_list": batch, "settings": opts}
            )
            result["responses"].append(data)
            result["upload_count"] += int(data.get("upload_count") or 0)
            ids = data.get("emailToLeadIdMap") or {}
            for bucket in ("newlyAddedLeads", "existingLeads"):
                for email, lead_id in (ids.get(bucket) or {}).items():
                    result["email_to_lead_id"][email.lower()] = int(lead_id)
            for email, lead_id in (ids.get("existingLeadsInOtherCampaigns") or {}).items():
                result["skipped"][email.lower()] = f"in_other_campaign:{lead_id}"
            if data.get("ok") is False or data.get("is_lead_limit_exhausted"):
                raise SmartleadError(f"add_leads stopped: {data.get('error') or data}")
            self._resolve_lead_ids(campaign_id, batch, result)
        return result

    def _resolve_lead_ids(self, campaign_id: int, batch: list[dict], result: dict) -> None:
        """Fill lead ids the add-leads response did not give, one lookup per email.

        An email counts as added only when Smartlead shows it in this campaign; otherwise it is
        marked skipped, with the reason the lookup shows where it can tell.
        """
        for lead in batch:
            email = str(lead.get("email") or "").lower()
            if not email or email in result["email_to_lead_id"] or email in result["skipped"]:
                continue
            found = self.get_lead_by_email(email) or {}
            campaigns = {
                int(c["campaign_id"]) for c in found.get("lead_campaign_data") or [] if c.get("campaign_id")
            }
            if found.get("id") and campaign_id in campaigns:
                result["email_to_lead_id"][email] = int(found["id"])
            elif found.get("is_unsubscribed"):
                result["skipped"][email] = "unsubscribed"
            elif campaigns:
                result["skipped"][email] = f"in_other_campaign:{min(campaigns)}"
            else:
                result["skipped"][email] = "not_added"

    def list_leads(self, campaign_id: int, **filters: Any) -> list[dict]:
        """GET /campaigns/{id}/leads paged by offset/limit (max 100). Returns the lead rows."""
        out: list[dict] = []
        offset = 0
        while True:
            page = self.request(
                "GET",
                f"/campaigns/{campaign_id}/leads",
                params={"offset": offset, "limit": PAGE_SIZE, **filters},
            )
            rows = page.get("leads") or page.get("data") or []
            out.extend(rows)
            if len(rows) < PAGE_SIZE:
                return out
            offset += PAGE_SIZE

    def get_lead_by_email(self, email: str) -> dict | None:
        """GET /leads/?email= -> the lead with lead_campaign_data[], or None ({} when not found)."""
        data = self.request("GET", "/leads/", params={"email": email})
        return data or None

    def pause_lead(self, campaign_id: int, lead_id: int) -> dict:
        return self.request("POST", f"/campaigns/{campaign_id}/leads/{lead_id}/pause")

    def resume_lead(self, campaign_id: int, lead_id: int, delay_days: int | None = None) -> dict:
        body = {"resume_lead_with_delay_days": delay_days} if delay_days is not None else {}
        return self.request("POST", f"/campaigns/{campaign_id}/leads/{lead_id}/resume", json=body)

    def unsubscribe_lead(self, campaign_id: int, lead_id: int) -> dict:
        """Stop this campaign for the lead (campaign-scoped)."""
        return self.request("POST", f"/campaigns/{campaign_id}/leads/{lead_id}/unsubscribe")

    def unsubscribe_lead_globally(self, lead_id: int) -> dict:
        """POST /leads/{id}/unsubscribe: all current and future campaigns. Cannot be undone via API."""
        return self.request("POST", f"/leads/{lead_id}/unsubscribe")

    # ------------------------------------------------------------------ categories
    def fetch_categories(self) -> list[dict]:
        """GET /leads/fetch-categories -> [{id, name, sentiment_type, created_at}]."""
        data = self.request("GET", "/leads/fetch-categories")
        return data if isinstance(data, list) else data.get("data", [])

    def set_lead_category(
        self, campaign_id: int, lead_id: int, category_id: int | None, pause: bool = False
    ) -> dict:
        return self.request(
            "POST",
            f"/campaigns/{campaign_id}/leads/{lead_id}/category",
            json={"category_id": category_id, "pause_lead": pause},
        )

    # ------------------------------------------------------------------ threads
    def message_history(self, campaign_id: int, lead_id: int, event_time_gt: str | None = None) -> dict:
        """GET /campaigns/{id}/leads/{lead_id}/message-history -> {messages: [...]}"""
        return self.request(
            "GET",
            f"/campaigns/{campaign_id}/leads/{lead_id}/message-history",
            params={"event_time_gt": event_time_gt},
        )

    def reply_to_thread(
        self, campaign_id: int, *, email_stats_id: str, email_body: str, **extra: Any
    ) -> dict:
        """POST /campaigns/{id}/reply-email-thread. email_stats_id and email_body are required;
        optional keys: to_email, to_first_name, to_last_name, scheduled_time, reply_message_id,
        reply_email_body, reply_email_time, cc, bcc, add_signature, attachments."""
        body = {"email_stats_id": email_stats_id, "email_body": email_body, **extra}
        return self.request("POST", f"/campaigns/{campaign_id}/reply-email-thread", json=body)

    # ------------------------------------------------------------------ stats
    def campaign_statistics(
        self, campaign_id: int, offset: int = 0, limit: int = 100, **filters: Any
    ) -> dict:
        """GET /campaigns/{id}/statistics: one row per email sent (limit up to 1,000).

        Rows: lead_name, lead_email, sequence_number, sent_time, is_opened, is_clicked,
        is_replied, is_bounced, sender_bounce (+ stats_id and *_time fields, not documented).
        """
        return self.request(
            "GET",
            f"/campaigns/{campaign_id}/statistics",
            params={"offset": offset, "limit": limit, **filters},
        )

    def iter_campaign_statistics(self, campaign_id: int, page_size: int = 500) -> Iterator[dict]:
        """Every statistics row of a campaign, page by page."""
        offset = 0
        while True:
            page = self.campaign_statistics(campaign_id, offset=offset, limit=page_size)
            rows = page.get("data") if isinstance(page, dict) else page
            if not isinstance(rows, list):
                return  # the doc also shows a summary object under "data"; nothing to page
            yield from rows
            if len(rows) < page_size:
                return
            offset += page_size

    def analytics(self, campaign_id: int) -> dict:
        """GET /campaigns/{id}/analytics: top-level counts for the campaign."""
        return self.request("GET", f"/campaigns/{campaign_id}/analytics")

    # ------------------------------------------------------------------ webhooks
    def list_webhooks(self, campaign_id: int) -> list[dict]:
        data = self.request("GET", f"/campaigns/{campaign_id}/webhooks")
        return data if isinstance(data, list) else data.get("data", [])

    def upsert_webhook(
        self,
        campaign_id: int,
        name: str,
        url: str,
        event_types: Iterable[str] = WEBHOOK_EVENTS,
        secret: str = "",
    ) -> dict:
        """POST /campaigns/{id}/webhooks {id, name, webhook_url, event_types}.

        Re-uses the id of an existing webhook with the same name, so re-running updates it
        instead of adding a duplicate. The secret travels as ?token= on the URL.
        """
        existing = next((w for w in self.list_webhooks(campaign_id) if w.get("name") == name), None)
        body = {
            "id": existing.get("id") if existing else None,
            "name": name,
            "webhook_url": with_query_token(url, secret),
            "event_types": list(event_types),
        }
        return self.request("POST", f"/campaigns/{campaign_id}/webhooks", json=body)

    # ------------------------------------------------------------------ block list
    def add_domains_to_block_list(self, entries: list[str], client_id: int | None = None) -> dict:
        """POST /leads/add-domain-block-list {domain_block_list}: domains or emails, account-wide."""
        body: dict[str, Any] = {"domain_block_list": entries}
        if client_id is not None:
            body["client_id"] = client_id
        return self.request("POST", "/leads/add-domain-block-list", json=body)
