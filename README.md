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
