"""npm registry collector."""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

import httpx

from ossuary.collectors.base import BaseCollector

logger = logging.getLogger(__name__)


@dataclass
class NpmData:
    """Data collected from npm registry.

    ``weekly_downloads`` is ``None`` when the api.npmjs.org fetch failed
    (rate limit, server error, malformed response). ``0`` is reserved
    for "package genuinely has zero downloads in the last week."
    Callers must treat ``None`` as a fetch failure and propagate via
    ``CollectedData.fetch_errors`` so the score is marked
    ``INSUFFICIENT_DATA`` rather than silently produced from partial
    data — without this signal, the engine cannot tell a 50M-downloads
    package from a 0-downloads one and the visibility bonus (the single
    largest protective factor, −10 to −20) silently fails to land.
    """

    name: str = ""
    version: str = ""
    description: str = ""
    homepage: str = ""
    repository_url: str = ""
    weekly_downloads: Optional[int] = None
    maintainers: list[str] = None
    fetch_errors: list[str] = field(default_factory=list)

    def __post_init__(self):
        if self.maintainers is None:
            self.maintainers = []


# Retry policy mirrors the PyPI collector. registry.npmjs.org and
# api.npmjs.org rate-limit (HTTP 429) and occasionally 5xx; two retries
# cover the common transient cases.
_MAX_RETRIES = 2
_BACKOFF_RATE_LIMIT_DEFAULT = 5.0
_BACKOFF_RATE_LIMIT_MAX = 30.0
_BACKOFF_SERVER_ERROR = 1.5
_BACKOFF_TIMEOUT = 1.0


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

    async def _fetch_with_retry(
        self, url: str, *, source_label: str
    ) -> tuple[Optional[httpx.Response], Optional[str]]:
        """Fetch ``url`` with the smart-retry policy.

        Returns ``(response, error_string)``. Exactly one is non-None.
        """
        last_error = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                response = await self.client.get(url)
            except httpx.TimeoutException as exc:
                last_error = f"{source_label}: timeout ({exc})"
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_BACKOFF_TIMEOUT * (attempt + 1))
                    continue
                return None, last_error
            except httpx.HTTPError as exc:
                last_error = f"{source_label}: transport error ({exc})"
                return None, last_error

            if response.status_code == 200:
                return response, None

            if response.status_code == 429:
                retry_after_raw = response.headers.get("Retry-After")
                wait_s = _BACKOFF_RATE_LIMIT_DEFAULT
                if retry_after_raw:
                    try:
                        wait_s = min(float(retry_after_raw), _BACKOFF_RATE_LIMIT_MAX)
                    except ValueError:
                        pass
                last_error = (
                    f"{source_label}: HTTP 429 (rate limited) from "
                    f"{response.url.host}"
                )
                if attempt < _MAX_RETRIES:
                    logger.warning(
                        "%s rate-limited; sleeping %.1fs before retry %d/%d",
                        source_label, wait_s, attempt + 1, _MAX_RETRIES,
                    )
                    await asyncio.sleep(wait_s)
                    continue
                return None, last_error
            if 500 <= response.status_code < 600:
                last_error = (
                    f"{source_label}: HTTP {response.status_code} from "
                    f"{response.url.host}"
                )
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_BACKOFF_SERVER_ERROR * (attempt + 1))
                    continue
                return None, last_error
            return None, (
                f"{source_label}: HTTP {response.status_code} from "
                f"{response.url.host}"
            )
        return None, last_error  # pragma: no cover — defensive

    async def get_package_info(
        self, package_name: str
    ) -> tuple[Optional[dict], Optional[str]]:
        """Get package metadata from npm registry.

        Returns ``(info_dict, error)`` — exactly one is non-None.
        """
        response, err = await self._fetch_with_retry(
            f"{self.REGISTRY_URL}/{package_name}",
            source_label="npm.package_info",
        )
        if err:
            logger.error(err)
            return None, err
        try:
            return response.json(), None
        except ValueError as exc:
            err = f"npm.package_info: malformed JSON ({exc})"
            logger.error(err)
            return None, err

    async def get_weekly_downloads(
        self, package_name: str
    ) -> tuple[Optional[int], Optional[str]]:
        """Get weekly download count for a package.

        Returns ``(count, error)`` — exactly one is non-None.
        ``count == 0`` means the package genuinely had zero downloads
        in the last week; ``count is None`` paired with a non-None
        ``error`` means the fetch failed.
        """
        response, err = await self._fetch_with_retry(
            f"{self.DOWNLOADS_URL}/point/last-week/{package_name}",
            source_label="npm.weekly_downloads",
        )
        if err:
            logger.warning(err)
            return None, err
        try:
            payload = response.json()
        except ValueError as exc:
            err = f"npm.weekly_downloads: malformed JSON ({exc})"
            logger.error(err)
            return None, err
        if not isinstance(payload, dict) or "downloads" not in payload:
            err = "npm.weekly_downloads: unexpected response schema"
            logger.error(err)
            return None, err
        try:
            return int(payload["downloads"]), None
        except (TypeError, ValueError) as exc:
            err = f"npm.weekly_downloads: downloads not an int ({exc})"
            logger.error(err)
            return None, err

    async def collect(self, package_name: str) -> NpmData:
        """Collect npm package data.

        Args:
            package_name: npm package name

        Returns:
            NpmData with package information. Any upstream fetch
            failures appear in ``data.fetch_errors`` and propagate to
            ``CollectedData.fetch_errors`` so the engine can
            short-circuit to ``RiskLevel.INSUFFICIENT_DATA``.
        """
        data = NpmData(name=package_name)

        # Get package metadata
        pkg_info, info_err = await self.get_package_info(package_name)
        if info_err:
            data.fetch_errors.append(info_err)
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

        # Get download stats. None means failure (already in fetch_errors);
        # 0 means the package genuinely had no recent downloads.
        downloads, downloads_err = await self.get_weekly_downloads(package_name)
        if downloads_err:
            data.fetch_errors.append(downloads_err)
        data.weekly_downloads = downloads

        return data

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()
