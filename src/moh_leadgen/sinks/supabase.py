"""Push leads into the Lovable Sales Portal's Supabase `leads` table (PostgREST upsert on domain).

Uses the service-role key, which bypasses RLS; keep it server-side only.
"""

from __future__ import annotations

import logging

import httpx

from ..config import settings
from ..models import Lead

log = logging.getLogger(__name__)


class SupabaseSink:
    def __init__(self, url: str | None = None, key: str | None = None):
        self.url = (url or settings.supabase_url).rstrip("/")
        self.key = key or settings.supabase_key
        if not self.url or not self.key:
            raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required for the Supabase sink")
        headers = {"apikey": self.key, "Content-Type": "application/json"}
        if not self.key.startswith("sb_"):
            headers["Authorization"] = f"Bearer {self.key}"
        self.client = httpx.Client(base_url=f"{self.url}/rest/v1", headers=headers, timeout=60)

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
