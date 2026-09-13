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
