"""Small Cal.com API v2 client: event types, team round-robin event types, bookings, webhook check.

Paths and headers copied from https://cal.com/docs/api-reference/v2 on 2026-11-27:
  GET  /v2/event-types                 cal-api-version: 2026-06-12
  POST /v2/teams/{teamId}/event-types  cal-api-version: 2026-06-12 (team admin; schedulingType
                                       roundRobin, hosts [{userId, priority, weight, mandatory}],
                                       isRRWeightsEnabled - weights are ignored without it)
  GET  /v2/bookings                    cal-api-version: 2026-05-01; filters afterStart / beforeEnd /
                                       afterCreatedAt / afterUpdatedAt; cursor pagination via
                                       limit (<= 100) + cursor, response pagination.nextCursor.
Auth on every call: Authorization: Bearer cal_live_...
Unverified (no key in this sandbox): exact error bodies, and whether hosts[].priority must be the
string enum (lowest..highest, as documented) on every account.

Webhook signature: X-Cal-Signature-256 is the hex HMAC-SHA256 of the raw request body keyed by the
webhook secret (https://cal.com/help/webhooks). Without a secret Cal.com sends 'no-secret-provided'.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

import httpx

from ..config import settings

BASE_URL = "https://api.cal.com/v2"
EVENT_TYPES_VERSION = "2026-06-12"
BOOKINGS_VERSION = "2026-05-01"
PRIORITIES = ("lowest", "low", "medium", "high", "highest")


class CalcomError(RuntimeError):
    pass


class CalcomClient:
    def __init__(self, api_key: str | None = None, *, transport: httpx.BaseTransport | None = None):
        key = api_key or settings.calcom_api_key
        if not key:
            raise RuntimeError("CALCOM_API_KEY is required for the Cal.com client")
        self.client = httpx.Client(
            base_url=BASE_URL, headers={"Authorization": f"Bearer {key}"}, timeout=30, transport=transport
        )

    def _call(self, method: str, path: str, version: str, **kwargs) -> dict:
        r = self.client.request(method, path, headers={"cal-api-version": version}, **kwargs)
        if r.status_code >= 300:
            raise CalcomError(f"cal.com {method} {path} -> {r.status_code}: {r.text[:300]}")
        body = r.json()
        if isinstance(body, dict) and body.get("status") == "error":
            raise CalcomError(f"cal.com {method} {path} error: {str(body)[:300]}")
        return body

    def list_event_types(self, username: str | None = None) -> list[dict]:
        params = {"username": username} if username else None
        return self._call("GET", "/event-types", EVENT_TYPES_VERSION, params=params).get("data") or []

    def create_team_round_robin_event_type(
        self,
        team_id: int | str,
        slug: str,
        title: str,
        length: int,
        host_user_ids: list[int],
        *,
        weights: dict[int, int] | None = None,
        priorities: dict[int, str] | None = None,
    ) -> dict:
        """One round-robin event type per segment; weights only apply with isRRWeightsEnabled."""
        hosts = []
        for uid in host_user_ids:
            host: dict[str, Any] = {"userId": uid}
            if weights and uid in weights:
                host["weight"] = weights[uid]
            if priorities and uid in priorities:
                if priorities[uid] not in PRIORITIES:
                    raise ValueError(f"priority must be one of {PRIORITIES}")
                host["priority"] = priorities[uid]
            hosts.append(host)
        body = {
            "lengthInMinutes": length,
            "title": title,
            "slug": slug,
            "schedulingType": "roundRobin",
            "hosts": hosts,
            "isRRWeightsEnabled": bool(weights),
        }
        return (
            self._call("POST", f"/teams/{team_id}/event-types", EVENT_TYPES_VERSION, json=body).get("data")
            or {}
        )

    def list_bookings(
        self, after: str | None = None, *, status: str | None = None, max_pages: int = 50
    ) -> list[dict]:
        """Bookings starting after `after` (ISO timestamp), following the cursor to the end."""
        params: dict[str, Any] = {"limit": 100}
        if after:
            params["afterStart"] = after
        if status:
            params["status"] = status
        out: list[dict] = []
        for _ in range(max_pages):
            body = self._call("GET", "/bookings", BOOKINGS_VERSION, params=params)
            out.extend(body.get("data") or [])
            pagination = body.get("pagination") or {}
            if not pagination.get("hasMore") or not pagination.get("nextCursor"):
                break
            params["cursor"] = pagination["nextCursor"]
        return out


def verify_webhook_signature(raw_body: bytes, header_value: str | None, secret: str | None) -> bool:
    """True when X-Cal-Signature-256 is the HMAC-SHA256 hex digest of the raw body with `secret`."""
    if not secret or not header_value or header_value == "no-secret-provided":
        return False
    received = header_value.strip()
    if received.lower().startswith("sha256="):
        received = received[7:]
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received.lower())
