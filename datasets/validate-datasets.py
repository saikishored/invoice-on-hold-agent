#!/usr/bin/env python3
"""Validate integrity across the synthetic datasets in this folder.

    python datasets/validate-datasets.py          # run every check
    python datasets/validate-datasets.py --list   # just list what is checked
    python datasets/validate-datasets.py -a       # show every violation, not
                                                  # the first few

Exit code is 0 when no error-severity check failed, 1 otherwise, so this can
sit in CI or a pre-commit hook.

This deliberately imports nothing from src/data_generation. The generators
assert their own invariants as they write; this script re-derives them from
the CSVs alone, so it still catches a hand-edited row, a half-finished
regeneration (one file rewritten, the others stale) or a generator change that
silently broke a downstream file. Where it needs to know something the
generators know -- how a scanner abbreviates a street type, say -- it holds
its own copy and says so.

Checks are grouped by what they protect:

    FILE  the files exist and have the columns everything else assumes
    KEY   primary keys are unique
    REF   every foreign key points at a row that exists
    FMT   field formats: ids, amounts, dates, codes, OCR payloads
    DOM   P2P business rules: matching, consumption, currency
    EVAL  expected_resolutions is a usable answer key
    VEND  vendor identity: tax id and address, master against invoice

To add a check, write a function that returns a list of human-readable
violations (empty means pass) and decorate it with @check.
"""

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

DATASETS = Path(__file__).resolve().parent

# short name -> (file name, exact column order)
SCHEMA = {
    "vendors": ("master_data_vendors.csv", [
        "vendor_id", "vendor_name", "tax_id", "street", "city", "postal_code",
        "country"]),
    "cost_centres": ("master_data_cost_centre.csv", [
        "cost_centre_id", "cost_centre_name", "email"]),
    "wbse": ("master_data_wbse.csv", ["wbse_id", "wbse_name"]),
    "po_headers": ("purchase_order_headers.csv", [
        "po_number", "vendor_id", "po_date", "currency", "requester",
        "description"]),
    "po_lines": ("purchase_order_lines.csv", [
        "po_number", "line_number", "description", "amount", "cost_centre",
        "wbse"]),
    "inv_headers": ("invoices_headers.csv", [
        "invoice_no", "vendor_id", "invoice_date", "currency", "po_number",
        "amount", "status", "invoice_description", "lines_description",
        "billing_tax_id", "billing_address"]),
    "inv_lines": ("invoices_lines.csv", [
        "invoice_no", "line_number", "po_number", "po_line_number", "amount",
        "line_text"]),
    "expected": ("expected_resolutions.csv", [
        "invoice_no", "outcome", "expected_po_number", "expected_po_line",
        "scenario", "contact_email", "notes"]),
}

STATUSES = {"posted", "parked"}
CURRENCIES = {"USD", "EUR", "GBP"}
OUTCOMES = {"reassign_po", "accept_cited_po", "escalate"}
SCENARIOS = {"po_line", "cost_centre", "wbse", "vendor_group", "unresolved"}
SGTXT_MAX = 50  # SAP RSEG-SGTXT is 50 characters

# How the scanner shortens a street type. This mirrors the OCR simulation in
# src/data_generation/generate_invoices.py; it is repeated here so the
# validator can read an invoice address without importing the generator.
STREET_EXPANSIONS = {
    "RD": "ROAD", "ST": "STREET", "AVE": "AVENUE", "DR": "DRIVE",
    "LN": "LANE", "BLVD": "BOULEVARD", "PKWY": "PARKWAY",
    "TRD": "TRADING", "EST": "ESTATE",
}


# --------------------------------------------------------------------------
# check registry
# --------------------------------------------------------------------------

CHECKS = []


def check(check_id, title, severity="error"):
    """Register a check. The function returns a list of violations."""
    def register(fn):
        CHECKS.append((check_id, title, severity, fn))
        return fn
    return register


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _money(value):
    """Decimal, or None when the field is not a usable amount (FMT-02 reports)."""
    try:
        return Decimal(value)
    except (InvalidOperation, TypeError):
        return None


def _day(value):
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _norm_tax_id(value):
    """Tax ids are printed with varying separators; compare on the characters."""
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def _canonical_street(value):
    """Expand a scanned street back to the wording the vendor master uses."""
    tokens = []
    for token in re.findall(r"[A-Z0-9]+", (value or "").upper()):
        if len(token) > 3 and token.endswith("STR"):  # Werkstr. -> Werkstrasse
            token = token[:-3] + "STRASSE"
        tokens.append(STREET_EXPANSIONS.get(token, token))
    return " ".join(tokens)


def _address_parts(value):
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def _counts(rows, *keys):
    return Counter(tuple(row[k] for k in keys) for row in rows)


def _fmt_key(key):
    return "/".join(key) if isinstance(key, tuple) else str(key)


