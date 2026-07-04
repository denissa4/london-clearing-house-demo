#!/usr/bin/env python3
"""
End-to-end tests for the deployed LCH MCP server (stdlib only — CI-friendly).

Drives the real MCP endpoint over Streamable HTTP (initialize -> initialized ->
tools/call) and asserts on the answers of 22 LCH-relevant questions covering
every feature:

  - Reference Rates API : discovery, latest fixing, pinned-date history
  - Legal Entities API  : GLEIF search, record by LEI, corporate hierarchy
  - CSV report route    : collateral, member summary, SwapClear flow,
                          EOD stock, dimension discovery (pinned COB)

Deterministic checks assert exact values (pinned dates / GLEIF reference data /
seeded synthetic reports); latest-rate checks assert structure only, since the
fixing changes daily.

Usage:
    python scripts/e2e_test.py                     # default: EKS CloudFront URL
    python scripts/e2e_test.py --url http://localhost:8080/mcp
    MCP_URL=https://.../mcp python scripts/e2e_test.py

Exit code 0 = all passed, 1 = failures (for CI).
"""
import argparse
import json
import os
import ssl
import sys
import urllib.request

DEFAULT_URL = "https://d3j87cdpfnkmyh.cloudfront.net/mcp"

# The committed sample reports this suite pins to (seeded, deterministic).
COB = "2026-07-02"

_CTX = ssl.create_default_context()
_HDRS = {"Content-Type": "application/json",
         "Accept": "application/json, text/event-stream"}


