"""ICP scoring for Bridge (partnership) candidates.

WHY THIS IS NOT `icp_scorer.py`
------------------------------
The sales scorer awards 40 of its 100 points as flat constants:

    _score_company_size      -> 15, always
    _score_industry          -> 10, always
    _score_linkedin_activity -> 15, always

That is defensible there, and the comments say why: the Apify actor filters on
company headcount and on `posted_on_linkedin` *in its input*, so every lead it
returns already satisfies those conditions — the response just doesn't echo
them back. The constant is standing in for a filter that really did run.

For Bridge only one of those three survives. A seed list does filter company
headcount, so the 15 is still earned. Nothing filters LinkedIn activity, so
giving a partnership candidate those points would be crediting them for a
check that never happened.

What Bridge has instead is something the sales pipeline doesn't: **the admin
chose the target companies by hand.** Every candidate works at a company
somebody deliberately typed into a seed list. That is far better evidence of
fit than the sales scorer's flat 10-point "industry" baseline, which measures
nothing at all.

So the 25 points the sales scorer hands out for industry + LinkedIn activity
are re-earned here as a single 25 for deliberate company targeting, and the
job title is weighted higher because with fewer components it carries more of
the discrimination.

The thresholds are deliberately shared with the sales scorer (70 / 50) and the
totals land on the same four rungs, so a Bridge lead and a scraper lead sitting
side by side on the same Kanban mean the same thing:

    Bridge   40 COLD   60 WARM   80 HOT   100 HOT
    Sales    40 COLD   60 WARM   70 HOT    90 HOT

Field availability, measured against production on 05/08/2026 (620 candidates):
`title` 620/620, `about` 424/620 (68%). Both components fire often enough to
discriminate; `about` being absent simply costs those 20 points, it does not
break the scale.

⚠️  INHERITS TWO KNOWN BUGS FROM icp_scorer.py — see the warning at the top of
that file. In short: PRIORITY_TITLES is matched as a substring, so every
"Director" scores as a CTO (dire-CTO-r) while "Chief Technology Officer"
scores nothing; and AI_SIGNAL_KEYWORDS contains the bare string "ai", which
matches av-AI-lable, em-AI-l and most other English text, so those 20 points
land on nearly every candidate with a bio.

They are imported rather than copied specifically so that one fix repairs
both pipelines. Until that fix lands, treat a Bridge score the same way you'd
treat a sales score: the ordering is roughly right for obvious C-level
abbreviations and meaningless everywhere else. This is still a strict
improvement on what it replaces, which was a hardcoded 'Cold' for every
partnership contact and no score at all.
"""

from typing import Any, Dict, List

# Shared with the sales scorer on purpose — a partnership contact and a sales
# lead with the same title should not disagree about whether that title is
# senior. If these ever need to diverge, that is a product decision worth
# writing down, not a drift to discover later.
from .icp_scorer import (
    PRIORITY_TITLES,
    AI_SIGNAL_KEYWORDS,
    HOT_THRESHOLD,
    WARM_THRESHOLD,
)

JOB_TITLE_MAX = 40
AI_SIGNALS_MAX = 20
TARGET_COMPANY_MAX = 25
COMPANY_SIZE_MAX = 15


def _score_job_title(candidate: Dict[str, Any]) -> int:
    # `title` is what bridge_job_runner writes; `job_title` is the raw actor
    # field, accepted too so this can be called before or after that mapping.
    title = (candidate.get("title") or candidate.get("job_title") or "").lower()
    if not title:
        return 0
    for priority_title in PRIORITY_TITLES:
        if priority_title in title:
            return JOB_TITLE_MAX
    return 0


def _score_ai_signals(candidate: Dict[str, Any]) -> int:
    text = (candidate.get("about") or "").lower()
    if not text:
        return 0
    for keyword in AI_SIGNAL_KEYWORDS:
        if keyword in text:
            return AI_SIGNALS_MAX
    return 0


def _score_target_company(candidate: Dict[str, Any]) -> int:
    # Every candidate reaching this point came from a company the admin named
    # in a seed list. Conditional on the company actually being identified,
    # rather than unconditional, so a row that lost its company association
    # doesn't collect points for targeting that can't be traced.
    has_company = bool(candidate.get("company_name") or candidate.get("company"))
    return TARGET_COMPANY_MAX if has_company else 0


def _score_company_size(candidate: Dict[str, Any]) -> int:
    # Same reasoning as the sales scorer: the seed list filters on
    # company_headcounts at input, so every candidate returned already matches
    # an accepted size — the actor just doesn't echo it back.
    return COMPANY_SIZE_MAX


def _classify(score: int) -> str:
    if score >= HOT_THRESHOLD:
        return "HOT"
    if score >= WARM_THRESHOLD:
        return "WARM"
    return "COLD"


def score_bridge_candidate(candidate: Dict[str, Any]) -> Dict[str, Any]:
    """Attach icp_score / icp_tier / icp_breakdown to one candidate, in place."""
    job_title_score = _score_job_title(candidate)
    ai_signals_score = _score_ai_signals(candidate)
    target_company_score = _score_target_company(candidate)
    company_size_score = _score_company_size(candidate)

    total_score = (
        job_title_score
        + ai_signals_score
        + target_company_score
        + company_size_score
    )

    candidate["icp_score"] = total_score
    candidate["icp_tier"] = _classify(total_score)
    candidate["icp_breakdown"] = {
        "job_title": job_title_score,
        "ai_signals": ai_signals_score,
        "target_company": target_company_score,
        "company_size": company_size_score,
    }
    return candidate


def score_bridge_candidates(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Score a batch. Unlike the sales scorer this does NOT reorder the list —
    Bridge groups candidates by company for review, and sorting by score would
    scatter each company's people across the page."""
    return [score_bridge_candidate(c) for c in candidates]
