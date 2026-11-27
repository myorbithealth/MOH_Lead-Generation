"""Push leads into the Lovable Sales Portal's Supabase `leads` table (PostgREST upsert on domain).

Also the small PostgREST helpers the outreach enrolment commands need (suppressions, contacts,
email_drafts, activities, variants, app_settings). Every call is small and retried once on a 5xx.

Uses the service-role key, which bypasses RLS; keep it server-side only.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx

from ..config import portal_http, settings
from ..models import Lead
from ..outreach.models import suppression_update

log = logging.getLogger(__name__)

PAGE_SIZE = 1000  # PostgREST's default max-rows
IN_CHUNK = 100  # values per in.(...) filter, keeps URLs short
# Contact fields a rep may have corrected in the portal; enrolment never overwrites them.
_REP_OWNED_CONTACT_FIELDS = {"first_name", "last_name", "title", "owner_id", "company_id", "phone"}


def _in(values: list[Any]) -> str:
    """PostgREST in.() filter with every value double-quoted (emails contain dots and @)."""
    quoted = ",".join('"' + str(v).replace('"', '\\"') + '"' for v in values)
    return f"in.({quoted})"


def _expiry(row: dict[str, Any]) -> datetime | None:
    """A suppressions row's expires_at as a datetime (rows carry it as an ISO string)."""
    value = row.get("expires_at")
    return datetime.fromisoformat(value) if isinstance(value, str) else value


