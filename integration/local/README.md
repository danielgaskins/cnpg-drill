# Disposable local recovery test

This fixture runs a real Barman plugin backup and restore in a dedicated namespace. It is intended for a throwaway local Kubernetes cluster. The object store has only a `ClusterIP` Service; keep it unexposed. Its single PVC is not durable disaster recovery storage.

## Version matrix used on 2026-09-29

| Component | Version |
| --- | --- |
| K3s / Kubernetes | `v1.35.8+k3s1` |
| CloudNativePG | `1.30.1` |
| Barman Cloud plugin | `0.15.0` |
| cert-manager | `1.21.2` |
| PostgreSQL image | `ghcr.io/cloudnative-pg/postgresql:18.6-system-trixie` |
| RustFS object store | `rustfs/rustfs:1.0.0` |

The K3s binary was downloaded from its tagged GitHub release and checked against `sha256sum-amd64.txt`. The official K3s installer was run with `INSTALL_K3S_SKIP_DOWNLOAD=true` and `INSTALL_K3S_EXEC='server --disable traefik --disable servicelb --disable metrics-server --write-kubeconfig-group daniel --write-kubeconfig-mode 0640'`. This installs a systemd service and `/usr/local/bin/k3s-uninstall.sh`; inspect those host changes before using it on another machine. The test machine's UFW policy denies incoming traffic and has no allow rule for the K3s API port 6443. The fixture exposes no public service or ingress.

## Install the pinned components

Use a disposable K3s node with `kubectl` access. Install cert-manager, then the operator, then the plugin:

```bash
kubectl apply -f https://github.com/cert-manager/cert-manager/releases/download/v1.21.2/cert-manager.yaml
kubectl -n cert-manager rollout status deployment/cert-manager deployment/cert-manager-cainjector deployment/cert-manager-webhook --timeout=300s
kubectl apply --server-side -f https://github.com/cloudnative-pg/cloudnative-pg/releases/download/v1.30.1/cnpg-1.30.1.yaml
kubectl -n cnpg-system rollout status deployment/cnpg-controller-manager --timeout=300s
kubectl apply -f https://github.com/cloudnative-pg/plugin-barman-cloud/releases/download/v0.15.0/manifest.yaml
kubectl -n cnpg-system rollout status deployment/barman-cloud --timeout=300s
```

Create a private test credential and install the namespaced fixture:

```bash
kubectl create namespace cnpg-drill-test
kubectl -n cnpg-drill-test create secret generic s3-creds \
  --from-literal=accessKey=cnpgdrilllocal \
  --from-literal=secretKey="$(openssl rand -hex 24)"
kubectl -n cnpg-drill-test apply -f integration/local/stack.yaml
kubectl -n cnpg-drill-test rollout status deployment/rustfs --timeout=300s
kubectl -n cnpg-drill-test wait --for=jsonpath='{.status.phase}'=ClusterHealthy cluster/app-db --timeout=300s
```

RustFS creates the S3 bucket on first use. Seed a known row, create a Barman plugin backup, and wait for completion:

```bash
kubectl -n cnpg-drill-test exec app-db-1 -c postgres -- psql -X -v ON_ERROR_STOP=1 -U postgres -d postgres -c \
  "CREATE TABLE public.drill_fixture (id integer PRIMARY KEY, marker text NOT NULL); INSERT INTO public.drill_fixture VALUES (1, 'backup-before');"
kubectl -n cnpg-drill-test apply -f integration/local/backup.yaml
kubectl -n cnpg-drill-test wait --for=jsonpath='{.status.phase}'=completed backup/app-db-backup-1 --timeout=300s
```

Run the latest-backup drill **before** changing the fixture row. Then use separate committed writes and wait for the WAL segment before testing PITR:

```bash
PYTHONPATH=src python3 scripts/live_smoke.py --config integration/local/full.json --report /tmp/cnpg-drill-full.json
python3 integration/local/prepare_pitr.py --output /tmp/cnpg-drill-pitr.json
PYTHONPATH=src python3 scripts/live_smoke.py --config /tmp/cnpg-drill-pitr.json --report /tmp/cnpg-drill-pitr-report.json
PYTHONPATH=src python3 scripts/live_smoke.py --config integration/local/negative.json --report /tmp/cnpg-drill-negative.json
```

The negative run should exit 1, report a failed assertion without query output, and still report `cluster-and-pvcs-deleted`. Check that only the source and object-store volumes remain:

```bash
kubectl -n cnpg-drill-test get cluster,pvc,backup
kubectl get pv -o custom-columns=NAME:.metadata.name,CLAIM:.spec.claimRef.name,STATUS:.status.phase
```

## Read-only recovery and denied-WAL test

