variable "project_name" {
  description = "Project slug used as a name prefix."
  type        = string
}

variable "aws_region" {
  description = "AWS region."
  type        = string
}

variable "account_id" {
  description = "AWS account id (for IAM ARNs)."
  type        = string
}

variable "processed_bucket_name" {
  description = "Processed bucket hosting thumbnails (CloudFront origin)."
  type        = string
}

variable "processed_bucket_arn" {
  description = "ARN of the processed bucket."
  type        = string
}

variable "glue_database_name" {
  description = "Glue database searched by the API Lambda."
  type        = string
}

variable "glue_table_name" {
  description = "Glue table searched by the API Lambda."
  type        = string
}

variable "athena_workgroup_name" {
  description = "Athena workgroup the search Lambda executes in."
  type        = string
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for Lambda (and optional API logs)."
  type        = number
  default     = 14
}

variable "cors_allowed_origins" {
  description = "Origins allowed to call the search HTTP API (Streamlit demo, etc.)."
  type        = list(string)
  default     = ["http://localhost:8501", "http://127.0.0.1:8501"]
}

variable "cloudfront_price_class" {
  description = "CloudFront price class. PriceClass_100 limits edge locations (cheaper)."
  type        = string
  default     = "PriceClass_100"
}

variable "tags" {
  description = "Common resource tags."
  type        = map(string)
  default     = {}
}
