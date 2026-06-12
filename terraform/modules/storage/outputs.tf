output "raw_bucket_name" {
  description = "Name of the raw uploads bucket."
  value       = aws_s3_bucket.raw.id
}

output "raw_bucket_arn" {
  description = "ARN of the raw uploads bucket."
  value       = aws_s3_bucket.raw.arn
}

output "processed_bucket_name" {
  description = "Name of the processed derivatives bucket."
  value       = aws_s3_bucket.processed.id
}

output "processed_bucket_arn" {
  description = "ARN of the processed derivatives bucket."
  value       = aws_s3_bucket.processed.arn
}

output "catalog_table_name" {
  description = "Name of the DynamoDB media catalog table."
  value       = aws_dynamodb_table.catalog.name
}

output "catalog_table_arn" {
  description = "ARN of the DynamoDB media catalog table."
  value       = aws_dynamodb_table.catalog.arn
}

output "catalog_table_stream_arn" {
  description = "Stream ARN of the catalog table (for downstream consumers)."
  value       = aws_dynamodb_table.catalog.stream_arn
}

output "artifacts_bucket_name" {
  description = "Name of the bucket staging Lambda deployment packages."
  value       = aws_s3_bucket.artifacts.id
}

output "artifacts_bucket_arn" {
  description = "ARN of the Lambda artifacts bucket."
  value       = aws_s3_bucket.artifacts.arn
}
