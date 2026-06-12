# ---------------------------------------------------------------------------
# Agent module (Phase 3).
#
#   EventBridge (nightly cron)
#     -> Step Functions: nightly_agent
#          1. album_agent Lambda {action: cluster_faces}   (Rekognition)
#          2. album_agent Lambda {action: propose_albums}  (Claude on Bedrock)
#          3. SNS publish -> approval email
#
# Persistent pieces: Rekognition face collection, faces DynamoDB table,
# album_assignments Glue table, approval SNS topic.
#
# NOTE: Bedrock *model access* (account-level, per model) cannot be managed
# by Terraform -- grant it once in the Bedrock console before the first run.
# ---------------------------------------------------------------------------

locals {
  function_name      = "${var.project_name}-album-agent"
  state_machine_name = "${var.project_name}-nightly-agent"
  collection_id      = "${var.project_name}-faces"
  faces_table_name   = "${var.project_name}-faces"
  source_dir         = abspath("${path.module}/../../../lambdas/album_agent")
  build_dir          = "${path.module}/build/pkg"
  build_script       = abspath("${path.module}/../../../scripts/build_lambda_package.sh")
  asl_path           = abspath("${path.module}/../../../step_functions/nightly_agent.asl.json")
}

# --- Face clustering state ----------------------------------------------------

resource "aws_rekognition_collection" "faces" {
  collection_id = local.collection_id
  tags          = var.tags
}

resource "aws_dynamodb_table" "faces" {
  name         = local.faces_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "face_id"

  attribute {
    name = "face_id"
    type = "S"
  }

  attribute {
    name = "cluster_id"
    type = "S"
  }

  # "All faces of person X" without scanning.
  global_secondary_index {
    name            = "cluster-index"
    hash_key        = "cluster_id"
    projection_type = "ALL"
  }

  tags = merge(var.tags, { Purpose = "face-clusters" })
}

# --- Approval topic --------------------------------------------------------------

resource "aws_sns_topic" "approval" {
  name = "${var.project_name}-album-approval"
  tags = var.tags
}

resource "aws_sns_topic_subscription" "approval_email" {
  count = var.approval_email == "" ? 0 : 1

  topic_arn = aws_sns_topic.approval.arn
  protocol  = "email"
  endpoint  = var.approval_email
}

# --- Lambda package (pyarrow -> staged on S3, same pattern as enrichment) --------

resource "null_resource" "build" {
  triggers = {
    requirements = filemd5("${local.source_dir}/requirements.txt")
    source       = filemd5("${local.source_dir}/album_agent_handler.py")
    script       = filemd5(local.build_script)
  }

  provisioner "local-exec" {
    command = "bash '${local.build_script}' '${local.source_dir}' '${abspath(local.build_dir)}'"
  }
}

data "archive_file" "album_agent" {
  type        = "zip"
  source_dir  = local.build_dir
  output_path = "${path.module}/build/album_agent.zip"

  depends_on = [null_resource.build]
}

resource "aws_s3_object" "album_agent_zip" {
  bucket      = var.artifacts_bucket_name
  key         = "lambda/album_agent.zip"
  source      = data.archive_file.album_agent.output_path
  source_hash = data.archive_file.album_agent.output_base64sha256
}

# --- Lambda IAM ---------------------------------------------------------------------

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "album_agent" {
  name               = "${local.function_name}-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = var.tags
}

data "aws_iam_policy_document" "album_agent" {
  statement {
    sid       = "ReadRawForFaceIndexing"
    actions   = ["s3:GetObject"]
    resources = ["${var.raw_bucket_arn}/*"]
  }

  statement {
    sid     = "WriteAlbumOutputs"
    actions = ["s3:PutObject"]
    resources = [
      "${var.processed_bucket_arn}/albums/*",
      "${var.processed_bucket_arn}/album_assignments/*",
    ]
  }

  statement {
    sid = "FaceCollection"
    actions = [
      "rekognition:IndexFaces",
      "rekognition:SearchFaces",
      "rekognition:ListFaces",
    ]
    resources = [aws_rekognition_collection.faces.arn]
  }

  statement {
    sid       = "CatalogReadWrite"
    actions   = ["dynamodb:Scan", "dynamodb:GetItem", "dynamodb:UpdateItem"]
    resources = [var.catalog_table_arn]
  }

  statement {
    sid       = "FacesTableReadWrite"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Query"]
    resources = [aws_dynamodb_table.faces.arn, "${aws_dynamodb_table.faces.arn}/index/*"]
  }

  statement {
    sid     = "InvokeClaude"
    actions = ["bedrock:InvokeModel"]
    resources = [
      # Direct foundation-model ids; region wildcard because cross-region
      # inference profiles may route to sibling regions.
      "arn:aws:bedrock:*::foundation-model/anthropic.*",
      "arn:aws:bedrock:${var.aws_region}:${var.account_id}:inference-profile/*",
    ]
  }
}

resource "aws_iam_role_policy" "album_agent" {
  name   = "${local.function_name}-policy"
  role   = aws_iam_role.album_agent.id
  policy = data.aws_iam_policy_document.album_agent.json
}

