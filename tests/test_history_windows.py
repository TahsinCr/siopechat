"""SQL history windows, incremental writes and archive consistency."""

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
