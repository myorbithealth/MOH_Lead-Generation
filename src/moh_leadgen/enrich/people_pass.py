"""Hunter people pass: named decision-maker emails for leads that only have role mailboxes.

Two passes share this module. The domain-search pass (run_people_pass) asks Hunter who works at a
domain. The finder pass (finder_pass) starts from names we already hold (owner_name, crawled
people[]) and asks Hunter's Email Finder for that person's address; see the section at the end.

Runs over leads already in the local store (no re-crawl). For each enriched lead without a
person-class email, one Hunter Domain Search (type=personal, limit 10) returns the people Hunter
indexes for the domain; up to two decision-makers are kept, each verified with the Email Verifier
first when verification is on. Every lead checked records source_detail["hunter"] and is not
searched again for RECHECK_DAYS.

The same verifier step runs inside `pipeline.enrich_lead` for pattern-guessed owner emails
(verify_guessed), so a guess becomes "verified" or is dropped instead of staying unsendable.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ..models import Lead, Person
from ..scoring import score_lead
from .email_hygiene import classify_local_part, clean_email
from .hunter import (
    BudgetExhausted,
    FinderResult,
    HunterClient,
    HunterStop,
    verification_outcome,
)

log = logging.getLogger(__name__)

RECHECK_DAYS = 90
MAX_PEOPLE = 2
MIN_CONFIDENCE = 60  # Hunter confidence (0-100) for a candidate without a decision-maker title
SOURCE_URL = "hunter:domain-search"

DM_TITLE_RE = re.compile(
    r"\b(co-?founder|founder|ceo|owner|president|medical director|director|head of|vp|"
    r"vice president|partner|manager)\b",
    re.IGNORECASE,
)


# ----------------------------------------------------------------------------- selection
def _is_person_email(email: str | None, name: str | None, domain: str, status: str | None) -> bool:
    """A cleaned, person-class address that is not just a pattern guess."""
    cleaned = clean_email(email)
    if not cleaned or status == "guessed":
        return False
    return classify_local_part(cleaned, name, domain) == "person"


def has_person_email(lead: Lead) -> bool:
    """True when the owner or a named person already has a person-class email (role, junk and
    pattern-guessed addresses do not count)."""
    if _is_person_email(lead.owner_email, lead.owner_name, lead.domain, lead.owner_email_status):
        return True
    return any(_is_person_email(p.email, p.name, lead.domain, p.email_status) for p in lead.people)


def checked_recently(lead: Lead, now: datetime, days: int = RECHECK_DAYS) -> bool:
    raw = (lead.source_detail.get("hunter") or {}).get("checked_at")
    if not raw:
        return False
    try:
        when = datetime.fromisoformat(str(raw))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return now - when < timedelta(days=days)


def select_decision_makers(
    emails: list[dict[str, Any]], domain: str, *, max_people: int = MAX_PEOPLE
) -> list[Person]:
    """Up to `max_people` people from a Domain Search result.

    Titles matching DM_TITLE_RE win (highest confidence first). Only when none match, the
    highest-confidence people at or above MIN_CONFIDENCE are taken. Nameless rows and role or
    junk addresses are skipped.
    """
    rows: list[tuple[bool, int, str, str, str | None, dict[str, Any]]] = []
    seen: set[str] = set()
    for entry in emails:
        addr = clean_email(entry.get("value"))
        if not addr or addr in seen or entry.get("type") not in (None, "personal"):
            continue
        name = " ".join(str(x).strip() for x in (entry.get("first_name"), entry.get("last_name")) if x)
        if not name or classify_local_part(addr, name, domain) != "person":
            continue
        seen.add(addr)
        title = entry.get("position") or None
        confidence = int(entry.get("confidence") or 0)
        rows.append((bool(title and DM_TITLE_RE.search(title)), confidence, addr, name, title, entry))
    titled = sorted((r for r in rows if r[0]), key=lambda r: r[1], reverse=True)
    if titled:
        picked = titled[:max_people]
    else:
        confident = [r for r in rows if r[1] >= MIN_CONFIDENCE]
        picked = sorted(confident, key=lambda r: r[1], reverse=True)[:max_people]
    return [
        Person(
            name=name,
            title=title,
            email=addr,
            email_status="provider",
            linkedin=entry.get("linkedin") or None,
            phone=entry.get("phone_number") or None,
            source_url=SOURCE_URL,
            confidence=confidence / 100,
            is_decision_maker=is_dm,
        )
        for is_dm, confidence, addr, name, title, entry in picked
    ]


# ----------------------------------------------------------------------------- verification
@dataclass
class VerifyCounts:
    verified: int = 0
    dropped: int = 0
    accept_all: int = 0


def _verify_person(person: Person, client: HunterClient, counts: VerifyCounts) -> bool:
    """Verify person.email in place. True when the address is kept."""
    result = client.verify_email(person.email or "")
    status, accept_all = verification_outcome(result)
    if status is None:
        counts.dropped += 1
        return False
    person.email_status = status
    person.accept_all = accept_all
    person.verifier_score = result.score if result else None
    counts.verified += status == "verified"
    counts.accept_all += accept_all
    return True


def guessed_emails(lead: Lead) -> list[str]:
    found = [lead.owner_email] if lead.owner_email and lead.owner_email_status == "guessed" else []
    found += [p.email for p in lead.people if p.email and p.email_status == "guessed"]
    return list(dict.fromkeys(e for e in found if e))


def verify_guessed(lead: Lead, client: HunterClient, counts: VerifyCounts | None = None) -> VerifyCounts:
    """Run the verifier on every pattern-guessed address on the lead: promote or drop it."""
    counts = counts or VerifyCounts()
    for email in guessed_emails(lead):
        probe = Person(name=lead.owner_name or email, email=email, email_status="guessed")
        kept = _verify_person(probe, client, counts)
        targets = [p for p in lead.people if p.email == email]
        for person in targets:
            if kept:
                person.email_status = probe.email_status
                person.accept_all, person.verifier_score = probe.accept_all, probe.verifier_score
            else:
                person.email, person.email_status = None, None
        if lead.owner_email == email:
            if kept:
                lead.owner_email_status = probe.email_status
            else:
                lead.owner_email, lead.owner_email_status = None, None
    if lead.owner_email_status != "guessed":
        lead.source_detail.pop("owner_email_guessed", None)
    return counts


def verify_guessed_with_hunter(lead: Lead, client: HunterClient | None = None) -> VerifyCounts | None:
    """The enrich-time hook: verify guessed addresses when there are any. Never raises."""
    if not guessed_emails(lead):
        return None
    own = client is None
    client = client or HunterClient()
    try:
        counts = verify_guessed(lead, client)
    except HunterStop as exc:
        log.warning("hunter verify skipped for %s: %s", lead.domain, exc)
        return None
    finally:
        if own:
            client.close()
    lead.source_detail["hunter_verify"] = {
        "checked_at": datetime.now(UTC).isoformat(),
        "verified": counts.verified,
        "dropped": counts.dropped,
        "accept_all": counts.accept_all,
    }
    return counts


# ----------------------------------------------------------------------------- one lead
def _merge_person(lead: Lead, new: Person) -> bool:
    """Add a Hunter person to lead.people. True when it gave the lead a new email."""
    for cur in lead.people:
        if cur.email and cur.email == new.email:
            if cur.email_status in (None, "guessed"):
                cur.email_status, cur.verifier_score = new.email_status, new.verifier_score
                cur.accept_all = new.accept_all
            cur.title = cur.title or new.title
            cur.linkedin, cur.phone = cur.linkedin or new.linkedin, cur.phone or new.phone
            return False
    for cur in lead.people:
        if cur.name.strip().lower() == new.name.strip().lower():
            if cur.email and cur.email_status != "guessed":
                return False  # keep the address we already trust for this person
            cur.email, cur.email_status = new.email, new.email_status
            cur.verifier_score, cur.accept_all = new.verifier_score, new.accept_all
            cur.title = cur.title or new.title
            cur.linkedin, cur.phone = cur.linkedin or new.linkedin, cur.phone or new.phone
            cur.confidence = max(cur.confidence, new.confidence)
            cur.is_decision_maker = cur.is_decision_maker or new.is_decision_maker
            return True
    lead.people.append(new)
    return True


def _fill_owner(lead: Lead, person: Person) -> None:
    """Owner fields are only filled when empty; a named owner is never replaced."""
    if not lead.owner_name:
        lead.owner_name = person.name
        lead.owner_title = lead.owner_title or person.title
        lead.owner_linkedin = lead.owner_linkedin or person.linkedin
        lead.owner_phone = lead.owner_phone or person.phone
    if lead.owner_name.strip().lower() == person.name.strip().lower() and not lead.owner_email:
        lead.owner_email, lead.owner_email_status = person.email, person.email_status
        lead.owner_title = lead.owner_title or person.title


@dataclass
class LeadResult:
    ok: bool = True
    results: int = 0
    kept: int = 0
    new_emails: int = 0
    credits: int = 0
    counts: VerifyCounts = field(default_factory=VerifyCounts)


def credits_needed(lead: Lead, verify: bool) -> int:
    """Worst-case credits for one lead, so the budget never runs out half way through a lead."""
    return 1 + ((MAX_PEOPLE + len(guessed_emails(lead))) if verify else 0)


def process_lead(lead: Lead, client: HunterClient, *, verify: bool, now: datetime) -> LeadResult:
    """Domain Search + selection + verification for one lead, mutating it in place."""
    before = client.credits_used
    found = client.domain_search(lead.domain)
    if found is None:
        return LeadResult(ok=False)
    out = LeadResult(results=len(found.emails))
    candidates = select_decision_makers(found.emails, lead.domain)
    if verify:
        verify_guessed(lead, client, out.counts)
        candidates = [p for p in candidates if _verify_person(p, client, out.counts)]
    out.kept = len(candidates)
    out.new_emails = sum(_merge_person(lead, p) for p in candidates)
    if candidates:
        _fill_owner(lead, candidates[0])
    if found.pattern:
        lead.email_pattern = found.pattern
    out.credits = client.credits_used - before
    previous = lead.source_detail.get("hunter") or {}
    lead.source_detail["hunter"] = {
        **({"finder": previous["finder"]} if previous.get("finder") else {}),
        "checked_at": now.isoformat(),
        "results": out.results,
        "pattern": found.pattern,
        "credits_used": out.credits,
        "accept_all": found.accept_all,
        "kept": [p.email for p in candidates],
    }
    if out.new_emails:
        providers = set(lead.source_detail.get("contact_providers") or [])
        lead.source_detail["contact_providers"] = sorted(providers | {SOURCE_URL})
    return out


# ----------------------------------------------------------------------------- the pass
@dataclass
class PassStats:
    scanned: int = 0  # enriched leads matching the filters
    eligible: int = 0  # ... without a person-class email
    skipped_recent: int = 0  # ... already checked in the last RECHECK_DAYS
    queued: int = 0
    checked: int = 0
    with_new_email: int = 0
    verified: int = 0
    dropped: int = 0
    accept_all: int = 0
    errors: int = 0
    credits_used: int = 0
    stopped: str | None = None
    domains: list[str] = field(default_factory=list)

    @property
    def hit_rate(self) -> float:
        return 100.0 * self.with_new_email / self.checked if self.checked else 0.0

    @property
    def coverage(self) -> float:
        """Share of the scanned enriched leads that now have a person-class email."""
        if not self.scanned:
            return 0.0
        return 100.0 * (self.scanned - self.eligible + self.with_new_email) / self.scanned

    def summary(self) -> str:
        line = (
            f"{self.checked} lead(s) checked, {self.with_new_email} with a new person email, "
            f"{self.verified} verified, {self.dropped} dropped, {self.accept_all} accept-all, "
            f"{self.credits_used} credit(s) used; estimated coverage {self.hit_rate:.0f}% of checked "
            f"leads, {self.coverage:.0f}% of {self.scanned} enriched leads have a person email"
        )
        if self.skipped_recent:
            line += f"; {self.skipped_recent} checked in the last {RECHECK_DAYS} days skipped"
        if self.errors:
            line += f"; {self.errors} error(s)"
        if self.stopped:
            line += f"; stopped: {self.stopped}"
        return line


def select_leads(leads: list[Lead], *, limit: int, now: datetime, stats: PassStats) -> list[Lead]:
    stats.scanned = len(leads)
    eligible = [lead for lead in leads if not has_person_email(lead)]
    stats.eligible = len(eligible)
    due = [lead for lead in eligible if not checked_recently(lead, now)]
    stats.skipped_recent = len(eligible) - len(due)
    oldest = datetime.min.replace(tzinfo=UTC)
    due.sort(key=lambda lead: (lead.score, lead.last_enriched_at or oldest), reverse=True)
    return due[:limit] if limit else due


def run_people_pass(
    store: Any,
    client: HunterClient | None,
    *,
    limit: int = 200,
    source: str | None = None,
    min_score: int | None = 40,
    verify: bool = True,
    dry_run: bool = False,
    save: Callable[[Any, Lead], Lead] | None = None,
    now: datetime | None = None,
    on_lead: Callable[[Lead, LeadResult], None] | None = None,
) -> PassStats:
    """Check up to `limit` leads with Hunter; `save` persists each changed lead (cli.save_enriched)."""
    now = now or datetime.now(UTC)
    stats = PassStats()
    leads = list(store.iter(source=source, enriched=True, min_score=min_score))
    queue = select_leads(leads, limit=limit, now=now, stats=stats)
    stats.queued = len(queue)
    if dry_run or client is None:
        stats.domains = [lead.domain for lead in queue]
        return stats
    persist = save or (lambda st, lead: st.save(lead) or lead)
    start = client.credits_used
    for lead in queue:
        if not client.budget.can_spend(credits_needed(lead, verify)):
            stats.stopped = f"credit budget of {client.budget.max_credits} reached"
            break
        try:
            result = process_lead(lead, client, verify=verify, now=now)
        except BudgetExhausted as exc:
            stats.stopped = str(exc)
            break
        except HunterStop as exc:
            stats.stopped = str(exc)
            break
        if not result.ok:
            stats.errors += 1
            continue
        stats.checked += 1
        stats.with_new_email += result.new_emails > 0 and has_person_email(lead)
        stats.verified += result.counts.verified
        stats.dropped += result.counts.dropped
        stats.accept_all += result.counts.accept_all
        stats.domains.append(lead.domain)
        saved = persist(store, score_lead(lead))
        if on_lead:
            on_lead(saved, result)
    stats.credits_used = client.credits_used - start
    return stats


# ----------------------------------------------------------------------------- finder pass
# Email Finder: GET /email-finder?domain=&first_name=&last_name=, 1 credit per found address.
FINDER_MIN_SCORE = 70  # Hunter finder score (0-100) needed to keep an address
FINDER_SOURCE_URL = "hunter:email-finder"
MAX_FINDER_NAMES = 2  # names tried per lead per run
FINDER_STEP_CREDITS = 1  # worst case per finder call without verification (+1 with it)

_FINDER_HONORIFICS = {"dr", "mr", "mrs", "ms", "miss", "mx", "prof", "rev", "sir"}
_FINDER_SUFFIXES = {
    "md", "do", "np", "rn", "phd", "pac", "pa", "pharmd", "mba", "dnp", "fnp", "aprn", "fnpc",
    "bsn", "msn", "lac", "dc", "nd", "dds", "dmd", "cnp", "crna", "lpn", "ms", "ma", "jr", "sr",
    "ii", "iii", "iv", "esq",
}  # fmt: skip
_FINDER_NOT_NAMES = {
    "clinic", "health", "wellness", "medical", "medicine", "team", "staff", "support", "office",
    "llc", "inc", "pllc", "group", "center", "centre", "care", "the", "and", "of", "admin",
}  # fmt: skip
_FINDER_TOKEN_RE = re.compile(r"^[A-Za-z][A-Za-z'\-]*[A-Za-z]$")


def split_person_name(name: str | None) -> tuple[str, str] | None:
    """(first, last) for an Email Finder lookup, or None when the name is not a plausible person.

    Honorifics (Dr., Mrs.) and credentials (", APRN", "MD", "Jr.") are dropped, middle names are
    ignored ('Dr. Carol Ann Linebarger' -> Carol, Linebarger). Single words, initials, digits and
    company-looking strings are refused.
    """
    if not name:
        return None
    head = name.split(",", 1)[0]  # "Paige Erickson, APRN" -> "Paige Erickson"
    tokens = []
    for raw in head.split():
        tok = raw.strip('.()"')
        key = re.sub(r"[^a-z]", "", tok.lower())
        if not key or key in _FINDER_HONORIFICS or key in _FINDER_SUFFIXES:
            continue
        tokens.append(tok)
    if not 2 <= len(tokens) <= 4:
        return None
    if any(re.sub(r"[^a-z]", "", t.lower()) in _FINDER_NOT_NAMES for t in tokens):
        return None
    first, last = tokens[0], tokens[-1]
    if not (_FINDER_TOKEN_RE.match(first) and _FINDER_TOKEN_RE.match(last)):
        return None

    def fix(tok: str) -> str:
        return tok.capitalize() if tok.isupper() or tok.islower() else tok

    return fix(first), fix(last)


def finder_names(lead: Lead) -> list[str]:
    """Plausible person names on the lead: owner first, then decision-makers, then the rest."""
    ranked = sorted(lead.people, key=lambda p: (p.is_decision_maker, p.confidence), reverse=True)
    names: list[str] = []
    seen: set[tuple[str, str]] = set()
    for raw in [lead.owner_name, *(p.name for p in ranked)]:
        split = split_person_name(raw)
        if not raw or not split:
            continue
        key = (split[0].lower(), split[1].lower())
        if key in seen:
            continue
        seen.add(key)
        names.append(raw.strip())
    return names


def _finder_log(lead: Lead) -> dict[str, Any]:
    hunter = lead.source_detail.setdefault("hunter", {})
    if not isinstance(hunter.get("finder"), dict):
        hunter["finder"] = {}
    return hunter["finder"]


def finder_tried_recently(lead: Lead, name: str, now: datetime, days: int = RECHECK_DAYS) -> bool:
    entry = ((lead.source_detail.get("hunter") or {}).get("finder") or {}).get(name) or {}
    raw = entry.get("checked_at") if isinstance(entry, dict) else None
    if not raw:
        return False
    try:
        when = datetime.fromisoformat(str(raw))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return now - when < timedelta(days=days)


def due_finder_names(lead: Lead, now: datetime) -> list[str]:
    return [n for n in finder_names(lead) if not finder_tried_recently(lead, n, now)]


def _apply_found(lead: Lead, name: str, person: Person) -> None:
    """Put a found address on the named Person entry (added when missing) and on the owner fields
    when the name is the owner's, or when the lead has no owner yet."""
    norm = name.strip().lower()
    target = next((p for p in lead.people if p.name.strip().lower() == norm), None)
    if target is None:
        lead.people.append(person)
        target = person
    elif not _is_person_email(target.email, target.name, lead.domain, target.email_status):
        target.email, target.email_status = person.email, person.email_status
        target.verifier_score, target.accept_all = person.verifier_score, person.accept_all
        target.confidence = max(target.confidence, person.confidence)
        target.title = target.title or person.title
        target.linkedin, target.phone = target.linkedin or person.linkedin, target.phone or person.phone
        target.source_url = target.source_url or person.source_url
    if not lead.owner_name:
        lead.owner_name = name.strip()
        lead.owner_title = lead.owner_title or target.title
    if lead.owner_name.strip().lower() == norm and not _is_person_email(
        lead.owner_email, lead.owner_name, lead.domain, lead.owner_email_status
    ):
        lead.owner_email, lead.owner_email_status = target.email, target.email_status
        lead.owner_title = lead.owner_title or target.title
        lead.owner_linkedin = lead.owner_linkedin or target.linkedin
        lead.owner_phone = lead.owner_phone or target.phone
    providers = set(lead.source_detail.get("contact_providers") or [])
    lead.source_detail["contact_providers"] = sorted(providers | {FINDER_SOURCE_URL})


