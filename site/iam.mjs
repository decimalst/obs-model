// Offline IAM inventory derived from the regional Terraform modules. This does
// not discover AWS state, grant permissions, or create any role or association.
export const iamDefaults = Object.freeze({
  iam: { terraformRoleArn: null, existingWorkloadRoles: {} },
});
const rolePattern = /^arn:aws:iam::(\d{12}):role\/[A-Za-z0-9+=,.@_/-]+$/;
const workloads = {
  mimir: {
    label: "Mimir object storage",
    buckets: ["mimir-blocks", "mimir-ruler", "mimir-alertmanager"],
  },
  tempo: { label: "Tempo object storage", buckets: ["tempo"] },
  flink: {
    label: "Flink checkpoints, savepoints and HA state",
    buckets: ["flink"],
  },
  pyroscope: {
    label: "Pyroscope profile object storage",
    buckets: ["pyroscope"],
  },
};
const bucketActions = ["s3:ListBucket", "s3:GetBucketLocation"];
const objectActions = [
  "s3:GetObject",
  "s3:PutObject",
  "s3:DeleteObject",
  "s3:AbortMultipartUpload",
  "s3:ListMultipartUploadParts",
];
const autoscalerReads = [
  "autoscaling:DescribeAutoScalingGroups",
  "autoscaling:DescribeAutoScalingInstances",
  "autoscaling:DescribeLaunchConfigurations",
  "autoscaling:DescribeScalingActivities",
  "autoscaling:DescribeTags",
  "ec2:DescribeInstanceTypes",
  "ec2:DescribeImages",
  "ec2:DescribeLaunchTemplateVersions",
  "ec2:GetInstanceTypesFromInstanceRequirements",
  "eks:DescribeNodegroup",
];
const json = (value) => JSON.stringify(value, null, 2) + "\n";
const insist = (condition, message) => {
  if (!condition) throw new Error(message);
};
const object = (value) =>
  value !== null && typeof value === "object" && !Array.isArray(value);
const accountOf = (arn) =>
  typeof arn === "string" ? rolePattern.exec(arn)?.[1] : undefined;
const accountPlaceholder = "<DEPLOYMENT_ACCOUNT_ID>";