resource "aws_cloudwatch_log_group" "album_agent" {
  name              = "/aws/lambda/${local.function_name}"
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

data "aws_iam_policy_document" "lambda_logs" {
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.album_agent.arn}:*"]
  }
}

resource "aws_iam_role_policy" "lambda_logs" {
  name   = "${local.function_name}-logs"
  role   = aws_iam_role.album_agent.id
  policy = data.aws_iam_policy_document.lambda_logs.json
}

# --- Lambda function ------------------------------------------------------------------

resource "aws_lambda_function" "album_agent" {
  function_name = local.function_name
  role          = aws_iam_role.album_agent.arn
  handler       = "album_agent_handler.handler"
  runtime       = "python3.11"
  architectures = ["arm64"]
  timeout       = 600 # clustering loops over Rekognition per photo
  memory_size   = 512

  s3_bucket        = aws_s3_object.album_agent_zip.bucket
  s3_key           = aws_s3_object.album_agent_zip.key
  source_code_hash = data.archive_file.album_agent.output_base64sha256

  environment {
    variables = {
      CATALOG_TABLE          = var.catalog_table_name
      FACES_TABLE            = aws_dynamodb_table.faces.name
      RAW_BUCKET             = var.raw_bucket_name
      PROCESSED_BUCKET       = var.processed_bucket_name
      FACE_COLLECTION_ID     = aws_rekognition_collection.faces.collection_id
      BEDROCK_MODEL_ID       = var.bedrock_model_id
      FACE_MATCH_THRESHOLD   = "90"
      MAX_ITEMS_PER_RUN      = "300"
      MIN_ITEMS_FOR_PROPOSAL = "5"
      LOG_LEVEL              = "INFO"
    }
  }

  depends_on = [aws_cloudwatch_log_group.album_agent]
  tags       = var.tags
}

# --- Step Functions ------------------------------------------------------------------

data "aws_iam_policy_document" "sfn_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "state_machine" {
  name               = "${local.state_machine_name}-role"
  assume_role_policy = data.aws_iam_policy_document.sfn_assume.json
  tags               = var.tags
}

data "aws_iam_policy_document" "state_machine" {
  statement {
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.album_agent.arn]
  }

  statement {
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.approval.arn]
  }
}

resource "aws_iam_role_policy" "state_machine" {
  name   = "${local.state_machine_name}-policy"
  role   = aws_iam_role.state_machine.id
  policy = data.aws_iam_policy_document.state_machine.json
}

resource "aws_sfn_state_machine" "nightly_agent" {
  name     = local.state_machine_name
  role_arn = aws_iam_role.state_machine.arn
  type     = "STANDARD" # 1 run/night: standard pricing is negligible
  tags     = var.tags

  definition = templatefile(local.asl_path, {
    AlbumAgentArn    = aws_lambda_function.album_agent.arn
    ApprovalTopicArn = aws_sns_topic.approval.arn
  })
}

# --- Nightly schedule ---------------------------------------------------------------

data "aws_iam_policy_document" "events_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${local.state_machine_name}-scheduler-role"
  assume_role_policy = data.aws_iam_policy_document.events_assume.json
  tags               = var.tags
}

data "aws_iam_policy_document" "scheduler" {
  statement {
    actions   = ["states:StartExecution"]
    resources = [aws_sfn_state_machine.nightly_agent.arn]
  }
}

resource "aws_iam_role_policy" "scheduler" {
  name   = "${local.state_machine_name}-scheduler-policy"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler.json
}

resource "aws_cloudwatch_event_rule" "nightly" {
  name                = "${local.state_machine_name}-schedule"
  description         = "Starts the nightly media agent workflow."
  schedule_expression = var.schedule_expression
  state               = var.schedule_enabled ? "ENABLED" : "DISABLED"
  tags                = var.tags
}

resource "aws_cloudwatch_event_target" "nightly" {
  rule     = aws_cloudwatch_event_rule.nightly.name
  arn      = aws_sfn_state_machine.nightly_agent.arn
  role_arn = aws_iam_role.scheduler.arn
}

# --- Glue: album assignments table ----------------------------------------------------
# Small, append-only, unpartitioned (a handful of rows per nightly run).
# Join against media_metadata on file_id to resolve current album membership.

resource "aws_glue_catalog_table" "album_assignments" {
  name          = "album_assignments"
  database_name = var.glue_database_name
  table_type    = "EXTERNAL_TABLE"

  parameters = {
    EXTERNAL       = "TRUE"
    classification = "parquet"
  }

  storage_descriptor {
    location      = "s3://${var.processed_bucket_name}/album_assignments/"
    input_format  = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"

    ser_de_info {
      serialization_library = "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
      parameters = {
        "serialization.format" = "1"
      }
    }

    columns {
      name = "album_id"
      type = "string"
    }
    columns {
      name = "album_name"
      type = "string"
    }
    columns {
      name = "file_id"
      type = "string"
    }
    columns {
      name = "status"
      type = "string"
    }
    columns {
      name = "assigned_ts"
      type = "timestamp"
    }
    columns {
      name = "model_id"
      type = "string"
    }
  }
}
