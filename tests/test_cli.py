import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cnpg_drill import cli

from test_core import FakeClient


class CliTest(unittest.TestCase):
    def test_plan_outputs_manifest_without_creating_cluster(self):
        fake = FakeClient()
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "config.json"
            config.write_text(json.dumps({"namespace": "production", "cluster": "app-db"}))
            output = io.StringIO()
            with patch.object(cli, "Kubectl", return_value=fake), contextlib.redirect_stdout(output):
                code = cli.main(["plan", "--config", str(config)])
        self.assertEqual(code, 0)
        self.assertIsNone(fake.created)
        self.assertEqual(json.loads(output.getvalue())["kind"], "Cluster")

    def test_run_writes_machine_readable_failure_report(self):
        fake = FakeClient(check_result="0")
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "config.json"
            report = Path(folder) / "report.json"
            config.write_text(json.dumps({"namespace": "production", "cluster": "app-db"}))
            with patch.object(cli, "Kubectl", return_value=fake), contextlib.redirect_stdout(io.StringIO()):
                code = cli.main(["run", "--config", str(config), "--report", str(report)])
            result = json.loads(report.read_text())
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["checks"][0]["passed"], False)

    def test_run_returns_code_two_for_preflight_failure(self):
        fake = FakeClient()
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / "config.json"
            config.write_text(json.dumps({"namespace": "production", "cluster": "app-db", "maxBackupAgeSeconds": 1}))
            with patch.object(cli, "Kubectl", return_value=fake), contextlib.redirect_stdout(io.StringIO()):
                code = cli.main(["run", "--config", str(config)])
        self.assertEqual(code, 2)
        self.assertIsNone(fake.created)


if __name__ == "__main__":
    unittest.main()
