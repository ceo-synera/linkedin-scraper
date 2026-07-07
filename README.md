# LinkedIn Scraper Backend

Backend FastAPI multi-tenant para el producto **LinkedIn CRM & Outreach Platform** de Insight Software. Corre en Railway y se conecta al CRM via Supabase.

## Arquitectura

- **api/** — aplicación FastAPI: endpoints, orquestación del pipeline, generación de mensajes, dedup y distribución de leads.
- **scraper/** — scraping de LinkedIn Sales Navigator via Apify y scoring ICP.

```
api/
  main.py              # FastAPI app y endpoints
  models.py            # Modelos Pydantic (RunRequest, SenderProfile, SdrAssignment)
  database.py          # Cliente Supabase, logging y actualización de estado de runs
  job_runner.py        # Pipeline completo del run
  config_generator.py  # Combos y sender profiles desde Supabase
  message_generator.py # Generación de mensajes con Claude
  dedup.py             # Dedup contra Supabase
  lead_distributor.py  # Distribución de leads entre SDRs
scraper/
  apify_scraper.py     # Scraping via Apify (sales-navigator-scraper-by-filters)
  icp_scorer.py         # Scoring ICP de leads
requirements.txt
Procfile
```

## Principios de diseño

- **Sin cuentas hardcodeadas**: toda la configuración (combos, sender profiles, asignaciones) viene de Supabase o del request.
- **Sin config estática**: los combos de scraping se resuelven dinámicamente por run desde `scraper_combos_master` y `org_combos`.
- **Sin Asana**: el dedup se hace contra `scraper_leads` y `prospects` en Supabase.
- **Concurrencia**: cada run usa su propio directorio temporal `/tmp/run_{run_id}/`.
- **Multi-tenant**: `organization_id` está presente en todas las operaciones.
- **Sin secretos persistidos**: las API keys de Apify y Claude viajan en cada request desde el CRM y nunca se guardan en el servidor.

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
10. `update_run_status` → `completed`

Si cualquier paso falla, se registra el error en `run_logs` y el run pasa a `failed`.

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

## Variables de entorno

Solo estas dos — las API keys de Apify y Claude viajan en cada request y nunca se persisten:

```
SUPABASE_URL
SUPABASE_SERVICE_ROLE_KEY
```

## Deploy

Diseñado para desplegarse en [Railway](https://railway.app) usando el `Procfile` incluido:

```
web: uvicorn api.main:app --host 0.0.0.0 --port $PORT
```
