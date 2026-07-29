import inspect
import json
import logging
import math
import os
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

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
from api.config_generator import (
    MarketNotFoundError,
    MixedRegionMarketsError,
    region_label,
    resolve_markets,
)
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

# ---------------------------------------------------------------------------
# HarvestAPI — alternative search actor (opt-in, off by default)
# ---------------------------------------------------------------------------
#
# WHY: the incumbent actor bills $0.50 per init_search plus $0.01 per
# country×combo cell — i.e. per cell REQUESTED, whether or not it returns a
# single lead. A measured run (16 countries × 3 combos, 25 leads) was billed
# for 48 cells; another (5 countries × 3 combos) returned 85 leads for 15
# cells — a 10x spread in cost per lead driven purely by how many countries
# were ticked, not by output. HarvestAPI bills per profile actually returned
# ($0.10 per search page of ~25 + $0.004 per full profile), so cost tracks
# results instead of request breadth, and it accepts every country in ONE
# call rather than one search per country.
#
# It is also a materially better-maintained actor: 4.8/5 over 82 reviews and
# 33K users, against 0.0/0 reviews and 584 users for the incumbent.
#
# HOW: set SCRAPER_ACTOR=harvest to switch. Anything else (including unset)
# keeps the incumbent path untouched. This is deliberately a runtime switch
# and not a replacement: the output field mapping below is inferred from the
# actor's published sample and has NOT yet been confirmed against a real run,
# so the safe rollback has to be one env var, not a revert.
HARVEST_ACTOR_ID = "harvestapi/linkedin-profile-search"


def _use_harvest() -> bool:
    return os.getenv("SCRAPER_ACTOR", "").strip().lower() == "harvest"


# Sales Navigator headcount label -> HarvestAPI code.
#
# ⚠ These letters are NOT the same letters the incumbent actor uses, and the
# collision is silent. COMPANY_HEADCOUNT_CODE_MAP below maps A->"1-10";
# HarvestAPI's A is "Self-employed" and its B is "1-10" — every bracket is
# shifted by one. Passing a letter straight through from one actor to the
# other therefore yields a plausible-looking filter for the WRONG company
# size, with no error anywhere. Always map via the human-readable label.
# Confirmed against the actor's own input reference; "10001+"->"I" is the one
# row extrapolated past the end of that table and still needs verifying.
HARVEST_HEADCOUNT = {
    "1-10": "B",
    "11-50": "C",
    "51-200": "D",
    "201-500": "E",
    "501-1000": "F",
    "1001-5000": "G",
    "5001-10000": "H",
    "10001+": "I",  # UNVERIFIED — table in the actor docs stops at H
}

# Sales Navigator seniority label -> HarvestAPI numeric id (sent as a STRING;
# the actor's own console emits e.g. ["220","310","300"], not integers).
# All ten of ALLOWED_SENIORITY_LEVELS map 1:1 — both actors use the same
# Sales Navigator vocabulary, so nothing is lost in translation here.
HARVEST_SENIORITY = {
    "In Training": "100",
    "Entry Level": "110",
    "Senior": "120",
    "Strategic": "130",
    "Entry Level Manager": "200",
    "Experienced Manager": "210",
    "Director": "220",
    "Vice President": "300",
    "CXO": "310",
    "Owner/Partner": "320",
}

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
# profiles for a given filter set once they're already stored). Kept modest
# (was 1.7): a bigger multiplier means more results per page to fetch and
# process, which measurably lengthens every combo's Flow 2 cycle. Overshoot
# past total_leads is no longer capped per-cell — job_runner trims the global
# pool by ICP score once, after scoring (see run_scraping's module docstring
# note below and job_runner.run_job).
OVERFETCH_MULTIPLIER = 1.2

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
MAX_CELL_TARGET_MULTIPLIER = 2.0

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


# If the actor's "processing" message (e.g. a "Done X/Y" progress indicator)
# comes back byte-for-byte identical this many attempts in a row, the search
# probably isn't making progress. Set generously: the actor can take several
# ~14s poll cycles between progress increments without being stuck, and a run
# was aborted at "Done 49/100" simply because Apify was slow between bumps.
# 8 * ~14s ≈ ~110s of genuine no-change before we suspect a real stall — long
# enough to tell "slow" from "stuck" (was 3 ≈ 42s, too twitchy).
STALL_LIMIT = 8

# When a stall is finally suspected, wait this much longer once more and refetch
# before giving up. The actor only returns leads once the whole search flips to
# "ok" — a processing response carries no partial data to salvage — so this last,
# longer window is the only way to recover an almost-finished search's results
# (e.g. one sitting at "Done 100/100" about to flip). If it's genuinely stuck
# this costs one extra wait, not the full MAX_PROCESSING_RETRIES.
STALL_GRACE_SECONDS = 45


