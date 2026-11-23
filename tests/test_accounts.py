"""Account identity, cache isolation and terminal routing without real sessions."""

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
from unittest.mock import Mock, patch

from webaiprovider import AccountPaused, RateLimited
from webaiprovider.collections import Page
from webaiprovider.resources.chat.types import Conversation
from webaiprovider.resources.message.protocol import (
    ConversationBound,
    ResponseComplete,
    TextDelta,
    TextReplacement,
)
from webaiprovider.resources.message.types import Content, MessageData
from webaiprovider.resources.model.model import ModelRef
from webaiprovider.resources.project.types import ProjectData

from accounts import Accounts, identity, origin
from tests.account_fixture import configured_application as create_application


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.api = create_application(Path(self.folder.name))
        self.addCleanup(self.api.runtime.close)
        self.accounts = self.api.get("accounts")
        self.gemini = self.accounts.add("gemini", "Personal Gemini")

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
            visible_accounts=lambda: account_items,
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
                "webaiprovider.providers.chatgpt.auth.BrowserSession.capture_auth",
                side_effect=AssertionError("browser"),
            ),
            patch("subprocess.Popen", side_effect=AssertionError("browser")),
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
            "remote-chat",
            "Memory check",
            (MessageData("m1", "assistant", (content,)),),
        )

        record = self.accounts.chat_record(self.gemini, chat)
        archived_content = record["messages"][0]["content"][0]

        self.assertEqual(archived_content, asdict(content))
        self.assertIs(archived_content["metadata"], nested_metadata)

    def test_begin_shutdown_cancels_active_provider_stream_promptly(self):
        started, cancelled = Event(), Event()

        def stream(_request):
            started.set()
            try:
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    with self.accounts._lock:
                        stop = (
                            all(
                                getattr(op, "_cancelled", False)
                                for op in self.accounts._active_operations
                            )
                            if self.accounts._active_operations
                            else True
                        )
                    if stop:
                        break
                    yield TextDelta("", "final")
                    time.sleep(0.01)
            finally:
                cancelled.set()
            yield TextDelta("unreachable")
            yield ResponseComplete()

        provider = self.accounts.provider(self.gemini)
        provider._opened = True
        provider._stream_provider_events = stream
        self.accounts._providers[self.gemini] = provider

        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(
                self.accounts.send, self.gemini, None, "Question", [], Mock(), Mock()
            )
            self.assertTrue(started.wait(1))
            self.accounts.begin_shutdown()
            with self.assertRaises(CancelledError):
                pending.result(timeout=2)

        self.assertTrue(cancelled.wait(1))
        self.assertEqual(self.accounts._sending.get(self.gemini), 0)

    def test_verification_survives_closed_browser_and_account_service_restart(self):
        self.accounts.credentials(self.gemini).save({"session": "verified test session"})
        self.accounts._remember_verification(self.gemini)
        restarted = Accounts(self.api)
        with patch.object(restarted, "browser_connected", return_value=False):
            state = restarted.status(self.gemini)
            self.assertTrue(state["authenticated"])
            self.assertEqual(state["transport"], "http")
        restarted.credentials(self.gemini).delete()
        self.assertFalse(restarted.status(self.gemini)["authenticated"])

    def test_transport_failure_preserves_verification_but_auth_rejection_invalidates_it(self):
        self.accounts.credentials(self.gemini).save({"session": "verified test session"})
        self.accounts._remember_verification(self.gemini)
        provider = self.accounts.provider(self.gemini)
        provider._opened = True
        provider.models = Mock(refresh=Mock(side_effect=RuntimeError("connection refused")))
        self.accounts._providers[self.gemini] = provider
        with self.assertRaisesRegex(RuntimeError, "connection refused"):
            self.accounts.models(self.gemini, force=True)
        self.assertTrue(self.accounts.status(self.gemini)["authenticated"])
        provider.models.refresh.side_effect = AccountPaused("Sign in required")
        with self.assertRaises(AccountPaused):
            self.accounts.models(self.gemini, force=True)
        self.assertFalse(self.accounts.status(self.gemini)["authenticated"])
        self.assertTrue(
            self.accounts.repository.get(self.gemini, "verification")["requires_verification"]
        )

    def test_saved_model_catalog_cannot_verify_an_account(self):
        self.accounts.credentials(self.gemini).save({"session": "unverified test session"})
        with patch.object(self.accounts, "browser_connected", return_value=True):
            state = self.accounts.status(self.gemini)
            self.assertFalse(state["authenticated"])
            self.assertEqual(state["transport"], "http")

    def test_catalog_single_flight_cache_and_account_local_disk_snapshot(self):
        refs = [ModelRef("gemini", "gemini-test", "Gemini")]

        def discover():
            time.sleep(0.04)
            return refs

        provider = self.accounts.provider(self.gemini)
        provider._opened = True
        provider.models = Mock(
            list=Mock(side_effect=discover),
            refresh=Mock(side_effect=discover),
        )
        self.accounts._providers[self.gemini] = provider
        with ThreadPoolExecutor(3) as pool:
            values = list(pool.map(lambda _: self.accounts.models(self.gemini), range(3)))
        self.assertEqual(provider.models.list.call_count, 1)
        self.assertEqual(values[0].chat_models[0].id, "gemini-test")
        self.assertTrue(all(v == values[0] for v in values))
        self.assertEqual(self.accounts.models(self.gemini), values[0])
        self.assertEqual(provider.models.list.call_count, 1)
        self.accounts.models(self.gemini, force=True)
        self.assertEqual(provider.models.refresh.call_count, 1)
        fresh = Accounts(self.api)
        self.assertEqual(fresh.cached_models(self.gemini), values[0])
        self.assertIsNone(fresh.cached_models("chatgpt/default"))

    def test_background_preparation_skips_accounts_without_credentials(self):
        item = {"key": self.gemini, "enabled": True}
        with (
            patch.object(self.accounts, "items", return_value=[item]),
            patch("accounts.service.session_exists", return_value=False),
            patch.object(self.accounts, "provider", side_effect=AssertionError("no session")),
        ):
            self.assertEqual(self.accounts.prepare_enabled(), {})

    def test_background_preparation_reports_each_account_without_raising(self):
        selected_key, failed_key = "chatgpt/default", "claude/default"
        items = [
            {"key": selected_key, "enabled": True},
            {"key": self.gemini, "enabled": True},
            {"key": failed_key, "enabled": True},
        ]
        self.accounts.set_active(selected_key, "active-chat")
        self.accounts.set_active(self.gemini, "other-chat")
        selected = Mock(opened=False, open=Mock(), models=Mock(list=Mock(return_value=[])))
        other = Mock(opened=False, open=Mock(), models=Mock(list=Mock(return_value=[])))
        failed = Mock(
            opened=False,
            open=Mock(side_effect=RuntimeError("offline")),
            models=Mock(list=Mock(return_value=[])),
        )
        providers = {selected_key: selected, self.gemini: other, failed_key: failed}
        self.accounts._providers.update(providers)
        with (
            patch.object(self.accounts, "items", return_value=items),
            patch("accounts.service.session_exists", return_value=True),
            patch.object(self.accounts, "prepare_chat") as prepare_chat,
        ):
            failures = self.accounts.prepare_enabled(selected_key=selected_key)
        self.assertEqual(failures, {failed_key: "offline"})
        selected.open.assert_called_once_with()
        selected.models.list.assert_called_once_with()
        prepare_chat.assert_called_once_with(selected_key, "active-chat")
        other.open.assert_called_once_with()
        other.models.list.assert_called_once_with()
        failed.models.list.assert_not_called()

    def test_failed_refresh_preserves_last_usable_model_catalog(self):
        refs = [ModelRef("gemini", "m", "Model")]
        provider = self.accounts.provider(self.gemini)
        provider._opened = True
        provider.models = Mock(
            list=Mock(return_value=refs),
            refresh=Mock(side_effect=RuntimeError("offline")),
        )
        self.accounts._providers[self.gemini] = provider
        good = self.accounts.models(self.gemini)
        with self.assertRaisesRegex(RuntimeError, "offline"):
            self.accounts.models(self.gemini, force=True)
        self.assertIs(self.accounts.cached_models(self.gemini), good)

    def test_failed_catalog_discovery_releases_lock_for_retry(self):
        provider = self.accounts.provider(self.gemini)
        provider._opened = True
        provider.models = Mock(list=Mock(side_effect=TimeoutError("timed out")))
        self.accounts._providers[self.gemini] = provider
        with self.assertRaises(TimeoutError):
            self.accounts.models(self.gemini)
        provider.models = Mock(list=Mock(return_value=[ModelRef("gemini", "m", "M")]))
        self.assertEqual(self.accounts.models(self.gemini).chat_models[0].id, "m")

    def test_history_timeout_isolated_from_other_account(self):
        self.accounts.status = lambda key: {"saved_snapshot": True}
        for key in (self.gemini, "chatgpt/default"):
            provider = self.accounts.provider(key)
            provider._opened = True
            provider._list_chats_data = (
                Mock(side_effect=TimeoutError("timed out"))
                if key == self.gemini
                else Mock(return_value=Page((Conversation("chat", "GPT"),)))
            )
            self.accounts._providers[key] = provider
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
        from tests.tui_support import terminal

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
        with patch("accounts.sessions.subprocess.run", return_value=Mock(returncode=0)) as launch:
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
        with patch("accounts.sessions.subprocess.run", return_value=Mock(returncode=1)):
            with self.assertRaisesRegex(RuntimeError, "Tarayıcı açılamadı"):
                self.accounts.session_action(self.gemini, "default_browser")

    def test_accounts_without_saved_sessions_are_reported_instead_of_silently_omitted(self):
        self.accounts.status = lambda _: {"saved_snapshot": False}
        for key in ("chatgpt/default", self.gemini):
            provider = self.accounts.provider(key)
            provider._opened = True
            provider._list_chats_data = Mock(side_effect=RuntimeError("No saved session"))
            provider._list_projects_data = Mock(side_effect=RuntimeError("No saved session"))
            self.accounts._providers[key] = provider
        for page in (self.accounts.list_chats(), self.accounts.all_projects()):
            self.assertEqual(set(page["failed_accounts"]), {"chatgpt/default", self.gemini})
            self.assertEqual(len(page["errors"]), 2)
            self.assertEqual(page["items"], [])

    def test_authoritative_unassigned_chat_clears_remembered_notebook_membership(self):
        linked = Conversation("c_chat", "Chat", project=ProjectData("notebooks/abcdef", ""))
        self.assertIsNotNone(self.accounts.chat_record(self.gemini, linked)["project_id"])
        plain = Conversation("c_chat", "Chat", metadata={"project_membership_known": True})
        self.assertIsNone(self.accounts.chat_record(self.gemini, plain)["project_id"])

    def test_mixed_list_filter_and_account_pagination(self):
        self.accounts.status = lambda _: {"saved_snapshot": True}
        for key in ("chatgpt/default", self.gemini):
            provider = self.accounts.provider(key)
            provider._opened = True
            provider._list_chats_data = Mock(
                return_value=Page(
                    (Conversation("same", key),),
                    "50" if key.startswith("chatgpt") else None,
                )
            )
            self.accounts._providers[key] = provider
        delivered = []
        page = self.accounts.list_chats(
            on_account_page=lambda account, value: delivered.append(
                (account["key"], value["items"])
            )
        )
        self.assertEqual(len({item["id"] for item in page["items"]}), 2)
        self.assertEqual({key for key, _ in delivered}, {"chatgpt/default", self.gemini})
        self.assertTrue(all(items for _, items in delivered))
        filtered = self.accounts.list_chats({"gemini"})
        self.assertEqual([item["provider_id"] for item in filtered["items"]], ["gemini"])
        self.accounts.list_chats(cursor=page["cursor"])
        self.accounts._providers["chatgpt/default"]._list_chats_data.assert_called_with(
            cursor="50", limit=50
        )

    def test_incremental_listing_preserves_new_pending_branches_in_final_result(self):
        known = {"id": "known", "remote_id": "known"}
        first = {"id": "first", "remote_id": "first"}
        later = {"id": "later", "remote_id": "later"}
        delivered = []
        with (
            patch.object(
                self.accounts, "read_record", return_value={"items": [known], "cursor": None}
            ),
            patch.object(
                self.accounts,
                "_pending_local_branches",
                side_effect=[[known, first], [known, first, later]],
            ),
        ):
            page = self.accounts.list_chats(
                account_keys=["chatgpt/default"],
                on_account_page=lambda _account, value: delivered.append(value["items"]),
            )
        self.assertEqual([item["id"] for item in delivered[0]], ["known", "first"])
        self.assertEqual([item["id"] for item in page["items"]], ["known", "first", "later"])

    def test_send_cache_and_temporary_mode_are_account_scoped(self):
        def stream(request):
            yield TextReplacement("Answer")
            yield ConversationBound("same")
            yield ResponseComplete()

        provider = self.accounts.provider(self.gemini)
        provider._opened = True
        provider._stream_provider_events = stream
        self.accounts._providers[self.gemini] = provider
        result = self.accounts.send(self.gemini, None, "Q", [], Mock(), Mock())
        self.assertEqual(origin(result["conversation_id"])[0], self.gemini)
        cached = self.accounts.cached(self.gemini, "same")
        self.assertEqual(cached["provider_id"], "gemini")
        self.assertIsNone(self.accounts.cached("chatgpt/default", "same"))
        self.accounts.send(self.gemini, None, "Private", [], Mock(), Mock(), temporary=True)
        # Temporary sends must not replace the durable archived conversation.
        archived = self.accounts.cached(self.gemini, "same")
        self.assertEqual(archived["messages"][0]["text"], "Q")
        self.assertNotIn("Private", str(archived))

    def test_rate_limit_cooldown_does_not_invalidate_account_verification(self):
        self.accounts.credentials(self.gemini).save({"session": "verified test session"})
        self.accounts._remember_verification(self.gemini)
        calls = 0

        def stream(_request):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RateLimited()
            yield TextReplacement("Answer")
            yield ConversationBound("same")
            yield ResponseComplete()

        provider = self.accounts.provider(self.gemini)
        provider._opened = True
        provider._stream_provider_events = stream
        self.accounts._providers[self.gemini] = provider

        with self.assertRaises(RateLimited):
            self.accounts.send(self.gemini, None, "Q", [], Mock(), Mock())
        self.assertTrue(self.accounts.status(self.gemini)["authenticated"])

        provider._stream_provider_events = Mock(side_effect=RateLimited())
        with self.assertRaises(RateLimited) as blocked:
            self.accounts.send(self.gemini, None, "Q", [], Mock(), Mock())
        self.assertEqual(blocked.exception.code, "rate_limited")
        self.assertTrue(self.accounts.status(self.gemini)["authenticated"])
        self.assertNotIn(self.gemini, self.accounts._verification_required)

    def test_view_callback_failure_does_not_truncate_archived_text_or_reasoning(self):
        def stream(_request):
            yield ConversationBound("callback-case")
            yield TextDelta("old thought", channel="analysis", part_id="thoughts")
            yield TextDelta("discarded", part_id="answer")
            yield TextReplacement("canonical thought", channel="analysis", part_id="thoughts")
            yield TextReplacement("final", part_id="answer")
            yield TextDelta(" answer", part_id="answer")
            yield ResponseComplete()

        provider = self.accounts.provider(self.gemini)
        provider._opened = True
        provider._stream_provider_events = stream
        self.accounts._providers[self.gemini] = provider
        calls = 0

        def failing_view(_event):
            nonlocal calls
            calls += 1
            raise RuntimeError("view closed during stream")

        with self.assertLogs("accounts", level="WARNING") as logs:
            self.accounts.send(self.gemini, None, "Question", [], failing_view, Mock())
            messages = self.accounts.cached(self.gemini, "callback-case")["messages"]

        self.assertIn("Detached failing stream observer", logs.output[0])
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
        def stream(_request):
            yield ConversationBound("archive-failure")
            yield TextReplacement("accepted response")
            yield ResponseComplete()

        provider = self.accounts.provider(self.gemini)
        provider._opened = True
        provider._stream_provider_events = stream
        self.accounts._providers[self.gemini] = provider

        with patch.object(
            self.accounts, "cached_window", side_effect=RuntimeError("local read failed")
        ):
            result = self.accounts.send(self.gemini, None, "Question", [], Mock(), Mock())

        self.assertEqual(result["conversation_id"], identity(self.gemini, "archive-failure"))
        self.assertEqual(result["cache_warning"], "Yanıt alındı ancak yerel geçmiş okunamadı")

    def test_stop_cancels_a_cross_account_upload_before_chat_submission(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "source.txt"
            path.write_text("source", encoding="utf-8")
            upload_started, upload_cancelled, stream_started = Event(), Event(), Event()
            stopped = Event()

            def upload(_path):
                upload_started.set()
                stopped.wait(3)
                upload_cancelled.set()
                return {"id": "uploaded"}

            def stream(_request):
                stream_started.set()
                yield ConversationBound("should-not-send")
                yield ResponseComplete()

            provider = self.accounts.provider(self.gemini)
            provider._opened = True
            provider.attachment_policy = lambda: {
                "max_files": 10,
                "max_bytes": 20 * 1024 * 1024,
            }
            provider.upload_file = upload
            provider._stream_provider_events = stream
            self.accounts._providers[self.gemini] = provider
            connections = []

            def capture_connection(connection):
                original_close = connection.close

                def close_and_wake():
                    original_close()
                    stopped.set()

                connection.close = close_and_wake
                connections.append(connection)

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
                    capture_connection,
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
        provider = self.accounts.provider(self.gemini)
        provider._opened = True
        provider._move_chat_data = Mock()
        self.accounts._providers[self.gemini] = provider
        with self.assertRaises(ValueError):
            self.accounts.callbacks(self.gemini)["move_conversation"](
                identity(self.gemini, "chat"), identity("chatgpt/default", "g-p-work")
            )
        provider._move_chat_data.assert_not_called()

    def test_models_without_saved_session_does_not_poison_verification_required(self):
        from webaiprovider import AccountPaused

        with self.assertRaises(AccountPaused):
            self.accounts.models(self.gemini)
        self.assertNotIn(self.gemini, self.accounts._verification_required)
        self.assertIsNone(self.accounts.repository.get(self.gemini, "verification"))
        self.assertTrue(self.accounts.account_is_visible(self.gemini))

    def test_prepare_chat_deduplicates_concurrent_calls_and_skips_while_sending(self):
        import threading

        entered = threading.Event()
        release = threading.Event()
        calls = 0

        class FakeChat:
            remote_id = "c-1"
            temporary = False

            def prepare(self):
                nonlocal calls
                calls += 1
                entered.set()
                release.wait(2)

        provider = self.accounts.provider(self.gemini)
        provider._opened = True
        self.accounts._providers[self.gemini] = provider
        self.accounts._chats[self.gemini, "c-1"] = FakeChat()
        setattr(self.accounts._chats[self.gemini, "c-1"], "provider", provider)

        t1 = threading.Thread(
            target=self.accounts.prepare_chat,
            args=(self.gemini, identity(self.gemini, "c-1")),
        )
        t1.start()
        self.assertTrue(entered.wait(2))
        self.accounts.prepare_chat(self.gemini, identity(self.gemini, "c-1"))
        release.set()
        t1.join(timeout=2)
        self.assertEqual(calls, 1)

        with self.accounts._lock:
            self.accounts._sending[self.gemini] = 1
        try:
            self.accounts.prepare_chat(self.gemini, identity(self.gemini, "c-1"))
        finally:
            with self.accounts._lock:
                self.accounts._sending[self.gemini] = 0
        self.assertEqual(calls, 1)

    def test_schedule_chat_prepare_deduplicates_typing_and_retriggers_on_project_change(self):
        from tui.chat.view import ChatView

        prepared = []
        service = Mock()
        service.active.return_value = self.gemini
        service.active_accounts.return_value = [{"key": self.gemini}]
        service.archive.return_value = None
        service.cached_models.return_value = None
        service.capabilities.return_value = frozenset()
        service.callbacks.return_value = {
            "load_conversation": Mock(),
            "send_prompt": Mock(),
        }
        service.status.return_value = {"authenticated": True, "saved_snapshot": True}
        service.preference.return_value = {}
        service.prepare_chat = lambda key, cid=None, *, project_id=None: prepared.append(
            (key, cid, project_id)
        )
        view = ChatView(
            lambda: [],
            Mock(),
            Mock(),
            account_service=service,
        )
        view.app = SimpleNamespace(is_running=True, invalidate=lambda: None)
        view._read_in_background = lambda read, ok, err, **kw: ok(read())
        try:
            view.input.text = "M"
            view.input.text = "Me"
            view.input.text = "Mer"
            self.assertEqual(len(prepared), 1)
            self.assertEqual(prepared[0], (self.gemini, None, None))

            view.current_project = {"id": identity(self.gemini, "p-1"), "name": "Proje"}
            view._save_active_tab()
            view._schedule_chat_prepare()
            self.assertEqual(len(prepared), 2)
            self.assertEqual(prepared[1], (self.gemini, None, identity(self.gemini, "p-1")))
        finally:
            view.close_background_work(wait=True)
