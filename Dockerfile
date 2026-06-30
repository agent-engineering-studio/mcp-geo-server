# ============================================================================
#  mcp-geo-server — application image (MCP server + FastAPI test UI)
#
#  This image bundles the Python package. It can run in two modes:
#    * Web UI (default CMD):  uvicorn webui.app:app
#    * MCP server (stdio):    override the command with `mcp-geo-server`
#
#  Build:  docker build -t mcp-geo-server:latest .
#  Web UI: docker run --rm -p 8000:8000 --env-file .env mcp-geo-server:latest
#  MCP:    docker run --rm -i --env-file .env mcp-geo-server:latest mcp-geo-server
#
#  NOTE: GeoServer/PostGIS run separately (see docker-compose.yml). Point
#  GEOSERVER_URL at the reachable host (e.g. http://host.docker.internal:8080/
#  geoserver from Docker Desktop, or the compose service name on a shared net).
# ============================================================================
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    WEBUI_PORT=8000 \
    GEO_MAP_OUTPUT_DIR=/app/maps

WORKDIR /app

# Install dependencies first (better layer caching). README is required because
# pyproject.toml references it as the long-description.
COPY pyproject.toml README.md ./
COPY src ./src
COPY webui ./webui

RUN pip install --upgrade pip \
    && pip install ".[webui]" \
    && mkdir -p "${GEO_MAP_OUTPUT_DIR}"

# Run as an unprivileged user.
RUN useradd --create-home --uid 10001 app \
    && chown -R app:app /app
USER app

EXPOSE 8000

# Health is checked against the static index ("/") so it does not require a
# reachable GeoServer just to report the container as healthy.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request; \
url='http://127.0.0.1:'+os.environ.get('WEBUI_PORT','8000')+'/'; \
urllib.request.urlopen(url, timeout=4)" || exit 1

# Default: serve the FastAPI test UI. Override the command for the MCP server.
CMD ["sh", "-c", "uvicorn webui.app:app --host 0.0.0.0 --port ${WEBUI_PORT}"]


# ============================================================================
#  bootstrap stage — one-shot data initialiser (compose service `geo-init`).
#
#  Adds GDAL (ogr2ogr) + the Postgres client on top of the app image so it can
#  load every shapefile found under /data into PostGIS and publish each one to
#  GeoServer. Kept as a separate stage so the webui/mcp images stay lean.
#
#  Build:  docker build --target bootstrap -t mcp-geo-server:bootstrap .
# ============================================================================
FROM base AS bootstrap

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends gdal-bin postgresql-client \
    && rm -rf /var/lib/apt/lists/*
USER app

# Idempotent: re-running just skips already-loaded tables / published layers.
CMD ["python", "-m", "mcp_geo_server.bootstrap"]
