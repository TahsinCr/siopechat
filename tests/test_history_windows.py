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
from unittest.mock import patch

from cli.history import prepare_virtual_transcript
from cli.paging import visible_window
from extensions.archive.repository import Files, SQLiteRepository, encode
from extensions.archive.sync import Archive


def message(index, size=400):
    return {"id": str(index), "role": "assistant", "text": f"Answer {index}: " + "x" * size}


class HistoryWindowTests(unittest.TestCase):
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
    def test_paging_keeps_overlap_anchor_and_late_page_cannot_replace_a_stream(self):
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
                window = archive.conversation_window("chatgpt/default", "chat")
                view.conversation_id = "@chatgpt/default/chat"
                view.messages = visible_window(window)
                view.tabs[0].update(
                    conversation_id=view.conversation_id,
                    messages=view.messages,
                    history_window=window["history_window"],
                )
                view._read_in_background = immediate
                view.follow_output = False
                view._virtual_transcript = prepare_virtual_transcript(view.messages, 60, {}, True)
                view.transcript_window.vertical_scroll = 0
                self.assertTrue(view.history_pager.request("older"))
                self.assertEqual(view.messages[0]["id"], "280")
                self.assertEqual(len(view.messages), 80)
                anchor = view.tabs[0]["history_anchor"]
                layout = prepare_virtual_transcript(view.messages, 60, {}, True)
                ordinals = [m["_archive_ordinal"] for m in view.messages]
                position = HistoryPager.position(layout, ordinals, anchor, 20, 0)
                target = next(
                    start
                    for start, _, index in layout.ranges
                    if view.messages[index]["id"] == "320"
                )
                self.assertEqual(position, target)
                self.assertTrue(view.history_pager.request("top"))
                self.assertEqual(view.messages[0]["id"], "0")
                self.assertTrue(view.history_pager.request("bottom"))
                self.assertEqual(view.messages[-1]["id"], "399")
                callbacks = []
                view._read_in_background = lambda operation, ready, failed, **kw: callbacks.append(
                    (operation, ready)
                )
                self.assertTrue(view.history_pager.request("older"))
                result = callbacks[0][0]()
                view.tabs[0]["busy"] = True
                current = view.messages
                view.messages.append({"role": "assistant", "text": "Live response"})
                callbacks[0][1](result)
                self.assertIs(view.messages, current)
                self.assertEqual(view.messages[-1]["text"], "Live response")
                view.tabs[0]["busy"] = False
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


class RenderReuseTests(unittest.TestCase):
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
                            view.app.invalidate()
                            await settle(lambda: view.tabs[0]["history_window"]["start"] < 320)
                            self.assertLessEqual(len(view.messages), 80)
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
