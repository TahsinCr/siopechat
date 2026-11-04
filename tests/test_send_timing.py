"""Content-free timing statistics for the ChatGPT send path."""

import asyncio
import threading
import unittest

from providers.timing import (
    SendTiming,
    collect_send_timing,
    current_send_timing,
    send_timing_report,
    use_send_timing,
)


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

    def test_context_survives_provider_loop_submission_and_worker_thread(self):
        timing = SendTiming()
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
