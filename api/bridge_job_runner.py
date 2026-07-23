import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from api.bridge_models import BridgeRunRequest
from api.database import get_supabase
from scraper.apify_scraper import BRIDGE_TITLE_KEYWORDS, run_bridge_scraping

log = logging.getLogger("scraper")

# Bridge is deliberately self-contained: its own runs/logs/candidates tables,
# its own dedup. It never reads or writes scraper_leads, prospects, runs or
# run_logs — a Bridge candidate and a sales lead are different things and
# mixing them would cross-contaminate both products' dedup.
_DEDUP_BATCH_SIZE = 50


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _chunk(items: List[str], size: int = _DEDUP_BATCH_SIZE) -> List[List[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def log_bridge_run(run_id: str, level: str, message: str) -> None:
    """Emit to Railway stdout at the right severity, then persist for the CRM.

    Same two-destination pattern as the lead pipeline's log_run, but writing to
    bridge_run_logs. stdout first so the line survives even if the insert fails.
    """
    if level == "error":
        log.error(message)
    else:
        log.info(message)

    supabase = get_supabase()
    supabase.table("bridge_run_logs").insert(
        {
            "run_id": run_id,
            "level": level,
            "message": message,
            "created_at": _now(),
        }
    ).execute()


async def _log(run_id: str, level: str, message: str) -> None:
    """log_bridge_run writes to Supabase synchronously; keep it off the loop."""
    await asyncio.to_thread(log_bridge_run, run_id, level, message)


def update_bridge_run_status(run_id: str, status: str, **fields: Any) -> None:
    payload: Dict[str, Any] = {"status": status, **fields}
    if status == "running":
        payload["started_at"] = _now()
    elif status in ("completed", "failed"):
        payload["completed_at"] = _now()

    supabase = get_supabase()
    supabase.table("bridge_runs").update(payload).eq("id", run_id).execute()


def get_bridge_seed_list(
    organization_id: str, seed_list_id: str
) -> Optional[Dict[str, Any]]:
    """Fetch one seed list, scoped to its owning organization.

    organization_id is in the WHERE clause, not a post-fetch check — a
    seed_list_id from another tenant matches no row and returns None.
    """
    supabase = get_supabase()
    res = (
        supabase.table("bridge_seed_lists")
        .select("*")
        .eq("id", seed_list_id)
        .eq("organization_id", organization_id)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def dedup_bridge_candidates(
    candidates: List[Dict[str, Any]], organization_id: str
) -> Tuple[List[Dict[str, Any]], int]:
    """Drop candidates this org already has in bridge_candidates.

    Scoped to bridge_candidates only — deliberately never consults
    scraper_leads or prospects. A person can legitimately be both a sales lead
    and a partnership contact; those pipelines must not filter each other.

    Identity is (company_name, linkedin_url): the same person can be a
    partnership contact for more than one company in a seed list.
    """
    supabase = get_supabase()

    linkedin_urls = [
        candidate["linkedin_url"]
        for candidate in candidates
        if candidate.get("linkedin_url")
    ]

    existing_pairs = set()
    for batch in _chunk(linkedin_urls):
        res = (
            supabase.table("bridge_candidates")
            .select("company_name,linkedin_url")
            .eq("organization_id", organization_id)
            .in_("linkedin_url", batch)
            .execute()
        )
        existing_pairs.update(
            (row.get("company_name"), row.get("linkedin_url")) for row in res.data
        )

    new_candidates: List[Dict[str, Any]] = []
    seen_in_batch = set()
    duplicates_count = 0

    for candidate in candidates:
        key = (candidate.get("company"), candidate.get("linkedin_url"))
        if key in existing_pairs or key in seen_in_batch:
            duplicates_count += 1
            continue
        seen_in_batch.add(key)
        new_candidates.append(candidate)

    return new_candidates, duplicates_count


def import_bridge_candidates(
    candidates: List[Dict[str, Any]],
    run_id: str,
    organization_id: str,
    seed_list_id: str,
    channel_family: Optional[str],
) -> int:
    """Insert candidates as pending, awaiting human review. Returns rows written."""
    if not candidates:
        return 0

    supabase = get_supabase()
    now = _now()

    rows = []
    for candidate in candidates:
        company_name = candidate.get("company")
        # company_name is NOT NULL and is half of the dedup identity — a row
        # without it isn't a usable partnership candidate.
        if not company_name:
            continue

        rows.append(
            {
                "run_id": run_id,
                # Always from the validated parameter, never from scraped data.
                "organization_id": organization_id,
                "seed_list_id": seed_list_id,
                "channel_family": channel_family,
                "company_name": company_name,
                "full_name": candidate.get("full_name"),
                "first_name": candidate.get("first_name"),
                "last_name": candidate.get("last_name"),
                "title": candidate.get("title") or candidate.get("job_title"),
                "linkedin_url": candidate.get("linkedin_url"),
                "location": candidate.get("location"),
                "about": candidate.get("about"),
                "verification_status": "pending",
                "created_at": now,
                "updated_at": now,
            }
        )

    if rows:
        supabase.table("bridge_candidates").insert(rows).execute()

    return len(rows)


async def run_bridge_job(request: BridgeRunRequest) -> None:
    run_id = request.run_id
    organization_id = request.organization_id

    try:
        await asyncio.to_thread(update_bridge_run_status, run_id, "running")
        await _log(run_id, "info", "Bridge run started")

        seed_list = await asyncio.to_thread(
            get_bridge_seed_list, organization_id, request.seed_list_id
        )
        if not seed_list:
            # Either it doesn't exist or it belongs to another org — same
            # outcome either way, and we deliberately don't distinguish them.
            raise ValueError(
                f"Seed list {request.seed_list_id} not found for this organization"
            )

        company_names = seed_list.get("company_names") or []
        industry_codes = seed_list.get("industry_codes") or []
        company_headcounts = seed_list.get("company_headcounts") or []
        geo_codes = seed_list.get("geo_codes") or []
        channel_family = seed_list.get("channel_family")

        await _log(
            run_id,
            "info",
            f"Loaded seed list {seed_list.get('name')!r} "
            f"(channel_family={channel_family}, {len(company_names)} company name(s), "
            f"{len(industry_codes)} industry code(s))",
        )

        await _log(run_id, "info", "Searching LinkedIn for partnership contacts...")
        raw_candidates = await asyncio.to_thread(
            run_bridge_scraping,
            request.apify_token,
            company_names,
            industry_codes,
            company_headcounts,
            geo_codes,
            BRIDGE_TITLE_KEYWORDS,
            log_fn=lambda msg: log_bridge_run(run_id, "info", msg),
        )
        await _log(
            run_id, "info", f"Found {len(raw_candidates)} raw candidate(s)"
        )

        await _log(run_id, "info", "Checking for candidates already discovered...")
        new_candidates, duplicates_count = await asyncio.to_thread(
            dedup_bridge_candidates, raw_candidates, organization_id
        )
        await _log(
            run_id,
            "info",
            f"Dedup complete: {len(new_candidates)} new candidate(s), "
            f"{duplicates_count} already known",
        )

        await _log(run_id, "info", "Saving candidates for review...")
        stored = await asyncio.to_thread(
            import_bridge_candidates,
            new_candidates,
            run_id,
            organization_id,
            request.seed_list_id,
            channel_family,
        )

        await asyncio.to_thread(
            update_bridge_run_status, run_id, "completed", total_candidates=stored
        )
        await _log(
            run_id,
            "info",
            f"Bridge run completed — {stored} candidate(s) ready for review",
        )

    except Exception as exc:
        await _log(run_id, "error", f"Bridge run failed: {exc}")
        await asyncio.to_thread(
            update_bridge_run_status, run_id, "failed", error_message=str(exc)
        )
        raise
