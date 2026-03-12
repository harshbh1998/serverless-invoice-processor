"""
extractor.py
============
Three responsibilities:
  1. is_valid_invoice()       — semantic gate: is this doc actually an invoice?
  2. get_overall_confidence() — weighted confidence score across all fields
  3. parse_expense_document() — map Textract response → flat DynamoDB dict
"""

import hashlib
from datetime import datetime, timezone, timedelta
from typing import Any

# ── Field type mappings ───────────────────────────────────────────────────────

# Summary fields Textract can return → DynamoDB attribute names
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
    "AMOUNT_PAID":            "totalAmount",   # fallback if TOTAL absent
    "PAYMENT_TERMS":          "paymentTerms",
    "PO_NUMBER":              "poNumber",
    "RECEIVER_NAME":          "receiverName",
    "RECEIVER_ADDRESS":       "receiverAddress",
}

# Line item fields → DynamoDB attribute names
LINE_ITEM_FIELD_MAP: dict[str, str] = {
    "ITEM":         "description",
    "PRODUCT_CODE": "productCode",
    "QUANTITY":     "quantity",
    "UNIT_PRICE":   "unitPrice",
    "PRICE":        "amount",
    "EXPENSE_ROW":  "rawRow",
}

# Fields that MUST be present for a document to qualify as an invoice.
# At least MIN_REQUIRED_FIELDS of these must be detected above MIN_CONFIDENCE.
INVOICE_REQUIRED_FIELDS: set[str] = {
    "VENDOR_NAME",
    "TOTAL",
    "SUBTOTAL",
    "INVOICE_RECEIPT_ID",
    "INVOICE_RECEIPT_DATE",
    "AMOUNT_PAID",
}

MIN_REQUIRED_FIELDS = 2       # minimum number of required fields that must match
MIN_FIELD_CONFIDENCE = 70.0   # minimum Textract confidence per field (0–100)

# Fields weighted 2x when computing overall confidence score
HIGH_WEIGHT_FIELDS: set[str] = {
    "VENDOR_NAME",
    "TOTAL",
    "AMOUNT_PAID",
    "INVOICE_RECEIPT_ID",
}


# ── 1. Invoice Validation ─────────────────────────────────────────────────────
def is_valid_invoice(expense_doc: dict) -> tuple[bool, str]:
    """
    Validates whether a Textract ExpenseDocument is actually an invoice
    or receipt rather than a random document.

    Checks:
      - At least MIN_REQUIRED_FIELDS invoice-specific fields are detected
      - Each matched field has confidence >= MIN_FIELD_CONFIDENCE
      - At least one SummaryField exists at all

    Returns:
        (True,  "")            — valid invoice, proceed
        (False, reason_str)    — not an invoice, reason explains why
    """
    summary_fields = expense_doc.get("SummaryFields", [])

    if not summary_fields:
        return False, (
            "Textract returned no summary fields — "
            "document does not appear to be an invoice or receipt"
        )

    matched   = []
    low_conf  = []

    for field in summary_fields:
        field_type  = _get_field_type(field)
        field_value = _get_field_value(field)
        confidence  = _get_confidence(field)

        if field_type not in INVOICE_REQUIRED_FIELDS or not field_value:
            continue

        if confidence >= MIN_FIELD_CONFIDENCE:
            matched.append(field_type)
        else:
            low_conf.append(f"{field_type}({confidence:.0f}%)")

    if len(matched) >= MIN_REQUIRED_FIELDS:
        return True, ""

    reason = (
        f"Only {len(matched)} of {MIN_REQUIRED_FIELDS} required invoice fields "
        f"detected with confidence >= {MIN_FIELD_CONFIDENCE}%. "
        f"Matched: {matched or 'none'}. "
        f"Low-confidence: {low_conf or 'none'}."
    )
    return False, reason


