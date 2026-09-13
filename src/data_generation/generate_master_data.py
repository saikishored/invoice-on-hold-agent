"""Generate synthetic master data: vendors, cost centres and WBS elements.

Vendor names are invented, not taken from any real vendor master. The source
SAP sample data mixed real company names with obvious test entries, so neither
was safe to reuse -- only the style was kept.

Ids follow SAP conventions (vendors zero-padded to 10 chars, WBS elements
dotted project codes) because it costs nothing and stays recognisable to
anyone from a P2P background.

The vendor mix matches what a CPG manufacturer actually buys -- ingredients,
fine chemicals, packaging, industrial services and logistics -- so that
vendor/description pairings stay believable once purchase orders reference
them. Each vendor carries a CATEGORY used only by the purchase order
generator; it is not written to the CSV.

Three near-duplicate name pairs are planted on purpose (VARIANT_PAIRS) to
support the "invoice arrives under a group company's other trading name"
scenario.
"""

import csv
from pathlib import Path

DATASETS = Path(__file__).resolve().parents[2] / "datasets"

INGREDIENTS = "ingredients"
CHEMICALS = "chemicals"
PACKAGING = "packaging"
INDUSTRIAL = "industrial"
LOGISTICS = "logistics"

# (vendor_id, vendor_name, category)
VENDORS = [
    ("0000103417", "Marland Sweeteners Inc.", INGREDIENTS),
    ("0000104882", "Cane & Beet Refiners Ltd", INGREDIENTS),
    ("0000106235", "Amber Syrup Traders", INGREDIENTS),
    ("0000108710", "Prairie Grain Millers", INGREDIENTS),
    ("0000110264", "Northfield Flour Mills", INGREDIENTS),
    ("0000112938", "Yeast Dynamics Co.", INGREDIENTS),
    ("0000114501", "Harvest Spice Trading", INGREDIENTS),
    ("0000116877", "Paprika Imports BV", INGREDIENTS),
    ("0000118340", "Cocoa Basin Commodities", INGREDIENTS),
    ("0000119925", "Saltwell Minerals", INGREDIENTS),
    ("0000121608", "Helio Actives GmbH", CHEMICALS),
    ("0000123194", "Solmera Chemicals", CHEMICALS),
    ("0000125730", "Benzo Fine Chemicals", CHEMICALS),
    ("0000127016", "Vitamer Nutrients Inc.", CHEMICALS),
    ("0000128449", "Titania Pigments Ltd", CHEMICALS),
    ("0000130882", "Zephyr Agrochem", CHEMICALS),
    ("0000132157", "Cintara Specialty Chemicals", CHEMICALS),
    ("0000133604", "Avonia Actives", CHEMICALS),
    ("0000135271", "Nordlys Chemical AS", CHEMICALS),
    ("0000136938", "Prisma Ingredients", CHEMICALS),
    ("0000138405", "Polymer Craft Industries", PACKAGING),
    ("0000140112", "Styrene Works Ltd", PACKAGING),
    ("0000141766", "Verdant Packaging Co.", PACKAGING),
    ("0000143209", "Cartona Paper Mills", PACKAGING),
    ("0000144873", "FlexiFilm Converters", PACKAGING),
    ("0000146530", "Baltic Resin Traders", PACKAGING),
    ("0000148194", "Ecopack Solutions", PACKAGING),
    ("0000149657", "Orbit Closures Inc.", PACKAGING),
    ("0000151028", "Aurora Bottling Supplies", PACKAGING),
    ("0000152493", "Kestrel Labels & Print", PACKAGING),
    ("0000154170", "Sterling Manufacturing", INDUSTRIAL),
    ("0000155836", "Neroprox Industrial Co.", INDUSTRIAL),
    ("0000157291", "Engiss Industrial Supplies", INDUSTRIAL),
    ("0000158964", "Tyranex Solutions", INDUSTRIAL),
    ("0000160317", "Conficio Co.", INDUSTRIAL),
    ("0000161885", "Cappaberry Trading", INDUSTRIAL),
    ("0000163240", "Beckett Industrial", INDUSTRIAL),
    ("0000164918", "Gattis Engineering Works", INDUSTRIAL),
    ("0000166375", "Industry Intel Services", INDUSTRIAL),
    ("0000167802", "Highstone Maintenance", INDUSTRIAL),
    ("0000169446", "Vantex Equipment Co.", INDUSTRIAL),
    ("0000170913", "Ferrolux Metals", INDUSTRIAL),
    ("0000172588", "Dunbarrow Adhesives", INDUSTRIAL),
    ("0000174031", "Redline Freight Partners", LOGISTICS),
    ("0000175694", "Harborlink Logistics", LOGISTICS),
    ("0000177260", "Meridian Cold Chain", LOGISTICS),
    ("0000178835", "Castellan Warehousing", LOGISTICS),
    # planted name variants of existing vendors (see module docstring)
    ("0000180492", "Sterling Mfg Co.", INDUSTRIAL),
    ("0000182157", "Polymer Craft Ltd", PACKAGING),
    ("0000183704", "Northfield Mills Inc.", INGREDIENTS),
]

