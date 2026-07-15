import json
import re
from typing import Any, Dict, List, Optional, Tuple

import anthropic

from api.models import SenderProfile

# Fallbacks only — a sender profile's own connection_note_max_chars /
# followup_max_chars win when set (see _resolve_char_limits).
DEFAULT_CUSTOM1_MAX_CHARS = 300
DEFAULT_CUSTOM2_MAX_CHARS = 500

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


def _resolve_char_limits(sender_profile: Optional[SenderProfile]) -> Tuple[int, int]:
    custom1_max = DEFAULT_CUSTOM1_MAX_CHARS
    custom2_max = DEFAULT_CUSTOM2_MAX_CHARS
    if sender_profile is not None:
        if sender_profile.connection_note_max_chars:
            custom1_max = sender_profile.connection_note_max_chars
        if sender_profile.followup_max_chars:
            custom2_max = sender_profile.followup_max_chars
    return custom1_max, custom2_max


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


def _build_product_context(product_description: Optional[str]) -> str:
    # The org may not have filled this in yet — omit the section entirely
    # rather than error or insert a fake-looking placeholder.
    if not product_description:
        return ""
    return f"\nWhat this company actually sells: {product_description}\n"


def _build_prompt(
    lead: Dict[str, Any],
    plan: str,
    sender_profile: Optional[SenderProfile],
    language: str,
    product_description: Optional[str],
    custom1_max: int,
    custom2_max: int,
) -> str:
    sender_context = _build_sender_context(plan, sender_profile)
    product_context = _build_product_context(product_description)
    lead_name = lead.get("full_name") or lead.get("name") or "the prospect"
    lead_title = lead.get("title") or lead.get("job_title") or ""
    lead_company = lead.get("company") or ""

    return f"""{sender_context}
{product_context}
Write two LinkedIn outreach messages in {language} for this prospect:
- Name: {lead_name}
- Title: {lead_title}
- Company: {lead_company}

1. custom1: a LinkedIn connection request note, maximum {custom1_max} characters.
2. custom2: a follow-up message sent after the connection is accepted, maximum {custom2_max} characters.

Respond with ONLY a JSON object in this exact shape, no markdown fences, no extra text:
{{"custom1": "...", "custom2": "..."}}"""


def _build_bd_prompt(
    lead: Dict[str, Any],
    plan: str,
    sender_profile: Optional[SenderProfile],
    language: str,
    product_description: Optional[str],
    hook_copy: Optional[str],
    custom1_max: int,
    custom2_max: int,
) -> str:
    sender_context = _build_sender_context(plan, sender_profile)
    product_context = _build_product_context(product_description)
    lead_name = lead.get("full_name") or lead.get("name") or "the contact"
    lead_title = lead.get("title") or lead.get("job_title") or ""
    lead_company = lead.get("company") or ""

    hook_line = (
        f"\nThe org's own angle for this type of partner — lead with this, don't invent a generic pitch:\n{hook_copy}\n"
        if hook_copy
        else ""
    )

    return f"""{sender_context}
{product_context}
This is BD Group outreach: {lead_name} ({lead_title} at {lead_company}) is a potential CHANNEL
PARTNER, not a direct buyer. Frame the message in THIRD PERSON — talk about how THEIR customers
would benefit ("your customers dealing with X..."), never "you have this problem". This is a
partnership pitch, not a direct sales pitch.
{hook_line}
Write two LinkedIn outreach messages in {language} for this contact:
- Name: {lead_name}
- Title: {lead_title}
- Company: {lead_company}

1. custom1: a LinkedIn connection request note, maximum {custom1_max} characters. This reads as a
   partnership introduction, not a terse cold pitch — use a meaningfully larger share of that
   limit than a brief one-liner would.
2. custom2: a follow-up message sent after the connection is accepted, maximum {custom2_max}
   characters. Same guidance: make full, substantive use of the available space rather than
   writing something minimal.

Respond with ONLY a JSON object in this exact shape, no markdown fences, no extra text:
{{"custom1": "...", "custom2": "..."}}"""


