"""Generate synthetic invoices, plus the expected resolution for each hold.

Invoice text is assumed to come from OCR at scanning time, so it is the
vendor's own wording, never the buyer's PO wording. That is the whole point:
the agent has to match meaning, not strings.

Header fields:
  invoice_description  the invoice's own header text, as scanned
  lines_description    the detail table as OCR captured it, JSON encoded. Real
                       scanners vary, so some invoices carry plain strings and
                       others carry {sno, text, qty, amount} objects.
  billing_tax_id       the vendor's registration number, off the invoice
  billing_address      the vendor's billing address, flattened to one line

The last two are the billing block the vendor prints on its own invoice --
who is billing, not who is being billed. A buyer-side "bill to" is the same
on every invoice here and so carries no signal; the vendor's own details are
what tie the paper to a row in the vendor master.

They arrive by OCR like everything else, so they are the dirty copy of clean
master data: the address comes flattened into one comma-separated line, street
types are sometimes abbreviated, and a postcode or country line is sometimes
lost off the edge. The tax id is missed outright on about one invoice in
seven, which is the realistic case the agent has to survive -- a missing tax
id is not evidence of anything.

Matching them back gives the agent two independent checks the description
paths cannot give it: a tax id that differs from the cited PO's vendor
disproves "same entity", and an address that matches that vendor's anyway is
what distinguishes a sister company from a stranger. The vendor_group
invoices therefore always carry a complete, legible billing block; without it
that scenario is not decidable and the expected answer would be unfair.

Statuses:
  posted  clean invoices that matched their PO. They consume PO line value,
          so open value is a real constraint the agent can reason about.
  parked  on hold. Every parked invoice cites a PO number that exists but
          belongs to a different vendor -- that is the hold reason in scope.

Invoice lines mirror SAP's RSEG, not the OCR detail: each line references
exactly one PO line and carries the amount it consumes. Only posted invoices
have lines. A parked invoice with a PO mismatch has none, because the system
cannot propose lines from a PO that belongs to another vendor; its detail
exists only as OCR text in lines_description. Resolving the hold is what
creates its lines.

line_text mirrors RSEG-SGTXT: a short note of at most 50 characters. On posted
invoices it is what an AP clerk typed, abbreviated from the invoice. Lines the
agent creates on resolution carry an "Agent: ..." note instead, so the UI can
tell who booked each line. Consumed value per PO line is the sum of invoice line
amounts against it, as SAP derives it from EKBE.

The 30 parked invoices resolve through four different evidence paths, so no
single signal is sufficient:

  po_line      10  invoice text paraphrases the PO line item
  cost_centre   5  invoice text paraphrases the cost centre the spend sits on
  wbse          5  invoice text paraphrases the WBS element
  vendor_group  5  cited PO is correct; the invoice is from a sister entity of
                   the PO vendor, so the apparent mismatch is legitimate
  unresolved    5  nothing matches on any path; needs a human

datasets/expected_resolutions.csv records the answer for every parked invoice
so the agent can be scored rather than eyeballed.
"""

import csv
import json
import random
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from generate_master_data import DATASETS, VARIANT_PAIRS
from generate_purchase_orders import currency_for

SEED = 20250913
POSTED_COUNT = 120
FIRST_INVOICE_NUMBER = 5105600001

SCENARIO_COUNTS = {
    "po_line": 10,
    "cost_centre": 5,
    "wbse": 5,
    "vendor_group": 5,
    "unresolved": 5,
}

QUARTER_PHRASES = {
    1: ["first quarter {y}", "Jan-Mar {y}", "Q1/{y}", "January to March {y}"],
    2: ["second quarter {y}", "Apr-Jun {y}", "Q2/{y}", "April to June {y}"],
    3: ["third quarter {y}", "Jul-Sep {y}", "Q3/{y}", "July to September {y}"],
    4: ["fourth quarter {y}", "Oct-Dec {y}", "Q4/{y}", "October to December {y}"],
}

