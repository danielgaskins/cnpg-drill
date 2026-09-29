"""Run one real drill and verify that its source Cluster spec stayed unchanged."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from cnpg_drill.core import API, Config, Kubectl, run_drill


parser = argparse.ArgumentParser()
parser.add_argument("--config", required=True, type=Path)
parser.add_argument("--report", required=True, type=Path)
args = parser.parse_args()

config = Config.from_dict(json.loads(args.config.read_text(encoding="utf-8")))
client = Kubectl()
before = client.get(API, config.cluster, config.namespace)["spec"]
report = run_drill(client, config)
after = client.get(API, config.cluster, config.namespace)["spec"]
report["sourceSpecUnchanged"] = before == after
if not report["sourceSpecUnchanged"]:
    report["status"] = "failed"
    report["sourceSpecError"] = "Source Cluster spec changed during the drill; investigate before relying on this result"
args.report.parent.mkdir(parents=True, exist_ok=True)
args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(json.dumps(report, indent=2, sort_keys=True))
sys.exit(0 if report["status"] == "passed" else 1)
