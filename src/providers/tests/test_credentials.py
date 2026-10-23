"""Every bundled backend accepts host-owned storage without importing the host."""

import tempfile
import unittest
from pathlib import Path

from providers import CredentialStore, create_provider
from providers.session import read_session, save_session


class MemoryCredentials:
    def __init__(self):
        self.value = None

    def load(self):
        if self.value is None:
            raise FileNotFoundError()
        return dict(self.value)

    def save(self, value):
        self.value = dict(value)

    def exists(self):
        return self.value is not None

    def delete(self):
        self.value = None


class CredentialContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_backends_accept_the_same_injected_store(self):
        with tempfile.TemporaryDirectory() as folder:
            for kind in ("chatgpt", "gemini", "claude"):
                store = MemoryCredentials()
                self.assertIsInstance(store, CredentialStore)
                provider = create_provider(
                    kind, "fixture", credential_file=store, cache_directory=Path(folder) / kind
                )
                try:
                    if kind == "chatgpt":
                        value = {
                            "url": "https://chatgpt.com/backend-api/conversations",
                            "headers": {
                                "authorization": "Bearer fixture",
                                "cookie": "session=fixture",
                            },
                        }
                        save_session(store, value)
                        self.assertEqual(provider.session.load()[0], value["url"])
                    else:
                        save_session(
                            store,
                            {"sessionKey": "fixture"}
                            if kind == "claude"
                            else {"__Secure-1PSID": "fixture"},
                        )
                        self.assertTrue(read_session(store))
                    await provider.forget_session()
                    self.assertFalse(store.exists())
                finally:
                    await provider.close()

    def test_chatgpt_store_requires_caller_owned_signer_cache(self):
        with self.assertRaises(ValueError):
            create_provider("chatgpt", "fixture", credential_file=MemoryCredentials())
