"""SPDX 2.3 interop test: round-trip enriched output through ``spdx-tools``.

Schema validation (``test_sbom_spdx_conformance.py``) proves the enriched
JSON conforms to the official SPDX 2.3 JSON Schema. This test goes one
step further: it loads our enriched output through the ``spdx-tools``
Python library — a real, independent SPDX consumer — and confirms that:

  1. Parsing succeeds (no exceptions, no logger errors).
  2. spdx-tools' ``validate_full_spdx_document`` returns zero issues
     (this is stricter than the JSON schema; e.g. it enforces
     ``documentNamespace`` per spec §6.5).
  3. The Ossuary annotations are reachable through the parsed data
     model, with the link back to their package preserved.

``spdx-tools`` is heavy (pulls rdflib, ply, beartype, …) and lives in the
optional ``[dev-spdx-interop]`` extra. The test auto-skips when the
library isn't installed, so it's safe in default ``[dev]`` test runs.
"""

import json

import pytest

from ossuary.services.sbom import enrich_spdx

spdx_parser = pytest.importorskip("spdx_tools.spdx.parser.parse_anything")
spdx_validator = pytest.importorskip("spdx_tools.spdx.validation.document_validator")


def _full_valid_spdx() -> dict:
    """A more complete SPDX 2.3 doc — passes both JSON Schema and spdx-tools.

    The JSON Schema doesn't require ``documentNamespace`` (despite spec
    §6.5 mandating it), so the conformance test fixture omits it. The
    interop fixture includes it because spdx-tools enforces the spec
    requirement.
    """
    return {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": "test-product-sbom",
        "documentNamespace": "https://example.com/sbom/test-product-1.0.0",
        "documentDescribes": ["SPDXRef-Product"],
        "creationInfo": {
            "created": "2026-04-17T00:00:00Z",
            "creators": ["Tool: ossuary-tests"],
        },
        "packages": [
            {
                "SPDXID": "SPDXRef-Product",
                "name": "test-product",
                "downloadLocation": "NOASSERTION",
                "versionInfo": "1.0.0",
            },
            {
                "SPDXID": "SPDXRef-lodash",
                "name": "lodash",
                "downloadLocation": "https://registry.npmjs.org/lodash/-/lodash-4.17.21.tgz",
                "versionInfo": "4.17.21",
            },
            {
                "SPDXID": "SPDXRef-express",
                "name": "express",
                "downloadLocation": "https://registry.npmjs.org/express/-/express-4.18.0.tgz",
                "versionInfo": "4.18.0",
            },
        ],
    }


def _score(final, level="HIGH"):
    return {
        "score": {"final": final, "risk_level": level},
        "metrics": {"maintainer_concentration": 80.0, "commits_last_year": 2},
        "chaoss_signals": {"bus_factor": 1},
    }


@pytest.fixture
def enriched_spdx_path(tmp_path):
    raw = _full_valid_spdx()
    enriched = enrich_spdx(
        raw,
        {1: _score(80, "CRITICAL"), 2: _score(20, "LOW")},
        ossuary_version="0.9.0",
    )
    path = tmp_path / "enriched.spdx.json"
    path.write_text(json.dumps(enriched), encoding="utf-8")
    return path


class TestSpdxToolsRoundTrip:
    def test_spdx_tools_parses_enriched_output(self, enriched_spdx_path):
        # Raises SPDXParsingError if parsing fails.
        doc = spdx_parser.parse_file(str(enriched_spdx_path))
        assert doc is not None
        assert len(doc.packages) == 3  # product + 2 dependencies

    def test_spdx_tools_validation_finds_no_issues(self, enriched_spdx_path):
        doc = spdx_parser.parse_file(str(enriched_spdx_path))
        issues = spdx_validator.validate_full_spdx_document(doc)
        assert issues == [], "\n".join(str(i) for i in issues)

    def test_ossuary_annotations_reachable_after_parse(self, enriched_spdx_path):
        """spdx-tools normalises per-package annotations onto Document.annotations,
        each carrying ``spdx_id`` linking back to the package they annotate.
        Verifies the link survives the JSON → in-memory model round trip.
        """
        doc = spdx_parser.parse_file(str(enriched_spdx_path))
        ossuary_annotations = [
            a for a in doc.annotations
            if a.annotator and "ossuary" in str(a.annotator).lower()
        ]
        assert len(ossuary_annotations) == 2
        annotated_ids = {str(a.spdx_id) for a in ossuary_annotations}
        assert annotated_ids == {"SPDXRef-lodash", "SPDXRef-express"}

    def test_ossuary_payload_parses_as_json(self, enriched_spdx_path):
        """The annotation comment is a JSON-encoded payload prefixed with
        ``ossuary-governance: ``. Confirm it survives round-trip and
        contains the score we wrote.
        """
        doc = spdx_parser.parse_file(str(enriched_spdx_path))
        scores_by_spdx_id = {}
        for a in doc.annotations:
            comment = a.annotation_comment or ""
            if not comment.startswith("ossuary-governance: "):
                continue
            payload = json.loads(comment.removeprefix("ossuary-governance: "))
            scores_by_spdx_id[str(a.spdx_id)] = payload["score"]
        assert scores_by_spdx_id == {
            "SPDXRef-lodash": 80,
            "SPDXRef-express": 20,
        }
