"""Recovery planning and execution, with a deliberately small Kubernetes surface."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import re
import secrets
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Callable


PLUGIN = "barman-cloud.cloudnative-pg.io"
API = "clusters.postgresql.cnpg.io"
DNS_NAME = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


class DrillError(RuntimeError):
    """A recoverable validation or drill failure."""


@dataclass(frozen=True)
class Check:
    name: str
    query: str
    expected: str | None = None
    max_age_seconds: int | None = None
    database: str = "postgres"


@dataclass(frozen=True)
class Config:
    namespace: str
    cluster: str
    recovery_object_store: str | None = None
    timeout_seconds: int = 1800
    poll_seconds: int = 5
    max_backup_age_seconds: int = 691200
    target_time: str | None = None
    checks: tuple[Check, ...] = (Check("connection", "SELECT 1", "1"),)
    retain_on_failure: bool = False
    drill_cluster_name: str | None = None
    recovery_service_account_annotations: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Config:
        if not isinstance(raw, dict):
            raise DrillError("Config must be a JSON object")
        allowed = {"namespace", "cluster", "recoveryObjectStore", "timeoutSeconds", "pollSeconds", "maxBackupAgeSeconds", "targetTime", "checks", "retainOnFailure", "drillClusterName", "recoveryServiceAccountAnnotations"}
        unknown = set(raw) - allowed
        if unknown:
            raise DrillError(f"Unknown config keys: {', '.join(sorted(unknown))}")
        namespace, cluster = raw.get("namespace"), raw.get("cluster")
        for label, value in (("namespace", namespace), ("cluster", cluster)):
            if not isinstance(value, str) or not DNS_NAME.fullmatch(value) or len(value) > 63:
                raise DrillError(f"{label} must be a Kubernetes DNS label")
        recovery_object_store = raw.get("recoveryObjectStore")
        if recovery_object_store is not None and (not isinstance(recovery_object_store, str) or not DNS_NAME.fullmatch(recovery_object_store) or len(recovery_object_store) > 63):
            raise DrillError("recoveryObjectStore must be a Kubernetes DNS label")
        drill_name = raw.get("drillClusterName")
        if drill_name is not None and (not isinstance(drill_name, str) or not DNS_NAME.fullmatch(drill_name) or len(drill_name) > 63 or drill_name == cluster):
            raise DrillError("drillClusterName must be a Kubernetes DNS label different from the source")
        annotations = raw.get("recoveryServiceAccountAnnotations", {})
        identity_keys = {"eks.amazonaws.com/role-arn", "azure.workload.identity/client-id", "azure.workload.identity/tenant-id"}
        if not isinstance(annotations, dict) or set(annotations) - identity_keys or any(not isinstance(v, str) or not v or len(v) > 2048 for v in annotations.values()):
            raise DrillError("recoveryServiceAccountAnnotations must contain supported cloud identity keys with nonempty string values")
        if annotations and not recovery_object_store:
            raise DrillError("Recovery cloud identity requires a separate recoveryObjectStore")
        timeout = raw.get("timeoutSeconds", 1800)
        poll = raw.get("pollSeconds", 5)
        max_backup_age = raw.get("maxBackupAgeSeconds", 691200)
        if type(timeout) is not int or not 30 <= timeout <= 86400:
            raise DrillError("timeoutSeconds must be 30..86400")
        if type(poll) is not int or not 1 <= poll <= 60:
            raise DrillError("pollSeconds must be 1..60")
        if type(max_backup_age) is not int or not 1 <= max_backup_age <= 31536000:
            raise DrillError("maxBackupAgeSeconds must be 1..31536000")
        target = raw.get("targetTime")
        if target is not None:
            if not isinstance(target, str):
                raise DrillError("targetTime must be an RFC3339 timestamp")
            try:
                parsed = dt.datetime.fromisoformat(target.replace("Z", "+00:00"))
            except ValueError as exc:
                raise DrillError("targetTime must be an RFC3339 timestamp") from exc
            if parsed.tzinfo is None:
                raise DrillError("targetTime must include a timezone")
            if parsed > dt.datetime.now(dt.timezone.utc):
                raise DrillError("targetTime cannot be in the future")
        checks_raw = raw.get("checks", [{"name": "connection", "query": "SELECT 1", "expected": "1"}])
        if not isinstance(checks_raw, list) or not 1 <= len(checks_raw) <= 50:
            raise DrillError("checks must be a list of 1..50 checks")
        checks: list[Check] = []
        for item in checks_raw:
            if not isinstance(item, dict) or not {"name", "query"} <= set(item) or set(item) - {"name", "query", "expected", "maxAgeSeconds", "database"}:
                raise DrillError("Each check needs name, query, and one comparison")
            if ("expected" in item) == ("maxAgeSeconds" in item):
                raise DrillError("Each check needs exactly one of expected or maxAgeSeconds")
            name, query, expected = item["name"], item["query"], item.get("expected")
            max_age = item.get("maxAgeSeconds")
            database = item.get("database", "postgres")
            if not isinstance(name, str) or not name or len(name) > 80:
                raise DrillError("Check name must be 1..80 characters")
            if "expected" in item and (not isinstance(expected, str) or len(expected) > 4096):
                raise DrillError("Check expected must be a string up to 4096 characters")
            if "maxAgeSeconds" in item and (type(max_age) is not int or not 1 <= max_age <= 31536000):
                raise DrillError("maxAgeSeconds must be 1..31536000")
            if not isinstance(query, str) or len(query) > 8192 or not re.match(r"^\s*(SELECT|WITH)\b", query, re.I) or ";" in query:
                raise DrillError(f"Check {name}: query must be one SELECT/WITH statement without semicolons")
            if not isinstance(database, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]{0,62}", database):
                raise DrillError(f"Check {name}: database must be a simple PostgreSQL database name")
            checks.append(Check(name, query.strip(), expected, max_age, database))
        retained = raw.get("retainOnFailure", False)
        if type(retained) is not bool:
            raise DrillError("retainOnFailure must be boolean")
        return cls(namespace, cluster, recovery_object_store, timeout, poll, max_backup_age, target, tuple(checks), retained, drill_name, copy.deepcopy(annotations))


class Kubectl:
    def __init__(self, executable: str = "kubectl", invoke: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run):
        self.executable = executable
        self.invoke = invoke

    def call(self, args: list[str], *, input_text: str | None = None, timeout: int = 60) -> str:
        try:
            result = self.invoke([self.executable, *args], input=input_text, text=True, capture_output=True, timeout=timeout, check=False)
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise DrillError(f"kubectl unavailable or timed out: {exc}") from exc
        if result.returncode:
            raise DrillError(f"kubectl {' '.join(args[:4])} failed: {result.stderr.strip()[:1000]}")
        return result.stdout

    def get(self, resource: str, name: str, namespace: str) -> dict[str, Any]:
        return json.loads(self.call(["-n", namespace, "get", resource, name, "-o", "json"]))

    def get_optional(self, resource: str, name: str, namespace: str) -> dict[str, Any] | None:
        data = self.call(["-n", namespace, "get", resource, name, "--ignore-not-found", "-o", "json"])
        return json.loads(data) if data.strip() else None

    def delete_owned_cluster(self, name: str, namespace: str, run_id: str) -> None:
        cluster = self.get_optional(API, name, namespace)
        if cluster is None:
            return
        metadata = cluster.get("metadata", {})
        if metadata.get("annotations", {}).get("cnpg-drill.dev/run-id") != run_id:
            raise DrillError("Refusing cleanup: Cluster belongs to another run")
        uid = metadata.get("uid")
        if not isinstance(uid, str) or not uid:
            raise DrillError("Refusing cleanup: Cluster UID is missing")
        options = {"apiVersion": "v1", "kind": "DeleteOptions", "preconditions": {"uid": uid}, "propagationPolicy": "Background"}
        self.call(["delete", f"--raw=/apis/postgresql.cnpg.io/v1/namespaces/{namespace}/clusters/{name}", "-f", "-"], input_text=json.dumps(options))
        self.call(["-n", namespace, "wait", "--for=delete", f"{API}/{name}", "--timeout=120s"], timeout=135)

    def list_pvcs(self, cluster: str, namespace: str) -> list[str]:
        data = json.loads(self.call(["-n", namespace, "get", "pvc", "-l", f"cnpg.io/cluster={cluster}", "-o", "json"]))
        return [item["metadata"]["name"] for item in data.get("items", [])]

    def recovery_logs(self, cluster: str, namespace: str) -> list[str]:
        data = json.loads(self.call(["-n", namespace, "get", "pods", "-l", f"cnpg.io/cluster={cluster}", "-o", "json"]))
        logs = []
        pods = sorted(data.get("items", []), key=lambda p: p.get("metadata", {}).get("creationTimestamp", ""), reverse=True)
        for pod in pods[:5]:
            name = pod.get("metadata", {}).get("name", "")
            if "full-recovery" not in name:
                continue
            for container in ("plugin-barman-cloud", "full-recovery"):
                try:
                    logs.append(self.call(["-n", namespace, "logs", name, "-c", container, "--tail=200"], timeout=20))
                except DrillError:
                    continue
        return logs

    def list_backups(self, namespace: str) -> list[dict[str, Any]]:
        data = json.loads(self.call(["-n", namespace, "get", "backups.postgresql.cnpg.io", "-o", "json"]))
        return data.get("items", [])

    def create(self, manifest: dict[str, Any], namespace: str) -> None:
        self.call(["-n", namespace, "create", "-f", "-"], input_text=json.dumps(manifest))

    def delete(self, resource: str, name: str, namespace: str, *, timeout: int = 120) -> None:
        self.call(["-n", namespace, "delete", resource, name, "--ignore-not-found=true", "--wait=true", f"--timeout={timeout}s"], timeout=timeout + 15)

    def exec_query(self, pod: str, namespace: str, query: str, database: str = "postgres") -> str:
        # One read-only transaction. The config parser rejects semicolons in query.
        sql = f"BEGIN READ ONLY; {query}; COMMIT;"
        return self.call(["-n", namespace, "exec", pod, "-c", "postgres", "--", "psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", database, "-c", sql], timeout=60).strip()


def choose_backup(backups: list[dict[str, Any]], config: Config, object_name: str | None = None) -> tuple[dict[str, Any], int]:
    now = dt.datetime.now(dt.timezone.utc)
    target = dt.datetime.fromisoformat(config.target_time.replace("Z", "+00:00")) if config.target_time else now
    candidates: list[tuple[dt.datetime, dict[str, Any]]] = []
    for backup in backups:
        if backup.get("spec", {}).get("cluster", {}).get("name") != config.cluster:
            continue
        status = backup.get("status", {})
        if status.get("phase") != "completed" or not status.get("backupId"):
            continue
        if (status.get("method") or backup.get("spec", {}).get("method")) != "plugin":
            continue
        if backup.get("spec", {}).get("pluginConfiguration", {}).get("name") not in (None, PLUGIN):
            continue
        backup_object = backup.get("spec", {}).get("pluginConfiguration", {}).get("parameters", {}).get("barmanObjectName")
        if object_name and backup_object and backup_object != object_name:
            continue
        try:
            stopped = dt.datetime.fromisoformat(status["stoppedAt"].replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError):
            continue
        if stopped.tzinfo and stopped <= target:
            candidates.append((stopped, backup))
    if not candidates:
        raise DrillError("No completed Barman plugin Backup with an ID exists before the recovery target")
    stopped, chosen = max(candidates, key=lambda item: item[0])
    age = int((now - stopped).total_seconds())
    if age > config.max_backup_age_seconds:
        raise DrillError(f"Latest eligible Backup is {age}s old, above maxBackupAgeSeconds={config.max_backup_age_seconds}")
    return chosen, age


def fresh_storage(raw: Any, field: str) -> dict[str, Any]:
    """Keep source capacity and class without binding a drill PVC to source storage."""
    if not isinstance(raw, dict):
        raise DrillError(f"Source Cluster needs {field}.size or {field}.pvcTemplate.resources.requests.storage")
    storage = copy.deepcopy(raw)
    template = storage.get("pvcTemplate")
    if isinstance(template, dict):
        for key in ("dataSource", "dataSourceRef", "selector", "volumeName"):
            template.pop(key, None)
    size = storage.get("size")
    resources = template.get("resources") if isinstance(template, dict) else None
    requests = resources.get("requests") if isinstance(resources, dict) else None
    template_size = requests.get("storage") if isinstance(requests, dict) else None
    if not (isinstance(size, str) and size) and not (isinstance(template_size, str) and template_size):
        raise DrillError(f"Source Cluster needs {field}.size or {field}.pvcTemplate.resources.requests.storage")
    return storage


def build_manifest(source: dict[str, Any], config: Config, *, name: str, backup: dict[str, Any] | None = None) -> dict[str, Any]:
    spec = source.get("spec", {})
    if spec.get("tablespaces"):
        raise DrillError("Tablespace clusters are not supported in v0.1; refusing incomplete restore")
    bootstrap = spec.get("bootstrap") or {}
    replica = spec.get("replica") or {}
    if replica.get("enabled") or replica.get("primary"):
        raise DrillError("Replica clusters and distributed replica topologies are not supported")
    if bootstrap.get("pg_basebackup"):
        raise DrillError("pg_basebackup bootstrap sources are not supported")
    recovered = bootstrap.get("recovery")
    if recovered:
        if not isinstance(recovered, dict) or not recovered.get("source"):
            raise DrillError("Recovery bootstrap must have a named external source")
        backup_status = backup.get("status", {}) if backup else {}
        backup_spec = backup.get("spec", {}) if backup else {}
        if (
            backup_status.get("phase") != "completed" or not backup_status.get("backupId")
            or backup_spec.get("cluster", {}).get("name") != config.cluster
            or (backup_status.get("method") or backup_spec.get("method")) != "plugin"
        ):
            raise DrillError("Recovered primary sources require their own completed plugin backup")
    elif bootstrap and "initdb" not in bootstrap:
        raise DrillError("Only default, initdb or verified recovered-primary sources are supported")
    plugins = [p for p in spec.get("plugins", []) if p.get("name") == PLUGIN and p.get("enabled", True)]
    if len(plugins) != 1:
        raise DrillError("Source needs one enabled Barman Cloud plugin")
    params = plugins[0].get("parameters", {})
    object_name = params.get("barmanObjectName")
    if not isinstance(object_name, str) or not DNS_NAME.fullmatch(object_name):
        raise DrillError("Barman plugin needs a valid barmanObjectName")
    storage = fresh_storage(spec.get("storage"), "storage")
    if len(name) > 63 or not DNS_NAME.fullmatch(name) or name == config.cluster:
        raise DrillError("Invalid or unsafe drill cluster name")
    recovery: dict[str, Any] = {"source": "backup-source"}
    if backup:
        recovery["recoveryTarget"] = {"backupID": backup["status"]["backupId"]}
    application = recovered or bootstrap.get("initdb") or {}
    for key in ("database", "owner", "secret"):
        if key in application:
            recovery[key] = copy.deepcopy(application[key])
    if config.target_time:
        recovery.setdefault("recoveryTarget", {})["targetTime"] = config.target_time
    external = {"name": "backup-source", "plugin": {"name": PLUGIN, "enabled": True, "parameters": {"barmanObjectName": config.recovery_object_store or object_name, "serverName": params.get("serverName") or config.cluster}}}
    output: dict[str, Any] = {
        "apiVersion": "postgresql.cnpg.io/v1", "kind": "Cluster",
        "metadata": {"name": name, "namespace": config.namespace, "labels": {"app.kubernetes.io/managed-by": "cnpg-drill", "cnpg-drill.dev/source": config.cluster}, "annotations": {"cnpg-drill.dev/source-uid": source.get("metadata", {}).get("uid", ""), "cnpg-drill.dev/backup-name": backup.get("metadata", {}).get("name", "") if backup else ""}},
        "spec": {"instances": 1, "storage": storage, "bootstrap": {"recovery": recovery}, "externalClusters": [external]},
    }
    for key in ("imageName", "imageCatalogRef", "imagePullSecrets", "resources"):
        if key in spec:
            output["spec"][key] = copy.deepcopy(spec[key])
    postgresql = spec.get("postgresql") or {}
    if any(extension.get("env") for extension in postgresql.get("extensions", [])):
        raise DrillError("Extension environment variables require explicit recovery review; refusing inherited credentials")
    recovery_postgresql = {key: copy.deepcopy(postgresql[key]) for key in ("extensions", "shared_preload_libraries") if key in postgresql}
    paths = {key: value for key, value in postgresql.get("parameters", {}).items() if key in {"extension_control_path", "dynamic_library_path"}}
    if paths:
        recovery_postgresql["parameters"] = copy.deepcopy(paths)
    if recovery_postgresql:
        output["spec"]["postgresql"] = recovery_postgresql
    if config.recovery_service_account_annotations:
        if not config.recovery_object_store or config.recovery_object_store == object_name:
            raise DrillError("Recovery cloud identity requires a separate recoveryObjectStore")
        output["spec"]["serviceAccountTemplate"] = {"metadata": {"annotations": copy.deepcopy(config.recovery_service_account_annotations)}}
        if "azure.workload.identity/client-id" in config.recovery_service_account_annotations:
            output["spec"]["inheritedMetadata"] = {"labels": {"azure.workload.identity/use": "true"}}
    if "walStorage" in spec:
        output["spec"]["walStorage"] = fresh_storage(spec["walStorage"], "walStorage")
    # S3-compatible stores may need these boto3 settings during recovery too.
    # Never copy arbitrary source env: it can contain writer credentials.
    recovery_env = [
        {"name": item["name"], "value": item["value"]} for item in spec.get("env", [])
        if isinstance(item, dict)
        and item.get("name") in {"AWS_REQUEST_CHECKSUM_CALCULATION", "AWS_RESPONSE_CHECKSUM_VALIDATION", "AWS_NO_CHUNKED_ENCODING"}
        and isinstance(item.get("value"), str)
        and "valueFrom" not in item
    ]
    if recovery_env:
        output["spec"]["env"] = recovery_env
    # Deliberately do not copy spec.plugins: the recovered cluster must not archive to source.
    return output


def new_name(source_name: str) -> str:
    suffix = dt.datetime.now(dt.timezone.utc).strftime("%m%d%H%M") + "-" + secrets.token_hex(3)
    return f"drill-{source_name[:48-len(suffix)]}-{suffix}"


def recovery_failure_reason(logs: list[str]) -> str | None:
    """Classify recovery logs without exposing archive paths, credentials, or SQL."""
    messages = []
    for log in logs:
        for line in log.splitlines()[-200:]:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            record = entry.get("record")
            if isinstance(record, dict):
                messages.append(str(record.get("message", "")))
            messages.append(str(entry.get("error", "")))
            messages.append(str(entry.get("msg", "")))
    combined = "\n".join(messages).lower()
    if re.search(r"access.?denied|permission.?denied|forbidden|\b403\b", combined):
        return "archive_access_denied"
    if re.search(r"(wal|archive).{0,100}(not found|no such key|missing|unavailable|does not exist)|requested wal segment.{0,100}removed", combined):
        return "wal_unavailable"
    if re.search(r"restore error|error while restoring a backup|fatal", combined):
        return "recovery_process_failed"
    return None


def prepare(client: Kubectl, config: Config, name: str | None = None) -> dict[str, Any]:
    source = client.get(API, config.cluster, config.namespace)
    if source.get("metadata", {}).get("deletionTimestamp"):
        raise DrillError("Source Cluster is being deleted")
    source_plugins = [p for p in source.get("spec", {}).get("plugins", []) if p.get("name") == PLUGIN and p.get("enabled", True)]
    object_name = source_plugins[0].get("parameters", {}).get("barmanObjectName") if len(source_plugins) == 1 else None
    backup, age = choose_backup(client.list_backups(config.namespace), config, object_name)
    manifest = build_manifest(source, config, name=name or config.drill_cluster_name or new_name(config.cluster), backup=backup)
    if source.get("spec", {}).get("bootstrap", {}).get("recovery"):
        status = source.get("status", {})
        primary = status.get("currentPrimary")
        source_uid = source.get("metadata", {}).get("uid")
        if not primary or not source_uid or status.get("readyInstances", 0) < 1:
            raise DrillError("Recovered source needs a ready primary before a drill")
        pod = client.get("pods", primary, config.namespace)
        metadata = pod.get("metadata", {})
        if metadata.get("labels", {}).get("cnpg.io/cluster") != config.cluster or not any(
            owner.get("uid") == source_uid and owner.get("kind") == "Cluster"
            and owner.get("apiVersion") == "postgresql.cnpg.io/v1"
            for owner in metadata.get("ownerReferences", [])
        ):
            raise DrillError("Recovered source primary Pod ownership could not be verified")
        if client.exec_query(primary, config.namespace, "SELECT pg_is_in_recovery()") != "f":
            raise DrillError("Recovered source PostgreSQL must be a primary, not in recovery")
    manifest["metadata"]["annotations"]["cnpg-drill.dev/backup-age-seconds"] = str(age)
    if not object_name:
        raise DrillError("Source needs one enabled Barman Cloud plugin")
    source_store = client.get("objectstores.barmancloud.cnpg.io", object_name, config.namespace)
    recovery_name = config.recovery_object_store or object_name
    if recovery_name != object_name:
        recovery_store = client.get("objectstores.barmancloud.cnpg.io", recovery_name, config.namespace)
        source_config = source_store.get("spec", {}).get("configuration", {})
        recovery_config = recovery_store.get("spec", {}).get("configuration", {})
        for field in ("destinationPath", "endpointURL"):
            if (field == "destinationPath" and not source_config.get(field)) or source_config.get(field) != recovery_config.get(field):
                raise DrillError(f"Recovery ObjectStore {field} must match source ObjectStore")
    if config.drill_cluster_name and client.get_optional(API, manifest["metadata"]["name"], config.namespace) is not None:
        raise DrillError("Configured drillClusterName already exists; inspect it before retrying")
    return manifest


def run_drill(client: Kubectl, config: Config, *, sleep: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    started_at = dt.datetime.now(dt.timezone.utc)
    started = monotonic()
    result: dict[str, Any] = {"schemaVersion": 1, "source": f"{config.namespace}/{config.cluster}", "startedAt": started_at.isoformat(), "targetTime": config.target_time, "checks": [], "status": "error"}
    name: str | None = None
    created = False
    run_id = secrets.token_hex(16)
    try:
        manifest = prepare(client, config)
        name = manifest["metadata"]["name"]
        result["drillCluster"] = name
        result["sourceClusterUID"] = manifest["metadata"]["annotations"]["cnpg-drill.dev/source-uid"]
        result["backupName"] = manifest["metadata"]["annotations"]["cnpg-drill.dev/backup-name"]
        result["backupID"] = manifest["spec"]["bootstrap"]["recovery"]["recoveryTarget"]["backupID"]
        result["backupAgeSeconds"] = int(manifest["metadata"]["annotations"]["cnpg-drill.dev/backup-age-seconds"])
        manifest["metadata"]["annotations"]["cnpg-drill.dev/run-id"] = run_id
        created = True
        # A create request can reach the API even if the client loses its response.
        # In that case, remove only a Cluster bearing this run's ownership token.
        client.create(manifest, config.namespace)
        recovery_started = monotonic()
        deadline = started + config.timeout_seconds
        primary = None
        while monotonic() < deadline:
            cluster = client.get(API, name, config.namespace)
            if cluster.get("metadata", {}).get("annotations", {}).get("cnpg-drill.dev/run-id") != run_id:
                raise DrillError("Recovery Cluster belongs to another run; refusing SQL checks")
            status = cluster.get("status", {})
            if status.get("readyInstances", 0) >= 1 and status.get("currentPrimary"):
                primary = status["currentPrimary"]
                break
            sleep(config.poll_seconds)
        if not primary:
            try:
                reason = recovery_failure_reason(client.recovery_logs(name, config.namespace))
            except DrillError:
                reason = None
            result["failureReason"] = reason or "recovery_timeout"
            hints = {
                "archive_access_denied": "Check recovery ObjectStore permissions to read both base backup and WAL objects.",
                "wal_unavailable": "Check that the required WAL segment exists and is readable in the recovery archive.",
                "recovery_process_failed": "Inspect the drill recovery Pod and operator logs for the underlying restore error.",
                "recovery_timeout": "Inspect the drill recovery Pod and operator logs for the cause.",
            }
            raise DrillError(f"Recovery did not become ready within {config.timeout_seconds}s. {hints[result['failureReason']]}")
        result["recoverySeconds"] = round(monotonic() - recovery_started, 2)
        for check in config.checks:
            observed = client.exec_query(primary, config.namespace, check.query, check.database)
            check_result: dict[str, Any] = {"name": check.name, "database": check.database, "observedSha256": hashlib.sha256(observed.encode()).hexdigest()}
            if check.max_age_seconds is not None:
                try:
                    observed_time = dt.datetime.fromisoformat(observed.replace("Z", "+00:00"))
                    if observed_time.tzinfo is None:
                        raise ValueError("timestamp lacks timezone")
                    age = (dt.datetime.now(dt.timezone.utc) - observed_time).total_seconds()
                    check_result["ageSeconds"] = round(age, 2)
                    check_result["maxAgeSeconds"] = check.max_age_seconds
                    passed = 0 <= age <= check.max_age_seconds
                except ValueError:
                    passed = False
                    check_result["error"] = "query did not return a timestamp with timezone"
            else:
                passed = observed == check.expected
            check_result["passed"] = passed
            result["checks"].append(check_result)
            if not passed:
                raise DrillError(f"Check {check.name!r} did not meet its assertion")
        result["status"] = "passed"
    except (DrillError, ValueError, KeyError) as exc:
        result["error"] = str(exc)
        result["status"] = "failed"
        if not created:
            result["phase"] = "preflight"
    finally:
        if created and name and not (result["status"] == "failed" and config.retain_on_failure):
            try:
                client.delete_owned_cluster(name, config.namespace, run_id)
                cleanup_deadline = monotonic() + 60
                remaining = client.list_pvcs(name, config.namespace)
                while remaining and monotonic() < cleanup_deadline:
                    sleep(2)
                    remaining = client.list_pvcs(name, config.namespace)
                if remaining:
                    raise DrillError(f"Drill PVCs still exist after Cluster deletion: {', '.join(remaining)}")
                result["cleanup"] = "cluster-and-pvcs-deleted"
            except DrillError as exc:
                result["cleanup"] = "failed"
                result["cleanupError"] = str(exc)
                result["status"] = "failed"
        elif created:
            result["cleanup"] = "retained-for-investigation"
        result["finishedAt"] = dt.datetime.now(dt.timezone.utc).isoformat()
        result["durationSeconds"] = round(monotonic() - started, 2)
    return result
