"""SBOM (Software Bill of Materials) ingestion and enrichment.

Supports CycloneDX 1.4+ JSON and SPDX 2.3+ JSON, the two formats most likely
to be specified by the Commission's Article 13(24) implementing act on SBOM
format. Components are identified by Package URL (PURL) where present,
otherwise by name + ecosystem hint.

The enriched output preserves the original SBOM structure and adds Ossuary
governance scores as native properties (CycloneDX) or annotations (SPDX),
so downstream tooling that does not understand the additions still parses
the file correctly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional
from urllib.parse import unquote


# PURL type → Ossuary ecosystem name mapping. Source: PURL spec
# (https://github.com/package-url/purl-spec) cross-referenced with the
# ecosystems Ossuary collectors support.
PURL_TYPE_TO_ECOSYSTEM = {
    "npm": "npm",
    "pypi": "pypi",
    "cargo": "cargo",
    "gem": "rubygems",
    "composer": "packagist",
    "nuget": "nuget",
    "golang": "go",
    "github": "github",
}

SUPPORTED_FORMATS = ("cyclonedx", "spdx")


@dataclass
class SBOMComponent:
    """A single component extracted from an SBOM, ready to be scored."""

    name: str
    ecosystem: Optional[str]
    version: Optional[str] = None
    purl: Optional[str] = None
    bom_ref: Optional[str] = None  # CycloneDX reference, used for round-trip
    spdx_id: Optional[str] = None  # SPDX SPDXID, used for round-trip
    raw_index: int = -1  # position in raw['components'] / raw['packages']


@dataclass
class SBOMDocument:
    """A parsed SBOM with its detected format and component list."""

    format: str  # "cyclonedx" or "spdx"
    spec_version: str
    components: list[SBOMComponent]
    raw: dict


def parse_purl(purl: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Extract (ecosystem, name, version) from a Package URL.

    Returns (None, None, None) if the PURL is unparseable or its type is not
    in PURL_TYPE_TO_ECOSYSTEM. Namespace handling per PURL spec — for npm
    scoped packages the namespace is the scope (e.g. ``@scope/name``), for
    Go and Composer it is part of the canonical name.
    """
    if not purl or not purl.startswith("pkg:"):
        return None, None, None

    body = purl[4:]
    if "?" in body:
        body = body.split("?", 1)[0]
    if "#" in body:
        body = body.split("#", 1)[0]

    try:
        type_part, rest = body.split("/", 1)
    except ValueError:
        return None, None, None

    ecosystem = PURL_TYPE_TO_ECOSYSTEM.get(type_part.lower())
    if not ecosystem:
        return None, None, None

    if "@" in rest:
        name_part, version = rest.rsplit("@", 1)
        version = unquote(version)
    else:
        name_part, version = rest, None

    name_part = unquote(name_part)

    if "/" in name_part:
        namespace, name = name_part.rsplit("/", 1)
        if ecosystem == "npm" and namespace:
            full_name = f"{namespace}/{name}"
        elif ecosystem in ("go", "packagist", "github"):
            full_name = f"{namespace}/{name}"
        else:
            full_name = name
    else:
        full_name = name_part

    return ecosystem, full_name, version


def _detect_format(raw: dict) -> Optional[str]:
    """Identify whether a parsed JSON document is CycloneDX or SPDX."""
    if raw.get("bomFormat") == "CycloneDX":
        return "cyclonedx"
    if raw.get("spdxVersion"):
        return "spdx"
    if "components" in raw and "specVersion" in raw:
        return "cyclonedx"
    if "packages" in raw and ("SPDXID" in raw or "documentDescribes" in raw):
        return "spdx"
    return None


def _iter_cyclonedx_components(raw: dict) -> Iterator[SBOMComponent]:
    for idx, component in enumerate(raw.get("components", []) or []):
        purl = component.get("purl")
        ecosystem, purl_name, purl_version = parse_purl(purl) if purl else (None, None, None)
        name = purl_name or component.get("name")
        if not name:
            continue
        version = purl_version or component.get("version")
        yield SBOMComponent(
            name=name,
            ecosystem=ecosystem,
            version=version,
            purl=purl,
            bom_ref=component.get("bom-ref"),
            raw_index=idx,
        )


def _iter_spdx_components(raw: dict) -> Iterator[SBOMComponent]:
    document_describes = set(raw.get("documentDescribes") or [])
    for idx, package in enumerate(raw.get("packages", []) or []):
        spdx_id = package.get("SPDXID")
        # Skip the root document package (the product itself, not a dependency).
        if spdx_id and spdx_id in document_describes:
            continue

        purl = None
        for ref in package.get("externalRefs", []) or []:
            if (
                ref.get("referenceCategory") == "PACKAGE-MANAGER"
                and ref.get("referenceType") == "purl"
            ):
                purl = ref.get("referenceLocator")
                break

        ecosystem, purl_name, purl_version = parse_purl(purl) if purl else (None, None, None)
        name = purl_name or package.get("name")
        if not name:
            continue
        version = purl_version or package.get("versionInfo")
        yield SBOMComponent(
            name=name,
            ecosystem=ecosystem,
            version=version,
            purl=purl,
            spdx_id=spdx_id,
            raw_index=idx,
        )


