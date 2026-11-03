"""Tool 1: LegitScript-certified merchants.

Why they're great leads: LegitScript certification is required to advertise telehealth,
pharmacy and addiction-treatment services on Google/Meta. A business that paid for it is
(a) real, (b) compliance-aware and (c) spending on acquisition - exactly who needs
white-label telehealth infrastructure to scale.

Discovery stack (all optional, more = more coverage):
  1. seed file          seeds/legitscript_seed_domains.txt (hand-curated + exports)
  2. search dorks       pages that embed the seal / mention certification (SerpAPI / Google CSE / Bing)
  3. Semrush backlinks  referring domains of legitscript.com (= sites linking from the seal)
  4. Semrush ads        advertisers on regulated healthcare keywords (certification is a Google prerequisite)
Verification happens in the pipeline: we look for the seal on the merchant's own site.
"""

from __future__ import annotations

from pathlib import Path

from ..discovery import (
    SearchDiscovery,
    SeedFileDiscovery,
    SemrushAdvertisersDiscovery,
    SemrushBacklinksDiscovery,
)
from ..discovery.base import DiscoverySource
from .base import Tool

DEFAULT_SEED = Path("seeds/legitscript_seed_domains.txt")

SEARCH_DORKS = [
    '"legitscript certified" telehealth',
    '"legitscript certified" "weight loss"',
    '"legitscript certified" semaglutide OR tirzepatide',
    '"legitscript certified" testosterone OR TRT',
    '"legitscript certified" peptides',
    '"legitscript certified" "med spa" OR medspa',
    '"legitscript certified" "online pharmacy"',
    '"legitscript certified" "compounding pharmacy"',
    '"legitscript certified" "hair loss" OR "erectile"',
    '"legitscript certified" "addiction treatment" OR rehab',
    '"legitscript certified" "mental health" OR ketamine OR ADHD',
    '"verify legitscript approval"',
    '"legitscript.com/websites" telehealth',
    '"legitscript" "certified" "licensed providers" -site:legitscript.com',
    'intext:"LegitScript" intext:"certified" "book a consultation"',
]

AD_KEYWORDS = [
    "online weight loss prescription",
    "semaglutide online",
    "tirzepatide online",
    "glp-1 online prescription",
    "trt online",
    "testosterone replacement therapy online",
    "online hair loss treatment",
    "ed medication online",
    "peptide therapy online",
    "compounded semaglutide",
    "online doctor prescription",
    "telehealth weight loss",
    "online suboxone doctor",
    "online adhd treatment",
    "ketamine therapy at home",
]


class LegitScriptTool(Tool):
    name = "legitscript"
    source = "legitscript"
    description = "LegitScript-certified telehealth, pharmacy, med spa and treatment businesses"

    def discovery_sources(self) -> list[DiscoverySource]:
        seed = self.seed_file or DEFAULT_SEED
        return [
            SeedFileDiscovery(seed, source=self.source),
            SearchDiscovery(SEARCH_DORKS, source=self.source, per_query=int(self.options.get("per_query", 30))),
            SemrushBacklinksDiscovery("legitscript.com", source=self.source),
            SemrushAdvertisersDiscovery(AD_KEYWORDS, source=self.source),
        ]
