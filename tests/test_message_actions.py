"""Message mutation durability, tab ownership and asynchronous UI behavior."""

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from extensions.archive.repository import Files, SQLiteRepository
from extensions.archive.sync import Archive
from providers import ConversationBound, MessageAction, MessageMetadata, ReplaceText
from providers.models import Content, Conversation, Message, Origin
from tests.account_fixture import configured_application
from tests.tui_support import terminal
from tui.markdown import transcript_fragments, visible_chat_message, visible_chat_messages


class MutationArchiveTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.archive = Archive(SQLiteRepository(root / "archive.sqlite3"), Files(root / "files"))
        self.addCleanup(self.archive.close)

    def test_provider_refresh_keeps_reply_settings_and_files_through_sqlite_reload(self):
        from providers.history import merge_conversation

        local = {
            "messages": [
                {
                    "id": "answer",
                    "role": "assistant",
                    "text": "Done",
                    "metadata": {
                        "model": "model-a",
                        "effort": "high",
                        "files": [
                            {
                                "url": "sandbox:/mnt/data/result.csv",
                                "name": "result.csv",
                                "size_bytes": 17,
                            }
                        ],
                        "sources": [{"title": "Reference", "url": "https://example.com/source"}],
                    },
                }
            ]
        }
        remote = {
            "messages": [
                {
                    "id": "answer",
                    "role": "assistant",
                    "text": "Done",
                    "metadata": {"native": {"id": "answer"}},
                }
            ]
        }
        value = merge_conversation(remote, local)
        self.archive.put("chatgpt/default", "conversation", "chat", value)
        stored = self.archive.get("chatgpt/default", "conversation", "chat")
        visible = visible_chat_messages(stored["messages"])
        self.assertEqual(visible[-1]["_files"][0]["size_bytes"], 17)
        self.assertIn("result.csv", visible[-1]["text"])
        self.assertEqual(
            dict(visible[-1]["_response_settings"]), {"Model": "model-a", "Effort": "high"}
        )
        self.assertEqual(
            visible[-1]["_thinking_sources"],
            ({"title": "Reference", "url": "https://example.com/source"},),
        )

    def test_chatgpt_search_groups_remain_answer_sources_after_sqlite_reload(self):
        from tui.markdown import MessageRenderer

        message = {
            "id": "answer",
            "role": "assistant",
            "text": "Published answer",
            "metadata": {
                "metadata": {
                    "search_result_groups": [
                        {
                            "domain": "docs.example",
                            "entries": [
                                {
                                    "title": "Official documentation",
                                    "url": "https://docs.example/guide",
                                    "snippet": "Published citation excerpt",
                                }
                            ],
                        }
                    ]
                }
            },
        }
        self.archive.put(
            "chatgpt/default", "conversation", "chat", {"title": "Chat", "messages": [message]}
        )

        visible = visible_chat_messages(
            self.archive.get("chatgpt/default", "conversation", "chat")["messages"]
        )

        self.assertEqual([item["role"] for item in visible], ["assistant"])
        self.assertEqual(
            visible[0]["_thinking_sources"],
            (
                {
                    "title": "Official documentation",
                    "url": "https://docs.example/guide",
                    "description": "Published citation excerpt",
                },
            ),
        )
        self.assertEqual(visible[0]["text"], "Published answer")
        rendered, _ = MessageRenderer().transcript(visible, 80, show_reasoning=True)
        self.assertNotIn("Düşünme özeti", rendered)
        self.assertNotIn("Official documentation", rendered)
        self.assertIn("Published answer", rendered)

    def test_read_started_before_edit_cannot_restore_old_tail_or_pending_messages(self):
        old = {
            "title": "Chat",
            "messages": [
                {"id": "u", "role": "user", "text": "Old"},
                {"id": "a", "role": "assistant", "text": "Old answer"},
                {"id": "pending", "role": "user", "text": "Obsolete", "local_pending": True},
            ],
        }
        fresh = {
            "title": "Chat",
            "messages": [
                {"id": "edited", "role": "user", "text": "Changed"},
                {"id": "new", "role": "assistant", "text": "New answer"},
            ],
        }
        self.archive.put("chatgpt/default", "conversation", "chat", old)
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def fetch():
            started.set()
            if not release.wait(3):
                raise RuntimeError("test release timeout")
            return old

        self.archive.read("chatgpt/default", "conversation", "chat", fetch, force=True, wait=False)
        self.assertTrue(started.wait(2))
        future = next(iter(self.archive._pending.values()))
        self.archive.replace_history("chatgpt/default", "chat", fresh)
        release.set()
        future.result(timeout=3)
        self.assertEqual(
            self.archive.get("chatgpt/default", "conversation", "chat")["messages"],
            fresh["messages"],
        )

    def test_partial_rewrite_retains_sql_prefix_and_removes_only_replaced_suffix(self):
        old = [{"id": str(i), "role": "assistant", "text": str(i)} for i in range(10)]
        self.archive.put(
            "gemini/account", "conversation", "chat", {"title": "Chat", "messages": old}
        )
        revised = [old[5], {"id": "replacement", "role": "assistant", "text": "Changed"}]
        self.archive.replace_history(
            "gemini/account",
            "chat",
            {
                "title": "Chat",
                "history_complete": False,
                "messages": revised,
            },
        )
        self.assertEqual(
            self.archive.get("gemini/account", "conversation", "chat")["messages"],
            old[:5] + revised,
        )

    def test_partial_edit_can_replace_the_first_returned_turn_without_losing_older_sql_rows(self):
        old = [
            {"id": "older", "role": "user", "text": "Keep"},
            {"id": "edited", "role": "user", "text": "Old prompt"},
            {"id": "answer", "role": "assistant", "text": "Old answer"},
        ]
        self.archive.put("gemini/account", "conversation", "chat", {"messages": old})
        revised = [
            {"id": "new-user", "role": "user", "text": "Changed"},
            {"id": "new-answer", "role": "assistant", "text": "New answer"},
        ]
        self.archive.replace_history(
            "gemini/account",
            "chat",
            {"history_complete": False, "messages": revised},
            replaced_message_id="edited",
        )
        self.assertEqual(
            self.archive.get("gemini/account", "conversation", "chat")["messages"],
            old[:1] + revised,
        )

    def test_repeated_local_retries_are_saved_in_sqlite_order(self):
        self.archive.put(
            "chatgpt/default",
            "conversation",
            "chat",
            {
                "messages": [
                    {"id": "user", "role": "user", "text": "Prompt"},
                    {"id": "answer-1", "role": "assistant", "text": "First answer"},
                ]
            },
        )
        for previous, current, text in (
            ("answer-1", "answer-2", "Second answer"),
            ("answer-2", "answer-3", "Third answer"),
        ):
            self.archive.replace_history(
                "chatgpt/default",
                "chat",
                {
                    "messages": [
                        {"id": "user", "role": "user", "text": "Prompt"},
                        {"id": current, "role": "assistant", "text": text},
                    ]
                },
                retry_message_id=previous,
                new_message_id=current,
            )

        versions = [
            self.archive.response_version("chatgpt/default", "chat", "answer-3", index)
            for index in (1, 2, 3)
        ]
        self.assertEqual(
            [item["message"]["text"] for item in versions],
            ["First answer", "Second answer", "Third answer"],
        )
        self.assertEqual([item["current_index"] for item in versions], [3, 3, 3])

    def test_provider_refresh_keeps_locally_captured_retry_versions(self):
        self.archive.put(
            "chatgpt/default",
            "conversation",
            "chat",
            {
                "messages": [
                    {"id": "user", "role": "user"},
                    {"id": "answer-1", "role": "assistant", "text": "Local old"},
                ]
            },
        )
        self.archive.replace_history(
            "chatgpt/default",
            "chat",
            {
                "messages": [
                    {"id": "user", "role": "user"},
                    {"id": "answer-2", "role": "assistant", "text": "Current"},
                ]
            },
            retry_message_id="answer-1",
            new_message_id="answer-2",
        )
        self.archive.put(
            "chatgpt/default",
            "conversation",
            "chat",
            {
                "messages": [
                    {"id": "user", "role": "user"},
                    {"id": "answer-2", "role": "assistant", "text": "Current"},
                ]
            },
        )

        versions = [
            self.archive.response_version("chatgpt/default", "chat", "answer-2", index)
            for index in (1, 2)
        ]
        self.assertEqual(
            [item["message"]["text"] for item in versions],
            ["Local old", "Current"],
        )
        self.assertEqual([item["current_index"] for item in versions], [2, 2])


