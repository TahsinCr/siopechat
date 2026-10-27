import asyncio
import threading
import unittest
from unittest.mock import patch

from providers.api import ConversationBound, SendRequest, TextDelta
from providers.chatgpt.adapter import ChatGPTProvider


class PersistentClient:
    instances = []

    def __init__(self, session):
        self.session = session
        self.closed = threading.Event()
        self.temporary_conversations = None
        type(self).instances.append(self)

    def send_prompt(self, conversation_id, prompt, on_event, **_options):
        on_event({"type": "delta", "text": "OK"})
        if prompt == "cancel me":
            self.closed.wait(2)
        return "temporary-chat", {"messages": []}, set()

    def close(self):
        self.closed.set()


class ChatGPTConnectionPoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_reuses_prepared_client_and_discards_it_after_cancel(self):
        PersistentClient.instances.clear()
        with patch("providers.chatgpt.adapter.ChatGPTClient", PersistentClient):
            provider = ChatGPTProvider(object(), client_factory=PersistentClient)

        with provider._client_scope() as prepared_client:
            pass
        self.assertEqual(len(PersistentClient.instances), 1)

        request = SendRequest("chatgpt", "default", "local", "hello", temporary=True)
        events = [event async for event in provider.stream(request)]
        self.assertTrue(any(isinstance(event, ConversationBound) for event in events))
        self.assertIs(provider._http_client, prepared_client)
        self.assertFalse(prepared_client.closed.is_set())

        cancelled = provider.stream(
            SendRequest("chatgpt", "default", "local", "cancel me", temporary=True)
        )
        first = await asyncio.wait_for(anext(cancelled), timeout=1)
        self.assertIsInstance(first, TextDelta)
        await asyncio.wait_for(cancelled.aclose(), timeout=2)
        self.assertTrue(prepared_client.closed.is_set())
        self.assertIsNone(provider._http_client)
        self.assertEqual(len(PersistentClient.instances), 1)
        await provider.close()