# ── 2. Confidence Scoring ─────────────────────────────────────────────────────
def get_overall_confidence(expense_doc: dict) -> float:
    """
    Calculates a weighted overall confidence score (0–100) for the document.

    Strategy:
      - Fields in HIGH_WEIGHT_FIELDS are weighted 2x (most critical for invoices)
      - All other detected, non-empty fields are weighted 1x
      - Empty / undetected fields are excluded entirely (not penalised)
      - Returns 0.0 if no fields detected

    This weighted approach avoids a single low-confidence minor field
    dragging down an otherwise high-quality extraction.
    """
    total_weight = 0.0
    weighted_sum = 0.0

    for field in expense_doc.get("SummaryFields", []):
        field_type  = _get_field_type(field)
        field_value = _get_field_value(field)
        confidence  = _get_confidence(field)

        if not field_value:
            continue    # skip empty detections

        weight        = 2.0 if field_type in HIGH_WEIGHT_FIELDS else 1.0
        weighted_sum += confidence * weight
        total_weight += weight

    if total_weight == 0.0:
        return 0.0

    return round(weighted_sum / total_weight, 2)


# ── 3. Document Parsing ───────────────────────────────────────────────────────
def parse_expense_document(
    expense_doc: dict,
    bucket:      str,
    key:         str,
    doc_index:   int,
) -> dict[str, Any]:
    """
    Convert a single Textract ExpenseDocument into a flat dict
    ready for DynamoDB PutItem.

    Args:
        expense_doc: One element from response["ExpenseDocuments"]
        bucket:      Source S3 bucket name
        key:         Source S3 object key
        doc_index:   Index within response (multi-page PDFs produce multiple docs)

    Returns:
        Flat dict with all extracted invoice fields + metadata
    """
    now = datetime.now(timezone.utc)

    invoice: dict[str, Any] = {
        # ── Keys ──────────────────────────────────────────────────────────────
        "invoiceId":   _generate_id(bucket, key, doc_index),
        "timestamp":   now.isoformat(),

        # ── Source metadata ───────────────────────────────────────────────────
        "s3Bucket":    bucket,
        "s3Key":       key,

        # ── Confidence score ──────────────────────────────────────────────────
        "confidenceScore": str(get_overall_confidence(expense_doc)),

        # ── TTL (auto-delete from DynamoDB after 1 year) ──────────────────────
        "ttl": int((now + timedelta(days=365)).timestamp()),
    }

    # ── Parse summary fields ──────────────────────────────────────────────────
    # If two fields map to the same DynamoDB key (e.g. TOTAL and AMOUNT_PAID
    # both map to totalAmount), keep the one with higher confidence.
    confidence_tracker: dict[str, float] = {}

    for field in expense_doc.get("SummaryFields", []):
        field_type  = _get_field_type(field)
        field_value = _get_field_value(field)
        confidence  = _get_confidence(field)

        if not field_type or not field_value:
            continue

        db_key = SUMMARY_FIELD_MAP.get(field_type)
        if not db_key:
            continue

        # Only overwrite if this field has higher confidence
        if confidence >= confidence_tracker.get(db_key, -1.0):
            invoice[db_key] = field_value
            confidence_tracker[db_key] = confidence

    # ── Parse line items ──────────────────────────────────────────────────────
    line_items = _parse_line_items(expense_doc.get("LineItemGroups", []))
    if line_items:
        invoice["lineItems"]     = line_items
        invoice["lineItemCount"] = len(line_items)

    return invoice


# ── Line item parsing ─────────────────────────────────────────────────────────
def _parse_line_items(line_item_groups: list) -> list[dict]:
    items = []
    for group in line_item_groups:
        for line_item in group.get("LineItems", []):
            parsed = _parse_single_line_item(line_item)
            if parsed:
                items.append(parsed)
    return items


def _parse_single_line_item(line_item: dict) -> dict:
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


# ── Low-level field helpers ───────────────────────────────────────────────────
def _get_field_type(field: dict) -> str:
    return (field.get("Type") or {}).get("Text", "").strip().upper()


def _get_field_value(field: dict) -> str:
    return ((field.get("ValueDetection") or {}).get("Text") or "").strip()


def _get_confidence(field: dict) -> float:
    try:
        return float((field.get("ValueDetection") or {}).get("Confidence", 0))
    except (TypeError, ValueError):
        return 0.0


# ── ID generation ─────────────────────────────────────────────────────────────
def _generate_id(bucket: str, key: str, doc_index: int) -> str:
    """
    Deterministic, unique invoice ID.
    Format: <8-char MD5 of s3 path>-<unix-milliseconds>
    """
    raw        = f"{bucket}/{key}#{doc_index}"
    short_hash = hashlib.md5(raw.encode()).hexdigest()[:8]
    ts         = int(datetime.now(timezone.utc).timestamp() * 1000)
    return f"{short_hash}-{ts}"