MONTH_NAMES = {
    "Jan": "January", "Feb": "February", "Mar": "March", "Apr": "April",
    "May": "May", "Jun": "June", "Jul": "July", "Aug": "August",
    "Sep": "September", "Oct": "October", "Nov": "November", "Dec": "December",
}
MONTH_INDEX = {m: i + 1 for i, m in enumerate(MONTH_NAMES)}

GOODS_TEMPLATES = [
    "{noun} deliveries", "supply of {noun}", "{noun} consignments",
    "charges for {noun}", "{noun} - goods supplied",
]
SERVICE_TEMPLATES = [
    "{noun} works", "provision of {noun}", "charges for {noun}",
    "{noun} carried out", "{noun} - services rendered",
]

HEADER_TEMPLATES = [
    "Invoice for {body}",
    "{body}",
    "Billing - {body}",
    "{body} as per agreement",
    "Tax invoice: {body}",
]

# incidental lines a vendor adds alongside the substantive one
FILLER_LINES = [
    "handling charges", "delivery charges", "administration fee",
    "fuel surcharge", "packaging and freight",
]

# Street types as a scanner abbreviates them. Continental street names are one
# word, so the suffix is abbreviated in place.
STREET_ABBREVIATIONS = [
    ("Road", "Rd"), ("Street", "St"), ("Avenue", "Ave"), ("Drive", "Dr"),
    ("Lane", "Ln"), ("Boulevard", "Blvd"), ("Parkway", "Pkwy"),
    ("Trading Estate", "Trd Est"), ("strasse", "str."),
]

# the two companies of a planted pair, in both directions
GROUP_SIBLINGS: dict[str, str] = {}
for _variant, _parent in VARIANT_PAIRS.items():
    GROUP_SIBLINGS[_variant] = _parent
    GROUP_SIBLINGS[_parent] = _variant

# spend this buyer never raises a PO for, used for the unresolvable cases
UNRELATED_ITEMS = [
    "legal advisory retainer", "market research study",
    "executive coaching programme", "trade show stand hire",
    "brand photography shoot", "recruitment agency fee",
    "translation services", "office plant hire",
]


def _read(name: str) -> list[dict]:
    with (DATASETS / name).open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _noun(item: str) -> tuple[str, bool]:
    """Strip the PO wording down to the thing itself. Returns (noun, is_service)."""
    if item.endswith(" supply"):
        return item[: -len(" supply")], False
    if item.endswith(" services"):
        return item[: -len(" services")], True
    # anything else ("conveyor servicing", "emergency callout support") reads
    # correctly as-is; stripping the tail produced "provision of conveyor"
    return item, True


def _paraphrase_item(rng: random.Random, item: str) -> str:
    noun, is_service = _noun(item)
    template = rng.choice(SERVICE_TEMPLATES if is_service else GOODS_TEMPLATES)
    return template.format(noun=noun)


def _paraphrase_period(rng: random.Random, description: str) -> tuple[str, date]:
    """Return vendor-style period wording plus the date the period ends."""
    if description.startswith("Q"):
        quarter = int(description[1])
        year = int(description.split()[1])
        phrase = rng.choice(QUARTER_PHRASES[quarter]).format(y=year)
        end_month = quarter * 3
        end = date(year + (end_month == 12 and 0 or 0), end_month, 28)
        return phrase, end
    # adhoc: "<item> - Sep 2025"
    tail = description.split(" - ")[-1]
    month, year_text = tail.split()
    year = int(year_text)
    phrase = rng.choice([
        "{} {}".format(MONTH_NAMES[month], year),
        "{}/{}".format(month, year),
        "month of {} {}".format(MONTH_NAMES[month], year),
    ])
    return phrase, date(year, MONTH_INDEX[month], 28)


def _paraphrase_name(rng: random.Random, name: str) -> str:
    """Loosen a cost centre or WBS element name into vendor-style wording."""
    cleaned = name.replace(" - ", " ").lower()
    return rng.choice([
        "{} activities".format(cleaned),
        "work relating to {}".format(cleaned),
        "{} - support".format(cleaned),
        "services for {}".format(cleaned),
    ])


