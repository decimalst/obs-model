# Security reporting

Do not report exploitable vulnerabilities, credentials, or private deployment details in a public issue or pull request.

If this repository's **Security** tab offers **Report a vulnerability**, use that private reporting channel. Its availability depends on repository settings; this document does not imply it is enabled. If no private channel is available, open a public issue that only asks a maintainer to establish one. Withhold technical details until a private channel is confirmed.

A private report should identify the affected commit and component, describe the impact and required access, and provide a minimal reproduction using synthetic data. Include a suggested mitigation if known. Never send live credentials, Terraform state, Secret values, raw telemetry, pod specifications, or unredacted deployment exports. If a credential has already been exposed, revoke or rotate it through its owner; deleting the visible text is not sufficient.

There is no published security response SLA or supported-release matrix. Do not assume a pinned dependency is free of vulnerabilities or that a passed test establishes production readiness. The [validation record](docs/validation.md) states what was exercised and what remains pending.

For deployment security, read the [security setup and transport boundaries](docs/security.md), [IAM ownership guide](docs/iam.md), and [existing-cluster ownership rules](docs/existing-clusters.md). In particular, optional security profiles require existing identity, certificate, Secret, proxy, or mesh configuration; they do not automatically secure every telemetry hop.
