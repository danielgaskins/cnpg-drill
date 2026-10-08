#!/usr/bin/env python3
"""Check the rendered chart's destructive and exec permissions (requires PyYAML)."""

import json
import os
import subprocess
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
HELM = os.environ.get("HELM", "helm")
CHART = str(ROOT / "deploy/helm/cnpg-drill")


def render(release="test", *overrides):
    result = subprocess.run(
        [HELM, "template", release, CHART, "--namespace", "production",
         "--set", "cluster=source", *overrides],
        text=True, capture_output=True,
    )
    return result


def allows(rules, resource, verb, name):
    return any(
        resource in rule["resources"] and verb in rule["verbs"]
        and ("resourceNames" not in rule or name in rule["resourceNames"])
        for rule in rules
    )


for release, options, source_pod, logs in (
    ("test", [], None, False),
    ("custom", ["--set", "drillClusterName=reserved-drill", "--set",
                "rbac.sourcePrimaryPodName=source-3", "--set", "rbac.recoveryLogs=true"],
     "source-3", True),
    ("a" * 53, [], None, False),
):
    result = render(release, *options)
    assert result.returncode == 0, result.stderr
    docs = list(yaml.safe_load_all(result.stdout))
    role = next(doc for doc in docs if doc and doc["kind"] == "Role")
    config = json.loads(next(doc for doc in docs if doc and doc["kind"] == "ConfigMap")["data"]["drill.json"])
    drill = config["drillClusterName"]
    pod = drill + "-1"
    rules = role["rules"]
    assert len(pod) <= 63 and drill != "source"
    assert allows(rules, "clusters", "get", "source")
    assert allows(rules, "clusters", "get", drill)
    assert allows(rules, "clusters", "delete", drill)
    assert not allows(rules, "clusters", "delete", "source")
    assert not allows(rules, "clusters", "get", "unrelated")
    for verb in ("list", "watch"):
        assert allows(rules, "clusters", verb, drill)
        assert not allows(rules, "clusters", verb, "source")
    assert allows(rules, "clusters", "create", "unrelated")  # RBAC creation limit is explicit.
    for resource, verb in (("pods", "get"), ("pods/exec", "create")):
        assert allows(rules, resource, verb, pod)
        assert not allows(rules, resource, verb, "unrelated-1")
        assert not allows(rules, resource, verb, "source-1")
        assert not allows(rules, resource, verb, drill + "-2")
        if source_pod:
            assert allows(rules, resource, verb, source_pod)
    assert allows(rules, "pods/log", "get", "unrelated-1") == logs
    assert not any("secrets" in rule["resources"] for rule in rules)
    job = next(doc for doc in docs if doc and doc["kind"] == "CronJob")
    assert job["spec"]["suspend"] is True
    assert job["spec"]["concurrencyPolicy"] == "Forbid"

for bad in ("source", "a" * 62, "Bad_Name"):
    result = render("test", "--set", "drillClusterName=" + bad)
    assert result.returncode != 0 and "drillClusterName" in result.stderr, result.stderr
result = render("test", "--set", "rbac.sourcePrimaryPodName=Bad_Name")
assert result.returncode != 0 and "sourcePrimaryPodName" in result.stderr
print("Chart RBAC checks passed: named deletion and exec, optional source access, name limits and defaults.")
