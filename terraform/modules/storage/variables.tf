variable "project_name" {
  description = "Project slug used as a name prefix."
  type        = string
}

variable "account_id" {
  description = "AWS account id, appended to bucket names for global uniqueness."
  type        = string
}

variable "raw_archive_days" {
  description = "Days before raw originals are eligible for IT Archive Access tier."
  type        = number
  default     = 90
}

variable "raw_deep_archive_days" {
  description = "Days before raw originals are eligible for IT Deep Archive tier."
  type        = number
  default     = 180
}

variable "tags" {
  description = "Common resource tags."
  type        = map(string)
  default     = {}
}
