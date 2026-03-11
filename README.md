# Invoice Processor — Python Lambda
## Deployment Guide (Local → AWS Lambda)

---

## Prerequisites

| Tool | Version | Install |
|------|---------|---------|
| Python | 3.14+ | https://python.org |
| AWS CLI | v2 | https://aws.amazon.com/cli |
| pip | latest | bundled with Python |

---

## Project Structure

```
invoice-processor-python/
├── lambda_function.py    # Lambda entry point  (handler: lambda_function.lambda_handler)
├── extractor.py          # Textract response parser
├── test_extractor.py     # Unit tests (pytest)
├── requirements.txt      # Dependencies
└── README.md             # This file
```

---

## STEP 1 — Set Up Local Environment

```bash
# 1a. Create and activate a virtual environment
python3.14 -m venv .venv
source .venv/bin/activate          # macOS / Linux
# .venv\Scripts\activate           # Windows

# 1b. Install dependencies
pip install -r requirements.txt

# 1c. Verify everything installed correctly
python --version                   # Should show Python 3.14.x
pip show boto3
```

---

## STEP 2 — Configure AWS CLI

```bash
# 2a. Configure credentials (use your IAM user — NOT root)
aws configure

# You will be prompted for:
# AWS Access Key ID:     <your-iam-user-access-key>
# AWS Secret Access Key: <your-iam-user-secret-key>
# Default region name:   us-east-1   (or your preferred region)
# Default output format: json

# 2b. Verify credentials are working
aws sts get-caller-identity
# Expected: prints your account ID, user ARN, and user ID
```

---

## STEP 3 — Run Unit Tests Locally

```bash
# From the project root directory
python -m pytest test_extractor.py -v

# Expected output:
# test_extractor.py::TestParseExpenseDocument::test_summary_fields_parsed_correctly PASSED
# test_extractor.py::TestParseExpenseDocument::test_invoice_id_is_always_set PASSED
# ... (all tests green)
```

---

## STEP 4 — Create AWS Infrastructure (one-time setup)

### 4a. Create the DynamoDB Table

```bash
aws dynamodb create-table \
  --table-name InvoiceExpenses \
  --attribute-definitions \
      AttributeName=invoiceId,AttributeType=S \
      AttributeName=timestamp,AttributeType=S \
  --key-schema \
      AttributeName=invoiceId,KeyType=HASH \
      AttributeName=timestamp,KeyType=RANGE \
  --billing-mode PAY_PER_REQUEST \
  --region us-east-1

# Enable TTL (auto-delete records after 1 year)
aws dynamodb update-time-to-live \
  --table-name InvoiceExpenses \
  --time-to-live-specification Enabled=true,AttributeName=ttl
```

### 4b. Create the IAM Role for Lambda

```bash
# Create the trust policy file
cat > trust-policy.json << 'EOF'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Service": "lambda.amazonaws.com" },
    "Action": "sts:AssumeRole"
  }]
}
EOF

# Create the role
aws iam create-role \
  --role-name LambdaInvoiceProcessorRole \
  --assume-role-policy-document file://trust-policy.json

# Attach required AWS managed policies
aws iam attach-role-policy \
  --role-name LambdaInvoiceProcessorRole \
  --policy-arn arn:aws:iam::aws:policy/AWSLambdaBasicExecutionRole

aws iam attach-role-policy \
  --role-name LambdaInvoiceProcessorRole \
  --policy-arn arn:aws:iam::aws:policy/AmazonTextractFullAccess

aws iam attach-role-policy \
  --role-name LambdaInvoiceProcessorRole \
  --policy-arn arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess

aws iam attach-role-policy \
  --role-name LambdaInvoiceProcessorRole \
  --policy-arn arn:aws:iam::aws:policy/AmazonDynamoDBFullAccess

# Save the role ARN — you need it in Step 5
aws iam get-role \
  --role-name LambdaInvoiceProcessorRole \
  --query 'Role.Arn' \
  --output text
# e.g. arn:aws:iam::123456789012:role/LambdaInvoiceProcessorRole
```

### 4c. Create the S3 Source Bucket

```bash
# Replace YOUR-UNIQUE-BUCKET-NAME with something globally unique
aws s3api create-bucket \
  --bucket YOUR-UNIQUE-BUCKET-NAME \
  --region us-east-1

# Block all public access (security best practice)
aws s3api put-public-access-block \
  --bucket YOUR-UNIQUE-BUCKET-NAME \
  --public-access-block-configuration \
      BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

---

## STEP 5 — Package and Deploy the Lambda Function

### 5a. Create the deployment ZIP

```bash
# boto3 is pre-installed in Lambda — no need to bundle it.
# Only bundle third-party packages NOT in the Lambda runtime.
# For this project: just zip the source files.

