"""Native mutation contracts and published metadata without network or host imports."""

import json
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx

from providers import BranchContext, MessageAction, SendRequest
from providers.chatgpt.client import ChatGPTClient
from providers.claude.transport import ClaudeTransport
from providers.gemini.history import history
from providers.gemini.transport import generation_request
from providers.messages import files_from, sources_from
from providers.models import Origin


class ChatGPTActionsTests(unittest.TestCase):
    def test_download_metadata_supports_native_resources_and_ignores_web_page_links(self):
        cases = (
            (
                {"content": {"parts": ["[Report](sandbox:/mnt/data/report.pdf)"]}},
                "sandbox:/mnt/data/report.pdf",
            ),
            (
                {
                    "content": [
                        {
                            "resource": {
                                "id": "http://googleusercontent.com/image_generation_content/0",
                                "name": "image.png",
                                "remote_url": "https://lh3.googleusercontent.com/signed",
                            }
                        }
                    ]
                },
                "http://googleusercontent.com/image_generation_content/0",
            ),
            (
                {
                    "files": [
                        {
                            "file_name": "report.docx",
                            "path": "/mnt/user-data/outputs/report.docx",
                            "size_bytes": 123,
                        }
                    ]
                },
                "/mnt/user-data/outputs/report.docx",
            ),
        )
        for value, target in cases:
            with self.subTest(target=target):
                files = files_from(value)
                self.assertEqual(files[0]["url"], target)
        self.assertEqual(files_from({"text": "[Search result](https://example.com/page)"}), ())
        self.assertEqual(
            files_from(
                {
                    "files": [
                        {"url": "file:///etc/passwd"},
                        {"url": "https://user:secret@example.com/file"},
                    ]
                }
            ),
            (),
        )

    def run_request(self, action, target, *, model="account-model", branch=None):
        sent = []

        def accept(request):
            sent.append(json.loads(request.content))
            event = {
                "conversation_id": "chat",
                "message": {
                    "id": "new-answer",
                    "author": {"role": "assistant"},
                    "channel": "final",
                    "content": {"content_type": "text", "parts": ["New answer"]},
                },
            }
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text="data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n",
            )

        conversation = {
            "conversation_id": "chat",
            "current_node": "answer",
            "messages": [
                {"id": "root", "author": {"role": "system"}},
                {
                    "id": "user",
                    "author": {"role": "user"},
                    "content": {
                        "content_type": "multimodal_text",
                        "parts": [
                            {
                                "content_type": "image_asset_pointer",
                                "asset_pointer": "file-service://file-image",
                            },
                            "Original",
                        ],
                    },
                    "metadata": {"attachments": [{"id": "file-image", "name": "image.png"}]},
                },
                {"id": "answer", "author": {"role": "assistant"}},
            ],
        }
        client = ChatGPTClient.__new__(ChatGPTClient)
        client.headers = {}
        client.create_sentinel_headers = lambda: {}
        with httpx.Client(transport=httpx.MockTransport(accept)) as client.http:
            result = client.send_prompt_direct(
                conversation,
                "Edited",
                [],
                model=model,
                action=action,
                message_id=target,
                branch=branch,
            )
        return sent, result

    def test_retry_reuses_the_user_parent_without_submitting_a_second_user_message(self):
        sent, result = self.run_request(MessageAction.RETRY, "answer")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["action"], "variant")
        self.assertEqual(sent[0]["parent_message_id"], "user")
        self.assertEqual(sent[0]["messages"], [])
        self.assertEqual(sent[0]["model"], "account-model")
        self.assertEqual(result, ("chat", "user"))

    def test_edit_replaces_the_prompt_but_keeps_native_image_and_attachment_references(self):
        sent, result = self.run_request(MessageAction.EDIT, "user")
        body = sent[0]
        self.assertEqual(body["parent_message_id"], "root")
        self.assertEqual(body["messages"][0]["content"]["parts"][-1], "Edited")
        self.assertEqual(
            body["messages"][0]["content"]["parts"][0]["asset_pointer"], "file-service://file-image"
        )
        self.assertEqual(body["messages"][0]["metadata"]["attachments"][0]["id"], "file-image")
        self.assertNotEqual(result[1], "user")

    def test_branch_first_turn_uses_source_identities_without_sending_the_preview_id(self):
        sent, _ = self.run_request(None, None, branch=BranchContext("source", "branch-point"))
        self.assertNotIn("conversation_id", sent[0])
        self.assertEqual(sent[0]["branching_from_conversation_id"], "source")
        self.assertEqual(sent[0]["branching_from_message_id"], "branch-point")
        self.assertEqual(sent[0]["parent_message_id"], "branch-point")

    def test_foreign_or_removed_message_cannot_trigger_a_generation(self):
        with self.assertRaisesRegex(Exception, "no longer editable"):
            self.run_request(MessageAction.EDIT, "other-conversation-message")


