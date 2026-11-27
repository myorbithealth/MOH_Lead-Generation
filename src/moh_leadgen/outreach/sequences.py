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
the only money talk, and never in 'fda_compliance'), one ask (a 20-minute call via {{booking_link}}), subjects 3-6 lowercase words. The footer is appended to every rendered email.

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
from .models import FDA_LIST_VIA_PREFIX, FDA_SEGMENT, SEGMENTS, Variant

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


def is_fda_list_lead(lead: Lead) -> bool:
    """True for a lead the FDA warning-letter seed list found (first or as a corroborating source)."""
    vias = [lead.discovered_via or "", *lead.also_found_via]
    return bool(lead.source_detail.get("fda_list")) or any(v.startswith(FDA_LIST_VIA_PREFIX) for v in vias)


def assign_segment(lead: Lead) -> str:
    """Which campaign a lead belongs to. Order matters: the first match wins.

    fda_compliance: anything from the FDA warning-letter seed list, before every other rule. These
        get compliance-only copy (no price-beat pitch) and need a human review before the gate
        lets them through.
    glp1_competitor: weight-loss/GLP-1 brands, and peptide brands running on a competitor platform.
    trt_peptides: men's health and peptide/longevity brands without a platform.
    med_spa_clinic: med spas, gyms, primary care, women's health, and Places (local) leads.
    pharmacy_general: online pharmacies, general telehealth, CBD.
    other: everything else (mental health, addiction, unknown vertical).
    """
    if is_fda_list_lead(lead):
        return FDA_SEGMENT
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
    "glp1_competitor": "you keep your storefront, pricing and patient list; our physician network runs "
    "consults and refills behind it, and the pharmacy can stay yours or come from our "
    "LegitScript-reviewed partners, with no markup on the medication.",
    "trt_peptides": "our physician network runs consults and ongoing refills, and the pharmacy can stay "
    "yours or come from our LegitScript-reviewed partners, shipping under your brand with no markup "
    "on the medication.",
    "med_spa_clinic": "a med spa or clinic can offer prescription programs without hiring prescribers or "
    "setting up a pharmacy, and keeps the patient relationship.",
    "pharmacy_general": "we can sit behind your brand as the provider and intake layer, or run the "
    "whole stack; either way the processor and the data stay yours.",
    "other": "we can run the whole stack, or only the provider and pharmacy layer behind what you "
    "already have; the processor and the data stay yours.",
    FDA_SEGMENT: "every patient gets a real consult with a prescriber licensed in their state, and "
    "orders ship from LegitScript-reviewed partner pharmacies under your brand.",
}

_GUARANTEE = (
    "Here's the offer: whatever you pay today per consult and per order, we'll beat it. One physician "
    "fee covers a patient's consults for every medication, there's no markup on the meds, and if we "
    "can't beat your numbers we'll integrate your current pharmacy for you, no charge."
)
_OWN = "You keep your own payment processor and your patient data. We don't mark up the medication."

# The sign-off is not rendered into the body: Smartlead fills %signature% in the sequence template
# from the sending mailbox, so each persona signs its own emails (see smartlead_sequences).
_SIGN_OFF = ""
SIGNATURE_TAG = "%signature%"
_ASK_DEMO = "Worth 20 minutes to run your numbers side by side? {{booking_link}}"


def _step1_variants(segment: str) -> tuple[VariantDef, ...]:
    return (
        VariantDef(
            name="price_beat",
            angle="price_beat_guarantee",
            subject="your per-consult costs",
            body=(f"Hi {{{{first_name}}}},\n\n{{{{opener}}}}\n\n{_GUARANTEE}\n\n{_ASK_DEMO}\n\n{_SIGN_OFF}"),
            hypothesis="A price-beat guarantee on consults and meds is the sharpest reason to take a call.",
        ),
        VariantDef(
            name="own_the_stack",
            angle="ownership_no_markup",
            subject="who owns your patient data",
            body=(
                "Hi {{first_name}},\n\n{{opener}}\n\n"
                f"{_OWN} Our physicians charge one fee per patient that covers every medication, not a "
                "consult fee per med. And if we can't beat what you pay today, we'll integrate your "
                "pharmacy for you at no charge.\n\n"
                f"Worth 20 minutes? {{{{booking_link}}}}\n\n{_SIGN_OFF}"
            ),
            hypothesis="Ownership of processor and data is the pain that makes operators switch.",
        ),
        VariantDef(
            name="better_medicine",
            angle="clinical_quality_refills",
            subject="consults, refills, margin",
            body=(
                "Hi {{first_name}},\n\n{{opener}}\n\n"
                "Our physicians do real consults and patients get multiple refills, so they stay. One "
                "physician fee covers every medication, no markup on the meds, and intake, pricing and "
                "the program list are yours to set. If we can't beat what you pay today, we'll integrate "
                "your pharmacy for you at no charge.\n\n"
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
                "MyOrbit Health runs the back end: our physicians for consults, pharmacy fulfilment, "
                f"intake and checkout, all under your brand. {_GUARANTEE}\n\n"
                f"Could I show you in 20 minutes? {{{{booking_link}}}}\n\n{_SIGN_OFF}"
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
                        f"One concrete thing on how this would work for {{{{company_name}}}}: {proof}\n\n"
                        "Bring what you pay per consult and per order and we'll put ours next to it on "
                        f"the call: {{{{booking_link}}}}\n\n{_SIGN_OFF}"
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
                        "Another way to look at it. Most programs we talk to pay a consult charge per "
                        "medication and a markup on every order, often with a platform fee on top. With "
                        "us it's one physician network, one fee per patient across every medication, no "
                        "markup, and you keep your processor, your data and the margin.\n\n"
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
                        "Last note from me. The offer stands: if we can't beat what {{company_name}} "
                        "pays for consults and medication, we'll integrate your pharmacy for you at no "
                        f"charge. Twenty minutes is all it takes to check: {{{{booking_link}}}}\n\n{_SIGN_OFF}"
                    ),
                    hypothesis="A polite last note with the guarantee gets the 'not now, but later' replies.",
                ),
            ),
        ),
    )


