"""Annex VII technical-documentation export.

CRA Article 13(4) requires the cybersecurity risk assessment of a product to
be included in the technical documentation set out in Annex VII. Articles
13(12)–(13) require the documentation to be retained for at least 10 years
or the support period (whichever is longer).

This module produces a structured, timestamped, methodology-versioned record
of an Ossuary scoring run suitable for inclusion in that documentation. It
is not a substitute for the technical documentation itself — it is one
component (the governance-risk assessment of OSS dependencies).

The emitted record is JSON. The fields are explicit about scope: it covers
governance signals on third-party components per Article 13(5); it does not
cover vulnerability scanning, license compliance, or any obligation under
Article 14 (vulnerability and incident reporting).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ossuary import __version__ as OSSUARY_VERSION
from ossuary.scoring import METHODOLOGY_VERSION
from ossuary.services.support_period import ProductSupportPeriod


SCOPE_DECLARATION = {
    "covers": [
        "Governance risk of third-party software components (CRA Art. 13(5))",
        "Implied maximum supportable horizon per component (CRA Art. 13(8))",
        "CHAOSS bus factor and contributor concentration signals",
    ],
    "does_not_cover": [
        "Vulnerability scanning or known-CVE detection",
        "Licence compliance",
        "CRA Art. 14 vulnerability and incident reporting",
        "Detection of account-compromise attacks against well-governed projects",
        "Detection of CI/CD pipeline exploits",
    ],
    "interpretation": (
        "A high governance score indicates structural conditions that increase "
        "the likelihood of supply-chain incidents (abandonment, takeover, "
        "unilateral maintainer action). It does not predict any specific "
        "incident. The implied support-period horizon is a heuristic upper "
        "bound based on present governance signals; manufacturers may justify "
        "different horizons with compensating controls."
    ),
}


def _sha256_of_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def build_annex_vii_record(
    component_score_dicts: list[dict],
    skipped_components: Optional[list[dict]] = None,
    failed_components: Optional[list[dict]] = None,
    product_support_period: Optional[ProductSupportPeriod] = None,
    source_sbom_path: Optional[str | Path] = None,
    source_sbom_format: Optional[str] = None,
    source_sbom_spec_version: Optional[str] = None,
    extra_metadata: Optional[dict] = None,
) -> dict:
    """Assemble the structured Annex VII governance-assessment record.

    ``component_score_dicts`` is a list of full ``RiskBreakdown.to_dict()``
    payloads, one per successfully scored component.

    ``skipped_components`` carries components that were *not attempted*
    (no PURL and no ecosystem default; format reasons). Each entry:
    ``{name, ecosystem, reason}``.

    ``failed_components`` carries components that *were attempted but
    errored* during scoring (network failure, repository not found,
    rate-limit, etc.). Each entry: ``{name, ecosystem, error}``. Kept
    distinct from skipped because an audit needs to see attempted-but-
    incomplete coverage separately from intentionally-out-of-scope coverage.

    ``product_support_period`` is the SBOM-level implied support period if
    derived; ``None`` for single-package mode.

    ``source_sbom_path``/``format``/``spec_version`` describe the SBOM the
    record was built from; if absent the record records the assessment as
    package-level rather than SBOM-level.
    """
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    source = {"type": "package" if not source_sbom_path else "sbom"}
    if source_sbom_path:
        source["path"] = str(source_sbom_path)
        source["sha256"] = _sha256_of_file(source_sbom_path)
        if source_sbom_format:
            source["format"] = source_sbom_format
        if source_sbom_spec_version:
            source["spec_version"] = source_sbom_spec_version

    record = {
        "schema": "ossuary.annex_vii.v1",
        "generated_at": now_utc,
        "tool": {
            "name": "ossuary",
            "version": OSSUARY_VERSION,
            "methodology_version": METHODOLOGY_VERSION,
            "homepage": "https://github.com/anicka-net/ossuary-risk",
        },
        "regulation": {
            "name": "Cyber Resilience Act",
            "reference": "Regulation (EU) 2024/2847",
            "articles_addressed": ["13(2)", "13(3)", "13(4)", "13(5)", "13(8)"],
        },
        "source": source,
        "scope": SCOPE_DECLARATION,
        "summary": {
            "components_scored": len(component_score_dicts),
            "components_skipped": len(skipped_components or []),
            "components_failed": len(failed_components or []),
        },
        "components": component_score_dicts,
        "skipped_components": skipped_components or [],
        "failed_components": failed_components or [],
    }

    if product_support_period is not None:
        record["implied_support_period"] = product_support_period.to_dict()
        record["summary"]["implied_support_period_months"] = (
            product_support_period.horizon_months
        )
        record["summary"]["cra_minimum_supportable"] = (
            product_support_period.cra_minimum_supportable
        )

    if extra_metadata:
        record["extra_metadata"] = extra_metadata

    return record


def write_annex_vii_record(record: dict, output_path: str | Path) -> None:
    """Write the Annex VII record to disk as pretty-printed JSON."""
    Path(output_path).write_text(
        json.dumps(record, indent=2, default=str), encoding="utf-8"
    )
