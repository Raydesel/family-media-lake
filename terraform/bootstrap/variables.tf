variable "project_name" {
  description = "Short project slug used as a prefix for all resource names."
  type        = string
  default     = "family-media"
}

variable "aws_region" {
  description = "AWS region to create the remote-state resources in."
  type        = string
  default     = "us-east-1"
}
