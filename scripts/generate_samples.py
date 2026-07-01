#!/usr/bin/env python3
"""
Generate realistic sample data for the three LCH reports in scope.

Schemas are modelled on the *real* LCH specifications:

1. REP00036a - SOD Non Cash Collateral Holdings
   Field layout taken from the LCH "Banking Member Reports Text File Formats"
   spec (cobdate, scmmnemonic, account, currency, ISIN, units, haircut, price,
   cover, factor, expirydate, custodian, ...). Securities + triparty.

2. DPG - SwapClear registered/outstanding activity trade volume
   Dimensions/metrics from the LCH "SwapClear Volume Data" factsheet:
   currency, product, index, residual tenor bucket, client-vs-dealer,
   notional, trade count, DV01.

3. EOD Volume Vanilla Swaps
   End-of-day notional outstanding, same dimensions as (2).

These are SYNTHETIC values for a training lab. ISINs, members, notionals are
invented. Do not treat as real LCH data.
"""

import csv
import random
from datetime import datetime, timedelta
from pathlib import Path

random.seed(42)  # reproducible

OUT = Path(__file__).resolve().parent.parent / "data"
OUT.mkdir(exist_ok=True)

COB = datetime(2026, 6, 30)  # a Tuesday - our close-of-business date
COB_STR = COB.strftime("%Y-%m-%d")
COB_COMPACT = COB.strftime("%Y%m%d")

# ---------------------------------------------------------------------------
# Reference data (synthetic but plausible)
# ---------------------------------------------------------------------------

# Three-letter clearing member mnemonics (scmmnemonic is Varchar(11) in spec,
# but the classic LCH member codes are three letters).
MEMBERS = ["ABC", "DBK", "JPM", "BRC", "HSB", "SGL", "MSL", "CTI"]

# account: identifies house or client business (spec note). We model a house
# account and a couple of client/segregated accounts per member.
ACCOUNTS = ["HOUSE", "CLIENT-ISA", "CLIENT-OSA", "CUST-SEG"]

# Sovereign government bonds used as non-cash collateral, with plausible
# (synthetic) ISINs, the issuer currency, and an approximate LCH-style haircut.
# Haircuts: shorter/higher-quality -> smaller; longer -> larger. LCH's real
# haircuts range from ~4% short govvies to ~18% GNMA MBS.
SECURITIES = [
    # ISIN,          desc,                        ccy,   base_haircut, custodian
    ("DE0001102580", "German Bund 0% 2027",       "EUR", 0.041, "Euroclear Bank"),
    ("DE0001102614", "German Bund 1.7% 2032",      "EUR", 0.058, "Clearstream"),
    ("FR0013451507", "French OAT 0.5% 2029",       "EUR", 0.049, "Euroclear Bank"),
    ("GB00BMBL1D50", "UK Gilt 0.25% 2028",         "GBP", 0.045, "Euroclear UK"),
    ("GB00BLPK7110", "UK Gilt 4.25% 2034",         "GBP", 0.071, "Euroclear UK"),
    ("US912828YV63", "US Treasury Note 1.5% 2030", "USD", 0.052, "BNY Mellon"),
    ("US91282CGT58", "US Treasury Note 4% 2033",   "USD", 0.068, "BNY Mellon"),
    ("US912810TM08", "US Treasury Bond 3.6% 2053", "USD", 0.142, "BNY Mellon"),
    ("NL0015000B02", "Dutch DSL 0% 2028",          "EUR", 0.047, "Euroclear Bank"),
    ("CA135087P204", "Canada Govt 2.25% 2029",     "CAD", 0.061, "BNY Mellon"),
]

# Triparty holdings: instead of a specific ISIN, triparty is delivered as a
# basket via a triparty agent. We model these as a basket reference.
TRIPARTY_BASKETS = [
    # basket_ref,      ccy,   base_haircut, agent
    ("TPB-EUR-GOVT-1", "EUR", 0.055, "Euroclear (Triparty)"),
    ("TPB-USD-UST-1",  "USD", 0.060, "BNY Mellon (Triparty)"),
    ("TPB-GBP-GILT-1", "GBP", 0.052, "Euroclear (Triparty)"),
]

