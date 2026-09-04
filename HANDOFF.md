# HANDOFF — LinkedIn Scraper Backend

Notas operativas sobre el actor de Apify y el esquema de Supabase. La mayoría de
los bugs de este proyecto vienen de discrepancias entre lo que el código asume y
lo que el actor / la base realmente esperan. Documentado acá para no
re-descubrirlo.

Para la arquitectura general y los pipelines, ver [README.md](README.md).

## Actor de Apify: `bestscrapers/sales-navigator-scraper-by-filters`

Funciona con **dos flows**, ambos vía `ApifyClient.actor(ACTOR_ID).call(run_input=...)`.
`.call()` bloquea hasta que el run termina y devuelve un `Run`; la respuesta del
actor son los items que empuja a su **default dataset** (se leen con
`dataset(id).list_items().items`, **no** con `iterate_items()`). La respuesta es
un único objeto: `items[0]`.

`.call()` se invoca con **`logger=None`** para desactivar el streaming de logs
del actor (las líneas `[apify.<actor> runId:...]`), que Railway marcaba como
errores.

### Flow 1 — Init Search

Se llama con los filtros (sin `request_id`). Input del pipeline de Leads:

```python
{
    "title_keywords":      combo.get("title_keywords", []),
    "company_headcounts":  [...labels de rango...],   # ver abajo
    "geo_codes":           [103323778, ...],          # ENTEROS, no strings
    "posted_on_linkedin":  "true",                    # STRING "true", no bool
    "seniority_levels":    [...enum exacto...],       # ver abajo
    "limit":               leads_a_pedir,
}
```

Respuesta:

```json
[ { "request_id": "b78d...", "message": "Successfully initialized the search. Use the returned request ID to retrieve results after 5–10 minutes." } ]
```

La nota dice "5–10 minutos" pero en la práctica suele estar listo en ~15–30 s.

### Flow 2 — Fetch Results

Se llama con `{ "request_id": ..., "page": N }`. Respuesta:

```json
[ { "data": [ {...lead...}, ... ], "message": "ok" } ]
```

- **La señal de "listo" es `message == "ok"`, NO un campo `status`.** Bug real:
  el código leía `status`, obtenía `None`, y descartaba 10 leads válidos.
- Mientras la búsqueda corre, devuelve un `message` distinto de `"ok"` y sin
  `data`. Ejemplo visto: `"Your search was added to queue. Please wait and try
  again later!"`.
- Cada página trae hasta **100** items. Una página con menos de 100 es la última.
- El fetch reintenta hasta **30 veces** con 10 s de espera (5 min) si sigue
  "processing".

> **Pedir páginas adicionales NO repite la espera larga de Flow 1.** Flow 1 corre
> una sola vez por búsqueda; las páginas 2, 3… reusan el mismo `request_id` vía
> Flow 2 y tardan segundos. Verificado contando invocaciones.

### Campos que devuelve el actor por lead

`about`, `company`, `company_id`, `first_name`, `full_name`, `job_title`,
`last_name`, `linkedin_url`, `location`, `profile_id`.

**No** devuelve `industry`, `company_size`, `email` ni `posted_on_linkedin`.

`_map_lead` conserva **ambos** `job_title` y `title` con el mismo valor: el ICP
scorer lee `job_title`, y `job_runner`/`message_generator` leen `title`.

Las filas de metadata (sin `linkedin_url`) se descartan: un perfil real siempre
trae esa URL.

### `company_headcounts`

Solo acepta labels de rango exactos: `"Self-employed"`, `"1-10"`, `"11-50"`,
`"51-200"`, `"201-500"`, `"501-1000"`, `"1001-5000"`, `"5001-10000"`, `"10001+"`.
Los combos pueden guardar códigos de letra de LinkedIn (A–I); hay un
`COMPANY_HEADCOUNT_CODE_MAP` que los traduce (B→11-50, C→51-200, D→201-500, …).
Los labels válidos pasan sin cambios.

### `seniority_levels`

