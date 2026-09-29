---
name: Recovery drill feedback
about: Share a sanitized first-run result or adoption blocker
title: "[Recovery drill] "
---

Please omit credentials, kubeconfig, archive URLs, SQL results, and business data.
Redact cluster and namespace names from reports if needed.

**Environment:** CloudNativePG, Barman Cloud Plugin, Kubernetes, PostgreSQL,
and object-store versions (only what you can share).

**Recovery tested:** Full restore, PITR, or both. Did the application-specific
checks pass? Was the temporary Cluster and storage removed?

**Time and effort:** How long did recovery take? How much operator time did
setup and troubleshooting take?

**What would make repeated use practical?** Include any missing backup mode,
permissions, report retention, or alerting needs.
