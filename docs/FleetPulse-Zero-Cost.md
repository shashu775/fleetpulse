# FleetPulse Zero-Cost Track

A student-budget build path for [FleetPulse](FleetPulse-Blueprint.md) that stays inside the AWS Free
Tier while still producing something you can defend in a DevOps interview.

This is a **different architecture**, not a discount version of the
[production blueprint](FleetPulse-Blueprint.md). Where the two diverge, the divergence is
deliberate and labelled.

---

## Read this before you provision anything

Three facts that determine whether this plan works for you. None of them are optional reading.

### ⚠️ 1. Your AWS Free Tier may not be the one this plan assumes

AWS restructured the Free Tier in **mid-2025**. Which model your account gets depends entirely on
when you created it:

| | Classic model (accounts before ~July 2025) | Credit model (newer accounts) |
|---|---|---|
| What you get | 12 months of per-service allowances (750 hrs EC2, 750 hrs RDS, etc.) | ~$100 signup credit + up to $100 more from onboarding activities |
| Duration | 12 months | ~6 months or until credits are spent |
| After expiry | Everything bills at list price | Everything bills at list price |

**This plan is written for the classic model.** Under the credit model, "free" means *spending
credits* — this architecture burns roughly $25–30/month of them, so ~$100–200 buys you 4–6 months.
That is still enough to complete the whole roadmap, but you must plan for the end date.

**Do this first:** open the AWS Billing console → **Free Tier** page. It will show either
per-service usage allowances or a credit balance. Everything below assumes you have checked.

Also worth pursuing as a student: the **GitHub Student Developer Pack** and **AWS Educate** both
provide credits, and are stackable with the above.

### ⚠️ 2. A 1 GB instance cannot run the full stack. Here is the arithmetic.

You asked for `t2.micro`/`t3.micro`. Both are **1024 MB, ~958 MB usable**. The honest budget:

```
Amazon Linux 2023 base OS ............  ~150 MB
K3s server (SQLite datastore) ........  ~500 MB
  ├─ Traefik ingress (bundled) .......   ~50 MB   ← disableable
  └─ metrics-server ..................   ~30 MB   ← keep, needed for HPA
SSM agent ............................   ~40 MB
─────────────────────────────────────────────────
Consumed before your first pod .......  ~770 MB
Remaining for workload ...............  ~190 MB
```

Against that ~190 MB, the blueprint's stack wants:

| Component | RAM | Verdict |
|---|---|---|
| 4 × Go services @ ~35 MB | 140 MB | Tight but feasible |
| RabbitMQ (Erlang VM) | ~150 MB | ❌ Does not fit |
| Redis | ~40 MB | Marginal |
| Prometheus (realistic minimum) | ~250 MB | ❌ Does not fit |
| Grafana | ~130 MB | ❌ Does not fit |
| **Total demand** | **~710 MB** | **3.7× over budget** |

Anyone telling you to "just run Prometheus and Grafana pods on a t3.micro" has not done this
arithmetic. You will get `OOMKilled` pods and a node that goes `NotReady` under load, and you will
lose days to it.

**Four changes make it fit.** Each is a real engineering trade, explained in §1.2:

1. **2 GB swap file** (free — comes out of your 30 GB EBS allowance)
2. **NATS JetStream instead of RabbitMQ** — 30 MB instead of 150 MB
3. **Grafana Cloud free tier + Alloy agent** — 80 MB instead of 380 MB
4. **RDS holds Postgres** — which is why using managed RDS here *saves* memory rather than costing it

Revised budget: **~960 MB demand against 958 MB + 2 GB swap.** It fits, and it runs — slowly under
burst, which is fine for a learning system.

### ⚠️ 3. Do the depth locally; use AWS for the thin slice that must be real

Your laptop has 8–16 GB of RAM. AWS is giving you 1 GB. **Do not fight that ratio.**

| Do this **locally** (kind/k3d, free, fast) | Do this **on AWS** (proves you can wire the real thing) |
|---|---|
| All 4 microservices | Terraform: VPC, subnets, SGs, EC2, RDS, ECR |
| Full RabbitMQ, Redis, Prometheus, Grafana, Jaeger | K3s bootstrap via user-data |
| Chaos experiments, HPA/KEDA testing | GitHub Actions → ECR → automated deploy |
| Load testing with the simulator | IAM roles, OIDC federation, SSM access |
| Iterating on Helm charts | 2–3 services running end to end, RDS-backed |

An interviewer will not be impressed that you crammed Prometheus onto a t3.micro. They will be
impressed that you know **why you didn't**, and that you can explain the memory budget above from
memory. Constraint-reasoning is the skill being tested.

---

## 1. Cost-Optimized AWS Architecture

### 1.1 Topology

```mermaid
flowchart TB
    subgraph GH["GitHub"]
        A[GitHub Actions<br/>build · test · push]
    end

    subgraph AWS["AWS — single region, us-east-1"]
        ECR[(ECR<br/>500 MB free tier)]

        subgraph VPC["VPC 10.0.0.0/16"]
            subgraph AZ1["us-east-1a"]
                SN1["Public Subnet 10.0.1.0/24"]
                EC2["EC2 t3.micro<br/>K3s server<br/>+ Elastic IP"]
            end
            subgraph AZ2["us-east-1b"]
                SN2["Public Subnet 10.0.2.0/24<br/>(RDS subnet group only)"]
            end
            RDS[("RDS PostgreSQL<br/>db.t3.micro · 20 GB<br/>publicly_accessible = false")]
            IGW["Internet Gateway<br/>(no NAT — $0)"]
        end
    end

    GC["Grafana Cloud<br/>free tier"]

    A -->|OIDC, no static keys| ECR
    A -->|SSM Run Command| EC2
    EC2 -->|pull image| ECR
    EC2 -->|:5432, SG-to-SG| RDS
    EC2 -->|remote_write via Alloy| GC
    IGW <--> EC2
```

**Why there is no NAT Gateway.** A NAT Gateway exists so instances in *private* subnets can reach
the internet. Put the instance in a **public subnet with a public IP**, and it routes outbound
through the Internet Gateway — which is free. The security boundary moves from "network placement"
to "security group rules," which is a perfectly defensible posture for a single-node learning
cluster as long as the SG is tight (§4.2).

