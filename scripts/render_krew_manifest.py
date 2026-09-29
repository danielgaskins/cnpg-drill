"""Render a Krew manifest after uploading the exact release archive."""

from __future__ import annotations

import argparse
import hashlib
import pathlib


parser = argparse.ArgumentParser()
parser.add_argument("--repo", required=True, help="GitHub owner/repo containing the release")
parser.add_argument("--version", required=True, help="Semver with v prefix")
parser.add_argument("--archive", type=pathlib.Path, required=True)
args = parser.parse_args()
if "/" not in args.repo or not args.version.startswith("v"):
    parser.error("--repo must be owner/repo and --version must start with v")
digest = hashlib.sha256(args.archive.read_bytes()).hexdigest()
print(f"""apiVersion: krew.googlecontainertools.github.com/v1alpha2
kind: Plugin
metadata:
  name: cnpg-drill
spec:
  version: {args.version}
  homepage: https://github.com/{args.repo}
  shortDescription: Prove CloudNativePG backup recovery
  description: |
    Restores a CloudNativePG Barman backup into a disposable cluster,
    runs read-only checks, and reports whether recovery succeeded.
  caveats: |
    Requires Python 3.10+, kubectl, CloudNativePG, and the Barman Cloud plugin.
    Review the recovery plan and RBAC before running against production.
  platforms:
  - selector:
      matchExpressions:
      - {{key: os, operator: In, values: [darwin, linux]}}
    uri: https://github.com/{args.repo}/releases/download/{args.version}/{args.archive.name}
    sha256: {digest}
    files:
    - from: kubectl-cnpg_drill
      to: .
    - from: LICENSE
      to: .
    bin: kubectl-cnpg_drill
""")
