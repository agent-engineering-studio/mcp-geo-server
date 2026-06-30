# ============================================================================
#  mcp-geo-server — Makefile
#  Build, run and test the MCP GeoServer server, the web UI and the Docker stack.
#
#  Quick start:
#    make install     # create .venv and install the package + extras
#    make docker-up   # start GeoServer + PostGIS
#    make test        # run the unit/behavioural test suite
#    make run         # start the MCP server (stdio)
#    make webui       # start the FastAPI test UI
#
#  Run `make` or `make help` to list every target.
# ============================================================================

# ---- configuration ---------------------------------------------------------
VENV        ?= .venv
PYTHON      ?= python3
PY          := $(VENV)/bin/python
PIP         := $(VENV)/bin/pip
PYTEST      := $(VENV)/bin/pytest
UVICORN     := $(VENV)/bin/uvicorn

WEBUI_PORT  ?= 8000

# Application Docker image (built from ./Dockerfile).
IMAGE       ?= mcp-geo-server:latest

# Ollama model used by the intelligent MCP agent.
OLLAMA_MODEL ?= qwen2.5

# ---- platform switch: pick native base images per host architecture ---------
# Apple Silicon / arm64 -> multi-arch community images (no QEMU emulation).
# Intel / amd64          -> the canonical upstream images.
# The GeoServer data-dir path differs between the two GeoServer distributions,
# so it travels alongside the image choice.
ARCH := $(shell uname -m)
ifeq ($(filter $(ARCH),arm64 aarch64),$(ARCH))
  POSTGIS_IMAGE      ?= imresamu/postgis:16-3.4
  GEOSERVER_IMAGE    ?= kartoza/geoserver:2.28.0
  GEOSERVER_DATA_DIR ?= /opt/geoserver/data_dir
else
  POSTGIS_IMAGE      ?= postgis/postgis:16-3.4
  GEOSERVER_IMAGE    ?= docker.osgeo.org/geoserver:2.28.0
  GEOSERVER_DATA_DIR ?= /opt/geoserver_data
endif
export POSTGIS_IMAGE GEOSERVER_IMAGE GEOSERVER_DATA_DIR

# Load variables from .env then .env.local (local overrides) so both `make run`
# and `docker compose` interpolation pick them up. .env.local is the per-machine
# config (e.g. OLLAMA_LLM_MODEL, API keys) and is gitignored.
ifneq (,$(wildcard .env))
include .env
export
endif
ifneq (,$(wildcard .env.local))
include .env.local
export
endif

# Sources used by compile / format targets.
PY_SOURCES  := src webui tests

# Marker file: lets `install` short-circuit when the venv is already populated.
STAMP       := $(VENV)/.install-stamp

# Use bash and fail fast on errors inside recipes.
SHELL       := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c

.DEFAULT_GOAL := help

# ----------------------------------------------------------------------------
# Self-documenting help: any target with a `## comment` is listed.
# ----------------------------------------------------------------------------
.PHONY: help
help: ## Show this help
	@echo "mcp-geo-server — available targets:"
	@echo
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
	@echo

# ============================================================================
#  Environment & installation
# ============================================================================
.PHONY: venv
venv: $(VENV)/bin/python ## Create the virtual environment

$(VENV)/bin/python:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip

.PHONY: install
install: $(STAMP) ## Install the package with [dev,webui] extras (editable)
	@echo "Dependencies installed in $(VENV) (run 'make reinstall' to force)."

$(STAMP): pyproject.toml | venv
	$(PIP) install -e ".[dev,webui]"
	@touch $(STAMP)

.PHONY: reinstall
reinstall: ## Force a clean reinstall of all dependencies
	@rm -f $(STAMP)
	$(MAKE) install

.PHONY: env
env: ## Create .env from .env.example if it does not exist
	@if [ ! -f .env ]; then cp .env.example .env && echo "Created .env (edit it)"; \
	else echo ".env already exists"; fi

# ============================================================================
#  Quality: compile & tests
# ============================================================================
.PHONY: compile
compile: install ## Byte-compile every source file (fast syntax check)
	$(PY) -m py_compile $$(find $(PY_SOURCES) -name '*.py')
	@echo "py_compile OK"

.PHONY: tools
tools: install ## List the geo_* tools the intelligent agent can call
	@$(PY) -c "from mcp_geo_server.tools import collect_tools; \
	 fns=collect_tools(); print('Agent tools:', len(fns)); \
	 [print(' ', f.__name__) for f in fns]"