def _harvest_input(
    combo: Dict[str, Any],
    market_names: List[str],
    limit: int,
    start_page: int,
    emit: LogFn,
) -> Dict[str, Any]:
    """Build HarvestAPI's input from the same combo shape the incumbent uses.

    Key names confirmed against the actor console's own JSON output, so they
    are exact rather than guessed. Two structural differences from the
    incumbent are worth knowing:

      * `locations` takes country NAMES, not LinkedIn geo codes — which is
        what `run_scraping` already receives in `markets`, so the geo_code
        lookup is not needed on this path at all.
      * `locations` accepts the whole list in ONE call. The incumbent needed
        one search per country×combo cell; here 16 countries and 1 combo is a
        single request. This is the change that removes the cost blow-up.
    """
    headcounts: List[str] = []
    for label in _normalize_company_headcounts(combo.get("company_headcounts", [])):
        code = HARVEST_HEADCOUNT.get(label)
        if code:
            headcounts.append(code)
        else:
            emit(f"[harvest] dropped unmapped headcount label: {label!r}")

    seniorities: List[str] = []
    for label in _normalize_seniority_levels(combo.get("seniority_levels", []), emit):
        sid = HARVEST_SENIORITY.get(label)
        if sid:
            seniorities.append(sid)
        else:
            emit(f"[harvest] dropped unmapped seniority label: {label!r}")

    payload: Dict[str, Any] = {
        # "Full" = $0.10 per search page + $0.004 per profile. "Full + email
        # search" costs $0.01 per profile instead and adds an email column we
        # have nowhere to store yet — revisit once prospects has a field for
        # it, since it is the cheapest email source we have found.
        "profileScraperMode": "Full",
        "currentJobTitles": _truncate_title_keywords(
            combo.get("title_keywords", []), emit
        ),
        "locations": list(market_names),
        "maxItems": limit,
        "startPage": start_page,
        # Left off deliberately: autoQuerySegmentation splits one query into
        # many to break past LinkedIn's ~1000-result-per-query ceiling. It
        # multiplies search pages (and therefore cost), and our per-combo
        # targets are far below that ceiling, so it would be spend with no
        # return. Turn it on only for a combo that provably saturates.
        "autoQuerySegmentation": False,
    }
    if headcounts:
        payload["companyHeadcount"] = headcounts
    if seniorities:
        payload["seniorityLevelIds"] = seniorities
    if combo.get("posted_on_linkedin"):
        payload["recentlyPostedOnLinkedIn"] = True
    return payload


# HarvestAPI returns camelCase and splits the name into parts; the incumbent
# returned snake_case with a pre-joined `full_name`. Each tuple is (our field,
# candidate source keys in priority order) so one mapper tolerates both the
# documented shape and minor drift between actor builds.
_HARVEST_FIELD_CANDIDATES = (
    ("first_name", ("firstName", "first_name")),
    ("last_name", ("lastName", "last_name")),
    ("job_title", ("headline", "jobTitle", "job_title", "title")),
    ("linkedin_url", ("linkedinUrl", "profileUrl", "url", "linkedin_url")),
    ("location", ("location", "locationName", "geoLocation")),
    ("about", ("about", "summary")),
    ("profile_id", ("publicIdentifier", "id", "profile_id")),
)


def _pick(item: Dict[str, Any], keys: Tuple[str, ...]) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _first_position(item: Dict[str, Any]) -> Dict[str, Any]:
    """The current role, as a dict, from whichever shape the actor used.

    Company name and id are NOT top-level fields — confirmed against a real
    run, whose top-level keys were firstName/lastName/headline/linkedinUrl/
    location/about/publicIdentifier/currentPosition/experience/... with no
    `companyName` anywhere. Reading `companyName` off the item (as the first
    version of this mapper did) therefore yielded None for every single lead,
    silently: the run reports success, leads store fine, and the damage only
    surfaces as outreach addressed to "<name> at None".

    `currentPosition` may be a list or a single dict depending on build, so
    both are handled; `experience[0]` is the fallback.
    """
    for key in ("currentPosition", "experience"):
        value = item.get(key)
        if isinstance(value, list) and value:
            value = value[0]
        if isinstance(value, dict) and value:
            return value
    return {}


