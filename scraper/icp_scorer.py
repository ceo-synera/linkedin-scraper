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

TOP_COMPANY_SIZES = {"11-50", "51-200"}

TOP_INDUSTRIES = [
    "computer software",
    "internet",
    "it services",
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
INDUSTRY_MAX = 20
LINKEDIN_ACTIVITY_MAX = 15
AI_SIGNALS_MAX = 20

HOT_THRESHOLD = 70
WARM_THRESHOLD = 50


def _score_job_title(lead: Dict[str, Any]) -> int:
    title = (lead.get("title") or lead.get("job_title") or "").lower()
    if not title:
        return 0
    for priority_title in PRIORITY_TITLES:
        if priority_title in title:
            return JOB_TITLE_MAX
    return 0


def _score_company_size(lead: Dict[str, Any]) -> int:
    size = (lead.get("company_size") or "").strip()
    if size in TOP_COMPANY_SIZES:
        return COMPANY_SIZE_MAX
    return 0


def _score_industry(lead: Dict[str, Any]) -> int:
    industry = (lead.get("industry") or "").lower()
    if not industry:
        return 0
    for top_industry in TOP_INDUSTRIES:
        if top_industry in industry:
            return INDUSTRY_MAX
    return 0


def _score_linkedin_activity(lead: Dict[str, Any]) -> int:
    if lead.get("posted_on_linkedin"):
        return LINKEDIN_ACTIVITY_MAX
    return 0


def _score_ai_signals(lead: Dict[str, Any]) -> int:
    text = " ".join(
        [
            (lead.get("about") or lead.get("bio") or ""),
            (lead.get("headline") or ""),
        ]
    ).lower()
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
