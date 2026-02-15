#!/usr/bin/env python3
"""Discover GitHub repositories for openSUSE/OBS packages.

Scans an OBS project via the `osc` CLI tool and extracts GitHub upstream
URLs from _service files and spec files.

Usage:
    python scripts/discover_suse.py                          # Factory (default)
    python scripts/discover_suse.py --project openSUSE:Leap:16.0
    python scripts/discover_suse.py --delay 0.5              # be extra gentle
    python scripts/discover_suse.py --resume                 # skip already-found

Common OBS projects:
    openSUSE:Factory           ~18K packages (Tumbleweed)
    openSUSE:Leap:16.0         ~113 packages (Leap 16 overlay)
    openSUSE:Leap:15.6         ~211 packages (Leap 15.6 overlay)

Output format (JSON list):
    [
      {"obs_package": "podman", "obs_project": "openSUSE:Factory",
       "github_owner": "containers", "github_repo": "podman",
       "repo_url": "https://github.com/containers/podman", "source": "service"},
      ...
    ]
"""

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass

try:
    from rich.console import Console
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
        TimeElapsedColumn,
        TimeRemainingColumn,
    )
except ImportError:
    print("rich is required: pip install rich")
    sys.exit(1)

console = Console()

# Regex patterns for GitHub URLs
GITHUB_URL_RE = re.compile(
    r"https?://github\.com/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)"
)

# Patterns to skip — meta-packages, patterns, etc.
SKIP_PREFIXES = (
    "000product",
    "000release-packages",
    "_",
    "skelcd-",
    "installation-images",
    "patterns-",
)

DEFAULT_PROJECT = "openSUSE:Factory"


class RateLimiter:
    """Thread-safe rate limiter for OBS API calls.

    Ensures a minimum delay between consecutive osc calls across all
    worker threads to avoid hammering the OBS server.
    """

    def __init__(self, min_delay: float = 0.1):
        self._min_delay = min_delay
        self._lock = threading.Lock()
        self._last_call = 0.0

    def wait(self):
        """Block until it's safe to make the next call."""
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_call
            if elapsed < self._min_delay:
                time.sleep(self._min_delay - elapsed)
            self._last_call = time.monotonic()


# Global rate limiter — set in main()
_rate_limiter: RateLimiter | None = None


@dataclass
class DiscoveredPackage:
    """A discovered package with its GitHub repo."""

    obs_package: str
    obs_project: str
    github_owner: str
    github_repo: str
    repo_url: str
    source: str  # "service" or "spec"


