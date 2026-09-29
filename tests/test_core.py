import copy
import datetime as dt
import hashlib
import json
import unittest

from cnpg_drill.core import Config, DrillError, build_manifest, choose_backup, run_drill


SOURCE = {
    "metadata": {"name": "app-db", "namespace": "production", "uid": "source-uid-123"},
    "spec": {
        "instances": 3,
        "imageName": "ghcr.io/cloudnative-pg/postgresql:17",
        "storage": {"size": "10Gi", "storageClass": "fast"},
        "walStorage": {"size": "2Gi"},
        "plugins": [{"name": "barman-cloud.cloudnative-pg.io", "enabled": True, "isWALArchiver": True, "parameters": {"barmanObjectName": "app-store"}}],
    },
}


class FakeClient:
    def __init__(self, *, ready=True, check_result="1", cleanup_error=False, remaining_pvcs=None):
        self.ready = ready
        self.check_result = check_result
        self.cleanup_error = cleanup_error
        self.created = None
        self.deleted = None
        self.queries = []
        self.remaining_pvcs = remaining_pvcs or []

    def get(self, resource, name, namespace):
        if name == "app-db":
            return copy.deepcopy(SOURCE)
        if name == "app-store":
            return {"metadata": {"name": name}}
        return {"status": {"readyInstances": 1 if self.ready else 0, "currentPrimary": f"{name}-1" if self.ready else ""}}

    def create(self, manifest, namespace):
        self.created = manifest

    def delete(self, resource, name, namespace):
        if self.cleanup_error:
            raise DrillError("delete denied")
        self.deleted = name

    def exec_query(self, pod, namespace, query):
        self.queries.append((pod, query))
        return self.check_result

    def list_pvcs(self, cluster, namespace):
        return self.remaining_pvcs

    def list_backups(self, namespace):
        stopped = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat()
        return [{"metadata": {"name": "app-db-backup"}, "spec": {"cluster": {"name": "app-db"}, "method": "plugin", "pluginConfiguration": {"name": "barman-cloud.cloudnative-pg.io"}}, "status": {"phase": "completed", "backupId": "20260929T120000", "stoppedAt": stopped, "method": "plugin"}}]


class ConfigTest(unittest.TestCase):
    def test_rejects_mutating_or_multiple_statements(self):
        for query in ("DELETE FROM users", "SELECT 1; DROP TABLE users", "  INSERT INTO x VALUES (1)"):
            with self.subTest(query=query), self.assertRaises(DrillError):
                Config.from_dict({"namespace": "production", "cluster": "app-db", "checks": [{"name": "unsafe", "query": query, "expected": "1"}]})

    def test_requires_timezone_for_pitr(self):
        with self.assertRaises(DrillError):
            Config.from_dict({"namespace": "production", "cluster": "app-db", "targetTime": "2026-01-01T00:00:00"})


class ManifestTest(unittest.TestCase):
    def setUp(self):
        self.config = Config.from_dict({"namespace": "production", "cluster": "app-db", "targetTime": "2026-01-01T00:00:00Z"})

    def test_recovery_uses_source_archive_but_does_not_archive(self):
        manifest = build_manifest(SOURCE, self.config, name="drill-app-db-test")
        spec = manifest["spec"]
        self.assertNotIn("plugins", spec)
        self.assertNotIn("backup", spec)
        self.assertEqual(spec["bootstrap"]["recovery"]["recoveryTarget"]["targetTime"], "2026-01-01T00:00:00Z")
        self.assertEqual(spec["externalClusters"][0]["plugin"]["parameters"], {"barmanObjectName": "app-store", "serverName": "app-db"})
        self.assertEqual(spec["storage"]["size"], "10Gi")
        self.assertEqual(spec["walStorage"]["size"], "2Gi")

    def test_fails_closed_on_tablespaces(self):
        source = copy.deepcopy(SOURCE)
        source["spec"]["tablespaces"] = [{"name": "data"}]
        with self.assertRaises(DrillError):
            build_manifest(source, self.config, name="drill-app-db-test")

    def test_preserves_custom_application_database_identity(self):
        source = copy.deepcopy(SOURCE)
        source["spec"]["bootstrap"] = {"initdb": {"database": "orders", "owner": "orders_user", "secret": {"name": "orders-owner"}}}
        recovery = build_manifest(source, self.config, name="drill-app-db-test")["spec"]["bootstrap"]["recovery"]
        self.assertEqual(recovery["database"], "orders")
        self.assertEqual(recovery["owner"], "orders_user")
        self.assertEqual(recovery["secret"], {"name": "orders-owner"})


