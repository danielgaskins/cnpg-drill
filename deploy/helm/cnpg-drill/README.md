# cnpg-drill Helm chart

This chart installs a suspended CronJob that restores a CloudNativePG Barman
Cloud Plugin backup into a disposable Cluster, checks the recovered database,
prints a JSON report, and removes the drill Cluster and PVCs. It creates a
namespace-scoped ServiceAccount and Role. The scheduled Job does not run until
you set `suspended: false` or create a Job manually.

The chart needs a source CloudNativePG `Cluster`, a completed plugin `Backup`,
and its Barman Cloud `ObjectStore` in the release namespace. A separately named
`ObjectStore` with read-only access to the same archive is recommended for
recovery. See the [full project guide](https://github.com/danielgaskins/cnpg-drill)
and [local validation matrix](https://github.com/danielgaskins/cnpg-drill/tree/main/integration/local).

## First drill

Create a values file with a query that proves useful recovered data is present:

```yaml
cluster: app-db
recoveryObjectStore: app-db-recovery-readonly
checks:
  - name: orders-exist
    database: app
    query: SELECT count(*) > 0 FROM public.orders
    expected: "t"
```

Install the chart in the **same namespace** as the source Cluster and both
ObjectStores. The default image is pinned to a published digest.

```bash
helm install recovery-check oci://ghcr.io/danielgaskins/charts/cnpg-drill \
  --version 0.1.3 --namespace production --values drill-values.yaml
kubectl -n production create job recovery-check-manual \
  --from=cronjob/recovery-check-cnpg-drill
kubectl -n production wait --for=condition=complete job/recovery-check-manual --timeout=35m
kubectl -n production logs job/recovery-check-manual
```

If the Job fails, read its logs for the JSON `failureReason` and `cleanup`
fields. Check the drill Cluster, PVCs, and storage provider volumes after the
first run. The default `SELECT 1` check only proves that the recovered server
accepts a connection; replace it before scheduling recurring drills.

## Important values

| Value | Default | Purpose |
| --- | --- | --- |
| `cluster` | required | Source CloudNativePG Cluster name. |
| `recoveryObjectStore` | empty | Separately credentialed read-only ObjectStore; empty uses the source store. |
| `checks` | `SELECT 1` | Read-only SQL assertions. |
| `schedule` | `0 3 * * 0` | Cron expression, used only after unsuspending. |
| `suspended` | `true` | Prevents an unreviewed scheduled restore. |
| `timeoutSeconds` | `1800` | Drill deadline; the Job gets another three minutes for cleanup. |
| `maxBackupAgeSeconds` | `691200` | Rejects a stale completed Backup before creating a Cluster. |
| `targetTime` | empty | Optional RFC3339 PITR target with a timezone. |
| `retainOnFailure` | `false` | Leaves a failed drill Cluster and its storage for investigation when true. |
| `image.digest` | pinned | Tested container image; set a new digest when updating. |

The Job writes the JSON result to its logs. This chart does not yet include
durable report storage or missed-run alerts. Turn on recurring drills only
after validating the checks, restore cost, cleanup, and log collection for
your cluster.

Maintained by [Daniel Gaskins](https://danielgaskins.com/).
