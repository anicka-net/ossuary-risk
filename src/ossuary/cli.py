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
def deps(
    package: str = typer.Argument(..., help="Root package (e.g., 'express')"),
    ecosystem: str = typer.Option("npm", "-e", "--ecosystem", help="Package ecosystem (npm or pypi)"),
    max_depth: int = typer.Option(6, "--depth", help="Max dependency depth"),
    max_packages: int = typer.Option(80, "--max", help="Max packages to include"),
    json_output: bool = typer.Option(False, "--json", "-j", help="Output as JSON"),
):
    """Show the dependency tree of a package with risk scores.

    Fetches the dependency tree from the package registry and displays it
    as an indented tree with risk scores from the database.
    """
    if ecosystem not in _DEP_ECOSYSTEMS:
        console.print(f"[red]Supported ecosystems: {', '.join(_DEP_ECOSYSTEMS)}[/red]")
        raise typer.Exit(1)

    console.print(f"[bold]Fetching dependency tree for {package} ({ecosystem})...[/bold]")
    adj = _fetch_dep_tree(package, ecosystem, max_depth, max_packages)
    if not adj:
        console.print(f"[red]Could not fetch dependencies for {package}[/red]")
        raise typer.Exit(1)

    # Load scores from DB
    scores_db = {}
    try:
        from ossuary.db.session import session_scope
        from ossuary.db.models import Package, Score
        with session_scope() as session:
            for name in adj:
                db_name = name.replace("_", "-").lower() if ecosystem == "pypi" else name
                pkg = session.query(Package).filter(
                    Package.name == db_name, Package.ecosystem == ecosystem,
                ).first()
                if pkg:
                    latest = (
                        session.query(Score).filter(Score.package_id == pkg.id)
                        .order_by(Score.calculated_at.desc()).first()
                    )
                    if latest:
                        scores_db[name] = {
                            "score": latest.final_score,
                            "risk_level": latest.risk_level,
                        }
    except Exception:
        pass

    risk_color = {
        "CRITICAL": "red", "HIGH": "orange1", "MODERATE": "yellow",
        "LOW": "green", "VERY_LOW": "green",
    }

    def label(name):
        info = scores_db.get(name)
        if info:
            rl = info["risk_level"]
            color = risk_color.get(rl, "white")
            return f"[cyan]{name}[/cyan] [{color}]{info['score']} {rl}[/{color}]"
        return f"[cyan]{name}[/cyan] [dim]unscored[/dim]"

    if json_output:
        visited = set()

        def build_node(name):
            info = scores_db.get(name, {})
            node = {"name": name}
            if info:
                node["score"] = info["score"]
                node["risk_level"] = info["risk_level"]
            children = adj.get(name, [])
            if children and name not in visited:
                visited.add(name)
                node["dependencies"] = [build_node(c) for c in sorted(children) if c in adj]
            return node

        tree_json = build_node(package)
        console.print(json.dumps({
            "root": package,
            "ecosystem": ecosystem,
            "packages": len(adj),
            "tree": tree_json,
        }, indent=2))
        return

    # Rich Tree display
    from rich.tree import Tree

    visited = set()

    def add_branch(tree_node, name):
        children = adj.get(name, [])
        for child in sorted(children):
            if child not in adj:
                continue
            if child in visited:
                tree_node.add(f"{label(child)} [dim](see above)[/dim]")
            else:
                visited.add(child)
                branch = tree_node.add(label(child))
                add_branch(branch, child)

    tree = Tree(label(package))
    visited.add(package)
    add_branch(tree, package)
    console.print()
    console.print(tree)

    # Summary
    level_counts = {}
    for info in scores_db.values():
        rl = info["risk_level"]
        level_counts[rl] = level_counts.get(rl, 0) + 1
    unscored = len(adj) - len(scores_db)

    parts = []
    for lvl in ["CRITICAL", "HIGH", "MODERATE", "LOW", "VERY_LOW"]:
        if lvl in level_counts:
            color = risk_color[lvl]
            parts.append(f"[{color}]{level_counts[lvl]} {lvl}[/{color}]")
    if unscored:
        parts.append(f"[dim]{unscored} unscored[/dim]")

    console.print(f"\n[bold]{len(adj)} packages[/bold]: {', '.join(parts)}")