def _map_harvest_lead(item: Dict[str, Any]) -> Dict[str, Any]:
    lead: Dict[str, Any] = {}
    for field, candidates in _HARVEST_FIELD_CANDIDATES:
        lead[field] = _pick(item, candidates)

    # `full_name` has no direct counterpart — HarvestAPI splits the name — so
    # rebuild it. Downstream (`cleanScrapedName`, message generation, the CRM's
    # prospects.name) all read full_name, so leaving it None would produce
    # nameless leads that look like a scraper failure rather than a mapping bug.
    full_name = _pick(item, ("fullName", "full_name", "name"))
    if not full_name:
        parts = [lead.get("first_name"), lead.get("last_name")]
        full_name = " ".join(p for p in parts if p).strip() or None
    lead["full_name"] = full_name

    # Company lives inside the current role, never at the top level. Getting
    # this wrong is expensive rather than merely wrong: `company` is what the
    # outreach message is built around, so a None here ships personalised
    # messages that address the prospect at no company at all.
    position = _first_position(item)
    lead["company"] = _pick(position, ("companyName", "company", "name"))
    lead["company_id"] = _pick(position, ("companyId", "company_id", "id"))

    # `headline` is a free-text tagline ("CIO | Digital Transformation | ...")
    # rather than a job title. Prefer the structured role title when the
    # position carries one, and keep the headline only as the fallback — the
    # ICP scorer matches this against the combo's title keywords, so a
    # marketing tagline scores worse than the actual role.
    position_title = _pick(position, ("title", "position", "jobTitle"))
    if position_title:
        lead["job_title"] = position_title

    # `title` and `job_title` are the same value under two names: the ICP
    # scorer reads job_title, job_runner and the message generator read title.
    lead["title"] = lead.get("job_title")
    return lead


def _scrape_combo_harvest(
    client: ApifyClient,
    combo: Dict[str, Any],
    market_names: List[str],
    cell_target: int,
    organization_id: str,
    market: str,
    combo_code: Optional[str],
    emit: LogFn,
) -> Dict[str, Any]:
    """HarvestAPI equivalent of `_scrape_combo`, returning the same session
    dict so everything above it — the absorb/dedup loop, the 80% shortfall
    retry, market detection — works unchanged.

    The two-flow machinery has no equivalent here and is not reimplemented:
    HarvestAPI is a single synchronous call, so there is no request_id to
    carry, no 5-10 minute init wait to amortise, and no "processing" state to
    poll. `request_id` stays None in the session for exactly that reason.
    """
    leads_to_fetch = int(cell_target * OVERFETCH_MULTIPLIER)
    session: Dict[str, Any] = {
        "combo_code": combo_code,
        "market": market,
        "cell_target": cell_target,
        "request_id": None,
        "raw_leads": [],
        "leads": [],
        "last_page": 0,
        "exhausted": True,
    }

    run_input = _harvest_input(combo, market_names, leads_to_fetch, 1, emit)
    emit(f"[harvest] code={combo_code!r} input: {_dump(run_input)}")

    try:
        items = _call_actor_harvest(client, run_input)
    except Exception as exc:  # noqa: BLE001 - one combo must not kill the run
        emit(f"[harvest] code={combo_code!r} actor call failed: {exc!r}")
        return session

    if not items:
        emit(f"[harvest] code={combo_code!r} returned no items")
        return session

    # Log the first item's keys verbatim once per combo. The field mapping
    # above is inferred from published samples, and this line is what turns a
    # silent mis-map (leads arriving with every field None) into something
    # diagnosable from the Railway logs alone.
    emit(f"[harvest] first item keys: {sorted(_first_dict(items).keys())}")

    raw = [item for item in items if isinstance(item, dict)]
    mapped = [_map_harvest_lead(item) for item in raw]
    mapped = [lead for lead in mapped if lead.get("linkedin_url")]

    new_leads, _ = dedup_leads(mapped, organization_id) if mapped else ([], 0)
    session["raw_leads"] = raw
    session["leads"] = new_leads
    session["last_page"] = 1
    # A short result set means the query is spent; a full one means more pages
    # exist. Mirrors the incumbent's meaning so the shortfall retry above can
    # reason about both paths identically.
    session["exhausted"] = len(raw) < leads_to_fetch
    # The incumbent's retry resumes by request_id alone; HarvestAPI has no such
    # handle, so the retry has to rebuild the query. Carry what it needs.
    session["combo"] = combo
    session["market_names"] = list(market_names)
    emit(
        f"[harvest] code={combo_code!r} raw={len(raw)} mapped={len(mapped)} "
        f"new_after_dedup={len(new_leads)} exhausted={session['exhausted']}"
    )
    return session