def _period_key(description: str) -> str:
    """The period a PO line covers: "Q2 2025" or "Sep 2025"."""
    if description.startswith("Q"):
        return description.split(" - ")[0]
    return description.split(" - ")[-1]


def _uniquely_identified(scenario: str, candidate: dict, vendor_lines: list[dict]) -> bool:
    """True when no other line of this vendor shares the evidence and period.

    Without this, an invoice naming only a cost centre and a quarter can point
    at two PO lines equally well, and the expected answer becomes unfair.
    """
    period = _period_key(candidate["description"])
    rivals = [l for l in vendor_lines if _period_key(l["description"]) == period]
    if scenario == "cost_centre":
        rivals = [l for l in rivals if l["cost_centre"] == candidate["cost_centre"]]
    elif scenario == "wbse":
        rivals = [l for l in rivals if l["wbse"] == candidate["wbse"]]
    else:
        def item_of(d):
            return d.split(" - ")[-1] if d.startswith("Q") else d.split(" - ")[0]
        rivals = [l for l in rivals
                  if item_of(l["description"]) == item_of(candidate["description"])]
    return len(rivals) == 1


LINE_TEXT_MAX = 50

# how clerks shorten things when typing a line note
ABBREVIATIONS = [
    ("preventive maintenance", "prev maint"), ("maintenance", "maint"),
    ("equipment", "equip"), ("calibration", "calib"), ("engineering", "eng"),
    ("inspection", "insp"), ("emergency", "emerg"), ("distribution", "distrib"),
    ("transport", "trnsp"), ("warehouse", "whse"), ("services", "svcs"),
    ("polypropylene", "PP"), ("polystyrene", "PS"), ("titanium dioxide", "TiO2"),
    ("vitamin E acetate", "Vit E acetate"), ("corrugated", "corr"),
    ("emulsifier", "emuls"), ("benzophenone", "benzophen"),
]


def _clerk_line_text(rng: random.Random, po_line_description: str) -> str:
    """A short, abbreviated note in the style an AP clerk types into SGTXT."""
    description = po_line_description
    if description.startswith("Q"):
        item = description.split(" - ")[-1]
        quarter, year = description.split(" - ")[0].split()
        period = rng.choice(["{}/{}".format(quarter, year[2:]), "{} {}".format(quarter, year[2:]),
                             "{}-{}".format(quarter, year)])
    else:
        item = description.split(" - ")[0]
        month, year = description.split(" - ")[-1].split()
        period = rng.choice(["{}{}".format(month, year[2:]), "{} {}".format(month, year),
                             "{:02d}/{}".format(MONTH_INDEX[month], year[2:])])
    noun, _ = _noun(item)
    for long, short in ABBREVIATIONS:
        noun = noun.replace(long, short)
    text = rng.choice(["{n} {p}", "{p} {n}", "{n} - {p}"]).format(n=noun, p=period)
    return (text[0].upper() + text[1:])[:LINE_TEXT_MAX]


def _ocr_tax_id(rng: random.Random, tax_id: str, allow_missing: bool = True) -> str:
    """The registration number as the scanner read it off the invoice.

    Vendors print the same number several ways, so separators are not
    reliable: an EIN loses its hyphen, a VAT number gains a space after the
    country prefix. Comparison has to strip them.
    """
    roll = rng.random()
    if allow_missing and roll < 0.15:
        return ""  # the tax line was cropped or too faint to read
    if roll < 0.40:
        if "-" in tax_id:
            return tax_id.replace("-", "")
        return "{} {}".format(tax_id[:2], tax_id[2:])
    return tax_id


def _ocr_address(rng: random.Random, vendor: dict, complete: bool = False) -> str:
    """The vendor's address block, flattened the way OCR flattens it."""
    street = vendor["street"]
    if rng.random() < 0.30:
        for long, short in STREET_ABBREVIATIONS:
            street = street.replace(long, short)

    parts = [street, vendor["city"], vendor["postal_code"], vendor["country"]]
    if not complete:
        roll = rng.random()
        if roll < 0.15:
            del parts[2]  # postcode not picked up
        elif roll < 0.35:
            del parts[3]  # country line cropped off the bottom
    return ", ".join(parts)


