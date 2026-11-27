"""In-memory stand-ins for the portal, the Anthropic client and the Smartlead client."""

from __future__ import annotations

import itertools
import json
from types import SimpleNamespace

from moh_leadgen.outreach.portal_io import PortalError


def _matches(row: dict, key: str, expr: str) -> bool:
    value = row.get(key)
    if expr == "is.null":
        return value is None
    if expr == "not.is.null":
        return value is not None
    if expr == "is.true":
        return value is True
    if expr == "is.false":
        return value is False
    op, _, arg = expr.partition(".")
    if op == "eq":
        return str(value) == arg if value is not None else False
    if op == "ilike":
        return value is not None and str(value).lower() == arg.lower()
    if op == "in":
        return value is not None and str(value) in arg.strip("()").split(",")
    if op == "gte":
        return value is not None and str(value) >= arg
    raise NotImplementedError(expr)


class FakePortal:
    def __init__(self, tables: dict[str, list[dict]] | None = None, rpc_results: dict | None = None):
        self.tables = {k: [dict(r) for r in v] for k, v in (tables or {}).items()}
        self.rpc_results = rpc_results or {}
        self.rpc_calls: list[tuple[str, dict]] = []
        self.unique: dict[str, tuple[str, ...]] = {
            "tasks": ("source",),
            "reply_classifications": ("webhook_event_id",),
        }
        self._ids = itertools.count(1)

    def _filter(self, table, filters):
        rows = self.tables.setdefault(table, [])
        out = []
        for row in rows:
            ok = True
            for key, expr in (filters or {}).items():
                for e in expr if isinstance(expr, list) else [expr]:
                    ok = ok and _matches(row, key, e)
            if ok:
                out.append(row)
        return out

    def select(self, table, filters=None, *, columns="*", order=None, limit=None, offset=None):
        rows = [dict(r) for r in self._filter(table, filters)]
        return rows[(offset or 0) : (offset or 0) + limit] if limit else rows

    def select_all(self, table, filters=None, **kwargs):
        kwargs.pop("limit", None)
        return self.select(table, filters, **kwargs)

    def insert(self, table, rows, *, on_conflict=None):
        rows = rows if isinstance(rows, list) else [rows]
        out = []
        for row in rows:
            for col in self.unique.get(table, ()):
                if row.get(col) is not None and any(
                    r.get(col) == row[col] for r in self.tables.get(table, [])
                ):
                    raise PortalError(409, "duplicate key")
            stored = {"id": f"{table}-{next(self._ids)}", **row}
            self.tables.setdefault(table, []).append(stored)
            out.append(dict(stored))
        return out

    def patch(self, table, filters, values):
        rows = self._filter(table, filters)
        for row in rows:
            row.update(values)
        return [dict(r) for r in rows]

    def rpc(self, function, args=None):
        self.rpc_calls.append((function, args or {}))
        result = self.rpc_results.get(function)
        if isinstance(result, Exception):
            raise result
        return result


def response(payload: dict | None = None, stop_reason: str = "end_turn"):
    content = [SimpleNamespace(type="text", text=json.dumps(payload))] if payload is not None else []
    usage = SimpleNamespace(
        input_tokens=100, output_tokens=50, cache_read_input_tokens=0, cache_creation_input_tokens=0
    )
    return SimpleNamespace(stop_reason=stop_reason, content=content, usage=usage)


class FakeAnthropic:
    """Returns queued responses; records every request for assertions."""

    def __init__(self, *responses):
        self.queue = list(responses)
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self.queue.pop(0)


class FakeSmartlead:
    def __init__(self):
        self.calls: list[tuple] = []

    def fetch_categories(self):
        self.calls.append(("fetch_categories",))
        return [
            {"id": 1, "name": "Interested"},
            {"id": 2, "name": "Not Interested"},
            {"id": 3, "name": "Do Not Contact"},
            {"id": 4, "name": "Out of Office"},
            {"id": 5, "name": "Meeting Booked"},
        ]

    def set_lead_category(self, campaign_id, lead_id, category_id, pause=False):
        self.calls.append(("category", campaign_id, lead_id, category_id, pause))

    def pause_lead(self, campaign_id, lead_id):
        self.calls.append(("pause", campaign_id, lead_id))

    def unsubscribe_lead(self, campaign_id, lead_id):
        self.calls.append(("unsubscribe", campaign_id, lead_id))

    def unsubscribe_lead_globally(self, lead_id):
        self.calls.append(("unsubscribe_globally", lead_id))
