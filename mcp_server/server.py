#!/usr/bin/env python3
"""
LCH Reporting MCP Server (FastMCP)
==================================

An MCP facade over three LCH report files, demonstrating the exact "platform
shift" pattern in the LCH job description: instead of members ingesting static
CSV/PDF files pushed over SFTP, an AI agent (or any MCP client) queries the data
programmatically through typed, discoverable tools.

Reports exposed:
  1. REP00036a - SOD Non Cash Collateral Holdings (securities + triparty)
  2. DPG SwapClear registered daily volume
  3. EOD Volume Vanilla Swaps (notional outstanding)

Data source is pluggable via the DATA_BACKEND env var:
  - "local" (default): read CSVs from LCH_DATA_DIR (e.g. an SFTP-landed folder)
  - "s3":              read CSVs from s3://LCH_S3_BUCKET/LCH_S3_PREFIX/

Run locally:
    pip install "fastmcp>=2.0" pandas boto3
    python mcp_server/server.py            # stdio transport (for Claude Desktop / MCP Inspector)
    # or HTTP:
    MCP_TRANSPORT=http MCP_PORT=8080 python mcp_server/server.py

Security note (talk to this in interview): this lab intentionally keeps auth
simple. In production over LCH data you would put this behind OAuth 2.1 +
PKCE, authorize per-tool-call, scope tokens per clearing member (a member must
never see another member's holdings), validate every input against the schema,
and log every tool invocation for audit/compliance.
"""

import io
import json
import os
import re
import sys
import uuid
from datetime import date
from functools import lru_cache
from typing import Optional

