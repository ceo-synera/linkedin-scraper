"""ICP scoring for scraped sales leads.

⚠️  KNOWN BROKEN — MEASURED 05/08/2026, DELIBERATELY NOT FIXED YET
==================================================================
Read this before trusting any icp_score or HOT/WARM/COLD tier in the product.
The fix was deferred on purpose: correcting it re-scores every existing lead,
which changes temperatures SDRs already know, so it needs to be a scheduled
change rather than a drive-by.

1. THREE OF THE FIVE COMPONENTS ARE CONSTANTS (40 of 100 points)
   `_score_company_size`, `_score_industry` and `_score_linkedin_activity`
   return a fixed number for every lead. Two are defended in their comments by
   the actor filtering on those criteria at input; `_score_industry` is openly
   a placeholder. Whatever the justification, they cannot discriminate between
   two leads.

2. `ai` IS MATCHED AS A SUBSTRING (20 more points, effectively constant)
   AI_SIGNAL_KEYWORDS contains the bare string "ai", tested with `in`. It
   matches av-AI-lable, m-AI-ntain, em-AI-l, det-AI-l, ch-AI-rman, ret-AIl,
   T-AI-pei — essentially any non-empty English bio scores the full 20. This
   is not a signal, it is a fourth constant.

3. PRIORITY_TITLES MATCHES SUBSTRINGS TOO, AND FAILS BOTH WAYS (the last 30)
   False positives — score as C-level:
       "Director of Marketing", "Sales Director", "Art Director"  -> dire(CTO)r
       "Recruiting Coordinator", "Social Media Coordinator"       -> (COO)rdinator
   False negatives — score zero while being exactly the target:
       "Chief Technology Officer", "Chief Executive Officer",
       "Chief Information Officer", "VP of Engineering",
       "Head of Product", "Head of Engineering"

   In other words every Director and every Coordinator in the database has
   been scored as if they were a CTO, and every C-level title written out in
   full — which is how many people write it on LinkedIn — scored nothing.

NET EFFECT: 85 of the 100 points measure nothing, and the remaining 30 are
wrong in both directions. In practice almost every lead with a bio lands on
60 (WARM), rising to 90 (HOT) only when the title happens to contain "cto" or
"coo" as a substring.

4. IT IS ALSO NOT MULTI-TENANT, WHICH IS A SEPARATE PROBLEM
   These lists encode ONE ideal customer profile — technical decision-makers
   who mention AI. Every organisation using this product sells something
   different; one selling logistics software wants "Head of Supply Chain" and
   mentions of "warehouse". The product ALREADY accepts this: `org_combos`
   lets each org choose which titles to search for. So the scraper searches
   for the titles the customer chose and then scores the results against a
   completely different hardcoded profile. The two halves don't talk.

   Fixing (1)–(3) alone yields a scorer that works correctly while measuring
   the wrong profile for every customer but Insight Software itself.

WHEN FIXING: word-boundary regex instead of `in`, add the spelled-out title
variants, drop or replace the components that measure nothing, and source the
target titles from the org's own combos. `bridge_icp_scorer.py` imports
PRIORITY_TITLES and AI_SIGNAL_KEYWORDS from here precisely so one fix covers
both pipelines.
"""

from typing import Any, Dict, List

PRIORITY_TITLES = [
    "cto",
    "cio",
    "ceo",
    "founder",
    "co-founder",
    "vp engineering",
    "marketing director",
    "cdo",
    "coo",
    "product manager",
    "engineering manager",
]

AI_SIGNAL_KEYWORDS = [
    "chatgpt",
    "openai",
    "claude",
    "ai",
    "llm",
    "copilot",
]

JOB_TITLE_MAX = 30
COMPANY_SIZE_MAX = 15
INDUSTRY_BASE_SCORE = 10
LINKEDIN_ACTIVITY_MAX = 15
AI_SIGNALS_MAX = 20

HOT_THRESHOLD = 70
WARM_THRESHOLD = 50


def _score_job_title(lead: Dict[str, Any]) -> int:
    # `job_title` is the real field returned by the actor.
    title = (lead.get("job_title") or "").lower()
    if not title:
        return 0
    for priority_title in PRIORITY_TITLES:
        if priority_title in title:
            return JOB_TITLE_MAX
    return 0


def _score_company_size(lead: Dict[str, Any]) -> int:
    # The Apify actor filters by company_headcounts on input, so every
    # returned lead already matches an accepted size — it just isn't
    # echoed back in the response.
    return COMPANY_SIZE_MAX


def _score_industry(lead: Dict[str, Any]) -> int:
    # The actor doesn't return industry at all. Baseline score until
    # this is enriched from another source.
    return INDUSTRY_BASE_SCORE


def _score_linkedin_activity(lead: Dict[str, Any]) -> int:
    # The actor filters by posted_on_linkedin=true on input, so every
    # returned lead already satisfies this — it just isn't echoed back.
    return LINKEDIN_ACTIVITY_MAX


def _score_ai_signals(lead: Dict[str, Any]) -> int:
    text = (lead.get("about") or "").lower()
    if not text:
        return 0
    for keyword in AI_SIGNAL_KEYWORDS:
        if keyword in text:
            return AI_SIGNALS_MAX
    return 0


def _classify(score: int) -> str:
    if score >= HOT_THRESHOLD:
        return "HOT"
    if score >= WARM_THRESHOLD:
        return "WARM"
    return "COLD"


def score_lead(lead: Dict[str, Any]) -> Dict[str, Any]:
    job_title_score = _score_job_title(lead)
    company_size_score = _score_company_size(lead)
    industry_score = _score_industry(lead)
    linkedin_activity_score = _score_linkedin_activity(lead)
    ai_signals_score = _score_ai_signals(lead)

    total_score = (
        job_title_score
        + company_size_score
        + industry_score
        + linkedin_activity_score
        + ai_signals_score
    )

    lead["icp_score"] = total_score
    lead["icp_breakdown"] = {
        "job_title": job_title_score,
        "company_size": company_size_score,
        "industry": industry_score,
        "linkedin_activity": linkedin_activity_score,
        "ai_signals": ai_signals_score,
    }
    lead["icp_tier"] = _classify(total_score)
    return lead


def score_leads(leads: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    scored = [score_lead(lead) for lead in leads]
    scored.sort(key=lambda lead: lead["icp_score"], reverse=True)
    return scored