# confusable vendor_id -> vendor_id of the group company it resembles
VARIANT_PAIRS = {
    "0000180492": "0000154170",  # Sterling Mfg Co.     <-> Sterling Manufacturing
    "0000182157": "0000138405",  # Polymer Craft Ltd    <-> Polymer Craft Industries
    "0000183704": "0000110264",  # Northfield Mills Inc <-> Northfield Flour Mills
}

# (cost_centre_id, cost_centre_name, email)
# One owner mailbox per cost centre: who a hold on this spend routes to.
# All addresses use example.com, which RFC 2606 reserves for documentation,
# so nothing here can ever reach a real mailbox.
COST_CENTRES = [
    ("0000001010", "Production Line 1", "marta.kowalczyk@example.com"),
    ("0000001020", "Production Line 2", "dev.ramaswamy@example.com"),
    ("0000001030", "Blending and Mixing", "hannah.beaumont@example.com"),
    ("0000001040", "Packaging Hall", "tomas.eriksen@example.com"),
    ("0000001110", "Plant Maintenance", "grace.mbeki@example.com"),
    ("0000001120", "Utilities and Boilerhouse", "ivan.petrov@example.com"),
    ("0000001210", "Quality Laboratory", "lucia.ferraro@example.com"),
    ("0000001220", "Regulatory Affairs", "noor.haddad@example.com"),
    ("0000001310", "Warehouse Operations", "kenji.watanabe@example.com"),
    ("0000001320", "Inbound Logistics", "aoife.brennan@example.com"),
    ("0000001330", "Outbound Distribution", "samuel.adeyemi@example.com"),
    ("0000001410", "Facilities Management", "petra.novak@example.com"),
]

# People who raise purchase orders. Kept separate from cost centre owners:
# the person requesting the spend is usually not the person accountable for
# the cost centre, and a hold may need either of them.
REQUESTERS = [
    "elena.marchetti@example.com", "raj.chandrasekar@example.com",
    "fiona.oleary@example.com", "mateo.alvarez@example.com",
    "yuki.tanaka@example.com", "hassan.rahimi@example.com",
    "clara.bergstrom@example.com", "daniel.okonkwo@example.com",
    "priya.venkatesh@example.com", "lars.jorgensen@example.com",
    "imani.washington@example.com", "sofia.reyes@example.com",
    "viktor.horvath@example.com", "nadia.belkacem@example.com",
    "oliver.grant@example.com", "mei.lin@example.com",
    "ahmed.faruq@example.com", "greta.lindqvist@example.com",
    "joseph.mwangi@example.com", "camille.dubois@example.com",
]

# (wbse_id, wbse_name)
WBS_ELEMENTS = [
    ("PRJ-2024-007.1", "Line 2 Capacity Uplift - Civils"),
    ("PRJ-2024-007.2", "Line 2 Capacity Uplift - Equipment"),
    ("PRJ-2024-015.1", "Cold Store Expansion - Build"),
    ("PRJ-2025-002.1", "Sunscreen Range Launch - Formulation"),
    ("PRJ-2025-002.2", "Sunscreen Range Launch - Packaging"),
    ("PRJ-2025-009.1", "Boiler Replacement Programme"),
    ("PRJ-2025-011.1", "Warehouse Automation - Phase 1"),
    ("PRJ-2025-018.1", "Sustainable Packaging Trial"),
]


def _write(path: Path, header: list[str], rows: list[tuple]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


def main() -> None:
    vendor_rows = [(vid, name) for vid, name, _ in VENDORS]
    assert len({r[0] for r in vendor_rows}) == len(vendor_rows), "duplicate vendor_id"
    assert len({r[1] for r in vendor_rows}) == len(vendor_rows), "duplicate vendor_name"

    _write(DATASETS / "master_data_vendors.csv", ["vendor_id", "vendor_name"], vendor_rows)
    _write(
        DATASETS / "master_data_cost_centre.csv",
        ["cost_centre_id", "cost_centre_name", "email"],
        COST_CENTRES,
    )
    _write(DATASETS / "master_data_wbse.csv", ["wbse_id", "wbse_name"], WBS_ELEMENTS)

    print(f"vendors:      {len(vendor_rows):>3}")
    print(f"cost centres: {len(COST_CENTRES):>3}")
    print(f"wbs elements: {len(WBS_ELEMENTS):>3}")


if __name__ == "__main__":
    main()