CURRENCIES = ["USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "SEK"]

# SwapClear product / index reference. Post-LIBOR the indices are RFRs.
PRODUCTS = ["IRS", "OIS", "BASIS", "INFLATION", "FRA", "VNS"]
INDEX_BY_CCY = {
    "USD": ["SOFR", "FedFunds"],
    "EUR": ["ESTR", "EURIBOR"],
    "GBP": ["SONIA"],
    "JPY": ["TONA"],
    "CHF": ["SARON"],
    "CAD": ["CORRA"],
    "AUD": ["AONIA", "BBSW"],
    "SEK": ["SWESTR"],
}

# Residual tenor buckets used by the SwapClear volume data
TENOR_BUCKETS = ["0-2Y", "2-5Y", "5-10Y", "10-15Y", "15-30Y", "30Y+"]

COUNTERPARTY_TYPE = ["DEALER", "CLIENT"]


def r2(x):  # round to 2dp
    return round(x, 2)


# ---------------------------------------------------------------------------
# 1. REP00036a - SOD Non Cash Collateral Holdings
# ---------------------------------------------------------------------------
def gen_sod_non_cash_collateral():
    """
    Columns follow the LCH REP00036a text-file spec:
      cobdate, scmmnemonic, account, currency, ISIN, units, haircut,
      price, cover, factor, expirydate, custodian
    'cover' = value of securities after haircut and pricing.
    'factor' = pay-down factor (1.0 for non-MBS).
    We add collateraltype (SECURITY/TRIPARTY) to distinguish the two - the real
    report separates these via account/instrument but a flag makes the lab clearer.
    """
    fname = OUT / f"REP00036a_SOD_NonCashCollateralHoldings_{COB_COMPACT}.csv"
    rows = []
    for member in MEMBERS:
        # Each member holds a random handful of securities across some accounts.
        n_lines = random.randint(4, 9)
        for _ in range(n_lines):
            account = random.choice(ACCOUNTS)

            if random.random() < 0.25:
                # Triparty basket line
                basket, ccy, base_hc, agent = random.choice(TRIPARTY_BASKETS)
                isin = basket  # triparty uses a basket ref, not a single ISIN
                coll_type = "TRIPARTY"
                custodian = agent
                # price for a basket modelled as ~ par (per 100 nominal)
                price = r2(random.uniform(97.0, 101.5))
                factor = 1.0
                expiry = ""  # baskets roll; no single expiry
            else:
                isin, desc, ccy, base_hc, custodian = random.choice(SECURITIES)
                coll_type = "SECURITY"
                price = r2(random.uniform(88.0, 106.0))
                # MBS-style paydown factor occasionally < 1
                factor = 1.0 if random.random() > 0.1 else r2(random.uniform(0.4, 0.95))
                # expiry date somewhere 2027-2053
                yrs = random.randint(1, 27)
                expiry = (COB + timedelta(days=365 * yrs)).strftime("%Y-%m-%d")

            # haircut jitter around the security's base haircut
            haircut = r2(min(0.30, max(0.03, base_hc + random.uniform(-0.005, 0.01))))

            # units = nominal held (round lots, min 1,000,000 flavour)
            units = random.choice([1_000_000, 2_000_000, 5_000_000, 10_000_000,
                                    25_000_000, 3_000_000, 7_500_000])

            # cover = nominal * factor * (price/100) * (1 - haircut)
            cover = r2(units * factor * (price / 100.0) * (1.0 - haircut))

            rows.append({
                "cobdate": COB_STR,
                "scmmnemonic": member,
                "account": account,
                "currency": ccy,
                "collateraltype": coll_type,
                "ISIN": isin,
                "units": units,
                "haircut": haircut,
                "price": price,
                "cover": cover,
                "factor": factor,
                "expirydate": expiry,
                "custodian": custodian,
            })

    cols = ["cobdate", "scmmnemonic", "account", "currency", "collateraltype",
            "ISIN", "units", "haircut", "price", "cover", "factor",
            "expirydate", "custodian"]
    with open(fname, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    print(f"  {fname.name}: {len(rows)} rows")
    return fname


# ---------------------------------------------------------------------------
# 2. DPG - SwapClear registered activity (daily volume)
# ---------------------------------------------------------------------------
def gen_dpg_registered_volume():
    """
    Daily registered/cleared volume, aggregated by the SwapClear volume-data
    dimensions: cob date, currency, product, index, residual tenor bucket,
    counterparty type (dealer/client). Metrics: trade_count, notional, dv01.
    """
    fname = OUT / f"DPG_SwapClear_RegisteredVolume_{COB_COMPACT}.csv"
    rows = []
    for ccy in CURRENCIES:
        for product in PRODUCTS:
            for index in INDEX_BY_CCY[ccy]:
                for bucket in TENOR_BUCKETS:
                    for cptype in COUNTERPARTY_TYPE:
                        # sparsity: not every combination trades every day
                        if random.random() < 0.45:
                            continue
                        trade_count = random.randint(1, 850)
                        # notional scales with count and a per-trade size
                        avg_ticket = random.uniform(5e6, 250e6)
                        notional = r2(trade_count * avg_ticket)
                        # DV01 ~ notional * duration factor by bucket
                        dur = {"0-2Y": 0.00008, "2-5Y": 0.00025, "5-10Y": 0.0006,
                               "10-15Y": 0.0011, "15-30Y": 0.0019, "30Y+": 0.0028}[bucket]
                        dv01 = r2(notional * dur)
                        rows.append({
                            "cobdate": COB_STR,
                            "currency": ccy,
                            "product": product,
                            "index": index,
                            "residual_tenor_bucket": bucket,
                            "counterparty_type": cptype,
                            "trade_count": trade_count,
                            "notional": notional,
                            "dv01": dv01,
                        })

    cols = ["cobdate", "currency", "product", "index", "residual_tenor_bucket",
            "counterparty_type", "trade_count", "notional", "dv01"]
    with open(fname, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    print(f"  {fname.name}: {len(rows)} rows")
    return fname


# ---------------------------------------------------------------------------
# 3. EOD Volume Vanilla Swaps (notional outstanding)
# ---------------------------------------------------------------------------
def gen_eod_vanilla_swaps():
    """
    End-of-day notional OUTSTANDING for vanilla swaps (the standing book, not
    the day's new trades). Same dimensions; metric is outstanding notional +
    open trade count + dv01. Vanilla = IRS/OIS on fixed tenor points.
    """
    fname = OUT / f"EOD_Volume_VanillaSwaps_{COB_COMPACT}.csv"
    rows = []
    vanilla_products = ["IRS", "OIS"]
    for ccy in CURRENCIES:
        for product in vanilla_products:
            for index in INDEX_BY_CCY[ccy]:
                for bucket in TENOR_BUCKETS:
                    if random.random() < 0.2:
                        continue
                    open_trades = random.randint(50, 12000)
                    avg_ticket = random.uniform(20e6, 400e6)
                    outstanding_notional = r2(open_trades * avg_ticket)
                    dur = {"0-2Y": 0.00008, "2-5Y": 0.00025, "5-10Y": 0.0006,
                           "10-15Y": 0.0011, "15-30Y": 0.0019, "30Y+": 0.0028}[bucket]
                    dv01 = r2(outstanding_notional * dur)
                    rows.append({
                        "cobdate": COB_STR,
                        "currency": ccy,
                        "product": product,
                        "index": index,
                        "residual_tenor_bucket": bucket,
                        "open_trade_count": open_trades,
                        "outstanding_notional": outstanding_notional,
                        "dv01": dv01,
                    })

    cols = ["cobdate", "currency", "product", "index", "residual_tenor_bucket",
            "open_trade_count", "outstanding_notional", "dv01"]
    with open(fname, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    print(f"  {fname.name}: {len(rows)} rows")
    return fname


if __name__ == "__main__":
    print(f"Generating LCH sample reports for COB {COB_STR} into {OUT}/")
    gen_sod_non_cash_collateral()
    gen_dpg_registered_volume()
    gen_eod_vanilla_swaps()
    print("Done.")
