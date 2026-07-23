import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from api.config_generator import (
    get_channel_hooks,
    get_company_seed_lists,
    get_sender_profile,
)
from api.database import get_supabase, log_run, update_run_status
from api.dedup import dedup_leads
from api.message_generator import generate_bd_messages_for_batch
from api.models import BDMessageRequest, BDRunRequest, SenderProfile
from scraper.apify_scraper import run_company_seed_scraping


async def _log(run_id: str, level: str, message: str) -> None:
    """log_run writes to Supabase synchronously; keep it off the event loop."""
    await asyncio.to_thread(log_run, run_id, level, message)


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
    leads: List[Dict[str, Any]],
    run_id: str,
    organization_id: str,
    channel_family_by_seed_list_id: Optional[Dict[str, Any]] = None,
) -> None:
    """Insert BD Group candidates into scraper_leads as tagged, unscored rows.

    No icp_score, no channel score, no outreach message — this phase only
    produces tagged, unverified candidates for a human to review.
    """
    if not leads:
        return

    supabase = get_supabase()
    now = datetime.now(timezone.utc).isoformat()
    channel_family_by_seed_list_id = channel_family_by_seed_list_id or {}

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
                "channel_family": channel_family_by_seed_list_id.get(lead.get("seed_list_id")),
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
        await asyncio.to_thread(update_run_status, run_id, "running")
        await _log(run_id, "info", "BD Group run started")

        seed_lists = await asyncio.to_thread(
            get_company_seed_lists, organization_id, bd_run_request.seed_list_ids
        )
        await _log(run_id, "info", f"Loaded {len(seed_lists)} company seed lists")

        channel_family_by_seed_list_id = {
            seed_list["id"]: seed_list.get("channel_family") for seed_list in seed_lists
        }

        # Blocking Apify scraping (HTTP + time.sleep polling) — off the loop.
        raw_leads = await asyncio.to_thread(
            run_company_seed_scraping,
            bd_run_request.apify_token,
            seed_lists,
            bd_run_request.total_leads,
            log_fn=lambda msg: log_run(run_id, "info", msg),
        )
        await _log(run_id, "info", f"Scraped {len(raw_leads)} raw BD candidates")

        new_leads, duplicates_count = await asyncio.to_thread(
            dedup_leads, raw_leads, organization_id
        )
        await _log(
            run_id,
            "info",
            f"Dedup complete: {len(new_leads)} new candidates, {duplicates_count} duplicates",
        )

        await asyncio.to_thread(
            import_bd_candidates_to_supabase,
            new_leads,
            run_id,
            organization_id,
            channel_family_by_seed_list_id,
        )
        await _log(run_id, "info", f"Stored {len(new_leads)} BD candidates in scraper_leads")

        # Best-effort bookkeeping: a schema mismatch here must not fail a run
        # whose candidates were already stored.
        try:
            await asyncio.to_thread(
                _update_run_sdr_assignment,
                run_id,
                bd_run_request.owner_sdr_id,
                len(new_leads),
            )
        except Exception as exc:
            await _log(run_id, "error", f"Bookkeeping update failed (non-fatal): {exc}")

        await asyncio.to_thread(
            update_run_status, run_id, "completed", total_leads=len(new_leads)
        )
        await _log(run_id, "info", "BD Group run completed")

    except Exception as exc:
        await _log(run_id, "error", f"BD Group run failed: {exc}")
        await asyncio.to_thread(
            update_run_status, run_id, "failed", error_message=str(exc)
        )
        raise


def _fetch_bd_leads_by_id(
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
        await _log(
            run_id,
            "info",
            f"BD messaging requested for {len(request.lead_ids)} lead(s)",
        )

        supabase, rows = await asyncio.to_thread(
            _fetch_bd_leads_by_id, run_id, organization_id, request.lead_ids
        )

        # Guard against generating messages for anything other than a
        # human-confirmed candidate. verification_status's CHECK constraint
        # (Phase 1 migration) only allows 'pending' | 'confirmed' | 'rejected'
        # — excluding just 'pending' would let 'rejected' rows (candidates a
        # human already discarded as noise) through too, defeating the point
        # of the verification step. Only 'confirmed' should ever get a paid
        # message generated for it.
        leads = [row for row in rows if row.get("verification_status") == "confirmed"]
        skipped = len(rows) - len(leads)
        if skipped:
            await _log(
                run_id,
                "info",
                f"Skipped {skipped} lead(s) not verification_status='confirmed' "
                "(still pending or already rejected)",
            )

        if not leads:
            await _log(run_id, "info", "No confirmed BD leads to message; nothing to do")
            return

        # _resolve_sender_profile / get_channel_hooks may hit Supabase.
        sender_profile = await asyncio.to_thread(_resolve_sender_profile, request)
        language = sender_profile.language if sender_profile else "en"
        hook_copy_by_channel_family = await asyncio.to_thread(
            get_channel_hooks, organization_id
        )

        await generate_bd_messages_for_batch(
            leads,
            request.anthropic_key,
            request.plan,
            sender_profile,
            language,
            request.anthropic_base_url,
            request.anthropic_model,
            hook_copy_by_channel_family=hook_copy_by_channel_family,
            log_fn=lambda msg: log_run(run_id, "info", msg),
        )

        def _persist_messages() -> None:
            for lead in leads:
                supabase.table("scraper_leads").update(
                    {"custom1": lead.get("custom1"), "custom2": lead.get("custom2")}
                ).eq("id", lead["id"]).execute()

        await asyncio.to_thread(_persist_messages)

        await _log(run_id, "info", f"BD messaging completed for {len(leads)} lead(s)")

    except Exception as exc:
        await _log(run_id, "error", f"BD messaging failed: {exc}")
        raise
