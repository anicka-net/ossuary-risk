"""Risk scoring engine implementation."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from ossuary.scoring.factors import ProtectiveFactors, RiskBreakdown, RiskLevel
from ossuary.scoring.methodology import FRUSTRATION_WEIGHT
from ossuary.scoring.reputation import ReputationBreakdown, ReputationScorer


@dataclass
class PackageMetrics:
    """Collected metrics for a package."""

    # Core metrics from git history
    maintainer_concentration: float = 0.0
    commits_last_year: int = 0
    unique_contributors: int = 0
    top_contributor_email: str = ""
    top_contributor_name: str = ""
    last_commit_date: Optional[datetime] = None

    # External API data
    weekly_downloads: int = 0
    repo_stargazers: int = 0  # GitHub stars (visibility proxy when no download data)

    # Maintainer info (basic)
    maintainer_username: Optional[str] = None
    maintainer_public_repos: int = 0
    maintainer_total_stars: int = 0
    has_github_sponsors: bool = False

    # Reputation data (for composite scoring)
    maintainer_account_created: Optional[datetime] = None
    maintainer_repos: list[dict] = None  # Full repo data
    maintainer_sponsor_count: int = 0
    maintainer_orgs: list[str] = None
    packages_maintained: list[str] = None  # Packages by this maintainer

    # Computed reputation
    reputation: Optional[ReputationBreakdown] = None

    # Repository info
    is_org_owned: bool = False
    org_admin_count: int = 0
    cii_badge_level: str = "none"

    # Maturity detection
    total_commits: int = 0
    first_commit_date: Optional[datetime] = None
    lifetime_contributors: int = 0
    lifetime_concentration: float = 0.0
    is_mature: bool = False
    repo_age_years: float = 0.0
    bus_factor: int = 0
    elephant_factor: int = 0
    inactive_contributor_ratio: float = 0.0
    takeover_shift: float = 0.0
    takeover_suspect: str = ""
    takeover_suspect_name: str = ""

    # Sentiment analysis results
    average_sentiment: float = 0.0
    frustration_detected: bool = False
    frustration_evidence: list[str] = None

    def __post_init__(self):
        if self.frustration_evidence is None:
            self.frustration_evidence = []
        if self.maintainer_repos is None:
            self.maintainer_repos = []
        if self.maintainer_orgs is None:
            self.maintainer_orgs = []
        if self.packages_maintained is None:
            self.packages_maintained = []


class RiskScorer:
    """
    Risk scoring engine implementing the ossuary methodology.

    Score = Base Risk + Activity Modifier + Protective Factors
    Range: 0-100 (higher = riskier)
    """

    # Tier-1 maintainer thresholds
    TIER1_REPOS_THRESHOLD = 500
    TIER1_STARS_THRESHOLD = 100_000

    # Download thresholds for visibility factor
    MASSIVE_VISIBILITY_THRESHOLD = 50_000_000
    HIGH_VISIBILITY_THRESHOLD = 10_000_000

    # Stars thresholds (visibility proxy for repos without download data)
    MASSIVE_STARS_THRESHOLD = 50_000
    HIGH_STARS_THRESHOLD = 10_000

    def calculate_base_risk(self, concentration: float, bus_factor: int = 0) -> int:
        """
        Calculate base risk from maintainer concentration and bus factor.

        Uses the worse (higher risk) of two signals:
        - Top-1 concentration: how dominant is the single top contributor?
        - Bus factor: how many people needed for 50% of commits?

        A project like trivy (18% top-1, but bus factor 3) has low
        concentration risk but real bus factor risk — 3 people leaving
        would lose half the project's development capacity.

        Args:
            concentration: Percentage of commits from top contributor (0-100)
            bus_factor: Minimum contributors for 50% of commits (0 = unknown)

        Returns:
            Base risk score (20-100)
        """
        # Risk from top-1 concentration
        if concentration < 30:
            conc_risk = 20
        elif concentration < 50:
            conc_risk = 40
        elif concentration < 70:
            conc_risk = 60
        elif concentration < 90:
            conc_risk = 80
        else:
            conc_risk = 100

        # Risk from bus factor (CHAOSS metric)
        # The bus factor catches cases concentration misses: e.g. trivy has
        # 18% top-1 (looks distributed) but bus factor 3 (just 3 people for
        # 50% of commits). However, bf=1 shouldn't override concentration —
        # if one person does 65% of commits, concentration already captures
        # that at base 60. We only use bus factor to RAISE the floor when
        # concentration is misleadingly low.
        # bus_factor=0 means no recent human commits — don't apply
        if bus_factor <= 0:
            bf_risk = 0
        elif bus_factor == 1:
            bf_risk = 60   # Single person dominates, but conc already captures this
        elif bus_factor == 2:
            bf_risk = 40   # Two people control the project
        elif bus_factor <= 5:
            bf_risk = 40   # Small group, moderate risk
        else:
            bf_risk = 20   # Well-distributed

        # Use the worse signal — a project can look distributed by top-1
        # concentration but still have a dangerously low bus factor
        return max(conc_risk, bf_risk)

    def calculate_activity_modifier(self, commits_last_year: int) -> int:
        """
        Calculate activity modifier from commit frequency.

        Args:
            commits_last_year: Number of commits in the last 12 months

        Returns:
            Activity modifier (-30 to +20)
        """
        if commits_last_year > 50:
            return -30  # Active: reduces risk significantly
        elif commits_last_year >= 12:
            return -15  # Moderate: reduces risk somewhat
        elif commits_last_year >= 4:
            return 0  # Low: neutral
        else:
            return 20  # Abandoned: increases risk critically

    def calculate_protective_factors(
        self, metrics: PackageMetrics, ecosystem: str = "npm"
    ) -> ProtectiveFactors:
        """
        Calculate all protective factors.

        Args:
            metrics: Collected package metrics
            ecosystem: Package ecosystem for reputation lookup

        Returns:
            ProtectiveFactors breakdown
        """
        pf = ProtectiveFactors()

        # Factor 1: Maintainer Reputation (composite score)
        if metrics.reputation:
            # Use pre-calculated reputation
            reputation = metrics.reputation
        else:
            # Calculate reputation on the fly
            reputation_scorer = ReputationScorer()
            reputation = reputation_scorer.calculate(
                username=metrics.maintainer_username or "",
                account_created=metrics.maintainer_account_created,
                repos=metrics.maintainer_repos,
                sponsor_count=metrics.maintainer_sponsor_count,
                orgs=metrics.maintainer_orgs,
                packages_maintained=metrics.packages_maintained,
                ecosystem=ecosystem,
            )

        pf.reputation_score = reputation.tier.risk_reduction
        if pf.reputation_score != 0:
            pf.reputation_evidence = reputation.summary()

        # Factor 2: Economic Sustainability (-15)
        if metrics.has_github_sponsors:
            pf.funding_score = -15
            pf.funding_evidence = "GitHub Sponsors enabled"

        # Factor 3: Organization Ownership (-15)
        if metrics.is_org_owned and metrics.org_admin_count >= 3:
            pf.org_score = -15

        # Factor 4: Visibility (-10 to -20)
        # Use download counts when available (npm/pypi), fall back to GitHub stars
        if metrics.weekly_downloads > self.MASSIVE_VISIBILITY_THRESHOLD:
            pf.visibility_score = -20
        elif metrics.weekly_downloads > self.HIGH_VISIBILITY_THRESHOLD:
            pf.visibility_score = -10
        elif metrics.weekly_downloads == 0 and metrics.repo_stargazers > 0:
            # Stars-based proxy for repos without download data
            if metrics.repo_stargazers > self.MASSIVE_STARS_THRESHOLD:
                pf.visibility_score = -20
            elif metrics.repo_stargazers > self.HIGH_STARS_THRESHOLD:
                pf.visibility_score = -10

        # Factor 5: Distributed Governance (-10)
        # Require enough commits to draw meaningful conclusions about distribution
        if metrics.maintainer_concentration < 40 and metrics.commits_last_year >= 10:
            pf.distributed_score = -10

        # Factor 6: Active Community (-10)
        if metrics.unique_contributors > 20:
            pf.community_score = -10

        # Factor 7: CII Best Practices (-10)
        if metrics.cii_badge_level in ("gold", "silver", "passing"):
            pf.cii_score = -10

        # Factor 8: Economic Frustration (+FRUSTRATION_WEIGHT)
        # Weight lowered from +20 to +15 in v6.3 after the §5.10 ablation: at
        # +20 the floor leaked one residual FP (rayon, cargo) without earning
        # recall; at +15 the same rule-set is precision-positive and the
        # bounded instrument frame is preserved.
        if metrics.frustration_detected:
            pf.frustration_score = FRUSTRATION_WEIGHT
            pf.frustration_evidence = metrics.frustration_evidence

        # Factor 9: Sentiment Analysis — no score contribution as of v6.3.
        # The §5.10 ablation found 0/167 packages cross the ±0.3 threshold,
        # so the VADER magnitude signal never participated in the score on
        # the validation set. The rule-based frustration layer captures the
        # detectable emotional signal; the deferred layer-3 embedding work
        # (§6.5) is what would make a sentiment factor earn its place again.
        # Field retained on ProtectiveFactors as structurally 0 to keep
        # cached-score deserialization stable.

        # Factor 10: Project Maturity — evidence only, not a score contribution.
        # maturity_score is structurally 0 (see ProtectiveFactors docstring);
        # the actual maturity mechanism is in calculate(): activity-penalty
        # suppression and lifetime-concentration fallback. We populate evidence
        # here so the explanation / dashboard can surface that a project was
        # treated as mature without pretending a protective-factor score fired.
        if metrics.is_mature:
            pf.maturity_evidence = (
                f"Stable project: {metrics.total_commits} commits over "
                f"{metrics.repo_age_years:.0f} years, "
                f"{metrics.lifetime_contributors} lifetime contributors"
            )

        # Factor 11: Takeover Risk (+20) — xz-utils proportion shift detection
        # Flags when a minor historical contributor suddenly dominates recent commits.
        # Threshold: >30% shift AND >40% of recent commits from that contributor.
        if (
            metrics.is_mature
            and metrics.takeover_shift > 30
        ):
            pf.takeover_risk_score = 20
            suspect = metrics.takeover_suspect_name or metrics.takeover_suspect
            pf.takeover_risk_evidence = (
                f"{suspect}: {metrics.takeover_shift:+.0f}pp shift in commit share "
                f"on mature project (xz-utils pattern)"
            )

        return pf

    def generate_explanation(self, breakdown: RiskBreakdown, metrics: PackageMetrics = None) -> str:
        """Generate human-readable explanation of the score."""
        parts = []

        # Maturity context (comes first if applicable)
        if metrics and metrics.is_mature:
            parts.append(
                f"Mature project ({metrics.repo_age_years:.0f} years, "
                f"{metrics.lifetime_contributors} lifetime contributors)"
            )

        # Concentration explanation
        conc = breakdown.maintainer_concentration
        if metrics and metrics.is_mature:
            # For mature projects, explain we're using lifetime concentration
            lt_conc = metrics.lifetime_concentration
            if lt_conc >= 90:
                parts.append(f"Single-maintainer lifetime ({lt_conc:.0f}% lifetime concentration)")
            elif lt_conc >= 50:
                parts.append(f"Moderately concentrated lifetime ({lt_conc:.0f}% lifetime)")
            else:
                parts.append(f"Distributed lifetime contributors ({lt_conc:.0f}% lifetime)")
        else:
            if conc >= 90:
                parts.append(f"Critical concentration ({conc:.0f}%): single person controls nearly all commits")
            elif conc >= 70:
                parts.append(f"High concentration ({conc:.0f}%): majority of commits from one person")
            elif conc >= 50:
                parts.append(f"Moderate concentration ({conc:.0f}%): some bus factor risk")
            else:
                parts.append(f"Distributed commits ({conc:.0f}%): healthy contributor diversity")

        # Activity explanation
        if breakdown.activity_modifier == 20:
            parts.append("Project appears abandoned (<4 commits/year)")
        elif breakdown.activity_modifier == -30:
            parts.append("Actively maintained (>50 commits/year)")
        elif breakdown.activity_modifier == -15:
            parts.append("Moderately active (12-50 commits/year)")
        elif breakdown.activity_modifier == 0:
            if metrics and metrics.is_mature and metrics.commits_last_year < 4:
                parts.append("Low recent activity (expected for mature project)")
            else:
                parts.append("Low activity (4-11 commits/year)")

        # Protective factors summary
        pf_total = breakdown.protective_factors.total
        if pf_total < -30:
            parts.append(f"Strong protective factors ({pf_total:+d} points)")
        elif pf_total < 0:
            parts.append(f"Some protective factors ({pf_total:+d} points)")
        elif pf_total > 0:
            parts.append(f"Warning signals present ({pf_total:+d} points)")

        # Frustration alert
        if breakdown.protective_factors.frustration_score > 0:
            parts.append("ALERT: Economic frustration signals detected")

        # Takeover alert
        if breakdown.protective_factors.takeover_risk_score > 0:
            parts.append("ALERT: Newcomer takeover pattern detected on mature project")

        return f"{breakdown.risk_level.semaphore} {breakdown.risk_level.value} ({breakdown.final_score}). " + ". ".join(
            parts
        )

    def generate_recommendations(self, breakdown: RiskBreakdown) -> list[str]:
        """Generate actionable recommendations based on the score."""
        recs = []

        if breakdown.final_score >= 80:
            recs.append("IMMEDIATE: Identify alternative packages or prepare to fork")
            recs.append("Do not accept new versions without manual code review")
            recs.append("Monitor for maintainer changes or ownership transfers")
        elif breakdown.final_score >= 60:
            recs.append("Review new releases carefully before updating")
            recs.append("Consider contributing to reduce maintainer concentration")
            recs.append("Monitor project health metrics monthly")
        elif breakdown.final_score >= 40:
            recs.append("Standard monitoring recommended")
            recs.append("Keep dependencies updated")
        else:
            recs.append("Low risk - standard dependency management practices apply")

        # Specific recommendations
        if breakdown.protective_factors.frustration_score > 0:
            recs.insert(0, "URGENT: Maintainer frustration detected - elevated sabotage risk")

        if breakdown.maintainer_concentration > 90 and breakdown.commits_last_year < 10:
            recs.insert(0, "HIGH PRIORITY: Single maintainer + low activity = prime takeover target")

        # Takeover-specific recommendations
        if breakdown.protective_factors.takeover_risk_score > 0:
            recs.insert(0, "ALERT: New contributor dominates recent commits on mature project — review carefully (xz-utils pattern)")

        # Mature project recommendation — triggered by the evidence string,
        # not by a (structurally zero) maturity_score.
        if (
            breakdown.protective_factors.maturity_evidence
            and breakdown.final_score is not None
            and breakdown.final_score < 40
        ):
            recs.append("Stable mature project — standard monitoring sufficient")

        return recs

    def calculate(
        self,
        package_name: str,
        ecosystem: str,
        metrics: PackageMetrics,
        repo_url: Optional[str] = None,
    ) -> RiskBreakdown:
        """
        Calculate complete risk score for a package.

        Args:
            package_name: Name of the package
            ecosystem: Package ecosystem (npm, pypi)
            metrics: Collected package metrics
            repo_url: Repository URL (optional)

        Returns:
            Complete RiskBreakdown
        """
        breakdown = RiskBreakdown(
            package_name=package_name,
            ecosystem=ecosystem,
            repo_url=repo_url,
        )

        # Copy metrics
        breakdown.maintainer_concentration = metrics.maintainer_concentration
        breakdown.bus_factor = metrics.bus_factor
        breakdown.elephant_factor = metrics.elephant_factor
        breakdown.inactive_contributor_ratio = metrics.inactive_contributor_ratio
        breakdown.commits_last_year = metrics.commits_last_year
        breakdown.unique_contributors = metrics.unique_contributors
        breakdown.weekly_downloads = metrics.weekly_downloads

        # Calculate components — two-track scoring for mature projects
        if metrics.is_mature:
            # Mature projects with some activity (1-3 commits/yr): suppress
            # activity penalty, fall back to lifetime concentration (recent
            # data from 1-3 commits is unreliable).
            # Mature projects with ZERO activity: truly abandoned — apply
            # full penalty and use default 100% concentration (no recent data).
            if metrics.commits_last_year == 0:
                # Zero activity = abandoned, even if historically mature.
                # Don't reward a project nobody's home for.
                breakdown.base_risk = self.calculate_base_risk(metrics.maintainer_concentration, metrics.bus_factor)
                breakdown.activity_modifier = self.calculate_activity_modifier(0)
            elif metrics.commits_last_year < 4:
                breakdown.base_risk = self.calculate_base_risk(metrics.lifetime_concentration, metrics.bus_factor)
                raw_activity = self.calculate_activity_modifier(metrics.commits_last_year)
                breakdown.activity_modifier = min(0, raw_activity)
            else:
                breakdown.base_risk = self.calculate_base_risk(metrics.maintainer_concentration, metrics.bus_factor)
                raw_activity = self.calculate_activity_modifier(metrics.commits_last_year)
                breakdown.activity_modifier = min(0, raw_activity)
        else:
            breakdown.base_risk = self.calculate_base_risk(metrics.maintainer_concentration, metrics.bus_factor)
            breakdown.activity_modifier = self.calculate_activity_modifier(metrics.commits_last_year)

        breakdown.protective_factors = self.calculate_protective_factors(metrics, ecosystem)

        # When a takeover pattern is detected, high commit activity is part of
        # the attack — don't let the activity bonus cancel the takeover signal.
        if breakdown.protective_factors.takeover_risk_score > 0 and breakdown.activity_modifier < 0:
            breakdown.activity_modifier = 0

        # Calculate final score (clamped to 0-100)
        raw_score = breakdown.base_risk + breakdown.activity_modifier + breakdown.protective_factors.total
        breakdown.final_score = max(0, min(100, raw_score))

        # Determine risk level
        breakdown.risk_level = RiskLevel.from_score(breakdown.final_score)

        # Generate explanation and recommendations
        breakdown.explanation = self.generate_explanation(breakdown, metrics)
        breakdown.recommendations = self.generate_recommendations(breakdown)

        return breakdown