# Make the repo root importable so `apis` resolves when run as
# `python mcp_server/server.py` (sys.path[0] would otherwise be mcp_server/).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
import pandas as pd
from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DATA_BACKEND = os.environ.get("DATA_BACKEND", "local").lower()
LCH_DATA_DIR = os.environ.get("LCH_DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "data"))
LCH_S3_BUCKET = os.environ.get("LCH_S3_BUCKET", "")
LCH_S3_PREFIX = os.environ.get("LCH_S3_PREFIX", "lch-reports").strip("/")

# COB resolution. A caller may pass an explicit `cob`; otherwise fall back to
# LCH_DEFAULT_COB if it is set (an explicit pin), and finally to the latest COB
# available in the backend — so the newest uploaded report is served
# automatically, with no redeploy or env change when a new day lands.
LCH_DEFAULT_COB = os.environ.get("LCH_DEFAULT_COB")  # optional pin; empty -> use latest

# The in-process data APIs (reference rates + legal entities). The MCP tools call
# these over localhost; the MCP layer never hits the upstream sources directly.
API_PORT = int(os.environ.get("API_PORT", "8001"))
API_BASE_URL = os.environ.get("API_BASE_URL", f"http://127.0.0.1:{API_PORT}")

mcp = FastMCP(
    name="lch-reporting",
    instructions=(
        "Two data routes are available:\n"
        "1. LCH clearing REPORTS (synthetic lab data): non-cash collateral holdings, "
        "SwapClear registered volumes, and EOD vanilla swap outstanding notional. Use "
        "get_collateral_holdings / get_swapclear_volume / get_eod_vanilla_outstanding and "
        "the list_dimensions discovery tool. If no COB date is given, the latest available "
        "report date is used.\n"
        "2. LIVE market data via APIs (real, public sources): reference risk-free rates "
        "(SOFR/ESTR/SONIA) and legal-entity/counterparty data (GLEIF LEI). Use "
        "get_reference_rate / get_rate_history / list_rates and lookup_legal_entity / "
        "get_legal_entity / get_entity_relationships.\n"
        "Pick the route that fits the question; rates and entities are live, reports are "
        "synthetic."
    ),
)


# ---------------------------------------------------------------------------
# Audit + API client (shared by the live-data tools)
# ---------------------------------------------------------------------------
def _client_ip() -> Optional[str]:
    """Best-effort caller IP for the audit log (X-Forwarded-For behind CloudFront/ALB)."""
    try:
        from fastmcp.server.dependencies import get_http_request
        req = get_http_request()
        xff = req.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
        return req.client.host if req.client else None
    except Exception:
        return None


def _audit(event: str, **fields) -> None:
    """Emit one structured audit record to stdout (picked up by container logs)."""
    print(json.dumps({"event": event, "ip": _client_ip(), **fields}, default=str), flush=True)


def _api_get(path: str, params: Optional[dict] = None) -> dict:
    """Call an in-process data API, logging the call and propagating a correlation id."""
    cid = uuid.uuid4().hex
    _audit("tool_call", path=path, params=params, correlation_id=cid)
    try:
        r = httpx.get(f"{API_BASE_URL}{path}", params=params or {},
                      headers={"X-Correlation-ID": cid}, timeout=20.0)
        _audit("api_call", path=path, status=r.status_code, correlation_id=cid)
        if r.status_code >= 400:
            detail = r.json().get("detail") if r.content else r.reason_phrase
            return {"error": detail, "status": r.status_code}
        return r.json()
    except Exception as e:
        _audit("api_error", path=path, error=str(e), correlation_id=cid)
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Data access layer
# ---------------------------------------------------------------------------
def _compact(cob: str) -> str:
    """'2026-06-30' -> '20260630'."""
    return date.fromisoformat(cob).strftime("%Y%m%d")


def _filename(report: str, cob: str) -> str:
    c = _compact(cob)
    return {
        "sod_collateral": f"REP00036a_SOD_NonCashCollateralHoldings_{c}.csv",
        "dpg_volume": f"DPG_SwapClear_RegisteredVolume_{c}.csv",
        "eod_vanilla": f"EOD_Volume_VanillaSwaps_{c}.csv",
    }[report]


# Report filenames end with a compact date suffix, e.g. '..._20260702.csv'.
_DATE_RE = re.compile(r"_(\d{8})\.csv$")


def _list_cob_compacts() -> list[str]:
    """Discover the report COB dates present in the backend as sorted 'YYYYMMDD'
    strings (chronological, since fixed-width dates sort lexicographically).

    Listed live on each call (not cached) so a newly uploaded day is picked up
    without restarting the server.
    """
    found: set[str] = set()
    if DATA_BACKEND == "s3":
        import boto3  # lazy, mirrors _load
        s3 = boto3.client("s3")
        prefix = f"{LCH_S3_PREFIX}/" if LCH_S3_PREFIX else ""
        for page in s3.get_paginator("list_objects_v2").paginate(
            Bucket=LCH_S3_BUCKET, Prefix=prefix
        ):
            for obj in page.get("Contents", []):
                m = _DATE_RE.search(obj["Key"])
                if m:
                    found.add(m.group(1))
    else:
        try:
            names = os.listdir(LCH_DATA_DIR)
        except FileNotFoundError:
            names = []
        for name in names:
            m = _DATE_RE.search(name)
            if m:
                found.add(m.group(1))
    return sorted(found)


def _latest_cob() -> str:
    """Latest COB date available in the backend, ISO 'YYYY-MM-DD'."""
    dates = _list_cob_compacts()
    if not dates:
        raise FileNotFoundError(
            f"No LCH report files found (backend='{DATA_BACKEND}', "
            f"prefix='{LCH_S3_PREFIX}'); cannot resolve the latest COB."
        )
    c = dates[-1]
    return f"{c[:4]}-{c[4:6]}-{c[6:8]}"


def _resolve_cob(cob: Optional[str]) -> str:
    """A caller-supplied cob wins; else the LCH_DEFAULT_COB pin; else latest available."""
    if cob:
        return cob
    if LCH_DEFAULT_COB:
        return LCH_DEFAULT_COB
    return _latest_cob()


@lru_cache(maxsize=32)
def _load(report: str, cob: str) -> pd.DataFrame:
    """Load one report/COB into a DataFrame, cached. Raises FileNotFoundError."""
    fname = _filename(report, cob)
    if DATA_BACKEND == "s3":
        import boto3  # imported lazily so local runs don't need it
        key = f"{LCH_S3_PREFIX}/{fname}"
        s3 = boto3.client("s3")
        obj = s3.get_object(Bucket=LCH_S3_BUCKET, Key=key)
        return pd.read_csv(io.BytesIO(obj["Body"].read()))
    else:
        path = os.path.join(LCH_DATA_DIR, fname)
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"No file for report='{report}' cob='{cob}' (looked for {fname})"
            )
        return pd.read_csv(path)