zip -r function.zip lambda_function.py extractor.py

# Verify the zip contents
unzip -l function.zip
```

### 5b. Create the Lambda function (first deployment only)

```bash
# Replace the role ARN with the one from Step 4b
aws lambda create-function \
  --function-name InvoiceProcessor \
  --runtime python3.14 \
  --role arn:aws:iam::YOUR-ACCOUNT-ID:role/LambdaInvoiceProcessorRole \
  --handler lambda_function.lambda_handler \
  --zip-file fileb://function.zip \
  --timeout 60 \
  --memory-size 256 \
  --environment Variables="{DYNAMODB_TABLE=InvoiceExpenses}" \
  --region us-east-1
```

### 5c. Update the function (subsequent deployments)

```bash
# Rebuild the zip
zip -r function.zip lambda_function.py extractor.py

# Push the new code
aws lambda update-function-code \
  --function-name InvoiceProcessor \
  --zip-file fileb://function.zip \
  --region us-east-1

# Wait for update to finish
aws lambda wait function-updated \
  --function-name InvoiceProcessor
```

---

## STEP 6 — Connect S3 Trigger to Lambda

### 6a. Grant S3 permission to invoke Lambda

```bash
# Replace YOUR-ACCOUNT-ID and YOUR-UNIQUE-BUCKET-NAME
aws lambda add-permission \
  --function-name InvoiceProcessor \
  --statement-id s3-invoke \
  --action lambda:InvokeFunction \
  --principal s3.amazonaws.com \
  --source-arn arn:aws:s3:::YOUR-UNIQUE-BUCKET-NAME \
  --source-account YOUR-ACCOUNT-ID
```

### 6b. Create the S3 event notification

```bash
cat > notification.json << 'EOF'
{
  "LambdaFunctionConfigurations": [{
    "LambdaFunctionArn": "arn:aws:lambda:us-east-1:YOUR-ACCOUNT-ID:function:InvoiceProcessor",
    "Events": ["s3:ObjectCreated:*"],
    "Filter": {
      "Key": {
        "FilterRules": [
          { "Name": "suffix", "Value": ".jpg" },
          { "Name": "suffix", "Value": ".png" }
        ]
      }
    }
  }]
}
EOF

aws s3api put-bucket-notification-configuration \
  --bucket YOUR-UNIQUE-BUCKET-NAME \
  --notification-configuration file://notification.json
```

> ⚠️ Note: S3 only supports one suffix filter per configuration block.
> For multiple file types (jpg, png, pdf) you'll need to either:
> - Use **three separate** LambdaFunctionConfigurations (one per suffix), OR
> - Drop the suffix filter and handle file type checks inside your Lambda code.

---

## STEP 7 — Test End-to-End

```bash
# 7a. Upload a test invoice image
aws s3 cp sample-invoice.jpg s3://YOUR-UNIQUE-BUCKET-NAME/

# 7b. Watch the Lambda logs in real time
aws logs tail /aws/lambda/InvoiceProcessor --follow

# 7c. Verify data landed in DynamoDB
aws dynamodb scan \
  --table-name InvoiceExpenses \
  --query 'Items[*].{ID:invoiceId.S,Vendor:vendorName.S,Total:totalAmount.S}' \
  --output table
```

---

## STEP 8 — Invoke Lambda Manually (for debugging)

```bash
# Create a fake S3 event payload
cat > test-event.json << 'EOF'
{
  "Records": [{
    "s3": {
      "bucket": { "name": "YOUR-UNIQUE-BUCKET-NAME" },
      "object": { "key": "sample-invoice.jpg" }
    }
  }]
}
EOF

# Invoke and capture the response
aws lambda invoke \
  --function-name InvoiceProcessor \
  --payload file://test-event.json \
  --cli-binary-format raw-in-base64-out \
  response.json

cat response.json
```

---

## Common Errors & Fixes

| Error | Cause | Fix |
|-------|-------|-----|
| `Runtime.ImportModuleError` | Wrong handler path | Set handler to `lambda_function.lambda_handler` |
| `AccessDeniedException` (Textract) | Missing IAM policy | Attach `AmazonTextractFullAccess` to role |
| `ResourceNotFoundException` (DynamoDB) | Wrong table name | Check `DYNAMODB_TABLE` env var matches actual table |
| `InvalidS3ObjectException` | File type not supported | Textract supports JPG, PNG, PDF, TIFF only |
| `Task timed out` | Textract slow on large PDFs | Increase Lambda timeout to 120s |
| `ResourceConflictException` on deploy | Previous update in progress | Run `aws lambda wait function-updated` first |