def _retry_harvest_page(
    client: ApifyClient,
    session: Dict[str, Any],
    organization_id: str,
    emit: LogFn,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], int, bool]:
    """Fetch one more page for a HarvestAPI combo that fell short.

    Returns the same 4-tuple as `_paginate_with_dedup` so the retry loop
    treats both actors identically. Dedup runs over the combo's ACCUMULATED
    raw items, not just this page's, so a lead already collected in the main
    pass is not counted twice toward the target.
    """
    next_page = session["last_page"] + 1
    shortfall = session["cell_target"] - len(session["leads"])
    run_input = _harvest_input(
        session["combo"],
        session["market_names"],
        max(shortfall, 1),
        next_page,
        emit,
    )
    emit(
        f"[harvest retry] code={session['combo_code']!r} page={next_page} "
        f"short by {shortfall}: {_dump(run_input)}"
    )

    try:
        items = _call_actor_harvest(client, run_input)
    except Exception as exc:  # noqa: BLE001 - a failed retry is not a failed run
        emit(f"[harvest retry] code={session['combo_code']!r} failed: {exc!r}")
        return session["leads"], session["raw_leads"], session["last_page"], True

    page_raw = [item for item in items if isinstance(item, dict)]
    if not page_raw:
        emit(f"[harvest retry] code={session['combo_code']!r} page {next_page} empty")
        return session["leads"], session["raw_leads"], next_page, True

    raw = list(session["raw_leads"]) + page_raw
    mapped = [_map_harvest_lead(item) for item in raw]
    mapped = [lead for lead in mapped if lead.get("linkedin_url")]
    new_leads, _ = dedup_leads(mapped, organization_id) if mapped else ([], 0)

    exhausted = len(page_raw) < max(shortfall, 1)
    emit(
        f"[harvest retry] code={session['combo_code']!r} page={next_page} "
        f"got={len(page_raw)} total_new={len(new_leads)} exhausted={exhausted}"
    )
    return new_leads, raw, next_page, exhausted


# `.call()` blocks until the actor finishes and, left alone, honours the
# ACTOR's own timeout — which for linkedin-profile-search is 30,000s, i.e. over
# eight hours. A queued or hung run would therefore pin a Railway worker for
# the rest of the day with no log line after "Searching LinkedIn via Apify...".
# (Observed: a run on a Free-plan token sat at READY, never started, and the
# call never returned.) The incumbent path was implicitly bounded by
# MAX_PROCESSING_RETRIES; this replaces that bound explicitly.
#
# 6 minutes: a healthy 30-profile call measured ~75s, so this is ~5x headroom
# for a slow one while still failing the combo — not the run — inside a
# timeframe a human is willing to wait.
HARVEST_CALL_TIMEOUT_SECONDS = 360


def _harvest_charge_cap(max_items: int) -> float:
    """Hard ceiling on what one combo call may bill, in USD.

    Apify enforces this server-side and aborts the run rather than exceeding
    it, so it is a real guard and not just bookkeeping.

    Sized from measured pricing — $0.10 per search page of ~25 profiles plus
    $0.004 per profile — then multiplied by 4. A healthy 30-profile call bills
    about $0.32 and is capped at $1.60, so the cap only ever fires on a run
    that has gone materially wrong (pathological filters paging forever, an
    actor-side pricing change). Without it, a single misbehaving combo can bill
    without bound; that failure mode is precisely what this whole migration
    exists to remove, so leaving it unguarded would be inconsistent.
    """
    expected = (max_items / 25.0) * 0.10 + max_items * 0.004
    return round(max(expected * 4, 1.0), 2)


