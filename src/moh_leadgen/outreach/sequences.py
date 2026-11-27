"""Segments, sequences and the copy we send.

One Smartlead campaign per segment, plus one 'generic' campaign for role mailboxes (info@, hello@).

How variants reach Smartlead: each campaign's sequence is a set of placeholders
({{step1_subject}}, {{step1_body}} ... {{step4_body}}). At enrolment the repo picks a variant for
the lead, renders its text with the lead's values and stores the result in that lead's custom
fields. That gives per-lead variant choice (Thompson sampling, a control floor) inside one campaign
per segment, and the variant a lead got on day 0 is the one it gets on day 14. Smartlead's own A/B
split would pick the variant itself, and we could not record it at enrolment.

Copy rules (checked by tests): plain text, step 1 under 120 words and follow-ups under 80 (footer
excluded), B2B framing, no medical claims, no pricing, one ask (a 20-minute demo via
{{booking_link}}), subjects 3-6 lowercase words. The footer is appended to every rendered email.

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


# One proof line per segment: step 1 (speed/displacement angles) and step 2 reuse it.
_PROOF = {
    "glp1_competitor": "You keep your storefront, pricing and patient list; we run providers, "
    "pharmacy and intake behind it.",
    "trt_peptides": "Our providers handle consults and follow-ups for ongoing programs, and partner "
    "pharmacies ship under your brand.",
    "med_spa_clinic": "It lets a clinic or gym offer telehealth programs without hiring prescribers "
    "or setting up a pharmacy.",
    "pharmacy_general": "It can sit behind an existing brand as the provider and intake layer, or run "
    "the whole stack.",
    "other": "It can run the whole stack, or just the provider and pharmacy layer behind what you "
    "already have.",
}

_SIGN_OFF = "{{sender_name}}"
_ASK_DEMO = "Worth a 20-minute demo? Here is my calendar: {{booking_link}}"


def _step1_variants(segment: str) -> tuple[VariantDef, ...]:
    proof = _PROOF[segment]
    return (
        VariantDef(
            name="competitor_displacement",
            angle="competitor_displacement",
            subject="question about {{platform}}",
            body=(
                "Hi {{first_name}},\n\n{{opener}}\n\n"
                "If you have ever wanted more control over providers, pharmacy and margin than "
                "{{platform}} gives you, that is the gap MyOrbit Health fills: white-label telehealth "
                f"infrastructure for {{{{vertical_phrase}}}}. {proof}\n\n"
                f"{_ASK_DEMO}\n\n{_SIGN_OFF}"
            ),
            hypothesis="Naming the platform they already use makes the offer concrete.",
        ),
        VariantDef(
            name="compliance_legitscript",
            angle="compliance_legitscript",
            subject="legitscript and pharmacy partners",
            body=(
                "Hi {{first_name}},\n\n{{opener}}\n\n"
                "LegitScript and payment processors look closely at who your prescribers and pharmacy "
                "are. MyOrbit Health gives {{vertical_phrase}} a white-label provider network, pharmacy "
                "fulfilment and checkout set up with that review in mind, so your team can stay on the "
                "brand.\n\n"
                f"Open to a 20-minute demo? Pick a time here: {{{{booking_link}}}}\n\n{_SIGN_OFF}"
            ),
            hypothesis="Compliance worry is a sharper pain than cost for regulated DTC brands.",
        ),
        VariantDef(
            name="speed_to_launch",
            angle="speed_to_launch",
            subject="launch time for new programs",
            body=(
                "Hi {{first_name}},\n\n{{opener}}\n\n"
                "Adding a new program can take months of finding providers, a pharmacy and a checkout "
                "that work together. MyOrbit Health is that stack, white-labelled: providers, pharmacy "
                f"fulfilment, intake and payments under the {{{{company_name}}}} name. {proof}\n\n"
                f"Would a 20-minute demo be useful? {{{{booking_link}}}}\n\n{_SIGN_OFF}"
            ),
            hypothesis="Time to launch matters more than switching cost for growing brands.",
        ),
        VariantDef(
            name="control",
            angle="plain",
            subject="white-label telehealth for {{company_name}}",
            body=(
                "Hi {{first_name}},\n\n{{opener}}\n\n"
                "MyOrbit Health runs the telehealth back end for {{vertical_phrase}}: licensed "
                "providers, pharmacy fulfilment, intake and checkout, all under your brand.\n\n"
                f"Could I show you how it works in a 20-minute demo? {{{{booking_link}}}}\n\n{_SIGN_OFF}"
            ),
            hypothesis="Plain description; the baseline every angle is measured against.",
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
                        f"If it is worth a look, here is a 20-minute slot: {{{{booking_link}}}}\n\n{_SIGN_OFF}"
                    ),
                    hypothesis="A concrete how-it-works line earns replies the opener missed.",
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
                        "A different way to look at it: instead of separate contracts for prescribers, a "
                        "pharmacy and a payment processor, {{company_name}} would work with one partner "
                        "and one point of contact.\n\n"
                        f"Happy to walk through it in 20 minutes: {{{{booking_link}}}}\n\n{_SIGN_OFF}"
                    ),
                    hypothesis="Vendor consolidation lands with operators who skipped the first angle.",
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
                        "I will stop here so I do not clutter your inbox. If white-label telehealth "
                        "infrastructure becomes a priority for {{company_name}}, a 20-minute demo is "
                        f"one click away: {{{{booking_link}}}}\n\n{_SIGN_OFF}"
                    ),
                    hypothesis="A polite last note gets the 'not now, but later' replies.",
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
            "white-label telehealth infrastructure for {{vertical_phrase}}: licensed providers, pharmacy "
            "fulfilment, intake and checkout under your brand.\n\n"
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
