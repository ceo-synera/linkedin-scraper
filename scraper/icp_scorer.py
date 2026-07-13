from typing import Any, Dict, List, Optional

COMPANY_SIZE_MAX = 15
LINKEDIN_ACTIVITY_MAX = 15


def _best_weight_match(text: str, keywords: List[Dict[str, Any]]) -> Optional[int]:
    matched_weights = [
        row["weight"] for row in keywords if str(row["keyword"]).lower() in text
    ]
    return max(matched_weights) if matched_weights else None


def _score_job_title(lead: Dict[str, Any], icp_keywords: Dict[str, List[Dict[str, Any]]]) -> int:
    # `job_title` is the real field returned by the actor.
    title = (lead.get("job_title") or "").lower()
    if not title:
        return 0

    decision_match = _best_weight_match(title, icp_keywords.get("decision_title", []))
    if decision_match is not None:
        return decision_match

    influencer_match = _best_weight_match(title, icp_keywords.get("influencer_title", []))
    if influencer_match is not None:
        return influencer_match

    return 0


def _score_company_size(lead: Dict[str, Any]) -> int:
    # The Apify actor filters by company_headcounts on input, so every
    # returned lead already matches an accepted size — it just isn't
    # echoed back in the response.
    return COMPANY_SIZE_MAX


def _score_industry(lead: Dict[str, Any], icp_keywords: Dict[str, List[Dict[str, Any]]]) -> int:
    text = (lead.get("about") or "").lower()
    if not text:
        return 0
    match = _best_weight_match(text, icp_keywords.get("industry", []))
    return match if match is not None else 0


def _score_linkedin_activity(lead: Dict[str, Any]) -> int:
    # The actor filters by posted_on_linkedin=true on input, so every
    # returned lead already satisfies this — it just isn't echoed back.
    return LINKEDIN_ACTIVITY_MAX


def _score_signal(lead: Dict[str, Any], icp_keywords: Dict[str, List[Dict[str, Any]]]) -> int:
    # Generic buying-signal dimension (category "ai_signal" in org_icp_keywords).
    # Every distinct matching keyword contributes its own weight, so multiple
    # hits count for more than one hit.
    text = (lead.get("about") or "").lower()
    if not text:
        return 0
    return sum(
        row["weight"]
        for row in icp_keywords.get("ai_signal", [])
        if str(row["keyword"]).lower() in text
    )


def score_lead(
    lead: Dict[str, Any], icp_keywords: Dict[str, List[Dict[str, Any]]]
) -> Dict[str, Any]:
    job_title_score = _score_job_title(lead, icp_keywords)
    company_size_score = _score_company_size(lead)
    industry_score = _score_industry(lead, icp_keywords)
    linkedin_activity_score = _score_linkedin_activity(lead)
    signal_score = _score_signal(lead, icp_keywords)

    total_score = (
        job_title_score
        + company_size_score
        + industry_score
        + linkedin_activity_score
        + signal_score
    )
    total_score = max(0, min(100, total_score))

    lead["icp_score"] = total_score
    lead["icp_breakdown"] = {
        "job_title": job_title_score,
        "company_size": company_size_score,
        "industry": industry_score,
        "linkedin_activity": linkedin_activity_score,
        "signal": signal_score,
    }
    return lead


def score_leads(
    leads: List[Dict[str, Any]], icp_keywords: Dict[str, List[Dict[str, Any]]]
) -> List[Dict[str, Any]]:
    scored = [score_lead(lead, icp_keywords) for lead in leads]
    scored.sort(key=lambda lead: lead["icp_score"], reverse=True)
    return scored
