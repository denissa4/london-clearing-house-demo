# Interview notes — how to talk about this lab

Use the lab as your evidence. When they ask "have you done MCP over APIs?", you can say *"yes, and I built a working LCH-shaped version to make sure I understood your exact scope — here's how I'd reason about it."*

---

## The 60-second architecture pitch

> "The reports today are static files pushed over SFTP — flow and stock data on collateral and swap volumes. I put a FastMCP server in front of that data and expose each report as a typed tool: `get_collateral_holdings`, `get_swapclear_volume`, `get_eod_vanilla_outstanding`, plus discovery tools so an agent can learn the valid dimensions. It runs as a container on Fargate behind an ALB, reads the report data from S3, and the whole stack is Terraform. Locally it reads from an SFTP-landed folder; in cloud it reads from the S3 landing zone — the ingestion boundary is pluggable. That's the platform shift: same source data, but now programmatic, access-controlled, and AI-consumable instead of a file dump."

---

## Design decisions and the "why" (expect to defend each)

**Why FastMCP?** It's the standard Python way to build MCP servers in 2026 — schema generation, validation and docs from a single `@mcp.tool` decorator, so the tool schema the client sees is derived from the function signature and docstring. Less boilerplate, fewer schema/impl drift bugs.

**Why tools *and* a resource?** Tools are actions the agent invokes (queries with parameters). Resources are read-only data the client can pull — I exposed the data dictionary as `lch://data-dictionary`. Knowing the three MCP primitives (Tools, Resources, Prompts) and using the right one is a credibility signal.

**Why discovery tools (`list_dimensions`)?** An agent shouldn't guess valid filter values. A cheap "what dimensions exist" tool lets the model construct correct queries instead of hallucinating a currency or tenor bucket. This is good agent-facing API design.

**Why Streamable HTTP for the deploy, stdio locally?** stdio is fastest (~1ms) but local-only — the host spawns the server as a child process. Streamable HTTP runs it as a web service with SSE, which is what you need for a remote, multi-client, containerised deployment. I default to stdio for MCP Inspector / Claude Desktop and flip to HTTP via an env var for Fargate.

**Why the flow-vs-stock split (two volume tools)?** DPG registered volume is *today's cleared flow*; EOD outstanding is *the standing book*. Different questions, different tools. (See DATA_DICTIONARY.md.)

**Why is `member` a parameter but flagged as "would come from the token"?** In the lab it's free text so you can explore. In production the access token would be bound to one clearing member, and the server would derive the member from the token — **Member A must never query Member B's collateral.** I called that out in the code comments deliberately.

---

## Security — the section that wins the interview

A clearing house cares more about this than about elegant code. Have these ready:

1. **OAuth 2.1 + PKCE.** Remote MCP servers are OAuth *Resource Servers*; the June 2025 spec standardised this. Clients get tokens from an auth server; the MCP server validates them.
2. **Authorize at every tool call, not just at login.** Real-time policy enforcement — a token being valid ≠ this specific tool call being allowed.
3. **Per-member scoping / multi-tenancy.** The single most important control here. Token → member identity → row-level filter enforced server-side.
4. **Never trust the session ID for auth.** The MCP spec is explicit: servers MUST NOT use sessions for authentication and MUST use non-deterministic session IDs. Session hijacking is a known attack class.
5. **Scope minimization.** Start with read-only discovery scopes; no wildcard/omnibus scopes. Elevate only when a privileged operation is first attempted.
6. **Validate every input against the schema.** Prompt-injection / tool-parameter abuse rides in through unvalidated inputs. FastMCP's schema validation is the first line; add range/enum checks.
7. **Audit every invocation.** For a CCP this is compliance, not just security: who/which agent called what, with what parameters, and was it allowed. Log with correlation IDs.
8. **Real incidents to cite** (shows you follow the space): the Asana MCP data-leak bug and the Atlassian MCP forged-input flaw, both 2025; CVE-2025-49596 (unauthenticated MCP Inspector RCE).

In the lab, the least-privilege posture shows up concretely: the **task IAM role can only `s3:GetObject` on exactly the reports bucket**, the **container runs as a non-root user**, and the **task security group only accepts traffic from the ALB SG**. Point at those.

---

## Terraform / infra talking points

- **Split IAM roles**: execution role (image pull + logs) vs task role (app runtime perms). Common mistake is to conflate them.
- **Remote state with locking**: live via **Terraform Cloud** (org `nlsql`, workspace `london-clearing-house-demo`) — the infra now lives in a central IaC repo that delegates to a reusable `london-clearing-house` module. "Reproducible, version-controlled environments" from my CV is literally this. (The app repo keeps the code + image build; the IaC repo owns the infrastructure.)
- **Image supply chain**: a **GitHub Actions** workflow (`.github/workflows/docker-publish.yml`) builds the image from the repo `Dockerfile` and pushes `denissa4/lch-mcp:latest` (+ a `:<sha>` tag) to **Docker Hub** on every push to `main` — no manual build step. The Fargate task pulls that image with credentials held in **Secrets Manager** (execution role can read exactly that one secret). No ECR in the current setup.
- **Least privilege**: task role scoped to one bucket, read-only.
- **Cost-conscious lab vs production**: I used public subnets to avoid NAT cost; I'd flag that production runs tasks in private subnets with VPC endpoints to S3/ECR, an HTTPS listener with an ACM cert, and autoscaling.
- **Fargate vs node-based EKS**: I run Fargate today — serverless containers, no node management, same task/pod model. The JD says EKS; the migration is straightforward and I'm comfortable with node-based EKS + Helm too. Be honest here, don't oversell.

---

## Likely curveballs

- **"How would you handle real-time vs the T+1 batch?"** The files land daily (from 6am per the SwapClear factsheet). True real-time would mean the MCP tools query the live API/warehouse behind the reports rather than the landed file — same tool interface, different data source. The pluggable backend (`local`/`s3`/…) is the seam where you'd add a `snowflake` or `api` backend.
- **"Where does Snowflake fit?"** Swap the file-reading data-access layer for parameterised Snowflake queries behind the same tools; add result-set size caps and cache hot queries. The tool contract the agent sees doesn't change.
- **"What about caching/performance?"** I used an `lru_cache` on file loads in the lab. In production: cache at the query layer, respect a TTL tied to the COB, and paginate large result sets (I cap with a `limit` param).
- **"How do you test this?"** `test_tools.py` exercises the tools directly without a transport — fast unit-level checks. Next layer: spin up the HTTP server and hit it with an MCP client in CI.

---

## One-liners to keep in your pocket

- "Same source data, new access layer — programmatic, scoped, auditable."
- "Tools for actions, resources for reads, discovery tools so the agent doesn't guess."
- "The token identifies the member; the server enforces the boundary."
- "stdio for local, Streamable HTTP for remote and multi-client."
- "Flow is DPG registered volume; stock is EOD outstanding."
- "Least privilege everywhere: one bucket, read-only, non-root, ALB-only ingress."
