"""Reference Rates API — SOFR / ESTR / SONIA, fronted from NY Fed, ECB and BoE.

These are the risk-free-rate indices used by SwapClear, so they line up with the
`index` dimension in the LCH volume reports.
"""
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from . import clients
from .models import RateHistory, RateInfo, RateObservation

router = APIRouter(prefix="/rates", tags=["reference-rates"])


@router.get("", summary="List available reference rates", response_model=list[RateInfo])
def list_rates():
    return [RateInfo(rate_id=k, **v) for k, v in clients.RATE_META.items()]


def _require_rate(rate_id: str) -> str:
    rid = rate_id.upper()
    if rid not in clients.RATE_META:
        raise HTTPException(404, f"Unknown rate '{rate_id}'. Available: {', '.join(clients.RATE_META)}")
    return rid


@router.get("/{rate_id}/latest", summary="Latest published value", response_model=RateObservation)
def latest(rate_id: str):
    rid = _require_rate(rate_id)
    try:
        obs = clients.fetch_rate(rid)
    except Exception as e:  # upstream failure
        raise HTTPException(502, f"Upstream error for {rid}: {e}")
    if not obs:
        raise HTTPException(404, f"No observations available for {rid}")
    last = obs[-1]
    m = clients.RATE_META[rid]
    return RateObservation(rate_id=rid, name=m["name"], currency=m["currency"], source=m["source"],
                           date=last["date"], value=last.get("value"),
                           volume_billions=last.get("volume_billions"))


@router.get("/{rate_id}/history", summary="Observations over a date range", response_model=RateHistory)
def history(rate_id: str,
            start: Optional[str] = Query(None, description="ISO start date YYYY-MM-DD"),
            end: Optional[str] = Query(None, description="ISO end date YYYY-MM-DD")):
    rid = _require_rate(rate_id)
    try:
        obs = clients.fetch_rate(rid, start=start, end=end)
    except Exception as e:
        raise HTTPException(502, f"Upstream error for {rid}: {e}")
    m = clients.RATE_META[rid]
    return RateHistory(rate_id=rid, name=m["name"], currency=m["currency"], source=m["source"],
                       observations=obs)
