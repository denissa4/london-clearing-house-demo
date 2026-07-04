# LCH MCP Lab

A hands-on lab that recreates, in miniature, the exact platform described in the Luxoft/LCH role:

> LCH publishes thousands of **static reports** (PDF/CSV/XML) over **SFTP and portal downloads**. The project replaces that with an **MCP facade over LCH's APIs**, so members query data programmatically and AI agents consume it in real time — *"not a reporting upgrade, a platform shift."*

This repo lets you build and run that shift end to end:

```
   LCH report CSVs                  MCP server (FastMCP)              MCP client
 ┌──────────────────┐   land in   ┌──────────────────────┐  query   ┌──────────────┐
 │ REP00036a SOD    │────────────▶│  tools:              │◀─────────│ Claude / MCP │
 │ DPG SwapClear vol│  SFTP / S3  │   get_collateral_…   │  tool    │  Inspector / │
 │ EOD Vanilla swaps│             │   get_swapclear_…    │  calls   │  your agent  │
 └──────────────────┘             │   get_eod_vanilla_…  │          └──────────────┘
      (static files)              └──────────────────────┘
                                    containerised on AWS EKS,
                                    infra defined in Terraform
```

The three reports match the **actual LCH scope** in the job description:

| In-scope report (JD)                                   | This lab's file                                    |
|--------------------------------------------------------|----------------------------------------------------|
| CALM Rep00036a — SOD Non-Cash Collateral Holdings      | `REP00036a_SOD_NonCashCollateralHoldings_*.csv`    |
| DPG — SwapClear registered/outstanding trade volume    | `DPG_SwapClear_RegisteredVolume_*.csv`             |
| EOD Volume Vanilla Swaps                               | `EOD_Volume_VanillaSwaps_*.csv`                    |

See `docs/DATA_DICTIONARY.md` for how each field maps to the real LCH spec.

> ⚠️ **All data is synthetic.** Field *layouts* follow public LCH specs; the ISINs, members, prices and notionals are invented for training. Not real LCH data.

---

## Quick start (local, no cloud, ~2 minutes)

```bash
# 1. Install deps
pip install -r requirements.txt

# 2. Generate the sample report files into ./data
python scripts/generate_samples.py

# 3. Sanity-check the query logic against the data
python scripts/test_tools.py

# 4a. Run the server over stdio (for Claude Desktop / MCP Inspector)
python mcp_server/server.py

# 4b. …or over HTTP (for a browser / remote client)
MCP_TRANSPORT=http MCP_PORT=8080 python mcp_server/server.py
```

### Point an MCP client at it

**MCP Inspector** (easiest to see tools/resources):
```bash
npx @modelcontextprotocol/inspector python mcp_server/server.py
```

**Claude Desktop** — add to `claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "lch-reporting": {
      "command": "python",
      "args": ["/ABSOLUTE/PATH/lch-mcp-lab/mcp_server/server.py"],
      "env": { "DATA_BACKEND": "local" }
    }
  }
}
```
Then ask Claude things like *"What non-cash collateral does member JPM hold?"* or *"Show me USD SOFR IRS registered volume in the 5-10Y bucket."*

---

## The SFTP angle

You wanted to practise the "files arrive over SFTP" side. Two options in `scripts/sftp_and_s3.sh`:

```bash
# Local throwaway SFTP server in Docker, then upload the CSVs into it
./scripts/sftp_and_s3.sh sftp-up
#   -> then run the server pointed at the landed folder:
DATA_BACKEND=local LCH_DATA_DIR=./sftp_root/data python mcp_server/server.py
./scripts/sftp_and_s3.sh sftp-down     # cleanup

# OR push the CSVs to the S3 landing zone (matches the cloud deploy)
./scripts/sftp_and_s3.sh s3-sync <bucket-name-from-terraform>
```

The mental model to articulate in interview: **SFTP/S3 is the ingestion boundary; the MCP server is the query/intelligence layer on top.** You are not throwing away the file pipeline — you are putting a programmatic, access-controlled, AI-consumable facade in front of it.

---

## Live data APIs (the API + MCP layer)

Beyond the synthetic CSV reports, the server ships two **governed APIs over live, public,
LCH-relevant data** (no API key). They run **in-process** alongside the MCP
(`127.0.0.1:8001` by default); the MCP tools call them over localhost, so the MCP layer
never touches the upstream sources directly.

| API | Path | Backed by | MCP tools |
|-----|------|-----------|-----------|
| **Reference Rates** | `/rates` | NY Fed (SOFR), ECB (€STR), Bank of England (SONIA) — the RFR indices used by SwapClear | `list_rates`, `get_reference_rate`, `get_rate_history` |
| **Legal Entities** | `/entities` | GLEIF Global LEI index (members / counterparties) | `lookup_legal_entity`, `get_legal_entity`, `get_entity_relationships` |

At query time the model picks the route that fits — the synthetic report tools *or* the
live API tools. Every tool call and downstream API call is written to a structured **audit
log** (JSON on stdout) with the caller's IP and a correlation id.

