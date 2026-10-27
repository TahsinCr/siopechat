"""Synchronous paging, anchoring and retained-history behavior."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from tests.history_support import message
from tui.history import prepare_virtual_transcript, read_history
from tui.paging import visible_window


class PagingUITests(unittest.TestCase):
    def test_sources_remain_on_answer_across_sql_page_boundary(self):
        source = {"title": "Reference", "url": "https://example.com/reference"}
        pages = [
            {
                "messages": [
                    {
                        "id": "thought",
                        "role": "assistant",
                        "channel": "analysis",
                        "text": "Searching the web",
                        "_archive_ordinal": 1,
                    }
                ],
                "history_window": {"start": 0, "end": 2, "total": 4},
                "history_version": 1,
            },
            {
                "messages": [
                    {
                        "id": "answer",
                        "role": "assistant",
                        "text": "Answer",
                        "metadata": {"sources": [source]},
                        "_archive_ordinal": 2,
                    }
                ],
                "history_window": {"start": 2, "end": 4, "total": 4},
                "history_version": 1,
            },
        ]
        archive = SimpleNamespace(conversation_window=Mock(side_effect=pages))

        result = read_history(archive, "gemini/a", "chat", start=0, complete=True)

        self.assertEqual([item["role"] for item in result["messages"]], ["reasoning", "assistant"])
        self.assertNotIn("_thinking_sources", result["messages"][0])
        self.assertEqual(result["messages"][1]["_thinking_sources"], (source,))
        self.assertEqual(archive.conversation_window.call_count, 2)

    def test_wheel_uses_published_geometry_before_the_next_terminal_paint(self):
        from types import SimpleNamespace

        from tests.tui_support import terminal

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

        from tests.tui_support import terminal

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

        from tests.tui_support import terminal

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

        from tests.tui_support import terminal

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

        from tests.tui_support import terminal

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
        from tui.paging import HistoryPager

        data = [dict(message(i, 20), _archive_ordinal=i) for i in range(100, 103)]
        layout = prepare_virtual_transcript(data, 60, {}, True)
        layout.message_ids = ("a", "b", "c")
        anchor = {"message_id": "b", "ordinal": 1, "offset": 1}
        self.assertEqual(
            HistoryPager.position(layout, (100, 101, 102), anchor, 20, 0), layout.ranges[1][0] + 1
        )

    def test_last_edge_request_loads_the_requested_tail_page(self):
        from types import SimpleNamespace

        from tests.tui_support import terminal

        original = [dict(message(i, 20), _archive_ordinal=i) for i in range(40, 120)]
        pages = [
            {
                "messages": [dict(message(i, 20), _archive_ordinal=i) for i in range(a, b)],
                "history_window": {"start": a, "end": b, "total": 200},
            }
            for a, b in ((0, 40), (120, 200))
        ]
        archive = SimpleNamespace(conversation_window=Mock(side_effect=pages))
        with terminal(initial_messages=original) as (view, _):
            tab = view.tabs[0]
            tab.update(
                conversation_id="@chatgpt/default/chat",
                history_window={"start": 40, "end": 120, "total": 200},
            )
            view.archive_service = lambda: archive
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
            self.assertEqual([m["id"] for m in view.messages], [str(i) for i in range(120, 200)])
            self.assertTrue(view.follow_output)

    def test_edge_navigation_reads_only_the_requested_archive_page(self):
        from types import SimpleNamespace

        from tests.tui_support import terminal

        current = [dict(message(i, 20), _archive_ordinal=i) for i in range(40, 120)]
        pages = [
            {
                "messages": [dict(message(i, 20), _archive_ordinal=i) for i in range(0, 80)],
                "history_window": {"start": 0, "end": 80, "total": 200},
            },
            {
                "messages": [dict(message(i, 20), _archive_ordinal=i) for i in range(120, 200)],
                "history_window": {"start": 120, "end": 200, "total": 200},
            },
        ]
        archive = SimpleNamespace(conversation_window=Mock(side_effect=pages))
        with terminal(initial_messages=current) as (view, _):
            tab = view.tabs[0]
            tab.update(
                conversation_id="@chatgpt/default/chat",
                history_window={"start": 40, "end": 120, "total": 200},
            )
            view.archive_service = lambda: archive
            jobs = []
            view._read_in_background = lambda read, ready, failed, **kw: jobs.append((read, ready))

            self.assertTrue(view.history_pager.request("top"))
            read, ready = jobs.pop()
            ready(read())
            archive.conversation_window.assert_called_once_with("chatgpt/default", "chat", start=0)
            self.assertEqual(tab["history_window"], {"start": 0, "end": 80, "total": 200})
            self.assertEqual([m["_archive_ordinal"] for m in view.messages], list(range(80)))

            self.assertTrue(view.history_pager.request("bottom"))
            read, ready = jobs.pop()
            ready(read())
            archive.conversation_window.assert_called_with("chatgpt/default", "chat", start=None)
            self.assertEqual(tab["history_window"], {"start": 120, "end": 200, "total": 200})
            self.assertEqual([m["_archive_ordinal"] for m in view.messages], list(range(120, 200)))

    def test_refresh_without_a_resident_range_loads_only_the_tail_page(self):
        from types import SimpleNamespace

        from tests.tui_support import terminal

        page = {
            "messages": [dict(message(i, 20), _archive_ordinal=i) for i in range(320, 400)],
            "history_window": {"start": 320, "end": 400, "total": 400},
        }
        archive = SimpleNamespace(conversation_window=Mock(return_value=page))
        with terminal(initial_messages=[]) as (view, _):
            tab = view.tabs[0]
            tab.update(
                conversation_id="@chatgpt/default/chat",
                history_window={"start": 0, "end": 0, "total": 400},
            )
            view.archive_service = lambda: archive
            jobs = []
            view._read_in_background = lambda read, ready, failed, **kw: jobs.append((read, ready))

            self.assertTrue(view.history_pager.request("bottom", refresh=True))
            read, ready = jobs.pop()
            ready(read())

        archive.conversation_window.assert_called_once_with("chatgpt/default", "chat", start=None)
        self.assertEqual([m["_archive_ordinal"] for m in view.messages], list(range(320, 400)))

    def test_scrolling_keeps_a_bounded_contiguous_resident_message_range(self):
        from types import SimpleNamespace

        from tests.tui_support import terminal
        from tui.paging import MAX_LOADED_MESSAGES

        cases = (
            ("older", 240, 480, 160, 240, 160, 400),
            ("newer", 160, 400, 400, 480, 240, 480),
        )
        for direction, first, end, page_first, page_end, kept_first, kept_end in cases:
            with self.subTest(direction=direction):
                current = [dict(message(i, 20), _archive_ordinal=i) for i in range(first, end)]
                incoming = [
                    dict(message(i, 20), _archive_ordinal=i) for i in range(page_first, page_end)
                ]
                page = {
                    "messages": incoming,
                    "history_window": {
                        "start": page_first,
                        "end": page_end,
                        "total": 1000,
                    },
                }
                with terminal(initial_messages=current) as (view, _):
                    tab = view.tabs[0]
                    tab.update(
                        conversation_id="@chatgpt/default/chat",
                        history_window={"start": first, "end": end, "total": 1000},
                    )
                    view.archive_service = lambda: SimpleNamespace(
                        conversation_window=Mock(return_value=page)
                    )
                    jobs = []
                    view._read_in_background = lambda read, ready, failed, **kw: jobs.append(
                        (read, ready)
                    )
                    self.assertTrue(view.history_pager.request(direction))
                    read, ready = jobs.pop()
                    ready(read())

                    ordinals = [m["_archive_ordinal"] for m in view.messages]
                    self.assertEqual(len(ordinals), MAX_LOADED_MESSAGES)
                    self.assertEqual(ordinals, list(range(kept_first, kept_end)))
                    self.assertEqual(
                        tab["history_window"],
                        {"start": kept_first, "end": kept_end, "total": 1000},
                    )

    def test_direction_reversal_keeps_new_prefix_and_the_latest_viewport(self):
        from types import SimpleNamespace

        from tests.tui_support import terminal

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
        from tests.account_fixture import configured_application
        from tests.tui_support import immediate, terminal
        from tui.paging import HistoryPager

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
                self.assertEqual([m["id"] for m in view.messages], [str(i) for i in range(80)])
                self.assertEqual(
                    view.tabs[0]["history_window"], {"start": 0, "end": 80, "total": 400}
                )
                self.assertTrue(view.history_pager.request("bottom"))
                self.assertEqual(
                    [m["id"] for m in view.messages], [str(i) for i in range(320, 400)]
                )
                view._reads.close()
                view._render_reads.close()
                view._operations.close()

    def test_late_history_page_preserves_the_viewport_after_fast_user_scroll(self):
        from tests.account_fixture import configured_application
        from tests.tui_support import terminal

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
        from tests.tui_support import terminal

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
