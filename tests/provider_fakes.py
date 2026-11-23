"""Provider protocol fixtures used by SiopeChat's application tests."""

import threading
import time
from http.cookiejar import Cookie

from webaiprovider.core.timing import _STREAM_PAUSED
from webaiprovider.providers.chatgpt.catalogs import parse_model_catalog

CATALOG = {
    "default_model_slug": "account-thinking",
    "models": [
        {
            "slug": "account-thinking",
            "title": "Account Thinking",
            "thinking_efforts": [
                {"thinking_effort": "standard", "full_label": "Standard", "is_default": True},
                {"thinking_effort": "extended", "title": "Extended"},
            ],
        },
        {"slug": "account-instant", "title": "Account Instant"},
        {"slug": "locked", "is_available": False},
    ],
}


def notebook():
    source = [None] * 22
    source[1], source[2], source[5] = 11, "Research notes", "notebooks/abcdef/sources/source1"
    detail = [None] * 15
    detail[0], detail[1], detail[10], detail[12] = (
        "My notebook",
        "Notebook description",
        [[source], []],
        [2, None, None, None, 3],
    )
    return ["notebooks/abcdef", detail, ["Use citations"]]


def cookie(name, value="secret", domain=".google.com", expires=None):
    return Cookie(
        0,
        name,
        value,
        None,
        False,
        domain,
        True,
        domain.startswith("."),
        "/",
        True,
        True,
        expires,
        False,
        None,
        None,
        {},
        False,
    )


class FakeClient:
    """Controllable synchronous client for ChatGPT provider lifecycle tests."""

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

    def __exit__(self, *_args):
        self.close()

    def close(self):
        self.closed.set()

    def verify(self):
        return {"items": []}

    def model_catalog(self, refresh=False):
        del refresh
        return parse_model_catalog(CATALOG)

    def list_page(self, offset, limit):
        return {"items": [self.raw]}

    def get_conversation(self, conversation_id):
        return self.raw

    def iter_send_prompt(self, conversation_id, prompt, **kwargs):
        del conversation_id, prompt, kwargs
        yield ("raw", {"type": "delta", "text": "partial"})
        self.started.set()
        deadline = time.monotonic() + 2.0
        while not self.closed.is_set() and time.monotonic() < deadline:
            yield ("paused", _STREAM_PAUSED)
            self.closed.wait(0.01)
        if self.failure:
            raise self.failure
        if not self.closed.is_set():
            raise RuntimeError("test did not release stream")
        yield ("raw", {"type": "replace", "text": "finished"})
        yield ("completed", ("remote", self.raw, None))

    def send_prompt(self, conversation_id, prompt, on_event):
        on_event({"type": "delta", "text": "partial"})
        self.started.set()
        if not self.closed.wait(2):
            raise RuntimeError("test did not release stream")
        if self.failure:
            raise self.failure
        on_event({"type": "replace", "text": "finished"})
        return "remote", self.raw, set()
