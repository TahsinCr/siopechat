import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class CoreCliTests(unittest.TestCase):
    def test_workspace_lifecycle_through_public_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            command = [
                sys.executable,
                "-m",
                "bootstrap",
                "workspace",
                "--db",
                str(Path(directory) / "workspaces.sqlite3"),
            ]

            def call(*args):
                result = subprocess.run(
                    command + list(args), capture_output=True, text=True, timeout=10, check=True
                )
                return json.loads(result.stdout)

            created = call("create", "Araştırma")
            self.assertEqual(call("show", created["id"]), created)
            self.assertEqual(call("list"), [{"id": created["id"], "name": "Araştırma"}])
            self.assertEqual(call("delete", created["id"]), {"deleted": True})
            self.assertEqual(call("list"), [])
