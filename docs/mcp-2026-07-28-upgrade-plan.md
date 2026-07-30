# Piano di aggiornamento a MCP 2026-07-28

Analisi di `mcp-geo-server` rispetto alla revisione
[2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28) del Model
Context Protocol, e piano di migrazione/miglioramento.

Documento redatto il 2026-07-30. Stato del codice analizzato: commit `b86e61f`.

---

## 1. Sintesi esecutiva

La revisione 2026-07-28 non è un aggiornamento incrementale: **rende MCP
stateless**. Spariscono l'handshake `initialize`, le sessioni di protocollo
(`Mcp-Session-Id`), l'endpoint GET/SSE separato e le richieste server→client.
Cambiano di conseguenza il *modo* in cui un server si presenta
(`server/discover`), il *modo* in cui chiede input all'utente (Multi
Round-Trip Requests) e il *modo* in cui gestisce lo stato tra chiamate
(handle espliciti come argomenti dei tool).

Per questo progetto emergono tre conclusioni:

1. **C'è un blocco architetturale.** `agent-framework-core` dichiara
   `mcp<2,>=1.24.0`, mentre l'unico SDK Python che parla 2026-07-28 è `mcp`
   **v2.0.0**. Il pattern attuale — esporre l'agente LLM come server MCP con
   `agent.as_mcp_server()` — non può raggiungere la nuova spec. Va disaccoppiato
   lo strato MCP dallo strato agente (§4).
2. **C'è un rischio immediato, indipendente dalla spec.** `pyproject.toml`
   dichiara `mcp>=1.0` senza tetto: una `pip install` oggi installa `mcp 2.0.0` e
   `src/mcp_geo_server/server.py` non parte più. Va messo un vincolo **subito**
   (§5, Fase 0).
3. **Il disaccoppiamento è un miglioramento anche a prescindere dalla spec.**
   Esporre direttamente i 25 tool `geo_*` invece di un unico tool opaco
   "chiedi all'agente" elimina un LLM dal percorso, riduce latenza e costo, e
   sblocca tutto ciò che la nuova spec offre per-tool: `outputSchema`,
   `annotations`, ordinamento deterministico, `ttlMs`, `icons`, `x-mcp-header`.

---

## 2. Cosa cambia nella spec 2026-07-28

