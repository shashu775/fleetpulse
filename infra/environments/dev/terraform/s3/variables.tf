variable "s3_bucket" {
  description = "name of the bucket"
  type        = string
}

variable "s3_versioning_enabled" {
  description = "versioning enabled or not?"
  type        = bool

}

variable "aws_region" {
  description = "aws region"
  type        = string
}