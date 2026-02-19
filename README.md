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
| Newcomer takeover patterns | Malicious code injection |
| Economic frustration signals | Active maintainer sabotage |

## Quick Start

```bash
# Install from GitHub
pip install git+https://github.com/anicka-net/ossuary-risk.git

# Set GitHub token for API access (optional but recommended)
export GITHUB_TOKEN=ghp_xxxxxxxxxxxxx

# Initialize database
ossuary init

# Score a package
ossuary score event-stream --ecosystem npm

# Score across ecosystems
ossuary score numpy --ecosystem pypi
ossuary score serde --ecosystem cargo

# Score with historical cutoff (T-1 analysis)
ossuary score event-stream --ecosystem npm --cutoff 2018-09-01

# Output as JSON
ossuary score requests --ecosystem pypi --json

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

When a takeover pattern is detected, the activity bonus is suppressed - high commit activity during a takeover is evidence of the attack, not project health.

See [methodology](docs/methodology.md) for full details.

## Dashboard

```bash
# Install with dashboard dependencies
pip install "ossuary-risk[dashboard] @ git+https://github.com/anicka-net/ossuary-risk.git"

# Run dashboard
streamlit run dashboard.py --server.port 8501
```

Features: risk overview, ecosystem breakdown, package detail with score history, delta detection (biggest movers).

## Validation

Validated on 144 packages across 8 ecosystems:

- **Accuracy**: 96.5%
- **Precision**: 100.0% (zero false positives)
- **Recall**: 80.0%
- **F1 Score**: 0.89

The 5 remaining false negatives are all account compromises on well-governed projects - confirming the known boundary of governance-based detection.

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

MBA thesis research on OSS supply chain risk (due Dec 2026). Key contribution: governance-based risk indicators are observable in public metadata before incidents occur, but they address a specific attack subset - not a universal detector.
