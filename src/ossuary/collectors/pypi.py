"""PyPI registry collector."""

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from ossuary.collectors.base import BaseCollector

logger = logging.getLogger(__name__)


@dataclass
class PyPIData:
    """Data collected from PyPI."""

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


class PyPICollector(BaseCollector):
    """Collector for PyPI data."""

    PYPI_URL = "https://pypi.org/pypi"
    STATS_URL = "https://pypistats.org/api"

    def __init__(self):
        """Initialize PyPI collector."""
        self.client = httpx.AsyncClient(timeout=30.0)

    def is_available(self) -> bool:
        """PyPI collector is always available."""
        return True

    async def get_package_info(self, package_name: str) -> Optional[dict]:
        """Get package metadata from PyPI."""
        try:
            response = await self.client.get(f"{self.PYPI_URL}/{package_name}/json")
            if response.status_code == 200:
                return response.json()
        except httpx.HTTPError as e:
            logger.error(f"PyPI API error: {e}")
        return None

    async def get_weekly_downloads(self, package_name: str) -> int:
        """Get approximate weekly download count."""
        try:
            response = await self.client.get(f"{self.STATS_URL}/packages/{package_name}/recent")
            if response.status_code == 200:
                data = response.json().get("data", {})
                monthly = data.get("last_month", 0)
                return monthly // 4  # Approximate weekly
        except httpx.HTTPError as e:
            logger.error(f"PyPI stats error: {e}")
        return 0

    def _extract_repo_url(self, info: dict) -> str:
        """Extract repository URL from package info."""
        # Check project_urls first
        project_urls = info.get("project_urls", {}) or {}
        for key in ["Repository", "Source", "Source Code", "GitHub", "Code"]:
            if key in project_urls:
                return project_urls[key]

        # Check home_page
        home_page = info.get("home_page", "") or ""
        if "github.com" in home_page or "gitlab.com" in home_page:
            return home_page

        return ""

    async def collect(self, package_name: str) -> PyPIData:
        """
        Collect PyPI package data.

        Args:
            package_name: PyPI package name

        Returns:
            PyPIData with package information
        """
        data = PyPIData(name=package_name)

        # Get package metadata
        pkg_info = await self.get_package_info(package_name)
        if pkg_info:
            info = pkg_info.get("info", {})
            data.version = info.get("version", "")
            data.description = info.get("summary", "")
            data.homepage = info.get("home_page", "")
            data.repository_url = self._extract_repo_url(info)

            # Get maintainer/author
            author = info.get("author", "")
            maintainer = info.get("maintainer", "")
            if maintainer:
                data.maintainers = [maintainer]
            elif author:
                data.maintainers = [author]

        # Get download stats
        data.weekly_downloads = await self.get_weekly_downloads(package_name)

        return data

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()
