"""Application storage isolation and repository-backed session status."""

import tempfile
import unittest
from pathlib import Path

from bootstrap import create_application


class ApplicationPathsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()

    def test_custom_application_root_keeps_data_and_runtime_under_root(self):
        app = create_application(self.root)
        try:
            self.assertEqual(app.get("data_root"), self.root / "data")
            self.assertEqual(app.get("runtime_root"), self.root / ".runtime")
            key = app.accounts.add("claude", "Claude")
            store = app.accounts.credentials(key)
            store.save({"sessionKey": "fixture"})
            self.assertTrue(store.exists())
            self.assertEqual(store.load(), {"sessionKey": "fixture"})
            self.assertTrue(app.accounts.repository.path.is_relative_to(self.root))
        finally:
            app.runtime.close()

    def test_status_reads_credentials_from_the_account_repository(self):
        app = create_application(self.root)
        try:
            key = app.accounts.add("chatgpt", "Personal")
            status = app.accounts.status(key)
            self.assertFalse(status["saved_snapshot"])
            app.accounts.credentials(key).save({"headers": {"authorization": "Bearer t"}})
            status = app.accounts.status(key)
            self.assertTrue(status["saved_snapshot"])
        finally:
            app.runtime.close()