def _df_to_records(df: pd.DataFrame, limit: int) -> list[dict]:
    return df.head(limit).to_dict(orient="records")


# ---------------------------------------------------------------------------
# Tools: REP00036a - SOD Non Cash Collateral Holdings
# ---------------------------------------------------------------------------
@mcp.tool
def get_collateral_holdings(
    member: str,
    cob: Optional[str] = None,
    account: Optional[str] = None,
    collateral_type: Optional[str] = None,
    currency: Optional[str] = None,
    limit: int = 500,
) -> dict:
    """
    Return SOD non-cash collateral holdings (REP00036a) for a single clearing member.

    In production this is the member-scoped tool: a token would be bound to one
    member and this parameter would be derived from the token, NOT free text.

    Args:
        member: Clearing member mnemonic, e.g. 'JPM', 'DBK'.
        cob: Close-of-business date, ISO 'YYYY-MM-DD'. Defaults to the latest available.
        account: Optional account filter, e.g. 'HOUSE', 'CLIENT-ISA', 'CUST-SEG'.
        collateral_type: Optional 'SECURITY' or 'TRIPARTY'.
        currency: Optional ISO currency filter, e.g. 'EUR'.
        limit: Max rows to return.

    Returns:
        dict with 'holdings' (list of rows) and 'summary' (count + total cover value).
    """
    cob = _resolve_cob(cob)
    df = _load("sod_collateral", cob)
    df = df[df["scmmnemonic"] == member.upper()]
    if account:
        df = df[df["account"] == account.upper()]
    if collateral_type:
        df = df[df["collateraltype"] == collateral_type.upper()]
    if currency:
        df = df[df["currency"] == currency.upper()]

    total_cover = float(df["cover"].sum()) if not df.empty else 0.0
    return {
        "report": "REP00036a SOD Non Cash Collateral Holdings",
        "cob": cob,
        "member": member.upper(),
        "summary": {
            "line_count": int(len(df)),
            "total_cover_value": round(total_cover, 2),
            "note": "cover = post-haircut, post-price collateral value",
        },
        "holdings": _df_to_records(df, limit),
    }


@mcp.tool
def get_collateral_summary_by_member(cob: Optional[str] = None) -> dict:
    """
    Aggregate total post-haircut collateral 'cover' value per clearing member
    for a COB (defaults to the latest available). Useful cross-member view (would
    be an LCH-internal / risk scope in production, not a member-facing one).
    """
    cob = _resolve_cob(cob)
    df = _load("sod_collateral", cob)
    agg = (
        df.groupby("scmmnemonic")
        .agg(line_count=("ISIN", "count"),
             total_cover=("cover", "sum"),
             securities=("collateraltype", lambda s: int((s == "SECURITY").sum())),
             triparty=("collateraltype", lambda s: int((s == "TRIPARTY").sum())))
        .reset_index()
        .sort_values("total_cover", ascending=False)
    )
    agg["total_cover"] = agg["total_cover"].round(2)
    return {"report": "REP00036a summary by member", "cob": cob,
            "members": agg.to_dict(orient="records")}


