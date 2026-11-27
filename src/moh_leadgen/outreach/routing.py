"""Route a reply to a rep: rep_rules (portal) -> first matching rule wins -> else least-loaded rep.

The rule condition mini-language is shared with the SQL function public.assign_rep() in
supabase/migrations/0003_outreach.sql and MUST behave identically:

    {"all": [{"field": F, "op": OP, "value": V}, ...]}
    F  in disposition, score, legitscript_status, vertical, competitor_platform, source, segment
    OP in eq, in, gte, lte, not_null, is_null
    eq / in compare lower-cased text; gte / lte compare numerically.
    An empty "all" list matches. Unknown field or op never matches.
    A rule whose rep has no portal profile id is skipped.

The portal RPC assign_rep is preferred (one source of truth, serialised in the database); this
Python evaluation is the fallback when the RPC is unavailable and powers `leadgen outreach route`.
"""

from __future__ import annotations

import json
import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..config import settings
from . import portal_io
from .models import RepAssignment

log = logging.getLogger(__name__)

FIELDS = (
    "disposition",
    "score",
    "legitscript_status",
    "vertical",
    "competitor_platform",
    "source",
    "segment",
)
_NUMERIC_RE = re.compile(r"^-?[0-9.]+$")

SHUBH = "255b30d3-3078-4dce-b109-e86972576890"
DIVANG = "234ce95d-a5d6-410d-9c60-13142cc446fb"

# Used when app_settings.outreach.reps is empty. Stacy has no portal profile yet, so her rules are
# skipped (as in SQL) until a profile id is added to app_settings.
DEFAULT_REPS: list[dict] = [
    {"profile_id": SHUBH, "email": "shubh@myorbithealth.com", "name": "Shubh", "booking_link": None},
    {"profile_id": DIVANG, "email": "devang@myorbithealth.com", "name": "Divang", "booking_link": None},
    {"profile_id": None, "email": None, "name": "Stacy", "booking_link": None},
]

DTC_VERTICALS = [
    "weight_loss_glp1",
    "mens_health_trt",
    "peptides_longevity",
    "online_pharmacy",
    "telehealth_general",
]
LOCAL_VERTICALS = ["med_spa", "fitness_gym", "primary_care", "womens_health", "addiction"]

# Mirrors the seed rows in 0003_outreach.sql. `rep_name` stands in for rep_id until resolved.
DEFAULT_RULES: list[dict] = [
    {
        "priority": 10,
        "name": "shubh_question_objection",
        "rep_name": "Shubh",
        "condition": {"all": [{"field": "disposition", "op": "in", "value": ["question_objection"]}]},
    },
    {
        "priority": 11,
        "name": "shubh_high_score",
        "rep_name": "Shubh",
        "condition": {"all": [{"field": "score", "op": "gte", "value": 80}]},
    },
    {
        "priority": 12,
        "name": "shubh_legitscript_seal",
        "rep_name": "Shubh",
        "condition": {"all": [{"field": "legitscript_status", "op": "eq", "value": "seal_detected"}]},
    },
    {
        "priority": 20,
        "name": "divang_dtc_competitor",
        "rep_name": "Divang",
        "condition": {
            "all": [
                {"field": "vertical", "op": "in", "value": DTC_VERTICALS},
                {"field": "competitor_platform", "op": "not_null"},
            ]
        },
    },
    {
        "priority": 30,
        "name": "stacy_local_verticals",
        "rep_name": "Stacy",
        "condition": {"all": [{"field": "vertical", "op": "in", "value": LOCAL_VERTICALS}]},
    },
    {
        "priority": 31,
        "name": "stacy_local_business",
        "rep_name": "Stacy",
        "condition": {"all": [{"field": "source", "op": "eq", "value": "local_business"}]},
    },
]