class BackupSelectionTest(unittest.TestCase):
    def setUp(self):
        self.config = Config.from_dict({"namespace": "production", "cluster": "app-db"})

    def test_chooses_latest_completed_plugin_backup(self):
        fake = FakeClient()
        eligible = fake.list_backups("production")[0]
        older = copy.deepcopy(eligible)
        older["status"]["backupId"] = "older"
        older["status"]["stoppedAt"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).isoformat()
        failed = copy.deepcopy(eligible)
        failed["status"]["phase"] = "failed"
        chosen, age = choose_backup([older, failed, eligible], self.config)
        self.assertEqual(chosen["status"]["backupId"], "20260929T120000")
        self.assertLess(age, 7200)

    def test_rejects_stale_backup(self):
        config = Config.from_dict({"namespace": "production", "cluster": "app-db", "maxBackupAgeSeconds": 30})
        with self.assertRaisesRegex(DrillError, "above maxBackupAgeSeconds"):
            choose_backup(FakeClient().list_backups("production"), config)

    def test_pitr_requires_backup_before_target(self):
        config = Config.from_dict({"namespace": "production", "cluster": "app-db", "targetTime": "2020-01-01T00:00:00Z"})
        with self.assertRaisesRegex(DrillError, "No completed"):
            choose_backup(FakeClient().list_backups("production"), config)


class RunTest(unittest.TestCase):
    def setUp(self):
        self.config = Config.from_dict({"namespace": "production", "cluster": "app-db"})

    def test_success_reports_check_and_cleans_up(self):
        client = FakeClient()
        report = run_drill(client, self.config)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["backupID"], "20260929T120000")
        self.assertLess(report["backupAgeSeconds"], 7200)
        self.assertEqual(report["cleanup"], "cluster-and-pvcs-deleted")
        self.assertEqual(client.deleted, report["drillCluster"])
        self.assertEqual(report["checks"], [{"name": "connection", "passed": True, "observedSha256": hashlib.sha256(b"1").hexdigest()}])

    def test_failed_check_still_cleans_up(self):
        client = FakeClient(check_result="0")
        report = run_drill(client, self.config)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["cleanup"], "cluster-and-pvcs-deleted")
        self.assertNotIn("got '0'", report["error"])

    def test_cleanup_failure_fails_drill(self):
        client = FakeClient(cleanup_error=True)
        report = run_drill(client, self.config)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["cleanup"], "failed")

    def test_retention_only_on_failure(self):
        client = FakeClient(check_result="0")
        config = Config.from_dict({"namespace": "production", "cluster": "app-db", "retainOnFailure": True})
        report = run_drill(client, config)
        self.assertEqual(report["cleanup"], "retained-for-investigation")
        self.assertIsNone(client.deleted)

    def test_freshness_check_passes_without_logging_timestamp(self):
        import datetime as dt
        observed = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=10)).isoformat()
        client = FakeClient(check_result=observed)
        config = Config.from_dict({"namespace": "production", "cluster": "app-db", "checks": [{"name": "recent-order", "query": "SELECT now()", "maxAgeSeconds": 60}]})
        report = run_drill(client, config)
        self.assertEqual(report["status"], "passed")
        self.assertNotIn(observed, str(report))
        self.assertLess(report["checks"][0]["ageSeconds"], 60)


if __name__ == "__main__":
    unittest.main()
