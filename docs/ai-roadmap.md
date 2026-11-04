# Roadmap AI — migliorie e nuove feature

Analisi di `mcp-geo-server` orientata alle capacità AI: cosa fa oggi, dove si
rompe o non scala, e quali feature nuove sono a portata dato il codice
esistente. Complementare a [`mcp-2026-07-28-upgrade-plan.md`](mcp-2026-07-28-upgrade-plan.md),
che copre la migrazione del protocollo MCP: qui quei punti sono richiamati, non
ripetuti.

Documento redatto il 2026-11-04. Stato del codice analizzato: commit `effbdba`.

**Avanzamento** (aggiornato al 2026-11-04)

| Fase | Stato | PR |
|---|---|---|
| 0 — debiti D1–D4, D7 | in `main` | #9 |
| 1 — eval, output strutturato, guardrail CQL | review: #11 → in `main` con questa PR | #11, #16 |
| 2 — `places.py`, metadata AI, OpenTelemetry | review: #12 → in `main` con questa PR | #12, #16 |
| 3 — retrieval per famiglie, router di modelli, prompt caching | review: #14 → in `main` con questa PR | #14, #16 |
| 4 — chat a più turni, intent dal modello, stili da testo | review: #15 → in `main` con questa PR | #15, #16 |
| 5 — tool MCP di alto livello, server MCP v2 tipizzato | in review | #17 |
| 6 — analitica, report, QA visivo | da fare | — |

Nota: nessuna fase ha ancora una baseline misurata su un LLM reale; il primo
passo dopo il merge è `make eval-resolver ARGS="--record runs/<modello>.json"`.

---

## 1. Sintesi

Il progetto ha già tre "superfici AI":

| Superficie | Dove | Come funziona oggi |
|---|---|---|
| **Agente GeoServer via MCP** | `agent.py`, `server.py` | Agent Framework + 33 tool `geo_*`, esposto come **un** tool MCP opaco |
| **Resolver NL → layer** | `webui/app.py` `POST /api/ask` | Prompt single-shot: catalogo intero + filtri + richiesta → JSON estratto a mano |
| **Euristiche "quasi AI"** | `webui/app.py` | Rilevamento comune/provincia/regione per string match; intent metrico DTM per parole chiave |

Le tre conclusioni principali:

1. **Il resolver è il cuore del prodotto ma non è misurato.** Non esiste un
   eval NL→layer: `tests/evals/geo_eval.xml` copre solo i tool. Qualunque
   modifica al prompt, al modello o al provider oggi è "a sensazione". Va fatto
   **prima** di tutto il resto (§3.1).
2. **Il resolver non scala con il catalogo.** `catalog_prompt()` serializza
   *tutti* i layer a ogni richiesta, e `_load_catalog()` rifà una
   `GetCapabilities` a ogni `/api/ask`. Con centinaia di layer costo, latenza e
   accuratezza peggiorano insieme. La soluzione naturale — retrieval semantico
   con **pgvector** — usa il PostGIS che lo stack ha già (§3.3).
3. **La qualità del resolver dipende dai metadata, che nessuno scrive.** Il
   catalogo è "domain-agnostic" perché legge title/abstract/keywords da
   GeoServer; ma un upload da `/upload` ha come titolo il nome del file e nessuna
   keyword. Un LLM che **genera i metadata all'ingest** migliora il resolver più
   di qualsiasi tuning del prompt (§4.1).

---

## 2. Debiti da chiudere prima (non AI, ma bloccanti)

Questi non sono feature AI, ma ogni feature AI che segue li peggiora se restano
aperti.

