"""Native Gemini protocol tests; no Gemini SDK imports or SDK-shaped mocks."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from providers import SendRequest
from providers.errors import ProviderError
from providers.gemini import GeminiProvider
from providers.gemini.session import GeminiSession
from providers.gemini.transport import Frames, generation_request, upload_files
from providers.models import Model


class NativeGeminiTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_catalog_discovery_refreshes_account_entitlements(self):
        provider = GeminiProvider(Path("unused"))
        first, second = Model("first", "First"), Model("second", "Second")
        session = SimpleNamespace(
            models=(first,), discover_models=AsyncMock(return_value=(second,))
        )
        provider._connect = AsyncMock(return_value=session)
        self.assertEqual((await provider.models()).models, (first,))
        self.assertEqual((await provider.models()).models, (second,))
        session.discover_models.assert_awaited_once()

    def test_frames_handle_utf8_boundaries_lengths_and_multiple_packets(self):
        frame = [["wrb.fr", None, json.dumps(["Türkçe"])]]
        wire = (")]}'\n\n123\n" + json.dumps(frame, ensure_ascii=False) + "\n").encode()
        parser = Frames()
        output = []
        for byte in wire:
            output.extend(parser.feed(bytes([byte])))
        output.extend(parser.feed(b"", final=True))
        self.assertEqual(output, frame)
        self.assertEqual(parser.pending, "")

    def test_frame_size_and_invalid_json_are_rejected(self):
        with self.assertRaises(ProviderError):
            list(Frames().feed(b"x" * (8 * 1024 * 1024 + 1)))
        with self.assertRaises(ProviderError):
            list(Frames().feed(b"[[invalid\n"))

    def test_model_selection_notebook_temporary_and_effort_are_native(self):
        model = Model(
            "dynamic",
            "Dynamic",
            metadata={"model_id": "remote", "capacity": 2, "capacity_slot": 12, "number": 3},
        )
        session = SimpleNamespace(
            models=(model,), language="en", token="test", session_id="session"
        )
        request = SendRequest(
            "gemini", "test", "local", "hello", model="dynamic", effort="extended", temporary=True
        )
        headers, data = generation_request(session, request, ["cid"], [], ("notebooks/test", [2]))
        body = json.loads(json.loads(data["f.req"])[1])
        self.assertEqual(
            (body[19], body[40][13], body[45], body[79], body[80]), ("notebooks/test", [2], 1, 3, 2)
        )
        selector = json.loads(headers["x-goog-ext-525001261-jspb"])
        self.assertEqual(selector[11], 2)
        with self.assertRaises(ValueError):
            generation_request(
                session,
                SendRequest("gemini", "test", "local", "hello", model="unavailable"),
                [],
                [],
            )

    async def test_uploads_use_local_paths_serially_and_close_mime(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / name for name in ("one.txt", "two.txt")]
            for path in paths:
                path.write_text("fixture")
            http = SimpleNamespace(
                post=AsyncMock(
                    return_value=SimpleNamespace(status_code=200, text="uploaded", headers={})
                )
            )
            session = SimpleNamespace(http=http, push_id="push")
            with patch(
                "pathlib.Path.read_bytes", side_effect=AssertionError("must stream from disk")
            ):
                files = await upload_files(session, paths)
            self.assertEqual([item[1] for item in files], ["one.txt", "two.txt"])
            self.assertEqual(http.post.await_count, 2)

    async def test_rpc_is_account_owned_and_never_retries_writes(self):
        from providers.gemini.rpc import NotebookRPC

        client = SimpleNamespace(
            batch=AsyncMock(return_value=SimpleNamespace(text='[["wrb.fr","rpc","[]"]]\n'))
        )
        rpc = NotebookRPC(AsyncMock(return_value=client))
        self.assertEqual(await rpc.call("rpc", ["test"]), [[]])
        client.batch.assert_awaited_once()
        client.batch.return_value.text = "invalid"
        with self.assertRaises(ProviderError):
            await rpc.call("rpc", [])

    async def test_close_cancels_and_joins_refresh_then_closes_http(self):
        session = GeminiSession(Path("unused"))
        session.http = SimpleNamespace(close=AsyncMock())
        http = session.http
        task = session.refresh_task = asyncio.create_task(asyncio.sleep(3600))
        await session.close()
        self.assertTrue(task.cancelled())
        http.close.assert_awaited_once()
        await session.close()

    async def test_history_preserves_thoughts_and_stable_ids(self):
        provider = GeminiProvider(Path("unused"), "test")
        candidate = [None] * 38
        candidate[0], candidate[1], candidate[37] = "rcid", ["Answer"], [["Thought"]]
        turn = [["cid", "rid"], None, [["Question"]], [[candidate]]]
        provider.rpc.call = AsyncMock(return_value=[[[turn]]])
        history = await provider.get_chat("cid")
        self.assertEqual([m.channel for m in history.messages], [None, "analysis", None])
        self.assertEqual(history.messages[-1].id, "rid:model")


class StreamTests(unittest.IsolatedAsyncioTestCase):
    def session(self, response):
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def stream(*args, **kwargs):
            try:
                yield response
            finally:
                response.closed = True

        return SimpleNamespace(
            http=SimpleNamespace(stream=stream),
            models=(),
            language="en",
            token="test",
            session_id="test",
            params=lambda: {},
            persist=AsyncMock(),
        )

    def response(self, payloads):
        async def content():
            for payload in payloads:
                yield payload

        return SimpleNamespace(status_code=200, headers={}, aiter_content=content, closed=False)

    async def test_completed_stream_preserves_text_thoughts_and_cid(self):
        from providers.gemini.transport import generate

        candidate = [None] * 38
        candidate[0], candidate[1], candidate[8], candidate[37] = (
            "rcid",
            ["answer"],
            [2],
            [["thought"]],
        )
        body = [None, ["c_new", "rid"], None, None, [candidate]]
        wire = (json.dumps([["wrb.fr", None, json.dumps(body)]]) + "\n").encode()
        response = self.response([wire[:20], wire[20:]])
        session = self.session(response)
        result = [
            c
            async for c in generate(session, SendRequest("gemini", "test", "local", "question"), [])
        ]
        self.assertEqual(
            (result[0]["cid"], result[0]["text"], result[0]["thoughts"]),
            ("c_new", "answer", "thought"),
        )
        self.assertTrue(response.closed)
        session.persist.assert_awaited_once()

    async def test_incomplete_stream_is_not_success_or_retried(self):
        from providers.gemini.transport import generate

        response = self.response([b""])
        session = self.session(response)
        with self.assertRaises(ProviderError):
            _ = [
                c
                async for c in generate(
                    session, SendRequest("gemini", "test", "local", "question"), []
                )
            ]
        self.assertTrue(response.closed)
        session.persist.assert_not_awaited()

    async def test_cancellation_closes_stream_without_resending(self):
        from providers.gemini.transport import generate

        entered = asyncio.Event()

        async def content():
            entered.set()
            await asyncio.Future()
            yield b""

        response = self.response([])
        response.aiter_content = content

        async def consume():
            return [
                c
                async for c in generate(
                    self.session(response), SendRequest("gemini", "test", "local", "question"), []
                )
            ]

        task = asyncio.create_task(consume())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(response.closed)
