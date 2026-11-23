"""Transport contracts for account-owned web catalogs and projects."""

import json
import unittest
from contextlib import nullcontext
from types import SimpleNamespace

from webaiprovider import MemoryCredentialStore
from webaiprovider.providers.chatgpt.auth import BrowserSession
from webaiprovider.providers.chatgpt.catalogs import parse_model_catalog
from webaiprovider.providers.chatgpt.client import ChatGPTClient
from webaiprovider.transport.base import Transport

from accounts.shared import AccountCatalog, CatalogChoice, CatalogModel
from tests.provider_fakes import CATALOG

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
        self.requests = []
        requests = self.requests

        class DiscoveryTransport(Transport):
            def __init__(self):
                super().__init__("https://chatgpt.com")

            def send(self, method, path, **options):
                requests.append((method, path, options))
                if "/backend-api/models" in path:
                    value = CATALOG
                elif path.endswith("/sidebar"):
                    value = {
                        "items": [
                            PROJECT,
                            {"gizmo": {"id": "g-custom", "display": {"name": "GPT"}}},
                        ],
                        "cursor": "next",
                    }
                elif path.endswith("/conversations"):
                    value = {
                        "items": [{"id": "remote-chat", "title": "Project conversation"}],
                        "cursor": "next-chat",
                    }
                else:
                    value = PROJECT["gizmo"]
                return SimpleNamespace(
                    status_code=200,
                    headers={"content-type": "application/json"},
                    content=json.dumps(value).encode(),
                    text=json.dumps(value),
                    json=lambda v=value: v,
                )

            def stream(self, method, path, **options):
                return nullcontext()

            def close(self):
                pass

        store = MemoryCredentialStore(
            {
                "version": 1,
                "url": "https://chatgpt.com/backend-api/conversations",
                "headers": {"authorization": "Bearer token"},
            }
        )
        self.session = BrowserSession(store)
        self.client = ChatGPTClient(
            self.session, authenticate=False, transport=DiscoveryTransport()
        )
        self.addCleanup(self.client.close)

    def test_catalog_is_cached_and_roundtrips_through_account_catalog(self):
        parsed = parse_model_catalog(CATALOG)
        models = tuple(
            CatalogModel(
                id=m.id,
                title=m.title or m.id,
                efforts=tuple(
                    CatalogChoice(e.id, e.title, is_default=e.id == m.default_effort)
                    for e in m.efforts
                ),
                default_effort=m.default_effort,
                chat_title=m.chat_title,
                is_default=m.id == parsed.default_model,
                metadata=dict(m.metadata),
            )
            for m in parsed.models
            if m.chat_selectable
        )
        default_item = next(
            (m for m in models if m.id == parsed.default_model), models[0] if models else None
        )
        raw_catalog = AccountCatalog(parsed.state, models, default_item)
        catalog = AccountCatalog.from_dict(json.loads(json.dumps(raw_catalog.to_dict())))
        self.assertEqual(
            [m.id for m in catalog.chat_models], ["account-thinking", "account-instant"]
        )
        self.assertEqual(catalog.effort("account-thinking"), "standard")
        self.assertEqual(catalog.chat_models[0].efforts[0].title, "Standard")
        self.assertIsNone(catalog.effort("account-instant"))

        first = self.client.model_catalog()
        second = self.client.model_catalog()
        self.assertIs(first, second)
        self.assertEqual(len(self.requests), 1)
        self.client.model_catalog(refresh=True)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.requests[0][2]["params"]["is_gizmo"], "false")
