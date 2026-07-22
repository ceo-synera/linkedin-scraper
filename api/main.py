import asyncio
import logging
import sys
from typing import Any, Dict

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from api.bd_job_runner import run_bd_job, run_bd_messages_job
from api.database import get_supabase
from api.job_runner import run_job
from api.models import BDMessageRequest, BDRunRequest, RunRequest

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

app = FastAPI(title="LinkedIn CRM & Outreach Platform - Scraper Backend", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

active_tasks: Dict[str, asyncio.Task] = {}


@app.get("/health")
async def health() -> Dict[str, str]:
    return {"status": "ok", "version": "1.0.0"}


@app.post("/runs")
async def start_run(run_request: RunRequest) -> Dict[str, str]:
    supabase = get_supabase()

    existing = (
        supabase.table("runs")
        .select("id,status")
        .eq("id", run_request.run_id)
        .limit(1)
        .execute()
    )

    if not existing.data:
        raise HTTPException(status_code=404, detail="Run not found")

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


@app.post("/bd-runs")
async def start_bd_run(bd_run_request: BDRunRequest) -> Dict[str, str]:
    supabase = get_supabase()

    existing = (
        supabase.table("runs")
        .select("id,status")
        .eq("id", bd_run_request.run_id)
        .limit(1)
        .execute()
    )

    if not existing.data:
        raise HTTPException(status_code=404, detail="Run not found")

    if existing.data[0]["status"] != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"Run is not pending (current status: {existing.data[0]['status']})",
        )

    def _on_done(task: asyncio.Task, run_id: str = bd_run_request.run_id) -> None:
        active_tasks.pop(run_id, None)

    task = asyncio.create_task(run_bd_job(bd_run_request))
    task.add_done_callback(_on_done)
    active_tasks[bd_run_request.run_id] = task

    return {"run_id": bd_run_request.run_id, "status": "started"}


@app.post("/bd-runs/{run_id}/messages")
async def start_bd_run_messages(run_id: str, message_request: BDMessageRequest) -> Dict[str, str]:
    supabase = get_supabase()

    existing = (
        supabase.table("runs")
        .select("id,status")
        .eq("id", run_id)
        .limit(1)
        .execute()
    )

    if not existing.data:
        raise HTTPException(status_code=404, detail="Run not found")

    def _on_done(task: asyncio.Task, run_id: str = run_id) -> None:
        active_tasks.pop(run_id, None)

    task = asyncio.create_task(run_bd_messages_job(run_id, message_request))
    task.add_done_callback(_on_done)
    active_tasks[run_id] = task

    return {"run_id": run_id, "status": "started"}


@app.get("/runs/{run_id}")
async def get_run(run_id: str) -> Dict[str, Any]:
    supabase = get_supabase()

    res = supabase.table("runs").select("*").eq("id", run_id).limit(1).execute()

    if not res.data:
        raise HTTPException(status_code=404, detail="Run not found")

    return res.data[0]


@app.get("/runs/{run_id}/logs")
async def get_run_logs(run_id: str) -> Dict[str, Any]:
    supabase = get_supabase()

    res = (
        supabase.table("run_logs")
        .select("*")
        .eq("run_id", run_id)
        .order("created_at", desc=False)
        .execute()
    )

    return {"run_id": run_id, "logs": res.data}


@app.delete("/runs/{run_id}")
async def cancel_run(run_id: str) -> Dict[str, str]:
    task = active_tasks.get(run_id)

    if not task:
        raise HTTPException(status_code=404, detail="No active task for this run")

    task.cancel()
    active_tasks.pop(run_id, None)

    return {"run_id": run_id, "status": "cancelled"}
