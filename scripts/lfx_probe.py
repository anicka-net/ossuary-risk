#!/usr/bin/env python3
"""
Probe LFX Insights for the Health Score of packages in Ossuary's
validation set, and print a comparison of Ossuary v6.3 scores against
the LFX scores for every overlap.

LFX Insights (insights.linuxfoundation.org) exposes an undocumented
but stable JSON endpoint:

    https://insights.linuxfoundation.org/api/project/{slug}

Slugs are mostly lowercase project names; for GitHub-scoped projects
we also try the `org-repo` form. Not every Ossuary package is in
LFX, and not every LFX match is the same artifact — callers should
spot-check the returned project name against what they expected.

Output is a small markdown table suitable for cross-check in
thesis §5.10.2.

Usage:
    python scripts/lfx_probe.py                # prints the table
    python scripts/lfx_probe.py --json out.json
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))


def probe(slug, timeout=8):
    """Return the LFX project payload for ``slug`` or None on miss."""
    url = f"https://insights.linuxfoundation.org/api/project/{slug}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return None
    except Exception:
        return None


# (Ossuary slug, LFX slug) pairs that probe matches but resolve to a
# different project than intended. Manually audited 2026-04-21.
SLUG_DENYLIST = {
    ("boltdb/bolt", "bolt"),  # bolt → Bolt CMS, the PHP CMS, not boltdb/bolt
}


def slug_candidates(pkg_name):
    """Yield plausible LFX slug forms for an Ossuary package name."""
    bare = pkg_name.lower()
    yield bare
    if "/" in bare:
        org, _, repo = bare.partition("/")
        yield repo
        yield f"{org}-{repo}"
    if "-" in bare:
        yield bare.replace("-", "")
    if "_" in bare:
        yield bare.replace("_", "-")


def health_tier(score):
    """LFX's documented 5-tier label for a 0-100 Health Score."""
    if score is None:
        return "—"
    if score >= 80:
        return "Excellent" if score >= 85 else "Healthy"
    if score >= 70:
        return "Healthy"
    if score >= 50:
        return "Stable"
    if score >= 30:
        return "Unsteady"
    return "Critical"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--ablation",
        default=str(REPO / "thesis" / "ablation_results.json"),
        help="Ablation results JSON (for Ossuary scores).",
    )
    ap.add_argument("--json", help="Optional: dump raw probe results as JSON.")
    args = ap.parse_args()

    ablation = json.loads(Path(args.ablation).read_text())
    baseline = ablation["runs"]["baseline"]["results"]

    hits = []
    misses = []
    for r in baseline:
        if r.get("error") or r.get("score") is None:
            continue
        for slug in slug_candidates(r["name"]):
            if (r["name"], slug) in SLUG_DENYLIST:
                continue
            d = probe(slug)
            if d and d.get("healthScore") is not None:
                hits.append(
                    {
                        "package": r["name"],
                        "ecosystem": r["ecosystem"],
                        "ossuary_score": r["score"],
                        "ossuary_class": r["classification"],
                        "lfx_slug": slug,
                        "lfx_name": d.get("name"),
                        "lfx_health": d["healthScore"],
                        "lfx_tier": health_tier(d["healthScore"]),
                    }
                )
                break
        else:
            misses.append(r["name"])

    print(
        f"| Package | Ossuary score | Ossuary class | LFX Health | LFX tier |"
    )
    print("|---|---:|---|---:|---|")
    for h in sorted(hits, key=lambda x: x["ossuary_score"]):
        print(
            f"| `{h['package']}` | {h['ossuary_score']} | {h['ossuary_class']} | "
            f"{h['lfx_health']} | {h['lfx_tier']} |"
        )
    print()
    print(f"Overlap: {len(hits)} of {len(baseline)} Ossuary packages matched an LFX slug.")

    if args.json:
        Path(args.json).write_text(json.dumps({"hits": hits, "misses": misses}, indent=2))


if __name__ == "__main__":
    main()
