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


_mx_supported: bool | None = None


def mx_supported() -> bool:
    """Some sandboxed resolvers answer A/NS but silently drop MX. Probe once with a domain that
    certainly has MX records so we never report a false 'no MX' for every lead."""
    global _mx_supported
    if _mx_supported is None:
        _mx_supported = bool(_query("gmail.com", "MX"))
        if not _mx_supported:
            log.warning("MX lookups are unavailable on this resolver; has_mx will be left unknown")
    return _mx_supported


def lookup(domain: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    mx = _query(domain, "MX") if mx_supported() else []
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
    out["has_mx"] = (bool(mx) if (mx or out["ns"] or a) else None) if mx_supported() else None
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
