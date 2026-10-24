"""Verify that closing an idle chat releases its in-memory transcript state."""

import gc
import tempfile
import tracemalloc
import unittest
import weakref
from pathlib import Path

from cli.history import prepare_virtual_transcript
from cli.transcript import VirtualTranscriptRows
from tests.account_fixture import configured_application
from tests.test_cli_interaction import terminal


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
                            "messages": [
                                {"id": "saved", "role": "assistant", "text": "saved"}
                            ],
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
                        view._prepared_transcripts = {
                            tab_id: {"source": source, "owner": owner}
                        }
                        rendered = view.renderer.render(
                            "assistant", source, view.transcript_width(), False, False
                        )
                        layout = prepare_virtual_transcript(
                            view.messages,
                            view.transcript_width(),
                            view.renderer.tokens,
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
                            "messages": [
                                {"id": "saved", "role": "assistant", "text": "saved"}
                            ],
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
                                view.renderer.tokens,
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
                        saved = archive.get(
                            account_key, "conversation", "repeated-memory-probe"
                        )
                        self.assertEqual(saved["messages"][0]["text"], "saved")
                finally:
                    app.runtime.close()
        finally:
            if not tracing_before:
                tracemalloc.stop()
