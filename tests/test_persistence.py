"""Shared persistence and source validation preserve existing IO contracts."""

import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from providers.chatgpt import BrowserSession, SessionPaths
from providers.projects import source_file
from utils.files import atomic_json_write


class AtomicPersistenceTests(unittest.TestCase):
    def test_json_format_permissions_and_failed_replace_preserve_previous_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "private" / "record.json"
            value = {"title": "Türkçe", "version": 1}
            atomic_json_write(path, value, indent=2, allow_nan=False, directory_mode=0o700)
            self.assertEqual(
                path.read_text(), json.dumps(value, ensure_ascii=False, indent=2) + "\n"
            )
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            for target in ("os.replace", "os.fsync"):
                with patch("utils.files." + target, side_effect=OSError("disk error")):
                    with self.assertRaises(OSError):
                        atomic_json_write(path, {"version": 2})
                self.assertEqual(json.loads(path.read_text()), value)
                self.assertEqual(list(path.parent.iterdir()), [path])
            with self.assertRaises(ValueError):
                atomic_json_write(path, {"invalid": float("nan")}, allow_nan=False)
            self.assertEqual(json.loads(path.read_text()), value)
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_concurrent_writes_never_publish_partial_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "record.json"

            def write(index):
                atomic_json_write(path, {"version": index, "body": str(index) * 1000})
                record = json.loads(path.read_text())
                self.assertEqual(record["body"], str(record["version"]) * 1000)

            with ThreadPoolExecutor(max_workers=8) as workers:
                list(workers.map(write, range(32)))
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_chatgpt_session_writer_keeps_wire_format_and_handles_concurrent_saves(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "session.json"
            session = BrowserSession(SessionPaths(path, root / "cache", root / "profile"))
            with ThreadPoolExecutor(max_workers=8) as workers:
                list(
                    workers.map(
                        lambda index: session.save("https://chatgpt.com/", {"test": str(index)}),
                        range(24),
                    )
                )
            record = json.loads(path.read_text())
            self.assertEqual(record["version"], 1)
            self.assertEqual(record["url"], "https://chatgpt.com/")
            self.assertTrue(path.read_text().endswith("\n"))
            self.assertEqual(list(root.iterdir()), [path])
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class ProjectSourceTests(unittest.TestCase):
    def test_source_validation_preserves_size_limit_resolution_and_error_messages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "source.bin"
            with path.open("wb") as stream:
                stream.truncate(20 * 1024 * 1024)
            self.assertEqual(source_file(path), path.resolve())
            with path.open("ab") as stream:
                stream.write(b"x")
            for message in ("Provider-specific message", "Source must be at most 20 MiB"):
                with self.assertRaisesRegex(ValueError, message):
                    source_file(path, error=message)
            with self.assertRaises(ValueError):
                source_file(root)
            with self.assertRaises(FileNotFoundError):
                source_file(root / "missing")
