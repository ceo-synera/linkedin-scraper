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
