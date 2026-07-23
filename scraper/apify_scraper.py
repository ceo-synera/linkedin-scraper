import json
import logging
import math
import time
from typing import Any, Callable, Dict, List, Optional

from apify_client import ApifyClient

try:  # present on the apify-client versions we run (1.8+)
    from apify_client.errors import InvalidRequestError
except ImportError:  # pragma: no cover - defensive against client layout changes
    InvalidRequestError = None

# NOTE: this crosses the scraper/api boundary — scraper/ is otherwise
# Apify-only (no DB, no secrets persisted). It's needed here so a combo can
# check, page by page, whether Apify's results are actually new-to-DB before
# deciding to fetch another page (see _scrape_combo). This preliminary dedup
# only informs the pagination decision; job_runner's final dedup_leads call
# on the full scored batch remains the authoritative one that determines
# what's actually stored.
from api.config_generator import MarketNotFoundError, get_market_geo_code
from api.dedup import dedup_leads

log = logging.getLogger(__name__)

# Callback used to emit debug output. job_runner passes a callback that writes
# to the CRM's run_logs table; without one we fall back to stdout logging.
LogFn = Callable[[str], None]


def _dump(value: Any) -> str:
    # Compact single-line JSON: a payload must be one log line, not twenty
    # (indent=2 split each dict into many lines in Railway).
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return repr(value)

ACTOR_ID = "bestscrapers/sales-navigator-scraper-by-filters"

# Markets are no longer a hardcoded dict — they live in the `markets` table so
# an org can pick from real countries and adding one takes no code change. One
# market is now one country with one geo code; the old "latam" meta-market that
# bundled several countries is gone.

# The actor runs as two flows. Flow 1 (init search) is called with the
# filters and returns a request_id. Flow 2 (fetch results) is called with
# that request_id plus a page number and returns status "processing" (not
# ready yet) or "ok" with the leads in data[]. Each page holds up to 100
# leads.
PAGE_SIZE = 100
INIT_WAIT_SECONDS = 10
PROCESSING_POLL_SECONDS = 10
MAX_PROCESSING_RETRIES = 30  # 30 * 10s = 5 minutes

# Ask Apify for more than actually requested, to leave margin against
# expected DB dedup losses (the actor tends to resurface the same top
# profiles for a given filter set once they're already stored).
OVERFETCH_MULTIPLIER = 1.7

# Backfill cap: how many total pages (not extra pages — total, including
# page 1) a single combo will fetch while trying to reach its requested
# new-lead count via the preliminary per-page DB dedup check.
MAX_COMBO_PAGES = 3

# Safety valve on the cross-cell compensation below: without it, a cell late
# in the run can inherit the ENTIRE deficit of every underperforming cell
# before it, with no ceiling — one narrow combo could end up targeted for the
# whole run's total_leads, each with its own overfetch + up to MAX_COMBO_PAGES
# pagination, stretching a run to many minutes. No cell may be asked for more
# than this multiplier times its fair share of the run's original total_leads.
MAX_CELL_TARGET_MULTIPLIER = 2.5

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


# The actor rejects any search with more than 20 title_keywords, failing the
# whole input with InvalidRequestError. Truncating defensively means no combo —
# present or future — can ever fail a run for this reason.
MAX_TITLE_KEYWORDS = 20


def _truncate_title_keywords(
    values: List[str], emit: LogFn, label: str = "combo"
) -> List[str]:
    keywords = list(values or [])
    if len(keywords) > MAX_TITLE_KEYWORDS:
        emit(
            f"[{label}] title_keywords has {len(keywords)} items, truncating to "
            f"{MAX_TITLE_KEYWORDS} (Apify limit)"
        )
        return keywords[:MAX_TITLE_KEYWORDS]
    return keywords


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


# The actor only accepts these exact Sales Navigator seniority labels; any
# other value makes it reject the whole input with InvalidRequestError.
ALLOWED_SENIORITY_LEVELS = {
    "Owner/Partner",
    "CXO",
    "Vice President",
    "Director",
    "Experienced Manager",
    "Entry Level Manager",
    "Strategic",
    "Senior",
    "Entry Level",
    "In Training",
}

# High-confidence aliases the combos may store → the allowed labels. Anything
# not matched here (or already exact) is dropped and logged, so an unexpected
# value can never crash the run.
SENIORITY_LEVEL_ALIASES = {
    "owner": "Owner/Partner",
    "partner": "Owner/Partner",
    "owner/partner": "Owner/Partner",
    "cxo": "CXO",
    "c-level": "CXO",
    "c level": "CXO",
    "clevel": "CXO",
    "vp": "Vice President",
    "vice-president": "Vice President",
}


