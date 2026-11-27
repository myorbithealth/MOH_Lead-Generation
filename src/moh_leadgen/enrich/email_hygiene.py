"""Email hygiene: clean scraped addresses and decide whether one is a person, a role box or junk.

import logging

log = logging.getLogger(__name__)

Scraped pages leak addresses in escaped forms (`u003econtact@x.com`, `%20support@x.com`,
`mailto:info@x.com`), copy addresses from templates (the web developer's gmail on three sites), and
publish platform or registrar mailboxes. Everything here is pure except `has_mx`, which does one DNS
lookup per domain (cached, 3 s timeout, resolver injectable for tests).
"""

from __future__ import annotations

import html
import re
from typing import Any, Literal
from urllib.parse import unquote

from ..domains import registrable

LocalKind = Literal["person", "role", "junk"]

_STRICT_EMAIL_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9._+-]{0,62}[a-z0-9_+-])?@(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$"
)
_JSON_ESCAPE_RE = re.compile(r"\\+u([0-9a-fA-F]{4})")
_BARE_ESCAPE_PREFIX_RE = re.compile(r"^(?:u00[0-9a-f]{2})+", re.IGNORECASE)
_SPLIT_RE = re.compile(r"[\s<>\"'(),;\[\]{}|\\]+")
_EDGE_PUNCT = ".,;:!?'\"()[]{}<>*`~/\\-_=+&#"
_FILE_TLDS = {"png", "jpg", "jpeg", "gif", "svg", "webp", "css", "js", "ico", "bmp", "tif", "tiff", "avif"}

PLACEHOLDER_LOCALS = {
    "jane.smith",
    "janesmith",
    "jane.doe",
    "janedoe",
    "john.doe",
    "johndoe",
    "john.smith",
    "johnsmith",
    "test",
    "tester",
    "testing",
    "example",
    "user",
    "username",
    "name",
    "email",
    "youremail",
    "yourname",
    "your.name",
    "you",
    "firstname",
    "first.last",
    "firstname.lastname",
    "lastname",
    "sample",
    "demo",
}
SYSTEM_LOCALS = {
    "noreply",
    "no-reply",
    "no_reply",
    "donotreply",
    "do-not-reply",
    "do_not_reply",
    "bounce",
    "bounces",
    "mailer-daemon",
    "mailerdaemon",
    "postmaster",
    "abuse",
    "dmca",
    "privacy",
    "legal",
    "webmaster",
    "hostmaster",
    "registrar",
    "sentry",
    "wordpress",
}
PLATFORM_DOMAINS = {
    "squarespace.com",
    "wixpress.com",
    "wix.com",
    "godaddy.com",
    "domainsbyproxy.com",
    "shopify.com",
    "sentry.io",
    "example.com",
    "namecheap.com",
    "cloudflare.com",
    "hubspot.com",
    "gohighlevel.com",
    "leadconnectorhq.com",
    "klaviyo.com",
    "mailchimp.com",
}
ROLE_LOCALS = {
    "info",
    "hello",
    "hi",
    "hey",
    "heythere",
    "hola",
    "contact",
    "contacts",
    "contactus",
    "support",
    "help",
    "care",
    "clientcare",
    "customercare",
    "customerservice",
    "customer.service",
    "service",
    "team",
    "office",
    "admin",
    "sales",
    "rx",
    "pharmacy",
    "health",
    "wellness",
    "clinic",
    "orders",
    "order",
    "billing",
    "appointments",
    "appointment",
    "booking",
    "bookings",
    "scheduling",
    "schedule",
    "frontdesk",
    "front.desk",
    "reception",
    "marketing",
    "press",
    "media",
    "pr",
    "partnerships",
    "partnership",
    "partners",
    "careers",
    "jobs",
    "hr",
    "legal",
    "compliance",
    "privacy",
    "enquiries",
    "enquiry",
    "inquiries",
    "inquiry",
    "questions",
    "feedback",
    "newsletter",
    "welcome",
    "concierge",
    "membership",
    "members",
    "patients",
    "patient",
    "medical",
    "doctor",
    "dr",
    "nurse",
    "staff",
    "general",
    "mail",
    "email",
    "web",
    "website",
    "online",
    "shop",
    "store",
    "webmaster",
    "security",
    "accounts",
    "account",
    "accounting",
    "finance",
    "operations",
    "ops",
    "studio",
    "spa",
    "medspa",
    "aesthetics",
    "telehealth",
    "telemedicine",
    "weightloss",
    "trt",
    "peptides",
}
FREEMAIL_DOMAINS = {
    "gmail.com",
    "googlemail.com",
    "yahoo.com",
    "ymail.com",
    "rocketmail.com",
    "outlook.com",
    "hotmail.com",
    "live.com",
    "msn.com",
    "icloud.com",
    "me.com",
    "mac.com",
    "proton.me",
    "protonmail.com",
    "pm.me",
    "aol.com",
}
_FREEMAIL_PREFIXES = ("yahoo.", "hotmail.", "outlook.", "live.", "gmail.")


