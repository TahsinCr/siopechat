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
    async def test_chatgpt_file_credentials_default_to_a_sibling_browser_profile(self):
        with tempfile.TemporaryDirectory() as folder:
            credentials = Path(folder) / "session.json"
            provider = create_provider("chatgpt", credential_file=credentials)
            try:
                self.assertEqual(provider.session.paths.profile, Path(folder) / "chrome-profile")
            finally:
                await provider.close()

    async def test_chatgpt_refresh_saves_through_host_store_without_a_filesystem_path(self):
        with tempfile.TemporaryDirectory() as folder:
            store = MemoryCredentials()
            provider = create_provider(
                "chatgpt", credential_file=store, profile_directory=Path(folder) / "profile"
            )
            try:
                provider.session.save(
                    "https://chatgpt.com/backend-api/conversations",
                    {"authorization": "Bearer refreshed"},
                )
                self.assertEqual(provider.session.load()[1]["authorization"], "Bearer refreshed")
            finally:
                await provider.close()

    async def test_all_backends_accept_the_same_injected_store(self):
        with tempfile.TemporaryDirectory() as folder:
            for kind in ("chatgpt", "gemini", "claude"):
                store = MemoryCredentials()
                self.assertIsInstance(store, CredentialStore)
                provider = create_provider(
                    kind, "fixture", credential_file=store, profile_directory=Path(folder) / kind
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

    def test_chatgpt_store_requires_caller_owned_profile_directory(self):
        with self.assertRaisesRegex(ValueError, "profile_directory is required"):
            create_provider("chatgpt", "fixture", credential_file=MemoryCredentials())
