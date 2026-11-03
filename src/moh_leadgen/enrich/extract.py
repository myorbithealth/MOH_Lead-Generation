"""Pull emails, phones, addresses, social links, JSON-LD and named people out of crawled pages."""

from __future__ import annotations

import html as htmllib
import json
import re
from collections import Counter
from typing import Any
from urllib.parse import unquote, urlsplit

import phonenumbers

from ..models import Person
from .site_crawler import Page, SiteSnapshot

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
OBFUSCATED_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-]+)\s*(?:\[at\]|\(at\)|\{at\}| at )\s*([A-Za-z0-9.-]+)\s*(?:\[dot\]|\(dot\)|\{dot\}| dot )\s*([A-Za-z]{2,})", re.IGNORECASE)
_EMAIL_JUNK = re.compile(r"(example\.|sentry|wixpress|\.png$|\.jpg$|\.gif$|\.svg$|\.webp$|@2x|noreply|no-reply|donotreply)", re.IGNORECASE)
_GENERIC_LOCALPARTS = ("info", "hello", "contact", "support", "admin", "office", "team", "sales", "care", "help", "frontdesk", "appointments")

SOCIAL_HOSTS = {
    "linkedin.com": "linkedin",
    "facebook.com": "facebook",
    "instagram.com": "instagram",
    "twitter.com": "twitter",
    "x.com": "twitter",
    "youtube.com": "youtube",
    "tiktok.com": "tiktok",
    "yelp.com": "yelp",
}

TITLE_WORDS = (
    r"founder|co-founder|cofounder|owner|co-owner|ceo|chief executive|president|principal|managing partner|partner|"
    r"medical director|clinical director|chief medical officer|cmo|coo|cfo|cto|chief [a-z]+ officer|"
    r"director|vp|vice president|head of|general manager|practice manager|administrator|physician|md|do|np|pa-c|rn|pharmd|pharmacist"
)
TITLE_RE = re.compile(rf"\b({TITLE_WORDS})\b", re.IGNORECASE)
# Inline "Name, Title" matching uses a stricter vocabulary: a role word must start the title.
ROLE_WORDS = (
    r"founder|co-founder|cofounder|owner|co-owner|ceo|chief executive officer|president|managing partner|"
    r"medical director|clinical director|chief medical officer|chief operating officer|chief technology officer|"
    r"chief [a-z]+ officer|director of [a-z]+|vice president|vp of [a-z]+|general manager|practice manager|lead physician"
)
DECISION_RE = re.compile(r"founder|owner|ceo|chief executive|president|managing|partner|medical director|cmo|coo|principal|general manager", re.IGNORECASE)
NAME_RE = re.compile(r"^(?:Dr\.?\s+)?[A-Z][a-zA-Z'\-]+(?:\s+[A-Z]\.?)?(?:\s+[A-Z][a-zA-Z'\-]+){1,2}(?:,?\s+(?:MD|DO|NP|PA-C|RN|PharmD|PhD|FNP|DNP|APRN|MBA))*$")
_NON_NAME = re.compile(
    r"\b(privacy|policy|terms|contact|about|home|login|sign|book|learn|read|more|our|the|and|team|services?|treatment|"
    r"health|healthcare|clinic|medical|care|center|group|wellness|rights|reserved|llc|inc|how|what|why|when|where|who|"
    r"works?|sites?|provider|providers|patient|patients|get|start|started|free|your|you|my|meet|new|now|today|online|"
    r"shop|buy|order|pricing|plans?|faqs?|blog|news|press|support|help|join|apply|visit|call|email|us|weight|loss|"
    r"therapy|program|programs|results|reviews|testimonials|locations?|hours|open|closed|menu|search|skip|content|"
    r"semaglutide|tirzepatide|testosterone|peptides?|hair|skin|men|women|kids|family|doctor|doctors|physician|"
    r"pharmacy|prescription|consult|consultation|appointment|schedule|discover|explore|see|view|watch|download|"
    r"county|state|states|united|america|american|city|street|avenue|suite|period|mediation|arbitration|agreement|"
    r"section|court|district|north|south|east|west|central|valley|beach|park|island|coast|national|international)\b",
    re.IGNORECASE,
)


