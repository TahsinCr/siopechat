"""Application and provider integration with the local archive."""

import asyncio
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from extensions.archive.repository import Files, SQLiteRepository, encode
from extensions.archive.sync import Archive
from providers import Capability
from providers.models import Choice, FeatureState, Model, ModelCatalog
from tests.account_fixture import configured_application as create_application
from tests.tui_support import immediate, terminal
from tui.history import prepare_archive_snapshot


class ArchiveIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.api = create_application(Path(self.folder.name))
        self.addCleanup(self.api.runtime.close)
        self.accounts = self.api.get("accounts")
        self.gemini = self.accounts.add("gemini", "Gemini")

    def test_active_account_is_chosen_independently_per_provider(self):
        second = self.accounts.add("chatgpt", "Other GPT")
        self.accounts.select(second)
        self.accounts.select(self.gemini)
        self.assertEqual({x["key"] for x in self.accounts.active_accounts()}, {second, self.gemini})
        self.accounts.select("chatgpt/default")
        self.assertEqual(
            {x["key"] for x in self.accounts.active_accounts()}, {"chatgpt/default", self.gemini}
        )
        self.accounts.set_enabled("gemini", False)
        self.assertEqual([x["key"] for x in self.accounts.active_accounts()], ["chatgpt/default"])

    def test_new_chat_models_include_selected_accounts_but_existing_chat_does_not(self):
        self.accounts.select(self.gemini)
        for key, model in [("chatgpt/default", "gpt"), (self.gemini, "flash")]:
            self.accounts._catalogs[key] = (
                time.monotonic(),
                ModelCatalog(
                    FeatureState.AVAILABLE, (Model(model, model, (Choice("high", "High"),)),)
                ),
            )
        with terminal(account_service=self.accounts) as (view, _):
            view._read_in_background = immediate
            view.open_palette("models")
            self.assertEqual(
                len([x for x in view.palette_items if x["id"].startswith("model.")]), 2
            )
            view.palette_filtered = [
                next(x for x in view.palette_items if x["id"].startswith("model.account:"))
            ]
            view.execute_palette_item()
            self.assertEqual((view.account_key, view.model), ("chatgpt/default", "gpt"))
            view.conversation_id = "@chatgpt/default/one"
            view.open_palette("models")
            self.assertEqual([x["detail"].split(" · ")[0] for x in view.palette_items], ["chatgpt"])

    def test_general_list_refresh_cannot_move_known_notebook_chat_out_of_its_project(self):
        archive = self.api.get("archive")
        cid = "@" + self.gemini + "/c_chat"
        pid = "@" + self.gemini + "/notebooks%2Fabcdef"
        plain = {"id": cid, "provider_id": "gemini", "title": "Updated", "project_id": None}
        with terminal(account_service=self.accounts) as (view, _):
            view.sidebar_items = [dict(plain, project_id=pid, project_name="Notebook")]
            view.project_chats_loaded.add(pid)
            archive.put(self.gemini, "chats", "", {"items": [plain]})
            view.apply_archive_updates()
            self.assertEqual(view.sidebar_items[0]["project_id"], pid)
            view.list_history_page = lambda cursor: {"items": [plain], "cursor": None}
            with patch.object(view._reads, "submit", side_effect=lambda work: work() or True):
                view.refresh_sidebar()
            self.assertEqual(view.sidebar_items[0]["project_id"], pid)
            view.history_cursor = "next"
            view._read_in_background = immediate
            view.load_more_history()
            self.assertEqual(view.sidebar_items[0]["project_id"], pid)
            archive.put(
                self.gemini,
                "chats",
                "",
                {"items": [dict(plain, metadata={"project_membership_known": True})]},
            )
            view.apply_archive_updates()
            self.assertIsNone(view.sidebar_items[0]["project_id"])

    def test_stale_sync_cannot_erase_a_completed_visible_reply(self):
        archive = self.api.get("archive")
        with terminal(account_service=self.accounts) as (view, _):
            view.conversation_id = "@chatgpt/default/one"
            view.messages = [
                {"id": "old", "role": "assistant", "text": "Earlier"},
                {"role": "user", "text": "New question", "local_pending": True},
                {"role": "reasoning", "text": "Published summary", "local_pending": True},
                {"role": "assistant", "text": "New completed reply", "local_pending": True},
            ]
            view._save_active_tab()
            view.input.text = "Next draft"
            archive.put(
                "chatgpt/default",
                "conversation",
                "one",
                {"messages": [{"id": "old", "role": "assistant", "text": "Earlier"}]},
            )
            archive._state(("chatgpt/default", "conversation", "one"), "ready")
            view.apply_archive_updates()
            self.assertEqual(
                [m["text"] for m in view.messages],
                ["Earlier", "New question", "Published summary", "New completed reply"],
            )
            self.assertEqual(view.input.text, "Next draft")

    def test_archive_revision_refreshes_only_the_resident_transcript_window(self):
        archive = self.api.get("archive")
        account, remote = self.gemini, "long-window"
        history = [
            {"id": f"m{i}", "role": "assistant", "text": f"message-{i}"} for i in range(1000)
        ]
        archive.put(account, "conversation", remote, {"title": "Long", "messages": history})
        archive._state((account, "conversation", remote), "ready")

        with terminal(account_service=self.accounts) as (view, _):
            view.account_key = account
            window = {"start": 400, "end": 480, "total": 1000}
            visible = [
                dict(item, _archive_ordinal=i) for i, item in enumerate(history[400:480], 400)
            ]
            tab = view.tabs[0]
            tab.update(
                conversation_id=f"@{account}/{remote}",
                account_key=account,
                history_window=window,
                history_version="old-version",
                archive_version=0,
                messages=visible,
            )
            view.messages = visible
            record = {
                key: tab.get(key)
                for key in (
                    "conversation_id",
                    "account_key",
                    "busy",
                    "archive_version",
                    "history_window",
                )
            }
            snapshot = prepare_archive_snapshot(
                archive,
                self.accounts.active_accounts(),
                [(tab, record)],
                [],
                [],
                account,
                archive.revision,
            )
            with patch.object(archive, "get", wraps=archive.get) as full_read:
                view.apply_archive_updates(snapshot)

        self.assertFalse(
            any(call.args[1:3] == ("conversation", remote) for call in full_read.call_args_list)
        )
        self.assertEqual(
            [item["_archive_ordinal"] for item in view.messages], list(range(400, 480))
        )
        self.assertEqual(tab["history_window"], window)

    def test_sync_updates_keep_draft_and_do_not_restore_deleted_first_page_rows(self):
        archive = self.api.get("archive")
        with terminal(account_service=self.accounts) as (view, _):
            view.input.text = "keep my draft"
            archive.put(
                "chatgpt/default",
                "chats",
                "",
                {"items": [{"id": "@chatgpt/default/one", "title": "Old"}]},
            )
            view.apply_archive_updates()
            self.assertTrue(view.sidebar_items)
            archive.put("chatgpt/default", "chats", "", {"items": []})
            view.apply_archive_updates()
            self.assertEqual(view.sidebar_items, [])
            self.assertEqual(view.input.text, "keep my draft")

    def test_opening_other_provider_chat_uses_its_preference_for_cached_and_cold_history(self):
        self.accounts.call(
            asyncio.to_thread(lambda: None)
        )  # Start real provider/executor threads before the UI-thread mock.
        self.accounts.select(self.gemini)
        key = "chatgpt/default"
        self.accounts.remember(key, "preferred-gpt", "high")
        self.accounts._catalogs[key] = (
            time.monotonic(),
            ModelCatalog(
                FeatureState.AVAILABLE,
                (
                    Model("old-gpt", "Old"),
                    Model("preferred-gpt", "Preferred", (Choice("high", "High"),)),
                ),
            ),
        )
        body = {"id": "@chatgpt/default/chat", "model": "old-gpt", "messages": []}
        for cached in (body, None):
            with (
                self.subTest(cached=cached is not None),
                terminal(account_service=self.accounts) as (view, _),
            ):
                view._read_in_background = immediate
                with (
                    patch.object(self.accounts, "cached", return_value=cached),
                    patch.object(self.accounts, "load", return_value=body),
                    patch.object(
                        view._reads, "submit", side_effect=lambda target: target() or True
                    ),
                ):
                    view.resume_conversation(body["id"])
                self.assertEqual(view.account_key, key)
                self.assertEqual(view.tabs[view.active_tab]["account_key"], key)
                self.assertEqual((view.model, view.effort), ("preferred-gpt", "high"))
                view.open_palette("models")
                self.assertEqual(
                    {i["id"] for i in view.palette_items},
                    {"model.select:old-gpt", "model.select:preferred-gpt"},
                )

    def test_unrelated_background_operation_does_not_block_sending(self):
        with terminal() as (view, _):
            view.background_jobs = 1
            view.input.text = "Hello"
            sent = Mock(return_value={"conversation_id": "new"})
            view.send_prompt = sent
            with patch(
                "tui.chat.composer.threading.Thread",
                side_effect=lambda **kw: Mock(
                    start=lambda: kw["target"](*kw.get("args", ()), **kw.get("kwargs", {}))
                ),
            ):
                view.submit_prompt()
            sent.assert_called_once()
            self.assertEqual(view.input.text, "")

    def test_extension_disable_preserves_archive_and_other_services(self):
        archive = self.api.get("archive")
        archive.put("chatgpt/default", "conversation", "one", {"title": "Saved"})
        self.api.get("extensions").disable("archive")
        self.assertIsNone(self.accounts.archive())
        self.assertTrue(self.api.get("workspaces"))
        self.api.get("extensions").enable("archive")
        self.assertEqual(
            self.api.get("archive").get("chatgpt/default", "conversation", "one")["title"], "Saved"
        )

    def test_cold_load_allows_another_tab_and_applies_only_to_its_owner(self):
        entered, release = threading.Event(), threading.Event()
        with terminal(account_service=self.accounts) as (view, _):
            owner = view.tabs[0]

            def load(remote):
                entered.set()
                release.wait(2)
                return {
                    "title": "Loaded",
                    "messages": [{"role": "assistant", "text": "Original tab"}],
                }

            view.load_conversation = load
            view.resume_conversation("@chatgpt/default/one")
            self.assertTrue(entered.wait(1))
            self.assertFalse(view.loading_conversation)
            self.assertTrue(owner["syncing"])
            self.assertEqual(view.conversation_id, "@chatgpt/default/one")
            view.new_tab()
            self.assertEqual(view.active_tab, 1)
            self.assertFalse(view.loading_conversation)
            view.input.text = "new draft"
            release.set()
            until = time.monotonic() + 3
            while owner.get("syncing") and time.monotonic() < until:
                time.sleep(0.01)
            self.assertFalse(owner["loading"])
            self.assertEqual(owner["title"], "Loaded")
            self.assertEqual(view.input.text, "new draft")
            self.assertIsNone(view.conversation_id)
            self.assertEqual(view.messages, [])

    def test_completed_reply_does_not_turn_into_request_failure_if_archive_write_fails(self):
        from providers import ConversationBound, ReplaceText

        async def stream(request):
            yield ReplaceText("Answer")
            yield ConversationBound("one")

        self.accounts._providers[self.gemini] = Mock(stream=stream, close=AsyncMock())
        archive = self.api.get("archive")
        archive.repository.append_turn = Mock(side_effect=OSError("disk full"))
        result = self.accounts.send(self.gemini, None, "Question", [], Mock(), Mock())
        self.assertTrue(result["conversation_id"].endswith("/one"))
        self.assertTrue(result["cache_warning"])

    def test_successful_file_send_keeps_metadata_without_archiving_file_content(self):
        from providers import ConversationBound, ReplaceText

        root = Path(self.folder.name)
        source = root / "notes.txt"
        source.write_text("Research source")

        async def stream(request):
            self.assertNotIn("path", request.attachments[0])
            yield ReplaceText("Answer")
            yield ConversationBound("one")

        provider = Mock(
            stream=stream,
            close=AsyncMock(),
            capabilities=frozenset({Capability.ATTACHMENTS}),
        )
        provider.attachment_policy.return_value = {"max_files": 10, "max_bytes": 1024}
        self.accounts._providers["chatgpt/default"] = provider
        self.accounts._attachment_paths["chatgpt/default", "file1"] = str(source)
        self.accounts.send(
            "chatgpt/default",
            None,
            "Question",
            [{"id": "file1", "name": "notes.txt"}],
            Mock(),
            Mock(),
        )
        archive = self.api.get("archive")
        resource = archive.resources("chatgpt/default", "conversation", "one")[0]
        self.assertEqual(resource["name"], "notes.txt")
        self.assertNotIn("path", resource)
        self.assertNotIn("sha256", resource)
        self.assertEqual(list(archive.files.root.glob("??/" + "?" * 64)), [])
        self.assertTrue(source.exists())

    def test_v5_migration_removes_legacy_uploaded_bodies_and_keeps_resource_metadata(self):

        archive = self.api.get("archive")
        repository_before = archive.repository
        relative, digest = archive.files.save(b"legacy uploaded body")
        archive.put(
            "chatgpt/a",
            "conversation",
            "chat",
            {
                "messages": [
                    {
                        "role": "user",
                        "resources": [
                            {
                                "id": "file-1",
                                "name": "notes.txt",
                                "mime": "text/plain",
                                "size": 20,
                                "path": relative,
                                "sha256": digest,
                            }
                        ],
                    }
                ]
            },
        )
        archive.put(
            "gemini/a",
            "container",
            "project",
            {
                "id": "project",
                "resources": [
                    {
                        "id": "source-1",
                        "name": "reference.pdf",
                        "mime": "application/pdf",
                        "path": relative,
                        "sha256": digest,
                    }
                ],
            },
        )
        with closing(sqlite3.connect(repository_before.path)) as db:
            db.executemany(
                "INSERT INTO resources(account,owner_kind,owner,ordinal,remote,kind,name,mime,uri,path,metadata) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    (
                        "gemini/a",
                        "container",
                        f"project-{index}",
                        0,
                        None,
                        "file",
                        "reference.pdf",
                        "application/pdf",
                        None,
                        relative,
                        encode({"id": f"source-{index}", "path": relative, "sha256": digest}),
                    )
                    for index in range(300)
                ),
            )
            for table in (
                "pending_branches",
                "response_variant_groups",
                "response_variants",
                "superseded_messages",
            ):
                db.execute(f"DROP TABLE {table}")
            db.execute("UPDATE schema_version SET version=4")
            db.commit()

        archive.close()
        repository = SQLiteRepository(repository_before.path)
        upgraded = Archive(repository, Files(archive.files.root))
        self.addCleanup(upgraded.close)
        resource = upgraded.get("chatgpt/a", "conversation", "chat")["messages"][0]["resources"][0]
        self.assertEqual(
            {key: resource[key] for key in ("id", "name", "mime", "size")},
            {"id": "file-1", "name": "notes.txt", "mime": "text/plain", "size": 20},
        )
        self.assertNotIn("path", resource)
        self.assertNotIn("sha256", resource)
        self.assertFalse((archive.files.root / relative).exists())
        project_resource = upgraded.get("gemini/a", "container", "project")["resources"][0]
        self.assertNotIn("path", project_resource)
        self.assertNotIn("sha256", project_resource)
        self.assertIsNone(repository.resources("gemini/a", "container", "project")[0].get("path"))
        with closing(sqlite3.connect(repository.path)) as db:
            self.assertIsNone(
                db.execute(
                    "SELECT path FROM resources WHERE account=? AND owner_kind=? AND owner=?",
                    ("gemini/a", "container", "project"),
                ).fetchone()[0]
            )
        self.assertFalse(repository.uploaded_file_cleanup_pending())
        with closing(sqlite3.connect(repository.path)) as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM resources WHERE path IS NOT NULL").fetchone()[0],
                0,
            )

    def test_cached_startup_revalidates_active_chat_without_resetting_draft(self):
        self.accounts.load = Mock(return_value={"id": "one", "messages": []})
        with terminal(account_service=self.accounts) as (view, _):
            view._read_in_background = immediate
            view.apply_archive_updates = Mock()
            view.conversation_id = "@chatgpt/default/one"
            view.input.text = "Continue drafting"
            view.refresh_open_conversation()
            self.accounts.load.assert_called_once_with(
                "@chatgpt/default/one", "chatgpt/default", force=True
            )
            view.apply_archive_updates.assert_called_once_with()
            self.assertEqual(view.input.text, "Continue drafting")
            self.assertFalse(view.loading_conversation)

    def test_opening_cached_chat_starts_a_forced_provider_refresh_in_background(self):
        conversation_id = f"@{self.gemini}/c_cached"
        self.accounts.cached_window = Mock(
            return_value={
                "id": conversation_id,
                "title": "Cached chat",
                "messages": [],
                "history_window": {"start": 0, "end": 0, "total": 0},
            }
        )
        self.accounts.load = Mock(return_value={"id": conversation_id, "messages": []})
        with terminal(account_service=self.accounts) as (view, _):
            view._read_in_background = immediate
            view._dispatch_ui = lambda callback: callback()
            with patch.object(view._reads, "submit", side_effect=lambda work: (work(), True)[1]):
                view.resume_conversation(conversation_id)

        self.accounts.load.assert_called_once_with(
            conversation_id, self.gemini, force=True, window=True
        )

    def test_active_archive_pointer_survives_restart_and_can_be_cleared(self):
        archive = self.api.get("archive")
        archive.put(
            self.gemini, "conversation", "one", {"id": "one", "title": "Saved", "messages": []}
        )
        self.accounts.set_active(self.gemini, "one")
        self.assertEqual(self.accounts.cached(self.gemini)["title"], "Saved")
        self.accounts.set_active(self.gemini, None)
        self.assertIsNone(self.accounts.cached(self.gemini))
        self.assertEqual(archive.conversations(self.gemini)[0]["title"], "Saved")

    def test_sidebar_icons_open_labels_and_project_names_are_separate_fragments(self):
        with terminal(account_service=self.accounts) as (view, _):
            view.sidebar_items = [
                {"id": "@chatgpt/default/one", "provider_id": "chatgpt", "title": "GPT chat"},
                {"id": "@" + self.gemini + "/two", "provider_id": "gemini", "title": "Gemini chat"},
            ]
            view.project_items = {
                "@chatgpt/default/project": {"name": "Research", "provider_id": "chatgpt"}
            }
            view.tabs[0]["conversation_id"] = "@chatgpt/default/one"
            fragments = list(view.render_sidebar())
            self.assertTrue(
                any(
                    style == "class:provider.gemini" and text == "✦ "
                    for style, text, *_ in fragments
                )
            )
            self.assertTrue(
                any(
                    style == "class:provider.chatgpt" and text == "◎ "
                    for style, text, *_ in fragments
                )
            )
            self.assertTrue(
                any(
                    "class:sidebar.open" in style and "GPT chat" in text
                    for style, text, *_ in fragments
                )
            )
            self.assertFalse(any("@chatgpt/" in text for _, text, *_ in fragments))
            self.assertTrue(any("class:tab.open" in style for style, _, *_ in view.render_tabs()))

    def test_read_returns_saved_conversation_while_remote_is_slow(self):
        archive = self.api.get("archive")
        archive.put(
            self.gemini, "conversation", "one", {"id": "one", "title": "Saved", "messages": []}
        )
        release = threading.Event()

        async def get_chat(remote):
            await asyncio.to_thread(release.wait, 2)
            from providers.models import Conversation, Origin

            return Conversation(Origin("gemini", self.gemini.split("/")[1], remote), "Remote")

        self.accounts._providers[self.gemini] = Mock(get_chat=get_chat, close=AsyncMock())
        started = time.monotonic()
        try:
            self.assertEqual(self.accounts.load("one", self.gemini)["title"], "Saved")
            self.assertLess(time.monotonic() - started, 0.1)
        finally:
            release.set()
            until = time.monotonic() + 3
            while archive._pending and time.monotonic() < until:
                time.sleep(0.01)
