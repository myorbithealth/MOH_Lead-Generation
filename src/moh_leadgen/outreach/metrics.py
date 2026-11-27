"""The learning loop: Beta posteriors per variant, P(best), retire/promote rules, Thompson sampling,
and the weekly Opus proposal of new step-1 variants (`leadgen outreach learn`).

Reward = positive reply (interested, meeting_requested, meeting_booked) per send. Opens are never a
reward (Apple/Gmail proxies inflate them). Posterior per arm: Beta(1 + positives, 1 + sends -
positives), the same formula as SQL outreach_refresh_variants(). A brand-new arm with no sends uses
its stored alpha/beta instead, which `learn` seeds with half the parent's, so children of good
parents get tried early.

Arms only compete inside one slot: the same segment and sequence step.
"""

from __future__ import annotations

import difflib
import json
import logging
import random
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from ..config import settings
from . import portal_io, sequences
from .models import FDA_SEGMENT, Variant

log = logging.getLogger(__name__)

RETIRE_P_BEST = 0.05
RETIRE_MIN_SENDS = 300
PROMOTE_P_BEST = 0.90
PROMOTE_MIN_SENDS = 500
PROMOTE_MIN_POSITIVES = 15
COMPETING = ("active", "control")
DUPLICATE_RATIO = 0.85
MAX_WORDS = 120
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Claims the learn job must never put into copy (B2B infrastructure pitch only).
BANNED_PHRASES = re.compile(
    r"\b(cure[sd]?|guarantee[sd]?|fda[- ]approved|lose \d+|weight loss results|clinically proven|"
    r"miracle|risk[- ]free|patients? (will|love)|side effects?)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------- posteriors
def posterior(v: Variant) -> tuple[float, float]:
    if v.sends <= 0:
        return max(float(v.alpha), 1e-6), max(float(v.beta), 1e-6)
    positives = min(v.positive_replies, v.sends)
    return 1.0 + positives, 1.0 + (v.sends - positives)


def beta_posteriors(variants: list[Variant]) -> dict[str, tuple[float, float]]:
    return {v.id: posterior(v) for v in variants}


def p_best(variants: list[Variant], draws: int = 20000, rng: random.Random | None = None) -> dict[str, float]:
    """Monte Carlo probability that each arm has the highest positive-reply rate."""
    if not variants:
        return {}
    rng = rng or random.Random()
    params = beta_posteriors(variants)
    wins: Counter[str] = Counter()
    for _ in range(draws):
        best = max(params, key=lambda vid: rng.betavariate(*params[vid]))
        wins[best] += 1
    return {vid: wins[vid] / draws for vid in params}


def slots(variants: list[Variant]) -> dict[tuple[str, int], list[Variant]]:
    """Competing arms grouped by (segment, sequence_step)."""
    grouped: dict[tuple[str, int], list[Variant]] = defaultdict(list)
    for v in variants:
        if v.status in COMPETING:
            grouped[(v.segment, v.sequence_step)].append(v)
    return dict(grouped)


def decide(
    variants: list[Variant], draws: int = 20000, rng: random.Random | None = None
) -> list[tuple[str, str, str]]:
    """(variant_id, retire|promote|keep, reason) for every competing arm."""
    rng = rng or random.Random()
    out: list[tuple[str, str, str]] = []
    for arms in slots(variants).values():
        probs = p_best(arms, draws, rng) if len(arms) > 1 else {arms[0].id: 1.0}
        for v in arms:
            p = probs[v.id]
            if v.status == "control":
                out.append((v.id, "keep", f"control arm, P(best)={p:.2f}"))
            elif p < RETIRE_P_BEST and v.sends >= RETIRE_MIN_SENDS:
                out.append((v.id, "retire", f"P(best)={p:.3f} < {RETIRE_P_BEST} after {v.sends} sends"))
            elif (
                p > PROMOTE_P_BEST
                and v.sends >= PROMOTE_MIN_SENDS
                and v.positive_replies >= PROMOTE_MIN_POSITIVES
            ):
                out.append(
                    (v.id, "promote", f"P(best)={p:.3f} with {v.sends} sends, {v.positive_replies} positive")
                )
            else:
                out.append((v.id, "keep", f"P(best)={p:.2f}, {v.sends} sends"))
    return out


def thompson_choose(
    variants: list[Variant], rng: random.Random | None = None, control_floor: float = 0.10
) -> Variant | None:
    """Pick the arm for one lead. The control keeps at least `control_floor` of traffic."""
    rng = rng or random.Random()
    arms = [v for v in variants if v.status in COMPETING]
    if not arms:
        return None
    controls = [v for v in arms if v.status == "control"]
    if controls and rng.random() < control_floor:
        return rng.choice(controls)
    return max(arms, key=lambda v: rng.betavariate(*posterior(v)))


def apply_decisions(portal, variants: list[Variant], decisions: list[tuple[str, str, str]]) -> int:
    """Write status changes. A promoted arm becomes the slot's control; the old control goes active."""
    by_id = {v.id: v for v in variants}
    changed = 0
    for vid, action, _reason in decisions:
        if action == "retire":
            portal.patch("variants", {"id": f"eq.{vid}"}, {"status": "retired"})
            changed += 1
        elif action == "promote":
            v = by_id[vid]
            for old in variants:
                same_slot = (old.segment, old.sequence_step) == (v.segment, v.sequence_step)
                if old.status == "control" and same_slot and old.id != vid:
                    portal.patch("variants", {"id": f"eq.{old.id}"}, {"status": "active"})
            portal.patch("variants", {"id": f"eq.{vid}"}, {"status": "control"})
            changed += 1
    return changed


# ---------------------------------------------------------------------- proposals
PROPOSAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["variants"],
    "properties": {
        "variants": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "angle", "subject", "body", "hypothesis", "parent_id"],
                "properties": {
                    "name": {"type": "string"},
                    "angle": {"type": "string"},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                    "hypothesis": {"type": "string"},
                    "parent_id": {"type": ["string", "null"]},
                },
            },
        }
    },
}

