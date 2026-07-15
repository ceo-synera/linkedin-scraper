from typing import Any, Dict, List, Optional

from api.database import get_supabase
from api.models import SenderProfile
from scraper.apify_scraper import GEO_CODES

__all__ = [
    "GEO_CODES",
    "get_channel_hooks",
    "get_combo_definitions",
    "get_company_seed_lists",
    "get_organization_product_description",
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


def get_company_seed_lists(
    organization_id: str, seed_list_ids: List[str]
) -> List[Dict[str, Any]]:
    supabase = get_supabase()

    res = (
        supabase.table("org_company_seed_lists")
        .select("*")
        .eq("organization_id", organization_id)
        .in_("id", seed_list_ids)
        .execute()
    )
    return res.data


def get_organization_product_description(organization_id: str) -> Optional[str]:
    """One-time, org-authored description of what the org sells.

    May be unset for an org that hasn't filled it in yet, and the
    product_description column may not even exist on this Supabase yet — in
    both cases callers must degrade gracefully (omit it from the prompt), not
    error or fabricate one.
    """
    supabase = get_supabase()

    try:
        res = (
            supabase.table("organizations")
            .select("product_description")
            .eq("id", organization_id)
            .limit(1)
            .execute()
        )
    except Exception:
        # e.g. the product_description column doesn't exist yet (PGRST204 /
        # 42703). Treat it as unset rather than failing the whole run.
        return None

    if not res.data:
        return None
    return res.data[0].get("product_description") or None


def get_channel_hooks(organization_id: str) -> Dict[str, str]:
    """Fetch the org's own BD Group pitch angle, keyed by channel_family.

    A missing channel_family for this org simply has no entry — callers must
    fall back to a generic angle, not error.
    """
    supabase = get_supabase()

    res = (
        supabase.table("org_channel_hooks")
        .select("channel_family, hook_copy")
        .eq("organization_id", organization_id)
        .execute()
    )
    return {row["channel_family"]: row["hook_copy"] for row in res.data}


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
