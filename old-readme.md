# invoice-on-hold-agent

Reference pattern for applying agentic AI to invoice-on-hold (P2P)

## Domain and Use Case

In P2P domain, Invoice On Hold (IOH) is often a pain area where vendor invoices get stuck resulting into rework, payment delays and lost discounts. A human intensive process involves an investigation and identifying a fix that takes time. Using an AI Agent will reduce resolution time and as well as Operational Costs. AI Agent is an inevitable choice to transform IOH process in P2P.

An invoice gets on hold for various reasons. As this is a POC, this only targets one specific reason "PO Mismatch" (2 Way) to demonstrate how AI can be leveraged. PO Mismatch refers to PO on the invoice either does not exist or belongs to another vendor.

## Collaboration

Claude edits code only when explicitly asked

## Key Assumptions

1. **Vendor group keys are not maintained.** ERP systems carry a field linking trading entities of the same supplier group (SAP: `LFA1-KONZS`), but in practice it is often left unpopulated. This POC assumes it is empty, so when an invoice arrives from a sister entity of the PO vendor, the agent must weigh the evidence rather than look the relationship up.
2. Invoice description, line items texts are pre populated by OCR at the time of scanning. File `datasets/invoices_headers.csv` and `datasets/invoices_lines.csv` are generated with this assumption. This is key for digital transformation of Accounts Payable in scanning process.
3. Authentication and Authorization not required as this is POC

## Key Notes

DB trigger has to fire only on the changes that should start the agent:

AFTER INSERT for a parked invoice
AFTER UPDATE OF user_comments, with a condition that the old and new comments differ (IS DISTINCT FROM)
never on status or recommendation columns

Design Choices to highlight ?
POC Scalability Strategy: Mention that direct Lambda-to-S3 logging was chosen intentionally given an expected workload ceiling of \(<100\) transactions per day, striking an optimal balance between lower system latency and zero structural infrastructure overhead compared to a Kinesis loop.

Deterministic Token Safeguards: Explicitly document that you limited downstream execution overhead and potential token starvation parameters by binding Lambda concurrency directly to a threshold constraint of 1.

Zero-Trust Network Topology: Call out that the code logic operates entirely inside isolated network loops where data transit strings never step past the internal boundary of AWS PrivateLink VPC endpoints.

SQS triggers IOH lambda directly - check if this is needed

RDS connections - state that maximumConcurrency on the queue triggers keeps connections within limits

1. Identify your first FSI Agentic AI case
2. Build POC on Amazon Bedcore
3. Engage your AWS Partner Development Manager
4. Publish to AWS Marketplace for scale

### Blog / Readme points

1. Highlight cost per invoice for this process

### Check this

#### Parked open points (2026-09-20)

Decisions taken in design discussion but not yet written into `new-design.md` or the diagram.

**Needs a decision**

1. **Audit failure path.** `Pending Audit` has no terminal states. Decide `Audit Passed` / `Audit Failed`, what a failure does (no reversal possible: SAP write-back is out of scope), and whether repeated failures feed back into the level-4 confidence threshold.
2. **Does `adminUsers` approve?** Separating configuration rights from approval rights is what an auditor expects; decide and record it.
3. **`resolve_invoice` full signature** and the verify-before-act read it performs before writing.
4. **`AUDIT_PERCENTAGE` location.** Lambda env var (simple, redeploy to change) vs SSM parameter read with the Powertools cache (matches the vendor-snapshot-version and user-allowlist pattern already used twice).

**Build items, agreed but not built**

5. Audit queue in the UI: fourth tab or status filter, visible to `smeUsers` only under route-level authorization.
6. Audit result written to the audit trail with `run_id`, so the Athena drill-down can show what the model saw on an SME-rejected case.
7. Metric: of auto-resolved invoices sampled, the percentage the SME confirmed. Stronger evidence than accuracy against the synthetic CSV, and the labels feed the eval set for free.
8. Dashed-line legend on the diagram; dashed currently means async invoke, telemetry, and manual redrive.
9. AgentCore Evaluations element on the diagram. POC uses a Python replay script over `datasets/expected_resolutions.csv` instead; production successor is the same script run against real accept/reject decisions.

#### `new-design.md` rewrite pending

The doc still describes Aurora, pgvector, the RDS Data API and a VPC. Superseded by decisions of 2026-09-19 and 2026-09-20.