**Note the distinction that trips people up:** the RDS instance sits in a *public subnet* but has
`publicly_accessible = false`. It therefore gets **no public IP** and is unreachable from the
internet. "Public subnet" describes the route table; "publicly accessible" describes whether AWS
assigns a public endpoint. They are independent.

### 1.2 The four deviations from the production blueprint

| # | Blueprint | Zero-cost track | Saves | What you give up |
|---|---|---|---|---|
| 1 | EKS | **K3s on one EC2** | $73/mo | Managed control plane, IRSA, Karpenter, multi-node scheduling |
| 2 | RabbitMQ / Amazon MQ | **NATS JetStream** | 120 MB + $14/mo | AMQP semantics, the RabbitMQ management UI, DLX ladder syntax |
| 3 | Self-hosted Prometheus + Grafana | **Grafana Alloy → Grafana Cloud free** | 300 MB | Full control of retention; capped at 10k series / 14 days |
| 4 | Multi-AZ everything | **Single AZ, single node** | ~$400/mo | All HA. A node failure is a total outage |

**On deviation 2 — this is the one to think hardest about.** [CLAUDE.md](../CLAUDE.md) records
RabbitMQ as a settled decision, and in the [blueprint §0.1](FleetPulse-Blueprint.md) I argued
strongly against swapping brokers between local and cloud because it destroys local/prod parity.
That argument still holds. So:

> **Recommendation: switch to NATS JetStream *everywhere*, including your local Compose stack — or
> keep RabbitMQ everywhere and accept that the EC2 box runs only 2 services at a time.** Do not run
> RabbitMQ locally and NATS on AWS. Parity is worth more than either broker.

NATS JetStream genuinely gives you what this project needs: durable streams, consumer groups, acks,
redelivery, dead-letter via `max_deliver`, and a built-in KV store that **replaces Redis for your
dedupe keys** (saving another 40 MB). It is a single ~15 MB Go binary. The outbox pattern, the
idempotent-consumer pattern, and the trace-propagation work from the blueprint all transfer
unchanged — only the client library differs.

What you lose is AMQP-specific vocabulary — exchanges, bindings, DLX. That vocabulary does come up
in interviews. If RabbitMQ specifically matters to your target roles, keep it locally where RAM is
free and demonstrate it there.

### 1.3 What each AWS service costs you

| Service | Configuration | Free Tier allowance | Cost if you stay inside |
|---|---|---|---|
| EC2 | 1 × `t3.micro`, 750 hrs/mo | 750 hrs/mo (12 mo) | **$0.00** |
| Public IPv4 | 1 Elastic IP, attached | 750 hrs/mo (12 mo) | **$0.00** |
| EBS | 30 GB `gp3` (root + swap) | 30 GB (12 mo) | **$0.00** |
| RDS | `db.t3.micro`, 20 GB, single-AZ | 750 hrs + 20 GB + 20 GB backup (12 mo) | **$0.00** |
| ECR | ≤ 500 MB private | 500 MB (12 mo) | **$0.00** |
| VPC / IGW / SG / route tables | — | Always free | **$0.00** |
| Data egress | < 100 GB/mo | 100 GB/mo always free | **$0.00** |
| SSM Session Manager + Run Command | — | Always free | **$0.00** |
| CloudWatch alarms | ≤ 10 | 10 always free | **$0.00** |
| AWS Budgets | 2 budgets | 2 always free | **$0.00** |
| Grafana Cloud | 10k series, 14-day retention | Free forever | **$0.00** |
| GitHub Actions | Public repo | Unlimited on public repos | **$0.00** |
| **Total** | | | **$0.00/mo** |

**The four ways this silently starts costing money** — each has a guardrail in §6:

1. **T3 "unlimited" CPU credits.** `t3.micro` defaults to *unlimited* mode, which bills
   **$0.05/vCPU-hour** for sustained CPU above the baseline. A busy K3s node will absolutely exceed
   baseline. **Set `cpu_credits = "standard"` in Terraform** (§4.3) so it throttles instead of
   billing. `t2.micro` defaults to standard and does not have this problem.
2. **A second public IPv4.** The free tier covers *750 hours*, which is one address running
   continuously. A second EIP, or an unattached one, bills at **$0.005/hr = $3.60/mo**.
3. **ECR creep.** Every CI run pushes an image. 500 MB fills in weeks without a lifecycle policy.
4. **RDS storage autoscaling.** Leave it enabled and a runaway migration grows you past 20 GB
   silently. Disable it.

---

## 2. Phase 1 — Local Setup & K3s Bootstrap

### 2.1 Local Docker Compose

Locally you have RAM, so run the **full** stack — this is where you do the real learning. Use
profiles so you can bring up only what you need.

```yaml
# infra/docker/docker-compose.yml
name: fleetpulse

x-svc: &svc
  restart: unless-stopped
  environment: &env
    NATS_URL: nats://nats:4222
    LOG_LEVEL: debug
    OTEL_EXPORTER_OTLP_ENDPOINT: http://otel-collector:4317
  depends_on:
    postgres: { condition: service_healthy }
    nats:     { condition: service_healthy }

services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    ports: ["5432:5432"]
    volumes:
      # Strip the UTF-8 BOM from this file first or psql fails on line 1.
      - ./postgres-init:/docker-entrypoint-initdb.d:ro
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER}"]
      interval: 5s
      retries: 10

  nats:
    image: nats:2.10-alpine
    command: ["-js", "-sd", "/data", "-m", "8222"]   # JetStream + monitoring endpoint
    ports: ["4222:4222", "8222:8222"]
    volumes: ["natsdata:/data"]
    healthcheck:
      test: ["CMD", "wget", "-qO-", "http://localhost:8222/healthz"]
      interval: 5s
      retries: 10

  consignment-service:
    <<: *svc
    build: ../../services/consignment-service
    environment:
      <<: *env
      OTEL_SERVICE_NAME: consignment-service
      DATABASE_URL: postgres://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/consignment_db?sslmode=disable
    ports: ["8081:8080"]

  # facility (8082), dispatch (8083), notification (8084) follow the same shape.

  webhook-sink:
    image: mendhak/http-https-echo:34
    environment: { HTTP_PORT: 8080 }
    ports: ["8090:8080"]

  # ---- observability profile: heavy, local only, never on the EC2 box ----
  prometheus:
    profiles: ["obs"]
    image: prom/prometheus:latest
    volumes: ["./prometheus:/etc/prometheus:ro"]
    ports: ["9090:9090"]

  grafana:
    profiles: ["obs"]
    image: grafana/grafana:latest
    environment: { GF_AUTH_ANONYMOUS_ENABLED: "true", GF_AUTH_ANONYMOUS_ORG_ROLE: Admin }
    volumes: ["./grafana/provisioning:/etc/grafana/provisioning:ro"]
    ports: ["3000:3000"]

  jaeger:
    profiles: ["obs"]
    image: jaegertracing/all-in-one:latest
    ports: ["16686:16686"]

volumes: { pgdata: {}, natsdata: {} }
```