@dataclass
class FinderStats:
    scanned: int = 0  # enriched leads matching the filters
    eligible: int = 0  # ... with a plausible person name and no person email
    skipped_recent: int = 0  # ... whose every name was tried in the last RECHECK_DAYS
    queued: int = 0
    tried: int = 0  # leads with at least one finder call
    calls: int = 0
    found: int = 0  # leads that gained a person email
    low_score: int = 0
    verified: int = 0
    dropped: int = 0
    accept_all: int = 0
    errors: int = 0
    credits_used: int = 0
    stopped: str | None = None
    domains: list[str] = field(default_factory=list)

    def summary(self) -> str:
        line = (
            f"finder: {self.tried} lead(s) tried ({self.calls} lookup(s)), {self.found} found, "
            f"{self.verified} verified, {self.dropped} dropped by the verifier, "
            f"{self.low_score} under the score threshold, {self.credits_used} credit(s) used"
        )
        if self.skipped_recent:
            line += f"; {self.skipped_recent} tried in the last {RECHECK_DAYS} days skipped"
        if self.errors:
            line += f"; {self.errors} error(s)"
        if self.stopped:
            line += f"; stopped: {self.stopped}"
        return line


def select_finder_leads(leads: list[Lead], *, limit: int, now: datetime, stats: FinderStats) -> list[Lead]:
    stats.scanned = len(leads)
    eligible = [lead for lead in leads if finder_names(lead) and not has_person_email(lead)]
    stats.eligible = len(eligible)
    due = [lead for lead in eligible if due_finder_names(lead, now)]
    stats.skipped_recent = len(eligible) - len(due)
    oldest = datetime.min.replace(tzinfo=UTC)
    due.sort(key=lambda lead: (lead.score, lead.last_enriched_at or oldest), reverse=True)
    return due[:limit] if limit else due


