# openSUSE / OBS Integration

Ossuary can discover and score all packages in an Open Build Service (OBS)
project that have GitHub upstream repositories. This is designed for
distribution-wide risk assessment.

## Prerequisites

- `osc` CLI tool configured with OBS credentials (`zypper install osc`)
- `GITHUB_TOKEN` environment variable set (for API rate limits)

## Quick start

```bash
# 1. Discover GitHub repos in openSUSE:Factory (Tumbleweed)
ossuary discover-suse

# 2. Score a small batch first to verify
ossuary seed-suse --limit 10

# 3. Score everything (takes hours for large projects)
ossuary seed-suse
```

## Discovery

The discovery step scans an OBS project and finds packages with GitHub
upstream URLs. It checks two sources per package:

1. **`_service` files** — `obs_scm` service entries with explicit git URLs
2. **Spec files** — `URL:` and `Source:` fields

Typical hit rate is ~40-50% across openSUSE:Factory.

### OBS projects

| Project | Packages | Description |
|---------|----------|-------------|
| `openSUSE:Factory` | ~18,000 | Tumbleweed (full rolling release) |
| `openSUSE:Leap:16.0` | ~100 | Leap 16.0 overlay |
| `openSUSE:Leap:15.6` | ~200 | Leap 15.6 overlay |

Note: Leap projects contain only the openSUSE-specific packages on top
of the SLE (SUSE Linux Enterprise) base. For a comprehensive scan, use
`openSUSE:Factory`.

### Options

```bash
# Scan a specific project
ossuary discover-suse --project openSUSE:Leap:16.0

# Custom output file
ossuary discover-suse --output leap16_packages.json

# Resume an interrupted scan
ossuary discover-suse --resume

# Limit to first N packages (useful for testing)
ossuary discover-suse --limit 100

# Adjust rate limiting (be gentle with OBS)
ossuary discover-suse --delay 0.5 --workers 3
```

### Rate limiting

The discovery script includes a thread-safe rate limiter to avoid
overloading the OBS server. Defaults:

- **5 workers** (parallel `osc` processes)
- **0.1s minimum delay** between API calls
- Effective rate: ~5-10 requests/second

For a gentler scan (recommended if you've been warned about load):

```bash
ossuary discover-suse --delay 0.5 --workers 3
```

This reduces the effective rate to ~2 requests/second. A full Factory
scan takes ~3-4 hours at this rate.

### Output format

```json
[
  {
    "obs_package": "podman",
    "obs_project": "openSUSE:Factory",
    "github_owner": "containers",
    "github_repo": "podman",
    "repo_url": "https://github.com/containers/podman",
    "source": "service"
  }
]
```

## Batch scoring

After discovery, score all packages:

```bash
# Score all discovered packages (3 concurrent by default)
ossuary seed-suse

# Score from a specific discovery file
ossuary seed-suse --file leap16_packages.json

# Limit concurrency (less load on GitHub API)
ossuary seed-suse --concurrent 2

# Score first 50 only
ossuary seed-suse --limit 50

# Force re-score even if recently scored
ossuary seed-suse --no-skip-fresh
```

### Performance estimates

| Scenario | Packages | Time | Notes |
|----------|----------|------|-------|
| Leap 16.0 overlay | ~50 | ~25 min | Quick test |
| Factory (first 100) | 100 | ~50 min | Verify setup |
| Factory (full) | ~7,000 | ~12 hours | One-time cost |

Each package takes ~30 seconds (blobless git clone + GitHub API calls).

## Keeping scores fresh

After the initial scoring, use the built-in refresh command:

```bash
# Re-score packages older than 7 days
ossuary refresh --max-age 7

# Re-score only github ecosystem packages
ossuary refresh --ecosystem github --max-age 7

# Force re-score everything
ossuary refresh --max-age 0
```

### Cron example

```bash
# Weekly refresh of stale scores
0 2 * * 0  cd /path/to/ossuary && .venv/bin/ossuary refresh --max-age 7
```

## API access

The REST API provides quick lookups for CI/CD pipelines:

```bash
# Quick check — just score and semaphore
curl http://localhost:8000/check/github/containers/podman
# {"package":"containers/podman","ecosystem":"github","score":12,"risk_level":"LOW","semaphore":"🟢"}

# Full breakdown
curl http://localhost:8000/score/github/containers/podman

# Force fresh score (ignore cache)
curl http://localhost:8000/check/npm/lodash?max_age=0
```

### Pipeline integration

```bash
#!/bin/bash
# Block deployment if dependency risk is too high
RESULT=$(curl -s http://ossuary:8000/check/npm/$PACKAGE)
SCORE=$(echo $RESULT | jq .score)

if [ "$SCORE" -ge 60 ]; then
    echo "RISK: $PACKAGE scored $SCORE — review required"
    exit 1
fi
```

## Container deployment

```bash
# Build and run with podman-compose
podman-compose up -d

# Seed from inside the container
podman exec ossuary-dashboard ossuary seed-suse --file /app/suse_packages.json

# Or mount the discovery file
podman run -v ./suse_packages.json:/app/suse_packages.json:ro \
    ossuary ossuary seed-suse
```