.PHONY: agent-check
agent-check: install ## Build the agent + MCP server offline (no Ollama needed)
	@GEOSERVER_URL=$${GEOSERVER_URL:-http://localhost:8080/geoserver} \
	 GEOSERVER_USER=$${GEOSERVER_USER:-admin} \
	 GEOSERVER_PASSWORD=$${GEOSERVER_PASSWORD:-geoserver} \
	 $(PY) -c "from mcp_geo_server.server import build_mcp_server; \
	 s=build_mcp_server(); print('MCP server built:', s.name)"

.PHONY: test
test: install ## Run the unit/behavioural test suite (no GeoServer needed)
	$(PYTEST) -q

.PHONY: test-verbose
test-verbose: install ## Run the test suite with verbose output
	$(PYTEST) -vv

.PHONY: test-cov
test-cov: install ## Run tests with a coverage report (installs pytest-cov)
	$(PIP) install -q pytest-cov
	$(PYTEST) --cov=mcp_geo_server --cov-report=term-missing

.PHONY: test-integration
test-integration: install ## Run live round-trip tests against a real GeoServer
	GEO_RUN_INTEGRATION=1 $(PYTEST) -q tests/integration

.PHONY: check
check: compile test ## Compile + run the full local test suite

# ============================================================================
#  Run the server / UI
# ============================================================================
.PHONY: run
run: install ## Start the intelligent MCP agent server (transport from GEO_MCP_TRANSPORT)
	$(PY) -m mcp_geo_server.server

.PHONY: webui
webui: install ## Start the FastAPI test UI (http://localhost:$(WEBUI_PORT))
	$(UVICORN) webui.app:app --reload --port $(WEBUI_PORT)

.PHONY: map
map: install ## Generate a demo Leaflet map into $(GEO_MAP_OUTPUT_DIR)
	$(PY) -c "from mcp_geo_server.tools.map import render_map; \
	import pathlib; \
	html=render_map('Demo','http://localhost:8080/geoserver/wms',['topp:states'], \
	bounds=[[24,-130],[50,-66]]); \
	p=pathlib.Path('maps'); p.mkdir(exist_ok=True); \
	(p/'demo.html').write_text(html); print('wrote maps/demo.html')"

# ============================================================================
#  Docker stack (GeoServer + PostGIS)
# ============================================================================
.PHONY: docker-up
docker-up: ## Start the whole stack (GeoServer + PostGIS + web UI + MCP) against HOST Ollama
	@if [ "$(GEO_LLM_PROVIDER)" = "ollama" ] || [ -z "$(GEO_LLM_PROVIDER)" ]; then \
		if ! curl -fsS http://localhost:11434/api/tags >/dev/null 2>&1; then \
			echo "⚠️  Host Ollama not reachable at http://localhost:11434 — start it with: ollama serve &"; \
			echo "    (or use 'make up-ollama-cloud' / 'make up-claude')"; \
			exit 1; \
		fi; \
		echo "✅ Host Ollama reachable."; \
	fi
	docker compose up -d
	@echo
	@echo ">> Stack started ($(ARCH) images: $(GEOSERVER_IMAGE), $(POSTGIS_IMAGE)):"
	@echo "   Web UI     -> http://localhost:$(WEBUI_PORT)        (service 'webui')"
	@echo "   MCP server -> http://localhost:9000/mcp        (service 'mcp', streamable-HTTP)"
	@echo "   GeoServer  -> http://localhost:8080/geoserver  (admin/geoserver)"
	@echo "   PostGIS    -> localhost:5432                   (gis/gis)"
	@echo
	@echo "   LLM: HOST Ollama (run 'make ollama-pull' once for the model)."

.PHONY: docker-down
docker-down: ## Stop the Docker stack (keep volumes)
	docker compose down

.PHONY: init
init: ## (Re)run the data bootstrap: load every shapefile in ./data into PostGIS + publish
	docker compose run --rm --build geo-init

.PHONY: init-force
init-force: ## Like 'init' but drop & reload tables that already exist (DESTRUCTIVE to loaded data)
	GEO_INIT_FORCE=true docker compose run --rm --build geo-init

.PHONY: init-logs
init-logs: ## Show the data-bootstrap (geo-init) logs
	docker compose logs geo-init

.PHONY: styles
styles: ## (Re)apply the thematic SLD styles to the published layers
	docker compose run --rm --build geo-init python -m mcp_geo_server.styling

# Short aliases so `make up` / `make down` work as expected.
.PHONY: up
up: docker-up ## Alias for docker-up

.PHONY: down
down: docker-down ## Alias for docker-down

.PHONY: ps
ps: docker-ps ## Alias for docker-ps

.PHONY: logs
logs: ## Tail logs for ALL services (incl. webui + mcp)
	docker compose logs -f

# ---- LLM provider variants (pick where the agent's model runs) -------------
# `make up` uses HOST Ollama (no Ollama container in the stack). The targets
# below switch provider to a hosted/cloud LLM instead.
.PHONY: up-host-ollama
up-host-ollama: docker-up ## Alias for 'make up' (stack always uses host Ollama by default)

.PHONY: up-ollama-cloud
up-ollama-cloud: ## Start the stack with GEO_LLM_PROVIDER=ollama-cloud (hosted, no Ollama container). Requires OLLAMA_API_KEY in .env.
	@if [ ! -f .env ] || ! grep -qE '^OLLAMA_API_KEY=.+' .env; then \
		echo "⚠️  OLLAMA_API_KEY not set in .env — the ollama-cloud provider needs it (run 'make env' first)."; \
		exit 1; \
	fi
	@echo "✅ Using Ollama Cloud as LLM provider (no Docker Ollama)…"
	GEO_LLM_PROVIDER=ollama-cloud docker compose up -d

.PHONY: up-claude
up-claude: ## Start the stack with GEO_LLM_PROVIDER=anthropic (Claude, no Ollama container). Requires ANTHROPIC_API_KEY in .env.
	@if [ ! -f .env ] || ! grep -qE '^ANTHROPIC_API_KEY=.+' .env; then \
		echo "⚠️  ANTHROPIC_API_KEY not set in .env — the anthropic provider needs it (run 'make env' first)."; \
		exit 1; \
	fi
	@echo "✅ Using Claude (Anthropic) as LLM provider (no Docker Ollama)…"
	GEO_LLM_PROVIDER=anthropic docker compose up -d

.PHONY: docker-clean
docker-clean: ## Stop the Docker stack and delete its volumes (DESTRUCTIVE)
	docker compose down -v

.PHONY: docker-logs
docker-logs: ## Tail the GeoServer logs
	docker compose logs -f geoserver

.PHONY: mcp-logs
mcp-logs: ## Tail the MCP agent logs
	docker compose logs -f mcp

.PHONY: ollama-pull
ollama-pull: ## Pull the LLM model into the HOST Ollama ($(OLLAMA_MODEL))
	@command -v ollama >/dev/null 2>&1 || { echo "⚠️  'ollama' not found on host — install it from https://ollama.com"; exit 1; }
	ollama pull $(OLLAMA_MODEL)

.PHONY: docker-ps
docker-ps: ## Show the status of the Docker services
	docker compose ps

.PHONY: wait-geoserver
wait-geoserver: ## Block until GeoServer answers on :8080
	@echo "Waiting for GeoServer at http://localhost:8080/geoserver ..."
	@until curl -sf -o /dev/null http://localhost:8080/geoserver/web/; do \
		sleep 3; echo "  ...still waiting"; done
	@echo "GeoServer is up."

.PHONY: stack-test
stack-test: docker-up wait-geoserver test-integration ## Bring up Docker, wait, run live tests

# ----------------------------------------------------------------------------
#  Application image (this project's MCP server + web UI)
# ----------------------------------------------------------------------------
.PHONY: build
build: ## Build the app image + pull the GeoServer/PostGIS base images (per host arch)
	@echo ">> Building application image (mcp-geo-server: webui + mcp agent)..."
	docker compose build
	@echo ">> Pulling base images for $(ARCH): $(GEOSERVER_IMAGE) + $(POSTGIS_IMAGE)..."
	docker compose pull postgis geoserver
	@echo
	@echo ">> All images ready:"
	@docker images --format 'table {{.Repository}}:{{.Tag}}\t{{.ID}}\t{{.Size}}' \
		| grep -E 'REPOSITORY|mcp-geo-server|postgis|geoserver'

.PHONY: build-app
build-app: ## Build only the application image via compose
	docker compose build app

.PHONY: image
image: ## Build the application image standalone with docker build ($(IMAGE))
	docker build -t $(IMAGE) .

.PHONY: image-webui
image-webui: env ## Run the web UI from the built image (uses .env)
	docker run --rm -p $(WEBUI_PORT):$(WEBUI_PORT) \
		--env-file .env -e WEBUI_PORT=$(WEBUI_PORT) $(IMAGE)

.PHONY: image-mcp
image-mcp: env ## Run the MCP server (stdio) from the built image (uses .env)
	docker run --rm -i --env-file .env $(IMAGE) mcp-geo-server

.PHONY: image-shell
image-shell: ## Open an interactive shell inside the application image
	docker run --rm -it --entrypoint sh $(IMAGE)

.PHONY: image-push
image-push: ## Push the application image to its registry
	docker push $(IMAGE)

# ============================================================================
#  Housekeeping
# ============================================================================
.PHONY: clean
clean: ## Remove caches, build artefacts and generated maps
	rm -rf .pytest_cache build dist *.egg-info src/*.egg-info
	rm -rf maps _test_maps
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type f -name '*.pyc' -delete

.PHONY: distclean
distclean: clean ## clean + remove the virtual environment
	rm -rf $(VENV)
