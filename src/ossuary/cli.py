"""Command-line interface for ossuary."""

import asyncio
import json
import sys
from datetime import datetime
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ossuary import __version__
from ossuary.collectors.git import GitCollector
from ossuary.collectors.github import GitHubCollector
from ossuary.collectors.npm import NpmCollector
from ossuary.collectors.pypi import PyPICollector
from ossuary.db.session import init_db
from ossuary.scoring.engine import PackageMetrics, RiskScorer
from ossuary.scoring.factors import RiskLevel
from ossuary.scoring.reputation import ReputationScorer
from ossuary.sentiment.analyzer import SentimentAnalyzer

app = typer.Typer(
    name="ossuary",
    help="OSS Supply Chain Risk Scoring - Where abandoned packages come to rest",
    add_completion=False,
)
console = Console()


def version_callback(value: bool):
    if value:
        console.print(f"ossuary version {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        None,
        "--version",
        "-v",
        callback=version_callback,
        is_eager=True,
        help="Show version and exit",
    ),
):
    """Ossuary - OSS Supply Chain Risk Scoring."""
    pass


@app.command()
def init():
    """Initialize the database."""
    console.print("Initializing database...")
    init_db()
    console.print("[green]Database initialized successfully[/green]")


@app.command()
def score(
    package: str = typer.Argument(..., help="Package name to analyze"),
    ecosystem: str = typer.Option("npm", "--ecosystem", "-e", help="Package ecosystem (npm, pypi)"),
    repo_url: Optional[str] = typer.Option(None, "--repo", "-r", help="Repository URL (auto-detected if not provided)"),
    cutoff_date: Optional[str] = typer.Option(None, "--cutoff", "-c", help="Cutoff date for T-1 analysis (YYYY-MM-DD)"),
    output_json: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Calculate risk score for a package."""
    asyncio.run(_score_package(package, ecosystem, repo_url, cutoff_date, output_json))


async def _score_package(
    package: str,
    ecosystem: str,
    repo_url: Optional[str],
    cutoff_date: Optional[str],
    output_json: bool,
):
    """Internal async function to score a package."""
    cutoff = None
    if cutoff_date:
        try:
            cutoff = datetime.strptime(cutoff_date, "%Y-%m-%d")
        except ValueError:
            console.print("[red]Invalid date format. Use YYYY-MM-DD[/red]")
            raise typer.Exit(1)

    with console.status(f"[bold blue]Analyzing {package}...[/bold blue]"):
        # Get package info
        if ecosystem == "npm":
            pkg_collector = NpmCollector()
            pkg_data = await pkg_collector.collect(package)
            await pkg_collector.close()
            if not repo_url:
                repo_url = pkg_data.repository_url
            weekly_downloads = pkg_data.weekly_downloads
        elif ecosystem == "pypi":
            pkg_collector = PyPICollector()
            pkg_data = await pkg_collector.collect(package)
            await pkg_collector.close()
            if not repo_url:
                repo_url = pkg_data.repository_url
            weekly_downloads = pkg_data.weekly_downloads
        else:
            console.print(f"[red]Unsupported ecosystem: {ecosystem}[/red]")
            raise typer.Exit(1)

        if not repo_url:
            console.print("[red]Could not find repository URL. Please provide with --repo[/red]")
            raise typer.Exit(1)

        console.print(f"  Repository: {repo_url}")

        # Collect git data
        console.print("  Collecting git history...")
        git_collector = GitCollector()
        git_metrics = await git_collector.collect(repo_url, cutoff)

        # Try to find top contributor's GitHub username from git email
        top_contributor_username = None
        if git_metrics.top_contributor_email:
            # Try to extract username from email (e.g., user@users.noreply.github.com)
            email = git_metrics.top_contributor_email
            if "noreply.github.com" in email:
                # Format: username@users.noreply.github.com or 12345+username@users.noreply.github.com
                parts = email.split("@")[0]
                if "+" in parts:
                    top_contributor_username = parts.split("+")[1]
                else:
                    top_contributor_username = parts
            # Otherwise we'll rely on the git author name or repo owner

        # Collect GitHub data (pass top contributor info to get correct maintainer data)
        console.print("  Collecting GitHub data...")
        github_collector = GitHubCollector()
        github_data = await github_collector.collect(
            repo_url,
            top_contributor_username=top_contributor_username,
            top_contributor_email=git_metrics.top_contributor_email,
        )
        await github_collector.close()

        # Parse account created date
        maintainer_account_created = None
        if github_data.maintainer_account_created:
            try:
                maintainer_account_created = datetime.fromisoformat(
                    github_data.maintainer_account_created.replace("Z", "+00:00")
                )
            except ValueError:
                pass

        # Calculate reputation score
        console.print("  Calculating reputation...")
        reputation_scorer = ReputationScorer()
        reputation = reputation_scorer.calculate(
            username=github_data.maintainer_username,
            account_created=maintainer_account_created,
            repos=github_data.maintainer_repos,
            sponsor_count=github_data.maintainer_sponsor_count,
            orgs=github_data.maintainer_orgs,
            packages_maintained=[package],  # At minimum, they maintain this package
            ecosystem=ecosystem,
        )

        # Run sentiment analysis
        console.print("  Analyzing sentiment...")
        sentiment_analyzer = SentimentAnalyzer()
        commit_sentiment = sentiment_analyzer.analyze_commits([c.message for c in git_metrics.commits])
        issue_sentiment = sentiment_analyzer.analyze_issues(
            [{"title": i.title, "body": i.body, "comments": i.comments} for i in github_data.issues]
        )

        # Aggregate sentiment
        total_frustration = commit_sentiment.frustration_count + issue_sentiment.frustration_count
        avg_sentiment = (commit_sentiment.average_compound + issue_sentiment.average_compound) / 2

        # Build metrics
        metrics = PackageMetrics(
            maintainer_concentration=git_metrics.maintainer_concentration,
            commits_last_year=git_metrics.commits_last_year,
            unique_contributors=git_metrics.unique_contributors,
            top_contributor_email=git_metrics.top_contributor_email,
            top_contributor_name=git_metrics.top_contributor_name,
            last_commit_date=git_metrics.last_commit_date,
            weekly_downloads=weekly_downloads,
            maintainer_username=github_data.maintainer_username,
            maintainer_public_repos=github_data.maintainer_public_repos,
            maintainer_total_stars=github_data.maintainer_total_stars,
            has_github_sponsors=github_data.has_github_sponsors,
            maintainer_account_created=maintainer_account_created,
            maintainer_repos=github_data.maintainer_repos,
            maintainer_sponsor_count=github_data.maintainer_sponsor_count,
            maintainer_orgs=github_data.maintainer_orgs,
            packages_maintained=[package],
            reputation=reputation,
            is_org_owned=github_data.is_org_owned,
            org_admin_count=github_data.org_admin_count,
            average_sentiment=avg_sentiment,
            frustration_detected=total_frustration > 0,
            frustration_evidence=commit_sentiment.frustration_evidence + issue_sentiment.frustration_evidence,
        )

        # Calculate score
        scorer = RiskScorer()
        breakdown = scorer.calculate(package, ecosystem, metrics, repo_url)

    # Output results
    if output_json:
        console.print(json.dumps(breakdown.to_dict(), indent=2))
    else:
        _display_results(breakdown, git_metrics, github_data, commit_sentiment, issue_sentiment)


def _display_results(breakdown, git_metrics, github_data, commit_sentiment, issue_sentiment):
    """Display results in a formatted way."""
    # Semaphore color
    color = {
        RiskLevel.CRITICAL: "red",
        RiskLevel.HIGH: "orange1",
        RiskLevel.MODERATE: "yellow",
        RiskLevel.LOW: "green",
        RiskLevel.VERY_LOW: "green",
    }[breakdown.risk_level]

    # Main score panel
    score_text = f"[bold {color}]{breakdown.risk_level.semaphore} {breakdown.final_score} - {breakdown.risk_level.value}[/bold {color}]"
    console.print(Panel(score_text, title=f"[bold]{breakdown.package_name}[/bold]", border_style=color))

    # Score breakdown table
    table = Table(title="Score Breakdown")
    table.add_column("Component", style="cyan")
    table.add_column("Value", style="magenta")
    table.add_column("Points", justify="right")

    table.add_row(
        "Base Risk (Concentration)",
        f"{breakdown.maintainer_concentration:.0f}%",
        f"{breakdown.base_risk:+d}",
    )
    table.add_row(
        "Activity Modifier",
        f"{breakdown.commits_last_year} commits/yr",
        f"{breakdown.activity_modifier:+d}",
    )
    table.add_row(
        "Protective Factors",
        "",
        f"{breakdown.protective_factors.total:+d}",
    )
    table.add_section()
    table.add_row("[bold]Final Score[/bold]", "", f"[bold]{breakdown.final_score}[/bold]")

    console.print(table)

    # Protective factors detail
    pf = breakdown.protective_factors
    pf_table = Table(title="Protective Factors Detail")
    pf_table.add_column("Factor", style="cyan")
    pf_table.add_column("Points", justify="right")
    pf_table.add_column("Evidence")

    if pf.reputation_score != 0:
        pf_table.add_row("Tier-1 Reputation", f"{pf.reputation_score:+d}", pf.reputation_evidence or "")
    if pf.funding_score != 0:
        pf_table.add_row("GitHub Sponsors", f"{pf.funding_score:+d}", pf.funding_evidence or "")
    if pf.org_score != 0:
        pf_table.add_row("Organization", f"{pf.org_score:+d}", f"{github_data.org_admin_count} admins")
    if pf.visibility_score != 0:
        pf_table.add_row("Visibility", f"{pf.visibility_score:+d}", f"{breakdown.weekly_downloads:,} downloads/wk")
    if pf.distributed_score != 0:
        pf_table.add_row("Distributed", f"{pf.distributed_score:+d}", f"<40% concentration")
    if pf.community_score != 0:
        pf_table.add_row("Community", f"{pf.community_score:+d}", f"{breakdown.unique_contributors} contributors")
    if pf.frustration_score != 0:
        pf_table.add_row(
            "[red]Frustration[/red]",
            f"[red]{pf.frustration_score:+d}[/red]",
            "; ".join(pf.frustration_evidence[:2]) if pf.frustration_evidence else "",
        )
    if pf.sentiment_score != 0:
        pf_table.add_row("Sentiment", f"{pf.sentiment_score:+d}", "")

    if pf_table.row_count > 0:
        console.print(pf_table)

    # Explanation
    console.print(f"\n[bold]Explanation:[/bold] {breakdown.explanation}")

    # Recommendations
    console.print("\n[bold]Recommendations:[/bold]")
    for rec in breakdown.recommendations:
        console.print(f"  • {rec}")


@app.command()
def check(
    packages_file: str = typer.Argument(..., help="JSON file with packages to check"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Output JSON file"),
):
    """Check multiple packages from a JSON file."""
    console.print(f"[yellow]Batch checking not yet implemented[/yellow]")
    raise typer.Exit(1)


@app.command()
def refresh(
    ecosystem: Optional[str] = typer.Option(None, "--ecosystem", "-e", help="Only refresh this ecosystem"),
    max_age: int = typer.Option(7, "--max-age", help="Re-score packages older than N days"),
):
    """Re-score all tracked packages. Intended for cron jobs."""
    asyncio.run(_refresh(ecosystem, max_age))


async def _refresh(ecosystem_filter: Optional[str], max_age: int):
    """Re-score tracked packages that are stale."""
    from ossuary.db.session import get_session
    from ossuary.db.models import Package, Score
    from ossuary.services.scorer import score_package as svc_score

    init_db()

    with next(get_session()) as session:
        query = session.query(Package)
        if ecosystem_filter:
            query = query.filter(Package.ecosystem == ecosystem_filter)
        packages = query.all()

    if not packages:
        console.print("No tracked packages found.")
        return

    stale = []
    now = datetime.utcnow()
    for pkg in packages:
        if pkg.last_analyzed is None:
            stale.append(pkg)
        else:
            age = (now - pkg.last_analyzed).days
            if age >= max_age:
                stale.append(pkg)

    console.print(f"Found {len(packages)} tracked packages, {len(stale)} need refresh (>{max_age} days old).")

    if not stale:
        console.print("[green]All packages are fresh.[/green]")
        return

    success = 0
    errors = 0
    for i, pkg in enumerate(stale, 1):
        console.print(f"  [{i}/{len(stale)}] {pkg.name} ({pkg.ecosystem})...", end=" ")
        result = await svc_score(pkg.name, pkg.ecosystem, repo_url=pkg.repo_url)
        if result.success:
            console.print(f"[green]{result.breakdown.final_score} {result.breakdown.risk_level.value}[/green]")
            success += 1
        else:
            console.print(f"[red]ERROR: {result.error}[/red]")
            errors += 1

    console.print(f"\nDone. {success} refreshed, {errors} errors.")


if __name__ == "__main__":
    app()