```bash
# Run the server (also starts the APIs); then hit the APIs directly:
make run-http                                   # MCP on :8080, APIs on 127.0.0.1:8001
curl 127.0.0.1:8001/rates/SOFR/latest           # {rate_id, date, value, source, ...}
curl "127.0.0.1:8001/entities/search?name=LCH"  # GLEIF LEI records
# OpenAPI docs: http://127.0.0.1:8001/docs
```

## Deploy to AWS (EKS + Terraform)

The **infrastructure lives in the central IaC repo**, not here — this repo owns the code,
the image build, and the report data. The Terraform is at
`../infrastructure-as-code/terraform/london-clearing-house-demo-eks` (module `lch-mcp-eks`
+ Helm chart `helm/charts/lch-mcp`) and uses **Terraform Cloud** for remote, locked state.
The pod pulls the container image from **Docker Hub** (`denissa4/lch-mcp`).

**CI/CD is fully automated for EKS** — a push to `main` does everything:

```bash
# Push to main. The `docker-publish` GitHub Actions workflow:
#   1. builds and pushes denissa4/lch-mcp:latest (+ a :<sha> tag) to Docker Hub
#   2. assumes the AWS OIDC role and runs `kubectl rollout restart deployment/lch-mcp`
#      on the EKS cluster, waiting for the rollout to complete
git push origin main

# The public MCP endpoint (CloudFront in front of the EKS ALB):
#   https://d3j87cdpfnkmyh.cloudfront.net/mcp
# (also available as `terraform output -raw cloudfront_url` in the EKS config dir)

# Upload report data to the bucket the pod reads:
TF=../infrastructure-as-code/terraform/london-clearing-house-demo-eks
./scripts/sftp_and_s3.sh s3-sync "$(terraform -chdir="$TF" output -raw reports_bucket)"

# Verify the deployment end-to-end (22 assertions against the live endpoint):
python scripts/e2e_test.py
```

**CI setup (one-time):** repo secrets `DOCKERHUB_USERNAME` / `DOCKERHUB_TOKEN`, and repo
variables `AWS_ROLE_ARN` / `EKS_CLUSTER_NAME` (from the EKS config's terraform outputs).

**Tear it down when done** (it costs money while running):
```bash
terraform -chdir="$TF" destroy
```

### What the Terraform builds (module `lch-mcp-eks`)
- **VPC** with public subnets (ALB) + private subnets (worker nodes) behind a NAT gateway.
- **EKS cluster** + managed node group, ALB ingress controller, IRSA for pod IAM.
- **S3 bucket** (private, encrypted, versioned) — the report landing zone; pod role is read-only on exactly that bucket.
- **Helm release** of `helm/charts/lch-mcp` — the MCP Deployment/Service/Ingress; image pulled from Docker Hub via an image-pull secret.
- **CloudFront** in front of the ALB for the public HTTPS endpoint.
- **State** is **Terraform Cloud** (remote + locked), not a local `.tfstate`.

Production deltas to mention out loud: HTTPS listener with ACM cert at the ALB, OAuth 2.1 in front of the MCP, autoscaling policy.

---

## Repo layout

```
lch-mcp-lab/
├── .github/workflows/
│   └── docker-publish.yml   CI: build + push image to Docker Hub on push to main
├── data/                    generated sample CSVs (git-ignore in real life)
├── mcp_server/server.py     the FastMCP server (both data routes + audit log)
├── apis/                    in-process FastAPI: rates + entities (live public data)
│   ├── clients.py           upstream clients (fetch split from parse) + TTL cache
│   ├── rates.py             /rates router (SOFR / €STR / SONIA)
│   └── entities.py          /entities router (GLEIF LEI)
├── scripts/
│   ├── generate_samples.py  builds the three report files
│   ├── test_tools.py        exercises each tool against the data
│   └── sftp_and_s3.sh       local SFTP + S3 landing-zone helpers
├── Dockerfile               multi-stage, non-root, HTTP transport
├── requirements.txt
└── docs/
    ├── DATA_DICTIONARY.md    field-by-field provenance vs real LCH specs
    └── INTERVIEW_NOTES.md    how to talk about every design choice

(AWS infra — VPC/ALB/ECS/S3/IAM — lives in the central IaC repo:
 ../infrastructure-as-code/terraform/london-clearing-house-demo-eks, on Terraform Cloud.)
```

---

## Learning path (suggested order)

1. **Generate + inspect the data** (`generate_samples.py`, then open the CSVs). Understand what a clearing house actually reports.
2. **Read `mcp_server/server.py` top to bottom.** This is where the interview lives — tools, schemas, resources, transports.
3. **Run MCP Inspector** and call each tool by hand. Watch the JSON-RPC.
4. **Wire it into Claude Desktop** and query in natural language — feel the "platform shift."
5. **Do the SFTP exercise** so you can speak to ingestion.
6. **Deploy on EKS with Terraform** (push to `main` for the CI/CD path), then `destroy`.
7. **Read `docs/INTERVIEW_NOTES.md`** and practise saying each design decision aloud.
```
