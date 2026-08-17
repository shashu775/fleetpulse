terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
  }

  # State is local on purpose. An S3 backend needs a bucket + DynamoDB table that
  # would themselves bill and outlive `terraform destroy` -- exactly what this
  # config is trying to avoid. Move to a remote backend when more than one person
  # (or CI) runs this.
  #
  # /!\ terraform.tfstate holds every value in plaintext. It is gitignored. Keep it
  #     that way, and never paste its contents anywhere.
}

provider "aws" {
  region = var.region

  # Every resource gets these, so a forgotten cluster is findable in Cost Explorer
  # by tag rather than by guesswork.
  default_tags {
    tags = {
      Project     = "fleetpulse"
      Environment = "dev"
      ManagedBy   = "terraform"
    }
  }
}
