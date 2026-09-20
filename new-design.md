# Invoice-on-Hold Agent — Target Design (v2)

Status: draft for build. Supersedes the trigger-based design in `sa/invoice-on-hold.drawio`.
Related: [AWS Guidance for Agentic ERP AP/AR Exception Handling](https://docs.aws.amazon.com/solutions/agentic-erp-accounts-payable-and-receivable-exception-handling-on-aws/) — this design extends it (see §12).

---

## 1. Purpose

Resolve invoices parked for **PO mismatch** (2-way, value-based POs): the invoice cites a PO that exists but belongs to a different vendor. An agent investigates as soon as the invoice is parked, recommends a resolution with evidence, and a human accepts or rejects. Every decision is auditable and the agent is scored against labelled expected outcomes.

Out of scope: other hold reasons (price/quantity variance, duplicates, missing PO), 3-way match, write-back to SAP.

## 2. Assumptions

1. SAP S/4HANA publishes business events via **SAP Integration Suite using the Amazon EventBridge receiver adapter**. Integration Suite is licensed. In the POC a simulator publishes the same event shapes.
2. Invoice header and line text are captured by **OCR at scanning**; they are the vendor's wording, never the buyer's PO wording. A PO-mismatch invoice has no PO-referenced lines yet (nothing valid to propose from), only OCR text.
3. **Vendor group keys are not maintained** (SAP `LFA1-KONZS` empty). Sister-entity invoices must be reasoned about, not looked up.
4. Events carry the full record (the POC has no SAP to call back). Production events would be thin; the verify-before-act step stands in for the call-back.
5. All data is synthetic. Emails use `example.com`.

## 3. Architecture overview

```
 SAP Integration Suite ──(EventBridge adapter)──┐
 Simulator Lambda (reads datasets/ from S3) ────┤
                                                ▼
                                        EventBridge bus  ── rules per domain ──▶  SQS master-data ─▶ Master Data Handler ─┐
                                                                                  SQS purchase-orders ─▶ PO Handler ──────┼─▶ Aurora Serverless v2
                                                                                  SQS invoices ─▶ Invoice Handler ────────┘   (pgvector, Data API)
                                                                                                        │ parked invoice
                                                                                                        ▼
                                                                              DynamoDB invoice_workflow ── stream ─┬▶ EventBridge Pipe (filter)
                                                                                        ▲                          │        │
                                                                                        │ writer Lambda            └▶ Communicator Lambda ─▶ SES ─▶ requester
                                                                                        │ (write_recommendation,            ▼        (Escalated / decision emails)
                                                                                        │  escalate)                 SQS invoice-on-hold (+DLQ)
                                                                                        │                                   │
                                                                              AgentCore Runtime (LangGraph agent, Python) ◀── Invoker Lambda
                                                                               ├─ Gateway (MCP tools → reader / writer Lambdas → Aurora / DynamoDB)
                                                                               ├─ Policy (Cedar, default-deny) + Guardrails
                                                                               ├─ (no AgentCore Memory: context is explicit, see §8)
                                                                               └─ Observability → CloudWatch

 React UI (S3 + CloudFront, Cognito) ─▶ API Gateway ─▶ API Lambdas ─▶ DynamoDB (queue, decisions) / Aurora (detail)
 Evaluations: expected_resolutions.csv ─▶ on-demand evals in CI; online sampling of production traces
```

Numbered main flow:

1. SAP (or the simulator) publishes events — `Supplier.*`, `CostCentre.*`, `WBSElement.*`, `PurchaseOrder.*`, `SupplierInvoice.Posted`, `SupplierInvoice.Parked`.
2. EventBridge rules route each detail-type to its domain queue.
3. Handlers **upsert** into Aurora (idempotent, order-tolerant). PO Handler also generates line-description embeddings via Bedrock.
4. Invoice Handler writes a parked invoice's workflow item to DynamoDB with `status = In Queue`.
5. The stream → Pipe filter forwards only _start-the-agent_ changes to the invoice-on-hold queue.
6. Invoker Lambda: conditional update `In Queue → Processing`, then `InvokeAgentRuntime` with a fresh per-run session ID and an explicit payload: invoice number, prior recommendation and `user_comments` (rework only), vendor history is fetched by the agent via a tool.
7. Agent investigates through Gateway tools (reader Lambda), then writes a recommendation or escalates (writer Lambda), status → `Awaiting Approval` / `Escalated`. The agent never sends email: the Communicator Lambda consumes the `invoice_workflow` stream and emails the PO requester when status becomes `Escalated`.
8. User reviews in the UI; **accepts** (status → `Resolved`, invoice lines created against the chosen PO line) or **rejects with comments** (status → `In Rework`, then `In Queue`; re-enters step 5).

## 4. Event contract

- `source`: `sap.s4hana` (simulator uses the same value; a `simulated: true` flag in `detail`).
- `detail-type`: `Supplier.Created|Changed`, `CostCentre.Created|Changed`, `WBSElement.Created|Changed`, `PurchaseOrder.Created|Changed`, `SupplierInvoice.Posted`, `SupplierInvoice.Parked`.
- `detail`: business keys + record + `changed_at` (used for order-tolerant upserts).
- Delivery is at-least-once: consumers are idempotent; a change older than the stored `changed_at` is ignored.
- Ordering across entities is not guaranteed. The simulator publishes master data → POs → invoices; the Invoice Handler treats _PO not found_ as **retryable** (message returns to the queue, lands in the DLQ if the PO never arrives) — never as a mismatch.

## 5. Queues

| Queue             | Events                            | Notes                                                                  |
| ----------------- | --------------------------------- | ---------------------------------------------------------------------- |
| `master-data`     | Supplier, CostCentre, WBSElement  | same simple upsert                                                     |
| `purchase-orders` | PurchaseOrder                     | embedding calls; lower `maximumConcurrency`, longer visibility timeout |
| `invoices`        | SupplierInvoice.\*                | largest payloads; depends on POs existing                              |
| `invoice-on-hold` | from the DynamoDB stream via Pipe | protects Bedrock quota; retries isolated from ingestion                |

Every queue: standard (not FIFO), own DLQ with `maxReceiveCount = 5`, visibility timeout ≈ 6 × Lambda timeout, `ReportBatchItemFailures`, alarm on DLQ depth → SNS. Redrive is manual.

## 6. Data stores

**Aurora Serverless v2 (PostgreSQL + pgvector), min ACU 0, accessed via RDS Data API.** No Lambda lives in the VPC.

- `vendors`, `cost_centres`, `wbs_elements`, `purchase_order_headers`, `purchase_order_lines`, `invoices_headers`, `invoices_lines`.
- `po_line_embeddings` (~1 per distinct description; no vector index needed at this size).
- Extensions: `vector`, `pg_trgm`, `fuzzystrmatch` (vendor-name and PO-number similarity).
- Consumed value per PO line = sum of `invoices_lines.amount` against it; open value is derived, never stored.

**DynamoDB `invoice_workflow`** (PK `invoice_no`): `status`, `version`, `user_comments`, `recommendation` (PO, line, confidence, evidence, rationale), `agent_run_id`, `updated_at`, `updated_by`. Stream `NEW_AND_OLD_IMAGES`.

**DynamoDB `resolution_history`** (PK `invoice_no`; GSI `vendor_id` sorted by `decided_at`): one compact item per decided invoice — `cited_po`, `resolved_po`, `resolved_line`, `outcome`, `evidence_path`, `confidence`, `user_decision` (accepted / rejected), `user_comments`, `decided_at`. Written by the decision API when the user accepts or rejects. This replaces managed long-term memory: it is exact-key retrievable, auditable, and keeps rejections as well as successes.

**DynamoDB `audit_trail`** (PK `invoice_no`, SK `timestamp#event`): append-only — IAM allows `PutItem` only; no update/delete. Records agent recommendations, tool-call summaries, user decisions. Payloads > 350 KB spill to S3 with a pointer. Exported to S3 for retention.

**S3**: `datasets` (seed CSVs), `website` (React build), `audit-archive`.

## 7. Workflow states

```
In Queue → Processing → Awaiting Approval → Resolved            (user accepts)
                                          → In Rework → In Queue (user rejects with comments)
In Queue → Processing → Escalated                                (agent cannot resolve; requester emailed)
```

- `version` increments on every write; the agent's final write is conditional on the version it started with (prevents two runs clobbering each other after rapid feedback).
- Pipe filter (loop prevention): forward `INSERT` with `status = In Queue`, and `MODIFY` where `status` transitioned to `In Queue` or `user_comments` changed. Drop everything else, including the agent's own writes.

## 8. Agent

- **Framework:** LangGraph (`create_react_agent` / small graph) on **AgentCore Runtime**. Not the legacy `AgentExecutor` — Evaluations and Observability instrument LangGraph.
- **Stateless runs, explicit context.** No AgentCore Memory (short- or long-term). Every run gets a fresh session ID (≥ 33 chars, unique per run) and receives all context explicitly: the invoice, and on rework the prior recommendation plus `user_comments` from DynamoDB. Vendor history comes from `get_vendor_history`. The same inputs therefore produce the same trace, which is what makes rework cases reproducible in Evaluations; a resumed session that may have expired would not be.
- **Model:** Bedrock, configurable; a fast tool-calling tier is sufficient. Local dev may use Gemini behind the same interface.
- **Languages:** Python for all runtime code (agent, tool Lambdas, handlers, API Lambdas); **CDK in TypeScript** for infrastructure. Python because AgentCore's toolkit, Evaluations examples and the public LangGraph case studies are Python, which is what a reviewer will compare this against; LangChain.js has an order of magnitude less adoption and a single public enterprise reference.
- **Tools (Gateway → two Lambda targets, grouped by permission boundary, not one Lambda per tool):** Gateway holds the tool schemas and does the MCP translation; each target Lambda receives the tool name in `context.client_context.custom["bedrockAgentCoreToolName"]` and the arguments as the event, and dispatches to a plain Python function. Grouping keeps cold starts to at most two per run instead of one per tool, while each role stays scoped to what its tools touch. Tool input models are Pydantic; their JSON schema is exported once and fed to both the Gateway target definition (read by CDK at synth) and the Lambda's validation, so the contract cannot drift. The same functions are what the local LangGraph run calls directly (§15).
  - `get_invoice(invoice_no)` — header, OCR lines, cited PO
  - `get_vendor_history(vendor_id, limit)` — last N items from `resolution_history`: prior outcomes, accepted/rejected, comments. Evidence, not the answer: a vendor with five prior reassignments still escalates when nothing matches this time.
  - `lookup_po(po_number)` — header, vendor, lines, open value
  - `search_candidate_po_lines(vendor_id, text, period_hint)` — hybrid: SQL filter on vendor/open value → vector similarity on description → trigram on names
  - `find_by_cost_centre / find_by_wbse(vendor_id, text)` — the indirect evidence paths
  - `compare_vendors(vendor_a, vendor_b)` — name trigram, address, tax ID (a differing tax ID disproves same entity; same address + similar name suggests group)
  - `check_open_value(po_number, line)`
  - `compliance_check(vendor_id)` — simulated sanctions/VAT stub
  - `write_recommendation(invoice_no, po_number, line, confidence, evidence)`
  - `escalate(invoice_no, contact_email, reason)` — sets status `Escalated`, records reason and contact, appends the audit entry. Does **not** send mail (see Communicator below).

  | Lambda           | Trigger                              | Tools / job                                                                                                                                           | IAM (least privilege)                                                                                                                                  |
  | ---------------- | ------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
  | **reader**       | Gateway target                       | `get_invoice`, `get_vendor_history`, `lookup_po`, `search_candidate_po_lines`, `find_by_*`, `compare_vendors`, `check_open_value`, `compliance_check` | RDS Data API execute (read-only DB user), Secrets Manager read of that secret, DynamoDB read on `resolution_history`                                   |
  | **writer**       | Gateway target                       | `write_recommendation`, `escalate`, later `resolve_invoice` (level 4)                                                                                 | DynamoDB `UpdateItem` on `invoice_workflow`, `PutItem` on `audit_trail`; for `resolve_invoice` a separate DB user with insert on `invoices_lines` only |
  | **communicator** | DynamoDB stream (`invoice_workflow`) | emails the PO requester on `Escalated` and on user decisions; keyed on the state transition, so redrives and rework runs never double-send            | SES `SendEmail` from one verified identity, stream read. Not a Gateway tool: neither the agent runtime nor the Gateway targets hold SES permissions    |

- **Outcomes:** `reassign_po`, `accept_cited_po` (vendor-group case), `escalate`.
- **Autonomy level (`agentLevel` in config):**
  - **3** (default): agent recommends; human approves every case.
  - **4**: agent may resolve automatically when confidence ≥ threshold; below it, human approval. Enforced in **Policy**, not only in app config (see §9).

## 9. Governance

- **AgentCore Policy (Cedar, default-deny):** allow read tools + `write_recommendation` + `escalate`. `resolve_invoice` (creates lines, sets Resolved) is denied at level 3; at level 4 allowed only with `confidence >= threshold` in the request context. Human approval is therefore guaranteed by the platform, not requested by the prompt. Policy authorises by tool name, so the reader/writer grouping in §8 does not change it.
- **Two independent controls on every write:** Cedar at Gateway (by tool name) and the IAM role of the Lambda that executes it (by resource and action). The reader role cannot write, the writer role cannot read Aurora beyond what `resolve_invoice` needs, and no agent-path role can send email. Gateway's invoke role and the Runtime's execution role are scoped to the specific Lambda ARNs and Gateway ARN, no wildcards, so the per-Lambda split holds up under review.
- **Bedrock Guardrails** attached via Policy: mask bank details / PII in prompts and responses.
- **Verify-before-act:** before any resolution write, re-read current open value and status (stands in for the synchronous SAP call in production).
- **Model invocation logging** to S3 for auditability.

## 10. Observability and evaluation

- **AgentCore Observability**: trace per invoice (tool sequence, tokens, latency). Short CloudWatch retention; this is the largest AgentCore-adjacent cost.
- **Lambda Powertools for Python** (structured logs, metrics, X-Ray) on all non-agent Lambdas; correlation ID = invoice number.
- **AgentCore Evaluations**: on-demand run of `datasets/expected_resolutions.csv` in CI (custom Lambda evaluator: exact match on outcome / PO / line); online sampling in production.
- **Metrics:** resolution accuracy vs expected; escalation precision (unresolvable cases escalated, resolvable ones not); acceptance rate; rework rate; time-to-recommendation; tool calls, tokens and **cost per invoice**.

## 11. UI and API

- **React** on S3 + CloudFront. Tabs: On Hold (queue), Posted, Master Data. Invoice view: recommendation, evidence, candidates considered, confidence, accept / reject-with-comments, audit timeline.
- **CopilotKit / AG-UI** panel (phase 2): "ask the agent about this invoice", streaming reasoning from Runtime.
- **API Gateway (HTTP) → Lambdas:** `GET /invoices?status=`, `GET /invoices/{id}`, `POST /invoices/{id}/decision`, `GET /invoices/{id}/audit`, `GET /purchase-orders/{id}`.
- **Cognito** user pool (groups `ap-clerk`, `ap-approver`); JWT authorizer on API Gateway and Gateway. The audit trail records `updated_by` from the token.
- **Escalations and decision notices:** SES to the PO requester (from `purchase_order_headers.requester`), sent by the Communicator Lambda from the `invoice_workflow` stream (§8), never from a tool or an API Lambda.

## 12. Relation to the AWS guidance

Same building blocks (AgentCore, Aurora pgvector, DynamoDB, SNS/SES). This design adds what the guidance lists as pending or absent: an exception type it doesn't cover (PO exists, wrong vendor, incl. sister entities); matching depth across four evidence paths; an ERP event-integration design; a proactive trigger (agent runs on parking, not on a user question); explicit approval states with Policy enforcement; labelled evaluation.

## 13. Decision records (summary)

| #   | Decision                                                                     | Alternatives                                          | Why                                                                                                                                                                                                                  |
| --- | ---------------------------------------------------------------------------- | ----------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | Aurora pgvector for relational + vector                                      | separate vector DB; OpenSearch                        | one store, one consistency model; ~100 vectors                                                                                                                                                                       |
| 2   | RDS Data API, no Lambdas in VPC                                              | RDS Proxy in VPC                                      | no NAT/endpoint cost, no pooling; Aurora alone in VPC                                                                                                                                                                |
| 3   | DynamoDB workflow state + Streams                                            | Postgres trigger + `aws_lambda`                       | no trigger loops/delay hacks, no Lambda endpoint, per-item ordering                                                                                                                                                  |
| 4   | Standard SQS grouped by domain                                               | FIFO; queue per event type                            | ordering handled by upserts; failure isolation without 5× alarms                                                                                                                                                     |
| 5   | AgentCore Runtime/Gateway/Policy                                             | LangChain on Lambda; Harness/Strands                  | managed hosting + governed tools; LangGraph kept for portability and learning                                                                                                                                        |
| 6   | Hybrid retrieval                                                             | pure vector search                                    | descriptions cluster; names/IDs need trigram/edit distance                                                                                                                                                           |
| 7   | Human approval by default, Policy-enforced                                   | prompt-only instruction                               | deterministic guarantee, not probabilistic                                                                                                                                                                           |
| 8   | Evaluations with labelled set                                                | eyeballing                                            | measurable accuracy; CI regression                                                                                                                                                                                   |
| 9   | Audit in DynamoDB (append-only), S3 archive                                  | S3 objects with Object Lock                           | queryable timeline; small records                                                                                                                                                                                    |
| 10  | Single agent + tools                                                         | multi-agent (audit agent, feedback agent)             | Evaluations online sampling _is_ the audit; `resolution_history` _is_ the feedback loop                                                                                                                              |
| 12  | Explicit context over managed memory                                         | AgentCore short-/long-term Memory                     | deterministic given inputs; rework reproducible in evals; history auditable in DynamoDB, includes rejections                                                                                                         |
| 11  | EventBridge adapter assumed                                                  | AEM, ABAP SDK, IDocs                                  | one realistic path; keeps focus on resolution                                                                                                                                                                        |
| 13  | Tool Lambdas grouped by permission boundary (reader / writer / communicator) | one Lambda per tool; one Lambda hosting an MCP server | ≤ 2 cold starts per run instead of ~10; roles stay narrow and auditable; Gateway already multiplexes tools onto one target, so a self-hosted MCP server would add an HTTP hop and the JSON-RPC handshake for nothing |
| 14  | Email from a stream consumer, not a tool                                     | `escalate` tool calls SES directly                    | agent path holds no SES permission; retries and DLQ live with the consumer; idempotent on state transition, so rework/redrive never double-sends                                                                     |
| 15  | Python runtime, TypeScript CDK                                               | LangChain.js / LangGraph.js end to end                | AgentCore toolkit, Evaluations samples and public case studies are Python; LangChain.js has ~30× fewer downloads and one public enterprise reference (Elastic); TS CDK is the mainstream IaC choice                  |

## 14. Cost (to be estimated in README)

Fixed at idle: Secrets Manager secret, CloudWatch retention. Variable: Aurora ACU-hours (scales to 0), Bedrock tokens (dominant), AgentCore Runtime seconds, Gateway/Policy per call, DynamoDB on-demand. Report **cost per resolved invoice** vs clerk handling time.

## 15. Open items

- Bank-detail validation (different hold reason) — defer or stub.
- Tax ID / address on vendor master and invoice text — add (small).
- Level-4 confidence threshold and how confidence is computed.
- Duplicate-invoice check, business-unit context — future scope.
- Circuit breaker / fallback when Bedrock throttles: queue backs up by design; alarm on age of oldest message.
- Local mode: docker-compose Postgres + local LangGraph run + Gemini. The reader/writer tool functions are imported directly as LangChain tools (same Pydantic models, no Gateway); the communicator is stubbed to log instead of sending.
