"""Content-free timing statistics for the ChatGPT send path."""

import asyncio
import threading
import unittest

from webaiprovider.core.timing import SendTiming, current_send_timing, use_send_timing

from tui.chat.sending import collect_send_timing, send_timing_report


class SendTimingTests(unittest.TestCase):
    def tearDown(self):
        send_timing_report()

    def test_report_uses_median_and_nearest_rank_p95_without_request_data(self):
        for value in (1.0, 2.0, 3.0, 4.0, 100.0):
            timing = SendTiming()
            timing.record("button_to_first_ui_frame_ms", int(value * 1_000_000))
            timing.increment("pre_send_conversation_cache_hit")
            collect_send_timing(timing)

        report = send_timing_report()

        self.assertIn("button_to_first_ui_frame_ms | 5 | 3.000 | 100.000", report)
        self.assertIn("pre_send_conversation_cache_hit | 5", report)
        self.assertNotIn("session", report.lower())
        self.assertNotIn("token", report.lower())
        self.assertNotIn("prompt", report.lower())

    def test_context_survives_worker_thread_submission(self):
        timing = SendTiming("chatgpt")
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()

        async def worker_value():
            return await asyncio.to_thread(current_send_timing)

        try:
            with use_send_timing(timing):
                future = asyncio.run_coroutine_threadsafe(worker_value(), loop)
                self.assertIs(future.result(timeout=2), timing)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=2)
            loop.close()


if __name__ == "__main__":
    unittest.main()
