"""LegitScript certification evidence + vertical inference.

LegitScript's own lookup page is reCAPTCHA-gated, so we never query it programmatically.
Instead we look for the official seal that certified merchants embed on their own site:

    <a href="https://www.legitscript.com/websites/?checker_keywords=example.com">
      <img src="https://static.legitscript.com/seals/1234567.png" ...>
    </a>

Newer seals load `https://static.legitscript.com/seals/<id>.js`. Either is strong evidence;
`verify_url` lets a human confirm in one click.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from ..models import LegitScriptEvidence
from .site_crawler import SiteSnapshot

SEAL_RE = re.compile(r"https?://static\.legitscript\.com/seals/(\d+)\.(?:png|js|gif|svg)", re.IGNORECASE)
VERIFY_RE = re.compile(
    r"https?://(?:www\.)?legitscript\.com/(?:websites/\?checker_keywords=|websites/|pharmacy/)([A-Za-z0-9.\-]+)",
    re.IGNORECASE,
)
MENTION_RE = re.compile(
    r"legitscript[\s\-]*(certified|certification|approved|verified|monitored)|(certified|approved|verified)\s+by\s+legitscript",
    re.IGNORECASE,
)

VERTICALS: list[tuple[str, re.Pattern[str], int]] = [
    (
        "weight_loss_glp1",
        re.compile(
            r"\b(glp-?1|semaglutide|tirzepatide|ozempic|wegovy|mounjaro|zepbound|weight[\s\-]*loss|medical weight)\b",
            re.IGNORECASE,
        ),
        3,
    ),
    (
        "mens_health_trt",
        re.compile(
            r"\b(trt|testosterone|erectile|ed treatment|hair loss|finasteride|men'?s health|low t)\b",
            re.IGNORECASE,
        ),
        2,
    ),
    (
        "peptides_longevity",
        re.compile(
            r"\b(peptides?|longevity|anti[\s\-]*aging|nad\+|bpc-?157|sermorelin|hormone optimization|hrt)\b",
            re.IGNORECASE,
        ),
        2,
    ),
    (
        "telehealth_general",
        re.compile(
            r"\b(telehealth|telemedicine|virtual (care|visit|consult)|online (doctor|prescription|consult)|licensed (provider|physician)s?)\b",
            re.IGNORECASE,
        ),
        1,
    ),
    (
        "online_pharmacy",
        re.compile(
            r"\b(online pharmacy|compounding pharmacy|compounded|pharmacy|prescriptions? (delivered|shipped)|mail[\s\-]*order)\b",
            re.IGNORECASE,
        ),
        2,
    ),
    (
        "addiction_treatment",
        re.compile(
            r"\b(addiction|rehab|recovery center|detox|substance (use|abuse)|sober living|suboxone|mat program)\b",
            re.IGNORECASE,
        ),
        2,
    ),
    (
        "mental_health",
        re.compile(
            r"\b(therapy|therapist|psychiatr|mental health|anxiety|depression|adhd|ketamine)\b", re.IGNORECASE
        ),
        1,
    ),
    (
        "med_spa_aesthetics",
        re.compile(
            r"\b(med[\s\-]*spa|medspa|botox|fillers?|aesthetics?|laser (hair|skin)|microneedling|iv (therapy|drip|hydration))\b",
            re.IGNORECASE,
        ),
        2,
    ),
    (
        "womens_health",
        re.compile(r"\b(women'?s health|menopause|birth control|fertility|pcos)\b", re.IGNORECASE),
        1,
    ),
    (
        "primary_care_clinic",
        re.compile(
            r"\b(primary care|urgent care|family medicine|family practice|clinic|physician'?s office)\b",
            re.IGNORECASE,
        ),
        1,
    ),
    (
        "fitness_gym",
        re.compile(r"\b(gym|fitness|personal train|crossfit|boot ?camp|membership)\b", re.IGNORECASE),
        1,
    ),
    ("dental", re.compile(r"\b(dentist|dental|orthodont)\b", re.IGNORECASE), 1),
    ("cbd_supplements", re.compile(r"\b(cbd|supplements?|nutraceutical|vitamins?)\b", re.IGNORECASE), 1),
]


def detect_legitscript(snapshot: SiteSnapshot) -> LegitScriptEvidence:
    ev = LegitScriptEvidence(status="not_found", checked_at=datetime.now(UTC))
    html = snapshot.all_html()
    text = snapshot.all_text()

    seal_ids = sorted(set(SEAL_RE.findall(html)))
    verify = VERIFY_RE.search(html)
    if seal_ids or verify:
        ev.status = "seal_detected"
        ev.seal_ids = seal_ids
        if verify:
            ev.verify_url = verify.group(0)
        else:
            ev.verify_url = f"https://www.legitscript.com/websites/?checker_keywords={snapshot.domain}"
        for m in SEAL_RE.finditer(html):
            ev.snippets.append(html[max(0, m.start() - 120) : m.end() + 60].replace("\n", " ")[:220])
            if len(ev.snippets) >= 2:
                break
        return ev

    mention = MENTION_RE.search(text)
    if mention:
        ev.status = "mentioned"
        ev.verify_url = f"https://www.legitscript.com/websites/?checker_keywords={snapshot.domain}"
        ev.snippets.append(text[max(0, mention.start() - 120) : mention.end() + 120].replace("\n", " "))
    return ev


def infer_vertical(text: str) -> tuple[str | None, dict[str, int]]:
    """Returns (best vertical, raw keyword hit counts per vertical)."""
    scores: dict[str, int] = {}
    sample = text[:60_000]
    for name, rx, weight in VERTICALS:
        hits = len(rx.findall(sample))
        if hits:
            scores[name] = hits * weight
    if not scores:
        return None, {}
    best = max(scores.items(), key=lambda kv: kv[1])[0]
    return best, scores
