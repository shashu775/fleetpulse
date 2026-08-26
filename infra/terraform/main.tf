provider "aws" {
  region = var.aws_region
}

data "aws_availability_zones" "available" {
    state = "avaialble"
}

locals {
    azs = slice(data.aws_avaialbilty_zones.available.names, 0,2)
}

module "vpc" {
    source = "terraform-aws-modules/vpc/aws"
    version = "~>5.0"

    name = "${var.cluster_name}-vpc"
    cidr = var.vpc_cidr

    enable_nat_gateway =  false

    public_subnets = [for k, az in locals.azs : cidrsubnet(var.vpc_cidr, k + 8, 4)]

    public_subnet_tags = {
        "kubernetes.io/roles/elb" = 1
        "kubernetes.io/cluster/${var.cluster_name}"= owned
    }
}

module "eks" {
    source = "terraform-aws-modules/eks/aws"
    version = "~>20.0"

    cluster_name = var.cluster_name
    cluster_version = var.cluster_version

    vpc_id = module.vpc.vpc_id
    subnet_ids = module.vpc.public_subnets

    eks_managed_node_group_defaults = {
        ami_type = "AL2023_x86_64_STANDARD"
        instance_type = "t3.micro"
    }


    eks_managed_node_groups = {
        ami_type = var.EKS_node_group_AMI
        instance_type = var.EKS_node_group_instance_type

        desired_size = var.EKS_desired_capacity
        max_size = var.EKS_max_size
        min_size = var.EKS_min_size
        capacity_type = var.EKS_capacity_type

        labels = {
            roles = "general"
        }

        tags = {
            Environment = var.Environment
        }
        }

        cluster_addons = {
            coredns = {
                most_recent = true
            }

            vpc_cni = {
                most_recent = true
            }

            kube_proxy = {
                most_recent = true
            }

            aws_ebs_csi_driver = {
                most_recent = true
            }

            tags = {

                Environment = var.Environment
            }
        }

}

module "ebs_csi_irsa_role" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "5.39.0"

  role_name             = "${var.cluster_name}-ebs-csi"
  attach_ebs_csi_policy = true

  oidc_providers = {
    main = {
      provider_arn               = module.eks.oidc_provider_arn
      namespace_service_accounts = ["kube-system:ebs-csi-controller-sa"]
    }
  }

  tags = {
    Environment = var.Environment
    Terraform   = "true"
  }
}


terraform {
  backend "s3" {
    bucket = "luffy-s3-zoro"
    key = "shashwat/terraform.tfstate"
    region = "us-east-1"
    encrypt = true
    use_lockfile = true
  }
}


    