class MutationAccountsTests(unittest.TestCase):
    def test_confirmed_edit_replaces_archive_and_keeps_account_and_project_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            api = configured_application(Path(folder))
            try:
                accounts = api.get("accounts")
                archive = accounts.archive()
                archive.put(
                    "chatgpt/default",
                    "conversation",
                    "chat",
                    {
                        "id": "chat",
                        "title": "Chat",
                        "messages": [
                            {"id": "old-user", "role": "user", "text": "Original"},
                            {"id": "old-answer", "role": "assistant", "text": "Old answer"},
                        ],
                    },
                )
                received = []

                class Provider:
                    id, account_id = "chatgpt", "default"

                    async def stream(self, request):
                        received.append(request)
                        yield ConversationBound("chat")
                        yield ReplaceText("New answer", "final")
                        yield MessageMetadata("new-answer", "new-user", actions=("retry", "branch"))

                    async def get_chat(self, remote):
                        return Conversation(
                            Origin("chatgpt", "default", remote),
                            "Chat",
                            (
                                Message(
                                    "new-user",
                                    "user",
                                    (Content("text", "Changed"),),
                                    actions=("edit",),
                                ),
                                Message(
                                    "new-answer",
                                    "assistant",
                                    (Content("text", "New answer"),),
                                    actions=("retry", "branch"),
                                ),
                            ),
                            project_id="g-p-work",
                        )

                    async def close(self):
                        pass

                provider = Provider()
                accounts._providers["chatgpt/default"] = provider
                result = accounts.send(
                    "chatgpt/default",
                    "chat",
                    "Changed",
                    [],
                    lambda event: None,
                    lambda connection: None,
                    action="edit",
                    message_id="old-user",
                )
                self.assertEqual(received[0].action, MessageAction.EDIT)
                saved = archive.get("chatgpt/default", "conversation", "chat")
                self.assertEqual([m["id"] for m in saved["messages"]], ["new-user", "new-answer"])
                self.assertEqual(saved["project_id"], "@chatgpt/default/g-p-work")
                self.assertEqual(result["conversation_id"], "@chatgpt/default/chat")
                self.assertEqual(result["history"]["messages"][-1]["text"], "New answer")
            finally:
                api.runtime.close()

    def test_pending_branch_reopens_offline_and_binds_only_after_accepted_first_turn(self):
        with tempfile.TemporaryDirectory() as folder:
            api = configured_application(Path(folder))
            self.addCleanup(api.runtime.close)
            accounts = api.get("accounts")
            archive = accounts.archive()
            source = {
                "title": "Source",
                "messages": [{"id": "a", "role": "assistant", "text": "Original"}],
            }
            archive.put("chatgpt/default", "conversation", "source", source)
            requests, reads = [], []

            class Provider:
                id, account_id = "chatgpt", "default"

                async def branch_chat(self, cid, mid):
                    return Conversation(
                        Origin(self.id, self.account_id, "preview"),
                        "Branch",
                        (Message("a", "assistant", (Content("text", "Original"),)),),
                        metadata={"pending_branch": {"conversation_id": cid, "message_id": mid}},
                    )

                async def stream(self, request):
                    requests.append(request)
                    yield ConversationBound("persisted")
                    yield ReplaceText("New answer", "final")

                async def get_chat(self, cid):
                    reads.append(cid)
                    if cid != "persisted":
                        raise AssertionError("An uncommitted branch must be read from SQL")
                    return Conversation(
                        Origin(self.id, self.account_id, cid),
                        "Branch",
                        (
                            Message("a", "assistant", (Content("text", "Original"),)),
                            Message("u", "user", (Content("text", "Continue"),)),
                            Message("new", "assistant", (Content("text", "New answer"),)),
                        ),
                    )

                async def close(self):
                    pass

            accounts._providers["chatgpt/default"] = Provider()
            record = accounts.branch_chat("chatgpt/default", "source", "a")
            reopened = accounts.load(record["id"], "chatgpt/default", force=True, window=True)
            self.assertEqual(reopened["messages"][0]["text"], "Original")
            self.assertEqual(reads, [])
            result = accounts.send(
                "chatgpt/default",
                record["id"],
                "Continue",
                [],
                lambda event: None,
                lambda connection: None,
            )
            self.assertEqual(requests[0].branch.conversation_id, "source")
            self.assertEqual(requests[0].branch.message_id, "a")
            self.assertEqual(result["conversation_id"], "@chatgpt/default/persisted")
            self.assertEqual(result["replaced_conversation_id"], "@chatgpt/default/preview")
            self.assertTrue(archive.deleted("chatgpt/default", "conversation", "preview"))
            self.assertEqual(
                archive.get("chatgpt/default", "conversation", "source")["messages"],
                source["messages"],
            )
            self.assertEqual(len(result["history"]["messages"]), 3)

    def test_gemini_branch_is_not_a_local_sidebar_preview(self):
        with tempfile.TemporaryDirectory() as folder:
            api = configured_application(Path(folder))
            self.addCleanup(api.runtime.close)
            accounts = api.get("accounts")
            key = accounts.add("gemini", "Gemini test")
            archive = accounts.archive()
            archive.put(
                key,
                "conversation",
                "c_source",
                {
                    "title": "Source",
                    "messages": [{"id": "answer", "role": "assistant", "text": "Original"}],
                },
            )
            reads = []

            class Provider:
                id, account_id = "gemini", key.split("/", 1)[1]

                async def branch_chat(self, conversation_id, message_id):
                    return Conversation(
                        Origin(self.id, self.account_id, "c_branch"),
                        "Branch",
                        (Message("answer", "assistant", (Content("text", "Original"),)),),
                    )

                async def get_chat(self, conversation_id):
                    reads.append(conversation_id)
                    return Conversation(
                        Origin(self.id, self.account_id, conversation_id),
                        "Branch",
                        (Message("answer", "assistant", (Content("text", "Original"),)),),
                    )

                async def close(self):
                    pass

            accounts._providers[key] = Provider()
            branch = accounts.branch_chat(key, "c_source", "answer")
            self.assertFalse(branch.get("pending_local_branch"))
            self.assertEqual(archive.pending_branches(key), [])

            # Ignore a marker left by older behavior and still consult Gemini on reopen.
            archive.mark_pending_branch(key, "c_branch")
            self.assertEqual(accounts._pending_local_branches(key, []), [])

            def fetch_now(account, kind, remote, fetch, force=False, **options):
                return fetch()

            with patch.object(accounts, "read_record", side_effect=fetch_now):
                reopened = accounts.load(branch["id"], key, force=True, window=True)
            self.assertEqual(reads, ["c_branch"])
            self.assertEqual(reopened["remote_id"], "c_branch")

    def test_retry_without_a_new_prompt_reaches_the_native_action(self):
        with tempfile.TemporaryDirectory() as folder:
            api = configured_application(Path(folder))
            self.addCleanup(api.runtime.close)
            accounts = api.get("accounts")
            accounts.archive().put(
                "chatgpt/default",
                "conversation",
                "chat",
                {
                    "title": "Chat",
                    "messages": [
                        {"id": "u", "role": "user", "text": "Original"},
                        {"id": "a", "role": "assistant", "text": "Old answer"},
                    ],
                },
            )
            requests = []

            class Provider:
                id, account_id = "chatgpt", "default"

                async def stream(self, request):
                    requests.append(request)
                    yield ConversationBound("chat")
                    yield ReplaceText("Retried", "final")
                    yield MessageMetadata("new-a")

                async def get_chat(self, cid):
                    return Conversation(
                        Origin(self.id, self.account_id, cid),
                        "Chat",
                        (
                            Message("u", "user", (Content("text", "Original"),)),
                            Message("new-a", "assistant", (Content("text", "Retried"),)),
                        ),
                    )

                async def close(self):
                    pass

            accounts._providers["chatgpt/default"] = Provider()
            result = accounts.send(
                "chatgpt/default",
                "chat",
                "",
                [],
                lambda event: None,
                lambda connection: None,
                action="retry",
                message_id="a",
            )
            self.assertEqual(requests[0].action, MessageAction.RETRY)
            self.assertEqual(requests[0].prompt, "")
            self.assertEqual(result["history"]["messages"][-1]["text"], "Retried")