- **Data store.** DynamoDB only (vector search GA Aug 2026). No Aurora, no Data API, no VPC. Decisions 1, 2 and 6 are superseded; keep them and mark them so rather than deleting.
- **Open value.** Derived `po_invoice` item collection keyed by PO line (PK `PO#..#LINE#..`, SK `INVOICE#..#LINE#..`); sum in code. Written transactionally with the invoice, or via the stream.
- **Retrieval.** One vector per PO line, text = vendor name, PO description, line description, cost centre or WBSE description. Index A partitioned by `vendor_id` is the primary path, fanned out with one `SearchVectors` call per candidate vendor (filters are equality-only). Index B global is a rare fallback when A finds nothing, and its hits count as weaker evidence. `company_code` rejected as a partition key: vendors do bill the wrong company code.
- **Vendor fuzzy matching is not vector search.** Embeddings capture meaning, not spelling. Normalised vendor snapshot in versioned S3, rebuilt by a full-table-scan builder on vendor stream events, current version id in an SSM parameter read via the Powertools cache, rapidfuzz in the reader Lambda. The builder must not mutate the reader Lambda's environment variables.
- **No separate workflow table.** Status lives on the invoice item. `resolution_history` merged in as a `history` list (one compact entry per run: run id, recommended PO and line, confidence, user decision, comments, timestamp) plus a sparse GSI on `vendor_id` + `decided_at` for `get_vendor_history`. Project only the summary attributes into that index, not the OCR lines.
- **Tool Lambdas** grouped by permission boundary: reader, writer, email-handler. Email is sent by the stream consumer, never as an agent tool, so no agent-path role holds SES permission.
- **Trigger.** `invoice-cdc-handler` invokes the agent asynchronously (dashed on the diagram). Still needs an event-source-mapping on-failure destination, a maximum retry count and a maximum record age. Loop prevention belongs in the ESM filter, not in handler code.
- **Guardrails** attach at the model call, not through Policy. Prompt-attack filter on the OCR text as guard content (untrusted vendor input), bank-detail masking on output only, denied topics. Tool results are not guard content, or masking would break the matching. Ingest and retrieval sit outside guardrails entirely, governed by IAM and the event contract.
- **Governance.** Bedrock IAM scoped per principal: Runtime (chat model + ApplyGuardrail), PO Handler and reader (embedding model only). Nothing else in the account holds a Bedrock permission. No Bedrock VPC endpoint.
- **Auth.** Cognito Essentials tier, passwordless email OTP sent through the existing SES identity, no Amplify (two calls: `InitiateAuth` with `USER_AUTH`, then `RespondToAuthChallenge`). Pre sign-up and pre-authentication triggers share one allowlist in SSM. Groups from CDK `adminUsers` / `smeUsers` / `processOwners`; route-level authorization by turning groups into scopes with a pre-token-generation trigger, enforced by the JWT authorizer. Enable "prevent user existence errors".
- **Audit and observability.** Three layers: DynamoDB audit trail for facts and the millisecond UI timeline; Bedrock model invocation logs written directly to S3 as the transcript store, tagged via `requestMetadata` with `document_no`, `business_unit` and `run_id` and queried through Athena over a date range; CloudWatch logs, traces and alarms for operations. The separately agent-written transcript file is dropped. Record the reproducibility envelope (model, prompt version, tool schema version, vendor snapshot version) on the run item.
- **VPC out of scope.** Authorisation is complete without it. Note the private profile that a fork would add (Runtime in PRIVATE mode with a `bedrock-runtime` interface endpoint; internal ALB with a private REST API serving the SPA from S3) and why it is excluded: untestable outside a corporate network, and roughly $100 a month at idle.
- **Local mode.** DynamoDB Local, not Postgres in docker-compose. Reader and writer tool functions imported directly as LangChain tools; email stubbed to log.
- **New decision records.** Tool Lambdas grouped by permission boundary; email from a stream consumer rather than a tool; Python runtime with TypeScript CDK; DynamoDB over Aurora; vector search for line text but not for names; vendor snapshot over GSI blocking keys; VPC out of scope; CloudFront VPC origin considered and rejected; Cognito managed login rejected; single agent as an explicit graph with per-node spans, not multi-agent.
- **Keep as notes only.** Bank-detail matching (KMS HMAC `GenerateMac`, never cleartext, about $1 a month); customer-managed KMS key option on DynamoDB, S3 and SQS; nightly Glue conversion of invocation logs to Parquet once Athena scans get large.

https://docs.aws.amazon.com/solutions/agentic-erp-accounts-payable-and-receivable-exception-handling-on-aws/

2. Does yours look like reinventing or overcomplicating?
   It will look like reinventing if you don't address the AWS guidance. Anyone who knows it — and at an AWS partner, SA interviewers are exactly the people who will — will ask "why didn't you use the AWS guidance?" Silence suggests you didn't know it existed.

It won't if you position it as filling their gaps. Add a "Related work" section to the README that cites the guidance and says plainly what yours adds:

an exception type they don't cover
the matching depth they list as pending
an ERP event-integration design where they have none
labelled evaluation
a proactive trigger and explicit human approval states
Positioned like that, it reads as someone who studied AWS's reference and extended it, which is exactly SA behaviour.

Where it would genuinely look overcomplicated is the agent hosting layer. Your SQS → Lambda → LangChain host, custom observability and hand-built tool plumbing duplicate what AgentCore Runtime, Gateway and Observability now provide. Given your goal is an AWS SA role, and you're taking the AWS agentic course right now, I'd seriously consider swapping only that layer:

host the agent on AgentCore Runtime, with Strands
expose your tools through AgentCore Gateway
use AgentCore Observability
Keep everything that makes yours distinctive: the event ingestion, the data model, the DynamoDB workflow state, the evaluation set, and the human approval flow. That isn't changing the whole design, just the part AWS now offers as a managed service. You could then describe it as "built on AgentCore, extending the AWS reference into proactive, ERP-integrated, evaluated exception handling", which is a much stronger story than a parallel, hand-built stack.

If you keep LangChain instead, that's a legitimate choice, but write a decision record explaining it, with portability and local development parity as the reasons. And their code is MIT-0 licensed, so you can reuse any of their tool contracts or patterns freely without attribution.
