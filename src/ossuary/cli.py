"""Command-line interface for ossuary."""

import asyncio
import json
import os
import sys
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ossuary import __version__
from ossuary.db.session import init_db
from ossuary.scoring.factors import RiskLevel

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


SUPPORTED_ECOSYSTEMS = ["npm", "pypi", "cargo", "rubygems", "packagist", "nuget", "go", "github"]


@app.command()
def score(
    package: str = typer.Argument(..., help="Package name to analyze"),
    ecosystem: str = typer.Option("github", "--ecosystem", "-e", help="Package ecosystem (npm, pypi, cargo, rubygems, packagist, nuget, go, github)"),
    repo_url: Optional[str] = typer.Option(None, "--repo", "-r", help="Repository URL (auto-detected if not provided)"),
    cutoff_date: Optional[str] = typer.Option(None, "--cutoff", "-c", help="Cutoff date for T-1 analysis (YYYY-MM-DD)"),
    output_json: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Calculate risk score for a package."""
    eco = ecosystem.lower()
    if eco not in SUPPORTED_ECOSYSTEMS:
        console.print(f"[red]Unsupported ecosystem: {ecosystem}[/red]")
        console.print(f"Supported: {', '.join(SUPPORTED_ECOSYSTEMS)}")
        raise typer.Exit(1)

    # Catch common mistake: bare package name with default github ecosystem
    if eco == "github" and "/" not in package and not repo_url:
        console.print(f"[red]Cannot score '{package}' as a GitHub repo — expected owner/repo format.[/red]")
        console.print()
        console.print("Either provide the full GitHub path:")
        console.print(f"  ossuary score [bold]pandas-dev/pandas[/bold]")
        console.print()
        console.print("Or specify the package ecosystem:")
        console.print(f"  ossuary score {package} [bold]-e pypi[/bold]")
        console.print(f"  ossuary score {package} [bold]-e npm[/bold]")
        console.print(f"  ossuary score {package} [bold]-e cargo[/bold]")
        console.print()
        console.print(f"[dim]Supported ecosystems: {', '.join(SUPPORTED_ECOSYSTEMS)}[/dim]")
        raise typer.Exit(1)

    asyncio.run(_score_package(package, eco, repo_url, cutoff_date, output_json))


async def _score_package(
    package: str,
    ecosystem: str,
    repo_url: Optional[str],
    cutoff_date: Optional[str],
    output_json: bool,
):
    """Internal async function to score a package."""
    from ossuary.services.scorer import score_package as svc_score

    init_db()

    cutoff = None
    if cutoff_date:
        try:
            cutoff = datetime.strptime(cutoff_date, "%Y-%m-%d")
        except ValueError:
            console.print("[red]Invalid date format. Use YYYY-MM-DD[/red]")
            raise typer.Exit(1)

    with console.status(f"[bold blue]Analyzing {package} ({ecosystem})...[/bold blue]"):
        result = await svc_score(
            package, ecosystem, repo_url=repo_url, cutoff_date=cutoff, force=True,
        )

    if not result.success:
        console.print(f"[red]Error: {result.error}[/red]")
        raise typer.Exit(1)

    breakdown = result.breakdown

    if result.warnings:
        for w in result.warnings:
            console.print(f"[yellow]Warning: {w}[/yellow]")

    # Output results
    if output_json:
        console.print(json.dumps(breakdown.to_dict(), indent=2))
    else:
        _display_results(breakdown)


def _display_results(breakdown):
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
        pf_table.add_row("Organization", f"{pf.org_score:+d}", "org-owned")
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
    if pf.maturity_score != 0:
        pf_table.add_row(
            "[green]Maturity[/green]",
            f"[green]{pf.maturity_score:+d}[/green]",
            pf.maturity_evidence or "",
        )
    if pf.takeover_risk_score != 0:
        pf_table.add_row(
            "[red]Takeover Risk[/red]",
            f"[red]{pf.takeover_risk_score:+d}[/red]",
            pf.takeover_risk_evidence or "",
        )

    if pf_table.row_count > 0:
        console.print(pf_table)

    # Explanation
    console.print(f"\n[bold]Explanation:[/bold] {breakdown.explanation}")

    # Recommendations
    console.print("\n[bold]Recommendations:[/bold]")
    for rec in breakdown.recommendations:
        console.print(f"  • {rec}")


@app.command()
def scan(
    file: str = typer.Argument(..., help="Dependency file to scan (requirements.txt, pyproject.toml, package.json, Cargo.toml, go.mod, Gemfile, composer.json, *.csproj)"),
    output: Optional[str] = typer.Option(None, "-o", "--output", help="Output JSON report file"),
    ecosystem: Optional[str] = typer.Option(None, "-e", "--ecosystem", help="Override ecosystem detection"),
    concurrent: int = typer.Option(3, "-c", "--concurrent", help="Parallel scoring workers"),
    limit: int = typer.Option(0, "-l", "--limit", help="Score first N packages (0=all)"),
    no_dev: bool = typer.Option(False, "--no-dev", help="Skip dev dependencies"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON to stdout"),
):
    """Scan a dependency file and score all packages.

    Automatically detects the ecosystem from the filename.
    Use -e to override for non-standard filenames.
    """
    asyncio.run(_scan(file, output, ecosystem, concurrent, limit, no_dev, json_output))


async def _scan(
    file: str,
    output: Optional[str],
    ecosystem_override: Optional[str],
    concurrent: int,
    limit: int,
    no_dev: bool,
    json_output: bool,
):
    """Score all packages from a dependency file."""
    from ossuary.services.batch import parse_dependency_file
    from ossuary.services.scorer import score_package as svc_score

    init_db()

    if not os.path.exists(file):
        console.print(f"[red]File not found: {file}[/red]")
        raise typer.Exit(1)

    try:
        eco, entries = parse_dependency_file(file, ecosystem_override, include_dev=not no_dev)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    if not entries:
        console.print("[yellow]No packages found in file.[/yellow]")
        raise typer.Exit(0)

    if limit > 0:
        entries = entries[:limit]

    console.print(f"\n[bold]Scanning {file}[/bold] ({eco}, {len(entries)} packages)\n")

    # Score all packages, collecting results
    semaphore = asyncio.Semaphore(concurrent)
    results = []

    async def score_one(entry):
        async with semaphore:
            try:
                result = await svc_score(entry.obs_package, entry.ecosystem, force=True)
                return entry.obs_package, result
            except Exception as e:
                from ossuary.services.scorer import ScoringResult
                return entry.obs_package, ScoringResult(success=False, error=str(e))

    tasks = [score_one(e) for e in entries]
    scored = 0
    errors = 0

    for coro in asyncio.as_completed(tasks):
        name, result = await coro
        scored += 1
        if result.success:
            b = result.breakdown
            color = {
                "CRITICAL": "red",
                "HIGH": "orange1",
                "MODERATE": "yellow",
                "LOW": "green",
                "VERY_LOW": "green",
            }.get(b.risk_level.value, "white")
            console.print(
                f"  [{scored}/{len(entries)}] [{color}]{b.final_score:3d} {b.risk_level.value:8s}[/{color}] {name}"
            )
        else:
            errors += 1
            console.print(f"  [{scored}/{len(entries)}] [red]ERROR[/red] {name}: {result.error}")
        results.append((name, result))

    # Sort by score descending for summary
    scored_results = [(n, r) for n, r in results if r.success]
    scored_results.sort(key=lambda x: -x[1].breakdown.final_score)
    error_results = [(n, r) for n, r in results if not r.success]

    # Summary table
    if scored_results:
        console.print()
        table = Table(title="Risk Summary")
        table.add_column("Package", style="cyan", min_width=20)
        table.add_column("Score", justify="right")
        table.add_column("Risk", min_width=10)
        table.add_column("Concentration", justify="right")
        table.add_column("Commits/yr", justify="right")

        for name, result in scored_results:
            b = result.breakdown
            color = {
                "CRITICAL": "red",
                "HIGH": "orange1",
                "MODERATE": "yellow",
                "LOW": "green",
                "VERY_LOW": "green",
            }.get(b.risk_level.value, "white")
            table.add_row(
                name,
                f"[{color}]{b.final_score}[/{color}]",
                f"[{color}]{b.risk_level.semaphore} {b.risk_level.value}[/{color}]",
                f"{b.maintainer_concentration:.0f}%",
                str(b.commits_last_year),
            )

        console.print(table)

    # Count by risk level
    level_counts = {}
    for _, r in scored_results:
        lvl = r.breakdown.risk_level.value
        level_counts[lvl] = level_counts.get(lvl, 0) + 1

    parts = []
    for lvl in ["CRITICAL", "HIGH", "MODERATE", "LOW", "VERY_LOW"]:
        if lvl in level_counts:
            parts.append(f"{level_counts[lvl]} {lvl}")

    console.print(
        f"\n[bold]Summary:[/bold] {len(scored_results)} scored, {errors} errors"
        + (f" — {', '.join(parts)}" if parts else "")
    )

    # JSON output
    report_data = {
        "file": file,
        "ecosystem": eco,
        "packages_total": len(entries),
        "packages_scored": len(scored_results),
        "packages_errored": errors,
        "risk_summary": level_counts,
        "results": [
            {
                "package": name,
                "score": r.breakdown.final_score,
                "risk_level": r.breakdown.risk_level.value,
                "concentration": round(r.breakdown.maintainer_concentration, 1),
                "commits_last_year": r.breakdown.commits_last_year,
                "unique_contributors": r.breakdown.unique_contributors,
                "explanation": r.breakdown.explanation,
                "recommendations": r.breakdown.recommendations,
            }
            for name, r in scored_results
        ],
        "errors": [
            {"package": name, "error": r.error}
            for name, r in error_results
        ],
    }

    if json_output:
        console.print(json.dumps(report_data, indent=2))

    if output:
        with open(output, "w") as f:
            json.dump(report_data, f, indent=2)
        console.print(f"\nReport saved to {output}")


@app.command()
def movers(
    limit: int = typer.Option(20, "--limit", "-n", help="Number of movers to show"),
    ecosystem: Optional[str] = typer.Option(None, "--ecosystem", "-e", help="Filter by ecosystem (npm, pypi, cargo, rubygems, packagist, nuget, go, github)"),
):
    """Show packages with the biggest score changes since last scoring."""
    from ossuary.db.session import session_scope
    from ossuary.db.models import Package, Score

    init_db()

    with session_scope() as session:
        packages = session.query(Package).all()
        if ecosystem:
            packages = [p for p in packages if p.ecosystem == ecosystem]

        changes = []
        for pkg in packages:
            scores = (
                session.query(Score)
                .filter(Score.package_id == pkg.id)
                .order_by(Score.calculated_at.desc())
                .limit(2)
                .all()
            )
            if len(scores) >= 2:
                delta = scores[0].final_score - scores[1].final_score
                if delta != 0:
                    changes.append({
                        "name": pkg.name,
                        "ecosystem": pkg.ecosystem,
                        "previous": scores[1].final_score,
                        "current": scores[0].final_score,
                        "delta": delta,
                    })

        changes.sort(key=lambda c: abs(c["delta"]), reverse=True)

        if not changes:
            console.print("[dim]No score changes detected.[/dim]")
            return

        console.print(f"\n[bold]Score changes[/bold] ({len(changes)} packages moved)\n")
        for c in changes[:limit]:
            d = c["delta"]
            color = "red" if d > 0 else "green"
            sign = "+" if d > 0 else ""
            console.print(
                f"  {c['name']:40s} {c['previous']:3d} → {c['current']:3d}  "
                f"[{color}]({sign}{d})[/{color}]  {c['ecosystem']}"
            )
        console.print()


@app.command()
def history(
    package: str = typer.Argument(..., help="Package name (e.g., 'requests', 'openSUSE/aaa_base')"),
    ecosystem: Optional[str] = typer.Option(None, "--ecosystem", "-e", help="Package ecosystem (required if name exists in multiple ecosystems)"),
    limit: int = typer.Option(20, "--limit", "-n", help="Number of records to show"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Show score history for a package over time."""
    from ossuary.db.session import session_scope
    from ossuary.db.models import Package, Score

    init_db()

    with session_scope() as session:
        query = session.query(Package).filter(Package.name == package)
        if ecosystem:
            query = query.filter(Package.ecosystem == ecosystem)
        packages = query.all()

        if not packages:
            console.print(f"[red]Package '{package}' not found in database.[/red]")
            console.print(f"Score it first with: ossuary score {package}")
            raise typer.Exit(1)

        if len(packages) > 1:
            ecosystems = [p.ecosystem for p in packages]
            console.print(f"[red]'{package}' exists in multiple ecosystems: {', '.join(ecosystems)}[/red]")
            console.print("Use -e/--ecosystem to specify which one.")
            raise typer.Exit(1)

        pkg = packages[0]

        scores = (
            session.query(Score)
            .filter(Score.package_id == pkg.id)
            .order_by(Score.calculated_at.desc())
            .limit(limit)
            .all()
        )

        if not scores:
            console.print(f"[yellow]No scores found for {package} ({pkg.ecosystem})[/yellow]")
            raise typer.Exit(0)

        total_count = session.query(Score).filter(Score.package_id == pkg.id).count()

        if json_output:
            records = [
                {
                    "date": s.calculated_at.isoformat(),
                    "score": s.final_score,
                    "risk_level": s.risk_level,
                    "concentration": round(s.maintainer_concentration, 1),
                    "commits_year": s.commits_last_year,
                    "contributors": s.unique_contributors,
                }
                for s in scores
            ]
            console.print(json.dumps({
                "package": pkg.name,
                "ecosystem": pkg.ecosystem,
                "total_records": total_count,
                "records": records,
            }, indent=2))
            return

        console.print(f"\n[bold]Score history for {pkg.name} ({pkg.ecosystem})[/bold]\n")

        table = Table()
        table.add_column("Date", style="cyan", min_width=12)
        table.add_column("Score", justify="right")
        table.add_column("Risk", min_width=10)
        table.add_column("Conc%", justify="right")
        table.add_column("Commits/yr", justify="right")
        table.add_column("Change", justify="right")

        for i, s in enumerate(scores):
            if i < len(scores) - 1:
                delta = s.final_score - scores[i + 1].final_score
                change_str = f"[{'red' if delta > 0 else 'green'}]{delta:+d}[/]" if delta != 0 else "[dim]0[/dim]"
            else:
                change_str = "[dim]--[/dim]"

            color = {
                "CRITICAL": "red", "HIGH": "orange1", "MODERATE": "yellow",
                "LOW": "green", "VERY_LOW": "green",
            }.get(s.risk_level, "white")

            table.add_row(
                s.calculated_at.strftime("%Y-%m-%d %H:%M"),
                f"[{color}]{s.final_score}[/{color}]",
                f"[{color}]{s.risk_level}[/{color}]",
                f"{s.maintainer_concentration:.0f}%",
                str(s.commits_last_year),
                change_str,
            )

        console.print(table)
        if total_count > limit:
            console.print(f"\n[dim]Showing {len(scores)} of {total_count} records (use -n to see more)[/dim]")
        console.print()


@app.command()
def trends(
    seed: Optional[str] = typer.Option(None, "--seed", "-s", help="Filter to packages in a YAML seed file"),
    ecosystem: Optional[str] = typer.Option(None, "--ecosystem", "-e", help="Filter by ecosystem"),
    days: int = typer.Option(90, "--days", "-d", help="Look back N days for comparison"),
    threshold: int = typer.Option(0, "--threshold", "-t", help="Minimum absolute score change to show"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Show score trends across tracked packages over a time window."""
    from ossuary.db.session import session_scope
    from ossuary.db.models import Package, Score
    from datetime import timedelta

    init_db()

    filter_set = None
    if seed:
        from ossuary.services.batch import load_custom_seed
        if not os.path.exists(seed):
            console.print(f"[red]Seed file not found: {seed}[/red]")
            raise typer.Exit(1)
        entries = load_custom_seed(seed)
        filter_set = set()
        for e in entries:
            if e.ecosystem == "github":
                filter_set.add((f"{e.github_owner}/{e.github_repo}", e.ecosystem))
            else:
                filter_set.add((e.obs_package, e.ecosystem))

    with session_scope() as session:
        query = session.query(Package)
        if ecosystem:
            query = query.filter(Package.ecosystem == ecosystem)
        all_packages = query.all()

        if filter_set:
            all_packages = [p for p in all_packages if (p.name, p.ecosystem) in filter_set]

        cutoff_date = datetime.utcnow() - timedelta(days=days)
        rising = []
        falling = []
        stable_count = 0
        no_history = 0

        for pkg in all_packages:
            latest = (
                session.query(Score)
                .filter(Score.package_id == pkg.id)
                .order_by(Score.calculated_at.desc())
                .first()
            )
            if not latest:
                no_history += 1
                continue

            oldest_in_window = (
                session.query(Score)
                .filter(Score.package_id == pkg.id, Score.calculated_at >= cutoff_date)
                .order_by(Score.calculated_at.asc())
                .first()
            )

            if not oldest_in_window or oldest_in_window.id == latest.id:
                stable_count += 1
                continue

            delta = latest.final_score - oldest_in_window.final_score
            if abs(delta) <= threshold:
                stable_count += 1
                continue

            entry = {
                "name": pkg.name,
                "ecosystem": pkg.ecosystem,
                "old_score": oldest_in_window.final_score,
                "new_score": latest.final_score,
                "delta": delta,
                "risk_level": latest.risk_level,
            }

            if delta > 0:
                rising.append(entry)
            else:
                falling.append(entry)

        rising.sort(key=lambda x: x["delta"], reverse=True)
        falling.sort(key=lambda x: x["delta"])

        total = len(all_packages)
        source_label = f"seed {os.path.basename(seed)}" if seed else (f"{ecosystem} packages" if ecosystem else "all packages")

        if json_output:
            console.print(json.dumps({
                "days": days,
                "source": source_label,
                "packages_analyzed": total,
                "rising": rising,
                "falling": falling,
                "stable_count": stable_count,
            }, indent=2))
            return

        console.print(f"\n[bold]Score trends[/bold] (last {days} days, {total} packages from {source_label})\n")

        if rising:
            console.print("[bold red]Rising risk:[/bold red]")
            for r in rising:
                console.print(
                    f"  {r['name']:40s} {r['old_score']:3d} -> {r['new_score']:3d}  "
                    f"[red](+{r['delta']})[/red]  now {r['risk_level']}"
                )
            console.print()

        if falling:
            console.print("[bold green]Falling risk:[/bold green]")
            for f_ in falling:
                console.print(
                    f"  {f_['name']:40s} {f_['old_score']:3d} -> {f_['new_score']:3d}  "
                    f"[green]({f_['delta']})[/green]  now {f_['risk_level']}"
                )
            console.print()

        if not rising and not falling:
            console.print("[dim]No score changes detected in this window.[/dim]\n")

        console.print(f"[bold]Summary:[/bold] {len(rising)} rising, {len(falling)} falling, {stable_count} stable")
        console.print()


@app.command()
def xkcd(
    report: str = typer.Argument(..., help="Scan report JSON (from 'ossuary scan -o')"),
    output: str = typer.Option("stack.svg", "-o", "--output", help="Output SVG file"),
    title: Optional[str] = typer.Option(None, "-t", "--title", help="Title (default: from report filename)"),
    max_width: int = typer.Option(800, "--width", help="Max SVG width in pixels"),
):
    """Generate an xkcd-2347-style dependency stack diagram.

    Block width = number of contributors (team size).
    Block color = risk score (red=critical, green=safe).
    Order preserved from scan report (intermingled for maximum comic effect).
    """
    if not os.path.exists(report):
        console.print(f"[red]File not found: {report}[/red]")
        raise typer.Exit(1)

    try:
        with open(report) as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        console.print(f"[red]Invalid JSON: {e}[/red]")
        raise typer.Exit(1)

    results = data.get("results", [])
    if not results:
        console.print("[yellow]No packages in report.[/yellow]")
        raise typer.Exit(0)

    # Shuffle to intermingle risk levels (the xkcd comic effect)
    import random
    random.seed(len(results))  # deterministic for same input
    results = results[:]
    random.shuffle(results)

    if not title:
        title = os.path.basename(data.get("file", report))

    _generate_xkcd_svg(results, output, title, max_width)
    console.print(f"[green]Generated {output}[/green] ({len(results)} packages)")


def _generate_xkcd_svg(results: list, output: str, title: str, max_width: int):
    """Generate the SVG stack diagram."""
    import html as html_module
    import math
    import random

    # Deterministic wobble per input
    rng = random.Random(sum(ord(c) for r in results for c in r["package"]))

    # Block dimensions
    block_height = 36
    block_gap = 2
    padding_left = 40
    padding_right = 220  # room for side labels
    title_height = 50
    caption_margin = 70

    # Risk level to color
    def score_to_color(score):
        if score >= 80:
            return "#c62828"  # deep red
        elif score >= 60:
            return "#d84315"  # deep orange
        elif score >= 40:
            return "#f57f17"  # amber
        elif score >= 20:
            return "#558b2f"  # olive green
        else:
            return "#2e7d32"  # forest green

    # Scale: sqrt for dramatic contrast
    # 1-person = tiny 18px sliver, many contributors = fills the tower area
    max_contributors = max(max(r.get("unique_contributors", 1), 1) for r in results)
    tower_width = max_width - padding_left - padding_right
    min_block_width = 18

    blocks = []
    for r in results:
        contributors = max(r.get("unique_contributors", 1), 1)
        ratio = math.sqrt(contributors) / math.sqrt(max_contributors)
        width = max(min_block_width, int(tower_width * (0.03 + 0.97 * ratio)))
        blocks.append({
            "name": r["package"],
            "score": r["score"],
            "risk_level": r["risk_level"],
            "contributors": contributors,
            "commits": r.get("commits_last_year", 0),
            "concentration": r.get("concentration", 0),
            "width": width,
            "color": score_to_color(r["score"]),
        })

    # Find the scariest package for the callout
    worst = max(blocks, key=lambda b: (b["score"], -b["contributors"]))
    worst_idx = blocks.index(worst)

    # Calculate wobble offsets — each block sits on the one below,
    # shifted randomly within overlap constraints
    tower_center = padding_left + tower_width / 2
    offsets = [0.0]
    for i in range(1, len(blocks)):
        below_w = blocks[i - 1]["width"]
        this_w = blocks[i]["width"]
        # Smaller blocks can perch more precariously (less overlap required)
        smaller = min(below_w, this_w)
        overlap_min = smaller * 0.45
        max_shift = (below_w + this_w) / 2 - overlap_min
        max_shift = max(0, max_shift)
        wobble = rng.uniform(-max_shift, max_shift)
        drift = offsets[i - 1] + wobble
        # Dampen drift to prevent walking off canvas
        drift *= 0.82
        half = this_w / 2
        limit = tower_width / 2 - half
        drift = max(-limit, min(limit, drift))
        offsets.append(drift)

    # Total SVG height
    stack_height = len(blocks) * (block_height + block_gap)
    total_height = title_height + stack_height + caption_margin + 20

    # Build SVG
    svg = []
    svg.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{max_width}" height="{int(total_height)}" '
        f'style="background: #fafafa; font-family: \'Comic Sans MS\', \'Chalkboard SE\', cursive, sans-serif;">'
    )

    # Arrow marker — dark blue, refX=10 so the tip lands at the endpoint
    svg.append(
        '<defs><marker id="arr" markerWidth="10" markerHeight="7" '
        'refX="10" refY="3.5" orient="auto" markerUnits="strokeWidth">'
        '<polygon points="0 0, 10 3.5, 0 7" fill="#1a237e"/>'
        '</marker></defs>'
    )

    # Title
    svg.append(
        f'<text x="{tower_center}" y="32" text-anchor="middle" '
        f'font-size="20" font-weight="bold" fill="#333">'
        f'{html_module.escape(title)}</text>'
    )

    # Draw blocks bottom-up, track positions for labels and callout
    block_positions = []
    for i, block in enumerate(blocks):
        bw = block["width"]
        bx = tower_center + offsets[i] - bw / 2
        by = title_height + (len(blocks) - 1 - i) * (block_height + block_gap)
        bcx = bx + bw / 2
        block_positions.append((bx, by, bw, bcx))

        # Block rectangle
        svg.append(
            f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bw}" height="{block_height}" '
            f'rx="2" ry="2" fill="{block["color"]}" stroke="#222" stroke-width="1.2"/>'
        )

        # Label: inside if fits, beside if not
        name = block["name"]
        max_inside_chars = max(1, int(bw / 7.5))

        if bw >= 100:
            # Label fits inside
            display = name if len(name) <= max_inside_chars else name[:max_inside_chars - 2] + ".."
            font_size = 11 if bw >= 140 else 10
            svg.append(
                f'<text x="{bcx:.1f}" y="{by + block_height / 2 + 1:.1f}" '
                f'text-anchor="middle" dominant-baseline="middle" '
                f'font-size="{font_size}" fill="#fff">'
                f'{html_module.escape(display)}</text>'
            )
        else:
            # Label to the right, connected by a thin line
            label_x = bx + bw + 6
            svg.append(
                f'<line x1="{bx + bw + 1:.1f}" y1="{by + block_height / 2:.1f}" '
                f'x2="{label_x - 1:.1f}" y2="{by + block_height / 2:.1f}" '
                f'stroke="#888" stroke-width="0.7"/>'
            )
            svg.append(
                f'<text x="{label_x:.1f}" y="{by + block_height / 2 + 1:.1f}" '
                f'dominant-baseline="middle" font-size="9" fill="#555">'
                f'{html_module.escape(name)}</text>'
            )

    # Callout arrow for the worst block — from the left side, dark blue
    if worst["score"] >= 40:
        bx, by, bw, bcx = block_positions[worst_idx]

        # Arrow tip: left edge of the block
        arrow_tip_x = bx - 2
        arrow_tip_y = by + block_height / 2

        # Arrow starts further left and below
        arrow_start_x = max(arrow_tip_x - 120, 10)
        arrow_start_y = arrow_tip_y + 60

        svg.append(
            f'<line x1="{arrow_start_x:.1f}" y1="{arrow_start_y:.1f}" '
            f'x2="{arrow_tip_x:.1f}" y2="{arrow_tip_y:.1f}" '
            f'stroke="#1a237e" stroke-width="2.5" marker-end="url(#arr)"/>'
        )

    # Caption at bottom
    caption_y = title_height + stack_height + 20
    if worst["score"] >= 40:
        if worst["commits"] == 0:
            activity = "No commits this year."
        elif worst["commits"] <= 4:
            activity = f'{worst["commits"]} commit{"s" if worst["commits"] != 1 else ""} this year.'
        else:
            activity = f'{worst["commits"]} commits/yr, {worst["concentration"]:.0f}% one person.'

        line1 = (
            f'All of this rests on "{html_module.escape(worst["name"])}", '
            f'maintained by {worst["contributors"]} '
            f'person{"" if worst["contributors"] == 1 else "s"}. {activity}'
        )
        svg.append(
            f'<text x="{tower_center}" y="{caption_y}" text-anchor="middle" '
            f'font-size="13" fill="#555" font-style="italic">{line1}</text>'
        )
    else:
        svg.append(
            f'<text x="{tower_center}" y="{caption_y}" text-anchor="middle" '
            f'font-size="13" fill="#555" font-style="italic">'
            f'{len(blocks)} dependencies — looking healthy!</text>'
        )

    # Footer
    svg.append(
        f'<text x="{tower_center}" y="{caption_y + 25}" text-anchor="middle" '
        f'font-size="9" fill="#bbb">generated by ossuary // inspired by xkcd.com/2347</text>'
    )

    svg.append('</svg>')

    with open(output, "w") as f:
        f.write('\n'.join(svg))


@app.command()
def diff(
    before: str = typer.Argument(..., help="Baseline scan report (JSON from 'ossuary scan -o')"),
    after: str = typer.Argument(..., help="New scan report (JSON from 'ossuary scan -o')"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Compare two scan reports to show added, removed, and changed packages."""
    for path, label in [(before, "before"), (after, "after")]:
        if not os.path.exists(path):
            console.print(f"[red]File not found ({label}): {path}[/red]")
            raise typer.Exit(1)

    try:
        with open(before) as f:
            before_data = json.load(f)
        with open(after) as f:
            after_data = json.load(f)
    except (json.JSONDecodeError, KeyError) as e:
        console.print(f"[red]Invalid JSON report: {e}[/red]")
        raise typer.Exit(1)

    before_pkgs = {r["package"]: r for r in before_data.get("results", [])}
    after_pkgs = {r["package"]: r for r in after_data.get("results", [])}

    added_names = sorted(after_pkgs.keys() - before_pkgs.keys())
    removed_names = sorted(before_pkgs.keys() - after_pkgs.keys())
    common_names = before_pkgs.keys() & after_pkgs.keys()

    changed = []
    for name in sorted(common_names):
        old_score = before_pkgs[name]["score"]
        new_score = after_pkgs[name]["score"]
        if old_score != new_score:
            changed.append({
                "package": name,
                "old_score": old_score,
                "new_score": new_score,
                "delta": new_score - old_score,
                "risk_level": after_pkgs[name]["risk_level"],
            })
    changed.sort(key=lambda x: -abs(x["delta"]))

    unchanged_count = len(common_names) - len(changed)

    if json_output:
        console.print(json.dumps({
            "before_file": before_data.get("file", before),
            "after_file": after_data.get("file", after),
            "added": [after_pkgs[n] for n in added_names],
            "removed": [before_pkgs[n] for n in removed_names],
            "changed": changed,
            "unchanged_count": unchanged_count,
        }, indent=2))
        return

    after_file = after_data.get("file", after)
    console.print(f"\n[bold]Dependency diff:[/bold] {after_file}\n")

    if added_names:
        added_sorted = sorted(added_names, key=lambda n: -after_pkgs[n]["score"])
        console.print(f"[bold green]Added ({len(added_names)} package{'s' if len(added_names) != 1 else ''}):[/bold green]")
        for name in added_sorted:
            r = after_pkgs[name]
            color = {
                "CRITICAL": "red", "HIGH": "orange1", "MODERATE": "yellow",
                "LOW": "green", "VERY_LOW": "green",
            }.get(r["risk_level"], "white")
            console.print(
                f"  {name:40s} [{color}]{r['score']:3d}  {r['risk_level']:10s}[/{color}] "
                f"{r['concentration']:.0f}% conc  {r['commits_last_year']} commits/yr"
            )
        console.print()

    if removed_names:
        console.print(f"[bold red]Removed ({len(removed_names)} package{'s' if len(removed_names) != 1 else ''}):[/bold red]")
        for name in removed_names:
            r = before_pkgs[name]
            console.print(f"  {name:40s} [dim]{r['score']:3d}  {r['risk_level']}[/dim]")
        console.print()

    if changed:
        console.print(f"[bold yellow]Changed ({len(changed)} package{'s' if len(changed) != 1 else ''}):[/bold yellow]")
        for c in changed:
            d = c["delta"]
            color = "red" if d > 0 else "green"
            sign = "+" if d > 0 else ""
            console.print(
                f"  {c['package']:40s} {c['old_score']:3d} -> {c['new_score']:3d}  "
                f"[{color}]({sign}{d})[/{color}]  now {c['risk_level']}"
            )
        console.print()

    if not added_names and not removed_names and not changed:
        console.print("[dim]No differences found.[/dim]\n")

    # Summary
    parts = []
    if added_names:
        parts.append(f"+{len(added_names)} added")
    if removed_names:
        parts.append(f"-{len(removed_names)} removed")
    if changed:
        parts.append(f"{len(changed)} changed")
    parts.append(f"{unchanged_count} unchanged")
    console.print(f"[bold]Summary:[/bold] {', '.join(parts)}")

    # Risk impact of added packages
    if added_names:
        added_levels = {}
        for name in added_names:
            lvl = after_pkgs[name]["risk_level"]
            added_levels[lvl] = added_levels.get(lvl, 0) + 1
        impact_parts = []
        for lvl in ["CRITICAL", "HIGH", "MODERATE", "LOW", "VERY_LOW"]:
            if lvl in added_levels:
                impact_parts.append(f"+{added_levels[lvl]} {lvl}")
        if impact_parts:
            console.print(f"  Risk impact: {', '.join(impact_parts)}")

    console.print()


@app.command()
def refresh(
    ecosystem: Optional[str] = typer.Option(None, "--ecosystem", "-e", help="Only refresh this ecosystem (npm, pypi, cargo, rubygems, packagist, nuget, go, github)"),
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


SEED_PACKAGES = [
    # npm — mix of risk profiles
    ("lodash", "npm"),
    ("express", "npm"),
    ("chalk", "npm"),
    ("minimist", "npm"),
    ("event-stream", "npm"),
    # pypi — popular packages
    ("requests", "pypi"),
    ("flask", "pypi"),
    ("django", "pypi"),
    ("black", "pypi"),
    ("numpy", "pypi"),
    # github — direct repos, diverse governance
    ("kubernetes/kubernetes", "github"),
    ("hashicorp/terraform", "github"),
    ("pallets/flask", "github"),
    ("go-kit/kit", "github"),
]


# Default SUSE seed file path (shipped with the package)
_SUSE_SEED_DEFAULT = os.path.join(os.path.dirname(__file__), "..", "..", "seeds", "suse-base.yaml")


@app.command()
def seed():
    """Score a curated set of packages to populate the dashboard."""
    asyncio.run(_seed())


async def _seed():
    """Score seed packages."""
    from ossuary.services.scorer import score_package as svc_score

    init_db()

    console.print(f"Seeding {len(SEED_PACKAGES)} packages across npm, pypi, and github...\n")

    success = 0
    errors = 0
    for i, (name, eco) in enumerate(SEED_PACKAGES, 1):
        console.print(f"  [{i}/{len(SEED_PACKAGES)}] {name} ({eco})...", end=" ")
        try:
            result = await svc_score(name, eco)
            if result.success:
                b = result.breakdown
                color = {
                    "CRITICAL": "red",
                    "HIGH": "orange1",
                    "MODERATE": "yellow",
                    "LOW": "green",
                    "VERY_LOW": "green",
                }.get(b.risk_level.value, "white")
                console.print(f"[{color}]{b.final_score} {b.risk_level.value}[/{color}]")
                success += 1
            else:
                console.print(f"[red]ERROR: {result.error}[/red]")
                errors += 1
        except Exception as e:
            console.print(f"[red]ERROR: {e}[/red]")
            errors += 1

    console.print(f"\nDone. {success} scored, {errors} errors.")
    console.print("Dashboard should now show tracked packages.")


@app.command("seed-suse-base")
def seed_suse_base():
    """Score the bundled SUSE seed (136 packages, no osc required).

    This is a convenience wrapper around 'seed-custom seeds/suse-base.yaml'.
    """
    seed_path = os.path.normpath(_SUSE_SEED_DEFAULT)
    if not os.path.exists(seed_path):
        # Fall back to seeds/ in the current directory
        seed_path = "seeds/suse-base.yaml"
    if not os.path.exists(seed_path):
        console.print("[red]SUSE seed file not found. Expected seeds/suse-base.yaml[/red]")
        raise typer.Exit(1)
    asyncio.run(_seed_custom(seed_path, limit=0, concurrent=3, skip_fresh=True, fresh_days=7))


@app.command("discover-suse")
def discover_suse(
    project: str = typer.Option("openSUSE:Factory", "--project", "-p", help="OBS project to scan"),
    output: str = typer.Option("suse_packages.json", "--output", "-o", help="Output JSON file"),
    workers: int = typer.Option(5, "--workers", "-w", help="Parallel osc workers"),
    delay: float = typer.Option(0.1, "--delay", "-d", help="Seconds between OBS API calls"),
    resume: bool = typer.Option(False, "--resume", help="Skip already-discovered packages"),
    limit: int = typer.Option(0, "--limit", "-l", help="Only process first N packages (0=all)"),
):
    """Discover GitHub repos for openSUSE/OBS packages via osc."""
    import subprocess

    # Check osc is available
    try:
        subprocess.run(["osc", "--version"], capture_output=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        console.print("[red]osc CLI not found. Install with: zypper install osc[/red]")
        raise typer.Exit(1)

    # Build the command
    script = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "discover_suse.py")
    script = os.path.normpath(script)

    if not os.path.exists(script):
        # Try relative to cwd
        script = os.path.join("scripts", "discover_suse.py")

    if not os.path.exists(script):
        console.print(f"[red]Discovery script not found: {script}[/red]")
        raise typer.Exit(1)

    cmd = [
        sys.executable, script,
        "--project", project,
        "--output", output,
        "--workers", str(workers),
        "--delay", str(delay),
    ]
    if resume:
        cmd.append("--resume")
    if limit > 0:
        cmd.extend(["--limit", str(limit)])

    console.print(f"Running discovery: {' '.join(cmd)}\n")
    result = subprocess.run(cmd)
    raise typer.Exit(result.returncode)


@app.command("seed-custom")
def seed_custom(
    file: str = typer.Argument(..., help="YAML config file with package list"),
    limit: int = typer.Option(0, "--limit", "-l", help="Score first N packages only (0=all)"),
    concurrent: int = typer.Option(3, "--concurrent", "-c", help="Parallel scoring workers"),
    skip_fresh: bool = typer.Option(True, "--skip-fresh/--no-skip-fresh", help="Skip recently scored packages"),
    fresh_days: int = typer.Option(7, "--fresh-days", help="Days before a score is stale"),
):
    """Score packages from a custom YAML seed file.

    The YAML file should have this format:

        packages:
          - name: owner/repo
            repo: https://github.com/owner/repo
          - name: numpy
            ecosystem: pypi

    GitHub packages require a 'repo' URL. For npm/pypi, 'repo' is optional.
    """
    asyncio.run(_seed_custom(file, limit, concurrent, skip_fresh, fresh_days))


async def _seed_custom(
    file: str, limit: int, concurrent: int, skip_fresh: bool, fresh_days: int
):
    """Score packages from a custom YAML seed file."""
    from ossuary.services.batch import load_custom_seed, batch_score

    init_db()

    if not os.path.exists(file):
        console.print(f"[red]Seed file not found: {file}[/red]")
        raise typer.Exit(1)

    try:
        packages = load_custom_seed(file)
    except ValueError as e:
        console.print(f"[red]Invalid seed file: {e}[/red]")
        raise typer.Exit(1)

    total = min(limit, len(packages)) if limit > 0 else len(packages)

    console.print(f"[bold]Scoring custom seed[/bold]")
    console.print(f"  Source: {file}")
    console.print(f"  Packages: {total} of {len(packages)}")
    console.print(f"  Concurrent: {concurrent}")
    console.print(f"  Skip fresh (<{fresh_days}d): {skip_fresh}\n")

    scored_count = 0
    skipped_count = 0
    error_count = 0

    def on_progress(current, total_pkgs, pkg_name, status):
        nonlocal scored_count, skipped_count, error_count

        if status == "scored":
            scored_count += 1
            console.print(f"  [{current}/{total_pkgs}] [green]OK[/green] {pkg_name}")
        elif status == "skipped":
            skipped_count += 1
            if skipped_count % 100 == 0:
                console.print(f"  [{current}/{total_pkgs}] skipped {skipped_count} fresh packages so far...")
        else:
            error_count += 1
            console.print(f"  [{current}/{total_pkgs}] [red]{status}[/red] {pkg_name}")

    result = await batch_score(
        packages,
        max_concurrent=concurrent,
        max_packages=limit,
        skip_fresh=skip_fresh,
        fresh_days=fresh_days,
        progress_callback=on_progress,
    )

    console.print(f"\n[bold green]Done![/bold green]")
    console.print(f"  Scored: {result.scored}")
    console.print(f"  Skipped (fresh): {result.skipped}")
    console.print(f"  Errors: {result.errors}")

    if result.errors > 0 and result.error_details:
        console.print(f"\n[bold yellow]Error summary (first 20):[/bold yellow]")
        for detail in result.error_details[:20]:
            console.print(f"  {detail}")


@app.command("seed-suse")
def seed_suse(
    file: str = typer.Option("suse_packages.json", "--file", "-f", help="Discovery JSON file"),
    limit: int = typer.Option(0, "--limit", "-l", help="Score first N packages only (0=all)"),
    concurrent: int = typer.Option(3, "--concurrent", "-c", help="Parallel scoring workers"),
    skip_fresh: bool = typer.Option(True, "--skip-fresh/--no-skip-fresh", help="Skip recently scored packages"),
    fresh_days: int = typer.Option(7, "--fresh-days", help="Days before a score is stale"),
):
    """Score all discovered SUSE packages from a discovery JSON file."""
    asyncio.run(_seed_suse(file, limit, concurrent, skip_fresh, fresh_days))


async def _seed_suse(
    file: str, limit: int, concurrent: int, skip_fresh: bool, fresh_days: int
):
    """Batch-score SUSE packages."""
    from ossuary.services.batch import load_discovery_file, batch_score

    init_db()

    if not os.path.exists(file):
        console.print(f"[red]Discovery file not found: {file}[/red]")
        console.print("Run 'ossuary discover-suse' first to generate it.")
        raise typer.Exit(1)

    packages = load_discovery_file(file)
    total = min(limit, len(packages)) if limit > 0 else len(packages)

    console.print(f"[bold]Batch scoring SUSE packages[/bold]")
    console.print(f"  Source: {file}")
    console.print(f"  Packages: {total} of {len(packages)}")
    console.print(f"  Concurrent: {concurrent}")
    console.print(f"  Skip fresh (<{fresh_days}d): {skip_fresh}\n")

    scored_count = 0
    skipped_count = 0
    error_count = 0

    def on_progress(current, total_pkgs, pkg_name, status):
        nonlocal scored_count, skipped_count, error_count

        if status == "scored":
            scored_count += 1
            console.print(f"  [{current}/{total_pkgs}] [green]OK[/green] {pkg_name}")
        elif status == "skipped":
            skipped_count += 1
            # Only print skip every 100 to avoid spam
            if skipped_count % 100 == 0:
                console.print(f"  [{current}/{total_pkgs}] skipped {skipped_count} fresh packages so far...")
        else:
            error_count += 1
            console.print(f"  [{current}/{total_pkgs}] [red]{status}[/red] {pkg_name}")

    result = await batch_score(
        packages,
        max_concurrent=concurrent,
        max_packages=limit,
        skip_fresh=skip_fresh,
        fresh_days=fresh_days,
        progress_callback=on_progress,
    )

    console.print(f"\n[bold green]Done![/bold green]")
    console.print(f"  Scored: {result.scored}")
    console.print(f"  Skipped (fresh): {result.skipped}")
    console.print(f"  Errors: {result.errors}")

    if result.errors > 0 and result.error_details:
        console.print(f"\n[bold yellow]Error summary (first 20):[/bold yellow]")
        for detail in result.error_details[:20]:
            console.print(f"  {detail}")


if __name__ == "__main__":
    app()