# --------------------------------------------------------------------------
# FILE -- the files exist and look like what every other check assumes
# --------------------------------------------------------------------------

def load():
    """Read every dataset. Returns (tables, violations); tables is {} on failure."""
    problems = []
    tables = {}
    for name, (filename, columns) in SCHEMA.items():
        path = DATASETS / filename
        if not path.exists():
            problems.append("{}: missing".format(filename))
            continue
        with path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f, restkey="__extra__", restval=None)
            header = reader.fieldnames or []
            if header != columns:
                problems.append("{}: columns are {}, expected {}".format(
                    filename, header, columns))
                continue
            rows = list(reader)
        if not rows:
            problems.append("{}: no data rows".format(filename))
            continue
        for number, row in enumerate(rows, start=2):
            if "__extra__" in row:
                problems.append("{} line {}: {} extra field(s)".format(
                    filename, number, len(row["__extra__"])))
            elif any(value is None for value in row.values()):
                problems.append("{} line {}: short row".format(filename, number))
            elif not any((value or "").strip() for value in row.values()):
                problems.append("{} line {}: blank row".format(filename, number))
        tables[name] = rows

    return ({} if problems else tables), problems


# --------------------------------------------------------------------------
# KEY -- primary keys
# --------------------------------------------------------------------------

def _unique(rows, label, *keys):
    return ["{} appears {} times: {}".format(label, count, _fmt_key(key))
            for key, count in _counts(rows, *keys).items() if count > 1]


@check("KEY-01", "vendor_id is unique in the vendor master")
def key_01(d):
    return _unique(d["vendors"], "vendor_id", "vendor_id")


@check("KEY-02", "vendor_name is unique (it is the fuzzy-match key)")
def key_02(d):
    return _unique(d["vendors"], "vendor_name", "vendor_name")


@check("KEY-03", "tax_id is unique (two vendors cannot file under one id)")
def key_03(d):
    return _unique(d["vendors"], "tax_id", "tax_id")


@check("KEY-04", "cost_centre_id is unique")
def key_04(d):
    return _unique(d["cost_centres"], "cost_centre_id", "cost_centre_id")


@check("KEY-05", "wbse_id is unique")
def key_05(d):
    return _unique(d["wbse"], "wbse_id", "wbse_id")


@check("KEY-06", "po_number is unique in purchase order headers")
def key_06(d):
    return _unique(d["po_headers"], "po_number", "po_number")


@check("KEY-07", "purchase order lines are unique on (po_number, line_number)")
def key_07(d):
    return _unique(d["po_lines"], "PO line", "po_number", "line_number")


@check("KEY-08", "invoice_no is unique in invoice headers")
def key_08(d):
    return _unique(d["inv_headers"], "invoice_no", "invoice_no")


@check("KEY-09", "invoice lines are unique on (invoice_no, line_number)")
def key_09(d):
    return _unique(d["inv_lines"], "invoice line", "invoice_no", "line_number")


@check("KEY-10", "expected_resolutions holds one row per invoice")
def key_10(d):
    return _unique(d["expected"], "invoice_no", "invoice_no")


# --------------------------------------------------------------------------
# REF -- foreign keys
# --------------------------------------------------------------------------

@check("REF-01", "every invoice line belongs to an invoice header")
def ref_01(d):
    known = {r["invoice_no"] for r in d["inv_headers"]}
    return ["invoice line {}/{}: invoice_no not in invoices_headers".format(
        r["invoice_no"], r["line_number"])
        for r in d["inv_lines"] if r["invoice_no"] not in known]


@check("REF-02", "every invoice names a vendor in the vendor master")
def ref_02(d):
    known = {r["vendor_id"] for r in d["vendors"]}
    return ["invoice {}: vendor_id {} not in vendor master".format(
        r["invoice_no"], r["vendor_id"])
        for r in d["inv_headers"] if r["vendor_id"] not in known]


@check("REF-03", "every purchase order names a vendor in the vendor master")
def ref_03(d):
    known = {r["vendor_id"] for r in d["vendors"]}
    return ["PO {}: vendor_id {} not in vendor master".format(
        r["po_number"], r["vendor_id"])
        for r in d["po_headers"] if r["vendor_id"] not in known]


@check("REF-04", "every purchase order line belongs to a purchase order header")
def ref_04(d):
    known = {r["po_number"] for r in d["po_headers"]}
    return ["PO line {}/{}: po_number not in purchase_order_headers".format(
        r["po_number"], r["line_number"])
        for r in d["po_lines"] if r["po_number"] not in known]


@check("REF-05", "every purchase order header has at least one line")
def ref_05(d):
    with_lines = {r["po_number"] for r in d["po_lines"]}
    return ["PO {}: header with no lines".format(r["po_number"])
            for r in d["po_headers"] if r["po_number"] not in with_lines]


