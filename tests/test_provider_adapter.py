import concurrent.futures
import unittest

from webaiprovider import (
    Account,
    ChatGPTProvider,
    MemoryCredentialStore,
    OperationCancelledError,
    OperationState,
    OutputDelta,
)
from webaiprovider.providers.chatgpt.auth import BrowserSession

from tests.provider_fakes import FakeClient


class ProviderAdapterTests(unittest.TestCase):
    def setUp(self):
        store = MemoryCredentialStore({"version": 1, "url": "https://chatgpt.com/", "headers": {}})
        self.account = Account("default", "chatgpt", store)
        self.session = BrowserSession(store)
        self.clients = []

        def make_client(session):
            client = FakeClient(session)
            self.clients.append(client)
            return client

        self.provider = ChatGPTProvider(
            self.account, session=self.session, client_factory=make_client
        )
        self.addCleanup(self.provider.close)

    def test_operation_cancellation_closes_sync_client_and_stops_stream(self):
        self.provider.open()
        chat = self.provider.chats.create()
        op = chat.responses.create("hello")
        chunks = []
        cancelled = []

        def run():
            try:
                for event in op.stream():
                    if isinstance(event, OutputDelta):
                        chunks.append(event.delta)
            except OperationCancelledError as error:
                cancelled.append(error)

        def send_client():
            return next(
                (client for client in self.clients[1:] if client.started.is_set()),
                self.clients[-1],
            )

        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            future = pool.submit(run)
            while len(self.clients) < 2:
                self.assertTrue(future.running())
            self.assertTrue(self.clients[-1].started.wait(2))
            op.cancel()
            future.result(timeout=3)

        self.assertEqual("".join(chunks), "partial")
        self.assertEqual(op.state, OperationState.CANCELLED)
        self.assertEqual(len(cancelled), 1)
        self.assertTrue(send_client().closed.is_set())
