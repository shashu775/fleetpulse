# FleetPulse on Kubernetes

Learning Kubernetes two ways: **minikube on Docker Desktop** for daily practice, and **AWS EKS**
for the cloud-specific skills you cannot learn locally.

Continues from [FleetPulse-Simple.md](FleetPulse-Simple.md). You should have the two FastAPI
services working under Docker Compose before starting here — putting a system you already
understand onto Kubernetes teaches far more than starting on Kubernetes cold.

---

## 0. Two tracks, and why you need both

### 0.1 The good news about EKS cost

You may have read that EKS costs $73/month. That is true **only if you leave it running.** The
control plane bills hourly, so a cluster you create and destroy per study session costs almost
nothing:

```
  Control plane ............... $0.10/hr
  2 x t3.small nodes .......... $0.0416/hr
  EBS 2 x 20 GB ............... ~$0.004/hr
  ─────────────────────────────────────────
  TOTAL ....................... ~$0.146/hr

  24x7 for a month ............ $107.00
  One 4-hour session .......... $  0.58
  Two sessions/week (32 hr) ... $  4.67   ← this is your real cost
```

**EKS is affordable if — and only if — you destroy the cluster every single time you finish.**
`eksctl delete cluster` takes 10 minutes and is the single habit that makes this whole track
possible. Everything in §5 is built around fast create/destroy for exactly this reason.

### 0.2 ⚠️ t3.micro does not work on EKS

This one surprises everyone. The AWS VPC CNI gives every pod a **real VPC IP address** drawn from
the node's network interfaces, so the pod limit is a networking constraint, not a memory one:

| Instance | ENIs × IPs | Max pods | Usable for your app |
|---|---|---|---|
| `t3.micro` | 2 × 2 | **4** | ❌ `kube-system` alone needs ~4 |
| `t3.small` | 3 × 4 | **11** | ✅ realistic floor |
| `t3.medium` | 3 × 6 | **17** | ✅ comfortable |

CoreDNS (×2), `aws-node`, and `kube-proxy` consume roughly four pod slots before you deploy
anything. **On `t3.micro` you have zero left.** Use `t3.small` minimum. This is unrelated to the
free tier — `t3.small` is not free-tier eligible, which is another reason the burst-and-destroy
pattern matters.

### 0.3 What each track teaches

Do not try to learn everything on EKS. Most Kubernetes concepts are identical everywhere, and
iterating locally is faster and free.

| Learn on **minikube** (free, unlimited) | Learn on **EKS** (burst sessions only) |
|---|---|
| Pods, Deployments, ReplicaSets | **IRSA** — IAM roles for pods |
| Services & cluster DNS | **AWS Load Balancer Controller** → real ALB |
| ConfigMaps & Secrets | **EBS CSI driver** → real persistent volumes |
| Liveness / readiness probes | Managed node groups & node autoscaling |
| Resource requests & limits | Multi-AZ scheduling, topology spread |
| Rolling updates & rollbacks | ECR auth via the node IAM role |
| Ingress routing | CloudWatch Container Insights |
| Helm charts | VPC CNI, pod density, subnet planning |
| HPA & metrics-server | `eksctl` and the Terraform EKS module |
| Jobs & init containers | Connecting pods to RDS across security groups |
| RBAC, namespaces | Cluster teardown discipline |
| `kubectl` debugging | Real cloud IAM failure modes |

**Rule of thumb:** if it works the same on any Kubernetes, learn it locally. Spend EKS hours only on
things that are genuinely AWS.

### 0.4 Your existing EC2 box

You have a `t3.micro` running Docker Compose. Leave it alone. It stays your always-on demo
environment — cheap, stable, and something you can show anyone at any time. Kubernetes is additive
here, not a replacement.

(K3s would technically fit on it at ~870 MB of 958 MB, but with 88 MB of headroom it would be
miserable to work on. minikube on your laptop is strictly better for learning.)

---

## 1. Track A — minikube on Docker Desktop

### 1.1 Setup (Windows 11 + PowerShell)

Install Docker Desktop, then:

```powershell
# Install the tools
winget install Kubernetes.minikube
winget install Kubernetes.kubectl
winget install Helm.Helm

# Start a cluster. 4 GB is enough for FleetPulse plus the addons.
minikube start --driver=docker --memory=4096 --cpus=2 --kubernetes-version=stable

# Addons you will actually use
minikube addons enable ingress          # NGINX ingress controller
minikube addons enable metrics-server   # required for HPA and `kubectl top`

kubectl get nodes
kubectl get pods -A
```

**Three Windows-specific things that trip people up:**

```powershell
# 1. Point your shell's Docker at minikube's internal daemon, so images you
#    build are immediately visible to the cluster (no registry needed).
#    The bash idiom `eval $(minikube docker-env)` does NOT work in PowerShell:
minikube docker-env | Invoke-Expression

# 2. `minikube tunnel` needs an Administrator PowerShell, and must stay
#    running in its own window. Only needed for LoadBalancer services.
minikube tunnel

# 3. Get the cluster IP for Ingress access
minikube ip
```

