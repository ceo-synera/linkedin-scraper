# LinkedIn Scraper Backend

Backend FastAPI multi-tenant para el producto **LinkedIn CRM & Outreach Platform** de Insight Software. Corre en Railway y se conecta al CRM vía Supabase.

Aloja **dos productos independientes**:

| Producto | Qué hace | Pipeline |
|---|---|---|
| **Leads** | Busca personas por título/geo para venta directa, las puntúa, y genera mensajes de outreach | `api/job_runner.py` |
| **Bridge** | Busca contactos de partnerships B2B dentro de empresas objetivo, para que un admin los revise a mano | `api/bridge_job_runner.py` |

No comparten tablas ni dedup. Un lead de venta y un contacto de partnership son cosas distintas, y mezclarlos contaminaría el dedup de ambos.

## Arquitectura

```
api/
  main.py                # FastAPI app y endpoints (Leads + Bridge)
  models.py              # Modelos Pydantic de Leads (RunRequest, SenderProfile, SdrAssignment)
  bridge_models.py       # Modelos Pydantic de Bridge
  database.py            # Cliente Supabase, log_run y update_run_status (tablas runs/run_logs)
  job_runner.py          # Pipeline de Leads
  bridge_job_runner.py   # Pipeline de Bridge (estado/logs/dedup propios)
  config_generator.py    # Combos y sender profiles desde Supabase
  message_generator.py   # Generación de mensajes con Claude (paralela, con semáforo)
  dedup.py               # Dedup de Leads contra scraper_leads + prospects
  lead_distributor.py    # Asignación de leads al SDR del run
scraper/
  apify_scraper.py       # Actor de Apify: protocolo de 2 flows, combos de Leads y búsqueda de Bridge
  icp_scorer.py          # Scoring ICP (keywords hardcodeadas)
requirements.txt
Procfile
```

## Principios de diseño

