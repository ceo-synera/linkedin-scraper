# LinkedIn Scraper Backend

Backend FastAPI multi-tenant para el producto **LinkedIn CRM & Outreach Platform** de Insight Software. Corre en Railway y se conecta al CRM vía Supabase.

## Arquitectura

- **api/** — aplicación FastAPI: endpoints, orquestación del pipeline, generación de mensajes, dedup y distribución de leads.
- **scraper/** — scraping de LinkedIn Sales Navigator vía Apify y scoring ICP.

```
api/
  main.py              # FastAPI app y endpoints
  models.py            # Modelos Pydantic (RunRequest, BDRunRequest, BDMessageRequest, SenderProfile, SdrAssignment)
  database.py          # Cliente Supabase, logging y actualización de estado de runs
  job_runner.py        # Pipeline del run de leads individuales
  bd_job_runner.py      # Pipeline del run de BD Group (scraping + mensajería bajo demanda)
  config_generator.py  # Combos, seed lists de BD Group, ICP keywords, channel hooks, product_description y sender profiles desde Supabase
  message_generator.py # Generación de mensajes con Claude (o proveedor custom): modo individual y modo BD Group
  dedup.py             # Dedup contra Supabase
  lead_distributor.py  # Distribución de leads entre SDRs (solo pipeline individual)
scraper/
  apify_scraper.py     # Scraping vía Apify (sales-navigator-scraper-by-filters): combos título/geo y BD Group por empresa
  icp_scorer.py         # Scoring ICP de leads
requirements.txt
Procfile
```

## Principios de diseño

- **Sin cuentas hardcodeadas**: toda la configuración (combos, sender profiles, asignaciones) viene de Supabase o del request.
- **Sin config estática**: los combos de scraping se resuelven dinámicamente por run desde `scraper_combos_master` y `org_combos`.
- **Sin Asana**: el dedup se hace contra `scraper_leads` y `prospects` en Supabase.
- **Concurrencia**: cada run usa su propio directorio temporal `/tmp/run_{run_id}/`, que se limpia al terminar (éxito o error).
- **Multi-tenant**: `organization_id` está presente en todas las operaciones.
- **Sin secretos persistidos**: las API keys de Apify y Claude/AITokenKing viajan en cada request desde el CRM y nunca se guardan en el servidor.
- **BD Group es un pipeline separado**: la búsqueda por empresa objetivo (`api/bd_job_runner.py`) no es un modo del pipeline de leads individuales — tiene su propio endpoint, su propio modelo de request y no reutiliza scoring, distribución ni mensajería. Un run BD Group pertenece a un único SDR (`owner_sdr_id`), no se reparte entre varios.

## Pipeline de un run (`api/job_runner.py`)

1. `update_run_status` → `running`
2. `get_combo_definitions` desde Supabase
3. `run_scraping` — un run de Apify por mercado
4. `score_leads` — scoring ICP
5. `dedup_leads` — dedup contra Supabase
6. `distribute_leads` — distribución entre SDRs respetando mercados asignados
7. Por cada SDR: `generate_messages_for_batch` (con perfil completo si el plan es Premium+)
8. `import_leads_to_supabase` — inserta en `scraper_leads` y en `prospects` (`assigned_to`, `outreach_status='new'`)
9. Actualiza `run_sdr_assignments` y `monthly_lead_counts`
10. `update_run_status` → `completed`, con `total_leads` (ya no se derivan `hot_count`/`warm_count`/`cold_count` — no hay clasificación automática)

Si cualquier paso falla, se registra el error en `run_logs` y el run pasa a `failed` con `error_message`.

## Scoring ICP (`scraper/icp_scorer.py`)

Las keywords de scoring ya no están hardcodeadas: se leen por organización desde la tabla
`org_icp_keywords` (`organization_id`, `category`, `keyword`, `weight`) vía
`api/config_generator.get_icp_keywords`, siguiendo el mismo patrón que `get_combo_definitions`.
Esto permite que cada organización cliente configure su propio ICP sin tocar código.

| Dimensión | Cómo se calcula |
|---|---|
| Job title | Se compara el `job_title` del lead contra las keywords de categoría `decision_title`; si hay match, se usa el peso (`weight`) más alto. Si no hay match, se repite contra `influencer_title` (tier más bajo). Sin match en ninguna categoría (o categoría sin configurar) → 0. |
| Company size | Fijo, 15 puntos — el actor de Apify ya filtra por `company_headcounts` en el input. |
| Industry | Se compara el bio/`about` del lead contra las keywords de categoría `industry`; se usa el mejor match (peso más alto), no una suma. Sin match o sin configurar → 0. |
| Actividad en LinkedIn | Fijo, 15 puntos — el actor ya filtra por `posted_on_linkedin=true` en el input. |
| Señal de compra (categoría `ai_signal` en la tabla, pero es una dimensión genérica de "señal", no específica de IA) | Se suma el peso de cada keyword distinta de esa categoría que aparezca en el bio/`about` — varios matches suman más que uno solo. Sin match o sin configurar → 0. |

El total se clampea entre 0 y 100, igual que antes.

No hay clasificación automática HOT/WARM/COLD: `icp_score` se calcula y se guarda igual que antes,
pero la temperatura del lead (`temperature` en `scraper_leads`/`prospects`) ya no se autoasigna —
queda en blanco al insertar, para que un SDR la determine más adelante en base a outreach real.

## Pipeline de un run BD Group (`api/bd_job_runner.py`)

BD Group busca por empresas objetivo (`current_company_names`) en vez de por título/geo. Es un
pipeline completamente separado del de leads individuales: setup propio, endpoint propio, y no
reutiliza scoring, distribución entre SDRs ni generación de mensajes (ambas son fases
posteriores). Un run pertenece a un único SDR (`owner_sdr_id`).

1. `update_run_status` → `running`
2. `get_company_seed_lists` desde Supabase (`org_company_seed_lists`, filtradas por
   `seed_list_ids` del request)
3. `run_company_seed_scraping` — un run de Apify por seed list, en batches (el actor acepta
   máx. 10 `current_company_names` y máx. 20 `title_keywords` por batch; si una seed list tiene
   más, se parte en varios batches y se agregan los resultados)
4. `dedup_leads` — mismo dedup contra `scraper_leads`/`prospects` que usa el pipeline individual
5. `import_bd_candidates_to_supabase` — inserta en `scraper_leads` con `lead_type =
   'bd_channel_contact'`, `seed_company_name` (la empresa que el actor realmente devolvió para
   ese lead, no el input de búsqueda), `verification_status = 'pending'`, `search_combo` (nombre
   de la seed list) y `market` (de la seed list). Sin `icp_score`, sin score de canal, sin
   mensaje de outreach — son fases posteriores.
6. Bookkeeping best-effort: upsert en `run_sdr_assignments` con `owner_sdr_id` y el total de
   candidatos guardados (mismo mecanismo que usa el pipeline individual para trackear SDR↔run,
   no una columna nueva en `scraper_leads`)
7. `update_run_status` → `completed`, con `total_leads`

Si cualquier paso falla, se registra el error en `run_logs` y el run pasa a `failed` con
`error_message`, igual que el pipeline individual.

**Mensajería BD Group no se genera durante el scraping.** Los candidatos quedan sin `custom1`/
`custom2` hasta que un humano confirma que el candidato es real (`verification_status` deja de
ser `'pending'`) — generar un mensaje pagado con Claude para cada candidato crudo, antes de que
nadie lo confirme, desperdiciaría llamadas en contactos que terminan descartados como ruido. La
mensajería se dispara por separado vía `POST /bd-runs/{run_id}/messages` (ver Endpoints), que:

1. Busca en `scraper_leads` las filas de `lead_ids` para ese `run_id`/`organization_id`
2. Descarta cualquiera que siga en `verification_status = 'pending'` (guardia de seguridad —
   nunca se genera mensaje para un candidato no confirmado, sin importar qué envíe el caller)
3. `get_organization_product_description` y `get_channel_hooks` desde Supabase
4. `generate_bd_messages_for_batch` — modo BD Group (ver sección de Mensajería más abajo)
5. Escribe `custom1`/`custom2` de vuelta en cada fila de `scraper_leads`

## Endpoints

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/health` | Health check |
| POST | `/runs` | Inicia un run de leads individuales (debe existir en Supabase con status `pending`) |
| POST | `/bd-runs` | Inicia un run de BD Group (búsqueda por empresa objetivo; debe existir en Supabase con status `pending`) |
| POST | `/bd-runs/{run_id}/messages` | Genera mensajes solo para candidatos BD Group ya confirmados por un humano (no se llama automáticamente durante el scraping) |
| GET | `/runs/{run_id}` | Estado del run (sirve para ambos pipelines — comparten la tabla `runs`) |
| GET | `/runs/{run_id}/logs` | Logs del run |
| DELETE | `/runs/{run_id}` | Cancela el run si está activo |

## Esquema de Supabase

El backend nunca crea tablas ni columnas — sólo lee/escribe sobre el esquema que ya existe en el proyecto de Supabase del CRM. Referencia de tablas y columnas usadas por este backend:

| Tabla | Columnas usadas por este backend |
|---|---|
| `runs` | `id` (PK, **no** `run_id`), `organization_id`, `status`, `plan`, `markets`, `combos`, `total_leads`, `total_leads_requested`, `hot_count`, `warm_count`, `cold_count`, `error_message`, `started_at`, `completed_at` |
| `run_logs` | `run_id`, `level`, `message`, `created_at` |
| `run_sdr_assignments` | `run_id`, `sdr_id`, `sender_profile_id`, `assigned_markets`, `leads_assigned` |
| `org_combos` | `organization_id`, `combo_code`, `is_active` (**no** `enabled`) |
| `scraper_combos_master` | `code` (**no** `combo_code`) |
| `sender_profiles` | `id`, `display_name`, `title`, `company`, `style_hint`, `icp_focus`, `language`, `years_experience`, `seniority`, `expertise_area`, `connection_note_max_chars`, `followup_max_chars` |
| `scraper_leads` / `prospects` | `organization_id`, `run_id`, `linkedin_url`, `full_name`, `title`, `company`, `market`, `icp_score`, `custom1`, `custom2`, `assigned_to` (solo `prospects`), `outreach_status` (solo `prospects`); `temperature` ya no se autoasigna, queda en blanco al insertar; para BD Group además `lead_type`, `seed_company_name`, `verification_status`, y `channel_family` (asumido — usado para elegir el `hook_copy` de `org_channel_hooks`, ver nota abajo) |
| `org_company_seed_lists` | `organization_id`, `list_name`, `company_names[]`, `market`, `title_keywords[]`, `seniority_levels[]` (BD Group) |
| `org_icp_keywords` | `organization_id`, `category` (`industry`, `ai_signal`, `decision_title`, `influencer_title`), `keyword`, `weight` |
| `org_channel_hooks` | `organization_id`, `channel_family`, `hook_copy` (ángulo propio de la org para mensajería BD Group) |
| `monthly_lead_counts` | `organization_id`, `year_month`, `lead_count` |
| `organizations` | `id`, `product_description` (descripción del producto de la org, usada para dar contexto real en los mensajes; las credenciales `anthropic_key`, `anthropic_base_url`, `anthropic_model`, `apify_token` siguen viajando en el request, nunca se leen desde esta tabla) |

> ⚠️ `channel_family` en `scraper_leads` no fue verificado contra el esquema real de Supabase —
> se asume que la CRM lo setea al confirmar un candidato BD Group (junto con `verification_status`).
> Si el nombre real de la columna es otro, `lead.get("channel_family")` en
> `api/message_generator.py` simplemente no encuentra el hook y degrada a un pitch genérico sin
> error — pero conviene confirmar el nombre real antes de depender de esto en producción.

> ⚠️ Antes de tocar cualquier query nueva contra Supabase, verificar el nombre exacto de la columna contra esta tabla. Varios de los bugs en producción (ver changelog) fueron justamente nombres de columna que no coincidían con el esquema real.

## Mensajería con Claude / proveedores custom

`api/message_generator.py` no hardcodea el modelo ni el proveedor: ambos viajan en el `RunRequest`:

- `anthropic_key` — API key, nunca se persiste.
- `anthropic_base_url` — default `https://api.anthropic.com`; se puede apuntar a un proxy custom (p. ej. AITokenKing).
- `anthropic_model` — default `claude-sonnet-4-6`.

El cliente se inicializa como `anthropic.Anthropic(api_key=anthropic_key, base_url=anthropic_base_url)` y cada llamada a `messages.create` usa `model=anthropic_model`.

Reglas de contenido (ambos modos, individual y BD Group):
- **Basic**: mensajes genéricos, sin nombre ni firma del sender.
- **Premium+**: perfil completo del SDR (`years_experience`, `seniority`, `expertise_area`) para dar contexto real.
- `custom1` (connection request) / `custom2` (follow-up): el límite de caracteres ya no está hardcodeado — viene de `sender_profiles.connection_note_max_chars` / `followup_max_chars`. Si el sender profile no los tiene seteados, se usa el default histórico (300 / 500).
- `get_organization_product_description` se resuelve una vez por run y se inyecta en el prompt de cada mensaje (individual y BD) para que la mensajería describa algo concreto de lo que vende la org. Si la org no lo llenó todavía, esa parte del prompt simplemente se omite — sin error, sin placeholder inventado.

**Modo BD Group** (`generate_bd_messages_for_batch`), distinto del modo individual:
- Encuadre en tercera persona ("your customers have this problem") en vez de segunda persona — es
  una propuesta de partnership, no una venta directa.
- Usa el `hook_copy` propio de la org para el `channel_family` de ese contacto (desde
  `org_channel_hooks`) como ángulo central, en vez de un pitch genérico. Sin hook configurado para
  ese `channel_family` → pitch genérico, sin error.
- Respeta el mismo límite real del sender (por `sender_profiles`) como techo duro, pero el prompt
  pide explícitamente usar una porción notablemente mayor de ese espacio que un mensaje individual
  típico — sin un largo objetivo hardcodeado, solo la instrucción de aprovechar el espacio
  disponible.
- Solo se genera bajo demanda vía `POST /bd-runs/{run_id}/messages`, nunca automáticamente durante
  el scraping (ver sección de pipeline BD Group).

## Variables de entorno

Solo estas dos — las API keys de Apify y Claude viajan en cada request y nunca se persisten:

```
SUPABASE_URL
SUPABASE_SERVICE_ROLE_KEY
```

`SUPABASE_URL` se normaliza automáticamente en `api/database.py` (se le quita `/rest/v1`, `/auth/v1` o `/` al final) para evitar el error `PGRST125 — Invalid path specified in request URL` que ocurre si el valor configurado en Railway ya trae ese sufijo. Aun así, configúrala como la URL base del proyecto (`https://<proyecto>.supabase.co`), sin sufijos.

## Deploy

Diseñado para desplegarse en [Railway](https://railway.app) usando el `Procfile` incluido:

```
web: uvicorn api.main:app --host 0.0.0.0 --port $PORT
```

## Changelog de fixes de esquema

Historial de bugs de producción encontrados y corregidos, para no reintroducirlos:

| Fecha | Problema | Fix |
|---|---|---|
| 2026-07-08 | `runs` no tiene columna `run_id` (la PK es `id`) → `PGRST125` en `POST /runs`, `GET /runs/{run_id}` y `update_run_status` | Todas las queries contra `runs` filtran por `id` |
| 2026-07-08 | `update_run_status` completaba el run con `total_leads_imported`, columna inexistente | Se usa `total_leads`, `hot_count`, `warm_count`, `cold_count` (derivados del `icp_tier` de cada lead) |
| 2026-07-08 | `SUPABASE_URL` con sufijo `/rest/v1` duplica el path y provoca `PGRST125` en cualquier tabla | `_normalize_supabase_url()` limpia el sufijo antes de crear el cliente |
| 2026-07-08 | `update_run_status` enviaba `updated_at`, columna inexistente en `runs` | Se eliminó del payload; solo se envían `status` + kwargs válidos |
| 2026-07-08 | `org_combos` se filtraba por `enabled`, la columna real es `is_active` | Query corregida a `.eq("is_active", True)` |
| 2026-07-08 | `scraper_combos_master` se filtraba por `combo_code`, la columna real es `code` | Query corregida a `.in_("code", ...)` (nota: `org_combos.combo_code` sí existe y es distinto — no se tocó) |

## Verificación

Antes de cada push, validar sintaxis de todos los módulos:

```
python -m py_compile api/*.py scraper/*.py
```
