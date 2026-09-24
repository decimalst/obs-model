# IAM roles, Kubernetes access and ownership

Choose the installation ownership first, then review the identities below. The platform does not create the role that runs Terraform, the role that installs Helm releases, or your GitHub federation. Those identities must already exist. A Grafana **Admin** is an application role; an EKS administrator is a Kubernetes authorization; neither grants AWS infrastructure permissions.

This inventory describes the committed Terraform and charts. A configuration preview is a review document, not proof that AWS has granted the permissions. Keep actual role ARNs and environment configuration in your private infrastructure repository.

## What each installation creates

| Installation | IAM roles created by this repository | IAM roles and access that remain external |
| --- | --- | --- |
| New region / EKS | Seven roles by default: cluster, node, EBS CSI, Cluster Autoscaler, Mimir, Tempo and Flink. Eight with CPU profiling / Pyroscope. Subtract one for each supplied workload role. | Terraform runner, Helm installer / EKS administrator, state access, GitHub OIDC provider and workflow roles. |
| Existing EKS with new data services | Three workload roles by default: Mimir, Tempo and Flink; four with profiling. Subtract supplied workload roles. This path is available through `terraform/live/existing-cluster`. | Cluster/node/add-on/autoscaling roles and all EKS access entries. Existing cluster ownership is preserved. |
| Software only | None. All required workload role ARNs and Pod Identity associations must already exist. | All AWS resources and identities, including storage permissions and associations. Helm can still create selected Kubernetes controllers, service accounts and RBAC. |

The counts are explicit `aws_iam_role` resources, per regional installation. They exclude existing roles, Kubernetes RBAC objects and service-linked roles that AWS services may create or reuse as part of their own lifecycle. Terraform does not declare service-linked-role resources here. The IAM roles are account resources even though their workloads and permissions are regional.

The Flink **infrastructure scaffold is always included** in both Terraform paths: bucket, role unless reused, and association when Terraform owns associations. The optional Flink application and its Kubernetes service account default to disabled. Disabling the Flink job does not remove that scaffold. Pyroscope storage and its role/association are created only when `enable_profiling = true`.

## Exact created-role inventory

`<name>` is the Terraform platform name. The checked-in new-region roots use `olly-us-east-1` and `olly-us-west-2`; the existing-cluster root accepts `name`. Every role below uses Terraform `name_prefix`, so AWS role names include a generated suffix. Do not construct an ARN by appending a guessed suffix: use the reviewed plan/state or `platform.role_arns` for the storage roles. The prefix can differ from the EKS cluster name when using an existing cluster.

| Role name prefix | Created in | Trust | Permissions and scope | Kubernetes binding |
| --- | --- | --- | --- | --- |
| `<name>-eks-` | New region only | `eks.amazonaws.com`, `sts:AssumeRole` | AWS managed `AmazonEKSClusterPolicy` for the EKS control plane. | Cluster's `role_arn`; not a pod service account. |
| `<name>-node-` | New region only | `ec2.amazonaws.com`, `sts:AssumeRole` | AWS managed `AmazonEKSWorkerNodePolicy`, `AmazonEC2ContainerRegistryPullOnly`, `AmazonEKS_CNI_Policy`. Shared by all three managed node groups. | EC2 node identity, not an application pod association. |
| `<name>-ebs-` | New region only | Pod Identity trust described below. | AWS managed `service-role/AmazonEBSCSIDriverPolicy`; volume/snapshot management using that policy's resource and tag conditions. | `kube-system/ebs-csi-controller-sa` |
| `<name>-autoscaler-` | New region only | Pod Identity trust described below. | Inline discovery and cluster-tag-scoped scaling policy, listed below. | `observability-system/cluster-autoscaler` |
| `<name>-mimir-` | Either Terraform path, unless reused | Pod Identity trust described below. | Inline S3 policy for only this installation's `mimir-blocks`, `mimir-ruler`, `mimir-alertmanager` buckets. The ruler currently reads file-provisioned rules, but the role still covers its reserved ruler bucket. | `observability/mimir` |
| `<name>-tempo-` | Either Terraform path, unless reused | Pod Identity trust described below. | Inline S3 policy for only this installation's `tempo` bucket. | `observability/tempo` |
| `<name>-flink-` | Either Terraform path, unless reused | Pod Identity trust described below. | Inline S3 policy for only this installation's `flink` bucket: HA state, checkpoints and savepoints. | `observability/flink`, used when the optional job is enabled. |
| `<name>-pyroscope-` | Either Terraform path with profiling, unless reused | Pod Identity trust described below. | Inline S3 policy for only this installation's `pyroscope` bucket. | `observability/pyroscope`; the Alloy profiler sends to Pyroscope and does not receive this role. |

