import os
import shutil
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from api.config_generator import get_combo_definitions, get_sender_profile
from api.database import get_supabase, log_run, update_run_status
from api.dedup import dedup_leads
from api.lead_distributor import distribute_leads
from api.message_generator import generate_messages_for_batch
from api.models import RunRequest, SenderProfile
from scraper.apify_scraper import run_scraping
from scraper.icp_scorer import score_leads


def _resolve_sender_profile(assignment) -> Optional[SenderProfile]:
    if assignment.sender_profile is not None:
        return assignment.sender_profile
    if assignment.sender_profile_id:
        return get_sender_profile(assignment.sender_profile_id)
    return None


def import_leads_to_supabase(
    leads: List[Dict[str, Any]], run_id: str, organization_id: str
) -> None:
    if not leads:
        return

    supabase = get_supabase()
    now = datetime.now(timezone.utc).isoformat()

    scraper_leads_rows = []
    prospects_rows = []

    for lead in leads:
        linkedin_url = lead.get("linkedin_url") or lead.get("linkedinUrl")
        full_name = lead.get("full_name") or lead.get("name")
        if not full_name:
            name_parts = [lead.get("first_name"), lead.get("last_name")]
            full_name = " ".join(part for part in name_parts if part) or None

        # prospects.name is NOT NULL, so a nameless lead can't be imported.
        if not full_name:
            continue

        title = lead.get("title") or lead.get("job_title")
        company = lead.get("company")
        industry = lead.get("industry")
        company_size = lead.get("company_size")
        icp_score = lead.get("icp_score")
        temperature = lead.get("icp_tier")
        search_combo = lead.get("combo")
        market = lead.get("market")
        custom1 = lead.get("custom1")
        custom2 = lead.get("custom2")

        scraper_leads_rows.append(
            {
                "organization_id": organization_id,
                "run_id": run_id,
                "linkedin_url": linkedin_url,
                "full_name": full_name,
                "first_name": lead.get("first_name"),
                "last_name": lead.get("last_name"),
                "company": company,
                "title": title,
                "industry": industry,
                "company_size": company_size,
                "location": lead.get("location"),
                "icp_score": icp_score,
                "temperature": temperature,
                "search_combo": search_combo,
                "custom1": custom1,
                "custom2": custom2,
                "market": market,
                "exported_to_crm": True,
                "created_at": now,
            }
        )

        prospects_rows.append(
            {
                "organization_id": organization_id,
                "name": full_name,
                "linkedin_url": linkedin_url,
                "company": company,
                "title": title,
                "industry": industry,
                "company_size": company_size,
                "icp_score": icp_score,
                "lead_temperature": temperature,
                "search_combo": search_combo,
                "scrape_date": now,
                "outreach_status": "new",
                "market": market,
                "assigned_to": lead.get("assigned_to"),
                "custom1": custom1,
                "custom2": custom2,
                "created_at": now,
            }
        )

    supabase.table("scraper_leads").insert(scraper_leads_rows).execute()
    supabase.table("prospects").insert(prospects_rows).execute()


def _update_run_sdr_assignments(
    run_id: str, distribution: Dict[str, List[Dict[str, Any]]]
) -> None:
    supabase = get_supabase()
    for sdr_id, sdr_leads in distribution.items():
        supabase.table("run_sdr_assignments").upsert(
            {
                "run_id": run_id,
                "sdr_id": sdr_id,
                "leads_assigned": len(sdr_leads),
            },
            on_conflict="run_id,sdr_id",
        ).execute()


def _update_monthly_lead_counts(organization_id: str, lead_count: int) -> None:
    if lead_count <= 0:
        return

    supabase = get_supabase()
    month = datetime.now(timezone.utc).strftime("%Y-%m")

    existing = (
        supabase.table("monthly_lead_counts")
        .select("lead_count")
        .eq("organization_id", organization_id)
        .eq("month", month)
        .limit(1)
        .execute()
    )

    new_count = lead_count
    if existing.data:
        new_count += existing.data[0]["lead_count"]

    supabase.table("monthly_lead_counts").upsert(
        {
            "organization_id": organization_id,
            "month": month,
            "lead_count": new_count,
        },
        on_conflict="organization_id,month",
    ).execute()


async def run_job(run_request: RunRequest) -> None:
    run_id = run_request.run_id
    organization_id = run_request.organization_id
    run_dir = f"/tmp/run_{run_id}"

    try:
        os.makedirs(run_dir, exist_ok=True)

        update_run_status(run_id, "running")
        log_run(run_id, "info", "Run started")

        combos = get_combo_definitions(organization_id, run_request.combos)
        log_run(run_id, "info", f"Loaded {len(combos)} combo definitions")

        # Route the scraper's debug output into the CRM's run_logs.
        raw_leads = run_scraping(
            run_request.apify_token,
            combos,
            run_request.markets,
            run_request.total_leads,
            log_fn=lambda msg: log_run(run_id, "info", msg),
        )
        log_run(run_id, "info", f"Scraped {len(raw_leads)} raw leads")

        scored_leads = score_leads(raw_leads)
        log_run(run_id, "info", "Scored leads against ICP")

        new_leads, duplicates_count = dedup_leads(scored_leads, organization_id)
        log_run(
            run_id,
            "info",
            f"Dedup complete: {len(new_leads)} new leads, {duplicates_count} duplicates",
        )

        distribution = distribute_leads(new_leads, run_request.sdr_assignments)
        log_run(run_id, "info", f"Distributed leads across {len(distribution)} SDRs")

        assignments_by_sdr = {
            assignment.sdr_id: assignment for assignment in run_request.sdr_assignments
        }

        for sdr_id, sdr_leads in distribution.items():
            if not sdr_leads:
                continue

            assignment = assignments_by_sdr.get(sdr_id)
            sender_profile = _resolve_sender_profile(assignment) if assignment else None
            language = sender_profile.language if sender_profile else "en"

            generate_messages_for_batch(
                sdr_leads,
                run_request.anthropic_key,
                run_request.plan,
                sender_profile,
                language,
                run_request.anthropic_base_url,
                run_request.anthropic_model,
            )

            for lead in sdr_leads:
                lead["assigned_to"] = sdr_id

            log_run(
                run_id,
                "info",
                f"Generated messages for SDR {sdr_id} ({len(sdr_leads)} leads)",
            )

        import_leads_to_supabase(new_leads, run_id, organization_id)
        log_run(run_id, "info", f"Imported {len(new_leads)} leads into Supabase")

        _update_run_sdr_assignments(run_id, distribution)
        _update_monthly_lead_counts(organization_id, len(new_leads))

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
        log_run(run_id, "info", "Run completed")

    except Exception as exc:
        log_run(run_id, "error", f"Run failed: {exc}")
        update_run_status(run_id, "failed", error_message=str(exc))
        raise

    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