| # | Problema | Evidenza | Azione |
|---|---|---|---|
| D1 | **Fase 0 del piano MCP non applicata**: `mcp>=1.0` ancora senza tetto | `pyproject.toml` | `agent-framework>=1.24` + `mcp>=2.2,<3`: dalla 1.24 `as_mcp_server()` gira sull'SDK v2 |
| D2 | Bind MCP di default su `0.0.0.0` | `config.py:152` | Default `127.0.0.1`, `0.0.0.0` esplicito in compose |
| D3 | **Web UI senza autenticazione**: `/api/ask` (costo LLM), `/api/upload`, `POST /api/datastores` (credenziali DB nel body) sono aperti | `webui/app.py` | API key / OIDC davanti alla UI, rate limit su `/api/ask` |
| D4 | Modello Anthropic di default datato | `config.py:143` `claude-sonnet-4-6` | Aggiornare il default a un modello corrente (es. `claude-sonnet-5-5`) |
| D5 | `webui/app.py` è un monolite di ~930 righe che contiene la logica di risoluzione | `webui/app.py` | Spostare resolver, admin-scope e draw-order in `mcp_geo_server/resolve.py` (vedi §4.6) |
| D6 | Livelli amministrativi **hardcoded** su ISTAT 2023 | `_ADMIN_LEVELS` in `webui/app.py` | Spostarli in config YAML come gli stili — oggi contraddicono il "domain-agnostic" del README |
| D7 | Catalogo ricaricato a ogni richiesta | `_load_catalog()` | Cache con TTL + invalidazione dopo ingest/upload |

---

## 3. Migliorie AI all'esistente

### 3.1 Eval NL→layer (priorità massima)

Senza misura non si può scegliere tra `qwen2.5` locale e Claude, né sapere se un
cambio di prompt migliora o peggiora.

- **Dataset**: 50–100 richieste reali in italiano e inglese con l'atteso:
  `layers` (set), `cql_filter` (normalizzato), area amministrativa. Esempi
  ovvi dal README: *"frane lineari del Molise"*, *"alta pericolosità idraulica a
  Bari"*, *"DTM della Valle d'Aosta"* (caso negativo già documentato nel
  prompt: **non** deve includere i layer frane).
- **Metriche**: precision/recall sui layer, exact-match sul CQL, tasso di JSON
  invalido (oggi → HTTP 502), latenza p50/p95, token.
- **Esecuzione**: in CI con risposte registrate (deterministico, gratis); a
  richiesta o nightly contro i provider reali, con una tabella comparativa per
  modello.
- **Dove**: `tests/evals/resolver/*.yaml` + un runner `make eval-resolver`.
  `validate_selection()` è già puro e testabile: il runner lo riusa.

### 3.2 Output strutturato al posto di `_extract_json`

Oggi `_extract_json()` cerca la prima `{` e l'ultima `}` nella risposta. Funziona
finché il modello obbedisce; quando non lo fa, l'utente vede un 502.

Tutti e quattro i provider supportano un vincolo di schema: Anthropic (tool use
con `input_schema` / structured outputs), OpenAI (`response_format` con JSON
Schema), Ollama (`format` con JSON Schema). Agent Framework espone
`response_format` con un modello Pydantic. Azione:

- definire `LayerSelection(BaseModel)` con `layers: list[str]`,
  `cql_filter: str | None`, `explanation: str`, **`confidence: float`**;
- restringere `layers` a un `enum` dei nomi del catalogo (o dei top-k del
  retrieval, §3.3) — l'allucinazione diventa impossibile invece che filtrata a
  valle;
- tenere `validate_selection()` come seconda linea di difesa.

### 3.3 Retrieval semantico del catalogo con pgvector

> **Rivisto in implementazione (#14).** Misurato sul golden set, un top-k
> lessicale perde un layer atteso in 8 richieste su 29: i layer con nomi opachi
> (`com01012023_g`) non condividono parole con la richiesta. La ridondanza vera
> è strutturale (105 dei 113 layer sono famiglie × regioni), quindi il retrieval
> riduce solo le *famiglie* ai membri nominati e tiene sempre i layer singoli:
> 113 → 13 layer quando c'è una regione, nessun layer atteso perso. pgvector e
> gli embedding restano un possibile seguito per sinonimi e traduzioni.

Il catalogo intero nel prompt è il collo di bottiglia di scala. Proposta:

1. All'ingest (bootstrap, upload) e allo start, calcolare un embedding per
   layer da `title + abstract + keywords + nomi attributi + valori distinti
   delle colonne categoriali`.
2. Salvarlo in PostGIS con l'estensione **pgvector** (un'immagine
   `postgis` + `pgvector` o `CREATE EXTENSION vector` nel container esistente).