The source `drill-store` keeps its writer credentials. The separate `ObjectStore` definitions in [recovery-stores.yaml](recovery-stores.yaml) point to the same archive but use restricted credentials. The [read-only policy](recovery-readonly-policy.json) permits bucket listing and object reads; the [base-only policy](recovery-baseonly-policy.json) is solely a negative-test fixture that denies WAL reads. These policies were exercised against RustFS `1.0.0` with RustFS CLI `v0.1.36`. Create both IAM users and matching Kubernetes Secrets before applying the ObjectStores. Do not use the base-only identity for a normal drill.

For this disposable namespace, port-forward RustFS to `127.0.0.1:19000`, configure a local RustFS CLI alias with the existing writer Secret, then create the policies and users. The alias stores credentials in the CLI's local config; remove it after testing if desired. Run the commands in a shell with `rc` in `PATH`:

```bash
kubectl -n cnpg-drill-test port-forward svc/rustfs 19000:9000
```

In another shell:

```bash
CNPG_ROOT_ACCESS=$(kubectl -n cnpg-drill-test get secret s3-creds -o jsonpath='{.data.accessKey}' | base64 -d)
CNPG_ROOT_SECRET=$(kubectl -n cnpg-drill-test get secret s3-creds -o jsonpath='{.data.secretKey}' | base64 -d)
rc alias set cnpg-root http://127.0.0.1:19000 "$CNPG_ROOT_ACCESS" "$CNPG_ROOT_SECRET"
unset CNPG_ROOT_ACCESS CNPG_ROOT_SECRET

rc admin policy create cnpg-root cnpg-recovery-readonly integration/local/recovery-readonly-policy.json
rc admin policy create cnpg-root cnpg-recovery-baseonly integration/local/recovery-baseonly-policy.json
CNPG_READ_SECRET=$(openssl rand -hex 24)
CNPG_BASE_SECRET=$(openssl rand -hex 24)
rc admin user add cnpg-root drillrecovery "$CNPG_READ_SECRET"
rc admin user add cnpg-root drillbaseonly "$CNPG_BASE_SECRET"
rc admin policy attach cnpg-root cnpg-recovery-readonly --user drillrecovery
rc admin policy attach cnpg-root cnpg-recovery-baseonly --user drillbaseonly
kubectl -n cnpg-drill-test create secret generic recovery-s3-creds \
  --from-literal=accessKey=drillrecovery --from-literal=secretKey="$CNPG_READ_SECRET"
kubectl -n cnpg-drill-test create secret generic recovery-baseonly-s3-creds \
  --from-literal=accessKey=drillbaseonly --from-literal=secretKey="$CNPG_BASE_SECRET"
rc alias set cnpg-recovery http://127.0.0.1:19000 drillrecovery "$CNPG_READ_SECRET"
rc alias set cnpg-baseonly http://127.0.0.1:19000 drillbaseonly "$CNPG_BASE_SECRET"
unset CNPG_READ_SECRET CNPG_BASE_SECRET
kubectl -n cnpg-drill-test apply -f integration/local/recovery-stores.yaml
```

Verify `rc object list -r cnpg-recovery/cnpg-drill` succeeds and `printf probe | rc pipe cnpg-recovery/cnpg-drill/readonly-probe` returns `AccessDenied`. For the negative case, verify `rc object stat cnpg-baseonly/cnpg-drill/app-db/base/<backup-id>/backup.info` succeeds while a known `app-db/wals/...` object returns `AccessDenied`. Compare `rc object list -r --json cnpg-root/cnpg-drill` immediately before and after the read-only drill while the source is idle. Set `recoveryObjectStore` to `drill-recovery-readonly` in the full/PITR config; use `drill-recovery-baseonly` and a 45-second timeout to test the permanent WAL-read failure. The failed report should include `failureReason`, exit nonzero, and confirm cleanup. Keep the source archive intact.

An image from the tested commit was built by the [manual container workflow](../../.github/workflows/container.yml). To validate the Helm chart, install it suspended and create one Job manually after the PITR fixture has left the source row at `after-pitr`:

```bash
helm install local deploy/helm/cnpg-drill --namespace cnpg-drill-test \
  --kubeconfig /etc/rancher/k3s/k3s.yaml --values integration/local/helm-values.yaml
kubectl -n cnpg-drill-test create job local-once --from=cronjob/local-cnpg-drill
kubectl -n cnpg-drill-test wait --for=condition=Complete job/local-once --timeout=300s
kubectl -n cnpg-drill-test logs job/local-once
```

## Observed result

On 2026-09-29, full restore passed with two assertions and cleanup in 51.74 seconds. A clean PITR run passed in 65.74 seconds; the generated fixture passed in 68.82 seconds. A deliberately failed SQL assertion returned exit 1 and removed the drill Cluster and PVC. A stale-backup preflight returned exit 2 without creating a Cluster. The source Cluster spec was unchanged in every `live_smoke.py` report. The only PVs remaining after the runs belonged to `app-db-1` and `rustfs-data`.

