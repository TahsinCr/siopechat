"""Attachment preflight and provider policy boundaries."""

import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from cli.attachments import AttachmentPicker
from providers.attachments import policy_for
from providers.chatgpt.client import ChatGPTClient
from providers.chatgpt.errors import ChatGPTWebError


class AttachmentPolicyTests(unittest.TestCase):
    def test_provider_limits_are_runtime_owned_and_distinct(self):
        self.assertEqual(policy_for("gemini").max_files, 10)
        self.assertEqual(policy_for("gemini").max_video_bytes, 2 * 1024 * 1024 * 1024)
        self.assertEqual(policy_for("claude").max_files, 20)
        self.assertEqual(policy_for("claude").max_files_scope, "conversation")
        self.assertEqual(policy_for("chatgpt").max_bytes, 512 * 1024 * 1024)
        self.assertEqual(policy_for("chatgpt").max_image_bytes, 20 * 1024 * 1024)
        self.assertEqual(policy_for("chatgpt").max_spreadsheet_bytes, 50 * 1024 * 1024)
        self.assertIsNone(policy_for("unknown"))

    def test_preflight_keeps_earliest_eligible_files_and_trims_limits(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first, second, third, unsupported = (
                root / "a.txt",
                root / "b.txt",
                root / "c.txt",
                root / "d.bin",
            )
            first.write_bytes(b"1234")
            second.write_bytes(b"12345")
            third.write_bytes(b"x")
            unsupported.write_bytes(b"x")
            view = SimpleNamespace(
                pending_attachments=[],
                tr=lambda value: value,
            )
            picker = AttachmentPicker(view)
            accepted, rejected = picker._prepare(
                "\n".join(map(str, (first, second, third, unsupported))),
                {
                    "max_files": 2,
                    "max_bytes": 5,
                    "max_total_bytes": 5,
                    "allowed_extensions": (".txt",),
                },
            )
            self.assertEqual([item["name"] for item in accepted], ["a.txt"])
            reasons = " ".join(reason for _, reason in rejected)
            self.assertIn("toplam boyut sınırı", reasons)
            self.assertIn("biçimi desteklemiyor", reasons)

    def test_native_file_chooser_is_scheduled_off_the_ui_callback(self):
        callbacks = {}
        editor = SimpleNamespace(text="/already/selected.txt")
        view = SimpleNamespace(
            forms=SimpleNamespace(fields={"files": editor}),
            tr=lambda value: value,
            app=SimpleNamespace(invalidate=Mock()),
            _read_in_background=lambda read, success, failure, **kwargs: callbacks.update(
                read=read, success=success, failure=failure
            ),
        )
        picker = AttachmentPicker(view)

        with patch("cli.file_selection.choose_files", return_value=["/tmp/new.txt"]) as choose:
            picker._choose_files_into((".txt",))
            choose.assert_not_called()
            self.assertEqual(editor.text, "/already/selected.txt")
            selected = callbacks["read"]()
            callbacks["success"](selected)
            choose.assert_called_once_with("Dosya seç", (".txt",), multiple=True)
        self.assertEqual(editor.text, "/already/selected.txt\n/tmp/new.txt")

    def test_video_uses_its_provider_specific_ceiling(self):
        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / "clip.mp4"
            video.write_bytes(b"video")
            picker = AttachmentPicker(SimpleNamespace(pending_attachments=[], tr=lambda x: x))
            accepted, rejected = picker._prepare(
                str(video), {"max_files": 10, "max_bytes": 1, "max_video_bytes": 10}
            )
            self.assertEqual(len(accepted), 1)
            self.assertEqual(rejected, [])

    def test_count_limit_keeps_earlier_choices_and_drops_the_tail(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = [Path(folder) / f"{index}.txt" for index in range(3)]
            for path in paths:
                path.write_text(path.stem, encoding="utf-8")
            view = SimpleNamespace(pending_attachments=[], tr=lambda value: value)
            accepted, rejected = AttachmentPicker(view)._prepare(
                "\n".join(map(str, paths)), {"max_files": 2, "max_bytes": 100}
            )
        self.assertEqual([item["name"] for item in accepted], ["0.txt", "1.txt"])
        self.assertEqual(rejected, [("2.txt", "dosya sayısı sınırı · sondaki dosya çıkarıldı")])

    def test_chatgpt_preflight_applies_image_and_spreadsheet_caps(self):
        with tempfile.TemporaryDirectory() as folder:
            image = Path(folder) / "large.png"
            table = Path(folder) / "large.csv"
            image.touch()
            table.touch()
            with image.open("wb") as stream:
                stream.truncate(20 * 1024 * 1024 + 1)
            with table.open("wb") as stream:
                stream.truncate(50 * 1024 * 1024 + 1)
            view = SimpleNamespace(pending_attachments=[], tr=lambda value: value)
            accepted, rejected = AttachmentPicker(view)._prepare(
                f"{image}\n{table}", policy_for("chatgpt").as_dict()
            )
        self.assertEqual(accepted, [])
        self.assertEqual([name for name, _ in rejected], ["large.png", "large.csv"])

    def test_existing_queue_paths_are_not_uploaded_twice(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "keep.txt"
            path.write_text("content", encoding="utf-8")
            view = SimpleNamespace(
                pending_attachments=[{"path": str(path), "name": "keep.txt", "size_bytes": 7}],
                tr=lambda value: value,
            )
            accepted, rejected = AttachmentPicker(view)._prepare(
                str(path), {"max_files": 10, "max_bytes": 100}
            )
        self.assertEqual(accepted, [])
        self.assertEqual(rejected, [])

    def test_foreign_ineligible_queue_items_do_not_block_compatible_additions(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "new.txt"
            path.write_text("content", encoding="utf-8")
            view = SimpleNamespace(
                pending_attachments=[
                    {"path": "/tmp/other.bin", "name": "other.bin", "size_bytes": 5}
                ],
                tr=lambda value: value,
            )
            accepted, rejected = AttachmentPicker(view)._prepare(
                str(path),
                {"max_files": 1, "max_bytes": 100, "allowed_extensions": (".txt",)},
            )
        self.assertEqual([item["name"] for item in accepted], ["new.txt"])
        self.assertEqual(rejected, [])

    def test_attachment_queue_survives_tab_changes_during_picker_work(self):
        from tests import test_cli_interaction as interaction

        with interaction.terminal() as (view, _):
            view.pending_attachments = [
                {"queue_id": "queued", "name": "keep.txt", "source_tab_id": "source"}
            ]
            view.tabs[0]["tab_id"] = "source"
            view.uploading_image = True
            view.new_tab()
            view.switch_tab(-1)
            self.assertEqual([item["name"] for item in view.pending_attachments], ["keep.txt"])
            self.assertEqual(len(view.tabs), 2)

    def test_clear_all_does_not_remove_files_during_send_or_upload(self):
        attachment = {"queue_id": "queued", "name": "keep.txt"}
        for in_flight, uploading in ((True, False), (False, True)):
            view = SimpleNamespace(
                pending_attachments=[attachment],
                _attachments_in_flight=in_flight,
                uploading_image=uploading,
                status="",
                tr=lambda value: value,
                app=SimpleNamespace(invalidate=Mock()),
                _release_staged_attachments=Mock(),
            )
            AttachmentPicker(view)._clear_pending({})
            self.assertEqual(view.pending_attachments, [attachment])
            view._release_staged_attachments.assert_not_called()
            view.app.invalidate.assert_called_once()

    def test_send_from_another_tab_consumes_the_shared_attachment_queue_on_success(self):
        from tests import test_cli_interaction as interaction

        with interaction.terminal() as (view, _):
            attachment = {
                "queue_id": "queued",
                "name": "keep.txt",
                "source_account_key": "chatgpt/default",
                "source_tab_id": "source",
            }
            view.pending_attachments = [attachment]
            view.tabs[0]["tab_id"] = "source"
            view.new_tab()
            target = view.tabs[1]
            target["busy"] = True
            view.busy = True
            sent = {}
            view._dispatch_ui = lambda callback: callback()

            def send(_conversation, _prompt, attachments, *_args, **_kwargs):
                sent["attachments"] = attachments
                return {
                    "sent_attachment_ids": ["queued"],
                    "conversation": {"title": "Second chat", "messages": []},
                }

            view.run_send(
                "hello",
                [attachment],
                1,
                target["messages"],
                None,
                "model",
                None,
                send_callback=send,
                target_tab=target,
            )
            self.assertEqual(sent["attachments"], [attachment])
            self.assertEqual(view.pending_attachments, [])

    def test_destination_account_reuploads_foreign_attachment_ids(self):
        from accounts import Accounts

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "notes.txt"
            path.write_text("hello", encoding="utf-8")
            service = object.__new__(Accounts)
            service.attachment_policy = lambda _key: {
                "max_files": 10,
                "max_bytes": 100,
            }
            service.attach_file = Mock(return_value={"id": "destination-file", "name": "notes.txt"})
            attachments, omitted = service._prepare_attachments(
                "gemini/other",
                [
                    {
                        "id": "source-file",
                        "queue_id": "queued",
                        "source_account_key": "chatgpt/default",
                        "path": str(path),
                        "name": "notes.txt",
                        "size_bytes": 5,
                    }
                ],
            )

        service.attach_file.assert_called_once_with("gemini/other", str(path))
        self.assertEqual(attachments[0]["id"], "destination-file")
        self.assertEqual(attachments[0]["queue_id"], "queued")
        self.assertEqual(attachments[0]["source_account_key"], "gemini/other")
        self.assertEqual(omitted, [])


class GeminiStreamingUploadTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(
        importlib.util.find_spec("gemini_webapi"),
        "Gemini transport tests require the optional Python 3.11+ SDK",
    )
    async def test_local_files_upload_serially_without_sdk_read_bytes_path(self):
        from providers.gemini.transport import stream_local_files

        with tempfile.TemporaryDirectory() as folder:
            paths = [Path(folder) / "a.txt", Path(folder) / "b.txt"]
            for path in paths:
                path.write_text(path.stem, encoding="utf-8")
            response = SimpleNamespace(text="remote-url", raise_for_status=Mock())
            live = SimpleNamespace(post=AsyncMock(return_value=response))
            captured = {}

            async def generate(self, **kwargs):
                captured.update(kwargs)
                yield "response chunk"

            class SDKClientDouble:
                _generate = generate
                auto_close = False
                push_id = "push"

                def __init__(self):
                    self._live_client = live
                    self._check_account_status = Mock()

                async def _sync_activity(self):
                    return None

            client = SDKClientDouble()
            chat = SimpleNamespace(metadata={"chat": True}, last_output=None)
            result = [
                item
                async for item in stream_local_files(
                    client, chat, "prompt", paths, extended_thinking=True
                )
            ]
            self.assertEqual(result, ["response chunk"])
            self.assertEqual(live.post.await_count, 2)
            self.assertEqual(
                captured["req_file_data"], [[["remote-url"], "a.txt"], [["remote-url"], "b.txt"]]
            )
            self.assertTrue(captured["extended_thinking"])


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
            client.http = SimpleNamespace(
                put=Mock(return_value=SimpleNamespace(raise_for_status=Mock()))
            )
            with patch.object(Path, "read_bytes", side_effect=AssertionError("full file read")):
                result = client.upload_file(path.name, "text/plain", path)
            chunks = client.http.put.call_args.kwargs["content"]
            self.assertEqual(b"".join(chunks), b"a" * (2 * 1024 * 1024 + 7))
            self.assertEqual(result["size_bytes"], path.stat().st_size)

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


if __name__ == "__main__":
    unittest.main()
