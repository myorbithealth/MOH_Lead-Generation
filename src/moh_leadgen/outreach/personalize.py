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

OPENER_MAX_WORDS = 35

OPENER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["opener"],
    "properties": {"opener": {"type": "string", "description": "One sentence, at most 35 words"}},
}

_HONORIFICS = {"dr", "dr.", "mr", "mr.", "mrs", "mrs.", "ms", "ms.", "miss", "prof", "prof."}
_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z'\-]+$")
_BANNED = re.compile(r"\b(cure|guarantee|treat|diagnos|patient results|weight loss of)\w*", re.IGNORECASE)


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
def opener_facts(lead: Lead) -> dict[str, str]:
    """The few facts the opener may reference. Same facts -> same cache key -> no new call."""
    facts: dict[str, str] = {"company": lead.company_name or lead.domain}
    platform = lead_platform(lead)
    if platform:
        facts["platform"] = platform
    if lead.legitscript.status == "seal_detected":
        facts["legitscript"] = "displays a LegitScript seal"
    if lead.vertical:
        facts["vertical"] = vertical_phrase(lead)
    if lead.city:
        facts["city"] = f"{lead.city}, {lead.state}" if lead.state else lead.city
    page = next((p for p in lead.pages_crawled if urlsplit(p).path.strip("/")), None)
    if page:
        facts["page"] = urlsplit(page).path.strip("/")[:80]
    summary = (lead.source_detail.get("ai") or {}).get("summary") or lead.description
    if summary:
        facts["summary"] = summary[:240]
    return facts


def facts_hash(facts: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(facts, sort_keys=True).encode("utf-8")).hexdigest()


def template_opener(facts: dict[str, str]) -> str:
    """Deterministic one-liner from the strongest fact we have (no model call)."""
    company = facts["company"]
    if facts.get("legitscript"):
        return f"I saw that {company} displays a LegitScript seal, so compliance is clearly taken seriously."
    if facts.get("platform"):
        return f"I noticed {company} runs its online intake on {facts['platform']}."
    if facts.get("city") and facts.get("vertical"):
        return f"I came across {company} while looking at {facts['vertical']} in {facts['city']}."
    if facts.get("vertical"):
        return f"I came across {company} while looking at {facts['vertical']} that offer online care."
    return f"I came across {company} while researching brands that offer online care."


def _valid_opener(text: str) -> bool:
    words = len(text.split())
    return 0 < words <= OPENER_MAX_WORDS and "\n" not in text and not _BANNED.search(text)


def _prompt(facts: dict[str, str]) -> str:
    return (
        "Write the first sentence of a short B2B cold email from MyOrbit Health (white-label telehealth "
        "infrastructure) to the business described by the facts below. Rules: one sentence, at most 35 "
        "words, reference exactly one concrete fact from the list, no flattery, no questions, no medical "
        "or outcome claims, no prices, do not mention MyOrbit Health, do not start with 'I hope'.\n\n"
        f"Facts (JSON): {json.dumps(facts, sort_keys=True)}"
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


def get_opener(lead: Lead, client: Any | None = None, *, use_model: bool = True) -> tuple[str, str]:
    """(opener, source) where source is 'cache', 'claude' or 'template'.

    Writes a Claude opener into lead.source_detail['ai']['opener']; the caller saves the lead.
    Template openers are not cached: they are free to rebuild.
    """
    facts = opener_facts(lead)
    digest = facts_hash(facts)
    cached = (lead.source_detail.get("ai") or {}).get("opener")
    if isinstance(cached, dict) and cached.get("facts_sha256") == digest and cached.get("text"):
        return cached["text"], "cache"
    if use_model and client is not None:
        text = claude_opener(facts, client)
        if text:
            ai = lead.source_detail.setdefault("ai", {})
            ai["opener"] = {"text": text, "facts_sha256": digest, "model": settings.personalize_model}
            return text, "claude"
    return template_opener(facts), "template"


# ------------------------------------------------------------------ custom fields
def booking_link_for(lead: Lead, rep: RepLike | None, segment: str, utm_content: str) -> str:
    """The rep's (or the shared) booking page with utm tags so Cal.com bookings attribute back."""
    base = (rep.booking_link if rep and rep.booking_link else None) or lead.booking_link or settings.booking_url
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
        "sender_name": (rep.rep_name if rep else None) or sender_name or settings.outreach_sender_name
        or "The MyOrbit Health team",
        "booking_link": booking_link_for(lead, rep, segment, utm_content),
        "company_name_legal": company_name_legal or settings.outreach_company_legal_name or "MyOrbit Health",
        "postal_address": postal_address or settings.outreach_postal_address,
        "segment": segment,
    }