For the ingress hostname, add a line to
`C:\Windows\System32\drivers\etc\hosts` (edit as Administrator):

```
192.168.49.2   fleetpulse.local
```

Replace the IP with whatever `minikube ip` printed.

### 1.2 Directory layout

Add to the structure from [FleetPulse-Simple.md](FleetPulse-Simple.md):

```
fleetpulse/
├── k8s/
│   ├── base/                       # raw manifests — learn these FIRST
│   │   ├── 00-namespace.yaml
│   │   ├── 01-configmap.yaml
│   │   ├── 02-secret.yaml
│   │   ├── 03-redis.yaml
│   │   ├── 04-consignment.yaml
│   │   ├── 05-dispatch.yaml
│   │   ├── 06-ingress.yaml
│   │   ├── 07-hpa.yaml
│   │   └── 08-migration-job.yaml
│   ├── overlays/
│   │   ├── minikube/               # local Postgres, NodePort, 1 replica
│   │   └── eks/                    # RDS, ALB ingress, 2 replicas
│   └── helm/
│       └── fleetpulse/             # the same thing, packaged (Milestone 7)
└── infra/terraform/eks/            # EKS cluster as code (Milestone 8)
```

Write the raw manifests first and only move to Helm once they work. Helm templates a thing you
already understand; learning both simultaneously is how people end up confused about which layer is
broken.

### 1.3 Namespace, config, secrets

```yaml
# k8s/base/00-namespace.yaml
# A namespace is a folder for your resources. Everything below lives here,
# so `kubectl delete namespace fleetpulse` cleans up in one command.
apiVersion: v1
kind: Namespace
metadata:
  name: fleetpulse
```

```yaml
# k8s/base/01-configmap.yaml
# ConfigMaps hold NON-SECRET configuration. Same idea as the `environment:`
# block in docker-compose.yml.
apiVersion: v1
kind: ConfigMap
metadata:
  name: fleetpulse-config
  namespace: fleetpulse
data:
  LOG_LEVEL: "INFO"
  REDIS_URL: "redis://redis:6379/0"
  # Kubernetes DNS: "consignment-service" resolves to that Service inside the
  # namespace. Exactly like Docker Compose's service-name DNS — same idea,
  # different implementation.
  CONSIGNMENT_URL: "http://consignment-service:8000"
```

```yaml
# k8s/base/02-secret.yaml
# ⚠️ DO NOT COMMIT A REAL PASSWORD. Kubernetes Secrets are only base64-encoded,
# NOT encrypted. Anyone who can read the manifest can read the password.
#
# Create it from the command line instead, and gitignore this file:
#
#   kubectl create secret generic fleetpulse-secrets `
#     --namespace fleetpulse `
#     --from-literal=DATABASE_URL="postgresql://fleetadmin:PASSWORD@postgres:5432/fleetpulse"
#
# This file is committed only as documentation of the expected shape.
apiVersion: v1
kind: Secret
metadata:
  name: fleetpulse-secrets
  namespace: fleetpulse
type: Opaque
stringData:
  DATABASE_URL: "postgresql://fleetadmin:CHANGEME@postgres:5432/fleetpulse"
```

### 1.4 Redis

```yaml
# k8s/base/03-redis.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: redis
  namespace: fleetpulse
spec:
  replicas: 1
  selector:
    matchLabels: { app: redis }
  template:
    metadata:
      labels: { app: redis }
    spec:
      containers:
        - name: redis
          image: redis:7-alpine
          args: ["redis-server", "--maxmemory", "64mb", "--maxmemory-policy", "allkeys-lru"]
          ports:
            - containerPort: 6379
          resources:
            # requests = what the scheduler reserves for this pod
            # limits   = the hard ceiling before the pod is killed
            requests: { memory: "32Mi", cpu: "10m" }
            limits:   { memory: "96Mi" }
          livenessProbe:
            tcpSocket: { port: 6379 }
            initialDelaySeconds: 5
            periodSeconds: 10
---
apiVersion: v1
kind: Service
metadata:
  name: redis
  namespace: fleetpulse
spec:
  selector: { app: redis }
  ports:
    - port: 6379
      targetPort: 6379
```

> **Why a Deployment and not a StatefulSet?** Redis here is a *cache*. If it restarts and loses
> everything, the app repopulates it from Postgres — that is exactly why `cache.py` fails soft.
> StatefulSets are for workloads whose identity and storage must survive restarts. Knowing when
> *not* to reach for one is as useful as knowing how to use it.

### 1.5 The application Deployments

```yaml
# k8s/base/04-consignment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: consignment-service
  namespace: fleetpulse
  labels: { app: consignment-service }