class MCPClient:
    """Minimal Streamable-HTTP MCP client (enough for tools/call)."""

    def __init__(self, url: str):
        self.url = url
        self.session_id = None
        self._id = 0

    def _post(self, payload: dict):
        headers = dict(_HDRS)
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(),
                                     headers=headers)
        with urllib.request.urlopen(req, timeout=45, context=_CTX) as r:
            sid = r.headers.get("mcp-session-id")
            if sid:
                self.session_id = sid
            body = r.read().decode()
        for line in body.splitlines():          # SSE frames: "data: {...}"
            if line.startswith("data: "):
                return json.loads(line[6:])
        return json.loads(body) if body.strip() else None

    def handshake(self):
        self._id += 1
        resp = self._post({"jsonrpc": "2.0", "id": self._id, "method": "initialize",
                           "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                      "clientInfo": {"name": "lch-e2e", "version": "1"}}})
        assert "result" in resp, f"initialize failed: {resp}"
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return resp["result"]

    def call(self, tool: str, args: dict) -> dict:
        self._id += 1
        resp = self._post({"jsonrpc": "2.0", "id": self._id, "method": "tools/call",
                           "params": {"name": tool, "arguments": args}})
        result = resp.get("result") or {}
        if result.get("isError"):
            raise AssertionError(f"tool error: {result}")
        text = next(b["text"] for b in result["content"] if b.get("type") == "text")
        return json.loads(text)


# ---------------------------------------------------------------------------
# Assertions (each returns nothing; raises AssertionError on failure)
# ---------------------------------------------------------------------------
def approx(a, b, tol=0.01):
    assert abs(float(a) - float(b)) <= tol, f"{a} != {b} (±{tol})"


def is_number(x):
    assert isinstance(x, (int, float)) and not isinstance(x, bool), f"not a number: {x!r}"


# Each test: (feature, question, tool, args, check(result))
def build_tests():
    T = []

    # ---- Rates: discovery -------------------------------------------------
    def chk_rates_list(r):
        ids = sorted(x["rate_id"] for x in r["rates"])
        assert ids == ["ESTR", "SOFR", "SONIA"], ids
    T.append(("rates/discovery", "Which reference risk-free rates can you provide live?",
              "list_rates", {}, chk_rates_list))

    def chk_rates_sources(r):
        src = {x["rate_id"]: x["source"] for x in r["rates"]}
        assert src["SOFR"] == "Federal Reserve Bank of New York", src
        assert src["ESTR"] == "European Central Bank", src
        assert src["SONIA"] == "Bank of England", src
    T.append(("rates/discovery", "Which RFR indices used by SwapClear are available, and who publishes each?",
              "list_rates", {}, chk_rates_sources))

    # ---- Rates: latest (structural only — value changes daily) ------------
    def chk_sofr_latest(r):
        assert r["rate_id"] == "SOFR" and r["currency"] == "USD", r
        assert r["source"] == "Federal Reserve Bank of New York", r
        is_number(r["value"]); is_number(r["volume_billions"])
        assert r["date"] >= "2026-01-01", r["date"]
    T.append(("rates/latest", "What is the latest SOFR rate?",
              "get_reference_rate", {"rate": "SOFR"}, chk_sofr_latest))

    def chk_sonia_latest(r):
        assert r["rate_id"] == "SONIA" and r["currency"] == "GBP", r
        assert r["source"] == "Bank of England", r
        is_number(r["value"])
    T.append(("rates/latest", "What's the current SONIA fixing from the Bank of England?",
              "get_reference_rate", {"rate": "SONIA"}, chk_sonia_latest))

    # ---- Rates: pinned history (deterministic) ----------------------------
    def chk_sofr_hist(r):
        obs = r["observations"]
        assert len(obs) == 1 and obs[0]["date"] == "2026-06-30", obs
        approx(obs[0]["value"], 3.68)
        approx(obs[0]["volume_billions"], 3418, tol=1)
    T.append(("rates/history", "What was SOFR on 30 June 2026?",
              "get_rate_history", {"rate": "SOFR", "start": "2026-06-30", "end": "2026-06-30"},
              chk_sofr_hist))

    def chk_estr_hist(r):
        obs = r["observations"]
        assert len(obs) == 7, f"expected 7 obs, got {len(obs)}"
        assert obs[0]["date"] == "2026-06-22", obs[0]
        approx(obs[0]["value"], 2.181)
        assert obs[-1]["date"] == "2026-06-30", obs[-1]
        approx(obs[-1]["value"], 2.182)
    T.append(("rates/history", "Show €STR daily fixings from 22 to 30 June 2026.",
              "get_rate_history", {"rate": "ESTR", "start": "2026-06-22", "end": "2026-06-30"},
              chk_estr_hist))

    # ---- Entities: search --------------------------------------------------
    def chk_lookup_lch(r):
        first = r["results"][0]
        assert first["lei"] == "969500GMCIARBCKBKS83", first
        assert first["name"] == "LCH", first
    T.append(("entities/search", "Find the LEI for LCH.",
              "lookup_legal_entity", {"name": "LCH", "limit": 3}, chk_lookup_lch))

    def chk_lookup_lch_ltd(r):
        hit = next((e for e in r["results"] if e["lei"] == "F226TOH6YD6XJB17KS62"), None)
        assert hit, f"LCH LIMITED not found in {[(e['lei'], e['name']) for e in r['results']]}"
        assert hit["name"] == "LCH LIMITED" and hit["status"] == "ACTIVE", hit
    T.append(("entities/search", "Search the LEI register for 'LCH Limited' — is it active?",
              "lookup_legal_entity", {"name": "LCH Limited", "limit": 5}, chk_lookup_lch_ltd))

    # ---- Entities: record by LEI -------------------------------------------
    def chk_lch_sa(r):
        assert r["name"] == "LCH" and r["status"] == "ACTIVE", r
        assert r["jurisdiction"] == "FR", r
    T.append(("entities/record", "What entity is LEI 969500GMCIARBCKBKS83, and where is it registered?",
              "get_legal_entity", {"lei": "969500GMCIARBCKBKS83"}, chk_lch_sa))

    def chk_dbk(r):
        assert r["name"] == "DEUTSCHE BANK AKTIENGESELLSCHAFT", r
        assert r["status"] == "ACTIVE" and r["jurisdiction"] == "DE", r
    T.append(("entities/record", "Look up LEI 7LTWFZYICNSX8D621K86.",
              "get_legal_entity", {"lei": "7LTWFZYICNSX8D621K86"}, chk_dbk))

    # ---- Entities: hierarchy -----------------------------------------------
    def chk_rel_lch_ltd(r):
        dp = (r.get("direct_parent") or {}).get("name")
        up = (r.get("ultimate_parent") or {}).get("name")
        assert dp == "LCH GROUP HOLDINGS LIMITED", dp
        assert up == "LONDON STOCK EXCHANGE GROUP PLC", up
    T.append(("entities/hierarchy", "Who are the direct and ultimate parents of LCH Limited?",
              "get_entity_relationships", {"lei": "F226TOH6YD6XJB17KS62"}, chk_rel_lch_ltd))

    def chk_rel_lch_sa(r):
        assert r.get("direct_parent") is None, r
        assert r.get("ultimate_parent") is None, r
    T.append(("entities/hierarchy", "Does LCH SA report a parent in GLEIF? (expected: none)",
              "get_entity_relationships", {"lei": "969500GMCIARBCKBKS83"}, chk_rel_lch_sa))

    # ---- CSV: collateral holdings (pinned COB, seeded data) ----------------
    def chk_coll_jpm(r):
        s = r["summary"]
        assert s["line_count"] == 4, s
        approx(s["total_cover_value"], 45791538.00)
    T.append(("reports/collateral", f"What non-cash collateral does JPM hold as of COB {COB}?",
              "get_collateral_holdings", {"member": "JPM", "cob": COB}, chk_coll_jpm))

    def chk_coll_dbk(r):
        s = r["summary"]
        assert s["line_count"] == 2, s
        approx(s["total_cover_value"], 6459740.00)
    T.append(("reports/collateral", f"Show DBK's EUR security collateral for COB {COB}.",
              "get_collateral_holdings",
              {"member": "DBK", "cob": COB, "currency": "EUR", "collateral_type": "SECURITY"},
              chk_coll_dbk))

    # ---- CSV: collateral summary -------------------------------------------
    def chk_summary_top(r):
        top = r["members"][0]
        assert top["scmmnemonic"] == "BRC", top
        approx(top["total_cover"], 70340340.00)
    T.append(("reports/summary", f"Which member posted the most collateral cover on {COB}?",
              "get_collateral_summary_by_member", {"cob": COB}, chk_summary_top))

    def chk_summary_all(r):
        assert len(r["members"]) == 8, len(r["members"])
        covers = [m["total_cover"] for m in r["members"]]
        assert covers == sorted(covers, reverse=True), "not sorted desc"
    T.append(("reports/summary", f"Summarise total collateral per member for COB {COB}.",
              "get_collateral_summary_by_member", {"cob": COB}, chk_summary_all))

    # ---- CSV: SwapClear flow -------------------------------------------------
    def chk_vol_usd(r):
        s = r["summary"]
        assert s["row_count"] == 2 and s["total_trade_count"] == 750, s
        approx(s["total_notional"], 150246048778.34)
    T.append(("reports/flow", f"How much USD SOFR IRS was registered in the 5-10Y bucket on {COB}?",
              "get_swapclear_volume",
              {"cob": COB, "currency": "USD", "product": "IRS", "index": "SOFR",
               "tenor_bucket": "5-10Y"}, chk_vol_usd))

    def chk_vol_gbp(r):
        s = r["summary"]
        assert s["row_count"] == 7 and s["total_trade_count"] == 3100, s
        approx(s["total_notional"], 305780664489.27)
    T.append(("reports/flow", f"What was total GBP SONIA OIS registered volume on {COB}?",
              "get_swapclear_volume",
              {"cob": COB, "currency": "GBP", "product": "OIS", "index": "SONIA"}, chk_vol_gbp))

    # ---- CSV: EOD stock -------------------------------------------------------
    def chk_eod_eur(r):
        s = r["summary"]
        assert s["row_count"] == 11 and s["total_open_trades"] == 61906, s
        approx(s["total_outstanding_notional"], 10468800208328.82)
    T.append(("reports/stock", f"What's the outstanding notional for EUR OIS at EOD {COB}?",
              "get_eod_vanilla_outstanding", {"cob": COB, "currency": "EUR", "product": "OIS"},
              chk_eod_eur))

    def chk_eod_usd(r):
        s = r["summary"]
        assert s["row_count"] == 1 and s["total_open_trades"] == 1808, s
        approx(s["total_outstanding_notional"], 674045617407.53)
    T.append(("reports/stock", f"Show the standing book for USD IRS in the 30Y+ bucket at COB {COB}.",
              "get_eod_vanilla_outstanding",
              {"cob": COB, "currency": "USD", "product": "IRS", "tenor_bucket": "30Y+"},
              chk_eod_usd))

    # ---- CSV: discovery --------------------------------------------------------
    def chk_dim_members(r):
        assert r["collateral"]["members"] == ["ABC", "BRC", "CTI", "DBK", "HSB", "JPM", "MSL", "SGL"], \
            r["collateral"]["members"]
    T.append(("reports/discovery", f"Which clearing members appear in the collateral report for {COB}?",
              "list_dimensions", {"cob": COB}, chk_dim_members))

    def chk_dim_buckets(r):
        assert sorted(r["swapclear_volume"]["tenor_buckets"]) == \
            sorted(["0-2Y", "2-5Y", "5-10Y", "10-15Y", "15-30Y", "30Y+"]), \
            r["swapclear_volume"]["tenor_buckets"]
    T.append(("reports/discovery", "What tenor buckets exist in the SwapClear volume data?",
              "list_dimensions", {"cob": COB}, chk_dim_buckets))

    return T


def main() -> int:
    ap = argparse.ArgumentParser(description="E2E tests against a deployed LCH MCP server.")
    ap.add_argument("--url", default=os.environ.get("MCP_URL", DEFAULT_URL),
                    help=f"MCP endpoint (default: $MCP_URL or {DEFAULT_URL})")
    args = ap.parse_args()

    client = MCPClient(args.url)
    print(f"endpoint : {args.url}")
    try:
        info = client.handshake()
        print(f"server   : {info['serverInfo']['name']} {info['serverInfo'].get('version', '')}\n")
    except Exception as e:
        print(f"FATAL: MCP handshake failed: {e}")
        return 1

    passed = failed = 0
    for feature, question, tool, targs, check in build_tests():
        label = f"[{feature}] {question}"
        try:
            check(client.call(tool, targs))
            print(f"PASS  {label}")
            passed += 1
        except Exception as e:
            print(f"FAIL  {label}\n      -> {e}")
            failed += 1

    print(f"\n{passed} passed, {failed} failed ({passed + failed} total)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
