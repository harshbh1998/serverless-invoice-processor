"""
test_extractor.py
=================
Unit tests for the Textract response parser.
Run with: python -m pytest test_extractor.py -v
"""

import pytest
from extractor import parse_expense_document, _get_field_type, _get_field_value, _get_confidence


# ── Helpers ───────────────────────────────────────────────────────────────────
def make_field(field_type: str, value: str, confidence: float = 99.0) -> dict:
    return {
        "Type": {"Text": field_type, "Confidence": 99.0},
        "ValueDetection": {"Text": value, "Confidence": confidence},
    }


def make_line_item(*fields: tuple[str, str]) -> dict:
    return {
        "LineItemExpenseFields": [
            make_field(ft, fv) for ft, fv in fields
        ]
    }


# ── Tests ─────────────────────────────────────────────────────────────────────
class TestParseExpenseDocument:
    def test_summary_fields_parsed_correctly(self):
        doc = {
            "SummaryFields": [
                make_field("VENDOR_NAME", "Acme Corp"),
                make_field("TOTAL", "$1,250.00"),
                make_field("INVOICE_RECEIPT_DATE", "2024-03-01"),
                make_field("TAX", "$100.00"),
                make_field("SUBTOTAL", "$1,150.00"),
                make_field("INVOICE_RECEIPT_ID", "INV-00142"),
                make_field("DUE_DATE", "2024-04-01"),
            ],
            "LineItemGroups": [],
        }
        inv = parse_expense_document(doc, "my-bucket", "invoices/test.jpg", 0)

        assert inv["vendorName"]    == "Acme Corp"
        assert inv["totalAmount"]   == "$1,250.00"
        assert inv["invoiceDate"]   == "2024-03-01"
        assert inv["tax"]           == "$100.00"
        assert inv["subtotal"]      == "$1,150.00"
        assert inv["invoiceNumber"] == "INV-00142"
        assert inv["dueDate"]       == "2024-04-01"
        assert inv["s3Bucket"]      == "my-bucket"
        assert inv["s3Key"]         == "invoices/test.jpg"

    def test_invoice_id_is_always_set(self):
        doc = {"SummaryFields": [], "LineItemGroups": []}
        inv = parse_expense_document(doc, "b", "k.jpg", 0)
        assert inv["invoiceId"] != ""

    def test_timestamp_is_always_set(self):
        doc = {"SummaryFields": [], "LineItemGroups": []}
        inv = parse_expense_document(doc, "b", "k.jpg", 0)
        assert "T" in inv["timestamp"]  # ISO 8601 contains a T

    def test_ttl_is_future_unix_timestamp(self):
        import time
        doc = {"SummaryFields": [], "LineItemGroups": []}
        inv = parse_expense_document(doc, "b", "k.jpg", 0)
        assert inv["ttl"] > int(time.time())

    def test_line_items_parsed(self):
        doc = {
            "SummaryFields": [],
            "LineItemGroups": [
                {
                    "LineItems": [
                        make_line_item(
                            ("ITEM", "Widget A"),
                            ("QUANTITY", "5"),
                            ("UNIT_PRICE", "$50.00"),
                            ("PRICE", "$250.00"),
                        )
                    ]
                }
            ],
        }
        inv = parse_expense_document(doc, "b", "k.jpg", 0)
        assert inv["lineItemCount"] == 1
        item = inv["lineItems"][0]
        assert item["description"] == "Widget A"
        assert item["quantity"]    == "5"
        assert item["unitPrice"]   == "$50.00"
        assert item["amount"]      == "$250.00"

    def test_empty_doc_has_no_line_items(self):
        doc = {"SummaryFields": [], "LineItemGroups": []}
        inv = parse_expense_document(doc, "b", "k.jpg", 0)
        assert "lineItems" not in inv

    def test_amount_paid_fallback_for_total(self):
        """AMOUNT_PAID should be used as totalAmount if TOTAL is absent."""
        doc = {
            "SummaryFields": [make_field("AMOUNT_PAID", "$500.00")],
            "LineItemGroups": [],
        }
        inv = parse_expense_document(doc, "b", "k.jpg", 0)
        assert inv["totalAmount"] == "$500.00"

    def test_no_temp_confidence_keys_in_output(self):
        doc = {
            "SummaryFields": [make_field("VENDOR_NAME", "Corp X")],
            "LineItemGroups": [],
        }
        inv = parse_expense_document(doc, "b", "k.jpg", 0)
        for key in inv:
            assert not key.startswith("_conf_"), f"Temp key leaked: {key}"


class TestHelpers:
    def test_get_field_type_returns_uppercase(self):
        field = {"Type": {"Text": "vendor_name"}}
        assert _get_field_type(field) == "VENDOR_NAME"

    def test_get_field_type_handles_none(self):
        assert _get_field_type({}) == ""
        assert _get_field_type({"Type": None}) == ""

    def test_get_field_value_strips_whitespace(self):
        field = {"ValueDetection": {"Text": "  Acme  "}}
        assert _get_field_value(field) == "Acme"

    def test_get_field_value_handles_none(self):
        assert _get_field_value({}) == ""
        assert _get_field_value({"ValueDetection": None}) == ""

    def test_get_confidence_returns_float(self):
        field = {"ValueDetection": {"Confidence": 97.5}}
        assert _get_confidence(field) == 97.5

    def test_get_confidence_defaults_to_zero(self):
        assert _get_confidence({}) == 0.0
