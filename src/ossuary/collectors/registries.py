"""Lightweight registry collectors for additional ecosystems.

Each collector fetches repository_url and weekly_downloads from its registry API.
All other scoring data (git history, GitHub info) comes from the shared pipeline.
"""

import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from ossuary.collectors.base import BaseCollector

logger = logging.getLogger(__name__)


@dataclass
class RegistryData:
    """Unified data from any package registry."""

    name: str = ""
    version: str = ""
    description: str = ""
    repository_url: str = ""
    weekly_downloads: int = 0


class CratesCollector(BaseCollector):
    """Collector for crates.io (Rust)."""

    API_URL = "https://crates.io/api/v1"

    def __init__(self):
        self.client = httpx.AsyncClient(
            timeout=30.0,
            headers={"User-Agent": "ossuary-risk (https://github.com/anicka-net/ossuary-risk)"},
        )

    def is_available(self) -> bool:
        return True

    async def collect(self, package_name: str) -> RegistryData:
        data = RegistryData(name=package_name)
        try:
            resp = await self.client.get(f"{self.API_URL}/crates/{package_name}")
            if resp.status_code == 200:
                crate = resp.json().get("crate", {})
                data.version = crate.get("newest_version", "")
                data.description = crate.get("description", "")
                data.repository_url = crate.get("repository", "") or ""
                # recent_downloads is last 90 days
                recent = crate.get("recent_downloads", 0) or 0
                data.weekly_downloads = recent // 13  # ~13 weeks in 90 days
        except httpx.HTTPError as e:
            logger.error(f"crates.io API error: {e}")
        return data

    async def close(self):
        await self.client.aclose()


class RubyGemsCollector(BaseCollector):
    """Collector for RubyGems (Ruby)."""

    API_URL = "https://rubygems.org/api/v1"

    def __init__(self):
        self.client = httpx.AsyncClient(timeout=30.0)

    def is_available(self) -> bool:
        return True

    async def collect(self, package_name: str) -> RegistryData:
        data = RegistryData(name=package_name)
        try:
            resp = await self.client.get(f"{self.API_URL}/gems/{package_name}.json")
            if resp.status_code == 200:
                gem = resp.json()
                data.version = gem.get("version", "")
                data.description = gem.get("info", "")
                # RubyGems uses source_code_uri or homepage_uri
                repo = (
                    gem.get("source_code_uri", "")
                    or gem.get("homepage_uri", "")
                    or ""
                )
                # Clean version-specific paths (e.g. /tree/v8.1.2)
                import re
                data.repository_url = re.sub(r"/tree/.*$", "", repo)
                # downloads is total; version_downloads is for latest version
                # Use downloads endpoint for recent data
                total = gem.get("downloads", 0)
                # Approximate: assume 5-year lifetime, convert to weekly
                # This is rough; better than nothing
                data.weekly_downloads = total // 260 if total else 0
        except httpx.HTTPError as e:
            logger.error(f"RubyGems API error: {e}")

        # Try to get more accurate download stats
        try:
            resp = await self.client.get(
                f"{self.API_URL}/versions/{package_name}/downloads.json"
            )
            if resp.status_code == 200:
                versions = resp.json()
                # Sum recent downloads across versions
                if isinstance(versions, dict):
                    # Get the most recent 7 days if available
                    pass  # Total downloads approximation is sufficient
        except httpx.HTTPError:
            pass

        return data

    async def close(self):
        await self.client.aclose()


class PackagistCollector(BaseCollector):
    """Collector for Packagist (PHP/Composer)."""

    API_URL = "https://packagist.org"

    def __init__(self):
        self.client = httpx.AsyncClient(timeout=30.0)

    def is_available(self) -> bool:
        return True

    async def collect(self, package_name: str) -> RegistryData:
        """Collect data. package_name should be vendor/package format."""
        data = RegistryData(name=package_name)
        try:
            resp = await self.client.get(f"{self.API_URL}/packages/{package_name}.json")
            if resp.status_code == 200:
                pkg = resp.json().get("package", {})
                data.description = pkg.get("description", "")
                data.repository_url = pkg.get("repository", "") or ""
                # Clean up git:// URLs
                if data.repository_url.startswith("git://"):
                    data.repository_url = data.repository_url.replace("git://", "https://")

                downloads = pkg.get("downloads", {})
                data.weekly_downloads = downloads.get("daily", 0) * 7

                # Get latest version
                versions = pkg.get("versions", {})
                if versions:
                    # First key is usually the latest
                    latest = next(iter(versions), {})
                    if isinstance(versions.get(latest), dict):
                        data.version = versions[latest].get("version", "")
        except httpx.HTTPError as e:
            logger.error(f"Packagist API error: {e}")
        return data

    async def close(self):
        await self.client.aclose()


