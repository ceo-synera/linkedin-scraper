import json
import re
from typing import Any, Dict, List, Optional

import anthropic

from api.models import SenderProfile

CUSTOM1_MAX_CHARS = 300
CUSTOM2_MAX_CHARS = 500

DEFAULT_LANGUAGE = "en"

# Default outreach language per market, used when there's no sender profile
# (Basic) or the profile language is the default.
MARKET_LANGUAGE = {
    "taiwan": "zh",
    "latam": "es",
    "vietnam": "vi",
    "global": "en",
}

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _resolve_language(
    language: str, market: Optional[str], sender_profile: Optional[SenderProfile]
) -> str:
    # A profile with an explicitly non-default language wins. Otherwise (Basic,
    # or a profile still on the default language) fall back to the market's
    # language instead of always defaulting to English.
    if sender_profile is not None and language and language != DEFAULT_LANGUAGE:
        return language
    return MARKET_LANGUAGE.get((market or "").lower(), DEFAULT_LANGUAGE)


def _build_sender_context(plan: str, sender_profile: Optional[SenderProfile]) -> str:
    if plan == "basic" or sender_profile is None:
        return "Write as a generic representative of the company. Do not use any personal name or signature."

    lines = [
        f"You are writing on behalf of {sender_profile.display_name}, {sender_profile.title} at {sender_profile.company}.",
    ]
    if sender_profile.years_experience is not None:
        lines.append(f"They have {sender_profile.years_experience} years of experience.")
    if sender_profile.seniority:
        lines.append(f"Seniority level: {sender_profile.seniority}.")
    if sender_profile.expertise_area:
        lines.append(f"Area of expertise: {sender_profile.expertise_area}.")
    if sender_profile.style_hint:
        lines.append(f"Style guidance: {sender_profile.style_hint}.")
    lines.append(
        "Ground the message in this real context so it reads as a credible, personal outreach from this person."
    )
    return "\n".join(lines)


def _build_prompt(
    lead: Dict[str, Any], plan: str, sender_profile: Optional[SenderProfile], language: str
) -> str:
    sender_context = _build_sender_context(plan, sender_profile)
    lead_name = lead.get("full_name") or lead.get("name") or "the prospect"
    lead_title = lead.get("title") or lead.get("job_title") or ""
    lead_company = lead.get("company") or ""

    return f"""{sender_context}

Write two LinkedIn outreach messages in {language} for this prospect:
- Name: {lead_name}
- Title: {lead_title}
- Company: {lead_company}

1. custom1: a LinkedIn connection request note, maximum {CUSTOM1_MAX_CHARS} characters.
2. custom2: a follow-up message sent after the connection is accepted, maximum {CUSTOM2_MAX_CHARS} characters.

Respond with ONLY a JSON object in this exact shape, no markdown fences, no extra text:
{{"custom1": "...", "custom2": "..."}}"""


def _parse_response(text: str) -> Dict[str, str]:
    match = _JSON_BLOCK_RE.search(text)
    if not match:
        raise ValueError(f"No JSON object found in Claude response: {text}")
    parsed = json.loads(match.group(0))
    return {
        "custom1": (parsed.get("custom1") or "")[:CUSTOM1_MAX_CHARS],
        "custom2": (parsed.get("custom2") or "")[:CUSTOM2_MAX_CHARS],
    }


def generate_messages_for_batch(
    leads: List[Dict[str, Any]],
    anthropic_key: str,
    plan: str,
    sender_profile: Optional[SenderProfile],
    language: str,
    anthropic_base_url: str,
    anthropic_model: str,
    market: Optional[str] = None,
) -> List[Dict[str, Any]]:
    # The Anthropic SDK appends /v1 itself, so a base_url that already ends in
    # /v1 (e.g. https://api.aitokenking.com.tw/api/v1) would produce /v1/v1.
    # Strip a trailing /v1 before handing it to the client.
    base_url = anthropic_base_url.rstrip("/")
    if base_url.endswith("/v1"):
        base_url = base_url[:-3]

    client = anthropic.Anthropic(
        api_key=anthropic_key,
        base_url=base_url,
    )

    for lead in leads:
        # Resolve language per lead: a batch can span markets when an SDR
        # covers several. Fall back to the batch-level market parameter.
        lead_market = lead.get("market") or market
        effective_language = _resolve_language(language, lead_market, sender_profile)
        prompt = _build_prompt(lead, plan, sender_profile, effective_language)
        response = client.messages.create(
            model=anthropic_model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
        messages = _parse_response(text)
        lead["custom1"] = messages["custom1"]
        lead["custom2"] = messages["custom2"]

    return leads
