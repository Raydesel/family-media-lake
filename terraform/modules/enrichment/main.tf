# ---------------------------------------------------------------------------
# Enrichment module (Phase 2).
#
#   DynamoDB stream (INSERTs from upload_trigger)
#     -> enrichment Lambda (EXIF + Rekognition + thumbnail + Parquet)
#     -> processed bucket (thumbnails/..., metadata/...)
#     -> Glue table (partition projection -- NO crawler, zero standing cost)
#     -> Athena workgroup (with a per-query scan cutoff as a cost guard)
# ---------------------------------------------------------------------------

locals {
  function_name = "${var.project_name}-enrichment"
  source_dir    = abspath("${path.module}/../../../lambdas/enrichment")
  build_dir     = "${path.module}/build/pkg"
  build_script   = abspath("${path.module}/../../../scripts/build_lambda_package.sh")
  fetch_ffmpeg   = abspath("${path.module}/../../../scripts/fetch_ffmpeg.sh")

  glue_database_name = replace(var.project_name, "-", "_")
  glue_table_name    = "media_metadata"
  metadata_location  = "s3://${var.processed_bucket_name}/metadata/"
}

# --- Package: pip install (linux/arm64 wheels) + zip + stage on S3 -----------
# Pillow + pyarrow exceed Lambda's direct-upload zip limit, so the zip is
# staged in the artifacts bucket. If the build dir is ever wiped without the
# triggers changing, re-run with:
#   terraform apply -replace=module.enrichment.null_resource.build
resource "null_resource" "build" {
  triggers = {
    requirements   = filemd5("${local.source_dir}/requirements.txt")
    source         = filemd5("${local.source_dir}/enrichment_handler.py")
    script         = filemd5(local.build_script)
    fetch_ffmpeg   = filemd5(local.fetch_ffmpeg)
  }

  provisioner "local-exec" {
    command = "bash '${local.build_script}' '${local.source_dir}' '${abspath(local.build_dir)}' && bash '${local.fetch_ffmpeg}' '${abspath(local.build_dir)}/bin'"
  }
}

data "archive_file" "enrichment" {
  type        = "zip"
  source_dir  = local.build_dir
  output_path = "${path.module}/build/enrichment.zip"

  depends_on = [null_resource.build]
}

resource "aws_s3_object" "enrichment_zip" {
  bucket      = var.artifacts_bucket_name
  key         = "lambda/enrichment.zip"
  source      = data.archive_file.enrichment.output_path
  source_hash = data.archive_file.enrichment.output_base64sha256
}

# --- IAM role -----------------------------------------------------------------

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "enrichment" {
  name               = "${local.function_name}-role"
  assume_role_policy = data.aws_iam_policy_document.assume.json
  tags               = var.tags
}

data "aws_iam_policy_document" "enrichment" {
  statement {
    sid       = "ReadRawObjects"
    actions   = ["s3:GetObject"]
    resources = ["${var.raw_bucket_arn}/*"]
  }

  # Write only to the prefixes this Lambda owns.
  statement {
    sid     = "WriteProcessedObjects"
    actions = ["s3:PutObject"]
    resources = [
      "${var.processed_bucket_arn}/thumbnails/*",
      "${var.processed_bucket_arn}/metadata/*",
    ]
  }

  statement {
    sid       = "DetectWithRekognition"
    actions   = ["rekognition:DetectLabels", "rekognition:DetectFaces"]
    resources = ["*"] # these actions do not support resource-level scoping
  }

  statement {
    sid       = "UpdateCatalog"
    actions   = ["dynamodb:UpdateItem"]
    resources = [var.catalog_table_arn]
  }

  statement {
    sid = "ReadCatalogStream"
    actions = [
      "dynamodb:GetRecords",
      "dynamodb:GetShardIterator",
      "dynamodb:DescribeStream",
      "dynamodb:ListStreams",
    ]
    resources = [var.catalog_stream_arn]
  }
}

resource "aws_iam_role_policy" "enrichment" {
  name   = "${local.function_name}-policy"
  role   = aws_iam_role.enrichment.id
  policy = data.aws_iam_policy_document.enrichment.json
}

resource "aws_cloudwatch_log_group" "enrichment" {
  name              = "/aws/lambda/${local.function_name}"
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

data "aws_iam_policy_document" "logs" {
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.enrichment.arn}:*"]
  }
}

resource "aws_iam_role_policy" "logs" {
  name   = "${local.function_name}-logs"
  role   = aws_iam_role.enrichment.id
  policy = data.aws_iam_policy_document.logs.json
}

# --- Lambda function ------------------------------------------------------------

resource "aws_lambda_function" "enrichment" {
  function_name = local.function_name
  role          = aws_iam_role.enrichment.arn
  handler       = "enrichment_handler.handler"
  runtime       = "python3.11"
  architectures = ["arm64"] # build script installs aarch64 wheels
  timeout       = 180
  memory_size   = 1536 # Pillow + ffmpeg frame extraction for videos

  ephemeral_storage {
    size = 1024 # /tmp space for downloaded originals before ffmpeg runs
  }

  s3_bucket        = aws_s3_object.enrichment_zip.bucket
  s3_key           = aws_s3_object.enrichment_zip.key
  source_code_hash = data.archive_file.enrichment.output_base64sha256

  environment {
    variables = {
      RAW_BUCKET           = var.raw_bucket_name
      PROCESSED_BUCKET     = var.processed_bucket_name
      CATALOG_TABLE        = var.catalog_table_name
      THUMBNAIL_MAX_PX     = "512"
      MAX_LABELS           = "25"
      MIN_LABEL_CONFIDENCE = "80"
      LOG_LEVEL            = "INFO"
    }
  }

  depends_on = [aws_cloudwatch_log_group.enrichment]
  tags       = var.tags
}