@check("REF-06", "purchase order line cost centres exist in the cost centre master")
def ref_06(d):
    known = {r["cost_centre_id"] for r in d["cost_centres"]}
    return ["PO line {}/{}: cost_centre {} not in cost centre master".format(
        r["po_number"], r["line_number"], r["cost_centre"])
        for r in d["po_lines"] if r["cost_centre"] and r["cost_centre"] not in known]


@check("REF-07", "purchase order line WBS elements exist in the WBSE master")
def ref_07(d):
    known = {r["wbse_id"] for r in d["wbse"]}
    return ["PO line {}/{}: wbse {} not in WBSE master".format(
        r["po_number"], r["line_number"], r["wbse"])
        for r in d["po_lines"] if r["wbse"] and r["wbse"] not in known]


@check("REF-08", "a posted invoice cites a purchase order that exists")
def ref_08(d):
    known = {r["po_number"] for r in d["po_headers"]}
    return ["invoice {}: cites PO {} which does not exist".format(
        r["invoice_no"], r["po_number"] or "(empty)")
        for r in d["inv_headers"]
        if r["status"] == "posted" and r["po_number"] not in known]


@check("REF-09", "a parked invoice cites a purchase order that exists",
       severity="warn")
def ref_09(d):
    # "PO does not exist" is a legitimate PO-mismatch reason in the domain but
    # is out of scope for this POC, so flag it rather than fail on it.
    known = {r["po_number"] for r in d["po_headers"]}
    return ["invoice {}: cites PO {} which does not exist".format(
        r["invoice_no"], r["po_number"] or "(empty)")
        for r in d["inv_headers"]
        if r["status"] == "parked" and r["po_number"] not in known]


@check("REF-10", "every invoice line points at a real purchase order line")
def ref_10(d):
    known = {(r["po_number"], r["line_number"]) for r in d["po_lines"]}
    return ["invoice line {}/{}: PO line {}/{} does not exist".format(
        r["invoice_no"], r["line_number"], r["po_number"], r["po_line_number"])
        for r in d["inv_lines"]
        if (r["po_number"], r["po_line_number"]) not in known]


@check("REF-11", "every expected_resolutions row names a real invoice")
def ref_11(d):
    known = {r["invoice_no"] for r in d["inv_headers"]}
    return ["expected_resolutions {}: invoice does not exist".format(r["invoice_no"])
            for r in d["expected"] if r["invoice_no"] not in known]


@check("REF-12", "every expected resolution points at a real purchase order line")
def ref_12(d):
    known = {(r["po_number"], r["line_number"]) for r in d["po_lines"]}
    return ["expected_resolutions {}: PO line {}/{} does not exist".format(
        r["invoice_no"], r["expected_po_number"], r["expected_po_line"])
        for r in d["expected"]
        if r["expected_po_number"]
        and (r["expected_po_number"], r["expected_po_line"]) not in known]


@check("REF-13", "every contact_email belongs to a requester or cost centre owner")
def ref_13(d):
    known = ({r["requester"] for r in d["po_headers"]}
             | {r["email"] for r in d["cost_centres"]})
    return ["expected_resolutions {}: contact {} is nobody in the data".format(
        r["invoice_no"], r["contact_email"] or "(empty)")
        for r in d["expected"] if r["contact_email"] not in known]


@check("REF-14", "no master record is left unreferenced", severity="warn")
def ref_14(d):
    used_vendors = ({r["vendor_id"] for r in d["po_headers"]}
                    | {r["vendor_id"] for r in d["inv_headers"]})
    used_cc = {r["cost_centre"] for r in d["po_lines"] if r["cost_centre"]}
    used_wbse = {r["wbse"] for r in d["po_lines"] if r["wbse"]}
    out = ["vendor {} ({}) is referenced by nothing".format(
        r["vendor_id"], r["vendor_name"])
        for r in d["vendors"] if r["vendor_id"] not in used_vendors]
    out += ["cost centre {} ({}) is referenced by nothing".format(
        r["cost_centre_id"], r["cost_centre_name"])
        for r in d["cost_centres"] if r["cost_centre_id"] not in used_cc]
    out += ["WBS element {} ({}) is referenced by nothing".format(
        r["wbse_id"], r["wbse_name"])
        for r in d["wbse"] if r["wbse_id"] not in used_wbse]
    return out


# --------------------------------------------------------------------------
# FMT -- field formats
# --------------------------------------------------------------------------

