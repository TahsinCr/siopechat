"""Message mutation durability, tab ownership and asynchronous UI behavior."""

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from cli.markdown import visible_chat_message
from extensions.archive.repository import Files, SQLiteRepository
from extensions.archive.sync import Archive
from providers import ConversationBound, MessageAction, MessageMetadata, ReplaceText
from providers.models import Content, Conversation, Message, Origin
from tests.account_fixture import configured_application
from tests.cli_support import terminal


class MutationArchiveTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.archive = Archive(SQLiteRepository(root / "archive.sqlite3"), Files(root / "files"))
        self.addCleanup(self.archive.close)

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

    def test_retry_without_a_new_prompt_reaches_the_native_action(self):
        with tempfile.TemporaryDirectory() as folder:
            api = configured_application(Path(folder))
            self.addCleanup(api.runtime.close)
            accounts = api.get("accounts")
            requests = []

            class Provider:
                id, account_id = "chatgpt", "default"

                async def stream(self, request):
                    requests.append(request)
                    yield ConversationBound("chat")
                    yield ReplaceText("Retried", "final")

                async def get_chat(self, cid):
                    return Conversation(
                        Origin(self.id, self.account_id, cid),
                        "Chat",
                        (
                            Message("u", "user", (Content("text", "Original"),)),
                            Message("a", "assistant", (Content("text", "Retried"),)),
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
            view.message_toolbar_action(
                view.tabs[view.active_tab]["tab_id"], view.messages[0], "sources"
            )
            self.assertTrue(view.forms.visible)
            view.app._redraw()
            view.forms.close()
