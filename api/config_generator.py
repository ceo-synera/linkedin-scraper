from typing import Any, Dict, List, Optional

from api.database import get_supabase
from api.models import SenderProfile
from scraper.apify_scraper import GEO_CODES

__all__ = ["GEO_CODES", "get_combo_definitions", "get_sender_profile"]


def get_combo_definitions(organization_id: str, combo_codes: List[str]) -> List[Dict[str, Any]]:
    supabase = get_supabase()

    org_combos_res = (
        supabase.table("org_combos")
        .select("combo_code")
        .eq("organization_id", organization_id)
        .eq("is_active", True)
        .in_("combo_code", combo_codes)
        .execute()
    )
    enabled_combo_codes = [row["combo_code"] for row in org_combos_res.data]

    if not enabled_combo_codes:
        return []

    combos_res = (
        supabase.table("scraper_combos_master")
        .select("*")
        .in_("combo_code", enabled_combo_codes)
        .execute()
    )
    return combos_res.data


def get_sender_profile(profile_id: str) -> Optional[SenderProfile]:
    supabase = get_supabase()

    res = (
        supabase.table("sender_profiles")
        .select("*")
        .eq("id", profile_id)
        .limit(1)
        .execute()
    )

    if not res.data:
        return None

    return SenderProfile(**res.data[0])