3. `/api/ask` recupera i top-k (es. 15) layer per similarità e passa al
   resolver solo quelli. Ricerca ibrida: vettore + `pg_trgm` sul nome per i
   match lessicali (nomi di regione nei nomi layer).
4. Embedding locali di default (Ollama `nomic-embed-text` / `bge-m3`
   multilingue) per restare coerenti con il principio "LLM locale", con
   fallback su un provider remoto.

Effetti: prompt di dimensione costante, latenza stabile, e la possibilità di
esporre la stessa ricerca come tool MCP `geo_search_layers` (§4.6).

### 3.4 Prompt caching e riuso

Istruzioni del resolver + filtri sono statici; il catalogo cambia solo
all'ingest. Ordinare il prompt *statico → semi-statico → richiesta* e:

- con Anthropic, marcare il prefisso con `cache_control` (risparmio forte su
  input token ripetuti);
- con Ollama, `keep_alive` per non ricaricare il modello;
- cache applicativa `(query normalizzata, versione catalogo) → selezione`
  per le richieste ripetute (demo, pagine condivise).

### 3.5 Guardrail sul CQL generato

Il `cql_filter` prodotto dal LLM finisce nelle richieste WMS. `_cql_attributes()`
estrae gli identificatori con una regex, ma non c'è una validazione
grammaticale. Proposta:

- parser CQL minimale (o `pygeofilter`) → AST;
- whitelist: solo attributi presenti nel layer selezionato, solo valori presenti
  nei `classes` dello style config o nei valori distinti reali;
- limite di complessità (profondità, numero di clausole) per evitare filtri
  patologici su layer grandi;
- in caso di rifiuto: render senza filtro + avviso, invece di un errore.

### 3.6 Intent e luoghi: dal keyword matching al modello

- `_metric_intent()` riconosce "pendenza", "quota", ecc. solo come token esatti:
  *"quanto sono ripide"* non attiva l'arricchimento DTM. Spostare l'intent
  nell'output strutturato del resolver (`metrics: list[Literal[...]]`).
- `_detect_admin()` fa string matching sui nomi ISTAT. Funziona bene per nomi
  esatti, non per *"vicino a Campobasso"*, *"entro 10 km da Matera"*, o luoghi
  non amministrativi (fiumi, località). Aggiungere al resolver un campo
  `place` + `relation` (`in` / `near` / `within_km`) e risolverlo con il
  gazetteer interno (layer ISTAT) e, opzionalmente, un geocoder esterno.

### 3.7 Router di modelli e fallback

Il provider è uno per processo. Con il `confidence` di §3.2:

- modello piccolo/locale per default;
- se `confidence < soglia`, JSON invalido o zero layer validi → **retry su un
  modello più grande** (es. Ollama → Claude), con trace dell'escalation;
- configurabile: `GEO_LLM_FALLBACK_PROVIDER`, `GEO_LLM_CONFIDENCE_MIN`.

### 3.8 Osservabilità GenAI

Agent Framework emette già span OpenTelemetry. Abilitarli e aggiungere gli
attributi delle *GenAI semantic conventions* (modello, token in/out, latenza,
esito della validazione, layer scartati come allucinati). Esportare verso un
collector nel compose (Jaeger / Grafana Tempo / Langfuse). È la base per vedere
in produzione quello che l'eval (§3.1) vede in CI.

---

## 4. Nuove feature AI

### 4.1 Metadata generati all'ingest ⭐

