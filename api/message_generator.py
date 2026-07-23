import asyncio
import json
import logging
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

import anthropic

from api.models import SenderProfile

log = logging.getLogger(__name__)

# Max concurrent Claude calls per batch. The messages are generated in
# parallel bounded by a semaphore so 90 leads don't run 90-at-a-time (which
# would hammer the AITokenKing proxy and risk 429s) nor one-at-a-time (which
# took ~8 min and caused 504s while the CRM polled the run). Lower to 3-4 if
# the proxy starts returning 429 at 6.
MESSAGE_CONCURRENCY = 6

# Fallbacks only — a sender profile's own connection_note_max_chars /
# followup_max_chars win when set (see _resolve_char_limits).
DEFAULT_CUSTOM1_MAX_CHARS = 300
DEFAULT_CUSTOM2_MAX_CHARS = 500

# Generous ceiling so custom1 + custom2 (in verbose languages like Spanish or
# Chinese) don't get truncated mid-JSON and become unparseable.
MESSAGE_MAX_TOKENS = 2048

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


def _build_company_context(company_context: str) -> str:
    # The org's admin may not have configured this yet — omit the section
    # entirely rather than error or insert a fake-looking placeholder, so
    # behavior is unchanged for orgs without it set.
    if not company_context:
        return ""
    return (
        f"\nCompany context: {company_context}\n\n"
        "Use this context naturally when relevant to the lead's role or "
        "company — don't force it into every message, but let it inform how "
        "you position the outreach when it makes sense.\n"
    )


def _build_prompt(
    lead: Dict[str, Any],
    plan: str,
    sender_profile: Optional[SenderProfile],
    language: str,
    company_context: str,
    custom1_max: int,
    custom2_max: int,
) -> str:
    sender_context = _build_sender_context(plan, sender_profile)
    company_context_block = _build_company_context(company_context)
    lead_name = lead.get("full_name") or lead.get("name") or "the prospect"
    lead_title = lead.get("title") or lead.get("job_title") or ""
    lead_company = lead.get("company") or ""

    return f"""{sender_context}
{company_context_block}
Write two LinkedIn outreach messages in {language} for this prospect:
- Name: {lead_name}
- Title: {lead_title}
- Company: {lead_company}

1. custom1: a LinkedIn connection request note, maximum {custom1_max} characters.
2. custom2: a follow-up message sent after the connection is accepted, maximum {custom2_max} characters.

Respond with ONLY a JSON object in this exact shape, no markdown fences, no extra text:
{{"custom1": "...", "custom2": "..."}}"""


def _extract_field(text: str, key: str) -> str:
    # Pull "key": "value" where value runs to the next unescaped quote, or to
    # the end of the text if the response was truncated before the closing
    # quote (max_tokens cut-off). Best-effort unescaping of the common escapes.
    match = re.search(rf'"{key}"\s*:\s*"((?:[^"\\]|\\.)*)', text)
    if not match:
        return ""
    return (
        match.group(1)
        .replace('\\"', '"')
        .replace("\\n", "\n")
        .replace("\\t", "\t")
    )


def _parse_response(text: str, custom1_max: int, custom2_max: int) -> Dict[str, str]:
    # Preferred path: a well-formed JSON object. strict=False tolerates literal
    # newlines/tabs inside string values (Claude sometimes emits raw instead of
    # escaped \n).
    match = _JSON_BLOCK_RE.search(text)
    if match:
        try:
            parsed = json.loads(match.group(0), strict=False)
            return {
                "custom1": (parsed.get("custom1") or "")[:custom1_max],
                "custom2": (parsed.get("custom2") or "")[:custom2_max],
            }
        except (ValueError, json.JSONDecodeError):
            pass  # fall through to salvage

    # Salvage path: the response was truncated (no closing brace/quote) or
    # otherwise not strict JSON. Recover custom1 / custom2 individually so a
    # cut-off custom2 still keeps a usable custom1 instead of losing both.
    custom1 = _extract_field(text, "custom1")
    custom2 = _extract_field(text, "custom2")
    if not custom1 and not custom2:
        raise ValueError(f"No JSON object found in Claude response: {text}")
    return {
        "custom1": custom1[:custom1_max],
        "custom2": custom2[:custom2_max],
    }