```bash
docker compose up -d                 # app only  (~600 MB)
docker compose --profile obs up -d   # + observability (~1.5 GB) — laptop only
```

### 2.2 Dockerfile — small images are a hard requirement here

Your ECR allowance is 500 MB total. A distroless Go binary is ~20 MB; a naive `golang:1.23` runtime
image is ~900 MB and blows the entire budget with one push.

```dockerfile
# services/consignment-service/Dockerfile
FROM golang:1.23-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/server ./cmd/server

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/server /server
USER nonroot:nonroot
EXPOSE 8080
ENTRYPOINT ["/server"]
```

**Build `linux/amd64` only.** `t3.micro` is x86; multi-arch would double your ECR consumption for
an architecture you are not running.

### 2.3 K3s bootstrap via EC2 user-data

This runs automatically at first boot (wired up in §4.3). It is idempotent enough to re-run.

```bash
#!/bin/bash
# infra/terraform/modules/ec2/user_data.sh
set -euxo pipefail
exec > >(tee /var/log/user-data.log) 2>&1

# ---------------------------------------------------------------
# 1. Swap — MANDATORY on 1 GB. Without this K3s OOMs under load.
# ---------------------------------------------------------------
if [ ! -f /swapfile ]; then
  dd if=/dev/zero of=/swapfile bs=1M count=2048 status=progress
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
# Prefer RAM; only swap under genuine pressure.
sysctl -w vm.swappiness=10
echo 'vm.swappiness=10' > /etc/sysctl.d/99-swap.conf

dnf update -y
dnf install -y git jq tar

# ---------------------------------------------------------------
# 2. K3s — trimmed for a 1 GB box
#    --disable traefik     : ~50 MB; we use NodePort instead
#    --disable servicelb   : ~10 MB; no LoadBalancer Services on one node
#    --write-kubeconfig-mode 644 : lets ec2-user run kubectl
#    fail-swap-on=false    : REQUIRED, kubelet refuses to start with swap on
# ---------------------------------------------------------------
curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="\
  --disable traefik \
  --disable servicelb \
  --write-kubeconfig-mode 644 \
  --kubelet-arg=fail-swap-on=false \
  --kubelet-arg=eviction-hard=memory.available<100Mi \
  --kubelet-arg=system-reserved=memory=200Mi \
" sh -

until kubectl get nodes 2>/dev/null | grep -q ' Ready '; do sleep 5; done

mkdir -p /home/ec2-user/.kube
cp /etc/rancher/k3s/k3s.yaml /home/ec2-user/.kube/config
chown -R ec2-user:ec2-user /home/ec2-user/.kube
echo 'export KUBECONFIG=/home/ec2-user/.kube/config' >> /home/ec2-user/.bashrc

# ---------------------------------------------------------------
# 3. Helm + namespace
# ---------------------------------------------------------------
curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
kubectl create namespace fleetpulse --dry-run=client -o yaml | kubectl apply -f -

# ---------------------------------------------------------------
# 4. NATS JetStream — 30 MB, replaces RabbitMQ + Redis on this box
# ---------------------------------------------------------------
helm repo add nats https://nats-io.github.io/k8s/helm/charts/
helm repo update
helm upgrade --install nats nats/nats -n fleetpulse \
  --set config.jetstream.enabled=true \
  --set config.jetstream.fileStore.pvc.size=1Gi \
  --set config.cluster.enabled=false \
  --set container.merge.resources.requests.memory=32Mi \
  --set container.merge.resources.limits.memory=96Mi

# ---------------------------------------------------------------
# 5. ECR credential refresh — ECR tokens expire every 12 hours.
#    Without this cron, image pulls start failing overnight.
# ---------------------------------------------------------------
cat > /usr/local/bin/refresh-ecr-secret.sh <<'SCRIPT'
#!/bin/bash
set -euo pipefail
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
REGION="$(curl -s http://169.254.169.254/latest/meta-data/placement/region)"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
kubectl create secret docker-registry ecr-creds -n fleetpulse \
  --docker-server="${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com" \
  --docker-username=AWS \
  --docker-password="$(aws ecr get-login-password --region "${REGION}")" \
  --dry-run=client -o yaml | kubectl apply -f -
SCRIPT
chmod +x /usr/local/bin/refresh-ecr-secret.sh
/usr/local/bin/refresh-ecr-secret.sh
echo "0 */6 * * * root /usr/local/bin/refresh-ecr-secret.sh" > /etc/cron.d/ecr-refresh
```

That ECR credential cron is the detail people miss. **ECR authorization tokens are valid for 12
hours.** Deploy on Monday, come back Wednesday, and every pod is in `ImagePullBackOff` with an
opaque auth error. Refreshing every 6 hours removes the whole class of problem.

Two kubelet flags also matter more than they look: `system-reserved=memory=200Mi` stops the kubelet
scheduling pods into memory the OS needs, and `eviction-hard=memory.available<100Mi` makes the
kubelet evict a pod rather than let the Linux OOM killer take down `k3s` itself. On a 1 GB box that
is the difference between "a pod restarted" and "the cluster is gone."

### 2.4 Verify the box

```bash
aws ssm start-session --target "$INSTANCE_ID"     # no SSH key needed

sudo -i
kubectl get nodes -o wide
kubectl top node                                   # needs metrics-server, ~30s to populate
free -h                                            # confirm swap is active
kubectl get pods -A
```

---

## 3. Phase 2 — Free CI/CD

