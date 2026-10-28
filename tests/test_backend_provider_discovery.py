"""Transport contracts for account-owned web catalogs and projects."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from backend import BackendService
from providers.chatgpt import BrowserSession, ChatGPTClient, SessionPaths
from providers.models import ModelCatalog
from providers.tests.fixtures import CATALOG

PROJECT = {
    "gizmo": {
        "gizmo": {
            "id": "g-p-work",
            "gizmo_type": "snorlax",
            "display": {"name": "Work"},
            "instructions": "Use concise answers.",
        },
        "files": [{"file_id": "file-ref", "name": "Reference.pdf", "type": "application/pdf"}],
    }
}


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.session = BrowserSession(
            SessionPaths(root / "session", root / "cache", root / "profile")
        )
        self.client = ChatGPTClient(self.session, authenticate=False)
        self.client.http.close()
        self.addCleanup(self.client.close)
        self.requests = []

        def respond(request):
            self.requests.append(request)
            if request.url.path == "/backend-api/models":
                value = CATALOG
            elif request.url.path.endswith("/sidebar"):
                value = {
                    "items": [PROJECT, {"gizmo": {"id": "g-custom", "display": {"name": "GPT"}}}],
                    "cursor": "next",
                }
            elif request.url.path.endswith("/conversations"):
                value = {
                    "items": [{"id": "remote-chat", "title": "Project conversation"}],
                    "cursor": "next-chat",
                }
            else:
                value = PROJECT["gizmo"]
            return httpx.Response(200, json=value)

        self.client.http = httpx.Client(transport=httpx.MockTransport(respond))

    def test_catalog_is_account_data_cached_and_roundtrips_through_ipc(self):
        backend = BackendService()
        value, stop = backend.handle_daemon_command(self.client, {"command": "models"})
        catalog = ModelCatalog.from_dict(json.loads(json.dumps(value))["catalog"])
        self.assertFalse(stop)
        self.assertEqual([m.id for m in catalog.models], ["account-thinking", "account-instant"])
        self.assertEqual(catalog.effort("account-thinking"), "standard")
        self.assertEqual(catalog.models[0].efforts[0].title, "Standard")
        self.assertIsNone(catalog.effort("account-instant"))
        self.client.model_catalog()
        self.assertEqual(len(self.requests), 1)
        self.client.model_catalog(refresh=True)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.requests[0].url.params["is_gizmo"], "false")

    def test_old_daemon_is_replaced_before_new_discovery_commands(self):
        backend = BackendService()
        with (
            patch.object(
                backend, "daemon_request", side_effect=[{"ready": True}, {"stopping": True}]
            ) as request,
            patch.object(backend, "daemon_is_ready", return_value=False),
            patch.object(backend, "start_daemon") as start,
        ):
            backend.ensure_daemon(Path("unused"), "http://localhost:9222")
        self.assertEqual(request.call_args_list[-1].args[1], {"command": "stop"})
        start.assert_called_once()
