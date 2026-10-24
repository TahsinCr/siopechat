"""Controllable offline providers shared by behavioral tests."""

import asyncio
import threading
from unittest.mock import Mock

from providers import Capability, ConversationBound, SendRequest, TextDelta
from providers.models import Choice, FeatureState, Model, ModelCatalog


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
        self.backend = Mock()

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

    def cache(self, key):
        return self.backend

    def select(self, key):
        self.selected = key

    def list_chats(self, selected=None, cursor=None, account_keys=None):
        return {"items": [], "cursor": None}

    def callbacks(self, key):
        model = Model(key, key, (Choice("standard", "Standart"),))
        return {
            "load_catalog": lambda: ModelCatalog(FeatureState.AVAILABLE, (model,)),
            "list_projects_callback": lambda cursor=None: {"items": [], "cursor": None},
            "session_action": lambda _: self.status(key),
            "attach_file_callback": lambda path: {"id": "file-test", "name": "demo.pdf"},
            "load_conversation": lambda _: {"messages": []},
        }


class FakeClient:
    """Controllable synchronous ChatGPT transport for provider tests."""

    def __init__(self, session):
        self.session = session
        self.closed = threading.Event()
        self.started = threading.Event()
        self.failure = None
        self.raw = {
            "id": "remote",
            "title": "Example",
            "messages": [
                {
                    "id": "message",
                    "author": {"role": "assistant"},
                    "channel": "analysis",
                    "content": {"parts": ["Visible summary", {"asset_pointer": "image-ref"}]},
                    "citations": [{"url": "https://example.test"}],
                }
            ],
        }

    def request(self, method, url, **kwargs):
        self.deleted = (method, url)
        return None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.closed.set()

    def verify(self):
        return {"items": []}

    def list_page(self, offset, limit):
        return {"items": [self.raw]}

    def get_conversation(self, conversation_id):
        return self.raw

    def send_prompt(self, conversation_id, prompt, on_event):
        on_event({"type": "delta", "text": "partial"})
        self.started.set()
        if not self.closed.wait(2):
            raise RuntimeError("test did not release stream")
        if self.failure:
            raise self.failure
        on_event({"type": "replace", "text": "finished"})
        return "remote", self.raw, set()


class ControlledProvider:
    capabilities = frozenset(Capability)

    def __init__(self, provider_id="gpt"):
        self.id = provider_id
        self.started = asyncio.Queue()
        self.releases = {}
        self.closed = set()
        self.calls = []
        self.failure = None
        self.failures = {}

    async def stream(self, request):
        self.calls.append(request)
        release = self.releases.setdefault(request.chat_id, asyncio.Event())
        await self.started.put(request.chat_id)
        try:
            yield ConversationBound(f"remote-{request.chat_id}")
            yield TextDelta("partial")
            await release.wait()
            failure = self.failures.get(request.chat_id, self.failure)
            if failure is not None:
                raise failure
            yield TextDelta(" response")
        finally:
            self.closed.add(request.chat_id)

    def release(self, chat_id):
        self.releases.setdefault(chat_id, asyncio.Event()).set()


def request(chat_id="one", provider="gpt", account="default", **kwargs):
    return SendRequest(provider, account, chat_id, "hello", **kwargs)
