import unittest
from types import SimpleNamespace

from tui.chat.stream import _SendStream
from tui.chat.transcript import TranscriptComponent


class ResponseCompleteUiTests(unittest.TestCase):
    def test_completion_notice_keeps_request_busy_until_confirmation(self):
        tab = {"busy": True}
        view = SimpleNamespace(
            tabs=[tab],
            active_tab=0,
            app=SimpleNamespace(invalidate=lambda: None),
            tr=lambda value: value,
            scroll_bottom=lambda **_kwargs: None,
            _dispatch_ui=lambda callback: callback(),
        )
        messages = [{"role": "assistant", "text": "answer"}]
        stream = _SendStream(view, tab, messages)

        stream.on_event({"type": "response_complete"})

        self.assertEqual(view.status, "Response complete")
        self.assertTrue(tab["generation_complete"])
        self.assertTrue(tab["completed_pending"])
        self.assertTrue(tab["busy"])
        self.assertTrue(tab["stream_received"])

    def test_completed_response_freezes_elapsed_status_while_request_is_busy(self):
        tab = {
            "tab_id": "active",
            "busy": True,
            "generation_complete": True,
            "generation_elapsed": 4.8,
        }
        view = SimpleNamespace(
            active_busy=lambda: True,
            stop_confirmation_tab_id=None,
            tabs=[tab],
            active_tab=0,
            busy=True,
            busy_tab_index=0,
            tr=lambda value: value,
            format_elapsed=TranscriptComponent.format_elapsed,
        )

        activity = TranscriptComponent.render_activity(view)

        self.assertEqual(activity[0][1], " — Worked for 4s ")
