aws_region                   = "us-east-1"
cluster_name                 = "fleetpulse"
cluster_version              = "1.36"
vpc_cidr                     = "10.0.0.0/16"
EKS_node_group_name          = "fleetpulsenode"
EKS_node_group_AMI           = "AL2023_x86_64_STANDARD"
EKS_node_group_instance_type = "t2.micro"
EKS_desired_capacity         = "2"
EKS_min_size                 = "2"
EKS_max_size                 = "3"
EKS_capacity_type            = "ON_DEMAND"
Environment                  = "dev"


