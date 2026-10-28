"""Saved web sessions: no browser processes, redirects or mutation retries."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx

from backend import BackendService
from providers.session import WebSession, save_session


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

    def test_custom_application_root_cannot_overwrite_real_session(self):
        backend = BackendService(Path(self.directory.name))
        self.assertTrue(backend.auth_store_path().is_relative_to(Path(self.directory.name)))
        self.assertTrue(
            backend.create_session().paths.profile.is_relative_to(Path(self.directory.name))
        )