The Helm Job passed two assertions and cleanup in 74.06 seconds using the namespace-scoped ServiceAccount and the published image pinned by SHA-256 digest. Kubernetes reported that same digest as the running container image ID. The CronJob remains suspended. Redacted JSON evidence is in [results](results/).

After adding the separate recovery store setting, the chart was upgraded with the new image pinned to `sha256:fbc62cb0bf2223d3590fbbb0887b5cae8399d9a8e83df5ebd0654c270c34c1bc`. A manually created Job used `drill-recovery-readonly`, passed two assertions and cleanup in 82.85 seconds, and reported that exact digest as its image ID. Only the source and RustFS volumes remained; see [helm-readonly.json](results/helm-readonly.json).

A separate PITR request for a target beyond the available recovery history returned a failed report after its 90-second deadline and deleted the drill Cluster and PVC. This checks bounded failure and cleanup; it does not simulate a corrupted object or a permanently broken archive.

The separately credentialed read-only full restore passed in 76.57 seconds, and PITR passed in 66.1 seconds. A `PutObject` probe with the recovery identity returned S3 `AccessDenied`. A base-only identity could read `backup.info` but received `AccessDenied` for a WAL object; its drill returned exit 1 with `failureReason: archive_access_denied`, a sanitized permission hint, and Cluster/PVC cleanup in 52.31 seconds. The source Cluster spec stayed unchanged. The root listing contained the same 12 object keys before and after these runs, and only the source and RustFS PVCs/PVs remained. Reports are in [results](results/).

The `v0.1.0` archive built from this code passed a local Krew `install --manifest=... --archive=...` using isolated `KREW_ROOT` and Krew `v0.5.0`. `kubectl cnpg-drill --version` returned `0.1.0`; `plan` selected `drill-recovery-readonly`; and `run` passed two assertions and cleanup in 72.22 seconds. The [Krew-run report](results/krew-run.json) contains no query output.

The public OCI chart `oci://ghcr.io/danielgaskins/charts/cnpg-drill:0.1.0` was fetched without registry credentials and installed as `oci-pilot` with the CronJob suspended. Its one-off Job used the multi-architecture image at `sha256:814dfb077dc1b886a870b97bf1f4b83ce5895628c2aa118c04ac67fb8735391b`, passed both assertions, and removed the drill Cluster and PVCs in 78.65 seconds. The Pod image ID matched that digest. The source and RustFS were the only remaining PVCs and PVs; see [helm-oci.json](results/helm-oci.json).

The chart release workflow repackaged version `0.1.0` at OCI digest `sha256:662fc793ccee720ac9e1ef49a4be46da32822f2e4aabc76370db5e807ad3594f`. After upgrading `oci-pilot` from that final public artifact, a second one-off Job passed the same assertions and cleanup in 75.32 seconds using the same image digest. The source and RustFS were again the only remaining PVCs; see [helm-oci-release.json](results/helm-oci-release.json).

Version `0.1.1` adds an optional database name per SQL check. A row was written to the `app` database, then a new Barman plugin backup completed with ID `20260929T202710`. The released v0.1.1 Krew archive restored that backup through `drill-recovery-readonly`, found the row in `app`, and removed its Cluster and PVCs; recovery took 61.38 seconds ([CLI report](results/app-database-cli-v011.json)). The public chart `0.1.2` was then pulled without credentials and run as a one-off Job using image digest `sha256:2e42ad8dc96434a9ef42dd42b42a2c922d3ef49cd90ac1f5710968e521a3f628`. It found the same row, recovered in 60.37 seconds, and removed its Cluster and PVCs ([Helm report](results/app-database-helm-v012.json)). After deleting the Job and uninstalling the chart, only the source `app-db` Cluster and the `app-db-1` and `rustfs-data` PVCs remained.

The pgEdge Helm chart `1.1.0` was also installed as a single node in a separate test namespace with image `ghcr.io/pgedge/pgedge-postgres:18-spock5-standard`. After seeding a row in its `app` database, a Barman plugin Backup completed with ID `20260929T203719`. The released v0.1.1 CLI recovered it through a read-only ObjectStore, found both the `spock` extension and the seeded row in `app`, and deleted its drill Cluster and PVCs. Recovery took 56.2 seconds ([pgEdge report](results/pgedge-chart-restore-v011.json)). The test namespace was deleted afterward. This checked a physical backup of one pgEdge node; it did not exercise Spock subscriptions or cross-node replication.

An initial PITR fixture failed because its target timestamp was captured before the write committed. A later attempt hit a missing WAL segment because `pg_switch_wal()` ran in the same SQL command as a write. The fixture helper now separates those transactions and waits for the required archive segment. The test has not yet proven behavior for corrupted objects, other object stores, or other PostgreSQL and operator versions.

To remove this fixture, delete only the test namespace: `kubectl delete namespace cnpg-drill-test`. The K3s uninstall script removes the local runtime and its data; run it only when that is intended.
