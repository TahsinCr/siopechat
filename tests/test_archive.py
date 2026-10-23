"""Archive isolation, revalidation, bounded memory and account/model UX."""

import asyncio
import hashlib
import io
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from extensions.archive import repository as archive_repository
from extensions.archive.repository import Files, SQLiteRepository
from extensions.archive.sync import Archive
from providers import Capability
from providers.models import Choice, FeatureState, Model, ModelCatalog
from tests.account_fixture import configured_application as create_application
from tests.test_cli_interaction import terminal
from tests.test_ui_controls import immediate


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.repository = SQLiteRepository(self.root / "archive.sqlite3")
        self.archive = Archive(self.repository, Files(self.root / "files"), memory_bytes=2048)
        self.addCleanup(self.archive.close)

    def wait(self):
        until = time.monotonic() + 10
        while any(x["state"] == "syncing" for x in self.archive.states()):
            self.assertLess(time.monotonic(), until)
            time.sleep(0.005)

    def test_account_isolation_roundtrip_reasoning_and_resources(self):
        path, digest = self.archive.files.save(b"file contents")
        value = {
            "id": "one",
            "title": "A",
            "project_id": "@gpt/a/project",
            "messages": [
                {
                    "id": "1",
                    "role": "assistant",
                    "channel": "analysis",
                    "text": "Visible summary",
                    "metadata": {"source": "provider"},
                },
                {
                    "id": "2",
                    "role": "assistant",
                    "text": "Answer",
                    "content": [
                        {
                            "kind": "image",
                            "resource": {
                                "id": "img",
                                "name": "image",
                                "path": path,
                                "sha256": digest,
                            },
                        }
                    ],
                },
            ],
        }
        self.archive.put("chatgpt/a", "conversation", "one", value)
        self.assertEqual(self.archive.get("chatgpt/a", "conversation", "one"), value)
        self.assertIsNone(self.archive.get("gemini/a", "conversation", "one"))
        self.assertEqual((self.root / "files" / path).read_bytes(), b"file contents")
        with closing(sqlite3.connect(self.repository.path)) as db:
            self.assertEqual(
                db.execute("SELECT channel FROM messages ORDER BY ordinal").fetchone()[0],
                "analysis",
            )
            self.assertEqual(db.execute("SELECT path FROM resources").fetchone()[0], path)
            self.assertEqual(db.execute("SELECT count(*) FROM memberships").fetchone()[0], 1)
        self.assertFalse(self.repository.put("chatgpt/a", "conversation", "one", value))
        fresh = SQLiteRepository(self.repository.path)
        self.assertEqual(fresh.get("chatgpt/a", "conversation", "one"), value)

    def test_conversation_digest_does_not_materialize_a_second_full_json_record(self):
        value = {
            "title": "Long history",
            "messages": [
                {"id": str(index), "role": "assistant", "text": "answer" * 200}
                for index in range(20)
            ],
        }
        expected = hashlib.sha256(archive_repository.encode(value).encode()).hexdigest()
        original_encode = archive_repository.encode

        with patch("extensions.archive.repository.encode", wraps=original_encode) as encode_value:
            self.repository.put("chatgpt/a", "conversation", "long", value)

        self.assertFalse(any(call.args[0] is value for call in encode_value.call_args_list))
        with closing(sqlite3.connect(self.repository.path)) as db:
            digest = db.execute(
                "SELECT digest FROM records WHERE account=? AND kind=? AND remote=?",
                ("chatgpt/a", "conversation", "long"),
            ).fetchone()[0]
        self.assertEqual(digest, expected)
        self.assertFalse(self.repository.put("chatgpt/a", "conversation", "long", value))

    def test_cached_read_returns_before_remote_and_single_flight(self):
        self.archive.put("gemini/a", "conversation", "one", {"messages": [], "title": "old"})
        entered, release = threading.Event(), threading.Event()

        def fetch():
            entered.set()
            self.assertTrue(release.wait(3))
            return {"messages": [], "title": "new"}

        start = time.monotonic()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(
                pool.map(
                    lambda _: self.archive.read("gemini/a", "conversation", "one", fetch), range(16)
                )
            )
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertTrue(
            entered.wait(5),
            f"archive closed={self.archive.closed}, deleted={self.archive._deleted}, "
            f"pending={tuple(self.archive._pending)}, states={self.archive.states()}",
        )
        self.assertEqual({v["title"] for v in results}, {"old"})
        self.assertEqual(len(self.archive._pending), 1)
        release.set()
        self.wait()
        self.assertEqual(self.archive.get("gemini/a", "conversation", "one")["title"], "new")

    def test_cold_record_syncs_when_monotonic_uptime_is_under_one_minute(self):
        from unittest.mock import patch

        with patch("extensions.archive.sync.time.monotonic", return_value=15):
            value = self.archive.read(
                "gemini/a", "conversation", "cold", lambda: {"title": "remote"}
            )
        self.assertEqual(value, {"title": "remote"})
        self.assertEqual(self.archive.get("gemini/a", "conversation", "cold"), value)

    def test_late_read_cannot_overwrite_completed_send(self):
        self.archive.put("chatgpt/a", "conversation", "one", {"title": "old"})
        release = threading.Event()
        self.archive.read(
            "chatgpt/a", "conversation", "one", lambda: release.wait(3) and {"title": "stale"}
        )
        self.archive.put("chatgpt/a", "conversation", "one", {"title": "sent"})
        release.set()
        self.wait()
        self.assertEqual(self.archive.get("chatgpt/a", "conversation", "one")["title"], "sent")

    def test_errors_preserve_saved_value_and_explicit_refresh_retries(self):
        self.archive.put("gemini/a", "container", "n", {"name": "Notebook"})
        calls = Mock(side_effect=ValueError("offline"))
        value = self.archive.read("gemini/a", "container", "n", calls)
        self.assertEqual(value["name"], "Notebook")
        self.wait()
        self.assertEqual(self.archive.states()[-1]["state"], "error")
        calls.side_effect = None
        calls.return_value = {"name": "Updated"}
        self.archive.refresh()
        self.wait()
        self.assertEqual(self.archive.get("gemini/a", "container", "n")["name"], "Updated")
        with closing(sqlite3.connect(self.repository.path)) as db:
            self.assertEqual(db.execute("SELECT kind FROM containers").fetchone()[0], "notebook")

    def test_refresh_of_a_failed_cold_record_never_waits_in_the_caller(self):
        release = threading.Event()
        fetch = Mock(side_effect=ValueError("offline"))
        with self.assertRaises(ValueError):
            self.archive.read("gemini/a", "conversation", "missing", fetch)
        self.wait()
        fetch.side_effect = lambda: release.wait(2) and {"title": "Recovered"}
        started = time.monotonic()
        try:
            self.archive.refresh()
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            release.set()
            self.wait()

    def test_hot_memory_is_bounded_and_temporary_data_is_not_archived(self):
        for i in range(25):
            self.archive.put("gemini/a", "conversation", str(i), {"text": "x" * 500})
        self.assertLessEqual(self.archive._size, 2048)
        self.assertLess(len(self.archive._hot), 25)
        self.assertEqual(self.archive.get("gemini/a", "conversation", "0")["text"], "x" * 500)
        self.archive.put(
            "gemini/a", "conversation", "private", {"temporary": True, "text": "secret"}
        )
        self.assertIsNone(self.repository.get("gemini/a", "conversation", "private"))


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
                "cli.chat.threading.Thread",
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
        from extensions.archive.repository import SQLiteRepository
        from extensions.archive.sync import Archive

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

    def test_cached_startup_revalidates_active_chat_without_resetting_draft(self):
        self.accounts.load = Mock(return_value={"id": "one", "messages": []})
        with terminal(account_service=self.accounts) as (view, _):
            view._read_in_background = immediate
            view.conversation_id = "@chatgpt/default/one"
            view.input.text = "Continue drafting"
            view.refresh_open_conversation()
            self.accounts.load.assert_called_once_with("@chatgpt/default/one", "chatgpt/default")
            self.assertEqual(view.input.text, "Continue drafting")
            self.assertFalse(view.loading_conversation)

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


