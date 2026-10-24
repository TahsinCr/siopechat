"""SQL history windows, incremental writes and render lifetime regressions."""

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


def message(index, size=400):
    return {"id": str(index), "role": "assistant", "text": f"Answer {index}: " + "x" * size}


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


class PagingUITests(unittest.TestCase):
    def test_wheel_uses_published_geometry_before_the_next_terminal_paint(self):
        from types import SimpleNamespace

        from tests.test_cli_interaction import terminal

        with terminal() as (view, _):
            view.transcript_window.render_info = SimpleNamespace(
                content_height=48, window_height=34
            )
            view.transcript_line_count = 94
            view._virtual_transcript = SimpleNamespace(line_count=94)
            view.transcript_window.vertical_scroll = 52
            view.transcript_window._scroll_up()
            self.assertEqual(view.transcript_window.vertical_scroll, 48)
            view.transcript_window._scroll_down()
            self.assertEqual(view.transcript_window.vertical_scroll, 52)

    def test_paged_render_does_not_move_later_thoughts_above_the_viewport(self):
        data = [
            {"role": "assistant", "text": "Earlier commentary"},
            {"role": "reasoning", "text": "Later summary"},
            {"role": "assistant", "text": "Final answer"},
        ]
        layout = prepare_virtual_transcript(data, 60, {}, True, preserve_order=True)
        self.assertEqual(layout.entries, [0, 1, 2])
        self.assertEqual([index for _, _, index in layout.ranges], [0, 1, 2])
        ordinary = prepare_virtual_transcript(data, 60, {}, True)
        self.assertEqual(ordinary.entries, [1, 0, 2])

    def test_loaded_messages_are_retained_and_duplicate_pages_are_deduplicated(self):
        from types import SimpleNamespace
        from unittest.mock import Mock

        from tests.test_cli_interaction import terminal

        for direction, original, next_index in (("newer", (1, 2, 3), 4), ("older", (2, 3, 4), 1)):
            with self.subTest(direction=direction):
                messages = [dict(message(i, 20), _archive_ordinal=i) for i in original]
                anchor_index = 0 if direction == "newer" else 2
                result = {
                    "messages": [dict(message(next_index, 20), _archive_ordinal=next_index)],
                    "history_window": {"start": next_index, "end": next_index + 1, "total": 6},
                }
                archive = SimpleNamespace(conversation_window=Mock(return_value=result))
                with terminal(initial_messages=messages) as (view, _):
                    tab = view.tabs[0]
                    tab.update(
                        conversation_id="@chatgpt/default/chat",
                        history_window={"start": original[0], "end": original[-1] + 1, "total": 6},
                    )
                    view.archive_service = lambda: archive
                    view._virtual_transcript = prepare_virtual_transcript(messages, 60, {}, True)
                    view._virtual_transcript.ordinals = original
                    view.transcript_window.vertical_scroll = view._virtual_transcript.ranges[
                        anchor_index
                    ][0]
                    jobs = []
                    view._read_in_background = lambda read, ready, failed, **kw: jobs.append(
                        (read, ready)
                    )
                    self.assertTrue(view.history_pager.request(direction))
                    read, ready = jobs.pop()
                    ready(read())
                    self.assertEqual(
                        [m["_archive_ordinal"] for m in view.messages],
                        sorted((*original, next_index)),
                    )
                    self.assertTrue(view.history_pager.request(direction))
                    read, ready = jobs.pop()
                    ready(read())
                    self.assertEqual(len(view.messages), 4)

    def test_disjoint_sql_page_keeps_the_message_still_being_read(self):
        from types import SimpleNamespace
        from unittest.mock import Mock

        from tests.test_cli_interaction import terminal

        for provider in ("chatgpt", "claude", "gemini"):
            for direction, next_ordinal in (("older", 0), ("newer", 2)):
                with self.subTest(provider=provider, direction=direction):
                    current = dict(message(1), text="body\n" * 500, _archive_ordinal=1)
                    incoming = dict(message(next_ordinal), _archive_ordinal=next_ordinal)
                    result = {
                        "messages": [incoming],
                        "history_window": {
                            "start": next_ordinal,
                            "end": next_ordinal + 1,
                            "total": 3,
                        },
                    }
                    archive = SimpleNamespace(conversation_window=Mock(return_value=result))
                    with terminal(initial_messages=[current]) as (view, _):
                        tab = view.tabs[0]
                        tab.update(
                            conversation_id=f"@{provider}/default/chat",
                            history_window={"start": 1, "end": 2, "total": 3},
                        )
                        view.archive_service = lambda: archive
                        view._virtual_transcript = prepare_virtual_transcript(
                            [current], 60, {}, True
                        )
                        view._virtual_transcript.ordinals = (1,)
                        view.transcript_window.vertical_scroll = 20
                        jobs = []
                        view._read_in_background = lambda read, ready, failed, **kw: jobs.append(
                            (read, ready)
                        )
                        self.assertTrue(view.history_pager.request(direction))
                        read, ready = jobs.pop()
                        ready(read())
                        self.assertEqual(
                            [m["_archive_ordinal"] for m in view.messages],
                            sorted((1, next_ordinal)),
                        )
                        self.assertEqual(tab["history_anchor"], {"ordinal": 1, "offset": 20})
                        self.assertIs(
                            next(m for m in view.messages if m["id"] == "1")["text"],
                            current["text"],
                        )

    def test_idle_repaints_cannot_ping_pong_between_short_pages(self):
        from types import SimpleNamespace
        from unittest.mock import Mock

        from tests.test_cli_interaction import terminal

        with terminal() as (view, _):
            view.tabs[0]["history_window"] = {"start": 40, "end": 80, "total": 120}
            view.follow_output = False
            view.history_pager.request = Mock(return_value=True)
            layout = SimpleNamespace(line_count=20)
            for _ in range(100):
                view.history_pager.prefetch(layout, 0, 40)
            view.history_pager.request.assert_not_called()
            view.transcript_window.scroll_direction = -1
            view.history_pager.prefetch(layout, 0, 40)
            view.history_pager.request.assert_called_once_with("older")
            for _ in range(100):
                view.history_pager.prefetch(layout, 0, 40)
            view.history_pager.request.assert_called_once_with("older")

    def test_newer_target_uses_visible_ordinals_after_hidden_prefix(self):
        from types import SimpleNamespace
        from unittest.mock import Mock

        from tests.test_cli_interaction import terminal

        messages = [dict(message(i, 20), _archive_ordinal=i) for i in range(2000, 2080)]
        archive = SimpleNamespace(conversation_window=Mock(return_value=None))
        with terminal(initial_messages=messages) as (view, _):
            view.tabs[view.active_tab].update(
                conversation_id="@chatgpt/default/chat",
                history_window={"start": 0, "end": 2080, "total": 2500},
            )
            view.archive_service = lambda: archive
            jobs = []
            view._read_in_background = lambda read, ready, failed, **kw: jobs.append((read, ready))
            self.assertTrue(view.history_pager.request("newer"))
            jobs[0][0]()
            archive.conversation_window.assert_called_once_with(
                "chatgpt/default", "chat", start=2080
            )

    def test_anchor_follows_message_identity_after_sql_ordinals_change(self):
        from cli.paging import HistoryPager

        data = [dict(message(i, 20), _archive_ordinal=i) for i in range(100, 103)]
        layout = prepare_virtual_transcript(data, 60, {}, True)
        layout.message_ids = ("a", "b", "c")
        anchor = {"message_id": "b", "ordinal": 1, "offset": 1}
        self.assertEqual(
            HistoryPager.position(layout, (100, 101, 102), anchor, 20, 0), layout.ranges[1][0] + 1
        )

    def test_last_edge_request_keeps_fetched_prefix_and_loads_missing_tail(self):
        from types import SimpleNamespace
        from unittest.mock import Mock

        from tests.test_cli_interaction import terminal

        original = [dict(message(i, 20), _archive_ordinal=i) for i in range(40, 120)]
        pages = [
            {
                "messages": [dict(message(i, 20), _archive_ordinal=i) for i in range(a, b)],
                "history_window": {"start": a, "end": b, "total": 200},
            }
            for a, b in ((0, 40), (120, 200))
        ]
        with terminal(initial_messages=original) as (view, _):
            tab = view.tabs[0]
            tab.update(
                conversation_id="@chatgpt/default/chat",
                history_window={"start": 40, "end": 120, "total": 200},
            )
            view.archive_service = lambda: SimpleNamespace(
                conversation_window=Mock(side_effect=pages)
            )
            jobs = []
            view._read_in_background = lambda read, ready, failed, **kw: jobs.append((read, ready))
            self.assertTrue(view.history_pager.request("older"))
            self.assertTrue(view.history_pager.request("top"))
            self.assertTrue(view.history_pager.request("bottom"))
            read, ready = jobs.pop(0)
            ready(read())
            self.assertEqual([m["id"] for m in view.messages], [str(i) for i in range(120)])
            read, ready = jobs.pop(0)
            ready(read())
            self.assertEqual([m["id"] for m in view.messages], [str(i) for i in range(200)])
            self.assertTrue(view.follow_output)

    def test_direction_reversal_keeps_new_prefix_and_the_latest_viewport(self):
        from types import SimpleNamespace

        from tests.test_cli_interaction import terminal

        original = [dict(message(i, 20), _archive_ordinal=i) for i in range(40, 120)]
        window = {"start": 40, "end": 120, "total": 200}
        stale = {
            "messages": [dict(message(i, 20), _archive_ordinal=i) for i in range(0, 80)],
            "history_window": {"start": 0, "end": 80, "total": 200},
        }
        with terminal(initial_messages=original) as (view, _):
            tab = view.tabs[view.active_tab]
            tab.update(conversation_id="@chatgpt/default/chat", history_window=window)
            view.archive_service = lambda: SimpleNamespace(
                conversation_window=lambda *a, **kw: stale
            )
            layout = prepare_virtual_transcript(original, 60, {}, True)
            layout.ordinals = tuple(range(40, 120))
            view._virtual_transcript = layout
            view.follow_output = False
            view.transcript_window.vertical_scroll = 0
            jobs = []
            view._read_in_background = lambda read, ready, failed, **kw: jobs.append((read, ready))
            self.assertTrue(view.history_pager.request("older"))
            view.transcript_window.vertical_scroll = layout.ranges[70][0]
            read, ready = jobs.pop()
            ready(read())
            self.assertEqual(tab["history_window"], {"start": 0, "end": 120, "total": 200})
            self.assertEqual(len(view.messages), 120)
            self.assertEqual(tab["history_anchor"]["ordinal"], 110)
            self.assertEqual(view.messages[-1]["_archive_ordinal"], 119)
            self.assertEqual(view.transcript_window.vertical_scroll, layout.ranges[70][0])

    def test_prepend_retains_tail_and_late_read_cannot_replace_a_stream(self):
        from cli.paging import HistoryPager
        from tests.account_fixture import configured_application
        from tests.test_cli_interaction import immediate, terminal

        with tempfile.TemporaryDirectory() as folder:
            api = configured_application(Path(folder))
            self.addCleanup(api.runtime.close)
            archive = api.get("archive")
            archive.put(
                "chatgpt/default",
                "conversation",
                "chat",
                {"messages": [message(i, 20) for i in range(400)]},
            )
            with terminal(account_service=api.get("accounts")) as (view, _):
                page = archive.conversation_window("chatgpt/default", "chat")
                view.conversation_id = "@chatgpt/default/chat"
                view.messages = visible_window(page)
                view.tabs[0].update(
                    conversation_id=view.conversation_id,
                    messages=view.messages,
                    history_window=page["history_window"],
                )
                view._read_in_background = immediate
                view.follow_output = False
                view._virtual_transcript = prepare_virtual_transcript(view.messages, 60, {}, True)
                view.transcript_window.vertical_scroll = 0
                self.assertTrue(view.history_pager.request("older"))
                self.assertEqual(
                    [m["id"] for m in view.messages], [str(i) for i in range(240, 400)]
                )
                anchor = view.tabs[0]["history_anchor"]
                layout = prepare_virtual_transcript(view.messages, 60, {}, True)
                ordinals = [m["_archive_ordinal"] for m in view.messages]
                self.assertEqual(
                    HistoryPager.position(layout, ordinals, anchor, 20, 0), layout.ranges[80][0]
                )
                jobs = []
                view._read_in_background = lambda read, ready, failed, **kw: jobs.append(
                    (read, ready)
                )
                self.assertTrue(view.history_pager.request("older"))
                read, ready = jobs.pop()
                result = read()
                view.tabs[0]["busy"] = True
                current = view.messages
                current.append({"role": "assistant", "text": "Live response"})
                ready(result)
                self.assertIs(view.messages, current)
                self.assertEqual(view.messages[-1]["text"], "Live response")
                view.tabs[0]["busy"] = False
                view.messages.pop()
                view._read_in_background = immediate
                self.assertTrue(view.history_pager.request("top"))
                self.assertEqual([m["id"] for m in view.messages], [str(i) for i in range(400)])
                self.assertFalse(view.history_pager.request("bottom"))
                view._reads.close()
                view._render_reads.close()
                view._operations.close()

    def test_late_history_page_preserves_the_viewport_after_fast_user_scroll(self):
        from tests.account_fixture import configured_application
        from tests.test_cli_interaction import terminal

        with tempfile.TemporaryDirectory() as folder:
            api = configured_application(Path(folder))
            self.addCleanup(api.runtime.close)
            archive = api.get("archive")
            archive.put(
                "chatgpt/default",
                "conversation",
                "chat",
                {"messages": [message(i, 20) for i in range(400)]},
            )
            callbacks = []
            with terminal(account_service=api.get("accounts")) as (view, _):
                window = archive.conversation_window("chatgpt/default", "chat")
                view.conversation_id = "@chatgpt/default/chat"
                view.messages = visible_window(window)
                tab = view.tabs[0]
                tab.update(
                    conversation_id=view.conversation_id,
                    messages=view.messages,
                    history_window=window["history_window"],
                )
                view.follow_output = False
                view._virtual_transcript = prepare_virtual_transcript(view.messages, 60, {}, True)
                view.transcript_window.vertical_scroll = 0
                view._read_in_background = lambda operation, ready, failed, **kw: callbacks.append(
                    (operation, ready)
                )

                self.assertTrue(view.history_pager.request("older"))
                current_ordinal = 350
                current_line = next(
                    start
                    for start, _, index in view._virtual_transcript.ranges
                    if view.messages[index]["_archive_ordinal"] == current_ordinal
                )
                view.transcript_window.vertical_scroll = current_line
                result = callbacks[0][0]()
                callbacks[0][1](result)

                self.assertEqual(tab["history_anchor"]["ordinal"], current_ordinal)
                self.assertFalse(view.follow_output)
                view._reads.close()
                view._render_reads.close()
                view._operations.close()

    def test_tab_close_releases_selection_and_sources_without_deleting_archive(self):
        from tests.account_fixture import configured_application
        from tests.test_cli_interaction import terminal

        with tempfile.TemporaryDirectory() as folder:
            api = configured_application(Path(folder))
            try:
                archive = api.get("archive")
                archive.put("chatgpt/default", "conversation", "chat", {"messages": [message(0)]})
                with terminal(account_service=api.get("accounts")) as (view, _):
                    view.conversation_id = "@chatgpt/default/chat"
                    view.messages = visible_window(
                        archive.conversation_window("chatgpt/default", "chat")
                    )
                    view._save_active_tab()
                    layout = prepare_virtual_transcript(view.messages, 60, {}, True)
                    view._virtual_transcript = layout
                    view._virtual_tab_id = view.tabs[0]["tab_id"]
                    view.transcript_control.selection_rows = layout
                    closed = view.tabs[0]
                    view.close_tab()
                    self.assertEqual(closed["messages"], [])
                    self.assertEqual(layout.messages, ())
                    self.assertEqual(view.transcript_control.selection_rows, ())
                    self.assertIsNotNone(archive.conversation_window("chatgpt/default", "chat"))
                    view._reads.close()
                    view._render_reads.close()
                    view._operations.close()
            finally:
                api.runtime.close()


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
        from unittest.mock import Mock

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
        from unittest.mock import Mock

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
        from unittest.mock import Mock

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
        from tests.test_cli_interaction import immediate, terminal

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

        from tests.test_cli_interaction import terminal

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
        from tests.test_cli_interaction import terminal

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
        from tests.test_cli_interaction import terminal

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
            pane.render()
            self.assertTrue(pane.renderer.cache)
            view.close_tab()
            self.assertFalse(pane.messages)
            self.assertFalse(pane.renderer.cache)
            self.assertNotIn(key, view.layout_controller._panes)