@app.command("score-deps")
def score_deps(
    package: str = typer.Argument(..., help="Root package (e.g., 'express')"),
    ecosystem: str = typer.Option("npm", "-e", "--ecosystem", help="Package ecosystem (npm or pypi)"),
    max_depth: int = typer.Option(6, "--depth", help="Max dependency depth"),
    max_packages: int = typer.Option(80, "--max", help="Max packages to include"),
):
    """Score all packages in a dependency tree.

    Fetches the dependency tree and scores every package that hasn't been
    scored yet. Run this before xkcd-tree to get a fully colored visualization.
    """
    if ecosystem not in _DEP_ECOSYSTEMS:
        console.print(f"[red]Supported ecosystems: {', '.join(_DEP_ECOSYSTEMS)}[/red]")
        raise typer.Exit(1)

    if not os.environ.get("GITHUB_TOKEN"):
        console.print("[yellow]Warning: GITHUB_TOKEN not set — GitHub API will be rate-limited.[/yellow]")
        console.print("[yellow]Set it: export GITHUB_TOKEN=ghp_your_token[/yellow]\n")

    console.print(f"[bold]Fetching dependency tree for {package} ({ecosystem})...[/bold]")
    adj = _fetch_dep_tree(package, ecosystem, max_depth, max_packages)
    if not adj:
        console.print(f"[red]Could not fetch dependencies for {package}[/red]")
        raise typer.Exit(1)

    console.print(f"[bold]{len(adj)} packages[/bold] in dependency tree\n")

    # Check which are already scored
    from ossuary.db.session import session_scope
    from ossuary.db.models import Package, Score
    init_db()

    already_scored = set()
    with session_scope() as session:
        for name in adj:
            db_name = name.replace("_", "-").lower() if ecosystem == "pypi" else name
            pkg = session.query(Package).filter(
                Package.name == db_name, Package.ecosystem == ecosystem,
            ).first()
            if pkg:
                latest = session.query(Score).filter(
                    Score.package_id == pkg.id,
                ).first()
                if latest:
                    already_scored.add(name)

    to_score = [n for n in sorted(adj) if n not in already_scored]
    if not to_score:
        console.print("[green]All packages already scored.[/green]")
        return

    console.print(f"{len(already_scored)} already scored, [bold]{len(to_score)} to score[/bold]\n")

    from ossuary.services.scorer import score_package as svc_score

    ok, fail = 0, 0
    for i, name in enumerate(to_score, 1):
        db_name = name.replace("_", "-").lower() if ecosystem == "pypi" else name
        console.print(f"[{i:3}/{len(to_score)}] {name}... ", end="")
        try:
            result = asyncio.run(svc_score(db_name, ecosystem, force=True))
            if result.success:
                rl = result.breakdown.risk_level.value
                sc = result.breakdown.final_score
                color = {"CRITICAL": "red", "HIGH": "orange1", "MODERATE": "yellow"}.get(rl, "green")
                console.print(f"[{color}]{sc} {rl}[/{color}]")
                ok += 1
            else:
                console.print(f"[red]{result.error}[/red]")
                fail += 1
        except Exception as e:
            console.print(f"[red]{e}[/red]")
            fail += 1

    console.print(f"\n[bold]Done:[/bold] {ok} scored, {fail} errors")


@app.command("xkcd-tree")
def xkcd_tree(
    package: str = typer.Argument(..., help="Root package (e.g., 'express')"),
    ecosystem: str = typer.Option("npm", "-e", "--ecosystem", help="Package ecosystem (npm or pypi)"),
    output: str = typer.Option("tree.svg", "-o", "--output", help="Output SVG file"),
    max_depth: int = typer.Option(6, "--depth", help="Max dependency depth to traverse"),
    max_packages: int = typer.Option(80, "--max", help="Max packages to include"),
    tower: bool = typer.Option(False, "--tower", help="Render as Jenga tower instead of tree graph"),
    title: Optional[str] = typer.Option(None, "-t", "--title"),
    max_width: int = typer.Option(1200, "--width", help="SVG width in pixels"),
):
    """Generate a dependency tree diagram (xkcd-2347 style).

    Fetches the full dependency tree from the package registry and renders
    it as a layered graph where branches converge onto shared foundations.
    Block width = contributors, block color = risk score.

    With --tower, renders as a Jenga-style wobble tower ordered by real
    dependency structure: foundation packages at the bottom, root at the top.
    """
    if ecosystem not in ("npm", "pypi"):
        console.print("[red]Tree diagrams support npm and pypi ecosystems.[/red]")
        raise typer.Exit(1)

    console.print(f"[bold]Fetching dependency tree for {package} ({ecosystem})...[/bold]")

    adj = _fetch_dep_tree(package, ecosystem, max_depth, max_packages)
    if not adj:
        console.print(f"[red]Could not fetch dependencies for {package}[/red]")
        raise typer.Exit(1)

    console.print(f"\n  [bold]{len(adj)} packages[/bold] in dependency tree")

    if not title:
        title = f"{package} — dependency tree" if not tower else package

    if tower:
        _generate_tower_from_tree(adj, package, ecosystem, output, title, max_width)
    else:
        _generate_tree_svg(adj, package, ecosystem, output, title, max_width)
    console.print(f"[green]Generated {output}[/green]")


_DEP_ECOSYSTEMS = ("npm", "pypi", "cargo", "rubygems", "go", "packagist", "nuget", "github")


