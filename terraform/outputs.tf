output "raw_bucket_name" {
  description = "S3 bucket where original uploads land."
  value       = module.storage.raw_bucket_name
}

output "raw_bucket_arn" {
  value = module.storage.raw_bucket_arn
}

output "processed_bucket_name" {
  description = "S3 bucket for thumbnails, parquet metadata, and album manifests."
  value       = module.storage.processed_bucket_name
}

output "processed_bucket_arn" {
  value = module.storage.processed_bucket_arn
}

output "catalog_table_name" {
  description = "DynamoDB media catalog table name."
  value       = module.storage.catalog_table_name
}

output "catalog_table_arn" {
  value = module.storage.catalog_table_arn
}

output "upload_trigger_function_name" {
  description = "Lambda invoked on s3:ObjectCreated in the raw bucket."
  value       = module.ingestion.function_name
}

output "upload_trigger_function_arn" {
  value = module.ingestion.function_arn
}

output "enrichment_function_name" {
  description = "Lambda enriching new uploads (Rekognition + EXIF + Parquet)."
  value       = module.enrichment.function_name
}

output "glue_database_name" {
  description = "Glue database for Athena queries."
  value       = module.enrichment.glue_database_name
}

output "glue_table_name" {
  description = "Glue table over the parquet metadata."
  value       = module.enrichment.glue_table_name
}

output "athena_workgroup_name" {
  description = "Athena workgroup (results location + scan cutoff preconfigured)."
  value       = module.enrichment.athena_workgroup_name
}

output "nightly_agent_state_machine_arn" {
  description = "Step Functions state machine running the nightly album agent."
  value       = module.agent.state_machine_arn
}

output "album_agent_function_name" {
  description = "Lambda doing face clustering + Claude album proposals."
  value       = module.agent.function_name
}

output "approval_topic_arn" {
  description = "SNS topic for album proposal approval emails."
  value       = module.agent.approval_topic_arn
}

output "face_collection_id" {
  description = "Rekognition face collection id."
  value       = module.agent.face_collection_id
}

output "cloudfront_domain_name" {
  description = "CloudFront domain for thumbnail URLs."
  value       = module.access.cloudfront_domain_name
}

output "search_api_endpoint" {
  description = "Base URL for the search HTTP API."
  value       = module.access.api_endpoint
}

output "cognito_user_pool_id" {
  description = "Cognito user pool id (create family users here)."
  value       = module.access.cognito_user_pool_id
}

output "cognito_client_id" {
  description = "Cognito app client id for login (Streamlit / web)."
  value       = module.access.cognito_client_id
}
