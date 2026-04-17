"""Tests for SBOM ingestion and enrichment."""

import json

import pytest

from ossuary.services.sbom import (
    SBOMComponent,
    enrich_cyclonedx,
    enrich_spdx,
    parse_purl,
    parse_sbom,
)


class TestPurlParsing:
    def test_npm_scoped(self):
        assert parse_purl("pkg:npm/%40babel/core@7.23.0") == ("npm", "@babel/core", "7.23.0")

    def test_npm_scoped_unencoded(self):
        # Some toolchains emit @ unescaped in the namespace.
        assert parse_purl("pkg:npm/lodash@4.17.21") == ("npm", "lodash", "4.17.21")

    def test_pypi(self):
        assert parse_purl("pkg:pypi/requests@2.31.0") == ("pypi", "requests", "2.31.0")

    def test_pypi_no_version(self):
        assert parse_purl("pkg:pypi/numpy") == ("pypi", "numpy", None)

    def test_cargo(self):
        assert parse_purl("pkg:cargo/serde@1.0.193") == ("cargo", "serde", "1.0.193")

    def test_gem_to_rubygems(self):
        assert parse_purl("pkg:gem/rails@7.1.2") == ("rubygems", "rails", "7.1.2")

    def test_composer_to_packagist(self):
        ecosystem, name, version = parse_purl("pkg:composer/symfony/console@6.4.0")
        assert ecosystem == "packagist"
        assert name == "symfony/console"
        assert version == "6.4.0"

    def test_nuget(self):
        assert parse_purl("pkg:nuget/Newtonsoft.Json@13.0.3") == (
            "nuget", "Newtonsoft.Json", "13.0.3",
        )

    def test_golang(self):
        ecosystem, name, version = parse_purl("pkg:golang/github.com/gorilla/mux@v1.8.0")
        assert ecosystem == "go"
        assert name == "github.com/gorilla/mux"
        assert version == "v1.8.0"

    def test_github(self):
        ecosystem, name, version = parse_purl("pkg:github/anicka-net/ossuary-risk@v0.8.0")
        assert ecosystem == "github"
        assert name == "anicka-net/ossuary-risk"
        assert version == "v0.8.0"

    def test_unknown_type_returns_none(self):
        assert parse_purl("pkg:deb/ubuntu/openssl@1.1.1") == (None, None, None)

    def test_invalid_purl(self):
        assert parse_purl("not-a-purl") == (None, None, None)
        assert parse_purl("") == (None, None, None)
        assert parse_purl("pkg:") == (None, None, None)

    def test_purl_with_qualifiers_and_subpath(self):
        # PURL spec allows ?qualifiers and #subpath; ecosystem/name/version unchanged.
        assert parse_purl("pkg:npm/lodash@4.17.21?arch=x86#path") == (
            "npm", "lodash", "4.17.21",
        )


class TestCycloneDXParsing:
    def test_minimal_cyclonedx(self, tmp_path):
        doc = {
            "bomFormat": "CycloneDX",
            "specVersion": "1.5",
            "components": [
                {
                    "type": "library",
                    "bom-ref": "pkg:npm/lodash@4.17.21",
                    "name": "lodash",
                    "version": "4.17.21",
                    "purl": "pkg:npm/lodash@4.17.21",
                },
                {
                    "type": "library",
                    "bom-ref": "pkg:pypi/requests@2.31.0",
                    "name": "requests",
                    "version": "2.31.0",
                    "purl": "pkg:pypi/requests@2.31.0",
                },
            ],
        }
        path = tmp_path / "bom.json"
        path.write_text(json.dumps(doc))

        sbom = parse_sbom(path)
        assert sbom.format == "cyclonedx"
        assert sbom.spec_version == "1.5"
        assert len(sbom.components) == 2

        lodash, requests = sbom.components
        assert lodash.name == "lodash"
        assert lodash.ecosystem == "npm"
        assert lodash.version == "4.17.21"
        assert lodash.bom_ref == "pkg:npm/lodash@4.17.21"
        assert lodash.raw_index == 0

        assert requests.ecosystem == "pypi"
        assert requests.raw_index == 1

    def test_component_without_purl_falls_back_to_name(self, tmp_path):
        doc = {
            "bomFormat": "CycloneDX",
            "specVersion": "1.4",
            "components": [{"type": "library", "name": "mystery-lib", "version": "1.0"}],
        }
        path = tmp_path / "bom.json"
        path.write_text(json.dumps(doc))

        sbom = parse_sbom(path)
        assert len(sbom.components) == 1
        assert sbom.components[0].name == "mystery-lib"
        assert sbom.components[0].ecosystem is None  # no PURL → no ecosystem

    def test_components_without_name_skipped(self, tmp_path):
        doc = {
            "bomFormat": "CycloneDX",
            "specVersion": "1.5",
            "components": [
                {"type": "library", "version": "1.0"},  # no name → skip
                {"type": "library", "name": "ok", "version": "1.0"},
            ],
        }
        path = tmp_path / "bom.json"
        path.write_text(json.dumps(doc))

        sbom = parse_sbom(path)
        assert len(sbom.components) == 1
        assert sbom.components[0].name == "ok"


