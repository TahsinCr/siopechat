import unittest
from types import SimpleNamespace

from tui.chat.stream import _SendStream


class ResponseCompleteUiTests(unittest.TestCase):
    def test_completion_notice_keeps_request_busy_until_confirmation(self):
        tab = {"busy": True}
        view = SimpleNamespace(
            tabs=[tab],
            active_tab=0,
            app=SimpleNamespace(),
            tr=lambda value: value,
            scroll_bottom=lambda **_kwargs: None,
            _dispatch_ui=lambda callback: callback(),
        )
        messages = [{"role": "assistant", "text": "answer"}]
        stream = _SendStream(view, tab, messages)

        stream.on_event({"type": "response_complete"})

        self.assertEqual(view.status, "Response text complete; confirming…")
        self.assertTrue(tab["busy"])
        self.assertTrue(tab["stream_received"])
