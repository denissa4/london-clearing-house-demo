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
                                    containerised on AWS Fargate,
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

## Deploy to AWS (Fargate + Terraform)

This mirrors your existing "Fargate microservices deployed with Terraform templates" stack.
The **infrastructure lives in the central IaC repo**, not here — this repo owns the code,
the image build, and the report data. The Terraform is at
`../infrastructure-as-code/terraform/london-clearing-house-demo` and uses **Terraform Cloud**
(org `nlsql`, workspace `london-clearing-house-demo`) for remote, locked state. The ECS task
pulls the container image from **Docker Hub** (`denissa4/lch-mcp`) with credentials held in
Secrets Manager.

```bash
# Shorthand for the central config dir:
TF=../infrastructure-as-code/terraform/london-clearing-house-demo

# 1. Publish the image: just push to `main`. The `docker-publish` GitHub Actions
#    workflow builds and pushes denissa4/lch-mcp:latest (+ a :<sha> tag) to Docker
#    Hub. Wait for the run to go green before deploying. (Requires the repo secrets
#    DOCKERHUB_USERNAME and DOCKERHUB_TOKEN — see below.)
git push origin main

# 2. Deploy the infra (requires Terraform Cloud auth for the `nlsql` org;
#    `docker_password` is set as a sensitive variable in the TFC workspace):
terraform -chdir="$TF" init
terraform -chdir="$TF" apply

# 3. Upload the data to the reports bucket:
./scripts/sftp_and_s3.sh s3-sync "$(terraform -chdir="$TF" output -raw reports_bucket)"

# 4. The MCP endpoint:
terraform -chdir="$TF" output -raw mcp_endpoint    # http://<alb-dns>  (append the MCP path)
```

**CI setup (one-time):** add two repo secrets under **Settings → Secrets and variables
→ Actions** — `DOCKERHUB_USERNAME` (`denissa4`) and `DOCKERHUB_TOKEN` (a Docker Hub
access token with Read/Write on `denissa4/lch-mcp`). The image is then built from the
`Dockerfile` at the repo root on every push to `main` — no local `docker build` needed.

**Tear it down when done** (it costs money while running):
```bash
terraform -chdir="$TF" destroy
```

### What the Terraform builds
- **VPC** with 2 public subnets across 2 AZs (NAT-free to keep lab cost down).
- **S3 bucket** (private, encrypted, versioned) — the report landing zone.
- **ECS Fargate** service behind an **ALB**, `containerInsights` on. Image pulled from **Docker Hub** (creds in Secrets Manager) — no ECR.
- **IAM**: split execution role (image pull + logs + read the Docker Hub secret) and task role (read-only on *exactly* the reports bucket). Least privilege.
- **CloudWatch Logs** with 14-day retention.
- **State** is **Terraform Cloud** (remote + locked), not a local `.tfstate`.

Production deltas to mention out loud: private subnets + VPC endpoints (no public task IPs), HTTPS listener with ACM cert, OAuth 2.1 in front, autoscaling policy.

---

## Repo layout

```
lch-mcp-lab/
├── .github/workflows/
│   └── docker-publish.yml   CI: build + push image to Docker Hub on push to main
├── data/                    generated sample CSVs (git-ignore in real life)
├── mcp_server/server.py     the FastMCP server (the star of the show)
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
 ../infrastructure-as-code/terraform/london-clearing-house-demo, on Terraform Cloud.)
```

---

## Learning path (suggested order)

1. **Generate + inspect the data** (`generate_samples.py`, then open the CSVs). Understand what a clearing house actually reports.
2. **Read `mcp_server/server.py` top to bottom.** This is where the interview lives — tools, schemas, resources, transports.
3. **Run MCP Inspector** and call each tool by hand. Watch the JSON-RPC.
4. **Wire it into Claude Desktop** and query in natural language — feel the "platform shift."
5. **Do the SFTP exercise** so you can speak to ingestion.
6. **Deploy on Fargate with Terraform**, then `destroy`.
7. **Read `docs/INTERVIEW_NOTES.md`** and practise saying each design decision aloud.
```
