"""Base collector interface."""

from abc import ABC, abstractmethod
from typing import Any


class BaseCollector(ABC):
    """Abstract base class for data collectors."""

    @abstractmethod
    async def collect(self, identifier: str) -> dict[str, Any]:
        """
        Collect data for the given identifier.

        Args:
            identifier: Package name, repo URL, or other identifier

        Returns:
            Dictionary of collected data
        """
        pass

    @abstractmethod
    def is_available(self) -> bool:
        """Check if this collector is available (has required credentials, etc.)."""
        pass
