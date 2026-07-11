# HANDOFF — LinkedIn Scraper Backend

Notas operativas sobre el actor de Apify y el schema de Supabase. La mayoría
de los bugs de este proyecto vienen de discrepancias entre lo que el código
asume y lo que el actor / la base realmente esperan. Documentado aquí para no
re-descubrirlo.

## Actor de Apify: `bestscrapers/sales-navigator-scraper-by-filters`

El actor funciona con **dos flows**, ambos vía `ApifyClient.actor(ACTOR_ID).call(run_input=...)`.
`.call()` bloquea hasta que el run termina y devuelve un `Run`; la respuesta del
actor son los items que empuja a su **default dataset** (se leen con
`dataset(id).list_items().items`, **no** con `iterate_items()`). La respuesta es
un único objeto: `items[0]`.

### Flow 1 — Init Search

Se llama con los filtros (sin `request_id`). Input:

```python
{
    "title_keywords":      combo.get("title_keywords", []),
    "company_headcounts":  [...labels de rango...],   # ver abajo
    "geo_codes":           [103323778, ...],          # ENTEROS, no strings
    "posted_on_linkedin":  "true",                    # STRING "true", no bool
    "seniority_levels":    combo.get("seniority_levels", []),
    "limit":               leads_per_combo,
}
```

Respuesta:

```json
[ { "request_id": "b78d...", "message": "Successfully initialized the search. Use the returned request ID to retrieve results after 5–10 minutes." } ]
```

Se guarda el `request_id`. La nota dice "5–10 minutos" pero en la práctica los
resultados suelen estar listos en ~15–30 s.

### Flow 2 — Fetch Results

Se llama con `{ "request_id": ..., "page": N }`. Respuesta:

```json
[ { "data": [ {...lead...}, ... ], "message": "ok" } ]
```

- **La señal de "listo" es `message == "ok"`, NO un campo `status`.** (Este fue
  un bug real: el código leía `status`, obtenía `None`, y descartaba 10 leads
  válidos.) Mientras la búsqueda sigue corriendo, devuelve un `message` distinto
  de `"ok"` y sin lista `data`.
- Los leads vienen en `data[]`.
- **Paginación:** cada página trae hasta **100** items. Se pagina `page: 2, 3, …`
  hasta juntar los leads pedidos o hasta que una página devuelva < 100 (última).
- El código reintenta el fetch hasta **30 veces** con 10 s de espera (5 min) por
  si sigue en "processing".

### Campos que devuelve el actor por lead

`about`, `company`, `company_id`, `first_name`, `full_name`, `job_title`,
`last_name`, `linkedin_url`, `location`, `profile_id`.

**No** devuelve `industry`, `company_size`, `email` ni `posted_on_linkedin`.

Mapeo al formato interno (`_map_lead`): se conservan **ambos** `job_title` y
`title` con el mismo valor — el ICP scorer lee `job_title`; `job_runner` y
`message_generator` leen `title`.

### `company_headcounts`

El actor solo acepta labels de rango exactos: `"Self-employed"`, `"1-10"`,
`"11-50"`, `"51-200"`, `"201-500"`, `"501-1000"`, `"1001-5000"`, `"5001-10000"`,
`"10001+"`. Los combos pueden guardar códigos de letra de LinkedIn (A–I); hay un
`COMPANY_HEADCOUNT_CODE_MAP` que los traduce (B→11-50, C→51-200, D→201-500, …).
Los labels válidos pasan sin cambios.

### `geo_codes`

- Deben ser **enteros** (`"Field input.geo_codes.0 must be integer"` si son
  strings). Se guardan como strings en `GEO_CODES` y se castean con `int()` al
  construir el input.
- El lookup por mercado es **case-insensitive** (`GEO_CODES.get(market.lower())`):
  los mercados llegan como `"LATAM"`/`"Taiwan"` pero las claves son minúsculas.
  Si no calza, se manda `geo_codes: []` = **sin filtro geográfico** (bug que
  metía perfiles de fuera de la región).
- **España (`105646813`) NO es LATAM** y fue removida de la lista `latam`.

## Supabase

### Tabla `scraper_leads` (columnas reales)

`id` (uuid), `run_id` (uuid), `organization_id` (uuid), `linkedin_url` (text),
`full_name` (text), `first_name` (text), `last_name` (text), `company` (text),
`title` (text), `industry` (text), `company_size` (text), `location` (text),
`icp_score` (integer), `temperature` (text), `search_combo` (text),
`custom1` (text), `custom2` (text), `market` (text), `exported_to_crm` (boolean),
`created_at` (timestamptz).

**No tiene columna `email`** (causaba `PGRST204`). El insert solo manda columnas
que existen y que llevan datos reales; `industry`, `company_size`, `custom1`,
`custom2` se omiten porque siempre serían `None` (el actor no las provee y los
mensajes se sacaron del pipeline).

### Otras peculiaridades de schema (de sesiones previas)

- `runs`: PK es `id`, no `run_id`. Tiene `updated_at`, `total_leads`,
  `hot_count`, `warm_count`, `cold_count`.
- `run_logs`: sí usa `run_id`. **Los logs que ve el CRM salen de
  `log_run(run_id, level, message)`**, no de `log.info()` (eso va a stdout de
  Railway). El scraper recibe un `log_fn` callback desde `job_runner` para que su
  output de debug aparezca en el CRM.
- `org_combos`: filtra por `is_active`; referencia por `combo_code`.
- `scraper_combos_master`: la columna clave es `code`; filtros en
  `title_keywords`, `seniority_levels`, `company_headcounts`, `functions`.
- `prospects`: `name` (NOT NULL), `lead_temperature`, `scrape_date` (tipo
  `date`, no timestamp). No tiene `run_id`. **`area_id` es uuid NOT NULL** y es
  dato del CRM: por eso el insert a `prospects` NO se hace en el run — el CRM lo
  inserta después con su contexto de área/asignación. El run solo escribe en
  `scraper_leads` (incluyendo `custom1`/`custom2` con los mensajes generados).
- `monthly_lead_counts`: periodo = `month`; upsert por `(organization_id, month)`.

## Pendientes / riesgos abiertos

- `seniority_levels` y `functions`: el actor no publica su enum completo. Si un
  run futuro falla con `InvalidRequestError` en alguno, hay que mapearlos como se
  hizo con `company_headcounts`.
- `industry` en el ICP está en baseline fijo (10/20) hasta enriquecer con otra
  fuente — el actor no devuelve industria.
- El filtro geográfico depende de que los geo codes en `GEO_CODES` sean los
  correctos por mercado (España ya se sacó de LATAM; revisar el resto contra
  resultados reales).
