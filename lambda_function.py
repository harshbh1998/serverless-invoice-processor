"""
invoice_processor — AWS Lambda (Python 3.14)
=============================================
Triggered by S3 PUT events.
Flow: S3 → Textract AnalyzeExpense → DynamoDB
"""

import os
import logging
import boto3
from extractor import parse_expense_document

# ── Logging setup ─────────────────────────────────────────────────────────────
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ── AWS clients (initialized once at cold start, reused across warm invocations)
_textract = boto3.client("textract")
_dynamodb = boto3.resource("dynamodb")

TABLE_NAME = os.environ.get("DYNAMODB_TABLE", "InvoiceExpenses")
_table = _dynamodb.Table(TABLE_NAME)


# ── Lambda Entry Point ────────────────────────────────────────────────────────
def lambda_handler(event: dict, context) -> dict:
    """
    Main handler — called by AWS Lambda for every S3 PUT event.

    Args:
        event:   AWS S3 event payload (can contain multiple records)
        context: Lambda runtime context (unused but required by signature)

    Returns:
        dict with statusCode and a summary message
    """
    processed = []
    errors = []

    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = record["s3"]["object"]["key"]

        logger.info("📄 Processing invoice: s3://%s/%s", bucket, key)

        try:
            # Step 1 — Call Textract AnalyzeExpense
            response = _analyze_expense(bucket, key)
            expense_docs = response.get("ExpenseDocuments", [])
            logger.info("✅ Textract returned %d expense document(s)", len(expense_docs))

            # Step 2 — Parse each expense document
            for idx, expense_doc in enumerate(expense_docs):
                invoice = parse_expense_document(expense_doc, bucket, key, idx)
                logger.info(
                    "🧾 Parsed: id=%s vendor=%s total=%s",
                    invoice["invoiceId"],
                    invoice.get("vendorName", "N/A"),
                    invoice.get("totalAmount", "N/A"),
                )

                # Step 3 — Save to DynamoDB
                _save_invoice(invoice)
                logger.info("✅ Saved invoice %s to DynamoDB table '%s'", invoice["invoiceId"], TABLE_NAME)
                processed.append(invoice["invoiceId"])

        except Exception as exc:
            msg = f"❌ Failed to process s3://{bucket}/{key}: {exc}"
            logger.error(msg, exc_info=True)
            errors.append(msg)

    if errors:
        # Re-raise so Lambda marks the invocation as failed and S3 can retry
        raise RuntimeError(f"{len(errors)} record(s) failed:\n" + "\n".join(errors))

    return {
        "statusCode": 200,
        "body": f"Successfully processed {len(processed)} invoice(s): {processed}",
    }


# ── Textract ──────────────────────────────────────────────────────────────────
def _analyze_expense(bucket: str, key: str) -> dict:
    """
    Calls Textract AnalyzeExpense on the given S3 object.
    Supports JPEG, PNG, PDF, and TIFF.
    """
    return _textract.analyze_expense(
        Document={
            "S3Object": {
                "Bucket": bucket,
                "Name": key,
            }
        }
    )


# ── DynamoDB ──────────────────────────────────────────────────────────────────
def _save_invoice(invoice: dict) -> None:
    """
    Writes the invoice dict to DynamoDB using put_item.
    Existing items with the same invoiceId + timestamp are overwritten.
    """
    _table.put_item(Item=invoice)
