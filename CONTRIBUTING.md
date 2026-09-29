# Contributing

Contributions that make recovery verification more accurate or safer are welcome. Please open an issue with the CloudNativePG and Barman plugin versions, redacted source configuration, expected result, and observed result before adding a new backup mode.

## Local checks

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src
```

Add unit tests for manifest changes and a real restore transcript for behavior changes. Never put credentials, kubeconfig, database content, or backup data in issues or fixtures.

## Live integration gate

A release must demonstrate: a successful full restore; a successful PITR; a broken WAL path that fails; a failed SQL check that fails; no changes to the source Cluster or archive; cleanup of the drill Cluster, PVCs, and volumes. Run these on disposable infrastructure using a supported CloudNativePG and Barman plugin release. This gate has **not** been completed yet.

`PYTHONPATH=src python3 scripts/live_smoke.py --config <config.json> --report <report.json>` runs one real drill and checks the source Cluster spec before and after. Run it once for the latest backup and once with a PITR target. Keep credentials and data out of the report and PR transcript.

## Package submissions

The standalone `kubectl cnpg-drill` zipapp is built by `scripts/build_zipapp.py`. Once a tagged release and live integration gate exist, package it with this LICENSE and checksums, then submit a manifest PR to `kubernetes-sigs/krew-index` following its [guide](https://krew.sigs.k8s.io/docs/developer-guide/release/new-plugin/). Krew review is independent. Documentation or examples for CloudNativePG should be useful without any commercial link.
