#!/usr/bin/env python3
"""Benchmark snapshot-cache cold-vs-warm timing.

Wipes any existing snapshots for a small set of packages, measures the
cold-fetch time (full upstream collect + write snapshot), then measures
the warm-cache time (deserialise snapshot from DB), and reports the
ratio. Numbers feed the thesis operational-scalability section and
``docs/data_reuse_design.md``.

Usage:
    python scripts/cache_benchmark.py
    python scripts/cache_benchmark.py --packages axios@npm requests@pypi
    python scripts/cache_benchmark.py --output thesis/cache_benchmark.md

The default set is a small mix across ecosystems chosen to give a
representative spread without burning rate limit. For SUSE-pipeline
projection numbers, pass a larger ``--packages`` list.

The benchmark deletes only the targeted packages' snapshots from the
local DB; everything else in the cache is untouched.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO / ".env")

from ossuary.db.models import Package, RepoSnapshot  # noqa: E402
from ossuary.db.session import init_db, session_scope  # noqa: E402
from ossuary.services.scorer import cached_collect  # noqa: E402


# Default benchmark set: a mix across ecosystems and project sizes.
# Kept small so the cold pass doesn't burn the user's rate limit.
DEFAULT_BENCHMARK = [
    ("is-promise", "npm"),     # tiny, mostly idle — best case for cold
    ("axios", "npm"),          # popular npm
    ("requests", "pypi"),      # popular pypi
    ("flask", "pypi"),         # medium
    ("rayon", "cargo"),        # cargo
]


def wipe_package_cache(name: str, ecosystem: str) -> int:
    """Delete cached snapshots for this package; return number deleted.

    Targets only the named package — the rest of the cache is untouched.
    Used to force a cold-path measurement for the next ``cached_collect``.
    """
    with session_scope() as session:
        pkg = (
            session.query(Package)
            .filter(Package.name == name, Package.ecosystem == ecosystem)
            .first()
        )
        if pkg is None:
            return 0
        deleted = (
            session.query(RepoSnapshot)
            .filter(RepoSnapshot.package_id == pkg.id)
            .delete()
        )
        return deleted


async def time_collect(name: str, ecosystem: str) -> tuple[float, bool, str]:
    """Time a single ``cached_collect`` call. Returns (seconds, ok, note)."""
    t0 = time.perf_counter()
    data, warnings = await cached_collect(name, ecosystem)
    elapsed = time.perf_counter() - t0
    note = "" if data is not None else (warnings[0][:60] if warnings else "no data")
    return elapsed, data is not None, note


def _environment_metadata() -> dict:
    """Capture run metadata for thesis reproducibility.

    GPT review (cache phase 2 pass 3) flagged that operational-scalability
    claims are easier to defend academically when the underlying run is
    pinned to: which DB backend (SQLite vs Postgres has very different
    write costs), whether a GitHub token was present (anonymous limits
    are 60/h vs 5000/h), and the methodology / collector version.
    """
    import os
    import platform
    import sys as _sys

    from ossuary.db.session import DATABASE_URL
    from ossuary.scoring import METHODOLOGY_VERSION
    from ossuary.services.repo_cache import COLLECTOR_VERSION

    backend = DATABASE_URL.split("://", 1)[0] if "://" in DATABASE_URL else DATABASE_URL
    has_gh_token = bool(os.getenv("GITHUB_TOKEN"))
    has_pypi_token = bool(os.getenv("PYPI_TOKEN"))

    return {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z") or time.strftime("%Y-%m-%dT%H:%M:%S"),
        "db_backend": backend,
        "github_token_present": has_gh_token,
        "pypi_token_present": has_pypi_token,
        "methodology_version": METHODOLOGY_VERSION,
        "collector_version": COLLECTOR_VERSION,
        "python_version": _sys.version.split()[0],
        "platform": platform.platform(),
    }


async def benchmark(packages: list[tuple[str, str]], out) -> int:
    """Run the benchmark over ``packages``; write markdown to ``out``.

    Returns the number of successful (cold and warm both succeeded) rows.
    """
    init_db()
    meta = _environment_metadata()
    out.write(f"# Snapshot cache benchmark — n={len(packages)} packages\n\n")
    out.write(f"Generated: {meta['timestamp']}\n\n")
    out.write("## Environment\n\n")
    out.write(f"| Field | Value |\n|---|---|\n")
    for k in (
        "db_backend", "github_token_present", "pypi_token_present",
        "methodology_version", "collector_version",
        "python_version", "platform",
    ):
        out.write(f"| `{k}` | {meta[k]} |\n")
    out.write("\n")
    out.write("## Per-package timings\n\n")
    out.write(
        "Cold = full upstream collect + write snapshot.\n"
        "Warm = deserialise snapshot from DB (no upstream calls).\n"
        "Ratio = cold / warm.\n\n"
    )
    out.write("| Package | Ecosystem | Cold (s) | Warm (s) | Ratio |\n")
    out.write("|---|---|---:|---:|---:|\n")

    rows = []
    for name, ecosystem in packages:
        wipe_package_cache(name, ecosystem)
        cold_t, cold_ok, cold_note = await time_collect(name, ecosystem)
        if not cold_ok:
            out.write(
                f"| `{name}` | {ecosystem} | FAILED ({cold_note}) | — | — |\n"
            )
            continue
        warm_t, warm_ok, warm_note = await time_collect(name, ecosystem)
        if not warm_ok:
            out.write(
                f"| `{name}` | {ecosystem} | {cold_t:.2f} | FAILED ({warm_note}) | — |\n"
            )
            continue
        ratio = cold_t / warm_t if warm_t > 0 else float("inf")
        rows.append((name, ecosystem, cold_t, warm_t, ratio))
        out.write(
            f"| `{name}` | {ecosystem} | {cold_t:.2f} | {warm_t:.4f} | {ratio:,.0f}× |\n"
        )

    if rows:
        total_cold = sum(r[2] for r in rows)
        total_warm = sum(r[3] for r in rows)
        agg_ratio = total_cold / total_warm if total_warm > 0 else float("inf")
        out.write(
            f"| **Total / aggregate ratio** |  | **{total_cold:.2f}** | "
            f"**{total_warm:.4f}** | **{agg_ratio:,.0f}×** |\n"
        )
        out.write("\n")
        out.write(
            f"**Aggregate**: {len(rows)} successful packages. "
            f"Cold total {total_cold:.1f}s; warm total {total_warm:.4f}s; "
            f"warm path is **~{agg_ratio:,.0f}× faster** than cold.\n\n"
        )
        out.write(
            "**SUSE-scale projection** (linear extrapolation, ignores "
            "concurrency): a manufacturer's full dependency graph of N packages "
            f"costs roughly N × {total_cold/len(rows):.1f}s on first scoring "
            f"and N × {total_warm/len(rows):.4f}s on every subsequent re-scoring "
            "while snapshots remain Fresh (≤30 days, see methodology §4.-0). "
            "A 5,000-dependency manifest: ~"
            f"{(5000 * total_cold/len(rows))/3600:.1f}h cold vs ~"
            f"{(5000 * total_warm/len(rows))/60:.1f}min warm.\n"
        )

    return len(rows)


def parse_packages(raw: list[str]) -> list[tuple[str, str]]:
    out = []
    for spec in raw:
        if "@" not in spec:
            raise SystemExit(f"--packages entries must be name@ecosystem (got {spec!r})")
        name, eco = spec.rsplit("@", 1)
        out.append((name, eco))
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--packages",
        nargs="+",
        default=None,
        help="package@ecosystem pairs (default: small mixed set)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write markdown to this file (default: stdout)",
    )
    args = parser.parse_args()

    packages = parse_packages(args.packages) if args.packages else DEFAULT_BENCHMARK

    if args.output:
        with args.output.open("w") as f:
            n = asyncio.run(benchmark(packages, f))
        print(f"Wrote {n} successful rows to {args.output}", file=sys.stderr)
    else:
        n = asyncio.run(benchmark(packages, sys.stdout))
        if n == 0:
            sys.exit(1)


if __name__ == "__main__":
    main()