@check("FMT-01", "ids keep their ERP width and padding")
def fmt_01(d):
    out = []
    for r in d["vendors"]:
        if not re.fullmatch(r"\d{10}", r["vendor_id"]):
            out.append("vendor_id {} is not 10 digits".format(r["vendor_id"]))
    for r in d["cost_centres"]:
        if not re.fullmatch(r"\d{10}", r["cost_centre_id"]):
            out.append("cost_centre_id {} is not 10 digits".format(r["cost_centre_id"]))
    for r in d["po_headers"]:
        if not re.fullmatch(r"\d{10}", r["po_number"]):
            out.append("po_number {} is not 10 digits".format(r["po_number"]))
    for r in d["inv_headers"]:
        if not re.fullmatch(r"\d{10}", r["invoice_no"]):
            out.append("invoice_no {} is not 10 digits".format(r["invoice_no"]))
    for r in d["po_lines"] + d["inv_lines"]:
        if not re.fullmatch(r"\d+", r["line_number"]):
            out.append("line_number {} is not numeric".format(r["line_number"]))
    return out


@check("FMT-02", "amounts are positive and carry exactly two decimals")
def fmt_02(d):
    out = []
    for label, rows, key in (("PO line", d["po_lines"], "po_number"),
                             ("invoice", d["inv_headers"], "invoice_no"),
                             ("invoice line", d["inv_lines"], "invoice_no")):
        for r in rows:
            if not re.fullmatch(r"-?\d+\.\d{2}", r["amount"] or ""):
                out.append("{} {}: amount {!r} is not n.nn".format(
                    label, r[key], r["amount"]))
            elif _money(r["amount"]) <= 0:
                out.append("{} {}: amount {} is not positive".format(
                    label, r[key], r["amount"]))
    return out


@check("FMT-03", "dates are ISO yyyy-mm-dd")
def fmt_03(d):
    out = ["PO {}: po_date {!r} is not ISO".format(r["po_number"], r["po_date"])
           for r in d["po_headers"] if _day(r["po_date"]) is None]
    out += ["invoice {}: invoice_date {!r} is not ISO".format(
        r["invoice_no"], r["invoice_date"])
        for r in d["inv_headers"] if _day(r["invoice_date"]) is None]
    return out


@check("FMT-04", "currency codes are known three-letter codes")
def fmt_04(d):
    out = ["PO {}: currency {!r}".format(r["po_number"], r["currency"])
           for r in d["po_headers"] if r["currency"] not in CURRENCIES]
    out += ["invoice {}: currency {!r}".format(r["invoice_no"], r["currency"])
            for r in d["inv_headers"] if r["currency"] not in CURRENCIES]
    return out


@check("FMT-05", "invoice status is posted or parked")
def fmt_05(d):
    return ["invoice {}: status {!r}".format(r["invoice_no"], r["status"])
            for r in d["inv_headers"] if r["status"] not in STATUSES]


@check("FMT-06", "every email address is a reserved example.com address")
def fmt_06(d):
    # RFC 2606 reserves example.com, so no synthetic row can ever reach a real
    # mailbox if the agent is pointed at this data.
    out = ["cost centre {}: {}".format(r["cost_centre_id"], r["email"])
           for r in d["cost_centres"] if not r["email"].endswith("@example.com")]
    out += ["PO {}: requester {}".format(r["po_number"], r["requester"])
            for r in d["po_headers"] if not r["requester"].endswith("@example.com")]
    out += ["expected_resolutions {}: contact {}".format(
        r["invoice_no"], r["contact_email"])
        for r in d["expected"] if not r["contact_email"].endswith("@example.com")]
    return out


@check("FMT-07", "invoice line_text fits SAP's 50-character note field")
def fmt_07(d):
    return ["invoice line {}/{}: line_text is {} chars".format(
        r["invoice_no"], r["line_number"], len(r["line_text"]))
        for r in d["inv_lines"] if len(r["line_text"]) > SGTXT_MAX]


@check("FMT-08", "lines_description is JSON in one of the two OCR shapes")
def fmt_08(d):
    out = []
    for r in d["inv_headers"]:
        try:
            payload = json.loads(r["lines_description"])
        except (ValueError, TypeError):
            out.append("invoice {}: lines_description is not JSON".format(r["invoice_no"]))
            continue
        if not isinstance(payload, list) or not payload:
            out.append("invoice {}: lines_description is not a non-empty list".format(
                r["invoice_no"]))
            continue
        for item in payload:
            if isinstance(item, str):
                continue
            if isinstance(item, dict) and {"sno", "text", "qty", "amount"} <= set(item):
                continue
            out.append("invoice {}: OCR row is neither a string nor "
                       "{{sno, text, qty, amount}}".format(r["invoice_no"]))
            break
    return out


@check("FMT-09", "invoice_description is present")
def fmt_09(d):
    return ["invoice {}: empty invoice_description".format(r["invoice_no"])
            for r in d["inv_headers"] if not (r["invoice_description"] or "").strip()]


@check("FMT-10", "vendor country is a two-letter uppercase code")
def fmt_10(d):
    return ["vendor {}: country {!r}".format(r["vendor_id"], r["country"])
            for r in d["vendors"] if not re.fullmatch(r"[A-Z]{2}", r["country"] or "")]


# --------------------------------------------------------------------------
# DOM -- P2P business rules
# --------------------------------------------------------------------------

