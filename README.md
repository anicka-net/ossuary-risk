# Ossuary

**OSS Supply Chain Risk Scoring** - Where abandoned packages come to rest.

Ossuary analyzes open source packages to identify governance-based supply chain risks before incidents occur. It calculates a risk score (0-100) based on maintainer concentration, activity patterns, protective factors, and takeover detection.

## What It Detects

Ossuary targets the subset of supply chain attacks where **governance weakness is a precondition** - social engineering takeovers, abandoned packages, governance disputes. High maintainer concentration isn't inherently dangerous (pciutils has been maintained by one person for 28 years), but combined with other signals it becomes meaningful.

| Can Detect | Cannot Detect |
|------------|---------------|
| Social engineering takeover (xz pattern) | Account compromise (stolen tokens) |
| Abandoned packages | Dependency confusion |
| Governance disputes (left-pad pattern) | Typosquatting |
| Newcomer takeover patterns | CI/CD exploits |
| Economic frustration signals | Active maintainer sabotage |

## Quick Start

```bash
# Install from PyPI
pip install ossuary-risk

# Set GitHub token for API access (optional but recommended)
export GITHUB_TOKEN=ghp_xxxxxxxxxxxxx

# Initialize database
ossuary init

# Score a single package
ossuary score event-stream -e npm
ossuary score numpy -e pypi
ossuary score serde -e cargo

# Score with historical cutoff (T-1 analysis)
ossuary score event-stream -e npm --cutoff 2018-09-01

# Score an entire dependency tree
ossuary score-deps transformers -e pypi

# Show dependency tree with risk scores
ossuary deps express

# Generate xkcd-2347 tower visualization
ossuary xkcd-tree transformers -e pypi --tower -o tower.svg

# Batch score from seed file
ossuary seed-custom seeds/pypi-popular.yaml

# Show packages with biggest score changes
ossuary movers
```

## Supported Ecosystems

npm, PyPI, Cargo, RubyGems, Packagist, NuGet, Go, GitHub

## Scoring Methodology

```
Final Score = Base Risk + Activity Modifier + Protective Factors
             (20-100)      (-30 to +20)        (-70 to +20)
```

**Base Risk** from maintainer concentration. **Activity Modifier** rewards active maintenance, penalizes abandonment. **Protective Factors** include maintainer reputation, funding (GitHub Sponsors), org ownership, visibility (downloads/stars), community size, and takeover detection.

**Takeover Detection** (novel contribution): compares each contributor's recent commit share vs historical baseline. A newcomer jumping from 2% to 50% on a mature project triggers an alert. Guards prevent false positives for established contributors, long-tenure maintainers, and internal org handoffs.

See [methodology](docs/methodology.md) for full details.

## Visualization

The `xkcd-tree` command generates dependency tower diagrams inspired by [xkcd 2347](https://xkcd.com/2347/). Block color = risk score, block width = contributor count, arrow = most structurally critical dependency.

```bash
ossuary score-deps transformers -e pypi  # score all deps first
ossuary xkcd-tree transformers -e pypi --tower -o tower.svg
```

## Dashboard

```bash
# Install with dashboard dependencies
pip install "ossuary-risk[dashboard]"

# Run dashboard
ossuary dashboard
```

Features: risk overview, ecosystem breakdown, package detail with score history, delta detection (biggest movers).

## REST API

```bash
ossuary api
```

```bash
curl http://localhost:8100/score/pypi/flask
curl http://localhost:8100/check/npm/express
```

Interactive docs at `http://localhost:8100/docs`.

## Validation

Validated on 164 packages across 8 ecosystems with a formal scoped validation framework:

| Metric | All incidents | In-scope only (Scope B) |
|--------|--------------|------------------------|
| **Accuracy** | 87.2% | 94.7% |
| **Precision** | 96.2% | 96.0% |
| **Recall** | 55.6% | 77.4% |
| **F1 Score** | 0.70 | 0.857 |

Ossuary detects governance risk, not all supply chain attacks. The "in-scope" metrics count only incidents where governance weakness was observable before the attack — governance decay, protestware, weak-governance compromise, and governance risk. Out-of-scope incidents (credential theft on healthy projects, CI/CD exploits) are included in the dataset but not counted against recall.

1 false positive (rxjs). 6 in-scope false negatives, all explainable (community forks, reputation-protected maintainers, untracked ownership transfers).

See [validation report](docs/validation.md) for full analysis.

## Development

```bash
git clone https://github.com/anicka-net/ossuary-risk.git
cd ossuary-risk
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,dashboard]"
cp .env.example .env  # add GITHUB_TOKEN
ossuary init
```

## Configuration

```bash
GITHUB_TOKEN=ghp_xxxxxxxxxxxxx     # GitHub API access (recommended)
DATABASE_URL=sqlite:///ossuary.db  # Default; supports PostgreSQL
OSSUARY_CACHE_DAYS=7               # Score freshness threshold
```

## License

MIT

## Academic Context

MBA thesis research on OSS supply chain risk (due Dec 2026). The tool was co-developed with Claude (Anthropic). AI assistance was used for data collection, analysis scripts, and working notes. All thesis text is the author's own.

Key contribution: governance-based risk indicators are observable in public metadata before incidents occur, but they address a specific attack subset — not a universal detector.
