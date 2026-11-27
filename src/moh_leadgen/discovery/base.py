from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from ..config import settings
from ..models import Candidate


class DiscoverySource(ABC):
    """Yields candidate domains. Implementations must be cheap to construct and lazy to run.

    `discover(limit=...)` caps how many candidates the source *scans* (known domains included).
    The CLI's `--limit` counts NEW domains and is enforced by the caller, never passed here,
    because most yields are already in the store once the seed lists are loaded.
    """

    name: str = "base"
    scan_limit: int | None = None  # per-source override of settings.discovery_scan_limit

    @abstractmethod
    def discover(self, limit: int | None = None) -> Iterator[Candidate]: ...

    def effective_scan_limit(self) -> int:
        """The safety cap the CLI passes to discover(): this source's own, else the global one."""
        return self.scan_limit or settings.discovery_scan_limit

    def available(self) -> bool:
        """False when a required API key or input is missing. The CLI skips unavailable sources."""
        return True