def _fetch_dep_tree(package, ecosystem, max_depth, max_packages):
    """Fetch dependency tree from package registry (BFS, concurrent)."""
    import re
    import urllib.parse
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor, as_completed

    ua = "ossuary-risk/0.6 (https://github.com/anicka-net/ossuary-risk)"

    adj = {}
    to_fetch = {package: 0}

    def fetch_npm(name):
        try:
            url = f"https://registry.npmjs.org/{urllib.parse.quote(name, safe='@/')}/latest"
            req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": ua})
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read())
            return name, list(data.get("dependencies", {}).keys())
        except Exception:
            return name, []

    def fetch_pypi(name):
        try:
            url = f"https://pypi.org/pypi/{urllib.parse.quote(name, safe='')}/json"
            req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": ua})
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read())
            requires = data.get("info", {}).get("requires_dist") or []
            deps = []
            for r in requires:
                # Skip extras-only dependencies
                if "extra ==" in r or "extra==" in r:
                    continue
                m = re.match(r'^([A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?)', r)
                if m:
                    deps.append(m.group(1))
            return name, deps
        except Exception:
            return name, []

    def fetch_cargo(name):
        try:
            # First get latest version
            url = f"https://crates.io/api/v1/crates/{urllib.parse.quote(name, safe='')}"
            req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "application/json"})
            data = json.loads(urllib.request.urlopen(req, timeout=10).read())
            ver = data["crate"]["newest_version"]
            # Then get deps for that version
            url2 = f"https://crates.io/api/v1/crates/{urllib.parse.quote(name, safe='')}/{ver}/dependencies"
            req2 = urllib.request.Request(url2, headers={"User-Agent": ua, "Accept": "application/json"})
            data2 = json.loads(urllib.request.urlopen(req2, timeout=10).read())
            deps = [d["crate_id"] for d in data2.get("dependencies", [])
                    if d.get("kind") == "normal" and not d.get("optional")]
            return name, deps
        except Exception:
            return name, []

    def fetch_rubygems(name):
        try:
            # Get latest version
            url = f"https://rubygems.org/api/v1/gems/{urllib.parse.quote(name, safe='')}.json"
            req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "application/json"})
            data = json.loads(urllib.request.urlopen(req, timeout=10).read())
            ver = data.get("version", "")
            # Get deps for that version
            url2 = f"https://rubygems.org/api/v2/rubygems/{urllib.parse.quote(name, safe='')}/versions/{ver}.json"
            req2 = urllib.request.Request(url2, headers={"User-Agent": ua, "Accept": "application/json"})
            data2 = json.loads(urllib.request.urlopen(req2, timeout=10).read())
            deps = [d["name"] for d in data2.get("dependencies", {}).get("runtime", [])]
            return name, deps
        except Exception:
            return name, []

    def fetch_go(name):
        try:
            # Escape uppercase letters per Go proxy convention
            escaped = re.sub(r'[A-Z]', lambda m: '!' + m.group().lower(), name)
            url = f"https://proxy.golang.org/{escaped}/@latest"
            req = urllib.request.Request(url, headers={"User-Agent": ua})
            data = json.loads(urllib.request.urlopen(req, timeout=10).read())
            ver = data["Version"]
            # Fetch go.mod
            mod_url = f"https://proxy.golang.org/{escaped}/@v/{ver}.mod"
            req2 = urllib.request.Request(mod_url, headers={"User-Agent": ua})
            content = urllib.request.urlopen(req2, timeout=10).read().decode()
            # Parse require block and single-line requires
            deps = []
            in_block = False
            for line in content.splitlines():
                stripped = line.strip()
                if stripped.startswith("require ("):
                    in_block = True
                    continue
                if in_block and stripped == ")":
                    in_block = False
                    continue
                if in_block:
                    m = re.match(r'^(\S+)\s+\S+', stripped)
                    if m:
                        deps.append(m.group(1))
                elif stripped.startswith("require "):
                    m = re.match(r'^require\s+(\S+)\s+\S+', stripped)
                    if m:
                        deps.append(m.group(1))
            return name, deps
        except Exception:
            return name, []

    def fetch_packagist(name):
        try:
            url = f"https://repo.packagist.org/p2/{name.lower()}.json"
            req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "application/json"})
            data = json.loads(urllib.request.urlopen(req, timeout=10).read())
            versions = data.get("packages", {}).get(name.lower(), [])
            if not versions:
                return name, []
            latest = versions[0]
            deps = [k for k in latest.get("require", {}).keys() if "/" in k]
            return name, deps
        except Exception:
            return name, []

    def fetch_nuget(name):
        try:
            url = f"https://api.nuget.org/v3/registration5/{name.lower()}/index.json"
            req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "application/json"})
            data = json.loads(urllib.request.urlopen(req, timeout=10).read())
            # Get the last page (latest versions)
            last_page = data["items"][-1]
            items = last_page.get("items")
            if items is None:
                page_req = urllib.request.Request(last_page["@id"], headers={"User-Agent": ua})
                page_data = json.loads(urllib.request.urlopen(page_req, timeout=10).read())
                items = page_data["items"]
            # Get latest version's deps
            latest = items[-1]["catalogEntry"]
            deps = set()
            for group in latest.get("dependencyGroups", []):
                for dep in group.get("dependencies", []):
                    deps.add(dep["id"])
            return name, list(deps)
        except Exception:
            return name, []

    def fetch_github(name):
        """Fetch deps via GitHub SBOM API. Name must be owner/repo."""
        try:
            token = os.environ.get("GITHUB_TOKEN", "")
            headers = {"User-Agent": ua, "Accept": "application/vnd.github+json",
                       "X-GitHub-Api-Version": "2022-11-28"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            url = f"https://api.github.com/repos/{name}/dependency-graph/sbom"
            req = urllib.request.Request(url, headers=headers)
            data = json.loads(urllib.request.urlopen(req, timeout=15).read())
            deps = []
            for pkg in data.get("sbom", {}).get("packages", [])[1:]:
                pname = pkg.get("name", "")
                # Strip ecosystem prefix (pip:, npm:, etc.)
                if ":" in pname:
                    pname = pname.split(":", 1)[1]
                if pname and pname != name:
                    deps.append(pname)
            return name, deps
        except Exception:
            return name, []

    fetchers = {
        "npm": fetch_npm, "pypi": fetch_pypi, "cargo": fetch_cargo,
        "rubygems": fetch_rubygems, "go": fetch_go, "packagist": fetch_packagist,
        "nuget": fetch_nuget, "github": fetch_github,
    }
    fetcher = fetchers[ecosystem]

    while to_fetch and len(adj) < max_packages:
        batch = {n: d for n, d in to_fetch.items() if n not in adj}
        if not batch:
            break

        new_to_fetch = {}
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(fetcher, name): (name, depth) for name, depth in batch.items()}
            for future in as_completed(futures):
                name, depth = futures[future]
                pkg_name, deps = future.result()
                adj[pkg_name] = deps
                console.print(f"  [{len(adj):3d}] {pkg_name} ({len(deps)} deps)", highlight=False)
                if depth < max_depth:
                    for dep in deps:
                        if dep not in adj and dep not in new_to_fetch:
                            new_to_fetch[dep] = depth + 1

        to_fetch = new_to_fetch

    return adj


