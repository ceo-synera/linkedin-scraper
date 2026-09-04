"""ICP scoring for scraped sales leads.

WHAT THIS REPLACED, AND WHY IT HAD TO
-------------------------------------
The previous scorer awarded 100 points like this: 30 for a job title matched
against a hardcoded `PRIORITY_TITLES` list, 20 for an "AI signal" matched
against a hardcoded keyword list, and **50 as three flat constants** for company
size, industry and LinkedIn activity. Measured 05/08/2026:

  * The three constants gave every lead the same 50 points, so they could not
    tell any two leads apart.
  * The keyword lists were matched with `in`, i.e. as substrings. `"ai"` fired
    on av-AI-lable, em-AI-l, ret-AIl and T-AI-pei, so those 20 points were a
    fourth constant for anyone with a bio. `"cto"` fired on dire-CTO-r and
    `"coo"` on -COO-rdinator, so every Director scored as a C-level, while
    "Chief Technology Officer" and "VP of Engineering" — spelled out, as many
    people write them — scored zero.
  * Both lists described exactly one ideal customer: **ours**. Every other
    organisation on this platform sells something else, and `org_combos` had
    already existed for months to let each of them choose which titles to
    SEARCH for. The scraper searched the customer's titles and then scored the
    results against a profile they had never seen.

Net: 85 of 100 points measured nothing and the other 15 were wrong in both
directions, so nearly every lead with a bio landed on 60 (WARM) and reached 90
(HOT) only by substring accident.

WHAT IT DOES NOW
----------------
Three components, all of which can actually distinguish one lead from another,
and none of which encodes who *we* sell to:

| Component      | Max | Source                                                |
|----------------|-----|-------------------------------------------------------|
| Target title   | 50  | the run's OWN combos (`title_keywords`)               |
| Seniority      | 20  | the title itself, via universal seniority vocabulary  |
| Buying signal  | 30  | the org's `company_context`, matched against `about`  |

The target-title component is the whole point: an organisation's combos are its
ideal customer, stated by the customer, and since
`20260904_org_owned_combos.sql` they can write their own instead of only
toggling ours. The two halves of the pipeline finally agree — a run searches
for a set of titles and then scores against that same set.

Seniority stays global on purpose. "Is this person senior" is not an opinion
about who a good lead is; every customer wants to know it, and it is the same
question in Taipei and in Buenos Aires. That is the same reason the actor's
seniority enum is a constant: it describes the world, not our preferences.

MISSING DATA IS NOT A LOW SCORE
-------------------------------
A component the ORGANISATION has not configured is excluded from the
denominator rather than scored as zero — an org that never filled in
`company_context` gets a 0-100 scale out of the other two components instead of
a ceiling of 70 it can never reach and no way to know why. A field the LEAD is
missing (an empty `about`, which is ~32% of them) does score zero, because that
is a fact about the lead, not about our configuration.

SCORES ARE NOT COMPARABLE ACROSS THE CUTOVER
--------------------------------------------
Leads scored before this shipped keep their old numbers, and they mean
something else. They are deliberately NOT backfilled: `scraper_leads` has no
`about` column, so the buying-signal component cannot be recomputed for a past
lead, and a backfill would invent a third scale rather than restore the second.
Sort and filter within a period, and treat a pre-cutover 60 as "unknown" rather
than as WARM.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .text_match import (
    best_phrase_match,
    contains_phrase,
    normalize,
    significant_tokens,
)

# ---------------------------------------------------------------------------
# Weights
# ---------------------------------------------------------------------------
TARGET_TITLE_MAX = 50
SENIORITY_MAX = 20
BUYING_SIGNAL_MAX = 30

HOT_THRESHOLD = 70
WARM_THRESHOLD = 50

# ---------------------------------------------------------------------------
# Seniority vocabulary
#
# Deliberately global, and deliberately multilingual: these are the words that
# say how much authority someone has, in the languages this product sells into.
# They are NOT a target profile — a customer selling to plant supervisors still
# wants to know which of them is the plant DIRECTOR.
#
# The order of the checks in `score_seniority` matters more than the lists do,
# because "senior" titles are built by prefixing junior ones:
#
#     "Vice President"      contains "president"
#     "副總裁"               contains "總裁"    (and CJK matches as a substring)
#     "Deputy Chief ..."     contains "chief"
#
# A naive most-senior-first scan therefore reads every VP as a president. The
# deputy check runs BEFORE the president check for exactly that reason — this is
# the same class of bug as the substring matching this file was rewritten to
# fix, one level up.
# ---------------------------------------------------------------------------
_EXEC_PHRASES = (
    "ceo", "cto", "cio", "coo", "cfo", "cmo", "cdo", "cpo", "cso", "cro", "chro",
    "chief", "founder", "co founder", "cofounder", "owner", "partner",
    "managing director", "managing partner", "director general",
    "propietario", "fundador", "socio", "dueño",
    "執行長", "技術長", "資訊長", "營運長", "財務長", "行銷長", "數位長",
    "董事長", "總經理", "創辦人", "負責人",
    "tổng giám đốc", "nhà sáng lập",
)

# Kept apart from _EXEC_PHRASES only so the deputy check can sit between them.
_PRESIDENT_PHRASES = ("president", "presidente", "總裁", "chủ tịch")

_DEPUTY_PHRASES = (
    "vice president", "vice presidente", "vicepresidente", "deputy",
    "副總", "副總裁", "副理", "副總經理",
    "phó tổng giám đốc", "phó chủ tịch",
)

# Support roles that borrow an executive's title without any of the authority.
# "Executive Assistant to the CEO" is one of the most common titles on LinkedIn
# and contains "ceo"; scoring it as a C-level is the same false positive as
# reading dire-CTO-r as a CTO, arriving by a different route. Capped rather than
# zeroed — an assistant is a real person at a real target company, just not the
# buyer.
_ASSISTANT_PHRASES = (
    "assistant", "asistente", "auxiliar", "secretary", "secretaria",
    "助理", "秘書", "trợ lý", "thư ký",
)

_UPPER_MIDDLE_PHRASES = (
    "vp", "svp", "evp", "head", "head of", "director", "directora", "directeur",
    "總監", "主管", "處長", "協理",
    "giám đốc", "trưởng bộ phận",
)

_MANAGER_PHRASES = (
    "manager", "lead", "supervisor", "principal", "gerente", "jefe",
    "responsable", "encargado",
    "經理", "組長", "主任", "課長",
    "trưởng phòng", "quản lý",
)

_SENIORITY_EXEC = SENIORITY_MAX          # 20
_SENIORITY_UPPER_MIDDLE = 14
_SENIORITY_MANAGER = 8

# ---------------------------------------------------------------------------
# Buying signal
#
# The signal terms come from the organisation's own `company_context` — the
# free-text field where an admin already describes what they sell and to whom,
# and which is already sent to the message generator so outreach can reference
# real products. Reusing it means the signal is per-org without asking anyone to
# fill in a second form.
#
# These generic business words are dropped from it. Not because they are
# meaningless to the business, but because they appear in almost every LinkedIn
# bio, so matching on them would turn this component back into a constant —
# which is precisely how the old "ai" keyword failed.
# ---------------------------------------------------------------------------
_CONTEXT_NOISE = frozenset({
    "company", "companies", "business", "businesses", "customer", "customers",
    "client", "clients", "solution", "solutions", "service", "services",
    "product", "products", "platform", "platforms", "team", "teams",
    "market", "markets", "industry", "industries", "sector", "sectors",
    "help", "helps", "helping", "provide", "provides", "providing",
    "offer", "offers", "offering", "work", "working", "world", "global",
    "leading", "best", "quality", "based", "years", "experience", "people",
    "empresa", "empresas", "cliente", "clientes", "servicio", "servicios",
    "producto", "productos", "soluciones", "mercado", "negocio",
})

_MIN_CONTEXT_TERM_LEN = 4
_MAX_CONTEXT_TERMS = 80


# ---------------------------------------------------------------------------
# Title equivalences
#
# The same job has several spellings, and the two sides of a match are written
# by different people: a customer types "CTO" into a combo, the prospect writes
# "Chief Technology Officer" on their profile — or the other way round, or in
# Chinese. Matching them was the single biggest source of false negatives in the
# old scorer, and fixing the substring bug alone would not have helped: the
# strings genuinely differ.
#
# So each target title is expanded into its equivalents before scoring. Like the
# seniority vocabulary, this is a fact about how these job titles are written,
# not an opinion about who to sell to — the customer still chooses the titles;
# this only makes sure a choice isn't missed over an abbreviation.
#
# Members are substituted as whole phrases, so "VP of Engineering" also becomes
# "Vice President of Engineering" without anyone listing that combination.
# ---------------------------------------------------------------------------
_TITLE_EQUIVALENTS: Sequence[Sequence[str]] = (
    ("ceo", "chief executive officer", "執行長"),
    ("cto", "chief technology officer", "chief technical officer", "技術長"),
    ("cio", "chief information officer", "資訊長"),
    ("coo", "chief operating officer", "營運長"),
    ("cfo", "chief financial officer", "財務長"),
    ("cmo", "chief marketing officer", "行銷長"),
    ("cdo", "chief digital officer", "數位長"),
    ("cpo", "chief product officer"),
    ("chro", "chief human resources officer"),
    ("vp", "vice president"),
    ("svp", "senior vice president"),
    ("evp", "executive vice president"),
    ("hr", "human resources"),
    ("it", "information technology"),
    ("r&d", "research and development"),
)

_MAX_VARIANTS_PER_TITLE = 6


def expand_title_variants(phrase: object) -> List[str]:
    """A target title plus the other ways the same job gets written.

    One substitution group per phrase — enough for "VP of Engineering" ->
    "Vice President of Engineering" without generating a combinatorial pile of
    strings nobody has ever typed.
    """
    base = normalize(phrase)
    if not base:
        return []

    variants: List[str] = [base]
    base_tokens = base.split()

    for group in _TITLE_EQUIVALENTS:
        matched_at = None
        matched_len = 0
        matched_member = None
        for member in group:
            member_tokens = normalize(member).split()
            span = len(member_tokens)
            if not span or span > len(base_tokens):
                continue
            for start in range(len(base_tokens) - span + 1):
                if base_tokens[start:start + span] == member_tokens:
                    matched_at, matched_len, matched_member = start, span, member
                    break
            if matched_at is not None:
                break
        if matched_at is None:
            continue
        for other in group:
            if other == matched_member:
                continue
            other_tokens = normalize(other).split()
            variant = " ".join(
                base_tokens[:matched_at] + other_tokens + base_tokens[matched_at + matched_len:]
            )
            if variant and variant not in variants:
                variants.append(variant)
        break

    return variants[:_MAX_VARIANTS_PER_TITLE]


@dataclass
class IcpProfile:
    """What THIS organisation considers a good lead, for THIS run.

    Built once per run by `build_icp_profile` and handed to `score_leads`, so
    the scorer holds no opinion of its own about who to look for.
    """

    target_titles: List[str] = field(default_factory=list)
    signal_terms: List[str] = field(default_factory=list)

    @property
    def scores_titles(self) -> bool:
        return bool(self.target_titles)

    @property
    def scores_signal(self) -> bool:
        return bool(self.signal_terms)

    def describe(self) -> str:
        """One line for the run log, so an admin can see what they were scored against."""
        parts = []
        parts.append(
            f"{len(self.target_titles)} target title variant(s) from this run's combos"
            if self.scores_titles
            else "NO target titles (the run's combos have no title_keywords) — "
                 "titles are not being scored"
        )
        parts.append(
            f"{len(self.signal_terms)} buying-signal term(s) from company_context"
            if self.scores_signal
            else "no company_context set — buying signal is not being scored"
        )
        return "ICP profile: " + "; ".join(parts)


def build_icp_profile(
    combos: Optional[Sequence[Dict[str, Any]]] = None,
    company_context: str = "",
) -> IcpProfile:
    """Turn a run's combos and the org's company context into a scoring profile.

    Both inputs are already loaded by `job_runner` before scraping starts, so
    this costs nothing extra. Duplicate titles across combos are collapsed —
    a title appearing in three combos is not three times more of a match.
    """
    seen: set = set()
    target_titles: List[str] = []
    for combo in combos or []:
        if not isinstance(combo, dict):
            continue
        for keyword in combo.get("title_keywords") or []:
            for variant in expand_title_variants(keyword):
                if variant in seen:
                    continue
                seen.add(variant)
                target_titles.append(variant)

    signal_terms: List[str] = []
    for term in sorted(significant_tokens(company_context)):
        if term in _CONTEXT_NOISE:
            continue
        # CJK tokens are whole words at two characters; Latin ones need more
        # length before they are specific enough to mean anything.
        if term.isascii() and len(term) < _MIN_CONTEXT_TERM_LEN:
            continue
        signal_terms.append(term)
        if len(signal_terms) >= _MAX_CONTEXT_TERMS:
            break

    return IcpProfile(target_titles=target_titles, signal_terms=signal_terms)


def _lead_title(lead: Dict[str, Any]) -> str:
    # `job_title` is what the actor returns; `title` is what the rest of the
    # pipeline uses. `_map_lead` keeps both, but a lead can reach here from
    # either side, so read both rather than assuming.
    return lead.get("job_title") or lead.get("title") or ""


def score_seniority(title: object) -> int:
    """Seniority points from a job title. Public because Bridge scores it too.

    The check order is the algorithm — see the comment above the phrase lists.
    """
    if not normalize(title):
        return 0

    # Resolved before anything else, because both of the senior tiers are
    # spelled by prefixing a more junior word onto them: "Vice President"
    # contains "president", 副總裁 contains 總裁, "Deputy Chief" contains
    # "chief".
    is_deputy = any(contains_phrase(title, phrase) for phrase in _DEPUTY_PHRASES)

    if any(contains_phrase(title, phrase) for phrase in _EXEC_PHRASES) or (
        any(contains_phrase(title, phrase) for phrase in _PRESIDENT_PHRASES)
    ):
        points = _SENIORITY_UPPER_MIDDLE if is_deputy else _SENIORITY_EXEC
    elif is_deputy or any(contains_phrase(title, phrase) for phrase in _UPPER_MIDDLE_PHRASES):
        points = _SENIORITY_UPPER_MIDDLE
    elif any(contains_phrase(title, phrase) for phrase in _MANAGER_PHRASES):
        points = _SENIORITY_MANAGER
    else:
        points = 0

    if points > _SENIORITY_MANAGER and any(
        contains_phrase(title, phrase) for phrase in _ASSISTANT_PHRASES
    ):
        return _SENIORITY_MANAGER

    return points


def _score_target_title(lead: Dict[str, Any], profile: IcpProfile) -> int:
    match = best_phrase_match(_lead_title(lead), profile.target_titles)
    return int(round(TARGET_TITLE_MAX * match))


def _score_buying_signal(lead: Dict[str, Any], profile: IcpProfile) -> int:
    about = lead.get("about") or ""
    if not normalize(about):
        # A fact about the lead, not about the configuration: no bio, no signal.
        return 0
    hits = 0
    for term in profile.signal_terms:
        if contains_phrase(about, term):
            hits += 1
            if hits >= 2:
                break
    if hits >= 2:
        return BUYING_SIGNAL_MAX
    if hits == 1:
        return BUYING_SIGNAL_MAX // 2
    return 0


def _classify(score: int) -> str:
    if score >= HOT_THRESHOLD:
        return "HOT"
    if score >= WARM_THRESHOLD:
        return "WARM"
    return "COLD"


def score_lead(lead: Dict[str, Any], profile: Optional[IcpProfile] = None) -> Dict[str, Any]:
    """Attach `icp_score`, `icp_tier` and `icp_breakdown` to one lead, in place."""
    profile = profile or IcpProfile()

    earned = 0
    available = 0
    breakdown: Dict[str, Any] = {}

    if profile.scores_titles:
        points = _score_target_title(lead, profile)
        earned += points
        available += TARGET_TITLE_MAX
        breakdown["target_title"] = points
    else:
        breakdown["target_title"] = None  # not configured, not measured

    seniority = score_seniority(_lead_title(lead))
    earned += seniority
    available += SENIORITY_MAX
    breakdown["seniority"] = seniority

    if profile.scores_signal:
        points = _score_buying_signal(lead, profile)
        earned += points
        available += BUYING_SIGNAL_MAX
        breakdown["buying_signal"] = points
    else:
        breakdown["buying_signal"] = None

    # Rescale over what was actually measurable, so the number always means
    # "this share of what we could check" and stays a 0-100 the CRM's own CHECK
    # constraint accepts.
    score = int(round(earned / available * 100)) if available else 0
    score = max(0, min(100, score))

    lead["icp_score"] = score
    lead["icp_tier"] = _classify(score)
    lead["icp_breakdown"] = breakdown
    return lead


def score_leads(
    leads: List[Dict[str, Any]], profile: Optional[IcpProfile] = None
) -> List[Dict[str, Any]]:
    """Score a batch and return it sorted best-first.

    The sort is load-bearing: `job_runner` trims an over-delivering run down to
    `total_leads` with a slice, so this ordering is what makes the trim keep the
    best leads instead of an arbitrary subset.
    """
    profile = profile or IcpProfile()
    scored = [score_lead(lead, profile) for lead in leads]
    scored.sort(key=lambda lead: lead["icp_score"], reverse=True)
    return scored
