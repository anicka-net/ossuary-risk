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
