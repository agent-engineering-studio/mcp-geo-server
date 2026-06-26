# mcp-geo-server

An **intelligent MCP (Model Context Protocol) server** that lets you drive a
**GeoServer** instance in natural language. It is built with the **Microsoft
Agent Framework**: an LLM-backed *agent* is given the GeoServer operations as
tools and exposed as a single MCP tool via `agent.as_mcp_server()`. The LLM
backend is pluggable — **local Ollama**, **Ollama Cloud**, or **Anthropic
Claude**. Any MCP client sends a request like *"how many features in
topp:states?"* and the agent decides which GeoServer operations to call.

Under the hood the agent can: manage workspaces, PostGIS datastores, feature
types, layers and SLD styles via the REST API, and query/edit data via the OGC
services (**WMS**, **WFS GetFeature → GeoJSON**, **WFS-T** insert/update/delete).
It ships with a small **Leaflet** web UI and a **Docker** stack (GeoServer +
PostGIS + Ollama) for local development.

The same async core (`GeoServerClient` + the `geo_*` tool functions) is shared by
the agent and the web UI — there is exactly one place that talks to GeoServer.

---

## Prerequisites

- Python **3.11+**
- Docker + Docker Compose (for the local GeoServer/PostGIS stack)

## 1. Start the stack (Docker)

```bash
make build          # build the app image + pull GeoServer/PostGIS/Ollama
make ollama-pull    # download the LLM model into the Ollama container (once)
make docker-up      # start everything
```

The Compose project is named `mcp-geo-server`; containers are named coherently:

| Service | Container | Endpoint |
|---|---|---|
| `mcp` | `mcp-geo-server-mcp` | <http://localhost:9000/mcp> — intelligent MCP server (streamable-HTTP) |
| `webui` | `mcp-geo-server-webui` | <http://localhost:8000> — Leaflet test UI |
| `geoserver` | `mcp-geo-server-geoserver` | <http://localhost:8080/geoserver> (admin / `geoserver`) |
| `ollama` | `mcp-geo-server-ollama` | <http://localhost:11434> — local LLM backend |
| `postgis` | `mcp-geo-server-postgis` | `localhost:5432` (gis / gis / gis) |

GeoServer runs with `CORS_ENABLED=true` so the browser UI can call OGC services
directly. PostGIS has a healthcheck and GeoServer waits for it. The `mcp` agent
talks to GeoServer and Ollama over the internal Compose network.

