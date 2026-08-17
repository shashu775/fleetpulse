# FleetPulse -- EKS, built so the ONLY recurring charge is the cluster itself.
#
#   control plane .......... $0.1000/hr   <- "the EKS cluster"
#   2 x t3.small ........... $0.0416/hr
#   2 x 20GB gp3 ........... $0.0044/hr
#   2 x public IPv4 ........ $0.0100/hr
#   -----------------------------------
#   TOTAL .................. ~$0.156/hr  =  ~$0.62 per 4-hour session
#
# That total holds ONLY because several module defaults are turned off below.
# Each one is marked "COST GUARD". Do not remove one without knowing its price.
#
#   terraform apply                                          # ~15-20 min
#   aws eks update-kubeconfig --name fleetpulse-tf --region us-east-1
#   terraform destroy                                        # EVERY session. The
#                                                            # teardown is the skill.

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, 2)
}

# ---------------------------------------------------------------------------
# NETWORK -- public subnets only
# ---------------------------------------------------------------------------
# Everything here is free: VPC, subnets, internet gateway, route tables and
# security groups carry no hourly charge. The one thing in this module that DOES
# bill is the NAT Gateway, and it defaults to ON.
module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.13"

  name = "${var.cluster_name}-vpc"
  cidr = var.vpc_cidr
  azs  = local.azs

  # Public subnets ONLY, deliberately. The usual private-subnet layout requires a
  # NAT Gateway for nodes to reach ECR and the EKS API; without one, private
  # subnets are simply unroutable and nodes never join the cluster. Rather than
  # ship subnets that cannot work, there are none.
  public_subnets = [cidrsubnet(var.vpc_cidr, 8, 1), cidrsubnet(var.vpc_cidr, 8, 2)]

  # COST GUARD: module default is `true` -> ~$33/month + $0.045/GB processed.
  # This single line is the largest saving in the file.
  enable_nat_gateway = false

  # Nodes need public IPs to reach ECR and the control plane without NAT.
  #
  # /!\ AWS now bills $0.005/hr per public IPv4 (since Feb 2024), so 2 nodes add
  #     ~$0.01/hr. That is real but still ~4.5x cheaper than the NAT Gateway it
  #     replaces. FleetPulse-Kubernetes.md section 3.5 predates this charge and
  #     its arithmetic is slightly stale as a result.
  map_public_ip_on_launch = true

  enable_dns_hostnames = true
  enable_dns_support   = true

  # REQUIRED for the AWS Load Balancer Controller to discover subnets. No LB is
  # created here, but an untagged subnet is a baffling silent failure later if
  # you ever add one.
  public_subnet_tags = {
    "kubernetes.io/role/elb" = "1"
  }
}

# ---------------------------------------------------------------------------
# EKS
# ---------------------------------------------------------------------------
module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.24"

  cluster_name = var.cluster_name

  # cluster_version is deliberately NOT pinned. Tracking the module default keeps
  # the cluster in standard support; an out-of-support version costs 6x on the
  # control plane ($0.60/hr instead of $0.10/hr).

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.public_subnets

  cluster_endpoint_public_access = true

  # Without this, the identity that runs `terraform apply` gets no Kubernetes RBAC
  # and every kubectl call returns "error: You must be logged in to the server".
  enable_cluster_creator_admin_permissions = true

  # COST GUARD: the module defaults to creating a customer-managed KMS key for
  # envelope-encrypting Kubernetes secrets -- ~$1/month plus per-API-call charges,
  # and it survives `terraform destroy` in a 30-day pending-deletion window.
  # Secrets are still encrypted at rest by AWS-owned keys without it.
  create_kms_key            = false
  cluster_encryption_config = {}

  # COST GUARD: the module defaults to ["audit", "api", "authenticator"] streamed
  # to CloudWatch Logs. On a busy cluster that is $15-45/month, and the log group
  # outlives the cluster. Empty list = no log group, no ingestion, no storage.
  cluster_enabled_log_types              = []
  create_cloudwatch_log_group            = false
  cloudwatch_log_group_retention_in_days = 1

  # Only the three addons that make a cluster functional. All three are just pods
  # on your own nodes -- no addon fee.
  #
  # NOT installed, on purpose:
  #   - aws-ebs-csi-driver          -> see var.enable_ebs_csi below
  #   - aws-load-balancer-controller -> an Ingress would create a ~$16/mo ALB
  #   - amazon-cloudwatch-observability / Container Insights -> metrics billing
  #   - Prometheus / AMP            -> explicitly excluded
  cluster_addons = merge(
    {
      vpc-cni    = {}
      coredns    = {}
      kube-proxy = {}
    },
    var.enable_ebs_csi ? {
      aws-ebs-csi-driver = {
        service_account_role_arn = module.ebs_csi_irsa[0].iam_role_arn
      }
    } : {}
  )

  eks_managed_node_groups = {
    default = {
      instance_types = [var.node_instance_type]
      min_size       = var.node_min_size
      max_size       = var.node_max_size
      desired_size   = var.node_desired_size
      disk_size      = var.node_disk_size
      subnet_ids     = module.vpc.public_subnets

      # ON_DEMAND for predictability. SPOT is ~70% cheaper and a reasonable change
      # for a learning cluster -- but a reclaimed node mid-exercise looks exactly
      # like a bug you did not cause.
      capacity_type = "ON_DEMAND"
    }
  }
}

# ---------------------------------------------------------------------------
# OPTIONAL -- EBS CSI driver (see the long note on var.enable_ebs_csi)
# ---------------------------------------------------------------------------
# This is IRSA: a Kubernetes ServiceAccount assumes an IAM role through the
# cluster's OIDC provider, so the pod gets temporary credentials with no access
# keys on disk or in the image.
module "ebs_csi_irsa" {
  count = var.enable_ebs_csi ? 1 : 0

  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.44"

  role_name             = "${var.cluster_name}-ebs-csi"
  attach_ebs_csi_policy = true

  oidc_providers = {
    main = {
      provider_arn = module.eks.oidc_provider_arn
      # The trust policy pins BOTH the namespace and the service account name.
      # Pinning only the audience would let any service account in the cluster
      # assume this role.
      namespace_service_accounts = ["kube-system:ebs-csi-controller-sa"]
    }
  }
}