### 3.1 GitHub OIDC — no long-lived AWS keys

Do **not** put `AWS_ACCESS_KEY_ID` in GitHub Secrets. Federate with OIDC instead: GitHub presents a
short-lived token, AWS exchanges it for temporary credentials, and there is no static secret to leak
or rotate. It is free, and it is the answer interviewers want when they ask how you authenticate CI
to a cloud account.

```hcl
# infra/terraform/modules/iam-github/main.tf
data "aws_caller_identity" "current" {}

resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

data "aws_iam_policy_document" "trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    # Pin to YOUR repo. Without this, any GitHub repo on earth can assume this role.
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_owner}/${var.github_repo}:*"]
    }
  }
}

resource "aws_iam_role" "ci" {
  name               = "fleetpulse-github-ci"
  assume_role_policy = data.aws_iam_policy_document.trust.json
}

data "aws_iam_policy_document" "ci" {
  statement {
    sid       = "ECRAuth"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]                       # this action does not support resource scoping
  }
  statement {
    sid = "ECRPush"
    actions = [
      "ecr:BatchCheckLayerAvailability", "ecr:CompleteLayerUpload",
      "ecr:InitiateLayerUpload", "ecr:PutImage", "ecr:UploadLayerPart",
      "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer",
    ]
    resources = var.ecr_repository_arns
  }
  statement {
    sid       = "DeployViaSSM"
    actions   = ["ssm:SendCommand", "ssm:GetCommandInvocation", "ssm:ListCommandInvocations"]
    resources = [
      var.instance_arn,
      "arn:aws:ssm:${var.region}::document/AWS-RunShellScript",
    ]
  }
}

resource "aws_iam_role_policy" "ci" {
  role   = aws_iam_role.ci.id
  policy = data.aws_iam_policy_document.ci.json
}

output "role_arn" { value = aws_iam_role.ci.arn }
```

The `sub` condition is the one that matters. Omit it and you have granted every GitHub Actions
workflow in the world permission to push to your ECR and run shell commands on your instance.

### 3.2 Build, test, push

```yaml
# .github/workflows/ci-cd.yml
name: ci-cd

on:
  push:    { branches: [main] }
  pull_request:

permissions:
  contents: read
  id-token: write          # required for OIDC

env:
  AWS_REGION: us-east-1

jobs:
  changes:
    runs-on: ubuntu-latest
    outputs:
      services: ${{ steps.filter.outputs.changes }}
    steps:
      - uses: actions/checkout@v4
      - uses: dorny/paths-filter@v3
        id: filter
        with:
          filters: |
            consignment:  ['services/consignment-service/**', 'pkg/**']
            facility:     ['services/facility-service/**',    'pkg/**']
            dispatch:     ['services/dispatch-service/**',     'pkg/**']
            notification: ['services/notification-service/**', 'pkg/**']

  test:
    needs: changes
    if: needs.changes.outputs.services != '[]'
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        service: ${{ fromJSON(needs.changes.outputs.services) }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-go@v5
        with:
          go-version: '1.23'
          cache-dependency-path: services/${{ matrix.service }}-service/go.sum
      - uses: golangci/golangci-lint-action@v6
        with: { working-directory: services/${{ matrix.service }}-service }
      - name: test
        working-directory: services/${{ matrix.service }}-service
        run: go test -race ./...

  build-push:
    needs: [changes, test]
    if: github.ref == 'refs/heads/main' && needs.changes.outputs.services != '[]'
    runs-on: ubuntu-latest
    strategy:
      matrix:
        service: ${{ fromJSON(needs.changes.outputs.services) }}
    steps:
      - uses: actions/checkout@v4

      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_CI_ROLE_ARN }}   # ARN is not a secret, but keep it tidy
          aws-region: ${{ env.AWS_REGION }}

      - uses: aws-actions/amazon-ecr-login@v2
        id: ecr

      - uses: docker/setup-buildx-action@v3

      - uses: docker/build-push-action@v6
        with:
          context: services/${{ matrix.service }}-service
          platforms: linux/amd64            # single arch — protects the 500 MB ECR budget
          push: true
          tags: |
            ${{ steps.ecr.outputs.registry }}/fleetpulse/${{ matrix.service }}-service:${{ github.sha }}
            ${{ steps.ecr.outputs.registry }}/fleetpulse/${{ matrix.service }}-service:latest
          cache-from: type=gha
          cache-to: type=gha,mode=max

  deploy:
    needs: [changes, build-push]
    runs-on: ubuntu-latest
    steps:
      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_CI_ROLE_ARN }}
          aws-region: ${{ env.AWS_REGION }}

      - name: Roll services via SSM
        env:
          SERVICES: ${{ needs.changes.outputs.services }}
          SHA: ${{ github.sha }}
        run: |
          set -euo pipefail
          ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
          REGISTRY="${ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com"

          CMDS='export KUBECONFIG=/etc/rancher/k3s/k3s.yaml'
          CMDS="$CMDS && /usr/local/bin/refresh-ecr-secret.sh"
          for svc in $(echo "$SERVICES" | jq -r '.[]'); do
            CMDS="$CMDS && kubectl -n fleetpulse set image deploy/${svc}-service \
              ${svc}-service=${REGISTRY}/fleetpulse/${svc}-service:${SHA}"
            CMDS="$CMDS && kubectl -n fleetpulse rollout status deploy/${svc}-service --timeout=180s"
          done

          CMD_ID=$(aws ssm send-command \
            --instance-ids "${{ secrets.EC2_INSTANCE_ID }}" \
            --document-name "AWS-RunShellScript" \
            --comment "deploy ${SHA:0:7}" \
            --parameters "commands=[\"$CMDS\"]" \
            --query 'Command.CommandId' --output text)

          # Poll to completion so a failed rollout fails the workflow.
          for _ in $(seq 1 40); do
            STATUS=$(aws ssm get-command-invocation \
              --command-id "$CMD_ID" --instance-id "${{ secrets.EC2_INSTANCE_ID }}" \
              --query 'Status' --output text 2>/dev/null || echo Pending)
            case "$STATUS" in
              Success) echo "✅ deployed"; exit 0 ;;
              Failed|Cancelled|TimedOut)
                aws ssm get-command-invocation --command-id "$CMD_ID" \
                  --instance-id "${{ secrets.EC2_INSTANCE_ID }}" \
                  --query 'StandardErrorContent' --output text
                exit 1 ;;
            esac
            sleep 15
          done
          echo "timed out waiting for SSM"; exit 1
```

