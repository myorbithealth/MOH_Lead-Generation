"""Hunter.io API v2 client: Domain Search, Email Finder and Email Verifier.

Docs: https://hunter.io/api-documentation/v2

* Auth is the ``X-API-KEY`` header (the key never goes in a URL, so it stays out of logs).
* Hunter allows 15 requests/s (10/s for the verifier); a shared token bucket keeps every client in
  this process at or under 10 requests/s.
* A 429, a 5xx, a 403 ``too_many_requests`` or a verifier 202 ("still running") is retried once,
  after Retry-After when Hunter sends it. A 429 that persists is Hunter's usage limit: the client
  raises HunterStop so a batch ends cleanly instead of burning through errors.
* Credits are counted the way Hunter bills them: 1 per Domain Search that returns at least one
  email, 1 per Email Finder hit, 1 per completed verification. An optional CreditBudget refuses
  a call (BudgetExhausted) before it could take the run over its cap.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from ..config import settings
from ..models import Person

log = logging.getLogger(__name__)

HUNTER = "https://api.hunter.io/v2"
MAX_RATE_PER_SECOND = 10.0
MAX_RETRY_WAIT_SECONDS = 60.0


class HunterStop(Exception):
    """The run cannot continue against Hunter (usage limit, bad key, credit budget spent)."""


class BudgetExhausted(HunterStop):
    """The per-run credit budget has no room for another billable call."""


class TokenBucket:
    """Thread-safe token bucket: at most `rate` acquisitions per second on average."""

    def __init__(
        self,
        rate: float = MAX_RATE_PER_SECOND,
        capacity: float | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.rate = rate
        self.capacity = capacity if capacity is not None else rate
        self.tokens = self.capacity
        self.clock, self.sleep = clock, sleep
        self.updated = clock()
        self.lock = threading.Lock()

    def _refill(self) -> None:
        now = self.clock()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now

    def acquire(self) -> None:
        with self.lock:
            self._refill()
            if self.tokens < 1:
                self.sleep((1 - self.tokens) / self.rate)
                self._refill()
                self.tokens = max(self.tokens, 1.0)
            self.tokens -= 1


_SHARED_BUCKET = TokenBucket()


class CreditBudget:
    """Credits one run may spend; None means no cap (used by the per-lead verify in enrich)."""

    def __init__(self, max_credits: int | None = None):
        self.max_credits = max_credits
        self.used = 0
        self.lock = threading.Lock()

    @property
    def remaining(self) -> int | None:
        return None if self.max_credits is None else max(0, self.max_credits - self.used)

    def can_spend(self, n: int = 1) -> bool:
        return self.max_credits is None or self.used + n <= self.max_credits

    def require(self, n: int = 1) -> None:
        if not self.can_spend(n):
            raise BudgetExhausted(f"credit budget of {self.max_credits} spent")

    def charge(self, n: int = 1) -> None:
        with self.lock:
            self.used += n


@dataclass
class DomainSearchResult:
    emails: list[dict[str, Any]] = field(default_factory=list)
    pattern: str | None = None
    accept_all: bool | None = None
    organization: str | None = None


@dataclass
class VerifyResult:
    status: str  # valid | invalid | accept_all | webmail | disposable | unknown
    score: int | None = None
    accept_all: bool | None = None


@dataclass
class FinderResult:
    """One Email Finder answer. email None means Hunter found no address (no credit charged)."""

    email: str | None = None
    score: int | None = None  # Hunter confidence 0-100
    position: str | None = None
    linkedin: str | None = None
    phone: str | None = None
    accept_all: bool | None = None
    verification_status: str | None = None


def verification_outcome(result: VerifyResult | None) -> tuple[str | None, bool]:
    """(email_status to keep, accept_all flag); status None means drop the address.

    valid -> verified; accept_all -> provider, flagged (the server accepts every mailbox, so a
    bounce is still possible); webmail, disposable, invalid, unknown or no answer -> drop.
    """
    if result is None:
        return None, False
    if result.status == "valid":
        return "verified", False
    if result.status == "accept_all":
        return "provider", True
    return None, False


def _retry_after(r: httpx.Response, default: float = 1.0) -> float:
    raw = r.headers.get("Retry-After")
    if not raw:
        return default
    try:
        wait = float(raw)
    except ValueError:
        try:
            wait = (parsedate_to_datetime(raw) - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError):
            return default
    return min(MAX_RETRY_WAIT_SECONDS, max(0.0, wait))


def _is_rate_limited(r: httpx.Response) -> bool:
    return r.status_code == 403 and "too_many_requests" in r.text


def _split(name: str) -> tuple[str, str]:
    from .people_data import _split as split_name

    return split_name(name)


class HunterClient:
    def __init__(
        self,
        key: str | None = None,
        *,
        budget: CreditBudget | None = None,
        transport: httpx.BaseTransport | None = None,
        bucket: TokenBucket | None = None,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = 40,
    ):
        self.key = key or settings.hunter_key
        self.budget = budget or CreditBudget(None)
        self.bucket = bucket or _SHARED_BUCKET
        self.sleep = sleep
        self.client = httpx.Client(
            base_url=HUNTER, timeout=timeout, headers={"X-API-KEY": self.key}, transport=transport
        )

    def available(self) -> bool:
        return bool(self.key)

    def close(self) -> None:
        self.client.close()

    @property
    def credits_used(self) -> int:
        return self.budget.used

    # ------------------------------------------------------------------ transport
    def _get(self, path: str, params: dict[str, Any]) -> httpx.Response | None:
        """GET with the rate limiter and one retry. None on a network error; raises HunterStop on
        a usage limit or a rejected key."""
        r: httpx.Response | None = None
        for attempt in range(2):
            self.bucket.acquire()
            try:
                r = self.client.get(path, params=params)
            except httpx.HTTPError as exc:
                if attempt == 0:
                    continue
                log.warning("hunter %s failed: %s", path, exc)
                return None
            retryable = r.status_code in (202, 429) or r.status_code >= 500 or _is_rate_limited(r)
            if retryable and attempt == 0:
                self.sleep(_retry_after(r, 2.0 if r.status_code == 202 else 1.0))
                continue
            break
        if r is None:
            return None
        if r.status_code == 429:
            raise HunterStop("Hunter usage limit reached (429)")
        if r.status_code == 401:
            raise HunterStop("Hunter rejected the API key (401)")
        return r

    # ------------------------------------------------------------------ endpoints
    def domain_search(self, domain: str, *, limit: int = 10) -> DomainSearchResult | None:
        """GET /domain-search?domain=&type=personal&limit=. 1 credit when any email comes back."""
        self.budget.require(1)
        r = self._get("/domain-search", {"domain": domain, "type": "personal", "limit": limit})
        if r is None or r.status_code != 200:
            if r is not None:
                log.warning("hunter domain-search %s for %s: %s", r.status_code, domain, r.text[:200])
            return None
        data = r.json().get("data") or {}
        emails = [e for e in data.get("emails") or [] if isinstance(e, dict) and e.get("value")]
        if emails:
            self.budget.charge(1)
        return DomainSearchResult(
            emails=emails,
            pattern=data.get("pattern") or None,
            accept_all=data.get("accept_all"),
            organization=data.get("organization"),
        )

    def verify_email(self, email: str) -> VerifyResult | None:
        """GET /email-verifier?email=. 1 verification credit when it completes (200)."""
        self.budget.require(1)
        r = self._get("/email-verifier", {"email": email})
        if r is None:
            return None
        if r.status_code in (202, 222):  # still running / SMTP server misbehaved: no verdict
            return VerifyResult(status="unknown")
        if r.status_code != 200:
            log.warning("hunter verifier %s for %s: %s", r.status_code, email, r.text[:200])
            return None
        data = r.json().get("data") or {}
        status = data.get("status")
        if not status:
            return None
        self.budget.charge(1)
        return VerifyResult(status=status, score=data.get("score"), accept_all=data.get("accept_all"))

    def verify(self, email: str) -> str | None:
        """Hunter's verifier status for one address (kept for older callers)."""
        result = self.verify_email(email)
        return result.status if result else None

    def email_finder(self, domain: str, first_name: str, last_name: str) -> FinderResult | None:
        """GET /email-finder?domain=&first_name=&last_name=. 1 credit when an email is found.

        None on a network or HTTP error; FinderResult(email=None) when Hunter found nothing.
        Raises BudgetExhausted before the call when the budget has no room, HunterStop on a usage
        limit or a rejected key.
        """
        self.budget.require(1)
        params = {"domain": domain, "first_name": first_name, "last_name": last_name}
        r = self._get("/email-finder", params)
        if r is None:
            return None
        if r.status_code in (400, 404):  # no result for this person (or an unusable name)
            return FinderResult()
        if r.status_code != 200:
            log.warning("hunter email-finder %s for %s: %s", r.status_code, domain, r.text[:200])
            return None
        data = r.json().get("data") or {}
        email = (data.get("email") or "").strip().lower() or None
        if not email:
            return FinderResult()
        self.budget.charge(1)
        verification = data.get("verification") or {}
        return FinderResult(
            email=email,
            score=int(data.get("score") or 0),
            position=data.get("position") or None,
            linkedin=data.get("linkedin_url") or None,
            phone=data.get("phone_number") or None,
            accept_all=data.get("accept_all"),
            verification_status=verification.get("status") if isinstance(verification, dict) else None,
        )

    def find_email(self, person: Person, domain: str) -> Person:
        """Email Finder for one Person, filling it in place (kept for older callers)."""
        first, last = _split(person.name)
        if not (first and last) or not self.budget.can_spend(1):
            return person
        try:
            found = self.email_finder(domain, first, last)
        except HunterStop as exc:
            log.warning("hunter finder stopped: %s", exc)
            return person
        if found is None or not found.email:
            return person
        person.email = found.email
        score = found.score or 0
        person.email_status = "verified" if score >= 90 else "provider"
        person.confidence = max(person.confidence, score / 100)
        person.linkedin = person.linkedin or found.linkedin
        person.phone = person.phone or found.phone
        return person
