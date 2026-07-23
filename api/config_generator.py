from typing import Any, Dict, List, Optional

from api.database import get_supabase
from api.models import SenderProfile

__all__ = [
    "get_combo_definitions",
    "get_market_geo_code",
    "get_market_language",
    "get_sender_profile",
    "list_markets",
    "list_organization_markets",
]

DEFAULT_MARKET_LANGUAGE = "en"


class MarketNotFoundError(Exception):
    """A market name that isn't in the markets table.

    Raised instead of quietly returning no geo filter — a silent empty
    geo_codes is what produced the "Spain in LATAM" bug, where runs scraped
    the whole world and nobody noticed until the leads looked wrong.
    """


def get_market_geo_code(market_name: str) -> Optional[int]:
    """The LinkedIn geo code for a market, or None if it isn't configured.

    Matching is case-insensitive (ilike) so "taiwan"/"Taiwan"/"TAIWAN" all
    resolve — the old dict lookup was case-sensitive and silently missed.
    """
    supabase = get_supabase()

    res = (
        supabase.table("markets")
        .select("geo_code")
        .ilike("name", market_name)
        .eq("is_active", True)
        .limit(1)
        .execute()
    )

    if not res.data:
        return None
    return res.data[0]["geo_code"]


def get_market_language(market_name: str) -> str:
    """Outreach language for a market; falls back to English.

    Deliberately tolerant where get_market_geo_code is strict: an unknown
    market should not block message generation, it just gets English.
    """
    supabase = get_supabase()

    res = (
        supabase.table("markets")
        .select("default_language")
        .ilike("name", market_name)
        .limit(1)
        .execute()
    )

    if not res.data:
        return DEFAULT_MARKET_LANGUAGE
    return res.data[0]["default_language"] or DEFAULT_MARKET_LANGUAGE


def get_market_languages(market_names: List[str]) -> Dict[str, str]:
    """Resolve several markets in one query, keyed by the caller's own spelling.

    Message generation needs a language per lead; doing a round trip per lead
    would mean hundreds of queries per run, so the run resolves its markets
    once up front and passes the map down.
    """
    languages: Dict[str, str] = {}
    if not market_names:
        return languages

    supabase = get_supabase()
    res = supabase.table("markets").select("name, default_language").execute()

    by_lower = {
        row["name"].lower(): (row.get("default_language") or DEFAULT_MARKET_LANGUAGE)
        for row in res.data
    }
    for market_name in market_names:
        languages[market_name] = by_lower.get(
            (market_name or "").lower(), DEFAULT_MARKET_LANGUAGE
        )
    return languages


def list_markets() -> List[Dict[str, Any]]:
    supabase = get_supabase()
    res = (
        supabase.table("markets")
        .select("id, name, geo_code, region, default_language")
        .eq("is_active", True)
        .order("region", desc=False)
        .order("name", desc=False)
        .execute()
    )
    return res.data


def list_organization_markets(organization_id: str) -> List[Dict[str, Any]]:
    """Markets an organization has enabled, scoped by organization_id."""
    supabase = get_supabase()
    res = (
        supabase.table("organization_markets")
        .select("markets(id, name, geo_code, region, default_language)")
        .eq("organization_id", organization_id)
        .execute()
    )
    # PostgREST nests the joined row under the related table's name.
    return [row["markets"] for row in res.data if row.get("markets")]


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


def get_sender_profile(
    profile_id: str, organization_id: str
) -> Optional[SenderProfile]:
    """Fetch a sender profile, scoped to the requesting organization.

    organization_id is part of the WHERE clause, not a post-fetch check: a
    profile_id belonging to another tenant simply matches no row and returns
    None, exactly as if it didn't exist. Without this scoping any org could
    pass another org's sender_profile_id and generate outreach under a real
    SDR identity that isn't theirs.
    """
    supabase = get_supabase()

    res = (
        supabase.table("sender_profiles")
        .select("*")
        .eq("id", profile_id)
        .eq("organization_id", organization_id)
        .limit(1)
        .execute()
    )

    if not res.data:
        return None

    return SenderProfile(**res.data[0])
