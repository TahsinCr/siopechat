"""Transcript render reuse and tab-lifetime regressions."""

import gc
import unittest
import weakref
from unittest.mock import Mock, patch

from cli.history import prepare_virtual_transcript
from tests.history_support import message


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
            pane.render()
            self.assertTrue(pane.renderer.cache)
            view.close_tab()
            self.assertFalse(pane.messages)
            self.assertFalse(pane.renderer.cache)
            self.assertNotIn(key, view.layout_controller._panes)
