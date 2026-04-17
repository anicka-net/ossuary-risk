"""PyPI registry collector."""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

import httpx

from ossuary.collectors.base import BaseCollector

logger = logging.getLogger(__name__)


@dataclass
class PyPIData:
    """Data collected from PyPI.

    ``weekly_downloads`` is ``None`` when the pypistats.org fetch failed
    (rate limit, server error, malformed response). ``0`` is reserved
    for "package genuinely has zero downloads." Callers must treat
    ``None`` as a fetch failure and propagate via
    ``CollectedData.fetch_errors`` so the score is marked
    ``INSUFFICIENT_DATA`` rather than silently produced from partial
    data.
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


# Retry policy: pypistats.org and pypi.org both occasionally rate-limit
# (HTTP 429) or return 5xx during high-load periods. Two retries cover
# the common transient cases; past that we treat the fetch as a hard
# failure and surface it to the caller. Backoff windows are
# error-class-aware: 429 honours Retry-After when present (and otherwise
# uses a long pause), 5xx uses a short pause, and 4xx-non-429 is treated
# as a permanent error with no retry.
_MAX_RETRIES = 2
_BACKOFF_RATE_LIMIT_DEFAULT = 5.0  # seconds, when 429 has no Retry-After
_BACKOFF_RATE_LIMIT_MAX = 30.0     # cap on Retry-After we'll honour
_BACKOFF_SERVER_ERROR = 1.5        # seconds, for 5xx
_BACKOFF_TIMEOUT = 1.0             # seconds, for transport-level timeouts


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

    async def _fetch_with_retry(
        self, url: str, *, source_label: str
    ) -> tuple[Optional[httpx.Response], Optional[str]]:
        """Fetch ``url`` with the smart-retry policy.

        Returns ``(response, error_string)``. Exactly one of the two is
        non-None: a ``response`` with status 200 means success;
        an ``error_string`` like
        ``"<source_label>: HTTP 429 from pypistats.org"`` means the
        fetch failed in a known way after retries.
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
                # Transport errors are usually not retryable (DNS, TLS).
                return None, last_error

            if response.status_code == 200:
                return response, None

            # Non-200 — decide whether to retry.
            if response.status_code == 429:
                # Rate limited. Honour Retry-After if present and reasonable.
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
            # 4xx other than 429 — permanent error, no retry.
            return None, (
                f"{source_label}: HTTP {response.status_code} from "
                f"{response.url.host}"
            )
        return None, last_error  # pragma: no cover — defensive

    async def get_package_info(
        self, package_name: str
    ) -> tuple[Optional[dict], Optional[str]]:
        """Get package metadata from PyPI.

        Returns ``(info_dict, error)`` — exactly one is non-None.
        """
        response, err = await self._fetch_with_retry(
            f"{self.PYPI_URL}/{package_name}/json",
            source_label="pypi.package_info",
        )
        if err:
            logger.error(err)
            return None, err
        try:
            return response.json(), None
        except ValueError as exc:
            err = f"pypi.package_info: malformed JSON ({exc})"
            logger.error(err)
            return None, err

    async def get_weekly_downloads(
        self, package_name: str
    ) -> tuple[Optional[int], Optional[str]]:
        """Get approximate weekly download count.

        Returns ``(count, error)`` — exactly one is non-None.
        ``count == 0`` means the package genuinely has zero downloads;
        ``count is None`` paired with a non-None ``error`` means the
        fetch failed.
        """
        response, err = await self._fetch_with_retry(
            f"{self.STATS_URL}/packages/{package_name}/recent",
            source_label="pypi.weekly_downloads",
        )
        if err:
            logger.warning(err)
            return None, err
        try:
            payload = response.json()
        except ValueError as exc:
            err = f"pypi.weekly_downloads: malformed JSON ({exc})"
            logger.error(err)
            return None, err
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or "last_month" not in data:
            err = "pypi.weekly_downloads: unexpected response schema"
            logger.error(err)
            return None, err
        try:
            monthly = int(data["last_month"])
        except (TypeError, ValueError) as exc:
            err = f"pypi.weekly_downloads: last_month not an int ({exc})"
            logger.error(err)
            return None, err
        return monthly // 4, None  # Approximate weekly

    def _clean_repo_url(self, url: str) -> str:
        """Strip trailing paths like /issues, /tree/..., /blob/... from repo URLs."""
        import re

        # Remove fragments and query strings
        url = url.split("#")[0].split("?")[0].rstrip("/")
        # Strip known subpaths to get the base repo URL
        url = re.sub(r"/(issues|pulls|tree|blob|wiki|releases|actions|discussions)(/.*)?$", "", url)
        return url

    def _extract_repo_url(self, info: dict) -> str:
        """Extract repository URL from package info."""
        project_urls = info.get("project_urls", {}) or {}

        # Build case-insensitive lookup
        urls_lower = {k.lower(): v for k, v in project_urls.items()}

        # Priority 1: explicit repo keys (case-insensitive)
        for key in ["repository", "source", "source code", "github", "code"]:
            if key in urls_lower:
                return self._clean_repo_url(urls_lower[key])

        # Priority 2: homepage if it points to a code host
        for key in ["homepage", "home"]:
            url = urls_lower.get(key, "")
            if url and ("github.com" in url or "gitlab.com" in url):
                return self._clean_repo_url(url)

        # Priority 3: scan all project_urls values for github/gitlab links
        for url in project_urls.values():
            if "github.com" in url or "gitlab.com" in url:
                return self._clean_repo_url(url)

        # Priority 4: legacy home_page field
        home_page = info.get("home_page", "") or ""
        if "github.com" in home_page or "gitlab.com" in home_page:
            return self._clean_repo_url(home_page)

        return ""

    async def collect(self, package_name: str) -> PyPIData:
        """
        Collect PyPI package data.

        Args:
            package_name: PyPI package name

        Returns:
            PyPIData with package information. Any upstream fetch
            failures appear in ``data.fetch_errors`` and propagate to
            ``CollectedData.fetch_errors`` so the engine can
            short-circuit to ``RiskLevel.INSUFFICIENT_DATA``.
        """
        data = PyPIData(name=package_name)

        # Get package metadata
        pkg_info, info_err = await self.get_package_info(package_name)
        if info_err:
            data.fetch_errors.append(info_err)
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

        # Get download stats. None means failure (already in fetch_errors);
        # 0 means the package genuinely has no recent downloads.
        downloads, downloads_err = await self.get_weekly_downloads(package_name)
        if downloads_err:
            data.fetch_errors.append(downloads_err)
        data.weekly_downloads = downloads

        return data

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()
