from typing import Any, Dict, List, Tuple

from api.database import get_supabase


def _chunk(items: List[str], size: int = 200) -> List[List[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def dedup_leads(
    leads: List[Dict[str, Any]], organization_id: str
) -> Tuple[List[Dict[str, Any]], int]:
    supabase = get_supabase()

    linkedin_urls = [
        lead.get("linkedin_url") or lead.get("linkedinUrl")
        for lead in leads
        if lead.get("linkedin_url") or lead.get("linkedinUrl")
    ]

    existing_urls = set()

    for batch in _chunk(linkedin_urls):
        scraper_leads_res = (
            supabase.table("scraper_leads")
            .select("linkedin_url")
            .eq("organization_id", organization_id)
            .in_("linkedin_url", batch)
            .execute()
        )
        existing_urls.update(row["linkedin_url"] for row in scraper_leads_res.data)

        prospects_res = (
            supabase.table("prospects")
            .select("linkedin_url")
            .eq("organization_id", organization_id)
            .in_("linkedin_url", batch)
            .execute()
        )
        existing_urls.update(row["linkedin_url"] for row in prospects_res.data)

    new_leads = []
    duplicates_count = 0

    for lead in leads:
        linkedin_url = lead.get("linkedin_url") or lead.get("linkedinUrl")
        if linkedin_url and linkedin_url in existing_urls:
            duplicates_count += 1
            continue
        new_leads.append(lead)

    return new_leads, duplicates_count
