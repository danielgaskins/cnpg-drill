# GitHub-led SaaS opportunity research

Research date: 2026-09-28. Scope: growth through useful GitHub contributions and an attempted package submission to an established repository. This is a decision memo, not evidence of customer demand or a guarantee of upstream acceptance.

## Recommendation

Build an open-source **CloudNativePG recovery-drill operator** with an optional paid, hosted evidence and alerting control plane. The free software should schedule and run isolated restores of a customer's own CloudNativePG backups, execute declared database checks, measure recovery time and recovery-point age, and publish a machine-readable result. The hosted service should coordinate fleets, retain signed history, alert on missed or failed drills, and provide evidence for reviews. Backup bytes and SQL results should stay in the customer's cluster by default; the service should receive metadata only.

Target buyer: platform or database teams already running multiple production CloudNativePG clusters, especially those that must demonstrate recovery readiness. Do not target individual homelabs or small Supabase projects as the first paying segment.

The narrow opportunity is **recurring proof that a production-specific recovery path works**. CloudNativePG already performs backups and recovery, so the product must never claim to replace those functions. Its own docs explicitly advise operators to test restores regularly and measure full recovery time [1]. The recovery docs explain that a restore bootstraps a new cluster and may need a valid WAL archive, Barman Cloud plugin configuration, or a volume snapshot [2]. An upstream Barman plugin issue reports that nightly verification exposed backups that failed to restore with `wal.maxParallel=8` [3]. That is direct evidence of the failure mode, though only one reported team.

## Why this passes the AI-usage test, conditionally

An AI assistant can generate a basic CronJob that creates a throwaway Cluster. That is a credible substitute for a small team. The proposed product earns its fee only if it reliably maintains the harder loop: discovering clusters and backup modes, safely creating and cleaning isolated restore targets, testing both full and point-in-time recovery, coping with changing CNPG and Barman plugin versions, preserving least-privilege access, avoiding backup-path collisions, tracking absent drills, and producing durable evidence across many clusters. The official recovery docs warn about accidental reuse of an archive destination when restoring [2]. These are ongoing operations, rather than a one-time code-generation task.

The pay test should be measured, not assumed. Let repository distribution drive installs first, then expose an optional hosted fleet service only when repeated use and feature requests justify it. **$199 per production cluster per month** is a pricing hypothesis, not a market quote. If the self-hosted version is easy enough for most teams, the business case fails while the open-source tool can still succeed.

The yield hypothesis is concentrated account value, not mass-market volume: a five-cluster team would be a $995/month account at that proposed price. Ten such teams would be $9,950 MRR before costs and churn. Those figures are arithmetic scenarios, not a forecast. The buyer must value avoided operator work and stronger recovery evidence by more than the subscription and drill-compute costs.

## Existing products and collision risk

| Product | Verified overlap | Implication |
| --- | --- | --- |
| CloudNativePG itself | Native backups, Barman Cloud plugin recovery, snapshots, PITR, and `kubectl cnpg`; its CI tests backup and restore paths [2, 4]. | No room for a generic backup or restore wrapper. User-specific recurring drills remain the proposed gap. |
| `restore-drill` | Open-source Docker/Kubernetes restore verification for PostgreSQL and other stores, with validation, RTO/RPO reports, Helm CronJob, and alerts [5]. Local clone at commit `dea374d` showed no CloudNativePG-specific integration. | Direct functional competitor. Differentiate through CNPG resource semantics and production-safe orchestration, not “we test restores.” Reassess if it adds CNPG support. |
| `checkmydump-operator` | Open-source CloudNativePG backup restore checks with scheduled recovery and SQL validation [13]. Its repository describes it as educational and not production tested as of 2026-09-29. | Direct CNPG-specific overlap. The wedge depends on verified recovery behavior, safety controls, maintained releases, PITR, and useful packaging; there is no claim of an empty category. |
| BackupDrill | Open-source CLI plus hosted scheduled restore testing for Supabase, with paid plans at $19/$49/$99 per month [6]. | Validates an open-source-to-hosted model, but its buyer, backup format, and price are different. It does not establish willingness to pay $199 for CNPG. |
| Klio | Experimental CloudNativePG backup/recovery system; docs recommend periodic test restores [7]. | Adjacent upstream project could add this feature. Monitor its roadmap closely. |
| Internal scripts and CronJobs | A Barman plugin issue documents nightly restores already being run by one team [3]. | DIY is real. The hosted value must save measurable maintenance and audit time. |

I found **limited visible CNPG-specific competition**, not “no competition.” Search cannot prove that private products or unindexed repositories do not exist. `checkmydump-operator` overlaps directly, while `restore-drill` and Klio further narrow the wedge.

## GitHub-only distribution path

1. Publish a useful MIT-licensed `kubectl` plugin and Helm chart. The current release includes scheduled execution, full restore and PITR, SQL assertions, cleanup, and local JSON reports. Webhook/Prometheus outputs and a controller remain possible additions. A mandatory SaaS login would undercut contribution value.
2. Submit the open-source `kubectl` plugin to `kubernetes-sigs/krew-index` after a tagged release, cross-platform archives, license inclusion, and local installation test. Krew's official submission guide specifies those steps and a manifest PR [8]. Acceptance is discretionary; the locally cloned index README explicitly says admission is case by case [9]. This is the concrete “add package to repo” path.
3. Contribute neutral recovery examples or bug fixes to CloudNativePG and Barman plugin repositories where they genuinely improve the projects. A vendor link should be incidental and only included if maintainers invite it. Do not depend on an upstream endorsement or a promotional listing.
4. Submit the tested manifest to suitable custom Krew indexes and the chart to relevant registries. Curated repository entries should offer a real install path or a precise category fit, follow each repository's contribution rules, and avoid promotional changes to unrelated projects.
5. Use GitHub issues and releases to support users, document real restore failures with reproducible fixes, and measure opt-in install and usage signals. The conversion path is from an actually useful open-source package to optional hosted fleet operations. No team outreach or pilot recruitment is required for this distribution experiment.