export function validateIAM(r) {
  const iam = r.iam === undefined ? structuredClone(iamDefaults.iam) : r.iam;
  insist(
    object(iam) &&
      Object.keys(iam).length === 2 &&
      Object.keys(iam).every((key) =>
        ["terraformRoleArn", "existingWorkloadRoles"].includes(key),
      ),
    "IAM settings require only terraformRoleArn and existingWorkloadRoles; never include credentials",
  );
  insist(
    iam.terraformRoleArn === null ||
      (typeof iam.terraformRoleArn === "string" &&
        rolePattern.test(iam.terraformRoleArn)),
    "Terraform role must be an existing IAM role ARN or null; user, session and root ARNs are not accepted",
  );
  insist(
    object(iam.existingWorkloadRoles) &&
      Object.keys(iam.existingWorkloadRoles).every((key) =>
        Object.hasOwn(workloads, key),
      ),
    "Reusable workload roles support only mimir, tempo, flink and pyroscope",
  );
  for (const [key, arn] of Object.entries(iam.existingWorkloadRoles))
    insist(
      typeof arn === "string" && rolePattern.test(arn),
      `Set an existing ${key} IAM role ARN`,
    );
  insist(
    ["new", "existing"].includes(r.mode),
    "IAM review requires a new or existing deployment mode",
  );
  if (r.mode === "existing") {
    insist(
      iam.terraformRoleArn === null &&
        Object.keys(iam.existingWorkloadRoles).length === 0,
      "Software-only installation does not run Terraform; use the existing-platform role_arns contract instead of new-cluster IAM settings",
    );
    const roles = r.existing?.role_arns || {};
    insist(
      object(roles) &&
        Object.keys(roles).every((key) => Object.hasOwn(workloads, key)),
      "Existing workload role references contain unsupported fields",
    );
    for (const [key, arn] of Object.entries(roles))
      insist(
        typeof arn === "string" && rolePattern.test(arn),
        `Set an existing ${key} IAM role ARN`,
      );
    const clusterArn = r.existing?.cluster_arn;
    const clusterAccount =
      typeof clusterArn === "string"
        ? /^arn:aws:eks:[a-z0-9-]+:(\d{12}):cluster\/.+$/.exec(clusterArn)?.[1]
        : null;
    if (clusterArn !== undefined)
      insist(clusterAccount, "Use a valid existing EKS cluster ARN");
    const accounts = new Set(Object.values(roles).map(accountOf));
    insist(
      accounts.size <= 1,
      "Direct Pod Identity roles must all belong to the same cluster account",
    );
    if (clusterAccount)
      insist(
        [...accounts].every((account) => account === clusterAccount),
        "Direct Pod Identity role account must match the existing cluster ARN",
      );
  } else {
    insist(
      r.profiling || !Object.hasOwn(iam.existingWorkloadRoles, "pyroscope"),
      "Enable CPU profiling before supplying a new-cluster Pyroscope role; inactive ownership settings are not discarded",
    );
    const accounts = new Set(
      Object.values(iam.existingWorkloadRoles).map(accountOf),
    );
    insist(
      accounts.size <= 1,
      "Direct Pod Identity roles must all belong to the same deployment account",
    );
    if (iam.terraformRoleArn)
      insist(
        [...accounts].every(
          (account) => account === accountOf(iam.terraformRoleArn),
        ),
        "Direct Pod Identity roles must match the Terraform assumed-role account",
      );
  }
  return structuredClone(iam);
}

const policy = (Statement) => ({ Version: "2012-10-17", Statement });
const trust = (service, account) =>
  policy([
    {
      Effect: "Allow",
      Principal: { Service: service },
      Action:
        service === "pods.eks.amazonaws.com"
          ? ["sts:AssumeRole", "sts:TagSession"]
          : ["sts:AssumeRole"],
      ...(service === "pods.eks.amazonaws.com"
        ? {
            Condition: {
              StringEquals: {
                "aws:SourceAccount": account || accountPlaceholder,
              },
            },
          }
        : {}),
    },
  ]);
const association = (r, namespace, serviceAccount, managed) => ({
  clusterName: r.name,
  namespace,
  serviceAccount,
  ownership: managed ? "created" : "external",
  verified: false,
});