spec:
  replicas: 2
  # How Kubernetes replaces pods during an update: create one new pod,
  # wait for it to be Ready, then remove an old one. Zero downtime.
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 1
      maxUnavailable: 0
  selector:
    matchLabels: { app: consignment-service }
  template:
    metadata:
      labels: { app: consignment-service }
    spec:
      containers:
        - name: consignment
          image: fleetpulse/consignment-service:latest
          # For minikube with a locally built image. On EKS set this to
          # IfNotPresent so it pulls from ECR.
          imagePullPolicy: IfNotPresent
          ports:
            - containerPort: 8000
              name: http

          # Non-secret config from the ConfigMap, secrets from the Secret.
          envFrom:
            - configMapRef: { name: fleetpulse-config }
            - secretRef:    { name: fleetpulse-secrets }

          resources:
            requests: { memory: "96Mi", cpu: "50m" }
            limits:   { memory: "256Mi" }
            # NOTE: no CPU limit on purpose. CPU limits cause throttling that
            # shows up as mysterious slow requests. Set requests accurately
            # and let the pod burst.

          # READINESS: "can this pod serve traffic right now?"
          # If it fails, the pod is removed from the Service — but NOT restarted.
          readinessProbe:
            httpGet: { path: /health, port: 8000 }
            initialDelaySeconds: 5
            periodSeconds: 5
            failureThreshold: 3

          # LIVENESS: "is this process broken beyond recovery?"
          # If it fails, the pod is KILLED and restarted. Keep this check
          # simple — never make it depend on the database. A DB blip would
          # then restart every pod at once and turn a small problem into
          # an outage.
          livenessProbe:
            httpGet: { path: /health, port: 8000 }
            initialDelaySeconds: 15
            periodSeconds: 20
            failureThreshold: 3
---
apiVersion: v1
kind: Service
metadata:
  name: consignment-service
  namespace: fleetpulse
spec:
  type: ClusterIP          # internal only; the Ingress exposes it publicly
  selector: { app: consignment-service }
  ports:
    - port: 8000
      targetPort: 8000
      name: http
```

```yaml
# k8s/base/05-dispatch.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: dispatch-service
  namespace: fleetpulse
  labels: { app: dispatch-service }
spec:
  replicas: 2
  strategy:
    type: RollingUpdate
    rollingUpdate: { maxSurge: 1, maxUnavailable: 0 }
  selector:
    matchLabels: { app: dispatch-service }
  template:
    metadata:
      labels: { app: dispatch-service }
    spec:
      containers:
        - name: dispatch
          image: fleetpulse/dispatch-service:latest
          imagePullPolicy: IfNotPresent
          ports:
            - containerPort: 8000
              name: http
          envFrom:
            - configMapRef: { name: fleetpulse-config }
            - secretRef:    { name: fleetpulse-secrets }
          resources:
            requests: { memory: "96Mi", cpu: "50m" }
            limits:   { memory: "256Mi" }
          readinessProbe:
            httpGet: { path: /health, port: 8000 }
            initialDelaySeconds: 5
            periodSeconds: 5
          livenessProbe:
            httpGet: { path: /health, port: 8000 }
            initialDelaySeconds: 15
            periodSeconds: 20
---
apiVersion: v1
kind: Service
metadata:
  name: dispatch-service
  namespace: fleetpulse
spec:
  type: ClusterIP
  selector: { app: dispatch-service }
  ports:
    - port: 8000
      targetPort: 8000
      name: http
```

### 1.6 Ingress

```yaml
# k8s/base/06-ingress.yaml
# One entry point routing to both services by URL path.
# This replaces the "ports: 8001:8000 / 8002:8000" from docker-compose.
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: fleetpulse
  namespace: fleetpulse
  annotations:
    # Strip the /consignment or /dispatch prefix before forwarding, so your
    # FastAPI routes stay unchanged.
    nginx.ingress.kubernetes.io/rewrite-target: /$2
spec:
  ingressClassName: nginx
  rules:
    - host: fleetpulse.local
      http:
        paths:
          - path: /consignment(/|$)(.*)
            pathType: ImplementationSpecific
            backend:
              service:
                name: consignment-service
                port: { number: 8000 }
          - path: /dispatch(/|$)(.*)
            pathType: ImplementationSpecific
            backend:
              service:
                name: dispatch-service
                port: { number: 8000 }
```

```powershell
# After applying, with fleetpulse.local in your hosts file:
curl http://fleetpulse.local/consignment/health
curl http://fleetpulse.local/dispatch/health
# Swagger UI: http://fleetpulse.local/consignment/docs
```

### 1.7 Database migration as a Job

```yaml
# k8s/base/08-migration-job.yaml
# A Job runs to completion and stops — the right tool for schema migrations.
# Kubernetes will retry it up to backoffLimit times if it fails.
apiVersion: batch/v1
kind: Job
metadata:
  name: db-migrate
  namespace: fleetpulse