def run_osc(args: list[str], timeout: int = 30) -> str | None:
    """Run an osc command and return stdout, or None on failure.

    Respects the global rate limiter to avoid overloading OBS.
    """
    if _rate_limiter:
        _rate_limiter.wait()

    try:
        result = subprocess.run(
            ["osc"] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            return result.stdout
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def list_packages(project: str) -> list[str]:
    """List all packages in an OBS project."""
    console.print(f"[bold]Listing {project} packages...[/bold]")
    output = run_osc(["ls", project], timeout=120)
    if output is None:
        console.print(f"[red]Failed to list packages in {project}. Is osc configured?[/red]")
        sys.exit(1)

    packages = [
        p.strip()
        for p in output.strip().split("\n")
        if p.strip() and not p.strip().startswith(SKIP_PREFIXES)
    ]
    console.print(f"  Found {len(packages)} packages")
    return packages


def clean_github_url(url: str) -> tuple[str, str] | None:
    """Extract (owner, repo) from a GitHub URL, cleaning up suffixes."""
    match = GITHUB_URL_RE.search(url)
    if not match:
        return None

    owner = match.group(1)
    repo = match.group(2)

    # Clean up common suffixes
    repo = repo.removesuffix(".git")
    repo = repo.removesuffix("/")

    # Skip if owner or repo look invalid
    if owner in ("topics", "features", "explore", "settings", "orgs"):
        return None
    if not repo or repo in ("issues", "pulls", "wiki"):
        return None

    return owner, repo


def try_service_file(project: str, pkg_name: str) -> DiscoveredPackage | None:
    """Try to extract GitHub URL from a package's _service file."""
    output = run_osc(
        ["cat", f"{project}/{pkg_name}/_service"],
        timeout=15,
    )
    if output is None:
        return None

    result = clean_github_url(output)
    if result is None:
        return None

    owner, repo = result
    return DiscoveredPackage(
        obs_package=pkg_name,
        obs_project=project,
        github_owner=owner,
        github_repo=repo,
        repo_url=f"https://github.com/{owner}/{repo}",
        source="service",
    )


def try_spec_file(project: str, pkg_name: str) -> DiscoveredPackage | None:
    """Try to extract GitHub URL from a package's .spec file."""
    # First, list the files to find the spec file name
    output = run_osc(
        ["ls", f"{project}/{pkg_name}"],
        timeout=15,
    )
    if output is None:
        return None

    spec_file = None
    for line in output.strip().split("\n"):
        fname = line.strip()
        if fname.endswith(".spec"):
            spec_file = fname
            break

    if not spec_file:
        return None

    # Read the spec file
    output = run_osc(
        ["cat", f"{project}/{pkg_name}/{spec_file}"],
        timeout=15,
    )
    if output is None:
        return None

    # Look for GitHub URLs in URL: and Source: fields
    for line in output.split("\n"):
        stripped = line.strip()
        if stripped.lower().startswith(("url:", "source:", "source0:")):
            result = clean_github_url(stripped)
            if result:
                owner, repo = result
                return DiscoveredPackage(
                    obs_package=pkg_name,
                    obs_project=project,
                    github_owner=owner,
                    github_repo=repo,
                    repo_url=f"https://github.com/{owner}/{repo}",
                    source="spec",
                )

    return None


def discover_package(project: str, pkg_name: str) -> DiscoveredPackage | None:
    """Try to discover a GitHub repo for a single package."""
    # Try _service file first (more reliable)
    result = try_service_file(project, pkg_name)
    if result:
        return result

    # Fall back to spec file
    return try_spec_file(project, pkg_name)


def load_existing(output_path: str) -> set[str]:
    """Load already-discovered package names from output file."""
    if not os.path.exists(output_path):
        return set()

    with open(output_path) as f:
        data = json.load(f)

    return {item["obs_package"] for item in data}


def save_results(results: list[DiscoveredPackage], output_path: str):
    """Save results to JSON file."""
    data = [asdict(r) for r in results]
    # Sort by obs_package name for reproducibility
    data.sort(key=lambda x: x["obs_package"])

    with open(output_path, "w") as f:
        json.dump(data, f, indent=2)


def main():
    parser = argparse.ArgumentParser(
        description="Discover GitHub repos for openSUSE/OBS packages"
    )
    parser.add_argument(
        "--project",
        "-p",
        default=DEFAULT_PROJECT,
        help=f"OBS project to scan (default: {DEFAULT_PROJECT})",
    )
    parser.add_argument(
        "--output",
        "-o",
        default="suse_packages.json",
        help="Output JSON file (default: suse_packages.json)",
    )
    parser.add_argument(
        "--workers",
        "-w",
        type=int,
        default=5,
        help="Number of parallel osc workers (default: 5)",
    )
    parser.add_argument(
        "--delay",
        "-d",
        type=float,
        default=0.1,
        help="Minimum seconds between OBS API calls (default: 0.1)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip packages already in the output file",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Only process first N packages (0=all)",
    )
    parser.add_argument(
        "--list-projects",
        action="store_true",
        help="List common OBS projects and exit",
    )
    args = parser.parse_args()

    if args.list_projects:
        console.print("[bold]Common OBS projects:[/bold]")
        console.print("  openSUSE:Factory           Tumbleweed (~18K packages)")
        console.print("  openSUSE:Leap:16.0         Leap 16 overlay (~113)")
        console.print("  openSUSE:Leap:15.6         Leap 15.6 overlay (~211)")
        console.print("\nUse --project <name> to scan any OBS project.")
        console.print("Run 'osc ls' to see all available projects.")
        return

    # Set up rate limiter
    global _rate_limiter
    _rate_limiter = RateLimiter(min_delay=args.delay)

    project = args.project

    console.print(f"[bold]OBS project:[/bold] {project}")
    console.print(f"[bold]Rate limit:[/bold] {args.delay}s between calls, {args.workers} workers")
    # Estimate effective rate
    effective_rps = min(args.workers, 1.0 / args.delay) if args.delay > 0 else args.workers
    console.print(f"[bold]Effective rate:[/bold] ~{effective_rps:.0f} requests/second (max)\n")

    # List all packages
    all_packages = list_packages(project)

    # Resume support: load existing results
    existing = set()
    existing_results = []
    if args.resume and os.path.exists(args.output):
        existing = load_existing(args.output)
        with open(args.output) as f:
            existing_results = json.load(f)
        console.print(f"  Resuming: {len(existing)} packages already discovered")

    # Filter to packages we haven't processed
    to_process = [p for p in all_packages if p not in existing]

    if args.limit > 0:
        to_process = to_process[: args.limit]

    if not to_process:
        console.print("[green]Nothing to process.[/green]")
        return

    # Estimate time
    # Each package needs 1-3 osc calls (service file, maybe ls + spec)
    # With rate limiting, each call takes at least args.delay seconds
    avg_calls_per_pkg = 2.0
    est_seconds = (len(to_process) * avg_calls_per_pkg * args.delay) / args.workers
    est_minutes = est_seconds / 60
    if est_minutes > 1:
        console.print(f"  Estimated time: ~{est_minutes:.0f} minutes")

    console.print(f"  Processing {len(to_process)} packages with {args.workers} workers\n")

    # Parallel discovery
    discovered = [
        DiscoveredPackage(**item) for item in existing_results
    ]
    found = len(existing_results)
    errors = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Scanning...", total=len(to_process))

        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(discover_package, project, pkg): pkg
                for pkg in to_process
            }

            for future in as_completed(futures):
                pkg_name = futures[future]
                try:
                    result = future.result()
                    if result:
                        discovered.append(result)
                        found += 1
                except Exception:
                    errors += 1

                progress.advance(task)

                # Periodic save every 500 packages
                completed = progress.tasks[0].completed
                if completed % 500 == 0 and completed > 0:
                    save_results(discovered, args.output)

    # Final save
    save_results(discovered, args.output)

    # Summary
    total_scanned = len(to_process) + len(existing)
    console.print(f"\n[bold green]Done![/bold green]")
    console.print(f"  Project: {project}")
    console.print(f"  Total packages scanned: {total_scanned}")
    console.print(f"  GitHub repos found: {found}")
    if total_scanned > 0:
        console.print(f"  Hit rate: {found / total_scanned * 100:.1f}%")
    if errors:
        console.print(f"  Errors: {errors}")
    console.print(f"  Output: {args.output}")

    # Source breakdown
    service_count = sum(1 for d in discovered if d.source == "service")
    spec_count = sum(1 for d in discovered if d.source == "spec")
    console.print(f"  From _service files: {service_count}")
    console.print(f"  From .spec files: {spec_count}")


if __name__ == "__main__":
    main()
