# EKS Cluster — Design

**Date:** 2026-08-14
**Branch:** `infra/bootstrap`
**Status:** Approved, ready for implementation planning

> Less-common acronyms are expanded on first use. **Appendix B** is a glossary.

## Contents

| § | | |
|---|---|---|
| [1](#1-problem) | **Problem** | Why EKS, why the cost tension, what a session costs |
| [2](#2-decisions) | **Decisions** | The eight choices and their rationale |
| [3](#3-architecture) | **Architecture** | One diagram: what runs where, and what `destroy` deletes |
| [4](#4-layout) | **Layout** | The three layers — bootstrap, infrastructure, workload — and the invariant they protect |
| [5](#5-cluster-stack-terraformeks) | **Cluster stack** | `terraform/eks/`: VPC, endpoints, node group, access entry |
| [6](#6-workload-manifests-k8s) | **Workload manifests** | `k8s/`: Deployments, Services, ConfigMap, probes |
| [7](#7-bootstrap-additions-and-iam) | **Bootstrap additions and IAM** | [7a](#7a-added-in-phase-0-applied-by-hagop-admin) what's added · [7b](#7b-permissions-boundary--written-by-hand-from-intent) permissions boundary · [7c](#7c-deployer-identity-policy--adopted-not-derived) identity policy · [7d](#7d-reactive-tightening) reactive tightening · [7e](#7e-why-bootstrap-stays-admin-applied-permanently) why admin-only |
| [8](#8-runbook) | **Runbook** | [8a](#8a-two-shells-one-per-role) two shells · [8b](#8b-spin-up-20-min-mostly-waiting) spin up · [8c](#8c-verify) verify · [8d](#8d-tear-down) tear down |
| [9](#9-ci) | **CI** | One step to add to the existing workflow |
| [10](#10-phases) | **Phases** | Build, first cycle, verify under least privilege |
| [11](#11-definition-of-done) | **Definition of done** | Seven items; item 6 is the real finish line |
| [12](#12-out-of-scope) | **Out of scope** | Deliberately excluded, so it isn't mistaken for oversight |
| [13](#13-to-verify-at-implementation-time) | **To verify** | Two things not to take on trust |
| [14](#14-prior-state-confirmed-2026-08-14) | **Prior state confirmed** | What was checked against live AWS |
| [A](#appendix-a--deployer-identity-policy-as-adopted) | **Appendix A** | The deployer policy in full, commented |
| [B](#appendix-b--glossary) | **Appendix B** | Glossary |

**If you read only two things:** [§2 Decisions](#2-decisions) and [§4's invariant](#the-invariant).
Everything else follows from them.

## 1. Problem

The roadmap calls for Terraform-provisioned infrastructure and Kubernetes manifests. An earlier
scoping pass designed a single EC2 instance running `docker compose`, but the stated preference is
Kubernetes provisioned directly by Terraform, with no `user_data` / cloud-init bootstrapping owned
by us.

That collides with cost: **Elastic Kubernetes Service (EKS) bills ~$0.10/hr for the control plane
even with zero nodes** (~$73/month), contradicting the near-zero standing cost the earlier scoping
assumed. Three options were considered:

1. **EKS + disciplined `terraform destroy`** — real cloud, ~15 min to recreate
2. **k3s on one EC2 instance** — cheap, but installing it *requires* `user_data`, defeating the point
3. **Local `kind`/`k3d` via Terraform** — free, demonstrates the skill, loses the real-cloud story

**Option 1 is chosen.** Everything below follows from making teardown reliable enough that it
actually happens.

### Cost

| Item | Rate |
|---|---|
| EKS control plane | $0.100/hr |
| 1× `t3.large` on-demand | $0.083/hr |
| Public IPv4 address | $0.005/hr |
| 20 GiB gp3 root volume | $0.002/hr |
| **Total while running** | **~$0.19/hr** |

A session (~15 min create + use + ~10 min destroy) costs roughly **$0.27**. Standing cost after
`destroy` is the Elastic Container Registry (ECR) repo (~$0.30/month) plus IAM roles, the state
bucket, and the lock table.

**The risk is not the hourly rate — it is forgetting once.** A cluster left running for a month is
~$140. Spot instances were considered and rejected: they save ~$0.058/hr (≈$8/year at two sessions
a week), which does not justify an interruption mode on a cluster used for learning. The control
plane is 76% of the cost; node sizing optimises the small half.

## 2. Decisions

| # | Decision | Rationale |
|---|---|---|
| 1 | **Workload: app + Prometheus only** | Grafana proves nothing new — if Prometheus holds the data, Grafana only draws it. And a click-built dashboard lives inside the container, so it vanishes on teardown; surviving means exporting it to JSON and committing it. That export is trickier than it sounds — the JSON references Prometheus by an ID that differs in a fresh Grafana, breaking every panel until it's pinned. Already a separate roadmap item ("dashboards as code") |
| 2 | **Prometheus is told exactly what to scrape — no discovery.** `prometheus.yml` keeps `targets: ["app:9464"]`, unchanged from today | In Kubernetes pod addresses change on every restart, so a hand-written scrape list would go stale — that is what discovery is for. But we scrape the **Service name** `app`, which stays valid however often the pod behind it is replaced, so the list never goes stale. Discovery's other use is scraping many pods individually; there is one. `kube-prometheus-stack` also complicates teardown: its PersistentVolumeClaims (PVCs) make Kubernetes provision Elastic Block Store (EBS) volumes Terraform never sees, so `destroy` leaves them behind, still billing |
| 3 | **Build and iterate as `hagop-admin`; adopt a hand-written deployer policy; tighten reactively from CloudTrail only when a run fails** | Admin never gets `AccessDenied`, so iteration is fast; the adopted policy (§7c) is already tighter than a generated one, so an upfront derivation exercise is not worth ~3 hours |
| 4 | **Image built and pushed by hand from the host** | The CI pipeline is a later milestone and needs the OpenID Connect (OIDC) trust policy narrowed first |
| 5 | **Raw Terraform resources, no `terraform-aws-modules`** | The module's value concentrates in what we cut — IAM Roles for Service Accounts (IRSA), addons, NAT, autoscaling — and its extra `Describe*`/tagging calls would inflate any evidence-derived policy into an `eks:*` wildcard |
| 6 | **Secrets via `kubectl create secret --from-env-file=.env`** | The key never enters Terraform state or git. AWS Systems Manager (SSM) Parameter Store + External Secrets needs IRSA and Custom Resource Definitions (CRDs), both rejected |
| 7 | **Access via `kubectl port-forward` only** | A residential Xfinity IP is not static, so an IP-scoped security group means editing Terraform mid-session — and the unblock pressure leads to `0.0.0.0/0` on an unauthenticated Prometheus |
| 8 | **On-demand instance, one replica, namespace `default`** | Spot instances save ~$0.058/hr (~$8/year here) but AWS can reclaim the node on two minutes' notice — not worth an extra failure mode on a cluster used for learning. One replica is *required* by decision 2, since scraping through a Service breaks with more than one. A dedicated namespace isolates tenants; there is one tenant, so it would only add `-n` to every command |

**Boundary condition on decision 2:** the static scrape target is correct **only at `replicas: 1`**.
Scraping through a load-balanced Service with more than one replica hits a random pod per scrape,
so counters jump backwards and rates are meaningless. Scaling past one replica requires service
discovery or a headless Service.

## 3. Architecture

```
   YOUR LAPTOP                                    persistent, survives destroy
  ┌──────────────┐              ┌──────────────────────────────────────────┐
  │ terraform    │              │  terraform/bootstrap/  (admin, local st.)│
  │ kubectl      │              │   • ECR repo  platform-lab  (keep 3)     │
  │ docker       │──push image──┼──►• IAM: cluster role, node role         │
  └──────┬───────┘              │   • deployer role + boundary + OIDC      │
         │                      │   • budget alert  $20 forecasted         │
         │ kubectl              └──────────────────────────────────────────┘
         │ port-forward
         │ (HTTPS 443, IAM auth)
         ▼
  ╔═══════════════════════════════════════════════════════════════════════╗
  ║  EKS CONTROL PLANE   $0.10/hr   AWS-managed, outside your VPC         ║
  ║  api server · etcd · scheduler · controllers                          ║
  ╚═══════════════╤═══════════════════════════════════════════════════════╝
                  │ manages
  ┌───────────────┼───────────────────────────────────────────────────────┐
  │ VPC  10.0.0.0/16                        terraform/eks/ — ALL EPHEMERAL│
  │               │                                                       │
  │  ┌────────────┼──────────────────────┐  ┌─────────────────────────┐   │
  │  │ subnet 10.0.1.0/24   us-west-2a   │  │ subnet 10.0.2.0/24      │   │
  │  │                                   │  │ us-west-2b              │   │
  │  │  ┌─────────────────────────────┐  │  │                         │   │
  │  │  │ NODE  t3.large  on-demand   │  │  │   (empty — EKS requires │   │
  │  │  │ $0.083/hr + public IP       │  │  │    2 AZs even for       │   │
  │  │  │                             │  │  │    one node)            │   │
  │  │  │  ┌───────────────────────┐  │  │  └─────────────────────────┘   │
  │  │  │  │ pod: app              │  │  │                                │
  │  │  │  │  :8000 FastAPI        │◄─┼──┼──── svc/app      (ClusterIP)   │
  │  │  │  │  :9464 OTel metrics   │  │  │      DNS: "app"                │
  │  │  │  └──────────▲────────────┘  │  │                                │
  │  │  │             │ scrape 5s     │  │                                │
  │  │  │  ┌──────────┴────────────┐  │  │                                │
  │  │  │  │ pod: prometheus       │◄─┼──┼──── svc/prometheus (ClusterIP) │
  │  │  │  │  :9090  emptyDir      │  │  │      DNS: "prometheus"         │
  │  │  │  └───────────────────────┘  │  │                                │
  │  │  └─────────────┬───────────────┘  │                                │
  │  └────────────────┼──────────────────┘                                │
  │                   │ 0.0.0.0/0                                         │
  │              ┌────▼─────┐                                             │
  │              │   IGW    │  free, 1:1 NAT                              │
  │              └────┬─────┘                                             │
  └───────────────────┼───────────────────────────────────────────────────┘
                      │
              ┌───────┴────────┐
              ▼                ▼
        ECR (image pull)   Groq API  (LLM calls from the app)
```

- **`terraform destroy` deletes only the VPC box.** Everything in the persistent box survives.
- **The control plane sits outside the VPC.** AWS runs it; this is what the $0.10/hr buys, and why
  it costs the same with zero nodes.
- **`port-forward` reaches the control plane, not the node.** No inbound port is opened on the VPC
  and no security-group ingress rule exists.
- **Services are virtual** — a DNS name plus routing rules the kubelet programs on the node.
- **The second subnet stays empty**, existing only because EKS refuses a cluster whose subnets span
  fewer than two availability zones.

## 4. Layout

The design has three layers:

| Layer | Directory | The question it answers |
|---|---|---|
| **Bootstrap** | `terraform/bootstrap/` | What must exist before anything can be deployed at all? |
| **Infrastructure** | `terraform/eks/` | What does the application run *on*? |
| **Workload** | `k8s/` | What actually runs? |

"Bootstrap" creates the identities and
state backend everything else depends on — it cannot be managed by the thing it creates. Bootstrap
is technically infrastructure too, so the distinction between the first two layers is not *what
kind of thing* they are but *when they exist*: bootstrap is the permanent prerequisite, and
infrastructure is the disposable runtime built on top of it each session. **"Workload"** is
Kubernetes' own category name for the object types that create and manage pods — Deployment,
StatefulSet, DaemonSet, Job. `k8s/` holds only those, and deliberately not the application itself:
the source and the container image live at the repo root.

**Dependencies run strictly one way: bootstrap → infrastructure → workload.** Each layer consumes
outputs from the one above it — role ARNs and an ECR URL, then a cluster endpoint — and never the
reverse. That one-way flow is what lets the middle layer be destroyed nightly without disturbing
either neighbour.

The layers are separated by **lifetime**, and that split defines blast radius: `terraform destroy`
deletes everything in one state file, so the boundary decides what dies together. Implemented:

| Layer / directory | What it manages | Tool | State | Applied by | Lifetime |
|---|---|---|---|---|---|
| **Bootstrap**<br>`terraform/bootstrap/` *(exists)* | **Identities and things that must outlive the cluster** — IAM roles (deployer, cluster, node), the deployer's identity policy and permissions boundary, the OIDC provider, the ECR repo and its lifecycle policy, the budget alert | Terraform | Local, on host | `hagop-admin` only | **Permanent** |
| **Infrastructure**<br>`terraform/eks/` *(new)* | **Everything that costs money** — VPC, 2 subnets, internet gateway, route tables, the EKS cluster (control plane), the managed node group and its EC2 instance, the cluster access entry | Terraform | S3 + lock | admin → then deployer | **Ephemeral** |
| **Workload**<br>`k8s/` *(new)* | **Kubernetes objects only, nothing in AWS** — 2 Deployments, 2 Services, 1 ConfigMap (the Secret is created by hand, not from a file) | `kubectl` | **None** | per session | Dies with the cluster |

**ECR repo and both EKS roles live in bootstrap.** If `destroy` deleted the repo, every spin-up
would begin with a 15-minute rebuild and 2 GB push. Keeping the roles there is what lets the
deployer avoid `iam:CreateRole` — a privilege-escalation primitive — entirely.

**The workload is plain YAML, not Terraform's `kubernetes` provider.** That provider must
initialise before Terraform can plan anything, and it initialises from the cluster endpoint. Once
the cluster is gone or half-gone, `terraform destroy` cannot even *plan*, and recovery is manual
`terraform state rm` on each object. Separate directories make this structurally impossible:
`terraform/eks/` has exactly one provider, `aws`, which always initialises.

**`k8s/` needs no teardown.** Kubernetes objects are rows in the cluster's etcd, not AWS resources;
they evaporate with the cluster.

### The invariant

> **Everything that costs money lives in Terraform's state file.**

This holds only because `type: LoadBalancer` Services and PersistentVolumeClaims are excluded — the
two object kinds that create real AWS resources from inside the cluster. A LoadBalancer Service's
Elastic Load Balancer (ELB) is created by the Kubernetes control plane, is unknown to Terraform,
survives cluster deletion, and its leftover Elastic Network Interfaces (ENIs) block VPC deletion. A
PVC orphans an EBS volume the same way. **Adding either later breaks the invariant, and teardown
then requires a `kubectl delete` step first.**

## 5. Cluster stack (`terraform/eks/`)

| File | Resources |
|---|---|
| `main.tf` | `terraform{}` + S3 backend + `provider "aws"` (us-west-2) |
| `vpc.tf` | `aws_vpc`, 2× `aws_subnet`, `aws_internet_gateway`, `aws_route_table`, 2× association |
| `eks.tf` | `aws_eks_cluster`, `aws_eks_node_group`, `aws_eks_access_entry`, `aws_eks_access_policy_association` |
| `variables.tf` | cluster name, instance type, both role Amazon Resource Names (ARNs) from bootstrap, admin principal ARN |
| `outputs.tf` | cluster name, endpoint, ready-to-paste `update-kubeconfig` command |

**Values:** VPC `10.0.0.0/16` with **`enable_dns_support = true` and `enable_dns_hostnames = true`**;
subnets `10.0.1.0/24` (us-west-2a) and `10.0.2.0/24` (us-west-2b), both public with
`map_public_ip_on_launch = true`; default route to the IGW; cluster endpoint **public *and* private
access both enabled**; control-plane logging off; no KMS envelope encryption; node group
1× `t3.large`, `min = max = desired = 1`, 20 GiB gp3, `AL2023_x86_64_STANDARD`.

**Both access modes are enabled for the cluster's single endpoint, and this is free.** There is one
API server with one hostname; the two settings control only how that hostname *resolves*. With
public access alone, nodes resolve it to a *public* address, so their control-plane traffic exits
through the IGW, crosses the internet, and re-enters AWS. Enabling private access makes EKS publish
a private DNS record for the same hostname inside the VPC, so nodes resolve it to a private address
and reach the control plane over the cross-account network interfaces EKS already places in the
subnets — never leaving the VPC. Public access stays on so `kubectl` still works from the host:
outside the VPC the hostname resolves publicly, inside it resolves privately.

> **`enable_dns_hostnames` must be set explicitly.** It defaults to `false` on a non-default VPC
> (unlike `enable_dns_support`, which defaults to true). Without it, private endpoint resolution
> silently fails and nodes cannot reach the control plane.

**The node's public IP is for outbound only** — pulling the image from ECR and letting the app call
the Groq API. Nothing ever connects inward. It is auto-assigned from an AWS pool at launch and
released on termination, so **every recreate yields a different address**, as does any node
replacement mid-session. That is deliberate and costs nothing: no Elastic IP is used, because an
Elastic IP bills $0.005/hr *even while unassociated* — roughly $3.60/month standing for an address
used a few hours a week. Nothing in this design references the node by address, since access is
`port-forward` through the API server.

**No NAT gateway.** Nodes have public IPs, so the IGW performs the 1:1 translation for free. NAT
would cost ~$0.045/hr plus $0.045/GB and — more importantly — NAT gateways and their Elastic IPs
are among the most common causes of a `terraform destroy` that hangs on "VPC has dependencies."
Adding the two resources most associated with stuck teardowns, to a design whose premise is
reliable teardown, is the wrong trade. Inbound is blocked by the security group rather than by
routing, which is defensible because `port-forward` tunnels through the API server, so no port is
ever meant to be open. Note EKS creates and manages a cluster security group itself, carrying the
rules control plane and nodes need to talk to each other; **this design adds no ingress rules of
its own**, and none permitting traffic from the internet exist.

**Cluster access is explicit, not inherited.** `authentication_mode = "API"` with
`bootstrap_cluster_creator_admin_permissions = false`, plus an `aws_eks_access_entry` naming the
`hagop-admin` principal with `AmazonEKSClusterAdminPolicy`.

> **Gotcha this solves:** EKS grants cluster-admin `kubectl` access to whichever principal created
> the cluster. That is admin in Phase 1 but the *deployer* in the verification run — so without an
> explicit entry, `kubectl get nodes` would return `You must be logged in to the server` after a
> deployer-created cluster. The explicit entry makes access deterministic regardless of who applied.

**The classic raw-EKS `depends_on` hazard does not apply.** Node groups created before their role's
policy attachments exist produce nodes that never join the cluster — but that can only happen when
roles and node group are in the same apply. Ours are in different stacks applied at different times.

**Pin the Kubernetes version explicitly: `version = "1.36"`** so a cluster recreated in three months
matches today's. 1.36 is the latest available and stays in standard support the longest (until
2027-08-01) — older versions drop into extended support, which costs ~6× for the control plane.

## 6. Workload manifests (`k8s/`)

| File | Object | Key contents |
|---|---|---|
| `app-deployment.yaml` | Deployment | `replicas: 1`, ECR image tagged with git short SHA, env + `envFrom` secret, ports 8000/9464, three probes |
| `app-service.yaml` | Service (ClusterIP) | Named **`app`**, ports 8000 and 9464 |
| `prometheus-configmap.yaml` | ConfigMap | `prometheus.yml` verbatim |
| `prometheus-deployment.yaml` | Deployment | `prom/prometheus`, ConfigMap at `/etc/prometheus`, `emptyDir` data |
| `prometheus-service.yaml` | Service (ClusterIP) | Named **`prometheus`**, port 9090 |

### Service names are load-bearing

Kubernetes Service DNS matches Docker Compose service DNS. Naming the Services `app` and
`prometheus` means:

- `prometheus.yml`'s `targets: ["app:9464"]` — **unchanged**
- `metrics_analysis.py`'s default `http://prometheus:9090` — **unchanged**

No application code or config changes are needed to run on Kubernetes. `PROMETHEUS_URL` is
deliberately **not** set in the Deployment so the code default applies.

### A `startupProbe` is required, not optional

`main.py` imports `rag.router`, which imports `rag/tpr_rag.py`, which constructs
`SentenceTransformer` and the ChromaDB client **at module level** — so the app takes seconds to
tens of seconds before it can serve. With only a liveness probe, Kubernetes kills the container
mid-startup, forever, in a crash loop that reads as an application bug.

```yaml
startupProbe:   { httpGet: {path: /health, port: 8000}, periodSeconds: 5,  failureThreshold: 30 }
livenessProbe:  { httpGet: {path: /health, port: 8000}, periodSeconds: 10, failureThreshold: 3  }
readinessProbe: { httpGet: {path: /health, port: 8000}, periodSeconds: 5 }
```

150s startup budget, then ~30s detection if it wedges later. Both fit inside the default
`progressDeadlineSeconds` of 600s.

**Eager loading is the right design and should not be changed for the platform's convenience.** It
fails fast — a missing index or embedding-model mismatch (`tpr_rag.py` raises explicitly on the
latter) surfaces as `CrashLoopBackOff` within 30 seconds of deploy rather than on a user's first
RAG request. It also keeps "Ready" meaning "can serve every route."

### Other choices

- **Resources:** app `requests: 1Gi / 500m`, `limits: 2Gi`; Prometheus `requests: 256Mi`,
  `limits: 512Mi`. A `t3.large` has ~7 GiB allocatable, leaving room for the brief two-pod overlap
  during a rolling update (`maxSurge` rounds to 1, `maxUnavailable` to 0 at one replica).
- **Image tagged with the git short SHA, never `latest`.** With `latest` the pod template never
  changes, so its hash never changes, so no new ReplicaSet is created and nothing rolls — and there
  is no revision history to roll back to.
- **`emptyDir` for Prometheus data**, not a PVC. A teardown decision, not frugality.

**Excluded:** no PVC, no LoadBalancer, no Ingress, no HorizontalPodAutoscaler (HPA), no
NetworkPolicy, no custom ServiceAccount, no namespace beyond `default`.

### Known wart

`prometheus.yml` will exist twice — at the repo root for Compose, and inside
`k8s/prometheus-configmap.yaml` for Kubernetes. They can drift. Kustomize's `configMapGenerator`
generates the ConfigMap from the real file and would remove the duplication; that is a natural
follow-up milestone and consumes exactly the plain YAML written here.

### Runbook consequence

The Secret and ConfigMap are referenced *by name*, so their contents are not part of the pod
template hash. Changing either does **not** trigger a rollout, and running pods keep the old values
indefinitely. After changing either:

```bash
kubectl rollout restart deployment/app
kubectl rollout restart deployment/prometheus
```

## 7. Bootstrap additions and IAM

### 7a. Added in Phase 0 (applied by `hagop-admin`)

| Resource | Config |
|---|---|
| `aws_ecr_repository` | `platform-lab`, `image_tag_mutability = IMMUTABLE`, scan-on-push |
| `aws_ecr_lifecycle_policy` | Keep the last **3** images, expire the rest |
| `aws_iam_role` **cluster** | Trusted by `eks.amazonaws.com`; `AmazonEKSClusterPolicy` |
| `aws_iam_role` **node** | Trusted by `ec2.amazonaws.com`; `AmazonEKSWorkerNodePolicy`, `AmazonEKS_CNI_Policy`, `AmazonEC2ContainerRegistryReadOnly` |
| `aws_iam_policy` **boundary** + `permissions_boundary` on the deployer role | §7b |
| Rewritten deployer identity policy | §7c |
| `max_session_duration = 14400` on the deployer role | §8a |
| `aws_budgets_budget` | $20, `FORECASTED`, 80% threshold, email subscriber |
| Outputs | ECR repo URL, both role ARNs — consumed as `terraform/eks/` variables |

`AmazonEC2ContainerRegistryReadOnly` on the node role is what makes ECR pulls work with no
`imagePullSecret`.

### 7b. Permissions boundary — written by hand, from intent

A boundary sets the **maximum** permissions a role can have; effective permissions are the
intersection of identity policy and boundary. It grants nothing on its own — a role with only a
boundary can do nothing. It is written by hand because it is a design decision about what should
never be possible; there is nothing to observe.

> **Identity policy = what the role does today. Boundary = what it could ever do, however the
> policy changes later.** Add `s3:*` to the identity policy tomorrow and it still will not work
> unless the boundary allows S3 too.

**Allow (broad ceiling):** `eks:*`, `ec2:*`, `autoscaling:*`, `ecr:*`, `logs:*`, `cloudwatch:*`;
`s3:*` on the state bucket only; `dynamodb:*` on the lock table only; `iam:PassRole`,
`iam:GetRole`, `iam:ListRole*`, `iam:ListAttachedRolePolicies`.

**Explicit deny — wins over any allow, now or later:**

| Denied | Why |
|---|---|
| `iam:CreateRole`, `DeleteRole`, `PutRolePolicy`, `AttachRolePolicy`, `DetachRolePolicy`, `UpdateAssumeRolePolicy`, `CreateUser`, `CreatePolicyVersion` | The privilege-escalation set |
| **`iam:PutRolePermissionsBoundary`, `iam:DeleteRolePermissionsBoundary`** | **Critical** — without this the role could lift its own ceiling and the boundary is theatre |
| `cloudtrail:StopLogging`, `DeleteTrail`, `PutEventSelectors` | Protects the audit log, which is also the reactive-tightening evidence |
| `s3:DeleteBucket` | The state bucket must not be deletable by the thing storing state in it |
| `organizations:*`, `account:*` | Nothing here should touch account-level config |

**Region lock:** deny when `aws:RequestedRegion != us-west-2`, with
`NotAction: iam:*, sts:*, route53:*, cloudfront:*, organizations:*`. **The carve-out is
mandatory** — global services report no region or `us-east-1`, so a naive region condition breaks
every IAM and STS call, including the role assumption itself.

Three properties:

- **The boundary must be a superset of the identity policy**, or calls fail with `AccessDenied`
  that looks like an identity-policy bug. Broad allows, narrow denies.
- **`hagop-admin` is unaffected** — the boundary attaches only to the deployer role, so there is no
  lockout risk.
- **The cluster and node roles get no boundary** — they carry only tightly-scoped AWS-managed
  policies and are assumed by AWS services, not by a human.

**The boundary carries the security load in this design.** It is written before any cluster exists
and holds regardless of how the identity policy later evolves — including the realistic failure
mode of pasting `"Action": "*"` at 11pm to stop an `AccessDenied` loop on a billing cluster.

### 7c. Deployer identity policy — adopted, not derived

This replaces the current `deployer_permissions` document.

| Sid | Grants | Scope |
|---|---|---|
| `TerraformState` | Read/write the state file | That one bucket |
| `TerraformLock` | Take and release the state lock | That one table |
| `EKSRead` | List and describe clusters | Account-wide (no resource-level support) |
| `EKSWrite` | Create/delete/update cluster, node group, access entries | **Only resources named `platform-lab`** |
| `EC2Networking` | Build and tear down VPC, subnets, routing, security groups, launch templates | Region-wide — see below |
| `PassClusterAndNodeRoles` | Hand the two bootstrap roles to AWS services | **Two exact ARNs, two exact services** |
| `ReadOwnRoles` | Read those roles so Terraform can diff them | Same two ARNs |
| `ServiceLinkedRoles` | Let AWS create its own internal roles | **Only for three named services** |
| `AutoScalingRead` | Read and tag the Auto Scaling group (ASG) the node group creates | Account-wide, reads and tags only |

**Removed from today's policy:** `ec2:RunInstances` and `ec2:TerminateInstances` (managed node
groups launch instances under EKS's service-linked role, not this one); the entire `ECRAccess`
block (the repo is created in bootstrap and images are pushed from the host as admin — restore it
when CI pushes); `CloudWatchAccess` (control-plane logging is off and nothing here writes metrics).

**Added:** scoped `eks:*`, `iam:PassRole`, `iam:CreateServiceLinkedRole`, launch templates,
`ec2:DeleteTags`, autoscaling reads.

Two statements deserve attention:

- **`PassClusterAndNodeRoles` is the strongest.** `PassRole` is how a role hands an identity to an
  AWS service — the classic escalation vector when left open. Here it is double-locked: two exact
  role ARNs *and* an `iam:PassedToService` condition naming two services. It cannot be repurposed.
- **`EC2Networking` is `Resource: "*"` and unavoidably so.** `ec2:Describe*` has no resource-level
  support in AWS at all, and `CreateVpc`/`CreateSubnet` have no pre-existing resource to name.
  Containment comes from the boundary's region lock, not from this statement. **This must be stated
  in a code comment** so it reads as a known limit rather than an oversight.

**Honest exposure assessment.** The most useful review lens is not "is each action needed" but
*what is the worst thing this role could do if someone else obtained it?* The answer: **delete VPCs
and security groups anywhere in us-west-2**. Everything else is read-only, scoped to
`platform-lab`, or condition-locked. That exposure comes entirely from `EC2Networking`, which is
why the boundary's region lock matters.

### 7d. Reactive tightening

Expect one or two `AccessDenied` failures on the first deployer-run cycle — most likely additional
`ec2:*` verbs the AWS provider calls during refresh, or `eks:*` actions around update polling.
Rather than deriving the policy up front, query CloudTrail **only when a run fails**:

```bash
aws cloudtrail lookup-events --start-time <start> --end-time <end> \
  --query 'Events[].[CloudTrailEvent]' --output text \
  | jq -r 'fromjson | select(.errorCode != null)
           | "\(.errorCode)  \(.eventSource) \(.eventName)"' | sort -u
```

This returns precisely the denied calls and nothing else. Add exactly those, re-run, repeat. Each
iteration costs ~25 minutes and ~$0.10. CloudTrail Event History is on by default, free, and
retains 90 days — no trail, bucket, or setup is required.

**Do not over-tighten.** A policy fitted to one execution path breaks when Terraform takes another
— a replacement instead of an update, a retry, a drift correction. A minimal policy that breaks
every third run is brittleness, not least privilege.

**Considered and dropped:** an upfront CloudTrail harvest and an IAM Access Analyzer comparison
(~3 hours). Both derive a policy from observed activity and would have produced something *broader*
than §7c — Access Analyzer generates no condition keys and emits `"Resource": "*"` for services
without action-level support. It would also have required a trail, an S3 bucket, and an access role
created **before** the first apply.

The asymmetry that makes this safe: **missing grants fail loudly and are fixed reactively; extra
grants are invisible but capped by the boundary.** What is given up is knowledge of whether the
policy is over-broad. The cheap way to recover that later is
`aws iam get-service-last-accessed-details` (free) or IAM Access Analyzer's unused-access findings
(~$0.20/role/month, no trail needed) — both better than an upfront derivation because they reflect
weeks of real use rather than one run.

### 7e. Why bootstrap stays admin-applied, permanently

Three stacked reasons, the third being the general rule:

1. **It cannot work otherwise** — the deployer has no `iam:*`, so applying `iam.tf` as the deployer
   fails on the first API call.
2. **Self-lockout** — a role that manages its own permissions can delete its ability to fix itself;
   recovery needs admin anyway.
3. **Privilege escalation** — if the deployer could edit its own policy, its permissions are not a
   boundary but a suggestion. The trust policy currently accepts `repo:hagop17/platform-lab:*`,
   i.e. **any branch or pull request**, so a workflow-file change in a PR would suffice.

> **The rule: whatever defines a privilege boundary must be managed from outside that boundary.**

**Narrow the `sub` condition** to a specific branch or environment before any workflow assumes the
role.

## 8. Runbook

**Host prerequisites:** `terraform` ≥1.15, `aws` CLI v2, **`kubectl`** (new — add to CLAUDE.md),
`docker`, and a `.env` containing `GROQ_API_KEY`.

No AWS credentials exist in the dev container, so **every command below is a host action**.

### 8a. Two shells, one per role

| | Shell 1 — `hagop-admin` | Shell 2 — `platform-lab-deployer` |
|---|---|---|
| `terraform/bootstrap/` | ✅ once, and on IAM/ECR changes | ❌ never, by design |
| `terraform/eks/` apply & destroy | possible, not the routine | ✅ **the routine, unlimited** |
| `kubectl` | ✅ **only here** | ❌ refused — no access entry |
| Image push to ECR | ✅ | ❌ |

**A full session needs both shells.** Shell 2 runs `terraform apply`, Shell 1 runs `kubectl` and
port-forward, Shell 2 runs `terraform destroy`. This is the design working: the deployer provisions
AWS infrastructure and has no business holding cluster-admin inside Kubernetes.

Shell 1:

```bash
eval "$(aws configure export-credentials --profile hagop-admin --format env)"
```

Shell 2:

```bash
eval "$(aws configure export-credentials --profile hagop-admin --format env)"
eval "$(aws sts assume-role \
  --role-arn arn:aws:iam::<ACCOUNT_ID>:role/platform-lab-deployer \
  --role-session-name platform-lab-deploy \
  --query 'Credentials.[`export AWS_ACCESS_KEY_ID=`||AccessKeyId,
                        `export AWS_SECRET_ACCESS_KEY=`||SecretAccessKey,
                        `export AWS_SESSION_TOKEN=`||SessionToken]' \
  --output text | tr "\t" "\n")"
aws sts get-caller-identity     # must show .../platform-lab-deployer/...
```

A named profile with `source_profile` would be tidier, but `hagop-admin` authenticates via
`aws login`, whose credential type does not resolve as a source — the same constraint
`terraform/bootstrap/iam.tf`'s header already documents for the provider block.

> **`max_session_duration = 14400`** (4 hours) is set on the deployer role because assumed-role
> credentials default to one hour. A `destroy` started at minute 55 can fail partway with
> `ExpiredToken`, leaving a half-destroyed cluster still billing — precisely the failure this
> design exists to prevent. Re-run the assume-role block before `destroy` on long sessions.

### 8b. Spin up (~20 min, mostly waiting)

```bash
# Shell 2
cd terraform/eks && terraform apply                      # ~18 min

# Shell 1
aws eks update-kubeconfig --name platform-lab --region us-west-2   # EVERY session — see below
kubectl create secret generic platform-lab-secrets --from-env-file=.env
kubectl apply -f k8s/
kubectl rollout status deployment/app                    # waits out the 150s model load
kubectl port-forward svc/app 8000:8000 &
kubectl port-forward svc/prometheus 9090:9090 &
```

> **`update-kubeconfig` runs after every `apply`, not just the first.** A recreated cluster gets a
> new API endpoint hostname and new certificate data, so the kubeconfig entry from the previous
> session points at a cluster that no longer exists. Skipping it produces a connection or TLS
> failure that reads as though the *new* cluster is broken, rather than as stale local config.

### 8c. Verify

```bash
kubectl get nodes                       # 1 node, Ready
kubectl get pods                        # 2 pods, Running, 0 restarts
curl localhost:8000/health
for i in {1..5}; do curl -s localhost:8000/work > /dev/null; done
curl -s 'localhost:9090/api/v1/query?query=up{job="platform-lab"}'   # expect 1
```

The `/work` loop matters for the reason CLAUDE.md already documents for Compose: Prometheus only
has data if traffic happened. Hitting a route does not trigger a scrape.

### 8d. Tear down

```bash
kill %1 %2                                # Shell 1: stop port-forwards
cd terraform/eks && terraform destroy      # Shell 2, ~10 min

aws eks list-clusters --region us-west-2   # MUST be empty
aws ec2 describe-instances --region us-west-2 \
  --filters Name=instance-state-name,Values=running \
  --query 'Reservations[].Instances[].InstanceId'
```

**No `kubectl delete`.** The Kubernetes objects die with the cluster, and adding the step would
mean waiting ~30s for graceful pod termination before destroying the thing that would have removed
them anyway. More importantly, keeping it out of the runbook means it is not there later when
someone adds an operator whose CRD finalizers can hang a namespace in `Terminating` indefinitely.

**`aws eks list-clusters` is the real check, not the budget alert.** AWS billing data lags 8–24
hours, so a budget alert catches "forgot for days," never "forgot overnight." The budget exists as
the safety net for sessions where the runbook is skipped entirely.

Terraform reverses the dependency graph: access entry → node group (~3 min, drains and terminates
the instance) → cluster (~5–10 min) → route tables, IGW, subnets, VPC.

## 9. CI

`.github/workflows/ci.yml` already runs `terraform fmt -check -recursive terraform`, so the new
directory is covered without change.

**Add one step:** `terraform init -backend=false && terraform validate` in `terraform/eks/`. It
catches syntax and type errors offline with no AWS credentials, consistent with the repo's
convention that tests never touch the network.

## 10. Phases

| Phase | Work | Output |
|---|---|---|
| **0 — build** | Write `terraform/bootstrap/` additions and `terraform/eks/`; write `k8s/` manifests; build and push the image; apply bootstrap | Everything exists, nothing has run end to end |
| **1 — first cycle** | apply → verify → destroy, as `hagop-admin` | A cluster that provably works and provably tears down |
| **2 — verify under least privilege** | Full apply → verify → destroy as `platform-lab-deployer`, closing any `AccessDenied` gaps reactively | The milestone is done |

## 11. Definition of done

1. Bootstrap applied: ECR repo (keep 3, immutable tags), cluster role, node role, boundary, new
   identity policy, `max_session_duration`, budget alert
2. Image built and pushed to ECR with a git-SHA tag
3. `terraform apply` as admin produces a working cluster; both pods Running
4. Verification in 8c passes, including live Prometheus series
5. `terraform destroy` completes cleanly; `aws eks list-clusters` returns empty
6. **A full apply → verify → destroy cycle succeeds with Terraform running as
   `platform-lab-deployer`** in the two-shell model, with any `AccessDenied` gaps closed reactively
7. README roadmap bullets updated; `kubectl` added to CLAUDE.md's toolchain section

Item 6 is the real finish line — a cluster working under admin is halfway. It proves the identity
policy and boundary work end to end, which is stronger evidence than any derivation exercise.

## 12. Out of scope

Grafana and dashboards-as-code; the GitHub Actions build-and-push pipeline; NodePort or
LoadBalancer exposure; IRSA and EKS Pod Identity; Kustomize; the OTel Collector roadmap item;
private subnets with NAT; spot instances; multi-replica scaling; IAM Access Analyzer and a
CloudTrail trail.

## 13. To verify at implementation time

- **`use_lockfile` vs `dynamodb_table`** for the S3 backend. Terraform 1.15.8 is in use and
  S3-native locking may have deprecated the DynamoDB table. Existing S3 grants cover it either way.
  **Check; do not assume.** This resolves at the first `terraform init`, which either rejects
  `use_lockfile` as unsupported or warns that `dynamodb_table` is deprecated.

## 14. Prior state confirmed (2026-08-14)

- The applied deployer policy matches `terraform/bootstrap/iam.tf` exactly (state serial 6,
  Terraform 1.15.8). Gaps hold: no `eks:*`, no `iam:*`, no `autoscaling:*`, no
  `ec2:CreateLaunchTemplate`, only `CreateTags` and not `DeleteTags`. VPC/subnet/IGW/route-table
  grants are reusable as-is.
- State backend exists and is live: bucket `platform-lab-tfstate-<ACCOUNT_ID>-us-west-2-an` and
  DynamoDB table `platform-lab-tflock` (ACTIVE).
- Bootstrap deliberately stays on **local** state — it creates the identity everything else uses,
  so putting its state behind a backend those identities protect is circular. Its resilience story
  is the four `terraform import` commands in `iam.tf`'s footer.

## Appendix A — Deployer identity policy, as adopted

Replaces the existing `data "aws_iam_policy_document" "deployer_permissions"` in
`terraform/bootstrap/iam.tf`. Comments are part of the deliverable, not commentary on it.

```hcl
data "aws_iam_policy_document" "deployer_permissions" {

  # Terraform's own bookkeeping. Not AWS infrastructure — just the state
  # file and its lock. Scoped to the exact bucket and table.
  statement {
    sid       = "TerraformState"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket", "s3:DeleteObject"]
    resources = ["arn:aws:s3:::${var.tfstate_bucket_name}",
                 "arn:aws:s3:::${var.tfstate_bucket_name}/*"]
  }

  statement {
    sid       = "TerraformLock"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:DescribeTable"]
    resources = ["arn:aws:dynamodb:us-west-2:${var.account_id}:table/${var.tflock_table_name}"]
  }

  # Reads are separated from writes so the writes can be ARN-scoped.
  # List/Describe can't be — AWS gives them no resource-level support.
  statement {
    sid       = "EKSRead"
    actions   = ["eks:List*", "eks:Describe*"]
    resources = ["*"]
  }

  # Everything destructive, locked to resources named after THIS cluster.
  # This role cannot touch an EKS cluster it didn't create.
  # AccessEntry actions are what grant hagop-admin kubectl access —
  # omit them and you get a cluster you can't talk to.
  statement {
    sid = "EKSWrite"
    actions = [
      "eks:CreateCluster", "eks:DeleteCluster", "eks:UpdateClusterConfig", "eks:UpdateClusterVersion",
      "eks:CreateNodegroup", "eks:DeleteNodegroup", "eks:UpdateNodegroupConfig", "eks:UpdateNodegroupVersion",
      "eks:CreateAccessEntry", "eks:DeleteAccessEntry", "eks:UpdateAccessEntry",
      "eks:AssociateAccessPolicy", "eks:DisassociateAccessPolicy",
      "eks:TagResource", "eks:UntagResource",
    ]
    resources = [
      "arn:aws:eks:us-west-2:${var.account_id}:cluster/${var.cluster_name}",
      "arn:aws:eks:us-west-2:${var.account_id}:nodegroup/${var.cluster_name}/*",
      "arn:aws:eks:us-west-2:${var.account_id}:access-entry/${var.cluster_name}/*",
    ]
  }

  # The weakest statement, and unavoidably so:
  #   - ec2:Describe* has NO resource-level support in AWS at all
  #   - CreateVpc/CreateSubnet have no pre-existing resource to name
  # Containment comes from the permissions boundary's region lock, not
  # from here. Note RunInstances/TerminateInstances are deliberately
  # ABSENT — managed node groups launch instances under EKS's own
  # service-linked role, not under this one.
  statement {
    sid = "EC2Networking"
    actions = [
      "ec2:Describe*",
      "ec2:CreateVpc", "ec2:DeleteVpc", "ec2:ModifyVpcAttribute",
      "ec2:CreateSubnet", "ec2:DeleteSubnet", "ec2:ModifySubnetAttribute",
      "ec2:CreateInternetGateway", "ec2:DeleteInternetGateway",
      "ec2:AttachInternetGateway", "ec2:DetachInternetGateway",
      "ec2:CreateRouteTable", "ec2:DeleteRouteTable", "ec2:CreateRoute", "ec2:DeleteRoute",
      "ec2:AssociateRouteTable", "ec2:DisassociateRouteTable",
      "ec2:CreateSecurityGroup", "ec2:DeleteSecurityGroup",
      "ec2:AuthorizeSecurityGroupIngress", "ec2:AuthorizeSecurityGroupEgress",
      "ec2:RevokeSecurityGroupIngress", "ec2:RevokeSecurityGroupEgress",
      "ec2:CreateLaunchTemplate", "ec2:DeleteLaunchTemplate", "ec2:CreateLaunchTemplateVersion",
      "ec2:CreateTags", "ec2:DeleteTags",
    ]
    resources = ["*"]
  }

  # The strongest statement. PassRole is how a role hands an identity to
  # an AWS service — the classic escalation vector if left open. Here it's
  # double-locked: two exact role ARNs, AND only passable to two services.
  # It cannot be repurposed to give those roles to anything else.
  statement {
    sid       = "PassClusterAndNodeRoles"
    actions   = ["iam:PassRole"]
    resources = [var.eks_cluster_role_arn, var.eks_node_role_arn]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["eks.amazonaws.com", "ec2.amazonaws.com"]
    }
  }

  # Terraform reads these roles to compute a diff. Read-only, same two ARNs.
  statement {
    sid       = "ReadOwnRoles"
    actions   = ["iam:GetRole", "iam:ListAttachedRolePolicies", "iam:ListRolePolicies"]
    resources = [var.eks_cluster_role_arn, var.eks_node_role_arn]
  }

  # AWS services create their own internal roles on first use. Resource must
  # be "*" (the role doesn't exist yet), so the condition does the work —
  # only these three services, nothing else.
  statement {
    sid       = "ServiceLinkedRoles"
    actions   = ["iam:CreateServiceLinkedRole"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "iam:AWSServiceName"
      values   = ["eks.amazonaws.com", "eks-nodegroup.amazonaws.com", "autoscaling.amazonaws.com"]
    }
  }

  # Managed node groups create an Auto Scaling group on your behalf. Terraform
  # reads it during refresh. Reads and tags only — no scaling, no deletion.
  statement {
    sid       = "AutoScalingRead"
    actions   = ["autoscaling:Describe*", "autoscaling:CreateOrUpdateTags", "autoscaling:DeleteTags"]
    resources = ["*"]
  }
}
```

New variables required in `terraform/bootstrap/`: `cluster_name`, `eks_cluster_role_arn`,
`eks_node_role_arn`. The last two are outputs of the roles created in the same stack, so they are
resource references rather than input variables in practice.

## Appendix B — Glossary

### AWS

| Term | Expansion | What it is here |
|---|---|---|
| **ARN** | Amazon Resource Name | The globally unique identifier for an AWS resource. Used to scope policy statements to specific resources |
| **ASG** | Auto Scaling group | A managed group of EC2 instances. EKS creates one on your behalf for the node group |
| **AZ** | Availability Zone | An isolated datacentre within a region. EKS requires subnets in at least two, even for one node |
| **EBS** | Elastic Block Store | Network-attached disk for EC2. A PVC would provision one that Terraform never sees |
| **ECR** | Elastic Container Registry | Where the ~2 GB app image lives. Created in bootstrap so `destroy` cannot delete it |
| **EKS** | Elastic Kubernetes Service | AWS-managed Kubernetes. The $0.10/hr control-plane charge is the whole cost problem |
| **ELB** | Elastic Load Balancer | Created by Kubernetes — not Terraform — for a `type: LoadBalancer` Service. Excluded from this design because it orphans on teardown |
| **ENI** | Elastic Network Interface | A virtual network card. Leftover ENIs are the classic reason a VPC will not delete |
| **IGW** | Internet Gateway | The VPC's door to the internet. Free, one per VPC, and performs 1:1 address translation for instances with public IPs |
| **IRSA** | IAM Roles for Service Accounts | Gives an individual pod its own AWS identity. Out of scope — nothing in this cluster calls the AWS API |
| **OIDC** | OpenID Connect | Token-based identity federation. Lets GitHub Actions assume an AWS role with no stored keys |
| **SSM** | AWS Systems Manager | Its Parameter Store was considered for the Groq key and rejected as needing IRSA and an operator |

### Kubernetes

| Term | Expansion | What it is here |
|---|---|---|
| **CRD** | Custom Resource Definition | Extends the Kubernetes API with new object types. Operators install them; their finalizers can hang deletion, which is why `kubectl delete` stays out of the teardown runbook |
| **HPA** | HorizontalPodAutoscaler | Scales replicas on load. Excluded — this runs one replica |
| **PVC** | PersistentVolumeClaim | A request for persistent storage. Excluded — it would provision an EBS volume outside Terraform's knowledge |
| **ClusterIP** | — | The default Service type: an internal-only virtual IP and DNS name. Both Services here are ClusterIP |
| **emptyDir** | — | Pod-lifetime scratch storage. Used for Prometheus data so nothing persists and nothing orphans |
| **Finalizer** | — | A field blocking an object's deletion until its controller does cleanup. If the controller is gone, the object hangs in `Terminating` forever |

### Other

| Term | Expansion | What it is here |
|---|---|---|
| **CIDR** | Classless Inter-Domain Routing | The `10.0.0.0/16` notation for IP address ranges |
| **NAT** | Network Address Translation | Rewriting addresses in packet headers. The IGW does 1:1; a NAT Gateway does many-to-1 and is deliberately not used |
| **OTel** | OpenTelemetry | The instrumentation the app uses to expose metrics on port 9464 |
| **RAG** | Retrieval-Augmented Generation | The tangible-property-regulations feature whose embedding model makes app startup slow |
