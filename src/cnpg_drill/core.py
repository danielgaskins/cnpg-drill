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
from dataclasses import dataclass
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

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Config:
        if not isinstance(raw, dict):
            raise DrillError("Config must be a JSON object")
        allowed = {"namespace", "cluster", "recoveryObjectStore", "timeoutSeconds", "pollSeconds", "maxBackupAgeSeconds", "targetTime", "checks", "retainOnFailure"}
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
            if not isinstance(item, dict) or not {"name", "query"} <= set(item) or set(item) - {"name", "query", "expected", "maxAgeSeconds"}:
                raise DrillError("Each check needs name, query, and one comparison")
            if ("expected" in item) == ("maxAgeSeconds" in item):
                raise DrillError("Each check needs exactly one of expected or maxAgeSeconds")
            name, query, expected = item["name"], item["query"], item.get("expected")
            max_age = item.get("maxAgeSeconds")
            if not isinstance(name, str) or not name or len(name) > 80:
                raise DrillError("Check name must be 1..80 characters")
            if "expected" in item and (not isinstance(expected, str) or len(expected) > 4096):
                raise DrillError("Check expected must be a string up to 4096 characters")
            if "maxAgeSeconds" in item and (type(max_age) is not int or not 1 <= max_age <= 31536000):
                raise DrillError("maxAgeSeconds must be 1..31536000")
            if not isinstance(query, str) or len(query) > 8192 or not re.match(r"^\s*(SELECT|WITH)\b", query, re.I) or ";" in query:
                raise DrillError(f"Check {name}: query must be one SELECT/WITH statement without semicolons")
            checks.append(Check(name, query.strip(), expected, max_age))
        retained = raw.get("retainOnFailure", False)
        if type(retained) is not bool:
            raise DrillError("retainOnFailure must be boolean")
        return cls(namespace, cluster, recovery_object_store, timeout, poll, max_backup_age, target, tuple(checks), retained)


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

    def exec_query(self, pod: str, namespace: str, query: str) -> str:
        # One read-only transaction. The config parser rejects semicolons in query.
        sql = f"BEGIN READ ONLY; {query}; COMMIT;"
        return self.call(["-n", namespace, "exec", pod, "-c", "postgres", "--", "psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "postgres", "-c", sql], timeout=60).strip()


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


def build_manifest(source: dict[str, Any], config: Config, *, name: str, backup: dict[str, Any] | None = None) -> dict[str, Any]:
    spec = source.get("spec", {})
    if spec.get("tablespaces"):
        raise DrillError("Tablespace clusters are not supported in v0.1; refusing incomplete restore")
    bootstrap = spec.get("bootstrap") or {}
    if bootstrap.get("recovery") or bootstrap.get("pg_basebackup"):
        raise DrillError("Replica or recovery-source clusters are not supported in v0.1")
    if bootstrap and "initdb" not in bootstrap:
        raise DrillError("Only default or initdb bootstrap sources are supported in v0.1")
    plugins = [p for p in spec.get("plugins", []) if p.get("name") == PLUGIN and p.get("enabled", True)]
    if len(plugins) != 1:
        raise DrillError("Source needs one enabled Barman Cloud plugin")
    params = plugins[0].get("parameters", {})
    object_name = params.get("barmanObjectName")
    if not isinstance(object_name, str) or not DNS_NAME.fullmatch(object_name):
        raise DrillError("Barman plugin needs a valid barmanObjectName")
    storage = spec.get("storage")
    if not isinstance(storage, dict) or not storage.get("size"):
        raise DrillError("Source Cluster needs storage.size")
    if len(name) > 63 or not DNS_NAME.fullmatch(name) or name == config.cluster:
        raise DrillError("Invalid or unsafe drill cluster name")
    recovery: dict[str, Any] = {"source": "backup-source"}
    if backup:
        recovery["recoveryTarget"] = {"backupID": backup["status"]["backupId"]}
    initdb = bootstrap.get("initdb") or {}
    for key in ("database", "owner", "secret"):
        if key in initdb:
            recovery[key] = copy.deepcopy(initdb[key])
    if config.target_time:
        recovery.setdefault("recoveryTarget", {})["targetTime"] = config.target_time
    external = {"name": "backup-source", "plugin": {"name": PLUGIN, "enabled": True, "parameters": {"barmanObjectName": config.recovery_object_store or object_name, "serverName": params.get("serverName") or config.cluster}}}
    output: dict[str, Any] = {
        "apiVersion": "postgresql.cnpg.io/v1", "kind": "Cluster",
        "metadata": {"name": name, "namespace": config.namespace, "labels": {"app.kubernetes.io/managed-by": "cnpg-drill", "cnpg-drill.dev/source": config.cluster}, "annotations": {"cnpg-drill.dev/source-uid": source.get("metadata", {}).get("uid", ""), "cnpg-drill.dev/backup-name": backup.get("metadata", {}).get("name", "") if backup else ""}},
        "spec": {"instances": 1, "storage": copy.deepcopy(storage), "bootstrap": {"recovery": recovery}, "externalClusters": [external]},
    }
    for key in ("imageName", "imageCatalogRef", "walStorage", "resources"):
        if key in spec:
            output["spec"][key] = copy.deepcopy(spec[key])
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
    manifest = build_manifest(source, config, name=name or new_name(config.cluster), backup=backup)
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
            if not source_config.get(field) or source_config.get(field) != recovery_config.get(field):
                raise DrillError(f"Recovery ObjectStore {field} must match source ObjectStore")
    return manifest


def run_drill(client: Kubectl, config: Config, *, sleep: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    started_at = dt.datetime.now(dt.timezone.utc)
    started = monotonic()
    result: dict[str, Any] = {"schemaVersion": 1, "source": f"{config.namespace}/{config.cluster}", "startedAt": started_at.isoformat(), "targetTime": config.target_time, "checks": [], "status": "error"}
    name: str | None = None
    created = False
    try:
        manifest = prepare(client, config)
        name = manifest["metadata"]["name"]
        result["drillCluster"] = name
        result["sourceClusterUID"] = manifest["metadata"]["annotations"]["cnpg-drill.dev/source-uid"]
        result["backupName"] = manifest["metadata"]["annotations"]["cnpg-drill.dev/backup-name"]
        result["backupID"] = manifest["spec"]["bootstrap"]["recovery"]["recoveryTarget"]["backupID"]
        result["backupAgeSeconds"] = int(manifest["metadata"]["annotations"]["cnpg-drill.dev/backup-age-seconds"])
        created = True
        # A create request can reach the API even if the client loses its response.
        # In that case, still try to remove the uniquely named drill Cluster.
        client.create(manifest, config.namespace)
        recovery_started = monotonic()
        deadline = started + config.timeout_seconds
        primary = None
        while monotonic() < deadline:
            cluster = client.get(API, name, config.namespace)
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
            observed = client.exec_query(primary, config.namespace, check.query)
            check_result: dict[str, Any] = {"name": check.name, "observedSha256": hashlib.sha256(observed.encode()).hexdigest()}
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
                client.delete(API, name, config.namespace)
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
