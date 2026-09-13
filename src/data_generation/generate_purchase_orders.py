"""Generate synthetic purchase orders (headers and lines).

These are 2-way match, value-based purchase orders: there is no goods receipt,
and each line carries an amount rather than quantity x unit price (unit price
is always 1, so those columns would carry no information).

Two PO types are produced:

  adhoc      - a single line, one-off purchase, description carries the month
  quarterly  - one line per quarter, description carries the period. A PO may
               hold more than one line for the same quarter where different
               items or services were bought from that vendor in that period.

A PO line is consumed by one or more invoice lines, so consumed and open
amounts are derived from invoices rather than stored here.

Descriptions are the matching signal: invoice descriptions are compared
against PO header and line descriptions to find the right PO, so period,
service and item wording all matter.

Currency sits on the header, as it does in ERP systems, and is derived from
the legal suffix of the vendor name so a vendor always bills in one currency.
"""

import csv
import random
from datetime import date, timedelta
from pathlib import Path

from generate_master_data import (
    CHEMICALS,
    COST_CENTRES,
    DATASETS,
    INDUSTRIAL,
    INGREDIENTS,
    LOGISTICS,
    PACKAGING,
    REQUESTERS,
    VENDORS,
    WBS_ELEMENTS,
)

SEED = 20250912
PO_COUNT = 100
ADHOC_COUNT = 40
FIRST_PO_NUMBER = 4500000001

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

ITEMS = {
    INGREDIENTS: [
        "corn syrup supply", "cane sugar supply", "refined flour supply",
        "bakers yeast supply", "paprika extract supply", "cocoa powder supply",
        "food grade salt supply", "invert syrup supply",
    ],
    CHEMICALS: [
        "titanium dioxide supply", "avobenzone supply", "oxybenzone supply",
        "vitamin E acetate supply", "benzophenone supply",
        "metofluthrin supply", "emulsifier blend supply",
    ],
    PACKAGING: [
        "polypropylene resin supply", "polystyrene sheet supply",
        "corrugated carton supply", "flexible film supply",
        "bottle closure supply", "label stock supply", "shrink sleeve supply",
    ],
    INDUSTRIAL: [
        "preventive maintenance services", "equipment calibration services",
        "emergency callout support", "spare parts supply",
        "plant engineering support", "boiler inspection services",
        "conveyor servicing",
    ],
    LOGISTICS: [
        "inbound freight services", "outbound distribution services",
        "cold chain transport services", "warehouse handling services",
        "pallet storage services",
    ],
}

THEMES = {
    INGREDIENTS: "ingredient supply",
    CHEMICALS: "specialty chemical supply",
    PACKAGING: "packaging supply",
    INDUSTRIAL: "maintenance and engineering services",
    LOGISTICS: "logistics services",
}

# cost centres that plausibly carry each category of spend
COST_CENTRE_BY_CATEGORY = {
    INGREDIENTS: ["0000001010", "0000001020", "0000001030"],
    CHEMICALS: ["0000001030", "0000001210", "0000001220"],
    PACKAGING: ["0000001040", "0000001310"],
    INDUSTRIAL: ["0000001110", "0000001120", "0000001410"],
    LOGISTICS: ["0000001310", "0000001320", "0000001330"],
}

WBSE_BY_CATEGORY = {
    INGREDIENTS: ["PRJ-2024-007.1", "PRJ-2025-002.1"],
    CHEMICALS: ["PRJ-2025-002.1", "PRJ-2025-018.1"],
    PACKAGING: ["PRJ-2025-002.2", "PRJ-2025-018.1"],
    INDUSTRIAL: ["PRJ-2024-007.2", "PRJ-2025-009.1", "PRJ-2024-015.1"],
    LOGISTICS: ["PRJ-2025-011.1", "PRJ-2024-015.1"],
}

EUR_SUFFIXES = ("GmbH", "BV", "AS", "AG")
GBP_SUFFIXES = ("Ltd",)


def currency_for(vendor_name: str) -> str:
    last = vendor_name.split()[-1].rstrip(".")
    if last in EUR_SUFFIXES:
        return "EUR"
    if last in GBP_SUFFIXES:
        return "GBP"
    return "USD"


def _amount(rng: random.Random, low: int, high: int) -> str:
    """Round-ish amounts, mostly whole, with occasional odd cents."""
    value = rng.randint(low, high)
    if rng.random() < 0.35:
        value += rng.randrange(5, 100, 5) / 100
    return f"{value:.2f}"


