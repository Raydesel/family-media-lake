variable "project_name" {
  description = "Project slug used as a name prefix."
  type        = string
}

variable "raw_bucket_name" {
  description = "Raw uploads bucket name (Rekognition reads originals here)."
  type        = string
}

variable "raw_bucket_arn" {
  description = "ARN of the raw uploads bucket."
  type        = string
}

variable "processed_bucket_name" {
  description = "Processed bucket name (thumbnails, parquet metadata, Athena results)."
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

variable "catalog_stream_arn" {
  description = "Stream ARN of the catalog table (triggers the enrichment Lambda)."
  type        = string
}

variable "artifacts_bucket_name" {
  description = "Bucket where the (large) Lambda deployment zip is staged."
  type        = string
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for the Lambda log group."
  type        = number
  default     = 14
}

variable "athena_bytes_scanned_cutoff" {
  description = "Per-query scan limit in bytes for the Athena workgroup (cost guard)."
  type        = number
  default     = 1073741824 # 1 GiB
}

variable "tags" {
  description = "Common resource tags."
  type        = map(string)
  default     = {}
}
