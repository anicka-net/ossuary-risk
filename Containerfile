# Ossuary - OSS Supply Chain Risk Scoring
# Works with both podman and docker:
#   podman build -t ossuary .
#   docker build -t ossuary .

FROM registry.opensuse.org/opensuse/tumbleweed:latest AS base

RUN zypper -n install python313 python313-pip git && \
    zypper clean -a

WORKDIR /app

# Create venv (PEP 668 — Tumbleweed marks system Python as externally managed)
RUN python3.13 -m venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# Copy source and install
COPY pyproject.toml README.md ./
COPY src/ src/
RUN pip install --no-cache-dir ".[dashboard]"

# Copy dashboard files
COPY dashboard.py dashboard_utils.py ./
COPY pages/ pages/

# Create directories for runtime data
RUN mkdir -p /app/repos /app/data

# Default: run the dashboard
EXPOSE 8501 8000

ENV PYTHONUNBUFFERED=1
ENV REPOS_PATH=/app/repos

# Healthcheck for the dashboard
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health')" || exit 1

ENTRYPOINT ["python3", "-m"]
CMD ["streamlit", "run", "dashboard.py", "--server.address=0.0.0.0", "--server.port=8501", "--browser.gatherUsageStats=false"]
