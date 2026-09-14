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
