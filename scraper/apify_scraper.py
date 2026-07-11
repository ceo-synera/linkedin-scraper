import time
from typing import Any, Dict, List

from apify_client import ApifyClient

ACTOR_ID = "bestscrapers/sales-navigator-scraper-by-filters"

GEO_CODES: Dict[str, List[str]] = {
    "taiwan": ["104187078"],
    "latam": [
        "103323778",
        "100446943",
        "100877388",
        "104621616",
        "102927786",
        "105646813",
    ],
    "vietnam": ["104195383"],
    "global": [],
}

TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"}
POLL_INTERVAL_SECONDS = 5

# The actor only accepts these exact company-headcount range labels.
ALLOWED_COMPANY_HEADCOUNTS = {
    "Self-employed",
    "1-10",
    "11-50",
    "51-200",
    "201-500",
    "501-1000",
    "1001-5000",
    "5001-10000",
    "10001+",
}

# Combos in scraper_combos_master store LinkedIn Sales Navigator headcount
# letter codes; map them to the range labels the actor expects.
COMPANY_HEADCOUNT_CODE_MAP = {
    "A": "1-10",
    "B": "11-50",
    "C": "51-200",
    "D": "201-500",
    "E": "501-1000",
    "F": "1001-5000",
    "G": "5001-10000",
    "H": "10001+",
    "I": "10001+",
}


def _normalize_company_headcounts(values: List[Any]) -> List[str]:
    normalized: List[str] = []
    for value in values or []:
        if not isinstance(value, str):
            continue
        candidate = value.strip()
        if candidate in ALLOWED_COMPANY_HEADCOUNTS:
            normalized.append(candidate)
            continue
        mapped = COMPANY_HEADCOUNT_CODE_MAP.get(candidate.upper())
        if mapped:
            normalized.append(mapped)
    return normalized


def _run_field(run: Any, dict_key: str, attr_name: str) -> Any:
    # apify-client returns plain dicts (camelCase keys) on some versions and
    # typed Run objects (snake_case attributes) on others; support both.
    if run is None:
        return None
    if isinstance(run, dict):
        return run.get(dict_key)
    return getattr(run, attr_name, None)


def _wait_for_run(client: ApifyClient, run_id: str) -> Any:
    while True:
        run = client.run(run_id).get()
        if run and _run_field(run, "status", "status") in TERMINAL_STATUSES:
            return run
        time.sleep(POLL_INTERVAL_SECONDS)


def _scrape_combo(
    client: ApifyClient, combo: Dict[str, Any], geo_codes: List[str], leads_for_combo: int
) -> List[Dict[str, Any]]:
    run_input = {
        "title_keywords": combo.get("title_keywords", []),
        "seniority_levels": combo.get("seniority_levels", []),
        "company_headcounts": _normalize_company_headcounts(
            combo.get("company_headcounts", [])
        ),
        "functions": combo.get("functions", []),
        "geo_codes": geo_codes,
        "limit": leads_for_combo,
    }

    run = client.actor(ACTOR_ID).start(run_input=run_input)
    finished_run = _wait_for_run(client, _run_field(run, "id", "id"))

    if _run_field(finished_run, "status", "status") != "SUCCEEDED":
        return []

    dataset_id = _run_field(finished_run, "defaultDatasetId", "default_dataset_id")
    if not dataset_id:
        return []

    items = client.dataset(dataset_id).list_items().items

    leads: List[Dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("data"), list):
            leads.extend(item["data"])
        else:
            leads.append(item)
    return leads


def run_scraping(
    apify_token: str,
    combos: List[Dict[str, Any]],
    markets: List[str],
    total_leads: int,
) -> List[Dict[str, Any]]:
    client = ApifyClient(apify_token)

    all_leads: List[Dict[str, Any]] = []
    seen_linkedin_urls = set()

    if not markets:
        return all_leads

    leads_per_market = total_leads // len(markets)

    for market in markets:
        geo_codes = GEO_CODES.get(market, [])
        combos_for_market = combos or [{}]
        leads_per_combo = max(leads_per_market // len(combos_for_market), 1)

        for combo in combos_for_market:
            combo_leads = _scrape_combo(client, combo, geo_codes, leads_per_combo)

            for lead in combo_leads:
                linkedin_url = lead.get("linkedin_url") or lead.get("linkedinUrl")
                if linkedin_url:
                    if linkedin_url in seen_linkedin_urls:
                        continue
                    seen_linkedin_urls.add(linkedin_url)

                lead["market"] = market
                lead["combo"] = combo.get("code") if isinstance(combo, dict) else None
                all_leads.append(lead)

    return all_leads