Estratto dal [changelog ufficiale](https://modelcontextprotocol.io/specification/2026-07-28/changelog),
limitato a ciò che tocca questo progetto.

### Modifiche breaking

| # | Cambiamento | Impatto qui |
|---|---|---|
| 1 | Rimosse le sessioni di protocollo e l'header `Mcp-Session-Id`. Lo stato cross-call si gestisce con **handle espliciti** passati come normali argomenti dei tool | Medio — già `stateless=True`, ma il pattern handle non c'è |
| 2 | **Rimosso l'handshake `initialize`/`notifications/initialized`.** Ogni richiesta porta versione e capability del client in `_meta` (`io.modelcontextprotocol/protocolVersion`, `.../clientCapabilities`, `.../clientInfo`) | Alto — gestito dall'SDK, ma richiede l'SDK v2 |
| 3 | Nuovo RPC **`server/discover`**: il server **DEVE** implementarlo (versioni supportate, capability, identità) | Alto |
| 4 | Endpoint GET e `resources/subscribe` sostituiti da **`subscriptions/listen`** (un unico stream POST long-lived, con opt-in per tipo di notifica) | Basso oggi (nessuna resource), rilevante dopo §6.3 |
| 5 | Rimossi `ping`, `logging/setLevel`, `notifications/roots/list_changed`. Il log level è per-richiesta via `_meta`; il server **NON DEVE** emettere `notifications/message` se la richiesta non lo ha richiesto | Basso |
| 6 | Tasks spostati dal core a una **extension** (`io.modelcontextprotocol/tasks`), con polling `tasks/get` e `tasks/update` | Rilevante (§6.4) |
| 7 | **Multi Round-Trip Requests (MRTR)**: il server non invia più richieste proprie. Restituisce `InputRequiredResult` con `inputRequests`; il client ritenta la richiesta originale con `inputResponses` | **Alto — risolve un problema noto del progetto** (§6.1) |
| 8 | Tutti i result portano un campo obbligatorio `resultType` (`"complete"` \| `"input_required"`) | Gestito dall'SDK |
| 9 | Rimossa la resumability SSE (`Last-Event-ID`, event ID) | Basso |

### Modifiche minori rilevanti

- `tools/list` **DOVREBBE** restituire i tool in **ordine deterministico** (cache
  client + prompt-cache hit rate). `collect_tools()` già garantisce un ordine
  stabile: è un punto di forza da preservare.
- Nuovi campi **obbligatori** `ttlMs` e `cacheScope` sui result di `tools/list`,
  `prompts/list`, `resources/list`, `resources/read`,
  `resources/templates/list` (interfaccia `CacheableResult`).
- Header standard **richiesti** su POST Streamable HTTP: `Mcp-Method`,
  `Mcp-Name`, `MCP-Protocol-Version`. Il server **DEVE** validare che
  corrispondano al body, altrimenti `400` + errore `-32020` (`HeaderMismatch`).
- Nuovo `x-mcp-header` per esporre parametri di tool come header HTTP
  (routing/WAF senza parsing del body).
- `inputSchema`/`outputSchema` accettano qualsiasi keyword JSON Schema 2020-12;
  `structuredContent` accetta qualsiasi valore JSON.
- Codice errore "resource not found" da `-32002` a `-32602`.
- Nuova politica di allocazione dei codici errore: `-32020`..`-32099` riservati
  alla spec.
- Convenzioni OpenTelemetry per la propagazione del trace context in `_meta`
  (`traceparent`, `tracestate`, `baggage`).

### Deprecazioni (finestra minima 12 mesi)

- **Roots, Sampling, Logging** deprecati. Migrazioni suggerite: passare
  file/directory via parametri di tool o resource URI invece di Roots;
  integrare direttamente le API del provider LLM invece di Sampling; loggare su
  `stderr` (stdio) o usare OpenTelemetry invece di Logging.
- **Transport HTTP+SSE** (2024-11-05) riclassificato Deprecated.
- **OAuth 2.0 Dynamic Client Registration** (RFC 7591) deprecato a favore dei
  **Client ID Metadata Documents**.

Buona notizia: il progetto **non usa** Roots, Sampling né `logging/setLevel`, e
non espone HTTP+SSE. Nessun debito da deprecazione.

---

## 3. Analisi dello stato attuale

### 3.1 Come è esposto MCP oggi

```
build_agent()            # agent_framework.Agent + LLM (ollama|ollama-cloud|anthropic)
  └─ tools=collect_tools()   # 25 funzioni async geo_*
  └─ middleware=[DestructiveGuard()]
        ↓
agent.as_mcp_server()    # → UN SOLO tool MCP: "manda una richiesta in linguaggio naturale"
        ↓
stdio  |  StreamableHTTPSessionManager(app=server, stateless=True)  → Starlette Mount("/mcp")
```

Il client MCP vede **un tool opaco**. Tutta la risoluzione
richiesta→operazione avviene dentro un secondo LLM ospitato dal server.

### 3.2 Gap di conformità

| Requisito 2026-07-28 | Stato | Note |
|---|---|---|
| `server/discover` (**MUST**) | ❌ assente | Richiede SDK v2 |
| `_meta` per-richiesta (versione, capability, clientInfo) | ❌ assente | Richiede SDK v2 |
| Nessun `initialize` | ❌ usa `create_initialization_options()` | API v1 |
| `resultType` sui result | ❌ assente | Richiede SDK v2 |
| Statelessness del transport | ✅ già `stateless=True` | Buona base di partenza |
| Nessun `Mcp-Session-Id` emesso | ✅ implicito con `stateless=True` | Da verificare |
| GET/DELETE su `/mcp` → `405` | ⚠️ da verificare | Il `Mount` custom non lo gestisce esplicitamente |
| Validazione header `Mcp-Method`/`Mcp-Name` + `-32020` | ❌ assente | |
| Validazione header `Origin` (**MUST**) | ❌ assente | Anti DNS-rebinding |
| Bind su localhost quando locale (**SHOULD**) | ❌ `GEO_MCP_HOST` default `0.0.0.0` | `config.py:150` |
| `ttlMs`/`cacheScope` su `tools/list` | ❌ assente | |
| `tools/list` in ordine deterministico | ✅ `collect_tools()` | Da preservare |
| `outputSchema` / `structuredContent` | ❌ nessun tool lo dichiara | Impossibile col tool singolo |
| `annotations` (readOnlyHint/destructiveHint) | ❌ assente | I tool distruttivi sono già noti in `middreware.py` |
| `icons`, `title` sui tool | ❌ assente | |
| MRTR per l'approvazione umana | ❌ impossibile con l'architettura attuale | Vedi §6.1 |
| Extension Tasks | ❌ assente | Non ancora nell'SDK v2.0.0 |
| `X-Accel-Buffering: no` su SSE | ⚠️ da verificare | Rilevante dietro nginx |

### 3.3 Problemi trovati indipendenti dalla spec

**A. Igiene dei secret.** `.env.example` è **tracciato da git** e va mantenuto
con soli placeholder: i valori reali appartengono a `.env.local` (già in
`.gitignore`). Se un valore reale finisce in `.env.example`, va rimosso e la
credenziale corrispondente **revocata e riemessa**, perché va considerata
compromessa dal momento in cui viene pushata.

Presidio consigliato: un hook pre-commit di secret scanning (`gitleaks` o
`detect-secrets`) e l'abilitazione del secret scanning + push protection di
GitHub sul repo, così il caso viene intercettato prima del push invece che
dopo.

**B. Dipendenza non vincolata.** `mcp>=1.0` in `pyproject.toml`: `pip install`
oggi risolve a `mcp 2.0.0`, che rimuove le API v1 usate da `server.py`. Build
non riproducibile e rotta.

**C. La superficie MCP non è coperta da test.** `server.py` non compare in
nessun test: `grep` di `build_mcp_server|as_mcp_server|streamable|ClientSession`
in `tests/` non dà risultati. I test coprono bene i tool e gli helper, ma
nessuno verifica il protocollo. L'SDK v2 offre `Client(server)` in-memory, che
rende questi test banali da scrivere.

**D. Limite di 4 MiB sul body.** L'SDK v2 rifiuta con HTTP 413 i body Streamable
HTTP oltre 4 MiB. Rilevante per un server geospaziale: `geo_wfs_get_feature`,
`geo_enrich_from_dtm` con `include_features=True` e `geo_wms_get_map` possono
superarlo. Serve il pattern handle/resource (§6.5).

**E. `geo_build_web_map` scrive HTML su disco** e restituisce un path. Per un
client MCP remoto quel file è inaccessibile. È esattamente il caso d'uso
dell'extension MCP Apps (§6.2).

---

## 4. La decisione architetturale

### Il vincolo

```
agent-framework-core 1.12.1  →  mcp<2,>=1.24.0
mcp 2.0.0 (28/07/2026)       →  unico SDK Python che parla 2026-07-28
```

`agent.as_mcp_server()` è scritto contro l'API `mcp` v1 (lowlevel `Server`,
`create_initialization_options`). **Non esiste un percorso verso 2026-07-28 che
passi da lì**, finché Microsoft non porta agent-framework su `mcp` v2.

