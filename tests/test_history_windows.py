"""SQL history windows, archive consistency and transcript render lifetime."""

import gc
import sqlite3
import tempfile
import threading
import time
import unittest
import weakref
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from cli.history import prepare_virtual_transcript
from cli.paging import visible_window
from extensions.archive.repository import Files, SQLiteRepository, encode
from extensions.archive.sync import Archive
from tests.history_support import message


class HistoryWindowTests(unittest.TestCase):
    def test_cached_structured_text_remains_visible_without_resync(self):
        self.archive.put(
            "chatgpt/a",
            "conversation",
            "audio",
            {
                "messages": [
                    {
                        "id": "thought",
                        "role": "assistant",
                        "channel": "analysis",
                        "text": "Thinking",
                    },
                    {
                        "id": "answer",
                        "role": "assistant",
                        "text": "",
                        "content": [
                            {
                                "kind": "provider",
                                "metadata": {
                                    "value": {
                                        "content_type": "audio_transcription",
                                        "text": "Spoken answer",
                                    }
                                },
                            }
                        ],
                    },
                ]
            },
        )
        page = self.archive.conversation_window("chatgpt/a", "audio")
        visible = visible_window(page)
        self.assertEqual([m["text"] for m in visible], ["Thinking", "Spoken answer"])

    def test_visible_windows_skip_internal_gaps_and_keep_thoughts_and_answer_reachable(self):
        data = [{"id": "user", "role": "user", "text": "Question"}]
        data += [{"id": f"tool-{i}", "role": "tool", "text": "internal"} for i in range(250)]
        data += [
            {"id": "thought", "role": "assistant", "channel": "analysis", "text": "t" * 300_000}
        ]
        data += [{"id": f"hidden-{i}", "role": "tool", "text": "internal"} for i in range(250)]
        data += [{"id": "answer", "role": "assistant", "channel": "final", "text": "Answer"}]
        data += [{"id": f"suffix-{i}", "role": "tool", "text": "internal"} for i in range(100)]
        self.archive.put("chatgpt/a", "conversation", "chat", {"messages": data})
        page = self.archive.conversation_window("chatgpt/a", "chat")
        self.assertEqual([m["id"] for m in page["messages"]], ["answer"])
        self.assertEqual(page["history_window"]["end"], len(data))
        seen = set()
        while True:
            self.assertTrue(page["messages"])
            seen.update(m["id"] for m in page["messages"])
            if page["history_window"]["start"] == 0:
                break
            boundary = page["messages"][0]["_archive_ordinal"]
            page = self.archive.conversation_window("chatgpt/a", "chat", before=boundary)
        self.assertEqual(seen, {"user", "thought", "answer"})
        raw = self.repo.conversation_window("chatgpt/a", "chat")
        self.assertTrue(any(m["role"] == "tool" for m in raw["messages"]))

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.repo = SQLiteRepository(self.root / "archive.sqlite3")
        self.archive = Archive(self.repo, Files(self.root / "files"))
        self.addCleanup(self.archive.close)

    def test_window_reads_are_bounded_scoped_and_never_decode_full_record(self):
        self.archive.put(
            "claude/a",
            "conversation",
            "chat",
            {
                "title": "Saved",
                "metadata": {"native_body": "x" * 1_000_000},
                "messages": [message(i) for i in range(2000)],
            },
        )
        with patch.object(self.repo, "get", side_effect=AssertionError("full record read")):
            tail = self.archive.conversation_window("claude/a", "chat")
            middle = self.archive.conversation_window("claude/a", "chat", start=900)
            self.assertIsNone(self.archive.conversation_window("claude/b", "chat"))
        self.assertEqual(tail["messages"][-1]["id"], "1999")
        self.assertEqual(middle["messages"][0]["id"], "900")
        self.assertEqual(len(tail["messages"]), 80)
        self.assertEqual(tail["history_window"], {"start": 1920, "end": 2000, "total": 2000})
        self.assertNotIn("metadata", tail)
        self.assertEqual(self.archive._size, 0)

    def test_character_budget_limits_windows_without_truncating_single_messages(self):
        self.archive.put(
            "gemini/a",
            "conversation",
            "chat",
            {"messages": [message(i, 50_000) for i in range(100)]},
        )
        tail = self.archive.conversation_window("gemini/a", "chat")
        self.assertLessEqual(len(tail["messages"]), 5)
        one = self.archive.conversation_window("gemini/a", "chat", max_chars=2)
        self.assertEqual(len(one["messages"]), 1)
        self.assertEqual(one["messages"][0]["text"], message(99, 50_000)["text"])

    def test_append_preserves_old_rows_and_is_idempotent_without_full_reads(self):
        self.archive.put(
            "claude/a",
            "conversation",
            "chat",
            {"title": "Saved", "messages": [message(i) for i in range(1000)]},
        )
        with closing(sqlite3.connect(self.repo.path)) as db:
            db.execute(
                "CREATE TRIGGER protect_history BEFORE DELETE ON messages "
                "WHEN OLD.ordinal < 968 BEGIN SELECT RAISE(ABORT,'old row rewritten'); END"
            )
            db.commit()
        turn = {
            "title": "Saved",
            "messages": [
                {"id": "new-user", "role": "user", "text": "Question", "local_pending": True},
                {
                    "id": "new-answer",
                    "role": "assistant",
                    "text": "New",
                    "local_pending": True,
                    "resources": [{"id": "file", "path": "local/file"}],
                },
            ],
        }
        with patch.object(self.archive, "get", side_effect=AssertionError("full read")):
            self.archive.save_turn("claude/a", "chat", turn)
            self.archive.save_turn("claude/a", "chat", turn)
        result = self.repo.get("claude/a", "conversation", "chat")
        self.assertEqual(len(result["messages"]), 1002)
        self.assertEqual(result["messages"][0], message(0))
        self.assertEqual(result["messages"][-1]["text"], "New")
        self.assertNotIn("_archive_sql_messages", result)
        self.assertEqual(len(self.repo.resources("claude/a", "conversation", "chat")), 1)

    def test_v3_migration_preserves_exact_history_and_source_references(self):
        value = {
            "title": "Legacy",
            "messages": [dict(message(0), resource={"path": "old/path"})],
            "metadata": {"native": {"messages": [message(0)]}},
        }
        self.repo.put("chatgpt/a", "conversation", "chat", value)
        with closing(sqlite3.connect(self.repo.path)) as db:
            db.execute("UPDATE records SET payload=? WHERE kind='conversation'", (encode(value),))
            db.execute("UPDATE conversations SET metadata=?", (encode(value),))
            db.execute("DELETE FROM messages")
            for table in (
                "pending_branches",
                "response_variant_groups",
                "response_variants",
                "superseded_messages",
            ):
                db.execute(f"DROP TABLE {table}")
            db.execute("UPDATE schema_version SET version=3")
            db.commit()
        upgraded = SQLiteRepository(self.repo.path)
        self.assertEqual(upgraded.get("chatgpt/a", "conversation", "chat"), value)
        self.assertIn("old/path", upgraded.file_references())
        with closing(sqlite3.connect(self.repo.path)) as db:
            self.assertNotIn('"messages":', db.execute("SELECT payload FROM records").fetchone()[0])

    def test_archived_pending_turns_do_not_accumulate_when_windows_change(self):
        old = [dict(message(i), local_pending=True, _archive_ordinal=i) for i in range(80)]
        current = {"messages": [dict(message(i), _archive_ordinal=i) for i in range(80, 160)]}
        self.assertEqual(len(visible_window(current, old)), 80)

    def test_disposed_transcript_releases_source_even_if_last_frame_keeps_layout(self):
        class Source(str):
            pass

        source = Source("A retained answer")
        reference = weakref.ref(source)
        layout = prepare_virtual_transcript([{"role": "assistant", "text": source}], 60, {}, True)
        del source
        layout.prefetch(0, layout.line_count)
        layout.dispose()
        gc.collect()
        self.assertIsNone(reference())
        self.assertEqual(layout.cache_bytes, 0)
        self.assertEqual(layout.messages, ())

    def test_disposal_does_not_wait_for_slow_background_render(self):
        layout = prepare_virtual_transcript([message(1)], 60, {}, True)
        entered, release = threading.Event(), threading.Event()
        render = layout._render

        def slow(index):
            entered.set()
            release.wait(3)
            return render(index)

        with patch.object(layout, "_render", slow):
            worker = threading.Thread(target=lambda: layout.prefetch(0, layout.line_count))
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                start = time.monotonic()
                layout.dispose()
                self.assertLess(time.monotonic() - start, 0.1)
            finally:
                release.set()
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(layout.messages, ())


