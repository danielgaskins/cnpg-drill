# Run a first recovery drill

This guide is for an operator who already has a CloudNativePG cluster using the
Barman Cloud Plugin and at least one completed plugin Backup. The drill creates
a temporary PostgreSQL Cluster and storage, so run it in a namespace and at a
time where that resource use is acceptable. No account or telemetry is needed.

1. Confirm the source Cluster, Barman `ObjectStore`, and a completed Backup are
   in the same namespace. The [supported scope](../README.md#what-it-supports)
   is one Barman Cloud Plugin source with object-store recovery. The first live
   matrix is [documented here](../integration/local/README.md).
2. Create a second `ObjectStore` pointing to the same archive with credentials
   limited to listing and reading backup and WAL objects. Confirm a write
   attempt with that identity is denied. Set its name as
   `recoveryObjectStore`. The tool checks the archive destination and endpoint
   match before creating a drill Cluster.
3. Pick an application-specific SQL invariant whose expected value is known in
   the backup. A recent business timestamp can be checked with
   `maxAgeSeconds`. `SELECT 1` verifies connectivity only. Checks execute in
   a read-only PostgreSQL transaction; provide trusted SQL.
4. Run `plan` and inspect the generated manifest. It should have a distinct
   Cluster name, one instance, the read-only recovery ObjectStore in
   `externalClusters`, and no `spec.plugins` or backup schedule.
5. Run the drill and save the JSON report. A pass requires recovery, assertions,
   and cleanup. Inspect the Job/CLI exit code, `backupID`, `recoverySeconds`,
   each check's `passed` flag, and `cleanup`. Verify the temporary PVC and
   provider volume are gone after the first run.

For a local CLI run, copy [examples/drill.json](../examples/drill.json), set the
namespace, source Cluster, recovery ObjectStore, and checks, then run:

```bash
cnpg-drill plan --config drill.json
cnpg-drill run --config drill.json --report drill-result.json
```

For a suspended CronJob and a one-time Job, use the
[Helm chart instructions](../deploy/helm/cnpg-drill/README.md). Once a full
restore passes, test a PITR target inside your retained WAL history. A drill
that fails because WAL is unavailable is useful evidence of a recovery gap.

## Recovery policies and extension images

These options require CLI v0.1.4 or chart 0.1.6 or later.

Set `drillClusterName` when a network policy or cloud role requires a specific
recovery name, such as `app-db-drill`. The name must differ from the source.
An existing Cluster at that name stops the drill before creation. Use one
scheduler for that name. Cleanup checks a per-run ownership token and sends a
UID precondition to Kubernetes; a competing or replacement Cluster is left
alone and the report fails. Inspect leftovers before retrying. Reserve a drill
name separately from the Cluster name used by your incident recovery procedure.
Chart 0.1.8 uses a fixed name derived from the Helm release when this value is
empty; direct CLI use still generates a name for each run.

For workload identity, set `recoveryServiceAccountAnnotations` explicitly on
the recovery Cluster's ServiceAccount. Supported keys are
`eks.amazonaws.com/role-arn`, `azure.workload.identity/client-id`, and
`azure.workload.identity/tenant-id`. Azure client identity also adds
`azure.workload.identity/use: "true"` to recovery Pods. The source's identity
annotations are not copied. A separate `recoveryObjectStore` is required;
provision and verify its read-only recovery identity and the required namespace,
ServiceAccount trust and network access before execution. The tool cannot
verify IAM privileges. Local testing does not establish AWS or Azure access.

```json
{
  "drillClusterName": "app-db-drill",
  "recoveryObjectStore": "app-db-recovery-readonly",
  "recoveryServiceAccountAnnotations": {
    "eks.amazonaws.com/role-arn": "arn:aws:iam::123456789012:role/postgres-recovery-readonly"
  }
}
```

Add these fields to a complete drill config with application assertions. The
same keys are available in Helm values. Test the pinned recovery
image before enabling scheduling.

The restore preserves source `postgresql.extensions`,
`shared_preload_libraries`, custom extension/library paths, and image pull
Secret references. Extension binaries must match the PostgreSQL major version,
distribution and architecture. ImageVolume extensions need a compatible
Kubernetes/container runtime. Assert recovered extension data or functions;
connectivity alone does not test extension loading. The database's extension
state comes from the backup; the drill does not run `CREATE EXTENSION`.
Custom extension environment variables are rejected rather than inheriting
potential writer credentials.

## Recovered primary sources

CLI v0.1.5 and chart 0.1.7 support a primary originally bootstrapped through
`bootstrap.recovery.source`. Wait until PostgreSQL has completed recovery and
the source has its own completed plugin backup in its current writer archive.
Both `plan` and `run` verify the ready primary Pod's Cluster ownership and run
`pg_is_in_recovery()` in a read-only transaction against that source Pod.
The result must be false before the tool creates a drill Cluster.

With chart 0.1.8, set `rbac.sourcePrimaryPodName` to the source
`status.currentPrimary` after checking its ownership. The chart permits exec on
that exact Pod for this query. If the source primary changes, review and update
the allowed name. This grants Pod exec access; the read-only transaction does
not constrain what a compromised image could execute. See the
[chart permission limits](../deploy/helm/cnpg-drill/README.md#permissions-in-chart-018).

The drill selects the source's new backup and current writer server name.
It preserves the application database, owner and Secret reference from the
recovery bootstrap, without copying the old recovery target or replica
configuration. Active replicas, distributed replica topologies and
`pg_basebackup` bootstrap sources remain unsupported.

## Share what happened

If you can, [open a recovery feedback issue](https://github.com/danielgaskins/cnpg-drill/issues/new?template=recovery-feedback.md).
The most useful observations are whether the backup restored, whether you
could check real application data, total recovery time, what failed, and the
operator time needed to keep this running. Redact cluster names, archive URLs,
credentials, kubeconfigs, SQL outputs, and business data from public reports.
The JSON report hashes observed query output but may still contain names and
timestamps you consider sensitive.