- **Sin secretos persistidos**: las API keys de Apify y Claude/AITokenKing viajan en cada request desde el CRM y nunca se guardan en el servidor. Solo hay 2 env vars.
- **Sin config estática**: los combos se resuelven por run desde `scraper_combos_master` + `org_combos`.
- **Multi-tenant estricto**: `organization_id` va en el `WHERE` de cada query, no como chequeo posterior. Ver [Aislamiento multi-tenant](#aislamiento-multi-tenant).
- **El backend nunca crea tablas ni columnas** — solo lee/escribe sobre el esquema que ya existe en el Supabase del CRM.
- **Nada bloqueante en el event loop**: todo el trabajo síncrono (Supabase, Apify) va en `asyncio.to_thread`. Ver [Concurrencia](#concurrencia-y-el-event-loop).
- **Concurrencia por run**: cada run de Leads usa su propio `/tmp/run_{run_id}/`, que se limpia al terminar (éxito o error).

## Pipeline de Leads (`api/job_runner.py`)

1. `update_run_status` → `running`
2. `get_combo_definitions` desde Supabase
3. `run_scraping` — Apify, con sobre-pedido y paginación (ver abajo)
4. `score_leads` — scoring ICP
5. `dedup_leads` — contra `scraper_leads` + `prospects`
6. `distribute_leads` — asigna al SDR del run
7. `generate_messages_for_batch` — mensajes en paralelo (semáforo de 6)
8. `import_leads_to_supabase` — inserta en **`scraper_leads` únicamente**
9. Bookkeeping best-effort: `run_sdr_assignments` y `monthly_lead_counts`
10. `update_run_status` → `completed` con `total_leads`

Si algo falla, se registra en `run_logs` y el run pasa a `failed` con `error_message`.

### Un solo SDR por run

El CRM manda siempre **exactamente un** `SdrAssignment` (los SDRs perdieron acceso al Scraper; solo el admin corre runs). Con un único destinatario garantizado, `distribute_leads` le asigna el **100% de los leads sin filtrar por mercado** — el ruteo por mercado solo generaba riesgo de leads huérfanos por mismatch de mayúsculas o un `assigned_markets` incompleto. La lógica de reparto por mercado sigue presente como fallback por si alguna vez se vuelven a mandar varios.

### Sobre-pedido y paginación contra el dedup

El actor tiende a devolver los mismos perfiles top para un mismo set de filtros, así que corrida tras corrida el dedup los descarta y el run rinde cada vez menos. Dos compensaciones:

- **`OVERFETCH_MULTIPLIER = 1.2`** — a Apify se le pide 20% más de lo solicitado, como margen contra la pérdida esperada por dedup (antes 1.7×; se bajó porque un margen más grande también alarga cada ciclo de Flow 2, al traer y procesar más resultados por página).
- **Backfill por paginación** — tras cada página, se corre un dedup preliminar contra la base; si quedó corto, se pide la página siguiente (mismo `request_id`, solo Flow 2) hasta **`MAX_COMBO_PAGES = 3`**. Ese dedup solo decide si vale la pena paginar; el `dedup_leads` final de `job_runner` sigue siendo el autoritativo.
- **Cada combo aporta todo lo que encontró** — ya no se recorta a su propio target al terminar (eso se probó y se revirtió: descartaba de más justo cuando un combo rendía bien, en vez de comparar contra el resto). El techo de `total_leads` se aplica una sola vez, al final, en `job_runner` (ver más abajo). Como consecuencia, un combo que ya "tiene suficiente" no salta a los siguientes: la fórmula de reparto solo hace que su `cell_target` caiga a un mínimo de 1, para que todos los combos sigan intentándose y puedan aportar leads de mejor calidad al pool final.
- **Corte de estancamiento en el polling** — si el `message` de Flow 2 (ej. `"Done 12/100"`) no cambia durante 3 intentos consecutivos, se corta el polling de ese combo ahí mismo en vez de esperar los 30 intentos completos (5 min). Log: `"[combo] market='X' code='Y' stalled at 'Z' for 3 consecutive attempts, giving up on this combo early"`.
- **Chequeo de 80% con una ronda de reintento acotada** — si al terminar todas las celdas el total queda por debajo de `ceil(total_leads * 0.8)`, se identifican las celdas que se quedaron cortas *sin* que el actor haya confirmado que no hay más resultados (`exhausted=False`, es decir, pararon por tope de páginas, no por falta real de datos) y se les da un presupuesto fresco de hasta `MAX_COMBO_PAGES` páginas más — reutilizando el mismo `request_id`, sin correr Flow 1 de nuevo. Es una sola ronda, no un bucle: si después sigue por debajo del 80% se continúa igual con lo conseguido. Log de cada desenlace: `"Reached X/Y (Z%) after retry — proceeding"` o `"Run finished below 80% threshold: X of Y requested (Z%) after retry attempt"`.

El target se reparte dinámicamente entre celdas — **una por combo**, sin importar cuántos países traiga `markets`: cada una recibe `ceil(faltante / celdas_restantes)`, así un combo que rinde de menos es compensado por los siguientes.

### Varios países de una misma región en un solo `markets`

El CRM puede mandar varios países de la misma región (ej. `markets: ["Argentina", "Chile", "Colombia"]`). Se combinan en **un solo `geo_codes`** para cada combo — el actor acepta varios geo codes en un mismo input — en vez de crear una celda por país, así que **5 países no tardan más que 1**: las celdas siguen siendo solo por combo. Reglas:

- **Todos los países de un run deben compartir región.** Mezclar regiones (`["Argentina", "Taiwan"]`) lanza `MixedRegionMarketsError` antes de gastar ninguna llamada a Apify.
- El actor no dice de qué país vino cada lead. Con 1 país, `lead["market"]` guarda ese país tal cual. Con varios, guarda el **nombre de la región** (`"Latin America"`).
- El idioma de los mensajes usa un campo aparte (`language_market`), tomado del **primer país de la lista** — necesario porque el idioma se busca por nombre de país real, y `"Latin America"` no matchearía nada. Es una elección documentada, no una garantía: si la región mezcla idiomas, los leads de los demás países pueden salir en el idioma equivocado.

### `prospects` lo inserta el CRM, no este backend

El run escribe **solo** en `scraper_leads`. `prospects` tiene campos que son dominio del CRM —sobre todo `area_id` (uuid **NOT NULL**)— que el scraper no tiene de dónde sacar. El CRM hace ese insert después, con su contexto de área/asignación.

## Scoring ICP (`scraper/icp_scorer.py`)

Las keywords están **hardcodeadas** en el módulo. Hubo una versión que las leía por organización desde `org_icp_keywords`, pero se revirtió: esa tabla no existía en Supabase y el loader no toleraba su ausencia, así que todo run fallaba con `PGRST205`. Además, con la tabla vacía todo lead puntuaba ≤30 y salía COLD.

| Dimensión | Máx | Cómo se calcula |
|---|---|---|
| Job title | 30 | `job_title` contra `PRIORITY_TITLES` |
| Company size | 15 | Fijo — el actor ya filtra por `company_headcounts` |
| Industry | 10 | Baseline fijo — el actor no devuelve industria |
| Actividad en LinkedIn | 15 | Fijo — el actor ya filtra por `posted_on_linkedin` |
| Señal de compra | 20 | Keywords de `AI_SIGNAL_KEYWORDS` en el `about` |

Clasificación: **HOT ≥ 70 / WARM 50-69 / COLD < 50**.

`icp_tier` se calcula y se loguea (`"Scored: 12 HOT, 20 WARM, 13 COLD"`), pero **no se persiste**: la columna `temperature` de `scraper_leads` no se escribe. La temperatura real es un juicio del SDR después del outreach, no algo que se derive al scrapear.

> Si alguna vez se quiere ICP configurable por organización, la forma correcta es **híbrida**: estos defaults hardcodeados como base, y la tabla por org como *override* opcional. Así una org nueva funciona out-of-the-box y nada se rompe si la tabla no existe.

## Bridge (`api/bridge_job_runner.py`)

Bridge busca contactos de partnerships dentro de empresas/industrias objetivo. **No genera mensajes de outreach** — solo descubre y organiza candidatos para revisión humana. Reemplaza por completo al viejo "BD Group", que fue eliminado.

1. `update_bridge_run_status` → `running`
2. `get_bridge_seed_list` — la seed list, filtrada por `organization_id` **y** `id`
3. `run_bridge_scraping` — Apify con los filtros de la seed list
4. `dedup_bridge_candidates` — **exclusivamente contra `bridge_candidates`**
5. `import_bridge_candidates` — inserta con `verification_status='pending'`
6. `update_bridge_run_status` → `completed` con `total_candidates`

### Modos de búsqueda

Combinables: se manda lo que la seed list tenga configurado.

- **Por empresa** — `company_names` → `current_company_names`. Se batchea de a 10 (límite del actor) y se piden 3 resultados por empresa (`BRIDGE_RESULTS_PER_COMPANY`), porque poca gente tiene estos títulos en una misma empresa.
- **Por filtros** — `industry_codes`, `company_headcounts`, `geo_codes`. Sin ancla por empresa, se topea en 1 página (100).

Los filtros vacíos **se omiten** del input en vez de mandarse como arrays vacíos.

### Keywords fijas

`BRIDGE_TITLE_KEYWORDS` (17 títulos de partnerships en inglés, español y chino) es fijo y **no configurable por el admin**, a diferencia de los combos de Leads. El admin solo elige en qué empresas/industrias buscar.

### `industry_codes` puede no estar soportado

No está confirmado que el actor acepte ese campo, y el actor rechaza el **input entero** ante un campo desconocido. Por eso `_init_bridge_search` lo intenta con `industry_codes` y, si el actor rechaza el input, **reintenta una vez sin él**, logueando `"industry_codes not supported by actor, retrying without it"`. Se pierde ese filtro, no el run. Un error sobre *otro* campo se propaga sin enmascararse.

### Dedup aislado

`dedup_bridge_candidates` consulta **solo** `bridge_candidates`, nunca `scraper_leads` ni `prospects`. La identidad es `(company_name, linkedin_url)`: la misma persona puede ser contacto de partnership para más de una empresa. Una persona puede ser legítimamente lead de venta **y** contacto de partnership, y esos pipelines no deben filtrarse entre sí.

## Endpoints

### Leads

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/health` | Health check |
| GET | `/markets` | Mercados activos, agrupados por región |
| GET | `/organizations/{organization_id}/markets` | Mercados habilitados de una org |
| POST | `/runs` | Inicia un run (la fila debe existir en `runs` con status `pending`) |
| GET | `/runs/{run_id}` | Estado del run |
| GET | `/runs/{run_id}/logs` | Logs del run |
| DELETE | `/runs/{run_id}` | Cancela el run si está activo |

### Bridge

| Método | Ruta | Descripción |
|---|---|---|
| POST | `/bridge/seed-lists` | Crea una seed list |
| GET | `/bridge/seed-lists?organization_id=` | Lista las seed lists de una org |
| POST | `/bridge/runs` | Inicia un run (la fila debe existir en `bridge_runs` con status `pending`) |
| GET | `/bridge/runs/{run_id}?organization_id=` | Estado del run |
| GET | `/bridge/runs/{run_id}/logs?organization_id=` | Logs del run |
| GET | `/bridge/candidates?run_id=&organization_id=` | Candidatos de un run |
| PATCH | `/bridge/candidates/{candidate_id}` | Confirma / rechaza / restaura un candidato |

Tanto `POST /runs` como `POST /bridge/runs` son **fire-and-forget**: validan y lanzan la tarea con `asyncio.create_task`, devolviendo de inmediato. El CRM debe hacer polling del estado.

## Autenticación server-to-server

Todo request al backend (excepto `/health`) debe incluir el header:

```
X-Internal-Api-Key: <INTERNAL_API_KEY>
```

Sin ese header, o con uno incorrecto, la respuesta es **401**. Es un secreto
compartido con el CRM, **no** el service role de Supabase.

Esto **no reemplaza** la validación de `organization_id` — esa sigue protegiendo
los datos de una organización frente a otra. Esta capa responde una pregunta
distinta: *quién puede hablarle al backend en absoluto*. Sin ella, cualquiera que
descubriera la URL de Railway podía llamar a cualquier endpoint y, pasando un
`organization_id` arbitrario, leer datos de cualquier org.

**La aplicación es condicional a que `INTERNAL_API_KEY` esté seteada.** Si la
variable no existe, el backend acepta a todos y loguea un WARNING al arrancar.
Es deliberado: permite deployar el backend antes de tocar el CRM y activar la
autenticación después, sin una ventana en la que el CRM quede bloqueado. Una vez
seteada, la validación es inmediata y la comparación es de tiempo constante
(`secrets.compare_digest`).

`/health` queda público a propósito: el healthcheck de Railway lo consulta sin
headers custom, y exigirle auth haría fallar los deploys. No expone nada más que
un status y la versión.

## Aislamiento multi-tenant

Este backend usa el **service role key** de Supabase, que **bypassa RLS por completo**. Es decir: **RLS no protege contra un bug de filtrado en este código**. La única defensa real es que cada query filtre correctamente por `organization_id`. RLS sigue siendo útil como defensa en profundidad para cualquier consumidor que acceda con la sesión de un usuario (el CRM client-side), pero no para este servicio.

Reglas que se siguen en todo el código:

- **`organization_id` va en el `WHERE`**, no como chequeo posterior. Un id de otro tenant simplemente no matchea ninguna fila. PostgREST combina los `.eq()`/`.in_()` encadenados en un único `WHERE ... AND ...`, así que el orden en Python no altera la evaluación en SQL.
- **`get_sender_profile(profile_id, organization_id)`** exige la org. Antes filtraba solo por `id`, lo que permitía que una organización usara el `sender_profile_id` de otra y generara mensajes bajo la identidad real de un SDR ajeno.
- **`_assert_owned_by_org(row, organization_id, resource)`** valida el dueño real del recurso antes de operar. **Falla cerrado**: una fila con `organization_id` ausente o `null` también se rechaza (403).
- **Al insertar, `organization_id` viene siempre del parámetro validado**, nunca de datos scrapeados.
- `bridge_run_logs` no tiene `organization_id`; el acceso a sus logs se autoriza vía el run padre.

## Mensajería con Claude / proveedores custom

`api/message_generator.py` no hardcodea modelo ni proveedor: ambos viajan en el `RunRequest`.

- `anthropic_key` — nunca se persiste.
- `anthropic_base_url` — default `https://api.anthropic.com`; se puede apuntar a un proxy custom (p. ej. AITokenKing). **Si termina en `/v1` se le recorta**, porque el SDK de Anthropic agrega `/v1` por su cuenta y quedaría `/v1/v1`.
- `anthropic_model` — se pasa **exactamente como viene**, sin normalizar. Ojo: los proxies tienen su propia nomenclatura; AITokenKing, por ejemplo, no acepta los IDs oficiales de Anthropic. Consultá su `/models` para ver la lista real.

### Generación en paralelo

Los mensajes se generan **concurrentemente** con `anthropic.AsyncAnthropic` + `asyncio.gather`, acotados por `asyncio.Semaphore(MESSAGE_CONCURRENCY = 6)`. Antes era secuencial (~5 s por lead), y 90 leads tardaban ~8 min, provocando 504 en el polling del CRM. Con 6 en paralelo baja a ~80 s. Verificado que el proxy de AITokenKing responde 200 (sin 429) con 6 concurrentes; si aparecieran 429, bajar esa constante a 3-4.

El progreso se loguea **por lote** (`"Generated messages for 24/90 leads"`), no por mensaje.

### Resiliencia ante respuestas cortadas

Una sola respuesta truncada solía tirar el run entero y perder todos los mensajes ya generados. Tres defensas:

- `_parse_response` **rescata** `custom1`/`custom2` por separado si el JSON viene truncado, así una `custom2` cortada no se lleva puesta también a la `custom1` completa.
- `try/except` **por lead**: una respuesta mala loguea un warning y saltea ese lead, sin tumbar el batch.
- `MESSAGE_MAX_TOKENS = 2048`, para que mensajes largos en español o chino no se corten a la mitad.

### Reglas de contenido

- **Basic**: mensajes genéricos, sin nombre ni firma del sender.
- **Premium+**: perfil completo del SDR (`years_experience`, `seniority`, `expertise_area`).
- Límites de caracteres desde `sender_profiles.connection_note_max_chars` / `followup_max_chars`; default 300 / 500.
- **`company_context`** (texto libre que configura el admin, viaja en el `RunRequest`) se inyecta en el prompt para que el mensaje suene informado sobre el negocio. Si viene vacío, esa sección se omite por completo.
- **Idioma por mercado**: si no hay sender profile (Basic) o su idioma es el default, se usa el del mercado — `taiwan→zh`, `latam→es`, `vietnam→vi`, `global→en`. Se resuelve **por lead**, no por batch.

## Concurrencia y el event loop

`supabase-py` es **síncrono** y `run_scraping` bloquea por minutos (HTTP a Apify + `time.sleep` de polling). Ambos corrían directo dentro de handlers/tareas async sobre el mismo event loop que sirve la API, lo que **congelaba FastAPI** mientras un run scrapeaba: el `GET /runs/{run_id}` del CRM daba 504, y dos runs simultáneos se serializaban en vez de solaparse.

Todo el trabajo bloqueante va ahora en `asyncio.to_thread`: `run_scraping`, `dedup_leads`, `import_leads_to_supabase`, los `log_run`, `update_run_status`, y las queries de todos los endpoints (incluidos los de polling). Medido con 2 runs concurrentes: antes, peor latencia de poll **3808 ms** con 1 poll atendido; después, **1 ms** con 11 atendidos, y los runs se solapan (2,0 s) en vez de serializarse (4,0 s).

> El `log_fn` que se le pasa a `run_scraping` queda **síncrono a propósito**: ya se ejecuta dentro del worker thread.

## Logging

Railway marca como `error` todo lo que sale por **stderr**. Como el logging default de Python escribe ahí, todos los logs normales aparecían en rojo y era imposible distinguir un error real.

- El root logger escribe a **stdout** (`StreamHandler(sys.stdout)`, `force=True`).
- `httpx`/`httpcore` en **WARNING** — logueaban cada request HTTP individual.
- El log-streaming del actor de Apify (`"[apify.<actor> runId:...]"`) se desactiva con `logger=None` en `.call()`.
- **`log_run()` escribe a los dos destinos**: stdout (con la severidad correcta) y la tabla `run_logs` (que es lo que el CRM muestra en vivo). Los errores reales usan `logging.error` para destacarse.
- Los payloads se loguean en **una sola línea** (`json.dumps` sin `indent`).

## Esquema de Supabase

| Tabla | Columnas usadas |
|---|---|
| `runs` | `id` (PK, **no** `run_id`), `organization_id`, `status`, `total_leads`, `error_message`, `updated_at` |
| `run_logs` | `run_id`, `level`, `message`, `created_at` |
| `run_sdr_assignments` | `run_id`, `sdr_id`, `leads_assigned` |
| `org_combos` | `organization_id`, `combo_code`, `is_active` (**no** `enabled`) |
| `scraper_combos_master` | `code` (**no** `combo_code`), `title_keywords`, `seniority_levels`, `company_headcounts` |
| `sender_profiles` | `id`, **`organization_id`**, `display_name`, `title`, `company`, `style_hint`, `icp_focus`, `language`, `years_experience`, `seniority`, `expertise_area`, `connection_note_max_chars`, `followup_max_chars` |
| `scraper_leads` | `organization_id`, `run_id`, `linkedin_url`, `full_name`, `first_name`, `last_name`, `company`, `title`, `location`, `icp_score`, `search_combo`, `custom1`, `custom2`, `market`, `exported_to_crm`, `created_at`. **No tiene `email`.** |
| `prospects` | Solo se **lee** para dedup (`linkedin_url`). El insert lo hace el CRM. |
| `monthly_lead_counts` | `organization_id`, **`year_month`** (`"YYYY-MM"`), **`count`** |
| `markets` | `id`, `name` (UNIQUE), `geo_code`, `region` (`asia`/`latin_america`/`europe`/`usa`), `default_language`, `is_active` |
| `organization_markets` | `organization_id`, `market_id` (UNIQUE juntos) |
| `bridge_seed_lists` | `id`, `organization_id`, `name`, `channel_family`, `company_names[]`, `industry_codes[]`, `company_headcounts[]`, `geo_codes[]` |
| `bridge_runs` | `id`, `organization_id`, `seed_list_id`, `status`, `total_candidates`, `error_message`, `started_at`, `completed_at` |
| `bridge_run_logs` | `run_id`, `level`, `message`, `created_at` |
| `bridge_candidates` | `run_id`, `organization_id`, `seed_list_id`, `channel_family`, `company_name` (NOT NULL), `full_name`, `first_name`, `last_name`, `title`, `linkedin_url`, `location`, `about`, `verification_status`, `created_at`, `updated_at` |

> ⚠️ Antes de escribir cualquier query nueva, verificá el nombre exacto de la columna contra esta tabla. **Casi todos los bugs de producción de este proyecto fueron nombres de columna que no coincidían con el esquema real** (ver changelog).

## Variables de entorno

Las API keys de Apify y Claude viajan en cada request y nunca se persisten.

```
SUPABASE_URL
SUPABASE_SERVICE_ROLE_KEY
INTERNAL_API_KEY          # secreto compartido con el CRM (ver Autenticación)
```

`SUPABASE_URL` se normaliza en `api/database.py` (se le quita `/rest/v1`, `/auth/v1` o `/` final) para evitar `PGRST125`. Aun así, configurala como la URL base (`https://<proyecto>.supabase.co`).

## Deploy

Railway, con el `Procfile` incluido:

```
web: uvicorn api.main:app --host 0.0.0.0 --port $PORT
```

## Changelog de fixes

| Fecha | Problema | Fix |
|---|---|---|
| 2026-07-08 | `runs` no tiene `run_id` (la PK es `id`) → `PGRST125` | Todas las queries contra `runs` filtran por `id` |
| 2026-07-08 | `SUPABASE_URL` con sufijo `/rest/v1` duplica el path → `PGRST125` | `_normalize_supabase_url()` |
| 2026-07-08 | `org_combos` se filtraba por `enabled` | La columna real es `is_active` |
| 2026-07-08 | `scraper_combos_master` se filtraba por `combo_code` | La columna real es `code` |
| 2026-07-20 | El código leía `status` en la respuesta del actor; la señal real es `message == "ok"` | Se descartaban leads válidos. Corregido |
| 2026-07-20 | `GEO_CODES.get(market)` fallaba con `"LATAM"` (claves en minúscula) → sin filtro geográfico | `market.lower()`; luego reemplazado por la tabla `markets` |
| 2026-07-20 | El actor exige `geo_codes` **enteros** | `int()` al construir el input |
| 2026-07-20 | España (`105646813`) estaba en la lista `latam` | Removida |
| 2026-07-20 | `scraper_leads` no tiene columna `email` → `PGRST204` | Fuera del insert |
| 2026-07-20 | `monthly_lead_counts` usaba `month`/`lead_count` | Los reales son `year_month`/`count` |
| 2026-07-20 | Un `seniority_levels` fuera del enum del actor tumbaba el run | `_normalize_seniority_levels` mapea alias y descarta+loguea lo desconocido |
| 2026-07-20 | Una respuesta truncada de Claude tiraba el batch entero | Rescate de JSON parcial + `try/except` por lead + `max_tokens` 2048 |
| 2026-07-20 | Mensajes secuenciales: 90 leads = ~8 min → 504 en el polling | Paralelización con semáforo de 6 |
| 2026-07-20 | `org_icp_keywords` no existía → `PGRST205` en todo run | ICP revertido a keywords hardcodeadas |
| 2026-07-20 | Todos los logs salían como `error` en Railway (stderr) | Root logger a stdout, `httpx` a WARNING, `logger=None` en el actor |
| 2026-07-23 | Trabajo bloqueante congelaba el event loop → 504 y runs serializados | Todo en `asyncio.to_thread` |
| 2026-07-23 | `get_sender_profile` filtraba solo por `id` → fuga cross-tenant de identidad de SDRs | Exige `organization_id` |
| 2026-07-23 | Los endpoints no validaban que el `run_id` fuera de la org del request | `_assert_owned_by_org`, 403 y falla cerrado |
| 2026-07-24 | Un run con N países de una región creaba N×combos celdas, escalando el tiempo con la cantidad de países | Se combinan en un solo `geo_codes`; celdas vuelven a ser solo por combo |
| 2026-07-24 | Una celda con pool abundante aportaba más leads de los que le tocaban (target=46, sumó 78) → runs terminaban por encima de `total_leads` | Se recorta cada celda a su target exacto (muestra aleatoria); `OVERFETCH_MULTIPLIER` bajado a 1.2; chequeo de 80% con una ronda de reintento acotada |
| 2026-07-24 | El recorte por celda descartaba leads de un combo que rendía bien en vez de compararlo contra el resto; el polling podía tardar 5 min en un combo estancado | Revertido a "cada combo aporta todo"; recorte final único por ICP score en `job_runner`; corte de estancamiento a 3 intentos sin cambio de mensaje |
| 2026-07-23 | Cualquiera con la URL de Railway podía llamar al backend | `X-Internal-Api-Key` obligatorio (401), activable vía `INTERNAL_API_KEY` |

## Verificación

```
python3 -m py_compile api/*.py scraper/*.py
```

Ver también [HANDOFF.md](HANDOFF.md) para las peculiaridades del actor de Apify y del esquema de Supabase.
