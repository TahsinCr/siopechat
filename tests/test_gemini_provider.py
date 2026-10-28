"""Native Gemini protocol tests; no Gemini SDK imports or SDK-shaped mocks."""

import asyncio
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from providers import ReplaceText, SendRequest
from providers.errors import AccountPaused, ProviderError
from providers.gemini import GeminiProvider
from providers.gemini.session import GeminiSession
from providers.gemini.transport import Frames, generation_request, upload_files
from providers.models import Choice, Model


class NativeGeminiTests(unittest.IsolatedAsyncioTestCase):
    async def test_logout_waits_for_inflight_open_and_removes_rotated_cookie_write(self):
        with tempfile.TemporaryDirectory() as folder:
            credential = Path(folder) / "session.json"
            credential.write_text('{"__Secure-1PSID":"initial"}', encoding="utf-8")
            started, release = asyncio.Event(), asyncio.Event()

            async def open_session():
                started.set()
                await release.wait()
                credential.write_text('{"__Secure-1PSID":"rotated"}', encoding="utf-8")

            client = SimpleNamespace(
                open=open_session,
                close=AsyncMock(),
                models=(),
                discover_models=AsyncMock(return_value=(Model("one", "One"),)),
            )
            provider = GeminiProvider(credential, client_factory=lambda _path: client)
            opening = asyncio.create_task(provider.prepare())
            await started.wait()
            logout = asyncio.create_task(provider.forget_session())
            await asyncio.sleep(0)
            self.assertFalse(logout.done())
            release.set()
            await asyncio.gather(opening, logout)

            self.assertFalse(credential.exists())
            self.assertIsNone(provider._client)
            client.close.assert_awaited_once_with()
            with self.assertRaises(AccountPaused):
                await provider.prepare()

    async def test_close_releases_gemini_session_when_editor_cleanup_fails(self):
        provider = GeminiProvider(Path("unused"))
        client = SimpleNamespace(close=AsyncMock())
        provider._client = client
        provider.editor.close = AsyncMock(side_effect=RuntimeError("editor close failed"))

        with self.assertRaisesRegex(RuntimeError, "editor close failed"):
            await provider.close()

        client.close.assert_awaited_once_with()
        self.assertIsNone(provider._client)

    async def test_background_prepare_and_first_request_share_session_open(self):
        provider = GeminiProvider(Path("saved"), client_factory=lambda _path: client)
        first = Model("first", "First")

        async def open_session():
            await asyncio.sleep(0.02)

        client = SimpleNamespace(
            open=AsyncMock(side_effect=open_session),
            close=AsyncMock(),
            models=(),
            discover_models=AsyncMock(return_value=(first,)),
        )
        with patch("providers.gemini.adapter.session_exists", return_value=True):
            await asyncio.gather(provider.prepare(), provider.models())
        client.open.assert_awaited_once_with()
        client.discover_models.assert_awaited_once_with()
        await provider.close()

    async def test_web_activity_is_synced_immediately_before_generation(self):
        provider = GeminiProvider(Path("unused"), "test")
        events = []
        client = SimpleNamespace(
            models=(Model("flash", "Flash"),),
            sync_activity=AsyncMock(side_effect=lambda: events.append("activity")),
        )
        provider._connect = AsyncMock(return_value=client)

        async def generate(*_args, **_kwargs):
            events.append("generation")
            yield {
                "cid": "remote",
                "metadata": ["remote", "rid", "rcid"],
                "context_only": False,
                "files": [],
                "sources": (),
                "text": "answer",
                "thoughts": "",
            }

        request = SendRequest("gemini", "test", None, "question")
        with patch("providers.gemini.transport.generate", generate):
            _ = [event async for event in provider._stream(request)]

        self.assertEqual(events, ["activity", "generation"])

    async def test_silent_generation_time_is_not_mistaken_for_network_idle(self):
        provider = GeminiProvider(Path("unused"), "test")
        provider.stream_idle_timeout = 0.005
        provider.stream_total_timeout = 0.5

        async def delayed_response(_request):
            await asyncio.sleep(0.02)
            yield ReplaceText("answer")

        provider._stream = delayed_response
        events = [
            event
            async for event in provider.stream(
                SendRequest("gemini", "test", "local", "question")
            )
        ]

        self.assertEqual(events, [ReplaceText("answer")])

    async def test_stream_still_enforces_the_total_request_deadline(self):
        provider = GeminiProvider(Path("unused"), "test")
        provider.stream_total_timeout = 0.005

        async def stalled_response(_request):
            await asyncio.sleep(0.02)
            yield ReplaceText("answer")

        provider._stream = stalled_response
        with self.assertRaisesRegex(ProviderError, "10 dakikalık süre sınırını"):
            _ = [
                event
                async for event in provider.stream(
                    SendRequest("gemini", "test", "local", "question")
                )
            ]

    async def test_activity_sync_uses_the_web_preference_rpc(self):
        session = GeminiSession(Path("unused"))
        session.batch = AsyncMock()

        await session.sync_activity()

        payloads = session.batch.await_args.args[0]
        self.assertEqual([(item.rpcid, item.payload) for item in payloads], [
            ("ESY5D", '[[["bard_activity_enabled"]]]'),
        ])

    async def test_catalog_is_cached_but_expiry_and_force_refresh_entitlements(self):
        provider = GeminiProvider(Path("unused"))
        first, second = Model("first", "First"), Model("second", "Second")
        session = SimpleNamespace(
            models=(first,), discover_models=AsyncMock(return_value=(second,))
        )
        provider._connect = AsyncMock(return_value=session)
        provider._catalog_at = time.monotonic()
        self.assertEqual((await provider.models()).models, (first,))
        await asyncio.gather(provider.models(), provider.models())
        session.discover_models.assert_not_awaited()
        self.assertEqual((await provider.models(force=True)).models, (second,))
        provider._catalog_at -= 301
        await asyncio.gather(provider.models(), provider.models())
        self.assertEqual(session.discover_models.await_count, 2)

    async def test_rotated_credentials_are_serialized_and_saved_on_close(self):
        session = GeminiSession("unused")
        current = {"__Secure-1PSID": "first"}
        session.verified = True
        session.http = SimpleNamespace(
            cookies=SimpleNamespace(get_dict=lambda: dict(current)), close=AsyncMock()
        )
        started, release = threading.Event(), threading.Event()
        writes = []

        def save(_path, value):
            writes.append(value)
            if len(writes) == 1:
                started.set()
                if not release.wait(2):
                    raise TimeoutError("save not released")

        with patch("providers.session.save_session", side_effect=save):
            first = asyncio.create_task(session.persist())
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            current["__Secure-1PSID"] = "rotated"
            second = asyncio.create_task(session.persist())
            await asyncio.sleep(0.01)
            self.assertEqual(len(writes), 1)
            release.set()
            await asyncio.gather(first, second)
            await session.persist()
            self.assertEqual(len(writes), 2)
            current["__Secure-1PSID"] = "last"
            await session.close()
        self.assertEqual([v["__Secure-1PSID"] for v in writes], ["first", "rotated", "last"])
        self.assertIsNone(session.http)

    def test_frames_handle_utf8_boundaries_lengths_and_multiple_packets(self):
        frames = [
            [["wrb.fr", None, json.dumps(["Türkçe 🛰️"])]],
            [["wrb.fr", None, json.dumps(["ikinci çerçeve"])]],
        ]

        def encode(rows):
            payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
            size = len(("\n" + payload).encode("utf-16-le")) // 2
            return f"{size}\n{payload}"

        wire = (")]}'\n\n" + "".join(encode(frame) for frame in frames) + "\n"
        ).encode()
        parser = Frames()
        output = []
        for byte in wire:
            output.extend(parser.feed(bytes([byte])))
        output.extend(parser.feed(b"", final=True))
        self.assertEqual(output, frames[0] + frames[1])
        self.assertEqual(parser.pending, "")

    def test_frame_size_and_invalid_json_are_rejected(self):
        with self.assertRaises(ProviderError):
            list(Frames().feed(b"8388609\n"))
        with self.assertRaises(ProviderError):
            list(Frames().feed(b"10\n[[invalid", final=True))

    def test_model_selection_notebook_temporary_and_effort_are_native(self):
        model = Model(
            "dynamic",
            "Dynamic",
            (Choice("extended", "Remote title"),),
            default_effort="extended",
            metadata={
                "model_id": "remote",
                "capacity": 2,
                "capacity_slot": 12,
                "number": 3,
                "effort_values": {"extended": 2},
            },
        )
        session = SimpleNamespace(
            models=(model,), language="en", token="test", session_id="session"
        )
        request = SendRequest(
            "gemini",
            "test",
            "local",
            "hello",
            model="dynamic",
            effort="extended",
            temporary=True,
        )
        headers, data = generation_request(session, request, ["cid"], [], ("notebooks/test", [2]))
        body = json.loads(json.loads(data["f.req"])[1])
        self.assertEqual(
            (body[19], body[40][13], body[45], body[79], body[80]), ("notebooks/test", [2], 1, 3, 2)
        )
        self.assertEqual(body[6], [1])
        self.assertEqual(body[17], [[0]])
        selector = json.loads(headers["x-goog-ext-525001261-jspb"])
        self.assertEqual(selector[11], 2)
        self.assertEqual(selector[7], 1)
        with self.assertRaises(ValueError):
            generation_request(
                session,
                SendRequest("gemini", "test", "local", "hello", model="unavailable"),
                [],
                [],
            )

    async def test_model_policy_controls_available_efforts_defaults_and_wire_values(self):
        row = [None] * 18
        row[0], row[1], row[11], row[17] = "remote", "flash", "Flash", 1
        legacy = [None] * 18
        legacy[15], legacy[16], legacy[17] = [row], [], []
        options = [
            {"thinkingLevel": "THINKING_LEVEL_STANDARD", "title": "Fast"},
            {"thinkingLevel": "THINKING_LEVEL_MEDIUM", "title": "Balanced"},
            {"thinkingLevel": "THINKING_LEVEL_DEEP_THINK"},
            {"thinkingLevel": "THINKING_LEVEL_EXTENDED", "title": "Thorough", "isDisabled": True},
        ]
        policy = {
            "modeConfig": {"id": "", "modeCategory": "FAST", "title": "Provider model"},
            "hosts": ["MODE_PICKER_HOST_CHAT"],
            "implicitThinkingLevel": "THINKING_LEVEL_MEDIUM",
            "thinkingLevelConfigs": options,
        }
        status = {
            "bardModeConfigs": [{"id": "remote", "modeCategory": "FAST"}],
            "geminiModePolicy": {"configs": [policy]},
        }
        session = GeminiSession(Path("unused"))
        session.rpc_path_prefix = "/_/BardChatUi/"
        session.http = SimpleNamespace(
            get=AsyncMock(
                return_value=SimpleNamespace(
                    status_code=200,
                    text=json.dumps([{"data": {"viewer": {"rpcs": {"getUserStatus": status}}}}]),
                )
            )
        )
        session.batch = AsyncMock(
            return_value=SimpleNamespace(
                text=json.dumps([["wrb.fr", "otAQ7b", json.dumps(legacy)]]) + "\n",
            )
        )
        session.models = await session.discover_models()
        (model,) = session.models
        self.assertEqual(
            [(e.id, e.title) for e in model.efforts],
            [("standard", "standard"), ("medium", "medium"), ("deep_think", "deep_think")],
        )
        self.assertEqual(model.default_effort, "medium")
        self.assertEqual(model.title, "Provider model")
        session.token, session.session_id = "token", "session"
        request = SendRequest("gemini", "default", "local", "hello", model=model.id)
        headers, data = generation_request(session, request, [], [])
        self.assertEqual(json.loads(json.loads(data["f.req"])[1])[80], 4)
        self.assertEqual(json.loads(headers["x-goog-ext-525001261-jspb"])[-2], 4)
        self.assertEqual(json.loads(headers["x-goog-ext-525001261-jspb"])[7], 0)
        from dataclasses import replace

        with self.assertRaisesRegex(ValueError, "Efor"):
            generation_request(session, replace(request, effort="extended"), [], [])

        # A refreshed server policy changes the choices without local model-name tables.
        options[1]["isDisabled"] = True
        options[2]["isDisabled"] = True
        policy["implicitThinkingLevel"] = "THINKING_LEVEL_STANDARD"
        session.http.get.return_value.text = json.dumps(
            [
                {"data": {"viewer": {"rpcs": {"getUserStatus": status}}}},
            ]
        )
        (refreshed,) = await session.discover_models()
        self.assertEqual(refreshed.efforts, (Choice("standard", "standard"),))
        self.assertEqual(refreshed.default_effort, "standard")
        policy["modeConfig"]["isDisabled"] = True
        session.http.get.return_value.text = json.dumps(
            [
                {"data": {"viewer": {"rpcs": {"getUserStatus": status}}}},
            ]
        )
        with self.assertRaisesRegex(ProviderError, "kullanılabilir model"):
            await session.discover_models()

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

    async def test_large_rpc_json_is_decoded_off_the_provider_loop(self):
        from providers.gemini.rpc import NotebookRPC

        response = SimpleNamespace(
            content=b" " * (256 * 1024),
            text='[["wrb.fr","rpc","[]"]]\n',
        )
        client = SimpleNamespace(batch=AsyncMock(return_value=response))
        rpc = NotebookRPC(AsyncMock(return_value=client))
        original = rpc._decode
        threads = []

        def decode(*args):
            threads.append(threading.get_ident())
            return original(*args)

        with patch.object(rpc, "_decode", side_effect=decode):
            self.assertEqual(await rpc.call("rpc", []), [[]])
        self.assertNotEqual(threads, [threading.get_ident()])

    async def test_close_cancels_and_joins_refresh_then_closes_http(self):
        session = GeminiSession(Path("unused"))
        session.http = SimpleNamespace(close=AsyncMock())
        http = session.http
        task = session.refresh_task = asyncio.create_task(asyncio.sleep(3600))
        await session.close()
        self.assertTrue(task.cancelled())
        http.close.assert_awaited_once()
        await session.close()

    async def test_cookie_refresh_retries_transient_network_error_with_backoff(self):
        session = GeminiSession(Path("unused"))
        response = SimpleNamespace(status_code=200, headers={})
        requests = 0

        async def post(*_args, **_kwargs):
            nonlocal requests
            requests += 1
            if requests == 1:
                raise OSError("temporary network outage")
            return response

        session.http = SimpleNamespace(post=post)
        session.persist = AsyncMock()
        delays = []

        async def sleep(delay):
            delays.append(delay)
            if len(delays) == 3:
                raise asyncio.CancelledError

        with patch("providers.gemini.session.asyncio.sleep", side_effect=sleep):
            task = asyncio.create_task(session.refresh())
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertEqual(requests, 2)
        self.assertEqual(delays, [600, 15, 600])
        session.persist.assert_awaited_once_with()

    async def test_cookie_refresh_stops_on_expired_session(self):
        session = GeminiSession(Path("unused"))
        session.http = SimpleNamespace(
            post=AsyncMock(return_value=SimpleNamespace(status_code=401, headers={}))
        )

        with patch("providers.gemini.session.asyncio.sleep", new=AsyncMock()):
            await asyncio.wait_for(session.refresh(), 1)

        session.http.post.assert_awaited_once()

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
        if response is not None:
            response.astream_task = asyncio.create_task(asyncio.Event().wait())
            response.astream_task.add_done_callback(lambda _: setattr(response, "closed", True))

        return SimpleNamespace(
            http=SimpleNamespace(request=AsyncMock(return_value=response)),
            models=(
                Model(
                    "flash",
                    "Flash",
                    (Choice("standard", "Low"),),
                    "standard",
                    {
                        "model_id": "remote",
                        "capacity": 2,
                        "capacity_slot": 12,
                        "number": 1,
                        "effort_values": {"standard": 1},
                    },
                ),
            ),
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
        candidate[0], candidate[1], candidate[8] = "rcid", ["answer"], [2]
        activity = [None] * 8
        activity[1] = activity[5] = "Searching the web"
        activity[7] = [["favicon", "https://example.com", "Example"]]
        candidate[37] = [["thought"], [activity]]
        body = [None, ["c_new", "rid"], None, None, [candidate]]
        acknowledged = [None, ["c_new", "rid"]]

        def encode_frame(payload):
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            units = len(("\n" + encoded).encode("utf-16-le")) // 2
            return f"{units}\n{encoded}"

        wire = (
            ")]}'\n\n"
            + encode_frame([["wrb.fr", None, json.dumps(acknowledged)]])
            + encode_frame([["wrb.fr", None, json.dumps(body)]])
            + "\n"
        ).encode()
        response = self.response([wire[:20], wire[20:]])
        session = self.session(response)
        result = [
            c
            async for c in generate(session, SendRequest("gemini", "test", "local", "question"), [])
        ]
        self.assertEqual(
            (result[1]["cid"], result[1]["text"], result[1]["thoughts"]),
            ("c_new", "answer", "thought\nSearching the web"),
        )
        self.assertEqual(
            result[1]["sources"], ({"title": "Example", "url": "https://example.com"},)
        )
        self.assertEqual(session.http.request.await_args.kwargs["params"], {})
        self.assertTrue(result[0]["context_only"])
        self.assertEqual(result[0]["cid"], "c_new")
        self.assertTrue(response.closed)
        session.persist.assert_awaited_once()

    async def test_cancellation_aborts_an_actual_stalled_http_transfer(self):
        from curl_cffi.requests import AsyncSession

        from providers.gemini.transport import generate

        accepted = asyncio.Event()
        requests = []

        async def serve(reader, writer):
            try:
                requests.append(await reader.readuntil(b"\r\n\r\n"))
                if requests[-1].startswith(b"GET "):
                    writer.write(
                        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK"
                    )
                    await writer.drain()
                    return
                length = next(
                    int(line.split(b":", 1)[1])
                    for line in requests[-1].splitlines()
                    if line.lower().startswith(b"content-length:")
                )
                await reader.readexactly(length)
                writer.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\n[]\n\r\n")
                await writer.drain()
                accepted.set()
                await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        session = self.session(None)
        session.http = AsyncSession(trust_env=False)
        try:
            with patch("providers.gemini.transport.BASE", f"http://127.0.0.1:{port}/"):
                stream = generate(session, SendRequest("gemini", "test", "local", "question"), [])
                pending = asyncio.create_task(anext(stream))
                await asyncio.wait_for(accepted.wait(), 2)
                pending.cancel()
                done, _ = await asyncio.wait({pending}, timeout=2)
                self.assertIn(pending, done, "Cancellation must release a stalled transfer")
                with self.assertRaises(asyncio.CancelledError):
                    await pending
                # The pool may retain idle sockets, but cancellation must free
                # the transfer and allow a separate request to finish normally.
                result = await session.http.get(f"http://127.0.0.1:{port}/health", timeout=2)
                self.assertEqual(result.text, "OK")
                self.assertEqual(sum(r.startswith(b"POST ") for r in requests), 1)
        finally:
            await session.http.close()
            server.close()
            await server.wait_closed()

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
