output "function_name" {
  description = "Name of the upload_trigger Lambda."
  value       = aws_lambda_function.upload_trigger.function_name
}

output "function_arn" {
  description = "ARN of the upload_trigger Lambda."
  value       = aws_lambda_function.upload_trigger.arn
}

output "role_arn" {
  description = "ARN of the Lambda execution role."
  value       = aws_iam_role.upload_trigger.arn
}

output "log_group_name" {
  description = "CloudWatch log group for the Lambda."
  value       = aws_cloudwatch_log_group.upload_trigger.name
}