def finder_pass(
    store: Any,
    client: HunterClient | None,
    *,
    limit: int = 100,
    max_credits: int | None = 150,
    min_score: int = FINDER_MIN_SCORE,
    verify: bool = True,
    dry_run: bool = False,
    source: str | None = None,
    min_lead_score: int | None = 40,
    save: Callable[[Any, Lead], Lead] | None = None,
    now: datetime | None = None,
    on_lead: Callable[[Lead, str], None] | None = None,
    echo: Callable[[str], None] | None = print,
) -> FinderStats:
    """Email Finder over the names we already hold, for leads without a person email.

    Leads go highest score first; per lead up to MAX_FINDER_NAMES names not tried in the last
    RECHECK_DAYS are looked up until one gives a kept address. A found address needs a finder
    score >= `min_score`; with `verify` it then goes through the Email Verifier (valid ->
    verified, accept_all -> provider with the flag, anything else dropped). Every attempt is
    recorded in source_detail["hunter"]["finder"][name] = {checked_at, outcome, ...}. The run
    stops before a call could take it past `max_credits` (1 per found address, 1 per completed
    verification) or the client's own budget. `echo` prints the summary line (None for silence).
    """
    now = now or datetime.now(UTC)
    stats = FinderStats()
    leads = list(store.iter(source=source, enriched=True, min_score=min_lead_score))
    queue = select_finder_leads(leads, limit=limit, now=now, stats=stats)
    stats.queued = len(queue)
    if dry_run or client is None:
        stats.domains = [lead.domain for lead in queue]
        if echo:
            echo(f"finder dry run: {stats.queued} lead(s) would be tried; no Hunter calls")
        return stats
    persist = save or (lambda st, lead: st.save(lead) or lead)
    start = client.credits_used
    step = FINDER_STEP_CREDITS + (1 if verify else 0)

    def room() -> bool:
        spent = client.credits_used - start
        within_pass = max_credits is None or spent + step <= max_credits
        return within_pass and client.budget.can_spend(step)

    for lead in queue:
        if stats.stopped:
            break
        touched = False
        outcome = ""
        for name in due_finder_names(lead, now)[:MAX_FINDER_NAMES]:
            if not room():
                stats.stopped = f"credit cap of {max_credits} reached"
                break
            first, last = split_person_name(name) or ("", "")
            try:
                found = client.email_finder(lead.domain, first, last)
            except BudgetExhausted as exc:
                stats.stopped = str(exc)
                break
            except HunterStop as exc:
                stats.stopped = str(exc)
                break
            stats.calls += 1
            touched = True
            outcome, kept = _finder_outcome(
                lead, name, found, client, stats, verify=verify, min_score=min_score
            )
            if outcome != "error":  # a network error is retried on the next run
                entry: dict[str, Any] = {"checked_at": now.isoformat(), "outcome": outcome}
                if found is not None and found.email:
                    entry.update(email=found.email, score=found.score)
                _finder_log(lead)[name] = entry
            if kept:
                break
        if not touched:
            continue
        stats.tried += 1
        stats.found += outcome in ("verified", "provider")
        stats.domains.append(lead.domain)
        saved = persist(store, score_lead(lead))
        if on_lead:
            on_lead(saved, outcome)
    stats.credits_used = client.credits_used - start
    if echo:
        echo(stats.summary())
    return stats


