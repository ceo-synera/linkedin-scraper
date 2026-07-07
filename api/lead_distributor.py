import random
from collections import defaultdict
from typing import Any, Dict, List

from api.models import SdrAssignment


def distribute_leads(
    leads: List[Dict[str, Any]], sdr_assignments: List[SdrAssignment]
) -> Dict[str, List[Dict[str, Any]]]:
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
