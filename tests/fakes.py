"""Controllable offline providers shared by behavioral tests (webaiprovider v2)."""

from __future__ import annotations

from webaiprovider import Capability
from webaiprovider.resources.model.model import ModelCatalogState

from accounts.shared import AccountCatalog, CatalogChoice, CatalogModel


class Accounts:
    """Small in-memory account service for terminal UI tests."""

    def __init__(self):
        self.selected = "chatgpt/default"
        self.entries = [
            {
                "key": "chatgpt/default",
                "provider": "chatgpt",
                "id": "default",
                "label": "GPT",
                "icon": "◈",
            },
            {
                "key": "gemini/personal",
                "provider": "gemini",
                "id": "personal",
                "label": "Gemini",
                "icon": "✦",
            },
        ]
        self.active_conversations = {}

    def capabilities(self, key):
        return (
            frozenset({Capability.PROJECTS, Capability.PROJECT_CREATE})
            if key.startswith("chatgpt")
            else frozenset()
        )

    def active(self):
        return self.selected

    def cached_models(self, _key):
        return None

    def items(self):
        return self.entries

    def status(self, key):
        return {"saved_snapshot": False, "authenticated": False, "account": key}

    def queue_active(self, key, value):
        self.active_conversations[key] = value

    def select(self, key):
        self.selected = key

    def list_chats(self, selected=None, cursor=None, account_keys=None):
        return {"items": [], "cursor": None}

    def callbacks(self, key):
        model = CatalogModel(key, key, (CatalogChoice("standard", "Standart"),))
        return {
            "load_catalog": lambda: AccountCatalog(ModelCatalogState.AVAILABLE, (model,)),
            "list_projects_callback": lambda cursor=None: {"items": [], "cursor": None},
            "session_action": lambda _: self.status(key),
            "attach_file_callback": lambda path: {"id": "file-test", "name": "demo.pdf"},
            "load_conversation": lambda _: {"messages": []},
        }