def _finder_outcome(
    lead: Lead,
    name: str,
    found: FinderResult | None,
    client: HunterClient,
    stats: FinderStats,
    *,
    verify: bool,
    min_score: int,
) -> tuple[str, bool]:
    """(outcome, kept) for one finder answer; applies a kept address to the lead."""
    if found is None:
        stats.errors += 1
        return "error", False
    if not found.email:
        return "not_found", False
    if (found.score or 0) < min_score:
        stats.low_score += 1
        return "low_score", False
    if classify_local_part(found.email, name, lead.domain) != "person":
        return "not_person", False
    norm = name.strip().lower()
    title = next((p.title for p in lead.people if p.name.strip().lower() == norm and p.title), None)
    if not title and (lead.owner_name or "").strip().lower() == norm:
        title = lead.owner_title
    title = title or found.position
    person = Person(
        name=name.strip(),
        title=title,
        email=found.email,
        email_status="provider",
        linkedin=found.linkedin,
        phone=found.phone,
        source_url=FINDER_SOURCE_URL,
        confidence=(found.score or 0) / 100,
        is_decision_maker=bool(title and DM_TITLE_RE.search(title)),
        accept_all=bool(found.accept_all),
    )
    if verify:
        counts = VerifyCounts()
        try:
            kept = _verify_person(person, client, counts)
        except BudgetExhausted:
            kept = False  # room() reserved the verification, so this only trips on a shared budget
        stats.verified += counts.verified
        stats.dropped += counts.dropped
        stats.accept_all += counts.accept_all
        if not kept:
            return "dropped", False
    _apply_found(lead, name, person)
    return (person.email_status or "provider"), True
