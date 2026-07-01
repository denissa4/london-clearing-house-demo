# Data Dictionary — provenance vs real LCH specs

This documents where each field comes from, so you can defend the schema in interview. Layouts are from **public LCH documentation**; values are synthetic.

---

## 1. REP00036a — SOD Non-Cash Collateral Holdings

Field layout taken from the LCH **"Banking Member Reports Text File Formats"** specification (section 1.19, REP00036a). These are the real column names and types LCH uses.

| Column           | Real LCH definition (from spec)                                               | Lab notes |
|------------------|--------------------------------------------------------------------------------|-----------|
| `cobdate`        | DateTime — value date on which the member will be called                      | fixed to the sample COB |
| `scmmnemonic`    | Varchar(11) — three-letter code identifying each clearing member firm to LCH  | e.g. `JPM`, `DBK` |
| `account`        | Varchar(200) — identifies house or client business (EMIR account identifier)  | `HOUSE`, `CLIENT-ISA`, `CLIENT-OSA`, `CUST-SEG` |
| `currency`       | Varchar(10) — denomination of cash from a particular country                  | ISO code |
| `collateraltype` | *(lab-added)* — distinguishes `SECURITY` vs `TRIPARTY`                         | real report separates these by account/instrument; flag added for clarity |
| `ISIN`           | Char(12) — collateral ISIN                                                    | for triparty rows this holds a basket reference instead |
| `units`          | Float — units (nominal) held by LCH                                           | round lots ≥ 1,000,000 |
| `haircut`        | Float — LCH haircut                                                           | ~4% short govvies → ~18% long/MBS, per LCH policy |
| `price`          | Float — price for securities, cash and performance bonds                      | per 100 nominal |
| `cover`          | Float — value of securities **after haircut and pricing**                     | computed: `units*factor*(price/100)*(1-haircut)` |
| `factor`         | Float — pay-down factor (MBS between 0.0–1.0, otherwise 1.0)                   | mostly 1.0 |
| `expirydate`     | DateTime — expiry date                                                        | blank for triparty baskets |
| `custodian`      | Varchar(50) — custodian name                                                  | Euroclear / Clearstream / BNY Mellon (+ triparty agents) |

**Domain facts to know:** haircuts are <cite>calibrated to cover the largest two-day price movement over the past five years with 99.7% confidence</cite>; there is an additional ~4% FX haircut on non-cash securities. Triparty collateral is delivered via an agent (Euroclear/Clearstream/BNY) and around a fifth of LCH's non-cash collateral arrives this way. Triparty transactions have a **1 million GBP/EUR/USD minimum**.

---

## 2. DPG — SwapClear Registered Volume

Dimensions and metrics from the LCH **"SwapClear Volume Data"** factsheet. The factsheet confirms LCH publishes *daily aggregated cleared IRS volumes, segmented by currency, product type, client vs dealer, with metrics notional, trade count and DV01.*

| Column                  | Source / meaning |
|-------------------------|------------------|
| `cobdate`               | close-of-business date |
| `currency`              | one of 28 SwapClear currencies (lab uses 8) |
| `product`               | IRS, OIS, BASIS, INFLATION, FRA, VNS (variable notional) |
| `index`                 | RFR/benchmark by currency: SOFR, ESTR, SONIA, TONA, SARON, CORRA, EURIBOR, FedFunds… (post-LIBOR) |
| `residual_tenor_bucket` | maturity bucket: 0-2Y, 2-5Y, 5-10Y, 10-15Y, 15-30Y, 30Y+ |
| `counterparty_type`     | DEALER vs CLIENT (the factsheet's "client vs dealer" dimension) |
| `trade_count`           | number of registered trades in the slice |
| `notional`              | aggregate notional (the day's registered activity) |
| `dv01`                  | dollar value of 1bp — risk sensitivity metric the factsheet lists |

The factsheet also notes data <cite>can be filtered to exclude inferred portfolio maintenance activity such as intercompany trades, portfolio transfers or compression</cite>, and is <cite>accessible daily from 6am via SFTP or the LCH Portal</cite> — exactly the ingestion boundary this lab models.

---

## 3. EOD Volume Vanilla Swaps

Same dimensions as (2), but the metric is **notional outstanding** — the standing cleared book at end of day, *including results from compression, netting and blending* — rather than the day's new trades. "Vanilla" = plain IRS/OIS on fixed tenor points.

| Column                  | Meaning |
|-------------------------|---------|
| `cobdate`               | close-of-business date |
| `currency` / `product` / `index` / `residual_tenor_bucket` | as above (products limited to IRS, OIS) |
| `open_trade_count`      | number of open/outstanding trades in the slice |
| `outstanding_notional`  | end-of-day notional outstanding |
| `dv01`                  | risk sensitivity of the outstanding book |

---

## Why two volume files, not one?

The JD lists both *registered/outstanding activity* (DPG) **and** *EOD Volume Vanilla Swaps*. These are genuinely two different questions:
- **Registered volume (DPG)** = *flow* — what got cleared today.
- **EOD outstanding** = *stock* — what's on the book at day end.

An MCP client can ask either. Being able to explain flow-vs-stock crisply is a good signal you understand the domain, not just the plumbing.
