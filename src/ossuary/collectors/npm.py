"""npm registry collector."""

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from ossuary.collectors.base import BaseCollector

logger = logging.getLogger(__name__)


@dataclass
class NpmData:
    """Data collected from npm registry."""

    name: str = ""
    version: str = ""
    description: str = ""
    homepage: str = ""
    repository_url: str = ""
    weekly_downloads: int = 0
    maintainers: list[str] = None

    def __post_init__(self):
        if self.maintainers is None:
            self.maintainers = []


class NpmCollector(BaseCollector):
    """Collector for npm registry data."""

    REGISTRY_URL = "https://registry.npmjs.org"
    DOWNLOADS_URL = "https://api.npmjs.org/downloads"

    def __init__(self):
        """Initialize npm collector."""
        self.client = httpx.AsyncClient(timeout=30.0)

    def is_available(self) -> bool:
        """npm collector is always available."""
        return True

    async def get_package_info(self, package_name: str) -> Optional[dict]:
        """Get package metadata from npm registry."""
        try:
            response = await self.client.get(f"{self.REGISTRY_URL}/{package_name}")
            if response.status_code == 200:
                return response.json()
        except httpx.HTTPError as e:
            logger.error(f"npm registry error: {e}")
        return None

    async def get_weekly_downloads(self, package_name: str) -> int:
        """Get weekly download count for a package."""
        try:
            response = await self.client.get(f"{self.DOWNLOADS_URL}/point/last-week/{package_name}")
            if response.status_code == 200:
                return response.json().get("downloads", 0)
        except httpx.HTTPError as e:
            logger.error(f"npm downloads API error: {e}")
        return 0

    async def collect(self, package_name: str) -> NpmData:
        """
        Collect npm package data.

        Args:
            package_name: npm package name

        Returns:
            NpmData with package information
        """
        data = NpmData(name=package_name)

        # Get package metadata
        pkg_info = await self.get_package_info(package_name)
        if pkg_info:
            latest = pkg_info.get("dist-tags", {}).get("latest", "")
            data.version = latest
            data.description = pkg_info.get("description", "")
            data.homepage = pkg_info.get("homepage", "")

            # Get repository URL
            repo = pkg_info.get("repository", {})
            if isinstance(repo, dict):
                data.repository_url = repo.get("url", "")
            elif isinstance(repo, str):
                data.repository_url = repo

            # Clean up repository URL
            if data.repository_url:
                data.repository_url = (
                    data.repository_url.replace("git+", "")
                    .replace("git://", "https://")
                    .replace(".git", "")
                )
                if data.repository_url.startswith("ssh://"):
                    data.repository_url = data.repository_url.replace("ssh://git@", "https://")

            # Get maintainers
            maintainers = pkg_info.get("maintainers", [])
            data.maintainers = [m.get("name", "") for m in maintainers if isinstance(m, dict)]

        # Get download stats
        data.weekly_downloads = await self.get_weekly_downloads(package_name)

        return data

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()