class SupabaseSink:
    def __init__(
        self,
        url: str | None = None,
        key: str | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        retry_delay: float = 1.0,
    ):
        base_url, headers = portal_http(url, key)
        self.url = base_url
        self.key = key or settings.supabase_key or settings.portal_token
        self.retry_delay = retry_delay
        self.client = httpx.Client(base_url=base_url, headers=headers, timeout=60, transport=transport)

    # ------------------------------------------------------------------ transport
    def _call(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """One request, retried once on a 5xx or a connection error. Raises on any other failure."""
        for attempt in (1, 2):
            try:
                r = self.client.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                if attempt == 2:
                    raise RuntimeError(f"supabase {method} {path} unreachable: {exc}") from exc
            else:
                if r.status_code < 300:
                    return r
                if r.status_code < 500 or attempt == 2:
                    raise RuntimeError(f"supabase {method} {path} failed {r.status_code}: {r.text[:400]}")
            time.sleep(self.retry_delay)
        raise AssertionError("unreachable")

    def _get(self, path: str, params: dict[str, Any]) -> list[dict]:
        return self._call("GET", path, params=params).json()

    def _get_all(self, path: str, params: dict[str, Any]) -> list[dict]:
        out: list[dict] = []
        offset = 0
        while True:
            rows = self._get(path, {**params, "limit": PAGE_SIZE, "offset": offset})
            out.extend(rows)
            if len(rows) < PAGE_SIZE:
                return out
            offset += PAGE_SIZE

    def _insert(self, path: str, rows: list[dict], *, returning: bool = True) -> list[dict]:
        if not rows:
            return []
        prefer = "return=representation" if returning else "return=minimal"
        r = self._call("POST", path, headers={"Prefer": prefer}, json=rows)
        return r.json() if returning else []

    def _patch(self, path: str, filters: dict[str, str], fields: dict[str, Any]) -> None:
        self._call("PATCH", path, params=filters, headers={"Prefer": "return=minimal"}, json=fields)

    # ------------------------------------------------------------------ leads (existing behaviour)
    def upsert(self, leads: list[Lead], batch_size: int = 100) -> int:
        total = 0
        for i in range(0, len(leads), batch_size):
            batch = [lead.to_portal_row() for lead in leads[i : i + batch_size]]
            r = self.client.post(
                "/leads",
                params={"on_conflict": "domain"},
                headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
                json=batch,
            )
            if r.status_code >= 300:
                raise RuntimeError(f"supabase upsert failed {r.status_code}: {r.text[:400]}")
            total += len(batch)
        return total

    def promote(self, domain: str, owner_user_id: str | None = None) -> dict:
        """Create company/contact/deal records from a lead via the `promote_lead` SQL function."""
        r = self.client.post("/rpc/promote_lead", json={"_domain": domain, "_owner": owner_user_id})
        if r.status_code >= 300:
            raise RuntimeError(f"promote failed {r.status_code}: {r.text[:400]}")
        return r.json()

    def fetch_status(self, domains: list[str]) -> dict[str, str]:
        """Pull back portal-side status so local DB mirrors what reps did."""
        out: dict[str, str] = {}
        for i in range(0, len(domains), 200):
            chunk = ",".join(domains[i : i + 200])
            r = self.client.get("/leads", params={"select": "domain,status", "domain": f"in.({chunk})"})
            if r.status_code == 200:
                out.update({row["domain"]: row["status"] for row in r.json()})
        return out

    def fetch_leads(self, since: datetime | None = None, page_size: int = 500) -> Iterator[dict]:
        """Every public.leads row (select=*), paged by limit/offset in domain order.

        `since` adds updated_at=gte.<iso> when the table has that column (probed with one row,
        since an unknown column in a filter is a 400). Paging stops at the first short page.
        """
        params: dict[str, Any] = {"select": "*", "order": "domain.asc"}
        if since is not None:
            if since.tzinfo is None:
                since = since.replace(tzinfo=UTC)
            probe = self._get("/leads", {"select": "*", "limit": 1})
            if probe and "updated_at" in probe[0]:
                params["updated_at"] = f"gte.{since.isoformat()}"
            elif probe:
                log.warning("leads has no updated_at column; ignoring since=%s", since.isoformat())
        offset = 0
        while True:
            rows = self._get("/leads", {**params, "limit": page_size, "offset": offset})
            yield from rows
            if len(rows) < page_size:
                return
            offset += page_size

    def fetch_contacts_for(self, domains: list[str]) -> dict[str, list[dict]]:
        """public.contacts rows grouped by lead_domain, queried in chunks of IN_CHUNK domains."""
        out: dict[str, list[dict]] = {}
        wanted = sorted({d for d in domains if d})
        for i in range(0, len(wanted), IN_CHUNK):
            rows = self._get_all(
                "/contacts",
                {"select": "*", "order": "id.asc", "lead_domain": _in(wanted[i : i + IN_CHUNK])},
            )
            for row in rows:
                if row.get("lead_domain"):
                    out.setdefault(row["lead_domain"], []).append(row)
        return out

    def update_lead_outreach(self, domain: str, fields: dict[str, Any]) -> None:
        """Set the 0003 outreach columns (segment, enrolled_at, smartlead_campaign_id, ...) on a lead."""
        self._patch("/leads", {"domain": f"eq.{domain}"}, fields)

    def rpc(self, name: str, args: dict[str, Any] | None = None) -> Any:
        r = self._call("POST", f"/rpc/{name}", json=args or {})
        return r.json() if r.content else None

    # ------------------------------------------------------------------ suppressions
    def fetch_suppressions(self) -> list[dict]:
        return self._get_all("/suppressions", {"select": "id,email,domain,reason,expires_at"})

    def upsert_suppressions(self, rows: list[dict]) -> int:
        """Insert rows that are not there yet, and upgrade weaker ones. Returns rows written.

        Unique indexes are on lower(email) / lower(domain), which PostgREST's on_conflict cannot
        name, so we look first. An existing row for the key is patched when the new reason is
        permanent and the old one expires, or when the old one has expired (suppression_update).
        """
        existing = self.fetch_suppressions()
        by_email = {(r.get("email") or "").lower(): r for r in existing if r.get("email")}
        by_domain = {
            (r.get("domain") or "").lower(): r for r in existing if r.get("domain") and not r.get("email")
        }
        now = datetime.now(UTC)
        new, patched = [], 0
        for row in rows:
            email = (row.get("email") or "").lower() or None
            domain = (row.get("domain") or "").lower() or None
            found = by_email.get(email) if email else by_domain.get(domain)
            if found is None:
                new.append({**row, "email": email, "domain": domain})
                continue
            changes = suppression_update(found, row.get("reason") or "manual", _expiry(row), now)
            if changes:
                self._patch(
                    "/suppressions", {"id": f"eq.{found['id']}"}, {**changes, "source": row.get("source")}
                )
                patched += 1
        self._insert("/suppressions", new, returning=False)
        return len(new) + patched

    # ------------------------------------------------------------------ contacts
    def fetch_contacts_by_email(self, emails: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        lowered = sorted({e.lower() for e in emails if e})
        for i in range(0, len(lowered), IN_CHUNK):
            rows = self._get("/contacts", {"select": "id,email", "email": _in(lowered[i : i + IN_CHUNK])})
            out.update({row["email"].lower(): row for row in rows if row.get("email")})
        return out

    def upsert_contacts(self, rows: list[dict]) -> dict[str, str]:
        """Insert or patch contacts matched on email. Returns {email: contact id}.

        Emails are stored lowercased so an eq filter finds what lower(email) indexes.
        """
        rows = [{**r, "email": r["email"].lower()} for r in rows if r.get("email")]
        existing = self.fetch_contacts_by_email([r["email"] for r in rows])
        ids: dict[str, str] = {}
        new = []
        for row in rows:
            found = existing.get(row["email"])
            if found:
                update = {
                    k: v
                    for k, v in row.items()
                    if k != "email" and v is not None and k not in _REP_OWNED_CONTACT_FIELDS
                }
                self._patch("/contacts", {"id": f"eq.{found['id']}"}, update)
                ids[row["email"]] = found["id"]
            else:
                new.append(row)
        for created in self._insert("/contacts", new):
            ids[created["email"].lower()] = created["id"]
        return ids

    # ------------------------------------------------------------------ email drafts
    def fetch_email_drafts(self, smartlead_lead_ids: list[int]) -> list[dict]:
        """Sequence-step sends for these Smartlead leads.

        Reply drafts are also direction 'outbound' and carry smartlead_lead_id, but have no
        sequence_step; they are left out because callers key rows by (lead id, step).
        """
        out: list[dict] = []
        ids = sorted(set(smartlead_lead_ids))
        cols = (
            "id,smartlead_lead_id,sequence_step,direction,status,sent_at,opened_at,replied_at,"
            "clicked_at,bounced_at,variant_id"
        )
        for i in range(0, len(ids), IN_CHUNK):
            out += self._get(
                "/email_drafts",
                {
                    "select": cols,
                    "smartlead_lead_id": _in(ids[i : i + IN_CHUNK]),
                    "direction": "eq.outbound",
                    "sequence_step": "not.is.null",
                },
            )
        return out

    def insert_email_drafts(self, rows: list[dict]) -> int:
        """Insert outbound drafts not already present for (smartlead_lead_id, sequence_step).

        The unique index is partial (where smartlead_lead_id is not null), which PostgREST's
        on_conflict cannot target, so existing rows are looked up first.
        """
        lead_ids = [r["smartlead_lead_id"] for r in rows if r.get("smartlead_lead_id")]
        existing = {(d["smartlead_lead_id"], d["sequence_step"]) for d in self.fetch_email_drafts(lead_ids)}
        new = [
            r
            for r in rows
            if not r.get("smartlead_lead_id")
            or (r["smartlead_lead_id"], r.get("sequence_step")) not in existing
        ]
        self._insert("/email_drafts", new, returning=False)
        return len(new)

    def update_email_draft(self, draft_id: str, fields: dict[str, Any]) -> None:
        self._patch("/email_drafts", {"id": f"eq.{draft_id}"}, fields)

    # ------------------------------------------------------------------ activities
    def insert_activity(self, row: dict[str, Any]) -> None:
        """activities(kind, body, company_id, contact_id, deal_id, meeting_id, actor_id)."""
        self._insert("/activities", [row], returning=False)

    # ------------------------------------------------------------------ variants
    def fetch_variants(self, segment: str | None = None) -> list[dict]:
        params: dict[str, Any] = {"select": "*"}
        if segment:
            params["segment"] = f"eq.{segment}"
        return self._get_all("/variants", params)

    def upsert_variants(self, rows: list[dict]) -> dict[tuple[str, int, str], str]:
        """Create missing variants; refresh copy and Smartlead ids on existing ones.

        Existing rows keep their id, status and counters (the learn job owns those).
        Returns {(segment, sequence_step, name): variant id}.
        """
        existing = {(v["segment"], int(v["sequence_step"]), v["name"]): v for v in self.fetch_variants()}
        ids: dict[tuple[str, int, str], str] = {}
        new = []
        copy_fields = (
            "angle",
            "subject",
            "body",
            "hypothesis",
            "smartlead_campaign_id",
            "smartlead_variant_label",
        )
        for row in rows:
            key = (row["segment"], int(row["sequence_step"]), row["name"])
            found = existing.get(key)
            if found is None:
                new.append(row)
                ids[key] = row["id"]
                continue
            ids[key] = found["id"]
            changed = {f: row[f] for f in copy_fields if f in row and row[f] != found.get(f)}
            if changed:
                self._patch("/variants", {"id": f"eq.{found['id']}"}, changed)
        self._insert("/variants", new, returning=False)
        return ids

    # ------------------------------------------------------------------ app settings
    def get_app_setting(self, key: str) -> Any:
        rows = self._get("/app_settings", {"select": "value", "key": f"eq.{key}"})
        return rows[0]["value"] if rows else None

    def set_app_setting(self, key: str, value: Any) -> None:
        self._call(
            "POST",
            "/app_settings",
            params={"on_conflict": "key"},
            headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
            json=[{"key": key, "value": value, "updated_at": datetime.now(UTC).isoformat()}],
        )
