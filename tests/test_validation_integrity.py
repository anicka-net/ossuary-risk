"""Publication-quality gates for the validation runner."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ossuary.collectors.github import GitHubData
from ossuary.scoring.factors import ProtectiveFactors, RiskBreakdown, RiskLevel
from ossuary.services.scorer import CollectedData

_VALIDATE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "validate.py"
_SPEC = importlib.util.spec_from_file_location("ossuary_validation_test", _VALIDATE_PATH)
assert _SPEC and _SPEC.loader
validation = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = validation
_SPEC.loader.exec_module(validation)


def test_artifact_separates_errors_and_provisional_rows():
    complete = validation.ValidationResult(
        case=validation.ValidationCase("safe", "npm", "safe"),
        score=10,
        predicted_outcome="safe",
        correct=True,
        classification="TN",
    )
    provisional = validation.ValidationResult(
        case=validation.ValidationCase(
            "incident", "npm", "incident", tier="T1"
        ),
        score=80,
        predicted_outcome="risky",
        correct=True,
        classification="TP",
        is_provisional=True,
        provisional_reasons=["github.issues: HTTP 502"],
    )
    failed = validation.ValidationResult(
        case=validation.ValidationCase("failed", "npm", "safe"),
        error="npm.package_info: HTTP 503",
    )

    summary = validation.calculate_summary([complete, provisional, failed])
    artifact = validation.build_artifact(
        summary,
        {"npm": {"total": 1, "correct": 1}},
        validation_cutoff_date="2026-08-15",
        run_started_at=datetime(2026, 8, 15, 10, 0),
    )

    assert summary.total == 1
    assert artifact["validation_cutoff_date"] == "2026-08-15"
    assert artifact["dataset"] == {
        "requested_cases": 3,
        "total_cases": 1,
        "controls": 1,
        "incidents": 0,
        "in_scope_incidents": 0,
        "errors": 1,
        "provisional_results": 1,
        "pinned_evidence_cases": 0,
    }
    assert artifact["scopes"]["scope_b"]["total_evaluated"] == 1
    assert artifact["results"][1]["is_provisional"] is True


@pytest.mark.asyncio
async def test_validate_package_propagates_provisional_state_and_refresh():
    case = validation.ValidationCase("demo", "npm", "safe")
    data = CollectedData(
        repo_url="https://github.com/example/demo",
        all_commits=[],
        github_data=GitHubData(maintainer_username="alice"),
        weekly_downloads=0,
        maintainer_account_created=None,
    )
    breakdown = RiskBreakdown(
        package_name="demo",
        ecosystem="npm",
        final_score=10,
        risk_level=RiskLevel.VERY_LOW,
        protective_factors=ProtectiveFactors(
            reputation_score=-25,
            reputation_evidence=(
                "alice: 60 pts (TIER_1) - tenure=15, portfolio=15, "
                "stars=15, sponsors=0, packages=0, top_packages=15, "
                "organizations=0"
            ),
        ),
        provisional_reasons=["github.issues: HTTP 502"],
    )
    collect = AsyncMock(return_value=(data, []))

    with patch.object(validation, "cached_collect", collect), patch.object(
        validation, "calculate_score_for_date", return_value=breakdown
    ):
        result = await validation.validate_package(
            case,
            current_cutoff=datetime(2026, 8, 15, 10, 0),
            refresh_data=True,
        )

    assert result.is_provisional is True
    assert result.provisional_reasons == ["github.issues: HTTP 502"]
    assert result.reputation_score == -25
    assert result.reputation_tier == "TIER_1"
    assert result.classification == "TN"
    assert collect.await_args.kwargs["refresh_data"] is True


@pytest.mark.asyncio
async def test_named_incident_cutoff_is_inclusive_and_explicitly_historical():
    case = validation.ValidationCase(
        "demo",
        "npm",
        "incident",
        cutoff_date="2026-08-14",
    )
    data = CollectedData(
        repo_url="https://github.com/example/demo",
        all_commits=[],
        github_data=GitHubData(),
        weekly_downloads=0,
        maintainer_account_created=None,
    )
    breakdown = RiskBreakdown(
        package_name="demo",
        ecosystem="npm",
        final_score=50,
        risk_level=RiskLevel.MODERATE,
    )
    collect = AsyncMock(return_value=(data, []))

    with patch.object(validation, "cached_collect", collect), patch.object(
        validation, "calculate_score_for_date", return_value=breakdown
    ) as calculate:
        await validation.validate_package(case)

    expected = datetime(2026, 8, 14, 23, 59, 59, 999999)
    assert collect.await_args.kwargs["cutoff_date"] == expected
    assert calculate.call_args.args[3] == expected
    assert calculate.call_args.kwargs["is_historical"] is True


@pytest.mark.asyncio
async def test_main_initializes_empty_database(monkeypatch):
    case = validation.ValidationCase("demo", "npm", "safe")
    complete = validation.ValidationResult(
        case=case,
        score=10,
        risk_level="VERY_LOW",
        predicted_outcome="safe",
        correct=True,
        classification="TN",
    )
    monkeypatch.setattr(validation, "VALIDATION_CASES", [case])
    monkeypatch.setattr(sys, "argv", ["validate.py"])

    with patch.object(validation, "init_db") as init, patch.object(
        validation, "validate_package", AsyncMock(return_value=complete)
    ):
        await validation.main()

    init.assert_called_once_with()


@pytest.mark.asyncio
async def test_filtered_run_cannot_target_canonical_artifact(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "validate.py",
            "--package",
            "demo",
            "--output",
            str(validation.REPO_ROOT / "validation_results.json"),
        ],
    )

    with pytest.raises(SystemExit) as exc:
        await validation.main()

    assert exc.value.code == 2


@pytest.mark.asyncio
async def test_allow_incomplete_cannot_bypass_canonical_gate(monkeypatch):
    case = validation.ValidationCase("demo", "npm", "safe")
    provisional = validation.ValidationResult(
        case=case,
        score=10,
        risk_level="VERY_LOW",
        predicted_outcome="safe",
        correct=True,
        classification="TN",
        is_provisional=True,
        provisional_reasons=["github.issues: HTTP 502"],
    )
    monkeypatch.setattr(validation, "VALIDATION_CASES", [case])
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "validate.py",
            "--allow-incomplete",
            "--output",
            str(validation.REPO_ROOT / "validation_results.json"),
        ],
    )

    with patch.object(validation, "init_db"), patch.object(
        validation,
        "validate_package",
        AsyncMock(return_value=provisional),
    ), patch("builtins.open") as output_file, pytest.raises(SystemExit) as exc:
        await validation.main()

    assert exc.value.code == 1
    output_file.assert_not_called()


@pytest.mark.asyncio
async def test_pyphetools_uses_pinned_original_repository_evidence():
    case = next(
        case
        for case in validation.VALIDATION_CASES
        if case.name == "monarch-initiative/pyphetools"
    )

    with patch.object(
        validation,
        "cached_collect",
        AsyncMock(side_effect=AssertionError("fixture case must not call upstream")),
    ):
        result = await validation.validate_package(case, refresh_data=True)

    assert case.evidence_fixture == (
        "validation_fixtures/pyphetools-2026-06-10.json"
    )
    assert result.error is None
    assert result.is_provisional is False
    assert result.score == 50
    assert result.concentration == pytest.approx(83.15867710258753)
    assert result.commits_last_year == 12
    assert result.classification == "FN"


def test_pyphetools_fixture_records_archive_overlap():
    case = next(
        case
        for case in validation.VALIDATION_CASES
        if case.name == "monarch-initiative/pyphetools"
    )
    fixture_path = validation.REPO_ROOT / case.evidence_fixture
    payload = json.loads(fixture_path.read_text())

    assert len(payload["collected_data"]["all_commits"]) == 533
    corroboration = payload["provenance"]["archive_corroboration"]
    assert corroboration["matching_commit_objects"] == 517
    assert corroboration["fixture_commit_objects"] == 533
    assert corroboration["archive_only_commit_objects"] == 0


def test_canonical_artifact_has_utc_metadata_and_real_reputation_factors():
    payload = json.loads(
        (validation.REPO_ROOT / "validation_results.json").read_text()
    )

    assert payload["timestamp"].endswith("Z")
    assert payload["run_started_at"].endswith("Z")
    assert datetime.fromisoformat(payload["timestamp"].replace("Z", "+00:00"))
    assert datetime.fromisoformat(
        payload["run_started_at"].replace("Z", "+00:00")
    )

    expected_factor = {"": 0, "TIER_1": -25, "TIER_2": -10}
    assert all(
        row["reputation_score"] == expected_factor[row["reputation_tier"]]
        for row in payload["results"]
    )


def test_ppkt2synergy_note_matches_current_artifact_score():
    payload = json.loads(
        (validation.REPO_ROOT / "validation_results.json").read_text()
    )
    row = next(
        item
        for item in payload["results"]
        if item["case"]["name"] == "P2GX/ppkt2synergy"
    )

    assert row["score"] == 25
    assert "base scores 25" in row["case"]["notes"]
