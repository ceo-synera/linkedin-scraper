from collections import defaultdict
from typing import Any, Dict, List, Optional

from api.database import get_supabase
from api.models import SenderProfile
from scraper.apify_scraper import GEO_CODES

__all__ = [
    "GEO_CODES",
    "get_combo_definitions",
    "get_icp_keywords",
    "get_sender_profile",
]


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
        .in_("code", enabled_combo_codes)
        .execute()
    )
    return combos_res.data


def get_icp_keywords(organization_id: str) -> Dict[str, List[Dict[str, Any]]]:
    """Fetch the org's ICP scoring keywords, grouped by category.

    Categories: industry, ai_signal, decision_title, influencer_title. A
    category missing from the config (or the whole table empty for this org)
    simply yields an empty list — callers must score that as 0, not error.
    """
    supabase = get_supabase()

    keywords_res = (
        supabase.table("org_icp_keywords")
        .select("category, keyword, weight")
        .eq("organization_id", organization_id)
        .execute()
    )

    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in keywords_res.data:
        grouped[row["category"]].append({"keyword": row["keyword"], "weight": row["weight"]})
    return dict(grouped)


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