def clean_email(raw: Any) -> str | None:
    """Return a normalised address, or None when `raw` does not hold one valid email.

    Lowercases and strips whitespace, `mailto:`, URL-encoding (`%20`, `%40`), JSON escapes
    (`\\u003e`, a bare `u003e` glued to the front), HTML entities (`&gt;`), query strings and
    surrounding punctuation, then validates with a strict pattern.
    """
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s or len(s) > 512:
        return None
    for _ in range(3):  # double-encoded values are common in scraped JSON
        prev = s
        s = _JSON_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), s)
        s = html.unescape(s)
        s = unquote(s)
        if s == prev:
            break
    s = s.lower()
    s = re.sub(r"^\s*mailto:", "", s)
    s = s.split("?", 1)[0].split("#", 1)[0]
    parts = [p for p in _SPLIT_RE.split(s) if "@" in p]
    if len(parts) != 1:
        return None
    s = parts[0]
    s = re.sub(r"^(?:mailto:)+", "", s)
    s = _BARE_ESCAPE_PREFIX_RE.sub("", s)
    s = s.strip(_EDGE_PUNCT)
    if s.count("@") != 1:
        return None
    local, domain = s.split("@")
    local, domain = local.strip(_EDGE_PUNCT), domain.strip(_EDGE_PUNCT)
    if not local or "." not in domain or ".." in local or ".." in domain:
        return None
    email = f"{local}@{domain}"
    if len(email) > 254 or not _STRICT_EMAIL_RE.match(email):
        return None
    if domain.rsplit(".", 1)[1] in _FILE_TLDS:
        return None
    return email


