provider "aws" {
  region = var.aws_region
}

resource "aws_s3_bucket" "dev" {

  bucket = var.s3_bucket

}

resource "aws_s3_bucket_versioning" "dev" {
  bucket = aws_s3_bucket.dev.id
  versioning_configuration {
    status = var.s3_versioning_enabled ? "Enabled" : "Suspended"
  }
}