## 2. Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,webui]"
cp .env.example .env   # then edit as needed
```

## 3. Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `GEOSERVER_URL` | — (required) | Base URL, e.g. `http://localhost:8080/geoserver` |
| `GEOSERVER_USER` | — (required) | REST username |
| `GEOSERVER_PASSWORD` | — (required) | REST password |
| `GEOSERVER_DEFAULT_WORKSPACE` | _(none)_ | Workspace used when a tool omits one |
| `GEOSERVER_DEFAULT_SRS` | `EPSG:4326` | SRS used when publishing without one |
| `GEOSERVER_TIMEOUT` | `30` | HTTP timeout (seconds) |
| `GEOSERVER_RETRIES` | `2` | Retry attempts on connect/timeout/502/503/504 |
| `GEOSERVER_RETRY_BACKOFF` | `0.5` | Linear backoff factor (seconds × attempt) |
| `GEOSERVER_VERIFY_TLS` | `true` | Verify TLS certificates |
| `GEO_MAP_OUTPUT_DIR` | `./maps` | Where generated maps / downloaded PNGs are saved |
| `WEBUI_PORT` | `8000` | Port for the test UI |
| `GEO_LLM_PROVIDER` | `ollama` | Agent LLM backend: `ollama`, `ollama-cloud` or `anthropic` |
| `OLLAMA_HOST` | `http://localhost:11434` | Local Ollama endpoint (`ollama` provider) |
| `OLLAMA_MODEL` | `qwen2.5` | Ollama model (must support tool calling); cloud model id for `ollama-cloud` |
| `OLLAMA_CLOUD_HOST` | `https://ollama.com` | Ollama Cloud endpoint (`ollama-cloud` provider) |
| `OLLAMA_API_KEY` | _(none)_ | Ollama Cloud API key (required for `ollama-cloud`) |
| `ANTHROPIC_API_KEY` | _(none)_ | Anthropic API key (required for `anthropic`) |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-6` | Claude model (`anthropic` provider) |
| `GEO_MCP_TRANSPORT` | `stdio` | MCP transport: `stdio` or `http` |
| `GEO_MCP_HOST` | `0.0.0.0` | Bind host when transport is `http` |
| `GEO_MCP_PORT` | `9000` | Port when transport is `http` |
| `GEO_ALLOW_DESTRUCTIVE` | `false` | Allow destructive tools (`geo_delete_*`, `geo_wfs_transaction`); blocked otherwise |

Secrets are never hardcoded — everything is read from the environment.

## 4. The intelligent MCP server (Microsoft Agent Framework)

The MCP server is an **agent**, not a flat list of tools. `server.py` builds a
GeoServer agent (`agent.py`: a chat client + the `geo_*` functions as tools) and
exposes it with `agent.as_mcp_server()`. The MCP client therefore sees **one**
tool, `geoserver-agent`, that takes a natural-language `task`.

### Choosing the LLM backend (`GEO_LLM_PROVIDER`)

| Provider | Value | Needs | Notes |
|---|---|---|---|
| Local Ollama | `ollama` | the `ollama` container | Default. No API key; run `make ollama-pull`. |
| Ollama Cloud | `ollama-cloud` | `OLLAMA_API_KEY` | Hosted models at `https://ollama.com`; set `OLLAMA_MODEL` to a cloud model (e.g. `gpt-oss:120b`). |
| Anthropic Claude | `anthropic` | `ANTHROPIC_API_KEY` | Uses `ANTHROPIC_MODEL` (default `claude-sonnet-4-6`). |

Examples:

```bash
# Ollama Cloud
GEO_LLM_PROVIDER=ollama-cloud OLLAMA_API_KEY=sk-... OLLAMA_MODEL=gpt-oss:120b mcp-geo-server

# Anthropic Claude
GEO_LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=sk-ant-... mcp-geo-server
```

For Docker, set the same variables in your shell (or `.env`) before `make
docker-up`; the `mcp` service forwards them. With `ollama-cloud`/`anthropic` the
local `ollama` container is not needed.

Run it over either transport (selected by `GEO_MCP_TRANSPORT`):

```bash
# stdio (for MCP clients that spawn the process, e.g. Claude Desktop)
mcp-geo-server

# streamable-HTTP (long-lived, visible container) at http://localhost:9000/mcp
GEO_MCP_TRANSPORT=http mcp-geo-server
```

In Docker the `mcp` service runs it over HTTP. Example MCP client call:

```python
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession

async with streamablehttp_client("http://localhost:9000/mcp") as (r, w, _):
    async with ClientSession(r, w) as s:
        await s.initialize()
        res = await s.call_tool("geoserver-agent",
                                {"task": "How many features are in topp:states?"})
        print(res.content[0].text)
```

For a stdio MCP client (`claude_desktop_config.json`), point `command` at
`mcp-geo-server` and set the `GEOSERVER_*` / `OLLAMA_*` env vars.

## 5. Run the test UI

```bash
uvicorn webui.app:app --reload --port 8000
```

Open <http://localhost:8000>. The sidebar has a form for each operation (create
workspace / PostGIS datastore, publish a feature type, create + assign a style,
run a WFS query) plus a connection-status indicator. The map uses an
**OpenStreetMap** basemap and shows selected layers as WMS overlays; the
**"Carica come GeoJSON"** button fetches features via WFS and draws them with
popups.

## 6. Tests

```bash
pytest                                  # unit + behavioural (no GeoServer needed)
GEO_RUN_INTEGRATION=1 pytest tests/integration   # live round-trip vs real GeoServer
```

- `tests/test_formatting.py`, `test_styles_helpers.py`, `test_ogc_helpers.py`,
  `test_map_template.py` — pure helpers / template rendering.
- `tests/test_tools_behaviour.py` — every tool driven with a `FakeClient`,
  asserting on the request bodies / params / WFS-T XML (no network).
