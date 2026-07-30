from typing import List, Literal, Optional

from pydantic import BaseModel, Field

# Mirrors the CHECK constraints on the bridge_* tables. Declaring them as
# Literals means an invalid value is rejected by FastAPI with a 422 before it
# ever reaches Postgres, instead of surfacing as an opaque DB constraint error.
ChannelFamily = Literal[
    "reseller",
    "referral",
    "technology_integration",
    "affiliate",
    "channel_distribution",
]

VerificationStatus = Literal["pending", "confirmed", "rejected"]


class BridgeSeedListInput(BaseModel):
    """A saved set of company/industry filters an admin searches partnerships in.

    organization_id is part of the payload (not in the original spec sketch)
    because bridge_seed_lists.organization_id is NOT NULL — the row can't be
    created without it, and it's what scopes every later read of this list.
    """

    organization_id: str
    name: str
    channel_family: ChannelFamily
    company_names: List[str] = Field(default_factory=list)
    industry_codes: List[int] = Field(default_factory=list)
    company_headcounts: List[str] = Field(default_factory=list)
    geo_codes: List[int] = Field(default_factory=list)


class BridgeRunRequest(BaseModel):
    """Start a Bridge discovery run over one seed list.

    No anthropic_* fields on purpose: Bridge only discovers and organizes
    candidates for human review — it never generates outreach messages.
    """

    run_id: str
    organization_id: str
    seed_list_id: str
    apify_token: str


class BridgeSeedListUpdate(BaseModel):
    """Edit a saved seed list in place.

    Every filter field is optional so the CRM can send only what changed; a
    field left unset is not written, which is what distinguishes "leave this
    alone" from "clear this list". That distinction matters here because the
    filters ARE lists — with a non-optional model, editing only the name would
    silently blank out company_names, industry_codes and the rest.

    `name` and `channel_family` are optional for the same reason. Passing an
    explicit empty list to clear a filter still works and is intentional.
    """

    organization_id: str
    name: Optional[str] = None
    channel_family: Optional[ChannelFamily] = None
    company_names: Optional[List[str]] = None
    industry_codes: Optional[List[int]] = None
    company_headcounts: Optional[List[str]] = None
    geo_codes: Optional[List[int]] = None


class BridgeCandidateUpdate(BaseModel):
    """Confirm / reject / restore a candidate after human review."""

    verification_status: VerificationStatus
    organization_id: str


class BridgeConfirmBatchRequest(BaseModel):
    """Confirm several candidates at once and generate their outreach messages.

    Reuses api.message_generator.generate_messages_for_batch — the same
    engine the main lead pipeline uses for custom1/custom2 — rather than a
    second implementation. anthropic_base_url/anthropic_model are Optional
    (not defaulted here) because the CRM proxy sends explicit `null` when the
    org hasn't overridden them; a pydantic default wouldn't apply to an
    explicit null, so the fallback is resolved in the endpoint instead.
    """

    organization_id: str
    candidate_ids: List[str] = Field(default_factory=list)
    sdr_id: str
    sender_profile_id: Optional[str] = None
    anthropic_key: str
    anthropic_base_url: Optional[str] = None
    anthropic_model: Optional[str] = None
    bridge_context: str = ""
