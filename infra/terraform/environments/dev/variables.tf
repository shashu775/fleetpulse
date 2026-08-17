variable "region" {
  description = "AWS region. Matches AWS_REGION in .env.example."
  type        = string
  default     = "us-east-1"
}

variable "cluster_name" {
  description = "EKS cluster name. Used by `aws eks update-kubeconfig --name`."
  type        = string
  default     = "fleetpulse-tf"
}

variable "vpc_cidr" {
  description = "CIDR for the cluster VPC. 10.1.0.0/16 avoids colliding with Docker's 172.17/16."
  type        = string
  default     = "10.1.0.0/16"
}

variable "node_instance_type" {
  description = <<-EOT
    Worker node size.

    /!\ Do NOT use t3.micro. The AWS VPC CNI gives every pod a real VPC IP drawn
    from the node's ENIs, so max-pods is a NETWORKING limit, not a memory one:

      t3.micro   2 ENIs x 2 IPs  ->  4 pods   <- kube-system alone needs ~4
      t3.small   3 ENIs x 4 IPs  -> 11 pods   <- realistic floor
      t3.medium  3 ENIs x 6 IPs  -> 17 pods

    FleetPulse is 8 pods (web x2, consignment x2, dispatch x2, redis, postgres)
    plus ~4 for kube-system. Two t3.small give 22 slots. See
    docs/FleetPulse-Kubernetes.md section 0.2.
  EOT
  type        = string
  default     = "t3.small"
}

variable "node_desired_size" {
  description = "Desired node count. 1 leaves no room for the HPA to scale consignment past 3."
  type        = number
  default     = 2
}

variable "node_min_size" {
  type    = number
  default = 1
}

variable "node_max_size" {
  type    = number
  default = 4
}

variable "node_disk_size" {
  description = "Root EBS volume per node, GiB. gp3, ~$0.08/GiB-month."
  type        = number
  default     = 20
}

variable "enable_ebs_csi" {
  description = <<-EOT
    Install the aws-ebs-csi-driver addon and its IRSA role.

    FALSE (default) honours the "only the cluster costs money" requirement -- but
    it has a consequence you WILL hit:

      /!\ With no CSI driver there is no default StorageClass, so the PVC in
          infra/k8s/base/03-postgres.yaml stays Pending forever, postgres-0 never
          starts, and both services fail their readiness probes behind it.

    Docker Desktop hides this because it ships a built-in `hostpath` provisioner.
    EKS ships none. So manifests that work locally will partially fail here --
    worth seeing deliberately once rather than debugging cold.

    Set true to make Postgres work. Cost is trivial: a 2Gi gp3 volume is about
    $0.16/month. The IRSA role it creates is also the clearest real example of
    "give a pod an IAM role" in this whole project.
  EOT
  type        = bool
  default     = false
}
