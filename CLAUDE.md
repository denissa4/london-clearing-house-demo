# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A miniature recreation of the LCH (London Clearing House) "platform shift": LCH
publishes static clearing reports (CSV/PDF) over SFTP; this lab puts a **FastMCP
server in front of that data** so members/AI agents query it programmatically via
typed, discoverable MCP tools. All data is **synthetic** — field *layouts* follow
public LCH specs, but ISINs, members, prices and notionals are invented.

Three reports are modelled (see `docs/DATA_DICTIONARY.md` for field-by-field
provenance against real LCH specs):

- **REP00036a** — SOD Non-Cash Collateral Holdings (securities + triparty)
- **DPG SwapClear Registered Volume** — *flow*: what got cleared today
- **EOD Volume Vanilla Swaps** — *stock*: standing outstanding notional at EOD

The flow-vs-stock distinction is why there are two volume tools, not one.

## Common commands (run from the repo root)

```bash
pip install -r requirements.txt          # fastmcp>=2.0, pandas, boto3
make data                                # generate the three sample CSVs into ./data
make test                                # smoke-test tool logic against the data (implies `data`)
make run                                 # run MCP server over stdio (Claude Desktop / Inspector)
make run-http                            # run over Streamable HTTP on :8080
make inspect                             # open MCP Inspector against the server
make clean                               # remove generated CSVs + caches
```

- **Single "test":** there is no pytest suite. `scripts/test_tools.py` is a
  script that calls each tool directly (no transport) and prints JSON. Edit the
  `show(...)` calls in it to exercise a specific tool/args, then `python scripts/test_tools.py`.
- **Run the server with a specific backend/data dir:**
  `DATA_BACKEND=local LCH_DATA_DIR=./sftp_root/data python mcp_server/server.py`
- **Deploy (billable AWS):** infra is **not in this repo** — it lives in the central
  IaC repo (`../infrastructure-as-code/terraform/london-clearing-house-demo`, Terraform
  Cloud). The image is built/published by **GitHub Actions**
  (`.github/workflows/docker-publish.yml`) on every push to `main` — no local
  `docker build`. Then `terraform -chdir=<that dir> apply` rolls the service;
  `terraform ... destroy` when done. See README's "Deploy to AWS" section for the full
  sequence and the two required Docker Hub repo secrets.

## Architecture

**`mcp_server/server.py` is the whole application** (single file). Structure:

1. **Config from env vars** — `DATA_BACKEND` (`local`|`s3`), `LCH_DATA_DIR`,
   `LCH_S3_BUCKET`/`LCH_S3_PREFIX`, `LCH_DEFAULT_COB` (optional COB pin; unset = latest),
   `MCP_TRANSPORT`, `MCP_PORT`.
2. **Pluggable data-access layer** — `_load(report, cob)` is the seam. It reads a
   CSV either from a local dir (an SFTP-landed folder) or from S3, and is wrapped
   in `@lru_cache`. `_filename()` maps a logical report name + COB date to the
   dated filename convention (`..._YYYYMMDD.csv`). To add a Snowflake/API backend,
   this is the only function that changes — the tool contract stays identical.
3. **MCP tools** (`@mcp.tool`) — one query tool per report
   (`get_collateral_holdings`, `get_swapclear_volume`, `get_eod_vanilla_outstanding`),
   plus aggregates (`get_collateral_summary_by_member`) and a discovery tool
   (`list_dimensions`) that returns valid filter values so an agent doesn't guess.
   Each tool filters a DataFrame by optional args and returns `{summary, rows}`.
4. **MCP resource** (`@mcp.resource("lch://data-dictionary")`) — read-only schema
   text. Deliberately a *resource*, not a tool, to demonstrate the MCP primitive split.
5. **Transport switch** in `__main__` — stdio by default, Streamable HTTP when
   `MCP_TRANSPORT=http` (the containerised/remote path).

**Ingestion boundary vs query layer:** SFTP/S3 is where files *land*; the MCP
server is the query layer on top. `scripts/sftp_and_s3.sh` provides both a local
Docker SFTP server and an S3 sync to the Terraform-created bucket.

**Deployment** — infra is **not in this repo**. It lives in the central IaC repo at
`../infrastructure-as-code/terraform/london-clearing-house-demo`, on **Terraform Cloud**
(org `nlsql`, workspace `london-clearing-house-demo`, remote + locked state), and delegates
to the reusable module `modules/london-clearing-house`. That module builds: VPC (2 public
subnets, NAT-free for cost) → ALB → ECS Fargate → task reads reports from S3. IAM uses a
**split**: execution role (image pull + logs + read the Docker Hub secret) vs task role
(read-only on *exactly* the reports bucket). The ECS task pulls the image from **Docker Hub**
(`docker.io/denissa4/lch-mcp`, creds in Secrets Manager) — there is no ECR. `Dockerfile` is
multi-stage, runs as non-root, defaults to HTTP transport + S3 backend, and bundles the
sample data so the image can also run standalone with `DATA_BACKEND=local`.

## Conventions and gotchas

- **CSV filenames are date-stamped and must match `_filename()`** — e.g.
  `REP00036a_SOD_NonCashCollateralHoldings_20260630.csv`. A tool call resolves the
  file from `(report, cob)`; a missing file raises `FileNotFoundError`.
- **COB resolution** (`_resolve_cob` in `server.py`): a caller-supplied `cob` wins;
  else the `LCH_DEFAULT_COB` env var if set (an explicit pin); else the **latest**
  report date discovered in the backend (`_latest_cob` lists the `_YYYYMMDD.csv` files
  live on each unfiltered call — so a newly uploaded day is served with no restart).
  The generator (`generate_samples.py`) defaults to **today** and takes `--cob YYYY-MM-DD`;
  it seeds `random.seed(42)` so only the date fields change run to run.
- **All string filters are upper-cased** before matching (`member.upper()`, etc.).
- **`member` is free-text in the lab but is a security boundary in production** —
  the code comments flag that the token, not a parameter, must bind the member so
  Member A can never query Member B's collateral. Preserve that comment/intent when
  editing collateral tools.
- **FastMCP wraps functions into Tool objects** — to call a tool's raw callable
  directly (as `test_tools.py` does), use `getattr(tool, "fn", tool)`.

## Interview context

`docs/INTERVIEW_NOTES.md` explains the "why" behind every design decision (FastMCP,
tools-vs-resources, discovery tools, stdio-vs-HTTP, flow-vs-stock, the security
posture, Terraform IAM split). Read it before changing a design choice — most
choices are deliberate talking points, not accidents.
