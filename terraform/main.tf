locals {
  account_id  = data.aws_caller_identity.current.account_id
  name_prefix = var.project_name

  common_tags = {
    Project = var.project_name
    Env     = var.environment
  }
}

# --- Phase 1: persistent storage (S3 data lake + DynamoDB catalog) ----------

module "storage" {
  source = "./modules/storage"

  project_name          = var.project_name
  account_id            = local.account_id
  raw_archive_days      = var.raw_archive_days
  raw_deep_archive_days = var.raw_deep_archive_days
  tags                  = local.common_tags
}

# --- Phase 1: ingestion (upload trigger Lambda + S3 notification) -----------

module "ingestion" {
  source = "./modules/ingestion"

  project_name       = var.project_name
  aws_region         = var.aws_region
  raw_bucket_name    = module.storage.raw_bucket_name
  raw_bucket_arn     = module.storage.raw_bucket_arn
  catalog_table_name = module.storage.catalog_table_name
  catalog_table_arn  = module.storage.catalog_table_arn
  log_retention_days = var.log_retention_days
  tags               = local.common_tags
}

# --- Phase 2: enrichment (Rekognition + EXIF + Parquet + Glue/Athena) -------

module "enrichment" {
  source = "./modules/enrichment"

  project_name          = var.project_name
  raw_bucket_name       = module.storage.raw_bucket_name
  raw_bucket_arn        = module.storage.raw_bucket_arn
  processed_bucket_name = module.storage.processed_bucket_name
  processed_bucket_arn  = module.storage.processed_bucket_arn
  catalog_table_name    = module.storage.catalog_table_name
  catalog_table_arn     = module.storage.catalog_table_arn
  catalog_stream_arn    = module.storage.catalog_table_stream_arn
  artifacts_bucket_name = module.storage.artifacts_bucket_name
  log_retention_days    = var.log_retention_days
  tags                  = local.common_tags
}

# --- Phase 3: AI agent (face clustering + Claude album proposals) -----------

module "agent" {
  source = "./modules/agent"

  project_name          = var.project_name
  aws_region            = var.aws_region
  account_id            = local.account_id
  raw_bucket_name       = module.storage.raw_bucket_name
  raw_bucket_arn        = module.storage.raw_bucket_arn
  processed_bucket_name = module.storage.processed_bucket_name
  processed_bucket_arn  = module.storage.processed_bucket_arn
  catalog_table_name    = module.storage.catalog_table_name
  catalog_table_arn     = module.storage.catalog_table_arn
  artifacts_bucket_name = module.storage.artifacts_bucket_name
  glue_database_name    = module.enrichment.glue_database_name

  bedrock_model_id    = var.bedrock_model_id
  approval_email      = var.approval_email
  schedule_expression = var.agent_schedule_expression
  schedule_enabled    = var.agent_schedule_enabled
  log_retention_days  = var.log_retention_days
  tags                = local.common_tags
}

# --- Phase 4: access (CloudFront + Cognito + search API) --------------------

module "access" {
  source = "./modules/access"

  project_name          = var.project_name
  aws_region            = var.aws_region
  account_id            = local.account_id
  processed_bucket_name = module.storage.processed_bucket_name
  processed_bucket_arn  = module.storage.processed_bucket_arn
  glue_database_name    = module.enrichment.glue_database_name
  glue_table_name       = module.enrichment.glue_table_name
  athena_workgroup_name = module.enrichment.athena_workgroup_name
  log_retention_days    = var.log_retention_days
  cors_allowed_origins  = var.api_cors_allowed_origins
  tags                  = local.common_tags
}
