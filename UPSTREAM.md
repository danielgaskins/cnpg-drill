# GitHub contribution path

This file separates confirmed submission mechanisms from possible contributions. A useful open-source release and live recovery proof come before any request for inclusion.

## 1. Kubernetes Krew index: package submission

**Repository:** [`kubernetes-sigs/krew-index`](https://github.com/kubernetes-sigs/krew-index). Its [developer guide](https://krew.sigs.k8s.io/docs/developer-guide/release/new-plugin/) documents a plugin manifest PR after an open-source license, tagged release, archive, and local installation test. Acceptance is discretionary.

Package name: `cnpg-drill`; command: `kubectl cnpg-drill`. `scripts/build_zipapp.py` produces a Linux/macOS Python zipapp and archive. `scripts/render_krew_manifest.py` renders the final manifest after a release has a real GitHub URL. The source is MIT licensed. Python 3.10+ and `kubectl` are stated as caveats. Do not submit a placeholder URL.

Release gate:

1. Run the live full/PITR/negative/cleanup matrix in [CONTRIBUTING.md](CONTRIBUTING.md).
2. Publish a tagged GitHub release with the archive and checksum. Record CloudNativePG and Barman plugin versions tested.
3. Install the archive locally through Krew's test command and verify `kubectl cnpg-drill --version`, `plan`, and `run` against a disposable cluster.
4. Render `plugins/cnpg-drill.yaml` with the exact archive hash and submit one focused PR.

Suggested PR body: “Adds `cnpg-drill`, a free kubectl plugin that restores a CloudNativePG Barman backup into a disposable cluster and verifies read-only SQL assertions. The archive includes its MIT license. Tested on [operator/plugin/Kubernetes versions], with full restore, PITR, failure, and cleanup results linked here. Local Krew install test: [command and result]. Python 3.10+ and kubectl are required.”

## 2. CloudNativePG: independent recovery examples

**Repository:** [`cloudnative-pg/cloudnative-pg`](https://github.com/cloudnative-pg/cloudnative-pg). The [backup docs](https://cloudnative-pg.io/docs/devel/backup/) already tell users to test recovery and measure time. After a real drill, propose a small documentation example showing a disposable restore, the meaning of `backupID`, how to check application data, and safe cleanup. It must work without `cnpg-drill`; the upstream guide should stand alone. Include a tool link only if maintainers request an ecosystem list entry.

## 3. Barman Cloud plugin: regression evidence

**Repository:** [`cloudnative-pg/plugin-barman-cloud`](https://github.com/cloudnative-pg/plugin-barman-cloud). Issue [#516](https://github.com/cloudnative-pg/plugin-barman-cloud/issues/516) describes nightly verification finding restore failures. If a live test reproduces a current plugin issue, contribute a minimal failing test or fix with versioned evidence. Do not attach this product to unrelated issues.

## 4. Helm discovery

Publish the chart from this GitHub repository after its render/install/upgrade tests pass. Artifact Hub can index a chart repository, but indexing is a separate distribution route, not an upstream endorsement. The chart remains suspended by default and requires an explicit image repository.

## What would make inclusion plausible

The free package should answer a real operational question in one command, have no mandatory account, produce verifiable reports tied to a backup ID, fail on unsupported configurations, remove its temporary resources, and have a reproducible live test transcript. Reviewers should be able to inspect the entire behavior and run it without contacting our service.

No PR, package submission, release, or external post has been made as of 2026-09-29.
