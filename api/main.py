import asyncio
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from api.bridge_job_runner import run_bridge_job
from api.bridge_models import (
    BridgeCandidateUpdate,
    BridgeRunRequest,
    BridgeSeedListInput,
)
from api.config_generator import list_markets, list_organization_markets
from api.database import get_supabase
from api.job_runner import run_job
from api.models import RunRequest

# Send all logs to stdout (not the default stderr) so Railway doesn't tag every
# normal INFO line as "error". force=True replaces any handler uvicorn set up.
logging.basicConfig(
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)],
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    force=True,
)

# httpx logs every single HTTP request at INFO (each Supabase/Apify call as its
# own line) — far too noisy. Only surface it when something actually breaks.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# apify-client streams the actor run's own log lines ("[apify.<actor> ...]").
# We disable that streaming at the call site (logger=None); quiet these here too
# as a backstop so any residual apify-client line doesn't show up as an error.
logging.getLogger("apify").setLevel(logging.WARNING)
logging.getLogger("apify_client").setLevel(logging.WARNING)

app = FastAPI(title="LinkedIn CRM & Outreach Platform - Scraper Backend", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

active_tasks: Dict[str, asyncio.Task] = {}


# supabase-py's client is synchronous, so every .execute() blocks the thread it
# runs on. Called straight from an async handler that would freeze the whole
# event loop — including the GET /runs/{id} polling the CRM depends on. These
# helpers stay sync and are dispatched with asyncio.to_thread by the handlers.
def _fetch_run_status(run_id: str) -> Any:
    supabase = get_supabase()
    return (
        supabase.table("runs")
        .select("id,status,organization_id")
        .eq("id", run_id)
        .limit(1)
        .execute()
    )


def _assert_owned_by_org(
    row: Dict[str, Any], organization_id: str, resource: str = "run"
) -> None:
    """Reject a resource id that belongs to a different tenant.

    Without this, knowing (or guessing) another org's pending run_id would let
    a caller start a pipeline under a run row they don't own — the scraped data
    itself stays correctly scoped, but that run's status/counters would be
    driven by someone else's request. The same guard covers Bridge candidates
    before they're mutated.

    Fails closed: a row with a missing or null organization_id is rejected too.
    """
    if row.get("organization_id") != organization_id:
        raise HTTPException(
            status_code=403,
            detail=f"This {resource} does not belong to your organization",
        )


def _fetch_run(run_id: str) -> Any:
    supabase = get_supabase()
    return supabase.table("runs").select("*").eq("id", run_id).limit(1).execute()


def _fetch_run_logs(run_id: str) -> Any:
    supabase = get_supabase()
    return (
        supabase.table("run_logs")
        .select("*")
        .eq("run_id", run_id)
        .order("created_at", desc=False)
        .execute()
    )


@app.get("/health")
async def health() -> Dict[str, str]:
    return {"status": "ok", "version": "1.0.0"}


@app.get("/markets")
async def get_markets() -> Dict[str, Any]:
    """All active markets, grouped by region."""
    markets = await asyncio.to_thread(list_markets)

    grouped: Dict[str, Any] = {}
    for market in markets:
        grouped.setdefault(market["region"], []).append(
            {
                "id": market["id"],
                "name": market["name"],
                "geo_code": market["geo_code"],
                "default_language": market["default_language"],
            }
        )
    return grouped


@app.get("/organizations/{organization_id}/markets")
async def get_organization_markets(organization_id: str) -> Dict[str, Any]:
    """Markets an organization has enabled.

    Scoped by organization_id in the query's WHERE clause, so it can only ever
    return that org's rows. Note this service has no user session to check the
    caller against — see the multi-tenant section of the README.
    """
    markets = await asyncio.to_thread(list_organization_markets, organization_id)
    return {"organization_id": organization_id, "markets": markets}


@app.post("/runs")
async def start_run(run_request: RunRequest) -> Dict[str, str]:
    existing = await asyncio.to_thread(_fetch_run_status, run_request.run_id)

    if not existing.data:
        raise HTTPException(status_code=404, detail="Run not found")

    _assert_owned_by_org(existing.data[0], run_request.organization_id)

    if existing.data[0]["status"] != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"Run is not pending (current status: {existing.data[0]['status']})",
        )

    def _on_done(task: asyncio.Task, run_id: str = run_request.run_id) -> None:
        active_tasks.pop(run_id, None)

    task = asyncio.create_task(run_job(run_request))
    task.add_done_callback(_on_done)
    active_tasks[run_request.run_id] = task

    return {"run_id": run_request.run_id, "status": "started"}


@app.get("/runs/{run_id}")
async def get_run(run_id: str) -> Dict[str, Any]:
    res = await asyncio.to_thread(_fetch_run, run_id)

    if not res.data:
        raise HTTPException(status_code=404, detail="Run not found")

    return res.data[0]


@app.get("/runs/{run_id}/logs")
async def get_run_logs(run_id: str) -> Dict[str, Any]:
    res = await asyncio.to_thread(_fetch_run_logs, run_id)

    return {"run_id": run_id, "logs": res.data}


@app.delete("/runs/{run_id}")
async def cancel_run(run_id: str) -> Dict[str, str]:
    task = active_tasks.get(run_id)

    if not task:
        raise HTTPException(status_code=404, detail="No active task for this run")

    task.cancel()
    active_tasks.pop(run_id, None)

    return {"run_id": run_id, "status": "cancelled"}


# ---------------------------------------------------------------------------
# Bridge (partnership discovery)
#
# Every read and write is scoped by organization_id inside the query's WHERE
# clause, so a resource id belonging to another tenant simply matches no row.
# Where a resource is fetched before being mutated, ownership is asserted
# against the row's real organization_id, never trusted from the request body.
# ---------------------------------------------------------------------------


