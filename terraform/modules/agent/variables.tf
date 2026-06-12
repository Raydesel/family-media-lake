variable "project_name" {
  description = "Project slug used as a name prefix."
  type        = string
}

variable "aws_region" {
  description = "AWS region (used to build the Bedrock foundation-model ARN)."
  type        = string
}

variable "account_id" {
  description = "AWS account id (used to build the Bedrock inference-profile ARN)."
  type        = string
}

variable "raw_bucket_name" {
  description = "Raw uploads bucket (Rekognition IndexFaces reads originals here)."
  type        = string
}

variable "raw_bucket_arn" {
  description = "ARN of the raw uploads bucket."
  type        = string
}

variable "processed_bucket_name" {
  description = "Processed bucket (album manifests + assignment parquet)."
  type        = string
}

variable "processed_bucket_arn" {
  description = "ARN of the processed bucket."
  type        = string
}

variable "catalog_table_name" {
  description = "DynamoDB catalog table name."
  type        = string
}

variable "catalog_table_arn" {
  description = "ARN of the DynamoDB catalog table."
  type        = string
}

variable "artifacts_bucket_name" {
  description = "Bucket where the Lambda deployment zip is staged."
  type        = string
}

variable "glue_database_name" {
  description = "Glue database to register the album_assignments table in."
  type        = string
}

variable "bedrock_model_id" {
  description = <<-EOT
    Bedrock model id (or cross-region inference profile id) used for album
    proposals. Default is Claude 3.5 Haiku: cheap enough (~cents/run) to stay
    under $1/month nightly. Switching to a Sonnet-class model multiplies cost.
  EOT
  type        = string
  default     = "us.anthropic.claude-3-5-haiku-20241022-v1:0"
}

variable "approval_email" {
  description = "Email address subscribed to the album-approval SNS topic. Empty = no subscription (confirm the subscription email AWS sends you after apply)."
  type        = string
  default     = ""
}

variable "schedule_expression" {
  description = "EventBridge schedule for the nightly agent run (UTC)."
  type        = string
  default     = "cron(0 8 * * ? *)" # 08:00 UTC ~ 2am US Central
}

variable "schedule_enabled" {
  description = "Whether the nightly schedule actually fires (the workflow can always be started manually)."
  type        = bool
  default     = true
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for the Lambda log group."
  type        = number
  default     = 14
}

variable "tags" {
  description = "Common resource tags."
  type        = map(string)
  default     = {}
}
