"""Decision-maker contact data from B2B people-data providers.

Websites publish role mailboxes (info@, support@); founders' direct emails, mobiles and LinkedIn
profiles live in licensed databases. Three providers are supported, used in this order:

* Apollo.io   - people search by company domain + title -> name, title, LinkedIn; enrichment ->
                verified work email. Phone reveal is asynchronous (needs APOLLO_PHONE_WEBHOOK_URL).
* Lusha       - person lookup by name + domain -> emails + direct/mobile phones (synchronous).
* Hunter.io   - email finder (name + domain -> email + confidence) and verifier.

Each adapter is a no-op when its key is absent, so the pipeline degrades gracefully.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ..config import settings
from ..models import Person
from .extract import DECISION_RE

log = logging.getLogger(__name__)

DECISION_TITLES = [
    "Founder",
    "Co-Founder",
    "Owner",
    "CEO",
    "Chief Executive Officer",
    "President",
    "COO",
    "Chief Operating Officer",
    "CMO",
    "Chief Medical Officer",
    "Medical Director",
    "Managing Partner",
    "General Manager",
    "Head of Growth",
    "VP Operations",
]

APOLLO = "https://api.apollo.io/api/v1"
LUSHA = "https://api.lusha.com/v2/person"
HUNTER = "https://api.hunter.io/v2"


def _split(name: str) -> tuple[str, str]:
    parts = [
        p
        for p in name.replace("Dr.", "").replace("Dr ", "").replace(",", " ").split()
        if p and p[0].isalpha()
    ]
    parts = [
        p
        for p in parts
        if p.upper() not in ("MD", "DO", "NP", "RN", "PHD", "PA-C", "PHARMD", "MBA", "DNP", "FNP")
    ]
    if not parts:
        return "", ""
    return parts[0], parts[-1] if len(parts) > 1 else ""


# ----------------------------------------------------------------------------- Apollo
class Apollo:
    def __init__(self, key: str | None = None):
        self.key = key or settings.apollo_key
        self.client = httpx.Client(
            base_url=APOLLO, timeout=40, headers={"x-api-key": self.key, "Content-Type": "application/json"}
        )

    def available(self) -> bool:
        return bool(self.key)

    def search_people(self, domain: str, limit: int = 8) -> list[Person]:
        body = {
            "q_organization_domains_list": [domain],
            "person_titles": DECISION_TITLES,
            "per_page": limit,
            "page": 1,
        }
        people: list[Person] = []
        for path in ("/mixed_people/api_search", "/mixed_people/search"):
            try:
                r = self.client.post(path, json=body)
            except httpx.HTTPError as exc:
                log.warning("apollo search failed for %s: %s", domain, exc)
                return []
            if r.status_code == 404:
                continue
            if r.status_code != 200:
                log.warning("apollo search %s for %s: %s", r.status_code, domain, r.text[:200])
                return []
            for p in r.json().get("people", []):
                name = p.get("name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
                if not name:
                    continue
                title = p.get("title")
                people.append(
                    Person(
                        name=name,
                        title=title,
                        linkedin=p.get("linkedin_url"),
                        email=(p.get("email") or "").lower() or None
                        if "@" in (p.get("email") or "") and "not_unlocked" not in (p.get("email") or "")
                        else None,
                        source_url="apollo",
                        confidence=0.85,
                        is_decision_maker=bool(title and DECISION_RE.search(title)),
                    )
                )
            break
        return people

    def enrich_person(self, person: Person, domain: str, *, reveal_phone: bool = False) -> Person:
        first, last = _split(person.name)
        body: dict[str, Any] = {
            "first_name": first,
            "last_name": last,
            "domain": domain,
            "reveal_personal_emails": False,
        }
        if person.linkedin:
            body["linkedin_url"] = person.linkedin
        if reveal_phone and settings.apollo_phone_webhook:
            body["reveal_phone_number"] = True
            body["webhook_url"] = settings.apollo_phone_webhook
        try:
            r = self.client.post("/people/match", json=body)
        except httpx.HTTPError as exc:
            log.warning("apollo match failed for %s: %s", person.name, exc)
            return person
        if r.status_code != 200:
            log.warning("apollo match %s for %s: %s", r.status_code, person.name, r.text[:200])
            return person
        data = r.json().get("person") or {}
        email = (data.get("email") or "").lower()
        if "@" in email:
            person.email = email
            person.email_status = "verified" if data.get("email_status") == "verified" else "provider"
        person.linkedin = person.linkedin or data.get("linkedin_url")
        person.title = person.title or data.get("title")
        for ph in data.get("phone_numbers") or []:
            num = ph.get("sanitized_number") or ph.get("raw_number")
            if num:
                person.phone = person.phone or num
                person.phone_type = ph.get("type")
                break
        return person


# ----------------------------------------------------------------------------- Lusha
class Lusha:
    def __init__(self, key: str | None = None):
        self.key = key or settings.lusha_key
        self.client = httpx.Client(timeout=40, headers={"api_key": self.key})

    def available(self) -> bool:
        return bool(self.key)

    def enrich_person(self, person: Person, domain: str) -> Person:
        first, last = _split(person.name)
        params: dict[str, Any] = {
            "firstName": first,
            "lastName": last,
            "companyDomain": domain,
            "revealEmails": "true",
            "revealPhones": "true",
        }
        if person.linkedin:
            params["linkedinUrl"] = person.linkedin
        try:
            r = self.client.get(LUSHA, params=params)
        except httpx.HTTPError as exc:
            log.warning("lusha failed for %s: %s", person.name, exc)
            return person
        if r.status_code != 200:
            log.warning("lusha %s for %s: %s", r.status_code, person.name, r.text[:200])
            return person
        # Lusha nests the contact under data/contact depending on plan; walk the tree defensively.
        stack: list[Any] = [r.json()]
        emails: list[dict] = []
        phones: list[dict] = []
        linkedin = None
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                emails += [e for e in node.get("emailAddresses", []) if isinstance(e, dict)]
                phones += [p for p in node.get("phoneNumbers", []) if isinstance(p, dict)]
                linkedin = linkedin or node.get("linkedinUrl")
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
        work = [e for e in emails if str(e.get("emailType", "")).lower().startswith("work")] or emails
        if work and work[0].get("email") and not person.email:
            person.email = work[0]["email"].lower()
            person.email_status = "provider"
        mobile = [p for p in phones if "mobile" in str(p.get("phoneType", "")).lower()] or phones
        if mobile and mobile[0].get("number"):
            person.phone = mobile[0]["number"]
            person.phone_type = mobile[0].get("phoneType")
        person.linkedin = person.linkedin or linkedin
        return person


# ----------------------------------------------------------------------------- Hunter
class Hunter:
    def __init__(self, key: str | None = None):
        self.key = key or settings.hunter_key
        self.client = httpx.Client(base_url=HUNTER, timeout=40)

    def available(self) -> bool:
        return bool(self.key)

    def find_email(self, person: Person, domain: str) -> Person:
        first, last = _split(person.name)
        if not (first and last):
            return person
        try:
            r = self.client.get(
                "/email-finder",
                params={"domain": domain, "first_name": first, "last_name": last, "api_key": self.key},
            )
        except httpx.HTTPError as exc:
            log.warning("hunter finder failed for %s: %s", person.name, exc)
            return person
        if r.status_code != 200:
            return person
        data = r.json().get("data") or {}
        if data.get("email"):
            person.email = data["email"].lower()
            score = data.get("score") or 0
            person.email_status = "verified" if score >= 90 else "provider"
            person.confidence = max(person.confidence, score / 100)
            person.linkedin = person.linkedin or data.get("linkedin_url")
            person.phone = person.phone or data.get("phone_number")
        return person

    def verify(self, email: str) -> str | None:
        try:
            r = self.client.get("/email-verifier", params={"email": email, "api_key": self.key})
        except httpx.HTTPError:
            return None
        if r.status_code != 200:
            return None
        return (r.json().get("data") or {}).get("status")


# ----------------------------------------------------------------------------- orchestration
def enrich_decision_makers(
    people: list[Person], domain: str, *, max_people: int = 2
) -> tuple[list[Person], list[str]]:
    """Fill email / phone / LinkedIn for the top decision-makers using whichever providers are configured.

    Returns (people, providers_used)."""
    used: list[str] = []
    apollo = Apollo() if settings.apollo_key else None
    lusha = Lusha() if settings.lusha_key else None
    hunter = Hunter() if settings.hunter_key else None
    if not (apollo or lusha or hunter):
        return people, used

    if apollo:
        found = apollo.search_people(domain)
        if found:
            used.append("apollo:search")
            by_name = {p.name.lower(): p for p in people}
            for p in found:
                cur = by_name.get(p.name.lower())
                if cur:
                    cur.linkedin = cur.linkedin or p.linkedin
                    cur.title = cur.title or p.title
                    cur.is_decision_maker = cur.is_decision_maker or p.is_decision_maker
                    cur.confidence = max(cur.confidence, p.confidence)
                else:
                    people.append(p)

    ranked = sorted(people, key=lambda p: (p.is_decision_maker, p.confidence), reverse=True)
    for person in ranked[:max_people]:
        if apollo and not person.email:
            apollo.enrich_person(person, domain, reveal_phone=True)
            if person.email:
                used.append("apollo:match")
        if hunter and not person.email:
            hunter.find_email(person, domain)
            if person.email:
                used.append("hunter:finder")
        if lusha and (not person.phone or not person.email):
            lusha.enrich_person(person, domain)
            if person.phone:
                used.append("lusha")
    return ranked, sorted(set(used))