Enum exacto, y **un valor inválido hace que el actor rechace el input entero**
(`InvalidRequestError`), tirando el run:

`"Owner/Partner"`, `"CXO"`, `"Vice President"`, `"Director"`,
`"Experienced Manager"`, `"Entry Level Manager"`, `"Strategic"`, `"Senior"`,
`"Entry Level"`, `"In Training"`.

`_normalize_seniority_levels` deja pasar los válidos, mapea alias de alta
confianza (`VP`→`Vice President`, `C-Level`→`CXO`, `Owner`/`Partner`→
`Owner/Partner`) y **descarta + loguea** cualquier otro, para que un valor
inesperado nunca tumbe un run. Si aparece un `[seniority] dropped unmapped
values`, revisar si conviene agregar ese alias en vez de perder el filtro.

### `geo_codes`

- Deben ser **enteros** (`"Field input.geo_codes.0 must be integer"` si son
  strings).
- **Ya no hay dict `GEO_CODES` hardcodeado.** Los mercados viven en la tabla
  `markets` (`name`, `geo_code`, `region`, `default_language`, `is_active`) y se
  resuelven con `get_market_geo_code(name)`, con match **case-insensitive**
  (`ilike`).
- **Un mercado = un país = un geo code**, individualmente configurable. El
  viejo meta-mercado `"latam"`, que agrupaba 6 países en una sola entrada de
  código, ya no existe: cada país es su propia fila en `markets`. Eso también
  elimina de raíz el bug de "España en LATAM".
- **El CRM puede mandar varios países de la misma región en `markets`**
  (ej. `["Argentina", "Chile", "Colombia"]`). `run_scraping` los resuelve todos
  con `resolve_markets()` (una sola query) y combina sus `geo_code` en **un
  único array**, en vez de crear una celda por país — el actor acepta varios
  `geo_codes` en un mismo input, así que 5 países cuestan lo mismo que 1: las
  celdas siguen siendo **solo por combo**. Antes de esto se consideró (y se
  descartó) crear celdas país×combo — hubiera multiplicado el tiempo del run
  por la cantidad de países, cada uno con su propia espera de Flow 1 y
  paginación de Flow 2.
- **Todos los mercados de un run deben ser de la misma región** (`region` en
  la tabla). Mezclar regiones (ej. `["Argentina", "Taiwan"]`) lanza
  `MixedRegionMarketsError` antes de gastar ninguna llamada a Apify — no hay
  forma de rotular ni razonar sobre un grupo así como un solo mercado.
- **El actor no dice de qué país vino cada lead** (solo `location` en texto
  libre). Con 1 mercado, `lead["market"]` guarda ese país exacto, sin cambios.
  Con varios países combinados, se guarda el **nombre de la región**
  (`"Latin America"`, vía `region_label()`) — adivinar el país por
  `location` no se implementó, no vale el riesgo de acertar mal.
- **El idioma de los mensajes usa un campo aparte, `lead["language_market"]`**,
  no `lead["market"]`. Es necesario: `market_languages` (en
  `message_generator.py`) está indexado por nombre de país real, y buscar por
  `"Latin America"` ahí no matchea nada — sin este campo, **todo run
  multi-país caería en silencio a inglés**. Con varios países se usa el
  idioma del **primero de la lista** como proxy — una elección deliberada y
  documentada, no una garantía: si la región mezcla idiomas (ej. combinar
  varios países de Asia con scripts distintos), los leads de los países que no
  son el primero pueden recibir el mensaje en el idioma equivocado. Con un
  único mercado, sigue resolviendo por su propio nombre exacto, sin cambios.
- **Un mercado desconocido corta el run** con `MarketNotFoundError: Market 'X'
  not found in markets table`, resuelto **antes** de gastar llamadas a Apify. El
  comportamiento anterior era devolver `geo_codes: []` = sin filtro geográfico,
  que pasaba desapercibido.

### `functions` ya no se manda

Se sacó del input. Era otro campo con enum no publicado y el mismo riesgo de
rechazo total que `seniority_levels`.

