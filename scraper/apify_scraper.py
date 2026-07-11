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


def _wait_for_run(client: ApifyClient, run_id: str) -> Dict[str, Any]:
    while True:
        run = client.run(run_id).get()
        if run and run.get("status") in TERMINAL_STATUSES:
            return run
        time.sleep(POLL_INTERVAL_SECONDS)


def _scrape_combo(
    client: ApifyClient, combo: Dict[str, Any], geo_codes: List[str], leads_for_combo: int
) -> List[Dict[str, Any]]:
    run_input = {
        "searchTitle": combo.get("name") or combo.get("code"),
        "titleKeywords": combo.get("title_keywords", []),
        "seniorityLevels": combo.get("seniority_levels", []),
        "companyHeadcounts": combo.get("company_headcounts", []),
        "functions": combo.get("functions", []),
        "geoCodes": geo_codes,
        "maxItems": leads_for_combo,
    }

    run = client.actor(ACTOR_ID).start(run_input=run_input)
    finished_run = _wait_for_run(client, run["id"])

    if finished_run.get("status") != "SUCCEEDED":
        return []

    dataset_id = finished_run.get("defaultDatasetId")
    if not dataset_id:
        return []

    items = client.dataset(dataset_id).list_items().items
    return items


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
