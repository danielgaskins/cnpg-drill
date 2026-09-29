"""Command-line interface for local use and Kubernetes CronJobs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .core import Config, DrillError, Kubectl, prepare, run_drill


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cnpg-drill", description="Verify a CloudNativePG backup with a disposable recovery cluster")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--kubectl", default="kubectl", help="kubectl executable path")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("plan", "run"):
        item = sub.add_parser(command)
        item.add_argument("--config", required=True, type=Path, help="JSON drill config")
        if command == "run":
            item.add_argument("--report", type=Path, help="write JSON report to this path")
    args = parser.parse_args(argv)
    try:
        config = Config.from_dict(json.loads(args.config.read_text(encoding="utf-8")))
        client = Kubectl(args.kubectl)
        if args.command == "plan":
            print(json.dumps(prepare(client, config), indent=2, sort_keys=True))
            return 0
        report = run_drill(client, config)
        encoded = json.dumps(report, indent=2, sort_keys=True)
        print(encoded)
        if args.report:
            args.report.write_text(encoded + "\n", encoding="utf-8")
        return 0 if report["status"] == "passed" else 1
    except (DrillError, OSError, json.JSONDecodeError) as exc:
        print(f"cnpg-drill: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
