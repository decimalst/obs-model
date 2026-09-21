# Regional desired state

Generate `clusters/REGION/` with `scripts/gitops_export.py` after the verified initial installation. The shared chart, dashboard, rule and query files are already version controlled; a real regional export requires your repository coordinates, platform contract and published quota image digest. No placeholder cluster is automatically reconciled.

See [the complete workflow](../docs/gitops.md). CI discovers all checked-in `clusters/*/**/release.yaml` files and validates their Flux schemas and Helm rendering. Terraform state and credentials never belong in this directory.
