# ---------------------------------------------------------------------------
# Ingestion module: the upload_trigger Lambda.
#
# Flow: client PUTs to raw bucket -> s3:ObjectCreated -> this Lambda parses
# the key, derives basic metadata, and writes an item to the DynamoDB catalog.
# (Rekognition / EXIF / parquet land in Phase 2's enrichment module.)
# ---------------------------------------------------------------------------

locals {
  function_name = "${var.project_name}-upload-trigger"
  source_dir    = "${path.module}/../../../lambdas/upload_trigger"
}

# --- Package the handler into a zip ----------------------------------------
# upload_trigger only uses boto3, which the Lambda runtime already provides,
# so we just zip the source directory (no pip install step needed).
data "archive_file" "upload_trigger" {
  type        = "zip"
  source_dir  = local.source_dir
  output_path = "${path.module}/build/upload_trigger.zip"
  excludes    = ["__pycache__", "requirements.txt"]
}

# --- IAM role ---------------------------------------------------------------
data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "upload_trigger" {
  name               = "${local.function_name}-role"
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags               = var.tags
}

# Least-privilege: read object metadata from raw, write items to catalog,
# and write its own logs.
data "aws_iam_policy_document" "upload_trigger" {
  statement {
    sid       = "ReadRawObjects"
    actions   = ["s3:GetObject", "s3:GetObjectTagging", "s3:HeadObject"]
    resources = ["${var.raw_bucket_arn}/*"]
  }

  statement {
    sid       = "WriteCatalog"
    actions   = ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:GetItem"]
    resources = [var.catalog_table_arn]
  }
}

resource "aws_iam_role_policy" "upload_trigger" {
  name   = "${local.function_name}-policy"
  role   = aws_iam_role.upload_trigger.id
  policy = data.aws_iam_policy_document.upload_trigger.json
}

# Scoped logging permissions (instead of the broad managed basic-execution role).
resource "aws_cloudwatch_log_group" "upload_trigger" {
  name              = "/aws/lambda/${local.function_name}"
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

data "aws_iam_policy_document" "logs" {
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.upload_trigger.arn}:*"]
  }
}

resource "aws_iam_role_policy" "logs" {
  name   = "${local.function_name}-logs"
  role   = aws_iam_role.upload_trigger.id
  policy = data.aws_iam_policy_document.logs.json
}

# --- Lambda function --------------------------------------------------------
resource "aws_lambda_function" "upload_trigger" {
  function_name    = local.function_name
  role             = aws_iam_role.upload_trigger.arn
  handler          = "handler.handler"
  runtime          = "python3.11"
  architectures    = ["arm64"] # cheaper + faster than x86 for this workload
  timeout          = 30
  memory_size      = 256
  filename         = data.archive_file.upload_trigger.output_path
  source_code_hash = data.archive_file.upload_trigger.output_base64sha256

  environment {
    variables = {
      CATALOG_TABLE = var.catalog_table_name
      RAW_BUCKET    = var.raw_bucket_name
      AWS_REGION_   = var.aws_region # AWS_REGION is reserved; use a custom key
      LOG_LEVEL     = "INFO"
    }
  }

  depends_on = [aws_cloudwatch_log_group.upload_trigger]
  tags       = var.tags
}

# --- S3 -> Lambda notification ---------------------------------------------
resource "aws_lambda_permission" "allow_s3" {
  statement_id  = "AllowS3Invoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.upload_trigger.function_name
  principal     = "s3.amazonaws.com"
  source_arn    = var.raw_bucket_arn
}

resource "aws_s3_bucket_notification" "raw" {
  bucket = var.raw_bucket_name

  lambda_function {
    lambda_function_arn = aws_lambda_function.upload_trigger.arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "raw/"
  }

  depends_on = [aws_lambda_permission.allow_s3]
}
