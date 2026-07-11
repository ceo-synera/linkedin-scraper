import os
import shutil
from datetime import datetime, timezone
from typing import Any, Dict, List

from api.config_generator import get_combo_definitions
from api.database import get_supabase, log_run, update_run_status
from api.dedup import dedup_leads
from api.models import RunRequest
from scraper.apify_scraper import run_scraping
from scraper.icp_scorer import score_leads


def import_leads_to_supabase(
    leads: List[Dict[str, Any]], run_id: str, organization_id: str
) -> None:
    """Insert scraped leads into scraper_leads only.

    Leads are stored unassigned. Distribution to SDRs and the insert into
    `prospects` happen later in the CRM (POST /api/runs/{id}/assign).
    """
    if not leads:
        return

    supabase = get_supabase()
    now = datetime.now(timezone.utc).isoformat()

    scraper_leads_rows = []
    for lead in leads:
        linkedin_url = lead.get("linkedin_url") or lead.get("linkedinUrl")
        full_name = lead.get("full_name") or lead.get("name")
        if not full_name:
            name_parts = [lead.get("first_name"), lead.get("last_name")]
            full_name = " ".join(part for part in name_parts if part) or None
        if not full_name:
            continue

        scraper_leads_rows.append(
            {
                "organization_id": organization_id,
                "run_id": run_id,
                "linkedin_url": linkedin_url,
                "full_name": full_name,
                "first_name": lead.get("first_name"),
                "last_name": lead.get("last_name"),
                "company": lead.get("company"),
                "title": lead.get("title") or lead.get("job_title"),
                "location": lead.get("location"),
                "icp_score": lead.get("icp_score"),
                "temperature": lead.get("icp_tier"),
                "search_combo": lead.get("combo"),
                "market": lead.get("market"),
                "exported_to_crm": False,
                "created_at": now,
            }
        )

    if scraper_leads_rows:
        supabase.table("scraper_leads").insert(scraper_leads_rows).execute()


async def run_job(run_request: RunRequest) -> None:
    run_id = run_request.run_id
    organization_id = run_request.organization_id
    run_dir = f"/tmp/run_{run_id}"

    try:
        os.makedirs(run_dir, exist_ok=True)

        # 1. running
        update_run_status(run_id, "running")
        log_run(run_id, "info", "Run started")

        # 2. combo definitions
        combos = get_combo_definitions(organization_id, run_request.combos)
        log_run(run_id, "info", f"Loaded {len(combos)} combo definitions")

        # 3. scraping (Apify) — route the scraper's debug output into the
        # CRM's run_logs so it shows up in the CRM log view.
        raw_leads = run_scraping(
            run_request.apify_token,
            combos,
            run_request.markets,
            run_request.total_leads,
            log_fn=lambda msg: log_run(run_id, "info", msg),
        )
        log_run(run_id, "info", f"Scraped {len(raw_leads)} raw leads")

        # 4. scoring
        scored_leads = score_leads(raw_leads)
        log_run(run_id, "info", "Scored leads against ICP")

        # 5. dedup
        new_leads, duplicates_count = dedup_leads(scored_leads, organization_id)
        log_run(
            run_id,
            "info",
            f"Dedup complete: {len(new_leads)} new leads, {duplicates_count} duplicates",
        )

        # 6. insert into scraper_leads (unassigned)
        import_leads_to_supabase(new_leads, run_id, organization_id)
        log_run(run_id, "success", f"Stored {len(new_leads)} leads in scraper_leads")

        # 7. completed
        hot_count = sum(1 for lead in new_leads if lead.get("icp_tier") == "HOT")
        warm_count = sum(1 for lead in new_leads if lead.get("icp_tier") == "WARM")
        cold_count = sum(1 for lead in new_leads if lead.get("icp_tier") == "COLD")

        update_run_status(
            run_id,
            "completed",
            total_leads=len(new_leads),
            hot_count=hot_count,
            warm_count=warm_count,
            cold_count=cold_count,
        )
        log_run(run_id, "success", "Run completed")

    except Exception as exc:
        log_run(run_id, "error", f"Run failed: {exc}")
        update_run_status(run_id, "failed", error_message=str(exc))
        raise

    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
