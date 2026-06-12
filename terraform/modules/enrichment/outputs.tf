output "function_name" {
  description = "Name of the enrichment Lambda."
  value       = aws_lambda_function.enrichment.function_name
}

output "function_arn" {
  description = "ARN of the enrichment Lambda."
  value       = aws_lambda_function.enrichment.arn
}

output "role_arn" {
  description = "ARN of the Lambda execution role."
  value       = aws_iam_role.enrichment.arn
}

output "glue_database_name" {
  description = "Glue database holding the media metadata table."
  value       = aws_glue_catalog_database.lake.name
}

output "glue_table_name" {
  description = "Glue table over the parquet metadata prefix."
  value       = aws_glue_catalog_table.media_metadata.name
}

output "athena_workgroup_name" {
  description = "Athena workgroup with results location + scan cutoff configured."
  value       = aws_athena_workgroup.lake.name
}