LEARN_RULES = """\
Compliance rules for every variant (hard requirements):
- B2B only: we sell telehealth infrastructure to business owners. Never pitch treatments.
- No medical claims, no outcome promises, no patient stories, no condition words in the subject.
- Do not write a footer, address or unsubscribe line: the sender appends the CAN-SPAM footer
  ({{company_name_legal}}, {{postal_address}}, opt-out sentence) to every email automatically.
- Subject: 3 to 6 lowercase words, truthful, no "re:"/"fwd:", no ! ? $ %.
- Body: plain text, under 120 words, no pricing, one ask (a 20-minute demo via {{booking_link}}),
  signed {{sender_name}}. Allowed placeholders: {{first_name}}, {{company_name}}, {{platform}},
  {{vertical_phrase}}, {{opener}}, {{booking_link}}, {{sender_name}}.
"""


def word_count(text: str) -> int:
    return len(re.sub(r"\{\{[^}]+\}\}", "x", text or "").split())


def lint_variant(p: dict) -> str | None:
    """Reason a proposed variant breaks the rules, or None when it is fine."""
    body, subject = p.get("body") or "", p.get("subject") or ""
    if not body.strip() or not subject.strip():
        return "empty subject or body"
    if word_count(body) >= MAX_WORDS:
        return f"body has {word_count(body)} words (limit {MAX_WORDS})"
    if BANNED_PHRASES.search(body) or BANNED_PHRASES.search(subject):
        return "contains a medical or outcome claim"
    if re.match(r"^\s*(re|fwd?):", subject, re.IGNORECASE):
        return "fake reply/forward subject"
    problems = sequences.subject_problems(subject)
    if problems:
        return "; ".join(problems)
    if re.search(r"unsubscribe|postal_address", body, re.IGNORECASE):
        return "writes its own footer"
    return None


def is_near_duplicate(body: str, others: list[str], ratio: float = DUPLICATE_RATIO) -> bool:
    return any(difflib.SequenceMatcher(None, body, o).ratio() > ratio for o in others if o)


def build_learn_prompt(
    segment: str,
    arms: list[Variant],
    probs: dict[str, float],
    summaries: list[str],
    objections: dict[str, int],
    n: int,
) -> str:
    lines = [
        (
            f"Segment: {segment}. Propose {n} new step-1 cold-email variants for MyOrbit Health "
            "(white-label telehealth infrastructure: licensed providers, pharmacy fulfilment, compliance, checkout)."
        ),
        "",
        "Current step-1 arms (positive replies / sends, P(best)):",
    ]
    for v in sorted(arms, key=lambda a: -probs.get(a.id, 0)):
        lines.append(
            f"- id={v.id} name={v.name} status={v.status} angle={v.angle!r} "
            f"{v.positive_replies}/{v.sends} P(best)={probs.get(v.id, 0):.2f}\n"
            f"  subject: {v.subject}\n  body: {v.body}"
        )
    lines += ["", "Objection counts in recent replies: " + json.dumps(objections, sort_keys=True)]
    lines += ["", "Recent interested / question replies (summaries, no PHI):"]
    lines += [f"- {s}" for s in summaries] or ["- (none yet)"]
    lines += [
        "",
        LEARN_RULES,
        (
            "Each variant must test one clear hypothesis that differs from the existing arms. Set parent_id to "
            "the id of the arm it builds on, or null for a fresh angle. Names are short snake_case and new."
        ),
    ]
    return "\n".join(lines)


