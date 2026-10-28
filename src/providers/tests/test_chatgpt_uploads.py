"""Provider upload and attachment-limit contracts."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from providers.attachments import policy_for
from providers.chatgpt.client import ChatGPTClient
from providers.chatgpt.errors import ChatGPTWebError


class AttachmentPolicyContractTests(unittest.TestCase):
    def test_provider_limits_are_runtime_owned_and_distinct(self):
        self.assertEqual(policy_for("gemini").max_files, 10)
        self.assertEqual(policy_for("gemini").max_video_bytes, 2 * 1024 * 1024 * 1024)
        self.assertEqual(policy_for("claude").max_files, 20)
        self.assertEqual(policy_for("claude").max_files_scope, "conversation")
        self.assertEqual(policy_for("chatgpt").max_bytes, 512 * 1024 * 1024)
        self.assertEqual(policy_for("chatgpt").max_image_bytes, 20 * 1024 * 1024)
        self.assertEqual(policy_for("chatgpt").max_spreadsheet_bytes, 50 * 1024 * 1024)
        self.assertIsNone(policy_for("unknown"))


class ChatGPTStreamingUploadTests(unittest.TestCase):
    def test_path_upload_streams_chunks_instead_of_reading_the_whole_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "large.txt"
            path.write_bytes(b"a" * (2 * 1024 * 1024 + 7))
            client = object.__new__(ChatGPTClient)
            client.request = Mock(
                side_effect=[
                    SimpleNamespace(
                        json=lambda: {"file_id": "file-id", "upload_url": "https://upload"}
                    ),
                    SimpleNamespace(json=lambda: {"status": "success"}),
                ]
            )
            uploaded = []

            def put(_url, *, content, **_kwargs):
                uploaded.extend(content)
                return SimpleNamespace(raise_for_status=Mock())

            client.http = SimpleNamespace(put=Mock(side_effect=put))
            with patch.object(Path, "read_bytes", side_effect=AssertionError("full file read")):
                result = client.upload_file(path.name, "text/plain", path)
            self.assertEqual(b"".join(uploaded), b"a" * (2 * 1024 * 1024 + 7))
            self.assertEqual(result["size_bytes"], path.stat().st_size)

    def test_path_upload_cancel_stops_later_chunks_and_skips_finalization(self):
        import threading

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cancel.txt"
            path.write_bytes(b"a" * (512 * 1024))
            cancel_event = threading.Event()
            client = object.__new__(ChatGPTClient)
            client.request = Mock(
                return_value=SimpleNamespace(
                    json=lambda: {"file_id": "file-id", "upload_url": "https://upload"}
                )
            )

            def put(_url, *, content, **_kwargs):
                chunks = iter(content)
                self.assertEqual(len(next(chunks)), 256 * 1024)
                cancel_event.set()
                with self.assertRaisesRegex(ChatGPTWebError, "cancelled"):
                    next(chunks)
                return SimpleNamespace(raise_for_status=Mock())

            client.http = SimpleNamespace(put=Mock(side_effect=put))
            with self.assertRaisesRegex(ChatGPTWebError, "cancelled"):
                client.upload_file(path.name, "text/plain", path, cancel_event=cancel_event)
            self.assertEqual(client.request.call_count, 1)
            client.http.put.assert_called_once()

    def test_chatgpt_client_rejects_files_above_documented_ceiling_before_network(self):
        client = object.__new__(ChatGPTClient)
        client.request = Mock()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "too-large.txt"
            with path.open("wb") as stream:
                stream.truncate(512 * 1024 * 1024 + 1)
            with self.assertRaisesRegex(ChatGPTWebError, "size limit"):
                client.upload_file(path.name, "text/plain", path)
        client.request.assert_not_called()
