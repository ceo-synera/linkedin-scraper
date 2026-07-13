from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from api.config_generator import (
    get_channel_hooks,
    get_company_seed_lists,
    get_organization_product_description,
    get_sender_profile,
)
from api.database import get_supabase, log_run, update_run_status
from api.dedup import dedup_leads
from api.message_generator import generate_bd_messages_for_batch
from api.models import BDMessageRequest, BDRunRequest, SenderProfile
from scraper.apify_scraper import run_company_seed_scraping


def _resolve_sender_profile(request: BDMessageRequest) -> Optional[SenderProfile]:
    if request.sender_profile is not None:
        return request.sender_profile
    if request.sender_profile_id:
        return get_sender_profile(request.sender_profile_id)
    return None


def _update_run_sdr_assignment(run_id: str, owner_sdr_id: str, leads_assigned: int) -> None:
    supabase = get_supabase()
    supabase.table("run_sdr_assignments").upsert(
        {
            "run_id": run_id,
            "sdr_id": owner_sdr_id,
            "leads_assigned": leads_assigned,
        },
        on_conflict="run_id,sdr_id",
    ).execute()


def import_bd_candidates_to_supabase(
    leads: List[Dict[str, Any]], run_id: str, organization_id: str
) -> None:
    """Insert BD Group candidates into scraper_leads as tagged, unscored rows.

    No icp_score, no channel score, no outreach message — this phase only
    produces tagged, unverified candidates for a human to review.
    """
    if not leads:
        return

    supabase = get_supabase()
    now = datetime.now(timezone.utc).isoformat()

    rows = []
    for lead in leads:
        linkedin_url = lead.get("linkedin_url") or lead.get("linkedinUrl")
        full_name = lead.get("full_name") or lead.get("name")
        if not full_name:
            name_parts = [lead.get("first_name"), lead.get("last_name")]
            full_name = " ".join(part for part in name_parts if part) or None
        if not full_name:
            continue

        rows.append(
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
                "market": lead.get("market"),
                "search_combo": lead.get("seed_list_name"),
                "lead_type": "bd_channel_contact",
                "seed_company_name": lead.get("company"),
                "verification_status": "pending",
                "exported_to_crm": False,
                "created_at": now,
            }
        )

    if rows:
        supabase.table("scraper_leads").insert(rows).execute()


async def run_bd_job(bd_run_request: BDRunRequest) -> None:
    run_id = bd_run_request.run_id
    organization_id = bd_run_request.organization_id

    try:
        update_run_status(run_id, "running")
        log_run(run_id, "info", "BD Group run started")

        seed_lists = get_company_seed_lists(organization_id, bd_run_request.seed_list_ids)
        log_run(run_id, "info", f"Loaded {len(seed_lists)} company seed lists")

        raw_leads = run_company_seed_scraping(
            bd_run_request.apify_token,
            seed_lists,
            bd_run_request.total_leads,
            log_fn=lambda msg: log_run(run_id, "info", msg),
        )
        log_run(run_id, "info", f"Scraped {len(raw_leads)} raw BD candidates")

        new_leads, duplicates_count = dedup_leads(raw_leads, organization_id)
        log_run(
            run_id,
            "info",
            f"Dedup complete: {len(new_leads)} new candidates, {duplicates_count} duplicates",
        )

        import_bd_candidates_to_supabase(new_leads, run_id, organization_id)
        log_run(run_id, "info", f"Stored {len(new_leads)} BD candidates in scraper_leads")

        # Best-effort bookkeeping: a schema mismatch here must not fail a run
        # whose candidates were already stored.
        try:
            _update_run_sdr_assignment(run_id, bd_run_request.owner_sdr_id, len(new_leads))
        except Exception as exc:
            log_run(run_id, "error", f"Bookkeeping update failed (non-fatal): {exc}")

        update_run_status(run_id, "completed", total_leads=len(new_leads))
        log_run(run_id, "info", "BD Group run completed")

    except Exception as exc:
        log_run(run_id, "error", f"BD Group run failed: {exc}")
        update_run_status(run_id, "failed", error_message=str(exc))
        raise


def _fetch_confirmed_bd_leads(
    run_id: str, organization_id: str, lead_ids: List[str]
) -> Any:
    supabase = get_supabase()
    res = (
        supabase.table("scraper_leads")
        .select("*")
        .eq("run_id", run_id)
        .eq("organization_id", organization_id)
        .in_("id", lead_ids)
        .execute()
    )
    return supabase, res.data


async def run_bd_messages_job(run_id: str, request: BDMessageRequest) -> None:
    """Generate outreach messages for a set of already-confirmed BD Group
    candidates. Only ever called on demand (by the CRM, after a human
    confirms a candidate) — never wired into the scraping flow in
    run_bd_job, since generating a paid message for every raw candidate
    before it's verified would waste API calls on contacts that get thrown
    out as noise.
    """
    organization_id = request.organization_id

    try:
        log_run(
            run_id,
            "info",
            f"BD messaging requested for {len(request.lead_ids)} lead(s)",
        )

        supabase, rows = _fetch_confirmed_bd_leads(run_id, organization_id, request.lead_ids)

        # Guard against generating messages for candidates that are still
        # 'pending' — this endpoint exists specifically so we never pay for a
        # message before a human has confirmed the candidate is real.
        leads = [row for row in rows if row.get("verification_status") != "pending"]
        skipped = len(rows) - len(leads)
        if skipped:
            log_run(
                run_id,
                "info",
                f"Skipped {skipped} lead(s) still verification_status='pending'",
            )

        if not leads:
            log_run(run_id, "info", "No confirmed BD leads to message; nothing to do")
            return

        sender_profile = _resolve_sender_profile(request)
        language = sender_profile.language if sender_profile else "en"
        product_description = get_organization_product_description(organization_id)
        hook_copy_by_channel_family = get_channel_hooks(organization_id)

        generate_bd_messages_for_batch(
            leads,
            request.anthropic_key,
            request.plan,
            sender_profile,
            language,
            request.anthropic_base_url,
            request.anthropic_model,
            product_description=product_description,
            hook_copy_by_channel_family=hook_copy_by_channel_family,
        )

        for lead in leads:
            supabase.table("scraper_leads").update(
                {"custom1": lead.get("custom1"), "custom2": lead.get("custom2")}
            ).eq("id", lead["id"]).execute()

        log_run(run_id, "info", f"BD messaging completed for {len(leads)} lead(s)")

    except Exception as exc:
        log_run(run_id, "error", f"BD messaging failed: {exc}")
        raise