export function iamPlan(r) {
  const iam = validateIAM(r),
    isNew = r.mode === "new";
  const accountId = isNew
    ? accountOf(iam.terraformRoleArn) || null
    : r.existing?.cluster_arn?.split(":")[4] || null;
  const account = accountId || accountPlaceholder;
  const reused = isNew
    ? iam.existingWorkloadRoles
    : r.existing?.role_arns || {};
  const roles = [];
  const baseRole = (
    id,
    label,
    service,
    permissions,
    binding = null,
    notes = [],
  ) => ({
    id,
    label,
    ownership: "created",
    arn: null,
    namePrefix: `${r.name}-${id}-`,
    trust: {
      source: "terraform",
      verified: false,
      document: trust(service, accountId),
    },
    permissions,
    association: binding,
    notes,
  });
  if (isNew) {
    roles.push(
      baseRole(
        "eks",
        "EKS control plane",
        "eks.amazonaws.com",
        { managedPolicies: ["arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"] },
        null,
        ["Used by the EKS service, not application pods."],
      ),
    );
    roles.push(
      baseRole(
        "node",
        "EC2 worker nodes",
        "ec2.amazonaws.com",
        {
          managedPolicies: [
            "AmazonEKSWorkerNodePolicy",
            "AmazonEC2ContainerRegistryPullOnly",
            "AmazonEKS_CNI_Policy",
          ].map((name) => `arn:aws:iam::aws:policy/${name}`),
        },
        null,
        [
          "Shared by the three AZ node groups. VPC CNI permissions currently live on this role; no separate CNI IAM role is created.",
          "Launch-template IMDSv2 requires tokens and a response hop limit of 1. This is not a complete isolation guarantee for privileged/host-network workloads.",
        ],
      ),
    );
    roles.push(
      baseRole(
        "ebs",
        "EBS CSI storage controller",
        "pods.eks.amazonaws.com",
        {
          managedPolicies: [
            "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy",
          ],
        },
        association(r, "kube-system", "ebs-csi-controller-sa", true),
      ),
    );
    roles.push(
      baseRole(
        "autoscaler",
        "Cluster Autoscaler",
        "pods.eks.amazonaws.com",
        {
          inlinePolicy: policy([
            { Effect: "Allow", Action: autoscalerReads, Resource: ["*"] },
            {
              Effect: "Allow",
              Action: [
                "autoscaling:SetDesiredCapacity",
                "autoscaling:TerminateInstanceInAutoScalingGroup",
              ],
              Resource: ["*"],
              Condition: {
                StringEquals: {
                  [`aws:ResourceTag/k8s.io/cluster-autoscaler/${r.name}`]:
                    "owned",
                },
              },
            },
          ]),
        },
        association(r, "observability-system", "cluster-autoscaler", true),
        [
          "Read actions use Resource *. Scaling writes also use Resource * but require the exact cluster ownership tag.",
        ],
      ),
    );
  }
  for (const [id, config] of Object.entries(workloads)) {
    if (isNew ? id === "pyroscope" && !r.profiling : !Object.hasOwn(reused, id))
      continue;
    const bucketNames = config.buckets.map((key) =>
      isNew
        ? `${r.name}-${account}-${r.region}-${key}`
        : r.existing?.buckets?.[key] ||
          `<EXISTING_${key.toUpperCase().replaceAll("-", "_")}_BUCKET>`,
    );
    const bucketARNs = bucketNames.map((name) => `arn:aws:s3:::${name}`);
    const item = baseRole(
      id,
      config.label,
      "pods.eks.amazonaws.com",
      {
        inlinePolicy: policy([
          { Effect: "Allow", Action: bucketActions, Resource: bucketARNs },
          {
            Effect: "Allow",
            Action: objectActions,
            Resource: bucketARNs.map((arn) => `${arn}/*`),
          },
        ]),
        ownership: Object.hasOwn(reused, id)
          ? "external-required-unverified"
          : "terraform",
      },
      association(r, "observability", id, isNew),
      [
        "No s3:* or bucket-administration permission is granted by this workload policy. Object deletion is required by backend retention/compaction.",
        "Default new buckets use SSE-S3 (AES256); no KMS IAM grant is generated. Externally encrypted buckets may need a separately reviewed KMS key policy and workload permissions.",
        ...(id === "flink" && isNew
          ? [
              "New regional infrastructure reserves this role and bucket even while the Flink job is disabled. Enabling a job and installing its operator remain separate setup steps.",
            ]
          : []),
        ...(id === "pyroscope" && !r.profiling
          ? [
              "Supplied reference is preserved while profiling is disabled; this installation neither uses nor changes its existing association.",
            ]
          : []),
      ],
    );
    if (Object.hasOwn(reused, id)) {
      item.ownership = "reused";
      item.arn = reused[id];
      item.namePrefix = null;
      item.trust.source = "external-required-unverified";
      if (Object.values(reused).filter((arn) => arn === reused[id]).length > 1)
        item.notes.push(
          "This ARN is shared by multiple workloads. Its owner must combine their required permissions rather than overwrite one policy with another; all associated workloads can use the combined permissions.",
        );
      item.notes.push(
        "This bundle does not create, import, attach policies to, or rewrite the trust of this existing role. Its owner must confirm the required trust and resource policy.",
      );
    }
    roles.push(item);
  }
  const createdRoles = roles.filter(
    (item) => item.ownership === "created",
  ).length;
  const reusedRoles = new Set(
    roles.filter((item) => item.ownership === "reused").map((item) => item.arn),
  ).size;
  const createdAssociations = roles.filter(
    (item) => item.association?.ownership === "created",
  ).length;
  return {
    version: 1,
    mode: r.mode,
    clusterName: r.name,
    region: r.region,
    accountId,
    accountSource: accountId
      ? isNew
        ? "configured Terraform assumed-role ARN; not live verified"
        : "supplied cluster ARN; not live verified"
      : "unknown until deployment credentials / cluster ARN are verified; administrator ARN is not the cluster account",
    summary: {
      createdRoles,
      reusedRoles,
      createdAssociations,
      roleBindingCount: roles.length,
      countScope:
        "IAM runtime roles only; reusedRoles counts distinct supplied ARNs, not the separate administrator/provisioner access purposes.",
    },
    execution: {
      roleArn: iam.terraformRoleArn,
      ownership: iam.terraformRoleArn ? "external" : "not-selected",
      mechanism: !isNew
        ? "No Terraform execution in software-only installation"
        : iam.terraformRoleArn
          ? "AWS provider STS AssumeRole into an existing role"
          : "Existing AWS credential chain / profile; no deployment role is created",
      verified: false,
      requirements: isNew
        ? [
            "The starting identity needs sts:AssumeRole if a Terraform role is selected, and the target role trust must allow that approved caller. The studio creates neither side of that grant.",
            "The provisioning identity needs reviewed create/update/tag/delete/read permissions for resources actually planned plus scoped iam:PassRole for their service roles. No ready-made AdministratorAccess policy is generated.",
            "Provider assume_role does not configure the S3 state backend. Configure state bucket access, locking and any backend role assumption separately.",
            "EKS cluster access and AWS provisioning permissions are independent. Choose the administrator role deliberately; the Terraform caller is not automatically a Kubernetes administrator.",
          ]
        : [
            "The installer caller needs existing EKS/Kubernetes access plus the read-only preflight permissions; no additional AWS administrator or provisioner role is created.",
          ],
    },
    roles,
    accessGrants: isNew
      ? [
          {
            principalArn: r.adminRole,
            roleOwnership: "external",
            createsRole: false,
            mechanism:
              "EKS STANDARD access entry and cluster-scoped access policy association",
            policyArn:
              "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy",
            scope: "cluster",
            bootstrapClusterCreatorAdminPermissions: false,
            verified: false,
          },
        ]
      : [],
    prerequisites: isNew
      ? [
          "Review the exact Terraform plan and account before applying. Generated role names have Terraform suffixes; namePrefix is not an actual role name or ARN.",
          "Provision the administrator role and optional Terraform role through their existing owners first.",
          "New infrastructure installs the EKS Pod Identity agent. Reused workload roles must be in the cluster account and ready before association creation.",
          "AWS services may require service-linked roles (EKS managed node groups, Auto Scaling, RDS, ElastiCache); these are not aws_iam_role resources managed by this module. Account owners must permit any first-use creation required by AWS.",
        ]
      : [
          "Existing EKS control-plane/node IAM roles, EBS CSI permissions, Pod Identity agent, networking/CNI permissions and selected autoscaler identity/association remain cluster-owner prerequisites; they are not adopted or created by software-only installation.",
          "Supply the cluster ARN to bind this review to an account; all direct Pod Identity roles must be in that account.",
          "The cluster/IAM owner must establish each required association at the exact namespace and service account listed before read-only preflight.",
          "Selecting install for the Cluster Autoscaler chart does not create its IAM role or Pod Identity association. Its external namespace/role/tag policy must match the release setup.",
        ],
    boundaries: [
      "Create/reuse choices describe intended ownership, not a safe migration of existing Terraform state. Changing a previously managed role to reused can schedule its destruction. Review the plan and transfer ownership/state deliberately before changing an existing deployment; keep Pod Identity association ownership explicit.",
      "This is a configuration review, not an AWS permission audit or successful deployment. All live trust, policy, association, credential-delivery and storage-operation checks remain in the deferred qualification batch.",
      "Current Terraform pod trust restricts the EKS Pods service by aws:SourceAccount only. It does not restrict trust by cluster ARN, namespace or service-account tags. Exact workload selection lives in Pod Identity associations; control who can create/update those associations and pass these roles.",
      "Kubernetes ServiceAccounts, Roles and ClusterRoles are separate from AWS IAM roles. Creating a ServiceAccount or operator chart alone grants no AWS IAM permissions.",
      "Grafana OAuth Viewer/Editor/Admin are Grafana application roles, not AWS IAM roles or Kubernetes access grants.",
      "Default Grafana/PostgreSQL and quota/Valkey credentials are delivered through existing Kubernetes Secrets. No workload Secrets Manager, KMS, cross-region, Kafka, ClickHouse or collector-wide AWS IAM role is generated.",
      "AWS-managed policies are referenced by ARN; their live contents are owned and can change in AWS. Review those policy versions during live qualification.",
    ],
    sources: [
      "terraform/modules/region/eks.tf",
      "terraform/modules/region/autoscaling.tf",
      "terraform/modules/platform-services/context.tf",
      "terraform/modules/platform-services/main.tf",
      "terraform/modules/platform-services/outputs.tf",
    ],
  };
}

