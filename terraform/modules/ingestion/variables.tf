variable "project_name" {
  description = "Project slug used as a name prefix."
  type        = string
}

variable "aws_region" {
  description = "AWS region (passed to the Lambda env)."
  type        = string
}

variable "raw_bucket_name" {
  description = "Name of the raw bucket whose ObjectCreated events trigger the Lambda."
  type        = string
}

variable "raw_bucket_arn" {
  description = "ARN of the raw bucket (for IAM + S3 invoke permission)."
  type        = string
}

variable "catalog_table_name" {
  description = "DynamoDB catalog table the Lambda writes to."
  type        = string
}

variable "catalog_table_arn" {
  description = "ARN of the DynamoDB catalog table."
  type        = string
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