**Why SSM Run Command rather than SSH.** No private key in GitHub Secrets, no port 22 open to
GitHub's IP ranges, every command logged in CloudTrail, and it costs nothing. If you prefer SSH,
the pattern is `appleboy/ssh-action` with a deploy key — but you have then reintroduced a long-lived
secret you must rotate, which is exactly what OIDC was for.

**On GitOps.** True pull-based GitOps needs Argo CD (~300 MB) or Flux (~150 MB), and neither fits
alongside everything else. This pipeline is **push-based CD**, which is honest — call it that in an
interview rather than overclaiming. Run Argo CD on your **local kind cluster** to learn the pull
model properly; the concepts (declarative desired state, reconciliation, drift detection, `selfHeal`)
are what you are being asked about, and a local demo shows them just as well.

### 3.3 ECR lifecycle policy — set this on day one

```hcl
resource "aws_ecr_lifecycle_policy" "keep_recent" {
  for_each   = aws_ecr_repository.svc
  repository = each.value.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Expire untagged after 1 day"
        selection    = { tagStatus = "untagged", countType = "sinceImagePushed",
                         countUnit = "days", countNumber = 1 }
        action       = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep only the 3 most recent tagged images"
        selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 3 }
        action       = { type = "expire" }
      },
    ]
  })
}
```

4 services × 3 images × ~25 MB ≈ **300 MB**, comfortably inside the 500 MB allowance. Without this
policy you will cross it in about three weeks of active development.

---

## 4. Phase 3 — Terraform

### 4.1 Layout and root module

```
infra/terraform/
├── modules/
│   ├── network/      # VPC, 2 public subnets, IGW, route table
│   ├── security/     # security groups
│   ├── ec2/          # instance, EIP, IAM instance profile, user_data.sh
│   ├── rds/          # subnet group, parameter group, db instance
│   ├── ecr/          # 4 repositories + lifecycle policies
│   ├── iam-github/   # OIDC provider + CI role
│   └── budget/       # AWS Budgets + CloudWatch billing alarm
└── environments/free/
    ├── main.tf
    ├── variables.tf
    ├── outputs.tf
    └── terraform.tfvars
```

```hcl
# infra/terraform/environments/free/main.tf
terraform {
  required_version = ">= 1.9"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.60" }
  }
  # Local state to start. Move to S3 when you are comfortable — an S3 bucket
  # for state is well inside the 5 GB free allowance.
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Project   = "fleetpulse"
      Env       = "free"
      ManagedBy = "terraform"
    }
  }
}

locals { name = "fleetpulse-free" }

module "network" {
  source = "../../modules/network"
  name   = local.name
  cidr   = "10.0.0.0/16"
  azs    = ["${var.region}a", "${var.region}b"]
}

module "security" {
  source        = "../../modules/security"
  name          = local.name
  vpc_id        = module.network.vpc_id
  my_ip_cidr    = var.my_ip_cidr        # e.g. "203.0.113.42/32"
}

module "ecr" {
  source = "../../modules/ecr"
  name   = local.name
  services = ["consignment", "facility", "dispatch", "notification"]
}

module "ec2" {
  source            = "../../modules/ec2"
  name              = local.name
  subnet_id         = module.network.public_subnet_ids[0]
  security_group_id = module.security.node_sg_id
  instance_type     = var.instance_type    # "t3.micro"
}

module "rds" {
  source            = "../../modules/rds"
  name              = local.name
  subnet_ids        = module.network.public_subnet_ids
  security_group_id = module.security.rds_sg_id
  db_password       = var.db_password       # from TF_VAR_db_password, never committed
}

module "iam_github" {
  source              = "../../modules/iam-github"
  region              = var.region
  github_owner        = var.github_owner
  github_repo         = var.github_repo
  ecr_repository_arns = module.ecr.repository_arns
  instance_arn        = module.ec2.instance_arn
}

module "budget" {
  source        = "../../modules/budget"
  name          = local.name
  alert_email   = var.alert_email
  limit_usd     = "1.0"
}
```

### 4.2 Network and security modules

```hcl
# infra/terraform/modules/network/main.tf
resource "aws_vpc" "this" {
  cidr_block           = var.cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = var.name }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = var.name }
}

# Two subnets in two AZs: EC2 lives in [0]; RDS needs a subnet group
# spanning >= 2 AZs even for a single-AZ instance.
resource "aws_subnet" "public" {
  count                   = length(var.azs)
  vpc_id                  = aws_vpc.this.id
  cidr_block              = cidrsubnet(var.cidr, 8, count.index + 1)
  availability_zone       = var.azs[count.index]
  map_public_ip_on_launch = true
  tags                    = { Name = "${var.name}-public-${count.index + 1}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id   # free egress — no NAT Gateway
  }
  tags = { Name = "${var.name}-public" }
}

resource "aws_route_table_association" "public" {
  count          = length(aws_subnet.public)
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

output "vpc_id"            { value = aws_vpc.this.id }
output "public_subnet_ids" { value = aws_subnet.public[*].id }
```

```hcl
# infra/terraform/modules/security/main.tf
resource "aws_security_group" "node" {
  name        = "${var.name}-node"
  description = "K3s node"
  vpc_id      = var.vpc_id

  # SSH restricted to YOUR IP only. Prefer SSM Session Manager and you can
  # delete this rule entirely — SSM needs no inbound ports at all.
  ingress {
    description = "SSH from my IP"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.my_ip_cidr]
  }

  ingress {
    description = "HTTP"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "HTTPS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # NodePort range — restricted to your IP, NOT the world.
  # 30000-32767 open to 0.0.0.0/0 exposes every service you ever deploy.
  ingress {
    description = "NodePort from my IP"
    from_port   = 30000
    to_port     = 32767
    protocol    = "tcp"
    cidr_blocks = [var.my_ip_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.name}-node" }
}

resource "aws_security_group" "rds" {
  name        = "${var.name}-rds"
  description = "Postgres — reachable only from the K3s node"
  vpc_id      = var.vpc_id
  tags        = { Name = "${var.name}-rds" }
}

# SG-to-SG reference, not a CIDR. The rule follows the instance even if
# its IP changes, and nothing else in the VPC can reach the database.
resource "aws_vpc_security_group_ingress_rule" "rds_from_node" {
  security_group_id            = aws_security_group.rds.id
  referenced_security_group_id = aws_security_group.node.id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
  description                  = "Postgres from K3s node"
}

output "node_sg_id" { value = aws_security_group.node.id }
output "rds_sg_id"  { value = aws_security_group.rds.id }
```