@check("DOM-01", "a posted invoice is against its own vendor's purchase order")
def dom_01(d):
    po_vendor = {r["po_number"]: r["vendor_id"] for r in d["po_headers"]}
    return ["invoice {}: posted against PO {} which belongs to {}".format(
        r["invoice_no"], r["po_number"], po_vendor[r["po_number"]])
        for r in d["inv_headers"]
        if r["status"] == "posted" and r["po_number"] in po_vendor
        and po_vendor[r["po_number"]] != r["vendor_id"]]


@check("DOM-02", "a parked invoice cites another vendor's purchase order")
def dom_02(d):
    # the hold reason in scope: if the PO were the vendor's own, there would
    # be no PO mismatch to resolve
    po_vendor = {r["po_number"]: r["vendor_id"] for r in d["po_headers"]}
    return ["invoice {}: parked but PO {} is already its own vendor's".format(
        r["invoice_no"], r["po_number"])
        for r in d["inv_headers"]
        if r["status"] == "parked" and r["po_number"] in po_vendor
        and po_vendor[r["po_number"]] == r["vendor_id"]]


@check("DOM-03", "a parked invoice has no invoice lines")
def dom_03(d):
    # SAP cannot propose lines from a PO that belongs to another vendor;
    # resolving the hold is what creates them
    parked = {r["invoice_no"] for r in d["inv_headers"] if r["status"] == "parked"}
    return ["invoice line {}/{}: parked invoices carry no lines".format(
        r["invoice_no"], r["line_number"])
        for r in d["inv_lines"] if r["invoice_no"] in parked]


@check("DOM-04", "a posted invoice has at least one invoice line")
def dom_04(d):
    with_lines = {r["invoice_no"] for r in d["inv_lines"]}
    return ["invoice {}: posted with no lines".format(r["invoice_no"])
            for r in d["inv_headers"]
            if r["status"] == "posted" and r["invoice_no"] not in with_lines]


@check("DOM-05", "invoice line amounts sum to the invoice header amount")
def dom_05(d):
    totals = defaultdict(Decimal)
    for r in d["inv_lines"]:
        amount = _money(r["amount"])
        if amount is not None:
            totals[r["invoice_no"]] += amount
    out = []
    for r in d["inv_headers"]:
        if r["invoice_no"] not in totals:
            continue
        header = _money(r["amount"])
        if header is not None and header != totals[r["invoice_no"]]:
            out.append("invoice {}: header {} but lines total {}".format(
                r["invoice_no"], header, totals[r["invoice_no"]]))
    return out


@check("DOM-06", "invoice lines sit on the purchase order the header cites")
def dom_06(d):
    cited = {r["invoice_no"]: r["po_number"] for r in d["inv_headers"]}
    return ["invoice line {}/{}: on PO {} but the header cites {}".format(
        r["invoice_no"], r["line_number"], r["po_number"], cited[r["invoice_no"]])
        for r in d["inv_lines"]
        if r["invoice_no"] in cited and r["po_number"] != cited[r["invoice_no"]]]


@check("DOM-07", "no purchase order line is consumed beyond its value")
def dom_07(d):
    consumed = defaultdict(Decimal)
    for r in d["inv_lines"]:
        amount = _money(r["amount"])
        if amount is not None:
            consumed[(r["po_number"], r["po_line_number"])] += amount
    out = []
    for r in d["po_lines"]:
        key = (r["po_number"], r["line_number"])
        value = _money(r["amount"])
        if value is not None and consumed[key] > value:
            out.append("PO line {}/{}: worth {} but invoices consume {}".format(
                r["po_number"], r["line_number"], value, consumed[key]))
    return out


@check("DOM-08", "a posted invoice bills in its purchase order's currency")
def dom_08(d):
    po_currency = {r["po_number"]: r["currency"] for r in d["po_headers"]}
    return ["invoice {}: billed in {} against a {} PO".format(
        r["invoice_no"], r["currency"], po_currency[r["po_number"]])
        for r in d["inv_headers"]
        if r["status"] == "posted" and r["po_number"] in po_currency
        and po_currency[r["po_number"]] != r["currency"]]


@check("DOM-09", "a vendor always trades in one currency")
def dom_09(d):
    seen = defaultdict(set)
    for r in d["po_headers"]:
        seen[r["vendor_id"]].add(r["currency"])
    for r in d["inv_headers"]:
        seen[r["vendor_id"]].add(r["currency"])
    return ["vendor {}: trades in {}".format(vendor_id, ", ".join(sorted(currencies)))
            for vendor_id, currencies in seen.items() if len(currencies) > 1]


@check("DOM-10", "every purchase order line carries a cost centre or a WBS element")
def dom_10(d):
    # both are the indirect evidence paths; a line with neither is unresolvable
    return ["PO line {}/{}: no account assignment".format(
        r["po_number"], r["line_number"])
        for r in d["po_lines"] if not r["cost_centre"] and not r["wbse"]]