export function compileIAM(r) {
  const plan = iamPlan(r);
  const roleSections = plan.roles
    .map(
      (item) =>
        `### ${item.label}\n\n- Ownership: **${item.ownership}**${item.arn ? `; existing ARN: \`${item.arn}\`` : `; Terraform name prefix: \`${item.namePrefix}\` (actual suffix/ARN known after deployment)`}.\n- Trust: ${item.trust.source}; ${item.trust.document.Statement[0].Principal.Service}.\n- Pod Identity: ${item.association ? `${item.association.ownership}; cluster \`${item.association.clusterName}\`, namespace \`${item.association.namespace}\`, service account \`${item.association.serviceAccount}\`.` : "none; this is a service/node role."}\n\nRequired trust${item.ownership === "reused" ? " to verify with the external role owner (not applied)" : " generated by Terraform"}:\n\n\`\`\`json\n${json(item.trust.document).trim()}\n\`\`\`\n\n${item.permissions.managedPolicies ? `AWS-managed policy attachments:\n\n${item.permissions.managedPolicies.map((arn) => `- \`${arn}\``).join("\n")}` : `Required permissions${item.ownership === "reused" ? " to verify externally (not attached)" : " in the Terraform inline policy"}:\n\n\`\`\`json\n${json(item.permissions.inlinePolicy).trim()}\n\`\`\``}\n\n${item.notes.map((note) => `- ${note}`).join("\n")}\n`,
    )
    .join("\n");
  const guide = `# IAM review for ${plan.clusterName}\n\nThis bundle creates nothing until an authorized operator later runs deployment. It contains no credentials. Keep these environment coordinates private. The selected ${plan.mode === "new" ? "new-region Terraform" : "software-only existing-EKS"} path proposes **${plan.summary.createdRoles} new IAM roles**, **${plan.summary.reusedRoles} distinct existing runtime-role ARNs** and **${plan.summary.createdAssociations} new Pod Identity associations**. The administrator and provisioning purposes below are counted separately.\n\nDeployment account: ${plan.accountId || "not yet established"}. ${plan.accountSource}. Placeholder account IDs in resource/trust examples must be resolved by the reviewed Terraform plan or existing cluster identity; never deploy them literally.\n\n## 1. Choose the provisioning and administrator identities\n\n${plan.execution.mechanism}${plan.execution.roleArn ? `: \`${plan.execution.roleArn}\`` : ""}.\n\n${plan.execution.requirements.map((line) => `- ${line}`).join("\n")}\n\n${plan.accessGrants.length ? `The already-existing administrator role \`${plan.accessGrants[0].principalArn}\` receives an EKS STANDARD access entry and the cluster-scoped \`AmazonEKSClusterAdminPolicy\` access association. This grants broad Kubernetes cluster administration; it creates no IAM role and attaches no IAM permissions policy to that role. Automatic cluster-creator administrator access is disabled. The administrator and Terraform role may be the same existing identity if deliberately approved; they remain separate privileges.\n` : "Existing Kubernetes access remains externally managed. The software-only bundle creates no EKS access entries, IAM roles, IAM policies or Pod Identity associations.\n"}\n## 2. Review every runtime role and association\n\n${roleSections}\n## 3. Create or reuse externally owned workload roles\n\n${plan.mode === "new" ? "By default Terraform creates the Mimir, Tempo and Flink storage roles, plus Pyroscope when profiling is enabled. To reuse a role, first have the IAM owner create/review it, then enter its ARN under the matching workload. Terraform still creates that workload's Pod Identity association but does not create/import the role or attach/update its policies. Cluster, node, EBS and autoscaler role replacement is not exposed by this regional module; use the existing-cluster path to retain those external resources." : "Have the cluster/IAM owner supply the regional buckets, workload role ARNs and existing associations. Enter those references in the existing-platform contract. The installer validates prerequisites without adopting them. Optional Flink references reserve access for a separately configured Flink job; the studio does not activate that job."}\n\n1. Confirm the actual target cluster account and regional bucket names. Direct Pod Identity role ARNs must use the cluster account; administrator access can use a separately approved external identity. Cross-account role chaining is not generated.\n2. Review the required Pods service trust shown above with the IAM owner. Current SourceAccount trust is account-wide, not bound to a single namespace/cluster in the trust policy. Association administration and iam:PassRole need their own least-privilege controls. Do not confuse the association selector with a trust condition.\n3. Review exact bucket/object statements. For new regional Terraform, the module's \`required_role_policies\` output supplies the authoritative policy documents once bucket/account values are known. The generated root nests this under \`platform.required_role_policies\`: export \`terraform output -json platform\` after the separately authorized deployment. Review the planned documents before first apply when reusing roles. Apply external-role policies through the IAM owner's repository; this bundle never modifies existing roles. Additional customer-managed KMS encryption needs a separately reviewed grant/key policy.\n4. Establish the listed cluster/namespace/service-account associations using the owning infrastructure repository. With new regional Terraform the association resources remain Terraform-owned even when their role is reused. With software-only installation the associations are entirely external. Never import/adopt an existing association implicitly.\n5. Ensure the EKS Pod Identity agent and cluster-owned infrastructure identities are ready. Run installer \`--render-only\` for an offline preview; later \`--check-only\` performs read-only live prerequisite checks. Preflight does not prove the effective runtime policy or every S3 operation.\n6. During the deferred live AWS qualification window, verify STS credential delivery and each backend's scoped read/write/delete behavior, forbidden-bucket denial, account/region match and the roles' actual trust/policy versions. Record evidence against the exact reviewed configuration; a downloaded IAM plan is not a passing gate.\n\n## 4. Owner prerequisites and boundaries\n\n${[...plan.prerequisites, ...plan.boundaries].map((line) => `- ${line}`).join("\n")}\n\nTerraform sources: ${plan.sources.map((path) => `\`${path}\``).join(", ")}. Read \`docs/setup-options.md\` and \`docs/live-qualification.md\` for the later deployment and acceptance steps.\n`;
  return { "IAM-PLAN.json": json(plan), "IAM.md": guide };
}
