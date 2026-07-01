#!/usr/bin/env python3
"""
Smoke-test the MCP tools directly (no transport) against the sample data.
Proves the query logic works before you wire up a client or deploy.
"""
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "mcp_server"))
import server  # noqa: E402


def show(title, obj):
    print(f"\n{'='*70}\n{title}\n{'='*70}")
    print(json.dumps(obj, indent=2, default=str)[:1600])


def unwrap(tool):
    """FastMCP decorates functions into Tool objects; get the raw callable."""
    return getattr(tool, "fn", tool)


show("list_dimensions()", unwrap(server.list_dimensions)())

show("get_collateral_holdings(member='ABC')",
     unwrap(server.get_collateral_holdings)(member="ABC"))

show("get_collateral_summary_by_member()",
     unwrap(server.get_collateral_summary_by_member)())

show("get_swapclear_volume(currency='USD', product='IRS', tenor_bucket='5-10Y')",
     unwrap(server.get_swapclear_volume)(currency="USD", product="IRS", tenor_bucket="5-10Y"))

show("get_eod_vanilla_outstanding(currency='EUR', product='OIS')",
     unwrap(server.get_eod_vanilla_outstanding)(currency="EUR", product="OIS"))

print("\nAll tool calls executed successfully.")
