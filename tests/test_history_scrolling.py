"""Asynchronous history loading while users scroll rapidly."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cli.paging import visible_window
from extensions.archive.repository import Files, SQLiteRepository
from extensions.archive.sync import Archive
from tests.history_support import message


class AsyncPagingUITests(unittest.IsolatedAsyncioTestCase):
    async def test_every_body_line_survives_disjoint_sql_pages_in_both_directions(self):
        import asyncio
        import re
        from types import SimpleNamespace

        from prompt_toolkit.data_structures import Size
        from prompt_toolkit.output import DummyOutput

        from tests.cli_support import terminal

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

        from tests.cli_support import terminal

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
        from tests.cli_support import terminal

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
                            self.assertEqual(
                                [message["id"] for message in view.messages],
                                [str(index) for index in range(240, 400)],
                            )
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