def _segment_campaign(segment: str) -> CampaignDef:
    return CampaignDef(key=segment, steps=(StepDef(1, 0, _step1_variants(segment)), *_follow_ups(segment)))


def _fda_compliance_campaign() -> CampaignDef:
    """FDA warning-letter list: three steps (day 0, 4, 10), compliance angle only.

    Never the price-beat guarantee, and never a word about the FDA, warning letters or enforcement:
    the email is about putting the program on solid ground, not about what happened to them.
    """
    intro = VariantDef(
        name="compliance_foundation",
        angle="compliance_foundation",
        subject="prescribing and pharmacy setup",
        body=(
            "Hi {{first_name}},\n\n{{opener}}\n\n"
            "The fastest way to put a program on solid ground is a physician network with licensed "
            "prescribers in every state, LegitScript-reviewed pharmacy partners and proper intake. "
            "MyOrbit Health runs exactly that under your brand. You keep your storefront, your "
            "processor and your patient data.\n\n"
            f"Worth 20 minutes to walk through how it'd look for {{{{company_name}}}}? {{{{booking_link}}}}"
            f"\n\n{_SIGN_OFF}"
        ),
        hypothesis="Operators rebuilding a program want a licensed, reviewed setup more than a lower price.",
        status="control",
    )
    proof = VariantDef(
        name="compliance_proof",
        angle="proof",
        subject="",
        body=(
            "Hi {{first_name}},\n\n"
            f"One concrete thing on how this works for {{{{company_name}}}}: {_PROOF[FDA_SEGMENT]} "
            "Your storefront and checkout don't change.\n\n"
            f"Happy to show you the intake and prescriber flow in 20 minutes: {{{{booking_link}}}}\n\n{_SIGN_OFF}"
        ),
        hypothesis="A concrete picture of the prescriber and pharmacy flow earns the reply.",
    )
    close = VariantDef(
        name="compliance_close",
        angle="close_loop",
        subject="",
        body=(
            "Hi {{first_name}},\n\n"
            "Last note from me. If getting prescribing and pharmacy on a licensed, reviewed footing is "
            "on your list this quarter, we can run it behind {{company_name}} without a rebuild.\n\n"
            f"Twenty minutes is all it takes: {{{{booking_link}}}}\n\n{_SIGN_OFF}"
        ),
        hypothesis="A short last note gets the 'not now, but later' replies.",
    )
    return CampaignDef(
        key=FDA_SEGMENT, steps=(StepDef(1, 0, (intro,)), StepDef(2, 4, (proof,)), StepDef(3, 10, (close,)))
    )


def _generic_campaign() -> CampaignDef:
    """Role mailboxes: two steps (day 0, day 5), addressed to the business, asks for a referral."""
    intro = VariantDef(
        name="generic_intro",
        angle="referral_ask",
        subject="telehealth for {{company_name}}",
        body=(
            "Hi {{company_name}} team,\n\n{{opener}}\n\n"
            "Could you pass this to whoever runs operations or partnerships? MyOrbit Health runs the "
            "telehealth back end for {{vertical_phrase}}: our physicians do the consults (one fee per "
            "patient, not per medication), pharmacy fulfilment with no markup, intake and checkout, all "
            "under your brand. If we can't beat what you pay today, we'll integrate your pharmacy for "
            "you at no charge.\n\n"
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
            "Checking this reached the right person at {{company_name}}. If cutting what you pay per "
            f"consult and per order is on the list, 20 minutes here: {{{{booking_link}}}}\n\n{_SIGN_OFF}"
        ),
        hypothesis="One reminder is enough for a shared inbox.",
    )
    return CampaignDef(key=GENERIC_CAMPAIGN, steps=(StepDef(1, 0, (intro,)), StepDef(2, 5, (close,))))


CAMPAIGNS: dict[str, CampaignDef] = {
    **{segment: _segment_campaign(segment) for segment in SEGMENTS if segment != FDA_SEGMENT},
    FDA_SEGMENT: _fda_compliance_campaign(),
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


def template_body(step: int) -> str:
    """The Smartlead-side body: our rendered step, the sending mailbox's signature, the legal footer.

    %signature% is Smartlead's tag for the mailbox signature (set per account), so a lead written to
    by grant@ is signed by Grant. The footer's {{company_name_legal}} and {{postal_address}} are lead
    custom fields that Smartlead fills at send time.
    """
    return f"{{{{step{step}_body}}}}<br><br>{SIGNATURE_TAG}<br><br>{as_email_html(FOOTER)}"


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
                "email_body": template_body(step.step),
            }
        )
        previous_day = step.day
    return out
