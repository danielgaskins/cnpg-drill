# cnpg-drill

**Prove that a CloudNativePG backup can restore.** `cnpg-drill` creates a disposable CloudNativePG cluster from the source cluster's Barman Cloud object store, waits for PostgreSQL to become ready, runs read-only SQL assertions, emits a JSON result, and deletes the drill cluster. It can run locally or on a schedule as a Kubernetes CronJob.

The core is free and works without an account or external service. It does not make backups or copy database contents to a vendor. The proposed hosted service in [RESEARCH.md](RESEARCH.md) is separate and has not been built.

## Status

**Early alpha (v0.1.0).** Unit tests cover manifest safety and execution state. A disposable [local integration run](integration/local/README.md) proved full restore, PITR, failed-check reporting, and Cluster/PVC cleanup on one version matrix. Do not rely on a pass as disaster recovery assurance until it has been validated against your operator, PostgreSQL image, storage, object store, and Barman plugin versions. No GitHub release or Krew submission exists yet.

## What it supports

- One source CloudNativePG cluster with one enabled Barman Cloud plugin and a named `ObjectStore` in the same namespace.
- Full restore from the latest completed Barman plugin `Backup` resource, or PITR to an explicit RFC3339 `targetTime`. The selected backup ID is pinned and included in the report; a stale backup fails preflight.
- Single-instance drill cluster using the source's PostgreSQL image, storage size/class, optional WAL storage, and resource requests.
- Read-only SQL checks. Exact expected values make reports easy to audit.
- Optional timestamp freshness assertions (`maxAgeSeconds`) for an application-defined recovery point. This measures age of that application's data, not the database WAL RPO.
- A JSON report on stdout and optionally in a local file. Query outputs are hashed rather than logged. Exit code 0 means all checks, Cluster deletion, and PVC cleanup passed; 1 means the drill failed; 2 means invalid input or a failed preflight.
- Scheduled execution with the Helm CronJob in `deploy/helm/cnpg-drill`.

The CLI refuses source clusters with tablespaces or recovery bootstrap because a naive clone could produce an incomplete or misleading pass. Volume snapshots, cross-namespace restores, cross-region recovery, custom Postgres extension image changes, and multi-cluster fleet management are future work.

## Install and use

Requirements: Python 3.10+, `kubectl` in `PATH`, CloudNativePG and the Barman Cloud plugin installed in the target cluster, and Kubernetes access to read the source Cluster and ObjectStore, create/get/delete a drill Cluster, and exec into its PostgreSQL pod.

```bash
python3 -m pip install -e .
cnpg-drill plan --config examples/drill.json
cnpg-drill run --config examples/drill.json --report reports/latest.json
```

`plan` reads the source configuration and prints the exact Cluster manifest it would create. Review that manifest before running the first drill. The example's table-count assertion is illustrative; replace it with an invariant from your own application. A minimal check is `SELECT 1`, but it only proves connection to the recovered server, not useful application data.

Example `drill.json`:

```json
{
  "namespace": "production",
  "cluster": "app-db",
  "timeoutSeconds": 1800,
  "maxBackupAgeSeconds": 691200,
  "checks": [
    {"name": "postgres-ready", "query": "SELECT 1", "expected": "1"},
    {"name": "orders-exist", "query": "SELECT count(*) > 0 FROM public.orders", "expected": "t"},
    {"name": "recent-order", "query": "SELECT max(created_at)::timestamptz FROM public.orders", "maxAgeSeconds": 86400}
  ]
}
```

For PITR, add `"targetTime": "2026-09-29T10:00:00Z"`. Pick a time inside the backup and WAL retention window. A passing PITR check proves only that target; use a second drill for the latest recovery path.

## Scheduled drill

The Helm chart creates a namespace-scoped ServiceAccount, Role, RoleBinding, ConfigMap, and CronJob. Set `cluster`, `checks`, and the other settings in a values file and install it **in the same namespace as the source Cluster and ObjectStore**. The chart defaults to suspension so installation does not immediately start a restore.

The example image build pins kubectl 1.36.4, suitable for Kubernetes 1.35–1.37 under the project's [version skew policy](https://kubernetes.io/releases/). Set `KUBECTL_VERSION` at image build time for another supported cluster version.

```bash
helm install cnpg-drill deploy/helm/cnpg-drill -n production \
  --set cluster=app-db --set image.repository=your-registry/cnpg-drill \
  --set image.tag=0.1.0 --set suspended=false
```

The sample container image in the chart is a placeholder until a real release is published. Build and supply your own image with `--set image.repository=... --set image.tag=...`. The CronJob uses `concurrencyPolicy: Forbid` and a bounded job deadline. Its logs contain the JSON result; failed runs have nonzero exit status. **The chart does not yet provide durable report storage, missed-run alerts, or fleet policy.**

## Safety boundary

- The generated recovery Cluster never copies `spec.plugins`, so it is not configured to archive WAL back to the source bucket. It refers to the source's Barman `ObjectStore` only as an external recovery source.
- The drill uses the same namespace because the plugin's `ObjectStore` and its credentials are namespace-scoped. The source Cluster object is never modified.
- SQL checks run in a `BEGIN READ ONLY` transaction. Queries are limited to one `SELECT` or `WITH` statement without semicolons. Only provide trusted SQL; this is a guard against mistakes, not a security sandbox.
- By default, the tool deletes the drill Cluster even when recovery or a check fails. `retainOnFailure` leaves it for investigation and can incur storage costs. Cleanup failures turn the result red.
- The tool checks that PVCs labeled for the drill cluster disappear after Cluster deletion and fails the report if they remain. Kubernetes PersistentVolume cleanup depends on the storage class; inspect cloud volumes after a first drill.
- The planned SaaS boundary is metadata only. This repository contains no telemetry upload or hosted service.

## Contributing and upstream distribution

Run `PYTHONPATH=src python3 -m unittest discover -s tests -v`. See [CONTRIBUTING.md](CONTRIBUTING.md) for how to add a recovery path, the live integration test gate, and the Krew package plan. We will submit only a tested, released package that stands on its own. Upstream acceptance is not assumed.

[UPSTREAM.md](UPSTREAM.md) records the specific repositories, proposed contribution value, and submission gates.

## Recovery references

- [CloudNativePG backup guidance](https://cloudnative-pg.io/docs/devel/backup/)
- [CloudNativePG recovery documentation](https://cloudnative-pg.io/docs/devel/recovery/)
- [Barman Cloud recovery example](https://cloudnative-pg.io/plugin-barman-cloud/docs/next/concepts/)