def _normalize_seniority_levels(values: List[Any], emit: LogFn) -> List[str]:
    normalized: List[str] = []
    dropped: List[str] = []
    for value in values or []:
        if not isinstance(value, str):
            dropped.append(str(value))
            continue
        candidate = value.strip()
        if candidate in ALLOWED_SENIORITY_LEVELS:
            mapped = candidate
        else:
            mapped = SENIORITY_LEVEL_ALIASES.get(candidate.lower())
        if mapped:
            if mapped not in normalized:
                normalized.append(mapped)
        else:
            dropped.append(candidate)
    if dropped:
        emit(f"[seniority] dropped unmapped values (not in actor enum): {dropped}")
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
    #
    # logger=None disables apify-client's default actor-log streaming — the
    # "[apify.<actor> runId:...] Status: RUNNING/SUCCEEDED" lines it prints,
    # which Railway tagged as errors. Our own [Flow ...] logs already narrate
    # the run.
    run = client.actor(ACTOR_ID).call(run_input=run_input, logger=None)
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


def _paginate(
    client: ApifyClient, request_id: str, limit: int, emit: LogFn
) -> List[Dict[str, Any]]:
    # Flow 2 — fetch results, paginating until we have enough leads or the
    # actor returns a short (last) page. Shared by every search flow (title/geo
    # combos, company-seed batches) once Flow 1 has handed back a request_id.
    leads: List[Dict[str, Any]] = []
    page = 1
    while len(leads) < limit:
        page_leads = _fetch_page(client, request_id, page, emit)
        if page_leads is None:
            break  # gave up waiting on "processing"
        if not page_leads:
            break  # no more results
        leads.extend(page_leads)
        if len(page_leads) < PAGE_SIZE:
            break  # last page
        page += 1

    return leads[:limit]


def _init_search(client: ApifyClient, init_input: Dict[str, Any], emit: LogFn) -> Optional[str]:
    init_items = _call_actor(client, init_input)
    init_response = _first_dict(init_items)
    request_id = init_response.get("request_id")
    emit(f"[Flow 1 init] request_id={request_id!r} message={init_response.get('message')!r}")
    if not request_id:
        emit("[Flow 1 init] no request_id returned; aborting")
        return None

    # Give the search backend a moment before fetching the first page.
    emit(f"[Flow 1 init] waiting {INIT_WAIT_SECONDS}s before first fetch")
    time.sleep(INIT_WAIT_SECONDS)
    return request_id


def _scrape_combo(
    client: ApifyClient,
    combo: Dict[str, Any],
    geo_codes: List[str],
    leads_requested: int,
    organization_id: str,
    market: str,
    combo_code: Optional[str],
    emit: LogFn,
) -> List[Dict[str, Any]]:
    # Overfetch: ask Apify for more than leads_requested, so there's margin
    # left once already-scraped/already-stored profiles get filtered out by
    # the DB dedup check below.
    leads_to_fetch = int(leads_requested * OVERFETCH_MULTIPLIER)
    emit(
        f"[combo] market={market!r} code={combo_code!r} "
        f"requested={leads_requested} fetching={leads_to_fetch}"
    )

    # Flow 1 — init search.
    init_input = {
        "title_keywords": _truncate_title_keywords(
            combo.get("title_keywords", []), emit
        ),
        "company_headcounts": _normalize_company_headcounts(
            combo.get("company_headcounts", [])
        ),
        "geo_codes": [int(code) for code in geo_codes],
        "posted_on_linkedin": "true",
        "seniority_levels": _normalize_seniority_levels(
            combo.get("seniority_levels", []), emit
        ),
        "limit": leads_to_fetch,
    }
    emit(f"[Flow 1 init] input: {_dump(init_input)}")

    # Flow 1 (_init_search) runs exactly once here, so its one-time 5-10 min
    # wait only ever happens once per combo. Every extra page below reuses
    # this same request_id through Flow 2 (_fetch_page) — Apify already has
    # the results computed internally, so re-requesting a page is a matter of
    # seconds, not a new long wait.
    request_id = _init_search(client, init_input, emit)
    if not request_id:
        return []

    # Flow 2 — fetch pages, running a preliminary DB dedup after each page to
    # decide whether fetching another page is worth it. This dedup only
    # drives the pagination decision here; job_runner's dedup_leads on the
    # full scored batch remains the authoritative check before storing.
    all_page_leads: List[Dict[str, Any]] = []
    new_leads: List[Dict[str, Any]] = []
    page = 1
    while True:
        page_leads = _fetch_page(client, request_id, page, emit)
        if not page_leads:
            break  # gave up waiting on "processing", or truly zero results

        all_page_leads.extend(page_leads)
        new_leads, _duplicates = dedup_leads(all_page_leads, organization_id)
        new_count = len(new_leads)

        if new_count >= leads_requested:
            break  # dedup already left us with enough new leads

        if len(page_leads) < PAGE_SIZE:
            break  # actor signaled this was the last available page

        if page >= MAX_COMBO_PAGES:
            emit(
                f"[combo] market={market!r} code={combo_code!r} reached max "
                f"pages ({MAX_COMBO_PAGES}), got {new_count}/{leads_requested} "
                "new leads"
            )
            break

        emit(
            f"[combo] market={market!r} code={combo_code!r} after page {page}: "
            f"{new_count} new leads (need {leads_requested}), fetching page {page + 1}"
        )
        page += 1

    return new_leads