# ---------------------------------------------------------------------------
# Tools: DPG SwapClear registered volume
# ---------------------------------------------------------------------------
@mcp.tool
def get_swapclear_volume(
    cob: Optional[str] = None,
    currency: Optional[str] = None,
    product: Optional[str] = None,
    index: Optional[str] = None,
    tenor_bucket: Optional[str] = None,
    counterparty_type: Optional[str] = None,
    limit: int = 500,
) -> dict:
    """
    Query DPG daily SwapClear registered volume, filtered by any combination of
    currency / product / index / residual tenor bucket / counterparty type.

    Metrics returned per row: trade_count, notional, dv01.

    Args:
        cob: Close-of-business date, ISO 'YYYY-MM-DD'. Defaults to the latest available.
        currency: e.g. 'USD', 'EUR', 'GBP'.
        product: e.g. 'IRS', 'OIS', 'BASIS', 'INFLATION', 'FRA', 'VNS'.
        index: e.g. 'SOFR', 'ESTR', 'SONIA'.
        tenor_bucket: e.g. '0-2Y', '2-5Y', '5-10Y', '10-15Y', '15-30Y', '30Y+'.
        counterparty_type: 'DEALER' or 'CLIENT'.
        limit: Max rows to return.
    """
    cob = _resolve_cob(cob)
    df = _load("dpg_volume", cob)
    if currency:
        df = df[df["currency"] == currency.upper()]
    if product:
        df = df[df["product"] == product.upper()]
    if index:
        df = df[df["index"] == index.upper()]
    if tenor_bucket:
        df = df[df["residual_tenor_bucket"] == tenor_bucket.upper()]
    if counterparty_type:
        df = df[df["counterparty_type"] == counterparty_type.upper()]

    return {
        "report": "DPG SwapClear Registered Volume",
        "cob": cob,
        "summary": {
            "row_count": int(len(df)),
            "total_trade_count": int(df["trade_count"].sum()) if not df.empty else 0,
            "total_notional": round(float(df["notional"].sum()), 2) if not df.empty else 0.0,
            "total_dv01": round(float(df["dv01"].sum()), 2) if not df.empty else 0.0,
        },
        "rows": _df_to_records(df, limit),
    }


# ---------------------------------------------------------------------------
# Tools: EOD Volume Vanilla Swaps
# ---------------------------------------------------------------------------
@mcp.tool
def get_eod_vanilla_outstanding(
    cob: Optional[str] = None,
    currency: Optional[str] = None,
    product: Optional[str] = None,
    index: Optional[str] = None,
    tenor_bucket: Optional[str] = None,
    limit: int = 500,
) -> dict:
    """
    Query EOD vanilla swap notional OUTSTANDING (the standing cleared book).
    Vanilla = IRS / OIS. Metrics: open_trade_count, outstanding_notional, dv01.
    COB defaults to the latest available.
    """
    cob = _resolve_cob(cob)
    df = _load("eod_vanilla", cob)
    if currency:
        df = df[df["currency"] == currency.upper()]
    if product:
        df = df[df["product"] == product.upper()]
    if index:
        df = df[df["index"] == index.upper()]
    if tenor_bucket:
        df = df[df["residual_tenor_bucket"] == tenor_bucket.upper()]

    return {
        "report": "EOD Volume Vanilla Swaps (outstanding)",
        "cob": cob,
        "summary": {
            "row_count": int(len(df)),
            "total_open_trades": int(df["open_trade_count"].sum()) if not df.empty else 0,
            "total_outstanding_notional": round(float(df["outstanding_notional"].sum()), 2) if not df.empty else 0.0,
            "total_dv01": round(float(df["dv01"].sum()), 2) if not df.empty else 0.0,
        },
        "rows": _df_to_records(df, limit),
    }


# ---------------------------------------------------------------------------
# Discovery helpers (cheap tools that let an agent learn valid filter values)
# ---------------------------------------------------------------------------
@mcp.tool
def list_dimensions(cob: Optional[str] = None) -> dict:
    """
    List the distinct filter values available across the reports for a COB
    (defaults to the latest available), so a client/agent can construct valid
    queries without guessing.
    """
    cob = _resolve_cob(cob)
    out = {"cob": cob}
    try:
        c = _load("sod_collateral", cob)
        out["collateral"] = {
            "members": sorted(c["scmmnemonic"].unique().tolist()),
            "accounts": sorted(c["account"].unique().tolist()),
            "collateral_types": sorted(c["collateraltype"].unique().tolist()),
            "currencies": sorted(c["currency"].unique().tolist()),
        }
    except FileNotFoundError:
        out["collateral"] = None
    try:
        v = _load("dpg_volume", cob)
        out["swapclear_volume"] = {
            "currencies": sorted(v["currency"].unique().tolist()),
            "products": sorted(v["product"].unique().tolist()),
            "indices": sorted(v["index"].unique().tolist()),
            "tenor_buckets": sorted(v["residual_tenor_bucket"].unique().tolist()),
            "counterparty_types": sorted(v["counterparty_type"].unique().tolist()),
        }
    except FileNotFoundError:
        out["swapclear_volume"] = None
    return out


