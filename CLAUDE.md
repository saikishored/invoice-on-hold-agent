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

## Gaps to Work on

1. Vendor master data should have Tax ID, Address.
2. Invoice text also should have billing address and tax id
3. Agent needs to validate by billing address and tax id
4. Need to decide how to validate bank details. May be by making a call to SAP with bank details. But how to get bank details mention on the invoice. Is it okay to include them in invoice text in invoice_headers.csv ?
5. Need to mention on Sanctions check and VAT check. A handler can be added that does legal compliance checks. If it not checks, at lest a +ve response can be given
6. DynamoDB holds only the invoice workflow state and streams to Lambda
7. Define agentLevel in config.ts - either 3 or 4. When set to 3, agent asks for AP SME confirmation. When set to 4, it decides the course of action based on confidence score
8. Need to think about context, business unit, common exceptions, vendor wise exceptions, duplicate check
9. circuit breakers, fallback mechanisms, retry policies
10. Instead of S3, use dynamodb for audit trail ? S3 can be used only if trail goes beyond 400 KB
11. Think about multi agent, if it is required. 2nd agent performs audit. Is it required. It could be a stream from Dynamodb that triggers audit agent
12. Data Governance
13. Feedback loop - when user_comments added, it should trigger an agent that summarizes and updates learnings
14. Performance metrics of agent need to be included in POC. Identify the agent metrics first
