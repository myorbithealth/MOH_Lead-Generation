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

from .enrich.tech import canonical_platform, platform_spellings
from .models import Candidate, Lead

_SCHEMA = """
create table if not exists leads (
  domain        text primary key,
  source        text not null,
  status        text not null default 'new',
  score         integer not null default 0,
  enriched      integer not null default 0,
  attempts      integer not null default 0,
  next_retry_at text,
  synced_at     text,
  created_at    text not null,
  updated_at    text not null,
  data          text not null
);
create index if not exists leads_source_idx on leads(source);
create index if not exists leads_enriched_idx on leads(enriched);
"""


# Columns added after the first release; older leads.db files get them on open.
_ADDED_COLUMNS = {
    "attempts": "integer not null default 0",
    "next_retry_at": "text",
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sortable(dt: datetime | None) -> str | None:
    """Fixed-width UTC timestamp so SQLite can compare retry times as plain strings."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_empty(value) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _richness(lead: Lead) -> tuple:
    """Which of two rows describing one business holds better evidence."""
    return (lead.last_enriched_at is not None, len(lead.pages_crawled), lead.score)


def merge_leads(a: Lead, b: Lead, domain: str) -> Lead:
    """Combine two rows for the same business: the richer one wins, the other fills its gaps."""
    base, other = (a, b) if _richness(a) >= _richness(b) else (b, a)
    merged = base.model_copy(deep=True)
    merged.domain = domain
    for name in Lead.model_fields:
        if name in ("domain", "also_found_via", "source_detail"):
            continue
        if _is_empty(getattr(merged, name)) and not _is_empty(getattr(other, name)):
            setattr(merged, name, getattr(other, name))
    for key, value in other.source_detail.items():
        merged.source_detail.setdefault(key, value)
    for via in [*other.also_found_via, other.discovered_via]:
        if via and via != merged.discovered_via and via not in merged.also_found_via:
            merged.also_found_via.append(via)
    merged.first_seen_at = min(a.first_seen_at, b.first_seen_at)
    return merged


class LeadStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        # timeout + WAL: a second process (a webhook worker, an overlapping run) waits for the
        # writer instead of failing with "database is locked", and readers never block writers.
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("pragma journal_mode=wal")
        self.conn.executescript(_SCHEMA)
        self._add_missing_columns()

    def _add_missing_columns(self) -> None:
        have = {row["name"] for row in self.conn.execute("pragma table_info(leads)")}
        for name, ddl in _ADDED_COLUMNS.items():
            if name not in have:
                self.conn.execute(f"alter table leads add column {name} {ddl}")
        self.conn.execute("create index if not exists leads_retry_idx on leads(next_retry_at)")
        self.conn.commit()

    # ------------------------------------------------------------ candidates
    def add_candidate(self, cand: Candidate) -> bool:
        """Insert a new candidate domain. Returns False if it already existed.

        A known domain found again by a different source is recorded in also_found_via:
        independent sources agreeing is evidence the lead is real.
        """
        detail = dict(cand.source_detail)
        if detail.get("platform"):
            detail["platform"] = canonical_platform(detail["platform"])
        existing = self.get(cand.domain)
        if existing:
            self._corroborate(existing, cand.discovered_via)
            return False
        lead = Lead(
            domain=cand.domain,
            website=f"https://{cand.domain}",
            source=cand.source,
            discovered_via=cand.discovered_via,
            source_detail=detail,
        )
        self.save(lead)
        return True

    def _corroborate(self, lead: Lead, via: str) -> None:
        if not via or via == lead.discovered_via or via in lead.also_found_via:
            return
        lead.also_found_via.append(via)
        # Write the JSON only: also_found_via is local evidence, so it should not mark the row
        # as changed for the portal sync.
        self.conn.execute("update leads set data=? where domain=?", (lead.model_dump_json(), lead.domain))
        self.conn.commit()

    def rename(self, old: str, new: str) -> Lead | None:
        """Move a row to a new domain (a platform tenant re-keyed to its brand domain).

        If the new domain already has a row, the two are merged: the richer evidence wins, gaps
        are filled from the other, and both discovered_via values are kept.
        """
        moving = self.get(old)
        if moving is None or old == new:
            return self.get(new)
        target = self.get(new)
        if target is None:
            merged = moving.model_copy(deep=True)
            merged.domain = new
        else:
            merged = merge_leads(target, moving, new)
        merged.source_detail["rekeyed_from"] = old
        merged.source_detail.pop("rekey_to", None)
        self.conn.execute("delete from leads where domain=?", (old,))
        self.save(merged)
        return merged

    # ------------------------------------------------------------ leads
    def save(self, lead: Lead, *, synced: bool = False) -> None:
        now = _now()
        data = lead.model_dump_json()
        existing = self.conn.execute(
            "select created_at, synced_at from leads where domain=?", (lead.domain,)
        ).fetchone()
        created_at = existing["created_at"] if existing else now
        synced_at = now if synced else (existing["synced_at"] if existing else None)
        self.conn.execute(
            """insert into leads(domain, source, status, score, enriched, attempts, next_retry_at,
                                 synced_at, created_at, updated_at, data)
               values(?,?,?,?,?,?,?,?,?,?,?)
               on conflict(domain) do update set source=excluded.source, status=excluded.status,
                 score=excluded.score, enriched=excluded.enriched, attempts=excluded.attempts,
                 next_retry_at=excluded.next_retry_at, synced_at=excluded.synced_at,
                 updated_at=excluded.updated_at, data=excluded.data""",
            (
                lead.domain,
                lead.source,
                lead.status,
                lead.score,
                1 if lead.last_enriched_at else 0,
                lead.attempts,
                _sortable(lead.next_retry_at),
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
        retry_due_only: bool = False,
        now: datetime | None = None,
    ) -> Iterator[Lead]:
        """Leads matching the filters, best score first.

        enriched=False means "due for a crawl": never attempted, or a failed crawl whose
        back-off has expired. Rows still backing off or parked are left out.
        retry_due_only=True selects only the failed crawls whose back-off has expired.
        """
        due = _sortable(now or datetime.now(UTC))
        sql = "select data from leads where 1=1"
        args: list = []
        if source:
            sql += " and source=?"
            args.append(source)
        if platform:
            # the seeding platform (source_detail.platform) or the fingerprinted one, any spelling
            clauses = []
            for name in platform_spellings(platform):
                clauses.append(
                    "json_extract(data,'$.source_detail.platform') like ?"
                    " or json_extract(data,'$.competitor_platform') like ?"
                )
                args += [f"{name}%", f"{name}%"]
            sql += " and (" + " or ".join(clauses) + ")"
        if retry_due_only:
            sql += " and next_retry_at is not null and next_retry_at <= ?"
            args.append(due)
        elif enriched is False:
            sql += " and ((enriched=0 and attempts=0) or (next_retry_at is not null and next_retry_at <= ?))"
            args.append(due)
        elif enriched is True:
            sql += " and enriched=1"
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

    def requeue_legacy_failures(self, now: datetime | None = None) -> int:
        """Put rows that older versions marked enriched after an empty crawl back in the queue.

        Before retry state existed, an unreachable site got last_enriched_at anyway and was never
        crawled again. Each such row becomes one failed attempt that is due now.
        """
        rows = self.conn.execute(
            "select data from leads where enriched=1 and attempts=0"
            " and json_array_length(json_extract(data,'$.pages_crawled'))=0"
        ).fetchall()
        when = now or datetime.now(UTC)
        for row in rows:
            lead = Lead.model_validate_json(row["data"])
            lead.last_enriched_at = None
            lead.attempts = 1
            lead.last_attempt_at = when
            lead.next_retry_at = when
            lead.last_error_kind = lead.last_error_kind or "other"
            self.save(lead)
        return len(rows)

    def stats(self) -> dict:
        out: dict = {"total": 0, "by_source": {}, "enriched": 0, "unsynced": 0, "score_buckets": {}}
        for row in self.conn.execute("select source, count(*) c from leads group by source"):
            out["by_source"][row["source"]] = row["c"]
            out["total"] += row["c"]
        out["enriched"] = self.conn.execute("select count(*) from leads where enriched=1").fetchone()[0]
        out["unsynced"] = self.conn.execute(
            "select count(*) from leads where synced_at is null or synced_at < updated_at"
        ).fetchone()[0]
        out["retry_scheduled"] = self.conn.execute(
            "select count(*) from leads where next_retry_at is not null"
        ).fetchone()[0]
        out["parked"] = self.conn.execute(
            "select count(*) from leads where enriched=0 and attempts>0 and next_retry_at is null"
        ).fetchone()[0]
        for lo, hi in ((0, 30), (30, 60), (60, 80), (80, 101)):
            n = self.conn.execute(
                "select count(*) from leads where score>=? and score<?", (lo, hi)
            ).fetchone()[0]
            out["score_buckets"][f"{lo}-{hi - 1}"] = n
        return out

    def export_json(self) -> list[dict]:
        return [
            json.loads(r["data"]) for r in self.conn.execute("select data from leads order by score desc")
        ]