- `tests/integration/test_live.py` — status + create/list/delete workspace
  round-trip; **skipped** unless `GEO_RUN_INTEGRATION=1`.
- `tests/evals/geo_eval.xml` — read-only eval questions against GeoServer's
  standard sample data (e.g. `topp:states` has 49 features). Confirm the
  expected answers on your live instance.

## Agent tools (28 `geo_*` functions)

These are the tools the agent calls internally to fulfil a request (they are not
exposed individually over MCP — the agent is). `make tools` lists them.


| Tool | Kind | Description |
|---|---|---|
| `geo_get_status` | read | Version + connectivity (`/rest/about/version.json`) |
| `geo_list_workspaces` | read | List workspaces |
| `geo_get_workspace` | read | Get one workspace |
| `geo_create_workspace` | write | Create workspace (optionally default) |
| `geo_delete_workspace` | destructive | Delete workspace (`recurse`) |
| `geo_list_datastores` | read | List datastores in a workspace |
| `geo_get_datastore` | read | Get one datastore |
| `geo_create_datastore_postgis` | write | Create a PostGIS datastore |
| `geo_delete_datastore` | destructive | Delete datastore (`recurse`) |
| `geo_list_featuretypes` | read | List feature types (or available tables) |
| `geo_publish_featuretype` | write | Publish a table as a layer (recalculates bbox) |
| `geo_list_layers` | read | List layers |
| `geo_get_layer` | read | Get one layer |
| `geo_get_layer_bbox` | read | Layer bounding boxes + SRS |
| `geo_update_layer` | idempotent | Set default style / enabled flag |
| `geo_delete_layer` | destructive | Delete layer (+ feature type cleanup) |
| `geo_list_styles` | read | List styles |
| `geo_get_style` | read | Get style SLD |
| `geo_create_style` | write | Create style from SLD string/file |
| `geo_update_style` | idempotent | Replace style SLD |
| `geo_assign_style_to_layer` | idempotent | Assign style to layer (default/extra) |
| `geo_delete_style` | destructive | Delete style (`purge`) |
| `geo_wms_get_capabilities` | read | WMS GetCapabilities |
| `geo_wms_get_map` | read | Build WMS GetMap URL (optionally download PNG) |
| `geo_wfs_get_capabilities` | read | WFS GetCapabilities |
| `geo_wfs_get_feature` | read | WFS GetFeature → GeoJSON (bbox **or** CQL) |
| `geo_wfs_transaction` | write | WFS-T delete / update / raw |
| `geo_build_web_map` | read | Generate a Leaflet HTML map (OSM + WMS overlays) |

### Destructive-operation safety

A Microsoft Agent Framework **function middleware** (`middleware.py`,
`DestructiveGuard`) intercepts destructive tools (`geo_delete_*` and
`geo_wfs_transaction`). Unless `GEO_ALLOW_DESTRUCTIVE=true`, the call is
short-circuited and the agent reports a refusal instead of mutating data — a
deterministic guard that works even though the MCP server is non-interactive
(no human-in-the-loop approval channel). Enable it explicitly to allow deletes.

## Resilience knobs

- **Retry with linear backoff** on connect errors, timeouts, and HTTP
  502/503/504 — controlled by `GEOSERVER_RETRIES` and `GEOSERVER_RETRY_BACKOFF`.
- **Actionable errors**: 401/403/404/405/409/500 are translated into messages
  with a suggested fix.
- **OGC exceptions**: `ServiceExceptionReport` / `ServiceException` (which arrive
  with HTTP 200) are detected and raised with the response body.
- **Logging** on the `mcp_geo_server` logger (set `GEO_LOG_LEVEL=DEBUG`).

## Project layout

```
src/mcp_geo_server/   core: config, client, formatting, agent (Agent Framework),
                      server (MCP stdio/http), tools/ (geo_* functions), templates/
webui/                FastAPI backend (app.py) + static Leaflet UI (static/index.html)
tests/                unit, behavioural, integration, evals
Dockerfile            app image (web UI + MCP agent)
docker-compose.yml    GeoServer 2.28.0 + PostGIS 16-3.4 + Ollama + webui + mcp
```