class TestSPDXParsing:
    def test_minimal_spdx(self, tmp_path):
        doc = {
            "spdxVersion": "SPDX-2.3",
            "SPDXID": "SPDXRef-DOCUMENT",
            "documentDescribes": ["SPDXRef-Product"],
            "packages": [
                {
                    "SPDXID": "SPDXRef-Product",
                    "name": "my-product",
                    "versionInfo": "1.0.0",
                },
                {
                    "SPDXID": "SPDXRef-lodash",
                    "name": "lodash",
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
        path = tmp_path / "bom.spdx.json"
        path.write_text(json.dumps(doc))

        sbom = parse_sbom(path)
        assert sbom.format == "spdx"
        assert sbom.spec_version == "SPDX-2.3"
        # Product (documentDescribes) is excluded; only the dependency remains.
        assert len(sbom.components) == 1

        lodash = sbom.components[0]
        assert lodash.name == "lodash"
        assert lodash.ecosystem == "npm"
        assert lodash.version == "4.17.21"
        assert lodash.spdx_id == "SPDXRef-lodash"
        # raw_index reflects the position in raw['packages'], not the filtered list
        assert lodash.raw_index == 1

    def test_spdx_without_purl(self, tmp_path):
        doc = {
            "spdxVersion": "SPDX-2.3",
            "packages": [
                {"SPDXID": "SPDXRef-foo", "name": "foo", "versionInfo": "0.1"},
            ],
        }
        path = tmp_path / "bom.spdx.json"
        path.write_text(json.dumps(doc))

        sbom = parse_sbom(path)
        assert len(sbom.components) == 1
        assert sbom.components[0].ecosystem is None


class TestUnrecognisedSBOM:
    def test_invalid_json_raises(self, tmp_path):
        path = tmp_path / "junk.json"
        path.write_text("not json {{{")
        with pytest.raises(ValueError, match="not valid JSON"):
            parse_sbom(path)

    def test_unknown_format_raises(self, tmp_path):
        path = tmp_path / "weird.json"
        path.write_text(json.dumps({"hello": "world"}))
        with pytest.raises(ValueError, match="Unrecognised SBOM format"):
            parse_sbom(path)


class TestEnrichment:
    def _score(self, final, level="HIGH", concentration=80.0, bus_factor=1, commits=2):
        return {
            "score": {"final": final, "risk_level": level},
            "metrics": {
                "maintainer_concentration": concentration,
                "commits_last_year": commits,
            },
            "chaoss_signals": {"bus_factor": bus_factor},
        }

    def test_enrich_cyclonedx_adds_properties(self):
        raw = {
            "bomFormat": "CycloneDX",
            "specVersion": "1.5",
            "components": [
                {"name": "lodash", "version": "4.17.21"},
                {"name": "express", "version": "4.18.0"},
            ],
        }
        scores = {0: self._score(80, "CRITICAL"), 1: self._score(20, "LOW")}

        enriched = enrich_cyclonedx(raw, scores)

        # Original raw is unchanged
        assert "properties" not in raw["components"][0]

        c0 = enriched["components"][0]
        names = {p["name"]: p["value"] for p in c0["properties"]}
        assert names["ossuary:governance:score"] == "80"
        assert names["ossuary:governance:risk_level"] == "CRITICAL"
        assert names["ossuary:governance:bus_factor"] == "1"

        c1 = enriched["components"][1]
        names = {p["name"]: p["value"] for p in c1["properties"]}
        assert names["ossuary:governance:score"] == "20"

    def test_enrich_cyclonedx_idempotent_on_re_enrichment(self):
        raw = {
            "bomFormat": "CycloneDX",
            "specVersion": "1.5",
            "components": [{"name": "lodash", "version": "4.17.21"}],
        }
        first = enrich_cyclonedx(raw, {0: self._score(80, "CRITICAL")})
        second = enrich_cyclonedx(first, {0: self._score(40, "MODERATE")})

        # Second enrichment replaces, does not append, ossuary properties.
        ossuary_props = [
            p for p in second["components"][0]["properties"]
            if p["name"].startswith("ossuary:governance")
        ]
        scores = [p for p in ossuary_props if p["name"] == "ossuary:governance:score"]
        assert len(scores) == 1
        assert scores[0]["value"] == "40"

    def test_enrich_cyclonedx_preserves_non_ossuary_properties(self):
        raw = {
            "bomFormat": "CycloneDX",
            "specVersion": "1.5",
            "components": [
                {
                    "name": "lodash",
                    "version": "4.17.21",
                    "properties": [{"name": "internal:reviewed", "value": "true"}],
                }
            ],
        }
        enriched = enrich_cyclonedx(raw, {0: self._score(40)})
        kept = [p for p in enriched["components"][0]["properties"] if p["name"] == "internal:reviewed"]
        assert kept == [{"name": "internal:reviewed", "value": "true"}]

    def test_enrich_spdx_adds_annotation(self):
        raw = {
            "spdxVersion": "SPDX-2.3",
            "packages": [
                {"SPDXID": "SPDXRef-lodash", "name": "lodash", "versionInfo": "4.17.21"},
            ],
        }
        enriched = enrich_spdx(raw, {0: self._score(80, "CRITICAL")}, ossuary_version="0.9.0")
        annotations = enriched["packages"][0]["annotations"]
        assert len(annotations) == 1
        ann = annotations[0]
        # Required SPDX 2.3 annotation fields per JSON schema
        assert ann["annotationType"] in ("REVIEW", "OTHER")
        assert ann["annotator"].startswith("Tool: ossuary-")
        assert ann["annotationDate"]
        assert "ossuary-governance" in ann["comment"]

    def test_enrich_spdx_embeds_spdx_id_in_payload(self):
        # Portability: even if a downstream tool extracts the annotation
        # from its package and stores it standalone, the link to the
        # original package must be preserved via SPDXID in the payload.
        raw = {
            "spdxVersion": "SPDX-2.3",
            "packages": [
                {"SPDXID": "SPDXRef-lodash", "name": "lodash", "versionInfo": "4.17.21"},
                {"SPDXID": "SPDXRef-express", "name": "express", "versionInfo": "4.18.0"},
            ],
        }
        enriched = enrich_spdx(
            raw,
            {0: self._score(80, "CRITICAL"), 1: self._score(20, "LOW")},
            ossuary_version="0.9.0",
        )

        for idx, expected_id in enumerate(["SPDXRef-lodash", "SPDXRef-express"]):
            ann = enriched["packages"][idx]["annotations"][0]
            comment = ann["comment"]
            assert comment.startswith("ossuary-governance: ")
            payload = json.loads(comment.removeprefix("ossuary-governance: "))
            assert payload["spdx_id"] == expected_id

    def test_enrich_spdx_idempotent(self):
        raw = {
            "spdxVersion": "SPDX-2.3",
            "packages": [
                {"SPDXID": "SPDXRef-lodash", "name": "lodash", "versionInfo": "4.17.21"},
            ],
        }
        first = enrich_spdx(raw, {0: self._score(80)}, ossuary_version="0.9.0")
        second = enrich_spdx(first, {0: self._score(40)}, ossuary_version="0.9.0")

        ossuary_annotations = [
            a for a in second["packages"][0]["annotations"]
            if str(a.get("annotator", "")).startswith("Tool: ossuary")
        ]
        assert len(ossuary_annotations) == 1
        assert "\"score\":40" in ossuary_annotations[0]["comment"]
