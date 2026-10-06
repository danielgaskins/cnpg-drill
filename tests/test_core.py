import copy
import datetime as dt
import hashlib
import json
import unittest

from cnpg_drill.core import API, Config, DrillError, Kubectl, build_manifest, choose_backup, prepare, recovery_failure_reason, run_drill


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
    def __init__(self, *, ready=True, check_result="1", cleanup_error=False, create_error=False, remaining_pvcs=None):
        self.ready = ready
        self.check_result = check_result
        self.cleanup_error = cleanup_error
        self.create_error = create_error
        self.created = None
        self.deleted = None
        self.queries = []
        self.remaining_pvcs = remaining_pvcs or []

    def get(self, resource, name, namespace):
        if name == "app-db":
            return copy.deepcopy(SOURCE)
        if name in ("app-store", "recovery-store", "wrong-store"):
            destination = "s3://other/" if name == "wrong-store" else "s3://backups/"
            return {"metadata": {"name": name}, "spec": {"configuration": {"destinationPath": destination, "endpointURL": "https://s3.example.test"}}}
        return {"metadata": copy.deepcopy(self.created["metadata"]) if self.created else {}, "status": {"readyInstances": 1 if self.ready else 0, "currentPrimary": f"{name}-1" if self.ready else ""}}

    def get_optional(self, resource, name, namespace):
        if self.created and self.created["metadata"]["name"] == name:
            cluster = copy.deepcopy(self.created)
            cluster["metadata"]["uid"] = "drill-uid"
            return cluster
        return None

    def delete_owned_cluster(self, name, namespace, run_id):
        cluster = self.get_optional(API, name, namespace)
        if cluster and cluster["metadata"]["annotations"].get("cnpg-drill.dev/run-id") != run_id:
            raise DrillError("Refusing cleanup: Cluster belongs to another run")
        self.delete(API, name, namespace)

    def create(self, manifest, namespace):
        self.created = manifest
        if self.create_error:
            raise DrillError("create response lost")

    def delete(self, resource, name, namespace):
        if self.cleanup_error:
            raise DrillError("delete denied")
        self.deleted = name

    def exec_query(self, pod, namespace, query, database="postgres"):
        self.queries.append((pod, database, query))
        return self.check_result

    def list_pvcs(self, cluster, namespace):
        return self.remaining_pvcs

    def recovery_logs(self, cluster, namespace):
        return []

    def list_backups(self, namespace):
        stopped = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat()
        return [{"metadata": {"name": "app-db-backup"}, "spec": {"cluster": {"name": "app-db"}, "method": "plugin", "pluginConfiguration": {"name": "barman-cloud.cloudnative-pg.io"}}, "status": {"phase": "completed", "backupId": "20260929T120000", "stoppedAt": stopped, "method": "plugin"}}]


class ConfigTest(unittest.TestCase):
    def test_recovery_store_name_is_validated(self):
        with self.assertRaisesRegex(DrillError, "recoveryObjectStore"):
            Config.from_dict({"namespace": "production", "cluster": "app-db", "recoveryObjectStore": "Other/namespace"})

    def test_rejects_mutating_or_multiple_statements(self):
        for query in ("DELETE FROM users", "SELECT 1; DROP TABLE users", "  INSERT INTO x VALUES (1)"):
            with self.subTest(query=query), self.assertRaises(DrillError):
                Config.from_dict({"namespace": "production", "cluster": "app-db", "checks": [{"name": "unsafe", "query": query, "expected": "1"}]})

    def test_requires_timezone_for_pitr(self):
        with self.assertRaises(DrillError):
            Config.from_dict({"namespace": "production", "cluster": "app-db", "targetTime": "2026-01-01T00:00:00"})

    def test_database_name_rejects_connection_strings(self):
        for database in ("host=elsewhere", "postgresql://elsewhere/db", "-h", "app db"):
            with self.subTest(database=database), self.assertRaisesRegex(DrillError, "database"):
                Config.from_dict({"namespace": "production", "cluster": "app-db", "checks": [{"name": "app-data", "database": database, "query": "SELECT 1", "expected": "1"}]})

    def test_exec_uses_selected_database_in_read_only_transaction(self):
        calls = []

        def invoke(argv, **kwargs):
            calls.append(argv)
            return type("Result", (), {"returncode": 0, "stdout": "1\n", "stderr": ""})()

        observed = Kubectl(invoke=invoke).exec_query("drill-1", "production", "SELECT 1", "app")
        self.assertEqual(observed, "1")
        self.assertEqual(calls[0][calls[0].index("-d") + 1], "app")
        self.assertIn("BEGIN READ ONLY; SELECT 1; COMMIT;", calls[0])