### `industry_codes` (solo Bridge) — no confirmado

No está verificado que el actor lo acepte. Como rechaza el input entero ante un
campo desconocido, `_init_bridge_search` lo intenta y, si el actor rechaza,
**reintenta una vez sin ese campo** logueando `"industry_codes not supported by
actor, retrying without it"`. Un error sobre otro campo se propaga sin
enmascararse. **Pendiente de confirmar** con un run real.

### `current_company_names` (Bridge)

Máximo **10 por request** (`MAX_COMPANY_NAMES_PER_BATCH`); listas más largas se
parten en batches.

## Supabase

### Autenticación de entrada

Todo endpoint salvo `/health` exige el header `X-Internal-Api-Key`, comparado
contra la env var `INTERNAL_API_KEY`. **Solo se aplica si esa variable está
seteada** — si falta, el backend acepta a todos y loguea un WARNING al arrancar.
Eso es lo que permitió deployar el backend antes de actualizar el CRM. Si un
request empieza a dar 401 inesperadamente, revisar que el CRM esté mandando el
header y que el valor coincida con el de Railway.

### El backend usa el service role key → **RLS no lo limita**

Consecuencia importante: **RLS no protege contra un bug de filtrado en este
código**. La única defensa real es que cada query filtre por `organization_id` en
el `WHERE`. Esto también explica una confusión histórica: el backend "veía" filas
que el admin del CRM no veía — el backend bypassa RLS, la sesión del usuario no.

### `scraper_leads`

`id`, `run_id`, `organization_id`, `linkedin_url`, `full_name`, `first_name`,
`last_name`, `company`, `title`, `industry`, `company_size`, `location`,
`icp_score`, `temperature`, `search_combo`, `custom1`, `custom2`, `market`,
`exported_to_crm`, `created_at`.

- **No tiene `email`** (causaba `PGRST204`).
- `temperature` **sí se escribe** desde el fix de persistencia: guarda el
  `icp_tier` del scorer. Mientras no se escribía, el CRM la leía como `NULL`,
  su `tempMap[...] ?? 'Cold'` no matcheaba, y todo lead aterrizaba en
  `prospects` como Cold sin importar cómo hubiera puntuado.
- `industry`/`company_size` existen pero no se insertan: el actor no los provee.

### `prospects` — el insert lo hace el CRM

Solo se **lee** para dedup. **`area_id` es uuid NOT NULL** y es dato del CRM
(las áreas cuelgan de `users`/`user_areas`), así que el scraper no tiene de dónde
sacarlo. Otras columnas: `name` (NOT NULL), `lead_temperature`, `scrape_date`
(tipo `date`, no timestamp), `outreach_status` (NOT NULL). No tiene `run_id`.

### `sender_profiles`

`get_sender_profile(profile_id, organization_id)` **exige `organization_id`**
desde el fix de aislamiento cross-tenant.

> ⚠️ **Pendiente de verificar**: que la tabla tenga columna `organization_id`. Si
> no la tiene, la query falla con `PGRST204` y **rompe la generación de mensajes
> en planes premium**. Comprobar con:
> ```sql
> SELECT column_name FROM information_schema.columns WHERE table_name = 'sender_profiles';
> ```
> Deliberadamente **no** se puso un `try/except` que degrade en silencio: eso
> reabriría el agujero de seguridad que ese fix cerró.

### Otras peculiaridades

- `runs`: PK es `id`, **no** `run_id`. Tiene `updated_at`, `total_leads`,
  `hot_count`, `warm_count`, `cold_count` (estos tres ya no se escriben).
- `run_logs`: sí usa `run_id`. **Los logs que ve el CRM salen de
  `log_run(run_id, level, message)`**. Desde el fix de logging, `log_run` escribe
  a **los dos** destinos: stdout de Railway y la tabla.