def _domain_of(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower()


def is_freemail(email_or_domain: str) -> bool:
    domain = _domain_of(email_or_domain) if "@" in email_or_domain else email_or_domain.lower()
    return domain in FREEMAIL_DOMAINS or domain.startswith(_FREEMAIL_PREFIXES)


def is_platform_domain(email_or_domain: str) -> bool:
    domain = _domain_of(email_or_domain) if "@" in email_or_domain else email_or_domain.lower()
    root = registrable(domain) or domain
    return root in PLATFORM_DOMAINS or domain in PLATFORM_DOMAINS


def _name_tokens(owner_name: str | None) -> tuple[str, str] | None:
    if not owner_name:
        return None
    words = re.sub(r"\b(dr|mr|mrs|ms|prof)\.?\s+", " ", owner_name.lower())
    tokens = [re.sub(r"[^a-z]", "", w) for w in re.split(r"[\s,]+", words)]
    tokens = [
        t
        for t in tokens
        if t
        and t not in {"md", "do", "np", "rn", "phd", "pac", "pharmd", "mba", "dnp", "fnp", "aprn", "jr", "sr"}
    ]
    if not tokens:
        return None
    return tokens[0], tokens[-1] if len(tokens) > 1 else ""


def matches_owner(email: str, owner_name: str | None) -> bool:
    """Local part is first, first.last, flast, firstl or last of owner_name (separators ignored)."""
    names = _name_tokens(owner_name)
    if not names:
        return False
    first, last = names
    local = email.split("@", 1)[0].lower().split("+", 1)[0]
    flat = re.sub(r"[._-]", "", local)
    forms = {first}
    if last:
        forms |= {f"{first}{last}", f"{first[0]}{last}", f"{first}{last[0]}", last, f"{last}{first}"}
    return local in forms or flat in forms


def _hash_like(local: str) -> bool:
    if re.search(r"[0-9a-f]{16,}", local):
        return True
    letters = re.sub(r"[^a-z0-9]", "", local)
    return len(letters) >= 20 and not re.search(r"[aeiouy]", letters)


def classify_local_part(email: str, owner_name: str | None = None) -> LocalKind:
    """'person', 'role' or 'junk' for one (already cleaned) address.

    junk: platform/registrar/vendor domains, system mailboxes (noreply, postmaster, privacy...),
    hash-like locals and template placeholders (jane.smith, test, yourname...). role: the generous
    ROLE_LOCALS list. person: everything else. A local part that matches owner_name is a person
    unless it is a system or hash address; a single role word (care@) still counts as a role.
    """
    email = email.lower()
    local = email.split("@", 1)[0]
    base = local.split("+", 1)[0]
    if is_platform_domain(email) or base in SYSTEM_LOCALS or _hash_like(base):
        return "junk"
    owner_match = matches_owner(email, owner_name)
    if base in PLACEHOLDER_LOCALS and not owner_match:
        return "junk"
    flat = re.sub(r"[._-]", "", base)
    is_role = base in ROLE_LOCALS or flat in ROLE_LOCALS
    if owner_match and not (base in ROLE_LOCALS and base == flat):
        return "person"  # first.last / flast style owner match wins over a role word
    return "role" if is_role else "person"


def is_cross_company_duplicate(email: str, domain: str, seen: dict[str, set[str]]) -> bool:
    """True when `email` is also attached to a company other than `domain` (registrable compare)."""
    owners = seen.get(email.strip().lower(), set())
    me = registrable(domain) or domain.lower()
    return any(other != me for other in owners)


def lead_emails(lead: Any) -> list[str]:
    """Every cleaned address on a lead (owner, people, primary, site emails), de-duplicated."""
    raw = [lead.owner_email, lead.primary_email, *lead.emails, *(p.email for p in lead.people)]
    out: list[str] = []
    for value in raw:
        email = clean_email(value)
        if email and email not in out:
            out.append(email)
    return out


def email_owners(leads: list[Any]) -> dict[str, set[str]]:
    """email -> set of registrable company domains it appears on, over every lead given."""
    seen: dict[str, set[str]] = {}
    for lead in leads:
        company = registrable(lead.domain) or lead.domain.lower()
        for email in lead_emails(lead):
            seen.setdefault(email, set()).add(company)
    return seen


def normalize_lead_emails(lead: Any) -> bool:
    """Clean lead.emails, primary_email, owner_email and people[].email in place. True if changed.

    Unparseable values are dropped (set to None / removed from the list); duplicates collapse.
    """
    changed = False
    emails: list[str] = []
    for value in lead.emails:
        email = clean_email(value)
        if email and email not in emails:
            emails.append(email)
    if emails != lead.emails:
        lead.emails = emails
        changed = True
    for attr in ("primary_email", "owner_email"):
        value = getattr(lead, attr)
        if value is not None:
            email = clean_email(value)
            if email != value:
                setattr(lead, attr, email)
                changed = True
    for person in lead.people:
        if person.email is not None:
            email = clean_email(person.email)
            if email != person.email:
                person.email = email
                changed = True
    return changed


# ----------------------------------------------------------------------------- MX
MX_TIMEOUT_SECONDS = 3.0
_MX_CACHE: dict[str, bool | None] = {}


def _default_resolver() -> Any:
    import dns.resolver

    resolver = dns.resolver.Resolver()
    resolver.timeout = MX_TIMEOUT_SECONDS
    resolver.lifetime = MX_TIMEOUT_SECONDS
    return resolver


def has_mx(domain: str, resolver: Any = None, *, cache: dict[str, bool | None] | None = None) -> bool | None:
    """True if `domain` publishes a usable MX, False if it provably does not, None when unknown.

    NXDOMAIN, no MX answer and a null MX (`0 .`) are False; timeouts and server failures are None.
    `resolver` is anything with `.resolve(name, 'MX', lifetime=...)` returning MX rdata (dnspython
    shape); the default dnspython resolver results are cached in-process.
    """
    domain = (domain or "").strip().lower().rstrip(".")
    if not domain or "." not in domain:
        return False
    if cache is None and resolver is None:
        cache = _MX_CACHE
    if cache is not None and domain in cache:
        return cache[domain]
    if resolver is None and not _mx_lookups_trustworthy():
        return None  # this network cannot answer MX queries at all; do not reject anyone on it
    result = _lookup_mx(domain, resolver or _default_resolver())
    if cache is not None:
        cache[domain] = result
    return result


_MX_TRUST: dict[str, bool] = {}
MX_CONTROL_DOMAIN = "gmail.com"


def _mx_lookups_trustworthy() -> bool:
    """One-time sanity check: if the resolver cannot even answer MX for gmail.com, every
    'no MX' verdict would be a false negative (sandboxes and some CI runners stub DNS), so
    has_mx() reports unknown instead of False until a network with real DNS runs it."""
    if "ok" not in _MX_TRUST:
        verdict = _lookup_mx(MX_CONTROL_DOMAIN, _default_resolver())
        _MX_TRUST["ok"] = verdict is True
        if not _MX_TRUST["ok"]:
            log.warning(
                "MX lookups are not trustworthy here (control domain %s -> %s); skipping MX checks",
                MX_CONTROL_DOMAIN,
                verdict,
            )
    return _MX_TRUST["ok"]


def _lookup_mx(domain: str, resolver: Any) -> bool | None:
    try:
        import dns.exception
        import dns.resolver
    except ImportError:  # pragma: no cover - dnspython is a declared dependency
        return None
    try:
        answers = resolver.resolve(domain, "MX", lifetime=MX_TIMEOUT_SECONDS)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return False
    except (dns.exception.DNSException, OSError):
        return None
    hosts = [str(getattr(r, "exchange", r)).rstrip(".") for r in answers]
    return any(hosts)