Il punto con il miglior rapporto valore/costo, perché alimenta direttamente il
resolver.

Quando `bootstrap.py` o `/api/upload` pubblicano un layer, un LLM riceve: nome
file/cartella, schema degli attributi, tipo di geometria, bbox, qualche valore
distinto per colonna, il `.prj`. Restituisce (output strutturato):

- `title`, `abstract`, `keywords` (IT + EN) → scritti su GeoServer via REST;
- descrizione delle colonne (`tipo_movim` → "tipo di movimento franoso");
- proposta di **attributo categoriale per la tematizzazione**.

Opt-in (`GEO_INIT_AI_METADATA=true`), non sovrascrive metadata già scritti a
mano, idempotente come il resto del bootstrap.

### 4.2 Stili da linguaggio naturale

*"Colora le frane per tipo di movimento con una palette rosso-arancio"*.

Il LLM **non** scrive SLD: produce una voce dello schema YAML che
`styling.py` già sa compilare (`kind`, `attribute`, `classes`, colori), più la
regola `assign`. Vantaggi: output validabile con uno schema, SLD sempre
corretto, e lo stile diventa config versionabile in `data/styles.yml`. I valori
delle classi vengono dai valori distinti reali della colonna, non inventati.

### 4.3 Chat multi-turno con raffinamento

Oggi ogni `/api/ask` è indipendente. Feature: lo stato della mappa (layer,
filtro, area) diventa contesto del turno successivo:

- *"mostrami le frane in Puglia"* → *"solo quelle ad alta pericolosità"* →
  *"aggiungi il DTM"* → *"togli i crolli"*.

Il resolver riceve la selezione corrente e restituisce un **delta**
(`add`/`remove`/`set_filter`). Lo stato vive nel client (coerente con la
direzione stateless di MCP 2026-07-28: handle espliciti, niente sessioni).

### 4.4 Domande analitiche: "quante", "quali", "dove di più"

Oggi la chat *mostra* layer; non risponde a domande quantitative. Due livelli:

1. **Aggregati su WFS** (sicuro, subito): conteggi e raggruppamenti per
   attributo sul layer risolto, con il CQL e l'area già calcolati. Risposta
   testuale + piccolo grafico nella chat.
2. **Text-to-SQL su PostGIS** (potente, da recintare): per domande spaziali
   (*"quante frane entro 500 m da una strada nella provincia di Salerno"*).
   Requisiti non negoziabili: ruolo DB **read-only** dedicato, solo `SELECT`
   verificato da parser SQL, `statement_timeout`, limite righe, schema esposto
   solo per le tabelle pubblicate. Il risultato può essere pubblicato come layer
   temporaneo (SQL view GeoServer) per vederlo sulla mappa.

### 4.5 Report e narrativa automatica

A partire dalla selezione corrente: statistiche descrittive, metriche DTM
(`geo_enrich_from_dtm` esiste già e restituisce un sommario), mappa PNG
(`geo_wms_get_map` con download), legenda → il LLM compone un **report** (HTML o
PDF) per un comune o un'area: *"scheda di rischio frana per il comune di X"*.
È un workflow multi-step naturale per Agent Framework (workflow con passi
deterministici + un passo di sintesi).

### 4.6 MCP: dal tool opaco a tool tipizzati + strumenti AI di alto livello

Il piano MCP (§4 di `mcp-2026-07-28-upgrade-plan.md`) propone di esporre i tool
`geo_*` direttamente. Questa analisi aggiunge che la logica oggi chiusa in
`webui/app.py` merita di diventare tool di prima classe, perché è proprio ciò
che un client MCP non sa fare da solo:

| Tool proposto | Origine | Valore |
|---|---|---|
| `geo_search_layers(query, k)` | §3.3 | Ricerca semantica nel catalogo |
| `geo_resolve_request(query)` | `/api/ask` | Layer + CQL + area + draw order pronti |
| `geo_admin_scope(place)` | `_admin_scope()` | Bbox + clip/filtro per un'area |
| `geo_describe_layer(layer)` | `_layer_info()` | Geometria, conteggio, attributi |