# ----------------------------------------------------------------------------- emails
def extract_emails(snapshot: SiteSnapshot, domain: str) -> list[str]:
    counts: Counter[str] = Counter()
    for page in snapshot.pages:
        for a in page.soup.select('a[href^="mailto:"]'):
            addr = unquote(a["href"][7:]).split("?", 1)[0].strip().lower()
            if addr:
                counts[addr] += 3
        for m in EMAIL_RE.findall(page.html):
            counts[m.lower()] += 1
        for m in OBFUSCATED_EMAIL_RE.findall(page.text):
            counts[f"{m[0]}@{m[1]}.{m[2]}".lower()] += 2
    root = domain.removeprefix("www.")
    out = []
    for addr, _ in counts.most_common():
        if _EMAIL_JUNK.search(addr):
            continue
        addr_domain = addr.split("@", 1)[1]
        # Prefer same-domain addresses; keep others only if they look like real mailboxes.
        if not (addr_domain == root or addr_domain.endswith("." + root) or "@gmail." in addr or "@outlook." in addr or "@yahoo." in addr):
            continue
        out.append(addr)
    return out[:10]


def pick_primary_email(emails: list[str]) -> str | None:
    if not emails:
        return None
    for local in _GENERIC_LOCALPARTS:
        for e in emails:
            if e.split("@")[0] == local:
                return e
    return emails[0]


# ----------------------------------------------------------------------------- phones
def extract_phones(snapshot: SiteSnapshot, region: str = "US") -> list[str]:
    counts: Counter[str] = Counter()
    for page in snapshot.pages:
        for a in page.soup.select('a[href^="tel:"]'):
            raw = unquote(a["href"][4:])
            try:
                num = phonenumbers.parse(raw, region)
                if phonenumbers.is_valid_number(num):
                    counts[phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)] += 3
            except phonenumbers.NumberParseException:
                pass
        for match in phonenumbers.PhoneNumberMatcher(page.text, region, leniency=phonenumbers.Leniency.VALID):
            counts[phonenumbers.format_number(match.number, phonenumbers.PhoneNumberFormat.E164)] += 1
    return [p for p, _ in counts.most_common(5)]


# ----------------------------------------------------------------------------- JSON-LD
def extract_jsonld(snapshot: SiteSnapshot) -> list[dict[str, Any]]:
    objs: list[dict[str, Any]] = []
    for page in snapshot.pages:
        for tag in page.soup.find_all("script", type=re.compile("ld\\+json", re.IGNORECASE)):
            try:
                data = json.loads(tag.string or tag.get_text() or "")
            except (ValueError, TypeError):
                continue
            stack = [data]
            while stack:
                item = stack.pop()
                if isinstance(item, list):
                    stack.extend(item)
                elif isinstance(item, dict):
                    objs.append(item)
                    if "@graph" in item:
                        stack.extend(item["@graph"])
    return objs


_ORG_TYPES = ("Organization", "LocalBusiness", "MedicalBusiness", "MedicalClinic", "Pharmacy", "Physician", "HealthAndBeautyBusiness", "Store", "Corporation", "MedicalOrganization", "Dentist", "ExerciseGym", "HealthClub", "DaySpa", "BeautySalon")


