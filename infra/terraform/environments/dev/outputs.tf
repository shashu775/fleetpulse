output "cluster_name" {
  description = "EKS cluster name."
  value       = module.eks.cluster_name
}

output "cluster_endpoint" {
  description = "Kubernetes API server endpoint."
  value       = module.eks.cluster_endpoint
}

output "region" {
  value = var.region
}

output "configure_kubectl" {
  description = "Run this to point kubectl at the new cluster."
  value       = "aws eks update-kubeconfig --name ${module.eks.cluster_name} --region ${var.region}"
}

output "storage_warning" {
  description = "Whether Postgres will actually start."
  value = var.enable_ebs_csi ? "EBS CSI enabled -- 03-postgres.yaml PVC will bind normally." : "NO CSI DRIVER: there is no default StorageClass, so the PVC in infra/k8s/base/03-postgres.yaml will stay Pending and postgres-0 will never start. Set enable_ebs_csi = true to fix (~$0.16/month)."
}

output "teardown" {
  description = "The habit that makes this affordable."
  value       = "kubectl delete ingress,pvc --all -A  #  BEFORE  ->  terraform destroy  ->  then verify with: aws eks list-clusters --region ${var.region}"
}