Two deliberate hardening choices worth being able to explain:

- **NodePort is restricted to `var.my_ip_cidr`, not `0.0.0.0/0`.** Opening 30000–32767 to the world
  publishes every service you will ever deploy, including debug endpoints and the NATS monitoring
  port. Interviewers notice this.
- **The RDS rule references the node's security group**, not a CIDR block. The database is reachable
  only by that instance, and the rule survives an instance replacement.

### 4.3 EC2 module — where the cost traps live

```hcl
# infra/terraform/modules/ec2/main.tf
data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-6.1-x86_64"
}

resource "aws_iam_role" "node" {
  name = "${var.name}-node"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Enables SSM Session Manager + Run Command. Free, and removes the need for SSH.
resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# Pull-only ECR access for the node.
resource "aws_iam_role_policy_attachment" "ecr_ro" {
  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}

resource "aws_iam_instance_profile" "node" {
  name = "${var.name}-node"
  role = aws_iam_role.node.name
}

resource "aws_instance" "node" {
  ami                    = data.aws_ssm_parameter.al2023.value
  instance_type          = var.instance_type
  subnet_id              = var.subnet_id
  vpc_security_group_ids = [var.security_group_id]
  iam_instance_profile   = aws_iam_instance_profile.node.name
  user_data              = file("${path.module}/user_data.sh")

  # ⚠️ THE #1 SURPRISE CHARGE ON t3 INSTANCES.
  # t3 defaults to "unlimited", which bills $0.05/vCPU-hour above the CPU
  # credit baseline. K3s will exceed baseline. "standard" throttles instead
  # of billing — slower, but genuinely $0.
  credit_specification {
    cpu_credits = "standard"
  }

  root_block_device {
    volume_size           = 30      # exactly the free-tier EBS allowance
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = true
  }

  metadata_options {
    http_tokens   = "required"      # IMDSv2 only — blocks SSRF credential theft
    http_endpoint = "enabled"
  }

  tags = { Name = "${var.name}-k3s" }
}

# Free ONLY while attached to a running instance, and only for the 750 hrs/mo
# of public IPv4 the free tier grants. An unattached EIP bills $3.60/mo.
resource "aws_eip" "node" {
  instance = aws_instance.node.id
  domain   = "vpc"
  tags     = { Name = "${var.name}-eip" }
}

output "public_ip"    { value = aws_eip.node.public_ip }
output "instance_id"  { value = aws_instance.node.id }
output "instance_arn" { value = aws_instance.node.arn }
```

`cpu_credits = "standard"` is the single most important line in this file. It is the difference
between a $0 bill and a surprise $15 one, and it is omitted from almost every "free tier EC2"
tutorial online.

### 4.4 RDS module

```hcl
# infra/terraform/modules/rds/main.tf
resource "aws_db_subnet_group" "this" {
  name       = "${var.name}-subnets"
  subnet_ids = var.subnet_ids           # needs >= 2 AZs even for single-AZ
}

resource "aws_db_parameter_group" "this" {
  name   = "${var.name}-pg16"
  family = "postgres16"

  # db.t3.micro has 1 GB RAM. The default max_connections (~112) will exhaust
  # memory before connections. Cap it and size your pgx pools to match.
  parameter {
    name         = "max_connections"
    value        = "60"
    apply_method = "pending-reboot"
  }
}

resource "aws_db_instance" "this" {
  identifier     = var.name
  engine         = "postgres"
  engine_version = "16.4"
  instance_class = "db.t3.micro"        # free tier eligible

  allocated_storage     = 20            # exactly the free-tier allowance
  max_allocated_storage = 0             # ⚠️ disable autoscaling — it silently bills past 20 GB
  storage_type          = "gp2"         # free tier covers gp2
  storage_encrypted     = true

  db_name  = "consignment_db"           # the other DBs are created by migration
  username = "fleetadmin"
  password = var.db_password

  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [var.security_group_id]
  parameter_group_name   = aws_db_parameter_group.this.name

  publicly_accessible = false           # no public endpoint, despite the public subnet
  multi_az            = false           # Multi-AZ is NOT free tier

  backup_retention_period      = 1      # free up to 20 GB of backup
  performance_insights_enabled = false  # NOT free on t3.micro
  monitoring_interval          = 0      # Enhanced Monitoring is NOT free
  skip_final_snapshot          = true
  deletion_protection          = false  # you will destroy this often

  apply_immediately = true
}

output "endpoint" { value = aws_db_instance.this.endpoint }
```

Four settings here exist purely to stay at $0: `max_allocated_storage = 0`, `multi_az = false`,
`performance_insights_enabled = false`, and `monitoring_interval = 0`. Performance Insights and
Enhanced Monitoring are both easy to enable by reflex and neither is free-tier covered.

Create the remaining databases from a migration job or once by hand:

```sql
CREATE DATABASE facility_db;
CREATE DATABASE dispatch_db;
```

### 4.5 Apply

```bash
cd infra/terraform/environments/free

export TF_VAR_db_password='<generate a strong one>'
export TF_VAR_my_ip_cidr="$(curl -s https://checkip.amazonaws.com)/32"

terraform init
terraform plan -out=tfplan
terraform apply tfplan

terraform output          # public_ip, instance_id, rds endpoint, ci_role_arn
```

Then set two GitHub repo secrets: `AWS_CI_ROLE_ARN` and `EC2_INSTANCE_ID` from those outputs.

---

## 5. Phase 4 — Observability

### 5.1 The honest recommendation: Grafana Cloud free tier

You asked how to run Prometheus and Grafana pods on the instance. **On 1 GB, you should not** — the
arithmetic at the top of this document shows they need ~380 MB against ~190 MB of headroom, and
they will evict your application pods to get it.

