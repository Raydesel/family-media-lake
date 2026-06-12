# ---------------------------------------------------------------------------
# Storage module: the data lake's persistent stores.
#   - raw bucket        : original uploads (write-once, archived over time)
#   - processed bucket  : thumbnails, parquet metadata, album manifests
#   - catalog table     : DynamoDB item-per-media-file index
#
# Bucket names are suffixed with the account id so they are globally unique
# without manual coordination.
# ---------------------------------------------------------------------------

locals {
  raw_bucket_name       = "${var.project_name}-raw-${var.account_id}"
  processed_bucket_name = "${var.project_name}-processed-${var.account_id}"
  artifacts_bucket_name = "${var.project_name}-artifacts-${var.account_id}"
  catalog_table_name    = "${var.project_name}-catalog"
}

# ===========================================================================
# RAW BUCKET
# ===========================================================================

resource "aws_s3_bucket" "raw" {
  bucket = local.raw_bucket_name
  tags   = merge(var.tags, { Purpose = "raw-uploads" })
}

resource "aws_s3_bucket_versioning" "raw" {
  bucket = aws_s3_bucket.raw.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "raw" {
  bucket = aws_s3_bucket.raw.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "raw" {
  bucket                  = aws_s3_bucket.raw.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Move every object into Intelligent-Tiering immediately. IT then auto-shifts
# objects between frequent/infrequent access with no retrieval fees, which is
# the main guard against runaway storage cost as the library grows.
resource "aws_s3_bucket_lifecycle_configuration" "raw" {
  bucket = aws_s3_bucket.raw.id

  rule {
    id     = "to-intelligent-tiering"
    status = "Enabled"
    filter {}

    transition {
      days          = 0
      storage_class = "INTELLIGENT_TIERING"
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# Optional async archive tiers WITHIN Intelligent-Tiering. These deepen the
# savings for old originals. NOTE: objects in the (Deep) Archive tiers require
# an asynchronous restore (minutes to hours) before they can be downloaded,
# so thumbnails are deliberately kept in the processed bucket, not here.
resource "aws_s3_bucket_intelligent_tiering_configuration" "raw_archive" {
  bucket = aws_s3_bucket.raw.id
  name   = "archive-old-originals"
  status = "Enabled"

  tiering {
    access_tier = "ARCHIVE_ACCESS"
    days        = var.raw_archive_days
  }

  tiering {
    access_tier = "DEEP_ARCHIVE_ACCESS"
    days        = var.raw_deep_archive_days
  }
}

# CORS so a browser can PUT directly to a pre-signed URL.
resource "aws_s3_bucket_cors_configuration" "raw" {
  bucket = aws_s3_bucket.raw.id

  cors_rule {
    allowed_methods = ["PUT", "GET", "HEAD"]
    allowed_origins = ["*"] # tighten to your domain(s) before going public
    allowed_headers = ["*"]
    expose_headers  = ["ETag"]
    max_age_seconds = 3000
  }
}

# ===========================================================================
# PROCESSED BUCKET
# ===========================================================================

resource "aws_s3_bucket" "processed" {
  bucket = local.processed_bucket_name
  tags   = merge(var.tags, { Purpose = "processed-derivatives" })
}

resource "aws_s3_bucket_versioning" "processed" {
  bucket = aws_s3_bucket.processed.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "processed" {
  bucket = aws_s3_bucket.processed.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "processed" {
  bucket                  = aws_s3_bucket.processed.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Processed objects (thumbnails especially) must stay instantly retrievable,
# so we use plain Intelligent-Tiering WITHOUT archive tiers.
resource "aws_s3_bucket_lifecycle_configuration" "processed" {
  bucket = aws_s3_bucket.processed.id

  rule {
    id     = "to-intelligent-tiering"
    status = "Enabled"
    filter {}

    transition {
      days          = 0
      storage_class = "INTELLIGENT_TIERING"
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  # Athena query results are scratch data; expire them quickly so repeated
  # queries never accumulate storage cost.
  rule {
    id     = "expire-athena-results"
    status = "Enabled"

    filter {
      prefix = "athena-results/"
    }

    expiration {
      days = 7
    }
  }
}

# ===========================================================================
# ARTIFACTS BUCKET (Lambda deployment packages)
# ===========================================================================
# Some Lambda zips (Pillow + pyarrow) exceed the ~50MB direct-upload limit,
# so Terraform stages them here and Lambda pulls from S3. Costs ~nothing:
# a handful of zips, versioning off, replaced in place on each deploy.

resource "aws_s3_bucket" "artifacts" {
  bucket = local.artifacts_bucket_name
  tags   = merge(var.tags, { Purpose = "lambda-artifacts" })
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ===========================================================================
# DYNAMODB MEDIA CATALOG
# ===========================================================================

resource "aws_dynamodb_table" "catalog" {
  name         = local.catalog_table_name
  billing_mode = "PAY_PER_REQUEST" # on-demand: scales to zero cost when idle
  hash_key     = "file_id"

  attribute {
    name = "file_id"
    type = "S"
  }

  attribute {
    name = "uploader"
    type = "S"
  }

  attribute {
    name = "upload_ts"
    type = "S"
  }

  # Query "all uploads by X, newest first" without scanning the table.
  global_secondary_index {
    name            = "uploader-upload_ts-index"
    hash_key        = "uploader"
    range_key       = "upload_ts"
    projection_type = "ALL"
  }

  # Stream lets Phase 2 enrichment react to new items if we choose that path.
  stream_enabled   = true
  stream_view_type = "NEW_AND_OLD_IMAGES"

  point_in_time_recovery {
    enabled = true
  }

  tags = merge(var.tags, { Purpose = "media-catalog" })
}