class ArchiveMaintenanceTests(unittest.TestCase):
    def test_future_schema_is_rejected_without_modification(self):
        import sqlite3

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "future.db"
            from contextlib import closing

            with closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TABLE schema_version(version INTEGER PRIMARY KEY)")
                db.execute("INSERT INTO schema_version VALUES(99)")
                db.commit()
            with self.assertRaises(OSError):
                SQLiteRepository(path)
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(
                    db.execute("SELECT version FROM schema_version").fetchall(), [(99,)]
                )

    def test_shared_sql_references_survive_until_the_last_owner_is_deleted(self):
        import os
        import time

        with tempfile.TemporaryDirectory() as folder:
            repository = SQLiteRepository(Path(folder) / "archive.db")
            files = Files(Path(folder) / "files")
            relative, _ = files.save(b"shared content")
            os.utime(files.root / relative, (time.time() - 7200,) * 2)
            for remote in ("first", "second"):
                repository.put(
                    "gemini/account",
                    "conversation",
                    remote,
                    {"messages": [{"resources": [{"path": relative}]}]},
                )
            repository.delete("gemini/account", "conversation", "first")
            self.assertEqual(files.collect(repository.file_references()), [])
            repository.delete("gemini/account", "conversation", "second")
            self.assertEqual(files.collect(repository.file_references()), [relative])
            repository.close()

    def test_failed_content_fsync_does_not_publish_a_partial_archive_file(self):
        with tempfile.TemporaryDirectory() as folder:
            files = Files(Path(folder))
            with patch(
                "extensions.archive.repository.os.fsync", side_effect=OSError("disk failure")
            ):
                with self.assertRaises(OSError):
                    files.save(b"not durable")
            self.assertEqual([path for path in files.root.rglob("*") if path.is_file()], [])

    def test_streaming_file_save_uses_bounded_reads_and_deduplicates_content(self):
        class BoundedReader(io.BytesIO):
            def read(self, size=-1):
                if size <= 0 or size > 64 * 1024:
                    raise AssertionError("archive read exceeded its bounded chunk size")
                return super().read(size)

        data = b"attachment" * (96 * 1024)
        with tempfile.TemporaryDirectory() as folder:
            files = Files(Path(folder) / "files")

            relative, digest = files.save_stream(BoundedReader(data))
            duplicate, duplicate_digest = files.save_stream(BoundedReader(data))

            self.assertEqual(relative, duplicate)
            self.assertEqual(digest, hashlib.sha256(data).hexdigest())
            self.assertEqual(duplicate_digest, digest)
            self.assertEqual((files.root / relative).read_bytes(), data)
            self.assertEqual(
                sum(path.is_file() for path in files.root.rglob("*")),
                1,
            )

    def test_gc_preserves_shared_references_recent_saves_and_unknown_files(self):
        import os
        import time

        with tempfile.TemporaryDirectory() as folder:
            files = Files(Path(folder))
            live, _ = files.save(b"shared")
            orphan, _ = files.save(b"orphan")
            recent, _ = files.save(b"recent")
            for item in (live, orphan):
                os.utime(files.root / item, (time.time() - 7200,) * 2)
            (files.root / "notes.txt").write_text("keep")
            self.assertEqual(files.collect({live}), [orphan])
            self.assertTrue((files.root / orphan).exists())
            self.assertEqual(files.collect({live}, dry_run=False), [orphan])
            self.assertTrue((files.root / live).exists())
            self.assertTrue((files.root / recent).exists())
            self.assertTrue((files.root / "notes.txt").exists())