**Grafana Cloud's free tier is genuinely free forever** (10k metric series, 50 GB logs, 50 GB
traces, 14-day retention) and needs only a lightweight agent on the box.

```bash
# ~80 MB total, replaces ~380 MB of Prometheus + Grafana
helm repo add grafana https://grafana.github.io/helm-charts
helm repo update

helm upgrade --install alloy grafana/alloy -n monitoring --create-namespace \
  --set alloy.configMap.content="$(cat <<'EOF'
prometheus.scrape "kubernetes_pods" {
  targets    = discovery.kubernetes.pods.targets
  forward_to = [prometheus.remote_write.grafanacloud.receiver]
  scrape_interval = "60s"        // 60s not 15s — 4x fewer series against the 10k cap
}

discovery.kubernetes "pods" {
  role = "pod"
}

prometheus.remote_write "grafanacloud" {
  endpoint {
    url = env("GC_PROM_URL")
    basic_auth {
      username = env("GC_PROM_USER")
      password = env("GC_PROM_KEY")
    }
  }
}
EOF
)" \
  --set-string "alloy.extraEnv[0].name=GC_PROM_URL"  --set-string "alloy.extraEnv[0].value=$GC_PROM_URL" \
  --set-string "alloy.extraEnv[1].name=GC_PROM_USER" --set-string "alloy.extraEnv[1].value=$GC_PROM_USER" \
  --set-string "alloy.extraEnv[2].name=GC_PROM_KEY"  --set-string "alloy.extraEnv[2].value=$GC_PROM_KEY" \
  --set alloy.resources.requests.memory=64Mi \
  --set alloy.resources.limits.memory=128Mi
```

**Cardinality discipline matters more here than in the paid architecture** — 10k series is a real
ceiling. Scrape at 60s, drop `go_*` and `promhttp_*` runtime metrics you are not using, and never
label a metric with `waybill` or `merchant_id` (blueprint §5.1).

### 5.2 If you insist on in-cluster Prometheus

It is possible with aggressive trimming, and it is a legitimate exercise in resource tuning. Expect
it to be tight, and run **at most 2 application services** alongside it.

```yaml
# infra/helm/values-prometheus-tiny.yaml  — kube-prometheus-stack, stripped
alertmanager:  { enabled: false }        # ~60 MB saved
nodeExporter:  { enabled: false }        # K3s gives you most of this
kubeStateMetrics: { enabled: true }

grafana:
  enabled: true
  resources:
    requests: { memory: 64Mi, cpu: 20m }
    limits:   { memory: 128Mi }
  persistence: { enabled: false }        # dashboards from ConfigMaps, not a PVC
  defaultDashboardsEnabled: false        # the bundled set is heavy

prometheus:
  prometheusSpec:
    retention: 6h                        # NOT 15d — this is the biggest memory lever
    retentionSize: 512MB
    scrapeInterval: 60s
    evaluationInterval: 60s
    resources:
      requests: { memory: 200Mi, cpu: 50m }
      limits:   { memory: 400Mi }
    walCompression: true
    # Keep ONLY fleetpulse metrics + basic kube state. Everything else is dropped
    # before ingestion, which is what keeps memory bounded.
    serviceMonitorSelector:
      matchLabels: { release: kube-prom }
```

```bash
helm upgrade --install kube-prom prometheus-community/kube-prometheus-stack \
  -n monitoring --create-namespace -f infra/helm/values-prometheus-tiny.yaml
```

`retention: 6h` is the setting that makes or breaks this. Prometheus memory scales with retained
series × retention; the default 15 days on this box means certain OOM.

Access Grafana without an ingress:

```bash
kubectl -n monitoring patch svc kube-prom-grafana \
  -p '{"spec":{"type":"NodePort","ports":[{"port":80,"nodePort":30300}]}}'
# http://<elastic-ip>:30300  — reachable only from your IP per the SG rule
```

### 5.3 Application resource limits

Every pod needs limits on a 1 GB node. Without them one leaky service takes down the cluster.

```yaml
resources:
  requests: { memory: 24Mi,  cpu: 10m }
  limits:   { memory: 64Mi }        # memory limit yes, CPU limit no (blueprint §2.5)
```

---

## 6. Cost Safety Guardrails

Do all of this **before** `terraform apply`. A budget alarm configured after the fact tells you
about money you have already spent.

### 6.1 Enable billing alerts (console, one time)

Billing metrics do not exist until you switch them on, and they live **only in `us-east-1`**
regardless of where your resources are.

1. Sign in as the **root user** (this step requires it).
2. **Billing and Cost Management** → **Billing preferences**.
3. Tick **Receive Free Tier Alerts** and enter your email.
4. Tick **Receive Billing Alerts** → **Save preferences**.
5. Wait ~24 hours for the first `EstimatedCharges` datapoint to appear.

If IAM users need to see billing, also enable **IAM user and role access to Billing information** on
the Account page.

### 6.2 AWS Budgets — $1.00 threshold

Console path: **Billing** → **Budgets** → **Create budget** → **Customize (advanced)** → **Cost
budget** → Period **Monthly**, Budget amount **$1.00** → add alerts at **80% actual**, **100%
actual**, and **100% forecasted** → email address → Create.

Or in Terraform, which is better because it is reproducible:

```hcl
# infra/terraform/modules/budget/main.tf
resource "aws_budgets_budget" "monthly" {
  name         = "${var.name}-monthly-usd"
  budget_type  = "COST"
  limit_amount = var.limit_usd            # "1.0"
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  # Warn at 80 cents.
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }

  # Alert the moment you actually cross $1.
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }

  # The most useful one: fires early in the month when the RUN RATE implies
  # you will exceed $1, giving you days of warning instead of a post-mortem.
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
  }
}

# Separate budget that tracks free-tier usage percentages rather than dollars.
resource "aws_budgets_budget" "free_tier_usage" {
  name         = "${var.name}-free-tier"
  budget_type  = "COST"
  limit_amount = "0.01"
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_filter {
    name   = "Service"
    values = ["Amazon Elastic Compute Cloud - Compute", "Amazon Relational Database Service"]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 1
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }
}
```

**AWS Budgets gives you 2 budgets free**; additional ones are $0.02/day. The two above use exactly
your free allowance.