# Company-name batching, shared by Bridge's run_bridge_scraping: the actor
# only accepts a limited number of current_company_names per request.
MAX_COMPANY_NAMES_PER_BATCH = 10


def _chunk_list(items: List[str], size: int) -> List[List[str]]:
    if not items:
        return [[]]
    return [items[i : i + size] for i in range(0, len(items), size)]


# ---------------------------------------------------------------------------
# Bridge (partnership discovery)
#
# A separate product from the lead scraper: it looks for partnership contacts
# inside specific target companies/industries, for an admin to review by hand.
# It shares the actor's two-flow protocol via _init_search / _paginate, but
# deliberately not _scrape_combo — the overfetch + DB-dedup backfill there is
# tuned for the lead pipeline and reads scraper_leads, which Bridge must never
# touch.
# ---------------------------------------------------------------------------

# Fixed for every Bridge search — unlike the lead combos, the admin doesn't
# configure these. They only choose which companies/industries to search in.
BRIDGE_TITLE_KEYWORDS = [
    "Head of Partnerships",
    "VP Partnerships",
    "Director of Partnerships",
    "Partnerships Manager",
    "Business Development Manager",
    "Head of Business Development",
    "Alliances Director",
    "Alliances Manager",
    "Channel Manager",
    "Channel Partnerships Manager",
    "VP Business Development",
    "Director de Alianzas",
    "Gerente de Alianzas",
    "Director de Desarrollo de Negocios",
    "夥伴關係經理",
    "業務開發經理",
    "策略聯盟主管",
]

# Few people hold these titles at any one company, so ask for a handful per
# company rather than a big page.
BRIDGE_RESULTS_PER_COMPANY = 3

# When no company_names are given the search is filter-only (industry /
# headcount / geo), so there's no per-company anchor to scale the limit from.
# Cap it at a single page.
BRIDGE_FILTER_ONLY_LIMIT = PAGE_SIZE


def _is_invalid_input_error(exc: Exception) -> bool:
    # Prefer the typed error; fall back to the message so a client-layout
    # change can't silently disable the retry.
    if InvalidRequestError is not None and isinstance(exc, InvalidRequestError):
        return True
    return "Input is not valid" in str(exc)


def _init_bridge_search(
    client: ApifyClient, init_input: Dict[str, Any], emit: LogFn
) -> Optional[str]:
    """Flow 1 for Bridge, tolerating an actor that rejects industry_codes.

    industry_codes isn't a field we've confirmed the actor accepts. Since the
    actor rejects the *entire* input on an unknown/invalid field, sending it
    blind would fail the whole run. Instead: try with it, and if the actor
    rejects the input, retry once without it — losing that one filter rather
    than the run.
    """
    try:
        return _init_search(client, init_input, emit)
    except Exception as exc:
        if "industry_codes" not in init_input or not _is_invalid_input_error(exc):
            raise

        emit(
            "[bridge] industry_codes not supported by actor, retrying without it "
            f"(actor said: {exc})"
        )
        fallback_input = {
            key: value for key, value in init_input.items() if key != "industry_codes"
        }
        emit(f"[Bridge Flow 1 init] retry input: {_dump(fallback_input)}")
        return _init_search(client, fallback_input, emit)