def parse_sbom(path: str | Path) -> SBOMDocument:
    """Read an SBOM file from disk and return a parsed SBOMDocument.

    Raises ValueError if the file is not recognisable JSON in CycloneDX or
    SPDX format. Component scope: dependencies of the product, excluding the
    document-describes root package for SPDX (which represents the product
    being documented, not a dependency).
    """
    raw_text = Path(path).read_text(encoding="utf-8")
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"SBOM file is not valid JSON: {exc}") from exc

    fmt = _detect_format(raw)
    if fmt == "cyclonedx":
        spec_version = raw.get("specVersion", "")
        components = list(_iter_cyclonedx_components(raw))
    elif fmt == "spdx":
        spec_version = raw.get("spdxVersion", "")
        components = list(_iter_spdx_components(raw))
    else:
        raise ValueError(
            "Unrecognised SBOM format. Expected CycloneDX (bomFormat=CycloneDX) "
            "or SPDX (spdxVersion present)."
        )

    return SBOMDocument(format=fmt, spec_version=spec_version, components=components, raw=raw)


# --- Enrichment -----------------------------------------------------------

OSSUARY_PROPERTY_NAMESPACE = "ossuary:governance"


def _ossuary_properties(score_dict: dict) -> list[dict]:
    """Build the CycloneDX properties[] entries describing one component's score."""
    score = score_dict.get("score", {})
    final_score = score.get("final")
    risk_level = score.get("risk_level")
    metrics = score_dict.get("metrics", {})
    chaoss = score_dict.get("chaoss_signals", {})

    props = [
        {"name": f"{OSSUARY_PROPERTY_NAMESPACE}:score", "value": str(final_score)},
        {"name": f"{OSSUARY_PROPERTY_NAMESPACE}:risk_level", "value": str(risk_level)},
        {
            "name": f"{OSSUARY_PROPERTY_NAMESPACE}:concentration",
            "value": str(metrics.get("maintainer_concentration", "")),
        },
        {
            "name": f"{OSSUARY_PROPERTY_NAMESPACE}:bus_factor",
            "value": str(chaoss.get("bus_factor", "")),
        },
        {
            "name": f"{OSSUARY_PROPERTY_NAMESPACE}:commits_last_year",
            "value": str(metrics.get("commits_last_year", "")),
        },
    ]
    return props


def _ossuary_annotation(
    score_dict: dict,
    ossuary_version: str,
    spdx_id: Optional[str] = None,
) -> dict:
    """Build an SPDX annotation describing one component's Ossuary score.

    The annotation is emitted in the form mandated by the SPDX 2.3 JSON
    schema (annotationType, annotator, annotationDate, comment). It is
    embedded in the package's ``annotations`` array so that nesting
    establishes the link to the package — both forms (embedded and
    document-level) are valid per the JSON schema.

    The package's SPDXID is *also* included in the JSON comment payload
    so the link survives if a downstream tool extracts annotations from
    their packages and stores them standalone (e.g. logging, reporting,
    or document-level migration).
    """
    score = score_dict.get("score", {})
    metrics = score_dict.get("metrics", {})
    chaoss = score_dict.get("chaoss_signals", {})
    payload = {
        "spdx_id": spdx_id,
        "score": score.get("final"),
        "risk_level": score.get("risk_level"),
        "concentration": metrics.get("maintainer_concentration"),
        "bus_factor": chaoss.get("bus_factor"),
        "commits_last_year": metrics.get("commits_last_year"),
    }
    return {
        "annotationType": "REVIEW",
        "annotator": f"Tool: ossuary-{ossuary_version}",
        "annotationDate": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "comment": "ossuary-governance: " + json.dumps(payload, separators=(",", ":")),
    }


def enrich_cyclonedx(raw: dict, scores_by_index: dict[int, dict]) -> dict:
    """Return a copy of the CycloneDX SBOM with ossuary properties on scored components.

    ``scores_by_index`` is keyed by the position of the component in
    ``raw['components']`` and contains the full score dictionary returned by
    ``RiskBreakdown.to_dict()``. Existing properties are preserved; ossuary
    properties under the ``ossuary:governance`` namespace are replaced if
    re-enriching.
    """
    enriched = json.loads(json.dumps(raw))  # deep copy
    components = enriched.get("components", []) or []
    for idx, component in enumerate(components):
        score = scores_by_index.get(idx)
        if not score:
            continue
        existing = [
            p for p in (component.get("properties") or [])
            if not str(p.get("name", "")).startswith(OSSUARY_PROPERTY_NAMESPACE)
        ]
        component["properties"] = existing + _ossuary_properties(score)
    return enriched


def enrich_spdx(raw: dict, scores_by_index: dict[int, dict], ossuary_version: str) -> dict:
    """Return a copy of the SPDX SBOM with ossuary annotations on scored packages.

    ``scores_by_index`` is keyed by the position of the package in
    ``raw['packages']``. Existing annotations are preserved; previous Ossuary
    annotations (annotator starts with ``Tool: ossuary``) are dropped to keep
    re-enrichment idempotent.

    Annotations are written to ``packages[].annotations[]`` (per-package
    embedding form, valid in the SPDX 2.3 JSON schema) and additionally
    carry the package SPDXID inside their JSON comment payload for
    portability across tools that extract annotations standalone.
    """
    enriched = json.loads(json.dumps(raw))  # deep copy
    packages = enriched.get("packages", []) or []
    for idx, package in enumerate(packages):
        score = scores_by_index.get(idx)
        if not score:
            continue
        existing = [
            a for a in (package.get("annotations") or [])
            if not str(a.get("annotator", "")).startswith("Tool: ossuary")
        ]
        package["annotations"] = existing + [
            _ossuary_annotation(score, ossuary_version, spdx_id=package.get("SPDXID"))
        ]
    return enriched
