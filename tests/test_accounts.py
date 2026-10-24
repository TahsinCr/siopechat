"""Account identity, cache isolation and terminal routing without real sessions."""

import asyncio
import json
import os
import subprocess
import tempfile
import time
import unittest
from concurrent.futures import CancelledError, ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from accounts import Accounts, identity, origin
from providers import Capability, ConversationBound, ReplaceText, TextDelta
from providers.errors import AccountPaused
from providers.models import (
    Content,
    Conversation,
    FeatureState,
    Message,
    Model,
    ModelCatalog,
    Origin,
    Page,
)
from tests.account_fixture import configured_application as create_application


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.api = create_application(Path(self.folder.name))
        self.addCleanup(self.api.runtime.close)
        self.accounts = self.api.get("accounts")
        self.gemini = self.accounts.add("gemini", "Personal Gemini")

    def test_legacy_account_json_moves_to_archive_before_source_is_removed(self):
        backend = self.accounts.cache(self.gemini)
        remote = "legacy-cache-chat"
        conversation_path = backend.conversation_cache_path(remote)
        backend.atomic_json_write(
            conversation_path,
            {
                "id": remote,
                "title": "Local history",
                "messages": [{"id": "m1", "role": "user", "text": "saved"}],
            },
        )
        backend.set_active_conversation(remote)

        self.accounts._migrate_legacy_account_cache()

        archive = self.accounts.archive()
        self.assertEqual(archive.get(self.gemini, "conversation", remote)["title"], "Local history")
        self.assertEqual(archive.get(self.gemini, "pointer", "active"), {"remote": remote})
        self.assertFalse(conversation_path.exists())
        self.assertFalse(backend.active_conversation_path().exists())

    def test_legacy_deleted_marker_migrates_as_tombstone(self):
        backend = self.accounts.cache(self.gemini)
        remote = "legacy-deleted-chat"
        backend.cache_conversation(
            {"id": remote, "title": "Deleted", "messages": [{"role": "user", "text": "old"}]}
        )
        marker = backend.conversation_cache_path(remote).with_suffix(".deleted")
        backend.mark_conversation_deleted(remote)

        self.accounts._migrate_legacy_account_cache()

        self.assertTrue(self.accounts.archive().deleted(self.gemini, "conversation", remote))
        self.assertFalse(marker.exists())
        self.assertIsNone(self.accounts.archive().get(self.gemini, "conversation", remote))

    def test_legacy_cache_uses_account_database_when_archive_is_disabled(self):
        backend = self.accounts.cache(self.gemini)
        remote = "fallback-cache-chat"
        conversation_path = backend.conversation_cache_path(remote)
        backend.atomic_json_write(
            conversation_path,
            {
                "id": remote,
                "title": "Fallback",
                "messages": [{"role": "assistant", "text": "kept"}],
            },
        )

        with patch.object(self.accounts, "archive", return_value=None):
            self.accounts._migrate_account_cache(self.gemini)

        cached = self.accounts.repository.cache_get(self.gemini, "conversation", remote)
        self.assertEqual(cached["title"], "Fallback")
        self.assertFalse(conversation_path.exists())

    def test_shutdown_drains_active_pointer_to_archive_before_fallback_is_selected(self):
        self.accounts.set_active(self.gemini, "chat-before-close")
        entered, release = Event(), Event()
        set_active = self.accounts.set_active

        def pause_write(key, value):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("active pointer write was not released")
            return set_active(key, value)

        with patch.object(self.accounts, "set_active", side_effect=pause_write):
            self.accounts.queue_active(self.gemini, "chat-selected-last")
            self.assertTrue(entered.wait(2))
            closer = ThreadPoolExecutor(max_workers=1)
            closing = closer.submit(self.accounts.close)
            try:
                release.set()
                closing.result(5)
            finally:
                release.set()
                closer.shutdown(wait=True)

        self.api.runtime.close()
        restarted = create_application(Path(self.folder.name))
        try:
            fresh_accounts = restarted.get("accounts")
            self.assertEqual(fresh_accounts.active_conversation(self.gemini), "chat-selected-last")
        finally:
            restarted.runtime.close()

    def test_project_refresh_reports_each_provider_as_soon_as_it_finishes(self):
        fast_key, slow_key = "chatgpt/fast", "claude/slow"
        account_items = [
            {"key": fast_key, "provider": "chatgpt", "label": "Fast"},
            {"key": slow_key, "provider": "claude", "label": "Slow"},
        ]
        slow_release, fast_ready = Event(), Event()
        reported = []

        def list_projects(key, cursor, force):
            if key == slow_key:
                slow_release.wait(2)
            return {"items": [{"id": f"@{key}/project", "name": key}], "cursor": None}

        service = SimpleNamespace(
            items=lambda: account_items,
            enabled=lambda _provider: True,
            status=lambda _key: {"saved_snapshot": True},
            archive=lambda: None,
            list_projects=list_projects,
        )

        def on_account(account, _result):
            reported.append(account["key"])
            if account["key"] == fast_key:
                fast_ready.set()

        with ThreadPoolExecutor(max_workers=1) as caller:
            pending = caller.submit(Accounts.all_projects, service, on_account=on_account)
            self.assertTrue(fast_ready.wait(1))
            self.assertFalse(pending.done())
            slow_release.set()
            result = pending.result(2)

        self.assertCountEqual(reported, [fast_key, slow_key])
        self.assertCountEqual(
            [item["id"] for item in result["items"]],
            [f"@{fast_key}/project", f"@{slow_key}/project"],
        )

    def test_account_status_does_not_require_a_browser_process(self):
        with (
            patch.object(self.accounts, "browser_connected", side_effect=AssertionError("browser")),
            patch(
                "providers.chatgpt.session.BrowserSession.launch",
                side_effect=AssertionError("browser"),
            ),
        ):
            self.assertEqual(self.accounts.status(self.gemini)["transport"], "http")
            self.assertFalse(self.accounts.status(self.gemini)["authenticated"])

    def test_project_details_use_local_cache_when_no_provider_session_is_saved(self):
        archive = self.accounts.archive()
        remote = "project-cache-only"
        project_id = identity(self.gemini, remote)
        cached = {
            "id": project_id,
            "remote_id": remote,
            "name": "Cached project",
            "instructions": "Keep this local copy available.",
            "resources": [],
        }
        archive.put(self.gemini, "container", remote, cached)

        with patch.object(self.accounts, "provider", side_effect=AssertionError("network")):
            result = self.accounts.project_details(self.gemini, project_id)

        self.assertEqual(result["name"], "Cached project")
        self.assertTrue(result["requires_auth"])

    def test_chat_record_shares_provider_metadata_instead_of_deep_copying_it(self):
        nested_metadata = {"details": {"items": ["source"]}}
        content = Content("text", "A large body", metadata=nested_metadata)
        chat = Conversation(
            Origin("gemini", self.gemini.split("/", 1)[1], "remote-chat"),
            "Memory check",
            (Message("m1", "assistant", (content,)),),
        )

        record = self.accounts.chat_record(self.gemini, chat)
        archived_content = record["messages"][0]["content"][0]

        self.assertEqual(archived_content, asdict(content))
        self.assertIs(archived_content["metadata"], nested_metadata)

    def test_runtime_shutdown_does_not_cancel_provider_loop_cleanup(self):
        self.accounts.call(asyncio.sleep(0))
        thread = self.accounts._thread

        self.api.runtime.close()

        self.assertTrue(self.accounts.closed)
        self.assertFalse(thread.is_alive())

    def test_shutdown_drains_background_persistence_before_stopping_loop(self):
        persisted = Event()

        async def write_later():
            await asyncio.sleep(0.02)
            persisted.set()

        async def schedule_write():
            asyncio.create_task(write_later(), name="siope-persist:test")

        self.accounts.call(schedule_write())
        self.api.runtime.close()

        self.assertTrue(persisted.is_set())

    def test_shutdown_keeps_a_live_event_loop_open_for_retry(self):
        loop, thread = Mock(), Mock()
        loop.is_running.return_value = True
        loop.is_closed.return_value = False
        thread.is_alive.side_effect = (True, True)
        self.accounts._loop, self.accounts._thread = loop, thread

        def close_coroutine(coroutine, *, _closing=False):
            coroutine.close()

        try:
            with (
                patch.object(self.accounts, "call", side_effect=close_coroutine),
                self.assertRaisesRegex(RuntimeError, "remains open"),
            ):
                self.accounts.close()
            loop.close.assert_not_called()
            loop.call_soon_threadsafe.assert_called_once_with(loop.stop)
            self.assertIs(self.accounts._loop, loop)
            self.assertIs(self.accounts._thread, thread)
        finally:
            self.accounts._loop = self.accounts._thread = None

    def test_shutdown_timeout_does_not_stop_or_close_the_live_event_loop(self):
        loop, thread = Mock(), Mock()
        loop.is_running.return_value = True
        loop.is_closed.return_value = False
        thread.is_alive.return_value = True
        self.accounts._loop, self.accounts._thread = loop, thread

        def time_out(coroutine, *, _closing=False):
            coroutine.close()
            raise TimeoutError("slow provider close")

        try:
            with (
                patch.object(self.accounts, "call", side_effect=time_out),
                self.assertRaisesRegex(RuntimeError, "remains open"),
            ):
                self.accounts.close()
            loop.call_soon_threadsafe.assert_not_called()
            loop.close.assert_not_called()
            thread.join.assert_not_called()
            self.assertIs(self.accounts._loop, loop)
        finally:
            self.accounts._loop = self.accounts._thread = None

    def test_verification_survives_closed_browser_and_account_service_restart(self):
        path = self.accounts.directory(self.gemini) / "cookies.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
        self.accounts._remember_verification(self.gemini)
        restarted = Accounts(self.api)
        with patch.object(restarted, "browser_connected", return_value=False):
            state = restarted.status(self.gemini)
            self.assertTrue(state["authenticated"])
            self.assertEqual(state["transport"], "http")
        restarted.credentials(self.gemini).delete()
        self.assertFalse(restarted.status(self.gemini)["authenticated"])

    def test_transport_failure_preserves_verification_but_auth_rejection_invalidates_it(self):
        path = self.accounts.directory(self.gemini) / "cookies.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
        self.accounts._remember_verification(self.gemini)
        provider = Mock()
        provider.models = AsyncMock(side_effect=RuntimeError("connection refused"))
        provider.close = AsyncMock()
        self.accounts._providers[self.gemini] = provider
        with self.assertRaisesRegex(RuntimeError, "connection refused"):
            self.accounts.models(self.gemini, force=True)
        self.assertTrue(self.accounts.status(self.gemini)["authenticated"])
        provider.models.side_effect = AccountPaused("Sign in required")
        with self.assertRaises(AccountPaused):
            self.accounts.models(self.gemini, force=True)
        self.assertFalse(self.accounts.status(self.gemini)["authenticated"])
        self.assertFalse(self.accounts.repository.get(self.gemini, "verification") is not None)

    def test_saved_model_catalog_cannot_verify_an_account(self):
        path = self.accounts.directory(self.gemini) / "cookies.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
        with patch.object(self.accounts, "browser_connected", return_value=True):
            state = self.accounts.status(self.gemini)
            self.assertFalse(state["authenticated"])
            self.assertEqual(state["transport"], "http")

    def test_catalog_single_flight_cache_and_account_local_disk_snapshot(self):
        catalog = ModelCatalog(FeatureState.AVAILABLE, (Model("gemini-test", "Gemini"),))

        async def discover(*, force=False):
            await asyncio.sleep(0.04)
            return catalog

        provider = Mock()
        provider.models = AsyncMock(side_effect=discover)
        provider.close = AsyncMock()
        self.accounts._providers[self.gemini] = provider
        with ThreadPoolExecutor(3) as pool:
            values = list(pool.map(lambda _: self.accounts.models(self.gemini), range(3)))
        self.assertEqual(provider.models.await_count, 1)
        self.assertTrue(all(v == catalog for v in values))
        self.assertEqual(self.accounts.models(self.gemini), catalog)
        self.assertEqual(provider.models.await_count, 1)
        self.accounts.models(self.gemini, force=True)
        self.assertEqual(provider.models.await_count, 2)
        self.assertFalse(provider.models.await_args_list[0].kwargs["force"])
        self.assertTrue(provider.models.await_args_list[1].kwargs["force"])
        fresh = Accounts(self.api)
        self.assertEqual(fresh.cached_models(self.gemini), catalog)
        self.assertIsNone(fresh.cached_models("chatgpt/default"))

    def test_background_preparation_skips_accounts_without_credentials(self):
        item = {"key": self.gemini, "enabled": True}
        with (
            patch.object(self.accounts, "items", return_value=[item]),
            patch("accounts.session_exists", return_value=False),
            patch.object(self.accounts, "provider", side_effect=AssertionError("no session")),
        ):
            self.assertEqual(self.accounts.prepare_enabled(), {})

    def test_background_preparation_reports_each_account_without_raising(self):
        item = {"key": self.gemini, "enabled": True}
        provider = Mock()
        provider.prepare = AsyncMock(side_effect=RuntimeError("offline"))
        provider.close = AsyncMock()
        with (
            patch.object(self.accounts, "items", return_value=[item]),
            patch("accounts.session_exists", return_value=True),
            patch.object(self.accounts, "provider", return_value=provider),
        ):
            failures = self.accounts.prepare_enabled()
        self.assertEqual(failures, {self.gemini: "offline"})
        provider.prepare.assert_awaited_once_with()

    def test_old_catalog_snapshot_is_not_restored_after_entitlement_schema_changes(self):
        from dataclasses import asdict

        catalog = ModelCatalog(FeatureState.AVAILABLE, (Model("old-locked", "Old"),))
        path = self.accounts.directory(self.gemini) / "models.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"account_key": self.gemini, "catalog": asdict(catalog)}))
        self.assertIsNone(Accounts(self.api).cached_models(self.gemini))

    def test_failed_refresh_preserves_last_usable_model_catalog(self):
        good = ModelCatalog(FeatureState.AVAILABLE, (Model("m", "Model"),))
        provider = Mock()
        provider.models = AsyncMock(return_value=good)
        provider.close = AsyncMock()
        self.accounts._providers[self.gemini] = provider
        self.accounts.models(self.gemini)
        provider.models.return_value = ModelCatalog(FeatureState.UNKNOWN, reason="offline")
        with self.assertRaisesRegex(RuntimeError, "offline"):
            self.accounts.models(self.gemini, force=True)
        self.assertIs(self.accounts.cached_models(self.gemini), good)

    def test_read_timeout_releases_catalog_single_flight_for_retry(self):
        async def stalled(*, force=False):
            await asyncio.sleep(10)

        provider = Mock()
        provider.models = AsyncMock(side_effect=stalled)
        provider.close = AsyncMock()
        self.accounts._providers[self.gemini] = provider
        with patch("accounts.MODEL_TIMEOUT", 0.02):
            with self.assertRaises(TimeoutError):
                self.accounts.models(self.gemini)
        self.assertNotIn(self.gemini, self.accounts._catalog_tasks)
        provider.models = AsyncMock(
            return_value=ModelCatalog(FeatureState.AVAILABLE, (Model("m", "M"),))
        )
        self.assertEqual(self.accounts.models(self.gemini).models[0].id, "m")
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            self.accounts.call(asyncio.sleep(10), timeout=0.02)
        self.assertLess(time.monotonic() - started, 1)

    def test_history_timeout_isolated_from_other_account(self):
        async def stalled(**kwargs):
            await asyncio.sleep(10)

        self.accounts.status = lambda key: {"saved_snapshot": True}
        for key in (self.gemini, "chatgpt/default"):
            provider = Mock()
            provider.close = AsyncMock()
            provider.list_chats = (
                AsyncMock(side_effect=stalled)
                if key == self.gemini
                else AsyncMock(
                    return_value=Page((Conversation(Origin("chatgpt", "default", "chat"), "GPT"),))
                )
            )
            self.accounts._providers[key] = provider
        with patch("accounts.HISTORY_TIMEOUT", 0.2):
            page = self.accounts.list_chats()
        self.assertEqual(len(page["items"]), 1)
        self.assertEqual(page["errors"], ["Personal Gemini"])

    def test_identity_and_private_settings_separate_accounts(self):
        self.assertNotEqual(identity("chatgpt/default", "same"), identity(self.gemini, "same"))
        self.assertEqual(origin(identity(self.gemini, "a/b")), (self.gemini, "a/b"))
        self.accounts.select(self.gemini)
        config = self.api.settings.snapshot()
        self.assertEqual(config["accounts"]["active"], self.gemini)
        self.assertNotIn("default", config["providers"])
        self.assertNotIn("cookie", json.dumps(config))

    def test_late_session_result_does_not_change_another_account(self):
        from tests.cli_support import terminal

        with terminal(session_action=lambda action: {"action": action}) as (view, _):
            pending = []
            view._read_in_background = lambda *args, **kw: pending.append(args)
            view.session_dialog.perform("verify_profile")
            read, success, _ = pending.pop()
            view.account_key = self.gemini
            view.session_state = {"account": "Gemini", "authenticated": False}
            view.session_action = lambda _: self.fail("must keep captured callback")
            self.assertEqual(read(), {"action": "verify_profile"})
            success({"authenticated": True})
            self.assertFalse(view.session_state["authenticated"])
            self.assertFalse(view.session_dialog.loading)

    def test_default_account_honors_debug_browser_override(self):
        self.accounts.cdp_url = "http://127.0.0.1:9333"
        self.assertEqual(self.accounts.session("chatgpt/default").cdp_url, self.accounts.cdp_url)
        self.assertNotEqual(self.accounts.session(self.gemini).cdp_url, self.accounts.cdp_url)

    def test_default_browser_launch_suppresses_child_output_from_terminal(self):
        with patch("accounts.subprocess.run", return_value=Mock(returncode=0)) as launch:
            self.assertEqual(
                self.accounts.session_action(self.gemini, "default_browser"), {"opened": True}
            )
        args, kwargs = launch.call_args
        self.assertEqual(args[0][1:4], ["-m", "webbrowser", "-t"])
        self.assertEqual(args[0][4], "https://gemini.google.com/")
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertIs(kwargs["stdout"], subprocess.DEVNULL)
        self.assertIs(kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(kwargs["timeout"], 15)
        if os.name == "nt":
            self.assertEqual(kwargs["creationflags"], 0x208)
        else:
            self.assertTrue(kwargs["start_new_session"])

    def test_default_browser_launch_failure_is_reported(self):
        with patch("accounts.subprocess.run", return_value=Mock(returncode=1)):
            with self.assertRaisesRegex(RuntimeError, "Tarayıcı açılamadı"):
                self.accounts.session_action(self.gemini, "default_browser")

    def test_accounts_without_saved_sessions_are_reported_instead_of_silently_omitted(self):
        self.accounts.status = lambda _: {"saved_snapshot": False}
        for key in ("chatgpt/default", self.gemini):
            provider = Mock()
            provider.capabilities = {Capability.PROJECTS}
            provider.close = AsyncMock()
            provider.list_chats = AsyncMock(side_effect=RuntimeError("No saved session"))
            provider.list_projects = AsyncMock(side_effect=RuntimeError("No saved session"))
            self.accounts._providers[key] = provider
        for page in (self.accounts.list_chats(), self.accounts.all_projects()):
            self.assertEqual(set(page["failed_accounts"]), {"chatgpt/default", self.gemini})
            self.assertEqual(len(page["errors"]), 2)
            self.assertEqual(page["items"], [])

    def test_authoritative_unassigned_chat_clears_remembered_notebook_membership(self):
        origin = Origin("gemini", self.gemini.split("/")[1], "c_chat")
        linked = Conversation(origin, "Chat", project_id="notebooks/abcdef")
        self.assertIsNotNone(self.accounts.chat_record(self.gemini, linked)["project_id"])
        plain = Conversation(origin, "Chat", metadata={"project_membership_known": True})
        self.assertIsNone(self.accounts.chat_record(self.gemini, plain)["project_id"])

    def test_mixed_list_filter_and_account_pagination(self):
        self.accounts.status = lambda _: {"saved_snapshot": True}
        for key in ("chatgpt/default", self.gemini):
            provider = Mock()
            provider.close = AsyncMock()
            provider.list_chats = AsyncMock(
                return_value=Page(
                    (Conversation(Origin(*key.split("/"), "same"), key),),
                    "50" if key.startswith("chatgpt") else None,
                )
            )
            self.accounts._providers[key] = provider
        page = self.accounts.list_chats()
        self.assertEqual(len({item["id"] for item in page["items"]}), 2)
        filtered = self.accounts.list_chats({"gemini"})
        self.assertEqual([item["provider_id"] for item in filtered["items"]], ["gemini"])
        self.accounts.list_chats(cursor=page["cursor"])
        self.accounts._providers["chatgpt/default"].list_chats.assert_awaited_with(
            cursor="50", limit=50
        )

    def test_new_accounts_never_import_legacy_chatgpt_conversations(self):
        legacy = Path(self.folder.name) / "legacy"
        (legacy / "conversations").mkdir(parents=True)
        (legacy / "active-conversation.json").write_text(json.dumps({"conversation_id": "gpt-id"}))
        (legacy / "conversations" / "gpt-id.json").write_text(
            json.dumps({"id": "gpt-id", "messages": []})
        )
        with patch("backend.legacy_data_dir", return_value=legacy):
            cache = self.accounts.cache(self.gemini)
            self.assertIsNone(cache.load_cached_conversation())
            self.assertIsNone(cache.load_cached_conversation("gpt-id"))
            # Even a pointer contaminated by the previous fallback cannot import GPT data.
            cache.set_active_conversation("gpt-id")
            self.assertIsNone(cache.load_cached_conversation())
            additional = self.accounts.add("chatgpt", "Second GPT")
            self.assertIsNone(self.accounts.cache(additional).load_cached_conversation())
            self.assertEqual(
                self.accounts.cache("chatgpt/default").load_cached_conversation()["id"], "gpt-id"
            )
            cache.cache_conversation(
                {"id": "c_gemini", "messages": [{"role": "assistant", "text": "Answer"}]}
            )
            self.assertEqual(cache.load_cached_conversation()["id"], "c_gemini")

    def test_send_cache_and_temporary_mode_are_account_scoped(self):
        async def stream(request):
            yield ReplaceText("Answer")
            yield ConversationBound("same")

        provider = Mock()
        provider.close = AsyncMock()
        provider.stream = stream
        self.accounts._providers[self.gemini] = provider
        result = self.accounts.send(self.gemini, None, "Q", [], Mock(), Mock())
        self.assertEqual(origin(result["conversation_id"])[0], self.gemini)
        cached = self.accounts.cached(self.gemini, "same")
        self.assertEqual(cached["provider_id"], "gemini")
        self.assertIsNone(self.accounts.cached("chatgpt/default", "same"))
        self.accounts.cache(self.gemini).conversation_cache_path("same").unlink(missing_ok=True)
        self.accounts.send(self.gemini, None, "Private", [], Mock(), Mock(), temporary=True)
        # Removing the legacy JSON snapshot must not delete the durable archive.
        archived = self.accounts.cached(self.gemini, "same")
        self.assertEqual(archived["messages"][0]["text"], "Q")
        self.assertNotIn("Private", str(archived))

    def test_view_callback_failure_does_not_truncate_archived_text_or_reasoning(self):
        async def stream(_request):
            yield ConversationBound("callback-case")
            yield TextDelta("discarded")
            yield TextDelta("old thought", channel="analysis")
            yield ReplaceText("final")
            yield ReplaceText("canonical thought", channel="analysis")
            yield TextDelta(" answer")

        provider = Mock()
        provider.close = AsyncMock()
        provider.stream = stream
        self.accounts._providers[self.gemini] = provider
        calls = 0

        def failing_view(_event):
            nonlocal calls
            calls += 1
            raise RuntimeError("view closed during stream")

        self.accounts.send(self.gemini, None, "Question", [], failing_view, Mock())
        messages = self.accounts.cached(self.gemini, "callback-case")["messages"]

        self.assertEqual(calls, 1)
        self.assertEqual(
            [(message["role"], message["text"]) for message in messages],
            [
                ("user", "Question"),
                ("reasoning", "canonical thought"),
                ("assistant", "final answer"),
            ],
        )

    def test_local_history_failure_keeps_successful_send_and_conversation_identity(self):
        async def stream(_request):
            yield ConversationBound("archive-failure")
            yield ReplaceText("accepted response")

        provider = Mock()
        provider.close = AsyncMock()
        provider.stream = stream
        self.accounts._providers[self.gemini] = provider

        with patch.object(self.accounts, "cached", side_effect=RuntimeError("local read failed")):
            result = self.accounts.send(self.gemini, None, "Question", [], Mock(), Mock())

        self.assertEqual(result["conversation_id"], identity(self.gemini, "archive-failure"))
        self.assertEqual(result["cache_warning"], "Yanıt alındı ancak yerel arşive kaydedilemedi")

    def test_stop_cancels_a_cross_account_upload_before_chat_submission(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "source.txt"
            path.write_text("source", encoding="utf-8")
            upload_started, upload_cancelled, stream_started = Event(), Event(), Event()

            async def upload(_path):
                upload_started.set()
                try:
                    await asyncio.sleep(30)
                finally:
                    upload_cancelled.set()

            async def stream(_request):
                stream_started.set()
                yield ConversationBound("should-not-send")

            provider = Mock()
            provider.close = AsyncMock()
            provider.capabilities = frozenset({Capability.ATTACHMENTS, Capability.CHAT})
            provider.attachment_policy = lambda: {
                "max_files": 10,
                "max_bytes": 20 * 1024 * 1024,
            }
            provider.upload_file = upload
            provider.stream = stream
            self.accounts._providers[self.gemini] = provider
            connections = []

            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    self.accounts.send,
                    self.gemini,
                    None,
                    "Question",
                    [
                        {
                            "path": str(path),
                            "name": path.name,
                            "size_bytes": path.stat().st_size,
                            "source_account_key": "chatgpt/default",
                            "queue_id": "staged-file",
                        }
                    ],
                    Mock(),
                    connections.append,
                )
                self.assertTrue(upload_started.wait(2))
                self.assertEqual(len(connections), 1)
                connections[0].close()
                with self.assertRaises(CancelledError):
                    future.result(timeout=3)

            self.assertTrue(upload_cancelled.wait(1))
            self.assertFalse(stream_started.is_set())
            self.assertEqual(self.accounts._sending.get(self.gemini), 0)

    def test_cross_account_project_move_fails_before_provider_call(self):
        provider = Mock()
        self.accounts._providers[self.gemini] = provider
        with self.assertRaises(ValueError):
            self.accounts.callbacks(self.gemini)["move_conversation"](
                identity(self.gemini, "chat"), identity("chatgpt/default", "g-p-work")
            )
        provider.move_chat.assert_not_called()