@check("DOM-11", "an invoice is dated on or after the purchase order it cites")
def dom_11(d):
    po_date = {r["po_number"]: _day(r["po_date"]) for r in d["po_headers"]}
    out = []
    for r in d["inv_headers"]:
        if r["status"] != "posted":
            continue
        raised, billed = po_date.get(r["po_number"]), _day(r["invoice_date"])
        if raised and billed and billed < raised:
            out.append("invoice {}: dated {} against a PO raised {}".format(
                r["invoice_no"], billed, raised))
    return out


# --------------------------------------------------------------------------
# EVAL -- expected_resolutions is a usable answer key
# --------------------------------------------------------------------------

@check("EVAL-01", "expected_resolutions covers the parked invoices, and only those")
def eval_01(d):
    parked = {r["invoice_no"] for r in d["inv_headers"] if r["status"] == "parked"}
    answered = {r["invoice_no"] for r in d["expected"]}
    out = ["invoice {}: parked with no expected resolution".format(n)
           for n in sorted(parked - answered)]
    out += ["expected_resolutions {}: invoice is not parked".format(n)
            for n in sorted(answered - parked)]
    return out


@check("EVAL-02", "outcome and scenario are known values")
def eval_02(d):
    out = ["expected_resolutions {}: outcome {!r}".format(r["invoice_no"], r["outcome"])
           for r in d["expected"] if r["outcome"] not in OUTCOMES]
    out += ["expected_resolutions {}: scenario {!r}".format(
        r["invoice_no"], r["scenario"])
        for r in d["expected"] if r["scenario"] not in SCENARIOS]
    return out


@check("EVAL-03", "the expected PO is filled in when the outcome needs one")
def eval_03(d):
    out = []
    for r in d["expected"]:
        has_po = bool(r["expected_po_number"] and r["expected_po_line"])
        if r["outcome"] == "escalate" and (r["expected_po_number"] or r["expected_po_line"]):
            out.append("expected_resolutions {}: escalate but names a PO".format(
                r["invoice_no"]))
        elif r["outcome"] in ("reassign_po", "accept_cited_po") and not has_po:
            out.append("expected_resolutions {}: {} without a PO and line".format(
                r["invoice_no"], r["outcome"]))
    return out


@check("EVAL-04", "outcome agrees with the scenario it came from")
def eval_04(d):
    expected_outcome = {
        "po_line": "reassign_po", "cost_centre": "reassign_po",
        "wbse": "reassign_po", "vendor_group": "accept_cited_po",
        "unresolved": "escalate",
    }
    return ["expected_resolutions {}: scenario {} with outcome {}".format(
        r["invoice_no"], r["scenario"], r["outcome"])
        for r in d["expected"]
        if r["scenario"] in expected_outcome
        and r["outcome"] != expected_outcome[r["scenario"]]]


@check("EVAL-05", "accept_cited_po keeps the cited PO, reassign_po moves off it")
def eval_05(d):
    cited = {r["invoice_no"]: r["po_number"] for r in d["inv_headers"]}
    out = []
    for r in d["expected"]:
        on_invoice = cited.get(r["invoice_no"])
        if r["outcome"] == "accept_cited_po" and r["expected_po_number"] != on_invoice:
            out.append("expected_resolutions {}: accepts PO {} but the invoice "
                       "cites {}".format(r["invoice_no"], r["expected_po_number"],
                                         on_invoice))
        if r["outcome"] == "reassign_po" and r["expected_po_number"] == on_invoice:
            out.append("expected_resolutions {}: reassigns to the PO it already "
                       "cites ({})".format(r["invoice_no"], on_invoice))
    return out


@check("EVAL-06", "a reassignment lands on the invoice vendor's own purchase order")
def eval_06(d):
    inv_vendor = {r["invoice_no"]: r["vendor_id"] for r in d["inv_headers"]}
    po_vendor = {r["po_number"]: r["vendor_id"] for r in d["po_headers"]}
    return ["expected_resolutions {}: reassigns to PO {}, held by {} not {}".format(
        r["invoice_no"], r["expected_po_number"],
        po_vendor[r["expected_po_number"]], inv_vendor[r["invoice_no"]])
        for r in d["expected"]
        if r["outcome"] == "reassign_po"
        and r["expected_po_number"] in po_vendor
        and r["invoice_no"] in inv_vendor
        and po_vendor[r["expected_po_number"]] != inv_vendor[r["invoice_no"]]]


