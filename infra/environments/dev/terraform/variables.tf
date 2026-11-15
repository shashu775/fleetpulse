variable "aws_region" {
  description = "The AWS region to deploy resources in"
  type        = string
}

variable "cluster_name" {
  description = "The name of the EKS cluster"
  type        = string
}

variable "cluster_version" {
  description = "The version of the EKS cluster"
  type        = string
}

variable "vpc_cidr" {
  description = "The CIDR block for the VPC"
  type        = string
}



variable "EKS_node_group_name" {
  description = "the name of the EKS node group"
  type        = string
}

variable "EKS_node_group_AMI" {
  description = "The AMI ID for the EKS node group"
  type        = string
}


variable "EKS_node_group_instance_type" {
  description = "The instance type for the EKS node group"
  type        = string
}

variable "EKS_desired_capacity" {
  description = "the desired capacity of the EKS cluster"
  type        = number
}

variable "EKS_min_size" {
  description = "The minimum number of EKS nodes"
  type        = number
}

variable "EKS_max_size" {
  description = "The maximum number of EKS nodes"
  type        = number
}

variable "EKS_capacity_type" {
  description = "Type of capacity of EKS"
  type        = string
}

variable "Environment" {
  description = "Environment name"
  type        = string
}