- `org_combos`: filtra por `is_active`; referencia por `combo_code`.
- `scraper_combos_master`: la columna clave es `code`.
- `monthly_lead_counts`: periodo = **`year_month`** (`"YYYY-MM"`), contador =
  **`count`**. (No es `month`/`lead_count`.)
- `users`: tiene `role` y `organization_id`; `my_role()` y `my_org_id()` (usadas
  por las policies de RLS del CRM) hacen `SELECT ... FROM users WHERE id =
  auth.uid()`, así que **`users.id` debe ser igual al `auth.uid()`** de Supabase
  Auth. `area_id` vive en `users` y en la tabla puente `user_areas`.

### Tablas de Bridge

`bridge_seed_lists`, `bridge_runs`, `bridge_run_logs`, `bridge_candidates` — ver
el README para las columnas. Tienen RLS activo con policies que exigen rol
`admin`/`admin_global` de la misma org (`bridge_run_logs` no lleva RLS porque no
tiene `organization_id`; el backend autoriza vía el run padre).

## Peculiaridades del proxy de Claude (AITokenKing)

- **`base_url` que termina en `/v1` hay que recortarlo**: el SDK de Anthropic
  agrega `/v1` por su cuenta y queda `/v1/v1`.
- **Los nombres de modelo son propios del proxy**, no los IDs oficiales de
  Anthropic. Ninguno de `claude-sonnet-4-6`, `claude-3-5-sonnet-20241022`, etc.
  existe ahí. Consultar la lista real:
  ```bash
  curl https://api.aitokenking.com.tw/api/v1/models -H "x-api-key: <key>"
  ```
  Los modelos Anthropic disponibles al momento de escribir esto:
  `claude-sonnet-5`, `claude-fable-5`, `claude-opus-4.6`, `claude-opus-4.7`,
  `claude-opus-4.8`. Para generación de mensajes en volumen, `claude-sonnet-5`.
- **Soporta al menos 6 llamadas concurrentes sin 429** (verificado). Si aparecen
  429, bajar `MESSAGE_CONCURRENCY` en `message_generator.py`.
- Las respuestas pueden venir **truncadas** a mitad del JSON; `_parse_response`
  rescata `custom1`/`custom2` por separado en ese caso.

## Trampas de arquitectura que ya nos mordieron

- **`supabase-py` es síncrono.** Cualquier `.execute()` dentro de un handler
  async **bloquea el event loop entero**. Todo va en `asyncio.to_thread`. Esto
  causaba 504 en el polling del CRM mientras un run scrapeaba.
- **`scraper/` importa `api/dedup`.** Rompe la separación de capas original
  (scraper = solo Apify), pero es necesario para decidir, página a página, si
  vale la pena paginar más. Está comentado en el código.
- **El dedup contra la base es el techo real de volumen**, no el scraping. El
  actor devuelve los mismos perfiles top para los mismos filtros; una vez
  guardados, los runs siguientes rinden cada vez menos. De ahí el overfetch y el
  backfill por paginación.

## Pendientes / riesgos abiertos

- **`sender_profiles.organization_id`** — verificar que exista (ver arriba).
  Bloquea mensajes en planes premium si falta.
- **`industry_codes` en Bridge** — confirmar con un run real si el actor lo
  acepta. Hay fallback, así que no rompe.
- **Tablas huérfanas** — `org_company_seed_lists`, `org_channel_hooks` y
  `org_icp_keywords` (si llegó a crearse) ya no las lee nadie tras eliminar BD
  Group y revertir el ICP. Se pueden borrar.
- **Volumen de 3000 leads/mes** — alcanzable solo si (a) se implementa paginación
  profunda con dedup dentro del loop, y (b) los combos cubren un pool lo bastante
  grande. Sales Navigator corta cada búsqueda en ~2.500 resultados, así que hace
  falta variedad de combos/mercados. Postergado hasta producción.
- **`icp_scorer` industria** — ya no es un pendiente: el componente de industria
  (baseline fijo de 10) se eliminó junto con los otros dos constantes. El actor
  sigue sin devolver industria; la diferencia es que ahora no se finge que sí.
