#!/usr/bin/env python3
"""Compare Ossuary scores with OpenSSF Scorecard scores.

Uses the public Scorecard API (api.securityscorecards.dev) to fetch
scores for all packages in our validation set, then compares with
Ossuary's governance-based risk scores.

Usage:
    python scripts/scorecard_compare.py [-o comparison.json]
"""

import argparse
import asyncio
import json
import os
import sys
import time
from statistics import mean, stdev

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

SCORECARD_API = "https://api.securityscorecards.dev/projects/github.com"

# Registry APIs for resolving package -> GitHub repo
NPM_REGISTRY = "https://registry.npmjs.org"
PYPI_API = "https://pypi.org/pypi"


async def resolve_npm_repo(client: httpx.AsyncClient, name: str) -> str | None:
    """Resolve npm package to GitHub owner/repo."""
    try:
        resp = await client.get(f"{NPM_REGISTRY}/{name}", timeout=15)
        if resp.status_code != 200:
            return None
        data = resp.json()
        repo = data.get("repository", {})
        url = repo.get("url", "") if isinstance(repo, dict) else ""
        # Parse github.com/owner/repo from URL
        import re
        m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url)
        return m.group(1) if m else None
    except Exception:
        return None


async def resolve_pypi_repo(client: httpx.AsyncClient, name: str) -> str | None:
    """Resolve PyPI package to GitHub owner/repo."""
    try:
        resp = await client.get(f"{PYPI_API}/{name}/json", timeout=15)
        if resp.status_code != 200:
            return None
        data = resp.json()
        info = data.get("info", {})
        urls = info.get("project_urls", {}) or {}
        # Check various URL fields
        import re
        for key in ["Source", "Repository", "Source Code", "Homepage", "Code"]:
            url = urls.get(key, "")
            m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url)
            if m:
                return m.group(1)
        # Fallback to home_page
        url = info.get("home_page", "")
        m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url)
        return m.group(1) if m else None
    except Exception:
        return None


async def resolve_rubygems_repo(client: httpx.AsyncClient, name: str) -> str | None:
    """Resolve RubyGems package to GitHub owner/repo."""
    try:
        resp = await client.get(f"https://rubygems.org/api/v1/gems/{name}.json", timeout=15)
        if resp.status_code != 200:
            return None
        data = resp.json()
        import re
        for field in ["source_code_uri", "homepage_uri"]:
            url = data.get(field, "")
            m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url)
            if m:
                return m.group(1)
        return None
    except Exception:
        return None


async def resolve_cargo_repo(client: httpx.AsyncClient, name: str) -> str | None:
    """Resolve Cargo crate to GitHub owner/repo."""
    try:
        resp = await client.get(f"https://crates.io/api/v1/crates/{name}", timeout=15)
        if resp.status_code != 200:
            return None
        data = resp.json()
        crate = data.get("crate", {})
        import re
        url = crate.get("repository", "")
        m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url)
        return m.group(1) if m else None
    except Exception:
        return None


async def resolve_packagist_repo(client: httpx.AsyncClient, name: str) -> str | None:
    """Resolve Packagist package to GitHub owner/repo."""
    try:
        resp = await client.get(f"https://repo.packagist.org/p2/{name}.json", timeout=15)
        if resp.status_code != 200:
            return None
        data = resp.json()
        packages = data.get("packages", {}).get(name, [])
        if not packages:
            return None
        import re
        url = packages[0].get("source", {}).get("url", "")
        m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url)
        return m.group(1) if m else None
    except Exception:
        return None


async def resolve_nuget_repo(client: httpx.AsyncClient, name: str) -> str | None:
    """Resolve NuGet package to GitHub owner/repo."""
    try:
        resp = await client.get(
            f"https://api.nuget.org/v3/registration5-gz-semver2/{name.lower()}/index.json",
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        items = data.get("items", [])
        if not items:
            return None
        # Get latest catalog entry
        pages = items[-1].get("items", [])
        if not pages:
            return None
        entry = pages[-1].get("catalogEntry", {})
        import re
        url = entry.get("projectUrl", "")
        m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url)
        return m.group(1) if m else None
    except Exception:
        return None


async def resolve_go_repo(client: httpx.AsyncClient, name: str) -> str | None:
    """Resolve Go module to GitHub owner/repo."""
    import re
    m = re.match(r"github\.com/([^/]+/[^/]+)", name)
    return m.group(1) if m else None


RESOLVERS = {
    "npm": resolve_npm_repo,
    "pypi": resolve_pypi_repo,
    "rubygems": resolve_rubygems_repo,
    "cargo": resolve_cargo_repo,
    "packagist": resolve_packagist_repo,
    "nuget": resolve_nuget_repo,
    "go": resolve_go_repo,
}


