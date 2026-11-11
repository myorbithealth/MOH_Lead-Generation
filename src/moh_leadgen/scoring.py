"""ICP fit score (0-100) for MyOrbit Health's white-label telehealth offer, with reasons.

Tunable: edit the weights, re-run `leadgen score` - no re-crawl needed.
"""

from __future__ import annotations

from datetime import UTC, datetime

from .domains import is_enterprise
from .models import Lead

VERTICAL_WEIGHTS = {
    "weight_loss_glp1": 25,
    "mens_health_trt": 22,
    "peptides_longevity": 22,
    "online_pharmacy": 18,
    "telehealth_general": 18,
    "med_spa_aesthetics": 20,
    "womens_health": 15,
    "addiction_treatment": 10,
    "mental_health": 10,
    "primary_care_clinic": 12,
    "fitness_gym": 10,
    "cbd_supplements": 6,
    "dental": 2,
}


def score_lead(lead: Lead) -> Lead:
    score = 0
    reasons: list[str] = []

    def add(points: int, why: str) -> None:
        nonlocal score
        score += points
        reasons.append(f"{'+' if points >= 0 else ''}{points} {why}")

    # Certification / compliance posture
    if lead.legitscript.status == "seal_detected":
        add(25, "LegitScript seal on site (ad-eligible, compliance-minded)")
    elif lead.legitscript.status == "mentioned":
        add(12, "mentions LegitScript certification")

    # Vertical fit
    if lead.vertical:
        add(VERTICAL_WEIGHTS.get(lead.vertical, 5), f"vertical: {lead.vertical}")

    # Buying signals
    if lead.competitor_platform:
        add(15, f"already on a competing platform ({lead.competitor_platform})")
    ads = [t for t in lead.tech_stack if t in ("Google Ads", "Meta Pixel", "TikTok Pixel")]
    if ads:
        add(6, f"running paid acquisition ({', '.join(ads)})")
    if any(t in lead.tech_stack for t in ("Shopify", "WooCommerce", "Stripe")):
        add(4, "sells online (checkout detected)")

    # Reachability
    if lead.owner_name:
        add(
            12,
            f"decision-maker identified: {lead.owner_name}"
            + (f" ({lead.owner_title})" if lead.owner_title else ""),
        )
    if lead.owner_email and lead.owner_email_status in ("verified", "provider", "on_site"):
        add(12, f"decision-maker email ({lead.owner_email_status})")
    elif lead.owner_email:
        add(4, "decision-maker email guessed from company pattern")
    elif lead.primary_email:
        add(2, "only a role mailbox found")
    if lead.owner_phone:
        add(8, "decision-maker direct phone")
    if lead.primary_phone:
        add(6, "phone number found")
    if lead.owner_linkedin:
        add(3, "LinkedIn profile for outreach")
    if lead.has_mx is False:
        add(-10, "no MX record (email undeliverable)")

    # Size / stage heuristics
    if is_enterprise(lead.domain):
        add(-30, "enterprise brand, unlikely to switch infrastructure")
    size = (lead.source_detail.get("ai") or {}).get("estimated_size")
    if size in ("small (2-10)", "mid (11-50)"):
        add(6, f"growth-stage size ({size})")
    elif size == "large (50+)":
        add(-8, "large org, long sales cycle")
    if lead.domain_created:
        try:
            created = datetime.fromisoformat(lead.domain_created)
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            age = (datetime.now(UTC) - created).days / 365
            if 1 <= age <= 7:
                add(4, f"domain age {age:.1f}y (established, still scaling)")
            elif age < 1:
                add(2, "new domain (launch stage)")
        except ValueError:
            pass

    if not lead.pages_crawled:
        add(-25, "site unreachable")
    redirected = lead.source_detail.get("redirected_to")
    if redirected and redirected.rsplit(".", 1)[0] != lead.domain.rsplit(".", 1)[0]:
        add(-15, f"domain redirects to a different brand ({redirected})")

    lead.score = max(0, min(100, score))
    lead.score_reasons = reasons
    return lead