spec:
  backoffLimit: 3
  ttlSecondsAfterFinished: 300     # auto-delete 5 min after success
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: migrate
          image: postgres:16-alpine
          env:
            - name: DATABASE_URL
              valueFrom:
                secretKeyRef: { name: fleetpulse-secrets, key: DATABASE_URL }
          command: ["sh", "-c"]
          args:
            - |
              set -e
              echo "Running migrations..."
              psql "$DATABASE_URL" -f /sql/init.sql
              echo "Done."
          volumeMounts:
            - name: sql
              mountPath: /sql
      volumes:
        - name: sql
          configMap: { name: db-init-sql }
```

```powershell
# Load your existing init.sql into a ConfigMap the Job can mount
kubectl create configmap db-init-sql --namespace fleetpulse --from-file=db/init.sql
```

Because `init.sql` uses `CREATE TABLE IF NOT EXISTS`, re-running the Job is safe. Idempotent
migrations are a habit worth forming now.

### 1.8 Deploy it

```powershell
# Build images INTO minikube's Docker daemon (no registry needed)
minikube docker-env | Invoke-Expression
docker build -t fleetpulse/consignment-service:latest ./services/consignment-service
docker build -t fleetpulse/dispatch-service:latest ./services/dispatch-service

# Create the secret (never from a committed file)
kubectl create secret generic fleetpulse-secrets --namespace fleetpulse `
  --from-literal=DATABASE_URL="postgresql://fleetadmin:localpass@postgres:5432/fleetpulse"

kubectl apply -f k8s/base/

kubectl get pods -n fleetpulse -w      # Ctrl+C when everything is Running
```

### 1.9 The kubectl commands you will actually use

Learn these six and you can debug almost anything:

```powershell
# What is running, and is it healthy?
kubectl get pods -n fleetpulse
kubectl get all -n fleetpulse

# WHY is this pod not starting?  ← the most useful command in Kubernetes.
# Read the Events section at the bottom first.
kubectl describe pod <pod-name> -n fleetpulse

# What is the application saying?
kubectl logs -f deploy/consignment-service -n fleetpulse
kubectl logs <pod-name> -n fleetpulse --previous     # logs from a CRASHED container

# Get a shell inside a running container
kubectl exec -it <pod-name> -n fleetpulse -- sh

# Reach a service without an Ingress
kubectl port-forward -n fleetpulse svc/consignment-service 8001:8000

# What just happened, cluster-wide?
kubectl get events -n fleetpulse --sort-by='.lastTimestamp'
```

**Common failures and what they mean:**

| Status | Meaning | First thing to check |
|---|---|---|
| `ImagePullBackOff` | Cannot fetch the image | Did you run `minikube docker-env`? Is the tag right? |
| `CrashLoopBackOff` | Container starts then exits | `kubectl logs <pod> --previous` |
| `Pending` | Cannot be scheduled | `kubectl describe pod` → insufficient CPU/memory? |
| `0/2 Ready` | Running but readiness failing | Is `/health` actually returning 200? |
| `CreateContainerConfigError` | Missing ConfigMap or Secret | Did you create the secret? |

### 1.10 Autoscaling — and using your simulator to prove it

```yaml
# k8s/base/07-hpa.yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: consignment-service
  namespace: fleetpulse
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: consignment-service
  minReplicas: 2
  maxReplicas: 8
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 60     # % of the CPU *request*, not the node
  behavior:
    scaleUp:
      stabilizationWindowSeconds: 30    # react quickly to load
    scaleDown:
      stabilizationWindowSeconds: 300   # descend slowly, avoid flapping
```

```powershell
# Terminal 1 — watch it scale
kubectl get hpa -n fleetpulse -w

# Terminal 2 — generate load with the simulator you already wrote
python simulator/simulate_delhivery_scans.py --parcels 500 --delay 0
```

Watching replicas climb from 2 to 6 under your own load generator, then settle back down, is the
moment autoscaling stops being abstract. **HPA requires `resources.requests.cpu` to be set** — the
target percentage is measured against the request. Without it the HPA reports `<unknown>` and never
scales, which is the single most common HPA problem.

---

## 2. Helm — packaging what you understand

Only do this once the raw manifests work. Helm's value is templating one chart across environments;
without that need it is just extra syntax.

```
k8s/helm/fleetpulse/
├── Chart.yaml
├── values.yaml               # defaults
├── values-minikube.yaml      # local overrides
├── values-eks.yaml           # cloud overrides
└── templates/
    ├── _helpers.tpl
    ├── configmap.yaml
    ├── deployment.yaml       # ONE template, looped over both services
    ├── service.yaml
    ├── ingress.yaml
    └── hpa.yaml
```

