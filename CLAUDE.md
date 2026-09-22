# invoice-on-hold-agent

This is a POC to show how AI Agent can be leveraged in P2P Domain.

## Domain and Use Case

In Procure to Pay (P2P) domain, an invoice gets on hold for various reasons. As this is a POC, this only targets one specific reason 'PO Mismatch' (2 Way).

PO Mismatch refers to PO on the invoice either does not exist or belongs to another vendor

## Stack

Python
LangChain
Streamlit
Postgres
pgvector
docker-compose
gemini-3.6-flash

folder `src` contains the code
folder `datasets` contains synthetic data

## Key Assumptions

1. **Vendor group keys are not maintained.** ERP systems carry a field linking trading entities of the same supplier group (SAP: `LFA1-KONZS`), but in practice it is often left unpopulated. This POC assumes it is empty, so when an invoice arrives from a sister entity of the PO vendor, the agent must weigh the evidence rather than look the relationship up.
2. Invoice description, line items texts are pre populated by OCR at the time of scanning. File `datasets/invoices_headers.csv` and `datasets/invoices_lines.csv` are generated with this assumption. This is key for digital transformation of Accounts Payable in scanning process.
3. **Tax ID and address identify the vendor, and OCR reads them imperfectly.** The vendor master carries `tax_id`, `street`, `city`, `postal_code` and `country` (SAP: `LFA1` STCEG, STRAS, ORT01, PSTLZ, LAND1). Each invoice carries the vendor's own billing block as scanned: `billing_tax_id` and `billing_address`, the latter flattened to one line. This is the vendor billing, not a buyer-side "bill to", which would be identical on every invoice and carry no signal. Roughly one invoice in seven has no tax id at all, because the line did not survive the scan, so a missing tax id must never be read as evidence. Together with assumption 1, this is how a supplier group is inferred: the planted sister entities share a registered address but each files under its own tax ID, so a differing tax ID disproves "same entity" while a shared address and a similar name evidence "same group".

## Gaps to Work on

1. Agent needs to validate by billing address and tax id. The data is in place (see assumption 3); the matching logic is not written yet.
2. Define agentLevel in config.ts - either 3 or 4. When set to 3, agent asks for AP SME confirmation. When set to 4, it decides the course of action based on confidence score
3. Need to think about context, business unit, common exceptions, vendor wise exceptions, duplicate check
4. circuit breakers, fallback mechanisms, retry policies
5. Instead of S3, use dynamodb for audit trail ? S3 can be used only if trail goes beyond 400 KB
6. Data Governance
7. Feedback loop - when user_comments added, it should trigger an agent that summarizes and updates learnings
8. Performance metrics of agent need to be included in POC. Identify the agent metrics first