@check("EVAL-07", "the expected purchase order line has room for the invoice")
def eval_07(d):
    value = {(r["po_number"], r["line_number"]): _money(r["amount"])
             for r in d["po_lines"]}
    consumed = defaultdict(Decimal)
    for r in d["inv_lines"]:
        amount = _money(r["amount"])
        if amount is not None:
            consumed[(r["po_number"], r["po_line_number"])] += amount
    amounts = {r["invoice_no"]: _money(r["amount"]) for r in d["inv_headers"]}
    out = []
    for r in d["expected"]:
        key = (r["expected_po_number"], r["expected_po_line"])
        if key not in value or value[key] is None:
            continue
        billed = amounts.get(r["invoice_no"])
        open_value = value[key] - consumed[key]
        if billed is not None and billed > open_value:
            out.append("expected_resolutions {}: bills {} onto PO line {}/{} "
                       "with only {} open".format(
                           r["invoice_no"], billed, key[0], key[1], open_value))
    return out


@check("EVAL-08", "the scenario mix is still 10/5/5/5/5", severity="warn")
def eval_08(d):
    wanted = {"po_line": 10, "cost_centre": 5, "wbse": 5,
              "vendor_group": 5, "unresolved": 5}
    actual = Counter(r["scenario"] for r in d["expected"])
    return ["scenario {}: {} rows, documented mix says {}".format(
        scenario, actual.get(scenario, 0), count)
        for scenario, count in wanted.items() if actual.get(scenario, 0) != count]


# --------------------------------------------------------------------------
# VEND -- vendor identity: tax id and address
# --------------------------------------------------------------------------

@check("VEND-01", "every vendor carries a tax id and a full address")
def vend_01(d):
    return ["vendor {}: {} is empty".format(r["vendor_id"], column)
            for r in d["vendors"]
            for column in ("tax_id", "street", "city", "postal_code", "country")
            if not (r[column] or "").strip()]


@check("VEND-02", "every invoice carries a billing address")
def vend_02(d):
    # the tax id may legitimately be missing -- OCR drops it -- but the
    # address block is always at least partly legible
    return ["invoice {}: empty billing_address".format(r["invoice_no"])
            for r in d["inv_headers"] if not (r["billing_address"] or "").strip()]


@check("VEND-03", "a scanned tax id matches the invoice vendor's master record")
def vend_03(d):
    master = {r["vendor_id"]: r for r in d["vendors"]}
    return ["invoice {}: billing_tax_id {} is not {}'s {}".format(
        r["invoice_no"], r["billing_tax_id"], r["vendor_id"],
        master[r["vendor_id"]]["tax_id"])
        for r in d["inv_headers"]
        if r["billing_tax_id"] and r["vendor_id"] in master
        and _norm_tax_id(r["billing_tax_id"]) != _norm_tax_id(master[r["vendor_id"]]["tax_id"])]


@check("VEND-04", "a scanned billing address is the vendor's own, abbreviations aside")
def vend_04(d):
    master = {r["vendor_id"]: r for r in d["vendors"]}
    out = []
    for r in d["inv_headers"]:
        vendor = master.get(r["vendor_id"])
        parts = _address_parts(r["billing_address"])
        if not vendor or len(parts) < 2:
            if vendor:
                out.append("invoice {}: billing_address {!r} has no street and "
                           "city".format(r["invoice_no"], r["billing_address"]))
            continue
        if _canonical_street(parts[0]) != _canonical_street(vendor["street"]):
            out.append("invoice {}: street {!r} is not {!r}".format(
                r["invoice_no"], parts[0], vendor["street"]))
        if parts[1] != vendor["city"]:
            out.append("invoice {}: city {!r} is not {!r}".format(
                r["invoice_no"], parts[1], vendor["city"]))
        # OCR may drop the postcode or the country line, but never invent one
        for extra in parts[2:]:
            if extra not in (vendor["postal_code"], vendor["country"]):
                out.append("invoice {}: address segment {!r} is in neither the "
                           "postcode nor the country of {}".format(
                               r["invoice_no"], extra, r["vendor_id"]))
    return out


@check("VEND-05", "a group invoice carries the legible tax id its scenario needs")
def vend_05(d):
    # without it the vendor_group case cannot be decided and the expected
    # answer would be unfair
    group = {r["invoice_no"] for r in d["expected"] if r["scenario"] == "vendor_group"}
    return ["invoice {}: vendor_group case with no billing_tax_id".format(
        r["invoice_no"])
        for r in d["inv_headers"]
        if r["invoice_no"] in group and not r["billing_tax_id"]]


