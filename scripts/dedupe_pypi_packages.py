#!/usr/bin/env python3
"""Merge PyPI package rows that differ only in PEP 503 canonicalisation.

Background: ``services/cache.py`` previously did a case-sensitive lookup
on ``Package.name``, so the same PyPI distribution could end up in the DB
under several spellings (``PyYAML`` vs ``pyyaml``) with separately-cached
scores. The cache is now normalised at the chokepoint, but the historical
data needs cleanup. This script does that cleanup.

Algorithm (per PyPI package whose name is not already canonical):

1. Compute the canonical name (PEP 503: lowercase, runs of ``-``, ``_``,
   ``.`` collapsed to a single ``-``).
2. Find or create a ``Package`` row at the canonical name.
3. Re-point every ``Score`` row that pointed at the old (non-canonical)
   row to the canonical row.
4. If the canonical row had no ``repo_url`` and the old row did, copy it
   over.
5. Update ``last_analyzed`` on the canonical row to the most recent of
   the two.
6. Delete the old row.

Score rows are not deduplicated — if both old and canonical rows had a
score for the same ``cutoff_date``, both survive on the canonical row.
This is conservative: we do not silently drop scoring history. The
dashboard's ``get_current_score`` already picks the most recent
``calculated_at`` so the user sees one number per package.

By default the script runs in **dry-run mode**: it prints what would
change and exits 0 without touching the DB. Pass ``--apply`` to execute.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from ossuary.db.models import Package, Score  # noqa: E402
from ossuary.db.session import session_scope  # noqa: E402
from ossuary.services.cache import normalize_package_name  # noqa: E402


def find_pypi_duplicates(session) -> dict[str, list[Package]]:
    """Return ``{canonical_name: [Package, ...]}`` for any PyPI canonical
    name that has more than one row, OR a single row that is non-canonical.
    """
    by_canonical: dict[str, list[Package]] = defaultdict(list)
    for pkg in session.query(Package).filter(Package.ecosystem == "pypi").all():
        canonical = normalize_package_name(pkg.name, "pypi")
        by_canonical[canonical].append(pkg)
    return {
        canonical: pkgs
        for canonical, pkgs in by_canonical.items()
        if len(pkgs) > 1 or (len(pkgs) == 1 and pkgs[0].name != canonical)
    }


def merge_group(session, canonical: str, packages: list[Package], *, apply: bool) -> dict:
    """Merge a group of packages into the canonical name. Returns a
    summary dict of what happened (or would happen)."""
    canonical_pkg = next((p for p in packages if p.name == canonical), None)
    others = [p for p in packages if p is not canonical_pkg]

    summary = {
        "canonical": canonical,
        "merging_in": [p.name for p in others],
        "score_rows_repointed": 0,
        "rows_deleted": 0,
        "repo_url_filled": None,
        "created_canonical_row": False,
    }

    if canonical_pkg is None:
        # No canonical row yet — promote the first non-canonical row's
        # data into a fresh canonical row.
        donor = others.pop(0)
        if apply:
            canonical_pkg = Package(
                name=canonical,
                ecosystem="pypi",
                repo_url=donor.repo_url,
                description=donor.description,
                homepage=donor.homepage,
                last_analyzed=donor.last_analyzed,
            )
            session.add(canonical_pkg)
            session.flush()
        summary["created_canonical_row"] = True
        summary["merging_in"].insert(0, donor.name)
        # The donor itself still needs its scores re-pointed and to be deleted.
        others.insert(0, donor)

    for old in others:
        # Re-point all Score rows from old to canonical.
        if apply:
            n = (
                session.query(Score)
                .filter(Score.package_id == old.id)
                .update({Score.package_id: canonical_pkg.id})
            )
        else:
            n = session.query(Score).filter(Score.package_id == old.id).count()
        summary["score_rows_repointed"] += n

        # Fill repo_url / description / homepage if the canonical row is
        # missing them and the old row has them.
        if apply and canonical_pkg is not None:
            if not canonical_pkg.repo_url and old.repo_url:
                canonical_pkg.repo_url = old.repo_url
                summary["repo_url_filled"] = old.repo_url
            if not canonical_pkg.description and old.description:
                canonical_pkg.description = old.description
            if not canonical_pkg.homepage and old.homepage:
                canonical_pkg.homepage = old.homepage
            # Pick the most recent last_analyzed.
            if old.last_analyzed and (
                not canonical_pkg.last_analyzed
                or old.last_analyzed > canonical_pkg.last_analyzed
            ):
                canonical_pkg.last_analyzed = old.last_analyzed

        if apply:
            session.delete(old)
        summary["rows_deleted"] += 1

    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually modify the database. Without this flag the script "
        "is a dry run and prints what would change.",
    )
    args = parser.parse_args()

    with session_scope() as session:
        duplicates = find_pypi_duplicates(session)

        if not duplicates:
            print("No PyPI normalisation duplicates found. Nothing to do.")
            return 0

        print(
            f"Found {len(duplicates)} canonical PyPI name(s) with "
            f"non-canonical or duplicate rows:"
        )
        print()

        total_repointed = 0
        total_deleted = 0
        total_created = 0

        for canonical, pkgs in sorted(duplicates.items()):
            summary = merge_group(session, canonical, list(pkgs), apply=args.apply)
            arrow = "->" if not args.apply else "->>"
            print(f"  {sorted({p.name for p in pkgs})}  {arrow}  {canonical!r}")
            if summary["created_canonical_row"]:
                print("    (would create canonical row)" if not args.apply
                      else "    created canonical row")
            print(
                f"    score rows re-pointed: {summary['score_rows_repointed']}; "
                f"rows {'would be ' if not args.apply else ''}deleted: "
                f"{summary['rows_deleted']}"
            )
            if summary["repo_url_filled"]:
                print(f"    canonical row repo_url <- {summary['repo_url_filled']}")
            total_repointed += summary["score_rows_repointed"]
            total_deleted += summary["rows_deleted"]
            total_created += int(summary["created_canonical_row"])

        print()
        print(f"Total: {total_repointed} score rows re-pointed, "
              f"{total_deleted} package rows deleted, "
              f"{total_created} canonical rows created.")

        if not args.apply:
            print()
            print("DRY RUN. Re-run with --apply to execute.")
        else:
            session.commit()
            print()
            print("Applied.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