def _adhoc_po_date(rng: random.Random, year: int, month_index: int) -> date:
    """Raised shortly before the month the purchase covers."""
    covered_from = date(year, month_index + 1, 1)
    return covered_from - timedelta(days=rng.randint(3, 40))


def _quarterly_po_date(rng: random.Random, year: int) -> date:
    """Annual agreements are put in place before the year they cover starts."""
    return date(year - 1, 11, 1) + timedelta(days=rng.randint(0, 55))


def _account_assignment(rng: random.Random, category: str) -> tuple[str, str]:
    """Every line carries a cost centre, a WBS element, or both."""
    roll = rng.random()
    cost_centre = rng.choice(COST_CENTRE_BY_CATEGORY[category])
    wbse = rng.choice(WBSE_BY_CATEGORY[category])
    if roll < 0.65:
        return cost_centre, ""
    if roll < 0.90:
        return "", wbse
    return cost_centre, wbse


def build() -> tuple[list[tuple], list[tuple]]:
    rng = random.Random(SEED)
    vendors = list(VENDORS)

    kinds = ["adhoc"] * ADHOC_COUNT + ["quarterly"] * (PO_COUNT - ADHOC_COUNT)
    rng.shuffle(kinds)

    headers: list[tuple] = []
    lines: list[tuple] = []

    for index, kind in enumerate(kinds):
        po_number = str(FIRST_PO_NUMBER + index)
        vendor_id, vendor_name, category = vendors[index % len(vendors)]
        currency = currency_for(vendor_name)
        catalogue = ITEMS[category]

        if kind == "adhoc":
            item = rng.choice(catalogue)
            year = rng.choice([2024, 2025])
            month_index = rng.randrange(len(MONTHS))
            po_date = _adhoc_po_date(rng, year, month_index)
            headers.append((
                po_number, vendor_id, po_date.isoformat(), currency,
                rng.choice(REQUESTERS), "Ad hoc purchase - " + item,
            ))
            cost_centre, wbse = _account_assignment(rng, category)
            lines.append((
                po_number, 10,
                "{} - {} {}".format(item, MONTHS[month_index], year),
                _amount(rng, 1_500, 60_000), cost_centre, wbse,
            ))
            continue

        year = rng.choice([2024, 2025])
        po_date = _quarterly_po_date(rng, year)
        headers.append((
            po_number, vendor_id, po_date.isoformat(), currency,
            rng.choice(REQUESTERS),
            "Annual agreement {} - {}".format(year, THEMES[category]),
        ))

        # one line per quarter, plus occasional extra lines within a quarter
        quarters = [1, 2, 3, 4]
        extras = rng.choices([0, 1, 2], weights=[60, 30, 10])[0]
        if extras:
            quarters += rng.sample([1, 2, 3, 4], k=extras)
        quarters.sort()

        used: set[tuple[int, str]] = set()
        line_number = 10
        for quarter in quarters:
            item = rng.choice(catalogue)
            while (quarter, item) in used:
                item = rng.choice(catalogue)
            used.add((quarter, item))
            cost_centre, wbse = _account_assignment(rng, category)
            lines.append((
                po_number, line_number,
                "Q{} {} - {}".format(quarter, year, item),
                _amount(rng, 12_000, 150_000), cost_centre, wbse,
            ))
            line_number += 10

    return headers, lines


def _write(path: Path, header: list[str], rows: list[tuple]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


def main() -> None:
    headers, lines = build()

    known_cost_centres = {c for c, *_ in COST_CENTRES}
    known_wbse = {w for w, _ in WBS_ELEMENTS}
    for po_number, line_number, _, _, cost_centre, wbse in lines:
        assert cost_centre or wbse, "{}/{} has no account assignment".format(
            po_number, line_number
        )
        assert not cost_centre or cost_centre in known_cost_centres
        assert not wbse or wbse in known_wbse

    _write(
        DATASETS / "purchase_order_headers.csv",
        ["po_number", "vendor_id", "po_date", "currency", "requester", "description"],
        headers,
    )
    _write(
        DATASETS / "purchase_order_lines.csv",
        ["po_number", "line_number", "description", "amount", "cost_centre", "wbse"],
        lines,
    )

    adhoc = sum(1 for h in headers if h[5].startswith("Ad hoc"))
    print("purchase orders: {}  (adhoc {}, quarterly {})".format(
        len(headers), adhoc, len(headers) - adhoc))
    print("lines:           {}".format(len(lines)))


if __name__ == "__main__":
    main()