def _amount(value: float) -> str:
    return "{:.2f}".format(round(value, 2))


def _split_amount(rng: random.Random, total: float, parts: int) -> list[float]:
    if parts == 1:
        return [total]
    cuts = sorted(rng.uniform(0.15, 0.85) for _ in range(parts - 1))
    shares, previous = [], 0.0
    for cut in cuts:
        shares.append(cut - previous)
        previous = cut
    shares.append(1.0 - previous)
    amounts = [round(total * s, 2) for s in shares]
    amounts[-1] = round(total - sum(amounts[:-1]), 2)
    return amounts


def _lines_description(rng: random.Random, texts: list[str], amounts: list[float]) -> str:
    """OCR output shape varies by scanner: sometimes structured, sometimes not."""
    if rng.random() < 0.5:
        return json.dumps(texts)
    return json.dumps([
        {"sno": i + 1, "text": text, "qty": 1, "amount": _amount(amount)}
        for i, (text, amount) in enumerate(zip(texts, amounts))
    ])


def build() -> tuple[list[tuple], list[tuple], list[tuple]]:
    rng = random.Random(SEED)

    vendor_master = {v["vendor_id"]: v for v in _read("master_data_vendors.csv")}
    vendors = {vid: v["vendor_name"] for vid, v in vendor_master.items()}
    cost_centres = {c["cost_centre_id"]: c["cost_centre_name"]
                    for c in _read("master_data_cost_centre.csv")}
    wbs = {w["wbse_id"]: w["wbse_name"] for w in _read("master_data_wbse.csv")}
    po_headers = {h["po_number"]: h for h in _read("purchase_order_headers.csv")}
    po_lines = _read("purchase_order_lines.csv")

    open_value = {(l["po_number"], l["line_number"]): float(l["amount"]) for l in po_lines}
    lines_by_vendor = defaultdict(list)
    for line in po_lines:
        lines_by_vendor[po_headers[line["po_number"]]["vendor_id"]].append(line)

    note_rng = random.Random(SEED + 1)
    # OCR noise draws from its own stream, so adding it leaves every other
    # column of every existing invoice exactly where it was
    ocr_rng = random.Random(SEED + 2)

    headers: list[tuple] = []
    lines: list[tuple] = []
    expected: list[tuple] = []
    counter = [0]

    def next_invoice_no() -> str:
        counter[0] += 1
        return str(FIRST_INVOICE_NUMBER + counter[0] - 1)

    def emit(vendor_id, cited_po, po_line, status, body, texts, total, period_end,
             full_billing_block=False):
        invoice_no = next_invoice_no()
        invoice_date = period_end + timedelta(days=rng.randint(5, 40))
        currency = currency_for(vendors[vendor_id])
        amounts = _split_amount(rng, total, len(texts))
        vendor = vendor_master[vendor_id]
        headers.append((
            invoice_no, vendor_id, invoice_date.isoformat(), currency,
            cited_po, _amount(total), status,
            rng.choice(HEADER_TEMPLATES).format(body=body),
            _lines_description(rng, texts, amounts),
            _ocr_tax_id(ocr_rng, vendor["tax_id"], allow_missing=not full_billing_block),
            _ocr_address(ocr_rng, vendor, complete=full_billing_block),
        ))
        if status == "posted":
            # the vendor may split the invoice into several OCR rows, but SAP
            # holds a single line consuming the matched PO line
            lines.append((
                invoice_no, 10, po_line["po_number"], po_line["line_number"],
                _amount(total), _clerk_line_text(note_rng, po_line["description"]),
            ))
        if po_line is not None:
            key = (po_line["po_number"], po_line["line_number"])
            open_value[key] = round(open_value[key] - total, 2)
        return invoice_no

    def pick_line(vendor_id, minimum=2_000.0, require=None):
        candidates = [
            l for l in lines_by_vendor[vendor_id]
            if open_value[(l["po_number"], l["line_number"])] > minimum
            and (require is None or l[require])
        ]
        return rng.choice(candidates) if candidates else None

    def texts_for(po_line, body, count):
        """First line carries the matching wording; any others are filler."""
        out = [body]
        for _ in range(count - 1):
            out.append(rng.choice(FILLER_LINES))
        return out

    # ---------------- posted invoices ----------------
    vendor_ids = [v for v in vendors if lines_by_vendor[v]]
    while counter[0] < POSTED_COUNT:
        vendor_id = rng.choice(vendor_ids)
        po_line = pick_line(vendor_id, minimum=3_000.0)
        if po_line is None:
            continue
        key = (po_line["po_number"], po_line["line_number"])
        remaining = open_value[key]
        total = round(remaining * rng.uniform(0.25, 0.85), 2)
        item = po_line["description"].split(" - ")[-1] if po_line["description"].startswith("Q") \
            else po_line["description"].split(" - ")[0]
        period, period_end = _paraphrase_period(rng, po_line["description"])
        body = "{}, {}".format(_paraphrase_item(rng, item), period)
        emit(vendor_id, po_line["po_number"], po_line, "posted", body,
             texts_for(po_line, body, rng.choices([1, 2, 3], weights=[55, 30, 15])[0]),
             total, period_end)

    # ---------------- parked invoices ----------------
    def cited_po_from_other_vendor(vendor_id: str) -> str:
        """A PO belonging to someone else, and never to a group sibling.

        A sibling's PO would make the invoice a vendor_group case -- same
        address, different tax id -- with a different right answer to the one
        the scenario records.
        """
        excluded = {vendor_id, GROUP_SIBLINGS.get(vendor_id)}
        while True:
            po_number, header = rng.choice(list(po_headers.items()))
            if header["vendor_id"] not in excluded:
                return po_number

    scenarios = [s for s, n in SCENARIO_COUNTS.items() for _ in range(n)]
    for scenario in scenarios:
        if scenario == "vendor_group":
            variant_id = rng.choice(list(VARIANT_PAIRS))
            parent_id = VARIANT_PAIRS[variant_id]
            po_line = pick_line(parent_id, minimum=5_000.0)
            if po_line is None:
                continue
            item = po_line["description"].split(" - ")[-1] if po_line["description"].startswith("Q") \
                else po_line["description"].split(" - ")[0]
            period, period_end = _paraphrase_period(rng, po_line["description"])
            body = "{}, {}".format(_paraphrase_item(rng, item), period)
            key = (po_line["po_number"], po_line["line_number"])
            total = round(open_value[key] * rng.uniform(0.3, 0.7), 2)
            invoice_no = emit(variant_id, po_line["po_number"], po_line, "parked",
                              body, texts_for(po_line, body, 1), total, period_end,
                              full_billing_block=True)
            expected.append((
                invoice_no, "accept_cited_po", po_line["po_number"],
                po_line["line_number"], "vendor_group",
                po_headers[po_line["po_number"]]["requester"],
                "Invoice from {} against a PO held by {}; separate tax ids "
                "filed from one registered address, so the two are the same "
                "supplier group and the cited PO is correct".format(
                    vendors[variant_id], vendors[parent_id]),
            ))
            continue

        if scenario == "unresolved":
            vendor_id = rng.choice(vendor_ids)
            cited = cited_po_from_other_vendor(vendor_id)
            item = rng.choice(UNRELATED_ITEMS)
            year = rng.choice([2024, 2025])
            quarter = rng.randint(1, 4)
            period = rng.choice(QUARTER_PHRASES[quarter]).format(y=year)
            body = "{}, {}".format(item, period)
            period_end = date(year, quarter * 3, 28)
            invoice_no = emit(vendor_id, cited, None, "parked", body, [body],
                              round(rng.uniform(3_000, 28_000), 2), period_end)
            expected.append((
                invoice_no, "escalate", "", "", "unresolved",
                po_headers[cited]["requester"],
                "No purchase order for {} covers {}; needs the requester to "
                "confirm whether a PO exists.".format(vendors[vendor_id], item),
            ))
            continue

        require = {"po_line": None, "cost_centre": "cost_centre", "wbse": "wbse"}[scenario]
        vendor_id, po_line = None, None
        for _ in range(200):
            candidate_vendor = rng.choice(vendor_ids)
            candidate = pick_line(candidate_vendor, minimum=5_000.0, require=require)
            if candidate is not None and _uniquely_identified(
                scenario, candidate, lines_by_vendor[candidate_vendor]
            ):
                vendor_id, po_line = candidate_vendor, candidate
                break
        if po_line is None:
            continue

        period, period_end = _paraphrase_period(rng, po_line["description"])
        if scenario == "po_line":
            item = po_line["description"].split(" - ")[-1] if po_line["description"].startswith("Q") \
                else po_line["description"].split(" - ")[0]
            body = "{}, {}".format(_paraphrase_item(rng, item), period)
            note = "Invoice text paraphrases the PO line item."
        elif scenario == "cost_centre":
            body = "{}, {}".format(
                _paraphrase_name(rng, cost_centres[po_line["cost_centre"]]), period)
            note = "Invoice text points at cost centre {} ({}), not the line item.".format(
                po_line["cost_centre"], cost_centres[po_line["cost_centre"]])
        else:
            body = "{}, {}".format(_paraphrase_name(rng, wbs[po_line["wbse"]]), period)
            note = "Invoice text points at WBS element {} ({}), not the line item.".format(
                po_line["wbse"], wbs[po_line["wbse"]])

        cited = cited_po_from_other_vendor(vendor_id)
        key = (po_line["po_number"], po_line["line_number"])
        total = round(open_value[key] * rng.uniform(0.3, 0.7), 2)
        invoice_no = emit(vendor_id, cited, po_line, "parked", body,
                          texts_for(po_line, body, rng.choices([1, 2], weights=[70, 30])[0]),
                          total, period_end)
        expected.append((
            invoice_no, "reassign_po", po_line["po_number"], po_line["line_number"],
            scenario, po_headers[po_line["po_number"]]["requester"],
            "Cited PO {} belongs to {}; {}".format(
                cited, vendors[po_headers[cited]["vendor_id"]].rstrip("."), note),
        ))

    return headers, lines, expected


