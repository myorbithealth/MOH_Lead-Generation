"""SendBlue (iMessage / SMS) client for the warm texting channel. Never used for cold outreach.

API facts used here, from https://docs.sendblue.com (checked 2026-11-27):

* POST https://api.sendblue.com/api/send-message with headers `sb-api-key-id` and
  `sb-api-secret-key`; JSON fields `number` (E.164, required), `content`, `from_number`,
  `media_url`, `status_callback`. The message id SendBlue documents is `message_handle`.
  (https://docs.sendblue.com/getting-started/sending-messages)
* GET /api/v2/messages/:message_id returns one message, including its `status`.
  (https://docs.sendblue.com/api-v2/messages)
* Limits per line: burst 10 messages per second; new contacts 50 per line per day (3 AM ET to
  3 AM ET) and 15 per rolling hour on Blue Ocean lines. AI Agent lines: 200 follow-up contacts
  per day per line. A 429 means the request rate was exceeded; send-time capacity declines come
  back as HTTP 400. (https://docs.sendblue.com/limits)

The rate limiter only knows what this process has sent. Callers seed it with numbers already
texted (contacts.last_texted_at not null) so those do not count as new contacts.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from ..config import settings

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.sendblue.com"
BURST_PER_SECOND = 10
NEW_CONTACTS_PER_DAY = 50
NEW_CONTACTS_PER_HOUR = 15
_DAY_RESET_TZ = ZoneInfo("America/New_York")  # the daily new-contact window runs 3 AM ET to 3 AM ET
_DAY_RESET_HOUR = 3

_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")


class SendblueError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"sendblue {status}: {message}")
        self.status = status
        self.body = message


class RateLimitExceeded(RuntimeError):
    """A documented per-line cap would be exceeded; try again in the next window."""


# ---------------------------------------------------------------------- phone numbers
def is_e164(number: str | None) -> bool:
    return bool(number) and bool(_E164_RE.match(number))


def normalize_us_e164(raw: str | None) -> str | None:
    """'(415) 555-0134', '415.555.0134', '1-415-555-0134', '+14155550134' -> '+14155550134'.

    US / NANP numbers only: area code and exchange must start with 2-9. Extensions ('x12') are
    dropped. Anything else returns None, so the caller never texts a guessed number.
    """
    if not raw:
        return None
    text = re.split(r"(?i)\s*(?:ext\.?|x|#)\s*\d+\s*$", str(raw).strip())[0]
    if text.startswith("+") and not text.startswith("+1"):
        return None
    digits = re.sub(r"\D", "", text)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10 or digits[0] in "01" or digits[3] in "01":
        return None
    return f"+1{digits}"


# ---------------------------------------------------------------------- rate limiting
class LineRateLimiter:
    """Token bucket for the per-second burst plus counters for the new-contact caps.

    `clock` returns seconds (monotonic) and `now` returns an aware datetime; both are injectable
    so tests run without sleeping.
    """

    def __init__(
        self,
        *,
        per_second: int = BURST_PER_SECOND,
        new_per_day: int = NEW_CONTACTS_PER_DAY,
        new_per_hour: int | None = NEW_CONTACTS_PER_HOUR,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] | None = None,
    ):
        self.rate = float(per_second)
        self.capacity = float(per_second)
        self.tokens = float(per_second)
        self.new_per_day = new_per_day
        self.new_per_hour = new_per_hour
        self.clock = clock
        self.sleep = sleep
        self.now = now or (lambda: datetime.now(_DAY_RESET_TZ))
        self.known: set[str] = set()
        self._day: date | None = None
        self._day_count = 0
        self._hour: deque[datetime] = deque()
        self._last = clock()
        self._lock = threading.Lock()

    def mark_known(self, numbers: Iterable[str]) -> None:
        self.known.update(n for n in numbers if n)

    @staticmethod
    def _window_day(moment: datetime) -> date:
        local = moment.astimezone(_DAY_RESET_TZ)
        return (local - timedelta(hours=_DAY_RESET_HOUR)).date()

    def check_new_contact(self, number: str) -> None:
        """Raise RateLimitExceeded if texting `number` would break a new-contact cap."""
        if number in self.known:
            return
        moment = self.now()
        if self._window_day(moment) != self._day:
            self._day, self._day_count = self._window_day(moment), 0
        while self._hour and moment - self._hour[0] >= timedelta(hours=1):
            self._hour.popleft()
        if self._day_count >= self.new_per_day:
            raise RateLimitExceeded(f"{self.new_per_day} new contacts per line per day reached")
        if self.new_per_hour is not None and len(self._hour) >= self.new_per_hour:
            raise RateLimitExceeded(f"{self.new_per_hour} new contacts per line per hour reached")

    def record(self, number: str) -> None:
        if number in self.known:
            return
        self.known.add(number)
        self._day_count += 1
        self._hour.append(self.now())

    def take(self) -> None:
        """Block until one send token is available (burst cap)."""
        with self._lock:
            while True:
                current = self.clock()
                self.tokens = min(self.capacity, self.tokens + (current - self._last) * self.rate)
                self._last = current
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                wait = (1 - self.tokens) / self.rate
                self.sleep(wait)


# ---------------------------------------------------------------------- client
class SendblueClient:
    def __init__(
        self,
        api_key_id: str | None = None,
        api_secret: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        *,
        from_number: str | None = None,
        transport: httpx.BaseTransport | None = None,
        limiter: LineRateLimiter | None = None,
        retry_delay: float = 1.0,
        timeout: float = 30,
    ):
        api_key_id = api_key_id or settings.sendblue_api_key_id
        api_secret = api_secret or settings.sendblue_api_secret
        if not api_key_id or not api_secret:
            raise RuntimeError("Set SENDBLUE_API_KEY_ID and SENDBLUE_API_SECRET_KEY to send texts")
        self.from_number = from_number or settings.sendblue_from_number or None
        self.limiter = limiter or LineRateLimiter()
        self.retry_delay = retry_delay
        self.client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={
                "sb-api-key-id": api_key_id,
                "sb-api-secret-key": api_secret,
                "Content-Type": "application/json",
            },
            timeout=timeout,
            transport=transport,
        )

    def _request(self, method: str, path: str, **kwargs) -> Any:
        """One request, retried once on a 5xx or a connection error; a 4xx raises with the body."""
        for attempt in (1, 2):
            try:
                r = self.client.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                if attempt == 2:
                    raise SendblueError(0, str(exc)) from exc
                time.sleep(self.retry_delay)
                continue
            if r.status_code >= 500 and attempt == 1:
                log.info("sendblue %s %s -> %s, retrying once", method, path, r.status_code)
                time.sleep(self.retry_delay)
                continue
            if r.status_code >= 400:
                raise SendblueError(r.status_code, r.text[:1000])
            return r.json() if r.content else {}
        raise AssertionError("unreachable")

    def send_message(
        self,
        number_e164: str,
        content: str,
        from_number: str | None = None,
        media_url: str | None = None,
        status_callback: str | None = None,
    ) -> dict:
        number = number_e164 if is_e164(number_e164) else normalize_us_e164(number_e164)
        if not number:
            raise ValueError(f"not an E.164 phone number: {number_e164!r}")
        if not content and not media_url:
            raise ValueError("a text needs content or media_url")
        payload: dict[str, Any] = {"number": number}
        if content:
            payload["content"] = content
        sender = from_number or self.from_number
        if sender:
            payload["from_number"] = sender
        if media_url:
            payload["media_url"] = media_url
        if status_callback:
            payload["status_callback"] = status_callback
        self.limiter.check_new_contact(number)
        self.limiter.take()
        result = self._request("POST", "/api/send-message", json=payload)
        self.limiter.record(number)
        return result

    def get_message_status(self, message_id: str) -> dict:
        """The message object (with `status`) for a message_handle / id."""
        return self._request("GET", f"/api/v2/messages/{message_id}")

    def close(self) -> None:
        self.client.close()


def message_id(result: dict) -> str | None:
    """The id SendBlue returned for a send (message_handle; some responses nest it)."""
    if not isinstance(result, dict):
        return None
    for key in ("message_handle", "messageId", "id"):
        if result.get(key):
            return str(result[key])
    data = result.get("data")
    return message_id(data) if isinstance(data, dict) else None