class HistoryReadTests(unittest.TestCase):
    @staticmethod
    def page(version, start, end, total=4):
        return {
            "history_version": version,
            "history_window": {"start": start, "end": end, "total": total},
            "messages": [
                dict(message(i, 20), text=version, _archive_ordinal=i) for i in range(start, end)
            ],
        }

    def test_complete_read_restarts_instead_of_mixing_archive_revisions(self):
        from types import SimpleNamespace

        from cli.history import read_history

        source = SimpleNamespace(
            conversation_window=Mock(
                side_effect=[
                    self.page("old", 0, 1),
                    self.page("new", 1, 2),
                    self.page("new", 0, 1),
                    self.page("new", 1, 4),
                ]
            )
        )
        value = read_history(source, "chatgpt/a", "chat", start=0, complete=True)
        self.assertEqual([m["id"] for m in value["messages"]], ["0", "1", "2", "3"])
        self.assertTrue(all(m["text"] == "new" for m in value["messages"]))
        self.assertEqual(source.conversation_window.call_count, 4)

    def test_bounded_complete_read_stops_at_the_requested_resident_end(self):
        from types import SimpleNamespace

        from cli.history import read_history

        source = SimpleNamespace(
            conversation_window=Mock(return_value=self.page("v", 40, 100, total=200))
        )
        value = read_history(source, "chatgpt/a", "chat", start=40, complete=True, until=65)
        self.assertEqual(
            [message["_archive_ordinal"] for message in value["messages"]], list(range(40, 65))
        )
        self.assertEqual(value["history_window"], {"start": 40, "end": 65, "total": 200})
        source.conversation_window.assert_called_once_with("chatgpt/a", "chat", start=40)

    def test_continuous_revision_changes_fail_without_publishing_a_partial_read(self):
        from types import SimpleNamespace

        from cli.history import read_history

        source = SimpleNamespace(
            conversation_window=Mock(
                side_effect=[
                    self.page("a", 0, 1),
                    self.page("b", 1, 2),
                    self.page("c", 0, 1),
                    self.page("d", 1, 2),
                ]
            )
        )
        with self.assertRaisesRegex(RuntimeError, "changed"):
            read_history(source, "chatgpt/a", "chat", start=0, complete=True)
        self.assertEqual(source.conversation_window.call_count, 4)

    def test_closed_read_stops_between_sql_batches(self):
        from types import SimpleNamespace

        from cli.history import read_history

        source = SimpleNamespace(conversation_window=Mock(return_value=self.page("a", 0, 1)))
        self.assertIsNone(
            read_history(
                source,
                "chatgpt/a",
                "chat",
                start=0,
                complete=True,
                cancelled=lambda: source.conversation_window.call_count > 0,
            )
        )
        self.assertEqual(source.conversation_window.call_count, 1)

    def test_view_metadata_cannot_change_the_persistent_conversation_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = SQLiteRepository(Path(directory) / "archive.sqlite3")
            value = {"messages": [message(0)]}
            repo.put("chatgpt/a", "conversation", "chat", value)
            before = repo.conversation_window("chatgpt/a", "chat")
            self.assertFalse(
                repo.put(
                    "chatgpt/a",
                    "conversation",
                    "chat",
                    dict(
                        value,
                        history_window=before["history_window"],
                        history_version=before["history_version"],
                    ),
                )
            )
            self.assertEqual(
                repo.conversation_window("chatgpt/a", "chat")["history_version"],
                before["history_version"],
            )


