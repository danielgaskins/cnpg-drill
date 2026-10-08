# Verify a release before running it

The container and chart workflows now generate signed GitHub build attestations.
They identify the producing repository, workflow and source commit. Release
actions are pinned to commits, and publishing runs on GitHub-hosted runners.
These workflow changes need a successful published run before their outputs can
be verified. Existing v0.1.5 / chart 0.1.7 artifacts do not have these attestations.

A checksum or OCI digest identifies bytes. An attestation ties those bytes to a
build identity. Neither establishes that the source code is safe: review the
source, dependencies, chart permissions and workflow at the expected commit.

## Choose the expected artifacts

Use a current GitHub CLI that supports `gh attestation verify`, including
`--source-digest` and `--signer-workflow`. Registry access may require your normal
GHCR login. See the [GitHub verification documentation](https://docs.github.com/en/actions/how-tos/secure-your-work/use-artifact-attestations/use-artifact-attestations)
and [CLI options](https://cli.github.com/manual/gh_attestation_verify).

Get the chart and image digests and their source commits from the release you
reviewed. The image and chart may be built from different commits because the
chart commits the image digest after its build. Do not accept any artifact solely
because it came from this repository.

Set these values before running the verification script:

```bash
export CNPG_CHART_DIGEST='sha256:<reviewed chart manifest digest>'
export CNPG_CHART_SOURCE='<reviewed chart source commit>'
export CNPG_IMAGE_DIGEST='sha256:<reviewed image manifest digest>'
export CNPG_IMAGE_SOURCE='<reviewed image source commit>'
export CNPG_ARTIFACT_DIR='/path/to/an/empty/review-directory'
```

## Verify and download

Save this block as a script and run it. It stops on a missing or mismatched
attestation. The directory must be absent; the script creates it for this review.

```bash
#!/usr/bin/env bash
set -euo pipefail
: "${CNPG_ARTIFACT_DIR:?Set a new review directory}"
for digest in "$CNPG_CHART_DIGEST" "$CNPG_IMAGE_DIGEST"; do
  [[ "$digest" =~ ^sha256:[a-f0-9]{64}$ ]]
done
for commit in "$CNPG_CHART_SOURCE" "$CNPG_IMAGE_SOURCE"; do
  [[ "$commit" =~ ^[a-f0-9]{40}$ ]]
done
mkdir "$CNPG_ARTIFACT_DIR"
gh attestation verify "oci://ghcr.io/danielgaskins/charts/cnpg-drill@${CNPG_CHART_DIGEST}" \
  --repo danielgaskins/cnpg-drill \
  --signer-workflow danielgaskins/cnpg-drill/.github/workflows/chart.yml \
  --source-digest "$CNPG_CHART_SOURCE" --deny-self-hosted-runners
helm pull "oci://ghcr.io/danielgaskins/charts/cnpg-drill@${CNPG_CHART_DIGEST}" \
  --destination "$CNPG_ARTIFACT_DIR"
gh attestation verify "$CNPG_ARTIFACT_DIR"/cnpg-drill-*.tgz \
  --repo danielgaskins/cnpg-drill \
  --signer-workflow danielgaskins/cnpg-drill/.github/workflows/chart.yml \
  --source-digest "$CNPG_CHART_SOURCE" --deny-self-hosted-runners
gh attestation verify "oci://ghcr.io/danielgaskins/cnpg-drill@${CNPG_IMAGE_DIGEST}" \
  --repo danielgaskins/cnpg-drill \
  --signer-workflow danielgaskins/cnpg-drill/.github/workflows/container.yml \
  --source-digest "$CNPG_IMAGE_SOURCE" --deny-self-hosted-runners
helm show values "$CNPG_ARTIFACT_DIR"/cnpg-drill-*.tgz
```

Confirm the chart's `image.digest` equals the image digest you verified and its
repository is `ghcr.io/danielgaskins/cnpg-drill`. Render the local archive with
your values and review the final image reference, RBAC, source, recovery store
and disposable Cluster name. Install that same local archive, rather than
fetching a mutable chart tag again. Overrides can change the image; verify any
replacement independently.

Missing attestations, unexpected identities or source commits, and failed
verification are reasons to stop. Do not fall back to a version tag. This guide
does not grant permission to run a restore; follow the [first-run guide](FIRST-RUN.md)
and your cluster's admission and access policies.

## What this does not prove

Attestations do not make the maintainer account or build infrastructure immune
to compromise. These workflows do not claim SLSA Build Level 3, nor do they add
a separate cosign image signature. The chart retains namespace-wide Cluster
creation permission; see its [permission limits](../deploy/helm/cnpg-drill/README.md#permissions-in-chart-018).