class RecoveryCompatibilityTest(unittest.TestCase):
    def test_rejects_source_name_and_invalid_names(self):
        for name in ("app-db", "Other/namespace", "a" * 64, ""):
            with self.subTest(name=name), self.assertRaisesRegex(DrillError, "drillClusterName"):
                Config.from_dict({"namespace": "production", "cluster": "app-db", "drillClusterName": name})

    def test_identity_requires_separate_store_and_supported_keys(self):
        identity = {"eks.amazonaws.com/role-arn": "arn:aws:iam::123456789012:role/recovery-readonly"}
        with self.assertRaisesRegex(DrillError, "separate recoveryObjectStore"):
            Config.from_dict({"namespace": "production", "cluster": "app-db", "recoveryServiceAccountAnnotations": identity})
        config = Config.from_dict({"namespace": "production", "cluster": "app-db", "recoveryObjectStore": "app-store", "recoveryServiceAccountAnnotations": identity})
        with self.assertRaisesRegex(DrillError, "separate recoveryObjectStore"):
            build_manifest(SOURCE, config, name="app-db-restore")
        with self.assertRaisesRegex(DrillError, "supported cloud identity"):
            Config.from_dict({"namespace": "production", "cluster": "app-db", "recoveryObjectStore": "recovery-store", "recoveryServiceAccountAnnotations": {"unrelated": "true"}})

    def test_preserves_extensions_without_source_writer_identity(self):
        source = copy.deepcopy(SOURCE)
        source["spec"].update({"postgresql": {"extensions": [{"name": "pgvector", "image": {"reference": "example/pgvector@sha256:abc"}}], "shared_preload_libraries": ["pg_stat_statements"], "parameters": {"dynamic_library_path": "$libdir", "archive_command": "writer"}}, "serviceAccountTemplate": {"metadata": {"annotations": {"eks.amazonaws.com/role-arn": "writer-role"}}}, "imagePullSecrets": [{"name": "registry-pull"}]})
        original = copy.deepcopy(source)
        identity = {"eks.amazonaws.com/role-arn": "readonly-role"}
        config = Config.from_dict({"namespace": "production", "cluster": "app-db", "recoveryObjectStore": "recovery-store", "recoveryServiceAccountAnnotations": identity})
        spec = build_manifest(source, config, name="app-db-restore")["spec"]
        self.assertEqual(spec["postgresql"]["extensions"], source["spec"]["postgresql"]["extensions"])
        self.assertEqual(spec["postgresql"]["shared_preload_libraries"], ["pg_stat_statements"])
        self.assertNotIn("archive_command", spec["postgresql"]["parameters"])
        self.assertEqual(spec["serviceAccountTemplate"]["metadata"]["annotations"], identity)
        self.assertEqual(spec["imagePullSecrets"], [{"name": "registry-pull"}])
        self.assertEqual(source, original)
        spec["postgresql"]["extensions"][0]["name"] = "changed"
        self.assertEqual(source, original)
        spec = build_manifest(source, Config(namespace="production", cluster="app-db"), name="app-db-restore")["spec"]
        self.assertNotIn("serviceAccountTemplate", spec)

    def test_extension_environment_is_not_inherited(self):
        source = copy.deepcopy(SOURCE)
        source["spec"]["postgresql"] = {"extensions": [{"name": "custom", "env": [{"name": "AWS_ACCESS_KEY_ID", "value": "writer"}]}]}
        with self.assertRaisesRegex(DrillError, "Extension environment"):
            build_manifest(source, Config(namespace="production", cluster="app-db"), name="app-db-restore")

    def test_azure_identity_marks_recovery_pods(self):
        config = Config.from_dict({"namespace": "production", "cluster": "app-db", "recoveryObjectStore": "recovery-store", "recoveryServiceAccountAnnotations": {"azure.workload.identity/client-id": "readonly-client"}})
        spec = build_manifest(SOURCE, config, name="app-db-restore")["spec"]
        self.assertEqual(spec["inheritedMetadata"]["labels"], {"azure.workload.identity/use": "true"})

    def test_aws_default_endpoint_can_be_omitted(self):
        class AWSStore(FakeClient):
            def get(self, resource, name, namespace):
                data = super().get(resource, name, namespace)
                if resource == "objectstores.barmancloud.cnpg.io":
                    data["spec"]["configuration"].pop("endpointURL", None)
                return data
        config = Config(namespace="production", cluster="app-db", recovery_object_store="recovery-store")
        self.assertEqual(prepare(AWSStore(), config)["kind"], "Cluster")

    def test_replacement_cluster_is_not_queried_or_deleted(self):
        class Replaced(FakeClient):
            def get(self, resource, name, namespace):
                data = super().get(resource, name, namespace)
                if self.created and name == self.created["metadata"]["name"]:
                    self.created["metadata"]["annotations"]["cnpg-drill.dev/run-id"] = "other-run"
                    data["metadata"]["annotations"]["cnpg-drill.dev/run-id"] = "other-run"
                return data
        client = Replaced()
        report = run_drill(client, Config(namespace="production", cluster="app-db"))
        self.assertEqual(report["status"], "failed")
        self.assertEqual(client.queries, [])
        self.assertIsNone(client.deleted)

    def test_fixed_name_exists_is_preflight_failure_without_cleanup(self):
        class Existing(FakeClient):
            def get_optional(self, resource, name, namespace):
                return {"metadata": {"name": name}}
        client = Existing()
        report = run_drill(client, Config(namespace="production", cluster="app-db", drill_cluster_name="app-db-restore"))
        self.assertEqual(report["phase"], "preflight")
        self.assertIsNone(client.created)
        self.assertIsNone(client.deleted)

    def test_competing_create_is_never_deleted(self):
        class Competing(FakeClient):
            def create(self, manifest, namespace):
                self.created = copy.deepcopy(manifest)
                self.created["metadata"]["annotations"]["cnpg-drill.dev/run-id"] = "other-run"
                raise DrillError("AlreadyExists")
        client = Competing()
        report = run_drill(client, Config(namespace="production", cluster="app-db", drill_cluster_name="app-db-restore"))
        self.assertEqual(report["cleanup"], "failed")
        self.assertIsNone(client.deleted)

    def test_delete_uses_uid_precondition(self):
        calls = []
        def invoke(argv, **kwargs):
            calls.append((argv, kwargs))
            data = {"metadata": {"uid": "owned-uid", "annotations": {"cnpg-drill.dev/run-id": "owned-run"}}}
            return type("Result", (), {"returncode": 0, "stdout": json.dumps(data) if "get" in argv else "", "stderr": ""})()
        client = Kubectl(invoke=invoke)
        client.delete_owned_cluster("app-db-restore", "production", "owned-run")
        self.assertEqual(json.loads(calls[1][1]["input"])["preconditions"], {"uid": "owned-uid"})
        self.assertIn("--raw=/apis/postgresql.cnpg.io/v1/namespaces/production/clusters/app-db-restore", calls[1][0])
        calls.clear()
        with self.assertRaisesRegex(DrillError, "another run"):
            client.delete_owned_cluster("app-db-restore", "production", "different-run")
        self.assertEqual(len(calls), 1)


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

    def test_pvc_template_uses_fresh_volume_with_source_capacity(self):
        source = copy.deepcopy(SOURCE)
        source["spec"]["storage"] = {
            "resizeInUseVolumes": True,
            "pvcTemplate": {
                "accessModes": ["ReadWriteOnce"],
                "resources": {"requests": {"storage": "50Gi"}},
                "storageClassName": "encrypted-local-path",
                "volumeMode": "Filesystem",
                "volumeName": "source-pv",
                "selector": {"matchLabels": {"source": "true"}},
                "dataSource": {"kind": "VolumeSnapshot", "name": "source-snapshot"},
            },
        }
        manifest = build_manifest(source, self.config, name="drill-app-db-test")
        storage = manifest["spec"]["storage"]
        self.assertEqual(storage["pvcTemplate"]["resources"]["requests"]["storage"], "50Gi")
        self.assertEqual(storage["pvcTemplate"]["storageClassName"], "encrypted-local-path")
        for key in ("volumeName", "selector", "dataSource", "dataSourceRef"):
            self.assertNotIn(key, storage["pvcTemplate"])
        self.assertIn("volumeName", source["spec"]["storage"]["pvcTemplate"])

    def test_rejects_storage_without_capacity(self):
        source = copy.deepcopy(SOURCE)
        source["spec"]["storage"] = {"pvcTemplate": {"storageClassName": "fast"}}
        with self.assertRaisesRegex(DrillError, "storage.pvcTemplate.resources.requests.storage"):
            build_manifest(source, self.config, name="drill-app-db-test")

    def test_separate_recovery_store_preserves_source_backup_selection(self):
        config = Config.from_dict({"namespace": "production", "cluster": "app-db", "recoveryObjectStore": "recovery-store"})
        manifest = prepare(FakeClient(), config, name="drill-app-db-test")
        self.assertEqual(manifest["spec"]["externalClusters"][0]["plugin"]["parameters"]["barmanObjectName"], "recovery-store")
        self.assertEqual(manifest["metadata"]["annotations"]["cnpg-drill.dev/backup-name"], "app-db-backup")
        self.assertNotIn("plugins", manifest["spec"])

    def test_rejects_recovery_store_pointing_elsewhere(self):
        config = Config.from_dict({"namespace": "production", "cluster": "app-db", "recoveryObjectStore": "wrong-store"})
        with self.assertRaisesRegex(DrillError, "destinationPath must match"):
            prepare(FakeClient(), config, name="drill-app-db-test")

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

    def test_copies_s3_compatibility_env_without_writer_credentials(self):
        source = copy.deepcopy(SOURCE)
        source["spec"]["env"] = [
            {"name": "AWS_REQUEST_CHECKSUM_CALCULATION", "value": "when_required"},
            {"name": "AWS_RESPONSE_CHECKSUM_VALIDATION", "value": "when_required"},
            {"name": "AWS_ACCESS_KEY_ID", "valueFrom": {"secretKeyRef": {"name": "writer", "key": "access"}}},
            {"name": "AWS_NO_CHUNKED_ENCODING", "valueFrom": {"secretKeyRef": {"name": "writer", "key": "flag"}}},
        ]
        env = build_manifest(source, self.config, name="drill-app-db-test")["spec"]["env"]
        self.assertEqual(env, [
            {"name": "AWS_REQUEST_CHECKSUM_CALCULATION", "value": "when_required"},
            {"name": "AWS_RESPONSE_CHECKSUM_VALIDATION", "value": "when_required"},
        ])


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
        self.assertEqual(report["checks"], [{"name": "connection", "database": "postgres", "passed": True, "observedSha256": hashlib.sha256(b"1").hexdigest()}])

    def test_check_can_query_application_database(self):
        config = Config.from_dict({"namespace": "production", "cluster": "app-db", "checks": [{"name": "app-data", "database": "app", "query": "SELECT 1", "expected": "1"}]})
        client = FakeClient()
        report = run_drill(client, config)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(client.queries[0][1], "app")
        self.assertEqual(report["checks"][0]["database"], "app")

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

    def test_attempts_cleanup_after_ambiguous_create_failure(self):
        client = FakeClient(create_error=True)
        report = run_drill(client, self.config)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(client.deleted, client.created["metadata"]["name"])
        self.assertNotEqual(report.get("phase"), "preflight")

    def test_stale_backup_is_preflight_failure(self):
        client = FakeClient()
        config = Config.from_dict({"namespace": "production", "cluster": "app-db", "maxBackupAgeSeconds": 1})
        report = run_drill(client, config)
        self.assertEqual(report["phase"], "preflight")
        self.assertIsNone(client.created)

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

    def test_recovery_diagnostic_classifies_without_leaking_logs(self):
        logs = ['{"error":"WAL file 000000010000000000000005 not found in archive s3://private/"}']
        self.assertEqual(recovery_failure_reason(logs), "wal_unavailable")
        self.assertEqual(recovery_failure_reason(['{"error":"AccessDenied: secret-bucket"}']), "archive_access_denied")
        self.assertIsNone(recovery_failure_reason(['{"msg":"restored log file from archive"}']))

    def test_timeout_reports_safe_reason_and_cleans_up(self):
        class FailedRecovery(FakeClient):
            def recovery_logs(self, cluster, namespace):
                return ['{"error":"AccessDenied for s3://secret-path/"}']

        client = FailedRecovery(ready=False)
        times = iter([0, 1, 31, 32, 33])
        config = Config.from_dict({"namespace": "production", "cluster": "app-db", "timeoutSeconds": 30})
        report = run_drill(client, config, sleep=lambda _: None, monotonic=lambda: next(times))
        self.assertEqual(report["failureReason"], "archive_access_denied")
        self.assertNotIn("secret-path", str(report))
        self.assertEqual(report["cleanup"], "cluster-and-pvcs-deleted")


if __name__ == "__main__":
    unittest.main()