# ---------------------------------------------------------------------- condition language
def _as_text(value: Any) -> str | None:
    """What Postgres `jsonb ->> key` / `#>> '{}'` would give for this JSON value."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict | list):
        return json.dumps(value)
    return str(value)


def _number(text: str | None) -> Decimal | None:
    try:
        return Decimal(text) if text is not None else None
    except InvalidOperation:
        return None


def clause_matches(clause: dict, facts: dict) -> bool:
    field, op, expected = clause.get("field"), clause.get("op"), clause.get("value")
    if field not in FIELDS:
        return False
    actual = _as_text(facts.get(field))
    if op == "not_null":
        return actual not in (None, "")
    if op == "is_null":
        return actual in (None, "")
    if actual is None:
        return False
    if op == "eq":
        # SQL: lower(actual) <> lower(NULL) is NULL, which does not reject; mirror that.
        return expected is None or actual.lower() == (_as_text(expected) or "").lower()
    if op == "in":
        if not isinstance(expected, list):
            return False
        return any(actual.lower() == (_as_text(v) or "").lower() for v in expected if v is not None)
    if op in ("gte", "lte"):
        if not _NUMERIC_RE.match(actual) or _number(actual) is None:
            return False
        limit = _number(_as_text(expected))
        if limit is None:
            return expected is None  # SQL: comparison with NULL does not reject
        return _number(actual) >= limit if op == "gte" else _number(actual) <= limit
    return False


def rule_matches(condition: dict | None, facts: dict) -> bool:
    return all(clause_matches(c, facts) for c in (condition or {}).get("all", []))


# ---------------------------------------------------------------------- config
def load_config(portal=None) -> tuple[list[dict], list[dict], dict]:
    """(rules, reps, outreach settings). Portal values win; built-in defaults fill the gaps."""
    outreach: dict = {}
    rules: list[dict] = []
    if portal is not None:
        try:
            outreach = portal_io.get_app_setting(portal, "outreach")
            rules = portal_io.list_rep_rules(portal)
        except Exception as exc:  # noqa: BLE001 - routing must still work offline
            log.warning("could not load routing config from portal, using defaults: %s", exc)
    reps = [r for r in outreach.get("reps") or [] if r.get("profile_id")] or DEFAULT_REPS
    if not rules:
        by_name = {r["name"]: r.get("profile_id") for r in [*DEFAULT_REPS, *reps] if r.get("profile_id")}
        rules = [{**r, "rep_id": by_name.get(r["rep_name"])} for r in DEFAULT_RULES]
    return rules, reps, outreach


def lead_facts(lead: dict | None, disposition: str | None) -> dict:
    lead = lead or {}
    return {
        "disposition": disposition or lead.get("disposition"),
        **{f: lead.get(f) for f in FIELDS if f != "disposition"},
    }


def with_tracking(link: str | None, lead_domain: str | None, contact_id: str | None) -> str | None:
    """Append lead_domain / contact_id so the Cal.com webhook can map the booking back."""
    if not link:
        return None
    parts = urlsplit(link)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k not in ("lead_domain", "contact_id")]
    if lead_domain:
        query.append(("lead_domain", lead_domain))
    if contact_id:
        query.append(("contact_id", contact_id))
    return urlunsplit(parts._replace(query=urlencode(query)))


def _assignment(
    rep: dict | None, rule: str, lead_domain: str | None, contact_id: str | None
) -> RepAssignment:
    rep = rep or {}
    link = rep.get("booking_link") or settings.booking_url or None
    return RepAssignment(
        rep_id=rep.get("profile_id"),
        rep_name=rep.get("name") or "unassigned",
        rep_email=rep.get("email"),
        booking_link=with_tracking(link, lead_domain, contact_id),
        rule=rule,
    )


def _rep_by_id(reps: list[dict], rep_id: str | None) -> dict | None:
    known = {r.get("profile_id"): r for r in [*DEFAULT_REPS, *reps] if r.get("profile_id")}  # settings win
    if rep_id in known:
        return known[rep_id]
    return {"profile_id": rep_id, "name": rep_id} if rep_id else None


def evaluate(
    rules: list[dict], reps: list[dict], facts: dict, open_tasks: dict[str, int] | None = None
) -> tuple[str | None, str]:
    """(rep profile id, rule name). Same order and fallback as SQL assign_rep()."""
    for rule in sorted(rules, key=lambda r: (r.get("priority", 0), r.get("id") or 0)):
        if rule.get("active", True) is False or not rule.get("rep_id"):
            continue
        if rule_matches(rule.get("condition"), facts):
            return rule["rep_id"], rule.get("name") or f"rule {rule.get('id')}"
    candidates = [r["profile_id"] for r in reps if r.get("profile_id")]
    if not candidates:
        return None, "no_reps_configured"
    counts = open_tasks or {}
    best = min(candidates, key=lambda rid: (counts.get(rid, 0), rid))
    return best, "fallback_least_loaded"


def route(
    lead_domain: str | None,
    disposition: str | None,
    portal=None,
    *,
    contact_id: str | None = None,
    lead: dict | None = None,
    use_rpc: bool = True,
) -> RepAssignment:
    """Pick the rep for a reply: portal RPC assign_rep first, Python rules if that fails."""
    rules, reps, _ = load_config(portal)
    if portal is not None and use_rpc and lead_domain and disposition:
        try:
            rep_id = portal.rpc("assign_rep", {"p_lead_domain": lead_domain, "p_disposition": disposition})
            if rep_id:
                return _assignment(_rep_by_id(reps, rep_id), "rpc:assign_rep", lead_domain, contact_id)
        except Exception as exc:  # noqa: BLE001 - fall back to the Python mirror
            log.info("assign_rep rpc unavailable (%s); using Python rules", exc)
    if lead is None and portal is not None and lead_domain:
        try:
            lead = portal_io.get_lead(portal, lead_domain)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not load lead %s: %s", lead_domain, exc)
    open_tasks: dict[str, int] = {}
    if portal is not None:
        try:
            open_tasks = portal_io.open_task_counts(
                portal, [r["profile_id"] for r in reps if r.get("profile_id")]
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("could not count open tasks: %s", exc)
    rep_id, rule = evaluate(rules, reps, lead_facts(lead, disposition), open_tasks)
    return _assignment(_rep_by_id(reps, rep_id), rule, lead_domain, contact_id)
