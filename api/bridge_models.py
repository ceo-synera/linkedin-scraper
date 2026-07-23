from typing import List, Literal

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


class BridgeCandidateUpdate(BaseModel):
    """Confirm / reject / restore a candidate after human review."""

    verification_status: VerificationStatus
    organization_id: str
