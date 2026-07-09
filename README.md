# LinkedIn Scraper Backend

Backend FastAPI multi-tenant para el producto **LinkedIn CRM & Outreach Platform** de Insight Software. Corre en Railway y se conecta al CRM vía Supabase.

## Arquitectura

- **api/** — aplicación FastAPI: endpoints, orquestación del pipeline, generación de mensajes, dedup y distribución de leads.
- **scraper/** — scraping de LinkedIn Sales Navigator vía Apify y scoring ICP.

```
api/
  main.py              # FastAPI app y endpoints
  models.py            # Modelos Pydantic (RunRequest, SenderProfile, SdrAssignment)
  database.py          # Cliente Supabase, logging y actualización de estado de runs
  job_runner.py        # Pipeline completo del run
  config_generator.py  # Combos y sender profiles desde Supabase
  message_generator.py # Generación de mensajes con Claude (o proveedor custom)
  dedup.py             # Dedup contra Supabase
  lead_distributor.py  # Distribución de leads entre SDRs
scraper/
  apify_scraper.py     # Scraping vía Apify (sales-navigator-scraper-by-filters)
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
10. `update_run_status` → `completed`, con `total_leads`, `hot_count`, `warm_count`, `cold_count`

Si cualquier paso falla, se registra el error en `run_logs` y el run pasa a `failed` con `error_message`.

## Scoring ICP (`scraper/icp_scorer.py`)

| Dimensión | Puntos máx. |
|---|---|
| Job title match (CTO, CIO, CEO, Founder, VP Engineering, Marketing Director, CDO, COO, Product Manager, Engineering Manager) | 30 |
| Company size (11-50, 51-200) | 15 |
| Industry (Computer Software, Internet, IT Services) | 20 |
| Actividad en LinkedIn | 15 |
| Señales de IA en bio/headline (ChatGPT, OpenAI, Claude, AI, LLM, Copilot) | 20 |

Clasificación: **HOT** ≥ 70, **WARM** 50-69, **COLD** < 50.

## Endpoints

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/health` | Health check |
| POST | `/runs` | Inicia un run (debe existir en Supabase con status `pending`) |
| GET | `/runs/{run_id}` | Estado del run |
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
| `sender_profiles` | `id`, `display_name`, `title`, `company`, `style_hint`, `icp_focus`, `language`, `years_experience`, `seniority`, `expertise_area` |
| `scraper_leads` / `prospects` | `organization_id`, `run_id`, `linkedin_url`, `full_name`, `title`, `company`, `market`, `icp_score`, `icp_tier`, `custom1`, `custom2`, `assigned_to` (solo `prospects`), `outreach_status` (solo `prospects`) |
| `monthly_lead_counts` | `organization_id`, `year_month`, `lead_count` |
| `organizations` | no se consulta directamente — las credenciales (`anthropic_key`, `anthropic_base_url`, `anthropic_model`, `apify_token`) viajan en el request, nunca se leen desde esta tabla en el servidor |

> ⚠️ Antes de tocar cualquier query nueva contra Supabase, verificar el nombre exacto de la columna contra esta tabla. Varios de los bugs en producción (ver changelog) fueron justamente nombres de columna que no coincidían con el esquema real.

## Mensajería con Claude / proveedores custom

`api/message_generator.py` no hardcodea el modelo ni el proveedor: ambos viajan en el `RunRequest`:

- `anthropic_key` — API key, nunca se persiste.
- `anthropic_base_url` — default `https://api.anthropic.com`; se puede apuntar a un proxy custom (p. ej. AITokenKing).
- `anthropic_model` — default `claude-sonnet-4-6`.

El cliente se inicializa como `anthropic.Anthropic(api_key=anthropic_key, base_url=anthropic_base_url)` y cada llamada a `messages.create` usa `model=anthropic_model`.

Reglas de contenido:
- **Basic**: mensajes genéricos, sin nombre ni firma del sender.
- **Premium+**: perfil completo del SDR (`years_experience`, `seniority`, `expertise_area`) para dar contexto real.
- `custom1` (connection request): máx. 300 caracteres.
- `custom2` (follow-up): máx. 500 caracteres.

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
