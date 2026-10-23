"""Remote deletion confirmation never treats network errors as absence."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from providers.contracts import confirm_deleted
from providers.errors import ProviderError, RemoteNotFound
from providers.gemini.rpc import NotebookRPC


class DeletionVerificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_readback_retries_until_server_confirms_absence(self):
        read = AsyncMock(side_effect=[{"id": "present"}, RemoteNotFound("deleted")])
        with patch("providers.contracts.asyncio.sleep", new_callable=AsyncMock):
            await confirm_deleted(read)
        self.assertEqual(read.await_count, 2)

    async def test_local_deletion_cannot_succeed_while_server_still_returns_the_record(self):
        with patch("providers.contracts.asyncio.sleep", new_callable=AsyncMock):
            with self.assertRaisesRegex(ProviderError, "yerel kayıt korundu"):
                await confirm_deleted(AsyncMock(return_value={"id": "present"}))

    async def test_network_errors_do_not_count_as_confirmed_deletion(self):
        with self.assertRaisesRegex(ProviderError, "network"):
            await confirm_deleted(AsyncMock(side_effect=ProviderError("network")))

    async def test_missing_gemini_rpc_reply_cannot_confirm_a_delete(self):
        client = SimpleNamespace(
            batch=AsyncMock(return_value=SimpleNamespace(text="not a valid response"))
        )
        with self.assertRaises(ProviderError):
            await NotebookRPC(AsyncMock(return_value=client)).call(
                "GzXR5e", ["c_fixture"], allow_empty=True
            )
