"""
test_extractor.py
=================
Unit tests for extractor.py — covers validation, confidence scoring,
document parsing, and all edge cases.

Run with:  python -m pytest test_extractor.py -v
"""

import pytest
from extractor import (
    is_valid_invoice,
    get_overall_confidence,
    parse_expense_document,
    _get_field_type,
    _get_field_value,
    _get_confidence,
)


# ── Helpers ───────────────────────────────────────────────────────────────────
def make_field(field_type: str, value: str, confidence: float = 98.0) -> dict:
    return {
        "Type":           {"Text": field_type, "Confidence": 99.0},
        "ValueDetection": {"Text": value,      "Confidence": confidence},
    }


def make_line_item(*fields: tuple[str, str]) -> dict:
    return {
        "LineItemExpenseFields": [make_field(ft, fv) for ft, fv in fields]
    }


def make_doc(summary_fields=None, line_item_groups=None) -> dict:
    return {
        "SummaryFields":  summary_fields  or [],
        "LineItemGroups": line_item_groups or [],
    }


# ── is_valid_invoice ──────────────────────────────────────────────────────────
class TestIsValidInvoice:

    def test_valid_invoice_passes(self):
        doc = make_doc([
            make_field("VENDOR_NAME",          "Acme Corp",  95.0),
            make_field("TOTAL",                "$1,000.00",  97.0),
            make_field("INVOICE_RECEIPT_DATE", "2024-01-15", 92.0),
        ])
        valid, reason = is_valid_invoice(doc)
        assert valid is True
        assert reason == ""

    def test_no_summary_fields_fails(self):
        valid, reason = is_valid_invoice(make_doc())
        assert valid is False
        assert "no summary fields" in reason.lower()

    def test_insufficient_required_fields_fails(self):
        # Only one required field — below MIN_REQUIRED_FIELDS (2)
        doc = make_doc([make_field("VENDOR_NAME", "Acme", 95.0)])
        valid, reason = is_valid_invoice(doc)
        assert valid is False
        assert "1" in reason

    def test_low_confidence_fields_not_counted(self):
        # Both fields present but both below MIN_FIELD_CONFIDENCE (70%)
        doc = make_doc([
            make_field("VENDOR_NAME", "Acme Corp",  50.0),
            make_field("TOTAL",       "$1,000.00",  45.0),
        ])
        valid, reason = is_valid_invoice(doc)
        assert valid is False
        assert "low-confidence" in reason.lower()

    def test_non_invoice_fields_only_fails(self):
        # Has fields but none are invoice-specific
        doc = make_doc([
            make_field("UNKNOWN_FIELD", "Some text", 99.0),
            make_field("ANOTHER",       "More text", 99.0),
        ])
        valid, reason = is_valid_invoice(doc)
        assert valid is False

    def test_mix_of_high_and_low_confidence(self):
        # One required field at high conf, one at low conf — should still fail
        doc = make_doc([
            make_field("VENDOR_NAME", "Acme Corp", 95.0),  # passes
            make_field("TOTAL",       "$500.00",   40.0),  # below threshold
        ])
        valid, reason = is_valid_invoice(doc)
        assert valid is False


# ── get_overall_confidence ────────────────────────────────────────────────────
class TestGetOverallConfidence:

    def test_all_high_confidence(self):
        doc = make_doc([
            make_field("VENDOR_NAME", "Acme",    98.0),
            make_field("TOTAL",       "$500.00", 96.0),
            make_field("TAX",         "$50.00",  94.0),
        ])
        score = get_overall_confidence(doc)
        assert score > 90.0

    def test_empty_doc_returns_zero(self):
        assert get_overall_confidence(make_doc()) == 0.0

    def test_empty_value_fields_excluded(self):
        # Field with empty value should not count toward score
        doc = make_doc([
            make_field("VENDOR_NAME", "Acme", 98.0),
            make_field("TOTAL",       "",     10.0),   # empty value → excluded
        ])
        score_with_empty = get_overall_confidence(doc)

        doc2 = make_doc([make_field("VENDOR_NAME", "Acme", 98.0)])
        score_without    = get_overall_confidence(doc2)

        assert score_with_empty == score_without

    def test_high_weight_fields_increase_score(self):
        """VENDOR_NAME and TOTAL are 2x weighted — high confidence in these
        should produce a higher overall score than the same confidence
        only in low-weight fields."""
        high_weight_doc = make_doc([
            make_field("VENDOR_NAME", "Acme",   100.0),   # 2x weight
            make_field("TOTAL",       "$100",   100.0),   # 2x weight
            make_field("SUBTOTAL",    "$90",     50.0),   # 1x weight
        ])
        low_weight_doc = make_doc([
            make_field("SUBTOTAL",      "$90",  100.0),   # 1x weight
            make_field("PAYMENT_TERMS", "Net30", 100.0),  # 1x weight
            make_field("PO_NUMBER",     "PO123",  50.0),  # 1x weight
        ])
        assert get_overall_confidence(high_weight_doc) > get_overall_confidence(low_weight_doc)

    def test_score_is_between_0_and_100(self):
        doc = make_doc([
            make_field("VENDOR_NAME", "Corp", 75.0),
            make_field("TOTAL",       "$200", 65.0),
        ])
        score = get_overall_confidence(doc)
        assert 0.0 <= score <= 100.0