```yaml
# k8s/helm/fleetpulse/values.yaml
global:
  namespace: fleetpulse
  imageRegistry: ""            # "" locally; ECR URL on EKS
  imageTag: latest

services:
  consignment:
    name: consignment-service
    replicas: 2
    port: 8000
    resources:
      requests: { memory: 96Mi, cpu: 50m }
      limits:   { memory: 256Mi }
    autoscaling: { enabled: true, minReplicas: 2, maxReplicas: 8, targetCPU: 60 }
  dispatch:
    name: dispatch-service
    replicas: 2
    port: 8000
    resources:
      requests: { memory: 96Mi, cpu: 50m }
      limits:   { memory: 256Mi }
    autoscaling: { enabled: false }

redis:
  enabled: true

ingress:
  enabled: true
  className: nginx
  host: fleetpulse.local
  annotations: {}
```

```yaml
# k8s/helm/fleetpulse/templates/deployment.yaml
# range over .Values.services generates BOTH Deployments from one template.
{{- range $key, $svc := .Values.services }}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ $svc.name }}
  namespace: {{ $.Values.global.namespace }}
  labels:
    app: {{ $svc.name }}
    app.kubernetes.io/managed-by: {{ $.Release.Service }}
spec:
  {{- if not $svc.autoscaling.enabled }}
  replicas: {{ $svc.replicas }}
  {{- end }}
  selector:
    matchLabels: { app: {{ $svc.name }} }
  template:
    metadata:
      labels: { app: {{ $svc.name }} }
      annotations:
        # Restart pods automatically when the ConfigMap changes.
        # Without this, config edits do nothing until you manually roll.
        checksum/config: {{ include (print $.Template.BasePath "/configmap.yaml") $ | sha256sum }}
    spec:
      containers:
        - name: {{ $key }}
          image: "{{ if $.Values.global.imageRegistry }}{{ $.Values.global.imageRegistry }}/{{ end }}fleetpulse/{{ $svc.name }}:{{ $.Values.global.imageTag }}"
          ports:
            - containerPort: {{ $svc.port }}
          envFrom:
            - configMapRef: { name: fleetpulse-config }
            - secretRef:    { name: fleetpulse-secrets }
          resources: {{- toYaml $svc.resources | nindent 12 }}
          readinessProbe:
            httpGet: { path: /health, port: {{ $svc.port }} }
            initialDelaySeconds: 5
            periodSeconds: 5
          livenessProbe:
            httpGet: { path: /health, port: {{ $svc.port }} }
            initialDelaySeconds: 15
            periodSeconds: 20
{{- end }}
```

```powershell
helm lint k8s/helm/fleetpulse

# See the generated YAML WITHOUT installing — do this before every install
helm template fleetpulse k8s/helm/fleetpulse -f k8s/helm/fleetpulse/values-minikube.yaml

helm upgrade --install fleetpulse k8s/helm/fleetpulse `
  --namespace fleetpulse --create-namespace `
  -f k8s/helm/fleetpulse/values-minikube.yaml

helm history fleetpulse -n fleetpulse
helm rollback fleetpulse 1 -n fleetpulse     # instant rollback — try this!
```

That `checksum/config` annotation is a genuinely useful trick: edit a ConfigMap without it and
nothing happens, because Kubernetes has no reason to restart pods. Many people lose an hour to this.

---

## 3. Track B — EKS

### 3.1 The teardown checklist (read before creating anything)

**Orphaned AWS resources are how a $0.58 session becomes a $40 month.** Deleting a cluster does
*not* delete load balancers or volumes it created — those were made by controllers, and AWS has no
idea they belonged to the cluster.

```powershell
# ALWAYS delete Kubernetes resources FIRST, then the cluster.
kubectl delete ingress --all -n fleetpulse    # removes the ALB (~$16/mo if orphaned)
kubectl delete svc --all -n fleetpulse        # removes any NLBs
kubectl delete pvc --all -n fleetpulse        # removes EBS volumes

# Wait ~60s for AWS to actually delete the load balancers, then:
eksctl delete cluster --name fleetpulse-learn --region us-east-1

# VERIFY. Do not trust, check.
aws elbv2 describe-load-balancers --query 'LoadBalancers[].LoadBalancerName'
aws ec2 describe-volumes --filters Name=status,Values=available --query 'Volumes[].VolumeId'
aws eks list-clusters
```

Put those four verification commands in a `scripts/verify-teardown.ps1` and run it every time.

### 3.2 Creating the cluster with eksctl

`eksctl` is one command and ~15 minutes, which is what makes burst sessions practical.