def ask_for_variants(client, prompt: str) -> list[dict]:
    response = client.beta.messages.create(
        model=settings.learn_model,
        max_tokens=8000,
        betas=[FALLBACK_BETA],
        fallbacks="default",
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": PROPOSAL_SCHEMA}},
        messages=[{"role": "user", "content": prompt}],
    )
    usage = getattr(response, "usage", None)
    if usage is not None:
        log.info("learn usage: input=%s output=%s", usage.input_tokens, usage.output_tokens)
    if response.stop_reason in ("refusal", "max_tokens"):
        log.warning("variant proposal stopped with %s", response.stop_reason)
        return []
    text = next((b.text for b in response.content if getattr(b, "type", None) == "text"), "")
    try:
        return list(json.loads(text).get("variants") or [])
    except (ValueError, AttributeError):
        log.warning("variant proposal was not valid JSON")
        return []


def proposal_rows(
    segment: str, proposals: list[dict], existing: list[Variant], activate: bool
) -> tuple[list[dict], list[str]]:
    """Lint + dedupe proposals into variants rows. Returns (rows, rejection notes)."""
    by_id = {v.id: v for v in existing}
    taken_names = {v.name for v in existing if v.segment == segment and v.sequence_step == 1}
    bodies = [v.body or "" for v in existing]
    rows, rejected = [], []
    for p in proposals:
        reason = lint_variant(p)
        if reason is None and is_near_duplicate(p["body"], bodies):
            reason = "near-duplicate of an existing body"
        if reason is None and p["name"] in taken_names:
            reason = "name already used"
        if reason:
            rejected.append(f"{p.get('name')}: {reason}")
            continue
        parent = by_id.get(p.get("parent_id") or "")
        alpha, beta = (max(1.0, parent.alpha / 2), max(1.0, parent.beta / 2)) if parent else (1.0, 1.0)
        rows.append(
            {
                "id": sequences.variant_id(segment, 1, p["name"]),  # same id scheme as enrolment
                "segment": segment,
                "sequence_step": 1,
                "name": p["name"],
                "angle": p.get("angle"),
                "subject": p["subject"],
                "body": p["body"],
                "hypothesis": p.get("hypothesis"),
                "parent_id": parent.id if parent else None,
                "status": "active" if activate else "proposed",
                "alpha": alpha,
                "beta": beta,
            }
        )
        bodies.append(p["body"])
        taken_names.add(p["name"])
    return rows, rejected


def _reply_context(portal) -> tuple[list[str], dict[str, int]]:
    summaries = portal.select(
        "reply_classifications",
        {
            "contains_phi": "is.false",
            "disposition": "in.(interested,question_objection)",
            "summary": "not.is.null",
        },
        columns="summary",
        order="created_at.desc",
        limit=20,
    )
    since = (datetime.now(UTC) - timedelta(days=90)).isoformat()
    objections = portal.select_all(
        "reply_classifications",
        {"created_at": f"gte.{since}", "objection": "not.is.null"},
        columns="objection",
    )
    counts = Counter(r["objection"] for r in objections if r.get("objection") and r["objection"] != "none")
    return [r["summary"] for r in summaries], dict(counts)


def learn(
    portal,
    *,
    propose: int = 3,
    dry_run: bool = False,
    activate: bool = False,
    client=None,
    rng: random.Random | None = None,
) -> dict:
    """Weekly loop: refresh posteriors, retire/promote, propose new step-1 arms per segment."""
    if not dry_run:
        try:
            portal.rpc("outreach_refresh_variants")
        except Exception as exc:  # noqa: BLE001 - stale counts are better than no run
            log.warning("outreach_refresh_variants failed: %s", exc)
    variants = [Variant.model_validate(r) for r in portal.select_all("variants")]
    decisions = decide(variants, rng=rng)
    report: dict[str, Any] = {"decisions": decisions, "proposed": [], "rejected": [], "applied": 0}

    if propose > 0:
        if client is None and settings.anthropic_key:
            import anthropic

            client = anthropic.Anthropic(api_key=settings.anthropic_key)
        if client is None:
            log.warning("ANTHROPIC_API_KEY not set; skipping variant proposals")
        else:
            summaries, objections = _reply_context(portal)
            for (segment, step), arms in sorted(slots(variants).items()):
                if segment == FDA_SEGMENT:
                    continue  # reviewed, compliance-only copy: never machine-proposed
                if step != 1:
                    continue
                probs = p_best(arms, rng=rng) if len(arms) > 1 else {arms[0].id: 1.0}
                prompt = build_learn_prompt(segment, arms, probs, summaries, objections, propose)
                rows, rejected = proposal_rows(
                    segment, ask_for_variants(client, prompt)[:propose], variants, activate
                )
                report["proposed"].extend(rows)
                report["rejected"].extend(rejected)

    if not dry_run:
        for row in report["proposed"]:
            portal_io.insert_ignoring_duplicates(portal, "variants", row)
        report["applied"] = apply_decisions(portal, variants, decisions)
    return report