### La proposta: invertire i due strati

Oggi l'agente *contiene* il server MCP. La proposta è che il server MCP
*esponga i tool*, e l'agente resti dov'è utile — la chat della web UI.

```
                      PRIMA                                    DOPO

  Client MCP                                   Client MCP
      │ 1 tool opaco                               │ 25 tool tipizzati
      ▼                                             ▼
  as_mcp_server()                              MCPServer (mcp v2)
      │                                             │
  Agent + LLM  ← secondo LLM sul server              │  (nessun LLM)
      │                                             │
  geo_* tools ──────────────────────────────────► geo_* tools  (core condiviso)
                                                    ▲
                                                    │
  webui/app.py ─── Agent + LLM ────────────────── webui/app.py (invariata)
```

I 25 tool sono già funzioni async pure con docstring e type hint: sono
esattamente la forma che `@mcp.tool()` consuma per generare `inputSchema`.
`webui/app.py` **non cambia**: già oggi importa il core direttamente e non passa
per un client MCP (lo dice il suo docstring, righe 3-5). `agent.py` resta come
motore della chat web.

### Perché è un miglioramento, non solo conformità

| | Tool singolo (oggi) | Tool diretti (proposta) |
|---|---|---|
| LLM nel percorso MCP | 2 (client + server) | 1 (solo client) |
| Latenza / costo per chiamata | Alta | Bassa |
| `inputSchema` per operazione | ❌ | ✅ generato dai type hint |
| `outputSchema` / `structuredContent` | ❌ | ✅ |
| `annotations` (readOnly/destructive) | ❌ | ✅ |
| Approvazione umana granulare | ❌ (env flag globale) | ✅ per-tool via MRTR |
| Il client vede cosa sta chiamando | ❌ | ✅ |
| Dipendenza da un LLM lato server | Sì (blocca l'avvio del valore) | No |

Nota di trade-off: si perde la risoluzione in linguaggio naturale *lato server*
per i client MCP. In pratica non è una perdita — è il lavoro che il modello del
client fa già, meglio e con più contesto. La capacità NL resta disponibile nella
web UI, dove serve davvero.

### Alternativa, se si vuole tenere agent-framework sul percorso MCP

Restare su `mcp>=1.28,<2` e fermarsi alla revisione 2025-11-25, aspettando
l'aggiornamento upstream di agent-framework. È una scelta legittima, ma va fatta
consapevolmente: significa non implementare `server/discover` (che è un
**MUST**) e restare fuori conformità dalla revisione corrente. Consigliata solo
come stato temporaneo, con la Fase 0 comunque applicata.

---

## 5. Piano in fasi

Le fasi 0 e 1 sono indipendenti e possono partire subito. Le fasi 2+ presuppongono
la 1.

### Fase 0 — Messa in sicurezza (immediata, ~1h)

Nessun rapporto con la spec: va fatta comunque.

1. Revocare la chiave Ollama esposta; ripristinare `OLLAMA_API_KEY=` in
   `.env.example`; verificare la storia git del file.
2. Vincolare le dipendenze in `pyproject.toml`:
   ```toml
   "mcp>=1.28,<2",   # tetto temporaneo: rimosso nella Fase 1
   ```
3. `GEO_MCP_HOST`: default a `127.0.0.1` invece di `0.0.0.0`; in
   `docker-compose.yml` impostare esplicitamente `GEO_MCP_HOST: 0.0.0.0` (dentro
   un container il bind aperto è corretto e necessario). Allinea al **SHOULD**
   della spec senza rompere il deploy.
4. Aggiungere un CI job che installi con dipendenze risolte al massimo
   (`uv pip install --upgrade`) per far emergere in futuro rotture come la B.

### Fase 1 — Migrazione a `mcp` v2 e conformità core (~2-4 giorni)

Obiettivo: il server parla 2026-07-28 e supera i **MUST**.

1. `pyproject.toml`: `mcp>=2.0,<3`. Spostare `agent-framework` nell'extra
   `webui` (serve solo alla chat), togliendolo dalle dipendenze base del server
   MCP.
2. Riscrivere `src/mcp_geo_server/server.py` su `MCPServer`:

   ```python
   from mcp.server import MCPServer
   from .tools import collect_tools

   def build_mcp_server() -> MCPServer:
       mcp = MCPServer(MCP_SERVER_NAME, instructions=MCP_INSTRUCTIONS)
       for fn in collect_tools():          # l'ordine stabile diventa
           mcp.tool()(fn)                  # l'ordine deterministico di tools/list
       return mcp
   ```

   `server/discover`, `_meta`, `resultType`, la negoziazione di versione e il
   fallback verso i client 2025-era sono gestiti dall'SDK ("serves every earlier
   revision from the same server, with nothing to configure").
3. Sostituire il `Mount` Starlette custom con il runner dell'SDK
   (`mcp.run(transport="streamable-http", ...)`), che porta con sé validazione
   header, `Origin`, `405` su GET/DELETE e `X-Accel-Buffering`. Verificare
   ognuno di questi punti con un test, non darli per assunti.
4. Arricchire i metadata dei tool. `DESTRUCTIVE_TOOLS` in `middleware.py` è già
   l'elenco autorevole: usarlo come sorgente delle `annotations`.
   ```python
   mcp.tool(
       annotations={"readOnlyHint": fn.__name__ not in MUTATING_TOOLS,
                    "destructiveHint": fn.__name__ in DESTRUCTIVE_TOOLS},
   )(fn)
   ```
5. Aggiungere `outputSchema` ai tool con ritorno strutturato stabile —
   `geo_get_status`, `geo_get_layer_bbox`, `geo_enrich_from_dtm` sono i
   candidati migliori (già restituiscono `dict` con forma nota).
6. Impostare `ttlMs`/`cacheScope` per `tools/list` (lista statica → TTL lungo,
   `cacheScope: "public"`).
7. **Test del protocollo** (colma il gap C) con il `Client` in-memory:
   ```python
   async with Client(build_mcp_server()) as client:
       assert client.protocol_version == "2026-07-28"
       tools = await client.list_tools()
       assert [t.name for t in tools] == EXPECTED_ORDER   # determinismo
   ```
8. Rimuovere il tetto `<2` e aggiornare README (sezione transport) e Dockerfile.

**Criterio di uscita**: `server/discover` risponde; un client 2026-07-28 e un
client 2025-11-25 funzionano entrambi; `tools/list` è deterministico e ha
`ttlMs`; GET su `/mcp` dà 405; header mismatch dà `-32020`.

### Fase 2 — Approvazione umana via MRTR (~2-3 giorni)

Il valore singolo più alto della nuova spec per questo progetto. Vedi §6.1.

### Fase 3 — Resource e mappa interattiva (~1 settimana)

Catalogo come resource MCP (§6.3) e MCP Apps per la mappa (§6.2).

### Fase 4 — Operazioni lunghe (bloccata)

Extension Tasks (§6.4). **In attesa dell'SDK**: le release notes di `mcp` v2.0.0
elencano "The tasks extension (SEP-2663) is not part of this release" tra i
*known gaps*. Nel frattempo si applica il mitigation di §6.5.

---

## 6. Miglioramenti abilitati dalla nuova spec

### 6.1 Approvazione umana granulare al posto del flag globale

`middleware.py` documenta esplicitamente il limite attuale:

> *"Because the agent is exposed as a (non-interactive) MCP server, we cannot
> pause for human approval mid-run; instead the guard short-circuits the call and
> returns an actionable refusal."*

La conseguenza è un'alternativa binaria e grossolana: `GEO_ALLOW_DESTRUCTIVE`
disabilita *tutte* le operazioni distruttive, oppure le abilita *tutte* senza
alcuna conferma. **MRTR rimuove esattamente questo limite.** Un tool può
restituire `InputRequiredResult`, il client presenta la conferma all'utente, e
ritenta con `inputResponses`.

Nell'SDK v2 si esprime con la dependency injection `Resolve`, e — punto
importante — *"one tool body serves both eras"*: lo stesso codice funziona anche
sui client 2025.

```python
async def confirm_delete() -> Elicit[Confirmation]:
    return Elicit("Confermi l'eliminazione definitiva del layer?", Confirmation)

@mcp.tool(annotations={"destructiveHint": True})
async def geo_delete_layer(
    layer: str,
    ok: Annotated[ElicitationResult[Confirmation], Resolve(confirm_delete)],
) -> dict:
    if not isinstance(ok, AcceptedElicitation):
        return {"deleted": False, "reason": "annullato dall'utente"}
    ...
```

`GEO_ALLOW_DESTRUCTIVE` resta utile come kill-switch per ambienti non
interattivi (bootstrap, CI), ma non è più l'unico meccanismo. Il guard passa da
"blocca tutto per default" a "chiedi conferma", che è il comportamento che gli
utenti si aspettano.

### 6.2 La mappa dentro la conversazione (MCP Apps)

`geo_build_web_map` genera HTML Leaflet, lo scrive su disco e restituisce un
path — inutile per un client remoto (problema E). L'extension **MCP Apps**
(inclusa nell'SDK v2) è progettata per questo: si registra una resource `ui://`
con l'HTML e si collega al tool via `_meta.ui.resourceUri`; il client la rende
in un iframe sandboxed dentro la chat, con canale bidirezionale verso i tool del
server.

Il risultato: l'utente chiede *"mostrami le frane del Molise"* in Claude Desktop
e **ottiene la mappa interattiva nella conversazione**, senza la web UI separata.
Il template `leaflet_map.html.j2` e `render_map()` sono già la base:
`render_map()` è puro (nessuna rete) e restituisce una stringa HTML — cioè
esattamente il contenuto di una resource `ui://`.

Esiste un esempio ufficiale direttamente pertinente:
[`map-server`](https://github.com/modelcontextprotocol/ext-apps/tree/main/examples/map-server)
(globo CesiumJS) nel repo `ext-apps`.

Da valutare: `_meta.ui.csp` deve consentire il basemap OSM e l'endpoint WMS
pubblico (`GEOSERVER_PUBLIC_URL`), altrimenti i tile non caricano nell'iframe.

### 6.3 Il catalogo dei layer come resource MCP

Oggi il catalogo è raggiungibile solo chiamando dei tool. Modellare i layer come
**resource** (`geo://{workspace}/{layer}`) permette al client di elencarli,
leggerli e metterli in cache — con `ttlMs`/`cacheScope` ora richiesti dalla
spec, e `subscriptions/listen` + `resourcesListChanged` per notificare i
cambiamenti dopo un ingest. `catalog.py` fornisce già la lettura del catalogo
dalle WMS capabilities.

Questo è anche il rimpiazzo consigliato per Roots (deprecato): passare i
riferimenti ai dati via resource URI.

### 6.4 Operazioni lunghe come Task

Candidati naturali: `geo_enrich_from_dtm` (letture raster finestrate + zonal
statistics), il bootstrap/ingest degli shapefile, l'applicazione degli stili in
massa. Oggi bloccano la connessione, con il rischio di timeout degli
intermediari.

L'extension Tasks dà handle durabile, `status` (`working`/`completed`/...),
polling con `pollIntervalMs`, resilienza alla disconnessione e — combinata con
§6.1 — lo stato `input_required` per l'approvazione a metà lavoro.

**Bloccata sull'SDK Python** (non in v2.0.0, additiva in 2.x). Da tracciare;
nessuna azione ora oltre al mitigation seguente.

### 6.5 Handle espliciti e payload grandi

Due spinte convergono: la spec rimuove lo stato di sessione e raccomanda
**handle espliciti** restituiti da un tool e riaccettati come argomento; l'SDK
v2 rifiuta i body oltre 4 MiB (problema D).

Applicazione concreta: `geo_enrich_from_dtm` restituisce già per default un
sommario con un campione di 5 feature invece della lista completa — la direzione
è giusta. Il passo successivo è restituire un **handle** al risultato completo
(es. `enrichment_id`), leggibile a pagine o come resource, invece del flag
`include_features=True` che può sforare il limite. Analogamente per
`geo_wfs_get_feature` su layer grandi.

Le linee guida della spec sulla progettazione degli handle vanno seguite:
opachi, con lifetime dichiarato nella description del tool, e con errore
esplicito su handle scaduto/ignoto perché il modello possa recuperare.

### 6.6 Osservabilità

L'SDK v2 abilita **OpenTelemetry di default**, e la spec documenta la
propagazione del trace context in `_meta`. Con Logging deprecato, questa è la
strada raccomandata. Per uno stack a più servizi (mcp, webui, geoserver,
postgis) il tracing distribuito è un guadagno reale a costo quasi nullo.

### 6.7 `x-mcp-header` per il routing multi-tenant

Opzionale e a bassa priorità, ma se il server dovesse mai servire più istanze
GeoServer, annotare un parametro `workspace` con `x-mcp-header` permetterebbe a
un load balancer di instradare senza leggere il body. Attenzione: la spec avverte
di **non** annotare parametri sensibili, perché gli header sono visibili agli
intermediari.

---

## 7. Rischi e questioni aperte

| Rischio | Impatto | Mitigazione |
|---|---|---|
| Nessun percorso da agent-framework a `mcp` v2 | Alto | Disaccoppiamento (§4); agent-framework resta nella web UI |
| Perdita della risoluzione NL per i client MCP | Medio | Il modello del client la fa meglio; la NL resta nella web UI |
| Tasks non disponibile nell'SDK | Medio | Fase 4 bloccata; mitigazione con handle (§6.5) |
| Limite 4 MiB sui payload geospaziali | Medio | Pattern handle/resource (§6.5) |
| Supporto client per MCP Apps disomogeneo | Basso | Extension opt-in; il fallback resta la web UI |
| Adozione client di 2026-07-28 ancora parziale | Basso | L'SDK v2 serve entrambe le ere dallo stesso server |
| Un solo tool → 25 tool cambia il contratto pubblico | Medio | Breaking change esplicito: bump a 0.2.0 e nota di migrazione nel README |

**Questioni da decidere prima di iniziare la Fase 1**

1. Il tool NL aggregato va **mantenuto in aggiunta** ai 25 tool diretti (come
   26° tool, per i client che preferiscono l'interfaccia conversazionale) o
   **rimosso**? Mantenerlo significa però tenere `agent-framework` — e quindi
   `mcp<2` — sul percorso del server: le due cose sono incompatibili nello stesso
   processo. Se serve entrambi, va fatto come due processi/servizi distinti.
2. Si accetta la rottura del contratto MCP pubblico (§7, ultima riga), o serve
   un periodo di convivenza?

---

## 8. Checklist di conformità

Da usare come criterio di accettazione della Fase 1.

**MUST**
- [ ] `server/discover` implementato e risponde con `supportedVersions`, `capabilities`, `serverInfo`
- [ ] Nessun `initialize` richiesto; `_meta` per-richiesta gestito
- [ ] `resultType` su tutti i result
- [ ] Nessun `Mcp-Session-Id` minted o echoed
- [ ] Header `Origin` validato → `403` se presente e invalido
- [ ] Header `Mcp-Method`/`Mcp-Name`/`MCP-Protocol-Version` validati vs body → `400` + `-32020`
- [ ] Versione non supportata → `400` + `UnsupportedProtocolVersionError`
- [ ] Metodo ignoto su HTTP → `404` + `-32601`
- [ ] `inputSchema` valido (non `null`) su ogni tool
- [ ] Input dei tool validati; output sanitizzati
- [ ] `ttlMs`/`cacheScope` sui result di `tools/list`

**SHOULD**
- [ ] `tools/list` in ordine deterministico
- [ ] GET/DELETE su `/mcp` → `405`
- [ ] `Last-Event-ID` ignorato
- [ ] `X-Accel-Buffering: no` sugli stream SSE
- [ ] Bind su localhost in esecuzione locale
- [ ] `annotations` su ogni tool (`readOnlyHint`, `destructiveHint`)
- [ ] `outputSchema` sui tool a ritorno strutturato
- [ ] `title` sui tool per la visualizzazione
- [ ] Nessun `notifications/message` per richieste senza `io.modelcontextprotocol/logLevel`

**Deprecazioni — verificato che non si usano**
- [x] Roots
- [x] Sampling
- [x] `logging/setLevel`
- [x] Transport HTTP+SSE
- [x] OAuth Dynamic Client Registration

---

## 9. Riferimenti

- [Spec 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28) ·
  [Changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog)
- [Streamable HTTP](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http) ·
  [`server/discover`](https://modelcontextprotocol.io/specification/2026-07-28/server/discover) ·
  [Tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)
- [MRTR](https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/mrtr) ·
  [Tasks](https://modelcontextprotocol.io/extensions/tasks/overview) ·
  [MCP Apps](https://modelcontextprotocol.io/extensions/apps/overview)
- [`mcp` Python SDK v2.0.0](https://github.com/modelcontextprotocol/python-sdk/releases/tag/v2.0.0) ·
  [Migration guide](https://py.sdk.modelcontextprotocol.io/migration/) ·
  [What's new in v2](https://py.sdk.modelcontextprotocol.io/whats-new/)
