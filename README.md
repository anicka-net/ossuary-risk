# Ossuary

**OSS Supply Chain Risk Scoring** - Where abandoned packages come to rest.

Ossuary analyzes open source packages to identify governance-based supply chain risks before incidents occur. It calculates a risk score based on maintainer concentration, activity levels, and protective factors.

## What It Detects

Ossuary focuses on **governance failures** - the type of vulnerability that enabled attacks like:

- **event-stream** (2018) - Abandoned package handed off to malicious maintainer
- **colors/faker** (2022) - Frustrated maintainer intentionally sabotaged packages

### Detection Capabilities

| Can Detect | Cannot Detect |
|------------|---------------|
| Maintainer abandonment | Account compromise (like ua-parser-js) |
| High concentration risk | Dependency confusion attacks |
| Economic frustration signals | Typosquatting |
| Declining activity trends | Malicious code injection |
| Governance centralization | |

## Quick Start

```bash
# Install
pip install ossuary

# Initialize database (optional, for caching)
ossuary init

# Score a package
ossuary score event-stream --ecosystem npm

# Score with historical cutoff (T-1 analysis)
ossuary score event-stream --ecosystem npm --cutoff 2018-09-01

# Output as JSON
ossuary score requests --ecosystem pypi --json
```

## Risk Levels

| Score | Level | Semaphore | Action |
|-------|-------|-----------|--------|
| 0-20 | Very Low | 🟢 | Routine monitoring |
| 21-40 | Low | 🟢 | Quarterly review |
| 41-60 | Moderate | 🟡 | Monthly review |
| 61-80 | High | 🟠 | Weekly review + contingency plan |
| 81-100 | Critical | 🔴 | Immediate action required |

## Scoring Methodology

```
Final Score = Base Risk + Activity Modifier + Protective Factors
             (20-100)      (-30 to +20)        (-100 to +20)
```

### Base Risk (Maintainer Concentration)

| Concentration | Points |
|---------------|--------|
| <30% | 20 |
| 30-50% | 40 |
| 50-70% | 60 |
| 70-90% | 80 |
| >90% | 100 |

### Activity Modifier

| Commits/Year | Points |
|--------------|--------|
| >50 | -30 |
| 12-50 | -15 |
| 4-11 | 0 |
| <4 | +20 |

### Protective Factors

| Factor | Points |
|--------|--------|
| Tier-1 maintainer (500+ repos or 100K+ stars) | -25 |
| GitHub Sponsors enabled | -15 |
| Organization with 3+ admins | -15 |
| >50M weekly downloads | -20 |
| >10M weekly downloads | -10 |
| <40% concentration | -10 |
| >20 contributors | -10 |
| CII Best Practices badge | -10 |
| **Frustration signals detected** | **+20** |

## API Usage

Start the API server:

```bash
uvicorn ossuary.api.main:app --host 0.0.0.0 --port 8000
```

Query a package:

```bash
curl "http://localhost:8000/score/npm/event-stream"
```

Response:

```json
{
  "package": "event-stream",
  "ecosystem": "npm",
  "score": 100,
  "risk_level": "CRITICAL",
  "semaphore": "🔴",
  "explanation": "🔴 CRITICAL (100). Critical concentration (90%): single person controls nearly all commits. Project appears abandoned (<4 commits/year).",
  "recommendations": [
    "IMMEDIATE: Identify alternative packages or prepare to fork",
    "Do not accept new versions without manual code review"
  ]
}
```

## Development

```bash
# Clone
git clone https://github.com/anicka-net/ossuary-risk.git
cd ossuary-risk

# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Run linter
ruff check src/

# Type check
mypy src/
```

## Configuration

Environment variables:

```bash
# Required for higher GitHub API rate limits
GITHUB_TOKEN=ghp_xxxxxxxxxxxxx

# Database (defaults to SQLite)
DATABASE_URL=postgresql://user:pass@localhost/ossuary

# Repository storage
REPOS_PATH=./repos
```

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        API / CLI                            │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                     Scoring Engine                           │
│  - Base risk (concentration)                                │
│  - Activity modifier                                        │
│  - Protective factors                                       │
│  - Sentiment analysis                                       │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                    Data Collectors                           │
│  GitCollector | GitHubCollector | NpmCollector | PyPICollector│
└─────────────────────────────────────────────────────────────┘
```

## Validation

Validated on 92 packages (20 incidents + 72 controls):

- **Accuracy**: 92.4%
- **Precision**: 100.0%
- **Recall**: 65.0%
- **F1 Score**: 0.79

T-1 analysis confirms **100% predictive detection** of governance-detectable incidents before they occurred.

See [methodology documentation](docs/methodology.md) for details.

## License

MIT

## Academic Context

This project supports MBA thesis research on OSS supply chain risk. Key contribution: demonstrating that meaningful risk indicators are observable in public metadata before incidents occur.
