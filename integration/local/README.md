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

A separate PITR request for a target beyond the available recovery history returned a failed report after its 90-second deadline and deleted the drill Cluster and PVC. This checks bounded failure and cleanup; it does not simulate a corrupted object or a permanently broken archive.

An initial PITR fixture failed because its target timestamp was captured before the write committed. A later attempt hit a missing WAL segment because `pg_switch_wal()` ran in the same SQL command as a write. The fixture helper now separates those transactions and waits for the required archive segment. The test has not yet proven behavior for corrupted or permanently unavailable WAL, other object stores, or other PostgreSQL and operator versions.

To remove this fixture, delete only the test namespace: `kubectl delete namespace cnpg-drill-test`. The K3s uninstall script removes the local runtime and its data; run it only when that is intended.