```yaml
# infra/eks/cluster.yaml
apiVersion: eksctl.io/v1alpha5
kind: ClusterConfig

metadata:
  name: fleetpulse-learn
  region: us-east-1
  # Omit `version` to get eksctl's default, which is always in standard
  # support. Pinning an old version silently moves you to EXTENDED support
  # at $0.60/hr instead of $0.10/hr — a 6x increase.

vpc:
  nat:
    # No NAT Gateway. Saves $33/month. Nodes sit in public subnets with
    # security groups as the boundary — fine for a learning cluster.
    gateway: Disable

managedNodeGroups:
  - name: ng-default
    # t3.small minimum — t3.micro allows only 4 pods (see §0.2).
    instanceType: t3.small
    desiredCapacity: 2
    minSize: 1
    maxSize: 4
    volumeSize: 20
    privateNetworking: false      # public subnets, since we disabled NAT
    iam:
      withAddonPolicies:
        imageBuilder: true        # ECR pull
        autoScaler: true
        cloudWatch: true
        ebs: true
        albIngress: true

# Lets pods assume IAM roles (IRSA). This is the main EKS-only concept.
iam:
  withOIDC: true

addons:
  - name: vpc-cni
  - name: coredns
  - name: kube-proxy
  - name: aws-ebs-csi-driver

cloudWatch:
  clusterLogging:
    # Keep this MINIMAL. EKS audit logs are chatty and CloudWatch charges
    # $0.50/GB ingested — easily more than the cluster itself.
    enableTypes: ["api"]
```

```powershell
winget install Weaveworks.eksctl

# ~15 minutes. Billing starts now.
eksctl create cluster -f infra/eks/cluster.yaml

kubectl get nodes
kubectl config current-context
```

### 3.3 What is genuinely different on EKS

**1. Images come from ECR, and no imagePullSecret is needed.** The node's IAM role grants ECR
access, so pulls just work:

```yaml
image: 123456789012.dkr.ecr.us-east-1.amazonaws.com/fleetpulse/consignment-service:latest
imagePullPolicy: IfNotPresent
```

**2. Ingress becomes a real ALB.** Install the AWS Load Balancer Controller:

```powershell
helm repo add eks https://aws.github.io/eks-charts
helm repo update

eksctl create iamserviceaccount `
  --cluster fleetpulse-learn --namespace kube-system `
  --name aws-load-balancer-controller `
  --attach-policy-arn arn:aws:iam::aws:policy/ElasticLoadBalancingFullAccess `
  --approve

helm install aws-load-balancer-controller eks/aws-load-balancer-controller `
  -n kube-system --set clusterName=fleetpulse-learn `
  --set serviceAccount.create=false `
  --set serviceAccount.name=aws-load-balancer-controller
```

```yaml
# k8s/overlays/eks/ingress.yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: fleetpulse
  namespace: fleetpulse
  annotations:
    alb.ingress.kubernetes.io/scheme: internet-facing
    alb.ingress.kubernetes.io/target-type: ip
    alb.ingress.kubernetes.io/healthcheck-path: /health
    # ⚠️ An ALB costs ~$16/month if you forget to delete it.
    alb.ingress.kubernetes.io/tags: Project=fleetpulse,DeleteMe=true
spec:
  ingressClassName: alb
  rules:
    - http:
        paths:
          - path: /consignment
            pathType: Prefix
            backend:
              service: { name: consignment-service, port: { number: 8000 } }
          - path: /dispatch
            pathType: Prefix
            backend:
              service: { name: dispatch-service, port: { number: 8000 } }
```

```powershell
# Takes 2-3 minutes for AWS to provision the ALB
kubectl get ingress -n fleetpulse
# ADDRESS shows the ALB DNS name — that is your public URL
```

**3. Connecting to your existing RDS.** Your RDS security group only allows the old EC2 instance.
Add the EKS nodes:

```powershell
$NODE_SG = aws eks describe-cluster --name fleetpulse-learn `
  --query 'cluster.resourcesVpcConfig.clusterSecurityGroupId' --output text

aws ec2 authorize-security-group-ingress `
  --group-id <your-rds-sg-id> --protocol tcp --port 5432 --source-group $NODE_SG
```

> **Note:** eksctl creates its *own* VPC by default, so it cannot reach an RDS instance in your
> existing VPC without peering. For learning, the simplest options are (a) run Postgres in-cluster
> for the EKS sessions, or (b) point `eksctl` at your existing VPC subnets via `vpc.subnets`.
> Option (a) is less work and teaches you StatefulSets and PVCs — which is arguably a better use of
> the session anyway.

**4. IRSA — the concept that only exists on EKS.** Give a pod an IAM role without any access keys:

```powershell
eksctl create iamserviceaccount `
  --cluster fleetpulse-learn --namespace fleetpulse `
  --name consignment-sa `
  --attach-policy-arn arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess `
  --approve
```

```yaml
spec:
  template:
    spec:
      serviceAccountName: consignment-sa   # pod now has IAM permissions
