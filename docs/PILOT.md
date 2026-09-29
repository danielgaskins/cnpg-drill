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

## Share what happened

If you can, [open a pilot feedback issue](https://github.com/danielgaskins/cnpg-drill/issues/new?template=pilot-feedback.md).
The most useful observations are whether the backup restored, whether you
could check real application data, total recovery time, what failed, and the
operator time needed to keep this running. Redact cluster names, archive URLs,
credentials, kubeconfigs, SQL outputs, and business data from public reports.
The JSON report hashes observed query output but may still contain names and
timestamps you consider sensitive.
