"""Thin PostgREST helpers for the reply side (classify, route, report, learn).

Why a separate module: sinks/supabase.py is the lead upsert path and is being extended by the
enrolment work at the same time; the reply side only needs plain select / insert / patch / rpc
against the Sales Portal, so it talks to PostgREST directly with the same env settings.

Uses the service-role key (bypasses RLS). Keep it server-side only. Every call retries once on a
5xx or a connection error; anything else raises PortalError so the caller can record the failure.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from ..config import settings
from .models import suppression_update

log = logging.getLogger(__name__)

PAGE_SIZE = 1000  # PostgREST's default max-rows; select_all pages past it


class PortalError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"portal {status}: {message}")
        self.status = status


class PortalClient:
    """select / insert / patch / rpc over the portal's PostgREST API.

    `filters` use PostgREST syntax as plain strings, e.g. {"processed_at": "is.null",
    "provider": "eq.smartlead"}. Pass a list of strings for repeated keys
    (e.g. {"created_at": ["gte.2026-01-01", "lt.2026-02-01"]}).
    """

    def __init__(
        self,
        url: str | None = None,
        key: str | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        retry_delay: float = 1.0,
    ):
        self.url = (url or settings.supabase_url).rstrip("/")
        self.key = key or settings.supabase_key or settings.supabase_anon_key
        if not self.url or not self.key:
            raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required for the portal")
        headers = {"apikey": self.key, "Content-Type": "application/json"}
        if not self.key.startswith("sb_"):  # legacy JWT keys also go in Authorization
            headers["Authorization"] = f"Bearer {self.key}"
        self.retry_delay = retry_delay
        self.client = httpx.Client(
            base_url=f"{self.url}/rest/v1", headers=headers, timeout=60, transport=transport
        )

    # ------------------------------------------------------------------ transport
    def _send(self, method: str, path: str, **kwargs) -> httpx.Response:
        """One request with a single retry on 5xx / network errors."""
        for attempt in (1, 2):
            try:
                r = self.client.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                if attempt == 2:
                    raise PortalError(0, str(exc)) from exc
                time.sleep(self.retry_delay)
                continue
            if r.status_code >= 500 and attempt == 1:
                log.info("portal %s %s -> %s, retrying once", method, path, r.status_code)
                time.sleep(self.retry_delay)
                continue
            if r.status_code >= 300:
                raise PortalError(r.status_code, r.text[:400])
            return r
        raise AssertionError("unreachable")

    @staticmethod
    def _params(filters: dict[str, Any] | None) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for key, value in (filters or {}).items():
            values = value if isinstance(value, list | tuple) else [value]
            out.extend((key, str(v)) for v in values)
        return out

    # ------------------------------------------------------------------ verbs
    def select(
        self,
        table: str,
        filters: dict[str, Any] | None = None,
        *,
        columns: str = "*",
        order: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict]:
        params = self._params(filters) + [("select", columns)]
        if order:
            params.append(("order", order))
        if limit is not None:
            params.append(("limit", str(limit)))
        if offset:
            params.append(("offset", str(offset)))
        return self._send("GET", f"/{table}", params=params).json()

    def select_all(self, table: str, filters: dict[str, Any] | None = None, **kwargs) -> list[dict]:
        """Every matching row, paging past PostgREST's 1000-row cap."""
        rows: list[dict] = []
        while True:
            page = self.select(table, filters, limit=PAGE_SIZE, offset=len(rows), **kwargs)
            rows.extend(page)
            if len(page) < PAGE_SIZE:
                return rows

    def insert(self, table: str, rows: dict | list[dict], *, on_conflict: str | None = None) -> list[dict]:
        """Insert (or upsert when `on_conflict` names a unique column list); returns the rows."""
        prefer = "return=representation"
        params = []
        if on_conflict:
            prefer += ",resolution=merge-duplicates"
            params.append(("on_conflict", on_conflict))
        r = self._send("POST", f"/{table}", params=params, json=rows, headers={"Prefer": prefer})
        return r.json() if r.content else []

    def patch(self, table: str, filters: dict[str, Any], values: dict) -> list[dict]:
        if not filters:
            raise ValueError("refusing to patch a whole table without filters")
        r = self._send(
            "PATCH",
            f"/{table}",
            params=self._params(filters),
            json=values,
            headers={"Prefer": "return=representation"},
        )
        return r.json() if r.content else []

    def rpc(self, function: str, args: dict | None = None) -> Any:
        r = self._send("POST", f"/rpc/{function}", json=args or {})
        return r.json() if r.content else None


# ---------------------------------------------------------------------- helpers
def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def first(rows: list[dict]) -> dict | None:
    return rows[0] if rows else None


def get_app_setting(portal, key: str) -> dict:
    """app_settings.value for `key`, or {} when the row is missing."""
    row = first(portal.select("app_settings", {"key": f"eq.{key}"}, columns="value"))
    return (row or {}).get("value") or {}


