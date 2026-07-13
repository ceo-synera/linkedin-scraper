from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class Plan(str, Enum):
    basic = "basic"
    premium = "premium"
    enterprise = "enterprise"
    ultra = "ultra"


class SenderProfile(BaseModel):
    id: Optional[str] = None
    display_name: str
    title: str
    company: str
    style_hint: Optional[str] = None
    icp_focus: List[str] = Field(default_factory=list)
    language: str = "en"
    years_experience: Optional[int] = None
    seniority: Optional[str] = None
    expertise_area: Optional[str] = None


class SdrAssignment(BaseModel):
    sdr_id: str
    sender_profile_id: Optional[str] = None
    sender_profile: Optional[SenderProfile] = None
    assigned_markets: List[str] = Field(default_factory=list)


class RunRequest(BaseModel):
    run_id: str
    organization_id: str
    plan: Plan
    markets: List[str]
    combos: List[str]
    total_leads: int
    sdr_assignments: List[SdrAssignment] = Field(default_factory=list)
    apify_token: str
    anthropic_key: str
    anthropic_base_url: Optional[str] = "https://api.anthropic.com"
    anthropic_model: Optional[str] = "claude-sonnet-4-6"


class BDRunRequest(BaseModel):
    """BD Group run: search by named target companies instead of job titles.

    Deliberately separate from RunRequest — a distinct pipeline with a single
    owning SDR (no even-split/multi-SDR distribution) and no scoring/messaging
    fields yet (those are later phases).
    """

    run_id: str
    organization_id: str
    seed_list_ids: List[str]
    owner_sdr_id: str
    total_leads: int
    apify_token: str