class NuGetCollector(BaseCollector):
    """Collector for NuGet (.NET)."""

    API_URL = "https://api.nuget.org/v3"
    SEARCH_URL = "https://azuresearch-usnc.nuget.org/query"

    def __init__(self):
        self.client = httpx.AsyncClient(timeout=30.0)

    def is_available(self) -> bool:
        return True

    async def collect(self, package_name: str) -> RegistryData:
        data = RegistryData(name=package_name)
        try:
            # Use search API for metadata
            resp = await self.client.get(
                self.SEARCH_URL,
                params={"q": f"packageid:{package_name}", "take": 1},
            )
            if resp.status_code == 200:
                results = resp.json().get("data", [])
                if results:
                    pkg = results[0]
                    data.version = pkg.get("version", "")
                    data.description = pkg.get("description", "")
                    data.weekly_downloads = pkg.get("totalDownloads", 0) // 260

                    # Try to find repo URL from projectUrl or registration data
                    project_url = pkg.get("projectUrl", "") or ""
                    if "github.com" in project_url or "gitlab.com" in project_url:
                        data.repository_url = project_url

                    # Also check package metadata for source repo
                    if not data.repository_url:
                        reg_url = f"{self.API_URL}/registration5-gz-semver2/{package_name.lower()}/index.json"
                        reg_resp = await self.client.get(reg_url)
                        if reg_resp.status_code == 200:
                            pages = reg_resp.json().get("items", [])
                            if pages:
                                items = pages[-1].get("items", [])
                                if items:
                                    catalog = items[-1].get("catalogEntry", {})
                                    repo_url = catalog.get("projectUrl", "") or ""
                                    if "github.com" in repo_url or "gitlab.com" in repo_url:
                                        data.repository_url = repo_url
        except httpx.HTTPError as e:
            logger.error(f"NuGet API error: {e}")
        return data

    async def close(self):
        await self.client.aclose()


class GoProxyCollector(BaseCollector):
    """Collector for Go modules via proxy.golang.org."""

    PROXY_URL = "https://proxy.golang.org"
    PKG_URL = "https://pkg.go.dev"

    def __init__(self):
        self.client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)

    def is_available(self) -> bool:
        return True

    async def collect(self, package_name: str) -> RegistryData:
        """Collect data. package_name is the Go module path (e.g. github.com/gin-gonic/gin)."""
        data = RegistryData(name=package_name)

        # For Go modules, the module path often IS the repo URL
        if package_name.startswith("github.com/"):
            data.repository_url = f"https://{package_name}"
        elif package_name.startswith("golang.org/x/"):
            # Standard library extensions are on GitHub
            name = package_name.split("/")[-1]
            data.repository_url = f"https://github.com/golang/{name}"

        try:
            # Get latest version from proxy
            resp = await self.client.get(f"{self.PROXY_URL}/{package_name}/@latest")
            if resp.status_code == 200:
                info = resp.json()
                data.version = info.get("Version", "").lstrip("v")
        except httpx.HTTPError as e:
            logger.error(f"Go proxy error: {e}")

        # Go doesn't have a public download count API
        # Downloads are proxied through proxy.golang.org but stats aren't public
        # We rely on GitHub stars as visibility proxy
        data.weekly_downloads = 0

        return data

    async def close(self):
        await self.client.aclose()


# Registry lookup for ecosystem -> collector class
REGISTRY_COLLECTORS = {
    "cargo": CratesCollector,
    "rubygems": RubyGemsCollector,
    "packagist": PackagistCollector,
    "nuget": NuGetCollector,
    "go": GoProxyCollector,
}