def _generate_tree_svg(adj, root, ecosystem, output, title, max_width):
    """Generate layered DAG SVG showing dependency convergence."""
    import html as html_module
    import math

    # --- 1. Assign layers — shared packages pushed to their deepest reachable layer ---
    layer_map = {}

    def assign_layers(name, depth, stack):
        if name in stack:
            return
        if name in layer_map and layer_map[name] >= depth:
            return
        layer_map[name] = depth
        stack.add(name)
        for dep in adj.get(name, []):
            if dep in adj:
                assign_layers(dep, depth + 1, stack)
        stack.discard(name)

    assign_layers(root, 0, set())
    if not layer_map:
        return

    # --- 2. Group by layer, order to minimize edge crossings ---
    max_layer = max(layer_map.values())
    layers = [[] for _ in range(max_layer + 1)]
    for name, layer in layer_map.items():
        layers[layer].append(name)

    # Reverse adjacency for parent lookup
    parents_of = {}
    for parent, children in adj.items():
        for child in children:
            if child in layer_map:
                parents_of.setdefault(child, []).append(parent)

    # Barycenter heuristic — order by average parent position
    for layer in layers:
        layer.sort()
    for _ in range(8):
        for l in range(1, len(layers)):
            order = {}
            for node in layers[l]:
                parent_pos = []
                for p in parents_of.get(node, []):
                    if p in layer_map and p in layers[layer_map[p]]:
                        parent_pos.append(layers[layer_map[p]].index(p))
                if parent_pos:
                    order[node] = sum(parent_pos) / len(parent_pos)
            layers[l].sort(key=lambda n: order.get(n, float('inf')))

    # --- 3. Risk scores from DB ---
    scores_db = {}
    try:
        from ossuary.db.session import session_scope
        from ossuary.db.models import Package, Score
        with session_scope() as session:
            for name in layer_map:
                db_name = name.replace("_", "-").lower() if ecosystem == "pypi" else name
                pkg = session.query(Package).filter(
                    Package.name == db_name, Package.ecosystem == ecosystem,
                ).first()
                if pkg:
                    latest = (
                        session.query(Score).filter(Score.package_id == pkg.id)
                        .order_by(Score.calculated_at.desc()).first()
                    )
                    if latest:
                        scores_db[name] = {
                            "score": latest.final_score,
                            "risk_level": latest.risk_level,
                            "contributors": max(latest.unique_contributors, 1),
                        }
    except Exception:
        pass

    def score_color(score):
        if score >= 80: return "#c62828"
        elif score >= 60: return "#d84315"
        elif score >= 40: return "#f57f17"
        elif score >= 20: return "#558b2f"
        else: return "#2e7d32"

    # --- 4. Block positions ---
    block_h = 26
    layer_gap = 50
    pad_top = 55
    pad_bottom = 70
    pad_x = 20
    min_bw = 36
    max_bw = 180
    gap = 6

    max_contribs = max(
        (scores_db.get(n, {}).get("contributors", 1) for n in layer_map),
        default=1,
    )

    def bwidth(name):
        c = scores_db.get(name, {}).get("contributors", 1)
        ratio = math.sqrt(c) / math.sqrt(max(max_contribs, 1))
        return max(min_bw, int(max_bw * (0.15 + 0.85 * ratio)))

    positions = {}
    available = max_width - 2 * pad_x

    for l, layer in enumerate(layers):
        y = pad_top + l * (block_h + layer_gap)
        widths = [bwidth(n) for n in layer]
        total = sum(widths) + gap * max(len(layer) - 1, 0)

        # Scale down if too wide
        g = gap
        if total > available and len(layer) > 1:
            scale = available / total
            widths = [max(min_bw, int(w * scale)) for w in widths]
            total = sum(widths) + gap * max(len(layer) - 1, 0)
            if total > available:
                g = max(2, (available - sum(widths)) // max(len(layer) - 1, 1))
                total = sum(widths) + g * max(len(layer) - 1, 0)

        x = pad_x + (available - total) / 2
        for i, name in enumerate(layer):
            positions[name] = (x, y, widths[i])
            x += widths[i] + g

    # --- 5. Build SVG ---
    total_h = pad_top + (max_layer + 1) * (block_h + layer_gap) + pad_bottom
    cx = max_width / 2

    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{max_width}" height="{int(total_h)}" '
        f'style="background: #fafafa; font-family: \'Comic Sans MS\', \'Chalkboard SE\', cursive, sans-serif;">',

        f'<text x="{cx}" y="32" text-anchor="middle" '
        f'font-size="20" font-weight="bold" fill="#333">'
        f'{html_module.escape(title)}</text>',
    ]

    # Edges (behind blocks) — S-curves
    for parent, children in adj.items():
        if parent not in positions:
            continue
        px, py, pw = positions[parent]
        pcx = px + pw / 2
        pby = py + block_h
        for child in children:
            if child not in positions:
                continue
            ccx_pos, cy, cw = positions[child]
            ccx = ccx_pos + cw / 2
            mid = (pby + cy) / 2
            svg.append(
                f'<path d="M{pcx:.0f},{pby:.0f} C{pcx:.0f},{mid:.0f} {ccx:.0f},{mid:.0f} {ccx:.0f},{cy:.0f}" '
                f'fill="none" stroke="#ddd" stroke-width="1" opacity="0.5"/>'
            )

    # Blocks + labels
    for name, (x, y, w) in positions.items():
        info = scores_db.get(name, {})
        score = info.get("score", -1)
        color = score_color(score) if score >= 0 else "#9e9e9e"

        svg.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w}" height="{block_h}" '
            f'rx="2" ry="2" fill="{color}" stroke="#222" stroke-width="0.8"/>'
        )

        max_chars = max(2, int(w / 7))
        display = name if len(name) <= max_chars else name[:max_chars - 2] + ".."
        fs = 10 if w >= 120 else (9 if w >= 60 else 7)
        svg.append(
            f'<text x="{x + w/2:.1f}" y="{y + block_h/2 + 1:.1f}" '
            f'text-anchor="middle" dominant-baseline="middle" '
            f'font-size="{fs}" fill="#fff">{html_module.escape(display)}</text>'
        )

    # Caption — worst foundation package (bottom half of tree)
    mid_layer = max_layer // 2
    foundation = [n for n in layer_map if layer_map[n] >= mid_layer and n in scores_db]
    worst = max(foundation, key=lambda n: scores_db[n]["score"], default=None) if foundation else None

    caption_y = total_h - pad_bottom + 20
    if worst:
        wi = scores_db[worst]
        svg.append(
            f'<text x="{cx}" y="{caption_y}" text-anchor="middle" '
            f'font-size="13" fill="#555" font-style="italic">'
            f'Everything converges on \u201c{html_module.escape(worst)}\u201d '
            f'({wi["contributors"]} contributor{"s" if wi["contributors"] != 1 else ""}, '
            f'score {wi["score"]})</text>'
        )
    else:
        svg.append(
            f'<text x="{cx}" y="{caption_y}" text-anchor="middle" '
            f'font-size="13" fill="#555" font-style="italic">'
            f'{len(layer_map)} packages in the dependency tree</text>'
        )

    svg.append(
        f'<text x="{cx}" y="{caption_y + 22}" text-anchor="middle" '
        f'font-size="9" fill="#bbb">generated by ossuary \u2014 inspired by xkcd.com/2347</text>'
    )
    svg.append('</svg>')

    with open(output, "w") as f:
        f.write('\n'.join(svg))


