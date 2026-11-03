"""DNS facts: MX (can we email them?), CNAME chain (who hosts them?), A + reverse PTR."""

from __future__ import annotations

import logging
from typing import Any

import dns.exception
import dns.resolver
import dns.reversename

log = logging.getLogger(__name__)

_resolver = dns.resolver.Resolver()
_resolver.lifetime = 6.0
_resolver.timeout = 3.0


def _query(name: str, rtype: str) -> list[str]:
    try:
        return [r.to_text().rstrip(".") for r in _resolver.resolve(name, rtype)]
    except (dns.exception.DNSException, OSError, ValueError):
        return []


def lookup(domain: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    mx = _query(domain, "MX")
    out["mx"] = [m.split()[-1] for m in mx]
    out["ns"] = _query(domain, "NS")

    chain: list[str] = []
    for host in (domain, f"www.{domain}"):
        name = host
        for _ in range(5):
            cn = _query(name, "CNAME")
            if not cn:
                break
            chain.append(cn[0])
            name = cn[0]
    out["cname_chain"] = chain

    a = _query(domain, "A") or _query(f"www.{domain}", "A")
    out["a"] = a
    # Only assert "no MX" when DNS is demonstrably working for this domain.
    out["has_mx"] = bool(mx) if (mx or out["ns"] or a) else None
    out["dns_ok"] = bool(mx or out["ns"] or a)
    if a:
        try:
            ptr = _query(dns.reversename.from_address(a[0]).to_text(), "PTR")
        except dns.exception.DNSException:
            ptr = []
        out["ptr"] = ptr
    mail = " ".join(out["mx"]).lower()
    if "google" in mail or "googlemail" in mail:
        out["mail_provider"] = "Google Workspace"
    elif "outlook" in mail or "microsoft" in mail:
        out["mail_provider"] = "Microsoft 365"
    elif mail:
        out["mail_provider"] = out["mx"][0]
    return out
