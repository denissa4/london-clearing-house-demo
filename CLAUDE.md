# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A recreation of the LCH (London Clearing House) "platform shift" as an **API + MCP**
PoC. A **FastMCP server** exposes two data routes as typed, discoverable tools; the model
picks whichever fits the question:

1. **Synthetic LCH reports** (read from CSV, local or S3) — the original lab data:
   - **REP00036a** — SOD Non-Cash Collateral Holdings (securities + triparty)
   - **DPG SwapClear Registered Volume** — *flow*: what got cleared today
   - **EOD Volume Vanilla Swaps** — *stock*: standing outstanding notional at EOD

   (flow-vs-stock is why there are two volume tools; see `docs/DATA_DICTIONARY.md`.)
2. **Live public data via two in-process APIs** (real, no API key) — the MCP tools call
   these over localhost; the MCP layer never hits the upstreams directly:
   - **Reference Rates** (`apis/rates.py`) — SOFR (NY Fed), €STR (ECB), SONIA (BoE); the
     RFR indices used by SwapClear.
   - **Legal Entities** (`apis/entities.py`) — GLEIF Global LEI index (members/counterparties).

The synthetic report data is invented (field *layouts* follow public LCH specs); the rates
and entity data are live.

## Common commands (run from the repo root)

```bash
pip install -r requirements.txt          # fastmcp, pandas, boto3, fastapi, uvicorn, httpx
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
- **Deploy (billable AWS, EKS only):** infra is **not in this repo** — it lives in the
  central IaC repo (`../infrastructure-as-code/terraform/london-clearing-house-demo-eks`,
  Terraform Cloud). **CI/CD is push-to-main**: `.github/workflows/docker-publish.yml`
  builds/pushes the image to Docker Hub, then rolls the EKS deployment via
  `kubectl rollout restart` (AWS OIDC role) — no local `docker build`, no manual apply
  for releases. Public endpoint: `https://d3j87cdpfnkmyh.cloudfront.net/mcp`. Verify with
  `python scripts/e2e_test.py` (22 live assertions, exit 0/1).

## Architecture

**`mcp_server/server.py`** is the MCP server; **`apis/`** is a small FastAPI app (the two
data APIs). Both run in the **same container** — `_start_api_server()` launches the FastAPI
app on `127.0.0.1:API_PORT` in a daemon thread, then the MCP serves on `MCP_PORT`. Only the
MCP port is exposed externally.

1. **Config from env vars** — `DATA_BACKEND` (`local`|`s3`), `LCH_DATA_DIR`,
   `LCH_S3_BUCKET`/`LCH_S3_PREFIX`, `LCH_DEFAULT_COB` (optional COB pin; unset = latest),
   `MCP_TRANSPORT`, `MCP_PORT`, and `API_PORT`/`API_BASE_URL` (in-process API tier).
2. **Two data routes:**
   - *CSV reports* — `_load(report, cob)` is a pluggable seam (`local`/`s3`, `@lru_cache`);
     `_filename()` maps report+COB to `..._YYYYMMDD.csv`. Tools: `get_collateral_holdings`,
     `get_swapclear_volume`, `get_eod_vanilla_outstanding`, `get_collateral_summary_by_member`,
     `list_dimensions`.
   - *Live APIs* — tools (`get_reference_rate`, `get_rate_history`, `list_rates`,
     `lookup_legal_entity`, `get_legal_entity`, `get_entity_relationships`) call the local
     FastAPI via `_api_get()`. The FastAPI routers (`apis/rates.py`, `apis/entities.py`) call
     upstreams through `apis/clients.py`, where **fetching is split from parsing** so parsers
     are unit-testable with no network. `clients.py` also holds a TTL cache and sends a real
     `User-Agent` (NY Fed blocks generic bots).
3. **Audit log** — `_audit()` prints one JSON record per `tool_call` and `api_call` with the
   client IP (`X-Forwarded-For`) and a `correlation_id` propagated MCP→API via header.
4. **MCP resource** (`@mcp.resource("lch://data-dictionary")`) — read-only schema text.
5. **Transport switch** in `__main__` — stdio by default, Streamable HTTP when
   `MCP_TRANSPORT=http` (the containerised/remote path).

**Ingestion boundary vs query layer:** SFTP/S3 is where files *land*; the MCP
server is the query layer on top. `scripts/sftp_and_s3.sh` provides both a local
Docker SFTP server and an S3 sync to the Terraform-created bucket.

**Deployment (EKS only)** — infra is **not in this repo**. It lives in the central IaC repo
at `../infrastructure-as-code/terraform/london-clearing-house-demo-eks` (Terraform Cloud,
module `modules/lch-mcp-eks` + Helm chart `helm/charts/lch-mcp`): VPC (public subnets for
the ALB, private subnets + NAT for worker nodes) → EKS cluster + managed node group → Helm
release of the MCP behind an ALB ingress, fronted by **CloudFront** — the public endpoint is
`https://d3j87cdpfnkmyh.cloudfront.net/mcp`. The pod pulls `docker.io/denissa4/lch-mcp` via
an image-pull secret; pod IAM (IRSA) is read-only on exactly the reports bucket. **CI/CD is
push-to-main**: `.github/workflows/docker-publish.yml` builds/pushes the image, then does a
`kubectl rollout restart` via an AWS OIDC role. Verify deployments with
`python scripts/e2e_test.py` (22 assertions against the live endpoint). `Dockerfile` is
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
