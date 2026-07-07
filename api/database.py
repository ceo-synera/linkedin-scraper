import os
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Optional

from supabase import Client, create_client

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")


@lru_cache(maxsize=1)
def get_supabase() -> Client:
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in the environment"
        )
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def log_run(run_id: str, level: str, message: str) -> None:
    supabase = get_supabase()
    supabase.table("run_logs").insert(
        {
            "run_id": run_id,
            "level": level,
            "message": message,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    ).execute()


def update_run_status(run_id: str, status: str, **kwargs: Any) -> None:
    supabase = get_supabase()
    payload = {"status": status, "updated_at": datetime.now(timezone.utc).isoformat()}
    payload.update(kwargs)
    supabase.table("runs").update(payload).eq("run_id", run_id).execute()
