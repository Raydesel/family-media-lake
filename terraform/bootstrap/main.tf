# ---------------------------------------------------------------------------
# Bootstrap: Terraform remote state backend.
#
# This stack is the ONLY one that uses LOCAL state, because it creates the
# very resources (S3 bucket + DynamoDB lock table) that the rest of the
# project uses as its remote backend (chicken-and-egg problem).
#
# Run order:
#   1. cd terraform/bootstrap && terraform init && terraform apply
#   2. Copy the outputs into terraform/backend.tf (already templated for you).
#   3. cd terraform && terraform init   (migrates to the S3 backend)
#
# Cost: an empty state bucket + on-demand lock table cost ~$0/month.
# ---------------------------------------------------------------------------

terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Local state on purpose. Commit the resulting terraform.tfstate is NOT
  # recommended (it is gitignored); instead this stack rarely changes.
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project   = var.project_name
      ManagedBy = "terraform"
      Stack     = "bootstrap"
    }
  }
}

data "aws_caller_identity" "current" {}

locals {
  # Globally-unique, deterministic names derived from the AWS account id so
  # nobody has to hand-pick a unique bucket name.
  state_bucket_name = "${var.project_name}-tfstate-${data.aws_caller_identity.current.account_id}"
  lock_table_name   = "${var.project_name}-tflock"
}

# --- Remote state bucket ----------------------------------------------------

resource "aws_s3_bucket" "state" {
  bucket = local.state_bucket_name

  # Safety: never let `terraform destroy` nuke the state bucket by accident.
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Expire old state versions so the bucket does not grow unbounded.
resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    id     = "expire-noncurrent-state-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 90
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# --- State lock table -------------------------------------------------------

resource "aws_dynamodb_table" "lock" {
  name         = local.lock_table_name
  billing_mode = "PAY_PER_REQUEST" # on-demand: no idle cost
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }

  lifecycle {
    prevent_destroy = true
  }
}