```

Understanding that a *pod* can hold an IAM identity, with no credentials on disk, is one of the
highest-value things you will learn on EKS. It comes up in interviews constantly.

### 3.4 EKS via Terraform (the portfolio artifact)

Do the eksctl version first to understand the shape, then rebuild it in Terraform — that is the
version that belongs in your repo.

```hcl
# infra/terraform/eks/main.tf
module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.13"

  name = "fleetpulse-eks-vpc"
  cidr = "10.1.0.0/16"
  azs  = ["us-east-1a", "us-east-1b"]

  public_subnets  = ["10.1.1.0/24", "10.1.2.0/24"]
  private_subnets = ["10.1.11.0/24", "10.1.12.0/24"]

  # ⚠️ The default here is `true`, which creates a NAT Gateway at $33/month.
  # We put nodes in public subnets instead.
  enable_nat_gateway = false

  # These tags are REQUIRED or the AWS Load Balancer Controller cannot
  # discover which subnets to use. Missing them is a classic silent failure.
  public_subnet_tags = { "kubernetes.io/role/elb" = "1" }
}

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.24"

  cluster_name = "fleetpulse-tf"
  # Omit cluster_version to track the module default and stay in standard support.

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.public_subnets

  cluster_endpoint_public_access           = true
  enable_cluster_creator_admin_permissions = true

  eks_managed_node_groups = {
    default = {
      instance_types = ["t3.small"]     # t3.micro = 4 pods max, unusable
      min_size       = 1
      max_size       = 4
      desired_size   = 2
      disk_size      = 20
      subnet_ids     = module.vpc.public_subnets
    }
  }

  cluster_addons = {
    vpc-cni    = {}
    coredns    = {}
    kube-proxy = {}
  }
}
```

```powershell
terraform init
terraform plan
terraform apply       # ~20 minutes

aws eks update-kubeconfig --name fleetpulse-tf --region us-east-1

# AT THE END OF EVERY SESSION — after deleting ingresses and PVCs:
terraform destroy
```

### 3.5 Cost guardrails specific to EKS

| Trap | Cost if missed | Guard |
|---|---|---|
| **Cluster left running overnight** | $3.50/night | Destroy at end of session. Set a phone alarm |
| **Orphaned ALB after cluster delete** | ~$16/mo | `kubectl delete ingress --all` **before** deleting the cluster |
| **NAT Gateway** (module default is on) | $33/mo | `enable_nat_gateway = false` |
| **Orphaned EBS from PVCs** | ~$1.60/mo each | `kubectl delete pvc --all` first, then verify |
| **Extended-support cluster version** | **6× control plane** | Do not pin an old version |
| **EKS audit logs to CloudWatch** | $15–45/mo | `enableTypes: ["api"]` only |

Add a second AWS Budget at **$10/month** with a forecast alert before your first `eksctl create`.
You already have the $1 budget from the Simple track; EKS needs its own headroom.

---

## 4. CI/CD to Kubernetes

Extends the pipeline from [FleetPulse-Simple.md §3](FleetPulse-Simple.md).

```yaml
# .github/workflows/deploy-k8s.yml (deploy job only)
  deploy-eks:
    needs: build-and-push
    if: github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: aws-actions/configure-aws-credentials@v4
        with:
          aws-access-key-id: ${{ secrets.AWS_ACCESS_KEY_ID }}
          aws-secret-access-key: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
          aws-region: us-east-1

      # Skip cleanly when the cluster does not exist — you destroy it
      # between sessions, and CI should not fail because of that.
      - name: Check cluster exists
        id: check
        run: |
          if aws eks describe-cluster --name fleetpulse-tf >/dev/null 2>&1; then
            echo "exists=true" >> $GITHUB_OUTPUT
          else
            echo "exists=false" >> $GITHUB_OUTPUT
            echo "::notice::Cluster not running — skipping deploy."
          fi

      - name: Deploy
        if: steps.check.outputs.exists == 'true'
        run: |
          aws eks update-kubeconfig --name fleetpulse-tf --region us-east-1
          helm upgrade --install fleetpulse k8s/helm/fleetpulse \
            --namespace fleetpulse --create-namespace \
            -f k8s/helm/fleetpulse/values-eks.yaml \
            --set global.imageTag=${{ github.sha }} \
            --wait --timeout 5m
          kubectl rollout status deploy/consignment-service -n fleetpulse
