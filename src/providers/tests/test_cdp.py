"""Behavioral checks for local Chrome DevTools boundaries and event flow."""

from __future__ import annotations

import json
import unittest
from collections import deque
from unittest.mock import patch

from providers import cdp


class FakeSocket:
    def __init__(self, messages):
        self.messages = deque(messages)
        self.timeouts = []
        self.sent = []
        self.closed = False

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def send(self, message):
        self.sent.append(json.loads(message))

    def recv(self):
        return self.messages.popleft()

    def close(self):
        self.closed = True


def event(method, params):
    return json.dumps({"method": method, "params": params})


class DevToolsBoundaryTests(unittest.TestCase):
    def test_debugger_http_address_accepts_loopback_and_rejects_remote_hosts(self):
        self.assertEqual(cdp._local_http_base("http://127.0.0.1:9222/"), "http://127.0.0.1:9222")
        self.assertEqual(cdp._local_http_base("http://[::1]:9222"), "http://[::1]:9222")
        for address in (
            "https://127.0.0.1:9222",
            "http://192.168.1.2:9222",
            "http://user@localhost:9222",
            "http://localhost:9222/json/list",
        ):
            with self.subTest(address=address), self.assertRaises(cdp.DevToolsError):
                cdp._local_http_base(address)

    def test_provider_page_matching_accepts_conversation_paths_only_on_supported_origins(self):
        self.assertEqual(cdp._page_origin("https://chatgpt.com/c/123"), "https://chatgpt.com")
        self.assertIsNone(cdp._page_origin("https://example.com/"))
        with self.assertRaises(cdp.DevToolsError):
            cdp._provider_origin("https://chatgpt.com/c/123")

    def test_command_wait_preserves_interleaved_cdp_events(self):
        socket = FakeSocket(
            [
                event("Page.loadEventFired", {"timestamp": 1}),
                json.dumps({"id": 1, "result": {"targetId": "target"}}),
            ]
        )
        connection = cdp.DevToolsConnection.__new__(cdp.DevToolsConnection)
        connection._socket = socket
        connection._timeout = 1.0
        connection._next_id = 0
        connection._events = deque()
        connection._event_bytes = 0

        result = connection.call("Target.createTarget", {"url": "https://chatgpt.com/"})

        self.assertEqual(result, {"targetId": "target"})
        self.assertEqual(connection.next_event(0.1)["method"], "Page.loadEventFired")
        self.assertLessEqual(connection._event_bytes, cdp._MAX_EVENT_QUEUE_BYTES)

    def test_command_fails_instead_of_silently_dropping_events_when_queue_is_full(self):
        socket = FakeSocket(
            [
                event("Network.requestWillBeSent", {"requestId": "first"}),
                event("Network.responseReceived", {"requestId": "second"}),
                json.dumps({"id": 1, "result": {}}),
            ]
        )
        connection = cdp.DevToolsConnection.__new__(cdp.DevToolsConnection)
        connection._socket = socket
        connection._timeout = 1.0
        connection._next_id = 0
        connection._events = deque()
        connection._event_bytes = 0

        with (
            patch.object(cdp, "_MAX_EVENT_QUEUE_ITEMS", 1),
            self.assertRaisesRegex(cdp.DevToolsError, "event buffer"),
        ):
            connection.call("Network.enable")

        self.assertEqual(connection.next_event(0.1)["method"], "Network.requestWillBeSent")

    def test_websocket_message_size_is_checked_before_json_parsing(self):
        connection = cdp.DevToolsConnection.__new__(cdp.DevToolsConnection)
        connection._socket = FakeSocket(["x" * (cdp._MAX_MESSAGE_BYTES + 1)])

        with self.assertRaisesRegex(cdp.DevToolsError, "size limit"):
            connection._receive(0.1)

    def test_chatgpt_auth_capture_correlates_events_and_rejects_failed_responses(self):
        request = {
            "requestId": "ok",
            "request": {
                "method": "GET",
                "url": "https://chatgpt.com/backend-api/me",
                "headers": {"accept": "application/json"},
            },
        }
        extra = {"requestId": "ok", "headers": {"authorization": "Bearer session"}}
        failed_request = {
            "requestId": "failed",
            "request": {
                "method": "GET",
                "url": "https://chatgpt.com/backend-api/me",
                "headers": {},
            },
        }
        events = iter(
            [
                {"method": "Network.requestWillBeSent", "params": failed_request},
                {
                    "method": "Network.responseReceived",
                    "params": {"requestId": "failed", "response": {"status": 401}},
                },
                {"method": "Network.requestWillBeSent", "params": request},
                {"method": "Network.requestWillBeSentExtraInfo", "params": extra},
                {
                    "method": "Network.responseReceived",
                    "params": {"requestId": "ok", "response": {"status": 200}},
                },
            ]
        )

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def call(self, *_args, **_kwargs):
                return {}

            def next_event(self, _timeout):
                return next(events)

        page = {"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/1"}
        with (
            patch.object(cdp, "_page_for_origin", return_value=page),
            patch.object(cdp, "DevToolsConnection", return_value=Connection()),
        ):
            url, headers = cdp.capture_chatgpt_auth("http://127.0.0.1:9222", timeout=1)

        self.assertEqual(url, "https://chatgpt.com/backend-api/me")
        self.assertEqual(headers["authorization"], "Bearer session")
        self.assertEqual(headers["accept"], "application/json")

    def test_devtools_health_check_requires_a_local_debugger_socket(self):
        with patch.object(
            cdp,
            "_get_json",
            return_value={"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/1"},
        ):
            self.assertTrue(cdp.is_devtools_available("http://127.0.0.1:9222"))
        with patch.object(
            cdp,
            "_get_json",
            return_value={"webSocketDebuggerUrl": "ws://example.com/devtools/browser/1"},
        ):
            self.assertFalse(cdp.is_devtools_available("http://127.0.0.1:9222"))


if __name__ == "__main__":
    unittest.main()