async def get_scorecard(client: httpx.AsyncClient, owner_repo: str) -> dict | None:
    """Fetch Scorecard data from public API."""
    try:
        resp = await client.get(f"{SCORECARD_API}/{owner_repo}", timeout=30)
        if resp.status_code != 200:
            return None
        data = resp.json()
        checks = {}
        for check in data.get("checks", []):
            checks[check["name"]] = check["score"]
        return {
            "score": data.get("score"),
            "checks": checks,
        }
    except Exception:
        return None


async def main(results_file: str, output_file: str | None):
    with open(results_file) as f:
        validation = json.load(f)

    results = validation["results"]
    print(f"Processing {len(results)} packages...\n")

    comparison = []

    async with httpx.AsyncClient(follow_redirects=True) as client:
        for i, r in enumerate(results):
            case = r["case"]
            name = case["name"]
            eco = case["ecosystem"]
            ossuary_score = r.get("score")
            expected = case["expected_outcome"]
            attack_type = case.get("attack_type", "")

            # Resolve to GitHub owner/repo
            owner_repo = None
            repo_url = case.get("repo_url")

            if repo_url:
                import re
                m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", repo_url)
                if m:
                    owner_repo = m.group(1)
            elif eco == "github":
                owner_repo = name
            else:
                resolver = RESOLVERS.get(eco)
                if resolver:
                    owner_repo = await resolver(client, name)

            if not owner_repo:
                print(f"[{i+1}/{len(results)}] {name} ({eco})... no GitHub repo found")
                comparison.append({
                    "name": name,
                    "ecosystem": eco,
                    "expected": expected,
                    "attack_type": attack_type,
                    "ossuary_score": ossuary_score,
                    "ossuary_level": r.get("risk_level", ""),
                    "github_repo": None,
                    "scorecard_score": None,
                    "scorecard_checks": {},
                    "error": "no GitHub repo found",
                })
                continue

            # Fetch Scorecard
            sc = await get_scorecard(client, owner_repo)
            sc_score = sc["score"] if sc else None
            sc_checks = sc["checks"] if sc else {}

            status = f"{sc_score:.1f}" if sc_score is not None else "N/A"
            print(f"[{i+1}/{len(results)}] {name} ({eco}) — {owner_repo} — "
                  f"Ossuary: {ossuary_score}, Scorecard: {status}")

            comparison.append({
                "name": name,
                "ecosystem": eco,
                "expected": expected,
                "attack_type": attack_type,
                "ossuary_score": ossuary_score,
                "ossuary_level": r.get("risk_level", ""),
                "github_repo": owner_repo,
                "scorecard_score": sc_score,
                "scorecard_checks": sc_checks,
            })

            # Small delay to be polite to the API
            time.sleep(0.1)

    # --- Analysis ---
    print("\n" + "=" * 80)
    print("COMPARISON ANALYSIS")
    print("=" * 80)

    # Filter to packages with both scores
    both = [c for c in comparison if c["ossuary_score"] is not None and c["scorecard_score"] is not None]
    print(f"\nPackages with both scores: {len(both)}/{len(comparison)}")

    if not both:
        print("No packages with both scores — cannot analyze.")
        return

    ossuary_scores = [c["ossuary_score"] for c in both]
    scorecard_scores = [c["scorecard_score"] for c in both]

    # Correlation
    n = len(both)
    mean_o = mean(ossuary_scores)
    mean_s = mean(scorecard_scores)
    std_o = stdev(ossuary_scores) if n > 1 else 0
    std_s = stdev(scorecard_scores) if n > 1 else 0

    if std_o > 0 and std_s > 0:
        cov = sum((o - mean_o) * (s - mean_s) for o, s in zip(ossuary_scores, scorecard_scores)) / (n - 1)
        pearson = cov / (std_o * std_s)
    else:
        pearson = 0

    # Spearman rank correlation
    def rank(values):
        sorted_idx = sorted(range(len(values)), key=lambda i: values[i])
        ranks = [0.0] * len(values)
        for rank_val, idx in enumerate(sorted_idx):
            ranks[idx] = rank_val + 1
        return ranks

    rank_o = rank(ossuary_scores)
    rank_s = rank(scorecard_scores)
    mean_ro = mean(rank_o)
    mean_rs = mean(rank_s)
    std_ro = stdev(rank_o) if n > 1 else 0
    std_rs = stdev(rank_s) if n > 1 else 0

    if std_ro > 0 and std_rs > 0:
        cov_r = sum((ro - mean_ro) * (rs - mean_rs) for ro, rs in zip(rank_o, rank_s)) / (n - 1)
        spearman = cov_r / (std_ro * std_rs)
    else:
        spearman = 0

    print(f"\nPearson correlation:  {pearson:.3f}")
    print(f"Spearman correlation: {spearman:.3f}")

    # Quadrant analysis
    # Ossuary: HIGH risk = score >= 60, LOW risk = score < 60
    # Scorecard: HIGH security = score >= 5.0, LOW security = score < 5.0
    q_hh = []  # High Ossuary risk, High Scorecard (good security practices but governance risk)
    q_hl = []  # High Ossuary risk, Low Scorecard (both flag problems)
    q_lh = []  # Low Ossuary risk, High Scorecard (both say healthy)
    q_ll = []  # Low Ossuary risk, Low Scorecard (poor practices but OK governance)

    for c in both:
        o_high = c["ossuary_score"] >= 60
        s_high = c["scorecard_score"] >= 5.0
        if o_high and s_high:
            q_hh.append(c)
        elif o_high and not s_high:
            q_hl.append(c)
        elif not o_high and s_high:
            q_lh.append(c)
        else:
            q_ll.append(c)

    print(f"\n{'Quadrant Analysis':=^60}")
    print(f"(Ossuary threshold: 60, Scorecard threshold: 5.0/10)")
    print()
    print(f"                        Scorecard >= 5.0    Scorecard < 5.0")
    print(f"  Ossuary >= 60         {len(q_hh):>6}              {len(q_hl):>6}")
    print(f"  Ossuary < 60          {len(q_lh):>6}              {len(q_ll):>6}")

    print(f"\n--- High Ossuary + High Scorecard ({len(q_hh)}) ---")
    print("  Good security practices but governance risk")
    for c in sorted(q_hh, key=lambda x: -x["ossuary_score"]):
        print(f"  {c['name']:30s}  Ossuary: {c['ossuary_score']:3d}  Scorecard: {c['scorecard_score']:.1f}  [{c['expected']}]")

    print(f"\n--- High Ossuary + Low Scorecard ({len(q_hl)}) ---")
    print("  Both tools flag problems")
    for c in sorted(q_hl, key=lambda x: -x["ossuary_score"]):
        print(f"  {c['name']:30s}  Ossuary: {c['ossuary_score']:3d}  Scorecard: {c['scorecard_score']:.1f}  [{c['expected']}]")

    print(f"\n--- Low Ossuary + Low Scorecard ({len(q_ll)}) ---")
    print("  Poor practices but OK governance")
    for c in sorted(q_ll, key=lambda x: x["scorecard_score"]):
        print(f"  {c['name']:30s}  Ossuary: {c['ossuary_score']:3d}  Scorecard: {c['scorecard_score']:.1f}  [{c['expected']}]")

    # Incident detection comparison
    print(f"\n{'Incident Detection':=^60}")
    incidents = [c for c in both if c["expected"] == "incident"]
    if incidents:
        ossuary_detected = sum(1 for c in incidents if c["ossuary_score"] >= 60)
        scorecard_low = sum(1 for c in incidents if c["scorecard_score"] < 5.0)
        print(f"  Incidents with both scores: {len(incidents)}")
        print(f"  Ossuary flagged (>=60):     {ossuary_detected} ({ossuary_detected/len(incidents)*100:.0f}%)")
        print(f"  Scorecard low (<5.0):       {scorecard_low} ({scorecard_low/len(incidents)*100:.0f}%)")

        print(f"\n  Per-incident comparison:")
        for c in sorted(incidents, key=lambda x: -x["ossuary_score"]):
            o_flag = "RISKY" if c["ossuary_score"] >= 60 else "safe"
            s_flag = "LOW" if c["scorecard_score"] < 5.0 else "OK"
            print(f"    {c['name']:30s}  Ossuary: {c['ossuary_score']:3d} ({o_flag:5s})  "
                  f"Scorecard: {c['scorecard_score']:.1f} ({s_flag})  "
                  f"[{c.get('attack_type', '')}]")

    # Save results
    output = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "packages_total": len(comparison),
        "packages_with_both_scores": len(both),
        "pearson_correlation": round(pearson, 4),
        "spearman_correlation": round(spearman, 4),
        "quadrants": {
            "high_ossuary_high_scorecard": len(q_hh),
            "high_ossuary_low_scorecard": len(q_hl),
            "low_ossuary_high_scorecard": len(q_lh),
            "low_ossuary_low_scorecard": len(q_ll),
        },
        "results": comparison,
    }

    out_path = output_file or "scorecard_comparison.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nResults saved to {out_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Compare Ossuary with OpenSSF Scorecard")
    parser.add_argument("results", nargs="?", default="validation_results_v4.json",
                        help="Validation results JSON file")
    parser.add_argument("-o", "--output", help="Output comparison JSON file")
    args = parser.parse_args()

    asyncio.run(main(args.results, args.output))
