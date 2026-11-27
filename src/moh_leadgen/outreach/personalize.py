"""Per-lead values for Smartlead: names, platform, vertical phrase, booking link and the opener.

The opener is one sentence that shows we looked at the business. Claude writes it from a small set
of facts (platform, LegitScript seal, vertical, city, one product or page); the result is cached in
lead.source_detail['ai']['opener'] keyed by a sha256 of those facts, so a lead is only paid for once
and again only when its facts change. Without ANTHROPIC_API_KEY (or on any API problem) a
deterministic template sentence is used instead, so enrolment never blocks on the model.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit

from ..config import settings
from ..models import Lead
from .sequences import DEFAULT_PLATFORM, lead_platform, vertical_phrase

log = logging.getLogger(__name__)

OPENER_MAX_WORDS = 75

OPENER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["opener"],
    "properties": {
        "opener": {
            "type": "string",
            "description": "Two or three short sentences, at most 75 words, plain text, no line breaks",
        }
    },
}

# Phrases that make an email read as machine-written or as a medical claim. Any hit = regenerate/fallback.
_AI_TELLS = re.compile(
    r"\b(i hope this (email )?finds you|i came across|i stumbled|i noticed that|excited|thrilled|"
    r"delighted|streamline|leverage|seamless|unlock|robust|cutting[- ]edge|game[- ]changer|"
    r"elevate|empower|synerg|revolutioni|innovative|best[- ]in[- ]class|world[- ]class|"
    r"reach(ing)? out|touch base|circle back|in today's|navigate|landscape|journey|"
    r"as a (founder|ceo|leader)|i wanted to|just wanted to|quick question|not sure if)\b",
    re.IGNORECASE,
)

_HONORIFICS = {"dr", "dr.", "mr", "mr.", "mrs", "mrs.", "ms", "ms.", "miss", "prof", "prof."}
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z'\-]+$")
_BANNED = re.compile(r"\b(cure|treat|diagnos|patient results|weight loss of|lose \d)\w*", re.IGNORECASE)


class RepLike(Protocol):
    rep_name: str
    booking_link: str | None


# ------------------------------------------------------------------ names
def first_name_from(full_name: str | None) -> str | None:
    """A first name safe to put after 'Hi', or None (then the greeting has no name).

    Skips honorifics, refuses initials, digits and company-looking strings, and fixes case.
    """
    if not full_name:
        return None
    tokens = [t.strip(",") for t in full_name.split()]
    tokens = [t for t in tokens if t and t.lower() not in _HONORIFICS]
    if not tokens or len(tokens) > 4:
        return None
    first = tokens[0]
    if not _NAME_RE.match(first) or len(first.replace("'", "").replace("-", "")) < 2:
        return None
    if first.isupper() or first.islower():
        first = first.capitalize()
    return first


def last_name_from(full_name: str | None) -> str | None:
    if not full_name or not first_name_from(full_name):
        return None
    tokens = [t.strip(",") for t in full_name.split() if t.lower() not in _HONORIFICS]
    return tokens[-1] if len(tokens) > 1 else None


# ------------------------------------------------------------------ opener
def opener_facts(lead: Lead, angle: str | None = None) -> dict[str, str]:
    """Everything the opener may draw on. Same facts -> same cache key -> no new call."""
    facts: dict[str, str] = {"company": lead.company_name or lead.domain, "domain": lead.domain}
    platform = lead_platform(lead)
    if platform:
        facts["platform"] = platform
    if lead.legitscript.status == "seal_detected":
        facts["legitscript"] = "displays a LegitScript certification seal"
    if lead.vertical:
        facts["vertical"] = vertical_phrase(lead)
    if lead.city:
        facts["city"] = f"{lead.city}, {lead.state}" if lead.state else lead.city
    paths = [urlsplit(p).path.strip("/") for p in lead.pages_crawled]
    programs = [
        x for x in paths if x and not re.search(r"privacy|terms|policy|contact|about|faq|login|cart", x)
    ]
    if programs:
        facts["pages"] = ", ".join(programs[:6])[:200]
    summary = (lead.source_detail.get("ai") or {}).get("summary") or lead.description
    if summary:
        facts["summary"] = summary[:400]
    if lead.owner_name:
        facts["contact"] = f"{lead.owner_name}" + (f", {lead.owner_title}" if lead.owner_title else "")
    signals = []
    tech = set(lead.tech_stack)
    if tech & {"Meta Pixel", "TikTok Pixel", "Google Ads"}:
        signals.append("runs paid ads")
    if tech & {"Shopify", "WooCommerce", "Stripe"}:
        signals.append("sells online with a checkout")
    if signals:
        facts["signals"] = "; ".join(signals)
    if angle:
        facts["angle"] = angle
    return facts


def facts_hash(facts: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(facts, sort_keys=True).encode("utf-8")).hexdigest()


def template_opener(facts: dict[str, str]) -> str:
    """Deterministic fallback (no model call). Plain, specific, no AI filler."""
    company = facts["company"]
    bits = []
    if facts.get("platform"):
        bits.append(
            f"{company} runs on {facts['platform']}, so you already know what a platform fee, a consult "
            "charge per medication and a cut of every order add up to each month."
        )
    elif facts.get("legitscript"):
        bits.append(
            f"{company} already carries LegitScript certification, which tells me you have done the hard "
            "compliance work and are paying real money for consults and fulfilment."
        )
    elif facts.get("vertical"):
        bits.append(f"{company} is one of the {facts['vertical']} we have been looking at this month.")
    else:
        bits.append(f"{company} is one of the brands we have been looking at this month.")
    bits.append(
        "Our physicians charge one fee per patient, not one per medication, we do not mark up the "
        "medication, and you keep your own processor and data."
    )
    return " ".join(bits)


def _valid_opener(text: str) -> bool:
    words = len(text.split())
    if not (20 <= words <= OPENER_MAX_WORDS) or "\n" in text:
        return False
    if _BANNED.search(text) or _AI_TELLS.search(text) or "!" in text or "—" in text:
        return False
    return "MyOrbit" not in text and "{{" not in text and "?" not in text


_ANGLE_HINTS = {
    "price_beat_guarantee": "build toward the money: what they pay today per consult and per order, and "
    "that one physician fee covers a patient's consults for every medication",
    "ownership_no_markup": "build toward ownership: who holds their payment processor, their patient "
    "data and the margin on each order",
    "clinical_quality_refills": "build toward retention: patients who get real consults and multiple "
    "refills stay, and one physician fee instead of one per medication",
    "plain": "state plainly what we would change for them",
    "referral_ask": "write for a shared inbox: who at the company this is for, and why",
}


def _prompt(facts: dict[str, str]) -> str:
    angle = _ANGLE_HINTS.get(facts.get("angle", ""), _ANGLE_HINTS["plain"])
    return (
        "You are Shubh, founder of MyOrbit Health (white-label telehealth: our physicians do the "
        "consults, pharmacy fulfilment, intake and checkout under the client's brand). You have studied "
        "the business below and are writing the two or three sentences that come right after 'Hi "
        "<name>,' in a short cold email. The rest of the email (our offer, the guarantee, the ask) is "
        "already written, so do not describe our offer at all.\n"
        "Sentence 1: what they sell and how they run it today, in their own terms (program names, the "
        "platform they use, who prescribes, where they are, that they run ads or sell online). "
        "Sentence 2: the cost or friction that setup almost certainly creates for them, concretely: a "
        "consult fee charged per medication, a platform cut on every order, a processor and patient "
        "data they do not control, patients dropping off when refills are a fight. Pick the one that "
        f"fits this angle: {angle}. Sentence 3 (optional, under ten words): a plain bridge in your own words that points at "
        "that one cost or friction; vary it, never reuse an example phrase.\n"
        "Hard rules: at most 75 words. No greeting, no name, no sign-off, no question, no exclamation "
        "mark, no bullet, no em dash, no flattery, no medical outcome claim, no price, no percentage, no "
        "mention of MyOrbit or of our fees. One hedge word at most ('likely' or 'probably'). Never "
        "write 'I hope', 'I came across', 'I noticed', 'reaching out', 'excited', 'streamline', "
        "'leverage', 'seamless', 'unlock', 'journey', 'landscape'. Sound like a person typing, not a "
        "brochure: contractions, short sentences, specific nouns. If the facts are thin, two sentences "
        "and nothing invented.\n\n"
        f"Their facts (JSON): {json.dumps(facts, sort_keys=True)}"
    )


def claude_opener(facts: dict[str, str], client: Any, model: str | None = None) -> str | None:
    """Ask Claude for the opener with schema-restricted output. None on any problem."""
    try:
        response = client.beta.messages.create(
            model=model or settings.personalize_model,
            max_tokens=1024,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": OPENER_SCHEMA}},
            messages=[{"role": "user", "content": _prompt(facts)}],
        )
    except Exception as exc:  # noqa: BLE001 - SDK is optional; any failure falls back to the template
        log.warning("opener generation failed for %s: %s", facts.get("company"), type(exc).__name__)
        return None
    if response.stop_reason in ("refusal", "max_tokens"):
        log.info("opener not usable (stop_reason=%s) for %s", response.stop_reason, facts.get("company"))
        return None
    text = next((b.text for b in response.content if getattr(b, "type", "") == "text"), "")
    try:
        opener = str(json.loads(text)["opener"]).strip()
    except (ValueError, KeyError, TypeError):
        return None
    return opener if _valid_opener(opener) else None


def default_client() -> Any | None:
    """An Anthropic client when the key and SDK are present, else None (template openers)."""
    if not settings.anthropic_key:
        return None
    try:
        import anthropic
    except ImportError:
        return None
    return anthropic.Anthropic(api_key=settings.anthropic_key)


def get_opener(
    lead: Lead, client: Any | None = None, *, use_model: bool = True, angle: str | None = None
) -> tuple[str, str]:
    """(opener, source) where source is 'cache', 'claude' or 'template'.

    Writes a Claude opener into lead.source_detail['ai']['opener']; the caller saves the lead.
    Template openers are not cached: they are free to rebuild.
    """
    facts = opener_facts(lead, angle)
    digest = facts_hash(facts)
    cached = (lead.source_detail.get("ai") or {}).get("opener")
    if isinstance(cached, dict) and cached.get("facts_sha256") == digest and cached.get("text"):
        return cached["text"], "cache"
    if use_model and client is not None:
        first = first_name_from(lead.owner_name) or ""
        text = claude_opener(facts, client)
        if text and first and re.search(rf"\b{re.escape(first)}\b", text):
            text = None  # the greeting already uses the name; a second use reads as a mail merge
        text = text or claude_opener(facts, client)
        if text and first and re.search(rf"\b{re.escape(first)}\b", text):
            text = None
        if text:
            ai = lead.source_detail.setdefault("ai", {})
            ai["opener"] = {"text": text, "facts_sha256": digest, "model": settings.personalize_model}
            return text, "claude"
    return template_opener(facts), "template"


# ------------------------------------------------------------------ custom fields
def booking_link_for(lead: Lead, rep: RepLike | None, segment: str, utm_content: str) -> str:
    """The rep's (or the shared) booking page with utm tags so Cal.com bookings attribute back."""
    base = (
        (rep.booking_link if rep and rep.booking_link else None) or lead.booking_link or settings.booking_url
    )
    if not base:
        return ""
    tags = urlencode({"utm_source": "smartlead", "utm_campaign": segment, "utm_content": utm_content})
    return f"{base}{'&' if '?' in base else '?'}{tags}"


def build_custom_fields(
    lead: Lead,
    rep: RepLike | None = None,
    *,
    segment: str,
    opener: str,
    utm_content: str = "s1",
    first_name: str | None = None,
    company_name_legal: str | None = None,
    postal_address: str | None = None,
    sender_name: str | None = None,
) -> dict[str, str]:
    """Values for Smartlead custom_fields (and for rendering our own templates)."""
    platform = lead_platform(lead)
    return {
        "first_name": first_name or "",
        "company_name": lead.company_name or lead.domain,
        "opener": opener,
        "platform": platform or DEFAULT_PLATFORM,
        "vertical_phrase": vertical_phrase(lead),
        "sender_name": (rep.rep_name if rep else None)
        or sender_name
        or settings.outreach_sender_name
        or "The MyOrbit Health team",
        "booking_link": booking_link_for(lead, rep, segment, utm_content),
        "company_name_legal": company_name_legal or settings.outreach_company_legal_name or "MyOrbit Health",
        "postal_address": postal_address or settings.outreach_postal_address,
        "segment": segment,
    }
