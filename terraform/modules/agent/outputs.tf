output "function_name" {
  description = "Name of the album_agent Lambda."
  value       = aws_lambda_function.album_agent.function_name
}

output "function_arn" {
  description = "ARN of the album_agent Lambda."
  value       = aws_lambda_function.album_agent.arn
}

output "state_machine_arn" {
  description = "ARN of the nightly agent Step Functions state machine."
  value       = aws_sfn_state_machine.nightly_agent.arn
}

output "state_machine_name" {
  description = "Name of the nightly agent state machine."
  value       = aws_sfn_state_machine.nightly_agent.name
}

output "approval_topic_arn" {
  description = "SNS topic receiving album proposal summaries."
  value       = aws_sns_topic.approval.arn
}

output "face_collection_id" {
  description = "Rekognition face collection id."
  value       = aws_rekognition_collection.faces.collection_id
}

output "faces_table_name" {
  description = "DynamoDB table mapping Rekognition face ids to person clusters."
  value       = aws_dynamodb_table.faces.name
}

output "assignments_table_name" {
  description = "Glue table over album assignment parquet files."
  value       = aws_glue_catalog_table.album_assignments.name
}