def organization_from_jsonld(objs: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for obj in objs:
        types = obj.get("@type")
        types = types if isinstance(types, list) else [types]
        if not any(t in _ORG_TYPES for t in types if isinstance(t, str)):
            continue
        out.setdefault("name", obj.get("name") or obj.get("legalName"))
        out.setdefault("telephone", obj.get("telephone"))
        out.setdefault("email", obj.get("email"))
        out.setdefault("description", obj.get("description"))
        addr = obj.get("address")
        if isinstance(addr, list):
            addr = addr[0] if addr else None
        if isinstance(addr, dict):
            out.setdefault("street", addr.get("streetAddress"))
            out.setdefault("city", addr.get("addressLocality"))
            out.setdefault("state", addr.get("addressRegion"))
            out.setdefault("postal", addr.get("postalCode"))
            out.setdefault("country", addr.get("addressCountry") if isinstance(addr.get("addressCountry"), str) else None)
        elif isinstance(addr, str):
            out.setdefault("street", addr)
        same_as = obj.get("sameAs")
        if same_as:
            out.setdefault("sameAs", same_as if isinstance(same_as, list) else [same_as])
        for key in ("founder", "founders", "employee", "employees", "member"):
            val = obj.get(key)
            if val:
                out.setdefault("people", []).extend(val if isinstance(val, list) else [val])
    return out


def people_from_jsonld(objs: list[dict[str, Any]], org: dict[str, Any]) -> list[Person]:
    people: list[Person] = []
    raw: list[Any] = list(org.get("people", []))
    for obj in objs:
        if obj.get("@type") == "Person":
            raw.append(obj)
    for item in raw:
        if isinstance(item, str):
            people.append(Person(name=item, confidence=0.6))
        elif isinstance(item, dict) and item.get("name"):
            title = item.get("jobTitle")
            same = item.get("sameAs") or []
            same = same if isinstance(same, list) else [same]
            linkedin = next((s for s in same if "linkedin.com/in/" in s), None)
            people.append(
                Person(
                    name=str(item["name"]).strip(),
                    title=title,
                    email=item.get("email"),
                    linkedin=linkedin,
                    confidence=0.8,
                    is_decision_maker=bool(title and DECISION_RE.search(title)),
                )
            )
    return people


# ----------------------------------------------------------------------------- socials
def extract_socials(snapshot: SiteSnapshot) -> dict[str, str]:
    socials: dict[str, str] = {}
    for page in snapshot.pages:
        for a in page.soup.find_all("a", href=True):
            href = a["href"].strip()
            host = urlsplit(href).netloc.lower().removeprefix("www.")
            key = SOCIAL_HOSTS.get(host)
            if not key:
                continue
            path = urlsplit(href).path.strip("/")
            if not path or path.startswith(("share", "sharer", "intent", "hashtag")):
                continue
            if key == "linkedin" and path.startswith("in/"):
                continue  # personal profile - handled as a person, not the company page
            socials.setdefault(key, href)
    return socials


def extract_linkedin_profiles(snapshot: SiteSnapshot) -> list[tuple[str, str]]:
    """(url, anchor/nearby text) for personal LinkedIn profiles."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for page in snapshot.pages:
        for a in page.soup.find_all("a", href=re.compile(r"linkedin\.com/in/", re.IGNORECASE)):
            url = a["href"].split("?")[0]
            if url in seen:
                continue
            seen.add(url)
            context = a.get_text(" ", strip=True)
            parent = a.find_parent(["li", "div", "article", "section"])
            if parent:
                context = (context + " " + parent.get_text(" ", strip=True))[:200]
            out.append((url, context))
    return out


# ----------------------------------------------------------------------------- people
def _clean_name(s: str) -> str:
    s = htmllib.unescape(s).strip(" \t\n:-–—|•·")
    s = re.sub(r"\s+", " ", s)
    return s


def _looks_like_name(s: str) -> bool:
    s = _clean_name(s)
    if len(s) < 5 or len(s) > 60:
        return False
    if _NON_NAME.search(s):
        return False
    return bool(NAME_RE.match(s))


def extract_people_heuristic(snapshot: SiteSnapshot) -> list[Person]:
    """Find 'Name + Title' pairs on team/about pages via heading/caption structure and inline patterns."""
    found: dict[str, Person] = {}

    def add(name: str, title: str | None, url: str, conf: float) -> None:
        name = _clean_name(name)
        if not _looks_like_name(name):
            return
        key = name.lower()
        title = _clean_name(title)[:80] if title else None
        p = found.get(key)
        if p is None:
            found[key] = Person(
                name=name, title=title, source_url=url, confidence=conf,
                is_decision_maker=bool(title and DECISION_RE.search(title)),
            )
        elif title and not p.title:
            p.title = title
            p.is_decision_maker = bool(DECISION_RE.search(title))
            p.confidence = max(p.confidence, conf)

    for page in snapshot.pages:
        if re.search(r"privacy|terms|legal|policy|hipaa|disclaimer|refund|accessibility", page.url, re.IGNORECASE):
            continue
        # 1. Headings followed by a short title-ish line.
        for h in page.soup.find_all(["h1", "h2", "h3", "h4", "h5", "strong", "b"]):
            name = h.get_text(" ", strip=True)
            if not _looks_like_name(name):
                continue
            nxt = h.find_next(string=True)
            title = None
            sib = h.find_next_sibling()
            for cand in (sib.get_text(" ", strip=True) if sib else "", str(nxt).strip() if nxt else ""):
                if cand and cand != name and len(cand) < 90 and TITLE_RE.search(cand):
                    title = cand
                    break
            if title:
                add(name, title, page.url, 0.6)

        # 2. Inline "Jane Doe, Founder & CEO" / "Founder: Jane Doe" / "founded by Jane Doe".
        for m in re.finditer(r"(?<![a-z] )((?:Dr\.?\s+)?[A-Z][a-zA-Z'\-]+(?:\s+[A-Z]\.?)?(?:\s+[A-Z][a-zA-Z'\-]+){1,2}(?:,\s*(?:MD|DO|NP|PA-C|RN|PharmD|PhD))?)\s*(?:,|-|–|—|\||:)\s*((?:" + ROLE_WORDS + r")(?:\s*(?:&|and|/|,)\s*(?:" + ROLE_WORDS + r"))?)\b", page.text, re.IGNORECASE):
            if m.group(2)[:1].isupper() or m.group(2).lower().startswith(("co-", "chief")):
                add(m.group(1), m.group(2), page.url, 0.65)
        for m in re.finditer(r"\b(founded|started|owned|led|established|created)\s+by\s+(?:Dr\.?\s+)?([A-Z][a-zA-Z'\-]+(?:\s+[A-Z]\.?)?(?:\s+[A-Z][a-zA-Z'\-]+){1,2})", page.text):
            add(m.group(2), "Founder", page.url, 0.7)
        for m in re.finditer(r"\b(" + ROLE_WORDS + r")\s*[:\-–—]\s*(?:Dr\.?\s+)?([A-Z][a-zA-Z'\-]+(?:\s+[A-Z]\.?)?(?:\s+[A-Z][a-zA-Z'\-]+){1,2})", page.text, re.IGNORECASE):
            add(m.group(2), m.group(1), page.url, 0.65)

    # 3. Personal LinkedIn links near a name.
    for url, context in extract_linkedin_profiles(snapshot):
        slug = urlsplit(url).path.split("/in/", 1)[-1].strip("/").split("-")
        guess = " ".join(w.capitalize() for w in slug if w.isalpha())[:60]
        matched = None
        for key, p in found.items():
            if key.split()[0] in context.lower() or key in context.lower():
                matched = p
                break
        if matched:
            matched.linkedin = matched.linkedin or url
            matched.confidence = min(1.0, matched.confidence + 0.15)
        elif _looks_like_name(guess):
            found[guess.lower()] = Person(name=guess, linkedin=url, source_url=url, confidence=0.4)

    people = sorted(found.values(), key=lambda p: (p.is_decision_maker, p.confidence), reverse=True)
    return people[:12]


# ----------------------------------------------------------------------------- misc
def extract_company_name(snapshot: SiteSnapshot, org: dict[str, Any], domain: str) -> str | None:
    if org.get("name"):
        return str(org["name"]).strip()[:120]
    home: Page | None = snapshot.homepage
    if home is None:
        return None
    og = home.soup.find("meta", property="og:site_name")
    if og and og.get("content"):
        return og["content"].strip()[:120]
    title = home.soup.title.get_text(strip=True) if home.soup.title else ""
    if title:
        name = re.split(r"\s+[|\-–—:]\s+", title)[0].strip()
        if 2 < len(name) < 80:
            return name
    return domain.split(".")[0].capitalize()


def extract_description(snapshot: SiteSnapshot, org: dict[str, Any]) -> str | None:
    if org.get("description"):
        return str(org["description"]).strip()[:500]
    home = snapshot.homepage
    if home is None:
        return None
    for sel in ({"name": "description"}, {"property": "og:description"}):
        tag = home.soup.find("meta", attrs=sel)
        if tag and tag.get("content"):
            return tag["content"].strip()[:500]
    return None


def extract_address_text(snapshot: SiteSnapshot) -> str | None:
    """Fallback US street-address pattern when JSON-LD has none."""
    rx = re.compile(r"\d{1,6}\s+[A-Za-z0-9.'\- ]{3,40}\b(?:St|Street|Ave|Avenue|Blvd|Boulevard|Rd|Road|Dr|Drive|Ln|Lane|Way|Pkwy|Parkway|Suite|Ste|Hwy|Highway)\b[^\n]{0,60}?\b[A-Z]{2}\s+\d{5}(?:-\d{4})?")
    for page in snapshot.pages:
        m = rx.search(page.text)
        if m:
            return re.sub(r"\s+", " ", m.group(0)).strip()[:200]
    return None
