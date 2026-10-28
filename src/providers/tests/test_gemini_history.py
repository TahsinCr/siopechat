"""Deletion, tombstone recovery and cached-history merging without remote traffic."""

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from providers.gemini import GeminiProvider


def message(role, text, **extra):
    return {"role": role, "text": text, **extra}


class GeminiHistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_listing_reads_remote_pages_and_returns_real_pagination(self):
        provider = GeminiProvider(Path("unused"))
        provider._connect = AsyncMock()
        provider.rpc.call = AsyncMock(
            return_value=[[None, None, [["c_" + str(i), str(i)] for i in range(5)]]]
        )
        first = await provider.list_chats(limit=2)
        second = await provider.list_chats(first.next_cursor, limit=2)
        self.assertEqual([x.title for x in first.items], ["0", "1"])
        self.assertEqual([x.title for x in second.items], ["2", "3"])
        self.assertEqual(second.next_cursor, "4")
        self.assertEqual(provider.rpc.call.await_count, 2)

    async def test_message_ids_do_not_shift_when_older_history_arrives(self):
        def turn(rid):
            return [["c_abc", rid], None, [["question"]], [[["candidate", ["answer"]]]]]

        provider = GeminiProvider(Path("unused"))
        provider.rpc.call = AsyncMock(return_value=[[[turn("r_stable")]]])
        first = await provider.get_chat("c_abc")
        provider.rpc.call.return_value = [[[turn("r_stable"), turn("r_old")]]]
        second = await provider.get_chat("c_abc")
        self.assertEqual([m.id for m in first.messages], [m.id for m in second.messages[-2:]])


class NotebookDeleteVerificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_index_may_lag_deletion_without_resubmitting_the_write(self):
        project = SimpleNamespace(
            name="Fixture", origin=SimpleNamespace(remote_id="notebooks/abcdef")
        )
        notebooks = SimpleNamespace(
            cdp_url="http://127.0.0.1:9231",
            get=AsyncMock(return_value=project),
            list=AsyncMock(
                side_effect=[SimpleNamespace(items=[project]), SimpleNamespace(items=[])]
            ),
        )
        provider = GeminiProvider(Path("unused"), notebooks=notebooks)
        with (
            patch.object(provider.editor, "write", new_callable=AsyncMock) as delete,
            patch("providers.gemini.adapter.asyncio.sleep", new_callable=AsyncMock) as wait,
        ):
            await provider.delete_project("notebooks/abcdef")
            delete.assert_awaited_once()
            wait.assert_awaited_once_with(0.25)
        self.assertEqual(notebooks.list.await_count, 2)