def _generate_tower_from_tree(adj, root, ecosystem, output, title, max_width):
    """Generate wide Jenga tower from real dependency graph.

    Each topological layer = a row of blocks. Blocks are positioned under
    their parents so the dependency structure is visible through spatial
    alignment alone — no explicit edges needed.
    Width = contributors, color = risk score.
    """
    import html as html_module
    import math
    from collections import deque

    # --- 1. Assign layers (shared packages pushed to deepest layer) ---
    layer_map = {}

    def assign_layers(name, depth, stack):
        if name in stack:
            return
        if name in layer_map and layer_map[name] >= depth:
            return
        layer_map[name] = depth
        stack.add(name)
        for dep in adj.get(name, []):
            if dep in adj:
                assign_layers(dep, depth + 1, stack)
        stack.discard(name)

    assign_layers(root, 0, set())
    if not layer_map:
        return

    # --- 2. Transitive dependents (for caption) ---
    reverse = {name: [] for name in adj}
    for parent, children in adj.items():
        for child in children:
            if child in reverse:
                reverse[child].append(parent)

    dependents = {name: set() for name in adj}
    for node in adj:
        visited = set()
        queue = deque(reverse.get(node, []))
        while queue:
            p = queue.popleft()
            if p in visited:
                continue
            visited.add(p)
            dependents[node].add(p)
            for gp in reverse.get(p, []):
                if gp not in visited:
                    queue.append(gp)

    # --- 3. Group by layer ---
    max_layer = max(layer_map.values())
    layers = [[] for _ in range(max_layer + 1)]
    for name, layer in layer_map.items():
        layers[layer].append(name)

    # --- 4. Get risk scores from DB ---
    scores_db = {}
    try:
        from ossuary.db.session import session_scope
        from ossuary.db.models import Package, Score
        with session_scope() as session:
            for name in layer_map:
                db_name = name.replace("_", "-").lower() if ecosystem == "pypi" else name
                pkg = session.query(Package).filter(
                    Package.name == db_name, Package.ecosystem == ecosystem,
                ).first()
                if pkg:
                    latest = (
                        session.query(Score).filter(Score.package_id == pkg.id)
                        .order_by(Score.calculated_at.desc()).first()
                    )
                    if latest:
                        # Parse lifetime commits from maturity evidence
                        lifetime_commits = 0
                        lifetime_years = 0
                        bd = latest.breakdown or {}
                        if isinstance(bd, str):
                            import json as _json
                            bd = _json.loads(bd)
                        maturity_ev = (
                            bd.get("score", {})
                            .get("components", {})
                            .get("protective_factors", {})
                            .get("maturity", {})
                            .get("evidence", "") or ""
                        )
                        import re as _re
                        m = _re.search(r"(\d+)\s+commits?\s+over\s+(\d+)\s+year", maturity_ev)
                        if m:
                            lifetime_commits = int(m.group(1))
                            lifetime_years = int(m.group(2))
                        scores_db[name] = {
                            "score": latest.final_score,
                            "base_risk": bd.get("score", {}).get("components", {}).get("base_risk", latest.final_score),
                            "risk_level": latest.risk_level,
                            "contributors": max(latest.unique_contributors, 1),
                            "commits": latest.commits_last_year,
                            "concentration": latest.maintainer_concentration,
                            "lifetime_commits": lifetime_commits,
                            "lifetime_years": lifetime_years,
                        }
    except Exception:
        pass

    def score_color(score):
        if score >= 80: return "#c62828"
        elif score >= 60: return "#d84315"
        elif score >= 40: return "#f57f17"
        elif score >= 20: return "#558b2f"
        else: return "#2e7d32"

    # --- 5. Block sizing ---
    block_h = 32
    row_gap = 4
    pad_x = 30
    available_w = max_width - 2 * pad_x
    min_gap = 3
    branch_gap = 18  # wider gap between unrelated subtrees

    max_contribs = max(
        (scores_db.get(n, {}).get("contributors", 1) for n in layer_map),
        default=1,
    )

    def raw_width(name):
        c = scores_db.get(name, {}).get("contributors", 1)
        ratio = math.sqrt(c) / math.sqrt(max(max_contribs, 1))
        return max(38, int(150 * (0.1 + 0.9 * ratio)))

    # --- 6. Position blocks: each block sits under its parents ---
    # positions[name] = (x_center, width)
    positions = {}

    # Layer 0: root centered
    root_w = raw_width(root)
    positions[root] = (available_w / 2, root_w)

    # Reverse adj lookup: child → [parents that are in the tree]
    parents_of = {}
    for parent, children in adj.items():
        for child in children:
            if child in layer_map and parent in layer_map:
                parents_of.setdefault(child, []).append(parent)

    for l in range(1, max_layer + 1):
        row = layers[l]

        # Compute target x for each block = average center of parents
        targets = {}
        for name in row:
            parents = [p for p in parents_of.get(name, []) if p in positions]
            if parents:
                targets[name] = sum(positions[p][0] for p in parents) / len(parents)
            else:
                targets[name] = available_w / 2

        # Sort by target x
        row.sort(key=lambda n: targets[n])

        # Compute widths
        widths = {n: raw_width(n) for n in row}

        # Place blocks: honor target x, push apart on overlap,
        # add wider gaps between blocks from different parent groups
        placed = []  # [(name, x_left, width)]
        for i, name in enumerate(row):
            w = widths[name]
            ideal_left = targets[name] - w / 2

            if i > 0:
                prev_name, prev_left, prev_w = placed[-1]
                prev_right = prev_left + prev_w

                # Determine gap: wider if from different parent group
                my_parents = set(parents_of.get(name, []))
                prev_parents = set(parents_of.get(prev_name, []))
                if my_parents & prev_parents:
                    gap = min_gap  # siblings — tight
                else:
                    gap = branch_gap  # different branches — wide gap

                min_left = prev_right + gap
                actual_left = max(ideal_left, min_left)
            else:
                actual_left = ideal_left

            placed.append((name, actual_left, w))

        # Center the whole row
        if placed:
            row_left = placed[0][1]
            row_right = placed[-1][1] + placed[-1][2]
            row_w = row_right - row_left
            shift = (available_w - row_w) / 2 - row_left
            for j, (n, xl, w) in enumerate(placed):
                placed[j] = (n, xl + shift, w)

            # Clamp to canvas
            if placed[0][1] < 0:
                nudge = -placed[0][1]
                placed = [(n, xl + nudge, w) for n, xl, w in placed]
            last_right = placed[-1][1] + placed[-1][2]
            if last_right > available_w:
                scale = available_w / last_right
                placed = [(n, xl * scale, max(20, int(w * scale))) for n, xl, w in placed]

        for name, xl, w in placed:
            positions[name] = (xl + w / 2, w)

    # --- 7. Build SVG ---
    title_h = 50
    caption_h = 70
    n_rows = max_layer + 1
    stack_h = n_rows * (block_h + row_gap)
    total_h = title_h + stack_h + caption_h + 20
    center_x = max_width / 2

    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{max_width}" height="{int(total_h)}" '
        f'style="background: #fafafa; font-family: \'Comic Sans MS\', \'Chalkboard SE\', cursive, sans-serif;">',

        '<defs><marker id="arr" markerWidth="10" markerHeight="7" '
        'refX="10" refY="3.5" orient="auto" markerUnits="strokeWidth">'
        '<polygon points="0 0, 10 3.5, 0 7" fill="#1a237e"/>'
        '</marker></defs>',

        f'<text x="{center_x}" y="32" text-anchor="middle" '
        f'font-size="20" font-weight="bold" fill="#333">'
        f'{html_module.escape(title)}</text>',
    ]

    # Draw rows top-down (layer 0 = root = top)
    worst_block = None
    worst_threat = -1

    for l in range(max_layer + 1):
        row_y = title_h + l * (block_h + row_gap)

        for name in layers[l]:
            cx, w = positions[name]
            bx = pad_x + cx - w / 2

            info = scores_db.get(name, {})
            score = info.get("score", -1)
            color = score_color(score) if score >= 0 else "#9e9e9e"
            n_dep = len(dependents.get(name, set()))

            svg.append(
                f'<rect x="{bx:.1f}" y="{row_y:.1f}" width="{w}" height="{block_h}" '
                f'rx="2" ry="2" fill="{color}" stroke="#222" stroke-width="1"/>'
            )

            # Label — always inside the block, multi-line if needed
            if w >= 120:
                fs = 10
            elif w >= 70:
                fs = 9
            elif w >= 55:
                fs = 7
            else:
                fs = 6
            char_w = fs * 0.65  # approximate char width
            max_chars = max(2, int((w - 4) / char_w))
            if len(name) <= max_chars:
                # Single line — fits
                svg.append(
                    f'<text x="{bx + w/2:.1f}" y="{row_y + block_h/2 + 1:.1f}" '
                    f'text-anchor="middle" dominant-baseline="middle" '
                    f'font-size="{fs}" fill="#fff">{html_module.escape(name)}</text>'
                )
            else:
                # Split into two lines at hyphen or midpoint
                mid = len(name) // 2
                # Prefer splitting at a hyphen near the middle
                best = mid
                best_dist = len(name)
                for k, ch in enumerate(name):
                    if ch in ('-', '.', '_') and 0 < k < len(name) - 1:
                        if abs(k - mid) < best_dist:
                            best_dist = abs(k - mid)
                            best = k + 1  # split after the delimiter
                line1 = name[:best]
                line2 = name[best:]
                ty1 = row_y + block_h / 2 - fs * 0.45
                ty2 = row_y + block_h / 2 + fs * 0.65
                tx = bx + w / 2
                svg.append(
                    f'<text x="{tx:.1f}" y="{ty1:.1f}" '
                    f'text-anchor="middle" dominant-baseline="middle" '
                    f'font-size="{fs}" fill="#fff">{html_module.escape(line1)}</text>'
                )
                svg.append(
                    f'<text x="{tx:.1f}" y="{ty2:.1f}" '
                    f'text-anchor="middle" dominant-baseline="middle" '
                    f'font-size="{fs}" fill="#fff">{html_module.escape(line2)}</text>'
                )

            # Track worst structural threat
            # Combines: maintenance fragility × code complexity × tree position
            #
            # Fragility: concentration / sqrt(contributors) — having 20
            #   contributors makes high concentration safe; having 1 doesn't.
            # Irreplaceability: log2(lifetime_commits) — a 10-line utility
            #   is trivial to fork; a 20-year protocol impl is not.
            # Tree impact: how many packages in this tree break if it fails.
            if score >= 0 and n_dep > 0 and name != root:
                contribs = info.get("contributors", 1)
                conc = info.get("concentration", 50)
                commits = info.get("commits", 0)
                lt_commits = info.get("lifetime_commits", 0)

                # Fragility: concentration adjusted by contributor depth
                # 1 contrib → full concentration risk
                # 4 contribs → halved, 16 contribs → quartered
                fragility = (conc / 100) / math.sqrt(max(contribs, 1))
                # Low recent activity amplifies fragility
                if commits <= 1:
                    fragility = min(1.0, fragility * 1.5)
                elif commits <= 5:
                    fragility = min(1.0, fragility * 1.2)

                # Irreplaceability: accumulated code complexity (log-scale)
                # log2(10)/12≈0.28, log2(300)/12≈0.69, log2(2000)/12≈0.92
                irreplaceability = min(1.0, math.log2(max(lt_commits, 10)) / 12)

                # Tree impact: how much breaks if it fails
                tree_impact = 1 + n_dep

                threat = fragility * irreplaceability * tree_impact * 100
                if threat > worst_threat:
                    worst_threat = threat
                    worst_block = {
                        "name": name, "x": bx, "y": row_y, "w": w,
                        "score": score, "contributors": contribs,
                        "commits": commits, "dependents": n_dep,
                        "concentration": conc,
                        "lifetime_commits": lt_commits,
                        "lifetime_years": info.get("lifetime_years", 0),
                    }

    # Arrow
    if worst_block:
        wb = worst_block
        tip_x = wb["x"] - 2
        tip_y = wb["y"] + block_h / 2
        start_x = max(tip_x - 120, 10)
        start_y = tip_y + 60
        svg.append(
            f'<line x1="{start_x:.1f}" y1="{start_y:.1f}" '
            f'x2="{tip_x:.1f}" y2="{tip_y:.1f}" '
            f'stroke="#1a237e" stroke-width="2.5" marker-end="url(#arr)"/>'
        )

    # Caption
    caption_y = title_h + stack_h + 20
    if worst_block:
        w = worst_block
        parts = []
        if w.get("lifetime_years"):
            parts.append(f'{w["lifetime_years"]} years of code')
        parts.append(f'{w["contributors"]} active maintainer{"s" if w["contributors"] != 1 else ""}')
        if w["commits"] == 0:
            parts.append("no commits this year")
        else:
            parts.append(f'{w["commits"]} commit{"s" if w["commits"] != 1 else ""}/yr')
        caption_detail = ", ".join(parts)
        svg.append(
            f'<text x="{center_x}" y="{caption_y}" text-anchor="middle" '
            f'font-size="13" fill="#555" font-style="italic">'
            f'\u201c{html_module.escape(w["name"])}\u201d: {caption_detail}. '
            f'{w["dependents"]} package{"s" if w["dependents"] != 1 else ""} depend on it.</text>'
        )
    else:
        svg.append(
            f'<text x="{center_x}" y="{caption_y}" text-anchor="middle" '
            f'font-size="13" fill="#555" font-style="italic">'
            f'{len(layer_map)} packages in the dependency tree</text>'
        )

    svg.append(
        f'<text x="{center_x}" y="{caption_y + 22}" text-anchor="middle" '
        f'font-size="9" fill="#bbb">generated by ossuary \u2014 inspired by xkcd.com/2347</text>'
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
