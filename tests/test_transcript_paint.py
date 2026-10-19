"""Exercise real terminal paints instead of timing only resume_conversation."""

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from bootstrap import create_application
from cli.markdown import MessageRenderer, index_transcript, open_link, transcript_fragments
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
            with patch("cli.markdown.subprocess.Popen") as launch:
                for y, (spans, _) in enumerate(control.rows):
                    x = 0
                    for span in spans:
                        if len(span) > 2:
                            event = MouseEvent(
                                Point(x, y), MouseEventType.MOUSE_UP, MouseButton.LEFT, frozenset()
                            )
                            control.mouse_handler(event)
                            self.assertEqual(launch.call_args.args[0][-1], url)
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

    def test_row_index_coalesces_styles_and_preserves_message_identity(self):
        rows = index_transcript([("s", "a"), ("s", "b\n"), ("t", "c")], [(0, 1, 0), (1, 2, 1)])
        self.assertEqual(rows[0][0], (("s", "ab"),))
        self.assertEqual(rows[1][0], (("t", "c"),))
        self.assertEqual([row[1] for row in rows], [0, 1])

    def test_oversized_transcript_is_retained_instead_of_repeating_placeholder(self):
        with terminal() as (view, _):
            tab = view.tabs[view.active_tab]
            text = "x" * 4_000_001
            messages = [{"role": "user", "text": "large"}]
            view.remember_transcript(tab, messages, 80, {}, True, text, [(0, 1, 0)], [("", text)])
            self.assertEqual(view._prepared_transcripts[tab["tab_id"]]["text"], text)

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
                    patch.object(view, "_read_in_background"),
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
