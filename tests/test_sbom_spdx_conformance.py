"""SPDX 2.3 schema conformance for enriched SBOM output.

Backs the project's claim that ``ossuary score-sbom --enrich`` produces
SPDX 2.3-conformant JSON. The official schema is vendored at
``tests/fixtures/spdx-schema-2.3.json`` (from the spdx-spec repository,
support/2.3 branch). Each test builds a minimal-but-complete SPDX
document, runs it through ``enrich_spdx``, and validates the result
against the schema with ``jsonschema``.

The schema uses ``additionalProperties: false`` at every level, so any
unknown field (in our enrichment payload or elsewhere) will fail
validation. That makes this an honest conformance check, not a smoke test.
"""

import json
from pathlib import Path

import pytest

from ossuary.services.sbom import enrich_spdx

jsonschema = pytest.importorskip("jsonschema")


SCHEMA_PATH = Path(__file__).parent / "fixtures" / "spdx-schema-2.3.json"


@pytest.fixture(scope="module")
def spdx_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def validator(spdx_schema):
    return jsonschema.Draft7Validator(spdx_schema)


def _minimal_valid_spdx() -> dict:
    """Return a minimal SPDX 2.3 document satisfying every required field.

    Required at top level: SPDXID, creationInfo (created, creators),
    dataLicense, name, spdxVersion. Required per package: SPDXID,
    downloadLocation, name.
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
                "externalRefs": [
                    {
                        "referenceCategory": "PACKAGE-MANAGER",
                        "referenceType": "purl",
                        "referenceLocator": "pkg:npm/lodash@4.17.21",
                    }
                ],
            },
        ],
    }


def _score(final, level="HIGH", concentration=80.0, bus_factor=1, commits=2):
    return {
        "score": {"final": final, "risk_level": level},
        "metrics": {
            "maintainer_concentration": concentration,
            "commits_last_year": commits,
        },
        "chaoss_signals": {"bus_factor": bus_factor},
    }


class TestUnenrichedFixtureIsValid:
    """Sanity check: the test fixture itself conforms to SPDX 2.3.

    If this fails, every other test in this module is meaningless — we'd
    be testing whether enrichment fixes a broken fixture, not whether
    enrichment preserves conformance.
    """

    def test_minimal_fixture_validates(self, validator):
        doc = _minimal_valid_spdx()
        errors = sorted(validator.iter_errors(doc), key=lambda e: e.path)
        assert errors == [], _format_errors(errors)


class TestEnrichedSPDXValidates:
    def test_enriched_minimal_spdx_validates(self, validator):
        raw = _minimal_valid_spdx()
        # Score the lodash package (index 1; index 0 is the product root).
        enriched = enrich_spdx(raw, {1: _score(80, "CRITICAL")}, ossuary_version="0.9.0")
        errors = sorted(validator.iter_errors(enriched), key=lambda e: e.path)
        assert errors == [], _format_errors(errors)

    def test_enriched_multiple_packages_validates(self, validator):
        raw = _minimal_valid_spdx()
        # Add a second dependency.
        raw["packages"].append({
            "SPDXID": "SPDXRef-express",
            "name": "express",
            "downloadLocation": "https://registry.npmjs.org/express/-/express-4.18.0.tgz",
            "versionInfo": "4.18.0",
            "externalRefs": [
                {
                    "referenceCategory": "PACKAGE-MANAGER",
                    "referenceType": "purl",
                    "referenceLocator": "pkg:npm/express@4.18.0",
                }
            ],
        })
        enriched = enrich_spdx(
            raw,
            {1: _score(80, "CRITICAL"), 2: _score(20, "LOW")},
            ossuary_version="0.9.0",
        )
        errors = sorted(validator.iter_errors(enriched), key=lambda e: e.path)
        assert errors == [], _format_errors(errors)

    def test_re_enrichment_still_validates(self, validator):
        raw = _minimal_valid_spdx()
        once = enrich_spdx(raw, {1: _score(80, "CRITICAL")}, ossuary_version="0.9.0")
        twice = enrich_spdx(once, {1: _score(40, "MODERATE")}, ossuary_version="0.9.0")
        errors = sorted(validator.iter_errors(twice), key=lambda e: e.path)
        assert errors == [], _format_errors(errors)

    def test_enriched_preserves_existing_annotations(self, validator):
        raw = _minimal_valid_spdx()
        raw["packages"][1]["annotations"] = [
            {
                "annotationType": "OTHER",
                "annotator": "Person: external-reviewer",
                "annotationDate": "2026-04-01T12:00:00Z",
                "comment": "manual review note",
            }
        ]
        enriched = enrich_spdx(raw, {1: _score(80)}, ossuary_version="0.9.0")
        errors = sorted(validator.iter_errors(enriched), key=lambda e: e.path)
        assert errors == [], _format_errors(errors)
        # External annotation kept, ossuary annotation added.
        annotations = enriched["packages"][1]["annotations"]
        annotators = [a["annotator"] for a in annotations]
        assert "Person: external-reviewer" in annotators
        assert any(a.startswith("Tool: ossuary-") for a in annotators)


class TestEnrichmentDoesNotIntroduceUnknownFields:
    """The schema sets additionalProperties=false on packages and annotations.
    This catches accidental schema drift in our enrichment code.
    """

    def test_no_unknown_fields_at_package_level(self, validator):
        raw = _minimal_valid_spdx()
        enriched = enrich_spdx(raw, {1: _score(80)}, ossuary_version="0.9.0")
        # Field-by-field re-validation of just the dependency package.
        package_schema = (
            jsonschema.Draft7Validator(validator.schema)
            .schema["properties"]["packages"]["items"]
        )
        package_validator = jsonschema.Draft7Validator(package_schema)
        errors = sorted(
            package_validator.iter_errors(enriched["packages"][1]),
            key=lambda e: e.path,
        )
        assert errors == [], _format_errors(errors)


def _format_errors(errors) -> str:
    """Format jsonschema errors so test failures are actually readable."""
    if not errors:
        return ""
    return "\n".join(
        f"  at {list(e.absolute_path)}: {e.message}"
        for e in errors
    )