# --- DynamoDB stream trigger ------------------------------------------------------
# Only INSERT events (new uploads). MODIFY events from our own catalog updates
# are filtered out at the event-source level, so the Lambda never sees its own
# writes (no loop, no wasted invocations).

resource "aws_lambda_event_source_mapping" "catalog_stream" {
  event_source_arn  = var.catalog_stream_arn
  function_name     = aws_lambda_function.enrichment.arn
  starting_position = "LATEST"

  batch_size                         = 5
  maximum_batching_window_in_seconds = 5
  maximum_retry_attempts             = 3
  bisect_batch_on_function_error     = true
  function_response_types            = ["ReportBatchItemFailures"]

  filter_criteria {
    filter {
      pattern = jsonencode({ eventName = ["INSERT"] })
    }
  }

  depends_on = [aws_iam_role_policy.enrichment]
}

# --- Glue catalog ------------------------------------------------------------------

resource "aws_glue_catalog_database" "lake" {
  name        = local.glue_database_name
  description = "Family media data lake."
}

# Partition projection: Athena derives year/month/day partitions directly
# from the key layout. No crawler, no MSCK REPAIR, no per-partition writes.
resource "aws_glue_catalog_table" "media_metadata" {
  name          = local.glue_table_name
  database_name = aws_glue_catalog_database.lake.name
  table_type    = "EXTERNAL_TABLE"

  parameters = {
    EXTERNAL                    = "TRUE"
    classification              = "parquet"
    "projection.enabled"        = "true"
    "projection.year.type"      = "integer"
    "projection.year.range"     = "2000,2035"
    "projection.month.type"     = "integer"
    "projection.month.range"    = "1,12"
    "projection.month.digits"   = "2"
    "projection.day.type"       = "integer"
    "projection.day.range"      = "1,31"
    "projection.day.digits"     = "2"
    "storage.location.template" = "s3://${var.processed_bucket_name}/metadata/year=$${year}/month=$${month}/day=$${day}"
  }

  partition_keys {
    name = "year"
    type = "int"
  }
  partition_keys {
    name = "month"
    type = "int"
  }
  partition_keys {
    name = "day"
    type = "int"
  }

  storage_descriptor {
    location      = local.metadata_location
    input_format  = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"

    ser_de_info {
      serialization_library = "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
      parameters = {
        "serialization.format" = "1"
      }
    }

    columns {
      name = "file_id"
      type = "string"
    }
    columns {
      name = "original_filename"
      type = "string"
    }
    columns {
      name = "uploader"
      type = "string"
    }
    columns {
      name = "upload_ts"
      type = "timestamp"
    }
    columns {
      name = "capture_ts"
      type = "timestamp"
    }
    columns {
      name = "gps_lat"
      type = "double"
    }
    columns {
      name = "gps_lon"
      type = "double"
    }
    columns {
      name = "location_name"
      type = "string"
    }
    columns {
      name = "rekognition_labels"
      type = "array<string>"
    }
    columns {
      name = "rekognition_faces"
      type = "array<struct<face_id:string,confidence:double,age_low:int,age_high:int,emotion:string>>"
    }
    columns {
      name = "file_size_bytes"
      type = "bigint"
    }
    columns {
      name = "media_type"
      type = "string"
    }
    columns {
      name = "s3_raw_key"
      type = "string"
    }
    columns {
      name = "s3_thumbnail_key"
      type = "string"
    }
    columns {
      name = "album_ids"
      type = "array<string>"
    }
  }
}

# --- Athena -------------------------------------------------------------------------

resource "aws_athena_workgroup" "lake" {
  name          = "${var.project_name}-wg"
  force_destroy = true
  tags          = var.tags

  configuration {
    enforce_workgroup_configuration    = true
    publish_cloudwatch_metrics_enabled = false

    # Cost guard: refuse any query that would scan more than the cutoff.
    bytes_scanned_cutoff_per_query = var.athena_bytes_scanned_cutoff

    result_configuration {
      output_location = "s3://${var.processed_bucket_name}/athena-results/"

      encryption_configuration {
        encryption_option = "SSE_S3"
      }
    }
  }
}

resource "aws_athena_named_query" "photos_by_label" {
  name        = "photos-by-label"
  description = "Find photos carrying a given Rekognition label (partition-pruned)."
  workgroup   = aws_athena_workgroup.lake.name
  database    = aws_glue_catalog_database.lake.name

  query = <<-SQL
    SELECT file_id, original_filename, capture_ts, s3_thumbnail_key, rekognition_labels
    FROM ${local.glue_table_name}
    WHERE year = 2026 AND month = 6
      AND contains(rekognition_labels, 'Beach')
    ORDER BY capture_ts
    LIMIT 100;
  SQL
}

resource "aws_athena_named_query" "face_stats_by_day" {
  name        = "face-stats-by-day"
  description = "Photos and detected faces per day for a month."
  workgroup   = aws_athena_workgroup.lake.name
  database    = aws_glue_catalog_database.lake.name

  query = <<-SQL
    SELECT year, month, day,
           count(*)                          AS photos,
           sum(cardinality(rekognition_faces)) AS faces
    FROM ${local.glue_table_name}
    WHERE year = 2026 AND month = 6 AND media_type = 'photo'
    GROUP BY year, month, day
    ORDER BY day;
  SQL
}