def run_bridge_scraping(
    apify_token: str,
    company_names: List[str],
    industry_codes: List[int],
    company_headcounts: List[str],
    geo_codes: List[int],
    title_keywords: List[str],
    limit: int = BRIDGE_RESULTS_PER_COMPANY,
    log_fn: Optional[LogFn] = None,
) -> List[Dict[str, Any]]:
    """Find partnership contacts for a Bridge seed list.

    `limit` is per company: a batch of N companies asks the actor for
    limit * N results. Company-name mode and filter-only mode are combinable —
    whatever the seed list has set gets sent together.
    """
    emit: LogFn = log_fn or log.info
    client = ApifyClient(apify_token)

    title_keywords = _truncate_title_keywords(title_keywords, emit, label="bridge")

    # Filters shared by every search, only included when actually set so the
    # actor never receives empty arrays it might treat as "match nothing".
    base_filters: Dict[str, Any] = {}
    normalized_headcounts = _normalize_company_headcounts(company_headcounts)
    if normalized_headcounts:
        base_filters["company_headcounts"] = normalized_headcounts
    if geo_codes:
        base_filters["geo_codes"] = [int(code) for code in geo_codes]
    if industry_codes:
        base_filters["industry_codes"] = [int(code) for code in industry_codes]

    # Company-name searches are chunked; a filter-only search runs once.
    batches = (
        _chunk_list(company_names, MAX_COMPANY_NAMES_PER_BATCH)
        if company_names
        else [[]]
    )

    candidates: List[Dict[str, Any]] = []
    seen_linkedin_urls = set()

    for batch in batches:
        search_limit = limit * len(batch) if batch else BRIDGE_FILTER_ONLY_LIMIT

        init_input: Dict[str, Any] = {
            "title_keywords": title_keywords,
            "limit": search_limit,
            **base_filters,
        }
        if batch:
            init_input["current_company_names"] = batch

        emit(
            f"[bridge] searching {len(batch)} company name(s), "
            f"limit={search_limit}"
        )
        emit(f"[Bridge Flow 1 init] input: {_dump(init_input)}")

        request_id = _init_bridge_search(client, init_input, emit)
        if not request_id:
            continue

        for lead in _paginate(client, request_id, search_limit, emit):
            linkedin_url = lead.get("linkedin_url")
            if not linkedin_url or linkedin_url in seen_linkedin_urls:
                continue
            seen_linkedin_urls.add(linkedin_url)
            candidates.append(lead)

        emit(f"[bridge] running total: {len(candidates)} candidate(s)")

    return candidates


def run_scraping(
    apify_token: str,
    combos: List[Dict[str, Any]],
    markets: List[str],
    total_leads: int,
    organization_id: str,
    log_fn: Optional[LogFn] = None,
) -> List[Dict[str, Any]]:
    emit: LogFn = log_fn or log.info
    client = ApifyClient(apify_token)

    all_leads: List[Dict[str, Any]] = []
    seen_linkedin_urls = set()

    if not markets:
        return all_leads

    # Flatten (market, combo) into cells and give each a dynamic target of the
    # remaining shortfall spread over the cells still to come. A combo that
    # returns fewer real matches than its share (now measured post-DB-dedup,
    # see _scrape_combo's overfetch + pagination backfill) leaves `remaining`
    # high, so later cells automatically pick up the slack — instead of the
    # old fixed per-combo cap that could never recover an under-delivering
    # combo. Only leads actually added (after this run's own dedup, on top of
    # the DB dedup _scrape_combo already applied) count toward the target.
    # Resolve every market's geo code up front, so an unknown market fails the
    # run immediately with a clear message — before spending a single Apify
    # call — instead of silently scraping with no geo filter, which is exactly
    # how the "Spain in LATAM" bug went unnoticed.
    geo_code_by_market: Dict[str, int] = {}
    for market in markets:
        geo_code = get_market_geo_code(market)
        if geo_code is None:
            raise MarketNotFoundError(f"Market '{market}' not found in markets table")
        geo_code_by_market[market] = geo_code
        emit(f"[market] {market!r} -> geo_code={geo_code}")

    cells = [(market, combo) for market in markets for combo in (combos or [{}])]
    remaining = total_leads

    # Fixed for the whole run — the original fair share if every cell had
    # performed equally, not recomputed against cells_left like cell_target is.
    fair_share_cap = math.ceil(
        (total_leads / len(cells)) * MAX_CELL_TARGET_MULTIPLIER
    )

    for index, (market, combo) in enumerate(cells):
        if remaining <= 0:
            break

        cells_left = len(cells) - index
        cell_target = max(-(-remaining // cells_left), 1)  # ceil division
        geo_codes = [geo_code_by_market[market]]
        combo_code = combo.get("code") if isinstance(combo, dict) else None

        if cell_target > fair_share_cap:
            emit(
                f"[combo] market={market!r} code={combo_code!r} target capped at "
                f"{fair_share_cap} (fair share ceiling), some leads may fall "
                "short of total_leads requested"
            )
            cell_target = fair_share_cap

        emit(
            f"[combo] market={market!r} code={combo_code!r} "
            f"target={cell_target} remaining={remaining}"
        )
        combo_leads = _scrape_combo(
            client, combo, geo_codes, cell_target, organization_id, market, combo_code, emit
        )

        added = 0
        for lead in combo_leads:
            if not isinstance(lead, dict):
                continue

            # Real profiles always carry a linkedin_url; skip anything without
            # one and dedup within this run.
            linkedin_url = lead.get("linkedin_url")
            if not linkedin_url or linkedin_url in seen_linkedin_urls:
                continue
            seen_linkedin_urls.add(linkedin_url)

            lead["market"] = market
            lead["combo"] = combo_code
            all_leads.append(lead)
            added += 1

        remaining -= added
        emit(f"[combo] market={market!r} code={combo_code!r} added={added} remaining={remaining}")

    return all_leads