def _parse_response(text: str, custom1_max: int, custom2_max: int) -> Dict[str, str]:
    match = _JSON_BLOCK_RE.search(text)
    if not match:
        raise ValueError(f"No JSON object found in Claude response: {text}")
    # Claude sometimes emits literal newlines/tabs inside JSON string values
    # (e.g. "custom2": "Hola,\n\nGracias...") instead of the escaped \n form.
    # strict=False allows raw control characters inside strings so a stray
    # line break doesn't blow up the whole batch.
    parsed = json.loads(match.group(0), strict=False)
    return {
        "custom1": (parsed.get("custom1") or "")[:custom1_max],
        "custom2": (parsed.get("custom2") or "")[:custom2_max],
    }


def _build_client(anthropic_key: str, anthropic_base_url: str) -> anthropic.Anthropic:
    # The Anthropic SDK appends /v1 itself, so a base_url that already ends in
    # /v1 (e.g. https://api.aitokenking.com.tw/api/v1) would produce /v1/v1.
    # Strip a trailing /v1 before handing it to the client.
    base_url = anthropic_base_url.rstrip("/")
    if base_url.endswith("/v1"):
        base_url = base_url[:-3]
    return anthropic.Anthropic(api_key=anthropic_key, base_url=base_url)


def generate_messages_for_batch(
    leads: List[Dict[str, Any]],
    anthropic_key: str,
    plan: str,
    sender_profile: Optional[SenderProfile],
    language: str,
    anthropic_base_url: str,
    anthropic_model: str,
    market: Optional[str] = None,
    product_description: Optional[str] = None,
) -> List[Dict[str, Any]]:
    client = _build_client(anthropic_key, anthropic_base_url)
    custom1_max, custom2_max = _resolve_char_limits(sender_profile)

    for lead in leads:
        # Resolve language per lead: a batch can span markets when an SDR
        # covers several. Fall back to the batch-level market parameter.
        lead_market = lead.get("market") or market
        effective_language = _resolve_language(language, lead_market, sender_profile)
        prompt = _build_prompt(
            lead,
            plan,
            sender_profile,
            effective_language,
            product_description,
            custom1_max,
            custom2_max,
        )
        response = client.messages.create(
            model=anthropic_model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
        messages = _parse_response(text, custom1_max, custom2_max)
        lead["custom1"] = messages["custom1"]
        lead["custom2"] = messages["custom2"]

    return leads


def generate_bd_messages_for_batch(
    leads: List[Dict[str, Any]],
    anthropic_key: str,
    plan: str,
    sender_profile: Optional[SenderProfile],
    language: str,
    anthropic_base_url: str,
    anthropic_model: str,
    product_description: Optional[str] = None,
    hook_copy_by_channel_family: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """Same shape as generate_messages_for_batch, but for BD Group candidates:
    third-person partnership framing, org-authored hook_copy as the core
    angle, and a fuller message within the sender's real character ceiling.

    Deliberately not wired into scraping — callers only invoke this for
    already human-confirmed scraper_leads rows.
    """
    client = _build_client(anthropic_key, anthropic_base_url)
    custom1_max, custom2_max = _resolve_char_limits(sender_profile)
    hook_copy_by_channel_family = hook_copy_by_channel_family or {}

    for lead in leads:
        lead_market = lead.get("market")
        effective_language = _resolve_language(language, lead_market, sender_profile)
        hook_copy = hook_copy_by_channel_family.get(lead.get("channel_family"))
        prompt = _build_bd_prompt(
            lead,
            plan,
            sender_profile,
            effective_language,
            product_description,
            hook_copy,
            custom1_max,
            custom2_max,
        )
        response = client.messages.create(
            model=anthropic_model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
        messages = _parse_response(text, custom1_max, custom2_max)
        lead["custom1"] = messages["custom1"]
        lead["custom2"] = messages["custom2"]

    return leads
