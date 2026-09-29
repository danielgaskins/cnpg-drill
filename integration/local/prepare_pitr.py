"""Create a committed PITR fixture and wait until its WAL segment is archived."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path


def sql(query: str) -> str:
    result = subprocess.run(
        ["kubectl", "-n", "cnpg-drill-test", "exec", "app-db-1", "-c", "postgres", "--",
         "psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1", "-U", "postgres", "-d", "postgres", "-c", query],
        check=True, capture_output=True, text=True, timeout=60,
    )
    return result.stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    # Each statement runs through a separate psql call so the recorded target
    # is after the first UPDATE commits and before the second UPDATE begins.
    sql("UPDATE public.drill_fixture SET marker='at-pitr' WHERE id=1")
    target = sql("SELECT to_char(clock_timestamp(), 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')")
    time.sleep(1)
    sql("UPDATE public.drill_fixture SET marker='after-pitr' WHERE id=1")
    wal_name = sql("SELECT pg_walfile_name(pg_current_wal_lsn())")
    sql("SELECT pg_switch_wal()")

    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        archived = sql("SELECT coalesce(last_archived_wal, '') FROM pg_stat_archiver")
        if archived >= wal_name:
            break
        time.sleep(2)
    else:
        raise RuntimeError(f"WAL segment {wal_name} was not archived within 180 seconds")

    config = {
        "namespace": "cnpg-drill-test", "cluster": "app-db", "timeoutSeconds": 600,
        "maxBackupAgeSeconds": 7200, "targetTime": target,
        "checks": [{"name": "pitr-fixture", "query": "SELECT marker FROM public.drill_fixture WHERE id=1", "expected": "at-pitr"}],
    }
    args.output.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    print(f"PITR target {target}; WAL segment {wal_name} archived; config {args.output}")


if __name__ == "__main__":
    main()