class AsyncPagingUITests(unittest.IsolatedAsyncioTestCase):
    async def test_every_body_line_survives_disjoint_sql_pages_in_both_directions(self):
        import asyncio
        import re
        from types import SimpleNamespace

        from prompt_toolkit.data_structures import Size
        from prompt_toolkit.output import DummyOutput

        from tests.test_cli_interaction import terminal

        async def settle(predicate):
            for _ in range(400):
                if predicate():
                    return
                await asyncio.sleep(0.005)
            self.fail("paged transcript did not settle")

        expected = {f"TOKEN_{i}_{j}" for i in range(6) for j in range(45)}
        data = [
            {
                "id": str(i),
                "role": ("user", "assistant", "reasoning", "assistant", "reasoning", "assistant")[
                    i
                ],
                "text": "```text\n" + "\n".join(f"TOKEN_{i}_{j}" for j in range(45)) + "\n```",
            }
            for i in range(6)
        ]
        with tempfile.TemporaryDirectory() as directory:
            archive = Archive(
                SQLiteRepository(Path(directory) / "archive.sqlite3"),
                Files(Path(directory) / "files"),
            )
            archive.put("chatgpt/default", "conversation", "chat", {"messages": data})
            reader = SimpleNamespace(
                revision=0,
                states=lambda: [],
                conversation_window=lambda *args, **kwargs: archive.conversation_window(
                    *args, max_chars=600, **kwargs
                ),
            )
            page = reader.conversation_window("chatgpt/default", "chat")
            output = DummyOutput()
            output.get_size = lambda: Size(rows=43, columns=100)
            try:
                with terminal(output=output, initial_messages=visible_window(page)) as (view, pipe):
                    view.conversation_id = "@chatgpt/default/chat"
                    view._save_active_tab()
                    view.tabs[0]["history_window"] = page["history_window"]
                    view.archive_service = lambda: reader
                    seen = set()

                    def remember_painted_lines(_app):
                        info = view.transcript_window.render_info
                        rows = view.transcript_control.rows
                        if info is None or not hasattr(rows, "paint_row"):
                            return
                        for line in range(
                            info.vertical_scroll,
                            min(len(rows), info.vertical_scroll + info.window_height),
                        ):
                            row = rows.paint_row(line)
                            if row is not None:
                                seen.update(
                                    re.findall(r"TOKEN_\d+_\d+", "".join(x[1] for x in row[0]))
                                )

                    view.app.after_render += remember_painted_lines
                    run = asyncio.create_task(view.app.run_async())

                    def painted_window_ready():
                        layout = getattr(view, "_virtual_transcript", None)
                        info = view.transcript_window.render_info
                        if (
                            layout is None
                            or info is None
                            or view.tabs[0].get("history_read")
                            or view.tabs[0].get("history_anchor")
                            or getattr(view, "_virtual_pending_signature", None) is not None
                            or tuple(m.get("_archive_ordinal") for m in view.messages)
                            != getattr(layout, "ordinals", None)
                            or info.content_height != layout.line_count
                            or info.vertical_scroll != view.transcript_window.vertical_scroll
                        ):
                            return False
                        start = info.vertical_scroll
                        return all(
                            layout.cached_row(line) is not None
                            for line in range(
                                start, min(layout.line_count, start + info.window_height)
                            )
                        )

                    try:
                        await settle(lambda: getattr(view, "_virtual_transcript", None) is not None)
                        await settle(
                            lambda: (
                                view.transcript_window.render_info is not None
                                and view.transcript_window.render_info.content_height
                                == view._virtual_transcript.line_count
                            )
                        )
                        for mode, direction in (
                            (mode, direction)
                            for mode in ("wheel", "page keys", "mouse reports")
                            for direction in (-1, 1)
                        ):
                            seen.clear()
                            (view.scroll_bottom if direction < 0 else view.scroll_top)()
                            await settle(
                                lambda: (
                                    not view.tabs[0].get("history_read")
                                    and not view.tabs[0].get("history_anchor")
                                    and getattr(view, "_virtual_pending_signature", None) is None
                                )
                            )
                            await settle(painted_window_ready)
                            remember_painted_lines(view.app)
                            for _ in range(400):
                                if expected <= seen:
                                    break
                                frame = view.app.render_counter
                                scroll_revision = getattr(view, "_manual_scroll_revision", 0)
                                if mode == "wheel":
                                    (
                                        view.transcript_window._scroll_up
                                        if direction < 0
                                        else view.transcript_window._scroll_down
                                    )()
                                elif mode == "page keys":
                                    pipe.send_bytes(b"\x1b[5~" if direction < 0 else b"\x1b[6~")
                                else:
                                    pipe.send_bytes(
                                        b"\x1b[<64;60;10M" if direction < 0 else b"\x1b[<65;60;10M"
                                    )
                                view.app.invalidate()
                                if mode == "page keys":
                                    # Test each displayed page, not the frames
                                    # intentionally skipped by a fast key burst.
                                    await settle(
                                        lambda: (
                                            view.app.render_counter > frame
                                            and getattr(view, "_manual_scroll_revision", 0)
                                            > scroll_revision
                                            and painted_window_ready()
                                        )
                                    )
                                else:
                                    await asyncio.sleep(0.025)
                            self.assertEqual(
                                expected - seen, set(), f"missing body lines: {mode}, {direction}"
                            )
                    finally:
                        if view.app.is_running:
                            view.app.exit()
                        await run
                        view._reads.close()
                        view._render_reads.close()
                        view._operations.close()
            finally:
                archive.close()

    async def test_short_thought_page_stays_still_after_leaving_large_answer(self):
        import asyncio

        from prompt_toolkit.data_structures import Size
        from prompt_toolkit.output import DummyOutput

        from tests.test_cli_interaction import terminal

        async def settle(predicate):
            deadline = asyncio.get_running_loop().time() + 5
            while not predicate():
                if asyncio.get_running_loop().time() > deadline:
                    self.fail("viewport did not settle")
                await asyncio.sleep(0.01)

        with tempfile.TemporaryDirectory() as folder:
            repo = SQLiteRepository(Path(folder) / "archive.sqlite3")
            archive = Archive(repo, Files(Path(folder) / "files"))
            data = [
                {"role": "assistant", "channel": "analysis", "text": f"Thought {i}"}
                for i in range(6)
            ]
            data.append(
                {
                    "role": "assistant",
                    "channel": "final",
                    "text": "```text\n" + "body\n" * 60_000 + "```",
                }
            )
            archive.put("chatgpt/default", "conversation", "chat", {"messages": data})
            page = archive.conversation_window("chatgpt/default", "chat")
            output = DummyOutput()
            output.get_size = lambda: Size(rows=43, columns=140)
            try:
                with terminal(output=output, initial_messages=visible_window(page)) as (view, _):
                    view.conversation_id = "@chatgpt/default/chat"
                    view._save_active_tab()
                    view.tabs[0]["history_window"] = page["history_window"]
                    view.archive_service = lambda: archive
                    run = asyncio.create_task(view.app.run_async())
                    try:
                        await settle(
                            lambda: (
                                getattr(view, "_virtual_transcript", None) is not None
                                and view.transcript_window.render_info is not None
                            )
                        )
                        view.transcript_window.vertical_scroll = 0
                        view.transcript_window._scroll_up()
                        view.app.invalidate()
                        await settle(
                            lambda: (
                                view.tabs[0]["history_window"]["start"] == 0
                                and len(view.messages) == 7
                                and getattr(view, "_virtual_pending_signature", None) is None
                            )
                        )
                        window = view.tabs[0]["history_window"]
                        for _ in range(35):
                            view.app.invalidate()
                            await asyncio.sleep(0.02)
                            self.assertIs(view.tabs[0]["history_window"], window)
                    finally:
                        if view.app.is_running:
                            view.app.exit()
                        await run
                        view._reads.close()
                        view._render_reads.close()
                        view._operations.close()
            finally:
                archive.close()

    async def test_running_terminal_pages_both_edges_and_keeps_the_draft(self):
        import asyncio

        from tests.account_fixture import configured_application
        from tests.test_cli_interaction import terminal

        async def settle(predicate):
            deadline = asyncio.get_running_loop().time() + 5
            while not predicate():
                if asyncio.get_running_loop().time() > deadline:
                    self.fail("history window did not settle")
                await asyncio.sleep(0.01)

        with tempfile.TemporaryDirectory() as folder:
            api = configured_application(Path(folder))
            accounts, archive = api.get("accounts"), api.get("archive")
            archive.put(
                "chatgpt/default",
                "conversation",
                "chat",
                {"messages": [message(i, 20) for i in range(400)]},
            )
            try:
                with terminal(account_service=accounts) as (view, _):
                    with (
                        patch.object(view, "refresh_provider_catalog"),
                        patch.object(
                            accounts,
                            "load",
                            side_effect=lambda cid, key, **kw: accounts.cached_window(key, "chat"),
                        ),
                    ):
                        run = asyncio.create_task(view.app.run_async())
                        try:
                            await settle(lambda: view.app.is_running)
                            view.resume_conversation("@chatgpt/default/chat")
                            await settle(
                                lambda: bool(view.messages) and view.messages[-1]["id"] == "399"
                            )
                            view.input.text = "Unsent draft"
                            # Exercise actual paints and SQL reads between wheel
                            # events, including direction changes during paging.
                            for step in range(120):
                                if step % 30 < 15:
                                    view.transcript_window._scroll_up()
                                else:
                                    view.transcript_window._scroll_down()
                                view.app.invalidate()
                                await asyncio.sleep(0.005)
                            view.scroll_top()
                            await settle(lambda: view.messages[0]["id"] == "0")
                            view.scroll_bottom()
                            await settle(lambda: view.messages[-1]["id"] == "399")
                            await settle(
                                lambda: (
                                    view.tabs[0].get("history_anchor") is None
                                    and getattr(view, "_virtual_pending_signature", None) is None
                                )
                            )
                            view.pause_following()
                            view.transcript_window.vertical_scroll = 0
                            view.transcript_window._scroll_up()
                            view.app.invalidate()
                            await settle(lambda: view.tabs[0]["history_window"]["start"] < 320)
                            self.assertEqual(len(view.messages), 400)
                            self.assertEqual(view.input.text, "Unsent draft")
                            view.close_tab()
                            self.assertEqual(view.messages, [])
                        finally:
                            view.app.exit()
                            await run
                            view._reads.close()
                            view._render_reads.close()
                            view._operations.close()
            finally:
                await api.runtime.aclose()