# ---------------------------------------------------------------------------
# Live-data tools (call the in-process APIs; real public sources, not the CSVs)
# ---------------------------------------------------------------------------
@mcp.tool
def list_rates() -> dict:
    """List the reference risk-free rates available live (SOFR, ESTR, SONIA) with their
    source and currency. These are the RFR indices used by SwapClear."""
    return {"rates": _api_get("/rates")}


@mcp.tool
def get_reference_rate(rate: str) -> dict:
    """Latest published value of a reference risk-free rate.

    Args:
        rate: 'SOFR' (USD, NY Fed), 'ESTR' (EUR, ECB), or 'SONIA' (GBP, Bank of England).
    """
    return _api_get(f"/rates/{rate.upper()}/latest")


@mcp.tool
def get_rate_history(rate: str, start: Optional[str] = None, end: Optional[str] = None) -> dict:
    """Historical observations of a reference rate.

    Args:
        rate: 'SOFR', 'ESTR', or 'SONIA'.
        start: ISO start date 'YYYY-MM-DD' (optional).
        end: ISO end date 'YYYY-MM-DD' (optional).
    """
    params = {}
    if start:
        params["start"] = start
    if end:
        params["end"] = end
    return _api_get(f"/rates/{rate.upper()}/history", params)


@mcp.tool
def lookup_legal_entity(name: str, limit: int = 10) -> dict:
    """Search the GLEIF Global LEI index for legal entities by (part of) name — use this to
    resolve a clearing member / counterparty to its LEI and registration details.

    Args:
        name: Full or partial legal entity name, e.g. 'LCH' or 'JPMorgan'.
        limit: Max records to return (1-200).
    """
    return _api_get("/entities/search", {"name": name, "limit": limit})


@mcp.tool
def get_legal_entity(lei: str) -> dict:
    """Fetch one legal entity's LEI record (name, status, jurisdiction, legal address)."""
    return _api_get(f"/entities/{lei}")


@mcp.tool
def get_entity_relationships(lei: str) -> dict:
    """Corporate hierarchy (direct and ultimate parent) of a legal entity, from GLEIF."""
    return _api_get(f"/entities/{lei}/relationships")


# A read-only MCP *resource* (as opposed to a tool) exposing the data dictionary.
@mcp.resource("lch://data-dictionary")
def data_dictionary() -> str:
    """Human-readable schema for the three reports."""
    return (
        "REP00036a SOD Non Cash Collateral Holdings columns:\n"
        "  cobdate, scmmnemonic, account, currency, collateraltype, ISIN, units,\n"
        "  haircut, price, cover, factor, expirydate, custodian\n"
        "  (cover = units*factor*(price/100)*(1-haircut))\n\n"
        "DPG SwapClear Registered Volume columns:\n"
        "  cobdate, currency, product, index, residual_tenor_bucket,\n"
        "  counterparty_type, trade_count, notional, dv01\n\n"
        "EOD Volume Vanilla Swaps columns:\n"
        "  cobdate, currency, product, index, residual_tenor_bucket,\n"
        "  open_trade_count, outstanding_notional, dv01\n"
    )


# ---------------------------------------------------------------------------
# In-process data APIs
# ---------------------------------------------------------------------------
def _start_api_server() -> None:
    """Launch the FastAPI data APIs on 127.0.0.1:API_PORT in a daemon thread, so the
    MCP tools can reach them over localhost within the same container. Only the MCP
    port is exposed externally."""
    import socket
    import threading
    import time as _time

    import uvicorn

    from apis.app import app as api_app

    def _run():
        uvicorn.run(api_app, host="127.0.0.1", port=API_PORT, log_level="warning")

    threading.Thread(target=_run, daemon=True, name="data-apis").start()

    # Wait briefly for the port to accept connections before serving MCP.
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", API_PORT), timeout=0.2):
                return
        except OSError:
            _time.sleep(0.1)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "stdio").lower()
    _start_api_server()  # start the rates + entities APIs alongside the MCP
    if transport == "http":
        port = int(os.environ.get("MCP_PORT", "8080"))
        # Streamable HTTP transport - the right choice for a remote,
        # multi-client, containerised deployment (vs stdio for local).
        mcp.run(transport="http", host="0.0.0.0", port=port)
    else:
        mcp.run()  # stdio
