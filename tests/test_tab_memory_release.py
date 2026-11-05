"""Verify that closing an idle chat releases its in-memory transcript state."""

import gc
import tempfile
import tracemalloc
import unittest
import weakref
from pathlib import Path
from unittest.mock import patch

from tests.account_fixture import configured_application
from tests.tui_support import terminal
from tui.history import prepare_virtual_transcript
from tui.transcript import VirtualTranscriptRows


class TabMemoryReleaseTests(unittest.TestCase):
    def test_close_releases_source_and_render_heap_but_keeps_archived_chat(self):
        tracing_before = tracemalloc.is_tracing()
        if not tracing_before:
            tracemalloc.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                app = configured_application(Path(directory))
                try:
                    archive = app.get("archive")
                    account_key = "chatgpt/default"
                    archive.put(
                        account_key,
                        "conversation",
                        "memory-probe",
                        {
                            "title": "Memory probe",
                            "messages": [{"id": "saved", "role": "assistant", "text": "saved"}],
                        },
                    )
                    with terminal(account_service=app.get("accounts")) as (view, _):
                        baseline = tracemalloc.get_traced_memory()[0]
                        tab = view.tabs[view.active_tab]
                        tab.update(
                            account_key=account_key,
                            conversation_id="@chatgpt/default/memory-probe",
                        )
                        view.conversation_id = "@chatgpt/default/memory-probe"

                        class Payload:
                            pass

                        owner = Payload()
                        owner.buffer = bytearray(8 * 1024 * 1024)
                        owner_ref = weakref.ref(owner)
                        source = "A rendered transcript line with **formatting**.\n" * 4_000
                        message = {"role": "assistant", "text": source, "metadata": owner}
                        view.messages = [message]
                        tab["render_payload"] = {
                            "snapshot": ((("assistant", source),), (0,), ("message",)),
                            "owner": owner,
                        }
                        tab_id = tab["tab_id"]
                        view._prepared_transcripts = {tab_id: {"source": source, "owner": owner}}
                        rendered = view.renderer.render(
                            "assistant", source, view.transcript_width(), False, False
                        )
                        layout = prepare_virtual_transcript(
                            view.messages,
                            view.transcript_width(),
                            view.renderer.labels,
                            True,
                            renderer=view.renderer,
                        )
                        layout.prefetch(0, min(layout.line_count, 64))
                        view._virtual_transcript = layout
                        view._virtual_tab_id = tab_id
                        view._virtual_conversation_id = view.conversation_id
                        view.transcript_control.rows = VirtualTranscriptRows(layout)
                        view._transcript_cache = rendered
                        view._transcript_lines = (rendered,)

                        del message, owner, source, rendered
                        gc.collect()
                        retained = tracemalloc.get_traced_memory()[0]
                        self.assertGreater(retained - baseline, 8 * 1024 * 1024)

                        view.close_tab()
                        gc.collect()
                        released = tracemalloc.get_traced_memory()[0]

                        self.assertIsNone(owner_ref())
                        self.assertEqual(tab["messages"], [])
                        self.assertIsNone(tab.get("render_payload"))
                        self.assertEqual(layout.messages, ())
                        self.assertFalse(view.renderer.cache)
                        self.assertEqual(view._prepared_transcripts, {})
                        self.assertEqual(view._transcript_cache, "")
                        self.assertLess(released, retained - 8 * 1024 * 1024)

                        saved = archive.get(account_key, "conversation", "memory-probe")
                        self.assertEqual(saved["messages"][0]["text"], "saved")
                finally:
                    app.runtime.close()
        finally:
            if not tracing_before:
                tracemalloc.stop()

    def test_repeated_tab_open_close_does_not_accumulate_live_transcript_heap(self):
        tracing_before = tracemalloc.is_tracing()
        if not tracing_before:
            tracemalloc.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                app = configured_application(Path(directory))
                try:
                    archive = app.get("archive")
                    account_key = "chatgpt/default"
                    conversation_id = "@chatgpt/default/repeated-memory-probe"
                    archive.put(
                        account_key,
                        "conversation",
                        "repeated-memory-probe",
                        {
                            "title": "Repeated memory probe",
                            "messages": [{"id": "saved", "role": "assistant", "text": "saved"}],
                        },
                    )
                    with terminal(account_service=app.get("accounts")) as (view, _):
                        closed_heap = []
                        view.bind_account = lambda *_: None
                        view.refresh_provider_catalog = lambda *_: None
                        for cycle in range(10):
                            view.new_tab()

                            tab = view.tabs[view.active_tab]
                            tab.update(
                                account_key=account_key,
                                conversation_id=conversation_id,
                            )
                            view.conversation_id = conversation_id

                            class Payload:
                                pass

                            owner = Payload()
                            owner.buffer = bytearray(b"x" * (8 * 1024 * 1024))
                            owner_ref = weakref.ref(owner)
                            source = f"Cycle {cycle}: rendered transcript content.\n" * 4_000
                            message = {
                                "role": "assistant",
                                "text": source,
                                "metadata": owner,
                            }
                            view.messages = [message]
                            tab_id = tab["tab_id"]
                            tab["render_payload"] = {
                                "snapshot": ((("assistant", source),), (0,), ("message",)),
                                "owner": owner,
                            }
                            view._prepared_transcripts = {
                                tab_id: {"source": source, "owner": owner}
                            }
                            rendered = view.renderer.render(
                                "assistant", source, view.transcript_width(), False, False
                            )
                            layout = prepare_virtual_transcript(
                                view.messages,
                                view.transcript_width(),
                                view.renderer.labels,
                                True,
                                renderer=view.renderer,
                            )
                            layout.prefetch(0, min(layout.line_count, 64))
                            view._virtual_transcript = layout
                            view._virtual_tab_id = tab_id
                            view._virtual_conversation_id = conversation_id
                            view.transcript_control.rows = VirtualTranscriptRows(layout)
                            view._transcript_cache = rendered
                            view._transcript_lines = (rendered,)

                            del message, owner, source, rendered
                            gc.collect()
                            retained = tracemalloc.get_traced_memory()[0]

                            view.close_tab()
                            gc.collect()
                            released = tracemalloc.get_traced_memory()[0]

                            self.assertIsNone(owner_ref(), f"cycle {cycle} retained payload")
                            self.assertEqual(tab["messages"], [])
                            self.assertIsNone(tab.get("render_payload"))
                            self.assertEqual(layout.messages, ())
                            self.assertFalse(view.renderer.cache)
                            self.assertEqual(view._prepared_transcripts, {})
                            self.assertEqual(view._transcript_cache, "")
                            self.assertGreaterEqual(
                                retained - released,
                                8 * 1024 * 1024,
                                f"cycle {cycle} did not release the payload",
                            )
                            closed_heap.append(released)

                        steady_state = closed_heap[1:]
                        self.assertLessEqual(
                            max(steady_state) - min(steady_state),
                            512 * 1024,
                            "live traced heap grew across repeated tab close cycles",
                        )
                        saved = archive.get(account_key, "conversation", "repeated-memory-probe")
                        self.assertEqual(saved["messages"][0]["text"], "saved")
                finally:
                    app.runtime.close()
        finally:
            if not tracing_before:
                tracemalloc.stop()

    def test_close_all_tabs_clears_prepared_transcripts_and_invokes_release_conversation(self):
        from tests.fakes import Accounts as FakeAccounts
        from tests.tui_support import immediate

        service = FakeAccounts()
        active_accounts_calls = []
        service.active_accounts = lambda: active_accounts_calls.append(True) or []
        released_chats = []
        service.callbacks = lambda key: {
            "release_conversation": lambda chat: released_chats.append((key, chat)),
            "forget_temporary": lambda chat: None,
        }
        with terminal(account_service=service) as (view, _):
            view._read_in_background = immediate
            view.new_tab()
            tab = view.tabs[view.active_tab]
            tab["tab_id"] = "tab-probe"
            tab["conversation_id"] = "@chatgpt/default/test-chat"
            tab["account_key"] = "chatgpt/default"
            view.conversation_id = "@chatgpt/default/test-chat"
            view._prepared_transcripts = {"tab-probe": {"source": "data"}}
            active_accounts_calls.clear()

            with patch("tui.chat.tabs.trim_memory"):
                view.close_all_tabs()

            self.assertEqual(getattr(view, "_prepared_transcripts", {}), {})
            self.assertIn(("chatgpt/default", "@chatgpt/default/test-chat"), released_chats)
            self.assertNotIn(("chatgpt/default", None), released_chats)
            self.assertEqual(active_accounts_calls, [])

    def test_chatgpt_client_forget_conversation_clears_only_the_selected_cache(self):
        from unittest.mock import Mock

        from webai_provider.providers.chatgpt.client import ChatGPTClient

        client = ChatGPTClient(Mock(), authenticate=False)
        dummy_conv = {
            "conversation_id": "c1",
            "current_node": "m1",
            "messages": [
                {
                    "id": "m1",
                    "content": {"parts": ["hello world"]},
                }
            ],
        }
        client._remember_conversation(dummy_conv)
        second = dict(dummy_conv, conversation_id="c2")
        client._remember_conversation(second)
        self.assertIn("c1", client._continuation_cache)
        self.assertIn("c2", client._continuation_cache)
        self.assertGreater(client._continuation_cache_text_chars, 0)

        client.forget_conversation("c1")
        self.assertNotIn("c1", client._continuation_cache)
        self.assertIn("c2", client._continuation_cache)
        self.assertGreater(client._continuation_cache_text_chars, 0)

    def test_tab_memory_trim_is_scheduled_in_the_background(self):
        with terminal() as (view, _):
            pending = []
            view._read_in_background = lambda callback, *_args, **_kwargs: pending.append(callback)
            with (
                patch("tui.chat.tabs.trim_memory") as trim,
                patch.object(view.renderer, "clear_cache") as clear_cache,
            ):
                view._trim_closed_tabs()
                trim.assert_not_called()
                self.assertEqual(len(pending), 1)

                pending.pop()()

                clear_cache.assert_called_once_with(wait=True)
                trim.assert_called_once_with()

    def test_dispose_does_not_wait_for_an_inflight_render_lock(self):
        from tui.transcript import VirtualTranscript

        class BusyLock:
            def __init__(self):
                self.kwargs = None

            def acquire(self, **kwargs):
                self.kwargs = kwargs
                return False

        layout = object.__new__(VirtualTranscript)
        layout._disposed = False
        layout._release_renderer = True
        layout._lock = BusyLock()
        with patch.object(layout, "_release") as release:
            layout.dispose()

        self.assertEqual(layout._lock.kwargs, {"blocking": False})
        release.assert_not_called()