def _bridge_insert_seed_list(payload: Dict[str, Any]) -> Any:
    supabase = get_supabase()
    return supabase.table("bridge_seed_lists").insert(payload).execute()


def _bridge_list_seed_lists(organization_id: str) -> Any:
    supabase = get_supabase()
    return (
        supabase.table("bridge_seed_lists")
        .select("*")
        .eq("organization_id", organization_id)
        .order("created_at", desc=True)
        .execute()
    )


def _bridge_fetch_run(run_id: str) -> Any:
    supabase = get_supabase()
    return (
        supabase.table("bridge_runs").select("*").eq("id", run_id).limit(1).execute()
    )


def _bridge_fetch_run_logs(run_id: str) -> Any:
    supabase = get_supabase()
    return (
        supabase.table("bridge_run_logs")
        .select("*")
        .eq("run_id", run_id)
        .order("created_at", desc=False)
        .execute()
    )


def _bridge_list_candidates(run_id: str, organization_id: str) -> Any:
    supabase = get_supabase()
    return (
        supabase.table("bridge_candidates")
        .select("*")
        .eq("run_id", run_id)
        .eq("organization_id", organization_id)
        .order("created_at", desc=False)
        .execute()
    )


def _bridge_fetch_candidate(candidate_id: str) -> Any:
    supabase = get_supabase()
    return (
        supabase.table("bridge_candidates")
        .select("id,organization_id")
        .eq("id", candidate_id)
        .limit(1)
        .execute()
    )


def _bridge_update_candidate_status(
    candidate_id: str, organization_id: str, verification_status: str
) -> Any:
    supabase = get_supabase()
    # organization_id stays in the WHERE clause as well as being asserted by
    # the caller — belt and braces, so the UPDATE itself can't touch another
    # tenant's row even if the guard above were ever bypassed.
    return (
        supabase.table("bridge_candidates")
        .update(
            {
                "verification_status": verification_status,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        .eq("id", candidate_id)
        .eq("organization_id", organization_id)
        .execute()
    )


@app.post("/bridge/seed-lists")
async def create_bridge_seed_list(seed_list: BridgeSeedListInput) -> Dict[str, Any]:
    payload = seed_list.model_dump()
    res = await asyncio.to_thread(_bridge_insert_seed_list, payload)

    if not res.data:
        raise HTTPException(status_code=500, detail="Failed to create seed list")

    return res.data[0]


@app.get("/bridge/seed-lists")
async def list_bridge_seed_lists(organization_id: str) -> Dict[str, Any]:
    res = await asyncio.to_thread(_bridge_list_seed_lists, organization_id)
    return {"organization_id": organization_id, "seed_lists": res.data}


@app.post("/bridge/runs")
async def start_bridge_run(bridge_run_request: BridgeRunRequest) -> Dict[str, str]:
    existing = await asyncio.to_thread(_bridge_fetch_run, bridge_run_request.run_id)

    if not existing.data:
        raise HTTPException(status_code=404, detail="Bridge run not found")

    _assert_owned_by_org(
        existing.data[0], bridge_run_request.organization_id
    )

    if existing.data[0]["status"] != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"Run is not pending (current status: {existing.data[0]['status']})",
        )

    def _on_done(task: asyncio.Task, run_id: str = bridge_run_request.run_id) -> None:
        active_tasks.pop(run_id, None)

    task = asyncio.create_task(run_bridge_job(bridge_run_request))
    task.add_done_callback(_on_done)
    active_tasks[bridge_run_request.run_id] = task

    return {"run_id": bridge_run_request.run_id, "status": "started"}


@app.get("/bridge/runs/{run_id}")
async def get_bridge_run(run_id: str, organization_id: str) -> Dict[str, Any]:
    res = await asyncio.to_thread(_bridge_fetch_run, run_id)

    if not res.data:
        raise HTTPException(status_code=404, detail="Bridge run not found")

    _assert_owned_by_org(res.data[0], organization_id)

    return res.data[0]


@app.get("/bridge/runs/{run_id}/logs")
async def get_bridge_run_logs(run_id: str, organization_id: str) -> Dict[str, Any]:
    # bridge_run_logs has no organization_id of its own, so ownership is
    # established via the parent run before any log line is returned.
    run_res = await asyncio.to_thread(_bridge_fetch_run, run_id)

    if not run_res.data:
        raise HTTPException(status_code=404, detail="Bridge run not found")

    _assert_owned_by_org(run_res.data[0], organization_id)

    res = await asyncio.to_thread(_bridge_fetch_run_logs, run_id)
    return {"run_id": run_id, "logs": res.data}


@app.get("/bridge/candidates")
async def list_bridge_candidates(run_id: str, organization_id: str) -> Dict[str, Any]:
    res = await asyncio.to_thread(_bridge_list_candidates, run_id, organization_id)
    return {"run_id": run_id, "organization_id": organization_id, "candidates": res.data}


@app.patch("/bridge/candidates/{candidate_id}")
async def update_bridge_candidate(
    candidate_id: str, update: BridgeCandidateUpdate
) -> Dict[str, Any]:
    existing = await asyncio.to_thread(_bridge_fetch_candidate, candidate_id)

    if not existing.data:
        raise HTTPException(status_code=404, detail="Candidate not found")

    _assert_owned_by_org(existing.data[0], update.organization_id, "candidate")

    res = await asyncio.to_thread(
        _bridge_update_candidate_status,
        candidate_id,
        update.organization_id,
        update.verification_status,
    )

    if not res.data:
        raise HTTPException(status_code=404, detail="Candidate not found")

    return res.data[0]
