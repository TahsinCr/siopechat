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


class DaemonIsolationTests(unittest.TestCase):
    def test_invalid_request_and_slow_client_do_not_block_ping(self):
        import os
        import socket
        import time
        from concurrent.futures import ThreadPoolExecutor

        from backend import BackendService

        if os.name == "nt" or not hasattr(socket, "AF_UNIX"):
            self.skipTest("legacy Unix IPC")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "ipc.sock"
            script = """
import sys,time
from pathlib import Path
from contextlib import nullcontext
from backend import BackendService
root = Path(sys.argv[1])
class Client:
    def list_conversations(self, maximum):
        (root / 'started').touch()
        until = time.monotonic() + 5
        while not (root / 'release').exists() and time.monotonic() < until:
            time.sleep(.01)
        return []
service = BackendService(root)
service.create_client = lambda _: nullcontext(Client())
service.run_daemon(root / 'ipc.sock', '')
"""
            process = subprocess.Popen(
                [sys.executable, "-c", script, folder],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            backend = BackendService(Path(folder))
            try:
                until = time.monotonic() + 5
                while not path.exists() and process.poll() is None and time.monotonic() < until:
                    time.sleep(0.01)
                self.assertTrue(path.exists())
                with socket.socket(socket.AF_UNIX) as connection:
                    connection.connect(str(path))
                    connection.sendall(b"{bad")
                    connection.shutdown(socket.SHUT_WR)
                    self.assertFalse(backend.receive_json(connection)["ok"])
                with self.assertRaises(Exception):
                    backend.daemon_request(path, {"command": "unknown"})
                with ThreadPoolExecutor(1) as pool:
                    slow = pool.submit(backend.daemon_request, path, {"command": "list"})
                    until = time.monotonic() + 2
                    while not (Path(folder) / "started").exists() and time.monotonic() < until:
                        time.sleep(0.01)
                    self.assertTrue(backend.daemon_request(path, {"command": "ping"})["ready"])
                    (Path(folder) / "release").touch()
                    self.assertEqual(slow.result(2), {"items": []})
                backend.daemon_request(path, {"command": "stop"})
                self.assertEqual(process.wait(3), 0)
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(5)
                process.stderr.close()
