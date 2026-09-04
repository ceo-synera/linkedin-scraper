"""ICP scoring for Bridge (partnership) candidates.

WHY THIS IS NOT `icp_scorer.py`
-------------------------------
Bridge knows different things about a candidate than the sales pipeline knows
about a lead, so it scores different things:

  * **It has no combos.** Partnership titles are a fixed list
    (`BRIDGE_TITLE_KEYWORDS` in `apify_scraper.py`), passed in by the caller
    rather than imported here, so the day Bridge gets per-org partnership
    titles this file needs no change. That fixed list is a known
    multi-tenancy gap — see MULTI_TENANCY.md item 4.
  * **It has no `company_context` on the run.** `BridgeRunRequest` carries no
    Anthropic fields at all (discovery never generates messages), so there is
    no buying-signal component to compute.
  * **It has something the sales pipeline doesn't: the admin chose the target
    companies by hand.** Every candidate works at a company somebody
    deliberately typed into a seed list. That is real evidence of fit and it is
    worth more than any proxy the sales scorer has.

So: 45 for the partnership title, 20 for seniority, 35 for belonging to a
deliberately targeted company.

WHAT CHANGED
------------
This used to import `PRIORITY_TITLES` and `AI_SIGNAL_KEYWORDS` from the sales
scorer and match them as substrings, which meant it inherited both of that
file's bugs: every "Director" scored as a CTO (dire-CTO-r), and the bare string
"ai" matched av-AI-lable in most English bios, so those 20 points landed on
nearly every candidate. It also awarded a flat 15 for company size — a constant,
and one Bridge could not even justify the way the sales scorer did, since a seed
list may set no headcount filter at all.

Both hardcoded lists are gone. Matching now goes through `scraper.text_match`,
which is phrase-based and script-aware, so a Traditional Chinese partnership
title matches as a substring while "Head of Partnerships" matches as whole
words.

The thresholds stay shared with the sales scorer (70 / 50) on purpose: a Bridge
candidate and a scraped lead sitting side by side on the same board should mean
the same thing when they say HOT.
"""

from typing import Any, Dict, List, Optional, Sequence

from .icp_scorer import (
    HOT_THRESHOLD,
    WARM_THRESHOLD,
    expand_title_variants,
    score_seniority,
)
from .text_match import best_phrase_match

JOB_TITLE_MAX = 45
SENIORITY_MAX = 20
TARGET_COMPANY_MAX = 35


def _candidate_title(candidate: Dict[str, Any]) -> str:
    # `title` is what bridge_job_runner writes; `job_title` is the raw actor
    # field, accepted too so this can be called before or after that mapping.
    return candidate.get("title") or candidate.get("job_title") or ""


def _score_job_title(candidate: Dict[str, Any], target_titles: Sequence[str]) -> int:
    if not target_titles:
        return 0
    match = best_phrase_match(_candidate_title(candidate), target_titles)
    return int(round(JOB_TITLE_MAX * match))


def _score_target_company(candidate: Dict[str, Any]) -> int:
    # Conditional on the company actually being identified, rather than
    # unconditional: a row that lost its company association can't collect
    # points for targeting that cannot be traced.
    has_company = bool(candidate.get("company_name") or candidate.get("company"))
    return TARGET_COMPANY_MAX if has_company else 0


def _classify(score: int) -> str:
    if score >= HOT_THRESHOLD:
        return "HOT"
    if score >= WARM_THRESHOLD:
        return "WARM"
    return "COLD"


def expand_target_titles(titles: Sequence[str]) -> List[str]:
    """The partnership titles plus their common spellings, deduped.

    Worth doing once per run rather than once per candidate: a Bridge run scores
    hundreds of candidates against the same list.
    """
    out: List[str] = []
    seen = set()
    for title in titles or []:
        for variant in expand_title_variants(title):
            if variant not in seen:
                seen.add(variant)
                out.append(variant)
    return out


def score_bridge_candidate(
    candidate: Dict[str, Any], target_titles: Optional[Sequence[str]] = None
) -> Dict[str, Any]:
    """Attach `icp_score` / `icp_tier` / `icp_breakdown` to one candidate, in place.

    `target_titles` should already be expanded (see `expand_target_titles`) when
    scoring a batch; passing the raw list still works, it just does the
    expansion work per candidate.
    """
    titles = list(target_titles or [])

    job_title_score = _score_job_title(candidate, titles)
    seniority_score = score_seniority(_candidate_title(candidate))
    target_company_score = _score_target_company(candidate)

    total_score = job_title_score + seniority_score + target_company_score
    total_score = max(0, min(100, total_score))

    candidate["icp_score"] = total_score
    candidate["icp_tier"] = _classify(total_score)
    candidate["icp_breakdown"] = {
        "job_title": job_title_score,
        "seniority": seniority_score,
        "target_company": target_company_score,
    }
    return candidate


def score_bridge_candidates(
    candidates: List[Dict[str, Any]], target_titles: Optional[Sequence[str]] = None
) -> List[Dict[str, Any]]:
    """Score a batch. Unlike the sales scorer this does NOT reorder the list —
    Bridge groups candidates by company for review, and sorting by score would
    scatter each company's people across the page."""
    expanded = expand_target_titles(target_titles or [])
    return [score_bridge_candidate(c, expanded) for c in candidates]
