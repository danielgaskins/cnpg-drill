"""Build a dependency-free kubectl plugin archive for Linux/macOS."""

from __future__ import annotations

import hashlib
import os
import pathlib
import tarfile
import zipapp


root = pathlib.Path(__file__).resolve().parents[1]
dist = root / "dist"
dist.mkdir(exist_ok=True)
binary = dist / "kubectl-cnpg_drill"
zipapp.create_archive(
    root / "src", target=binary, interpreter="/usr/bin/env python3",
    main="cnpg_drill.cli:main",
    filter=lambda path: "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"},
)
binary.chmod(binary.stat().st_mode | 0o111)
archive = dist / "cnpg-drill_0.1.0_unix.tar.gz"
with tarfile.open(archive, "w:gz") as tar:
    tar.add(binary, arcname=binary.name)
    tar.add(root / "LICENSE", arcname="LICENSE")
digest = hashlib.sha256(archive.read_bytes()).hexdigest()
(dist / (archive.name + ".sha256")).write_text(f"{digest}  {archive.name}\n", encoding="ascii")
print(f"{archive}: sha256 {digest}")