def list_rep_rules(portal) -> list[dict]:
    return portal.select("rep_rules", {"active": "is.true"}, order="priority.asc,id.asc")


def get_lead(portal, domain: str) -> dict | None:
    if not domain:
        return None
    return first(portal.select("leads", {"domain": f"eq.{domain.lower()}"}))


def find_contact_by_email(portal, email: str) -> dict | None:
    """Case-insensitive exact match (ilike treats '_' as a wildcard, so re-check in Python)."""
    if not email:
        return None
    rows = portal.select("contacts", {"email": f"ilike.{email}"})
    return next((r for r in rows if (r.get("email") or "").lower() == email.lower()), None)


def get_contact(portal, contact_id: str) -> dict | None:
    if not contact_id:
        return None
    return first(portal.select("contacts", {"id": f"eq.{contact_id}"}))


def find_outbound_draft(
    portal,
    *,
    smartlead_lead_id: Any = None,
    sequence_step: Any = None,
    contact_id: str | None = None,
    to_email: str | None = None,
) -> dict | None:
    """The outbound email a reply answers: exact Smartlead step first, then the latest send."""
    base = {"direction": "eq.outbound"}
    attempts: list[dict] = []
    if smartlead_lead_id and sequence_step:
        attempts.append(
            {**base, "smartlead_lead_id": f"eq.{smartlead_lead_id}", "sequence_step": f"eq.{sequence_step}"}
        )
    if smartlead_lead_id:
        attempts.append({**base, "smartlead_lead_id": f"eq.{smartlead_lead_id}"})
    if contact_id:
        attempts.append({**base, "contact_id": f"eq.{contact_id}"})
    if to_email:
        attempts.append({**base, "to_email": f"ilike.{to_email}"})
    for filters in attempts:
        row = first(
            portal.select("email_drafts", filters, order="sent_at.desc.nullslast,created_at.desc", limit=1)
        )
        if row:
            return row
    return None


def unprocessed_reply_events(portal, limit: int = 200) -> list[dict]:
    return portal.select(
        "webhook_events",
        {"provider": "eq.smartlead", "event_type": "eq.EMAIL_REPLY", "processed_at": "is.null"},
        order="received_at.asc",
        limit=limit,
    )


def mark_event(portal, event_id: str, error: str | None = None) -> None:
    """processed_at on success; error (and processed_at left null) on failure so the sweep retries."""
    values = {"error": error[:1000]} if error else {"processed_at": now_iso(), "error": None}
    portal.patch("webhook_events", {"id": f"eq.{event_id}"}, values)


def classification_for_event(portal, event_id: str) -> dict | None:
    return first(portal.select("reply_classifications", {"webhook_event_id": f"eq.{event_id}"}))


def insert_ignoring_duplicates(portal, table: str, row: dict) -> dict | None:
    """Insert; a unique-index conflict (409) means it is already there, which is fine on a retry."""
    try:
        return first(portal.insert(table, row))
    except PortalError as exc:
        if exc.status == 409:
            return None
        raise


def add_suppression(
    portal,
    *,
    email: str | None,
    domain: str | None,
    reason: str,
    source: str,
    expires_at: datetime | None = None,
) -> None:
    """One suppressions row (email-level when email is given, else domain-level), idempotently.

    An existing row for the same key is upgraded, not skipped, when the new reason is permanent
    and the old row expires, or when the old row has already expired (see suppression_update).
    """
    if email:
        rows = portal.select("suppressions", {"email": f"ilike.{email}"}, limit=5)
        existing = [r for r in rows if (r.get("email") or "").lower() == email.lower()]
    elif domain:
        existing = portal.select("suppressions", {"domain": f"ilike.{domain}", "email": "is.null"}, limit=5)
    else:
        return
    if existing:
        changes = suppression_update(existing[0], reason, expires_at, datetime.now(UTC))
        if changes:
            portal.patch("suppressions", {"id": f"eq.{existing[0]['id']}"}, {**changes, "source": source})
        return
    insert_ignoring_duplicates(
        portal,
        "suppressions",
        {
            "email": email.lower() if email else None,
            "domain": domain.lower() if domain else None,
            "reason": reason,
            "source": source,
            "expires_at": expires_at.isoformat() if expires_at else None,
        },
    )


def open_task_counts(portal, rep_ids: list[str], days: int = 7) -> dict[str, int]:
    """Open tasks per rep created in the last `days` days (the assign_rep fallback's load measure)."""
    if not rep_ids:
        return {}
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    rows = portal.select_all(
        "tasks",
        {"assignee_id": f"in.({','.join(rep_ids)})", "done": "is.false", "created_at": f"gte.{since}"},
        columns="assignee_id",
    )
    counts = dict.fromkeys(rep_ids, 0)
    for row in rows:
        if row.get("assignee_id") in counts:
            counts[row["assignee_id"]] += 1
    return counts
