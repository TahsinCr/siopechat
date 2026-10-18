"""Exercise real terminal paints instead of timing only resume_conversation."""

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from bootstrap import create_application
from cli.markdown import MessageRenderer, index_transcript
from tests.test_cli_interaction import terminal


class TranscriptPaintTests(unittest.IsolatedAsyncioTestCase):
    def test_row_index_coalesces_styles_and_preserves_message_identity(self):
        rows = index_transcript([("s", "a"), ("s", "b\n"), ("t", "c")], [(0, 1, 0), (1, 2, 1)])
        self.assertEqual(rows[0][0], (("s", "ab"),))
        self.assertEqual(rows[1][0], (("t", "c"),))
        self.assertEqual([row[1] for row in rows], [0, 1])

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
                        async with asyncio.timeout(8):
                            while (
                                not all(tab["messages"] for tab in view.tabs)
                                or view.transcript_line_count < 100
                            ):
                                await asyncio.sleep(0.02)
                        self.assertFalse(
                            violations, "Rich Markdown preparation ran on the terminal loop"
                        )
                        self.assertEqual(len(view.tabs), 4)
                    finally:
                        view.app.exit()
                        await task
            finally:
                await app.runtime.aclose()
