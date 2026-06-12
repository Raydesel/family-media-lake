variable "project_name" {
  description = "Short project slug used as a prefix for all resource names."
  type        = string
  default     = "family-media"
}

variable "environment" {
  description = "Deployment environment (e.g. dev, prod). Used in tags and some names."
  type        = string
  default     = "dev"
}

variable "aws_region" {
  description = "AWS region for all resources."
  type        = string
  default     = "us-east-1"
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for Lambda log groups (keeps log costs bounded)."
  type        = number
  default     = 14
}

variable "raw_archive_days" {
  description = "Days before raw originals are eligible for Intelligent-Tiering Archive Access tier."
  type        = number
  default     = 90
}

variable "raw_deep_archive_days" {
  description = "Days before raw originals are eligible for Intelligent-Tiering Deep Archive tier."
  type        = number
  default     = 180
}

variable "bedrock_model_id" {
  description = "Bedrock model (or inference profile) id for album proposals. Default Claude 3.5 Haiku keeps nightly runs at ~cents each; Sonnet-class models cost ~10x."
  type        = string
  default     = "us.anthropic.claude-3-5-haiku-20241022-v1:0"
}

variable "approval_email" {
  description = "Email subscribed to album-proposal notifications (empty = no subscription)."
  type        = string
  default     = ""
}

variable "agent_schedule_expression" {
  description = "EventBridge cron for the nightly agent run (UTC)."
  type        = string
  default     = "cron(0 8 * * ? *)"
}

variable "agent_schedule_enabled" {
  description = "Whether the nightly agent schedule fires automatically."
  type        = bool
  default     = true
}

variable "api_cors_allowed_origins" {
  description = "Origins allowed to call the search HTTP API (Streamlit demo defaults to localhost:8501)."
  type        = list(string)
  default     = ["http://localhost:8501", "http://127.0.0.1:8501"]
}