# ── parse_expense_document ────────────────────────────────────────────────────
class TestParseExpenseDocument:

    def test_all_summary_fields_parsed(self):
        doc = make_doc([
            make_field("VENDOR_NAME",          "Acme Corp"),
            make_field("VENDOR_ADDRESS",        "123 Main St"),
            make_field("VENDOR_PHONE",          "555-1234"),
            make_field("TOTAL",                "$1,250.00"),
            make_field("SUBTOTAL",             "$1,150.00"),
            make_field("TAX",                  "$100.00"),
            make_field("INVOICE_RECEIPT_ID",   "INV-001"),
            make_field("INVOICE_RECEIPT_DATE", "2024-03-01"),
            make_field("DUE_DATE",             "2024-04-01"),
        ])
        inv = parse_expense_document(doc, "my-bucket", "submitted-invoices/inv.jpg", 0)

        assert inv["vendorName"]    == "Acme Corp"
        assert inv["vendorAddress"] == "123 Main St"
        assert inv["vendorPhone"]   == "555-1234"
        assert inv["totalAmount"]   == "$1,250.00"
        assert inv["subtotal"]      == "$1,150.00"
        assert inv["tax"]           == "$100.00"
        assert inv["invoiceNumber"] == "INV-001"
        assert inv["invoiceDate"]   == "2024-03-01"
        assert inv["dueDate"]       == "2024-04-01"
        assert inv["s3Bucket"]      == "my-bucket"
        assert inv["s3Key"]         == "submitted-invoices/inv.jpg"

    def test_invoice_id_always_set(self):
        inv = parse_expense_document(make_doc(), "b", "k.jpg", 0)
        assert inv["invoiceId"] != ""
        assert "-" in inv["invoiceId"]

    def test_timestamp_is_iso8601(self):
        inv = parse_expense_document(make_doc(), "b", "k.jpg", 0)
        assert "T" in inv["timestamp"]
        assert "+" in inv["timestamp"] or "Z" in inv["timestamp"]

    def test_ttl_is_future(self):
        import time
        inv = parse_expense_document(make_doc(), "b", "k.jpg", 0)
        assert inv["ttl"] > int(time.time())

    def test_confidence_score_stored(self):
        doc = make_doc([make_field("VENDOR_NAME", "Acme", 90.0)])
        inv = parse_expense_document(doc, "b", "k.jpg", 0)
        assert "confidenceScore" in inv
        assert float(inv["confidenceScore"]) > 0

    def test_line_items_parsed(self):
        doc = make_doc(
            summary_fields=[make_field("VENDOR_NAME", "Corp", 95.0)],
            line_item_groups=[{
                "LineItems": [
                    make_line_item(
                        ("ITEM",       "Widget A"),
                        ("QUANTITY",   "5"),
                        ("UNIT_PRICE", "$50.00"),
                        ("PRICE",      "$250.00"),
                    )
                ]
            }]
        )
        inv = parse_expense_document(doc, "b", "k.jpg", 0)
        assert inv["lineItemCount"] == 1
        li = inv["lineItems"][0]
        assert li["description"] == "Widget A"
        assert li["quantity"]    == "5"
        assert li["unitPrice"]   == "$50.00"
        assert li["amount"]      == "$250.00"

    def test_amount_paid_fallback_for_total(self):
        """AMOUNT_PAID should populate totalAmount when TOTAL is absent."""
        doc = make_doc([make_field("AMOUNT_PAID", "$500.00", 95.0)])
        inv = parse_expense_document(doc, "b", "k.jpg", 0)
        assert inv["totalAmount"] == "$500.00"

    def test_higher_confidence_wins_for_duplicate_keys(self):
        """If both TOTAL and AMOUNT_PAID are present, higher-confidence wins."""
        doc = make_doc([
            make_field("TOTAL",       "$1,000.00", 99.0),
            make_field("AMOUNT_PAID", "$900.00",   55.0),
        ])
        inv = parse_expense_document(doc, "b", "k.jpg", 0)
        assert inv["totalAmount"] == "$1,000.00"

    def test_empty_doc_has_no_line_items(self):
        inv = parse_expense_document(make_doc(), "b", "k.jpg", 0)
        assert "lineItems"     not in inv
        assert "lineItemCount" not in inv

    def test_different_doc_indexes_produce_different_ids(self):
        doc = make_doc()
        id0 = parse_expense_document(doc, "b", "k.pdf", 0)["invoiceId"]
        id1 = parse_expense_document(doc, "b", "k.pdf", 1)["invoiceId"]
        assert id0 != id1


# ── Low-level helpers ─────────────────────────────────────────────────────────
class TestHelpers:

    def test_get_field_type_uppercase(self):
        assert _get_field_type({"Type": {"Text": "vendor_name"}}) == "VENDOR_NAME"

    def test_get_field_type_none(self):
        assert _get_field_type({}) == ""
        assert _get_field_type({"Type": None}) == ""

    def test_get_field_value_strips(self):
        assert _get_field_value({"ValueDetection": {"Text": "  Acme  "}}) == "Acme"

    def test_get_field_value_none(self):
        assert _get_field_value({}) == ""
        assert _get_field_value({"ValueDetection": None}) == ""

    def test_get_confidence_float(self):
        assert _get_confidence({"ValueDetection": {"Confidence": 97.5}}) == 97.5

    def test_get_confidence_default_zero(self):
        assert _get_confidence({}) == 0.0
        assert _get_confidence({"ValueDetection": {}}) == 0.0