class GeminiActionsTests(unittest.TestCase):
    def test_ready_media_resources_survive_a_file_only_history_response(self):
        body = [None] * 8
        body[7] = [
            "https://lh3.googleusercontent.com/thumbnail",
            "https://media.googlevideo.com/video",
        ]
        audio = [None] * 8
        audio[7] = [
            "https://lh3.googleusercontent.com/thumbnail",
            "https://media.googlevideo.com/audio",
        ]
        candidate = [None] * 38
        candidate[0], candidate[1] = "rc_media", [""]
        candidate[12] = [{"60": [[[[body]]]], "87": [[None, audio]]}]
        turn = [[None, "r_media"], None, [["Make media"]], [[candidate]]]
        value = history([[turn]], lambda cid: Origin("gemini", "account", cid), "c_chat")
        answer = value.messages[-1]
        self.assertEqual(answer.content[0].text, "")
        self.assertEqual(
            [part.resource.media_type for part in answer.content[1:]], ["video/mp4", "audio/mpeg"]
        )
        self.assertEqual(answer.content[1].resource.id, "gemini-media:rc_media:video")

    def test_native_edit_and_retry_use_distinct_operation_controls(self):
        session = SimpleNamespace(models=(), language="en", token="fixture", session_id="fixture")
        for action, mode, reason in ((MessageAction.EDIT, 0, 2), (MessageAction.RETRY, 2, 7)):
            request = SendRequest(
                "gemini",
                "account",
                "local",
                "Prompt",
                "c_fixture",
                action=action,
                message_id="r_fixture:user",
            )
            _, data = generation_request(
                session, request, ["c_fixture", "r_parent", "rc_parent"], []
            )
            body = json.loads(json.loads(data["f.req"])[1])
            self.assertEqual(body[2], ["c_fixture", "r_parent", "rc_parent"])
            self.assertEqual((body[18], body[32], body[72]), (mode, 1, reason))

    def test_history_keeps_sources_separate_from_thoughts_and_only_latest_retry_is_offered(self):
        turns = []
        for number in (2, 1):
            candidate = [None] * 38
            candidate[0], candidate[1], candidate[37] = (
                f"rc_{number}",
                ["Answer"],
                [["Published thought"]],
            )
            rich = [None] * 44
            entry = [None, None, None, [[None, "https://example.com/source", "Source"]]]
            rich[43] = [[[" [cite: 1]"], [entry]]]
            candidate[12] = rich
            turns.append([[None, f"r_{number}"], None, [["Question"]], [[candidate]]])
        result = history([turns], lambda cid: Origin("gemini", "account", cid), "c_fixture")
        final = [
            message
            for message in result.messages
            if message.role == "assistant" and message.channel != "analysis"
        ]
        self.assertEqual(final[0].actions, ("branch",))
        self.assertEqual(final[1].actions, ("retry", "branch"))
        self.assertEqual(
            final[1].metadata["sources"],
            ({"title": "Source", "url": "https://example.com/source"},),
        )
        thoughts = [message for message in result.messages if message.channel == "analysis"]
        self.assertTrue(all(not message.actions for message in thoughts))
        self.assertTrue(all(message.content[0].text == "Published thought" for message in thoughts))

    def test_history_reads_gemini_published_search_steps_and_result_sources(self):
        candidate = [None] * 38
        candidate[0], candidate[1] = "rc_activity", ["Answer"]
        activity = [None] * 8
        activity[1] = activity[5] = "Web'de arama yapıldı"
        activity[7] = [
            ["favicon", "https://example.com/one", "First result"],
            ["favicon", "https://example.com/two", "Second result"],
            ["favicon", "https://example.com/one", "Duplicate result"],
        ]
        candidate[37] = [[""], [activity]]
        turn = [[None, "r_activity"], None, [["Question"]], [[candidate]]]

        result = history([[turn]], lambda cid: Origin("gemini", "account", cid), "c_fixture")

        thought = next(message for message in result.messages if message.channel == "analysis")
        answer = next(
            message
            for message in result.messages
            if message.role == "assistant" and message.channel != "analysis"
        )
        self.assertEqual(thought.content[0].text, "Web'de arama yapıldı")
        self.assertTrue(thought.metadata["published_activity"])
        self.assertEqual(
            answer.metadata["sources"],
            (
                {"title": "First result", "url": "https://example.com/one"},
                {"title": "Second result", "url": "https://example.com/two"},
            ),
        )

    def test_published_sources_are_bounded_deduplicated_and_cannot_embed_credentials(self):
        values = [
            {"url": "https://example.com", "title": "Valid"},
            {"url": "javascript:bad"},
            {"url": "https://secret@example.com"},
            {"url": "https://example.com", "title": "Duplicate"},
        ]
        self.assertEqual(
            sources_from({"citations": values}), ({"title": "Valid", "url": "https://example.com"},)
        )
        self.assertEqual(sources_from({"text": "https://unverified.example"}), ())
        self.assertEqual(
            sources_from({"citations": [{"file_id": "file-example", "filename": "notes.pdf"}]}),
            ({"title": "notes.pdf", "file_id": "file-example"},),
        )


class NativeDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_chatgpt_asset_reference_gets_a_fresh_url_and_checks_conversation_membership(
        self,
    ):
        from providers.chatgpt.adapter import ChatGPTProvider

        provider = ChatGPTProvider.__new__(ChatGPTProvider)
        client = Mock()
        client.get_conversation.return_value = {
            "messages": [
                {
                    "content": {
                        "parts": [
                            {
                                "content_type": "image_asset_pointer",
                                "asset_pointer": "file-service://file-output",
                            }
                        ]
                    }
                }
            ]
        }
        client.request.return_value.json.return_value = {
            "download_url": "https://cdn.example.com/fresh",
            "file_name": "output.png",
        }
        client.headers = {"Authorization": "private"}
        provider._client_scope = lambda: nullcontext(client)
        source = await provider.resolve_download("file-service://file-output", "chat", "answer")
        self.assertEqual(source.url, "https://cdn.example.com/fresh")
        self.assertEqual(source.filename, "output.png")
        self.assertEqual(source.headers, {})
        client.request.assert_called_once_with(
            "GET",
            "https://chatgpt.com/backend-api/files/download/file-output",
            params={"conversation_id": "chat", "inline": "false"},
        )
        client.request.reset_mock()
        with self.assertRaises(ValueError):
            await provider.resolve_download("file-service://file-foreign", "chat", "answer")
        client.request.assert_not_called()