# ---------------------------------------------------------------------- funnel report
POSITIVE = ("interested", "meeting_requested", "meeting_booked")


def _chunks(items: list[str], size: int = 200):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def funnel(portal, days: int | None = None) -> list[dict]:
    """Funnel rows per (segment, variant_id, sequence_step).

    All time: read straight from the outreach_funnel view. With `days`: recomputed from the raw
    tables for sends in the window (the view has no date column), attributing each meeting to the
    contact's latest send before it, like the view does.
    """
    if not days:
        return portal.select_all("outreach_funnel")
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    sent = portal.select_all(
        "email_drafts",
        {"direction": "eq.outbound", "sent_at": f"gte.{since}"},
        columns="id,contact_id,lead_domain,variant_id,sequence_step,sent_at,opened_at,replied_at",
    )
    variant_segment = {v["id"]: v.get("segment") for v in portal.select_all("variants", columns="id,segment")}
    domains = sorted({(d.get("lead_domain") or "").lower() for d in sent if d.get("lead_domain")})
    lead_segment: dict[str, str | None] = {}
    for chunk in _chunks(domains):
        for row in portal.select("leads", {"domain": f"in.({','.join(chunk)})"}, columns="domain,segment"):
            lead_segment[row["domain"].lower()] = row.get("segment")
    positives = Counter(
        r["email_draft_id"]
        for r in portal.select_all(
            "reply_classifications",
            {"created_at": f"gte.{since}", "disposition": f"in.({','.join(POSITIVE)})"},
            columns="email_draft_id",
        )
        if r.get("email_draft_id")
    )
    meetings = portal.select_all(
        "meetings", {"created_at": f"gte.{since}"}, columns="id,contact_id,created_at,status,outcome"
    )

    def key_of(d: dict) -> tuple:
        segment = variant_segment.get(d.get("variant_id")) or lead_segment.get(
            (d.get("lead_domain") or "").lower()
        )
        return segment, d.get("variant_id"), d.get("sequence_step")

    rows: dict[tuple, dict] = {}
    for d in sent:
        k = key_of(d)
        row = rows.setdefault(
            k,
            {
                "segment": k[0],
                "variant_id": k[1],
                "sequence_step": k[2],
                "sends": 0,
                "opens": 0,
                "replies": 0,
                "positive_replies": 0,
                "meetings": 0,
                "held": 0,
            },
        )
        row["sends"] += 1
        row["opens"] += 1 if d.get("opened_at") else 0
        row["replies"] += 1 if d.get("replied_at") else 0
        row["positive_replies"] += positives.get(d["id"], 0)
    by_contact: dict[str, list[dict]] = defaultdict(list)
    for d in sent:
        if d.get("contact_id"):
            by_contact[d["contact_id"]].append(d)
    for m in meetings:
        earlier = [d for d in by_contact.get(m.get("contact_id"), []) if d["sent_at"] <= m["created_at"]]
        if not earlier:
            continue
        row = rows[key_of(max(earlier, key=lambda d: d["sent_at"]))]
        row["meetings"] += 1
        if m.get("status") == "completed" or str(m.get("outcome") or "").lower().startswith("held"):
            row["held"] += 1
    return list(rows.values())


def report_rows(
    funnel_rows: list[dict], names: dict[str, str] | None = None, rng: random.Random | None = None
) -> list[dict]:
    """Funnel rows plus positive rate and P(best) within each (segment, step) slot."""
    names = names or {}
    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for row in funnel_rows:
        grouped[(row.get("segment"), row.get("sequence_step"))].append(row)
    out = []
    for (segment, step), rows in sorted(grouped.items(), key=lambda kv: (str(kv[0][0]), kv[0][1] or 0)):
        arms = [
            Variant(
                id=str(r["variant_id"]),
                segment=str(segment),
                sequence_step=int(step or 0),
                name="",
                sends=int(r.get("sends") or 0),
                positive_replies=int(r.get("positive_replies") or 0),
            )
            for r in rows
            if r.get("variant_id")
        ]
        probs = p_best(arms, draws=5000, rng=rng) if len(arms) > 1 else {a.id: 1.0 for a in arms}
        for r in rows:
            sends = int(r.get("sends") or 0)
            out.append(
                {
                    **r,
                    "variant": names.get(str(r.get("variant_id")), r.get("variant_id") or "(none)"),
                    "positive_rate": (int(r.get("positive_replies") or 0) / sends) if sends else 0.0,
                    "p_best": probs.get(str(r.get("variant_id"))),
                }
            )
    return out
