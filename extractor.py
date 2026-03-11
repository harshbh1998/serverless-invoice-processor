"""
extractor.py
============
Parses a raw Textract ExpenseDocument dict into a flat Invoice dict
ready to be written to DynamoDB.
"""

import hashlib
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Any

# ── Field-type → invoice key mapping ─────────────────────────────────────────
# Textract returns normalized field type labels. We map them to DynamoDB keys.
SUMMARY_FIELD_MAP: dict[str, str] = {
    "VENDOR_NAME":            "vendorName",
    "VENDOR_ADDRESS":         "vendorAddress",
    "VENDOR_PHONE":           "vendorPhone",
    "INVOICE_RECEIPT_ID":     "invoiceNumber",
    "INVOICE_RECEIPT_DATE":   "invoiceDate",
    "DUE_DATE":               "dueDate",
    "SUBTOTAL":               "subtotal",
    "TAX":                    "tax",
    "TOTAL":                  "totalAmount",
    "AMOUNT_PAID":            "totalAmount",  # fallback if TOTAL absent
    "PAYMENT_TERMS":          "paymentTerms",
    "PO_NUMBER":              "poNumber",
}

LINE_ITEM_FIELD_MAP: dict[str, str] = {
    "ITEM":         "description",
    "PRODUCT_CODE": "productCode",
    "QUANTITY":     "quantity",
    "UNIT_PRICE":   "unitPrice",
    "PRICE":        "amount",
    "EXPENSE_ROW":  "rawRow",
}


# ── Public API ────────────────────────────────────────────────────────────────
def parse_expense_document(
    expense_doc: dict,
    bucket: str,
    key: str,
    doc_index: int,
) -> dict:
    """
    Convert a single Textract ExpenseDocument into a flat dict
    suitable for DynamoDB PutItem.

    Args:
        expense_doc: One element from response["ExpenseDocuments"]
        bucket:      Source S3 bucket name
        key:         Source S3 object key
        doc_index:   Index of this doc within the response (for multi-page PDFs)

    Returns:
        dict with all invoice fields
    """
    now = datetime.now(timezone.utc)

    invoice: dict[str, Any] = {
        # ── Keys ──────────────────────────────────────────────────────────
        "invoiceId":   _generate_id(bucket, key, doc_index),
        "timestamp":   now.isoformat(),

        # ── Source metadata ───────────────────────────────────────────────
        "s3Bucket": bucket,
        "s3Key":    key,

        # ── TTL (DynamoDB auto-delete after 1 year) ───────────────────────
        "ttl": int((now + timedelta(days=365)).timestamp()),
    }

    # ── Parse summary fields ──────────────────────────────────────────────────
    for field in expense_doc.get("SummaryFields", []):
        field_type  = _get_field_type(field)
        field_value = _get_field_value(field)
        confidence  = _get_confidence(field)

        if not field_type or not field_value:
            continue

        db_key = SUMMARY_FIELD_MAP.get(field_type)
        if db_key:
            # Don't overwrite a higher-confidence value already set
            existing_conf_key = f"_conf_{db_key}"
            existing_conf = invoice.get(existing_conf_key, -1)
            if confidence >= existing_conf:
                invoice[db_key] = field_value
                invoice[existing_conf_key] = confidence  # temp tracking key

    # Remove temporary confidence-tracking keys before saving
    invoice = {k: v for k, v in invoice.items() if not k.startswith("_conf_")}

    # ── Parse line items ──────────────────────────────────────────────────────
    line_items = _parse_line_items(expense_doc.get("LineItemGroups", []))
    if line_items:
        invoice["lineItems"] = line_items
        invoice["lineItemCount"] = len(line_items)

    return invoice


# ── Line item parsing ─────────────────────────────────────────────────────────
def _parse_line_items(line_item_groups: list) -> list[dict]:
    """Extract all line items from all groups."""
    items = []
    for group in line_item_groups:
        for line_item in group.get("LineItems", []):
            parsed = _parse_single_line_item(line_item)
            if parsed:
                items.append(parsed)
    return items


def _parse_single_line_item(line_item: dict) -> dict:
    """Parse one LineItemFields object into a flat dict."""
    item: dict[str, str] = {}

    for field in line_item.get("LineItemExpenseFields", []):
        field_type  = _get_field_type(field)
        field_value = _get_field_value(field)

        if not field_type or not field_value:
            continue

        db_key = LINE_ITEM_FIELD_MAP.get(field_type)
        if db_key:
            item[db_key] = field_value

    return item


# ── Helper functions ──────────────────────────────────────────────────────────
def _get_field_type(field: dict) -> str:
    """Safely extract the field type label from a Textract field."""
    return (field.get("Type") or {}).get("Text", "").strip().upper()


def _get_field_value(field: dict) -> str:
    """Safely extract the detected text value from a Textract field."""
    return ((field.get("ValueDetection") or {}).get("Text") or "").strip()


def _get_confidence(field: dict) -> float:
    """Extract Textract confidence score (0–100). Defaults to 0."""
    try:
        return float((field.get("ValueDetection") or {}).get("Confidence", 0))
    except (TypeError, ValueError):
        return 0.0


def _generate_id(bucket: str, key: str, doc_index: int) -> str:
    """
    Generate a deterministic, unique invoice ID.
    Format: <8-char md5>-<unix-ms>
    """
    raw = f"{bucket}/{key}#{doc_index}"
    short_hash = hashlib.md5(raw.encode()).hexdigest()[:8]
    ts = int(datetime.now(timezone.utc).timestamp() * 1000)
    return f"{short_hash}-{ts}"
