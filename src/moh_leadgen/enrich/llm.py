"""Optional Claude-powered extraction: reads the crawled pages and returns the decision-makers,
a one-line company summary and the vertical. Only used when ANTHROPIC_API_KEY is set and the
`llm` extra is installed (`pip install -e .[llm]`).

Uses structured outputs so the result always validates, and the server-side refusal fallback so a
rare safety decline degrades to a sibling model instead of an empty answer.

Results are cached in Lead.source_detail["ai"] keyed by a sha256 of the page text sent to Claude,
so `enrich --redo` only pays again for sites whose content actually changed.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from pydantic import BaseModel, Field

from ..config import settings
from ..models import Person
from .extract import DECISION_RE
from .site_crawler import SiteSnapshot

log = logging.getLogger(__name__)

MAX_CHARS = 40_000


class ExtractedPerson(BaseModel):
    name: str
    title: str | None = None
    email: str | None = None
    linkedin: str | None = None
    evidence: str = Field(description="Short quote from the page that supports this person/title")


class Extraction(BaseModel):
    company_name: str | None = None
    summary: str = Field(description="One sentence: what the business sells and to whom")
    vertical: str | None = Field(
        description="e.g. weight_loss_glp1, mens_health_trt, med_spa_aesthetics, addiction_treatment, online_pharmacy, telehealth_general, fitness_gym, primary_care_clinic"
    )
    is_multi_location: bool = False
    estimated_size: str | None = Field(description="solo | small (2-10) | mid (11-50) | large (50+)")
    people: list[ExtractedPerson] = Field(default_factory=list)
    uses_telehealth_already: bool = False
    pitch_angle: str = Field(
        description="One sentence on why white-label telehealth infrastructure would help this specific business"
    )


def available() -> bool:
    if not settings.anthropic_key:
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


def build_corpus(snapshot: SiteSnapshot) -> str:
    """The page text Claude reads, capped at MAX_CHARS (homepage first)."""
    pages = []
    budget = MAX_CHARS
    for p in snapshot.pages:
        chunk = p.text[: min(len(p.text), budget)]
        pages.append(f"### {p.url}\n{chunk}")
        budget -= len(chunk)
        if budget <= 0:
            break
    return "\n\n".join(pages)


def corpus_hash(corpus: str) -> str:
    return hashlib.sha256(corpus.encode("utf-8")).hexdigest()


def _to_people(extracted: list[ExtractedPerson]) -> list[Person]:
    return [
        Person(
            name=p.name.strip(),
            title=p.title,
            email=(p.email or "").lower() or None,
            linkedin=p.linkedin,
            source_url="claude",
            confidence=0.75,
            is_decision_maker=bool(p.title and DECISION_RE.search(p.title)),
        )
        for p in extracted
        if p.name.strip()
    ]


def _from_cache(cached: dict[str, Any] | None, digest: str) -> tuple[list[Person], dict[str, Any]] | None:
    """Reuse an earlier answer when it was made from exactly the same page text."""
    if not cached or cached.get("corpus_sha256") != digest:
        return None
    try:
        extracted = [ExtractedPerson.model_validate(p) for p in cached.get("people", [])]
    except ValueError:
        return None
    return _to_people(extracted), cached


def extract(
    snapshot: SiteSnapshot, cached: dict[str, Any] | None = None
) -> tuple[list[Person], dict[str, Any]] | None:
    """Ask Claude for decision-makers and a summary; `cached` is the lead's previous source_detail["ai"]."""
    corpus = build_corpus(snapshot)
    digest = corpus_hash(corpus)
    hit = _from_cache(cached, digest)
    if hit:
        log.debug("claude extraction cache hit for %s", snapshot.domain)
        return hit
    if not available():
        return None
    import anthropic

    client = anthropic.Anthropic(api_key=settings.anthropic_key)
    prompt = (
        "You are a B2B sales researcher for MyOrbit Health, a white-label telehealth infrastructure provider "
        "(licensed providers in 50 states, pharmacy fulfilment, patient intake). Read the crawled pages below for "
        f"the business at {snapshot.domain} and extract the owner / founder / executives / medical director with "
        "their titles, only when the page text supports it. Never invent names or emails. Keep the summary factual.\n\n"
        f"{corpus}"
    )
    try:
        response = client.beta.messages.create(
            model=settings.llm_model,
            max_tokens=4000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={
                "effort": "low",
                "format": {"type": "json_schema", "schema": Extraction.model_json_schema()},
            },
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.APIStatusError as exc:
        log.warning("claude extraction failed for %s: %s", snapshot.domain, exc)
        return None
    except anthropic.APIConnectionError as exc:
        log.warning("claude unreachable: %s", exc)
        return None

    if response.stop_reason == "refusal":
        log.info("claude declined %s", snapshot.domain)
        return None
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        data = Extraction.model_validate(json.loads(text))
    except (ValueError, TypeError) as exc:
        log.warning("claude returned unparseable output for %s: %s", snapshot.domain, exc)
        return None

    meta = data.model_dump(exclude={"people"})
    meta["people"] = [p.model_dump() for p in data.people]
    meta["corpus_sha256"] = digest
    meta["model"] = settings.llm_model
    return _to_people(data.people), meta
