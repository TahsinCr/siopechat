"""Exercise real terminal paints instead of timing only resume_conversation."""

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from cli.markdown import MessageRenderer, index_transcript, open_link, transcript_fragments
from tests.account_fixture import configured_application as create_application
from tests.test_cli_interaction import terminal


class TranscriptPaintTests(unittest.IsolatedAsyncioTestCase):
    def test_markdown_link_click_opens_original_url_after_row_indexing(self):
        from prompt_toolkit.data_structures import Point
        from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

        url = "https://example.com/docs?a=1&b=2"
        with terminal(
            initial_messages=[
                {
                    "role": "assistant",
                    "text": f"説明 [Read the documentation here]({url}) afterwards.",
                }
            ]
        ) as (view, _):
            control = view.transcript_control
            control.create_content(40, 20)
            clicked = 0
            with patch("cli.markdown.open_link") as launch:
                for y, (spans, _) in enumerate(control.rows):
                    x = 0
                    for span in spans:
                        if len(span) > 2:
                            event = MouseEvent(
                                Point(x, y), MouseEventType.MOUSE_UP, MouseButton.LEFT, frozenset()
                            )
                            control.mouse_handler(event)
                            self.assertEqual(launch.call_args.args[0], url)
                            clicked += 1
                        x += len(span[1])
                self.assertGreater(clicked, 0)
                self.assertFalse(view.message_selection)

    def test_link_targets_survive_wrapping_and_background_preparation(self):
        from cli.history import prepare_transcript

        text = (
            "["
            + "A long link label " * 12
            + "](https://example.com/one) and [two](https://example.com/two)"
        )
        _, _, fragments, rows = prepare_transcript(
            [{"role": "assistant", "text": text}], 22, {}, True
        )
        linked_rows = [row for row, _ in rows if any(len(span) > 2 for span in row)]
        self.assertGreater(len(linked_rows), 1)
        self.assertTrue(any(len(span) > 2 for span in fragments))
        # Both OSC-8 terminators and style transitions preserve the target.
        spans = transcript_fragments(
            "\x1b]8;;https://example.com\x07\x1b[31mred\x1b[0m\x1b]8;;\x07 plain"
        )
        self.assertEqual("".join(span[1] for span in spans if len(span) > 2), "red")

    def test_link_opener_rejects_non_web_and_control_character_targets(self):
        with patch("cli.markdown.subprocess.Popen") as launch:
            for url in (
                "javascript:alert(1)",
                "file:///tmp/local",
                "https://example.com/\nunsafe",
                "https://[broken",
            ):
                open_link(url)
            launch.assert_not_called()
        with patch("cli.markdown.subprocess.Popen", side_effect=OSError):
            open_link("https://example.com")

    async def test_terminal_mouse_event_reaches_rendered_markdown_link(self):
        from tests.test_terminal_responsiveness import ResizableOutput

        output = ResizableOutput()
        with terminal(
            output=output,
            initial_messages=[
                {"role": "assistant", "text": "Click [Example](https://example.com/target) here."}
            ],
        ) as (view, pipe):
            run = asyncio.create_task(view.app.run_async())
            try:
                for _ in range(100):
                    screen = view.app.renderer._last_screen
                    if screen:
                        hit = next(
                            (
                                (x, y)
                                for y in range(output.rows)
                                for x in range(output.columns - 7)
                                if "".join(screen.data_buffer[y][x + i].char for i in range(7))
                                == "Example"
                            ),
                            None,
                        )
                        if hit:
                            break
                    await asyncio.sleep(0.01)
                else:
                    self.fail("Link did not render")
                x, y = hit
                with patch("cli.markdown.open_link") as launch:
                    pipe.send_bytes(f"\x1b[<0;{x + 1};{y + 1}M\x1b[<0;{x + 1};{y + 1}m".encode())
                    for _ in range(100):
                        if launch.called:
                            break
                        await asyncio.sleep(0.01)
                    launch.assert_called_once()
                    self.assertEqual(launch.call_args.args[0], "https://example.com/target")
            finally:
                view.app.exit()
                await run

    def test_native_browser_opener_ignores_stale_browser_environment(self):
        with (
            patch.dict("os.environ", {"BROWSER": "missing-firefox"}),
            patch("cli.markdown.sys.platform", "linux"),
            patch("cli.markdown.shutil.which", return_value="/usr/bin/xdg-open"),
            patch("cli.markdown.subprocess.Popen") as launch,
        ):
            open_link("https://example.com")
            self.assertEqual(launch.call_args.args[0], ["xdg-open", "https://example.com"])
            self.assertNotIn("BROWSER", launch.call_args.kwargs["env"])
        with (
            patch("cli.markdown.sys.platform", "darwin"),
            patch("cli.markdown.subprocess.Popen") as launch,
        ):
            open_link("https://example.com")
            self.assertEqual(launch.call_args.args[0], ["/usr/bin/open", "https://example.com"])
        with (
            patch("cli.markdown.sys.platform", "win32"),
            patch("cli.markdown.os.startfile", create=True) as launch,
        ):
            open_link("https://example.com")
            launch.assert_called_once_with("https://example.com")

    def test_row_index_coalesces_styles_and_preserves_message_identity(self):
        rows = index_transcript([("s", "a"), ("s", "b\n"), ("t", "c")], [(0, 1, 0), (1, 2, 1)])
        self.assertEqual(rows[0][0], (("s", "ab"),))
        self.assertEqual(rows[1][0], (("t", "c"),))
        self.assertEqual([row[1] for row in rows], [0, 1])

    def test_row_window_matches_full_index_without_retaining_other_rows(self):
        from cli.markdown import index_transcript_window

        ansi = [("style", "first\nsecond\n"), ("link", "third\nfourth\n", object())]
        ranges = [(0, 2, 7), (2, 4, 8)]
        full = index_transcript(ansi, ranges)
        self.assertEqual(index_transcript_window(ansi, ranges, 1, 3), full[1:3])

    def test_oversized_transcript_is_not_retained_in_formatted_cache(self):
        with terminal() as (view, _):
            tab = view.tabs[view.active_tab]
            text = "x" * 4_000_001
            messages = [{"role": "user", "text": "large"}]
            view.remember_transcript(tab, messages, 80, {}, True, text, [(0, 1, 0)], [("", text)])
            self.assertNotIn(tab["tab_id"], view._prepared_transcripts)

    def test_closing_tab_discards_formatted_rows_but_keeps_no_archive_mutation(self):
        with terminal(initial_messages=[{"role": "user", "text": "Retained source"}]) as (
            view,
            _,
        ):
            tab = view.tabs[view.active_tab]
            tab_id = tab["tab_id"]
            view.renderer.render("user", "Retained source", 40, False, False)
            view._prepared_transcripts = {tab_id: {"cost": 1}}
            view._transcript_cache = "formatted snapshot"
            view.close_tab()
            self.assertNotIn(tab_id, view._prepared_transcripts)
            self.assertFalse(view.renderer.cache)
            self.assertEqual(view._transcript_cache, "")

    def test_virtual_rows_match_complete_transcript_and_cache_only_prefetched_rows(self):
        from cli.history import prepare_transcript
        from cli.transcript import VirtualTranscript, VirtualTranscriptRows

        messages = [
            {"role": "user", "text": "Question"},
            {"role": "reasoning", "text": "Brief thought"},
            {"role": "assistant", "text": "Answer with **formatting**\n\nSecond paragraph"},
        ]
        _, _, _, expected = prepare_transcript(messages, 48, {}, True)
        virtual = VirtualTranscript(messages, 48, True, renderer=MessageRenderer())
        rows = VirtualTranscriptRows(virtual)
        virtual.prefetch(0, len(rows))
        self.assertEqual(tuple(rows), tuple(expected))
        self.assertLessEqual(virtual.cache_bytes, virtual.CACHE_BYTES)
        self.assertGreater(virtual.cache_bytes, 0)

    def test_virtual_message_snapshot_reuses_source_text_without_copying_it(self):
        from cli.transcript import VirtualTranscript

        source = "A long answer."
        virtual = VirtualTranscript(
            [{"role": "assistant", "text": source}], 48, renderer=MessageRenderer()
        )
        self.assertEqual(virtual.messages, (("assistant", source),))
        self.assertIs(virtual.messages[0][1], source)

    def test_virtual_large_message_indexes_only_the_requested_page(self):
        from cli.transcript import VirtualTranscript

        message = {"role": "assistant", "text": "\n".join(f"Line {i}" for i in range(300))}
        with (
            patch.object(VirtualTranscript, "CACHE_BYTES", 1),
            patch("cli.transcript.index_transcript", side_effect=AssertionError("full index")),
        ):
            virtual = VirtualTranscript([message], 48, renderer=MessageRenderer())
            rows = [virtual.row(i) for i in range(32)]
        self.assertTrue(any(row[0] for row in rows))

    def test_large_code_block_skips_expensive_syntax_highlighting(self):
        from rich.syntax import Syntax as RichSyntax

        large = "```python\n" + "value = 1\n" * 8000 + "```"
        with (
            patch("cli.markdown.Syntax", side_effect=AssertionError("large block highlighted")),
            patch("cli.markdown.ChatMarkdown", side_effect=AssertionError("large block parsed")),
        ):
            rendered = MessageRenderer().render("assistant", large, 80, False, False)
        self.assertIn("value = 1", rendered)

        small = "```python\nprint(1)\n```"
        with patch("cli.markdown.Syntax", wraps=RichSyntax) as syntax:
            MessageRenderer().render("assistant", small, 80, False, False)
        syntax.assert_called_once()

    def test_virtual_large_code_message_indexes_and_renders_only_requested_rows(self):
        from cli.transcript import VirtualTranscript

        text = "```text\n" + "\n".join(f"row-{index:05}" for index in range(8000)) + "\n```"
        renderer = MessageRenderer()
        with patch.object(renderer, "render", side_effect=AssertionError("full message render")):
            virtual = VirtualTranscript(
                [{"role": "assistant", "text": text}], 80, renderer=renderer
            )
            row, message_index = virtual.row(128 * 20)

        self.assertEqual(message_index, 0)
        self.assertIn("row-02560", "".join(span[1] for span in row))
        self.assertLessEqual(virtual.cache_bytes, virtual.CACHE_BYTES)
        self.assertEqual(len(virtual._pages), 1)
        virtual.dispose()
        self.assertEqual(virtual.messages, ())

    def test_large_code_window_checkpoints_preserve_wrapped_rows(self):
        from cli.markdown import LargeCodeBlock

        source = "abcdefghij " * 40
        block = LargeCodeBlock(source, (0, len(source)), 12, 3)
        expected = block._wrapped(source)
        actual = tuple(
            row
            for page in range((block.line_count + 2) // 3)
            for row in block.rows(page * 3, min(block.line_count, (page + 1) * 3))
        )
        self.assertEqual([row.plain for row in actual], [row.plain for row in expected])

    def test_virtual_message_key_does_not_scan_the_full_history(self):
        class NonIteratingList(list):
            def __iter__(self):
                raise AssertionError("message-key calculation must stay constant-time")

        with terminal() as (view, _):
            messages = NonIteratingList(
                [{"role": "user", "text": "Question"}, {"role": "assistant", "text": "Answer"}]
            )
            self.assertEqual(
                view._virtual_message_key(messages),
                (
                    id(messages),
                    2,
                    id(messages[0]),
                    id(messages[-1]),
                    id(messages[-1]["text"]),
                ),
            )

    def test_virtual_prefetch_keeps_scrolled_rows_in_bounded_lru(self):
        from cli.transcript import VirtualTranscript

        message = {
            "role": "assistant",
            "text": "\n".join(f"Row {i}  " for i in range(1200)),
        }
        with patch("cli.transcript.index_transcript", wraps=index_transcript) as index_rows:
            virtual = VirtualTranscript([message], 48, renderer=MessageRenderer())
            virtual.prefetch(0, 64)
            first_pages = set(virtual._pages)
            self.assertTrue(first_pages)
            virtual.prefetch(600, 664)
            self.assertEqual(index_rows.call_count, 1)
        self.assertTrue(virtual._pages)
        self.assertIn((0, 0), virtual._pages)
        self.assertNotEqual(set(virtual._pages), set())
        self.assertTrue(first_pages.issubset(virtual._pages))
        self.assertLessEqual(virtual.cache_bytes, virtual.CACHE_BYTES)

    def test_selection_tracks_the_same_message_after_older_history_is_inserted(self):
        with terminal(
            initial_messages=[{"role": "user", "text": "Q"}, {"role": "assistant", "text": "A"}]
        ) as (view, _):
            view.select_message(1)
            view.messages.insert(0, {"role": "user", "text": "Earlier"})
            view.transcript_rows()
            self.assertEqual(view.selected_message, 2)

    async def test_four_large_chats_paint_without_rendering_markdown_on_ui_thread(self):
        with tempfile.TemporaryDirectory() as directory:
            app = create_application(Path(directory))
            archive = app.get("archive")
            body = (
                "Some **formatted text** and a [source](https://example.com).\n\n```python\nprint(1)\n```\n\n"
            ) * 8
            for number in range(4):
                archive.put(
                    "chatgpt/default",
                    "conversation",
                    f"c{number}",
                    {
                        "title": str(number),
                        "messages": [
                            {
                                "id": f"{number}-{i}",
                                "role": "user" if i % 2 == 0 else "assistant",
                                "text": f"Message {i}\n{body}",
                            }
                            for i in range(100)
                        ],
                    },
                )
            ui_thread = threading.get_ident()
            render = MessageRenderer.render
            violations = []

            def checked(renderer, *args, **kwargs):
                if threading.get_ident() == ui_thread:
                    violations.append(True)
                return render(renderer, *args, **kwargs)

            try:
                with (
                    terminal(account_service=app.get("accounts")) as (view, _),
                    patch.object(view, "refresh_provider_catalog"),
                    patch.object(MessageRenderer, "render", checked),
                ):
                    task = asyncio.create_task(view.app.run_async())
                    await asyncio.sleep(0.03)
                    try:
                        for number in range(4):
                            if number:
                                view.new_tab()
                            view.resume_conversation(f"@chatgpt/default/c{number}")
                            await asyncio.sleep(0.1)

                        async def loaded():
                            while (
                                not all(tab["messages"] for tab in view.tabs)
                                or view.transcript_line_count < 100
                            ):
                                await asyncio.sleep(0.02)

                        await asyncio.wait_for(loaded(), 8)
                        self.assertFalse(
                            violations, "Rich Markdown preparation ran on the terminal loop"
                        )
                        self.assertEqual(len(view.tabs), 4)
                    finally:
                        view.app.exit()
                        await task
            finally:
                await app.runtime.aclose()
