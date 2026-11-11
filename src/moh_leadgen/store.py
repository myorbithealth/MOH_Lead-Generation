"""Local SQLite store: the working set of leads before/after sync.

One row per domain. The full Lead model is stored as JSON so the schema can evolve
without migrations; a few columns are denormalised for filtering.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from .models import Candidate, Lead

_SCHEMA = """
create table if not exists leads (
  domain        text primary key,
  source        text not null,
  status        text not null default 'new',
  score         integer not null default 0,
  enriched      integer not null default 0,
  synced_at     text,
  created_at    text not null,
  updated_at    text not null,
  data          text not null
);
create index if not exists leads_source_idx on leads(source);
create index if not exists leads_enriched_idx on leads(enriched);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class LeadStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)

    # ------------------------------------------------------------ candidates
    def add_candidate(self, cand: Candidate) -> bool:
        """Insert a new candidate domain. Returns False if it already existed."""
        row = self.conn.execute("select 1 from leads where domain = ?", (cand.domain,)).fetchone()
        if row:
            return False
        lead = Lead(
            domain=cand.domain,
            website=f"https://{cand.domain}",
            source=cand.source,
            discovered_via=cand.discovered_via,
            source_detail=cand.source_detail,
        )
        self.save(lead)
        return True

    # ------------------------------------------------------------ leads
    def save(self, lead: Lead, *, synced: bool = False) -> None:
        now = _now()
        data = lead.model_dump_json()
        existing = self.conn.execute("select created_at, synced_at from leads where domain=?", (lead.domain,)).fetchone()
        created_at = existing["created_at"] if existing else now
        synced_at = now if synced else (existing["synced_at"] if existing else None)
        self.conn.execute(
            """insert into leads(domain, source, status, score, enriched, synced_at, created_at, updated_at, data)
               values(?,?,?,?,?,?,?,?,?)
               on conflict(domain) do update set source=excluded.source, status=excluded.status,
                 score=excluded.score, enriched=excluded.enriched, synced_at=excluded.synced_at,
                 updated_at=excluded.updated_at, data=excluded.data""",
            (
                lead.domain,
                lead.source,
                lead.status,
                lead.score,
                1 if lead.last_enriched_at else 0,
                synced_at,
                created_at,
                now,
                data,
            ),
        )
        self.conn.commit()

    def get(self, domain: str) -> Lead | None:
        row = self.conn.execute("select data from leads where domain=?", (domain,)).fetchone()
        return Lead.model_validate_json(row["data"]) if row else None

    def iter(
        self,
        *,
        source: str | None = None,
        enriched: bool | None = None,
        min_score: int | None = None,
        unsynced_only: bool = False,
        limit: int | None = None,
        platform: str | None = None,
    ) -> Iterator[Lead]:
        sql = "select data from leads where 1=1"
        args: list = []
        if source:
            sql += " and source=?"
            args.append(source)
        if platform:
            # the seeding platform (source_detail.platform) or the fingerprinted one
            sql += " and (json_extract(data,'$.source_detail.platform') like ? or json_extract(data,'$.competitor_platform') like ?)"
            args += [f"{platform}%", f"{platform}%"]
        if enriched is not None:
            sql += " and enriched=?"
            args.append(1 if enriched else 0)
        if min_score is not None:
            sql += " and score>=?"
            args.append(min_score)
        if unsynced_only:
            sql += " and (synced_at is null or synced_at < updated_at)"
        sql += " order by score desc, created_at asc"
        if limit:
            sql += f" limit {int(limit)}"
        for row in self.conn.execute(sql, args):
            yield Lead.model_validate_json(row["data"])

    def mark_synced(self, domains: list[str]) -> None:
        now = _now()
        self.conn.executemany("update leads set synced_at=? where domain=?", [(now, d) for d in domains])
        self.conn.commit()

    def stats(self) -> dict:
        out: dict = {"total": 0, "by_source": {}, "enriched": 0, "unsynced": 0, "score_buckets": {}}
        for row in self.conn.execute("select source, count(*) c from leads group by source"):
            out["by_source"][row["source"]] = row["c"]
            out["total"] += row["c"]
        out["enriched"] = self.conn.execute("select count(*) from leads where enriched=1").fetchone()[0]
        out["unsynced"] = self.conn.execute(
            "select count(*) from leads where synced_at is null or synced_at < updated_at"
        ).fetchone()[0]
        for lo, hi in ((0, 30), (30, 60), (60, 80), (80, 101)):
            n = self.conn.execute("select count(*) from leads where score>=? and score<?", (lo, hi)).fetchone()[0]
            out["score_buckets"][f"{lo}-{hi - 1}"] = n
        return out

    def export_json(self) -> list[dict]:
        return [json.loads(r["data"]) for r in self.conn.execute("select data from leads order by score desc")]
