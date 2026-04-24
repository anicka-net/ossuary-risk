"""Lightweight registry collectors for additional ecosystems.

Each collector fetches repository_url and weekly_downloads from its registry API.
All other scoring data (git history, GitHub info) comes from the shared pipeline.

Failure contract (matches PyPI/npm):
- ``weekly_downloads = None`` is reserved for *fetch failure*; ``0`` means
  the package genuinely has no downloads. Engine treats ``None`` as
  ``INSUFFICIENT_DATA`` because the visibility factor is the single
  largest protective bonus (−10 to −20) and without it the engine
  cannot tell popular packages from obscure ones — the missing factor
  defaults to 0 and the score silently misses the bonus a real
  high-traffic package would have received.
- ``fetch_errors`` is the list of single-line failure descriptions that
  the scorer aggregates into ``CollectedData.fetch_errors``.
- The Go ecosystem has no public download API, so ``weekly_downloads``
  is *always* ``0`` (real signal: zero) and the Go collector never
  populates ``fetch_errors`` from a "missing downloads" condition; only
  proxy.golang.org metadata failures count.
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

import httpx

from ossuary.collectors.base import BaseCollector

logger = logging.getLogger(__name__)


# Retry policy mirrors the PyPI/npm collectors.
_MAX_RETRIES = 2
_BACKOFF_RATE_LIMIT_DEFAULT = 5.0
_BACKOFF_RATE_LIMIT_MAX = 30.0
_BACKOFF_SERVER_ERROR = 1.5
_BACKOFF_TIMEOUT = 1.0


@dataclass
class RegistryData:
    """Unified data from any package registry.

    ``weekly_downloads = None`` signals fetch failure (see module
    docstring); ``0`` is a real measurement.

    ``homepage_url`` is an optional secondary URL used as a fallback
    when ``repository_url`` 404s downstream — most registries expose a
    separate "homepage" field that maintainers sometimes keep in sync
    when they let ``repository_url`` rot (or never updated a typo). The
    classic case is cargo's ``agg`` crate: ``repository`` field has a
    typo (``savge13``), ``homepage`` field has the correct URL
    (``savage13``). Currently only populated by ``CratesCollector``;
    other collectors can opt in.
    """

    name: str = ""
    version: str = ""
    description: str = ""
    repository_url: str = ""
    homepage_url: str = ""
    weekly_downloads: Optional[int] = None
    fetch_errors: list[str] = field(default_factory=list)


async def _fetch_with_retry(
    client: httpx.AsyncClient, url: str, *, source_label: str
) -> tuple[Optional[httpx.Response], Optional[str]]:
    """Shared smart-retry helper for the lightweight collectors.

    Same contract as the PyPI/npm equivalents: returns ``(response,
    error)`` with exactly one non-None.
    """
    last_error = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            response = await client.get(url)
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
        resp, err = await _fetch_with_retry(
            self.client, f"{self.API_URL}/crates/{package_name}",
            source_label="cargo.crate_info",
        )
        if err:
            data.fetch_errors.append(err)
            return data
        try:
            payload = resp.json()
        except ValueError as exc:
            data.fetch_errors.append(f"cargo.crate_info: malformed JSON ({exc})")
            return data
        crate = payload.get("crate", {}) if isinstance(payload, dict) else {}
        if not isinstance(crate, dict) or not crate:
            data.fetch_errors.append("cargo.crate_info: unexpected response schema")
            return data
        data.version = crate.get("newest_version", "")
        data.description = crate.get("description", "")
        data.repository_url = crate.get("repository", "") or ""
        # Homepage often points to the canonical repo when repository
        # is stale or has a typo. Only carry it as a fallback when it
        # looks like a code-host URL and differs from repository.
        homepage = crate.get("homepage", "") or ""
        if (
            homepage
            and homepage != data.repository_url
            and ("github.com" in homepage or "gitlab.com" in homepage)
        ):
            data.homepage_url = homepage
        recent = crate.get("recent_downloads")
        if recent is None:
            data.fetch_errors.append("cargo.recent_downloads: field missing")
            return data
        try:
            data.weekly_downloads = int(recent) // 13  # ~13 weeks in 90 days
        except (TypeError, ValueError) as exc:
            data.fetch_errors.append(
                f"cargo.recent_downloads: not an int ({exc})"
            )
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
        resp, err = await _fetch_with_retry(
            self.client, f"{self.API_URL}/gems/{package_name}.json",
            source_label="rubygems.gem_info",
        )
        if err:
            data.fetch_errors.append(err)
            return data
        try:
            gem = resp.json()
        except ValueError as exc:
            data.fetch_errors.append(f"rubygems.gem_info: malformed JSON ({exc})")
            return data
        if not isinstance(gem, dict):
            data.fetch_errors.append("rubygems.gem_info: unexpected response schema")
            return data
        data.version = gem.get("version", "")
        data.description = gem.get("info", "")
        # RubyGems uses source_code_uri or homepage_uri
        repo = (
            gem.get("source_code_uri", "")
            or gem.get("homepage_uri", "")
            or ""
        )
        # Clean version-specific paths (e.g. /tree/v8.1.2)
        data.repository_url = re.sub(r"/tree/.*$", "", repo)
        # ``downloads`` is the lifetime total; we approximate weekly by
        # assuming a 5-year lifetime (260 weeks). Coarse but the only
        # signal RubyGems exposes; better than dropping the visibility
        # bonus entirely. A genuine zero-download gem returns 0 and is
        # treated as a real measurement.
        total = gem.get("downloads")
        if total is None:
            data.fetch_errors.append("rubygems.downloads: field missing")
            return data
        try:
            data.weekly_downloads = int(total) // 260
        except (TypeError, ValueError) as exc:
            data.fetch_errors.append(
                f"rubygems.downloads: not an int ({exc})"
            )
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

    @staticmethod
    def _pick_latest_packagist_version(versions: dict) -> str:
        """Pick the newest Packagist version deterministically.

        Prefer stable releases with a parseable ``version_normalized`` and fall
        back to the highest normalized entry overall. JSON object order is not a
        reliable source of version recency.
        """
        candidates: list[tuple[tuple[int, ...], bool, str]] = []

        for meta in versions.values():
            if not isinstance(meta, dict):
                continue

            version = meta.get("version", "")
            normalized = str(meta.get("version_normalized", "") or "")
            if not version or not normalized:
                continue

            parts = re.match(r"^(\d+(?:\.\d+)*)", normalized)
            if not parts:
                continue

            numeric = tuple(int(p) for p in parts.group(1).split("."))
            is_stable = "dev" not in normalized.lower()
            candidates.append((numeric, is_stable, version))

        if not candidates:
            for meta in versions.values():
                if isinstance(meta, dict) and meta.get("version"):
                    return meta["version"]
            return ""

        stable_candidates = [c for c in candidates if c[1]]
        ranked = stable_candidates or candidates
        ranked.sort(key=lambda item: item[0], reverse=True)
        return ranked[0][2]

    async def collect(self, package_name: str) -> RegistryData:
        """Collect data. package_name should be vendor/package format."""
        data = RegistryData(name=package_name)
        resp, err = await _fetch_with_retry(
            self.client, f"{self.API_URL}/packages/{package_name}.json",
            source_label="packagist.package_info",
        )
        if err:
            data.fetch_errors.append(err)
            return data
        try:
            payload = resp.json()
        except ValueError as exc:
            data.fetch_errors.append(
                f"packagist.package_info: malformed JSON ({exc})"
            )
            return data
        pkg = payload.get("package", {}) if isinstance(payload, dict) else {}
        if not isinstance(pkg, dict) or not pkg:
            data.fetch_errors.append(
                "packagist.package_info: unexpected response schema"
            )
            return data
        data.description = pkg.get("description", "")
        data.repository_url = pkg.get("repository", "") or ""
        # Clean up git:// URLs
        if data.repository_url.startswith("git://"):
            data.repository_url = data.repository_url.replace("git://", "https://")

        downloads = pkg.get("downloads")
        if not isinstance(downloads, dict) or "daily" not in downloads:
            data.fetch_errors.append("packagist.downloads: field missing")
        else:
            try:
                data.weekly_downloads = int(downloads.get("daily", 0)) * 7
            except (TypeError, ValueError) as exc:
                data.fetch_errors.append(
                    f"packagist.downloads: not an int ({exc})"
                )

        # Get latest version
        versions = pkg.get("versions", {})
        if versions:
            data.version = self._pick_latest_packagist_version(versions)
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
        # Use search API for metadata
        resp, err = await _fetch_with_retry(
            self.client,
            f"{self.SEARCH_URL}?q=packageid:{package_name}&take=1",
            source_label="nuget.search",
        )
        if err:
            data.fetch_errors.append(err)
            return data
        try:
            payload = resp.json()
        except ValueError as exc:
            data.fetch_errors.append(f"nuget.search: malformed JSON ({exc})")
            return data
        results = payload.get("data", []) if isinstance(payload, dict) else []
        if not results:
            data.fetch_errors.append(
                f"nuget.search: package '{package_name}' not found"
            )
            return data
        pkg = results[0]
        data.version = pkg.get("version", "")
        data.description = pkg.get("description", "")
        total_downloads = pkg.get("totalDownloads")
        if total_downloads is None:
            data.fetch_errors.append("nuget.totalDownloads: field missing")
        else:
            try:
                data.weekly_downloads = int(total_downloads) // 260
            except (TypeError, ValueError) as exc:
                data.fetch_errors.append(
                    f"nuget.totalDownloads: not an int ({exc})"
                )

        # Try to find repo URL from projectUrl or registration data
        project_url = pkg.get("projectUrl", "") or ""
        if "github.com" in project_url or "gitlab.com" in project_url:
            data.repository_url = project_url

        # Also check package metadata for source repo (best-effort, not
        # contractually required — failure here doesn't affect scoring)
        if not data.repository_url:
            reg_url = (
                f"{self.API_URL}/registration5-gz-semver2/"
                f"{package_name.lower()}/index.json"
            )
            reg_resp, _ = await _fetch_with_retry(
                self.client, reg_url, source_label="nuget.registration",
            )
            if reg_resp is not None:
                try:
                    pages = reg_resp.json().get("items", [])
                except ValueError:
                    pages = []
                if pages:
                    items = pages[-1].get("items", []) if isinstance(pages[-1], dict) else []
                    if items:
                        catalog = items[-1].get("catalogEntry", {}) if isinstance(items[-1], dict) else {}
                        repo_url = catalog.get("projectUrl", "") or ""
                        if "github.com" in repo_url or "gitlab.com" in repo_url:
                            data.repository_url = repo_url
        return data

    async def close(self):
        await self.client.aclose()


class GoProxyCollector(BaseCollector):
    """Collector for Go modules via proxy.golang.org.

    Go has no public download API; ``weekly_downloads`` is always set to
    ``0`` (a real measurement of "we have no signal" — visibility
    falls back to GitHub stars in the engine). The proxy.golang.org
    metadata fetch is the only thing that can fail.
    """

    PROXY_URL = "https://proxy.golang.org"
    PKG_URL = "https://pkg.go.dev"

    def __init__(self):
        self.client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)

    def is_available(self) -> bool:
        return True

    async def collect(self, package_name: str) -> RegistryData:
        """Collect data. package_name is the Go module path (e.g. github.com/gin-gonic/gin)."""
        # Set weekly_downloads to 0 up front: Go has no download API,
        # which is a structural absence, not a fetch failure.
        data = RegistryData(name=package_name, weekly_downloads=0)

        # For Go modules, the module path often IS the repo URL
        if package_name.startswith("github.com/"):
            data.repository_url = f"https://{package_name}"
        elif package_name.startswith("golang.org/x/"):
            # Standard library extensions are on GitHub
            name = package_name.split("/")[-1]
            data.repository_url = f"https://github.com/golang/{name}"

        resp, err = await _fetch_with_retry(
            self.client, f"{self.PROXY_URL}/{package_name}/@latest",
            source_label="go.proxy_latest",
        )
        if err:
            # Repository URL was already inferred from module path above
            # for github.com/* and golang.org/x/*, and Go has no download
            # signal regardless. The proxy fetch only supplies the
            # version *display string*, which scoring doesn't consume,
            # so a failure here is non-essential — log only, don't
            # propagate to fetch_errors and don't trigger INSUFFICIENT_DATA.
            logger.warning(err)
            return data
        try:
            info = resp.json()
        except ValueError as exc:
            logger.warning("go.proxy_latest: malformed JSON (%s)", exc)
            return data
        data.version = info.get("Version", "").lstrip("v")
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
