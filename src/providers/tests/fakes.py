"""Small transport and cookie fakes shared by offline provider tests."""

import threading
from http.cookiejar import Cookie


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
