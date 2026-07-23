import random
from collections import defaultdict
from typing import Any, Dict, List

from api.models import SdrAssignment


def distribute_leads(
    leads: List[Dict[str, Any]], sdr_assignments: List[SdrAssignment]
) -> Dict[str, List[Dict[str, Any]]]:
    # The CRM now always sends exactly one SdrAssignment per run (SDRs no
    # longer have Scraper access; only the org admin runs it, for that one
    # SDR). With a single guaranteed recipient, market-based routing serves no
    # purpose and only risks orphaning leads whose market isn't covered by
    # assigned_markets (or doesn't match its casing) — give that SDR 100% of
    # the leads directly. The market-routing path below is kept as a fallback
    # in case multiple assignments are ever sent again.
    if len(sdr_assignments) == 1:
        return {sdr_assignments[0].sdr_id: list(leads)}

    distribution: Dict[str, List[Dict[str, Any]]] = {
        assignment.sdr_id: [] for assignment in sdr_assignments
    }

    leads_by_market: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for lead in leads:
        leads_by_market[lead.get("market")].append(lead)

    for market_leads in leads_by_market.values():
        random.shuffle(market_leads)

    markets_to_sdrs: Dict[str, List[str]] = defaultdict(list)
    for assignment in sdr_assignments:
        for market in assignment.assigned_markets:
            markets_to_sdrs[market].append(assignment.sdr_id)

    for market, sdr_ids in markets_to_sdrs.items():
        market_leads = leads_by_market.get(market, [])
        if not sdr_ids or not market_leads:
            continue

        for index, lead in enumerate(market_leads):
            sdr_id = sdr_ids[index % len(sdr_ids)]
            distribution[sdr_id].append(lead)

    return distribution