class RenderReuseTests(unittest.TestCase):
    def test_tab_switch_reuses_layout_and_close_releases_its_source(self):
        from tests.cli_support import immediate, terminal

        class Source(str):
            pass

        with terminal() as (view, _):
            view._read_in_background = immediate
            source = Source("**Original message**\n\n" * 40)
            ref = weakref.ref(source)
            view.messages = [{"role": "assistant", "text": source}]
            view._save_active_tab()
            del source
            view._virtual_transcript_rows(20)
            original = view._virtual_transcript
            view.new_tab()
            view._virtual_transcript_rows(20)
            with patch(
                "cli.chat.prepare_virtual_transcript", side_effect=AssertionError("rerender")
            ):
                view.select_tab(0)
                view._virtual_transcript_rows(20)
            self.assertIs(view._virtual_transcript, original)
            view.close_tab()
            gc.collect()
            self.assertTrue(original._disposed)
            self.assertIsNone(ref())

    def test_inactive_render_cache_evicts_oldest_layout_under_shared_budget(self):
        from types import SimpleNamespace

        from tests.cli_support import terminal

        with terminal() as (view, _):
            layouts = [
                SimpleNamespace(resident_bytes=2 * 1024 * 1024, dispose=Mock()) for _ in range(3)
            ]
            view.tabs = [
                dict(render_layout=layout, render_used=i) for i, layout in enumerate(layouts)
            ]
            view.active_tab = 2
            view._trim_transcript_cache()
            layouts[0].dispose.assert_called_once_with()
            self.assertNotIn("render_layout", view.tabs[0])
            self.assertIn("render_layout", view.tabs[1])
            layouts[2].dispose.assert_not_called()

    def test_shared_viewport_worker_uses_latest_target_and_clear_cancels_queued_work(self):
        from types import SimpleNamespace

        from cli.transcript import TranscriptPrefetch

        jobs = []
        layout = SimpleNamespace(line_count=1000, prefetch=Mock())
        queue = TranscriptPrefetch(lambda *args: jobs.append(args), Mock())
        queue.request(layout, 0, 20)
        for top in range(400):
            queue.request(layout, top, 20)
        self.assertEqual(len(jobs), 1)
        result = jobs[0][0]()
        self.assertEqual(layout.prefetch.call_args_list[0].args[:2], (399, 419))
        jobs[0][1](result)
        queue.request(layout, 500, 20)
        queue.clear()
        self.assertIsNone(jobs[1][0]())
        self.assertIsNone(queue._target)

    def test_prepend_reuses_geometry_and_rendered_rows_without_rich_conversion(self):
        from cli.markdown import MessageRenderer
        from cli.transcript import VirtualTranscript

        data = [message(i, 60) for i in range(5)]
        renderer = MessageRenderer()
        old = VirtualTranscript(data, 60, renderer=renderer, preserve_order=True)
        old.prefetch(0, old.line_count)
        old_spans = old._pages[(0, 0)][0][0][0]
        with patch.object(renderer, "line_count", wraps=renderer.line_count) as measure:
            new = VirtualTranscript(
                [message(-1, 60), *data], 60, renderer=renderer, preserve_order=True, previous=old
            )
            self.assertEqual(measure.call_count, 1)
        self.assertIs(new._pages[(1, 0)][0][0][0], old_spans)
        self.assertEqual(new._pages[(1, 0)][0][0][1], 1)
        old.dispose(retain_renderer=True)
        self.assertIsNotNone(new.cached_row(new.ranges[1][0]))
        self.assertLessEqual(new.cache_bytes, new.CACHE_BYTES)

    def test_large_code_index_is_shared_across_prepend_without_reindexing(self):
        from cli.transcript import VirtualTranscript

        data = [{"role": "assistant", "text": "```text\n" + "body\n" * 20_000 + "```"}]
        old = VirtualTranscript(data, 60, preserve_order=True)
        with patch.object(
            old.renderer, "large_code_block", wraps=old.renderer.large_code_block
        ) as index:
            new = VirtualTranscript(
                [message(-1), *data], 60, renderer=old.renderer, preserve_order=True, previous=old
            )
            self.assertEqual(index.call_count, 1)
        self.assertIs(old._large_code[0][0].offsets, new._large_code[1][0].offsets)

    def test_render_cache_compresses_large_ansi_and_retains_no_source_keys(self):
        from cli.markdown import ChatMarkdown, MessageRenderer

        renderer = MessageRenderer()
        renderer.cache_byte_limit = 32 * 1024
        text = "**Formatted paragraph** with a [link](https://example.com).\n\n" * 300
        with patch("cli.markdown.ChatMarkdown", wraps=ChatMarkdown) as render:
            first = renderer.render("assistant", text, 60, False, False)
            second = renderer.render("assistant", text, 60, True, False)
            self.assertEqual(first, second)
            self.assertEqual(render.call_count, 1)
        self.assertTrue(any(isinstance(v, bytes) for v in renderer.cache.values()))
        self.assertTrue(
            all(isinstance(key[1], bytes) and len(key[1]) == 32 for key in renderer.cache)
        )
        self.assertLessEqual(renderer.cache_bytes, renderer.cache_byte_limit)

    def test_closing_tab_releases_queued_render_payload_and_viewport_target(self):
        from tests.cli_support import terminal

        class Source(str):
            pass

        with terminal() as (view, _):
            source = Source("source body\n" * 10_000)
            ref = weakref.ref(source)
            view.messages = [{"role": "assistant", "text": source}]
            view._save_active_tab()
            del source
            jobs = []
            view._read_in_background = lambda read, ready, failed, **kw: jobs.append(
                (read, ready, failed)
            )
            view._virtual_transcript_rows(20)
            self.assertTrue(view.tabs[0]["render_payload"])
            view.close_tab()
            gc.collect()
            self.assertIsNone(ref())
            self.assertIsNone(view._transcript_prefetch._target)
            with self.assertRaises(InterruptedError):
                jobs[0][0]()

    def test_history_layout_reuses_counts_after_rendered_text_was_evicted(self):
        from cli.markdown import ChatMarkdown, MessageRenderer
        from cli.transcript import VirtualTranscript

        renderer = MessageRenderer()
        renderer.cache_byte_limit = 1
        messages = [{"role": "assistant", "text": f"**Message {i}**\n\n" * 8} for i in range(30)]
        with patch("cli.markdown.ChatMarkdown", wraps=ChatMarkdown) as render:
            first = VirtualTranscript(messages, 60, renderer=renderer)
            calls = render.call_count
            self.assertFalse(renderer.cache)
            second = VirtualTranscript(messages, 60, renderer=renderer)
            self.assertEqual(render.call_count, calls)
            self.assertEqual(first.ranges, second.ranges)
            changed = [dict(m) for m in messages]
            changed[-1]["text"] += "\nChanged"
            VirtualTranscript(changed, 60, renderer=renderer)
            self.assertEqual(render.call_count, calls + 1)
        self.assertLessEqual(len(renderer._line_counts), renderer.cache_limit)
        self.assertTrue(
            all(isinstance(key[1], bytes) and len(key[1]) == 32 for key in renderer._line_counts)
        )
        renderer.clear_cache()
        self.assertFalse(renderer._line_counts)

    def test_stream_updates_reuse_markdown_for_unchanged_messages(self):
        from cli.markdown import ChatMarkdown

        stable = {"role": "user", "text": "Stable **formatted** prompt"}
        with patch("cli.markdown.ChatMarkdown", wraps=ChatMarkdown) as markdown:
            first = prepare_virtual_transcript(
                [stable, {"role": "assistant", "text": "Partial"}], 60, {}, True, active_index=1
            )
            second = prepare_virtual_transcript(
                [stable, {"role": "assistant", "text": "Partial answer"}],
                60,
                {},
                True,
                active_index=1,
                renderer=first.renderer,
            )
            first.dispose(retain_renderer=True)
            self.assertEqual(markdown.call_count, 3)
            self.assertTrue(second.renderer.cache)
            second.dispose()
            self.assertFalse(second.renderer.cache)

    def test_closed_split_pane_drops_its_source_and_formatted_cache(self):
        from extensions.layout.panes import SplitLayout, _Pane
        from tests.cli_support import terminal

        with terminal(layout_factory=SplitLayout) as (view, _):
            view.messages = [message(1)]
            view._save_active_tab()
            key = view.tabs[0]["tab_id"]
            view.new_tab()
            view._save_active_tab()
            view.layout_controller.groups = [
                {
                    "axis": "columns",
                    "tabs": [key, view.tabs[1]["tab_id"]],
                }
            ]
            view.select_tab(0)
            pane = _Pane(view, key)
            view.layout_controller._panes[key] = pane
            pane.transcript_rows(1)
            self.assertTrue(pane.renderer.cache)
            view.close_tab()
            self.assertFalse(pane.messages)
            self.assertFalse(pane.renderer.cache)
            self.assertNotIn(key, view.layout_controller._panes)
