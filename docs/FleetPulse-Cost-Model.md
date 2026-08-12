# FleetPulse Cost Model

Monthly AWS cost estimation for the architecture in [FleetPulse-Blueprint.md](FleetPulse-Blueprint.md),
across three scale tiers, with per-line-item arithmetic you can audit and adjust.

> **Pricing basis:** `us-east-1`, on-demand list prices, **730 hours/month** (365 × 24 ÷ 12).
> Figures reflect published rates as of **August 2026**. AWS pricing changes without notice and
> varies significantly by region — `eu-west-1` runs ~5–10% higher, `ap-south-1` (Mumbai, natural for
> a Delhivery-modelled system) is broadly similar to `us-east-1` on compute but differs on data
> transfer. **Verify against the [AWS Pricing Calculator](https://calculator.aws) before committing,
> and wire `infracost` into CI (Blueprint §4.5) so every PR reports its own cost delta.**
>
> These are *infrastructure* estimates. They exclude taxes, support plans, and any AWS credits.

---

## The headline finding, first

Cost in this architecture is driven almost entirely by **always-on managed-service floors and
high-availability multipliers — not by traffic.**

| | Dev / Sandbox | Moderate Prod | High-Scale Prod |
|---|---|---|---|
| Requests/day | ~0 (simulated bursts) | 10,000 | 1,000,000 |
| **Traffic multiple vs. previous tier** | — | — | **100×** |
| **Cost multiple vs. previous tier** | — | **~5.6×** | **~2.2×** |

Going from a sandbox to a 10k/day production posture costs 5.6× more while serving traffic that
would fit on a Raspberry Pi. Going from 10k/day to **one million** requests/day — a hundredfold
traffic increase — costs only 2.2× more.

You are not buying throughput. You are buying **redundancy, managed control planes, and NAT
gateways.** Every optimization in §3 follows from that single observation.

---

## 1. Service-by-Service Breakdown

### 1.1 Compute — EKS and EC2 nodes

| Component | Rate | Dev | Moderate | High-Scale |
|---|---|---|---|---|
| EKS control plane | $0.10/hr | $73.00 | $73.00 | $73.00 |
| System node group (on-demand, `t4g.medium`) | $0.0336/hr | 2 × spot → $17.20 | 2 × OD → $49.06 | 3 × OD → $73.59 |
| Workload nodes — Karpenter | see below | — | 3 × `m7g.large` spot → $62.55 | 8 × `m7g.large` spot → $166.80 |
| Workload nodes — on-demand baseline | $0.0816/hr | — | — | 2 × `m7g.large` → $119.14 |
| EBS `gp3` root volumes | $0.08/GB-mo | 60 GB → $4.80 | 100 GB → $8.00 | 360 GB → $28.80 |
| **Compute subtotal** | | **$95.00** | **$192.61** | **$461.33** |

Instance rates used (us-east-1, Graviton/arm64):

| Instance | On-demand /hr | On-demand /mo | Spot (~65% off) /mo |
|---|---|---|---|
| `t4g.small` | $0.0168 | $12.26 | $4.29 |
| `t4g.medium` | $0.0336 | $24.53 | $8.60 |
| `m7g.large` | $0.0816 | $59.57 | $20.85 |
| `m7g.xlarge` | $0.1632 | $119.14 | $41.70 |
| `c7g.large` | $0.0725 | $52.93 | $18.53 |

Two things that materially move this line:

**⚠️ EKS extended support is a 6× cliff.** The control plane is $0.10/hr only while your cluster
runs a version in *standard* support. Let it age out and it silently becomes **$0.60/hr = $438/mo**.
For a sandbox you touch intermittently this is a very real trap — a cluster you forgot about on an
EOL version costs more than the entire dev tier. Set a calendar reminder at each version's standard-
support end date, or destroy between sessions (§3.3).

**Graviton + spot is already priced in.** The blueprint's multi-arch images (§3.3) and arm64
Karpenter NodePool (§4.3) exist precisely so these numbers are achievable. On x86 on-demand the
compute line roughly triples.

### 1.2 Databases, Caches & Message Broker

| Component | Dev | Moderate | High-Scale |
|---|---|---|---|
| **RDS PostgreSQL** instance | `db.t4g.micro` single-AZ<br/>$0.016/hr → **$11.68** | `db.t4g.medium` **Multi-AZ**<br/>$0.065/hr × 2 → **$94.90** | `db.m7g.large` **Multi-AZ**<br/>$0.171/hr × 2 → **$249.66** |
| RDS read replica | — | — | `db.m7g.large` → **$124.83** |
| RDS storage (`gp3` @ $0.115/GB-mo) | 20 GB → $2.30 | 100 GB → $11.50 | 500 GB → $57.50 |
| RDS backups (free ≤ 100% of provisioned; then $0.095/GB-mo) | $0.00 | $0.00 | 200 GB over → $19.00 |
| **ElastiCache (Valkey)** | `cache.t4g.micro`<br/>→ **$9.35** | `cache.t4g.small` × 2<br/>→ **$37.38** | `cache.m7g.large` × 2<br/>→ **$182.21** |
| **Amazon MQ for RabbitMQ** | `mq.t3.micro` single<br/>$0.0195/hr → **$14.24** | **`mq.m5.large` CLUSTER (3 nodes)**<br/>$0.294/hr × 3 → **$643.86** | `mq.m5.large` CLUSTER (3)<br/>→ **$643.86** |
| MQ broker storage ($0.30/GB-mo) | 5 GB → $1.50 | 20 GB → $6.00 | 50 GB → $15.00 |
| **Data subtotal** | **$39.07** | **$793.64** | **$1,292.06** |

**⚠️ The single biggest cost cliff in this entire architecture is Amazon MQ high availability.**

Amazon MQ for RabbitMQ offers exactly two deployment modes, and there is nothing between them:

| Mode | Minimum instance | Nodes billed | Monthly |
|---|---|---|---|
| `SINGLE_INSTANCE` | `mq.t3.micro` | 1 | **$14.24** |
| `CLUSTER_MULTI_AZ` | **`mq.m5.large`** (t3.micro not permitted) | **3** | **$643.86** |

Turning on broker HA multiplies that line by **45×**, and at the moderate tier it becomes ~55% of
the entire bill. There is no `mq.t3.small` cluster option to soften the jump.

Three honest ways to handle it:

1. **Accept single-instance** with `mq.m5.large` ($214.62) or smaller. Amazon MQ single-instance
   brokers still get automated patching and backups; you accept a few minutes of downtime during
   maintenance and a longer RTO on failure. **Given that the blueprint's outbox pattern (§1.6) means
   producers keep accepting writes during a broker outage and drain on recovery, this is a far more
   defensible trade than it would be in an architecture without an outbox.** Saves $429/mo.
2. **Self-host RabbitMQ on EKS** via the RabbitMQ Cluster Operator — a 3-node StatefulSet on nodes
   you are already paying for. Near-zero marginal cost, real HA, and considerably more operational
   learning. Costs you the managed-service convenience and adds a component you must patch. Saves
   ~$644/mo.
3. **Pay for the cluster.** Correct for genuine production. Wrong for a sandbox.

For a learning project, option 2 is arguably the *best* choice pedagogically — running a stateful
clustered broker on Kubernetes teaches more than clicking a managed one into existence.

### 1.3 Networking & Traffic

| Component | Rate | Dev | Moderate | High-Scale |
|---|---|---|---|---|
| NAT Gateway (hourly) | $0.045/hr | 1 → $32.85 | 3 → $98.55 | 3 → $98.55 |
| NAT data processing | $0.045/GB | 10 GB → $0.45 | 50 GB → $2.25 | 2 TB → $90.00 |
| ALB (hourly) | $0.0225/hr | $16.43 | $16.43 | $16.43 |
| ALB LCUs | $0.008/LCU-hr | ~0.5 → $2.92 | ~2 → $11.68 | ~10 → $58.40 |
| Cross-AZ transfer | $0.01/GB each way | ~$0 | 200 GB → $4.00 | 2 TB → $40.00 |
| Internet egress (first 100 GB/mo free) | $0.09/GB | 0 → $0.00 | 200 GB → $9.00 | 2 TB → $171.00 |
| **Networking subtotal** | | **$52.65** | **$141.91** | **$474.38** |

**NAT Gateway is the most over-paid line in most AWS bills.** At the dev tier it is $33/mo to give
private-subnet nodes outbound access they mostly use for pulling container images and reaching AWS
APIs — both of which have cheaper paths (§3.3). At three AZs it is $98.55/mo *before a single byte
moves*.

**Cross-AZ transfer is the sneaky one.** Multi-AZ Kubernetes means pod-to-pod and pod-to-RDS traffic
routinely crosses availability zones at $0.01/GB **in each direction** — so $0.02/GB round trip. It
appears on the bill as an unexplained "EC2-Other" line that people struggle to attribute. `topology
aware routing` / `trafficDistribution: PreferClose` on Services keeps chatty paths in-zone.

**ALB LCU math** — you are billed on the *maximum* of four dimensions, not their sum: new
connections (25/s per LCU), active connections (3,000/min), processed bytes (1 GB/hr), and rule
evaluations (1,000/s). At 10k requests/day you are nowhere near any threshold; the LCU estimate
above is dominated by idle active-connection counts. A single shared ALB with host-based routing
across all four services (as the blueprint assumes) is correct — one ALB per service would multiply
the $16.43 base by four for no benefit.

### 1.4 Storage & Registries

| Component | Rate | Dev | Moderate | High-Scale |
|---|---|---|---|---|
| ECR private storage | $0.10/GB-mo | 5 GB → $0.50 | 15 GB → $1.50 | 30 GB → $3.00 |
| EBS — Prometheus/Grafana/Loki PVCs | $0.08/GB-mo | 20 GB → $1.60 | 100 GB → $8.00 | 500 GB → $40.00 |
| S3 — Terraform state | $0.023/GB-mo | ~$0.10 | ~$0.10 | ~$0.10 |
| S3 — logs / backups / SBOMs | $0.023/GB-mo | 5 GB → $0.12 | 50 GB → $1.15 | 500 GB → $11.50 |
| S3 requests | PUT $0.005/1k, GET $0.0004/1k | ~$0.05 | ~$0.30 | ~$2.00 |
| **Storage subtotal** | | **$2.37** | **$11.05** | **$56.60** |

ECR is cheap but grows without bound if untended — every CI run pushes a SHA-tagged multi-arch
image. **Set an ECR lifecycle policy from day one** (keep last 20 tagged, expire untagged after 3
days) or you will find 400 GB of dead images a year from now. Pulls from ECR to EKS in the same
region are free; the storage is the only real charge.

### 1.5 Observability & Management

This is the section where the choice between self-hosted and managed swings the bill hardest.

**Option A — self-hosted (`kube-prometheus-stack`, Grafana, Loki, Jaeger on the cluster).** The
blueprint's default. Marginal AWS cost is only the EBS in §1.4 plus the node capacity to run it
(~2–3 GB RAM, already counted in §1.1).

| Component | Dev | Moderate | High-Scale |
|---|---|---|---|
| Prometheus / Grafana / Loki / Jaeger compute + storage | *(in §1.1 / §1.4)* | *(in §1.1 / §1.4)* | *(in §1.1 / §1.4)* |
| CloudWatch Logs — EKS control plane + fallback ($0.50/GB ingest) | 10 GB → $5.00 | 50 GB → $25.00 | 500 GB → $250.00 |
| CloudWatch Logs storage ($0.03/GB-mo) | $0.30 | $1.50 | $15.00 |
| CloudWatch metrics/alarms | ~$3.00 | ~$5.00 | ~$10.00 |
| **Observability subtotal (self-hosted)** | **$8.30** | **$31.50** | **$275.00** |

**Option B — AWS managed (AMP + AMG).**

Amazon Managed Prometheus bills on **samples ingested**: $0.90 per 10M samples for the first 2B/mo,
$0.35/10M for the next 248B, $0.16/10M beyond. The sample count is what surprises people:

```
samples/month = active_series × (60 / scrape_interval_seconds) × 60 × 24 × 30

At a 30s scrape interval:  1 series = 86,400 samples/month
```

| Tier | Active series | Samples/mo | AMP ingest | AMP storage | AMG (1 editor) | **Total** |
|---|---|---|---|---|---|---|
| Dev | 5,000 | 432M | $38.88 | ~$1 | $9 | **~$49** |
| Moderate | 30,000 | 2.59B | $180.00 + $20.65 | ~$5 | $9 | **~$215** |
| High-Scale | 120,000 | 10.4B | $180.00 + $294.00 | ~$20 | $14 | **~$508** |

**Self-hosting Prometheus saves ~$180/mo at the moderate tier and ~$500/mo at high scale** — and for
a project whose explicit purpose is learning observability engineering, running the stack yourself
*is* the exercise. Use AMP only if you specifically want to learn AMP.

**⚠️ CloudWatch Logs at $0.50/GB ingested is the most common runaway cost in EKS.** Two specific
traps in this architecture:

- **EKS audit logging** (which the blueprint enables in §4.2 for good security reasons) is extremely
  chatty — 1–3 GB/day on even a quiet cluster, so **$15–45/mo in dev alone** for logs you will
  almost never read. In dev, either disable the `audit` log type or set a 1-day retention.
- **Application logs at high scale.** 500 GB/mo of structured JSON is $250 to ingest. Ship to Loki
  with S3 backing instead ($11.50 for the same volume) and keep CloudWatch for control-plane logs only.

**Never leave CloudWatch log groups on infinite retention.** New groups default to "Never expire."
Set retention in Terraform on every group you create.

### 1.6 Combined totals per tier

| Category | Dev / Sandbox | Moderate Prod | High-Scale Prod |
|---|---|---|---|
| Compute (§1.1) | $95.00 | $192.61 | $461.33 |
| Data & Broker (§1.2) | $39.07 | $793.64 | $1,292.06 |
| Networking (§1.3) | $52.65 | $141.91 | $474.38 |
| Storage (§1.4) | $2.37 | $11.05 | $56.60 |
| Observability (§1.5, self-hosted) | $8.30 | $31.50 | $275.00 |
| **Total (24×7, on-demand list)** | **$197.39** | **$1,170.71** | **$2,559.37** |

---

## 2. Scale-Based Cost Tiers

### 2.1 Dev / Sandbox — ~$197/mo running 24×7

**Configuration:** single NAT, single-AZ data tier, all-spot nodes, single-instance broker,
self-hosted observability, no replicas.

| | |
|---|---|
| EKS control plane | $73.00 |
| 2 × `t4g.medium` spot + 60 GB EBS | $22.00 |
| RDS `db.t4g.micro` single-AZ + 20 GB | $13.98 |
| ElastiCache `cache.t4g.micro` (Valkey) | $9.35 |
| Amazon MQ `mq.t3.micro` single + 5 GB | $15.74 |
| 1 × NAT Gateway + processing | $33.30 |
| ALB + ~0.5 LCU | $19.35 |
| ECR, S3, observability EBS | $2.37 |
| CloudWatch (modest retention) | $8.30 |
| **Total** | **$197.39** |

**But you should almost never pay this.** The single most effective lever in the entire document:

> **`terraform destroy` between sessions.** At 10 hours/week of actual use (~43 hrs/mo, or 6% of
> 730), every hourly-billed resource — EKS control plane included — scales down proportionally.
> Only S3 state and ECR storage persist.
>
> **Effective cost: ~$15–25/month.**

The full stack rebuilds in roughly 20 minutes. Practising destroy-and-rebuild is not a compromise;
it is the exercise that proves your IaC is actually complete. A `make dev-up` / `make dev-down` pair
removes the friction that otherwise makes you leave it running.

**⚠️ The most expensive mistake in this project is leaving the dev tier running.** $197/mo × 12 =
**$2,369/year** for a sandbox you use a few hours a week.

### 2.2 Moderate Production — ~$1,171/mo

**Configuration:** 3 AZs, Multi-AZ RDS, replicated cache, clustered broker, ~10,000 requests/day.

| | |
|---|---|
| EKS + 5 nodes (mixed OD/spot) + EBS | $192.61 |
| RDS `db.t4g.medium` Multi-AZ + 100 GB | $106.40 |
| ElastiCache `cache.t4g.small` × 2 | $37.38 |
| **Amazon MQ CLUSTER_MULTI_AZ (3 × `mq.m5.large`)** | **$649.86** |
| 3 × NAT Gateway + processing | $100.80 |
| ALB + 2 LCU + cross-AZ + 200 GB egress | $41.11 |
| Storage & registries | $11.05 |
| CloudWatch + self-hosted observability | $31.50 |
| **Total** | **$1,170.71** |

**10,000 requests/day is 0.12 requests/second.** Essentially none of this bill is throughput —
it is entirely the cost of *surviving an AZ failure*. The broker cluster alone is 55%.

Drop to a single-instance broker and one NAT and the same workload runs at **~$594/mo** with a
degraded-but-recoverable failure posture that the outbox pattern already partially compensates for.

### 2.3 High-Scale Production — ~$2,559/mo

**Configuration:** 3 AZs, Multi-AZ RDS + read replica, Karpenter autoscaling, ~1,000,000
requests/day (11.6 rps average, ~50 rps peak).

| | |
|---|---|
| EKS + 13 nodes (spot-heavy) + 360 GB EBS | $461.33 |
| RDS `db.m7g.large` Multi-AZ + replica + 500 GB + backups | $451.00 |
| ElastiCache `cache.m7g.large` × 2 | $182.21 |
| Amazon MQ CLUSTER (3 × `mq.m5.large`) + 50 GB | $658.86 |
| 3 × NAT + 2 TB processing | $188.55 |
| ALB + 10 LCU + cross-AZ + 2 TB egress | $285.83 |
| Storage & registries | $56.60 |
| CloudWatch Logs 500 GB + metrics | $275.00 |
| **Total** | **$2,559.37** |

Note what changed from the moderate tier and what did not. **Traffic went up 100×; the bill went up
2.2×.** The broker cluster line is *identical* — `mq.m5.large` handles both. What actually grew was
compute (+$269), the database tier (+$345), egress and NAT data processing (+$333), and CloudWatch
Logs (+$244).

**At this tier the marginal costs are data movement and log ingestion, not servers.** That is a
different optimization problem from the lower tiers, and §3.3 treats it separately.

---

## 3. Cost Optimization Strategies

### 3.1 Free Tier eligibility

**⚠️ Verify which Free Tier model applies to your account first.** AWS restructured the Free Tier in
mid-2025: newer accounts receive a **credit-based** package (roughly $100 on signup plus up to $100
more earned through onboarding activities, expiring in ~6 months) rather than the classic
12-month-per-service allowances. Older accounts retain the classic model. Check the Billing console
before planning around any of the below — the difference is material.

**Classic 12-month Free Tier (if your account qualifies):**

| Service | Allowance | Covers in FleetPulse? |
|---|---|---|
| RDS | 750 hrs/mo `db.t4g.micro` single-AZ + 20 GB storage + 20 GB backup | ✅ **Fully covers the dev RDS line** (−$13.98/mo) |
| ElastiCache | 750 hrs/mo `cache.t4g.micro` | ✅ **Fully covers the dev cache line** (−$9.35/mo) |
| Amazon MQ | 750 hrs/mo single-instance `mq.t3.micro` + 20 GB storage | ✅ **Fully covers the dev broker line** (−$15.74/mo) |
| ALB | 750 hrs + 15 LCUs/mo | ✅ **Fully covers the dev ALB line** (−$19.35/mo) |
| EC2 | 750 hrs `t2.micro`/`t3.micro` | ⚠️ **x86 only — does not apply to Graviton `t4g`.** Using it means giving up arm64 |
| ECR | 500 MB private storage | ⚠️ Partial — multi-arch Go images will exceed this |
| S3 | 5 GB Standard | ✅ Covers Terraform state |
| CloudWatch | 10 metrics, 10 alarms, 5 GB logs ingest | ⚠️ Partial — EKS audit logs blow through 5 GB fast |

**Always free (no expiry):** CloudWatch 10 custom metrics / 10 alarms / 5 GB log ingest, and — most
usefully here — **VPC gateway endpoints for S3 and DynamoDB cost nothing.**

**Never free, at any tier:**

- **EKS control plane — $73/mo from the first hour.** There is no free tier for EKS. This is the
  irreducible floor of the dev environment.
- **NAT Gateway — $32.85/mo.** No free tier.
- **EBS beyond the 30 GB general-purpose allowance.**

> **Best case for a qualifying new account:** Free Tier covers RDS, ElastiCache, Amazon MQ, and ALB
> in the dev tier — about **$58/mo of the $197**. The remaining ~$139 is EKS, NAT, compute, and
> CloudWatch. **The floor is EKS + NAT ≈ $106/mo** no matter what else you do, which is exactly why
> destroy-when-idle beats every other optimization.

### 3.2 Savings Plans, Reserved Instances & Spot

| Mechanism | Discount | Commitment | Applies to | Verdict for FleetPulse |
|---|---|---|---|---|
| **Spot instances** | 60–70% | None | EC2 / Karpenter nodes | ✅ **Already assumed throughout.** Best single compute lever, zero commitment |
| **Graviton (arm64)** | ~20% vs x86 + better price/perf | None | EC2, RDS, ElastiCache | ✅ **Already assumed.** Free money; images are multi-arch already |
| Compute Savings Plan (1yr, no upfront) | ~27% | 1 year | EC2 + Fargate + Lambda, any family/region | ⚠️ Only for the on-demand *baseline* |
| Compute Savings Plan (3yr, all upfront) | up to 66% | 3 years | same | ❌ Do not commit a learning project for 3 years |
| EC2 Instance Savings Plan | up to 72% | 1–3 yrs | Locked to family + region | ❌ Too rigid alongside Karpenter's instance flexibility |
| **RDS Reserved Instance (1yr, no upfront)** | ~35% | 1 year | RDS instance hours | ⚠️ Worth it at moderate/high tier only |
| RDS RI (3yr, all upfront) | up to 69% | 3 years | RDS | ❌ Sandbox shouldn't commit |
| **ElastiCache Reserved Nodes** | ~35–55% | 1–3 yrs | Cache node hours | ⚠️ Moderate/high tier only |
| **Amazon MQ** | **No reserved pricing exists** | — | — | ❌ **You cannot discount the broker.** Only right-sizing or self-hosting reduces it |

**For the dev/sandbox tier: buy nothing.** Every commitment instrument assumes steady-state usage.
A learning project that should be destroyed between sessions has the opposite profile — Savings
Plans would bill you for the hours you deliberately are not using. **Spot plus destroy-when-idle
beats any commitment.**

That Amazon MQ has *no* reserved pricing is worth internalizing: the $644/mo cluster line is
undiscountable by any financial instrument. It can only be changed architecturally.

### 3.3 Architecture trade-offs — reaching 40–60% reduction

Ordered by savings-per-unit-of-lost-capability.

#### Dev tier: $197 → $89/mo (**−55%**)

| Change | Saves | What you give up |
|---|---|---|
| **Self-host PostgreSQL, Redis, RabbitMQ in-cluster** (StatefulSets on nodes you already pay for) | **$39.07** | Managed backups/patching. *Gain:* real StatefulSet, PVC, and operator experience |
| **Replace NAT Gateway with VPC endpoints** — gateway endpoints for S3 (free) + interface endpoints for ECR API/DKR, STS, Secrets Manager, CloudWatch Logs ($7.30/mo each) | **$18.30** net | Nodes lose general internet egress (usually fine — they only need AWS APIs and ECR) |
| *or* replace NAT GW with a `t4g.nano` NAT instance | $30.00 | You patch and monitor it; single point of failure |
| **Disable EKS `audit` log type in dev**, 1-day retention on the rest | **$5.00** | Audit trail you were not reading in a sandbox |
| **ECR lifecycle policy** (keep 20 tagged, expire untagged after 3d) | **$0.35** | Old image history |
| **Scale workload nodes to 1 × `t4g.medium` spot** when idle | **$8.60** | Headroom; Karpenter re-provisions on demand |
| **Sub-total** | **$71.32** | |
| **Lean dev total** | **~$126/mo** | |

Push further by dropping the managed control plane entirely:

| Aggressive change | Saves | What you give up |
|---|---|---|
| **Run `k3s` on a single `t4g.large` spot instead of EKS** | **$73.00** | IRSA, EKS-managed addons, Karpenter, the actual EKS learning |

That lands at **~$53/mo** but hollows out the point of Phase 3. **The better pattern: keep EKS, but
run it in bursts.** Do Milestone 5–6 work in concentrated multi-hour sessions, destroy afterwards,
and pay ~$15–25/mo effective while still learning real EKS.

#### Moderate tier: $1,171 → $531/mo (**−55%**)

| Change | Saves | What you give up |
|---|---|---|
| **Self-host RabbitMQ on EKS** (Cluster Operator, 3-node StatefulSet) *— or* single-instance Amazon MQ (saves $429) | **$643.86** | Managed broker. *Gain:* the most valuable ops learning in the stack |
| **3 NAT Gateways → 1** + VPC endpoints for AWS-API traffic | **$65.70** | Single-AZ egress failure domain (dev-acceptable, not prod-correct) |
| **Ship app logs to Loki/S3**, CloudWatch for control plane only | **$20.00** | CloudWatch Logs Insights queries |
| **RDS 1-year RI, no upfront** (~35%) | **$33.22** | 1-year commitment |
| **Topology-aware routing** to cut cross-AZ chatter | **$2.00** | Slightly less even load distribution |
| **Higher spot ratio on workload nodes** | **$25.00** | More interruption churn (which your PDBs should already handle) |
| **Total saved** | **$789.78** | |
| **Optimized moderate total** | **~$381/mo** (−67%) | |

Even keeping *managed* MQ but single-instance rather than clustered — the conservative version —
lands at **~$594/mo (−49%)**, comfortably inside the 40–60% target with only the broker's failure
posture changed.

#### High-scale tier: $2,559 → $1,344/mo (**−47%**)

At this tier the levers shift from "stop paying for idle capacity" to "stop moving data."

| Change | Saves | What you give up |
|---|---|---|
| **Self-host RabbitMQ on EKS** | **$643.86** | Managed broker |
| **Loki/S3 for logs instead of CloudWatch** (500 GB: $250 → $11.50) | **$238.50** | CloudWatch-native tooling |
| **VPC endpoints for ECR/S3/CloudWatch** — removes the bulk of NAT *data processing* | **$70.00** | Endpoint hourly fees (already netted) |
| **RDS + ElastiCache 1-year RIs** (~35%) | **$221.00** | 1-year commitment |
| **CloudFront in front of the ALB** for cacheable/static responses; CloudFront egress is cheaper than EC2 egress and origin-to-CloudFront transfer is free | **$60.00** | Added component to manage |
| **Tail-sampling traces + log-level tuning** (blueprint §5.3 already specifies this) | **$40.00** | Full-fidelity debug logs |
| **Total saved** | **$1,273.36** | |
| **Optimized high-scale total** | **~$1,286/mo** (−50%) | |

**Note the pattern across tiers.** Dev savings come from *turning things off*; moderate savings come
from *removing HA multipliers*; high-scale savings come from *not moving bytes through metered
paths*. Applying dev-tier tactics at high scale barely helps, and vice versa.

### 3.4 Guardrails to configure before the first `terraform apply`

Repeating from Blueprint §4.5 because it belongs here too:

1. **AWS Budgets alarms at $50 / $100 / $200** with email actions — the very first thing, before any
   infrastructure exists.
2. **`infracost` in CI** on every `terraform plan` so PRs show their cost delta.
3. **Karpenter `limits.cpu: "32"`** — a hard ceiling so a misconfigured HPA cannot provision an
   unbounded fleet.
4. **Cost Allocation Tags activated** (`Project=fleetpulse`, `Environment`) in the Billing console —
   tags only become filterable in Cost Explorer *after* activation, and it is not retroactive. Do
   this on day one or your first month is unattributable.
5. **`make dev-down`** wired to `terraform destroy`, and actually used.
6. **A calendar reminder for EKS version standard-support end dates** (§1.1's 6× cliff).

---

## 4. Summary — estimated monthly totals

| Tier | Traffic | On-demand list, 24×7 | Optimized (§3.3) | Realistic sandbox usage |
|---|---|---|---|---|
| **Dev / Sandbox** | Simulated bursts | **$180 – $215** | **$110 – $135** | **$15 – $25** *(destroy between sessions)* |
| **Moderate Production** | ~10,000 req/day | **$1,100 – $1,250** | **$380 – $600** | — |
| **High-Scale Production** | ~1,000,000 req/day | **$2,400 – $3,200** | **$1,250 – $1,500** | — |

Ranges reflect spot price volatility, actual data-transfer volumes, and log retention choices — the
three inputs with the widest real-world variance.

### The three numbers worth remembering

1. **~$106/mo** — the irreducible dev floor (EKS control plane $73 + NAT Gateway $33) that no free
   tier, Savings Plan, or reserved instance reduces. Only destroying the stack avoids it.
2. **$644/mo** — Amazon MQ `CLUSTER_MULTI_AZ`, the largest single line item at both production
   tiers, undiscountable by any financial instrument, and avoidable only by self-hosting the broker
   or accepting a single instance.
3. **2.2×** — the cost multiple for a **100×** traffic increase, from 10k to 1M requests/day. Cloud
   spend at this architecture's scale is a function of redundancy and data movement, not load.