def _write(path: Path, header: list[str], rows: list[tuple]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


def main() -> None:
    headers, lines, expected = build()

    parked = [h for h in headers if h[6] == "parked"]
    assert len(parked) == sum(SCENARIO_COUNTS.values()), "parked count drifted"
    assert len(expected) == len(parked), "every parked invoice needs an expected row"
    for header in parked:
        assert header[4], "parked invoice must cite a PO number"
    parked_numbers = {h[0] for h in parked}
    assert not any(l[0] in parked_numbers for l in lines), "parked invoices have no lines"

    # the group scenario is only decidable if its billing block survived OCR
    group_invoices = {e[0] for e in expected if e[4] == "vendor_group"}
    for header in headers:
        assert header[10], "{} has no billing address".format(header[0])
        if header[0] in group_invoices:
            assert header[9], "{} needs a legible tax id".format(header[0])

    _write(
        DATASETS / "invoices_headers.csv",
        ["invoice_no", "vendor_id", "invoice_date", "currency", "po_number",
         "amount", "status", "invoice_description", "lines_description",
         "billing_tax_id", "billing_address"],
        headers,
    )
    _write(
        DATASETS / "invoices_lines.csv",
        ["invoice_no", "line_number", "po_number", "po_line_number", "amount", "line_text"],
        lines,
    )
    _write(
        DATASETS / "expected_resolutions.csv",
        ["invoice_no", "outcome", "expected_po_number", "expected_po_line",
         "scenario", "contact_email", "notes"],
        expected,
    )

    print("invoices: {} ({} posted, {} parked)".format(
        len(headers), len(headers) - len(parked), len(parked)))
    print("lines:    {}".format(len(lines)))
    print("expected: {}".format(len(expected)))


if __name__ == "__main__":
    main()
