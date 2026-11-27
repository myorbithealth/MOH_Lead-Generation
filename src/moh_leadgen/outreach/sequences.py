"""Segments, sequences and the copy we send.

One Smartlead campaign per segment, plus one 'generic' campaign for role mailboxes (info@, hello@).

How variants reach Smartlead: each campaign's sequence is a set of placeholders
({{step1_subject}}, {{step1_body}} ... {{step4_body}}). At enrolment the repo picks a variant for
the lead, renders its text with the lead's values and stores the result in that lead's custom
fields. That gives per-lead variant choice (Thompson sampling, a control floor) inside one campaign
per segment, and the variant a lead got on day 0 is the one it gets on day 14. Smartlead's own A/B
split would pick the variant itself, and we could not record it at enrolment.

Copy rules (checked by tests): plain text, step 1 under 150 words and follow-ups under 90 (footer
excluded), B2B framing, no medical outcome claims, no specific prices (the price-beat guarantee is
the only money talk), one ask (a 20-minute call via {{booking_link}}), subjects 3-6 lowercase words. The footer is appended to every rendered email.

Unsubscribe: Smartlead's docs name no unsubscribe merge tag, so the opt-out link comes from the
campaign-level `unsubscribe_text` setting (plus `add_unsubscribe_tag`). The footer also says that
replying "stop" works, and the reply classifier treats that as an unsubscribe.
"""

from __future__ import annotations

import html
import re
import uuid
from dataclasses import dataclass, field

from ..models import Lead
from .models import SEGMENTS, Variant

# Stable ids, so the same (segment, step, name) always maps to the same portal variant row.
VARIANT_NAMESPACE = uuid.UUID("6f0c6a4e-2d55-4b8e-9f1e-4d7a6c0b5e21")

GENERIC_CAMPAIGN = "generic"
CAMPAIGN_KEYS: tuple[str, ...] = (*SEGMENTS, GENERIC_CAMPAIGN)
CAMPAIGN_PREFIX = "MOH outbound"
WEBHOOK_NAME = "moh-portal-smartlead-webhook"

UNSUBSCRIBE_TEXT = "Unsubscribe from these emails"
FOOTER = (
    "--\n"
    "{{company_name_legal}}, {{postal_address}}\n"
    "Not relevant for you? Use the unsubscribe link below and I will not write again."
)

# vertical -> segment. Keys are vertical prefixes so 'med_spa_aesthetics' matches 'med_spa'.
_TRT_PEPTIDES = ("mens_health", "peptides")
_MED_SPA_CLINIC = ("med_spa", "fitness", "gym", "primary_care", "womens_health")
_PHARMACY_GENERAL = ("online_pharmacy", "pharmacy", "telehealth_general", "cbd")

VERTICAL_PHRASES = {
    "weight_loss_glp1": "weight-loss programs",
    "mens_health_trt": "men's health clinics",
    "peptides_longevity": "longevity programs",
    "med_spa": "med spas",
    "fitness_gym": "gyms and fitness studios",
    "primary_care": "primary care practices",
    "womens_health": "women's health practices",
    "online_pharmacy": "online pharmacies",
    "telehealth_general": "telehealth brands",
    "cbd": "wellness brands",
    "mental_health": "mental health practices",
    "addiction": "recovery programs",
}
DEFAULT_VERTICAL_PHRASE = "health and wellness brands"
DEFAULT_PLATFORM = "your current setup"


def _starts(vertical: str, prefixes: tuple[str, ...]) -> bool:
    return any(vertical.startswith(p) for p in prefixes)


def lead_platform(lead: Lead) -> str | None:
    """The competitor platform we fingerprinted, else the one the lead was seeded from."""
    return lead.competitor_platform or lead.source_detail.get("platform") or None


def assign_segment(lead: Lead) -> str:
    """Which campaign a lead belongs to. Order matters: the first match wins.

    glp1_competitor: weight-loss/GLP-1 brands, and peptide brands running on a competitor platform.
    trt_peptides: men's health and peptide/longevity brands without a platform.
    med_spa_clinic: med spas, gyms, primary care, women's health, and Places (local) leads.
    pharmacy_general: online pharmacies, general telehealth, CBD.
    other: everything else (mental health, addiction, unknown vertical).
    """
    vertical = (lead.vertical or "").lower()
    if vertical.startswith("weight_loss") or ("peptide" in vertical and lead_platform(lead)):
        return "glp1_competitor"
    if _starts(vertical, _TRT_PEPTIDES):
        return "trt_peptides"
    if _starts(vertical, _MED_SPA_CLINIC) or lead.source == "local_business":
        return "med_spa_clinic"
    if _starts(vertical, _PHARMACY_GENERAL):
        return "pharmacy_general"
    return "other"


