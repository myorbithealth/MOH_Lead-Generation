"""Certificate-transparency discovery via crt.sh (free, no key).

Useful for competitor platforms that host white-label tenants on subdomains
(e.g. `brandname.platform.com`) or issue certificates for customer domains.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator

import httpx

from ..domains import is_junk, normalize_domain
from ..models import Candidate, LeadSource
from .base import DiscoverySource

log = logging.getLogger(__name__)

_IGNORED_LABELS = re.compile(
    r"^(www|api|app|staging|stage|dev|test|cdn|static|mail|smtp|admin|docs|status|demo|sandbox|analytics|otel|pg|qr|feedback|franchise|\*)\.|-demo\.|\.demo\."
)


class CrtShDiscovery(DiscoverySource):
    name = "crtsh"

    def __init__(self, platform_domain: str, platform_name: str, source: LeadSource = "competitor_platform"):
        self.platform_domain = platform_domain
        self.platform_name = platform_name
        self.source = source

    def discover(self, limit: int | None = None) -> Iterator[Candidate]:
        try:
            with httpx.Client(timeout=90) as client:
                r = client.get("https://crt.sh/", params={"q": f"%.{self.platform_domain}", "output": "json"})
                if r.status_code != 200:
                    log.warning("crt.sh %s for %s", r.status_code, self.platform_domain)
                    return
                entries = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("crt.sh failed for %s: %s", self.platform_domain, exc)
            return

        seen: set[str] = set()
        count = 0
        for entry in entries:
            for name in str(entry.get("name_value", "")).splitlines():
                name = name.strip().lower().lstrip("*.")
                if not name.endswith("." + self.platform_domain) or _IGNORED_LABELS.match(name):
                    continue
                sub = name[: -len(self.platform_domain) - 1]
                if not sub or sub in seen:
                    continue
                seen.add(sub)
                # The tenant subdomain *is* the lead handle; the business's own domain is found during enrichment.
                root = normalize_domain(name)
                if not root or is_junk(root):
                    continue
                yield Candidate(
                    domain=name,
                    source=self.source,
                    discovered_via=f"crtsh:{self.platform_domain}",
                    source_detail={
                        "platform": self.platform_name,
                        "tenant_subdomain": sub,
                        "not_before": entry.get("not_before"),
                    },
                )
                count += 1
                if limit and count >= limit:
                    return