Krew has discoverability, but **distribution volume is unproven**. GitHub stars are not customers. The CloudNativePG GitHub page showed about 9.4k stars during FlowDriver inspection on the research date; this is only an ecosystem activity signal. Krew submission is a gate, not a growth forecast.

## Initial product boundary

- First supported combination: current supported CloudNativePG release, Barman Cloud plugin, S3-compatible object storage, one Kubernetes cluster. Validate both full restore and a recent point-in-time target.
- Run drill clusters in a dedicated namespace with quotas, network restrictions, a collision-resistant name, read-only source bucket access, an explicit maximum cost/runtime, and automatic cleanup. Preserve failed targets briefly only by explicit configuration.
- Checks: recovered cluster ready, expected databases/tables, user-defined read-only SQL assertions, latest transaction timestamp if the application exposes one, base-backup and WAL reachability, measured RTO, and RPO age. A green status must state what was actually checked.
- Hosted service: fleet policy, missed-run detection, retained signed results, alert escalation, access control, and an evidence export. Keep credentials and database contents local where possible.
- Later: volume snapshots, Azure/GCS stores, cross-region drills, application-level smoke tests, and organization-specific compliance formats. Do not promise those in v1.

## Validation gates before a full build

1. **Technical proof:** On a disposable kind cluster, produce a backup, then demonstrate successful full recovery and PITR through the Barman plugin. Intentionally break WAL access and show the drill fails with a useful diagnosis. Verify cleanup and no modification of the source Cluster or archive.
2. **Contribution proof:** Publish a working free release and Krew manifest; run the Krew local install test and submit the PR. A rejected PR is not fatal if the tool receives direct GitHub use, but it invalidates the primary distribution assumption.
3. **Distribution proof:** Get a working package accepted into relevant indexes and catalogs, then watch for downstream installs, references, issues, and contributions attributable to those repos. Favor contributions that let an agent evaluate and install the tool from repository context; do not count submitted PRs as adoption.
4. **Retention and paid demand:** Look for independent repeat use, recurring drill configurations, and requests for fleet evidence or alerting. Build a hosted layer only when observed maintenance burden and opt-in paid interest support it. If users prefer the free tool, preserve the OSS project and stop the SaaS investment.

## Alternatives investigated

| Candidate | Reason it ranked lower |
| --- | --- |
| n8n workflow regression testing | Real pain, but n8n now advertises native replay, mocking, and evaluations, and community tools already offer test harnesses [10, 11]. Harder to identify a distinct paid wedge through a node package. |
| OpenTelemetry dynamic multi-tenant routing | Strong enterprise need is documented in an active upstream issue [12], but solving it involves deep collector/operator changes and existing vendor infrastructure. Distribution via a package PR is less direct, and upstream might absorb the core feature. |

## Sources

1. [CloudNativePG backup guidance](https://cloudnative-pg.io/docs/devel/backup/), mirrored in local `cloudnative-pg` clone at commit `2a68080`, `docs/src/backup.md:255-264`.
2. [CloudNativePG recovery documentation](https://cloudnative-pg.io/docs/devel/recovery/), local clone at commit `2a68080`, `docs/src/recovery.md`.
3. [Barman Cloud plugin issue #516](https://github.com/cloudnative-pg/plugin-barman-cloud/issues/516).
4. [CloudNativePG end-to-end test documentation](https://cloudnative-pg.io/docs/devel/e2e/).
5. [`restore-drill` repository](https://github.com/RamazanKara/restore-drill), cloned locally at commit `dea374d`.
6. [BackupDrill product and pricing](https://backupdrill.com/).
7. [Klio backup and restore documentation](https://cloudnative-pg.io/klio/docs/next/user/backup_and_restore/).
8. [Krew new plugin submission guide](https://krew.sigs.k8s.io/docs/developer-guide/release/new-plugin/).
9. [Krew index README](https://github.com/kubernetes-sigs/krew-index/blob/master/README.md), cloned locally at commit `39220c2`.
10. [n8n product page](https://n8n.io/).
11. [n8n community testing feature request](https://community.n8n.io/t/workflow-unit-testing-with-test-cases/254023/).
12. [OpenTelemetry multi-tenant issue #48895](https://github.com/open-telemetry/opentelemetry-collector-contrib/issues/48895).
13. [`checkmydump-operator` repository](https://github.com/anddimario/checkmydump-operator), inspected on 2026-09-29.

Research methods: web search and direct official/project sources; FlowDriver read-only inspection of live GitHub issue search and the Krew index contribution page; local Git shallow clones and `rg` inspection of CloudNativePG docs, Krew index, `restore-drill`, and `checkmydump-operator` source. Subsequent distribution work produced [Krew index PR #6373](https://github.com/kubernetes-sigs/krew-index/pull/6373), [custom Krew index PR #44](https://github.com/ishantanu/awesome-kubectl-plugins/pull/44), and [kubetools PR #431](https://github.com/collabnix/kubetools/pull/431). No paid customer interviews were performed.