The AWS managed policy **names** are fixed; AWS can update their underlying policy versions. They are not restricted to a specific regional platform by an additional repository policy. Review their current documents when qualifying an account: [cluster policy](https://docs.aws.amazon.com/aws-managed-policy/latest/reference/AmazonEKSClusterPolicy.html), [worker policy](https://docs.aws.amazon.com/aws-managed-policy/latest/reference/AmazonEKSWorkerNodePolicy.html), [ECR pull policy](https://docs.aws.amazon.com/aws-managed-policy/latest/reference/AmazonEC2ContainerRegistryPullOnly.html), [CNI policy](https://docs.aws.amazon.com/aws-managed-policy/latest/reference/AmazonEKS_CNI_Policy.html), [EBS CSI policy](https://docs.aws.amazon.com/aws-managed-policy/latest/reference/AmazonEBSCSIDriverPolicy.html).

The VPC CNI permissions currently live on the **node role**. This repository does not create a separate `aws-node` IAM role. Splitting that role is an explicit cluster networking change, not something the observability installer performs. Existing clusters retain their networking and add-on identity choices; the platform requires a functioning EKS Pod Identity agent for its storage workloads.

Source: [cluster and add-on roles](../terraform/modules/region/eks.tf), [autoscaler role](../terraform/modules/region/autoscaling.tf), [storage roles and policies](../terraform/modules/platform-services/main.tf).

### Storage policy actions

The Terraform-created storage roles have exactly two inline allow statements:

| Actions | Resource scope |
| --- | --- |
| `s3:ListBucket`, `s3:GetBucketLocation` | Each assigned bucket ARN, `arn:aws:s3:::<bucket>`. |
| `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject`, `s3:AbortMultipartUpload`, `s3:ListMultipartUploadParts` | Each assigned bucket's object ARN, `arn:aws:s3:::<bucket>/*`. |

Managed bucket names are `<name>-<account-id>-<region>-<purpose>`. These roles can delete current objects for backend retention/compaction; they are not read-only. They do not receive bucket administration, Secrets Manager, KMS, database, EKS or cross-account assumption permissions. Terraform uses S3 AES256 encryption, so no customer-managed KMS key grant is created. Existing SSE-KMS buckets need separately reviewed key policy and role permissions. Version restoration and version deletion permissions are also absent; the backup/recovery identity is separate.

### Cluster Autoscaler policy actions

The read statement permits these actions on `*`: `autoscaling:DescribeAutoScalingGroups`, `autoscaling:DescribeAutoScalingInstances`, `autoscaling:DescribeLaunchConfigurations`, `autoscaling:DescribeScalingActivities`, `autoscaling:DescribeTags`, `ec2:DescribeInstanceTypes`, `ec2:DescribeImages`, `ec2:DescribeLaunchTemplateVersions`, `ec2:GetInstanceTypesFromInstanceRequirements`, `eks:DescribeNodegroup`.

The write statement permits `autoscaling:SetDesiredCapacity` and `autoscaling:TerminateInstanceInAutoScalingGroup` on `*` **only** when `aws:ResourceTag/k8s.io/cluster-autoscaler/<name>` equals `owned`. Terraform adds this ownership tag and the discovery tag to its node-group Auto Scaling Groups. Restrict who can change those tags. No additional region condition is present in this inline policy. Existing-cluster installations do not create or change this role and default to externally owned capacity management.

### Pod Identity trust and association

Created EBS, autoscaler and storage roles trust the service principal `pods.eks.amazonaws.com` for `sts:AssumeRole` and `sts:TagSession`, with `StringEquals` on `aws:SourceAccount` for the Terraform account. **The current generated trust is account-scoped; it does not contain a cluster ARN, namespace or service-account condition.** The EKS association binds the chosen cluster, namespace and service account in the inventory above. Restrict association-management permissions and `iam:PassRole` to the intended roles.

An existing role owner can add compatible session-tag conditions for the selected cluster, namespace and service account as a separate defense. Do not disable the session tags such a policy depends on. AWS documents the trust actions and namespace/service-account conditions in [Pod Identity role trust](https://docs.aws.amazon.com/eks/latest/userguide/pod-id-role.html). The platform does not modify a supplied role's trust.

The direct association role must be in the cluster's account. AWS supports a separate target-role chain for cross-account access, but this repository does not generate that configuration and it is not a shortcut for cross-region telemetry replication. See [AWS target-role behavior](https://docs.aws.amazon.com/eks/latest/userguide/pod-id-assign-target-role.html).

## Identities you supply

| Identity | What it does | Setup / ownership |
| --- | --- | --- |
| Terraform provisioning principal | Creates or changes only the resources selected by the reviewed Terraform root, including IAM and associations it owns. | Your platform/IAM team supplies federated credentials or a role. Checked-in new-region roots use the normal AWS credential chain; studio-generated roots also support `iam.terraformRoleArn` as a provider `assume_role`. The existing-cluster root supports `terraform_role_arn`. No provisioning role or general-purpose administrator policy is generated. |
| Terraform backend principal | Reads/writes regional state and S3 lockfiles in an existing encrypted state bucket. | Bootstrap externally. Restrict to the selected state/lock keys and necessary bucket listing; grant KMS access only if your backend requires it. Provider `assume_role` does not configure the separate Terraform backend. State contains sensitive values. |
| EKS installer / administrator | Connects to the Kubernetes API and installs the selected controllers, CRDs, namespaces, RBAC, charts and schema job. | New-region `admin_principal_arn` must refer to an existing IAM **role**. Terraform creates a `STANDARD` EKS access entry and attaches EKS `AmazonEKSClusterAdminPolicy` at cluster scope. It does not create the role or add AWS infrastructure permissions to it. Existing-cluster/software-only access remains external. |
| Secret bootstrap principal | If `secret_source: aws`, fetches the RDS and quota secret values, writes the four core Kubernetes Secrets and preserves existing generated Grafana/ClickHouse passwords. | Usually the private installer runner, with `secretsmanager:GetSecretValue` limited to the two configured secret ARNs; add `kms:Decrypt` only for relevant customer-managed keys. Runtime pods do not inherit this permission. With `secret_source: existing`, the secret owner seeds Kubernetes Secrets and this AWS read path is unused. |
| GitHub debug operations principal | Assumes the configured workflow role and submits bounded debug grants to approved workloads. | Existing GitHub OIDC provider/role, `OBSERVABILITY_OPERATIONS_ROLE` repository/environment variable, protected `observability-operations` environment and private runner. Scope AWS `eks:DescribeCluster` and Kubernetes access to intended clusters/namespaces. The role is not created by this repository. |
| GitOps controller identity | Reconciles the reviewed Kubernetes releases and provisioned content. | Existing Flux installation and Kubernetes permissions; repository authentication and any OCI/image credentials remain owner-managed. The platform does not give Flux a general AWS provisioning role. |
| Recovery / qualification operator | Performs the separately reviewed backup, restore and failover work. | Explicit account-specific identity and isolated restore targets. The qualification planner grants no permissions and performs no cloud actions. |

`AmazonEKSClusterAdminPolicy` is an **EKS access policy**, not an IAM managed policy. AWS API permissions and Kubernetes authorization are separate; `iam:PassRole` also does not let a human log in as that role. See [EKS access-policy semantics](https://docs.aws.amazon.com/eks/latest/userguide/access-policies.html). New clusters disable implicit creator administrator access, so running Terraform successfully does not automatically make that caller a Kubernetes administrator.

The new-region endpoint is private-only. The installer needs a runner inside the VPC or a connected network with DNS, routing and security-group access to the endpoint; credentials alone do not provide a network route. See [EKS private endpoint access](https://docs.aws.amazon.com/eks/latest/userguide/cluster-endpoint.html). The debug workflow explicitly selects a self-hosted `observability-private` runner. The ordinary CI validation workflow does not assume an AWS role or deploy infrastructure.

## Reusing workload roles: owner handoff

1. Select the installation path and record the AWS account, region, cluster, platform name, bucket names and optional profiling choice. Match the namespace/service-account names in the inventory; they are not freely renamed by the studio.
2. For a Terraform path, supply any subset of `existing_role_arns = { mimir = "...", tempo = "...", flink = "..." }`, plus `pyroscope` when profiling is enabled. A supplied ARN means **external ownership**: Terraform creates no role and attaches no policy for that key. Omitted roles are created. Software-only configuration supplies the active runtime roles in `role_arns`; its buckets and roles remain external.
3. Have the role owner review the Pod Identity trust, account match and exact storage policy. The module exposes `required_role_policies` for this purpose. It is a map of permission documents, not evidence they have been attached. In the existing-cluster root it is a top-level output; in a new-region root it is nested under `platform.required_role_policies`. These documents do not include trust policies. Before apply, bucket ARNs can be reviewed in the saved Terraform plan; after the authorized apply, export the nonsecret output for the owner. Keep reuse of a broad role or one shared with unrelated services an explicit decision.
4. Choose association ownership independently from role ownership. With Terraform's default `manage_pod_identity_associations = true`, it creates the associations even for reused roles. The existing-cluster root permits `false` only when **all three** base roles, and `pyroscope` when enabled, are supplied; the cluster owner must already own matching associations. The new-region root manages its associations. Software-only creates none.
5. The principal that creates an association needs the appropriate EKS association actions and `iam:PassRole` on that **specific** role. Supplying its ARN does not grant those permissions. AWS explains the association prerequisite and confirms that Pod Identity does not use IRSA service-account annotations in [association setup](https://docs.aws.amazon.com/eks/latest/userguide/pod-id-association.html).
6. Keep Terraform-provider identity, AWS CLI identity and kubeconfig authentication aligned deliberately. The existing-cluster root's `terraform_role_arn` and studio request's `iam.terraformRoleArn` apply only to their AWS provider; they do not make `install.py`, `aws eks update-kubeconfig` or `kubectl` assume the same role. They also do not configure the separate Terraform backend. Set the chosen local federated profile or protected CI session for each stage, and arrange EKS access separately.
7. Review the generated files offline with `install.py --render-only`. Later, from the authorized private runner, `--check-only` reads AWS/Kubernetes state and renders without deployment. Record effective pod credentials, intended bucket access and denied access to unrelated buckets in the [deferred AWS qualification batch](live-qualification.md).

For an existing live stack, changing a managed role to a supplied ARN or disabling managed associations is a **state/ownership migration**. Do not simply toggle the setting: Terraform may otherwise destroy an object it still owns. Preserve the committed `moved` blocks and review an explicit state transfer. Neither the studio nor installer adopts existing IAM resources.

## Runtime identities that are not IAM roles

Grafana has no AWS role or Pod Identity association in this platform. It reads PostgreSQL and ClickHouse credentials from Kubernetes Secrets, queries regional backends, and optionally maps IdP groups to Grafana Viewer/Editor/Admin. The SSO client Secret, shared Grafana encryption-key Secret, ingress TLS Secret and any external proxy/mesh identity are existing-owner prerequisites. They are separate from AWS IAM, and are not created by the security profile.

Vector, OTel collectors, Kafka consumers, ClickHouse/Keeper, the quota service, support probes and the debug cleanup controller receive no application IAM role from these Terraform modules. Kafka, Valkey and ClickHouse authentication are not automatically AWS IAM authentication. The platform's Kubernetes identities include:

| Service account / role | Kubernetes authorization |
| --- | --- |
| `observability/vector-agent` | Cluster read/watch of pods, namespaces and nodes for log metadata. |
| `observability/otel-agent` | Read `nodes/stats` for kubelet metrics. |
| `observability/otel-scrape` | Cluster read/watch of pods, nodes, namespaces, services, endpoints and EndpointSlices for discovery. |
| `observability/alloy-profiles` when enabled | Read/watch pods in explicitly selected namespaces; privileged profiling remains a separate opt-in. |
| `observability/flink` when enabled | Namespace operations on pods, ConfigMaps and Services for Flink HA/job management, in addition to its separate S3 IAM role. |
| `debug-grants` in an application namespace | Optional cleanup chart reads workloads/ReplicaSets and gets/lists/patches pods in that namespace. It is not the GitHub operations IAM role. |
| Selected operators and backend subcharts | Their rendered service accounts, Roles/ClusterRoles and bindings. Reused controllers retain external ownership. Review the full rendered operator and platform manifests; this table does not replace their RBAC inventory. |

For the protected debug workflow, the external operations identity needs a separate namespace Role/RoleBinding (or equivalent approved EKS authorization): get/patch the permitted Deployment/StatefulSet/DaemonSet, list/read its ReplicaSets and pods, and patch owned pods. The cleanup chart intentionally lacks permission to grant debug on workloads and cannot substitute for this identity. Restrict workload names where feasible. Kubernetes list/patch permissions alone cannot express the catalog's service ownership or an annotation-only patch boundary; protected workflow identity, exact environment/owner checks and restricted access to the runner remain necessary.

For GitHub OIDC, use `sts:AssumeRoleWithWebIdentity`, the `sts.amazonaws.com` audience and a trust subject restricted to the intended repository and protected environment (`repo:<owner>/<repo>:environment:observability-operations` for this workflow). Protect the environment and default branch; a subject wildcard covering unrelated repositories is not required. Provisioning the provider/role remains an IAM-owner task. See [AWS GitHub OIDC trust guidance](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_create_for-idp_oidc.html).

## What preflight can establish

Current AWS read checks use `eks:DescribeCluster`, `ec2:DescribeVpcs`, `s3:GetBucketLocation`, `eks:ListPodIdentityAssociations`, `eks:DescribePodIdentityAssociation` and, with AWS secret sourcing, `secretsmanager:DescribeSecret`. Scope resource-capable actions to the intended cluster, buckets and secrets; some describe APIs require `*`. Kubernetes checks read cluster health, add-ons, controllers, storage, resource ownership and required Secret/ConfigMap **key names**. Kubernetes authorization to read a Secret still permits retrieving its data; the key-only output is a tool behavior, not a special restricted Secret permission.

Preflight compares the configured association ARNs for Mimir, Tempo and optional Pyroscope. It does not validate the disabled Flink scaffold's effective credentials, the full attached IAM policies, trust conditions, service-control policies, permission boundaries, KMS policies, backend restore access or live S3 writes. Treat all of those as pending live gates. No IAM role, policy, association or AWS resource is created during offline validation.