Più **MCP prompts** (template riusabili, es. "analisi rischio per comune") e il
catalogo come **resource** (`geo://ws/layer`), già previsti dal piano.

### 4.7 QA visivo della mappa (multimodale)

Dopo il render, un modello vision guarda il PNG della GetMap e segnala problemi
che i controlli numerici non vedono: mappa vuota per un CQL troppo
restrittivo, raster opaco che copre i vettori, palette illeggibile. Utile
soprattutto come controllo nella generazione degli stili (§4.2) e nell'eval.

### 4.8 Qualità dei dati assistita all'ingest

Il codice contiene già riparazioni euristiche (`_demojibake`, fallback
`GEO_INIT_SOURCE_SRS`). Un passo AI all'ingest può:

- proporre l'SRS mancante dal bbox delle coordinate (es. coordinate in metri
  compatibili con EPSG:32633/3003 sull'Italia);
- rilevare encoding errato dai campioni di testo;
- segnalare geometrie invalide e colonne vuote/costanti.

Output: un report di ingest leggibile nella pagina `/upload`, non correzioni
silenziose.

### 4.9 Esplorative (da valutare, non da pianificare)

- **Suscettibilità da frana con ML**: inventario frane + derivate del DTM
  (pendenza, esposizione, curvatura — già calcolate da `enrich.py`) come
  feature per un modello (gradient boosting) → raster di suscettibilità
  pubblicato come coverage. Molto in tema con i dati ISPRA, ma è un progetto di
  data science con validazione di dominio.
- **Geo foundation model su immagini satellitari** (segmentazione, change
  detection) per proporre nuovi poligoni. Richiede GPU e dati che lo stack oggi
  non ha.
- **Input vocale** nella chat (Web Speech API): basso costo, utile sul campo.

---

## 5. Priorità

Ordinate per valore/costo, tenendo conto delle dipendenze.

| Fase | Contenuto | Stima | Dipende da |
|---|---|---|---|
| **0** | Debiti D1–D4, D7 | 1–2 g | — |
| **1** | Eval NL→layer (§3.1) + output strutturato (§3.2) + guardrail CQL (§3.5) | 4–6 g | 0 |
| **2** | Estrazione `resolve.py` (D5, D6) + metadata AI all'ingest (§4.1) + osservabilità (§3.8) | 1 sett. | 1 |
| **3** | pgvector + retrieval (§3.3), prompt caching (§3.4), router modelli (§3.7) | 1 sett. | 1, 2 |
| **4** | Chat multi-turno (§4.3), intent/luoghi nel modello (§3.6), stili NL (§4.2) | 1–2 sett. | 1 |
| **5** | Tool MCP di alto livello (§4.6), in parallelo alla Fase 1 del piano MCP | 1 sett. | 2, piano MCP |
| **6** | Analitica WFS → text-to-SQL (§4.4), report (§4.5), QA visivo (§4.7) | 2–3 sett. | 3, 4 |

La regola: **nessuna feature della Fase 3+ entra senza un caso nell'eval della
Fase 1** che ne misuri l'effetto.

---

## 6. Rischi

| Rischio | Mitigazione |
|---|---|
| Costi LLM non controllati con UI pubblica | Auth + rate limit (D3), cache (§3.4), modello locale di default |
| Text-to-SQL come superficie d'attacco | Ruolo read-only, parser, timeout, nessun accesso fuori dalle tabelle pubblicate |
| Metadata generati errati che peggiorano il resolver | Opt-in, mai sovrascrivere metadata manuali, misurati nell'eval |
| Modelli locali piccoli non reggono l'output strutturato | Eval per modello; router con fallback (§3.7) |
| Lock-in su un provider | Restare dietro l'astrazione chat client di Agent Framework, già multi-provider |
