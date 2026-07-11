import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional

from apify_client import ApifyClient

log = logging.getLogger(__name__)

# Callback used to emit debug output. job_runner passes a callback that writes
# to the CRM's run_logs table; without one we fall back to stdout logging.
LogFn = Callable[[str], None]


def _dump(value: Any) -> str:
    try:
        return json.dumps(value, indent=2, default=str)
    except (TypeError, ValueError):
        return repr(value)

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

# The actor runs as two flows. Flow 1 (init search) is called with the
# filters and returns a request_id. Flow 2 (fetch results) is called with
# that request_id plus a page number and returns status "processing" (not
# ready yet) or "ok" with the leads in data[]. Each page holds up to 100
# leads.
PAGE_SIZE = 100
INIT_WAIT_SECONDS = 10
PROCESSING_POLL_SECONDS = 10
MAX_PROCESSING_RETRIES = 30  # 30 * 10s = 5 minutes

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

# Combos in scraper_combos_master may still store LinkedIn Sales Navigator
# headcount letter codes; map them to the range labels the actor expects.
# Valid range labels pass through unchanged.
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


def _call_actor(client: ApifyClient, run_input: Dict[str, Any]) -> List[Any]:
    # Both flows go through the same .call() entrypoint. .call() blocks until
    # the run finishes and returns the Run; the actor's response is whatever it
    # pushes to its default dataset. Return the full item list so callers can
    # log it verbatim for debugging.
    run = client.actor(ACTOR_ID).call(run_input=run_input)
    dataset_id = _run_field(run, "defaultDatasetId", "default_dataset_id")
    if not dataset_id:
        return []
    return client.dataset(dataset_id).list_items().items


def _first_dict(items: List[Any]) -> Dict[str, Any]:
    if items and isinstance(items[0], dict):
        return items[0]
    return {}


def _map_lead(item: Dict[str, Any]) -> Dict[str, Any]:
    # Keep both `job_title` (the raw actor field the ICP scorer reads) and
    # `title` (what job_runner / message_generator read); they hold the same
    # value.
    job_title = item.get("job_title")
    return {
        "full_name": item.get("full_name"),
        "first_name": item.get("first_name"),
        "last_name": item.get("last_name"),
        "job_title": job_title,
        "title": job_title,
        "company": item.get("company"),
        "company_id": item.get("company_id"),
        "linkedin_url": item.get("linkedin_url"),
        "location": item.get("location"),
        "about": item.get("about"),
        "profile_id": item.get("profile_id"),
    }


def _fetch_page(
    client: ApifyClient, request_id: str, page: int, emit: LogFn
) -> Optional[List[Dict[str, Any]]]:
    # Returns the mapped leads for the page, an empty list when the page has
    # no results, or None when the actor never stopped "processing".
    for attempt in range(MAX_PROCESSING_RETRIES):
        fetch_input = {"request_id": request_id, "page": page}
        emit(
            f"[Flow 2 fetch] attempt {attempt + 1}/{MAX_PROCESSING_RETRIES} "
            f"input: {_dump(fetch_input)}"
        )
        items = _call_actor(client, fetch_input)
        emit(f"[Flow 2 fetch] page {page} full actor output: {_dump(items)}")

        response = _first_dict(items)
        # The actor signals readiness via `message` ("ok"), not `status`, and
        # returns the leads under `data`. While the search is still running it
        # returns a non-"ok" message and no `data` list.
        message = response.get("message")
        data = response.get("data")
        ready = isinstance(data, list) or (
            isinstance(message, str) and message.strip().lower() == "ok"
        )
        emit(
            f"[Flow 2 fetch] page {page} message={message!r} "
            f"data items={len(data) if isinstance(data, list) else 0}"
        )

        if ready:
            rows = data if isinstance(data, list) else []
            leads = [_map_lead(item) for item in rows if isinstance(item, dict)]
            emit(f"[Flow 2 fetch] page {page} ok: {len(leads)} mapped leads")
            return leads

        emit(
            f"[Flow 2 fetch] page {page} not ready (message={message!r}), "
            f"waiting {PROCESSING_POLL_SECONDS}s"
        )
        time.sleep(PROCESSING_POLL_SECONDS)

    emit(
        f"[Flow 2 fetch] page {page} still processing after "
        f"{MAX_PROCESSING_RETRIES} retries; giving up"
    )
    return None


def _scrape_combo(
    client: ApifyClient,
    combo: Dict[str, Any],
    geo_codes: List[str],
    leads_for_combo: int,
    emit: LogFn,
) -> List[Dict[str, Any]]:
    # Flow 1 — init search.
    init_input = {
        "title_keywords": combo.get("title_keywords", []),
        "company_headcounts": _normalize_company_headcounts(
            combo.get("company_headcounts", [])
        ),
        "geo_codes": [int(code) for code in geo_codes],
        "posted_on_linkedin": "true",
        "seniority_levels": combo.get("seniority_levels", []),
        "limit": leads_for_combo,
    }
    emit(f"[Flow 1 init] input: {_dump(init_input)}")

    init_items = _call_actor(client, init_input)
    emit(f"[Flow 1 init] full actor output: {_dump(init_items)}")

    init_response = _first_dict(init_items)
    request_id = init_response.get("request_id")
    emit(f"[Flow 1 init] extracted request_id={request_id!r}")
    if not request_id:
        emit("[Flow 1 init] no request_id returned; aborting combo")
        return []

    # Give the search backend a moment before fetching the first page.
    emit(f"[Flow 1 init] waiting {INIT_WAIT_SECONDS}s before first fetch")
    time.sleep(INIT_WAIT_SECONDS)

    # Flow 2 — fetch results, paginating until we have enough leads or the
    # actor returns a short (last) page.
    leads: List[Dict[str, Any]] = []
    page = 1
    while len(leads) < leads_for_combo:
        page_leads = _fetch_page(client, request_id, page, emit)
        if page_leads is None:
            break  # gave up waiting on "processing"
        if not page_leads:
            break  # no more results
        leads.extend(page_leads)
        if len(page_leads) < PAGE_SIZE:
            break  # last page
        page += 1

    return leads[:leads_for_combo]


def run_scraping(
    apify_token: str,
    combos: List[Dict[str, Any]],
    markets: List[str],
    total_leads: int,
    log_fn: Optional[LogFn] = None,
) -> List[Dict[str, Any]]:
    emit: LogFn = log_fn or log.info
    client = ApifyClient(apify_token)

    all_leads: List[Dict[str, Any]] = []
    seen_linkedin_urls = set()

    if not markets:
        return all_leads

    leads_per_market = total_leads // len(markets)

    for market in markets:
        geo_codes = GEO_CODES.get(market.lower(), [])
        combos_for_market = combos or [{}]
        leads_per_combo = max(leads_per_market // len(combos_for_market), 1)

        for combo in combos_for_market:
            emit(
                f"[combo] market={market!r} code="
                f"{combo.get('code') if isinstance(combo, dict) else None!r} "
                f"leads_per_combo={leads_per_combo}"
            )
            combo_leads = _scrape_combo(client, combo, geo_codes, leads_per_combo, emit)

            for lead in combo_leads:
                if not isinstance(lead, dict):
                    continue

                # Real profiles always carry a linkedin_url; skip anything
                # without one and dedup within this run.
                linkedin_url = lead.get("linkedin_url")
                if not linkedin_url:
                    continue
                if linkedin_url in seen_linkedin_urls:
                    continue
                seen_linkedin_urls.add(linkedin_url)

                lead["market"] = market
                lead["combo"] = combo.get("code") if isinstance(combo, dict) else None
                all_leads.append(lead)

    return all_leads