def _build_async_client(
    anthropic_key: str, anthropic_base_url: str
) -> anthropic.AsyncAnthropic:
    # The Anthropic SDK appends /v1 itself, so a base_url that already ends in
    # /v1 (e.g. https://api.aitokenking.com.tw/api/v1) would produce /v1/v1.
    # Strip a trailing /v1 before handing it to the client.
    base_url = anthropic_base_url.rstrip("/")
    if base_url.endswith("/v1"):
        base_url = base_url[:-3]
    return anthropic.AsyncAnthropic(api_key=anthropic_key, base_url=base_url)


async def _run_batch(
    leads: List[Dict[str, Any]],
    client: anthropic.AsyncAnthropic,
    anthropic_model: str,
    custom1_max: int,
    custom2_max: int,
    build_prompt: Callable[[Dict[str, Any]], str],
    log_fn: Optional[Callable[[str], None]],
) -> None:
    # Generate messages for every lead concurrently, bounded by a semaphore so
    # no more than MESSAGE_CONCURRENCY Claude calls are in flight at once.
    semaphore = asyncio.Semaphore(MESSAGE_CONCURRENCY)
    total = len(leads)
    completed = 0

    async def _process(lead: Dict[str, Any]) -> None:
        nonlocal completed
        prompt = build_prompt(lead)
        async with semaphore:
            try:
                response = await client.messages.create(
                    model=anthropic_model,
                    max_tokens=MESSAGE_MAX_TOKENS,
                    messages=[{"role": "user", "content": prompt}],
                )
                text = "".join(
                    block.text
                    for block in response.content
                    if getattr(block, "type", None) == "text"
                )
                messages = _parse_response(text, custom1_max, custom2_max)
                lead["custom1"] = messages["custom1"]
                lead["custom2"] = messages["custom2"]
            except Exception as exc:
                # One bad/truncated response must not lose the whole batch.
                log.warning(
                    "Message generation failed for lead %s: %s",
                    lead.get("linkedin_url"),
                    exc,
                )
        # asyncio is single-threaded, so this counter is race-free. Log per
        # completed batch rather than per individual message.
        completed += 1
        if log_fn and (completed % MESSAGE_CONCURRENCY == 0 or completed == total):
            # log_fn writes to Supabase synchronously — keep it off the loop.
            await asyncio.to_thread(
                log_fn, f"Generated messages for {completed}/{total} leads"
            )

    await asyncio.gather(*(_process(lead) for lead in leads))


async def generate_messages_for_batch(
    leads: List[Dict[str, Any]],
    anthropic_key: str,
    plan: str,
    sender_profile: Optional[SenderProfile],
    language: str,
    anthropic_base_url: str,
    anthropic_model: str,
    market: Optional[str] = None,
    company_context: str = "",
    log_fn: Optional[Callable[[str], None]] = None,
) -> List[Dict[str, Any]]:
    if not leads:
        return leads

    custom1_max, custom2_max = _resolve_char_limits(sender_profile)

    def build_prompt(lead: Dict[str, Any]) -> str:
        # Resolve language per lead: a batch can span markets when an SDR
        # covers several. Fall back to the batch-level market parameter.
        lead_market = lead.get("market") or market
        effective_language = _resolve_language(language, lead_market, sender_profile)
        return _build_prompt(
            lead,
            plan,
            sender_profile,
            effective_language,
            company_context,
            custom1_max,
            custom2_max,
        )

    async with _build_async_client(anthropic_key, anthropic_base_url) as client:
        await _run_batch(
            leads, client, anthropic_model, custom1_max, custom2_max, build_prompt, log_fn
        )

    return leads