```

`--wait --timeout 5m` makes Helm block until pods are actually Ready, so a broken deploy fails the
pipeline instead of reporting success while pods crash-loop.

---

## 5. Roadmap

Four milestones continuing from Simple's Milestone 4. Roughly 3–4 weeks.

### Milestone 5 — minikube fundamentals (Week 1)

- [ ] Install minikube, kubectl, Helm; start a cluster; enable `ingress` and `metrics-server`
- [ ] Write `00-namespace` through `05-dispatch` by hand — **type them, do not paste**
- [ ] Build images into minikube's daemon; deploy; reach `/health` via `port-forward`
- [ ] Deliberately break something (wrong image tag) and diagnose it with `describe` + `logs`
- [ ] Add the Ingress; reach both services through `fleetpulse.local`
- [ ] Run the migration Job against an in-cluster Postgres

> ✅ **Checkpoint:** `simulate_delhivery_scans.py` pointed at `fleetpulse.local` runs end to end.
> 🎉 You have deployed a real multi-service app to Kubernetes.

### Milestone 6 — Operating it (Week 2)

- [ ] Add readiness and liveness probes; explain the difference out loud
- [ ] Set resource requests/limits on everything
- [ ] Do a rolling update: change code, rebuild, `kubectl set image`, watch pods cycle
- [ ] `kubectl rollout undo` — practise rolling back
- [ ] Add the HPA; drive it with the simulator; **watch replicas climb and fall**
- [ ] Add a PodDisruptionBudget; drain a node with `kubectl drain`
- [ ] Learn `kubectl describe`, `events`, `top pods` properly

> ✅ **Checkpoint:** you can push a bad image, watch the rollout stall, and roll back — without
> looking anything up.

### Milestone 7 — Helm (Week 3)

- [ ] Convert the manifests to a chart with one templated Deployment
- [ ] `values-minikube.yaml` and `values-eks.yaml`
- [ ] `helm template` to inspect output before installing
- [ ] Add the `checksum/config` annotation; prove a ConfigMap edit restarts pods
- [ ] `helm rollback` to a previous revision

> ✅ **Checkpoint:** one chart deploys to minikube with `-f values-minikube.yaml`, and you can
> explain every templated field.

### Milestone 8 — EKS (Week 4, in short sessions)

- [ ] **Create a $10 AWS Budget with a forecast alert — first**
- [ ] Write `scripts/verify-teardown.ps1` **before** creating the cluster
- [ ] `eksctl create cluster`; deploy the Helm chart; delete the cluster the same day
- [ ] Session 2: AWS Load Balancer Controller + ALB Ingress; reach it publicly; **tear down**
- [ ] Session 3: IRSA — give a pod an IAM role, verify with `aws sts get-caller-identity` in-pod
- [ ] Session 4: rebuild the cluster in Terraform; `apply`, deploy, `destroy`
- [ ] Session 5: CI/CD deploying to EKS automatically
- [ ] Check Cost Explorer the next day — confirm you actually spent ~$5, not ~$50

> ✅ **Checkpoint:** you can create an EKS cluster, deploy to it, and destroy it cleanly with no
> orphaned resources. **The teardown is the skill**, not the creation.

---

## 6. Interview answers this unlocks

**"What's the difference between readiness and liveness probes?"**
> "Readiness controls whether the pod receives traffic — it's removed from the Service endpoints but
> keeps running. Liveness restarts the container. The important part is that liveness should never
> check dependencies like the database: if the DB blips, every pod fails liveness at once and you
> turn a small problem into a full outage. Readiness can check dependencies; liveness should only
> ask whether the process is deadlocked."

**"How do you give a pod AWS permissions?"**
> "IRSA on EKS — the cluster gets an OIDC provider, and a service account is annotated with an IAM
> role ARN. The pod gets temporary credentials projected in as a token, so there are no access keys
> on disk or in the image. The trust policy has to pin both the namespace and the service account
> name; if you only pin the audience, any service account in the cluster can assume the role."

**"Why did you use minikube and EKS rather than just one?"**
> "Cost and iteration speed. Most Kubernetes concepts are identical everywhere, so I learned
> Deployments, probes, Helm, and HPA locally for free with a 30-second feedback loop. I used EKS only
> for things that genuinely don't exist locally — IRSA, the ALB controller, VPC CNI pod density —
> and I destroyed the cluster after every session. That kept EKS to about $5/month instead of $107."

**"What's the most surprising thing you hit?"**
> "That `t3.micro` is unusable on EKS. It's not a memory limit — the VPC CNI gives every pod a real
> VPC IP, and `t3.micro` supports only 4 pods total. `kube-system` uses all four, so there's no room
> for your app. I moved to `t3.small` at 11 pods."

That last one is a great answer because it is specific, it is true, and it shows you actually ran
the thing rather than following a tutorial.

---

## 7. Where things live now

| Environment | What runs there | Cost | Purpose |
|---|---|---|---|
| **Docker Compose (local)** | Everything | $0 | Fast day-to-day development |
| **minikube (local)** | Full stack on Kubernetes | $0 | Kubernetes learning, unlimited |
| **EC2 + Compose (AWS)** | 2 services + Redis | $0 free tier | Always-on demo you can show anyone |
| **EKS (AWS)** | Full stack + ALB + IRSA | ~$5/mo burst | Cloud-Kubernetes skills |

Keep all four. They serve different purposes, and being able to explain why you have each one is
itself the architectural judgement that interviews are testing.