def vertical_phrase(lead: Lead) -> str:
    vertical = (lead.vertical or "").lower()
    for key, phrase in VERTICAL_PHRASES.items():
        if vertical.startswith(key) or key.startswith(vertical or "~"):
            return phrase
    return DEFAULT_VERTICAL_PHRASE


# ------------------------------------------------------------------ copy
@dataclass(frozen=True)
class VariantDef:
    name: str
    angle: str
    subject: str  # empty for follow-ups: Smartlead threads them as "Re: <step 1 subject>"
    body: str
    hypothesis: str
    status: str = "active"  # 'control' for the control arm


@dataclass(frozen=True)
class StepDef:
    step: int
    day: int  # days after enrolment; Smartlead wants the gap from the previous step
    variants: tuple[VariantDef, ...]


@dataclass(frozen=True)
class CampaignDef:
    key: str  # a segment name, or 'generic'
    steps: tuple[StepDef, ...]
    hypotheses: dict[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return f"{CAMPAIGN_PREFIX} - {self.key}"

    @property
    def step_count(self) -> int:
        return len(self.steps)


# The pitch, per the founder: beat what they pay today for consults and for medication, or we
# integrate their current pharmacy for them; they own the processor and the data; no markup on
# medication; we are a physician network that does the consults; patients get multiple refills;
# everything is customisable. One proof line per segment colours the ask.
_PROOF = {
    "glp1_competitor": "You keep your storefront, pricing and patient list; our physician network runs "
    "consults, refills and pharmacy fulfilment behind it, with no markup on the medication.",
    "trt_peptides": "Our physician network runs consults and ongoing refills, and partner pharmacies "
    "ship under your brand with no markup on the medication.",
    "med_spa_clinic": "A clinic or gym can offer prescription programs without hiring prescribers or "
    "setting up a pharmacy, and keeps the patient relationship.",
    "pharmacy_general": "We can sit behind your brand as the provider and intake layer, or run the "
    "whole stack; either way the processor and the data stay yours.",
    "other": "We can run the whole stack, or only the provider and pharmacy layer behind what you "
    "already have; the processor and the data stay yours.",
}

_GUARANTEE = (
    "Our offer is simple: whatever you pay today for consults and for medication, we will beat it. "
    "If we cannot, we will integrate your current pharmacy for you at no charge."
)
_OWN = "You own the payment processor and your patient data. We never mark up the medication."

_SIGN_OFF = "{{sender_name}}"
_ASK_DEMO = "Worth 20 minutes to check the numbers? Here is my calendar: {{booking_link}}"


def _step1_variants(segment: str) -> tuple[VariantDef, ...]:
    proof = _PROOF[segment]
    return (
        VariantDef(
            name="price_beat",
            angle="price_beat_guarantee",
            subject="your {{platform}} pricing",
            body=(
                "Hi {{first_name}},\n\n{{opener}}\n\n"
                f"{_GUARANTEE} MyOrbit Health is white-label telehealth infrastructure for "
                f"{{{{vertical_phrase}}}}: a physician network that does the consults, pharmacy "
                "fulfilment, intake and checkout, all under your brand and fully customisable.\n\n"
                f"{_ASK_DEMO}\n\n{_SIGN_OFF}"
            ),
            hypothesis="A price-beat guarantee on consults and meds is the sharpest reason to take a call.",
        ),
        VariantDef(
            name="own_the_stack",
            angle="ownership_no_markup",
            subject="who owns your patient data",
            body=(
                "Hi {{first_name}},\n\n{{opener}}\n\n"
                "Most platforms sit between you and your patients: their processor, their data, a "
                f"margin on every order. MyOrbit Health is built the other way round. {_OWN} {proof}\n\n"
                f"{_GUARANTEE}\n\n"
                f"Open to a 20-minute look at the numbers? {{{{booking_link}}}}\n\n{_SIGN_OFF}"
            ),
            hypothesis="Ownership of processor and data is the pain that makes operators switch.",
        ),
        VariantDef(
            name="better_medicine",
            angle="clinical_quality_refills",
            subject="consults, refills and margin",
            body=(
                "Hi {{first_name}},\n\n{{opener}}\n\n"
                "Two things most {{vertical_phrase}} tell us: patients churn when refills are a fight, "
                "and the platform takes a cut of every consult and every order. Our physician network "
                "does real consults, gives patients multiple refills, and charges no markup on the "
                "medication; everything from intake to pricing is customisable.\n\n"
                f"{_GUARANTEE}\n\n"
                f"Worth a 20-minute call? {{{{booking_link}}}}\n\n{_SIGN_OFF}"
            ),
            hypothesis="Clinical quality plus refills speaks to operators who care about retention.",
        ),
        VariantDef(
            name="control",
            angle="plain",
            subject="white-label telehealth for {{company_name}}",
            body=(
                "Hi {{first_name}},\n\n{{opener}}\n\n"
                "MyOrbit Health runs the telehealth back end for {{vertical_phrase}}: a physician "
                "network for consults, pharmacy fulfilment, intake and checkout, all under your brand. "
                f"{_GUARANTEE}\n\n"
                f"Could I show you how it works in a 20-minute demo? {{{{booking_link}}}}\n\n{_SIGN_OFF}"
            ),
            hypothesis="Plain description with the guarantee; the baseline every angle is measured against.",
            status="control",
        ),
    )


def _follow_ups(segment: str) -> tuple[StepDef, ...]:
    proof = _PROOF[segment]
    return (
        StepDef(
            2,
            3,
            (
                VariantDef(
                    name="proof",
                    angle="proof",
                    subject="",
                    body=(
                        "Hi {{first_name}},\n\n"
                        f"One specific on how this would work for {{{{company_name}}}}: {proof}\n\n"
                        "Bring your current per-consult and per-order costs to the call and we will "
                        f"show you the comparison live: {{{{booking_link}}}}\n\n{_SIGN_OFF}"
                    ),
                    hypothesis="A concrete how-it-works line plus a live comparison earns replies.",
                ),
            ),
        ),
        StepDef(
            3,
            7,
            (
                VariantDef(
                    name="angle_switch",
                    angle="one_partner",
                    subject="",
                    body=(
                        "Hi {{first_name}},\n\n"
                        "A different way to look at it: instead of a platform fee, a processor you do "
                        "not control and a markup on every order, {{company_name}} would work with one "
                        f"physician network and keep the processor, the data and the margin. {_OWN}\n\n"
                        f"Happy to walk through it in 20 minutes: {{{{booking_link}}}}\n\n{_SIGN_OFF}"
                    ),
                    hypothesis="Vendor consolidation and ownership land with operators who skipped the first angle.",
                ),
            ),
        ),
        StepDef(
            4,
            14,
            (
                VariantDef(
                    name="close_loop",
                    angle="close_loop",
                    subject="",
                    body=(
                        "Hi {{first_name}},\n\n"
                        "I will stop here so I do not clutter your inbox. The guarantee stands: if we "
                        "cannot beat what {{company_name}} pays for consults and medication, we will "
                        "integrate your pharmacy for you at no charge. A 20-minute call is one click "
                        f"away: {{{{booking_link}}}}\n\n{_SIGN_OFF}"
                    ),
                    hypothesis="A polite last note with the guarantee gets the 'not now, but later' replies.",
                ),
            ),
        ),
    )


def _segment_campaign(segment: str) -> CampaignDef:
    return CampaignDef(key=segment, steps=(StepDef(1, 0, _step1_variants(segment)), *_follow_ups(segment)))


def _generic_campaign() -> CampaignDef:
    """Role mailboxes: two steps (day 0, day 5), addressed to the business, asks for a referral."""
    intro = VariantDef(
        name="generic_intro",
        angle="referral_ask",
        subject="telehealth for {{company_name}}",
        body=(
            "Hi {{company_name}} team,\n\n{{opener}}\n\n"
            "Could you point me to whoever looks after partnerships or operations? MyOrbit Health is "
            "white-label telehealth infrastructure for {{vertical_phrase}}: a physician network for "
            "consults, pharmacy fulfilment with no markup on medication, intake and checkout under your "
            "brand. We beat what you pay today for consults and medication, or we integrate your "
            "pharmacy for you.\n\n"
            f"If it is easier, they can book a 20-minute demo directly: {{{{booking_link}}}}\n\n{_SIGN_OFF}"
        ),
        hypothesis="A shared inbox forwards a short, specific referral ask.",
        status="control",
    )
    close = VariantDef(
        name="generic_close",
        angle="close_loop",
        subject="",
        body=(
            "Hi again,\n\n"
            "Just checking this reached the right person at {{company_name}}. If white-label telehealth "
            f"is ever on the list, a 20-minute demo is here: {{{{booking_link}}}}\n\n{_SIGN_OFF}"
        ),
        hypothesis="One reminder is enough for a shared inbox.",
    )
    return CampaignDef(key=GENERIC_CAMPAIGN, steps=(StepDef(1, 0, (intro,)), StepDef(2, 5, (close,))))


CAMPAIGNS: dict[str, CampaignDef] = {
    **{segment: _segment_campaign(segment) for segment in SEGMENTS},
    GENERIC_CAMPAIGN: _generic_campaign(),
}


# ------------------------------------------------------------------ rendering
_TAG = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def render(template: str, fields: dict[str, str]) -> str:
    """Fill {{tags}} from `fields`. An unknown first name drops the name, not the greeting."""
    if not fields.get("first_name"):
        template = template.replace(" {{first_name}}", "")
    return _TAG.sub(lambda m: str(fields.get(m.group(1)) or ""), template)


def with_footer(body: str) -> str:
    return f"{body}\n\n{FOOTER}"


def as_email_html(text: str) -> str:
    """Plain text to the minimal HTML Smartlead expects in email_body (line breaks kept)."""
    return html.escape(text, quote=False).replace("\n", "<br>")


def word_count(text: str) -> int:
    return len(re.findall(r"\S+", text))


def subject_problems(subject: str) -> list[str]:
    """Why a step-1 subject breaks the house rules (empty list = fine)."""
    problems = []
    static = _TAG.sub("x", subject)
    if not 3 <= word_count(static) <= 6:
        problems.append("subject must be 3-6 words")
    if static != static.lower():
        problems.append("subject must be lowercase")
    if any(ch in static for ch in "!?$%") or static.lower().startswith(("re:", "fwd:")):
        problems.append("subject looks like clickbait")
    return problems


# ------------------------------------------------------------------ exports
def variant_id(segment: str, step: int, name: str) -> str:
    return str(uuid.uuid5(VARIANT_NAMESPACE, f"{segment}:{step}:{name}"))


def variant_label(step: int, name: str) -> str:
    """The label stored in variants.smartlead_variant_label (also used in logs and utm_content)."""
    return f"s{step}_{name}"


def campaign_variants(key: str, smartlead_campaign_id: int | None = None) -> list[Variant]:
    """Every variant of one campaign as outreach.models.Variant rows for the portal `variants` table."""
    campaign = CAMPAIGNS[key]
    rows = []
    for step in campaign.steps:
        for v in step.variants:
            rows.append(
                Variant(
                    id=variant_id(key, step.step, v.name),
                    segment=key,
                    sequence_step=step.step,
                    name=v.name,
                    angle=v.angle,
                    subject=v.subject or None,
                    body=v.body,
                    hypothesis=v.hypothesis,
                    status="control" if v.status == "control" else "active",
                    smartlead_campaign_id=smartlead_campaign_id,
                )
            )
    return rows


def all_variants() -> list[Variant]:
    return [v for key in CAMPAIGN_KEYS for v in campaign_variants(key)]


def smartlead_sequences(campaign: CampaignDef) -> list[dict]:
    """The `sequences` payload for POST /campaigns/{id}/sequences: placeholders filled per lead."""
    out = []
    previous_day = 0
    for step in campaign.steps:
        out.append(
            {
                "id": None,
                "seq_number": step.step,
                "seq_delay_details": {"delay_in_days": step.day - previous_day},
                "subject": "{{step1_subject}}" if step.step == 1 else "",
                "email_body": f"{{{{step{step.step}_body}}}}",
            }
        )
        previous_day = step.day
    return out