class ClaudeActionsTests(unittest.IsolatedAsyncioTestCase):
    def test_presented_files_are_attached_to_the_following_answer_in_history(self):
        from providers.claude.records import conversation

        value = {
            "uuid": "chat",
            "chat_messages": [
                {
                    "uuid": "tool",
                    "sender": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "present_files",
                            "input": {"filepaths": ["/mnt/user-data/outputs/result.docx"]},
                        }
                    ],
                },
                {
                    "uuid": "answer",
                    "sender": "assistant",
                    "content": [{"type": "text", "text": "Done"}],
                },
            ],
        }
        messages = conversation(value, "account").messages
        self.assertEqual(
            messages[-1].metadata["files"],
            ({"url": "/mnt/user-data/outputs/result.docx", "name": "result.docx"},),
        )
        value["chat_messages"].pop()
        file_only = conversation(value, "account").messages[-1]
        self.assertEqual(file_only.channel, "final")
        self.assertEqual(file_only.metadata["files"][0]["name"], "result.docx")

    async def test_edit_and_retry_keep_their_native_parents_and_reuse_the_account_model(self):
        chat_id = "00000000-0000-4000-8000-000000000001"
        user_id = "00000000-0000-4000-8000-000000000002"
        answer_id = "00000000-0000-4000-8000-000000000003"
        raw = {
            "uuid": chat_id,
            "current_leaf_message_uuid": answer_id,
            "chat_messages": [
                {
                    "uuid": user_id,
                    "sender": "human",
                    "text": "Original",
                    "parent_message_uuid": None,
                    "files": [{"uuid": "uploaded-file"}],
                    "attachments": [{"file_name": "notes.txt", "extracted_content": "data"}],
                },
                {
                    "uuid": answer_id,
                    "sender": "assistant",
                    "text": "Answer",
                    "parent_message_uuid": user_id,
                },
            ],
        }

        class Response:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                pass

            async def aiter_lines(self):
                yield 'data: {"type":"message_start","message":{"uuid":"reply","parent_uuid":"prompt"}}'
                yield ""
                yield 'data: {"type":"content_block_delta","delta":{"type":"citations_delta","citation":{"url":"https://example.com/source","title":"Published source"}}}'
                yield ""
                yield 'data: {"type":"content_block_start","index":1,"content_block":{"type":"tool_use","name":"present_files","input":{}}}'
                yield ""
                yield "data: " + json.dumps(
                    {
                        "type": "content_block_delta",
                        "index": 1,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": '{"filepaths":["/mnt/user-data/outputs/result.pdf"]}',
                        },
                    }
                )
                yield ""
                yield 'data: {"type":"content_block_stop","index":1}'
                yield ""
                yield 'data: {"type":"message_stop"}'
                yield ""

        writes = []

        def stream(method, url, **values):
            writes.append((url, values["json"]))
            return Response()

        session = SimpleNamespace(
            path=None,
            request=AsyncMock(return_value=raw),
            connect=lambda: SimpleNamespace(stream=stream),
            url=lambda path: path,
            check=lambda response: None,
        )
        transport = ClaudeTransport(None, session=session)
        transport.base = AsyncMock(return_value="/api/organizations/account")
        transport.catalog = {
            "default_model": "available-model",
            "models": [{"id": "available-model", "efforts": []}],
        }
        for action, target in ((MessageAction.EDIT, user_id), (MessageAction.RETRY, answer_id)):
            events = [
                event
                async for event in transport.events(
                    "send",
                    action=action,
                    message_id=target + ":0",
                    conversation_id=chat_id,
                    prompt="Edited",
                )
            ]
            self.assertEqual(events[-1]["type"], "done")
            metadata = next(event for event in events if event["type"] == "metadata")
            self.assertEqual(
                metadata["files"],
                ({"url": "/mnt/user-data/outputs/result.pdf", "name": "result.pdf"},),
            )
            self.assertEqual(
                metadata["sources"],
                ({"title": "Published source", "url": "https://example.com/source"},),
            )
            self.assertEqual(
                metadata["user_message_id"], "prompt:0" if action == MessageAction.EDIT else None
            )
        edit, retry = writes
        self.assertTrue(edit[0].endswith("/completion"))
        self.assertNotIn("parent_message_uuid", edit[1])
        self.assertEqual(edit[1]["files"], ["uploaded-file"])
        self.assertEqual(edit[1]["attachments"][0]["extracted_content"], "data")
        self.assertEqual(
            edit[1]["tools"], [{"type": "web_search_v0", "name": "web_search"}]
        )
        self.assertTrue(retry[0].endswith("/retry_completion"))
        self.assertEqual(retry[1]["parent_message_uuid"], answer_id)
        self.assertEqual(retry[1]["model"], "available-model")
        self.assertEqual(
            retry[1]["tools"], [{"type": "web_search_v0", "name": "web_search"}]
        )
        with self.assertRaisesRegex(ValueError, "from this account"):
            _ = [
                event
                async for event in transport.events(
                    "send", prompt="X", model="not-in-this-subscription"
                )
            ]
        self.assertEqual(len(writes), 2)