class MessageToolbarTests(unittest.TestCase):
    def test_message_actions_come_from_the_message_and_stale_tab_click_does_not_send(self):
        source = {"id": "a", "role": "assistant", "text": "Answer", "actions": ["retry", "branch"]}
        self.assertEqual(visible_chat_message(source)["actions"], ["retry", "branch"])
        with terminal(initial_messages=[source]) as (view, _):
            view._submit_message_action = Mock()
            old_tab = view.tabs[view.active_tab]["tab_id"]
            old_message = view.messages[0]
            view.new_tab()
            view.message_toolbar_action(old_tab, old_message, "retry")
            view._submit_message_action.assert_not_called()

    def test_branch_is_added_to_sidebar_only_after_the_provider_lists_it(self):
        with terminal() as (view, _):
            key = "gemini/default"
            conversation_id = "@gemini/default/c_branch"
            page = {"items": []}
            service = SimpleNamespace(
                list_chats=Mock(return_value=page),
                callbacks=Mock(return_value={}),
            )
            view.account_service = service
            view.account_key = key
            view.sidebar_items = []

            def immediate_read(read, success, failure, **kwargs):
                success(read())

            view._read_in_background = immediate_read

            view._confirm_branch_listing({"account_key": key, "project": {}}, conversation_id)
            self.assertEqual(view.sidebar_items, [])

            page["items"].append(
                {"id": conversation_id, "remote_id": "c_branch", "title": "Listed by Gemini"}
            )
            view._confirm_branch_listing({"account_key": key, "project": {}}, conversation_id)
            self.assertEqual([item["id"] for item in view.sidebar_items], [conversation_id])
            service.list_chats.assert_called_with(
                selected={"gemini"}, force=True, account_keys={key}
            )

    def test_project_branch_confirmation_uses_the_provider_project_list(self):
        with terminal() as (view, _):
            key = "gemini/default"
            project_id = "@gemini/default/g-p-notebook"
            conversation_id = "@gemini/default/c_branch"
            project_list = Mock(
                return_value={
                    "items": [
                        {
                            "id": conversation_id,
                            "remote_id": "c_branch",
                            "title": "Listed in Gemini notebook",
                        }
                    ]
                }
            )
            service = SimpleNamespace(
                list_chats=Mock(),
                callbacks=Mock(return_value={"list_project_chats_callback": project_list}),
            )
            view.account_service = service
            view.account_key = key
            view.sidebar_items = []

            def immediate_read(read, success, failure, **kwargs):
                success(read())

            view._read_in_background = immediate_read
            view._confirm_branch_listing(
                {"account_key": key, "project": {"id": project_id}}, conversation_id
            )

            project_list.assert_called_once_with(project_id)
            service.list_chats.assert_not_called()
            self.assertEqual([item["id"] for item in view.sidebar_items], [conversation_id])

    def test_retry_failure_restores_original_history_and_leaves_other_tab_untouched(self):
        with terminal(
            initial_messages=[
                {"id": "u", "role": "user", "text": "Prompt"},
                {"id": "a", "role": "assistant", "text": "Original"},
            ]
        ) as (view, _):
            target = view.tabs[0]
            original = list(view.messages)
            pending = [original[0], {"role": "assistant", "text": ""}]
            view.new_tab()
            other = view.tabs[view.active_tab]
            other["messages"] = [{"role": "user", "text": "Other chat"}]
            view._dispatch_ui = lambda callback: callback()
            target.update(messages=pending, busy=True)
            view.run_send(
                "",
                [],
                0,
                pending,
                "chat",
                "model",
                None,
                target_tab=target,
                send_callback=Mock(side_effect=RuntimeError("provider refused")),
                message_action="retry",
                message_id="a",
                rollback=(original, None),
            )
            self.assertEqual(target["messages"], original)
            self.assertEqual(other["messages"][0]["text"], "Other chat")
            self.assertFalse(target["busy"])

    def test_duplicate_split_view_cannot_mutate_a_conversation_while_it_is_streaming(self):
        source = {"id": "a", "role": "assistant", "text": "Answer", "actions": ["retry"]}
        with terminal(initial_messages=[source]) as (view, _):
            tab = view.tabs[0]
            tab.update(account_key="chatgpt/default", conversation_id="chat")
            view.account_service = SimpleNamespace()
            view.background_tabs.append(dict(conversation_id="chat", busy=True))
            view._submit_message_action = Mock()
            view.message_toolbar_action(tab["tab_id"], view.messages[0], "retry")
            view._submit_message_action.assert_not_called()
            self.assertIn("finish", view.status)

    def test_file_citations_render_without_a_fabricated_link_or_network_request(self):
        message = {
            "id": "answer",
            "role": "assistant",
            "text": "From your file",
            "metadata": {"sources": [{"title": "notes.pdf", "file_id": "file-ref"}]},
        }
        with terminal(initial_messages=[message]) as (view, _):
            self.assertEqual(view.messages[0]["role"], "assistant")
            self.assertEqual(
                view.messages[0]["_thinking_sources"],
                ({"title": "notes.pdf", "file_id": "file-ref"},),
            )
            self.assertNotIn("](", view.messages[0]["text"])
            self.assertEqual(view.messages[0]["text"], "From your file")

    def test_published_sources_stay_with_the_answer_after_repeated_presentation(self):
        raw = [
            {"id": "think", "role": "reasoning", "text": "Published summary"},
            {
                "id": "answer",
                "role": "assistant",
                "text": "Answer",
                "metadata": {
                    "model": "model-a",
                    "effort": "high",
                    "sources": [{"title": "Reference", "url": "https://example.com/source"}],
                },
            },
        ]
        first = visible_chat_messages(raw)
        second = visible_chat_messages(first)
        self.assertEqual(first, second)
        self.assertEqual([x["role"] for x in second], ["reasoning", "assistant"])
        self.assertEqual(second[0]["text"], "Published summary")
        self.assertNotIn("_thinking_sources", second[0])
        self.assertEqual(
            second[1]["_thinking_sources"],
            ({"title": "Reference", "url": "https://example.com/source"},),
        )
        self.assertEqual(
            second[-1]["_response_settings"], (("Model", "model-a"), ("Effort", "high"))
        )

    def test_assistant_toolbar_opens_sources_after_other_left_actions(self):
        message = {
            "id": "answer",
            "role": "assistant",
            "text": "Answer",
            "metadata": {
                "sources": [{"title": "Reference", "url": "https://example.com/source"}]
            },
            "actions": ["retry", "branch"],
        }
        with terminal(initial_messages=[message]) as (view, _):
            from tui.widgets import TranscriptControl

            answer = view.messages[0]
            fragments = TranscriptControl(view)._message_toolbar(0, 100)
            labels = [
                text
                for style, text, *_ in fragments
                if style == "class:message.toolbar.action"
            ]
            self.assertEqual(len(labels), 4)
            self.assertIn("↗", labels[-1])

            raw_answer = dict(message)
            view.tabs[0]["messages"][0] = raw_answer
            view.messages = view.tabs[0]["messages"]
            view.forms.open = Mock()
            open_link_list = view.forms.open_link_list
            captured = {}

            def show_sources(title, items):
                captured.update(title=title, items=items)
                open_link_list(title, items)

            view.forms.open_link_list = show_sources
            view.open_message_link = Mock()
            view.message_toolbar_action(view.tabs[0]["tab_id"], raw_answer, "sources")

            self.assertTrue(view.forms.visible)
            self.assertEqual(captured["title"], view.tr("Used sources"))
            self.assertIn("example.com", captured["items"][0]["url"])
            view.forms.open.assert_not_called()
            with patch("tui.markdown.open_link") as open_link:
                captured["items"][0]["callback"]()
            open_link.assert_called_once_with("https://example.com/source")
            view.open_message_link.assert_not_called()

    def test_deleted_chat_tabs_are_removed_without_creating_a_new_chat(self):
        with terminal() as (view, _):
            template = view.tabs[0]

            def tab(chat_id):
                value = dict(template)
                value.update(
                    tab_id=chat_id,
                    conversation_id=chat_id,
                    title=chat_id,
                    messages=[{"id": chat_id, "role": "assistant", "text": chat_id}],
                    project={},
                    busy=False,
                    loading=False,
                )
                return value

            view.tabs = [tab("previous"), tab("deleted"), tab("next")]
            view.active_tab = 1
            view._restore_active_tab(focus=False)
            view.remove_deleted_conversation_tabs({"deleted"})

            self.assertEqual(
                [item["conversation_id"] for item in view.tabs], ["previous", "next"]
            )
            self.assertEqual(view.tabs[view.active_tab]["conversation_id"], "previous")

            view.tabs = [tab("left"), tab("inactive-delete"), tab("active")]
            view.active_tab = 2
            view._restore_active_tab(focus=False)
            view.remove_deleted_conversation_tabs({"inactive-delete"})
            self.assertEqual(view.tabs[view.active_tab]["conversation_id"], "active")
            self.assertEqual(
                [item["conversation_id"] for item in view.tabs], ["left", "active"]
            )

            view.tabs = [tab("last")]
            view.active_tab = 0
            view.tabs[0]["busy"] = True
            view._restore_active_tab(focus=False)
            view.remove_deleted_conversation_tabs({"last"})
            self.assertTrue(view.tabs[0]["empty_workspace"])
            self.assertFalse(view.background_tabs)
            self.assertIsNone(view.tabs[0]["conversation_id"])
            self.assertEqual(view.tabs[0]["title"], "")
            self.assertNotIn(
                "New conversation", "".join(fragment[1] for fragment in view.render_tabs())
            )

            view.new_tab()
            self.assertFalse(view.tabs[0]["empty_workspace"])
            self.assertEqual(view.tabs[0]["title"], view.tr("New conversation"))

    def test_sources_without_a_thinking_message_stay_on_the_answer_toolbar(self):
        from tui.widgets import TranscriptControl

        visible = visible_chat_messages(
            [
                {
                    "id": "answer",
                    "role": "assistant",
                    "text": "Answer",
                    "metadata": {
                        "sources": [{"title": "Reference", "url": "https://example.com"}]
                    },
                }
            ]
        )
        self.assertEqual([message["role"] for message in visible], ["assistant"])
        with terminal(initial_messages=visible) as (view, _):
            view.render_transcript()
            control = TranscriptControl(view)
            content = control.create_content(80, 20)
            toolbar_row, = control._toolbar_rows

            self.assertEqual(control._toolbar_rows[toolbar_row], 0)
            self.assertTrue(any("Sources" in fragment[1] for fragment in content.get_line(toolbar_row)))

    def test_completion_metadata_adds_sources_and_file_to_the_owning_answer_once(self):
        from tui.chat import _SendStream

        with terminal(initial_messages=[{"id": "draft", "role": "assistant", "text": "Done"}]) as (
            view,
            _,
        ):
            stream = _SendStream(view, view.tabs[0], view.messages)
            stream._schedule_refresh = Mock()
            event = {
                "type": "metadata",
                "message_id": "answer",
                "model": "model-a",
                "sources": [{"title": "Source", "url": "https://example.com"}],
                "files": [
                    {"url": "sandbox:/mnt/data/result.csv", "name": "result.csv", "size_bytes": 42}
                ],
            }
            stream.on_event(event)
            stream.on_event(event)
            self.assertEqual(len(view.messages), 1)
            self.assertEqual(view.messages[0]["_thinking_sources"], tuple(event["sources"]))
            self.assertEqual(view.messages[0]["text"].count("↓"), 1)
            self.assertEqual(view.messages[0]["_body_length"], len("Done"))

    def test_file_card_retains_metadata_and_starts_the_download_manager(self):
        message = {
            "id": "answer",
            "role": "assistant",
            "text": "Your file",
            "metadata": {
                "files": [
                    {
                        "name": "result.html",
                        "download_url": "https://example.com/result",
                        "media_type": "text/html",
                        "size_bytes": 1024,
                    }
                ]
            },
        }
        with terminal(initial_messages=[message]) as (view, _):
            view.conversation_id = "chat"
            view.account_service = SimpleNamespace(
                download=Mock(return_value={"kind": "file", "path": "/downloads/result.html"})
            )
            view._read_in_background = lambda read, done, failed, **kw: done(read())
            text, _ = view.renderer.transcript(view.messages, 80)
            fragments = transcript_fragments(text)
            target = next(
                span[2].url
                for span in fragments
                if len(span) > 2 and getattr(span[2], "url", "").startswith("siope-file:")
            )
            self.assertTrue(any(span[0] == "class:message.file" for span in fragments))
            view.open_message_link(target, 0)
            call = view.account_service.download.call_args
            self.assertEqual(call.args[1:4], ("https://example.com/result", "chat", "answer"))
            self.assertTrue(call.kwargs["force_download"])
            self.assertEqual(view.messages[0]["_body_length"], len("Your file"))
