"""Governed API facade over public, LCH-relevant data sources for the PoC.

Two APIs live here:
  - rates    : reference risk-free rates (SOFR / ESTR / SONIA) — the RFR indices
               used by SwapClear, fronted from NY Fed, ECB and Bank of England.
  - entities : legal-entity / counterparty data from the GLEIF Global LEI index.

The MCP server calls these over localhost; the MCP layer never talks to the
upstream sources directly.
"""
