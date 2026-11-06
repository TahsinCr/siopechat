"""Saved web sessions: no browser processes, redirects or mutation retries."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
from webai_provider.api import WebSession, save_session

from backend import BackendService


class SessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "session.json"
        save_session(self.path, {"sessionKey": "fixture"})
        self.client = SimpleNamespace(
            request=AsyncMock(return_value=httpx.Response(200, json={"ok": True})),
            cookies={"sessionKey": "rotated"},
            close=AsyncMock(),
        )
        self.factory = Mock(return_value=self.client)
        self.session = WebSession(
            "https://claude.ai", self.path, "sessionKey", client_factory=self.factory
        )
        self.addAsyncCleanup(self.session.close)

    def test_custom_application_root_keeps_data_and_runtime_under_root(self):
        root = Path(self.directory.name).resolve()
        backend = BackendService(root)
        self.assertEqual(backend.data_root, root / "data")
        backend._session_credentials.save({"sessionKey": "fixture"})
        self.assertTrue(backend._session_credentials.exists())
        self.assertTrue(backend.create_session().paths.profile.is_relative_to(root))
        self.assertEqual(backend.default_socket_path(), root / ".runtime" / "daemon.sock")

    def test_daemon_status_reads_credentials_from_the_account_repository(self):
        backend = BackendService(Path(self.directory.name))
        client = SimpleNamespace(session=SimpleNamespace(is_ready=lambda: False))
        status = backend.handle_daemon_command(client, {"command": "status"})[0]
        self.assertFalse(status["session_saved"])
        backend._session_credentials.save({"sessionKey": "fixture"})
        status = backend.handle_daemon_command(client, {"command": "status"})[0]
        self.assertTrue(status["session_saved"])
