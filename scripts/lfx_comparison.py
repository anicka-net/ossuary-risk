#!/usr/bin/env python3
"""LFX Insights vs Ossuary comparison benchmark.

Scores a fixed set of 20 packages on both LFX Health (0-100, high=healthy)
and Ossuary Risk (0-100, high=risky). Expected relationship: negative
correlation — LFX high should correspond to Ossuary low, and vice versa.

The package set is designed to span the full LFX Health spectrum (0–90)
and includes both healthy CNCF flagships and known-struggling/archived
projects. Re-run periodically to track scoring drift on both sides.

Usage:
    python scripts/lfx_comparison.py
    python scripts/lfx_comparison.py --json          # machine-readable output
    python scripts/lfx_comparison.py --output FILE   # save JSON to file

Reference results live in benchmarks/lfx_comparison_YYYY_MM_DD.json.
Baseline: 2026-05-15, methodology v6.4, Pearson r = -0.615 (n=19).
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime

import httpx

BENCHMARK_PACKAGES = [
    # (display_name, lfx_slug, ossuary_package, ecosystem)
    # --- LFX < 20: dead / archived ---
    ("OpenTracing", "opentracing", "opentracing/opentracing-go", "github"),
    ("SPIRE", "spire", "spiffe/spire", "github"),
    ("Brigade", "brigade", "brigadecore/brigade", "github"),
    # --- LFX 20–60: struggling / deprecated ---
    ("RKT", "rkt", "rkt/rkt", "github"),
    ("SPIFFE", "spiffe", "spiffe/spiffe", "github"),
    # --- LFX 60–75: moderate ---
    ("Zuul", "zuul", "zuul/zuul", "github"),
    ("Tremor", "tremor", "tremor-rs/tremor-runtime", "github"),
    ("Git", "git", "git/git", "github"),
    ("Django", "django", "django/django", "github"),
    # --- LFX 75–85: healthy ---
    ("Axios", "axios", "axios/axios", "github"),
    ("Linkerd", "linkerd", "linkerd/linkerd2", "github"),
    ("CoreDNS", "coredns", "coredns/coredns", "github"),
    ("Lodash", "lodash", "lodash/lodash", "github"),
    ("Curl", "curl", "curl/curl", "github"),
    ("FastAPI", "fastapi", "fastapi/fastapi", "github"),
    # --- LFX 85+: flagships ---
    ("React", "react", "facebook/react", "github"),
    ("Envoy", "envoy", "envoyproxy/envoy", "github"),
    ("TensorFlow", "tensorflow", "tensorflow/tensorflow", "github"),
    ("Kubernetes", "k8s", "kubernetes/kubernetes", "github"),
    ("OpenTelemetry", "opentelemetry", "open-telemetry/opentelemetry-go", "github"),
]


async def fetch_lfx_score(client: httpx.AsyncClient, slug: str) -> dict:
    try:
        r = await client.get(
            f"https://insights.linuxfoundation.org/api/project/{slug}",
            headers={"User-Agent": "Mozilla/5.0 (Ossuary LFX comparison)"},
        )
        if r.status_code != 200:
            return {"health_score": None, "error": f"HTTP {r.status_code}"}
        d = r.json()
        return {
            "health_score": d.get("healthScore"),
            "index_score": d.get("score"),
            "rank": d.get("rank"),
        }
    except Exception as e:
        return {"health_score": None, "error": str(e)}


async def fetch_ossuary_score(package: str, ecosystem: str) -> dict:
    from dotenv import load_dotenv
    load_dotenv()
    from ossuary.services.scorer import score_package

    try:
        r = await score_package(package, ecosystem)
        if r.breakdown and r.breakdown.final_score is not None:
            bd = r.breakdown
            return {
                "risk_score": bd.final_score,
                "risk_level": bd.risk_level.value,
                "bus_factor": bd.bus_factor,
                "concentration": round(bd.maintainer_concentration, 1),
                "commits_yr": bd.commits_last_year,
                "contributors": bd.unique_contributors,
            }
        return {"risk_score": None, "error": r.error or "INSUFFICIENT_DATA"}
    except Exception as e:
        return {"risk_score": None, "error": str(e)}


async def run_comparison():
    results = []
    async with httpx.AsyncClient(timeout=15.0) as client:
        for display, slug, ossuary_pkg, eco in BENCHMARK_PACKAGES:
            lfx = await fetch_lfx_score(client, slug)
            ossuary = await fetch_ossuary_score(ossuary_pkg, eco)
            results.append({
                "name": display,
                "lfx_slug": slug,
                "ossuary_package": ossuary_pkg,
                "lfx": lfx,
                "ossuary": ossuary,
            })
            await asyncio.sleep(0.3)
    return results


def print_table(results):
    print(f"\nLFX vs Ossuary comparison — {datetime.now().strftime('%Y-%m-%d')}")
    print(f"{'Package':<16} {'LFX Health':>10} {'Ossuary Risk':>12} {'Risk Level':<12} "
          f"{'BF':>3} {'Conc%':>6} {'Agree?':<8}")
    print("-" * 75)
    for r in sorted(results, key=lambda x: x["lfx"].get("health_score") or -1):
        lfx_hs = r["lfx"].get("health_score")
        oss_rs = r["ossuary"].get("risk_score")
        rl = r["ossuary"].get("risk_level", "?")
        bf = r["ossuary"].get("bus_factor", "?")
        conc = r["ossuary"].get("concentration", "?")

        lfx_str = str(lfx_hs) if lfx_hs is not None else "ERR"
        oss_str = str(oss_rs) if oss_rs is not None else "ERR"

        if lfx_hs is not None and oss_rs is not None:
            lfx_bad = lfx_hs < 60
            oss_bad = oss_rs >= 40
            if lfx_bad == oss_bad:
                agree = "YES"
            elif lfx_bad and not oss_bad:
                agree = "LFX-only"
            else:
                agree = "OSS-only"
        else:
            agree = "?"

        print(f"{r['name']:<16} {lfx_str:>10} {oss_str:>12} {rl:<12} "
              f"{bf!s:>3} {conc!s:>6} {agree:<8}")

    scored = [(r["lfx"]["health_score"], r["ossuary"]["risk_score"])
              for r in results
              if r["lfx"].get("health_score") is not None
              and r["ossuary"].get("risk_score") is not None]
    if len(scored) >= 3:
        lfx_vals = [s[0] for s in scored]
        oss_vals = [s[1] for s in scored]
        n = len(scored)
        mean_l = sum(lfx_vals) / n
        mean_o = sum(oss_vals) / n
        cov = sum((l - mean_l) * (o - mean_o) for l, o in scored) / n
        std_l = (sum((l - mean_l) ** 2 for l in lfx_vals) / n) ** 0.5
        std_o = (sum((o - mean_o) ** 2 for o in oss_vals) / n) ** 0.5
        corr = cov / (std_l * std_o) if std_l > 0 and std_o > 0 else 0
        print(f"\nPearson r = {corr:.3f} (n={n}, expected: negative)")


def main():
    parser = argparse.ArgumentParser(description="LFX vs Ossuary comparison")
    parser.add_argument("--json", action="store_true", help="JSON output")
    parser.add_argument("--output", "-o", help="Save JSON to file")
    args = parser.parse_args()

    results = asyncio.run(run_comparison())

    if args.json or args.output:
        output = {
            "timestamp": datetime.now().isoformat(),
            "packages": len(results),
            "results": results,
        }
        if args.output:
            with open(args.output, "w") as f:
                json.dump(output, f, indent=2, default=str)
            print(f"Saved to {args.output}")
        else:
            print(json.dumps(output, indent=2, default=str))
    else:
        print_table(results)


if __name__ == "__main__":
    main()