@check("VEND-06", "a group invoice's vendor shares the cited PO vendor's address")
def vend_06(d):
    master = {r["vendor_id"]: r for r in d["vendors"]}
    po_vendor = {r["po_number"]: r["vendor_id"] for r in d["po_headers"]}
    inv = {r["invoice_no"]: r for r in d["inv_headers"]}
    out = []
    for r in d["expected"]:
        if r["scenario"] != "vendor_group":
            continue
        header = inv.get(r["invoice_no"])
        if not header or header["po_number"] not in po_vendor:
            continue
        mine = master.get(header["vendor_id"])
        theirs = master.get(po_vendor[header["po_number"]])
        if not mine or not theirs:
            continue
        address = ("street", "city", "postal_code", "country")
        if [mine[k] for k in address] != [theirs[k] for k in address]:
            out.append("invoice {}: {} and {} are a group pair but are "
                       "registered at different addresses".format(
                           r["invoice_no"], mine["vendor_name"], theirs["vendor_name"]))
        if mine["tax_id"] == theirs["tax_id"]:
            out.append("invoice {}: {} and {} share a tax id, so they are one "
                       "entity, not a group".format(
                           r["invoice_no"], mine["vendor_name"], theirs["vendor_name"]))
    return out


@check("VEND-07", "only group cases share an address with the PO vendor")
def vend_07(d):
    # a stranger at the same registered address would make the shared-address
    # signal ambiguous and the non-group scenarios unfair
    master = {r["vendor_id"]: r for r in d["vendors"]}
    po_vendor = {r["po_number"]: r["vendor_id"] for r in d["po_headers"]}
    scenario = {r["invoice_no"]: r["scenario"] for r in d["expected"]}
    address = ("street", "city", "postal_code", "country")
    out = []
    for r in d["inv_headers"]:
        if r["status"] != "parked" or scenario.get(r["invoice_no"]) == "vendor_group":
            continue
        cited_vendor = po_vendor.get(r["po_number"])
        if cited_vendor == r["vendor_id"]:
            continue  # not a mismatch at all; DOM-02 owns that case
        mine = master.get(r["vendor_id"])
        theirs = master.get(cited_vendor)
        if mine and theirs and [mine[k] for k in address] == [theirs[k] for k in address]:
            out.append("invoice {}: {} case, but {} shares a registered address "
                       "with the cited PO's vendor {}".format(
                           r["invoice_no"], scenario.get(r["invoice_no"], "?"),
                           mine["vendor_name"], theirs["vendor_name"]))
    return out


@check("VEND-08", "vendors at one address are a near-name pair, not strangers")
def vend_08(d):
    by_address = defaultdict(list)
    for r in d["vendors"]:
        by_address[(r["street"], r["city"], r["postal_code"], r["country"])].append(r)
    out = []
    for address, group in by_address.items():
        if len(group) < 2:
            continue
        words = [set(re.findall(r"[a-z]+", r["vendor_name"].lower())) for r in group]
        if not set.intersection(*words):
            out.append("{}: {} share an address but no word of their names".format(
                ", ".join(address), " / ".join(r["vendor_name"] for r in group)))
        if len({r["tax_id"] for r in group}) != len(group):
            out.append("{}: {} share an address and a tax id".format(
                ", ".join(address), " / ".join(r["vendor_name"] for r in group)))
    return out


# --------------------------------------------------------------------------
# runner
# --------------------------------------------------------------------------

def list_checks():
    print("{} checks:\n".format(len(CHECKS)))
    group = None
    for check_id, title, severity, _ in CHECKS:
        if check_id.split("-")[0] != group:
            group = check_id.split("-")[0]
            print("  {}".format({
                "KEY": "KEY   primary keys",
                "REF": "REF   foreign keys",
                "FMT": "FMT   field formats",
                "DOM": "DOM   business rules",
                "EVAL": "EVAL  the answer key",
                "VEND": "VEND  vendor identity",
            }.get(group, group)))
        flag = "  (warn)" if severity == "warn" else ""
        print("    {:<8} {}{}".format(check_id, title, flag))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("-l", "--list", action="store_true",
                        help="list the checks and exit")
    parser.add_argument("-a", "--all", action="store_true",
                        help="show every violation, not the first few")
    args = parser.parse_args()

    if args.list:
        list_checks()
        return 0

    tables, problems = load()
    print("FILE-01  files, columns and row shapes")
    if problems:
        for problem in problems:
            print("    - {}".format(problem))
        print("\nFAILED before any other check could run.")
        return 1
    print("    ok: {} files, {} rows".format(
        len(tables), sum(len(rows) for rows in tables.values())))
    print()

    failed = warned = 0
    limit = None if args.all else 5
    for check_id, title, severity, fn in CHECKS:
        violations = fn(tables)
        if not violations:
            print("  ok    {:<8} {}".format(check_id, title))
            continue
        if severity == "warn":
            warned += 1
            label = "warn"
        else:
            failed += 1
            label = "FAIL"
        print("  {}  {:<8} {}  ({} violation{})".format(
            label, check_id, title, len(violations),
            "" if len(violations) == 1 else "s"))
        for violation in violations[:limit]:
            print("          - {}".format(violation))
        if limit and len(violations) > limit:
            print("          ... {} more (-a to show)".format(len(violations) - limit))

    print("\n{} checks: {} passed, {} failed, {} warned".format(
        len(CHECKS), len(CHECKS) - failed - warned, failed, warned))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
