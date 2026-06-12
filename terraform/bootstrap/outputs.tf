output "state_bucket_name" {
  description = "Name of the S3 bucket holding remote Terraform state. Put this in terraform/backend.tf."
  value       = aws_s3_bucket.state.id
}

output "state_bucket_arn" {
  description = "ARN of the remote state bucket."
  value       = aws_s3_bucket.state.arn
}

output "lock_table_name" {
  description = "Name of the DynamoDB table used for state locking. Put this in terraform/backend.tf."
  value       = aws_dynamodb_table.lock.name
}

output "lock_table_arn" {
  description = "ARN of the state lock table."
  value       = aws_dynamodb_table.lock.arn
}

output "region" {
  description = "Region the state resources live in."
  value       = var.aws_region
}
