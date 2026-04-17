"""Tests for the Annex VII compliance report builder."""

import hashlib
import json
from pathlib import Path

from ossuary import __version__ as OSSUARY_VERSION
from ossuary.scoring import METHODOLOGY_VERSION
from ossuary.services.annex_vii import build_annex_vii_record, write_annex_vii_record
from ossuary.services.support_period import (
    ProductSupportPeriod,
    SupportPeriodEstimate,
)


def _score_dict(name, score, level):
    return {
        "package": {"name": name, "ecosystem": "npm"},
        "score": {"final": score, "risk_level": level},
        "metrics": {"maintainer_concentration": 50.0, "commits_last_year": 10},
        "chaoss_signals": {"bus_factor": 2},
    }


class TestRecordStructure:
    def test_required_top_level_fields(self):
        record = build_annex_vii_record(
            component_score_dicts=[_score_dict("lodash", 40, "MODERATE")],
        )
        assert record["schema"] == "ossuary.annex_vii.v1"
        assert record["tool"]["version"] == OSSUARY_VERSION
        assert record["tool"]["methodology_version"] == METHODOLOGY_VERSION
        assert record["regulation"]["reference"] == "Regulation (EU) 2024/2847"
        assert "13(5)" in record["regulation"]["articles_addressed"]
        assert record["scope"]["covers"]
        assert record["scope"]["does_not_cover"]
        assert record["summary"]["components_scored"] == 1
        assert record["summary"]["components_skipped"] == 0

    def test_generated_at_is_iso_utc(self):
        record = build_annex_vii_record([])
        # Ends in Z (UTC marker)
        assert record["generated_at"].endswith("Z")

    def test_package_source_when_no_sbom(self):
        record = build_annex_vii_record([_score_dict("x", 20, "LOW")])
        assert record["source"] == {"type": "package"}

    def test_sbom_source_records_sha256(self, tmp_path):
        sbom_path = tmp_path / "bom.json"
        sbom_path.write_text('{"hello": "world"}')
        expected = hashlib.sha256(sbom_path.read_bytes()).hexdigest()

        record = build_annex_vii_record(
            component_score_dicts=[],
            source_sbom_path=sbom_path,
            source_sbom_format="cyclonedx",
            source_sbom_spec_version="1.5",
        )
        assert record["source"]["type"] == "sbom"
        assert record["source"]["sha256"] == expected
        assert record["source"]["format"] == "cyclonedx"
        assert record["source"]["spec_version"] == "1.5"

    def test_support_period_included_when_provided(self):
        est = SupportPeriodEstimate(
            package_name="xz-utils", ecosystem="github", score=80,
            risk_level="CRITICAL", horizon_months=6,
            cra_minimum_supportable=False, reason="critical",
        )
        psp = ProductSupportPeriod(
            horizon_months=6, cra_minimum_supportable=False,
            critical_top_n=5, critical_selection_method="worst_score",
            limiting_components=[est], critical_components=[est],
            components_total=3, components_scored=3,
        )
        record = build_annex_vii_record(
            component_score_dicts=[_score_dict("xz-utils", 80, "CRITICAL")],
            product_support_period=psp,
        )
        assert record["implied_support_period"]["horizon_months"] == 6
        assert record["summary"]["cra_minimum_supportable"] is False
        assert record["summary"]["implied_support_period_months"] == 6

    def test_skipped_components_recorded(self):
        record = build_annex_vii_record(
            component_score_dicts=[],
            skipped_components=[
                {"name": "mystery", "ecosystem": None, "reason": "no PURL"},
            ],
        )
        assert record["summary"]["components_skipped"] == 1
        assert record["skipped_components"][0]["name"] == "mystery"

    def test_failed_components_recorded_separately_from_skipped(self):
        # Regression: previously, components that failed to score (network
        # errors, repo not found) silently disappeared from the Annex VII
        # record, making it look complete when it was not.
        record = build_annex_vii_record(
            component_score_dicts=[_score_dict("ok-pkg", 30, "LOW")],
            skipped_components=[
                {"name": "no-purl", "ecosystem": None, "reason": "no PURL"},
            ],
            failed_components=[
                {"name": "ghosted", "ecosystem": "npm", "error": "Repository not found"},
                {"name": "rate-limited", "ecosystem": "pypi", "error": "GitHub API timeout"},
            ],
        )
        assert record["summary"]["components_scored"] == 1
        assert record["summary"]["components_skipped"] == 1
        assert record["summary"]["components_failed"] == 2
        assert len(record["failed_components"]) == 2
        names = {c["name"] for c in record["failed_components"]}
        assert names == {"ghosted", "rate-limited"}
        # Failed and skipped must be distinct lists.
        assert "ghosted" not in {c["name"] for c in record["skipped_components"]}

    def test_failed_components_default_empty(self):
        record = build_annex_vii_record(component_score_dicts=[])
        assert record["summary"]["components_failed"] == 0
        assert record["failed_components"] == []


class TestPersistence:
    def test_write_and_re_read_roundtrip(self, tmp_path):
        record = build_annex_vii_record([_score_dict("lodash", 25, "LOW")])
        out = tmp_path / "report.json"
        write_annex_vii_record(record, out)
        reloaded = json.loads(out.read_text())
        assert reloaded["schema"] == "ossuary.annex_vii.v1"
        assert reloaded["components"][0]["package"]["name"] == "lodash"