The **forecasted** alert is the one that actually saves you. Actual-spend alerts tell you the money
is gone; a forecast alert on day 3 saying "you are trending toward $12 this month" gives you time to
find the resource you left running.

### 6.3 CloudWatch billing alarm (belt and braces)

```hcl
# NOTE: billing metrics exist ONLY in us-east-1.
provider "aws" {
  alias  = "billing"
  region = "us-east-1"
}

resource "aws_sns_topic" "billing" {
  provider = aws.billing
  name     = "${var.name}-billing-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  provider  = aws.billing
  topic_arn = aws_sns_topic.billing.arn
  protocol  = "email"
  endpoint  = var.alert_email       # confirm the subscription email AWS sends you
}

resource "aws_cloudwatch_metric_alarm" "over_one_dollar" {
  provider            = aws.billing
  alarm_name          = "${var.name}-estimated-charges-over-1usd"
  namespace           = "AWS/Billing"
  metric_name         = "EstimatedCharges"
  dimensions          = { Currency = "USD" }
  statistic           = "Maximum"
  period              = 21600       # 6h — the metric only updates ~4x/day
  evaluation_periods  = 1
  threshold           = 1.0
  comparison_operator = "GreaterThanThreshold"
  alarm_actions       = [aws_sns_topic.billing.arn]
  alarm_description   = "FleetPulse free-tier spend exceeded $1.00"
  treat_missing_data  = "notBreaching"
}
```

You get **10 CloudWatch alarms free, forever.** This uses one.

> ⚠️ **Check your email and click the SNS confirmation link.** An unconfirmed subscription delivers
> nothing, and this is the most common reason people's billing alarms silently never fire.

### 6.4 Also enable — all free

- **Cost Anomaly Detection** (Billing → Cost Anomaly Detection): free, uses ML to flag unusual
  spend, catches things a fixed threshold misses.
- **Free Tier usage alerts**: emails at 85% of any free-tier allowance. Enabled in §6.1 step 3.
- **Cost Allocation Tags**: activate `Project` in Billing → Cost allocation tags. **Not
  retroactive** — activate on day one or your first month is unattributable.

### 6.5 The weekly habit

```bash
# Sunday, 2 minutes:
aws ce get-cost-and-usage \
  --time-period Start=$(date -d '7 days ago' +%Y-%m-%d),End=$(date +%Y-%m-%d) \
  --granularity DAILY --metrics UnblendedCost \
  --group-by Type=DIMENSION,Key=SERVICE \
  --query 'ResultsByTime[].Groups[?Metrics.UnblendedCost.Amount!=`0`]'
```

And when you finish a study session:

```bash
# Not using it for a few days? RDS can be stopped for up to 7 days at a time.
aws rds stop-db-instance --db-instance-identifier fleetpulse-free
aws ec2 stop-instances --instance-ids "$INSTANCE_ID"
```

A stopped EC2 instance consumes no instance-hours. **But note: a stopped instance with an attached
Elastic IP starts billing for the IP** ($0.005/hr), because the free public-IPv4 allowance applies
to *running* instances. For breaks longer than a week, `terraform destroy` is cleaner than stopping.

---

## 7. Milestones for the zero-cost track

Reworked from [blueprint §6](FleetPulse-Blueprint.md) for this architecture. Local-first, AWS second.

| # | Milestone | Where | Exit criterion |
|---|---|---|---|
| **1** | Guardrails before anything | AWS console | Budgets + billing alarm live; SNS email **confirmed**; Free Tier model identified |
| **2** | One service, end to end | Local Compose | `POST /v1/waybills` persists, `/metrics` exposes a custom counter, tests green, **first git commit** |
| **3** | Event backbone with NATS JetStream | Local Compose | Booking → webhook fires; stop NATS, book 10, restart → all 10 drain from the outbox |
| **4** | All 4 services + simulator | Local kind | 100 parcels/min for 10 min, no lost events, no leaks |
| **5** | Terraform + K3s on AWS | AWS | `terraform apply` → SSM into a Ready K3s node; RDS reachable from it; `terraform destroy` clean |
| **6** | CI/CD to the live cluster | GitHub + AWS | Push to `main` → ECR → SSM rollout → new SHA live. **Zero static AWS keys anywhere** |
| **7** | Observability | AWS + Grafana Cloud | Custom FleetPulse metrics visible in a hosted dashboard; one alert fires on a real condition |
| **8** | Production depth | Local kind | Argo CD pull-based GitOps, HPA/KEDA, chaos experiments — where the RAM is free |

Milestone 1 first is not padding. Configure the alarms before you can possibly spend anything.

---

## 8. What to say in the interview

This architecture is a **deliberate cost-constrained design**, and that framing is worth more than
pretending it is production-grade. Three answers to prepare:

**"Why K3s and not EKS?"**
> "EKS is $73/month for the control plane alone, which was outside my budget. K3s gave me the same
> Kubernetes API for $0, so I could still practise Helm, RBAC, resource management, and rolling
> deploys. I designed the Terraform modules so that swapping in an EKS module is a contained change
> — the workload manifests are unchanged. What I gave up is the managed control plane, IRSA, and
> multi-node scheduling, so I practised those separately on a local kind cluster."

**"How do you authenticate CI to AWS?"**
> "GitHub OIDC federation. No long-lived access keys anywhere — Actions exchanges a short-lived OIDC
> token for temporary credentials, and the trust policy is pinned to my specific repo on both the
> `aud` and `sub` claims. Deployment goes over SSM Run Command rather than SSH, so there's also no
> private key in GitHub Secrets and no inbound port open."

**"How did you keep it at zero cost?"**
> "The three things that actually catch people: t3 instances default to unlimited CPU credit mode,
> which bills per vCPU-hour above baseline, so I pinned `cpu_credits = "standard"`. Public IPv4 has
> been chargeable since February 2024, so I run exactly one Elastic IP and only while the instance
> is running. And I set an ECR lifecycle policy on day one because the free tier is 500 MB and every
> CI run pushes an image. Then Budgets with a forecast alert, because actual-spend alerts only tell
> you money is already gone."

That last answer demonstrates cost awareness as an engineering discipline, which is a genuine
differentiator for a junior DevOps candidate.