def _supported_call_kwargs(actor_client: Any, desired: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only the kwargs this installed apify-client's .call() accepts.

    requirements.txt pins `apify-client>=1.7.0`, an open range, so the version
    actually deployed is whatever the last image build resolved — not whatever
    is current. `timeout_secs`, `wait_secs` and `max_total_charge_usd` are all
    recent additions; passing them blind raised
    `TypeError: unexpected keyword argument 'timeout_secs'` on Railway and
    failed every combo, while the same call worked against a freshly installed
    2.5.1 locally.

    Introspecting the signature means the guards apply where the client
    supports them and are skipped where it does not, instead of the whole
    scraper depending on a version nobody pinned.
    """
    try:
        accepted = inspect.signature(actor_client.call).parameters
    except (TypeError, ValueError):
        return {}
    return {k: v for k, v in desired.items() if k in accepted}


def _call_actor_harvest(client: ApifyClient, run_input: Dict[str, Any]) -> List[Any]:
    actor_client = client.actor(HARVEST_ACTOR_ID)
    guards = _supported_call_kwargs(
        actor_client,
        {
            "timeout_secs": HARVEST_CALL_TIMEOUT_SECONDS,
            "wait_secs": HARVEST_CALL_TIMEOUT_SECONDS,
            "max_total_charge_usd": _harvest_charge_cap(
                int(run_input.get("maxItems") or 25)
            ),
        },
    )
    missing = {"timeout_secs", "wait_secs", "max_total_charge_usd"} - set(guards)
    if missing:
        log.warning(
            "[harvest] apify-client too old for %s — call runs unguarded; "
            "pin a newer apify-client to restore the timeout and spend cap",
            sorted(missing),
        )
    run = actor_client.call(run_input=run_input, logger=None, **guards)
    # On timeout the client returns whatever state the run is in rather than
    # raising, so an unfinished run must be treated as empty instead of having
    # its (absent or partial) dataset read as if it were complete.
    status = _run_field(run, "status", "status")
    if status and str(status).upper() not in ("SUCCEEDED",):
        log.warning(
            "[harvest] actor run did not succeed within %ss (status=%s); "
            "treating this combo as empty",
            HARVEST_CALL_TIMEOUT_SECONDS,
            status,
        )
        return []
    dataset_id = _run_field(run, "defaultDatasetId", "default_dataset_id")
    if not dataset_id:
        return []
    return client.dataset(dataset_id).list_items().items


def _try_fetch_once(
    client: ApifyClient, request_id: str, page: int, emit: LogFn
) -> Tuple[Optional[List[Dict[str, Any]]], Any]:
    """One Flow 2 fetch. Returns (leads, message).

    leads is the mapped list when the search is ready ("ok" / a `data` list),
    or None while still processing — in which case `message` holds the actor's
    progress text so the caller can detect a stall.
    """
    items = _call_actor(client, {"request_id": request_id, "page": page})
    response = _first_dict(items)
    # The actor signals readiness via `message` ("ok"), not `status`, and
    # returns the leads under `data`. While still running it returns a
    # non-"ok" message and no `data` list (no partial data to grab).
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
        return leads, message
    return None, message


def _fetch_page(
    client: ApifyClient,
    request_id: str,
    page: int,
    emit: LogFn,
    market: Optional[str] = None,
    combo_code: Optional[str] = None,
) -> Optional[List[Dict[str, Any]]]:
    # Returns the mapped leads for the page, an empty list when the page has
    # no results, or None when the actor never stopped "processing" (either it
    # exhausted MAX_PROCESSING_RETRIES, or it stalled — see STALL_LIMIT).
    def _stall_label() -> str:
        if market is not None or combo_code is not None:
            return f"[combo] market={market!r} code={combo_code!r}"
        return f"[Flow 2 fetch] page {page}"

    last_message: Any = object()  # sentinel that can't equal a real message
    stall_count = 0

    for attempt in range(MAX_PROCESSING_RETRIES):
        emit(
            f"[Flow 2 fetch] attempt {attempt + 1}/{MAX_PROCESSING_RETRIES} "
            f"input: {_dump({'request_id': request_id, 'page': page})}"
        )
        leads, message = _try_fetch_once(client, request_id, page, emit)
        if leads is not None:
            return leads

        stall_count = stall_count + 1 if message == last_message else 1
        last_message = message

        if stall_count >= STALL_LIMIT:
            # One last, longer grace window before giving up — the only way to
            # recover an almost-done search, since there are no partial results
            # in a processing response.
            emit(
                f"{_stall_label()} stalled at {message!r} for {STALL_LIMIT} "
                f"consecutive attempts; waiting {STALL_GRACE_SECONDS}s for a "
                "final grace attempt before giving up"
            )
            time.sleep(STALL_GRACE_SECONDS)
            leads, message = _try_fetch_once(client, request_id, page, emit)
            if leads is not None:
                emit(f"{_stall_label()} recovered {len(leads)} leads on the grace attempt")
                return leads

            emit(
                f"{_stall_label()} still stalled at {message!r} after the grace "
                "attempt (the actor returns no partial data mid-search); giving "
                "up on this search early"
            )
            return None

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


def _paginate_with_dedup(
    client: ApifyClient,
    request_id: str,
    cell_target: int,
    organization_id: str,
    raw_leads: List[Dict[str, Any]],
    start_page: int,
    max_page: int,
    market: str,
    combo_code: Optional[str],
    emit: LogFn,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], int, bool]:
    """Fetch pages starting at start_page, re-running the preliminary DB dedup
    after each page, until cell_target new leads are reached, page max_page has
    been fetched, or the actor signals no more results.

    raw_leads/start_page let a later retry resume an already-initialized
    search (same request_id) instead of starting over from page 1. max_page is
    an absolute page number, not a count — the main pass calls this with
    max_page=MAX_COMBO_PAGES (pages 1..3); a retry that wants its own fresh
    budget of up to MAX_COMBO_PAGES more pages passes
    max_page=last_page+MAX_COMBO_PAGES (e.g. pages 4..6), continuing the same
    request_id rather than a cumulative cap that a combo which already used
    its first budget could never satisfy.

    Returns (new_leads, updated_raw_leads, last_page_fetched, exhausted).
    exhausted=True means the actor confirmed there's nothing more to fetch (a
    short/empty page) — retrying such a combo again would be a wasted call, no
    matter how much page budget is left. This dedup only drives the pagination
    decision here; job_runner's dedup_leads on the full scored batch remains
    the authoritative check before storing.
    """
    raw = list(raw_leads)
    new_leads, _ = dedup_leads(raw, organization_id) if raw else ([], 0)
    page = start_page
    last_page = start_page - 1

    if page > max_page:
        return new_leads, raw, last_page, False  # no budget left in this pass

    while True:
        page_leads = _fetch_page(client, request_id, page, emit, market, combo_code)
        if not page_leads:
            return new_leads, raw, last_page, True  # confirmed: no more results

        raw.extend(page_leads)
        last_page = page
        new_leads, _duplicates = dedup_leads(raw, organization_id)

        if len(new_leads) >= cell_target:
            return new_leads, raw, last_page, False  # reached target

        if len(page_leads) < PAGE_SIZE:
            return new_leads, raw, last_page, True  # last available page, confirmed

        if page >= max_page:
            emit(
                f"[combo] market={market!r} code={combo_code!r} reached page "
                f"limit ({max_page}), got {len(new_leads)}/{cell_target} new leads"
            )
            return new_leads, raw, last_page, False  # budget spent, more may exist

        emit(
            f"[combo] market={market!r} code={combo_code!r} after page {page}: "
            f"{len(new_leads)} new leads (need {cell_target}), fetching page {page + 1}"
        )
        page += 1


def _scrape_combo(
    client: ApifyClient,
    combo: Dict[str, Any],
    geo_codes: List[int],
    cell_target: int,
    organization_id: str,
    market: str,
    combo_code: Optional[str],
    emit: LogFn,
) -> Dict[str, Any]:
    """Run one combo's search and return a resumable session.

    The session (request_id + accumulated raw leads + last page fetched) lets
    run_scraping request more pages for this exact combo later — reusing the
    same already-initialized search — if the run falls short of its 80%
    threshold, without paying Flow 1's 5-10 min wait a second time.
    """
    # Overfetch: ask Apify for a bit more than cell_target, so there's margin
    # left once already-scraped/already-stored profiles get filtered out by
    # the DB dedup check below.
    leads_to_fetch = int(cell_target * OVERFETCH_MULTIPLIER)
    emit(
        f"[combo] market={market!r} code={combo_code!r} "
        f"requested={cell_target} fetching={leads_to_fetch}"
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

    session: Dict[str, Any] = {
        "combo_code": combo_code,
        "market": market,
        "cell_target": cell_target,
        "request_id": None,
        "raw_leads": [],
        "leads": [],
        "last_page": 0,
        "exhausted": True,
    }

    # Flow 1 (_init_search) runs exactly once here, so its one-time 5-10 min
    # wait only ever happens once per combo. Every extra page below (and any
    # later retry) reuses this same request_id through Flow 2 (_fetch_page) —
    # Apify already has the results computed internally, so re-requesting a
    # page is a matter of seconds, not a new long wait.
    request_id = _init_search(client, init_input, emit)
    if not request_id:
        return session
    session["request_id"] = request_id

    new_leads, raw, last_page, exhausted = _paginate_with_dedup(
        client,
        request_id,
        cell_target,
        organization_id,
        [],
        1,
        MAX_COMBO_PAGES,
        market,
        combo_code,
        emit,
    )
    session["raw_leads"] = raw
    session["last_page"] = last_page
    session["exhausted"] = exhausted
    # No per-cell trim here: a combo contributes every new lead its own
    # preliminary dedup found, even if that's more than cell_target. The final
    # ceiling on total_leads is enforced once, globally, in job_runner after
    # ICP scoring — keeping the highest-scoring leads across the whole run
    # instead of arbitrarily discarding an over-performing combo's leads here.
    session["leads"] = new_leads
    return session


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


def _detect_market_from_location(
    location: Optional[str], candidate_markets: List[Dict[str, Any]]
) -> Optional[str]:
    """Best-effort country match from a lead's free-text `location`.

    The actor never returns a lead's country structured — only this free-text
    field (e.g. "Taipei, Taiwan", "Ho Chi Minh City, Vietnam"). Matching is a
    case-insensitive substring check against the real country names of THIS
    run's own resolved markets — not the whole markets table — since a lead
    can only be from one of the countries actually searched (geo_codes already
    restricted results to them), which also keeps the match space small enough
    to avoid unrelated-country false positives.

    Returns the matched market's exact name, or None if location is empty or
    nothing matched — callers fall back to language_market in that case.
    """
    if not location:
        return None
    location_lower = location.lower()
    for row in candidate_markets:
        name = row.get("name") or ""
        if name and name.lower() in location_lower:
            return name
    return None


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

    # Resolve every requested market up front, so an unknown market fails the
    # run immediately with a clear message — before spending a single Apify
    # call — instead of silently scraping with no geo filter, which is exactly
    # how the "Spain in LATAM" bug went unnoticed.
    resolved_markets = resolve_markets(markets)

    # Countries within one region are combined into a single geo_codes array —
    # the actor accepts multiple geo_codes in one input, so ["Argentina",
    # "Chile", "Colombia"] becomes one search per combo covering all three, not
    # three separate cells. Cells stay one-per-combo regardless of how many
    # countries are selected, so a 5-country run takes no longer than a
    # 1-country run.
    regions = {row["region"] for row in resolved_markets}
    if len(regions) > 1:
        raise MixedRegionMarketsError(
            f"Markets {markets!r} span multiple regions ({sorted(regions)}); "
            "a single run's markets must all belong to one region."
        )

    combined_geo_codes = sorted({int(row["geo_code"]) for row in resolved_markets})

    # The actor doesn't return which specific country a lead came from (only
    # a free-text location), so a lead can't be labeled with its real country
    # once several are combined. With one market there's no ambiguity, so keep
    # storing that market's own name exactly as before. With several, store
    # the region's display name instead of guessing.
    if len(resolved_markets) == 1:
        market_label = markets[0]
        language_market = markets[0]
    else:
        market_label = region_label(resolved_markets[0]["region"])
        # The actor still doesn't return a lead's real country, but its
        # free-text `location` is a best-effort signal — _absorb below tries
        # to match it against this run's own countries per lead. The
        # first-listed market's language remains the fallback for whatever
        # doesn't match (ambiguous/empty location, or no match at all) — not
        # a guarantee, but no longer the only option.
        language_market = markets[0]
        emit(
            f"[market] combined {markets!r} into region {market_label!r}, "
            f"geo_codes={combined_geo_codes} "
            f"(per-lead location will be checked; falling back to "
            f"{language_market!r} when it can't be determined)"
        )

    for row in resolved_markets:
        emit(f"[market] {row['name']!r} -> geo_code={row['geo_code']}")

    cells = list(combos or [{}])
    remaining = total_leads
    cell_sessions: List[Dict[str, Any]] = []

    # Fixed for the whole run — the original fair share if every cell had
    # performed equally, not recomputed against cells_left like cell_target is.
    fair_share_cap = math.ceil(
        (total_leads / len(cells)) * MAX_CELL_TARGET_MULTIPLIER
    )

    # Only meaningful when several countries were combined — with one market
    # language_market is already exactly right, so there's nothing to resolve
    # per lead and no point running the heuristic or counting its hits.
    resolve_language_per_lead = len(resolved_markets) > 1
    location_matched_count = 0
    location_fallback_count = 0

    def _absorb(leads: List[Dict[str, Any]], market: str, combo_code: Optional[str]) -> int:
        # Shared by the main pass and the retry pass below. Only genuinely new
        # (not-yet-seen-this-run) leads count — but deliberately NO total_leads
        # cap here: a combo keeps every new lead its own preliminary dedup
        # found, even past its own target. run_scraping's returned total can
        # exceed total_leads; job_runner enforces that ceiling once, globally,
        # after ICP scoring — keeping the highest-scoring leads across the
        # whole run rather than discarding an over-performing combo's leads
        # before their quality can even be compared.
        nonlocal location_matched_count, location_fallback_count
        added = 0
        for lead in leads:
            if not isinstance(lead, dict):
                continue
            linkedin_url = lead.get("linkedin_url")
            if not linkedin_url or linkedin_url in seen_linkedin_urls:
                continue
            seen_linkedin_urls.add(linkedin_url)
            lead["market"] = market

            if resolve_language_per_lead:
                detected = _detect_market_from_location(
                    lead.get("location"), resolved_markets
                )
                if detected:
                    lead["language_market"] = detected
                    location_matched_count += 1
                else:
                    lead["language_market"] = language_market
                    location_fallback_count += 1
            else:
                lead["language_market"] = language_market

            lead["combo"] = combo_code
            all_leads.append(lead)
            added += 1
        return added

    for index, combo in enumerate(cells):
        # No early exit once remaining <= 0: an earlier combo overshooting its
        # own target (no longer trimmed — see Change 1) can drive remaining
        # negative, and stopping here would skip every later combo outright
        # rather than let it run too. That would undermine the point of the
        # global ICP trim below — combos never attempted can't contribute
        # leads, however high-scoring they might have been. Once remaining
        # is <= 0 the ceil-division formula naturally floors cell_target at
        # 1 (a minimal ask), so every combo still gets a chance without
        # aggressively over-fetching from ones that are already "satisfied".
        cells_left = len(cells) - index
        cell_target = max(-(-remaining // cells_left), 1)  # ceil division
        combo_code = combo.get("code") if isinstance(combo, dict) else None

        if cell_target > fair_share_cap:
            emit(
                f"[combo] market={market_label!r} code={combo_code!r} target capped "
                f"at {fair_share_cap} (fair share ceiling), some leads may fall "
                "short of total_leads requested"
            )
            cell_target = fair_share_cap

        emit(
            f"[combo] market={market_label!r} code={combo_code!r} "
            f"target={cell_target} remaining={remaining}"
        )
        # Same session contract either way — see _scrape_combo_harvest. The
        # incumbent needs geo CODES, HarvestAPI needs country NAMES, and
        # `markets` already holds the names, so neither path converts.
        if _use_harvest():
            session = _scrape_combo_harvest(
                client,
                combo,
                markets,
                cell_target,
                organization_id,
                market_label,
                combo_code,
                emit,
            )
        else:
            session = _scrape_combo(
                client,
                combo,
                combined_geo_codes,
                cell_target,
                organization_id,
                market_label,
                combo_code,
                emit,
            )
        cell_sessions.append(session)

        added = _absorb(session["leads"], market_label, combo_code)
        remaining -= added
        emit(
            f"[combo] market={market_label!r} code={combo_code!r} "
            f"added={added} remaining={remaining}"
        )

    # If the run fell short, spend one bounded extra round fetching more pages
    # for the combos furthest from their own target — reusing each one's
    # already-initialized search (same request_id), never re-running Flow 1.
    threshold_80 = math.ceil(total_leads * 0.8)
    total_so_far = len(all_leads)

    if total_so_far < threshold_80:
        emit(
            f"[retry] {total_so_far}/{total_leads} leads is below the 80% "
            f"threshold ({threshold_80}); requesting additional pages for the "
            "combo(s) furthest from their target"
        )

        # Skip combos the actor already confirmed have nothing more (a
        # short/empty page) — only ones that stopped because they hit their
        # page budget (there might be more) are worth another request.
        # A HarvestAPI session has no request_id — it is one synchronous call,
        # so there is nothing to resume — but it is still retryable by asking
        # for the next page. Gating on request_id alone (as this did) would
        # have silently excluded every harvest combo from the shortfall retry,
        # quietly capping those runs at whatever the first page returned.
        retryable = [
            s
            for s in cell_sessions
            if not s["exhausted"]
            and len(s["leads"]) < s["cell_target"]
            and (s["request_id"] or s.get("combo"))
        ]
        retryable.sort(key=lambda s: s["cell_target"] - len(s["leads"]), reverse=True)

        for session in retryable:
            if len(all_leads) >= total_leads:
                break

            # Fresh budget of up to MAX_COMBO_PAGES more pages for this retry,
            # continuing the same request_id from where the main pass left off
            # — not a cumulative total, since a combo that already used its
            # first budget in the main pass is precisely the case worth
            # retrying here.
            if session["request_id"]:
                new_leads, raw, last_page, exhausted = _paginate_with_dedup(
                    client,
                    session["request_id"],
                    session["cell_target"],
                    organization_id,
                    session["raw_leads"],
                    session["last_page"] + 1,
                    session["last_page"] + MAX_COMBO_PAGES,
                    session["market"],
                    session["combo_code"],
                    emit,
                )
            else:
                new_leads, raw, last_page, exhausted = _retry_harvest_page(
                    client,
                    session,
                    organization_id,
                    emit,
                )
            session["raw_leads"] = raw
            session["last_page"] = last_page
            session["exhausted"] = exhausted
            session["leads"] = new_leads  # no per-cell trim — see _scrape_combo

            added = _absorb(session["leads"], session["market"], session["combo_code"])
            emit(
                f"[retry] market={session['market']!r} code={session['combo_code']!r} "
                f"added {added} more lead(s); running total {len(all_leads)}/{total_leads}"
            )

        total_so_far = len(all_leads)
        percent = round(total_so_far / total_leads * 100)
        if total_so_far >= threshold_80:
            emit(f"Reached {total_so_far}/{total_leads} ({percent}%) after retry — proceeding")
        else:
            emit(
                f"Run finished below 80% threshold: {total_so_far} of {total_leads} "
                f"requested ({percent}%) after retry attempt"
            )

    if resolve_language_per_lead:
        emit(
            f"Language resolution: {location_matched_count} leads resolved by "
            f"location, {location_fallback_count} used region fallback"
        )

    return all_leads
