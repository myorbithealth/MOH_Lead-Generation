from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from ..models import Candidate


class DiscoverySource(ABC):
    """Yields candidate domains. Implementations must be cheap to construct and lazy to run."""

    name: str = "base"

    @abstractmethod
    def discover(self, limit: int | None = None) -> Iterator[Candidate]: ...

    def available(self) -> bool:
        """False when a required API key or input is missing. The CLI skips unavailable sources."""
        return True
